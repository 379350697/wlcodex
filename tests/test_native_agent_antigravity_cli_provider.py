from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.native_agents.antigravity_cli_provider import AntigravityCliLocalProvider
from wlcodex.native_agents.antigravity_local_sessions import (
    AntigravityLocalSession,
    AntigravityLocalSessionIndex,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


class FakeAntigravityCliRunner:
    available = True
    error = ""
    binary = "/tmp/agy"

    def __init__(
        self,
        *,
        conversation_id: str = "ag-conv-1",
        fail: bool = False,
    ) -> None:
        self.conversation_id = conversation_id
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        conversation_id: str = "",
        model: str = "",
        extra_dirs: tuple[str, ...] = (),
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "conversation_id": conversation_id,
                "model": model,
                "extra_dirs": extra_dirs,
            }
        )
        if self.fail:
            raise RuntimeError("antigravity cli failed")
        yield {
            "type": "assistant",
            "text": "hello",
            "conversation_id": conversation_id or self.conversation_id,
        }


class BlockingAntigravityCliRunner(FakeAntigravityCliRunner):
    def __init__(self) -> None:
        super().__init__(conversation_id="ag-conv-1")
        self.release = asyncio.Event()

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        conversation_id: str = "",
        model: str = "",
        extra_dirs: tuple[str, ...] = (),
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": cwd,
                "conversation_id": conversation_id,
                "model": model,
                "extra_dirs": extra_dirs,
            }
        )
        await self.release.wait()
        yield {
            "type": "assistant",
            "text": "background hello",
            "conversation_id": conversation_id or "ag-conv-1",
        }


class EmptyAntigravityIndex:
    def list_recent(self, limit: int = 50):
        return []

    def get(self, session_id: str):
        return None

    def latest_for_cwd(self, cwd: str):
        return None


def _provider(
    tmp_path: Path,
    *,
    runner=None,
    local_session_index=None,
) -> tuple[
    AntigravityCliLocalProvider,
    NativeAgentSessionStore,
    RuntimeEventStore,
    object,
]:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = NativeAgentSessionStore(ledger)
    runtime_store = RuntimeEventStore(ledger._conn)
    fake_runner = runner or FakeAntigravityCliRunner()
    return (
        AntigravityCliLocalProvider(
            runner=fake_runner,
            session_store=store,
            runtime_store=runtime_store,
            default_cwd=str(tmp_path),
            local_session_index=local_session_index or EmptyAntigravityIndex(),
        ),
        store,
        runtime_store,
        fake_runner,
    )


@pytest.mark.asyncio
async def test_cli_provider_start_session_runs_in_background_and_streams_events(
    tmp_path: Path,
) -> None:
    runner = BlockingAntigravityCliRunner()
    provider, store, runtime_store, _runner = _provider(tmp_path, runner=runner)

    result = await asyncio.wait_for(
        provider.start_session(str(tmp_path), "say hi"),
        timeout=0.05,
    )

    assert result.provider == "antigravity"
    assert result.provider_engine == "cli-local"
    assert result.status == "started"
    assert result.turn_running is True
    assert result.active_turn_id == result.turn_id
    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "running"
    initial_events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in initial_events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
    ]
    await asyncio.sleep(0)
    assert runner.calls[0]["prompt"] == "say hi"
    assert runner.calls[0]["conversation_id"] == ""

    runner.release.set()
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert session.metadata["antigravity_conversation_id"] == "ag-conv-1"
    events = runtime_store.list_by_agent_run(session.agent_run_id)
    assert [event.event_type for event in events] == [
        EventType.AGENT_RUN_STARTED,
        EventType.USER_MESSAGE_RECEIVED,
        EventType.MODEL_TEXT_DELTA,
        EventType.AGENT_RUN_COMPLETED,
    ]
    assert events[2].payload["delta"] == "background hello"


@pytest.mark.asyncio
async def test_cli_provider_read_and_continue_use_saved_conversation_id(
    tmp_path: Path,
) -> None:
    runner = FakeAntigravityCliRunner(conversation_id="ag-conv-1")
    provider, _store, _runtime_store, _runner = _provider(tmp_path, runner=runner)

    result = await provider.start_session(str(tmp_path), "first")
    await provider.wait_for_background_tasks()

    payload = await provider.read_session(result.native_session_id)
    assert payload["turns"] == [
        {"role": "user", "text": "first", "native_turn_id": result.turn_id},
        {"role": "assistant", "text": "hello", "native_turn_id": result.turn_id},
    ]

    await provider.continue_session(result.native_session_id, "second")
    await provider.wait_for_background_tasks()

    assert runner.calls[1]["prompt"] == "second"
    assert runner.calls[1]["conversation_id"] == "ag-conv-1"


@pytest.mark.asyncio
async def test_cli_provider_start_session_does_not_resume_agy_conversation_by_id(
    tmp_path: Path,
) -> None:
    runner = BlockingAntigravityCliRunner()
    provider, _store, _runtime_store, _runner = _provider(tmp_path, runner=runner)

    _result = await asyncio.wait_for(
        provider.start_session(str(tmp_path), "start isolated"),
        timeout=0.05,
    )

    await asyncio.sleep(0)
    assert runner.calls[0]["conversation_id"] == ""

    runner.release.set()
    await provider.wait_for_background_tasks()


@pytest.mark.asyncio
async def test_cli_provider_start_session_uses_isolated_execution_cwd(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = BlockingAntigravityCliRunner()
    provider, store, _runtime_store, _runner = _provider(tmp_path, runner=runner)

    result = await asyncio.wait_for(
        provider.start_session(str(workspace), "start isolated"),
        timeout=0.05,
    )

    await asyncio.sleep(0)
    call = runner.calls[0]
    assert call["cwd"] != str(workspace)
    assert str(result.native_session_id) in str(call["cwd"])
    assert call["extra_dirs"] == (str(workspace),)
    assert Path(str(call["cwd"])).exists()

    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.metadata["antigravity_execution_cwd"] == call["cwd"]

    runner.release.set()
    await provider.wait_for_background_tasks()


@pytest.mark.asyncio
async def test_cli_provider_forwards_explicit_model_to_runner(
    tmp_path: Path,
) -> None:
    runner = BlockingAntigravityCliRunner()
    provider, _store, _runtime_store, _runner = _provider(tmp_path, runner=runner)

    await asyncio.wait_for(
        provider.start_session(
            str(tmp_path),
            "start with model",
            model="Claude Sonnet 4.6 (Thinking)",
        ),
        timeout=0.05,
    )

    await asyncio.sleep(0)
    assert runner.calls[0]["model"] == "Claude Sonnet 4.6 (Thinking)"

    runner.release.set()
    await provider.wait_for_background_tasks()


@pytest.mark.asyncio
async def test_cli_provider_imports_local_pb_history_and_can_continue_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "antigravity-cli"
    conversations = root / "conversations"
    brain = root / "brain" / "conv-1"
    cache = root / "cache"
    conversations.mkdir(parents=True)
    brain.mkdir(parents=True)
    cache.mkdir(parents=True)
    (conversations / "conv-1.pb").write_bytes(b"\x08\x01")
    (brain / "task.md").write_text("# Local AG Task\n\nInvestigate it.\n", encoding="utf-8")
    (cache / "last_conversations.json").write_text(
        json.dumps({str(workspace): "conv-1"}),
        encoding="utf-8",
    )
    local_index = AntigravityLocalSessionIndex(roots=(root,))
    runner = FakeAntigravityCliRunner()
    provider, _store, _runtime_store, _runner = _provider(
        tmp_path,
        runner=runner,
        local_session_index=local_index,
    )

    sessions = await provider.list_sessions()

    imported = [session for session in sessions if session.native_session_id == "conv-1"]
    assert len(imported) == 1
    session = imported[0]
    assert session.title == "Local AG Task"
    assert session.cwd == str(workspace)
    assert session.source_kind == "antigravity_cli_local"
    assert session.status == "done"
    assert session.metadata["antigravity_conversation_id"] == "conv-1"
    assert session.metadata["antigravity_source_path"] == str(conversations / "conv-1.pb")
    assert session.metadata["antigravity_brain_path"] == str(brain)

    await provider.continue_session("conv-1", "continue local")
    await provider.wait_for_background_tasks()

    assert runner.calls[0]["cwd"] == str(workspace)
    assert runner.calls[0]["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_cli_provider_imports_local_db_history(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "antigravity-cli"
    conversations = root / "conversations"
    cache = root / "cache"
    conversations.mkdir(parents=True)
    cache.mkdir(parents=True)
    (conversations / "db-conv.db").write_bytes(b"SQLite format 3\x00")
    (cache / "last_conversations.json").write_text(
        json.dumps({str(workspace): "db-conv"}),
        encoding="utf-8",
    )
    local_index = AntigravityLocalSessionIndex(roots=(root,))
    provider, _store, _runtime_store, _runner = _provider(
        tmp_path,
        local_session_index=local_index,
    )

    sessions = await provider.list_sessions()

    imported = [session for session in sessions if session.native_session_id == "db-conv"]
    assert len(imported) == 1
    assert imported[0].cwd == str(workspace)
    assert imported[0].metadata["antigravity_source_path"] == str(
        conversations / "db-conv.db"
    )


def test_antigravity_local_session_index_matches_equivalent_cwd_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_alias = tmp_path / "workspace-alias"
    workspace_alias.symlink_to(workspace, target_is_directory=True)
    root = tmp_path / "antigravity-cli"
    conversations = root / "conversations"
    cache = root / "cache"
    conversations.mkdir(parents=True)
    cache.mkdir(parents=True)
    (conversations / "equivalent-cwd.db").write_bytes(b"SQLite format 3\x00")
    (cache / "last_conversations.json").write_text(
        json.dumps({str(workspace_alias): "equivalent-cwd"}),
        encoding="utf-8",
    )

    local_index = AntigravityLocalSessionIndex(roots=(root,))

    session = local_index.latest_for_cwd(str(workspace))
    assert session is not None
    assert session.session_id == "equivalent-cwd"


@pytest.mark.asyncio
async def test_cli_provider_recovers_conversation_id_from_isolated_local_cwd(
    tmp_path: Path,
) -> None:
    class LatestIndex(EmptyAntigravityIndex):
        def latest_for_cwd(self, cwd: str):
            return AntigravityLocalSession(
                session_id="latest-conv",
                title="Latest",
                cwd=cwd,
                created_at="2026-06-03T00:00:00+00:00",
                updated_at="2999-06-03T00:00:01+00:00",
                source_path=str(tmp_path / "latest-conv.pb"),
            )

    class NoIdRunner(FakeAntigravityCliRunner):
        async def run(
            self,
            *,
            prompt: str,
            cwd: str,
            conversation_id: str = "",
            model: str = "",
            extra_dirs: tuple[str, ...] = (),
        ):
            self.calls.append(
                {
                    "prompt": prompt,
                    "cwd": cwd,
                    "conversation_id": conversation_id,
                    "model": model,
                    "extra_dirs": extra_dirs,
                }
            )
            yield {"type": "assistant", "text": "hello"}

    provider, store, _runtime_store, _runner = _provider(
        tmp_path,
        runner=NoIdRunner(),
        local_session_index=LatestIndex(),
    )

    result = await provider.start_session(str(tmp_path), "first")
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.metadata["antigravity_conversation_id"] == "latest-conv"


@pytest.mark.asyncio
async def test_cli_provider_ignores_stale_latest_local_cwd(
    tmp_path: Path,
) -> None:
    class StaleIndex(EmptyAntigravityIndex):
        def latest_for_cwd(self, cwd: str):
            return AntigravityLocalSession(
                session_id="stale-conv",
                title="Stale",
                cwd=cwd,
                created_at="2020-06-03T00:00:00+00:00",
                updated_at="2020-06-03T00:00:01+00:00",
                source_path=str(tmp_path / "stale-conv.pb"),
            )

    class NoIdRunner(FakeAntigravityCliRunner):
        async def run(
            self,
            *,
            prompt: str,
            cwd: str,
            conversation_id: str = "",
            model: str = "",
            extra_dirs: tuple[str, ...] = (),
        ):
            self.calls.append(
                {
                    "prompt": prompt,
                    "cwd": cwd,
                    "conversation_id": conversation_id,
                    "model": model,
                    "extra_dirs": extra_dirs,
                }
            )
            yield {"type": "assistant", "text": "hello"}

    provider, store, _runtime_store, _runner = _provider(
        tmp_path,
        runner=NoIdRunner(),
        local_session_index=StaleIndex(),
    )

    result = await provider.start_session(str(tmp_path), "first")
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "done"
    assert "antigravity_conversation_id" not in session.metadata


@pytest.mark.asyncio
async def test_cli_provider_empty_output_failure_mentions_cli_setup(
    tmp_path: Path,
) -> None:
    class EmptyRunner(FakeAntigravityCliRunner):
        async def run(
            self,
            *,
            prompt: str,
            cwd: str,
            conversation_id: str = "",
            model: str = "",
            extra_dirs: tuple[str, ...] = (),
        ):
            self.calls.append(
                {
                    "prompt": prompt,
                    "cwd": cwd,
                    "conversation_id": conversation_id,
                    "model": model,
                    "extra_dirs": extra_dirs,
                }
            )
            if False:
                yield {}

    provider, store, _runtime_store, _runner = _provider(
        tmp_path,
        runner=EmptyRunner(),
    )

    result = await provider.start_session(str(tmp_path), "first")
    await provider.wait_for_background_tasks()

    session = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert session is not None
    assert session.status == "failed"
    assert "Run agy" in session.metadata["error"]


@pytest.mark.asyncio
async def test_cli_provider_hides_local_duplicate_after_created_session_claims_pb(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_session = AntigravityLocalSession(
        session_id="latest-conv",
        title="Latest Local",
        cwd=str(workspace),
        created_at="2026-06-03T00:00:00+00:00",
        updated_at="2999-06-03T00:00:01+00:00",
        source_path=str(tmp_path / "latest-conv.pb"),
    )

    class RaceIndex(EmptyAntigravityIndex):
        sessions: list[AntigravityLocalSession]

        def __init__(self) -> None:
            self.sessions = []

        def list_recent(self, limit: int = 50):
            return self.sessions[:limit]

        def get(self, session_id: str):
            if session_id == local_session.session_id:
                return local_session
            return None

        def latest_for_cwd(self, cwd: str):
            if cwd == str(workspace):
                return local_session
            return None

    class BlockingExternalIdRunner(FakeAntigravityCliRunner):
        def __init__(self) -> None:
            super().__init__(conversation_id="")
            self.release = asyncio.Event()

        async def run(
            self,
            *,
            prompt: str,
            cwd: str,
            conversation_id: str = "",
            model: str = "",
            extra_dirs: tuple[str, ...] = (),
        ):
            self.calls.append(
                {
                    "prompt": prompt,
                    "cwd": cwd,
                    "conversation_id": conversation_id,
                    "model": model,
                    "extra_dirs": extra_dirs,
                }
            )
            await self.release.wait()
            yield {
                "type": "assistant",
                "text": "hello",
                "conversation_id": local_session.session_id,
            }

    local_index = RaceIndex()
    runner = BlockingExternalIdRunner()
    provider, store, _runtime_store, _runner = _provider(
        tmp_path,
        runner=runner,
        local_session_index=local_index,
    )

    result = await provider.start_session(str(workspace), "first")
    local_index.sessions = [local_session]
    during = await provider.list_sessions()
    assert {session.native_session_id for session in during} == {
        result.native_session_id,
        "latest-conv",
    }

    runner.release.set()
    await provider.wait_for_background_tasks()

    sessions = await provider.list_sessions()
    session_ids = [session.native_session_id for session in sessions]
    assert result.native_session_id in session_ids
    assert "latest-conv" not in session_ids
    created = store.get_by_native_session_id(
        provider="antigravity",
        provider_engine="cli-local",
        native_session_id=result.native_session_id,
    )
    assert created is not None
    assert created.metadata["antigravity_conversation_id"] == "latest-conv"
    assert created.metadata["antigravity_source_path"] == local_session.source_path


@pytest.mark.asyncio
async def test_cli_runner_builds_agy_print_command(monkeypatch, tmp_path: Path) -> None:
    from wlcodex.native_agents.antigravity_cli_provider import (
        AntigravityCliConfig,
        AntigravityCliRunner,
    )

    commands = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"hello\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    fake_binary = tmp_path / "agy"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    runner = AntigravityCliRunner(
        AntigravityCliConfig(
            binary=str(fake_binary),
            print_timeout="7m0s",
            dangerously_skip_permissions=True,
            sandbox=True,
        )
    )

    events = [
        event
        async for event in runner.run(
            prompt="hello",
            cwd=str(tmp_path),
            conversation_id="conv-1",
            extra_dirs=(str(tmp_path / "extra"),),
        )
    ]

    args, kwargs = commands[0]
    assert args == (
        str(fake_binary),
        "--print-timeout",
        "7m0s",
        "--conversation",
        "conv-1",
        "--add-dir",
        str(tmp_path),
        "--add-dir",
        str(tmp_path / "extra"),
        "--dangerously-skip-permissions",
        "--sandbox",
        "--model",
        "Gemini 3.5 Flash (Medium)",
        "--print",
        "hello",
    )
    assert kwargs["cwd"] == str(tmp_path)
    assert events == [
        {"type": "assistant", "text": "hello\n", "conversation_id": "conv-1"}
    ]


@pytest.mark.asyncio
async def test_cli_runner_rejects_authentication_prompt_as_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from wlcodex.native_agents.antigravity_cli_provider import (
        AntigravityCliConfig,
        AntigravityCliRunner,
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return (
                b"Authentication required. Please visit the URL to log in:\n"
                b"Waiting for authentication (timeout 30s)...\n"
                b"Error: authentication timed out.\n",
                b"",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    fake_binary = tmp_path / "agy"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    runner = AntigravityCliRunner(AntigravityCliConfig(binary=str(fake_binary)))

    with pytest.raises(RuntimeError, match="authentication required"):
        _events = [
            event
            async for event in runner.run(
                prompt="hello",
                cwd=str(tmp_path),
            )
        ]


@pytest.mark.asyncio
async def test_cli_runner_rejects_cli_error_text_as_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from wlcodex.native_agents.antigravity_cli_provider import (
        AntigravityCliConfig,
        AntigravityCliRunner,
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"Error: timed out waiting for response\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    fake_binary = tmp_path / "agy"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    runner = AntigravityCliRunner(AntigravityCliConfig(binary=str(fake_binary)))

    with pytest.raises(RuntimeError, match="timed out waiting for response"):
        await _collect_runner_events(runner, tmp_path)


@pytest.mark.asyncio
async def test_cli_runner_fails_fast_when_authentication_prompt_streams(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from wlcodex.native_agents.antigravity_cli_provider import (
        AntigravityCliConfig,
        AntigravityCliRunner,
    )

    class FakeStream:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = lines

        async def readline(self):
            await asyncio.sleep(0)
            if self._lines:
                return self._lines.pop(0)
            return b""

    class FakeProcess:
        returncode = None

        def __init__(self) -> None:
            self.stdout = FakeStream([b"Authentication required. Please log in.\n"])
            self.stderr = FakeStream([])
            self.killed = False

        async def communicate(self):
            await asyncio.sleep(3600)
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0.01)
            return self.returncode

    fake_process = FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    fake_binary = tmp_path / "agy"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    runner = AntigravityCliRunner(AntigravityCliConfig(binary=str(fake_binary)))

    with pytest.raises(RuntimeError, match="authentication required"):
        await asyncio.wait_for(
            _collect_runner_events(runner, tmp_path),
            timeout=0.2,
        )
    assert fake_process.killed is True


async def _collect_runner_events(runner, tmp_path: Path):
    return [
        event
        async for event in runner.run(
            prompt="hello",
            cwd=str(tmp_path),
        )
    ]
