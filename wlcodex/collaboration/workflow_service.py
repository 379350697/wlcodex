from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from wlcodex.collaboration.handoff_prompts import build_handoff_preview
from wlcodex.collaboration.models import (
    HandoffArtifact,
    HandoffIntent,
    HandoffPreviewInput,
)
from wlcodex.collaboration.workflow_store import WorkflowRunStore
from wlcodex.native_agents.provider import NativeAgentRegistry


_ARTIFACT_PATH_RE = re.compile(
    r"(docs/(?:superpowers/(?:specs|plans)|bugs)/[^\s,;:]+)",
)


class WorkflowService:
    def __init__(
        self,
        *,
        registry: NativeAgentRegistry,
        store: WorkflowRunStore,
        default_worker_id: int = 128,
    ) -> None:
        self._registry = registry
        self._store = store
        self._default_worker_id = default_worker_id

    async def preview_handoff(
        self,
        *,
        source_provider: str,
        source_thread_id: str,
        source_turn_id: str,
        target_provider: str,
        cwd: str,
        intent: str,
        user_note: str,
    ) -> dict[str, Any]:
        source = self._registry.get(source_provider)
        self._registry.get(target_provider)
        session = await source.read_session(source_thread_id)
        turns = _session_turns(session)
        recent_user_text = _newest_user_text(turns)
        session_summary = _session_summary(turns)
        artifacts = _artifacts_from_text(f"{recent_user_text}\n{session_summary}")
        preview = build_handoff_preview(
            HandoffPreviewInput(
                source_provider=source_provider,
                source_thread_id=source_thread_id,
                target_provider=target_provider,
                cwd=cwd,
                recent_user_text=recent_user_text,
                session_summary=session_summary,
                artifacts=artifacts,
                user_note=user_note,
                requested_intent=_handoff_intent(intent),
            )
        )
        stored = self._store.create_preview(
            source_provider=source_provider,
            source_thread_id=source_thread_id,
            source_turn_id=source_turn_id,
            target_provider=target_provider,
            cwd=cwd,
            intent=preview.intent,
            prompt=preview.prompt,
            artifacts=preview.artifacts,
            warnings=preview.warnings,
        )
        payload = preview.to_json_dict()
        payload.update(
            {
                "workflow_run_id": stored.workflow_run_id,
                "preview_id": stored.preview_id,
            }
        )
        return payload

    async def execute_handoff(
        self,
        *,
        workflow_run_id: str,
        preview_id: str,
        target_provider: str,
        cwd: str,
        prompt: str,
    ) -> dict[str, Any]:
        preview = self._store.get_preview(preview_id)
        if preview.workflow_run_id != workflow_run_id:
            raise ValueError(
                f"handoff preview {preview_id} does not belong to workflow run "
                f"{workflow_run_id}"
            )
        target = self._registry.get(target_provider)
        capabilities = target.capabilities()
        if not capabilities.can_start_session:
            raise ValueError(f"{target_provider} cannot start sessions")
        result = await target.start_session(cwd, prompt)
        step = self._store.record_execution(
            workflow_run_id=workflow_run_id,
            preview_id=preview_id,
            target_provider=target_provider,
            target_thread_id=result.native_session_id,
            target_agent_run_id=result.agent_run_id,
            submitted_prompt=prompt,
            status="running" if result.turn_running else result.status,
        )
        return {
            "workflow_run_id": workflow_run_id,
            "step_id": step.step_id,
            "target_provider": target_provider,
            "target_thread_id": result.native_session_id,
            "target_url": _target_url(
                result.agent_run_id,
                target_provider,
                result.native_session_id,
            ),
            "status": step.status,
        }


def _handoff_intent(raw: str) -> HandoffIntent:
    try:
        return HandoffIntent(raw or HandoffIntent.AUTO.value)
    except ValueError:
        return HandoffIntent.AUTO


def _target_url(agent_run_id: int, provider: str, native_session_id: str) -> str:
    return (
        f"/workers/{agent_run_id}/live?native_provider={quote(provider, safe='')}"
        f"&native_thread_id={quote(native_session_id, safe='')}"
    )


def _session_turns(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw_turns = session.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        thread = session.get("thread")
        raw_turns = thread.get("turns", []) if isinstance(thread, dict) else []
    turns: list[dict[str, Any]] = []
    for turn in raw_turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    turns.append(_turn_from_item(item))
            continue
        turns.append(turn)
    return turns


def _turn_text(turn: dict[str, Any]) -> str:
    content = turn.get("content", turn.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _turn_from_item(item: dict[str, Any]) -> dict[str, Any]:
    item_type = str(item.get("type") or "")
    if item_type == "userMessage":
        role = "user"
    elif item_type == "agentMessage":
        role = "assistant"
    else:
        role = str(item.get("role") or "unknown")
    return {"role": role, "content": item.get("content", item.get("text", ""))}


def _newest_user_text(turns: list[dict[str, Any]]) -> str:
    for turn in reversed(turns):
        if str(turn.get("role") or "").lower() == "user":
            text = _turn_text(turn).strip()
            if text:
                return text
    return ""


def _session_summary(turns: list[dict[str, Any]]) -> str:
    texts = []
    for turn in turns[-8:]:
        text = _turn_text(turn).strip()
        if not text:
            continue
        role = str(turn.get("role") or "unknown").lower()
        texts.append(f"{role}: {text}")
    return "\n".join(texts)


def _artifacts_from_text(text: str) -> list[HandoffArtifact]:
    artifacts = []
    seen = set()
    for match in _ARTIFACT_PATH_RE.finditer(text):
        path = match.group(1).rstrip(".)]")
        if path in seen:
            continue
        seen.add(path)
        artifacts.append(HandoffArtifact(kind=_artifact_kind(path), path=path))
    return artifacts


def _artifact_kind(path: str) -> str:
    normalized = path.lower()
    if "docs/superpowers/specs/" in normalized:
        return "spec"
    if "docs/superpowers/plans/" in normalized:
        return "plan"
    if "docs/bugs/" in normalized:
        return "bug_report"
    return "unknown"
