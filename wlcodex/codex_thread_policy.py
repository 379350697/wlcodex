"""Helpers for deciding whether a Codex thread can be safely reused."""

from __future__ import annotations


def codex_thread_policy_fingerprint(backend: object) -> str:
    """Return the sandbox/approval policy that a new Codex thread will inherit."""
    approval_policy = str(getattr(backend, "approval_policy", "") or "")
    sandbox = str(getattr(backend, "sandbox", "") or "")
    if not approval_policy and not sandbox:
        return ""
    return f"approval_policy={approval_policy};sandbox={sandbox}"


def can_reuse_codex_thread(
    thread_id: str, stored_policy: str, current_policy: str
) -> bool:
    if not thread_id:
        return False
    if not current_policy:
        return True
    return stored_policy == current_policy
