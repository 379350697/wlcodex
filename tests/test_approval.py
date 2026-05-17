"""Approval service tests."""

from pathlib import Path

import pytest

from wlcodex.approval import (
    ApprovalCallback,
    ApprovalService,
    decode_approval_callback,
    encode_approval_callback,
)
from wlcodex.codex_backend import FakeCodexBackend
from wlcodex.db import Ledger
from wlcodex.models import ApprovalKind, ApprovalStatus, TaskStatus


def test_encode_decode_roundtrip() -> None:
    encoded = encode_approval_callback(approval_id=42, action="approve_once")
    decoded = decode_approval_callback(encoded)
    assert decoded is not None
    assert decoded.approval_id == 42
    assert decoded.action == "approve_once"


def test_decode_rejects_malformed() -> None:
    assert decode_approval_callback("garbage") is None
    assert decode_approval_callback("approval:not_int:action") is None
    assert decode_approval_callback("") is None
    assert decode_approval_callback("foo:1:bar") is None


def test_all_actions_roundtrip() -> None:
    for action in ("approve_once", "approve_session", "deny", "cancel"):
        encoded = encode_approval_callback(1, action)
        decoded = decode_approval_callback(encoded)
        assert decoded is not None
        assert decoded.action == action


@pytest.mark.asyncio
async def test_approve_once_resolves_approval(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    svc = ApprovalService()

    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    approval = ledger.create_approval(
        task_id=task.id,
        codex_request_id="req-1",
        codex_item_id="item-1",
        codex_turn_id="turn-1",
        kind=ApprovalKind.COMMAND,
        summary="Run: ls",
        command_json='{"command":"ls"}',
    )

    cb = encode_approval_callback(approval.id, "approve_once")
    parsed = decode_approval_callback(cb)

    msg = await svc.resolve_callback(parsed, backend, ledger)

    assert "已处理" in msg
    assert backend._approval_resolutions == [("req-1", {"decision": "accept"})]

    # Duplicate should not re-resolve
    msg2 = await svc.resolve_callback(parsed, backend, ledger)
    assert "已处理" in msg2
    assert len(backend._approval_resolutions) == 1


@pytest.mark.asyncio
async def test_deny_sends_decline(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    svc = ApprovalService()

    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    approval = ledger.create_approval(
        task_id=task.id,
        codex_request_id="req-2",
        codex_item_id=None,
        codex_turn_id=None,
        kind=ApprovalKind.FILE_CHANGE,
        summary="Changed file",
    )

    cb = decode_approval_callback(encode_approval_callback(approval.id, "deny"))
    await svc.resolve_callback(cb, backend, ledger)

    assert backend._approval_resolutions == [("req-2", {"decision": "decline"})]


@pytest.mark.asyncio
async def test_legacy_approval_uses_review_decision_schema(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    svc = ApprovalService()

    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    approval = ledger.create_approval(
        task_id=task.id,
        codex_request_id="req-legacy",
        codex_item_id="call-1",
        codex_turn_id=None,
        kind=ApprovalKind.COMMAND,
        summary="Run legacy command",
        command_json='{"response_schema":"legacy_review_decision"}',
    )

    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))
    await svc.resolve_callback(cb, backend, ledger)

    assert backend._approval_resolutions == [
        ("req-legacy", {"decision": "approved"})
    ]


@pytest.mark.asyncio
async def test_unknown_approval_returns_message(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    backend = FakeCodexBackend()
    svc = ApprovalService()

    cb = ApprovalCallback(approval_id=9999, action="approve_once")
    msg = await svc.resolve_callback(cb, backend, ledger)
    assert "不存在" in msg


class FailingBackend:
    async def resolve_approval(self, codex_request_id: str, response: dict) -> None:
        raise RuntimeError("backend down")


@pytest.mark.asyncio
async def test_backend_failure_keeps_approval_pending(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    approval = ledger.create_approval(
        task.id, "req-1", None, None, ApprovalKind.COMMAND, "Run ls"
    )
    svc = ApprovalService(callback_timeout_seconds=3600, allow_session_approval=True)
    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))

    msg = await svc.resolve_callback(cb, FailingBackend(), ledger)

    assert "失败" in msg
    assert ledger.get_approval(approval.id).status.value == "pending"


@pytest.mark.asyncio
async def test_resolved_approval_decrements_pending_count(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    ledger.increment_pending_approvals(task.id, 1)
    approval = ledger.create_approval(
        task.id, "req-1", None, None, ApprovalKind.COMMAND, "Run ls"
    )
    backend = FakeCodexBackend()
    svc = ApprovalService(callback_timeout_seconds=3600, allow_session_approval=True)
    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))

    await svc.resolve_callback(cb, backend, ledger)

    assert ledger.get_task(task.id).pending_approval_count == 0


@pytest.mark.asyncio
async def test_expired_approval_unlocks_backend_before_local_expire(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    ledger.set_task_status(task.id, TaskStatus.WAITING_APPROVAL)
    ledger.increment_pending_approvals(task.id, 1)
    approval = ledger.create_approval(
        task.id, "req-expired", None, None, ApprovalKind.COMMAND, "Run ls"
    )
    backend = FakeCodexBackend()
    svc = ApprovalService(callback_timeout_seconds=-1, allow_session_approval=True)
    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))

    msg = await svc.resolve_callback(cb, backend, ledger)

    assert "已过期" in msg
    assert backend._approval_resolutions == [
        ("req-expired", {"decision": "cancel"})
    ]
    assert ledger.get_approval(approval.id).status == ApprovalStatus.EXPIRED
    assert ledger.get_task(task.id).pending_approval_count == 0
    assert ledger.get_task(task.id).status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_expired_approval_stays_pending_when_backend_unlock_fails(
    tmp_path: Path,
) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    ledger.set_task_status(task.id, TaskStatus.WAITING_APPROVAL)
    ledger.increment_pending_approvals(task.id, 1)
    approval = ledger.create_approval(
        task.id, "req-expired", None, None, ApprovalKind.COMMAND, "Run ls"
    )
    svc = ApprovalService(callback_timeout_seconds=-1, allow_session_approval=True)
    cb = decode_approval_callback(encode_approval_callback(approval.id, "approve_once"))

    msg = await svc.resolve_callback(cb, FailingBackend(), ledger)

    assert "后端解锁失败" in msg
    assert ledger.get_approval(approval.id).status == ApprovalStatus.PENDING
    assert ledger.get_task(task.id).pending_approval_count == 1
    assert ledger.get_task(task.id).status == TaskStatus.WAITING_APPROVAL
