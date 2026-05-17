"""Claude Code subprocess adapter and event normalization.

The first implementation is line-buffered subprocess execution. It:
- Accepts a compact prompt packet (never full Telegram transcripts).
- Sets cwd to the selected workspace.
- Captures stdout and stderr.
- Returns exit status and summary.
- Avoids shell=True.
- Exposes a fake-friendly interface for tests.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from wlcodex.agent_backend import AgentRequest, AgentResult, AgentStreamEvent

logger = logging.getLogger(__name__)


@dataclass
class ClaudeConfig:
    enabled: bool = False
    binary: str = "claude"
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 600.0


class ClaudeBackend:
    def __init__(self, config: ClaudeConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def send(self, request: AgentRequest) -> AgentResult:
        if not self._config.enabled:
            return AgentResult(text="Claude Code is not enabled.", exit_code=1)

        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                "--print",
                "--no-detach",
                request.prompt,
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

            return AgentResult(
                text=text,
                exit_code=proc.returncode or 0,
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
                "--print",
                "--no-detach",
                request.prompt,
                cwd=request.workspace_path or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            if proc.stdout is None:
                yield AgentStreamEvent(delta="(no output)", event_type="error")
                return

            async for line in proc.stdout:
                decoded = line.decode("utf-8", errors="replace")
                yield AgentStreamEvent(delta=decoded, event_type="text")

            await proc.wait()

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
