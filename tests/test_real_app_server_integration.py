"""Real app-server integration tests.

Gated by WLCODEX_RUN_CODEX_INTEGRATION=1.
Requires `codex` CLI on PATH and valid Codex auth.
"""

import os
from pathlib import Path

import pytest

from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig
from wlcodex.codex_backend import AppServerCodexBackend

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("WLCODEX_RUN_CODEX_INTEGRATION") != "1",
        reason="set WLCODEX_RUN_CODEX_INTEGRATION=1 to run real Codex app-server tests",
    ),
]


@pytest.mark.asyncio
async def test_real_app_server_thread_turn_and_events(tmp_path: Path) -> None:
    port = int(os.environ.get("WLCODEX_TEST_APP_SERVER_PORT", "17432"))
    process = AppServerProcess(AppServerProcessConfig(
        binary=os.environ.get("WLCODEX_CODEX_BINARY", "codex"),
        host="127.0.0.1",
        port=port,
        startup_timeout_seconds=20,
    ))
    process.start()
    try:
        assert await process.wait_ready_async()

        backend = AppServerCodexBackend(
            process.endpoint,
            approval_policy="on-request",
            sandbox="workspace-write",
            request_timeout_seconds=60,
        )
        backend.set_process_manager(process)

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("wlcodex integration\n", encoding="utf-8")

        thread_id = await backend.create_thread(str(workspace))
        assert thread_id

        turn_id = await backend.start_turn(
            thread_id,
            "Reply exactly with: wlcodex real integration ok",
        )
        assert turn_id

        seen: list[str] = []
        async for event in backend.events():
            seen.append(event.event_type)
            if event.event_type == "turn_completed":
                break
            if len(seen) > 200:
                raise AssertionError(f"too many events without completion: {seen}")

        assert "turn_started" in seen
        assert "turn_completed" in seen
        await backend.close()
    finally:
        process.shutdown()
