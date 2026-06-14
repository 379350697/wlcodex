"""Claude Code subprocess adapter and event normalization.

Calls the Claude CLI binary with -p (--print) for non-interactive output.
Sets cwd to the selected workspace, captures stdout/stderr, and returns
exit status and summary. Avoids shell=True.

All Claude subprocesses run with a sanitized environment: Telegram
delivery secrets and direct-messaging credentials are stripped so
Claude cannot bypass the platform controller for Telegram delivery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from wlcodex.agent_backend import AgentRequest, AgentResult, AgentStreamEvent
from wlcodex.claude_binary import (
    ClaudeCliCapabilities,
    probe_claude_capabilities,
    sanitized_claude_env,
)
from wlcodex.claude_permissions import (
    ClaudePermissionState,
    normalize_claude_permission_mode,
)
from wlcodex.runtime_events import EventType
from wlcodex.claude_stream_parser import ClaudeStreamEvent, parse_line

logger = logging.getLogger(__name__)

# Environment variable names (exact or prefix) stripped from Claude
# subprocess environments.  Claude must never see Telegram delivery
# credentials or any direct-messaging secrets.
def _sanitized_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with Telegram delivery secrets removed."""
    return sanitized_claude_env()


@dataclass
class ClaudeConfig:
    enabled: bool = False
    binary: str = "auto"
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 3600.0
    stream_idle_timeout_seconds: float = 600.0
    stream_drain_grace_seconds: float = 0.1
    permission_mode: str = "acceptEdits"
    model: str = "deepseek-v4-pro"
    effort: str = "max"
    binary_resolution_error: str = ""


class ClaudeBackend:
    def __init__(
        self,
        config: ClaudeConfig,
        permission_state: ClaudePermissionState | None = None,
        runtime_source: object | None = None,
    ) -> None:
        self._config = config
        self._permission_state = permission_state or ClaudePermissionState(
            config.permission_mode
        )
        self._runtime_source = runtime_source
        self._cli_capabilities: ClaudeCliCapabilities | None = None
        self._hook_events_supported: bool | None = None
        self._missing_capabilities_emitted: set[str] = set()
        self._last_session_id: str = ""

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def permission_mode(self) -> str:
        return self._permission_state.get()

    def set_runtime_source(self, runtime_source: object | None) -> None:
        """Set per-run runtime event source used by streaming calls."""
        self._runtime_source = runtime_source

    def set_permission_mode(self, mode: str) -> str:
        return self._permission_state.set(mode)

    async def send(self, request: AgentRequest) -> AgentResult:
        if not self._config.enabled:
            return AgentResult(text="Claude Code is not enabled.", exit_code=1)

        if self._config.binary_resolution_error:
            return AgentResult(
                text=self._binary_not_found_message(),
                exit_code=1,
            )

        capabilities = await self._probe_cli_capabilities()
        if capabilities.probe_error == "binary_not_found":
            return AgentResult(
                text=self._binary_not_found_message(),
                exit_code=1,
            )
        if request.workspace_path and not Path(request.workspace_path).is_dir():
            return AgentResult(
                text=_workspace_not_found_message(request.workspace_path),
                exit_code=1,
            )
        resume_session_id = str(request.extra.get("resume_session_id", "") or "")
        session_id = str(request.extra.get("session_id", "") or "")

        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *self._prompt_args(
                    request.prompt,
                    resume_session_id=resume_session_id,
                    session_id=session_id,
                ),
                cwd=request.workspace_path or None,
                env=_sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            async def _read() -> tuple[str, str]:
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=self._config.request_timeout_seconds,
                    )
                    return (
                        stdout.decode("utf-8", errors="replace") if stdout else "",
                        stderr.decode("utf-8", errors="replace") if stderr else "",
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise

            stdout, stderr = await _read()
            text = stdout or stderr or "(no output)"
            exit_code = proc.returncode or 0
            if _looks_like_permission_request(text):
                text = _permission_error_message(text)
                exit_code = 1

            return AgentResult(
                text=text,
                exit_code=exit_code,
                token_input=len(request.prompt) // 4,
                token_output=len(text) // 4,
                session_id=resume_session_id or self._last_session_id,
            )

        except FileNotFoundError:
            return AgentResult(
                text=self._binary_not_found_message(),
                exit_code=1,
            )
        except Exception as exc:
            logger.exception("Claude backend error")
            return AgentResult(
                text=f"Claude backend error: {exc}",
                exit_code=1,
            )

    async def send_streaming(self, request: AgentRequest):
        if not self._config.enabled:
            yield AgentStreamEvent(delta="Claude Code is not enabled.", event_type="error")
            return

        if self._config.binary_resolution_error:
            _emit_runtime_lifecycle(
                self._runtime_source,
                EventType.AGENT_RUN_FAILED,
                payload={"reason": "binary_not_found", "binary": self._config.binary},
            )
            yield AgentStreamEvent(
                delta=self._binary_not_found_message(),
                event_type="error",
            )
            return

        capabilities = await self._probe_cli_capabilities()
        if capabilities.probe_error == "binary_not_found":
            _emit_runtime_lifecycle(
                self._runtime_source,
                EventType.AGENT_RUN_FAILED,
                payload={"reason": "binary_not_found", "binary": self._config.binary},
            )
            yield AgentStreamEvent(
                delta=self._binary_not_found_message(),
                event_type="error",
            )
            return
        if request.workspace_path and not Path(request.workspace_path).is_dir():
            _emit_runtime_lifecycle(
                self._runtime_source,
                EventType.AGENT_RUN_FAILED,
                payload={
                    "reason": "workspace_not_found",
                    "workspace_path": request.workspace_path,
                },
            )
            yield AgentStreamEvent(
                delta=_workspace_not_found_message(request.workspace_path),
                event_type="error",
            )
            return
        resume_session_id = str(request.extra.get("resume_session_id", "") or "")
        session_id = str(request.extra.get("session_id", "") or "")

        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *self._prompt_args(
                    request.prompt,
                    stream_json=True,
                    resume_session_id=resume_session_id,
                    session_id=session_id,
                ),
                cwd=request.workspace_path or None,
                env=_sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024 * 16,
                start_new_session=True,
            )

            if proc.stdout is None:
                _emit_runtime_lifecycle(
                    self._runtime_source,
                    EventType.AGENT_RUN_FAILED,
                    payload={"reason": "no_stdout"},
                )
                yield AgentStreamEvent(delta="(no output)", event_type="error")
                return

            _emit_runtime_lifecycle(self._runtime_source, EventType.AGENT_RUN_STARTED)

            accumulated: list[str] = []
            assistant_text = ""
            reader_task: asyncio.Task[bytes] | None = None
            wait_task: asyncio.Task[int] | None = None
            timeout_reason = ""
            try:
                loop = asyncio.get_running_loop()
                started_at = loop.time()
                last_activity_at = started_at
                hard_timeout = self._config.request_timeout_seconds
                idle_timeout = self._config.stream_idle_timeout_seconds
                reader_task = asyncio.create_task(proc.stdout.readline())
                wait_task = asyncio.create_task(proc.wait())
                process_exited = False
                stdout_eof = False

                while True:
                    now = loop.time()
                    if hard_timeout > 0 and now - started_at >= hard_timeout:
                        timeout_reason = "hard"
                        break
                    if idle_timeout > 0 and now - last_activity_at >= idle_timeout:
                        timeout_reason = "idle"
                        break

                    if _process_has_exited(proc):
                        process_exited = True

                    done: set[asyncio.Task[object]] = set()
                    wait_items: set[asyncio.Task[object]] = set()
                    if reader_task is not None and reader_task.done():
                        done.add(reader_task)
                    elif reader_task is not None:
                        wait_items.add(reader_task)
                    if wait_task is not None and wait_task.done():
                        done.add(wait_task)
                    elif wait_task is not None:
                        wait_items.add(wait_task)

                    if not done and not wait_items:
                        break

                    if not done:
                        wait_timeout = self._config.stream_drain_grace_seconds
                        if hard_timeout > 0:
                            wait_timeout = min(
                                wait_timeout,
                                max(0.0, hard_timeout - (now - started_at)),
                            )
                        if idle_timeout > 0:
                            wait_timeout = min(
                                wait_timeout,
                                max(0.0, idle_timeout - (now - last_activity_at)),
                            )
                        done, _pending = await asyncio.wait(
                            wait_items,
                            timeout=wait_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            if process_exited or _process_has_exited(proc):
                                process_exited = True
                                continue
                            continue

                    if wait_task is not None and wait_task in done:
                        process_exited = True
                        wait_task = None

                    if reader_task is not None and reader_task in done:
                        line = reader_task.result()
                        if not line:
                            stdout_eof = True
                            reader_task = None
                        else:
                            last_activity_at = loop.time()
                            decoded = line.decode("utf-8", errors="replace")
                            parsed_events, assistant_text = parse_line(
                                decoded,
                                assistant_text=assistant_text,
                                has_emitted_text=bool(accumulated),
                            )
                            for parsed in parsed_events:
                                # Emit to runtime store when wired.
                                _emit_runtime(self._runtime_source, parsed)
                                # Capture session_id from result/system events
                                if parsed.session_id:
                                    self._last_session_id = parsed.session_id
                                # Yield AgentStreamEvent for backward compat.
                                agent_event = _to_agent_stream_event(parsed)
                                if agent_event is None:
                                    continue
                                if agent_event.event_type == "text" and agent_event.delta:
                                    accumulated.append(agent_event.delta)
                                    current_text = "".join(accumulated)
                                    if _looks_like_permission_request(current_text):
                                        await _kill_process(proc)
                                        yield AgentStreamEvent(
                                            delta=_permission_error_message(current_text),
                                            event_type="error",
                                        )
                                        return
                                yield agent_event
                            reader_task = asyncio.create_task(proc.stdout.readline())

                    if process_exited and stdout_eof:
                        break

                if timeout_reason:
                    await _kill_process(proc)
                    if timeout_reason == "idle":
                        _emit_runtime_lifecycle(
                            self._runtime_source,
                            EventType.WATCHDOG_IDLE_TIMEOUT,
                            payload={
                                "idle_seconds": self._config.stream_idle_timeout_seconds,
                                "elapsed_total_seconds": round(loop.time() - started_at, 3),
                            },
                        )
                        _emit_runtime_lifecycle(
                            self._runtime_source,
                            EventType.AGENT_RUN_TIMED_OUT,
                            payload={"reason": "idle_timeout"},
                        )
                        yield AgentStreamEvent(
                            delta=(
                                "Claude Code 运行超时："
                                f"超过 {self._config.stream_idle_timeout_seconds:g} "
                                "秒没有新的输出。"
                            ),
                            event_type="error",
                        )
                    else:
                        _emit_runtime_lifecycle(
                            self._runtime_source,
                            EventType.WATCHDOG_HARD_TIMEOUT,
                            payload={
                                "hard_seconds": self._config.request_timeout_seconds,
                                "elapsed_total_seconds": round(loop.time() - started_at, 3),
                            },
                        )
                        _emit_runtime_lifecycle(
                            self._runtime_source,
                            EventType.AGENT_RUN_TIMED_OUT,
                            payload={"reason": "hard_timeout"},
                        )
                        yield AgentStreamEvent(
                            delta=(
                                "Claude Code 运行超时："
                                f"超过 {self._config.request_timeout_seconds:g} 秒未完成。"
                            ),
                            event_type="error",
                        )
                    return
            except TimeoutError:
                await _kill_process(proc)
                _emit_runtime_lifecycle(
                    self._runtime_source,
                    EventType.WATCHDOG_HARD_TIMEOUT,
                    payload={
                        "hard_seconds": self._config.request_timeout_seconds,
                    },
                )
                _emit_runtime_lifecycle(
                    self._runtime_source,
                    EventType.AGENT_RUN_TIMED_OUT,
                    payload={"reason": "asyncio_timeout_error"},
                )
                yield AgentStreamEvent(
                    delta=(
                        "Claude Code 运行超时："
                        f"超过 {self._config.request_timeout_seconds:g} 秒未完成。"
                    ),
                    event_type="error",
                )
                return
            finally:
                if reader_task is not None and not reader_task.done():
                    reader_task.cancel()
                if wait_task is not None and not wait_task.done():
                    if _process_has_exited(proc):
                        _close_subprocess_transport(proc)
                    wait_task.cancel()

            text = "".join(accumulated)
            if _looks_like_permission_request(text):
                _emit_runtime_lifecycle(
                    self._runtime_source,
                    EventType.AGENT_RUN_FAILED,
                    payload={"reason": "permission_request"},
                )
                yield AgentStreamEvent(
                    delta=_permission_error_message(text),
                    event_type="error",
                )
                return
            if (proc.returncode or 0) != 0:
                _emit_runtime_lifecycle(
                    self._runtime_source,
                    EventType.AGENT_RUN_FAILED,
                    payload={
                        "reason": "non_zero_exit",
                        "exit_code": proc.returncode,
                    },
                )
                yield AgentStreamEvent(
                    delta=text or f"Claude Code exited with status {proc.returncode}.",
                    event_type="error",
                )
                return
            _emit_runtime_lifecycle(self._runtime_source, EventType.AGENT_RUN_COMPLETED)

        except FileNotFoundError:
            _emit_runtime_lifecycle(
                self._runtime_source,
                EventType.AGENT_RUN_FAILED,
                payload={
                    "reason": "binary_not_found",
                    "binary": self._config.binary,
                },
            )
            yield AgentStreamEvent(
                delta=self._binary_not_found_message(),
                event_type="error",
            )
        except Exception as exc:
            logger.exception("Claude streaming error")
            _emit_runtime_lifecycle(
                self._runtime_source,
                EventType.AGENT_RUN_FAILED,
                payload={"reason": "exception", "error": str(exc)[:2000]},
            )
            yield AgentStreamEvent(delta=f"Error: {exc}", event_type="error")

    def interrupt(self, session_id: str | None = None) -> None:
        # Claude subprocess termination is managed at the process level
        pass

    async def send_terminal_input(self, session_id: str, text: str) -> AgentResult:
        """Send raw input text to a Claude session for terminal surface.

        Uses ``claude --resume <session_id> -p <text>`` to continue the
        existing conversation context.  Returns the full text result so
        the terminal surface can display it as a frame.

        Raises ValueError when *session_id* is empty — callers must
        ensure a real session exists before calling this method.
        """
        if not session_id:
            raise ValueError(
                "Cannot send terminal input without a Claude session_id. "
                "Run a Claude task first to create a session, then attach "
                "via /terminal claude."
            )
        if not self._config.enabled:
            raise RuntimeError("Claude backend is not enabled.")
        if self._config.binary_resolution_error:
            raise RuntimeError(self._binary_not_found_message())

        capabilities = await self._probe_cli_capabilities()
        if capabilities.probe_error == "binary_not_found":
            raise RuntimeError(self._binary_not_found_message())
        if not capabilities.resume:
            raise RuntimeError(
                "Claude CLI does not support --resume; update Claude Code or run a new Claude task first."
            )

        resume_args = ["--resume", session_id, "-p", text]
        if capabilities.output_format and capabilities.stream_json_output:
            resume_args.extend(["--output-format", "stream-json", "--verbose"])
            if capabilities.include_partial_messages:
                resume_args.append("--include-partial-messages")
        if capabilities.permission_mode:
            resume_args.extend([
                "--permission-mode",
                normalize_claude_permission_mode(self.permission_mode),
            ])
        if self._config.model and capabilities.model:
            resume_args.extend(["--model", normalize_claude_model_name(self._config.model)])
        if self._config.effort and capabilities.effort:
            resume_args.extend(["--effort", self._config.effort])

        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *resume_args,
                cwd=None,
                env=_sanitized_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024 * 16,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise RuntimeError(f"Claude binary not found: {self._config.binary}") from None

        accumulated: list[str] = []
        try:
            if proc.stdout is not None:
                async for line in proc.stdout:
                    decoded = line.decode("utf-8", errors="replace")
                    parsed_events, _ = parse_line(decoded)
                    for parsed in parsed_events:
                        if parsed.session_id:
                            self._last_session_id = parsed.session_id
                        if parsed.agent_delta:
                            accumulated.append(parsed.agent_delta)
            await proc.wait()
        except Exception:
            await _kill_process(proc)
            raise

        text_output = "".join(accumulated)
        return AgentResult(
            text=text_output or "(no output)",
            exit_code=proc.returncode or 0,
            session_id=self._last_session_id,
        )

    def health(self) -> object:
        return _ClaudeHealth(self._config.enabled, self._config.binary)

    def _prompt_args(
        self,
        prompt: str,
        *,
        stream_json: bool = False,
        resume_session_id: str = "",
        session_id: str = "",
    ) -> list[str]:
        capabilities = self._capabilities_for_args()
        if resume_session_id:
            args = ["--resume", resume_session_id, "-p", prompt]
        elif session_id and capabilities.session_id:
            args = ["--session-id", session_id, "-p", prompt]
        else:
            args = ["-p", prompt]
        if capabilities.permission_mode:
            args.extend([
                "--permission-mode",
                normalize_claude_permission_mode(self.permission_mode),
            ])
        if self._config.model and capabilities.model:
            args.extend(["--model", normalize_claude_model_name(self._config.model)])
        if self._config.effort and capabilities.effort:
            args.extend(["--effort", self._config.effort])
        if stream_json and capabilities.output_format and capabilities.stream_json_output:
            args.extend([
                "--output-format",
                "stream-json",
                "--verbose",
            ])
            if capabilities.include_partial_messages:
                args.append("--include-partial-messages")
            if self._hook_events_supported and capabilities.include_hook_events:
                args.append("--include-hook-events")
        return args

    async def _probe_cli_capabilities(self) -> ClaudeCliCapabilities:
        if self._cli_capabilities is None:
            self._cli_capabilities = await probe_claude_capabilities(
                self._config.binary,
            )
            self._hook_events_supported = self._cli_capabilities.include_hook_events
        if not self._cli_capabilities.include_hook_events:
            self._emit_capability_missing_once("include-hook-events")
        return self._cli_capabilities

    def _capabilities_for_args(self) -> ClaudeCliCapabilities:
        return self._cli_capabilities or ClaudeCliCapabilities(
            print_prompt=True,
            output_format=True,
            stream_json_output=True,
            include_partial_messages=True,
            include_hook_events=bool(self._hook_events_supported),
            input_stream_json=True,
            permission_mode=True,
            model=True,
            effort=True,
            resume=True,
            session_id=True,
        )

    async def _probe_hook_events(self) -> bool:
        if self._hook_events_supported is not None:
            if not self._hook_events_supported:
                self._emit_capability_missing_once("include-hook-events")
            return self._hook_events_supported

        capabilities = await self._probe_cli_capabilities()
        supported = capabilities.include_hook_events
        self._hook_events_supported = supported

        if not supported:
            self._emit_capability_missing_once("include-hook-events")

        return supported

    def _emit_capability_missing_once(self, capability: str) -> None:
        if capability in self._missing_capabilities_emitted:
            return
        if self._runtime_source is None:
            return
        try:
            from wlcodex.claude_runtime_source import ClaudeRuntimeSource
            if isinstance(self._runtime_source, ClaudeRuntimeSource):
                self._runtime_source.emit_capability_missing(capability)
                self._missing_capabilities_emitted.add(capability)
        except Exception:
            logger.debug("Failed to emit capability missing event", exc_info=True)

    def _binary_not_found_message(self) -> str:
        if self._config.binary_resolution_error:
            return self._config.binary_resolution_error
        return (
            f"Claude binary not found: {self._config.binary}\n"
            "Set WLCODEX_CLAUDE_BINARY or install Claude Code CLI."
        )


def _workspace_not_found_message(workspace_path: str) -> str:
    return (
        f"Workspace path not found: {workspace_path}\n"
        "Update the selected workspace path before starting Claude Code."
    )


def _emit_runtime(runtime_source: object | None, parsed: ClaudeStreamEvent) -> None:
    """Emit *parsed* to the runtime event store when a source is wired."""
    if runtime_source is None:
        return
    try:
        from wlcodex.claude_runtime_source import ClaudeRuntimeSource
        if isinstance(runtime_source, ClaudeRuntimeSource):
            runtime_source.emit(parsed)
    except Exception:
        logger.debug("Runtime event emission failed", exc_info=True)


def _emit_runtime_lifecycle(
    runtime_source: object | None,
    event_type: str,
    *,
    payload: dict | None = None,
    visibility: str = "internal",
) -> None:
    """Emit a lifecycle event through *runtime_source* when wired."""
    if runtime_source is None:
        return
    try:
        from wlcodex.claude_runtime_source import ClaudeRuntimeSource
        if isinstance(runtime_source, ClaudeRuntimeSource):
            runtime_source.emit_lifecycle(
                event_type,
                payload=payload or {},
                visibility=visibility,
            )
    except Exception:
        logger.debug("Runtime lifecycle emission failed", exc_info=True)


def _to_agent_stream_event(parsed: ClaudeStreamEvent) -> AgentStreamEvent | None:
    """Convert a ``ClaudeStreamEvent`` to ``AgentStreamEvent`` for backward compat.

    Returns ``None`` for pure-activity events that carry no visible text or
    usage data — those should not produce user-facing stream output.
    """
    if parsed.agent_delta or parsed.agent_event_type == "error":
        return AgentStreamEvent(
            delta=parsed.agent_delta,
            event_type=parsed.agent_event_type,
            session_id=parsed.session_id,
        )
    if parsed.agent_usage is not None:
        return AgentStreamEvent(
            delta="",
            event_type="usage",
            usage=parsed.agent_usage,
            session_id=parsed.session_id,
        )
    return None


def _claude_stream_events_from_line(
    line: str,
    assistant_text: str,
    has_emitted_text: bool,
) -> tuple[list[AgentStreamEvent], str]:
    stripped = line.strip()
    if not stripped:
        return [], assistant_text
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return [AgentStreamEvent(delta=line, event_type="text")], assistant_text
    if not isinstance(payload, dict):
        return [], assistant_text

    event_type = str(payload.get("type") or "")
    if event_type == "stream_event":
        text = _extract_stream_delta_text(payload)
        if not text:
            return [], assistant_text
        return [AgentStreamEvent(delta=text, event_type="text")], assistant_text + text

    if event_type == "assistant":
        current_text = _extract_assistant_text(payload)
        if not current_text:
            return [], assistant_text
        if current_text.startswith(assistant_text):
            delta = current_text[len(assistant_text):]
            assistant_text = current_text
        else:
            delta = current_text
            assistant_text += delta
        if not delta:
            return [], assistant_text
        return [AgentStreamEvent(delta=delta, event_type="text")], assistant_text

    if event_type == "result":
        subtype = str(payload.get("subtype") or "")
        error_text = str(payload.get("error") or payload.get("message") or "")
        if subtype and subtype not in {"success", "done"}:
            return [
                AgentStreamEvent(
                    delta=error_text or f"Claude Code result status: {subtype}",
                    event_type="error",
                )
            ], assistant_text
        result_text = str(payload.get("result") or "")
        events: list[AgentStreamEvent] = []
        if result_text and not has_emitted_text:
            events.append(AgentStreamEvent(delta=result_text, event_type="text"))
        # Extract usage info if present
        usage = extract_claude_usage_from_result(payload)
        if usage:
            model = extract_claude_model_from_result(payload)
            if model:
                usage["model"] = model
            events.append(AgentStreamEvent(delta="", event_type="usage", usage=usage))
        return events, assistant_text

    if event_type in {"error", "api_error"}:
        text = str(payload.get("message") or payload.get("error") or payload)
        return [AgentStreamEvent(delta=text, event_type="error")], assistant_text

    return [], assistant_text


def _extract_stream_delta_text(payload: dict[str, object]) -> str:
    event = payload.get("event")
    if not isinstance(event, dict):
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return ""
    if delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _extract_assistant_text(payload: dict[str, object]) -> str:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


@dataclass
class _ClaudeHealth:
    enabled: bool
    binary: str

    @property
    def is_healthy(self) -> bool:
        if not self.enabled:
            return True  # Disabled is not unhealthy
        return Path(self.binary).exists() or _is_on_path(self.binary)


def _is_on_path(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def normalize_claude_model_name(model: str) -> str:
    normalized = model.strip()
    aliases = {
        "deepseek4pro": "deepseek-v4-pro",
        "deepseek-v4pro": "deepseek-v4-pro",
        "deepseek4-pro": "deepseek-v4-pro",
        "deepseek4flash": "deepseek-v4-flash",
        "deepseek-v4flash": "deepseek-v4-flash",
        "deepseek4-flash": "deepseek-v4-flash",
    }
    return aliases.get(normalized.lower(), normalized)


def _looks_like_permission_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "需要你的批准",
        "需要批准",
        "需要权限",
        "requires approval",
        "need your approval",
        "permission required",
        "waiting for approval",
    )
    return any(marker in lowered for marker in markers)


def _permission_error_message(raw_text: str) -> str:
    cleaned = " ".join(raw_text.split())
    return (
        "Claude Code 仍在等待交互式权限确认，当前实现未执行完成。\n"
        "请用 /claude_mode 选择“允许编辑”“自动模式”或更高权限后重试。\n\n"
        f"原始输出：{cleaned[:500]}"
    )


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    await _kill_process_group(proc)
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _process_has_exited(proc: asyncio.subprocess.Process) -> bool:
    if proc.returncode is not None:
        return True
    pid = getattr(proc, "pid", None)
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        state = stat.rsplit(") ", 1)[1].split(maxsplit=1)[0]
    except IndexError:
        return False
    return state == "Z"


def _close_subprocess_transport(proc: asyncio.subprocess.Process) -> None:
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:
        logger.debug("Failed to close subprocess transport", exc_info=True)


def record_claude_usage_event(
    ledger: object,
    *,
    prompt: str,
    output_text: str,
    conversation_id: int | None = None,
    orchestration_run_id: int | None = None,
    agent_run_id: int | None = None,
    task_id: int | None = None,
    role: str = "implementation",
    phase: str = "",
    model: str = "",
    usage: dict | None = None,
    latency_ms: int = 0,
    status: str = "completed",
) -> None:
    """Record a Claude usage event. Uses exact data from usage dict if available,
    otherwise falls back to approx_tokens() estimation.

    Does NOT raise — recording failure must not affect Claude running.
    """
    try:
        from wlcodex.context_packets import approx_tokens

        if usage and usage.get("source") == "exact":
            source = "exact"
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            cached_input_tokens = int(usage.get("cached_input_tokens", 0))
            reasoning_output_tokens = 0
            if not model:
                model = str(usage.get("model", ""))
        else:
            source = "estimated"
            input_tokens = approx_tokens(prompt)
            output_tokens = approx_tokens(output_text)
            cached_input_tokens = 0
            reasoning_output_tokens = 0

        if input_tokens == 0 and output_tokens == 0:
            return

        metadata: dict = {}
        if usage and usage.get("source") != "exact":
            metadata["raw_usage"] = usage

        ledger.record_usage_event(
            agent="claude",
            role=role,
            phase=phase,
            request_kind="send",
            request_index=1,
            model=model,
            source=source,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            latency_ms=latency_ms,
            status=status,
            conversation_id=conversation_id,
            orchestration_run_id=orchestration_run_id,
            agent_run_id=agent_run_id,
            task_id=task_id,
        )
    except Exception:
        pass  # Recording failure must not affect Claude running


def extract_claude_usage_from_result(payload: dict) -> dict | None:
    """Extract Claude usage info from a stream-json result event.

    Returns a dict with input_tokens, output_tokens, cached_input_tokens,
    total_tokens if usage is present, or None.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict = {}
    for src_key, dst_key in [
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cached_input_tokens"),
        ("cache_creation_input_tokens", "cached_input_tokens"),
    ]:
        val = usage.get(src_key)
        if isinstance(val, (int, float)):
            result[dst_key] = result.get(dst_key, 0) + int(val)
    if "input_tokens" in result and "output_tokens" in result:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
        result["source"] = "exact"
        return result
    return None


def extract_claude_model_from_result(payload: dict) -> str:
    """Extract model name from a Claude stream-json result event."""
    model = payload.get("model")
    if isinstance(model, str):
        return model
    return ""
