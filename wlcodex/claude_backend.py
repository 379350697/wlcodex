"""Claude Code subprocess adapter and event normalization.

Calls the Claude CLI binary with -p (--print) for non-interactive output.
Sets cwd to the selected workspace, captures stdout/stderr, and returns
exit status and summary. Avoids shell=True.
"""

from __future__ import annotations

import asyncio
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
    request_timeout_seconds: float = 600.0
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
                *self._prompt_args(request.prompt),
                cwd=request.workspace_path or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )

            if proc.stdout is None:
                yield AgentStreamEvent(delta="(no output)", event_type="error")
                return

            accumulated: list[str] = []
            reader_task: asyncio.Task[bytes] | None = None
            wait_task: asyncio.Task[int] | None = None
            try:
                async with asyncio.timeout(self._config.request_timeout_seconds):
                    reader_task = asyncio.create_task(proc.stdout.readline())
                    wait_task = asyncio.create_task(proc.wait())
                    process_exited = False
                    stdout_eof = False

                    while True:
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
                            done, _pending = await asyncio.wait(
                                wait_items,
                                timeout=self._config.stream_drain_grace_seconds,
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
                                decoded = line.decode("utf-8", errors="replace")
                                accumulated.append(decoded)
                                current_text = "".join(accumulated)
                                if _looks_like_permission_request(current_text):
                                    await _kill_process(proc)
                                    yield AgentStreamEvent(
                                        delta=_permission_error_message(current_text),
                                        event_type="error",
                                    )
                                    return
                                yield AgentStreamEvent(delta=decoded, event_type="text")
                                reader_task = asyncio.create_task(proc.stdout.readline())

                        if process_exited and stdout_eof:
                            break
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

    def _prompt_args(self, prompt: str) -> list[str]:
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
        return args


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
        "等待你确认",
        "需要确认",
        "权限确认",
        "requires approval",
        "need your approval",
        "permission required",
        "waiting for approval",
        "waiting for confirmation",
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
