from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from wlcodex.db import Ledger
from wlcodex.jsonrpc import JsonRpcError
from wlcodex.live_stream.hub import WorkerLiveStreamHub
from wlcodex.live_stream.server import (
    WorkerLiveStreamServer,
    _MAX_BODY_BYTES,
    _close_stream_writer,
    _live_page,
    _native_app_manifest,
    _native_codex_page,
    _codex_plugin_menu_items,
    _plugin_icon_data_url,
)
from wlcodex.live_stream.native_templates.registry import render_native_template
from wlcodex.native_agents.provider import NativeAgentRegistry
from wlcodex.native_timeline import NativeTimelineStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType, RuntimeEvent


_FAKE_SESSION_METADATA = {
    "model": "gpt-5.5",
    "effort": "high",
    "service_tier": "fast",
}


class AlreadyClosingWriter:
    def __init__(self) -> None:
        self.close_calls = 0
        self.wait_closed_calls = 0

    def is_closing(self) -> bool:
        return True

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1


@pytest.mark.asyncio
async def test_close_stream_writer_waits_for_already_closing_writer() -> None:
    writer = AlreadyClosingWriter()

    await _close_stream_writer(writer)  # type: ignore[arg-type]

    assert writer.close_calls == 1
    assert writer.wait_closed_calls == 1


def test_plugin_icon_data_url_resolves_paths_from_plugin_root(tmp_path: Path) -> None:
    plugin_root = tmp_path / "browser"
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    icon = plugin_root / "assets" / "composer-icon.png"
    manifest.parent.mkdir(parents=True)
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"fake-png")

    result = _plugin_icon_data_url(manifest, "./assets/composer-icon.png")

    assert result == "data:image/png;base64,ZmFrZS1wbmc="


def test_codex_plugin_menu_items_prefers_native_plugin_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / ".codex" / "plugins" / "cache"

    def write_plugin(source: str, slug: str, name: str, *, icon: str = "") -> None:
        plugin_root = cache_root / source / slug / "1.0.0"
        manifest = plugin_root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        if icon:
            icon_path = plugin_root / icon
            icon_path.parent.mkdir(parents=True)
            icon_path.write_bytes(b"icon")
        manifest.write_text(
            json.dumps(
                {
                    "name": slug,
                    "interface": {
                        "displayName": name,
                        "shortDescription": f"{name} description",
                        "composerIcon": icon,
                    },
                }
            ),
            encoding="utf-8",
        )

    write_plugin("codex-dev-flow", "codex-dev-flow", "Codex Dev Flow")
    write_plugin("openai-bundled", "browser", "Browser", icon="assets/browser.png")
    write_plugin("openai-primary-runtime", "pdf", "PDF", icon="assets/pdf.png")
    write_plugin("openai-primary-runtime", "documents", "Documents", icon="assets/documents.png")
    write_plugin("openai-primary-runtime", "spreadsheets", "Spreadsheets")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    items = _codex_plugin_menu_items()

    assert [item["name"] for item in items[:5]] == [
        "Documents",
        "PDF",
        "Spreadsheets",
        "Browser",
        "Codex Dev Flow",
    ]
    assert items[0]["icon"] == "data:image/png;base64,aWNvbg=="


@dataclass(frozen=True)
class FakeNativeSession:
    native_thread_id: str
    agent_run_id: int
    activity_at: str = "2026-05-31T12:39:00+00:00"
    updated_at: str = "2026-05-31T13:00:00+00:00"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "activity_at": self.activity_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class FakeControlResult:
    native_thread_id: str
    agent_run_id: int
    turn_id: str
    status: str = "ok"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "native_thread_id": self.native_thread_id,
            "agent_run_id": self.agent_run_id,
            "turn_id": self.turn_id,
            "status": self.status,
        }


class FakeNativeController:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.sessions = [
            FakeNativeSession(
                "thread-1",
                42,
                metadata=_FAKE_SESSION_METADATA,
            )
        ]

    async def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        return {"enabled": True, "connected": True, "remote_control_status": "ready"}

    async def list_sessions(self) -> list[FakeNativeSession]:
        self.calls.append(("list_sessions",))
        return self.sessions

    async def list_models(self) -> list[dict[str, Any]]:
        self.calls.append(("list_models",))
        return [
            {
                "id": "gpt-5.5",
                "model": "gpt-5.5",
                "displayName": "GPT-5.5",
                "description": "Most capable",
                "hidden": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "Deep"},
                ],
                "defaultReasoningEffort": "medium",
                "serviceTiers": [
                    {"id": "auto", "name": "Auto", "description": "Default"},
                    {"id": "fast", "name": "Fast", "description": "Lower latency"},
                ],
                "defaultServiceTier": "auto",
                "isDefault": True,
            }
        ]

    async def read_session(self, native_thread_id: str) -> dict[str, Any]:
        self.calls.append(("read_session", native_thread_id))
        return {"thread": {"id": native_thread_id, "turns": []}, "agent_run_id": 42}

    async def attach_session(self, native_thread_id: str) -> dict[str, Any]:
        self.calls.append(("attach_session", native_thread_id))
        return FakeControlResult(native_thread_id, 42, "turn-1", status="attached")

    async def sync_session(self, native_thread_id: str) -> FakeControlResult:
        self.calls.append(("sync_session", native_thread_id))
        return FakeControlResult(native_thread_id, 42, "turn-1", status="synced")

    async def continue_session(
        self,
        native_thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, Any] | None = None,
        collaboration_mode: dict[str, Any] | None = None,
        force_new_turn: bool = False,
    ) -> FakeControlResult:
        if force_new_turn:
            self.calls.append(
                ("continue_session", native_thread_id, prompt, force_new_turn)
            )
        elif (
            model is None
            and effort is None
            and service_tier is None
            and images is None
            and approval_policy is None
            and approvals_reviewer is None
            and sandbox_policy is None
            and collaboration_mode is None
        ):
            self.calls.append(("continue_session", native_thread_id, prompt))
        else:
            if (
                approval_policy is None
                and approvals_reviewer is None
                and sandbox_policy is None
                and collaboration_mode is None
            ):
                self.calls.append(
                    (
                        "continue_session",
                        native_thread_id,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                    )
                )
            elif collaboration_mode is None:
                self.calls.append(
                    (
                        "continue_session",
                        native_thread_id,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                    )
                )
            else:
                self.calls.append(
                    (
                        "continue_session",
                        native_thread_id,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                        collaboration_mode,
                    )
                )
        return FakeControlResult(native_thread_id, 42, "turn-2")

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox: str | None = None,
        sandbox_policy: dict[str, Any] | None = None,
        collaboration_mode: dict[str, Any] | None = None,
    ) -> FakeControlResult:
        if (
            approval_policy is None
            and approvals_reviewer is None
            and sandbox is None
            and sandbox_policy is None
            and collaboration_mode is None
        ):
            self.calls.append(
                ("start_session", cwd, prompt, model, effort, service_tier, images)
            )
        elif collaboration_mode is None:
            self.calls.append(
                (
                    "start_session",
                    cwd,
                    prompt,
                    model,
                    effort,
                    service_tier,
                    images,
                    approval_policy,
                    approvals_reviewer,
                    sandbox,
                    sandbox_policy,
                )
            )
        else:
            self.calls.append(
                (
                    "start_session",
                    cwd,
                    prompt,
                    model,
                    effort,
                    service_tier,
                    images,
                    approval_policy,
                    approvals_reviewer,
                    sandbox,
                    sandbox_policy,
                    collaboration_mode,
                )
            )
        return FakeControlResult("thread-new", 43, "turn-new")

    async def create_session(
        self,
        cwd: str,
        *,
        model: str | None = None,
        service_tier: str | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox: str | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ) -> FakeControlResult:
        if (
            approval_policy is None
            and approvals_reviewer is None
            and sandbox is None
            and sandbox_policy is None
        ):
            self.calls.append(("create_session", cwd, model, service_tier))
        else:
            self.calls.append(
                (
                    "create_session",
                    cwd,
                    model,
                    service_tier,
                    approval_policy,
                    approvals_reviewer,
                    sandbox,
                    sandbox_policy,
                )
            )
        return FakeControlResult("thread-empty", 44, "", status="created")

    async def steer_session(
        self,
        native_thread_id: str,
        expected_turn_id: str,
        prompt: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        service_tier: str | None = None,
        images: list[dict[str, Any]] | None = None,
        approval_policy: str | None = None,
        approvals_reviewer: str | None = None,
        sandbox_policy: dict[str, Any] | None = None,
    ) -> FakeControlResult:
        if (
            model is None
            and effort is None
            and service_tier is None
            and images is None
            and approval_policy is None
            and approvals_reviewer is None
            and sandbox_policy is None
        ):
            self.calls.append(("steer_session", native_thread_id, expected_turn_id, prompt))
        else:
            if approval_policy is None and approvals_reviewer is None and sandbox_policy is None:
                self.calls.append(
                    (
                        "steer_session",
                        native_thread_id,
                        expected_turn_id,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                    )
                )
            else:
                self.calls.append(
                    (
                        "steer_session",
                        native_thread_id,
                        expected_turn_id,
                        prompt,
                        model,
                        effort,
                        service_tier,
                        images,
                        approval_policy,
                        approvals_reviewer,
                        sandbox_policy,
                    )
                )
        return FakeControlResult(native_thread_id, 42, expected_turn_id)

    async def interrupt_session(
        self,
        native_thread_id: str,
        turn_id: str,
    ) -> FakeControlResult:
        self.calls.append(("interrupt_session", native_thread_id, turn_id))
        return FakeControlResult(native_thread_id, 42, turn_id, status="interrupted")

    async def resolve_approval(
        self,
        codex_request_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("resolve_approval", codex_request_id, response))
        return {"codex_request_id": codex_request_id, "status": "resolved"}


class FakeAntigravityProvider:
    provider = "antigravity"
    provider_engine = "cli-local"


class FakeClaudeProvider:
    provider = "claude"
    provider_engine = "cli-local"

    async def list_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "deepseek-v4-pro",
                "model": "deepseek-v4-pro",
                "displayName": "deepseek-v4-pro",
                "defaultReasoningEffort": "max",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "轻量"},
                    {"reasoningEffort": "medium", "description": "正常"},
                    {"reasoningEffort": "high", "description": "深度"},
                    {"reasoningEffort": "xhigh", "description": "极深"},
                    {"reasoningEffort": "max", "description": "最大"},
                ],
                "serviceTiers": [],
                "isDefault": True,
            }
        ]


class SlowCodexProviderWithCache:
    provider = "codex"
    provider_engine = "app-server"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def list_sessions(self, limit: int = 50) -> list[FakeNativeSession]:
        self.calls.append(("list_sessions", limit))
        await asyncio.sleep(2)
        return []

    async def list_cached_sessions(self, limit: int = 50) -> list[FakeNativeSession]:
        self.calls.append(("list_cached_sessions", limit))
        return [
            FakeNativeSession(
                "cached-thread",
                123,
                metadata={"source": "cache"},
            )
        ]


class PushCodexProviderWithCache(SlowCodexProviderWithCache):
    def __init__(self) -> None:
        super().__init__()
        self.refreshed = asyncio.Event()

    async def list_sessions(self, limit: int = 50) -> list[FakeNativeSession]:
        self.calls.append(("list_sessions", limit))
        self.refreshed.set()
        return [
            FakeNativeSession(
                "fresh-thread",
                456,
                metadata={"source": "daemon"},
            )
        ]


class SlowCodexProviderWithoutCache(SlowCodexProviderWithCache):
    async def list_cached_sessions(self, limit: int = 50) -> list[FakeNativeSession]:
        self.calls.append(("list_cached_sessions", limit))
        return []


class FailingCodexProviderWithCache(SlowCodexProviderWithCache):
    async def list_sessions(self, limit: int = 50) -> list[FakeNativeSession]:
        self.calls.append(("list_sessions", limit))
        raise RuntimeError("boom")


class FakeTranscriptMirror:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.session_signature = "sessions:0"
        self.thread_signatures: dict[str, str] = {}

    def index_recent_sessions(self, *, limit: int = 100) -> int:
        self.calls.append(("index_recent_sessions", limit))
        return 2

    def session_index_signature(self, *, limit: int = 100) -> str:
        self.calls.append(("session_index_signature", limit))
        return self.session_signature

    def thread_file_signature(self, native_thread_id: str) -> str:
        self.calls.append(("thread_file_signature", native_thread_id))
        return self.thread_signatures.get(native_thread_id, "")

    def sync_thread(self, native_thread_id: str, *, tail_lines: int | None = None) -> int:
        if tail_lines is None:
            self.calls.append(("sync_thread", native_thread_id))
        else:
            self.calls.append(("sync_thread", native_thread_id, tail_lines))
        return 1


class WarmupTranscriptMirror(FakeTranscriptMirror):
    def __init__(self, recent_thread_ids: list[str]) -> None:
        super().__init__()
        self.recent_thread_ids = list(recent_thread_ids)

    def recent_turn_thread_ids(self, *, limit: int = 2) -> list[str]:
        self.calls.append(("recent_turn_thread_ids", limit))
        return self.recent_thread_ids[:limit]


def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(ledger._conn)


async def _read_response(
    host: str,
    port: int,
    request: str,
    *,
    read_limit: int = 65536,
) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request.encode("utf-8"))
    await writer.drain()
    chunks: list[bytes] = []
    while True:
        chunk = await asyncio.wait_for(reader.read(read_limit), timeout=1.0)
        if not chunk:
            break
        chunks.append(chunk)
    writer.close()
    await writer.wait_closed()
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _read_initial_response(
    host: str,
    port: int,
    request: str,
    marker: bytes,
) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(request.encode("utf-8"))
    await writer.drain()
    try:
        chunk = await asyncio.wait_for(reader.readuntil(marker), timeout=1.0)
    finally:
        writer.close()
        await writer.wait_closed()
    return chunk.decode("utf-8", errors="replace")


def _json_body(response: str) -> dict[str, Any]:
    return json.loads(response.split("\r\n\r\n", 1)[1])


def _append_worker_event(
    store: RuntimeEventStore,
    agent_run_id: int,
    *,
    event_type: str = "model.text.delta",
    native_thread_id: str | None = None,
    native_turn_id: str | None = None,
    delta: str = "hello",
) -> None:
    payload: dict[str, Any] = {"delta": delta}
    if native_thread_id is not None:
        payload["native_thread_id"] = native_thread_id
    if native_turn_id is not None:
        payload["native_turn_id"] = native_turn_id
    store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=event_type,
            aggregate_type="agent_run",
            aggregate_id=str(agent_run_id),
            correlation_id=f"agent:{agent_run_id}",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload=payload,
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=agent_run_id,
        )
    )


@pytest.mark.asyncio
async def test_native_sessions_requires_authorization_when_token_is_configured(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 401" in response
    assert _json_body(response) == {"error": "unauthorized"}
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_routes_allow_public_loopback_when_token_is_disabled(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "sessions": [
                {
                    "native_thread_id": "thread-1",
                    "agent_run_id": 42,
                    "activity_at": "2026-05-31T12:39:00+00:00",
                    "updated_at": "2026-05-31T13:00:00+00:00",
                    "metadata": _FAKE_SESSION_METADATA,
                }
            ],
        "native_refresh_pending": False,
        "native_session_source": "daemon",
    }
    assert controller.calls == [("list_sessions",)]


@pytest.mark.asyncio
async def test_native_provider_index_links_static_stylesheet(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([FakeAntigravityProvider()]),
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert '<link rel="stylesheet" href="/static/native_index_bundle.css">' in response
    assert '<link rel="stylesheet" href="/static/base.css">' not in response
    assert '<link rel="stylesheet" href="/static/animations.css">' not in response
    assert '<link rel="stylesheet" href="/static/effects.css">' not in response
    assert '<link rel="stylesheet" href="/static/native_index.css">' not in response
    assert "Antigravity" in response
    assert '<button class="circle native-back" id="back" aria-label="back" aria-disabled="true" disabled>' in response


@pytest.mark.asyncio
async def test_native_provider_index_does_not_expose_codex_template_settings(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native?token=secret HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'href="/native/codex?token=secret"' in response
    assert 'class="native-settings-button"' not in response
    assert 'id="nativeSettingsButton"' not in response
    assert 'id="nativeSettingsSheet"' not in response
    assert "Codex 风格" not in response
    assert "重构版 Timeline" not in response
    assert 'data-native-provider="codex"' not in response
    assert "codex-v2" not in response
    assert "wlcodex:native-codex-template" not in response


@pytest.mark.asyncio
async def test_native_provider_page_links_single_static_stylesheet(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([FakeAntigravityProvider()]),
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/antigravity HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert '<link rel="stylesheet" href="/static/native_app_bundle.css">' in response
    assert '<link rel="stylesheet" href="/static/base.css">' not in response
    assert '<link rel="stylesheet" href="/static/animations.css">' not in response
    assert '<link rel="stylesheet" href="/static/effects.css">' not in response
    assert '<link rel="stylesheet" href="/static/components.css">' not in response


@pytest.mark.asyncio
async def test_static_assets_are_publicly_cacheable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /static/native_index.css HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        image_response = await _read_response(
            server.host,
            server.port,
            "GET /static/marvis/marvis-avatar.png HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        office_image_response = await _read_response(
            server.host,
            server.port,
            "GET /static/marvis/office-scene-empty.png HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        office_empty_slot_response = await _read_response(
            server.host,
            server.port,
            "GET /static/marvis/office-desk-empty-slot.png HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        office_roles_response = await _read_response(
            server.host,
            server.port,
            "GET /static/marvis/office-scene-roles-5.png HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Content-Type: text/css; charset=utf-8" in response
    assert "Cache-Control: public, max-age=300, stale-while-revalidate=60" in response
    assert "HTTP/1.1 200 OK" in image_response
    assert "Content-Type: image/png" in image_response
    assert "Cache-Control: public, max-age=300, stale-while-revalidate=60" in image_response
    assert "HTTP/1.1 200 OK" in office_image_response
    assert "Content-Type: image/png" in office_image_response
    assert "Cache-Control: public, max-age=300, stale-while-revalidate=60" in office_image_response
    assert "HTTP/1.1 200 OK" in office_empty_slot_response
    assert "Content-Type: image/png" in office_empty_slot_response
    assert (
        "Cache-Control: public, max-age=300, stale-while-revalidate=60"
        in office_empty_slot_response
    )
    assert "HTTP/1.1 200 OK" in office_roles_response
    assert "Content-Type: image/png" in office_roles_response
    assert (
        "Cache-Control: public, max-age=300, stale-while-revalidate=60"
        in office_roles_response
    )


@pytest.mark.asyncio
async def test_native_css_bundle_combines_static_assets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /static/native_app_bundle.css HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Content-Type: text/css; charset=utf-8" in response
    assert "Cache-Control: public, max-age=300, stale-while-revalidate=60" in response
    assert "--bg-root" in response
    assert "@keyframes" in response
    assert ".model-popover" in response


@pytest.mark.asyncio
async def test_regular_responses_close_http_connection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /health HTTP/1.1\r\nHost: test\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Connection: close" in response
    assert "Keep-Alive:" not in response
    assert '"status": "ok"' in response


@pytest.mark.asyncio
async def test_native_provider_page_back_button_returns_to_native_index(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([FakeAntigravityProvider()]),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/antigravity?token=secret HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Cache-Control: no-store, max-age=0" in response
    assert "Pragma: no-cache" in response
    assert 'const PROVIDER = "antigravity";' in response
    assert "function tokenizedPath(path)" in response
    assert 'document.getElementById("back").onclick = () => {' in response
    assert 'location.href = tokenizedPath("/native");' in response
    assert "history.back()" not in response


@pytest.mark.asyncio
async def test_native_public_root_and_page_open_without_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token=None,
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        root_response = await _read_response(
            server.host,
            server.port,
            "GET / HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        page_response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 303 See Other" in root_response
    assert "Location: /native/codex" in root_response
    assert "访问令牌" not in root_response
    assert "HTTP/1.1 200 OK" in page_response
    assert "<title>Codex</title>" in page_response


@pytest.mark.asyncio
async def test_native_public_root_and_page_open_on_loopback_testing_with_token(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=True,
    )
    await server.start()
    try:
        root_response = await _read_response(
            server.host,
            server.port,
            "GET / HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        page_response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 303 See Other" in root_response
    assert "Location: /native/codex" in root_response
    assert "访问令牌" not in root_response
    assert "HTTP/1.1 200 OK" in page_response
    assert "<title>Codex</title>" in page_response
    assert "访问令牌" not in page_response


@pytest.mark.asyncio
async def test_native_sessions_returns_json_with_bearer_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "sessions": [
                {
                    "native_thread_id": "thread-1",
                    "agent_run_id": 42,
                    "activity_at": "2026-05-31T12:39:00+00:00",
                    "updated_at": "2026-05-31T13:00:00+00:00",
                    "metadata": _FAKE_SESSION_METADATA,
                }
            ],
        "native_refresh_pending": False,
        "native_session_source": "daemon",
    }
    assert controller.calls == [("list_sessions",)]


@pytest.mark.asyncio
async def test_native_sessions_returns_cached_snapshot_when_provider_times_out(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = SlowCodexProviderWithCache()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([provider]),
        access_token="secret",
        allow_unauthenticated_loopback=False,
        native_sessions_timeout_seconds=10,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["sessions"] == [
        {
            "native_thread_id": "cached-thread",
            "agent_run_id": 123,
            "activity_at": "2026-05-31T12:39:00+00:00",
            "updated_at": "2026-05-31T13:00:00+00:00",
            "metadata": {"source": "cache"},
        }
    ]
    assert body["native_refresh_pending"] is True
    assert body["native_session_source"] == "cache"
    assert provider.calls == [("list_cached_sessions", 50), ("list_sessions", 50)]


@pytest.mark.asyncio
async def test_native_codex_sessions_background_refresh_indexes_jsonl_sessions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = SlowCodexProviderWithCache()
    mirror = FakeTranscriptMirror()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([provider]),
        access_token="secret",
        allow_unauthenticated_loopback=False,
        native_transcript_mirror=mirror,
        native_sessions_timeout_seconds=10,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert provider.calls == [("list_cached_sessions", 50), ("list_sessions", 50)]
    assert mirror.calls == [("index_recent_sessions", 100)]


@pytest.mark.asyncio
async def test_startup_warmup_syncs_only_two_latest_turn_threads(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    mirror = WarmupTranscriptMirror(["thread-newest", "thread-second", "thread-old"])
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_transcript_mirror=mirror,
    )
    await server.start()
    try:
        for _index in range(20):
            if ("sync_thread", "thread-second", 500) in mirror.calls:
                break
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert ("recent_turn_thread_ids", 2) in mirror.calls
    assert ("sync_thread", "thread-newest", 500) in mirror.calls
    assert ("sync_thread", "thread-second", 500) in mirror.calls
    assert ("sync_thread", "thread-old", 500) not in mirror.calls


@pytest.mark.asyncio
async def test_startup_warmup_skips_thread_with_existing_foreground_sync(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    mirror = WarmupTranscriptMirror(["thread-active", "thread-next"])
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_transcript_mirror=mirror,
    )
    server._native_background_tasks[("native_transcript", "codex", "thread-active")] = (
        asyncio.create_task(asyncio.sleep(1.0))
    )
    await server.start()
    try:
        for _index in range(20):
            if ("sync_thread", "thread-next", 500) in mirror.calls:
                break
            await asyncio.sleep(0.05)
    finally:
        await server.stop()

    assert ("sync_thread", "thread-active", 500) not in mirror.calls
    assert ("sync_thread", "thread-next", 500) in mirror.calls


@pytest.mark.asyncio
async def test_native_sessions_stream_pushes_cached_snapshot_and_refresh(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = PushCodexProviderWithCache()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([provider]),
        access_token="secret",
        allow_unauthenticated_loopback=False,
        native_sessions_timeout_seconds=10,
    )
    await server.start()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(
            b"GET /api/native/codex/sessions/stream HTTP/1.1\r\n"
            b"Host: test\r\n"
            b"Authorization: Bearer secret\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        initial = await asyncio.wait_for(reader.readuntil(b"cached-thread"), timeout=1.0)
        await asyncio.wait_for(provider.refreshed.wait(), timeout=1.0)
        refreshed = await asyncio.wait_for(reader.readuntil(b"fresh-thread"), timeout=1.0)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await server.stop()

    response = (initial + refreshed).decode("utf-8", errors="replace")
    assert "HTTP/1.1 200 OK" in response
    assert "Content-Type: text/event-stream; charset=utf-8" in response
    assert "event: native_sessions" in response
    assert "cached-thread" in response
    assert "fresh-thread" in response
    assert provider.calls == [
        ("list_cached_sessions", 50),
        ("list_sessions", 50),
    ]


@pytest.mark.asyncio
async def test_native_sessions_returns_immediately_with_empty_cache(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = SlowCodexProviderWithoutCache()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([provider]),
        access_token="secret",
        allow_unauthenticated_loopback=False,
        native_sessions_timeout_seconds=10,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["sessions"] == []
    assert body["native_refresh_pending"] is True
    assert body["native_session_source"] == "cache"
    assert provider.calls == [("list_cached_sessions", 50), ("list_sessions", 50)]


@pytest.mark.asyncio
async def test_native_sessions_cache_response_reports_background_refresh_error(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = FailingCodexProviderWithCache()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([provider]),
        access_token="secret",
        allow_unauthenticated_loopback=False,
        native_sessions_timeout_seconds=10,
    )
    await server.start()
    try:
        first_response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
        second_response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in first_response
    first_body = _json_body(first_response)
    assert "native_sync_error" not in first_body
    assert first_body["native_refresh_pending"] is True

    assert "HTTP/1.1 200 OK" in second_response
    second_body = _json_body(second_response)
    assert second_body["native_session_source"] == "cache"
    assert second_body["native_sync_error"] == "boom"
    assert second_body["native_refresh_pending"] is True
    assert provider.calls == [
        ("list_cached_sessions", 50),
        ("list_sessions", 50),
        ("list_cached_sessions", 50),
    ]


@pytest.mark.asyncio
async def test_native_models_route_returns_official_catalog(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/models HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["models"][0]["model"] == "gpt-5.5"
    assert body["models"][0]["supportedReasoningEfforts"][1] == {
        "reasoningEffort": "high",
        "description": "Deep",
    }
    assert body["models"][0]["serviceTiers"][1]["id"] == "fast"
    assert controller.calls == [("list_models",)]


@pytest.mark.asyncio
async def test_native_start_route_creates_project_thread_with_model_settings(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "cwd": "/Users/wl/projects/wlcodex",
            "prompt": "start in this project",
            "model": "gpt-5.5",
            "effort": "high",
            "service_tier": "fast",
            "images": [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["native_thread_id"] == "thread-new"
    assert controller.calls == [
        (
            "start_session",
            "/Users/wl/projects/wlcodex",
            "start in this project",
            "gpt-5.5",
            "high",
            "fast",
            [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_start_route_translates_codex_permission_mode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "cwd": "/Users/wl/projects/wlcodex",
            "prompt": "start read-only",
            "permission_mode": "read_only",
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "start_session",
            "/Users/wl/projects/wlcodex",
            "start read-only",
            None,
            None,
            None,
            None,
            "on-request",
            None,
            "read-only",
            {"type": "readOnly", "networkAccess": False},
        )
    ]


@pytest.mark.asyncio
async def test_native_start_route_passes_codex_collaboration_mode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "cwd": "/Users/wl/projects/wlcodex",
            "prompt": "make a plan",
            "model": "gpt-5.5",
            "collaboration_mode": {"mode": "plan"},
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "start_session",
            "/Users/wl/projects/wlcodex",
            "make a plan",
            "gpt-5.5",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            {"mode": "plan", "settings": {"model": "gpt-5.5"}},
        )
    ]


@pytest.mark.asyncio
async def test_native_start_route_starts_thread_with_image_only_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "cwd": "/Users/wl/projects/wlcodex",
            "prompt": "",
            "model": "gpt-5.5",
            "effort": "high",
            "images": [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["native_thread_id"] == "thread-new"
    assert controller.calls == [
        (
            "start_session",
            "/Users/wl/projects/wlcodex",
            "",
            "gpt-5.5",
            "high",
            None,
            [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_start_route_creates_empty_project_thread_without_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps({"cwd": "/Users/wl/projects/wlcodex"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body_json = _json_body(response)
    assert body_json["native_thread_id"] == "thread-empty"
    assert body_json["status"] == "created"
    assert controller.calls == [
        ("create_session", "/Users/wl/projects/wlcodex", None, None)
    ]


@pytest.mark.asyncio
async def test_native_start_route_passes_thread_permission_to_empty_session(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {"cwd": "/Users/wl/projects/wlcodex", "permission_mode": "full_access"}
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/start HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "create_session",
            "/Users/wl/projects/wlcodex",
            None,
            None,
            "never",
            None,
            "danger-full-access",
            None,
        )
    ]


@pytest.mark.asyncio
async def test_native_continue_posts_json_body_and_returns_control_result(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    timeline_store = NativeTimelineStore(store._conn)
    controller = FakeNativeController()
    body = json.dumps({"prompt": "keep going"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        native_timeline=timeline_store,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "native_thread_id": "thread-1",
        "agent_run_id": 42,
        "turn_id": "turn-2",
        "status": "ok",
    }
    assert controller.calls == [("continue_session", "thread-1", "keep going")]
    items = timeline_store.list_items("codex", "thread-1")
    assert items == []


@pytest.mark.asyncio
async def test_native_continue_route_passes_force_new_turn_flag(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps({"prompt": "keep going", "force_new_turn": True})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["turn_id"] == "turn-2"
    assert controller.calls == [("continue_session", "thread-1", "keep going", True)]


@pytest.mark.asyncio
async def test_native_continue_accepts_chunked_json_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    first = '{"prompt":"keep '
    second = 'going"}'
    request = (
        "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
        "Host: test\r\n"
        "Authorization: Bearer secret\r\n"
        "Content-Type: application/json\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n"
        f"{len(first.encode('utf-8')):x}\r\n"
        f"{first}\r\n"
        f"{len(second.encode('utf-8')):x}\r\n"
        f"{second}\r\n"
        "0\r\n\r\n"
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(server.host, server.port, request)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["turn_id"] == "turn-2"
    assert controller.calls == [("continue_session", "thread-1", "keep going")]


@pytest.mark.asyncio
async def test_native_continue_accepts_model_and_image_attachments(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "prompt": "describe this",
            "model": "gpt-5.5",
            "images": [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response)["turn_id"] == "turn-2"
    assert controller.calls == [
        (
            "continue_session",
            "thread-1",
            "describe this",
            "gpt-5.5",
            None,
            None,
            [{"url": "data:image/png;base64,abc", "filename": "photo.png"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_continue_route_translates_codex_permission_mode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "prompt": "keep going",
            "permission_mode": "full_access",
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "continue_session",
            "thread-1",
            "keep going",
            None,
            None,
            None,
            None,
            "never",
            None,
            {"type": "dangerFullAccess"},
        )
    ]


@pytest.mark.asyncio
async def test_native_continue_route_passes_codex_collaboration_mode(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "prompt": "continue with a plan",
            "model": "gpt-5.5",
            "collaboration_mode": {"mode": "plan"},
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "continue_session",
            "thread-1",
            "continue with a plan",
            "gpt-5.5",
            None,
            None,
            None,
            None,
            None,
            None,
            {"mode": "plan", "settings": {"model": "gpt-5.5"}},
        )
    ]


@pytest.mark.asyncio
async def test_native_status_and_read_routes_return_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        status_response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/status HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        read_response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions/thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in status_response
    assert _json_body(status_response)["remote_control_status"] == "ready"
    assert "HTTP/1.1 200 OK" in read_response
    assert _json_body(read_response)["thread"]["id"] == "thread-1"
    assert controller.calls == [("status",), ("read_session", "thread-1")]


@pytest.mark.asyncio
async def test_native_attach_route_resumes_session_without_prompt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/attach HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "native_thread_id": "thread-1",
        "agent_run_id": 42,
        "turn_id": "turn-1",
        "status": "attached",
    }
    assert controller.calls == [("attach_session", "thread-1")]


@pytest.mark.asyncio
async def test_native_sync_route_projects_server_side_and_returns_compact_result(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/sync HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert _json_body(response) == {
        "native_thread_id": "thread-1",
        "agent_run_id": 42,
        "turn_id": "turn-1",
        "status": "synced",
    }
    assert controller.calls == [("sync_session", "thread-1")]


@pytest.mark.asyncio
async def test_native_steer_interrupt_and_approval_routes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    steer_body = json.dumps({"expected_turn_id": "turn-2", "prompt": "adjust"})
    interrupt_body = json.dumps({"turn_id": "turn-2"})
    approval_body = json.dumps({"action": "approve_once"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        steer_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/steer HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(steer_body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{steer_body}",
        )
        interrupt_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/interrupt HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(interrupt_body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{interrupt_body}",
        )
        approval_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/approvals/req-1/resolve HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(approval_body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{approval_body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in steer_response
    assert _json_body(steer_response)["turn_id"] == "turn-2"
    assert "HTTP/1.1 200 OK" in interrupt_response
    assert _json_body(interrupt_response)["status"] == "interrupted"
    assert "HTTP/1.1 200 OK" in approval_response
    assert _json_body(approval_response) == {
        "codex_request_id": "req-1",
        "status": "resolved",
    }
    assert controller.calls == [
        ("steer_session", "thread-1", "turn-2", "adjust"),
        ("interrupt_session", "thread-1", "turn-2"),
        ("resolve_approval", "req-1", {"action": "approve_once"}),
    ]


@pytest.mark.asyncio
async def test_native_steer_accepts_image_attachments(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = json.dumps(
        {
            "expected_turn_id": "turn-2",
            "prompt": "adjust",
            "model": "gpt-5.5",
            "images": [{"url": "data:image/jpeg;base64,abc"}],
        }
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/steer HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert controller.calls == [
        (
            "steer_session",
            "thread-1",
            "turn-2",
            "adjust",
            "gpt-5.5",
            None,
            None,
            [{"url": "data:image/jpeg;base64,abc"}],
        )
    ]


@pytest.mark.asyncio
async def test_native_approval_route_returns_404_for_unknown_request(
    tmp_path: Path,
) -> None:
    class UnknownApprovalController(FakeNativeController):
        async def resolve_approval(
            self,
            codex_request_id: str,
            response: dict[str, Any],
        ) -> dict[str, Any]:
            raise KeyError(codex_request_id)

    store = _store(tmp_path)
    body = json.dumps({"action": "approve_once"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=UnknownApprovalController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/approvals/missing/resolve HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 404 Not Found" in response
    assert _json_body(response) == {"error": "approval request not found"}


@pytest.mark.asyncio
async def test_native_routes_return_rpc_error_message_instead_of_exception_class(
    tmp_path: Path,
) -> None:
    class RpcFailingController(FakeNativeController):
        async def continue_session(
            self,
            native_thread_id: str,
            prompt: str,
            *,
            model: str | None = None,
            effort: str | None = None,
            service_tier: str | None = None,
            images: list[dict[str, Any]] | None = None,
        ) -> FakeControlResult:
            raise JsonRpcError(-32000, "turn is not accepting input")

    store = _store(tmp_path)
    body = json.dumps({"prompt": "hello"})
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=RpcFailingController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 409" in response
    assert _json_body(response) == {
        "error": "turn is not accepting input",
        "code": -32000,
    }


@pytest.mark.asyncio
async def test_native_continue_accepts_mobile_sized_image_json_body(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    image_payload = "a" * (9 * 1024 * 1024)
    body = json.dumps(
        {
            "prompt": "describe this phone photo",
            "images": [{"url": f"data:image/jpeg;base64,{image_payload}"}],
        }
    )
    assert len(body.encode("utf-8")) > 8 * 1024 * 1024
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
            read_limit=1024 * 1024,
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    call = controller.calls[-1]
    assert call[:6] == (
        "continue_session",
        "thread-1",
        "describe this phone photo",
        None,
        None,
        None,
    )
    assert len(call[6][0]["url"]) > 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_native_post_rejects_oversized_json_body_before_controller_call(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    body = "{}"
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/sessions/thread-1/continue HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {_MAX_BODY_BYTES + 1}\r\n"
            "Connection: close\r\n\r\n"
            f"{body}",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 413 Payload Too Large" in response
    assert _json_body(response) == {"error": "request body too large"}
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_codex_page_contains_worker_and_session_selector(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "<title>Codex</title>" in response
    assert "Codex" in response
    assert 'localStorage.setItem("wlcodexToken", token)' in response
    assert 'const PROVIDER = "codex";' in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "api(`${API_BASE}/sessions`)" in response
    assert "device-chip" in response
    assert "await api(`/api/native/codex/sessions/${encodeURIComponent(selected.native_thread_id)}`).catch" not in response
    assert "const LIVE_PREFETCH_LIMIT = 4;" in response
    assert "function liveUrlForSession(session)" in response
    assert "scheduleLivePrefetch(filtered.slice(0, LIVE_PREFETCH_LIMIT));" in response
    assert "btn.classList.add(\"loading\");" in response


def test_native_codex_home_matches_remote_mobile_session_status_shape() -> None:
    response = _native_codex_page("codex")

    assert "background: #000;" in response
    assert ".topbar { position: relative; display: grid; grid-template-columns: 54px 1fr 54px;" in response
    assert ".circle { width: 54px; min-height: 54px;" in response
    assert "h1 { margin: 0; text-align: center; font-size: 22px;" in response
    assert ".device-chip { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 8px; max-width: 82vw; min-height: 44px;" in response
    assert ".nav-row, .project, .recent { position: relative; display: grid; grid-template-columns: 40px minmax(0, 1fr) auto;" in response
    assert ".icon-folder { width: 26px; height: 20px; border: 2.4px solid var(--text-primary);" in response
    assert ".icon-chat { width: 27px; height: 27px; border: 2.4px solid var(--text-primary); border-radius: 50%;" in response
    assert '<span class="chat-chevron"></span><span class="chat-prompt-dot"></span>' in response
    assert ".chat-chevron:before, .chat-chevron:after" in response
    assert ".label { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 17px;" in response
    assert ".recent { grid-template-columns: minmax(0, 1fr) 32px;" in response
    assert ".attach-button:before, .attach-button:after" in response
    assert 'controlsEl.dataset.view = viewMode;' in response
    assert 'body[data-native-view="home"] .attach-button' in response
    assert 'body[data-native-view="history"] .attach-button' in response
    assert 'body[data-native-view="compose"] .compose-hero' in response
    assert 'function openHistory(cwd, label)' in response
    assert 'function openCompose(cwd)' in response
    assert 'function showHome()' in response
    assert 'if (viewMode !== "compose") {' in response
    assert 'openCompose(selectedProjectCwd);' in response
    assert 'promptEl.placeholder = viewMode === "compose" ? "接下来我们该写什么代码？" : "搜索聊天";' in response
    assert 'if (viewMode === "compose") updateStartControls();' in response
    assert 'chatRow.onclick = () => openHistory("", "聊天");' in response
    assert '<section class="compose-hero" id="composeHero" hidden>' in response
    assert '<h2>开始处理</h2>' in response
    assert 'id="composeProjectButton"' in response
    assert '<section class="project-picker" id="projectPicker" hidden aria-label="选择项目">' in response
    assert 'function openProjectPicker()' in response
    assert 'function closeProjectPicker()' in response
    assert 'function renderProjectPicker()' in response
    assert 'function selectComposeProject(cwd)' in response
    assert 'composeProjectButton.onclick = openProjectPicker;' in response
    assert 'let noProjectSelected = false;' in response
    assert 'const label = selectedProjectCwd ? lastPath(selectedProjectCwd) : (noProjectSelected ? "无项目" : "选择项目");' in response
    assert 'const selectedMark = isProjectPickerRowSelected(cwd) ? "\\u2713" : "";' in response
    assert 'function isProjectPickerRowSelected(cwd)' in response
    assert 'noProjectSelected = !selectedProjectCwd;' in response
    assert 'const selectedMark = String(cwd || "") === selectedProjectCwd ? "\\u2713" : "";' not in response
    assert 'const selectedMark = String(cwd || "") === selectedProjectCwd ? "<svg' not in response
    assert 'button.project-picker-row:not(.secondary):not(.warn):not(:disabled):hover { background: transparent; filter: none; }' in response
    assert 'button.project-picker-row:not(.secondary):not(.warn):not(:disabled):active { background: transparent; filter: none; transform: none; }' in response
    assert 'body[data-native-view="compose"] .controls.has-draft button.chat { display: grid;' in response
    assert 'body[data-native-view="compose"] .controls.has-draft .mic-icon { display: none; }' in response
    assert 'controlsEl.classList.toggle("has-draft", viewMode === "compose" && hasDraft);' in response
    assert 'sendButton.innerHTML = viewMode === "compose" ? ICONS.send : \'<span class="compose-icon" aria-hidden="true"></span><span>聊天</span>\';' in response
    assert 'id="projectPickerRecent"' in response
    assert 'id="projectPickerCancel"' in response
    assert '当前目录' in response
    assert '无项目' in response
    assert '<span>工作区</span>' in response
    assert '<span>工作树</span>' in response
    assert ".search-wrap { position: relative; min-width: 0;" in response
    assert ".search-icon { position: absolute; left: 20px; top: 50%;" in response
    assert ".search-icon:before" in response
    assert '<span class="compose-icon" aria-hidden="true"></span><span>聊天</span>' in response
    assert ".compose-icon:before" in response
    assert ".recent-status.running::before" in response
    assert "border-right-color: var(--native-remote-blue);" in response
    assert ".recent-status.finished::before" in response
    assert "background: var(--native-remote-red);" in response
    assert "function sessionVisualStateClass(session)" in response
    assert "function hasViewedSession(session)" in response
    assert "markSessionViewed(session);" in response
    assert 'window.addEventListener("pageshow"' in response
    assert "status === \"running\" || status === \"in_progress\" || status === \"queued\"" in response
    assert "isUnreadCompletedSession(session)" in response
    assert 'status === "idle" && hasReviewableTurn(session)' in response
    assert "function hasReviewableTurn(session)" in response
    assert 'statusEl.className = `recent-status ${sessionVisualStateClass(session)}`;' in response


def test_native_template_registry_renders_stable_and_timeline_v2_variants() -> None:
    stable = render_native_template(
        "codex",
        "stable",
        {
            "stable_renderer": lambda provider, theme="": f"stable:{provider}:{theme}",
            "theme": "marvis",
        },
    )
    v2 = render_native_template("codex", "timeline_v2", {"initial_events": []})

    assert stable == "stable:codex:marvis"
    assert 'data-native-template="timeline-v2"' in v2
    assert 'const API_BASE = "/api/native/codex";' in v2
    assert "function renderLocalUserEcho" not in v2
    assert "wlcodexNativeTimelineV2State" in v2
    assert "nativeTimelinePath(`after=${latestEventId}&limit=100`)" in v2


def test_native_codex_stable_template_does_not_include_timeline_v2_state() -> None:
    response = _native_codex_page("codex")

    assert 'data-native-template="timeline-v2"' not in response
    assert "wlcodexNativeTimelineV2State" not in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "scheduleLivePrefetch(filtered.slice(0, LIVE_PREFETCH_LIMIT));" in response


@pytest.mark.asyncio
async def test_native_codex_v2_route_is_isolated_from_stable_local_echo(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        native_timeline=NativeTimelineStore(store._conn),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex-v2 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "<title>Codex Timeline V2</title>" in response
    assert 'data-native-template="timeline-v2"' in response
    assert "function renderLocalUserEcho" not in response
    assert "renderLocalUserEcho(" not in response
    assert "等待 timeline 确认" in response


@pytest.mark.asyncio
async def test_native_codex_v2_initial_render_uses_native_timeline_items(
    tmp_path: Path,
) -> None:
    runtime_store = _store(tmp_path)
    timeline_store = NativeTimelineStore(runtime_store._conn)
    runtime_store.add_projector(timeline_store.project_runtime_event)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "timeline 用户消息",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=42,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type=EventType.MODEL_MESSAGE_COMPLETED,
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "text": "timeline 助手回复",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:01+00:00",
            agent_run_id=42,
        )
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_controller=FakeNativeController(),
        native_timeline=timeline_store,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex-v2?native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "timeline 用户消息" in response
    assert "timeline 助手回复" in response
    assert "provider.raw.frame" not in response


def test_native_provider_home_does_not_expose_marvis_theme_entry() -> None:
    response = _native_codex_page("codex")
    marvis_response = _native_codex_page("codex", theme="marvis")

    assert 'id="themeToggle"' not in response
    assert 'class="circle theme-toggle"' not in response
    assert 'const THEME_STORAGE_KEY = "wlcodex:native-theme";' not in response
    assert "function applyThemePreference(theme)" not in response
    assert "function toggleTheme()" not in response
    assert "themeToggle.onclick = toggleTheme;" not in response
    assert 'if (currentTheme()) params.set("theme", currentTheme());' not in response
    assert 'data-theme="marvis"' not in marvis_response
    assert '<link rel="stylesheet" href="/static/marvis.css">' not in marvis_response
    assert "<title>Codex</title>" in marvis_response


@pytest.mark.asyncio
async def test_live_page_shell_is_not_cacheable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Cache-Control: no-store, max-age=0" in response
    assert "Pragma: no-cache" in response
    assert "<title>Codex</title>" in response
    assert "codex-run-shell" in response


def test_worker_live_page_matches_remote_mobile_running_header_and_dock_shape() -> None:
    response = _live_page(42, native_provider="codex")

    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1, '
        'maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        in response
    )
    assert (
        "html, body, .native-mobile-shell, .codex-run-shell, .codex-transcript, "
        ".transcript-body, .codex-input-dock, input, textarea { -webkit-text-size-adjust: 100%; "
        "text-size-adjust: 100%; }"
        in response
    )
    assert '<link rel="manifest" href="/native/manifest.webmanifest">' in response
    assert '<meta name="theme-color" content="#000000">' in response
    assert '<meta name="mobile-web-app-capable" content="yes">' in response
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in response
    assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in response
    assert "--native-top-control-y: calc(14px + env(safe-area-inset-top));" in response
    assert "--native-top-control-size: 46px;" in response
    assert ".circle { width: var(--native-top-control-size); height: var(--native-top-control-size);" in response
    assert "#back { position: fixed; top: var(--native-top-control-y);" in response
    assert ".session-float { position: fixed; top: var(--native-top-control-y);" in response
    assert ".session-float-title { min-width: 0; overflow: hidden; text-overflow: ellipsis;" in response
    assert ".session-float-state { flex: 0 0 auto; color: #d0d0d4;" in response
    assert ".session-float-run-id { flex: 0 0 auto; margin-left: auto;" in response
    assert ".header-run-indicator { position: fixed; top: var(--native-top-control-y);" in response
    assert "left: clamp(70px, 18.5vw, 74px); right: clamp(112px, 29vw, 118px);" in response
    assert "grid-template-columns: 27px 27px;" in response
    assert "width: 94px; height: var(--native-top-control-size); min-height: var(--native-top-control-size);" in response
    assert ".header-run-spinner { width: 23px; height: 23px; border: 3px solid #5a5b60;" in response
    assert ".header-run-indicator.running .header-run-spinner" in response
    assert "border-right-color: var(--native-remote-blue);" in response
    assert ".header-run-indicator.finished .header-run-dot" in response
    assert 'tone === "busy" ? "running"' in response
    assert 'tone === "failed" || tone === "done" ? "finished"' in response
    assert "background: var(--native-remote-red);" in response
    assert ".primary-action { position: absolute; right: 4px; bottom: 4px; width: 36px; min-height: 36px; border-radius: 50%;" in response
    assert ".primary-action.stop { background: #f4f4f5; color: #050505;" in response
    assert 'id="sessionFloat"' in response
    assert 'id="sessionFloatState">连接会话</span>' in response
    assert '<span class="session-float-run-id">#42</span>' in response
    assert 'id="runStatus"' not in response
    assert 'id="runStateLabel"' not in response
    assert "codex-status-flow" not in response
    assert 'id="headerRunIndicator"' in response
    assert "function updateHeaderRunIndicator(tone)" in response
    assert 'headerRunIndicator.className = "header-run-indicator " + visual;' in response
    assert "updateHeaderRunIndicator(tone || \"neutral\");" in response


def test_worker_live_page_matches_native_codex_mobile_composer_layout() -> None:
    response = _live_page(42, native_provider="codex")

    assert ".codex-input-dock { position: fixed; left: 0; right: 0; bottom: 0; z-index: 4; display: grid; gap: 6px; padding: 12px 18px 20px;" in response
    assert "border-top: 0;" in response
    assert '<div class="attachment-strip" id="attachmentStrip" hidden></div>\n      <div class="composer-tools">' in response
    assert ".composer-tools { display: flex; gap: 10px; align-items: center; min-width: 0; padding: 0;" in response
    assert ".composer-settings { position: relative; flex: 1; display: grid; grid-template-columns: minmax(128px, 1.2fr) minmax(96px, 1fr) minmax(96px, 1fr); gap: 8px; min-width: 0; max-width: 100%;" in response
    assert ".setting-pill { width: 100%; min-width: 0; min-height: 36px; border-radius: 18px; padding: 0 8px; overflow: hidden;" in response
    assert "font-size: 13px; font-weight: var(--weight-extrabold);" in response
    assert 'modelSettingsButton.textContent = [modelText, effortText].filter(Boolean).join(" ");' in response
    assert 'modelSettingsButton.textContent = summaryParts.join(" ");' not in response
    assert ".setting-pill.handoff { flex: 0 0 auto;" in response
    assert 'id="handoffButton" type="button">接棒执行</button>' in response
    assert ".dock-row { position: relative; display: grid; grid-template-columns: 44px minmax(0, 1fr); gap: 10px;" in response
    assert ".attach-button { width: 44px; min-height: 44px;" in response
    assert "#prompt { flex: 1; min-width: 0; min-height: 44px; max-height: 132px; border-radius: 22px;" in response
    assert "padding: 9px 48px 9px 18px; font-size: 18px; line-height: 24px;" in response
    assert ".primary-action { position: absolute; right: 4px; bottom: 4px; width: 36px; min-height: 36px;" in response
    assert ".primary-action svg { width: 25px; height: 25px; stroke-width: 2.5;" in response
    assert '<textarea id="prompt" rows="1" placeholder="继续 Codex 会话"></textarea>' in response
    assert "function resizePromptInput()" in response
    assert 'promptInput.style.height = "auto";' in response
    assert 'promptInput.style.height = `${Math.min(Math.max(promptInput.scrollHeight, 44), 132)}px`;' in response
    assert '<div class="dock-row">\n        <button class="attach-button" id="attachmentButton"' in response
    assert ".composer-action-menu { position: fixed; left: 26px; right: 72px; bottom: calc(110px + env(safe-area-inset-bottom));" in response
    assert "padding: 26px 38px 28px;" in response
    assert ".composer-menu-item { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 22px; align-items: center; width: 100%; min-height: 74px;" in response
    assert ".composer-menu-action { min-height: 86px; }" in response
    assert ".composer-menu-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 0; background: transparent;" in response
    assert ".composer-menu-title { display: block; min-width: 0; color: var(--btn-primary-bg); font-size: 22px;" in response
    assert ".composer-menu-desc { display: block; margin-top: 7px; min-width: 0; color: var(--text-dim); font-size: 17px;" in response
    assert ".plugin-dot { width: 42px; height: 42px; border-radius: 10px; background: transparent;" in response
    assert 'class="composer-menu-item composer-menu-action" id="menuUploadPhoto"' in response
    assert 'class="composer-menu-item composer-menu-action" id="menuPlanMode"' in response


def test_worker_live_page_exposes_viewport_debug_diagnostics() -> None:
    response = _live_page(42, native_provider="codex")

    assert 'const debugViewport = params.get("debug_viewport") === "1";' in response
    assert 'id="viewportDebug"' in response
    assert "function collectViewportDebugMetrics()" in response
    assert "visualViewport: window.visualViewport ? {" in response
    assert "computedCircleSize: computedSize(document.querySelector(\".circle\"))" in response
    assert "computedTranscriptSize: computedSize(document.querySelector(\".transcript-body\"))" in response
    assert "computedDockSize: computedSize(inputDock)" in response
    assert "window.visualViewport.addEventListener(\"resize\", updateViewportDebug);" in response


def test_native_codex_page_exposes_standalone_app_metadata() -> None:
    response = _native_codex_page("codex")

    assert '<link rel="manifest" href="/native/manifest.webmanifest">' in response
    assert '<meta name="theme-color" content="#000000">' in response
    assert '<meta name="mobile-web-app-capable" content="yes">' in response
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in response
    assert '<meta name="apple-mobile-web-app-title" content="WLCodex">' in response


def test_native_app_manifest_uses_standalone_display_and_native_start_url() -> None:
    manifest = json.loads(_native_app_manifest())

    assert manifest["name"] == "WLCodex Native"
    assert manifest["short_name"] == "WLCodex"
    assert manifest["start_url"] == "/native/codex"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["display_override"] == ["standalone", "fullscreen", "browser"]
    assert manifest["theme_color"] == "#000000"
    assert manifest["background_color"] == "#000000"
    assert manifest["icons"] == [
        {
            "src": "/native/icon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }
    ]


@pytest.mark.asyncio
async def test_native_manifest_and_icon_routes_are_served(tmp_path: Path) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        manifest_response = await _read_response(
            server.host,
            server.port,
            "GET /native/manifest.webmanifest HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        icon_response = await _read_response(
            server.host,
            server.port,
            "GET /native/icon.svg HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in manifest_response
    assert "Content-Type: application/manifest+json; charset=utf-8" in manifest_response
    assert '"display": "standalone"' in manifest_response
    assert "HTTP/1.1 200 OK" in icon_response
    assert "Content-Type: image/svg+xml; charset=utf-8" in icon_response
    assert "<svg" in icon_response


def test_worker_live_page_exposes_header_context_and_session_actions() -> None:
    response = _live_page(42, native_provider="codex")

    assert 'id="headerContextButton"' in response
    assert 'id="headerSessionMenuButton"' in response
    assert 'id="contextInfoPopover"' in response
    assert 'class="context-info-sheet"' in response
    assert ".context-info-sheet { position: fixed; left: 0; right: 0; bottom: 0; z-index: 30; display: grid; max-height: min(54vh, 420px);" in response
    assert ".context-info-row { display: grid; grid-template-columns: 158px minmax(0, 1fr); gap: 10px;" in response
    assert ".context-info-value { min-width: 0; color: #f4f4f5; font: 700 13px/1.45 var(--font-mono);" in response
    assert 'id="contextInfoClose"' in response
    assert 'id="contextThreadCopyButton"' in response
    assert 'id="sessionActionMenu"' in response
    assert "状态</h2>" in response
    assert "对话线程:" in response
    assert "目录:" in response
    assert "上下文:" in response
    assert "5 小时限制:" in response
    assert "7 天限制:" in response
    assert "复制会话 ID" in response
    assert "<span>置顶</span>" not in response
    assert "<span>重命名</span>" not in response
    assert "<span>归档</span>" not in response
    assert "pinSessionButton" not in response
    assert "renameSessionButton" not in response
    assert "archiveSessionButton" not in response
    assert "unavailableSessionAction" not in response
    assert "暂未接入" not in response
    assert "function toggleContextInfoPopover()" in response
    assert "function nativeContextUsageSummary()" in response
    assert "function nativeLimitSummary(kind)" in response
    assert "function toggleSessionActionMenu()" in response
    assert "function copyNativeSessionId()" in response


def test_worker_live_page_rejects_invalid_native_thread_id_before_attach() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isValidNativeThreadId(value)" in response
    assert "let invalidNativeThreadId = Boolean(nativeThreadId && !isValidNativeThreadId(nativeThreadId));" in response
    assert 'nativeThreadId = "";' in response
    assert 'renderStatus("native_session_invalid", "会话链接无效，请从最近会话重新打开");' in response
    assert "if (invalidNativeThreadId) return;" in response
    assert "if (!nativeThreadId || invalidNativeThreadId) return;" in response


def test_worker_live_page_marks_native_session_viewed_on_open() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function markNativeSessionViewed(threadId)" in response
    assert "markNativeSessionViewed(nativeThreadId);" in response
    assert '"wlcodex:native-session-viewed:" + PROVIDER + ":" + threadId' in response


@pytest.mark.asyncio
async def test_sse_stream_is_not_cacheable_and_not_buffered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hub = WorkerLiveStreamHub(store)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=hub,
    )

    class FakeWriter:
        def __init__(self) -> None:
            self.body = bytearray()
            self.ready = asyncio.Event()

        def write(self, data: bytes) -> None:
            self.body.extend(data)
            if b": connected\n\n" in self.body:
                self.ready.set()

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    writer = FakeWriter()
    task = asyncio.create_task(server._send_sse(writer, 42, 0))
    try:
        await asyncio.wait_for(writer.ready.wait(), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    response = writer.body.decode("utf-8", errors="replace")
    assert "HTTP/1.1 200 OK" in response
    assert "Content-Type: text/event-stream; charset=utf-8" in response
    assert "Cache-Control: no-cache" in response
    assert "X-Accel-Buffering: no" in response
    assert ": connected\n\n" in response


@pytest.mark.asyncio
async def test_sse_stream_watches_native_transcript_file_changes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    hub = WorkerLiveStreamHub(store)
    mirror = FakeTranscriptMirror()
    mirror.thread_signatures["thread-1"] = "thread:0"
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=hub,
        native_transcript_mirror=mirror,
    )

    class FakeWriter:
        def __init__(self) -> None:
            self.body = bytearray()
            self.ready = asyncio.Event()

        def write(self, data: bytes) -> None:
            self.body.extend(data)
            if b": connected\n\n" in self.body:
                self.ready.set()

        async def drain(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    writer = FakeWriter()
    task = asyncio.create_task(
        server._send_sse(
            writer,
            42,
            0,
            native_thread_id="thread-1",
            native_provider="codex",
        )
    )
    try:
        await asyncio.wait_for(writer.ready.wait(), timeout=1.0)
        for _index in range(20):
            if ("thread_file_signature", "thread-1") in mirror.calls:
                break
            await asyncio.sleep(0.1)
        mirror.thread_signatures["thread-1"] = "thread:1"
        for _index in range(20):
            if ("sync_thread", "thread-1") in mirror.calls:
                break
            await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert ("thread_file_signature", "thread-1") in mirror.calls
    assert ("sync_thread", "thread-1") in mirror.calls


@pytest.mark.asyncio
async def test_native_root_and_unauthorized_page_show_token_entry(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        root_response = await _read_response(
            server.host,
            server.port,
            "GET / HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        native_response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in root_response
    assert "<title>WLCodex</title>" in root_response
    assert "localStorage.getItem(\"wlcodexToken\")" in root_response
    assert 'location.replace("/native/codex")' in root_response
    assert "HTTP/1.1 401 Unauthorized" in native_response
    assert "Content-Type: text/html; charset=utf-8" in native_response
    assert "访问令牌" in native_response
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_one_time_login_ticket_sets_cookie_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        ticket_response = await _read_response(
            server.host,
            server.port,
            "POST /api/native/codex/login-ticket HTTP/1.1\r\n"
            "Host: test\r\n"
            "Authorization: Bearer secret\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        ticket_body = _json_body(ticket_response)
        login_path = ticket_body["path"]
        first_open = await _read_response(
            server.host,
            server.port,
            f"GET {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        second_open = await _read_response(
            server.host,
            server.port,
            f"GET {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Connection: close\r\n\r\n",
        )
        first_login = await _read_response(
            server.host,
            server.port,
            f"POST {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        second_login = await _read_response(
            server.host,
            server.port,
            f"POST {login_path} HTTP/1.1\r\n"
            "Host: test\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        cookie_login = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\n"
            "Cookie: wlcodex_token=secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in ticket_response
    assert ticket_body["expires_in"] > 0
    assert login_path.startswith("/native/codex/login?ticket=")
    assert "secret" not in login_path
    assert "HTTP/1.1 200 OK" in first_open
    assert "进入 Codex" in first_open
    assert "HTTP/1.1 200 OK" in second_open
    assert "HTTP/1.1 303 See Other" in first_login
    assert "Location: /native/codex" in first_login
    assert "Set-Cookie: wlcodex_token=secret;" in first_login
    assert "HTTP/1.1 401 Unauthorized" in second_login
    assert "HTTP/1.1 200 OK" in cookie_login
    assert "<title>Codex</title>" in cookie_login


@pytest.mark.asyncio
async def test_native_codex_page_uses_project_context_for_new_chat(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "let selectedProjectCwd = \"\";" in response
    assert "function selectProject(cwd)" in response
    assert "function openHistory(cwd, label)" in response
    assert "function openCompose(cwd)" in response
    assert "btn.onclick = () => openHistory(cwd, label || lastPath(cwd));" in response
    assert "projectNewChat.hidden = true;" in response
    assert "sessionProjectKey(session) === selectedProjectCwd" in response
    assert 'id="projectNewChat"' in response
    assert "function renderProjectAction()" in response
    assert "async function handleProjectNewChat()" in response
    assert "async function startNewChat(prompt)" in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "api(`${API_BASE}/sessions/start`" in response
    assert 'chatRow.onclick = () => openHistory("", "聊天");' in response
    assert "document.querySelector(\".controls\")" in response
    assert "const SESSION_PREVIEW_LIMIT = 10;" in response
    assert "renderSessionList(filtered.slice(0, SESSION_PREVIEW_LIMIT)" in response
    assert 'details.className = "more-sessions";' in response
    assert "更多聊天" in response
    assert ".label { display: block;" in response
    assert ".recent-title { display: -webkit-box;" in response
    assert "-webkit-line-clamp: 2;" in response
    assert 'class="label recent-title"' in response
    assert "relativeTime(sessionActivityAt(session))" in response
    assert "Date.parse(sessionActivityAt(right))" in response
    assert "return session.activity_at || session.updated_at || \"\";" in response


@pytest.mark.asyncio
async def test_native_codex_page_keeps_workspace_selection_dark_and_layout_stable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "button.project:not(.secondary):not(.warn):not(:disabled):hover" in response
    assert "button.project.active" in response
    assert "button.project.active::before" in response
    assert "filter: none;" in response
    assert "border-left: 3px solid var(--color-link);" not in response


@pytest.mark.asyncio
async def test_native_codex_project_new_chat_starts_even_without_draft(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "async function handleProjectNewChat()" in response
    assert "await startNewChat(promptEl.value.trim());" in response
    assert "projectNewChat.onclick = handleProjectNewChat;" in response
    assert 'await startNewChat("");' not in response


@pytest.mark.asyncio
async def test_native_provider_index_page_exposes_model_settings_for_new_sessions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'id="modelSettingsButton"' in response
    assert (
        '<button class="setting-pill permissions" id="permissionSettingsButton" '
        'type="button">自动审核</button>'
    ) in response
    assert 'id="modelPopover"' in response
    assert 'id="permissionPopover"' in response
    assert 'id="permissionSelector"' in response
    assert 'id="permissionOptions"' in response
    assert 'id="permissionSettingRow"' not in response
    assert 'id="modelSelector"' in response
    assert 'id="reasoningSelector"' in response
    assert 'id="serviceTierSelector"' in response
    assert 'id="attachmentButton"' in response
    assert 'id="composerActionMenu"' in response
    assert 'id="menuUploadPhoto"' in response
    assert 'id="menuPlanMode"' in response
    assert 'id="pluginList"' in response
    assert 'id="selectedPluginStrip"' in response
    assert 'id="pluginAutocomplete"' in response
    assert "上传照片" in response
    assert "计划模式" in response
    assert "插件" in response
    assert 'id="imageInput"' in response
    assert 'id="attachmentStrip"' in response
    assert "async function loadModelCatalog()" in response
    assert "api(`${API_BASE}/models`)" in response
    assert "function renderReasoningAndSpeed" in response
    assert "function highestReasoningEffort" in response
    assert "function preferredReasoningEffortDefault" in response
    assert "preferredReasoningEffortDefault(model, efforts)" in response
    assert "function updateSettingVisibility" in response
    assert "reasoningSettingRow.hidden = reasoningSelector.options.length <= 1;" in response
    assert "serviceTierSettingRow.hidden = serviceTierSelector.options.length <= 1;" in response
    assert "function readSelectedModelSettings()" in response
    assert "function readSelectedPermissionSettings()" in response
    assert "function renderPermissionSettings()" in response
    assert "const PERMISSION_SETTINGS_STORAGE_KEY" in response
    assert 'const DEFAULT_PERMISSION_MODE = "auto_review";' in response
    assert "const PERMISSION_SETTINGS_STORAGE_VERSION = 2;" in response
    assert "permissionOptions.hidden = false;" in response
    assert "saveModelSettingsIfChanged();" in response
    assert "savePermissionSettingsIfChanged();" in response
    assert "if (settings.model) body.model = settings.model;" in response
    assert "if (settings.effort) body.effort = settings.effort;" in response
    assert "if (settings.service_tier) body.service_tier = settings.service_tier;" in response
    assert "if (attachmentsForSend.length) {" in response
    assert "body.images = attachmentsForSend.map(image => ({" in response
    assert "function selectComposerPlugin(item)" in response
    assert "function pluginAutocompleteMatches(query)" in response
    assert "function promptHasPluginMention(value, mention)" in response
    assert "function updatePluginAutocomplete()" in response
    assert "row.onclick = () => selectComposerPlugin(item);" in response
    assert "replacePromptPluginQuery(item);" in response
    assert "renderSelectedPlugins();" in response
    assert "selectedPlugins: selectedPlugins.map(plugin => ({...plugin}))" in response
    assert "selectedPlugins = composerSnapshot.selectedPlugins.map(plugin => ({...plugin}));" in response
    assert (
        "imageAttachments = composerSnapshot.imageAttachments.map(image => ({...image}));\n"
        "        selectedPlugins = composerSnapshot.selectedPlugins.map(plugin => ({...plugin}));\n"
        "        renderAttachments();\n"
        "        renderSelectedPlugins();"
    ) in response
    assert "const COLLABORATION_MODE_STORAGE_KEY" in response
    assert "function readSelectedCollaborationMode()" in response
    assert "body.collaboration_mode = collaborationMode;" in response
    assert 'return {"mode": selectedCollaborationMode === "plan" ? "plan" : "default", "settings": {"model": settings.model}};' in response
    assert 'planModeCheck.innerHTML = enabled ? ICONS.check : "";' in response
    assert 'class="mode-chip plan-mode-chip" id="planModeChip" hidden' in response
    assert 'id="planModeChipCancel"' in response
    assert "function setSelectedCollaborationMode(mode)" in response
    assert "planModeChip.hidden = !enabled;" in response
    assert "planModeChipCancel.onclick = () => setSelectedCollaborationMode(\"default\");" in response
    assert "const permissionSettings = readSelectedPermissionSettings();" in response
    assert "let permissionMode = permissionSettings.permission_mode;" in response
    assert "body.permission_mode = permissionMode;" in response
    assert '{"value": "default", "label": "默认权限", "description": "在沙盒中运行命令"}' in response
    assert '{"value": "auto_review", "label": "自动审核", "description": "自动审查提权请求"}' in response
    assert '{"value": "read_only", "label": "只读", "description": "编辑文件或运行命令需要批准"}' in response
    assert '{"value": "full_access", "label": "完全访问权限", "description": "完全访问计算机（风险较高）"}' in response
    assert '"value": "on_request"' not in response
    assert '"value": "never"' not in response
    assert "setting-option-desc" in response
    assert "function readImageAttachment(file)" in response
    assert "function renderAttachments()" in response
    assert "attachmentButton.onclick = toggleComposerActionMenu;" in response
    assert "menuUploadPhoto.onclick = () => {" in response
    assert "imageInput.click();" in response
    assert "const sendButton = document.getElementById(\"send\");" in response
    assert "let startingChat = false;" in response
    assert "function updateStartControls()" in response
    assert 'controlsEl.classList.toggle("has-draft", viewMode === "compose" && hasDraft);' in response
    assert 'sendButton.disabled = startingChat || (viewMode === "compose" && !hasDraft);' in response
    assert "async function handleProjectNewChat()" in response
    assert "await startNewChat(promptEl.value.trim());" in response
    assert "promptEl.focus();" not in response
    assert "promptEl.addEventListener(\"input\", () => {" in response
    assert "updateStartControls();" in response
    assert "button.chat:disabled" in response
    assert "updateHandoffControls();" not in response
    assert "function sessionModelSettingsLabel(session)" in response
    assert "function sessionMetaText(session)" in response
    assert "const metadata = (session && session.metadata) || {};" in response
    assert "reasoningEffortLabel(metadata.effort" in response
    assert "serviceTierLabel(metadata.service_tier" in response
    assert 'const MODEL_SETTINGS_STORAGE_KEY = "wlcodexNativeModelSettings";' in response
    assert "let sessionsRefreshTimer = null;" in response
    assert "data.native_refresh_pending && sessionsRefreshTimer === null" in response
    assert "loadSessions(true);" in response


@pytest.mark.asyncio
async def test_native_provider_home_polling_skips_unchanged_list_rerender(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'let renderedSessionsDataSignature = "";' in response
    assert "function sessionsDataSignature()" in response
    assert "function renderSessionsIfDataChanged()" in response
    assert "const signature = sessionsDataSignature();" in response
    assert "if (signature === renderedSessionsDataSignature) return false;" in response
    assert "await loadSessions(false);" in response
    assert "updated_at: String(session.updated_at || \"\")," not in response
    assert "activity_at: String(session.activity_at || \"\")," not in response


def test_native_provider_home_signature_is_stable_for_all_session_pages() -> None:
    for provider_name in ("codex", "claude", "antigravity"):
        response = _native_codex_page(provider_name)

        signature_body = response.split("function sessionsDataSignature()", 1)[1].split(
            "function renderSessionsIfDataChanged()", 1
        )[0]
        apply_sessions_body = response.split("function applySessionsPayload", 1)[
            1
        ].split("async function loadSessions", 1)[0]
        load_sessions_body = response.split("async function loadSessions", 1)[1].split(
            "async function loadProjects()", 1
        )[0]

        assert "relativeTime(" not in signature_body
        assert "activity_label:" not in signature_body
        assert "renderSessionsIfDataChanged();" in apply_sessions_body
        assert "applySessionsPayload(data, render);" in load_sessions_body
        assert "renderedSessionsDataSignature = sessionsDataSignature();" not in load_sessions_body
        assert "renderNativePage();" not in load_sessions_body


def test_native_provider_home_signature_uses_stable_session_order() -> None:
    response = _native_codex_page("codex")
    signature_body = response.split("function sessionsDataSignature()", 1)[1].split(
        "function renderSessionsIfDataChanged()", 1
    )[0]

    assert "sessions: stableSignatureSessions().map(session => ({" in signature_body
    assert "function stableSignatureSessions()" in signature_body
    assert "sessionDomId(left).localeCompare(sessionDomId(right))" in signature_body
    assert "sessions: sessions.map(session => ({" not in signature_body


def test_native_provider_session_polling_uses_silent_incremental_updates() -> None:
    for provider_name in ("codex", "claude", "antigravity"):
        response = _native_codex_page(provider_name)

        render_changed_body = response.split(
            "function renderSessionsIfDataChanged()", 1
        )[1].split("async function loadHomeData()", 1)[0]
        render_sessions_body = response.split("function renderSessions(", 1)[1].split(
            "function renderSessionList(", 1
        )[0]

        assert "renderSessions({silent: true});" in render_changed_body
        assert "renderProjects();" not in render_changed_body
        assert "function syncSessionList(source, target)" in response
        assert "function createSessionButton(session)" in response
        assert "function updateSessionButton(btn, session)" in response
        assert "const silent = Boolean(options.silent);" in render_sessions_body
        assert "syncSessionList(filtered.slice(0, SESSION_PREVIEW_LIMIT), sessionsEl);" in render_sessions_body
        assert "syncSessionList(filtered.slice(SESSION_PREVIEW_LIMIT), body);" in render_sessions_body


def test_native_provider_projects_load_once_and_session_refresh_is_unified() -> None:
    for provider_name in ("codex", "claude", "antigravity"):
        response = _native_codex_page(provider_name)
        load_home_body = response.split("async function loadHomeData()", 1)[1].split(
            "async function loadModelCatalog()", 1
        )[0]
        startup_body = response.split("loadHomeData();", 1)[1].split("</script>", 1)[0]
        stream_body = response.split("function startSessionsStream()", 1)[1].split(
            "async function loadProjects()", 1
        )[0]

        assert "await loadProjects();" in load_home_body
        assert "renderNativePage();" in load_home_body
        assert "renderSessionsIfDataChanged();" in response
        assert "const SESSION_REFRESH_PENDING_DELAY_MS = 10000;" in response
        assert "const SESSION_POLL_INTERVAL_MS = 30000;" in response
        assert "setInterval(loadHomeData, 15000)" not in response
        assert "setInterval(refreshSessionsSilently, SESSION_POLL_INTERVAL_MS)" in response
        assert "sessions = data.sessions || [];" not in stream_body
        assert "applySessionsPayload(data, true);" in stream_body
        assert "loadProjects();" not in startup_body
        assert "}, SESSION_REFRESH_PENDING_DELAY_MS);" in response


def test_native_provider_home_uses_session_stream_with_polling_fallback() -> None:
    for provider_name in ("codex", "claude", "antigravity"):
        response = _native_codex_page(provider_name)

        assert "let sessionsEventSource = null;" in response
        assert "let sessionsReconnectTimer = null;" in response
        assert "function sessionsStreamPath()" in response
        assert "function closeSessionsStream()" in response
        assert "new EventSource(sessionsStreamPath())" in response
        assert "source.addEventListener(\"native_sessions\"" in response
        assert "applySessionsPayload(data, true);" in response
        assert "renderSessionsIfDataChanged();" in response
        assert "function startSessionsStream()" in response
        assert "startSessionsStream();" in response
        assert 'window.addEventListener("pagehide", closeSessionsStream);' in response
        assert 'window.addEventListener("pageshow", () => startSessionsStream());' in response
        assert "setInterval(refreshSessionsSilently, SESSION_POLL_INTERVAL_MS)" in response
        assert "setInterval(loadHomeData, 3000)" not in response


@pytest.mark.asyncio
async def test_claude_native_provider_plus_menu_exposes_plan_but_hides_plugins(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry(
            [FakeClaudeProvider(), FakeAntigravityProvider()]
        ),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/claude HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'id="menuUploadPhoto"' in response
    assert "上传照片" in response
    assert 'id="menuPlanMode" type="button" role="menuitem" aria-pressed="false"' in response
    assert 'id="pluginMenuSection" hidden' in response
    assert 'id="pluginList" hidden' in response
    assert "const SUPPORTS_PLAN_MODE = true;" in response
    assert "const SUPPORTS_PLUGIN_MENU = false;" in response
    assert "const USES_CLAUDE_PLAN_PERMISSION_MODE = true;" in response
    assert 'if (!SUPPORTS_PLAN_MODE) return "default";' in response
    assert "if (USES_CLAUDE_PLAN_PERMISSION_MODE) return null;" in response
    assert 'if (USES_CLAUDE_PLAN_PERMISSION_MODE && selectedCollaborationMode === "plan") {' in response
    assert 'permissionMode = "plan";' in response


@pytest.mark.asyncio
async def test_antigravity_native_provider_plus_menu_only_exposes_upload_photo(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry(
            [FakeClaudeProvider(), FakeAntigravityProvider()]
        ),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/antigravity HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'id="menuUploadPhoto"' in response
    assert "上传照片" in response
    assert 'id="menuPlanMode" type="button" role="menuitem" hidden aria-pressed="false"' in response
    assert 'id="pluginMenuSection" hidden' in response
    assert 'id="pluginList" hidden' in response
    assert "const SUPPORTS_PLAN_MODE = false;" in response
    assert "const SUPPORTS_PLUGIN_MENU = false;" in response
    assert "const USES_CLAUDE_PLAN_PERMISSION_MODE = false;" in response
    assert 'if (!SUPPORTS_PLAN_MODE) return "default";' in response


@pytest.mark.asyncio
async def test_native_codex_page_filters_session_workspace_projects_to_projects_root(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /native/codex HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROJECTS_URL = "/api/council/projects";' in response
    assert 'let projectRoot = "";' in response
    assert "let projectCatalog = [];" in response
    assert "async function loadProjects()" in response
    assert 'projectRoot = String(data.root || "");' in response
    assert "projectCatalog = Array.isArray(data.projects) ? data.projects : [];" in response
    assert "addProjectOption(project.cwd, project.name);" in response
    assert "for (const project of projectCatalog)" in response
    assert "for (const session of sessions)" in response
    assert "if (!isKnownProjectWorkspace(session.cwd)) continue;" in response
    assert "function isKnownProjectWorkspace(cwd)" in response
    assert "projectCatalog.some(project => String(project.cwd || \"\") === value)" in response
    assert "const normalizedRoot = projectRoot.endsWith(\"/\") ? projectRoot : projectRoot + \"/\";" in response
    assert "return parts.length === 1;" in response
    assert "await loadProjects();" in response
    assert "if (seen.size >= 4) break;" not in response


@pytest.mark.asyncio
async def test_native_routes_return_503_when_controller_is_unavailable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=None,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 503 Service Unavailable" in response
    assert _json_body(response) == {"error": "native controller unavailable"}


@pytest.mark.asyncio
async def test_worker_stream_routes_require_auth_when_token_is_configured(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        access_token="secret",
        allow_unauthenticated_loopback=False,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?after=0 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 401 Unauthorized" in response
    assert _json_body(response) == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_worker_events_schedules_native_thread_sync_before_returning_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _append_worker_event(store, agent_run_id=42)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=80&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["native_sync_error"] == ""
    assert body["native_sync_pending"] is True
    assert controller.calls == [("sync_session", "thread-1")]


@pytest.mark.asyncio
async def test_worker_events_tail_returns_snapshot_when_native_sync_times_out(
    tmp_path: Path,
) -> None:
    class SlowNativeController(FakeNativeController):
        async def sync_session(self, native_thread_id: str) -> FakeControlResult:
            self.calls.append(("sync_session", native_thread_id))
            await asyncio.sleep(2)
            return FakeControlResult(native_thread_id, 42, "turn-1", status="synced")

    store = _store(tmp_path)
    _append_worker_event(store, agent_run_id=42, native_thread_id="thread-1")
    controller = SlowNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
        native_sync_timeout_seconds=10,
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=80&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["events"][0]["payload"]["delta"] == "hello"
    assert body["native_sync_error"] == ""
    assert body["native_sync_pending"] is True
    assert controller.calls == [("sync_session", "thread-1")]


@pytest.mark.asyncio
async def test_worker_events_tail_reports_background_native_sync_error(
    tmp_path: Path,
) -> None:
    class FailingNativeController(FakeNativeController):
        async def sync_session(self, native_thread_id: str) -> FakeControlResult:
            self.calls.append(("sync_session", native_thread_id))
            raise RuntimeError("boom")

    store = _store(tmp_path)
    _append_worker_event(store, agent_run_id=42, native_thread_id="thread-1")
    controller = FailingNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        first_response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=80&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
        second_response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=80&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in first_response
    first_body = _json_body(first_response)
    assert first_body["events"][0]["payload"]["delta"] == "hello"
    assert first_body["native_sync_error"] == ""
    assert first_body["native_sync_pending"] is True

    assert "HTTP/1.1 200 OK" in second_response
    second_body = _json_body(second_response)
    assert second_body["events"][0]["payload"]["delta"] == "hello"
    assert second_body["native_sync_error"] == "boom"
    assert second_body["native_sync_pending"] is True
    assert controller.calls == [("sync_session", "thread-1")]


@pytest.mark.asyncio
async def test_worker_events_poll_does_not_sync_native_thread(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _append_worker_event(store, agent_run_id=42)
    controller = FakeNativeController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?after=0&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert body["native_sync_error"] == ""
    assert body["native_sync_pending"] is False
    assert controller.calls == []


@pytest.mark.asyncio
async def test_native_timeline_endpoint_replays_visible_messages_not_raw_tail(
    tmp_path: Path,
) -> None:
    runtime_store = _store(tmp_path)
    timeline_store = NativeTimelineStore(runtime_store._conn)
    runtime_store.add_projector(timeline_store.project_runtime_event)
    for index in range(120):
        _append_worker_event(
            runtime_store,
            agent_run_id=42,
            event_type="provider.raw.frame",
            native_thread_id="thread-1",
            native_turn_id="turn-1",
            delta=f"raw-{index}",
        )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="user.message.received",
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "用户真实消息",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=42,
        )
    )
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="provider.display.completed",
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "text": "助手真实回复",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=42,
        )
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_controller=FakeNativeController(),
        native_timeline=timeline_store,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions/thread-1/timeline?after=0&limit=20 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert [event["kind"] for event in body["events"]] == [
        "user_message",
        "message_completed",
    ]
    assert body["events"][0]["payload"]["text"] == "用户真实消息"
    assert body["events"][1]["payload"]["text"] == "助手真实回复"
    assert all(event["type"] != "provider.raw.frame" for event in body["events"])


@pytest.mark.asyncio
async def test_native_timeline_endpoint_initial_snapshot_compacts_delta_burst(
    tmp_path: Path,
) -> None:
    runtime_store = _store(tmp_path)
    timeline_store = NativeTimelineStore(runtime_store._conn)
    runtime_store.add_projector(timeline_store.project_runtime_event)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="user.message.received",
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "用户真实消息",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=42,
        )
    )
    for index in range(120):
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type="provider.display.delta",
                aggregate_type="agent_run",
                aggregate_id="42",
                correlation_id="agent:42",
                source="codex",
                actor="codex_native",
                visibility="user",
                payload={
                    "native_thread_id": "thread-1",
                    "native_turn_id": "turn-1",
                    "itemId": "agent-1",
                    "delta": f"片段{index:03d}",
                    "provider": "codex",
                },
                occurred_at="2026-05-30T00:00:00+00:00",
                agent_run_id=42,
            )
        )
    full_text = "最终回复" + ("内容" * 80)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="provider.display.completed",
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "agent-1",
                "text": full_text,
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:01+00:00",
            agent_run_id=42,
        )
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_controller=FakeNativeController(),
        native_timeline=timeline_store,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/codex/sessions/thread-1/timeline?limit=20 HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert [event["kind"] for event in body["events"]] == [
        "user_message",
        "message_completed",
    ]
    assert body["events"][1]["id"] == 122
    assert body["events"][1]["payload"]["text"] == full_text


@pytest.mark.asyncio
async def test_native_timeline_stream_uses_sequence_cursor_and_live_events(
    tmp_path: Path,
) -> None:
    runtime_store = _store(tmp_path)
    timeline_store = NativeTimelineStore(runtime_store._conn)
    runtime_store.add_projector(timeline_store.project_runtime_event)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="user.message.received",
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "第一条",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=42,
        )
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_controller=FakeNativeController(),
        native_timeline=timeline_store,
        access_token="secret",
    )
    await server.start()
    reader = writer = None
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(
            b"GET /api/native/codex/sessions/thread-1/timeline/stream?after=0 HTTP/1.1\r\n"
            b"Host: test\r\nAuthorization: Bearer secret\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=1.0)
        initial = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=1.0)
        assert b"id: 1\n" in initial
        runtime_store.append(
            RuntimeEvent(
                schema_version=1,
                event_type="provider.display.delta",
                aggregate_type="agent_run",
                aggregate_id="42",
                correlation_id="agent:42",
                source="codex",
                actor="codex_native",
                visibility="user",
                payload={
                    "native_thread_id": "thread-1",
                    "native_turn_id": "turn-1",
                    "itemId": "agent-1",
                    "delta": "第二条",
                    "provider": "codex",
                },
                occurred_at="2026-05-30T00:00:01+00:00",
                agent_run_id=42,
            )
        )
        rest = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=1.0)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await server.stop()

    assert b"id: 2\n" in rest
    assert b"event: text_delta\n" in rest
    assert "第二条" in rest.decode("utf-8")


@pytest.mark.asyncio
async def test_native_timeline_stream_does_not_drop_event_between_replay_and_subscribe(
    tmp_path: Path,
) -> None:
    class GapTimelineStore(NativeTimelineStore):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.inject_gap_event = True

        def list_events(self, *args: Any, **kwargs: Any) -> list[Any]:
            events = super().list_events(*args, **kwargs)
            if self.inject_gap_event:
                self.inject_gap_event = False
                self.project_runtime_event(
                    RuntimeEvent(
                        schema_version=1,
                        event_type="provider.display.delta",
                        aggregate_type="agent_run",
                        aggregate_id="42",
                        correlation_id="agent:42",
                        source="codex",
                        actor="codex_native",
                        visibility="user",
                        payload={
                            "native_thread_id": "thread-1",
                            "native_turn_id": "turn-1",
                            "itemId": "agent-1",
                            "delta": "不会丢的片段",
                            "provider": "codex",
                        },
                        occurred_at="2026-05-30T00:00:01+00:00",
                        agent_run_id=42,
                    )
                )
            return events

    runtime_store = _store(tmp_path)
    timeline_store = GapTimelineStore(runtime_store._conn)
    runtime_store.add_projector(timeline_store.project_runtime_event)
    runtime_store.append(
        RuntimeEvent(
            schema_version=1,
            event_type="user.message.received",
            aggregate_type="agent_run",
            aggregate_id="42",
            correlation_id="agent:42",
            source="codex",
            actor="codex_native",
            visibility="user",
            payload={
                "native_thread_id": "thread-1",
                "native_turn_id": "turn-1",
                "itemId": "user-1",
                "text": "第一条",
                "provider": "codex",
            },
            occurred_at="2026-05-30T00:00:00+00:00",
            agent_run_id=42,
        )
    )
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(runtime_store),
        native_controller=FakeNativeController(),
        native_timeline=timeline_store,
        access_token="secret",
    )
    await server.start()
    reader = writer = None
    try:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.write(
            b"GET /api/native/codex/sessions/thread-1/timeline/stream?after=0 HTTP/1.1\r\n"
            b"Host: test\r\nAuthorization: Bearer secret\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=1.0)
        first = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=1.0)
        second = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=1.0)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await server.stop()

    assert b"id: 1\n" in first
    assert b"id: 2\n" in second
    assert "不会丢的片段" in second.decode("utf-8")


@pytest.mark.asyncio
async def test_worker_events_tail_filters_to_current_native_turn(
    tmp_path: Path,
) -> None:
    class CurrentTurnController(FakeNativeController):
        async def sync_session(self, native_thread_id: str) -> FakeControlResult:
            self.calls.append(("sync_session", native_thread_id))
            return FakeControlResult(native_thread_id, 42, "turn-current")

    store = _store(tmp_path)
    _append_worker_event(
        store,
        agent_run_id=42,
        native_thread_id="thread-1",
        native_turn_id="turn-old",
        delta="old turn leaked late",
    )
    _append_worker_event(
        store,
        agent_run_id=42,
        native_thread_id="thread-1",
        native_turn_id="turn-current",
        delta="current turn",
    )
    controller = CurrentTurnController()
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=controller,
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/workers/42/events?tail=80&native_thread_id=thread-1"
            "&native_turn_id=turn-current HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
        await asyncio.sleep(0.1)
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = _json_body(response)
    assert [event["payload"]["delta"] for event in body["events"]] == ["current turn"]
    assert body["previous_event_count"] == 1
    assert controller.calls == [("sync_session", "thread-1")]


@pytest.mark.asyncio
async def test_worker_live_page_accepts_query_token_and_contains_native_controls(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "Cache-Control: no-store, max-age=0" in response
    assert "Pragma: no-cache" in response
    assert 'const streamPathBase = "/api/workers/42/stream";' in response
    assert 'const PROVIDER = "codex";' in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/${action}`" in response
    assert 'params.set("t", String(Date.now()));' in response
    assert "location.href = `/native/${encodeURIComponent(PROVIDER)}?${params.toString()}`;" in response
    assert "`${API_BASE}/approvals/${encodeURIComponent(requestId)}/resolve`" in response
    assert "attachNative" in response
    assert "syncNativeTranscript" in response
    assert "native-mobile-shell" in response
    assert "renderAssistant" in response
    assert "renderCommand" in response


@pytest.mark.asyncio
async def test_worker_live_page_exposes_working_codex_permission_settings(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert (
        '<button class="setting-pill permissions" id="permissionSettingsButton" '
        'type="button">自动审核</button>'
    ) in response
    assert 'id="permissionPopover"' in response
    assert 'id="permissionSelector"' in response
    assert 'id="permissionOptions"' in response
    assert 'id="permissionSettingRow"' not in response
    assert "const PERMISSION_SETTINGS_STORAGE_KEY" in response
    assert 'const DEFAULT_PERMISSION_MODE = "auto_review";' in response
    assert "const PERMISSION_SETTINGS_STORAGE_VERSION = 2;" in response
    assert "function readSelectedPermissionSettings()" in response
    assert "function renderPermissionSettings()" in response
    assert "permissionOptions.hidden = false;" in response
    assert "savePermissionSettingsIfChanged();" in response
    assert "const permissionSettings = readSelectedPermissionSettings();" in response
    assert "let permissionMode = permissionSettings.permission_mode;" in response
    assert "body.permission_mode = permissionMode;" in response
    assert '{"value": "default", "label": "默认权限", "description": "在沙盒中运行命令"}' in response
    assert '{"value": "auto_review", "label": "自动审核", "description": "自动审查提权请求"}' in response
    assert '{"value": "read_only", "label": "只读", "description": "编辑文件或运行命令需要批准"}' in response
    assert '{"value": "full_access", "label": "完全访问权限", "description": "完全访问计算机（风险较高）"}' in response
    assert '"value": "on_request"' not in response
    assert '"value": "never"' not in response
    assert "setting-option-desc" in response


@pytest.mark.asyncio
async def test_worker_live_page_uses_native_codex_run_interaction_model(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "codex-transcript" in response
    assert "codex-status-flow" not in response
    assert 'id="sessionFloatState">连接会话</span>' in response
    assert '<span class="session-float-run-id">#42</span>' in response
    assert "codex-tool-call" in response
    assert "codex-input-dock" in response
    assert "function renderTranscript" in response
    assert "function renderStatusEvent" in response
    assert "function renderStatus(kind, text)" in response
    assert "function renderToolCall" in response
    assert 'id="modelSettingsButton"' in response
    assert 'id="modelPopover"' in response
    assert 'id="modelSelector"' in response
    assert 'id="imageInput"' in response
    assert 'id="attachmentButton"' in response
    assert 'id="composerActionMenu"' in response
    assert 'id="menuUploadPhoto"' in response
    assert 'id="menuPlanMode"' in response
    assert 'id="pluginList"' in response
    assert 'id="selectedPluginStrip"' in response
    assert 'id="pluginAutocomplete"' in response
    assert "上传照片" in response
    assert "计划模式" in response
    assert "插件" in response
    assert "--native-top-control-y" in response
    assert "#back { position: fixed; top: var(--native-top-control-y);" in response
    assert ".session-float { position: fixed; top: var(--native-top-control-y);" in response
    assert ".header-run-indicator { position: fixed; top: var(--native-top-control-y);" in response
    assert "m22 13-1.3-1.3" in response
    assert "m3 17 2 2 4-4" in response
    assert '<span class="composer-menu-icon">▧</span>' not in response
    assert '<span class="composer-menu-icon">☷</span>' not in response
    assert 'aria-label="上传照片">＋</button>' not in response
    assert 'aria-label="发送">↑</button>' not in response
    assert 'id="attachmentStrip"' in response
    assert 'class="interruption-choice" id="interruptionChoice" hidden' in response
    assert "function submitPrompt" in response
    assert "continueButton.onclick = () => submitPrompt();" in response
    assert 'throw new Error("会话未连接");' in response
    assert "function nativeErrorMessage(message)" in response
    assert 'return "会话不存在或已被清理";' in response
    assert "let providerCapabilities = {};" in response
    assert "async function loadProviderCapabilities()" in response
    assert "await api(`${API_BASE}/capabilities`)" in response
    assert "function canSteerActiveTurn()" in response
    assert "function canInterruptActiveTurn()" in response
    assert "await pollEvents();" in response
    assert "function primaryComposerAction" in response
    assert "function applyNativeTurnState" in response
    assert 'id="composerActivity"' in response
    assert "function setComposerActivity" in response
    assert "continueButton.innerHTML = mode === \"interrupt\" ? ICONS.stop : ICONS.send;" in response
    assert 'const requiresTurn = mode === "interrupt" || mode === "steer";' in response
    assert "requiresTurn && !activeTurnId" in response
    assert "steerButton.hidden = !canSteerActiveTurn();" in response
    assert "interruptButton.hidden = !canInterruptActiveTurn();" in response
    assert 'class="dock-actions" hidden' in response
    assert "function readImageAttachment" in response
    assert "function selectComposerPlugin(item)" in response
    assert "function pluginAutocompleteMatches(query)" in response
    assert "function promptHasPluginMention(value, mention)" in response
    assert "function updatePluginAutocomplete()" in response
    assert "row.onclick = () => selectComposerPlugin(item);" in response
    assert "replacePromptPluginQuery(item);" in response
    assert "renderSelectedPlugins();" in response
    assert "function readSelectedCollaborationMode()" in response
    assert "body.collaboration_mode = collaborationMode;" in response
    assert 'return {"mode": selectedCollaborationMode === "plan" ? "plan" : "default", "settings": {"model": settings.model}};' in response
    assert 'planModeCheck.innerHTML = enabled ? ICONS.check : "";' in response
    assert 'class="mode-chip plan-mode-chip" id="planModeChip" hidden' in response
    assert 'id="planModeChipCancel"' in response
    assert "function setSelectedCollaborationMode(mode)" in response
    assert "planModeChip.hidden = !enabled;" in response
    assert "planModeChipCancel.onclick = () => setSelectedCollaborationMode(\"default\");" in response
    assert "function renderAttachments" in response
    assert "function renderLocalUserEcho" in response
    assert 'if (action === "continue") body.force_new_turn = true;' in response
    assert (
        'if (action !== "steer") '
        'renderLocalUserEcho(prompt, attachmentsSnapshot, draftTurnId);'
    ) in response
    assert "function clearMatchingLocalUserEcho(event)" in response
    assert "function localUserEchoMatchesEvent(node, incomingText, incomingImages)" in response
    assert "if (!options.historical) clearMatchingLocalUserEcho(event);" in response
    assert 'node.row.classList.contains("local-pending")' in response
    assert "transcriptNodes.delete(key);" in response
    assert "renderTranscriptImages(node.body, payload.images || [])" in response
    assert "node.append(document.createTextNode(String(text)))" in response
    assert "openInterruptionChoice()" in response
    assert 'submitPrompt("steer")' in response
    assert 'submitPrompt("continue")' in response
    assert ".bubble" not in response


def test_claude_worker_live_page_plus_menu_exposes_plan_but_hides_plugins() -> None:
    response = _live_page(42, native_provider="claude")

    assert 'id="menuUploadPhoto"' in response
    assert "上传照片" in response
    assert 'id="menuPlanMode" type="button" role="menuitem" aria-pressed="false"' in response
    assert 'id="pluginMenuSection" hidden' in response
    assert 'id="pluginList" hidden' in response
    assert "const SUPPORTS_PLAN_MODE = true;" in response
    assert "const SUPPORTS_PLUGIN_MENU = false;" in response
    assert "const USES_CLAUDE_PLAN_PERMISSION_MODE = true;" in response
    assert "if (USES_CLAUDE_PLAN_PERMISSION_MODE) return null;" in response
    assert 'if (USES_CLAUDE_PLAN_PERMISSION_MODE && selectedCollaborationMode === "plan") {' in response
    assert 'permissionMode = "plan";' in response


def test_antigravity_worker_live_page_plus_menu_only_exposes_upload_photo() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert 'id="menuUploadPhoto"' in response
    assert "上传照片" in response
    assert 'id="menuPlanMode" type="button" role="menuitem" hidden aria-pressed="false"' in response
    assert 'id="pluginMenuSection" hidden' in response
    assert 'id="pluginList" hidden' in response
    assert "const SUPPORTS_PLAN_MODE = false;" in response
    assert "const SUPPORTS_PLUGIN_MENU = false;" in response
    assert "const USES_CLAUDE_PLAN_PERMISSION_MODE = false;" in response
    assert 'if (!SUPPORTS_PLAN_MODE) return "default";' in response


def test_live_page_gates_active_turn_controls_with_provider_capabilities() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert "let providerCapabilities = {};" in response
    assert "async function loadProviderCapabilities()" in response
    assert "await api(`${API_BASE}/capabilities`)" in response
    assert "function canSteerActiveTurn()" in response
    assert "function canInterruptActiveTurn()" in response
    assert "steerButton.hidden = !canSteerActiveTurn();" in response
    assert "interruptButton.hidden = !canInterruptActiveTurn();" in response
    assert "message assistant" not in response


def test_live_page_exposes_workflow_handoff_controls() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert 'id="handoffButton"' in response
    assert ">接棒执行</button>" in response
    assert 'id="handoffPanel"' in response
    assert 'data-provider="codex"' in response
    assert 'data-provider="claude"' in response
    assert 'data-provider="antigravity"' in response
    assert 'id="handoffIntent"' in response
    assert 'id="handoffNote"' in response
    assert "async function previewHandoff" in response
    assert "async function executeHandoff" in response
    assert '"/api/native/workflows/handoffs/preview"' in response
    assert '"/api/native/workflows/handoffs/execute"' in response
    assert "source_provider: PROVIDER" in response
    assert "source_thread_id: nativeThreadId" in response
    assert "target_provider: handoffTargetProvider" in response
    assert "location.href = result.target_url;" in response


def test_live_page_handoff_preview_allows_backend_cwd_resolution() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert 'setHandoffStatus("工作目录未知"' not in response
    assert "cwd: handoffPreviewPayload.cwd || currentWorkspaceCwd()" in response


def test_live_page_handoff_warning_newlines_are_js_escaped() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert '? "\\n" + preview.warnings.join("\\n")' in response
    assert '? "\n" + preview.warnings.join("\n")' not in response


def test_live_page_defines_handoff_preview_escape_helper() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert "function escapeHtml(value)" in response
    assert "escapeHtml(preview.prompt || \"\")" in response


def test_live_page_handoff_prompt_preview_matches_mobile_native_shape() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert 'id="handoffCopyButton"' in response
    assert 'class="handoff-preview-head"' in response
    assert 'class="handoff-preview-title">Plain text</span>' in response
    assert 'class="handoff-prompt-body" id="handoffPromptBody"' in response
    assert ".handoff-preview { max-height: min(48vh, 620px);" in response
    assert ".handoff-prompt-body { min-height: 0; overflow: auto;" in response
    assert "font: 18px/1.42" in response


def test_live_page_handoff_prompt_preview_supports_copy_action() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert "const handoffCopyButton = document.getElementById(\"handoffCopyButton\");" in response
    assert "handoffCopyButton.onclick = copyHandoffPrompt;" in response
    assert "async function copyHandoffPrompt()" in response
    assert "navigator.clipboard.writeText(text)" in response
    assert "handoffPromptBody.innerHTML = escapeHtml(preview.prompt || \"\");" in response


def test_live_page_renders_generated_prompt_messages_as_mobile_prompt_cards() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert ".prompt-card { position: relative; width: auto; height: min(48vh, 620px);" in response
    assert ".prompt-card.collapsed { height: 250px; min-height: 250px; cursor: pointer;" in response
    assert ".prompt-card.collapsed::after { content: \"\";" in response
    assert ".transcript-item.prompt-message { justify-self: stretch;" in response
    assert "margin-left: 6px; margin-right: 6px;" in response
    assert ".transcript-item.prompt-message .transcript-meta { display: none;" in response
    assert "background: #303030 !important; background-color: #303030 !important;" in response
    assert ".prompt-card-head { display: flex;" in response
    assert "min-height: 0; padding: 20px 26px 0;" in response
    assert "background: #303030;" in response
    assert ".prompt-card-title { color: #f8fafc; font-size: 25px; font-weight: 400;" in response
    assert ".transcript-body .prompt-card-body { min-height: 0; overflow: auto;" in response
    assert "padding: 16px 26px 28px;" in response
    assert "border-radius: 0; background: #303030;" in response
    assert 'font: 22px/1.34 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;' in response
    assert "-webkit-text-size-adjust: 100%;" in response
    assert "border: 0;" in response
    assert "white-space: pre-wrap;" in response
    assert "function stripGeneratedPromptFence(text)" in response
    assert "function normalizeGeneratedPromptText(text)" in response
    assert "collapseGeneratedPromptHardWraps(source)" in response
    assert "preface: stripGeneratedPromptFence(source.slice(0, promptStart)).trim()" in response
    assert "const prompt = normalizeGeneratedPromptText(rawPrompt);" in response
    assert "function collapseGeneratedPromptHardWraps(text)" in response
    assert "function isGeneratedPromptSectionHeading(line)" in response
    assert "function isGeneratedPromptListItem(line)" in response
    assert "function normalizeGeneratedPromptListLine(line)" in response
    assert "function isGeneratedPromptSentenceBoundary(line)" in response
    assert "output.join(\"\\n\").replace(/\\n{3,}/g, \"\\n\\n\").trim()" in response
    assert "paragraph.push(trimmed);" in response
    assert "if (isGeneratedPromptSentenceBoundary(trimmed)) flushParagraph();" in response
    assert "function splitGeneratedPromptText(text)" in response
    assert "function generatedPromptStartMatch(source)" in response
    assert "PLEASE IMPLEMENT THIS PLAN:" in response
    assert "function renderGeneratedPromptTranscript(target, text, event)" in response
    assert "const renderedPrompt = renderGeneratedPromptTranscript(node.body, node.text, event);" in response
    assert "function hasNativePlanEventForTurn(event)" in response
    assert "function planTextFromExecutionPrompt(text)" in response
    assert "plan.className = \"plan-item prompt-plan-fallback\";" in response
    assert (
        "plan.append(createPlanCardElement(planText, \"\", {executable: false}));"
        in response
    )
    assert "仅文本计划，不能一键执行" in response
    assert "node.row.classList.toggle(\"prompt-message\", renderedPrompt);" in response
    assert "const generatedPrompt = groupHasGeneratedPrompt(group);" in response
    assert "!generatedPrompt &&" in response
    assert "function groupHasGeneratedPrompt(group)" in response
    assert "Boolean(splitGeneratedPromptText(visibleTranscriptText(event)))" in response
    assert "你在[\\s\\S]+?工作。" in response
    assert "背景：" in response
    assert "必须阅读" in response
    assert "重点就一句" in response


def test_live_page_renders_native_plan_updates_as_plan_cards() -> None:
    response = _live_page(42, native_provider="codex")

    assert ".plan-card { position: relative; display: grid;" in response
    assert ".plan-card-title { margin: 0; color: #ffffff; font-size: 33px;" in response
    assert ".plan-card:not(.expanded)::after" in response
    assert ".plan-page-backdrop { position: fixed; inset: 0; z-index: 12; overflow-y: auto; overflow-x: hidden;" in response
    assert ".plan-page-shell { box-sizing: border-box; width: 100%; max-width: 100vw; min-height: 100vh;" in response
    assert ".plan-page-top { position: sticky; top: 0; z-index: 1; box-sizing: border-box;" in response
    assert ".plan-page-content { box-sizing: border-box; width: 100%; max-width: 100vw; min-width: 0;" in response
    assert ".plan-page-content > * { min-width: 0; max-width: 100%; }" in response
    assert ".plan-page-title, .plan-page-summary, .plan-page-body { overflow-wrap: anywhere; word-break: break-word; }" in response
    assert ".plan-page-body code, .plan-page-summary code { white-space: normal; overflow-wrap: anywhere; word-break: break-word;" in response
    assert 'id="planPage"' in response
    assert 'class="plan-page-backdrop" id="planPage"' in response
    assert 'id="planPageClose"' in response
    assert 'id="planPageExecute"' in response
    assert "let activePlan = null;" in response
    assert "function isNativePlanEvent(event)" in response
    assert 'payload.action === "plan_updated"' in response
    assert "else if (isNativePlanEvent(event)) renderPlanEvent(event);" in response
    assert "function renderPlanEvent(event)" in response
    assert "setActivePlanFromEvent(event, planText, titleText, summaryText);" in response
    assert "label.innerHTML = `${ICONS.plan}<span>计划</span>`;" in response
    assert "download.innerHTML = ICONS.download;" in response
    assert 'copy.setAttribute("aria-label", "复制计划");' in response
    assert 'execute.textContent = "执行计划";' in response
    assert "execute.onclick = click => {" in response
    assert "executeActivePlan();" in response
    assert "openPlanPage(plan);" in response
    assert "function openPlanPage(plan = activePlan)" in response
    assert "function renderPlanPage(plan)" in response
    assert "function closePlanPage()" in response
    assert (
        "function createPlanCardElement(planText, titleFallback, options = {})"
        in response
    )
    assert 'executable: true' in response
    assert "const executable = options.executable === true;" in response
    assert "if (plan.executable) {" in response
    assert 'readonly.textContent = "仅文本计划，不能一键执行";' in response
    assert "function planTextFromPayload(payload)" in response
    assert "function planTitleFromText(text, fallback)" in response
    assert "function planSummaryFromText(text)" in response
    assert "function downloadPlanText(title, text)" in response
    assert "function executeActivePlan()" in response
    assert "planExecutionPrompt(activePlan.body)" in response
    assert "function clearSelectedPlanModeForExecution()" in response
    assert response.index("clearSelectedPlanModeForExecution();") < response.index(
        "const body = buildNativePromptBody(prompt, {collaborationMode: explicitDefaultCollaborationMode()});"
    )
    assert (
        "const body = buildNativePromptBody(prompt, {collaborationMode: explicitDefaultCollaborationMode()});\n"
        "      body.force_new_turn = true;"
    ) in response
    assert 'if (selectedCollaborationMode !== "plan") return;' in response
    assert 'setSelectedCollaborationMode("default");' in response
    assert (
        'if (isNativeExecutionDetail(event)) clearSelectedPlanModeForExecution();'
        not in response
    )
    assert (
        "function handleHiddenNativeFeedback(event)" in response
    )
    assert response.index("function handleHiddenNativeFeedback(event)") > response.index(
        "clearSelectedPlanModeForExecution();"
    )
    assert "planDetailTextFromText(plan.body, plan.summary)" in response
    assert "hideHandoffForPlan" not in response
    assert "handoffButton.hidden = false;" in response
    assert "handoffButton.disabled = sendingPrompt || !nativeThreadId;" in response
    assert "function explicitDefaultCollaborationMode()" in response
    assert (
        "buildNativePromptBody(prompt, {collaborationMode: explicitDefaultCollaborationMode()})"
        in response
    )
    assert "buildNativePromptBody(prompt, {includeCollaborationMode: true})" in response
    assert 'body: JSON.stringify(body)' in response
    assert "if (isNativePlanEvent(event)) return 35;" in response


def test_live_page_assistant_transcript_uses_clean_spacing_without_labels_or_left_rail() -> None:
    response = _live_page(42, native_provider="codex")

    assert ".transcript-item.assistant { justify-self: start; max-width: 100%; margin: 4px 0 12px; }" in response
    assert ".transcript-item.assistant .transcript-meta" not in response
    assert "if (!assistantRole) row.append(meta);" in response
    assert "row.append(body);" in response
    assert ".turn-fold-preview-assistant { justify-self: start; max-width: 100%; color: var(--text-secondary); }" in response
    assert ".transcript-item.assistant { justify-self: start; max-width: 100%; padding-left: 22px; border-left: 2px solid var(--border-default); }" not in response
    assert ".turn-fold-preview-assistant { justify-self: start; padding-left: 18px; border-left: 2px solid var(--border-default);" not in response


def test_live_page_generated_prompt_cards_support_copy_per_message() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert "function createPlainPromptCard(text)" in response
    assert "function isPlanExecutionPrompt(text)" in response
    assert "card.classList.add(\"collapsed\")" in response
    assert "card.classList.toggle(\"collapsed\")" in response
    assert "const renderedPrompt = renderGeneratedPromptTranscript(node.body, node.text, event);" in response
    assert "const renderedPrompt = renderGeneratedPromptTranscript(node.body, incomingText, event);" in response
    assert "title.textContent = promptCardTitle(text);" in response
    assert "body.textContent = promptCardBodyText(text);" in response
    assert "button.className = \"handoff-copy prompt-card-copy\";" in response
    assert "event.stopPropagation();" in response
    assert "copyPromptCardText(button, text);" in response
    assert "async function copyPromptCardText(button, text)" in response
    assert "setPromptCardCopyState(button, \"copied\")" in response


def test_live_page_waits_during_active_turn_when_provider_cannot_steer() -> None:
    response = _live_page(42, native_provider="antigravity")

    assert 'if (!nativeTurnRunning) return "continue";' in response
    assert 'if (canSteerActiveTurn() && composerHasDraft()) return "choose";' in response
    assert (
        'if (canInterruptActiveTurn() && !composerHasDraft()) return "interrupt";'
        in response
    )
    assert 'mode === "wait" ? "等待当前轮"' in response


def test_native_live_page_hides_provider_display_completed_projection() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isProviderDisplayCompletedEvent(event)" in response
    assert 'event.type === "provider.display.completed"' in response
    assert "isProviderDisplayCompletedEvent(event)" in response
    assert 'event.kind === "message_completed"' in response


def test_native_live_page_hides_compatibility_projection_mirrors() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isCompatibilityMirrorEvent(event)" in response
    assert 'event.kind === "compatibility_event"' in response
    assert 'payload.compatibility_projection === "model.text.delta"' in response
    assert "isCompatibilityMirrorEvent(event)" in response
    assert 'renderStatusEvent(event, statusText(event, payload), statusTone(event))' in response


def test_native_live_page_hides_provider_raw_frame_events() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isProviderRawFrameEvent(event)" in response
    assert 'event.type === "provider.raw.frame"' in response
    assert 'event.kind === "provider_raw_frame"' in response
    assert "isProviderRawFrameEvent(event)" in response


def test_native_live_page_omits_workspace_selector_from_composer() -> None:
    response = _live_page(42, native_provider="codex")

    assert "live-workspace-bar" not in response
    assert "liveWorkspaceChip" not in response
    assert "openWorkspaceSwitcher" not in response


@pytest.mark.asyncio
async def test_worker_live_page_hides_success_lifecycle_events_from_transcript(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function shouldRenderStatusEvent(event)" in response
    assert 'if (event.kind === "completed") return false;' in response
    assert (
        'if (event.kind === "lifecycle" && !isFailedStatus(payload.status)) '
        "return false;"
    ) in response
    assert (
        'if (event.kind === "lifecycle" && status === "running") '
        'return "正在回复";'
    ) in response


@pytest.mark.asyncio
async def test_worker_live_page_uses_official_model_catalog_settings(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'id="reasoningSelector"' in response
    assert 'id="serviceTierSelector"' in response
    assert "模型" in response
    assert "速度" in response
    assert "推理" in response
    assert "async function loadModelCatalog" in response
    assert 'const API_BASE = "/api/native/codex";' in response
    assert "api(`${API_BASE}/models`)" in response
    assert "function updateSettingSummary" in response
    assert 'id="serviceTierOptions"' in response
    assert 'id="reasoningOptions"' in response
    assert "pointer-events: none" in response
    assert "function renderSettingOptions" in response
    assert "function fillServiceTierSelector" in response
    assert "function highestReasoningEffort" in response
    assert "function preferredReasoningEffortDefault" in response
    assert "preferredReasoningEffortDefault(model, efforts)" in response
    assert "function serviceTierLabel" in response
    assert "function reasoningEffortLabel" in response
    assert 'if (key === "high") return "高";' in response
    assert 'if (["xhigh", "extra_high"].includes(key)) return "极高";' in response
    assert 'if (["max", "maximum"].includes(key)) return "最大";' in response
    assert "function preferredServiceTierDefault" in response
    assert "function updateSettingVisibility" in response
    assert "reasoningSettingRow.hidden = reasoningSelector.options.length <= 1;" in response
    assert "serviceTierSettingRow.hidden = serviceTierSelector.options.length <= 1;" in response
    assert 'normalOption.value = "";' in response
    assert 'renderSettingOptions(serviceTierOptions, serviceTierSelector, updateSettingSummary, {includeEmpty: true});' in response
    assert "const MODEL_SETTINGS_STORAGE_KEY" in response
    assert "function saveModelSettingsIfChanged" in response
    assert "if (willClose) saveModelSettingsIfChanged();" in response
    assert "function markModelSettingsDirty" in response
    assert "localStorage.setItem(MODEL_SETTINGS_STORAGE_KEY" in response
    assert "button.dataset.value = option.value;" in response
    assert "function syncSettingOptionsSelection" in response
    assert "syncSettingOptionsSelection(container, select);" in response
    assert "syncSettingOptionsSelection(reasoningOptions, reasoningSelector);" in response
    assert "syncSettingOptionsSelection(serviceTierOptions, serviceTierSelector);" in response
    assert "body.model = savedModelSettings.model;" in response
    assert "body.effort = savedModelSettings.effort;" in response
    assert "body.service_tier = savedModelSettings.service_tier;" in response
    assert "modelSettingsButton.disabled = false;" in response
    assert "function syncSettingOptionsDisabled" in response
    assert "reasoningSelector.disabled = sendingPrompt || nativeTurnRunning" in response
    assert "serviceTierSelector.disabled = sendingPrompt || nativeTurnRunning" in response
    assert 'service_tier: serviceTierSettingRow.hidden ? "" : serviceTierSelector.value,' in response
    assert 'modelSettingsButton.textContent = [modelText, effortText].filter(Boolean).join(" ");' in response
    assert 'if (!serviceTierSettingRow.hidden) summaryParts.push(tierText);' not in response
    assert 'modelSettingsButton.textContent = summaryParts.join(" ");' not in response


@pytest.mark.asyncio
async def test_claude_model_catalog_returns_deepseek_reasoning_without_speed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([FakeClaudeProvider()]),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /api/native/claude/models HTTP/1.1\r\n"
            "Host: test\r\nAuthorization: Bearer secret\r\n"
            "Connection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    body = response.split("\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert payload["models"][0]["model"] == "deepseek-v4-pro"
    assert payload["models"][0]["defaultReasoningEffort"] == "max"
    assert [
        item["reasoningEffort"]
        for item in payload["models"][0]["supportedReasoningEfforts"]
    ] == ["low", "medium", "high", "xhigh", "max"]
    assert payload["models"][0]["serviceTiers"] == []


@pytest.mark.asyncio
async def test_worker_live_page_uses_provider_scoped_model_catalog_for_antigravity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_registry=NativeAgentRegistry([FakeAntigravityProvider()]),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_provider=antigravity&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'const PROVIDER = "antigravity";' in response
    assert 'const API_BASE = "/api/native/antigravity";' in response
    assert "api(`${API_BASE}/models`)" in response
    assert "body.model = savedModelSettings.model;" in response


@pytest.mark.asyncio
async def test_worker_live_page_shows_approval_resolution_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert 'className = "approval-state"' in response
    assert ".approval-action.approve" in response
    assert ".approval-action.danger" in response
    assert ".approval-action.selected" in response
    assert ".approval-action.muted" in response
    assert "button.dataset.action = action;" in response
    assert "button.className = `approval-action ${tone}`;" in response
    assert "card.dataset.selectedAction = action;" in response
    assert "setApprovalButtons(card, action, state);" in response
    assert "function setApprovalButtons" in response
    assert "button.classList.toggle(\"selected\", selected);" in response
    assert "button.classList.toggle(\"muted\", state !== \"idle\" && !selected);" in response
    assert "function approvalResolvedAction" in response
    assert "function resolvedApprovalEventFor(requestId)" in response
    assert "function hasUnresolvedApprovalRequests(sourceEvents)" in response
    assert "hasUnresolvedApprovalRequests(loadedEvents)" in response
    assert "const alreadyResolved = resolvedApprovalEventFor(requestId);" in response
    assert 'setApprovalState(card, approvalResolvedAction(alreadyResolved), "resolved");' in response
    assert "const resolved = resolvedApprovalEventFor(requestId);" in response
    assert 'setApprovalState(card, approvalResolvedAction(resolved), "resolved");' in response
    assert "const action = approvalResolvedAction(event, card);" in response
    assert 'setApprovalState(card, action, "resolved");' in response
    assert 'setApprovalState(card, "approve_once", "resolved");' not in response
    assert "function approvalStateText" in response
    assert "if (action === \"approve_once\") return state === \"pending\" ? \"批准一次处理中\" : \"已批准一次\";" in response
    assert "if (action === \"approve_session\") return state === \"pending\" ? \"本会话批准处理中\" : \"本会话已批准\";" in response
    assert "setApprovalState(card, action, \"pending\")" in response
    assert "setApprovalState(card, action, \"resolved\")" in response
    assert "setApprovalState(card, action, \"failed\"" in response
    assert "button.onclick = () => resolveApproval(payload.codexRequestId, action, card)" in response


@pytest.mark.asyncio
async def test_worker_live_page_loads_native_timeline_and_folds_history(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "NATIVE_TIMELINE_RECENT_LIMIT" in response
    assert "OLDER_VISIBLE_PAGE_ATTEMPTS" in response
    assert 'nativeTimelinePath("limit=" + NATIVE_TIMELINE_RECENT_LIMIT)' in response
    assert 'eventsPath("tail=" + CURRENT_TURN_EVENT_LIMIT, {currentTurn: true})' not in response
    assert "function syncNativeTranscript" in response
    assert '`${API_BASE}/sessions/${encodeURIComponent(nativeThreadId)}/sync`' in response
    assert "syncNativeTranscript().then(pollEvents);" in response
    assert "let nativeSyncInFlight = false;" not in response
    assert "function startNativeTranscriptSyncLoop()" in response
    assert "setInterval(pollEvents, 1000)" in response
    assert "setInterval(syncNativeTranscriptAndPoll, 2500)" not in response
    assert "async function syncNativeTranscriptAndPoll()" not in response
    assert 'document.addEventListener("visibilitychange", () => {' in response
    assert "if (!document.hidden) pollEvents();" in response
    assert "function refreshNativeControlInBackground()" in response
    assert "refreshNativeControlInBackground();" in response
    assert "loadNativeSessionInfo().catch(() => {});" in response
    assert "loadRecentEvents().catch(error => {" in response
    assert "loadNativeSessionInfo().catch(() => {}).then(loadRecentEvents)" not in response
    assert "attachNative().then(syncNativeTranscript).then(loadNativeSessionInfo).then(() => {" not in response
    assert "attachNative().then(syncNativeTranscript).then(loadNativeSessionInfo).catch" not in response
    assert "attachNative().then(loadNativeSessionInfo).catch" in response
    assert "timeoutMs: 2500" in response
    assert "hasLiveDisplayEvents" in response
    assert "model.usage.updated" in response
    assert "function normalizeEventList(sourceEvents)" in response
    assert "function loadRecentEvents" in response
    assert "if (nativeThreadId) {" in response
    assert "loadNativeTimelineEvents" in response
    assert "loadedEvents = normalizeEventList(snapshot.events);" in response
    assert "function hasNativePlanEvents(sourceEvents)" in response
    assert "hasUnresolvedApprovalRequests(loadedEvents)\n        ) && nativeTurnId" not in response
    assert "function loadOlderEvents" in response
    assert "function scheduleOlderTranscriptSync()" in response
    assert "syncNativeTranscript().then(pollEvents);" in response
    assert "const visibleBeforeLoad = displayEventCount(loadedEvents);" in response
    assert "for (let attempt = 0; attempt < OLDER_VISIBLE_PAGE_ATTEMPTS; attempt++)" in response
    assert "if (displayEventCount(loadedEvents) > visibleBeforeLoad) break;" in response
    assert "function displayEventCount(sourceEvents)" in response
    assert "function pollEvents" in response
    assert "startNativeTranscriptSyncLoop();" in response
    assert "const nextEvents = normalizeEventList(snapshot.events);" in response
    assert "function nativeTimelinePath(params)" in response
    assert "eventsPath(`after=${latestEventId}&limit=100`)" not in response
    assert "nativeTimelinePath(`after=${latestEventId}&limit=100`)" in response
    assert "function eventsPath(params, options = {})" in response
    assert 'if (nativeThreadId) search.set("native_thread_id", nativeThreadId);' in response
    assert "function streamPathWithCursor(afterId)" in response
    assert 'return nativeTimelineStreamPath(afterId);' in response
    assert 'if (PROVIDER) params.set("native_provider", PROVIDER);' in response
    assert "let streamReconnectTimer = null;" in response
    assert "function closeLiveEventSource()" in response
    assert "function scheduleStreamReconnect()" in response
    assert "scheduleStreamReconnect();" in response
    assert 'window.addEventListener("pagehide", closeLiveEventSource);' in response
    assert 'window.addEventListener("pageshow", () => {' in response
    assert "function isInternalEvent(event)" in response
    assert "if (isInternalEvent(event)) return;" in response
    assert 'historyFold.textContent = previousEventCount > 0 ? "加载更早的消息" : "更早的消息";' in response
    assert "`${previousEventCount} 条以前的消息`" not in response
    assert "以前的消息" in response
    assert "previous_event_count" in response
    assert "new EventSource(streamPath)" not in response
    assert "new EventSource(streamPathWithCursor" in response


def test_worker_live_page_history_fold_disabled_state_stays_dark() -> None:
    response = _live_page(42, native_provider="codex")

    assert "appearance: none;" in response
    assert "-webkit-appearance: none;" in response
    assert ".history-fold:disabled { background: transparent; color: var(--text-dim); opacity: 1;" in response
    assert "-webkit-text-fill-color: var(--text-dim);" in response
    assert "historyFold.setAttribute(\"aria-busy\", \"true\");" in response
    assert "historyFold.removeAttribute(\"aria-busy\");" in response


def test_worker_live_page_filters_invalid_events_before_rendering() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isValidEventObject(event)" in response
    assert "function normalizeEventList(sourceEvents)" in response
    assert "if (!isValidEventObject(event)) return;" in response
    assert "for (const event of normalizeEventList(sourceEvents))" in response
    assert "return Boolean(event && (event.kind === \"text_delta\"" in response
    assert "if (!event) return 60;" in response
    assert "const payload = (event && event.payload) || {};" in response


@pytest.mark.asyncio
async def test_worker_live_page_renders_assistant_markdown_blocks(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function renderMarkdownLite(target, text)" in response
    assert "function appendInlineMarkdown(target, text)" in response
    assert "function appendMarkdownLink(target, label, href)" in response
    assert 'replace(/\\r\\n/g, "\\n")' in response
    assert 'paragraph.join("\\n").trim()' in response
    assert 'codeLines.join("\\n")' in response
    assert "\\[([^\\]]+)\\]\\(([^)]+)\\)" in response
    assert "renderMarkdownLite(node.body, visibleText);" in response
    assert "node.text += visibleText;" in response
    assert "node.text = visibleText;" in response
    assert ".transcript-body p" in response
    assert ".transcript-body ul" in response
    assert ".transcript-body pre" in response


def test_live_page_uses_native_codex_font_scale_for_all_native_providers() -> None:
    for provider in ("codex", "claude", "antigravity"):
        response = _live_page(42, native_provider=provider)

        assert "--native-ui-font-size: 15px;" in response
        assert "--native-code-font-size: 12px;" in response
        assert ".transcript-body { min-width: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; color: var(--btn-primary-bg); font-size: var(--native-ui-font-size); line-height: 1.55;" in response
        assert ".transcript-item.assistant .transcript-body { color: #b8bcc7; }" in response
        assert ".transcript-body pre code { white-space: pre; overflow-wrap: normal; word-break: normal; padding: 0; border-radius: 0; background: transparent; font-size: var(--native-code-font-size); line-height: 1.5; }" in response
        assert "#prompt { flex: 1; min-width: 0; min-height: 44px; max-height: 132px; border-radius: 22px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--btn-primary-bg); padding: 9px 48px 9px 18px; font-size: 18px; line-height: 24px; resize: none; overflow-y: auto; }" in response


def test_native_live_page_exposes_transcript_font_size_controls() -> None:
    response = _live_page(42, native_provider="codex")

    assert 'id="uiFontSizeInput"' in response
    assert 'id="codeFontSizeInput"' in response
    assert 'id="uiFontSizeInput" type="number" min="12" max="20" step="1"' in response
    assert 'id="codeFontSizeInput" type="number" min="12" max="20" step="1"' in response
    assert "UI字号" in response
    assert "代码字号" in response
    assert 'const DISPLAY_SETTINGS_STORAGE_KEY = "wlcodexNativeDisplaySettings";' in response
    assert 'document.documentElement.style.setProperty("--native-ui-font-size"' in response
    assert 'document.documentElement.style.setProperty("--native-code-font-size"' in response
    assert "localStorage.setItem(DISPLAY_SETTINGS_STORAGE_KEY" in response


def test_native_live_page_font_size_inputs_allow_draft_editing_before_commit() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function updateDisplayFontSizeDraft(input, key)" in response
    assert "function commitDisplayFontSizeInput(input, key)" in response
    assert "function setDisplayFontInputValue(input, value)" in response
    assert 'if (raw === "") return;' in response
    assert 'uiFontSizeInput.oninput = () => updateDisplayFontSizeDraft(uiFontSizeInput, "uiFontSize");' in response
    assert 'codeFontSizeInput.oninput = () => updateDisplayFontSizeDraft(codeFontSizeInput, "codeFontSize");' in response
    assert 'uiFontSizeInput.onblur = () => commitDisplayFontSizeInput(uiFontSizeInput, "uiFontSize");' in response
    assert 'codeFontSizeInput.onblur = () => commitDisplayFontSizeInput(codeFontSizeInput, "codeFontSize");' in response
    assert 'uiFontSizeInput.oninput = () => updateDisplayFontSize("uiFontSize", uiFontSizeInput.value);' not in response
    assert 'codeFontSizeInput.oninput = () => updateDisplayFontSize("codeFontSize", codeFontSizeInput.value);' not in response


def test_live_page_protects_mobile_transcript_layout_from_overlay_and_long_inline_code() -> None:
    response = _live_page(42, native_provider="codex")

    assert "main { padding: 12px 20px calc(var(--codex-dock-height, 150px) + 32px + env(safe-area-inset-bottom)); }" in response
    assert ".transcript-body { min-width: 0; max-width: 100%; white-space: normal;" in response
    assert ".transcript-body p { margin: 0 0 13px; overflow-wrap: anywhere; word-break: break-word; }" in response
    assert ".transcript-body code { white-space: normal; overflow-wrap: anywhere; word-break: break-word;" in response
    assert ".transcript-body pre code { white-space: pre; overflow-wrap: normal; word-break: normal;" in response
    assert ".transcript-item.user .transcript-body { white-space: pre-wrap;" in response
    assert "function syncDockHeight()" in response
    assert 'document.documentElement.style.setProperty("--codex-dock-height", `${Math.ceil(rect.height)}px`);' in response
    assert "new ResizeObserver(syncDockHeight)" in response


@pytest.mark.asyncio
async def test_worker_live_page_replaces_delta_with_completed_assistant_message(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert '"message_completed"' in response
    assert 'event.kind === "message_completed"' in response
    assert "hasCompletedAssistantMessageForTurn" in response
    assert "return Boolean(event && (event.kind === \"text_delta\" || event.kind === \"message_completed\"));" in response
    assert "node.text = String(incomingText);" in response
    assert "node.row.dataset.completed = \"true\";" in response
    assert "assistantMessageKey(event)" in response
    assert "completedAssistantMessageKey(event)" in response
    assert 'if (event.kind === "message_completed") return completedAssistantMessageKey(event);' in response
    assert 'return `${itemId}:${event.id || transcriptTextFingerprint(event)}`;' in response
    assert "foldTranscriptPreviewText(group, \"message_completed\")" in response


@pytest.mark.asyncio
async def test_worker_live_page_orders_mirrored_transcript_items_before_completed_reply(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function orderTranscriptGroupEvents(group)" in response
    assert "function displayEventOrder(event)" in response
    assert "function transcriptItemOrder(event)" in response
    assert "foldGroups(dedupeDisplayEvents(loadedEvents)).map(orderTranscriptGroupEvents)" in response
    assert "hasCompletedAssistantMessageForTurn(event)" in response
    assert "rebuildStream();" in response


@pytest.mark.asyncio
async def test_worker_live_page_hides_native_execution_details_from_user_feedback(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function isNativeExecutionDetail(event)" in response
    assert "function isNativeActivityDetail(event)" in response
    assert "function handleHiddenNativeFeedback(event)" in response
    assert "return Boolean(nativeThreadId || payload.native_thread_id);" in response
    assert "if (isNativeExecutionDetail(event)) {" in response
    assert "isNativeActivityDetail(event)" in response
    assert "handleHiddenNativeFeedback(event);" in response
    assert "else if (isNativeExecutionDetail(event)) renderToolCall(event);" not in response
    assert (
        'else if (event.kind === "command_started" || event.kind === "command_output" '
        '|| event.kind === "command_completed" || event.kind === "command_failed") '
        "renderToolCall(event);"
        not in response
    )


def test_worker_live_page_summarizes_native_diff_events_without_raw_patch() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function renderFileChangeSummary(event)" in response
    assert "function summarizeDiffPayload(payload)" in response
    assert "已更改" in response
    assert 'state.files.join("\\n")' in response
    assert 'String(text || "").split("\\n")' in response
    assert "payload.patch || payload.diff || payload.delta" not in response


@pytest.mark.asyncio
async def test_worker_live_page_does_not_bind_historical_turns_as_current_control(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "render(event, {scroll: false, historical: true});" in response
    assert "applyNativeTurnState(event, options);" in response
    assert "function applyNativeTurnState(event, options = {})" in response
    assert "let activeTurnId = \"\";" in response
    assert (
        "if (!mirroredTranscript && payload.native_turn_id) nativeTurnId = payload.native_turn_id;"
        in response
    )
    assert "const mirroredTranscript = isMirroredTranscriptEvent(event);" in response
    assert "if (options.historical) return;" in response
    assert "} else if (mirroredTranscript) {\n        return;\n      } else if (" in response
    assert "if (options.historical || mirroredTranscript) return;" not in response
    assert "body.expected_turn_id = activeTurnId;" in response
    assert "activeTurnId = result.active_turn_id || \"\";" in response


@pytest.mark.asyncio
async def test_worker_live_page_clears_running_composer_state_on_terminal_turn_events(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function isTerminalTurnEvent(event)" in response
    assert "isTerminalTurnEvent(event)" in response
    assert 'if (event.kind === "message_completed" && payload.native_turn_id) return true;' in response
    assert '["completed", "done", "succeeded", "success"]' in response
    assert '["failed", "error", "cancelled", "canceled", "interrupted", "aborted"]' in response
    assert "nativeTurnRunning = false;" in response
    assert 'continueButton.innerHTML = mode === "interrupt" ? ICONS.stop : ICONS.send;' in response
    assert "(!nativeTurnRunning && !composerHasDraft())" in response
    assert 'activeTurnId = result.turn_running ? (result.active_turn_id || result.turn_id || activeTurnId || "") : "";' in response
    assert "const terminalTranscriptSyncTurns = new Set();" in response
    assert "function scheduleTerminalTranscriptSync(event)" in response
    assert "function shouldSyncNativeTranscriptAfterTerminalEvent(event)" in response
    assert "scheduleTerminalTranscriptSync(event);" in response
    assert "await syncNativeTranscript();" in response
    assert "await pollEvents();" in response
    assert 'if (event.kind === "message_completed") return false;' in response


def test_worker_live_page_recovers_after_post_fetch_drop_without_resubmitting() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isFetchNetworkError(error)" in response
    assert "function nativeTurnAdvancedSince(snapshot)" in response
    assert "async function recoverNativeControlAfterFetchFailure(error, snapshot)" in response
    assert "await delay(700);" in response
    assert "await syncNativeTranscript();" in response
    assert "if (await recoverNativeControlAfterFetchFailure(error, controlSnapshot))" in response
    assert "clearComposerDraft();" in response
    assert "_retried" not in response


@pytest.mark.asyncio
async def test_worker_live_page_keeps_latest_turn_open_and_collapses_prior_turns(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "options.latest)" not in response
    assert "const latestTurnId = latestFoldGroupTurnId(groups);" in response
    assert "renderFoldGroup(group, {latestTurnId});" in response
    assert "function latestFoldGroupTurnId(groups)" in response
    assert "turnFoldTitle(group)" in response
    assert "function foldMessageCount(group)" in response
    assert "if (isInternalEvent(event)) continue;" in response
    assert 'if (event.kind === "lifecycle" || event.kind === "completed") return "";' in response
    assert "function dedupeDisplayEvents(sourceEvents)" in response
    assert "const seenUserMessages = new Map();" in response
    assert "const seenUserMessageText = new Map();" in response
    assert "function isSyntheticUserMessageEvent(event)" in response
    assert "function isTurnlessSyntheticUserMessageEvent(event)" in response
    assert "function userMessageTextFingerprint(event)" in response
    assert "function shouldDedupeUserBySyntheticText(event, previous)" in response
    assert "function userMessageDedupePriority(event)" in response
    assert "const groups = foldGroups(dedupeDisplayEvents(loadedEvents)).map(orderTranscriptGroupEvents);" in response
    assert "title.textContent = turnFoldTitle(group);" in response
    assert "nativeTurnId !== latestTurnId" in response
    assert "completed || nativeTurnId !== latestTurnId" not in response
    assert "group.length > 1" not in response


def test_worker_live_page_dedupes_assistant_mirror_text_by_turn() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function completedAssistantTextByTurn(sourceEvents)" in response
    assert "function assistantDisplayTextFingerprint(event)" in response
    assert "function shouldDropAssistantMirrorEvent(event, completedAssistantTexts)" in response
    assert 'const globalKey = "__global__";' in response
    assert "byTurn.get(globalKey).add(fingerprint);" in response
    assert 'completedAssistantTexts.get("__global__")?.has(fingerprint)' in response
    assert "function completedAssistantDedupePriority(event)" in response
    assert "function assistantVisibleDedupePriority(event)" in response
    assert "const seenAssistantVisible = new Map();" in response
    assert "const previousAssistantIndex = assistantFingerprint" in response
    assert "if (assistantFingerprint) seenAssistantVisible.set(assistantFingerprint, result.length);" in response
    assert "const seenAssistantCompleted = new Map();" in response
    assert "const completedFingerprint = completedAssistantTextFingerprint(event);" in response
    assert "if (completedFingerprint) seenAssistantCompleted.set(completedFingerprint, result.length);" in response
    assert 'if (!turnId) return `global:${fingerprint}`;' in response
    assert 'return `turn:${turnId}:${fingerprint}`;' in response
    assert "const completedAssistantTexts = completedAssistantTextByTurn(sourceEvents);" in response
    assert "if (shouldDropAssistantMirrorEvent(event, completedAssistantTexts)) continue;" in response
    assert "normalizeTranscriptText(visibleTranscriptText(event))" in response


def test_worker_live_page_strips_app_directives_from_visible_transcript() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function visibleTranscriptText(event)" in response
    assert "function stripCodexAppDirectives(text)" in response
    assert "function hasVisibleTranscriptText(event)" in response
    assert "if (isAssistantMessageEvent(event) && !hasVisibleTranscriptText(event)) continue;" in response
    assert "renderMarkdownLite(node.body, visibleText);" in response
    assert "node.text = visibleText;" in response
    assert "::git-stage" not in strip_codex_directive_test_surface(response)


def strip_codex_directive_test_surface(response: str) -> str:
    return response.split("function stripCodexAppDirectives(text)", 1)[0]


def test_worker_live_page_status_event_avoids_title_detail_duplication() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function statusDisplay(event, fallback)" in response
    assert "const display = statusDisplay(event, fallback);" in response
    assert "node.title.textContent = display.title;" in response
    assert "node.detail.hidden = !display.detail;" in response
    assert "if (detail === title) detail = \"\";" in response
    assert "appendText(node.detail, text);" not in response


def test_worker_live_page_suppresses_noisy_native_sync_not_found_status() -> None:
    response = _live_page(42, native_provider="codex")

    assert "function isNoisyNativeSyncError(message)" in response
    assert 'if (isNoisyNativeSyncError(text)) return;' in response
    assert 'clearStatusNode("native_sync_failed");' in response
    assert 'if (snapshot.native_sync_error) renderStatus("native_sync_failed", snapshot.native_sync_error);' in response
    assert 'else clearStatusNode("native_sync_failed");' in response


@pytest.mark.asyncio
async def test_worker_live_page_only_keeps_pending_approvals_expanded(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function hasPendingApproval(group)" in response
    assert "const pendingApproval = hasPendingApproval(group);" in response
    assert "!pendingApproval" in response
    assert "const hasApproval = group.some(event => event.kind === \"approval_requested\");" not in response


@pytest.mark.asyncio
async def test_worker_live_page_fold_keeps_native_transcript_previews(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "function renderFoldPreview(head, group)" in response
    assert "foldTranscriptPreviewText(group, \"user_message\")" in response
    assert "foldTranscriptPreviewText(group, \"text_delta\")" in response
    assert "foldTranscriptPreviewText(group, \"message_completed\")" in response
    assert "appendFoldPreviewLine(preview, \"user\", userText);" in response
    assert (
        'appendFoldPreviewLine(preview, "assistant", '
        "completedAssistantText || assistantText);"
    ) in response
    assert ".turn-fold-body-inner { min-height: 0; overflow: hidden;" in response
    assert 'inner.className = "turn-fold-body-inner";' in response
    assert "body.append(inner);" in response
    assert "renderTarget = inner;" in response
    assert "for (let index = group.length - 1; index >= 0; index--)" in response
    assert ".turn-fold:not(.collapsed) .turn-fold-preview { grid-template-rows: 0fr;" in response
    assert "/turn-summary" not in response


@pytest.mark.asyncio
async def test_worker_live_page_groups_turn_events_before_collapsing(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    server = WorkerLiveStreamServer(
        host="127.0.0.1",
        port=0,
        hub=WorkerLiveStreamHub(store),
        native_controller=FakeNativeController(),
        access_token="secret",
    )
    await server.start()
    try:
        response = await _read_response(
            server.host,
            server.port,
            "GET /workers/42/live?token=secret&native_thread_id=thread-1 HTTP/1.1\r\n"
            "Host: test\r\nConnection: close\r\n\r\n",
        )
    finally:
        await server.stop()

    assert "HTTP/1.1 200 OK" in response
    assert "const groupByKey = new Map();" in response
    assert "groupByKey.get(key).push(event);" in response
    assert "return Array.from(groupByKey.values()).sort" in response
