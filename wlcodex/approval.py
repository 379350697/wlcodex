"""Approval service — callback encoding, resolution, idempotence."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from wlcodex.codex_backend import (
    build_approval_response,
    build_legacy_approval_response,
)
from wlcodex.db import Ledger
from wlcodex.models import ApprovalKind, ApprovalStatus

logger = logging.getLogger(__name__)

CALLBACK_SEPARATOR = ":"


@dataclass(frozen=True)
class ApprovalCallback:
    approval_id: int
    action: str  # approve_once, approve_session, deny, cancel


def encode_approval_callback(approval_id: int, action: str) -> str:
    return f"approval{CALLBACK_SEPARATOR}{approval_id}{CALLBACK_SEPARATOR}{action}"


def decode_approval_callback(data: str) -> ApprovalCallback | None:
    try:
        parts = data.split(CALLBACK_SEPARATOR)
        if len(parts) != 3 or parts[0] != "approval":
            return None
        return ApprovalCallback(approval_id=int(parts[1]), action=parts[2])
    except (ValueError, TypeError):
        return None


class ApprovalService:
    def __init__(
        self,
        callback_timeout_seconds: int = 3600,
        allow_session_approval: bool = True,
        workspaces: dict[str, object] | None = None,
    ) -> None:
        self._callback_timeout = callback_timeout_seconds
        self._allow_session_approval = allow_session_approval
        self._workspaces = workspaces or {}

    async def resolve_callback(
        self,
        callback: ApprovalCallback,
        backend: object,
        ledger: Ledger,
    ) -> str:
        """Resolve an approval callback. Returns a user-facing status message.

        Order: load -> check pending -> check expiry -> build schema response ->
        send backend -> resolve local -> decrement -> move task to running.
        """
        if callback.action not in ("approve_once", "approve_session", "deny", "cancel"):
            return f"未知审批操作：{callback.action}"

        # 1. Load row
        try:
            approval = ledger.get_approval(callback.approval_id)
        except KeyError:
            return "审批不存在。"

        # 2. Check pending
        if approval.status != ApprovalStatus.PENDING:
            return f"审批已处理（{approval.status.value}）：{approval.resolution or ''}"

        # 3. Check expiry — must unlock the real Codex held request first,
        #    then update local DB so the Codex turn is not stuck forever.
        now = datetime.now(timezone.utc)
        age = (now - approval.created_at).total_seconds()
        if age > self._callback_timeout:
            unlocked = await self._send_backend_expiry(approval, backend, ledger)
            if not unlocked:
                ledger.set_approval_error(callback.approval_id, "expiry unlock failed")
                return (
                    f"审批 #{approval.id} 已过期，但后端解锁失败。"
                    "已保持本地待处理状态。"
                )
            await self._resolve_locally_expired(approval, ledger)
            return f"审批 #{approval.id} 已过期。"

        # 3b. Check workspace writability for approving write actions
        if callback.action in ("approve_once", "approve_session"):
            task = ledger.get_task(approval.task_id)
            ws = self._workspaces.get(task.workspace_alias)
            if ws is not None and not getattr(ws, "allow_write", True):
                return (
                    f"工作区 {task.workspace_alias} 是只读的。"
                    f"审批 #{approval.id} 未处理。"
                )

        # 4. Build schema response
        requested_permissions = {}
        if approval.kind == ApprovalKind.PERMISSIONS:
            try:
                requested_permissions = json.loads(approval.command_json or "{}")
            except json.JSONDecodeError:
                requested_permissions = {}

        response = self._build_backend_response(
            approval=approval,
            action=callback.action,
            requested_permissions=requested_permissions,
        )

        # 5. Send backend response FIRST (so we don't lose the approval
        #    if local state update fails)
        try:
            await backend.resolve_approval(approval.codex_request_id, response)
        except Exception as exc:
            logger.error("Failed to send approval resolution to backend: %s", exc)
            ledger.set_approval_error(callback.approval_id, str(exc))
            return f"审批已在本地记录，但发送到后端失败：{exc}"

        # 6. Resolve local row (only after backend send succeeded)
        resolution = callback.action
        if callback.action == "approve_once":
            ledger.resolve_approval(
                callback.approval_id, ApprovalStatus.APPROVED, resolution
            )
        elif callback.action == "approve_session":
            ledger.resolve_approval(
                callback.approval_id, ApprovalStatus.APPROVED, resolution
            )
        elif callback.action == "deny":
            ledger.resolve_approval(
                callback.approval_id, ApprovalStatus.DENIED, resolution
            )
        elif callback.action == "cancel":
            ledger.resolve_approval(
                callback.approval_id, ApprovalStatus.CANCELLED, resolution
            )
            # Per spec: cancel for permissions must also interrupt the
            # active turn when an active turn id is known.
            if approval.kind == ApprovalKind.PERMISSIONS:
                task = ledger.get_task(approval.task_id)
                if task.active_turn_id and task.codex_thread_id:
                    try:
                        await backend.interrupt_turn(
                            task.codex_thread_id, task.active_turn_id
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to interrupt turn after permissions cancel: %s", exc
                        )

        # 7. Decrement pending count
        ledger.decrement_pending_approvals(approval.task_id)

        # 8. Move task back to running if waiting and no pending remain
        from wlcodex.models import TaskStatus
        task = ledger.get_task(approval.task_id)
        if task.status == TaskStatus.WAITING_APPROVAL:
            remaining = ledger.pending_approvals(approval.task_id)
            if not remaining:
                ledger.set_task_status(approval.task_id, TaskStatus.RUNNING)

        return f"审批 #{approval.id} 已处理：{_action_label(resolution)}"

    def _build_backend_response(
        self,
        *,
        approval: object,
        action: str,
        requested_permissions: dict[str, object],
    ) -> dict[str, object]:
        """Return the response shape expected by the request's protocol."""
        if _uses_legacy_review_decision(approval):
            return build_legacy_approval_response(
                action=action,
                allow_session=self._allow_session_approval,
            )
        return build_approval_response(
            kind=approval.kind,
            action=action,
            requested_permissions=requested_permissions,
            allow_session=self._allow_session_approval,
        )

    async def _send_backend_expiry(
        self, approval: object, backend: object, ledger: Ledger | None = None
    ) -> bool:
        """Send cancel/decline to Codex app-server to unlock the held request.

        For permissions: send cancel (empty permissions) + interrupt turn.
        For command/file: send cancel.
        Returns False when the held request could not be resolved; callers
        must keep local approval state pending in that case.
        """
        requested_permissions = {}
        if approval.kind == ApprovalKind.PERMISSIONS:
            try:
                requested_permissions = json.loads(approval.command_json or "{}")
            except json.JSONDecodeError:
                requested_permissions = {}

        cancel_response = build_approval_response(
            kind=approval.kind,
            action="cancel",
            requested_permissions=requested_permissions,
            allow_session=self._allow_session_approval,
        )
        if _uses_legacy_review_decision(approval):
            cancel_response = build_legacy_approval_response(
                action="cancel",
                allow_session=self._allow_session_approval,
            )
        try:
            await backend.resolve_approval(approval.codex_request_id, cancel_response)
        except Exception as exc:
            logger.error(
                "Failed to send expiry cancel to backend for approval #%d: %s",
                approval.id, exc,
            )
            return False

        if approval.kind == ApprovalKind.PERMISSIONS and ledger is not None:
            try:
                task = ledger.get_task(approval.task_id)
                if task.active_turn_id and task.codex_thread_id:
                    await backend.interrupt_turn(
                        task.codex_thread_id, task.active_turn_id
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to interrupt after permissions expiry #%d: %s",
                    approval.id, exc,
                )
        return True

    async def _resolve_locally_expired(
        self, approval: object, ledger: Ledger
    ) -> None:
        """Resolve a single expired approval locally after backend is unlocked."""
        ledger.resolve_approval(approval.id, ApprovalStatus.EXPIRED, "timeout")
        ledger.decrement_pending_approvals(approval.task_id)

        from wlcodex.models import TaskStatus
        task = ledger.get_task(approval.task_id)
        if task.status == TaskStatus.WAITING_APPROVAL:
            remaining = ledger.pending_approvals(approval.task_id)
            if not remaining:
                ledger.set_task_status(approval.task_id, TaskStatus.RUNNING)

    async def expire_stale_approvals(
        self, ledger: Ledger, backend: object
    ) -> int:
        """Scan all pending approvals and expire those past the callback timeout.

        Sends backend cancel/decline before local resolution for each
        expired approval.  Returns the number of approvals expired.
        """
        now = datetime.now(timezone.utc)
        expired_count = 0

        tasks = ledger.list_tasks(limit=100, include_archived=False)
        for task in tasks:
            pending = ledger.pending_approvals(task.id)
            for approval in pending:
                age = (now - approval.created_at).total_seconds()
                if age > self._callback_timeout:
                    unlocked = await self._send_backend_expiry(
                        approval, backend, ledger
                    )
                    if not unlocked:
                        ledger.set_approval_error(
                            approval.id, "expiry unlock failed"
                        )
                        continue
                    await self._resolve_locally_expired(approval, ledger)
                    expired_count += 1

        if expired_count:
            logger.info("Expired %d stale approval(s)", expired_count)
        return expired_count


def _uses_legacy_review_decision(approval: object) -> bool:
    """Whether this held request uses the deprecated review-decision schema."""
    command_json = str(getattr(approval, "command_json", "") or "")
    try:
        payload = json.loads(command_json)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("response_schema") == "legacy_review_decision"
    )


def _action_label(action: str) -> str:
    return {
        "approve_once": "批准一次",
        "approve_session": "本会话批准",
        "deny": "拒绝",
        "cancel": "取消",
    }.get(action, action)
