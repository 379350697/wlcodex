from __future__ import annotations

from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.claude_sdk_deepseek_provider import (
    ClaudeSdkDeepSeekConfig,
    ClaudeSdkDeepSeekProvider,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore


class FakeSdkRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str,
        config: ClaudeSdkDeepSeekConfig,
    ):
        self.calls.append((prompt, cwd, session_id, config.base_url, config.model))
        if self.fail:
            raise RuntimeError("sdk failed")
        yield {"type": "assistant", "text": "done"}


def _provider(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    runner: FakeSdkRunner | None = None,
) -> tuple[ClaudeSdkDeepSeekProvider, FakeSdkRunner, NativeAgentSessionStore]:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = NativeAgentSessionStore(ledger)
    fake_runner = runner or FakeSdkRunner()
    provider = ClaudeSdkDeepSeekProvider(
        config=ClaudeSdkDeepSeekConfig(api_key_env="DEEPSEEK_API_KEY"),
        session_store=store,
        runner=fake_runner,
        env={"DEEPSEEK_API_KEY": "sk-test"} if env is None else env,
    )
    return provider, fake_runner, store


@pytest.mark.asyncio
async def test_status_reports_missing_api_key(tmp_path: Path) -> None:
    provider, _runner, _store = _provider(tmp_path, env={})

    status = await provider.status()

    assert status.connected is False
    assert status.status_code == "missing_api_key"


@pytest.mark.asyncio
async def test_start_session_uses_deepseek_anthropic_endpoint(tmp_path: Path) -> None:
    provider, runner, store = _provider(tmp_path)

    result = await provider.start_session(str(tmp_path), "fix tests")

    assert result.provider == "claude"
    assert result.provider_engine == "sdk-deepseek"
    assert result.status == "started"
    assert runner.calls[0][0] == "fix tests"
    assert runner.calls[0][1] == str(tmp_path)
    assert runner.calls[0][3] == "https://api.deepseek.com/anthropic"
    assert runner.calls[0][4] == "deepseek-v4-pro"
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert session.metadata["model"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_continue_session_uses_existing_sdk_session_id(
    tmp_path: Path,
) -> None:
    provider, runner, _store = _provider(tmp_path)
    created = await provider.create_session(str(tmp_path))

    result = await provider.continue_session(
        created.native_session_id,
        "continue",
        model="deepseek-v4-flash",
    )

    assert result.status == "continued"
    assert runner.calls[0][2] == created.native_session_id
    assert runner.calls[0][4] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_failed_run_marks_session_failed(tmp_path: Path) -> None:
    runner = FakeSdkRunner(fail=True)
    provider, _runner, store = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "fail")

    assert result.status == "failed"
    session = store.get_by_native_session_id(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "failed"
    assert session.metadata["error"] == "sdk failed"


def test_capabilities_do_not_expose_second_claude_provider(tmp_path: Path) -> None:
    provider, _runner, _store = _provider(tmp_path)

    assert provider.provider == "claude"
    assert provider.provider_engine == "sdk-deepseek"
    assert provider.capabilities().can_start_session is True
    assert provider.capabilities().can_steer_active_turn is False
