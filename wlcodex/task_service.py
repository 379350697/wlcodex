"""Task service — state machine, workspace locks, backend event ingestion."""

from __future__ import annotations

import logging
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from wlcodex.codex_backend import BackendEvent
from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.locks import ACTIVE_WRITE_STATUSES, active_write_task
from wlcodex.models import ApprovalKind, Task, TaskStatus

logger = logging.getLogger(__name__)

MAX_LOG_BYTES = 500 * 1024  # 500 KB — per-task log bounded to avoid 7x24 growth


class WorkspaceBusy(RuntimeError):
    pass


class InvalidTransition(RuntimeError):
    pass


_VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.PAUSED,
        TaskStatus.ABORTED,
    },
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.RUNNING,
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.PAUSED,
        TaskStatus.ABORTED,
    },
    TaskStatus.DONE: {TaskStatus.QUEUED, TaskStatus.ARCHIVED},
    TaskStatus.FAILED: {TaskStatus.QUEUED, TaskStatus.ARCHIVED},
    TaskStatus.PAUSED: {
        TaskStatus.QUEUED,
        TaskStatus.DONE,
        TaskStatus.ABORTED,
        TaskStatus.ARCHIVED,
    },
    TaskStatus.ABORTED: {TaskStatus.QUEUED, TaskStatus.ARCHIVED},
    TaskStatus.WAITING_SLOT: {TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.ABORTED, TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),
}


class TaskService:
    def __init__(
        self,
        ledger: Ledger,
        workspaces: Iterable[WorkspaceConfig],
        task_log_dir: Path | None = None,
        worktree_root: str = "runtime/worktrees",
    ) -> None:
        self._ledger = ledger
        self._workspaces = {workspace.alias: workspace for workspace in workspaces}
        self._task_log_dir = task_log_dir
        self._worktree_root = worktree_root

    # --- Public read methods ---

    def list_tasks(self, include_archived: bool = False) -> list[Task]:
        return self._ledger.list_tasks(include_archived=include_archived)

    def get_task(self, task_id: int) -> Task:
        return self._ledger.get_task(task_id)

    def get_workspace(self, alias: str) -> WorkspaceConfig:
        if alias not in self._workspaces:
            raise KeyError(f"unknown workspace alias: {alias}")
        return self._workspaces[alias]

    def ensure_workspace_writable(self, workspace_alias: str) -> WorkspaceConfig:
        workspace = self.get_workspace(workspace_alias)
        if not workspace.allow_write:
            raise PermissionError(f"workspace {workspace_alias} is read-only")
        return workspace

    def ensure_workspace_available(
        self, workspace_alias: str, exclude_task_id: int | None = None
    ) -> None:
        current = active_write_task(
            self._ledger.list_tasks(limit=100),
            workspace_alias,
            exclude_task_id=exclude_task_id,
        )
        if current is not None:
            raise WorkspaceBusy(
                f"workspace {workspace_alias} is busy with task #{current.id}"
            )

    # --- Waiting-slot queue ---

    def reserve_waiting_task(
        self,
        workspace_alias: str,
        prompt: str,
        telegram_chat_id: int | None = None,
        parent_task_id: int | None = None,
        blocker_task_id: int | None = None,
    ) -> Task:
        """Create a waiting_slot task when the workspace is busy.

        Does NOT create a Codex thread and does NOT acquire the workspace
        write lock.  The full prompt is stored in a task_waiting_slot_created
        event for later retrieval when the task is promoted.
        """
        workspace = self.ensure_workspace_writable(workspace_alias)
        task = self._ledger.create_task(
            workspace_alias=workspace.alias,
            workspace_path=str(workspace.path),
            title=_title(prompt),
            codex_thread_id=None,
            parent_task_id=parent_task_id,
            telegram_chat_id=telegram_chat_id,
            status=TaskStatus.WAITING_SLOT,
        )
        payload: dict[str, object] = {"prompt": prompt}
        if blocker_task_id is not None:
            payload["blocker_task_id"] = blocker_task_id
        self._ledger.add_event(task.id, "task_waiting_slot_created", payload)
        return task

    def get_stored_prompt(self, task_id: int) -> str:
        """Return the original prompt for a waiting task from its creation event."""
        events = self._ledger.list_events(task_id)
        for event in events:
            if event.event_type in ("task_waiting_slot_created", "task_reserved"):
                prompt = event.payload.get("prompt")
                if prompt and isinstance(prompt, str):
                    return prompt
        raise RuntimeError(f"missing stored prompt for waiting task #{task_id}")

    def promote_waiting_task(self, task_id: int) -> tuple[Task, str]:
        """Promote a waiting_slot task to queued. Returns (task, prompt)."""
        task = self._ledger.get_task(task_id)
        if task.status != TaskStatus.WAITING_SLOT:
            raise InvalidTransition(
                f"task #{task_id} is {task.status.value}, not waiting_slot"
            )
        prompt = self.get_stored_prompt(task_id)
        self._transition(task_id, TaskStatus.QUEUED)
        self._ledger.add_event(task_id, "task_promoted_from_waiting", {})
        return self._ledger.get_task(task_id), prompt

    def list_waiting_tasks(self, workspace_alias: str) -> list[Task]:
        """Return waiting_slot tasks for a workspace ordered by created_at, id."""
        return self._ledger.list_waiting_tasks(workspace_alias)

    def list_worktree_tasks(self, workspace_alias: str) -> list[Task]:
        """Return active worktree-isolated tasks for a workspace."""
        return [
            t for t in self._ledger.list_tasks(limit=100)
            if t.workspace_alias == workspace_alias
            and t.worktree_path
            and t.status in ACTIVE_WRITE_STATUSES
        ]

    def waiting_position(self, task_id: int) -> int:
        """Return the 1-based queue position of a waiting task within its workspace."""
        task = self._ledger.get_task(task_id)
        if task.status != TaskStatus.WAITING_SLOT:
            return 0
        waiting = self._ledger.list_waiting_tasks(task.workspace_alias)
        for idx, t in enumerate(waiting, start=1):
            if t.id == task_id:
                return idx
        return 0

    def blocker_for_workspace(self, workspace_alias: str) -> Task | None:
        """Return the active write task blocking a workspace, if any."""
        return active_write_task(
            self._ledger.list_tasks(limit=100), workspace_alias
        )

    # --- Force parallel ---

    def force_parallel_start(self, task_id: int) -> tuple[Task, str]:
        """Promote a waiting_slot task bypassing workspace lock check.

        Records force_parallel_started event and marks the task.
        The original workspace lock remains in place for subsequent /task calls.
        """
        task = self._ledger.get_task(task_id)
        if task.status != TaskStatus.WAITING_SLOT:
            raise InvalidTransition(
                f"task #{task_id} is {task.status.value}, not waiting_slot"
            )
        prompt = self.get_stored_prompt(task_id)
        self._transition(task_id, TaskStatus.QUEUED)
        self._ledger.set_force_parallel(task_id)
        self._ledger.add_event(task_id, "force_parallel_started", {
            "workspace_alias": task.workspace_alias,
            "workspace_path": task.workspace_path,
        })
        return self._ledger.get_task(task_id), prompt

    # --- Worktree isolation ---

    def setup_worktree(
        self, task_id: int, slug: str = ""
    ) -> tuple[Task, str, str]:
        """Create a git worktree for a waiting_slot task.

        Returns (task, worktree_path, branch_name).
        Does NOT promote the task yet — caller must call start_worktree_task.

        The worktree is created OUTSIDE the target workspace under the
        configured worktree_root (default runtime/worktrees/), so it never
        pollutes the workspace git status.
        """
        import os
        import subprocess

        task = self._ledger.get_task(task_id)
        if task.status != TaskStatus.WAITING_SLOT:
            raise InvalidTransition(
                f"task #{task_id} is {task.status.value}, not waiting_slot"
            )

        workspace = self.get_workspace(task.workspace_alias)
        branch = f"wlcodex/task-{task_id}-{slug}" if slug else f"wlcodex/task-{task_id}"
        # Absolute path outside the workspace — avoids polluting git status.
        wt_path = os.path.abspath(
            os.path.join(self._worktree_root, task.workspace_alias, f"task-{task_id}")
        )

        # Safety: refuse if the resolved worktree path falls inside the
        # workspace.  That would pollute the workspace git status and
        # eventually block merge_worktree's dirty-check.
        ws_abs = os.path.abspath(str(workspace.path))
        if os.path.commonpath([wt_path, ws_abs]) == ws_abs:
            raise RuntimeError(
                f"Worktree path {wt_path} is inside workspace {ws_abs}. "
                f"Set storage.worktree_root in config to a directory outside "
                f"all workspaces (e.g. ~/.local/state/wlcodex/worktrees)."
            )

        # Ensure parent directories exist so git worktree add can succeed.
        os.makedirs(os.path.dirname(wt_path), exist_ok=True)

        # Ensure the branch name is clean
        branch = "".join(c for c in branch if c.isalnum() or c in "/-_.")[:80]

        try:
            subprocess.run(
                ["git", "-C", str(workspace.path), "worktree", "add", "-b", branch, wt_path],
                capture_output=True, text=True, timeout=30, check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to create worktree for task #{task_id}: {exc.stderr.strip()}"
            ) from exc

        self._ledger.set_worktree_info(task_id, wt_path, branch)
        self._ledger.add_event(task_id, "worktree_created", {
            "worktree_path": wt_path,
            "branch": branch,
            "original_path": str(workspace.path),
        })
        return self._ledger.get_task(task_id), wt_path, branch

    def start_worktree_task(
        self, task_id: int,
    ) -> tuple[Task, str, str]:
        """Promote a waiting_slot task that has a worktree set up.

        Returns (task, prompt, worktree_path).
        """
        task = self._ledger.get_task(task_id)
        if task.status != TaskStatus.WAITING_SLOT:
            raise InvalidTransition(
                f"task #{task_id} is {task.status.value}, not waiting_slot"
            )
        if not task.worktree_path:
            raise RuntimeError(f"task #{task_id} has no worktree path set")

        prompt = self.get_stored_prompt(task_id)
        self._transition(task_id, TaskStatus.QUEUED)
        self._ledger.add_event(task_id, "worktree_task_started", {
            "worktree_path": task.worktree_path,
            "branch": task.worktree_branch,
        })
        return self._ledger.get_task(task_id), prompt, task.worktree_path

    def merge_worktree(self, task_id: int) -> str:
        """Merge worktree branch into the original workspace.

        Returns a status message. Conservative: stops on conflicts.
        The branch already exists in the main repo (created by git worktree add -b).
        """
        import subprocess

        task = self._ledger.get_task(task_id)
        if not task.worktree_branch:
            raise RuntimeError(f"task #{task_id} has no worktree branch")
        if not task.worktree_path:
            raise RuntimeError(f"task #{task_id} has no worktree path")

        workspace = self.get_workspace(task.workspace_alias)

        # Preflight: refuse merge if the main workspace is dirty.
        # Uncommitted changes would mix with the worktree merge result.
        status_check = subprocess.run(
            ["git", "-C", str(workspace.path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if status_check.stdout.strip():
            raise RuntimeError(
                f"主工作区有未提交的改动，无法安全合并。"
                f"请先提交或暂存工作区改动。\n\n"
                f"Worktree: {task.worktree_path}\n"
                f"Branch: {task.worktree_branch}"
            )

        # Verify the branch exists in the main repo
        branch_check = subprocess.run(
            ["git", "-C", str(workspace.path), "branch", "--list", task.worktree_branch],
            capture_output=True, text=True, timeout=10,
        )
        if not branch_check.stdout.strip():
            raise RuntimeError(
                f"Branch {task.worktree_branch} not found in main repo. "
                f"The worktree may have already been discarded."
            )

        try:
            result = subprocess.run(
                ["git", "-C", str(workspace.path), "merge", "--no-ff", task.worktree_branch],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Merge failed: {exc}") from exc

        if result.returncode != 0:
            # Abort the failed merge to leave a clean state
            subprocess.run(
                ["git", "-C", str(workspace.path), "merge", "--abort"],
                capture_output=True, text=True, timeout=10,
            )
            msg = (
                f"合并冲突 — 需要人工处理。已回滚合并，工作区保持干净。\n"
                f"Worktree: {task.worktree_path}\n"
                f"Branch: {task.worktree_branch}\n\n"
                f"{result.stdout[:500]}\n{result.stderr[:500]}"
            )
            self._ledger.add_event(task_id, "worktree_merge_conflict", {
                "stdout": result.stdout[:1000],
                "stderr": result.stderr[:1000],
            })
            return msg

        self._ledger.add_event(task_id, "worktree_merged", {
            "branch": task.worktree_branch,
        })
        return f"Worktree 分支 {task.worktree_branch} 已合并到主工作区。\n{result.stdout[:500]}"

    def discard_worktree(self, task_id: int) -> str:
        """Delete the worktree directory and branch.

        Does NOT affect the main workspace.
        """
        import subprocess

        task = self._ledger.get_task(task_id)
        if not task.worktree_path:
            raise RuntimeError(f"task #{task_id} has no worktree path")

        workspace = self.get_workspace(task.workspace_alias)
        wt_path = task.worktree_path
        branch = task.worktree_branch

        errors: list[str] = []

        # Remove worktree via git
        try:
            subprocess.run(
                ["git", "-C", str(workspace.path), "worktree", "remove", "--force", wt_path],
                capture_output=True, text=True, timeout=30, check=True,
            )
        except subprocess.CalledProcessError as exc:
            errors.append(f"git worktree remove: {exc.stderr.strip()}")

        # Delete branch if it still exists
        if branch:
            try:
                subprocess.run(
                    ["git", "-C", str(workspace.path), "branch", "-D", branch],
                    capture_output=True, text=True, timeout=30,
                )
            except subprocess.CalledProcessError:
                pass  # Branch may already be gone

        if errors:
            raise RuntimeError("; ".join(errors))

        self._ledger.add_event(task_id, "worktree_discarded", {
            "worktree_path": wt_path,
            "branch": branch,
        })
        return f"Worktree 已丢弃：{wt_path}"

    # --- Lifecycle: start ---

    def reserve_task(
        self,
        workspace_alias: str,
        prompt: str,
        telegram_chat_id: int | None = None,
        parent_task_id: int | None = None,
    ) -> Task:
        """Create a queued task reservation without a codex thread yet."""
        workspace = self.ensure_workspace_writable(workspace_alias)
        self.ensure_workspace_available(workspace_alias)
        task = self._ledger.create_task(
            workspace_alias=workspace.alias,
            workspace_path=str(workspace.path),
            title=_title(prompt),
            codex_thread_id=None,
            parent_task_id=parent_task_id,
            telegram_chat_id=telegram_chat_id,
        )
        self._ledger.add_event(
            task.id,
            "task_reserved",
            {"prompt": prompt, "context_policy": "fresh_thread_by_default"},
        )
        return task

    def set_task_thread(self, task_id: int, thread_id: str) -> Task:
        """Persist the codex thread id after a successful thread/start call."""
        self._ledger.set_thread_id(task_id, thread_id)
        self._ledger.add_event(task_id, "thread_started", {"threadId": thread_id})
        return self._ledger.get_task(task_id)

    def start_task(
        self,
        workspace_alias: str,
        prompt: str,
        codex_thread_id: str | None = None,
        telegram_chat_id: int | None = None,
        parent_task_id: int | None = None,
    ) -> Task:
        workspace = self.get_workspace(workspace_alias)
        self.ensure_workspace_available(workspace_alias)
        task = self._ledger.create_task(
            workspace_alias=workspace.alias,
            workspace_path=str(workspace.path),
            title=_title(prompt),
            codex_thread_id=codex_thread_id,
            parent_task_id=parent_task_id,
            telegram_chat_id=telegram_chat_id,
        )
        self._ledger.add_event(
            task.id,
            "task_created",
            {"prompt": prompt, "context_policy": "fresh_thread_by_default"},
        )
        return task

    # --- Lifecycle: continue ---

    def continue_task(
        self, task_id: int, prompt: str, new_codex_thread_id: str | None = None
    ) -> Task:
        task = self._ledger.get_task(task_id)

        if task.status == TaskStatus.WAITING_SLOT:
            raise RuntimeError(
                f"task #{task_id} is waiting_slot — 等待中的任务尚无 Codex 线程，无法继续。"
                f"请等待工作区空闲后自动启动，或用 /abort {task_id} 取消。"
            )

        if task.status == TaskStatus.ARCHIVED:
            raise RuntimeError(f"task #{task_id} is archived; unarchive first")

        if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL):
            raise RuntimeError(
                f"task #{task_id} is {task.status.value}, use /steer for active turns"
            )

        if task.codex_thread_id is None and new_codex_thread_id is None:
            raise RuntimeError(f"task #{task_id} has no codex thread; cannot continue")

        # Enforce writable workspace and availability (exclude self from lock check)
        self.ensure_workspace_writable(task.workspace_alias)
        self.ensure_workspace_available(
            task.workspace_alias,
            exclude_task_id=task_id,
        )

        # Transition to queued (ready for a new turn)
        self._transition(task_id, TaskStatus.QUEUED)
        self._ledger.clear_active_turn(task_id)

        self._ledger.add_event(
            task.id,
            "user_continue",
            {"prompt": prompt, "context_policy": "explicit_resume_only"},
        )
        return self._ledger.get_task(task_id)

    # --- Lifecycle: steer ---

    def steer_task(self, task_id: int, prompt: str) -> Task:
        task = self._ledger.get_task(task_id)
        if task.status == TaskStatus.WAITING_SLOT:
            raise RuntimeError(
                f"task #{task_id} is waiting_slot — 等待中的任务尚无活跃 turn，无法追加指令。"
                f"请等待工作区空闲后自动启动，或用 /abort {task_id} 取消。"
            )
        if task.active_turn_id is None:
            raise RuntimeError(
                f"task #{task_id} has no active turn. Use /continue to start a new turn."
            )
        if task.status not in (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL):
            raise RuntimeError(
                f"task #{task_id} is {task.status.value}. "
                f"Steer requires a running or waiting_approval task with an active turn."
            )
        self.ensure_workspace_writable(task.workspace_alias)
        self._ledger.add_event(task.id, "user_steer", {"prompt": prompt})
        return task

    # --- Lifecycle: pause ---

    def pause_task(self, task_id: int) -> Task:
        task = self._ledger.get_task(task_id)
        if task.status not in (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.QUEUED):
            raise RuntimeError(f"Cannot pause task #{task_id} in status {task.status.value}")
        self._transition(task_id, TaskStatus.PAUSED)
        self._ledger.add_event(task_id, "user_paused", {})
        return self._ledger.get_task(task_id)

    # --- Lifecycle: abort ---

    def abort_task(self, task_id: int) -> Task:
        task = self._ledger.get_task(task_id)
        if task.status not in (
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.QUEUED,
            TaskStatus.PAUSED,
            TaskStatus.WAITING_SLOT,
        ):
            raise RuntimeError(f"Cannot abort task #{task_id} in status {task.status.value}")
        self._ledger.clear_active_turn(task_id)
        self._transition(task_id, TaskStatus.ABORTED)
        self._ledger.add_event(task_id, "user_aborted", {})
        return self._ledger.get_task(task_id)

    # --- Lifecycle: archive ---

    def archive_task(self, task_id: int) -> Task:
        task = self._ledger.get_task(task_id)
        if task.status in (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.WAITING_APPROVAL):
            raise RuntimeError(
                f"Cannot archive task #{task_id} while it is {task.status.value}"
            )
        self._transition(task_id, TaskStatus.ARCHIVED)
        self._ledger.add_event(task_id, "user_archived", {})
        return self._ledger.get_task(task_id)

    # --- Lifecycle: fail ---

    def fail_task(self, task_id: int, error: str) -> Task:
        task = self._ledger.get_task(task_id)
        if task.status in (TaskStatus.DONE, TaskStatus.ARCHIVED):
            raise RuntimeError(f"Cannot fail task #{task_id} in status {task.status.value}")
        self._transition(task_id, TaskStatus.FAILED)
        self._ledger.set_task_status(task_id, TaskStatus.FAILED, error=error[:240])
        self._ledger.add_event(task_id, "task_failed", {"error": error})
        return self._ledger.get_task(task_id)

    # --- State transitions from backend events ---

    def apply_backend_event(self, event: BackendEvent) -> None:
        """Ingest a typed backend event and update task state + ledger."""
        etype = event.event_type
        payload = event.payload

        if etype == "turn_started":
            thread_id = str(payload.get("threadId", ""))
            turn_id = str(payload.get("turnId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                logger.warning("turn_started for unknown thread: %s", thread_id)
                return
            self._ledger.set_active_turn(task.id, turn_id)
            self._transition(task.id, TaskStatus.RUNNING)
            self._ledger.add_event(
                task.id, "turn_started", {"threadId": thread_id, "turnId": turn_id}
            )

        elif etype == "turn_completed":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            self._ledger.clear_active_turn(task.id)
            turn_status = _turn_status(payload)
            if turn_status == "failed":
                error = _turn_error_message(payload)
                self._mark_backend_failed(
                    task.id,
                    error or "Codex turn failed",
                    phase="failed",
                    event_type="turn_failed",
                    event_payload={"threadId": thread_id, "error": error},
                )
                return
            if turn_status == "interrupted":
                if task.status not in (TaskStatus.ABORTED, TaskStatus.ARCHIVED):
                    self._ledger.set_task_status(
                        task.id,
                        TaskStatus.ABORTED,
                        phase="interrupted",
                        error="Codex turn interrupted",
                    )
                    self._ledger.add_event(
                        task.id,
                        "turn_interrupted",
                        {"threadId": thread_id},
                    )
                return
            if turn_status == "inProgress":
                self._ledger.set_task_status(
                    task.id,
                    task.status,
                    phase="inProgress",
                )
                return
            if task.status in (TaskStatus.FAILED, TaskStatus.ABORTED, TaskStatus.ARCHIVED):
                return
            pending = self._ledger.pending_approvals(task.id)
            if pending:
                if task.status == TaskStatus.PAUSED:
                    self._ledger.set_task_status(
                        task.id,
                        TaskStatus.PAUSED,
                        phase="waiting_approval",
                    )
                else:
                    self._transition(task.id, TaskStatus.WAITING_APPROVAL)
            else:
                self._transition(task.id, TaskStatus.DONE)
            self._ledger.add_event(
                task.id, "turn_completed", {"threadId": thread_id}
            )

        elif etype == "approval_requested":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            kind = str(payload.get("kind", "command"))
            codex_request_id = str(payload.get("codexRequestId", payload.get("id", "")))
            codex_item_id = str(payload.get("codexItemId", "")) or None
            codex_turn_id = str(payload.get("codexTurnId", "")) or None
            summary = str(
                payload.get("summary")
                or payload.get("reason")
                or payload.get("command")
                or ""
            )

            try:
                approval_kind = ApprovalKind(kind)
            except ValueError:
                approval_kind = ApprovalKind.COMMAND

            # For permissions approvals, save the requested permissions profile.
            # For command/file-change approvals, save the command string.
            if approval_kind == ApprovalKind.PERMISSIONS:
                import json
                perms = payload.get("permissions", {})
                stored_payload = json.dumps(perms, ensure_ascii=False) if isinstance(perms, dict) else str(perms)
            elif payload.get("responseSchema") == "legacy_review_decision":
                stored_payload = json.dumps(
                    {
                        "response_schema": "legacy_review_decision",
                        "command": payload.get("command", ""),
                    },
                    ensure_ascii=False,
                )
            else:
                stored_payload = str(payload.get("command", payload.get("permissions", "{}")))

            existing_approval = self._ledger.get_approval_by_codex_id(
                codex_request_id, task_id=task.id
            )
            self._ledger.create_approval(
                task_id=task.id,
                codex_request_id=codex_request_id,
                codex_item_id=codex_item_id or None,
                codex_turn_id=codex_turn_id or None,
                kind=approval_kind,
                summary=summary,
                command_json=stored_payload,
            )
            if existing_approval is None:
                self._ledger.increment_pending_approvals(task.id, 1)
            if task.status == TaskStatus.PAUSED:
                self._ledger.set_task_status(
                    task.id,
                    TaskStatus.PAUSED,
                    phase="active:waitingOnApproval",
                )
            else:
                self._transition(task.id, TaskStatus.WAITING_APPROVAL)
            self._ledger.add_event(
                task.id, "approval_requested", {k: str(v) for k, v in payload.items()}
            )

        elif etype == "plan_updated":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            plan_text = str(payload.get("plan", payload.get("summary", "")))
            self._ledger.set_task_status(
                task.id, task.status, phase="planning", summary=plan_text
            )

        elif etype == "diff_updated":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            self._ledger.increment_changed_files(task.id, 1)
            diff_text = str(payload.get("diff", payload.get("summary", "")))
            if diff_text:
                self._ledger.add_event(
                    task.id,
                    "diff_updated",
                    {"diff": diff_text[:2000]},
                )

        elif etype == "file_change_delta":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            file_path = str(payload.get("path", ""))
            change_kind = str(payload.get("kind", "modified"))
            if file_path:
                self._ledger.record_touched_file(task.id, file_path, change_kind)

        elif etype == "token_usage_updated":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            ti = int(payload.get("inputTokens", 0))
            to = int(payload.get("outputTokens", 0))
            self._ledger.set_token_usage(task.id, ti, to)

        elif etype == "command_output_delta":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            delta = str(payload.get("delta", payload.get("text", "")))
            if delta:
                self._ledger.add_event(
                    task.id,
                    "command_output",
                    {"delta": delta[:2000]},
                )
                self._append_task_log(task.id, delta)

        elif etype == "agent_message_delta":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            delta = str(payload.get("delta", ""))
            if delta:
                summary = str(payload.get("summary", delta))[:240]
                self._ledger.set_task_status(
                    task.id, task.status,
                    summary=summary,
                )
                self._ledger.add_event(
                    task.id,
                    "agent_message_delta",
                    {"delta": delta[:2000], "summary": summary},
                )
                self._append_task_log(task.id, delta)

        elif etype == "thread_status_changed":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            status = _thread_status_type(payload)
            if status:
                if status == "systemError":
                    self._ledger.clear_active_turn(task.id)
                    self._mark_backend_failed(
                        task.id,
                        "Codex thread entered systemError",
                        phase=status,
                        event_type="thread_system_error",
                        event_payload={"threadId": thread_id, "status": status},
                    )
                    return
                self._ledger.set_task_status(
                    task.id, task.status, phase=status[:120]
                )

        elif etype == "item_started":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            item_payload = _item_event_payload(payload)
            self._ledger.add_event(
                task.id, "item_started", item_payload
            )

        elif etype == "item_completed":
            thread_id = str(payload.get("threadId", ""))
            task = self._find_by_thread(thread_id)
            if task is None:
                return
            item_payload = _item_event_payload(payload)
            self._ledger.add_event(
                task.id, "item_completed", item_payload
            )

    # --- Helpers ---

    def _append_task_log(self, task_id: int, text: str) -> None:
        """Write bounded text to the per-task log file.

        File is capped at MAX_LOG_BYTES.  When exceeded the oldest content
        is discarded (keep last ~75%).  When no log dir is configured this
        is a no-op and /tail falls back to SQLite event deltas.
        """
        if not text or self._task_log_dir is None:
            return
        try:
            self._task_log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._task_log_dir / f"{task_id}.log"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(text)
                if not text.endswith("\n"):
                    fh.write("\n")
            # Bound the file — keep the tail when it exceeds the cap
            if log_path.stat().st_size > MAX_LOG_BYTES:
                content = log_path.read_text(encoding="utf-8")
                keep_bytes = int(MAX_LOG_BYTES * 0.75)
                truncated = content[-keep_bytes:]
                # Snap to next newline so we don't start on a partial line
                nl = truncated.find("\n")
                if nl > 0 and nl < len(truncated) - 1:
                    truncated = truncated[nl + 1:]
                log_path.write_text(truncated, encoding="utf-8")
        except OSError:
            pass  # Best effort — inspection falls back to SQLite events

    def _find_by_thread(self, thread_id: str) -> Task | None:
        for task in self._ledger.list_tasks(limit=100):
            if task.codex_thread_id == thread_id:
                return task
        return None

    def _transition(self, task_id: int, new_status: TaskStatus) -> None:
        task = self._ledger.get_task(task_id)
        allowed = _VALID_TRANSITIONS.get(task.status, set())
        if new_status not in allowed and task.status != new_status:
            raise InvalidTransition(
                f"Cannot transition task #{task_id} from {task.status.value} to {new_status.value}"
            )
        self._ledger.set_task_status(task_id, new_status)

    def _mark_backend_failed(
        self,
        task_id: int,
        error: str,
        *,
        phase: str,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> None:
        error_text = error[:240]
        self._ledger.set_task_status(
            task_id,
            TaskStatus.FAILED,
            phase=phase[:120],
            error=error_text,
        )
        self._ledger.add_event(task_id, event_type, event_payload)
        self._ledger.add_event(task_id, "task_failed", {"error": error})

    # --- Legacy compatibility ---

    def record_user_continue(self, task_id: int, prompt: str) -> Task:
        return self.continue_task(task_id, prompt)

    def record_user_steer(self, task_id: int, prompt: str) -> Task:
        return self.steer_task(task_id, prompt)


def _title(prompt: str) -> str:
    one_line = " ".join(prompt.split())
    return one_line[:80]


async def drain_workspace(
    service: TaskService, backend: object, workspace_alias: str
) -> Task | None:
    """Promote and start the first waiting task if the workspace is free.

    Returns the promoted task on success, or None if no waiting task exists
    or the workspace is still busy.
    """
    from wlcodex.locks import active_write_task

    blocker = active_write_task(
        service._ledger.list_tasks(limit=100), workspace_alias
    )
    if blocker is not None:
        return None

    waiting = service.list_waiting_tasks(workspace_alias)
    if not waiting:
        return None

    next_task = waiting[0]
    try:
        task, prompt = service.promote_waiting_task(next_task.id)
    except RuntimeError:
        service.fail_task(next_task.id, "missing stored prompt")
        return None

    workspace = service.get_workspace(workspace_alias)
    try:
        thread_id = await backend.create_thread(str(workspace.path))
        service.set_task_thread(task.id, thread_id)
        await backend.start_turn(thread_id, prompt)
    except Exception as exc:
        service.fail_task(task.id, str(exc))
        return None

    return service._ledger.get_task(task.id)


def _turn_status(payload: dict[str, object]) -> str:
    turn = payload.get("turn")
    if isinstance(turn, dict):
        status = turn.get("status")
        if status:
            return str(status)
    status = payload.get("status")
    if status:
        return str(status)
    return "completed"


def _turn_error_message(payload: dict[str, object]) -> str:
    turn = payload.get("turn")
    error: object = payload.get("error")
    if isinstance(turn, dict) and turn.get("error") is not None:
        error = turn.get("error")
    if isinstance(error, dict):
        message = str(error.get("message", "")).strip()
        details = str(error.get("additionalDetails", "") or "").strip()
        code = error.get("codexErrorInfo")
        parts = [part for part in (message, details, str(code) if code else "") if part]
        return " | ".join(parts)
    return str(error or "").strip()


def _thread_status_type(payload: dict[str, object]) -> str:
    status = payload.get("status", payload.get("phase", ""))
    if isinstance(status, dict):
        status_type = str(status.get("type", ""))
        flags = status.get("activeFlags")
        if status_type == "active" and isinstance(flags, list) and flags:
            return f"active:{','.join(str(flag) for flag in flags)}"
        return status_type
    return str(status)


def _item_event_payload(payload: dict[str, object]) -> dict[str, object]:
    item = payload.get("item")
    if not isinstance(item, dict):
        return {"type": str(payload.get("type", ""))}

    result: dict[str, object] = {"type": str(item.get("type", ""))}
    for key in ("status", "command", "tool", "path"):
        if item.get(key) is not None:
            result[key] = str(item[key])
    if isinstance(item.get("changes"), list):
        result["change_count"] = len(item["changes"])
    if item.get("error") is not None:
        result["error"] = _compact_json(item["error"])
    return result


def _compact_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:500]
    except TypeError:
        return str(value)[:500]
