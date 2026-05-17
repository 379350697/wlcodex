"""App-server subprocess lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import shlex
import subprocess
import time

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppServerProcessConfig:
    binary: str
    host: str = "127.0.0.1"
    port: int = 17431
    startup_timeout_seconds: float = 15

    @property
    def endpoint(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def command(self) -> list[str]:
        return [self.binary, "app-server", "--listen", self.endpoint]


@dataclass
class BackendHealth:
    process_alive: bool
    websocket_connected: bool
    error: str | None = None
    external_process: bool = False

    @property
    def is_healthy(self) -> bool:
        process_ok = self.process_alive or self.external_process
        return process_ok and self.websocket_connected and self.error is None

    def summary(self) -> str:
        if self.is_healthy:
            return "Backend healthy"
        parts: list[str] = []
        if not self.process_alive and not self.external_process:
            parts.append("process not alive")
        if not self.websocket_connected:
            parts.append("websocket not connected")
        if self.error:
            parts.append(self.error)
        return "Backend unhealthy: " + "; ".join(parts)


class AppServerProcess:
    """Manages a local codex app-server subprocess."""

    def __init__(self, config: AppServerProcessConfig) -> None:
        if config.host not in ("127.0.0.1", "localhost"):
            raise ValueError(f"App-server must bind to loopback, not {config.host}")
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None
        self.external_process: bool = False  # True when reusing an existing process

    @property
    def endpoint(self) -> str:
        return self._config.endpoint

    def start(self) -> None:
        if self._process is not None:
            return
        logger.info("Starting app-server: %s", shlex.join(self._config.command))
        self._process = subprocess.Popen(
            self._config.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_ready(self) -> bool:
        """Block until the process is started. Returns True if still alive."""
        deadline = time.monotonic() + self._config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._process is None:
                return False
            poll = self._process.poll()
            if poll is not None:
                return False
            return True  # Still running after brief check
        return self._process is not None and self._process.poll() is None

    async def wait_ready_async(self) -> bool:
        """Probe the app-server WebSocket until connection succeeds or timeout.

        Unlike wait_ready() this performs a real connect/disconnect cycle
        so it verifies the app-server is actually accepting connections.
        """
        import asyncio as _asyncio
        import websockets

        deadline = time.monotonic() + self._config.startup_timeout_seconds
        last_error = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            try:
                async with websockets.connect(self.endpoint):
                    return True
            except Exception as exc:
                last_error = exc
                await _asyncio.sleep(0.25)
        logger.warning("app-server readiness probe failed: %s", last_error)
        return False

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def shutdown(self) -> None:
        if self._process is None:
            return
        logger.info("Shutting down app-server")
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None
