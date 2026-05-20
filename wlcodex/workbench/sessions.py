"""Agent Session Library projection (Repair Task 3).

Projects existing ``agent_runs`` ledger rows into user-safe
``AgentSessionSummary`` cards.  Internal ids (external_session_id,
thread id, task id) are never exposed through ``user_label``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json


class AgentSessionResumability(Enum):
    LIVE = "live"
    RESUMABLE = "resumable"
    SUMMARY_ONLY = "summary_only"


@dataclass(frozen=True)
class AgentSessionSummary:
    conversation_id: int
    agent: str
    title: str
    status: str
    resumability: AgentSessionResumability
    user_label: str
    internal_ref: str
    source_run_id: int


class AgentSessionLibrary:
    """Projects agent_runs into user-safe historical session cards."""

    def __init__(self, ledger):
        self._ledger = ledger

    def list_for_workbench(
        self, conversation_id: int, limit: int = 20
    ) -> list[AgentSessionSummary]:
        runs = self._ledger.list_recent_agent_runs(conversation_id, limit=limit * 2)

        seen: set[tuple[str, str]] = set()
        sessions: list[AgentSessionSummary] = []
        for run in runs:
            if run.agent not in ("codex", "claude"):
                continue

            internal_ref = run.external_session_id or ""
            dedup_key = (run.agent, internal_ref)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            raw_title = (
                run.completion_summary
                or run.prompt_packet_summary
                or run.role
                or run.agent
            )
            title = _human_title(raw_title)
            if not title.strip():
                title = run.agent

            resumability = _classify(run.status, internal_ref)

            user_label = _build_user_label(resumability)

            sessions.append(
                AgentSessionSummary(
                    conversation_id=run.conversation_id,
                    agent=run.agent,
                    title=title,
                    status=run.status,
                    resumability=resumability,
                    user_label=user_label,
                    internal_ref=internal_ref,
                    source_run_id=run.id,
                )
            )

            if len(sessions) >= limit:
                break

        return sessions

    def get_for_workbench(
        self, conversation_id: int, source_run_id: int
    ) -> AgentSessionSummary | None:
        for session in self.list_for_workbench(conversation_id):
            if session.source_run_id == source_run_id:
                return session
        return None


def _classify(status: str, internal_ref: str) -> AgentSessionResumability:
    if not internal_ref:
        return AgentSessionResumability.SUMMARY_ONLY
    return AgentSessionResumability.RESUMABLE


def _build_user_label(resumability: AgentSessionResumability) -> str:
    if resumability is AgentSessionResumability.LIVE:
        return "可接管"
    elif resumability is AgentSessionResumability.RESUMABLE:
        return "可继续"
    else:
        return "可回顾"


def _human_title(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("summary", "title", "message"):
                summary = parsed.get(key)
                if isinstance(summary, str) and summary.strip():
                    return summary.strip()
    return value
