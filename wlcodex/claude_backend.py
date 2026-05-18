"""Claude Code subprocess adapter and event normalization.

Calls the Claude CLI binary with -p (--print) for non-interactive output.
Sets cwd to the selected workspace, captures stdout/stderr, and returns
exit status and summary. Avoids shell=True.
"""

from __future__ import annotations

import asyncio
import logging
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
    permission_mode: str = "acceptEdits"
    model: str = "deepseek4pro"
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
            )

            if proc.stdout is None:
                yield AgentStreamEvent(delta="(no output)", event_type="error")
                return

            accumulated: list[str] = []
            try:
                async with asyncio.timeout(self._config.request_timeout_seconds):
                    async for line in proc.stdout:
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

                    await proc.wait()
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
            args.extend(["--model", self._config.model])
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
    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()
