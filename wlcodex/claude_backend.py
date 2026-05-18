"""Claude Code subprocess adapter and event normalization.

Calls the Claude CLI binary with -p (--print) for non-interactive output.
Sets cwd to the selected workspace, captures stdout/stderr, and returns
exit status and summary. Avoids shell=True.
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
from wlcodex.claude_permissions import (
    ClaudePermissionState,
    normalize_claude_permission_mode,
)

logger = logging.getLogger(__name__)


@dataclass
class ClaudeConfig:
    enabled: bool = False
    binary: str = "claude"
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 3600.0
    stream_idle_timeout_seconds: float = 600.0
    stream_drain_grace_seconds: float = 0.1
    permission_mode: str = "acceptEdits"
    model: str = "deepseek-v4-pro"
    effort: str = "max"


class ClaudeBackend:
    def __init__(
        self,
        config: ClaudeConfig,
        permission_state: ClaudePermissionState | None = None,
    ) -> None:
        self._config = config
        self._permission_state = permission_state or ClaudePermissionState(
            config.permission_mode
        )

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def permission_mode(self) -> str:
        return self._permission_state.get()

    def set_permission_mode(self, mode: str) -> str:
        return self._permission_state.set(mode)

    async def send(self, request: AgentRequest) -> AgentResult:
        if not self._config.enabled:
            return AgentResult(text="Claude Code is not enabled.", exit_code=1)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *self._prompt_args(request.prompt),
                cwd=request.workspace_path or None,
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
            )

        except FileNotFoundError:
            return AgentResult(
                text=f"Claude binary not found: {self._config.binary}",
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

        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *self._prompt_args(request.prompt, stream_json=True),
                cwd=request.workspace_path or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024 * 16,
                start_new_session=True,
            )

            if proc.stdout is None:
                yield AgentStreamEvent(delta="(no output)", event_type="error")
                return

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
                                await _kill_process_group(proc)
                                break
                            continue

                    if wait_task is not None and wait_task in done:
                        process_exited = True

                    if reader_task is not None and reader_task in done:
                        line = reader_task.result()
                        if not line:
                            stdout_eof = True
                            reader_task = None
                        else:
                            last_activity_at = loop.time()
                            decoded = line.decode("utf-8", errors="replace")
                            events, assistant_text = _claude_stream_events_from_line(
                                decoded,
                                assistant_text,
                                bool(accumulated),
                            )
                            if not events:
                                reader_task = asyncio.create_task(proc.stdout.readline())
                                continue
                            for event in events:
                                if event.event_type == "text":
                                    accumulated.append(event.delta)
                                    current_text = "".join(accumulated)
                                    if _looks_like_permission_request(current_text):
                                        await _kill_process(proc)
                                        yield AgentStreamEvent(
                                            delta=_permission_error_message(current_text),
                                            event_type="error",
                                        )
                                        return
                                yield event
                            reader_task = asyncio.create_task(proc.stdout.readline())

                    if process_exited and stdout_eof:
                        break

                if timeout_reason:
                    await _kill_process(proc)
                    if timeout_reason == "idle":
                        yield AgentStreamEvent(
                            delta=(
                                "Claude Code 运行超时："
                                f"超过 {self._config.stream_idle_timeout_seconds:g} "
                                "秒没有新的输出。"
                            ),
                            event_type="error",
                        )
                    else:
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
                yield AgentStreamEvent(
                    delta=_permission_error_message(text),
                    event_type="error",
                )
                return
            if (proc.returncode or 0) != 0:
                yield AgentStreamEvent(
                    delta=text or f"Claude Code exited with status {proc.returncode}.",
                    event_type="error",
                )

        except FileNotFoundError:
            yield AgentStreamEvent(
                delta=f"Claude binary not found: {self._config.binary}",
                event_type="error",
            )
        except Exception as exc:
            logger.exception("Claude streaming error")
            yield AgentStreamEvent(delta=f"Error: {exc}", event_type="error")

    def interrupt(self, session_id: str | None = None) -> None:
        # Claude subprocess termination is managed at the process level
        pass

    def health(self) -> object:
        return _ClaudeHealth(self._config.enabled, self._config.binary)

    def _prompt_args(self, prompt: str, *, stream_json: bool = False) -> list[str]:
        args = [
            "-p",
            prompt,
            "--permission-mode",
            normalize_claude_permission_mode(self.permission_mode),
        ]
        if self._config.model:
            args.extend(["--model", normalize_claude_model_name(self._config.model)])
        if self._config.effort:
            args.extend(["--effort", self._config.effort])
        if stream_json:
            args.extend([
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
            ])
        return args


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
            total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens))
            reasoning_output_tokens = 0
            if not model:
                model = str(usage.get("model", ""))
        else:
            source = "estimated"
            input_tokens = approx_tokens(prompt)
            output_tokens = approx_tokens(output_text)
            cached_input_tokens = 0
            reasoning_output_tokens = 0
            total_tokens = input_tokens + output_tokens

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
