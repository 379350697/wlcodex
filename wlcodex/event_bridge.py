"""Background event pump — consumes backend.events(), updates internal state,
sends Telegram approval buttons, and records runtime events.

Status/log data is NEVER fed back into Codex context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from wlcodex.codex_backend import BackendEvent
from wlcodex.codex_runtime_source import CodexRuntimeSource
from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector
from wlcodex.models import TaskStatus
from wlcodex.status import render_approval_card
from wlcodex.task_service import TaskService, drain_workspace
from wlcodex.telegram_digest import (
    render_auto_diagnose_digest,
    render_missing_diagnose_digest,
)
from wlcodex.auto_digest_llm import (
    DeepSeekDigestUsage,
    render_auto_draft_digest_with_llm,
)

logger = logging.getLogger(__name__)


def _changed_files_from_inspection_body(body: str) -> list[str]:
    changed_files: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("相关文件：", "暂无文件记录。"):
            continue
        if stripped.startswith("[") and "]" in stripped:
            stripped = stripped.split("]", 1)[1].strip()
        if stripped:
            changed_files.append(stripped[:200])
    return changed_files


def _diff_body_has_evidence(body: str) -> bool:
    stripped = body.strip()
    return bool(
        stripped
        and stripped not in ("暂无 diff 信息。", "工作区没有未提交变更。")
    )


# ---------------------------------------------------------------------------
# Deterministic diagnose JSON collection
# ---------------------------------------------------------------------------

_DIAGNOSE_SCRIPT_RELATIVE_PATH = "scripts/diagnose_live.py"
_DIAGNOSE_TIMEOUT_SECONDS = 30


def _run_diagnose_live(workspace_path: str, runtime_dir: str = "") -> str:
    """Run diagnose_live.py in the given workspace and return its JSON stdout.

    Returns the raw JSON string on success, or "" on any failure.
    This is the deterministic path — no model involvement.
    """
    script_path = Path(workspace_path) / _DIAGNOSE_SCRIPT_RELATIVE_PATH
    if not script_path.exists():
        logger.warning("diagnose_live.py not found at %s", script_path)
        return ""

    cmd = [sys_executable(), str(script_path), "--json"]
    if runtime_dir:
        cmd.extend(["--runtime-dir", runtime_dir])

    env = os.environ.copy()
    # Never inherit a potentially broken PYTHONPATH from the wlcodex venv
    env.pop("PYTHONPATH", None)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DIAGNOSE_TIMEOUT_SECONDS,
            cwd=str(workspace_path),
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("diagnose_live.py timed out after %ds", _DIAGNOSE_TIMEOUT_SECONDS)
        return ""
    except Exception as exc:
        logger.warning("diagnose_live.py subprocess failed: %s", exc)
        return ""

    if result.returncode != 0:
        logger.warning("diagnose_live.py exit %d: %s", result.returncode, result.stderr[:500])
        return ""

    stdout = result.stdout.strip()
    if not stdout:
        return ""

    # Basic validation: must be parseable JSON with schema_version
    try:
        import json as _json
        parsed = _json.loads(stdout)
        if not isinstance(parsed, dict) or "schema_version" not in parsed:
            logger.warning("diagnose_live.py output missing schema_version")
            return ""
    except Exception:
        logger.warning("diagnose_live.py output is not valid JSON")
        return ""

    return stdout


def sys_executable() -> str:
    """Return the python executable path, preferring the venv python3."""
    venv_python = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".venv", "bin", "python3",
    )
    if os.path.exists(venv_python):
        return venv_python
    return os.environ.get("PYTHON_EXECUTABLE", "python3")


def _extract_diagnose_json(text: str) -> str:
    """Try to extract a diagnose JSON block from Codex model output.

    Looks for ```json ... ``` blocks containing schema_version or diagnose
    markers. Falls back to empty string.
    """
    import json as _json
    import re as _re

    if not text:
        return ""

    # Find all json code blocks
    for match in _re.finditer(r"```(?:json)?\s*\n(.*?)\n```", text, _re.DOTALL):
        block = match.group(1).strip()
        try:
            parsed = _json.loads(block)
        except (_json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "schema_version" in parsed:
            # Valid diagnose JSON — return the raw string
            return block
    return ""


def _auto_run_expects_diagnose_json(auto_run: Any, agent_runs: list[Any]) -> bool:
    has_diagnose_role = any(
        getattr(agent_run, "role", "") in ("auto_analysis", "auto_final_plan")
        for agent_run in agent_runs
    )
    if not has_diagnose_role:
        return False

    goal = str(getattr(auto_run, "goal", "") or "").lower()
    analysis = str(getattr(auto_run, "last_codex_analysis", "") or "").lower()
    if any(term in goal for term in (
        "只改文档",
        "文档-only",
        "docs-only",
        "documentation-only",
        "doc-only",
    )):
        return False
    combined = " ".join((goal, analysis))

    domain_terms = (
        "lightfee",
        "交易",
        "交易所",
        "实盘",
        "仓位",
        "持仓",
        "开仓",
        "平仓",
        "挂单",
        "订单",
        "binance",
        "bybit",
        "gate",
        "reduce-only",
    )
    diagnose_terms = (
        "diagnose_live",
        "production diagnosis",
        "line diagnosis",
        "线上排障",
        "生产诊断",
        "诊断",
        "排障",
        "核验",
        "检查",
        "verify",
    )
    return (
        any(term in combined for term in domain_terms)
        and any(term in combined for term in diagnose_terms)
    )


def _brief_diagnose_supplement(diagnose_digest: str) -> str:
    """Short supplement for final-plan stages — diagnose evidence reference only.

    Must be short (1-2 lines) and must NOT reproduce the full diagnose digest.

    diagnose_digest is accepted but intentionally unused — the current policy
    returns a fixed disclaimer to avoid diagnostic content leaking into the
    plan display. The parameter is retained for future callers that may want
    to extract a controlled sub-field (e.g. conclusion status only).
    """
    if not diagnose_digest:
        return ""
    return "诊断证据：已采集结构化诊断；详细诊断仅作证据参考，不替代最终方案。"


def _extract_list_after_heading(lines: list[str], heading: str) -> list[str]:
    items: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        normalized = stripped.lower().replace("-", "_")
        if normalized.startswith(f"{heading}:"):
            in_section = True
            remainder = stripped.split(":", 1)[1].strip()
            if remainder:
                items.append(remainder)
            continue
        if in_section and stripped.endswith(":"):
            break
        if in_section and stripped:
            items.append(stripped.lstrip("-* ").strip())
    return [item for item in items if item]


def _audit_evidence_refs_from_json(report: dict[str, object]) -> list[str]:
    refs = _audit_evidence_ref_items(report.get("test_evidence_refs"))
    refs.extend(_audit_evidence_ref_items(report.get("evidence_refs")))
    for key in ("passed_checks", "checks", "verification", "verification_results"):
        refs.extend(_audit_evidence_refs_from_value(report.get(key)))
    return list(dict.fromkeys(refs))


def _audit_evidence_refs_from_value(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, list):
        for item in value:
            refs.extend(_audit_evidence_refs_from_value(item))
        return refs
    if isinstance(value, dict):
        for key in ("evidence", "evidence_ref", "evidence_refs", "test_evidence_refs"):
            refs.extend(_audit_evidence_ref_items(value.get(key)))
        for nested_key, nested_value in value.items():
            if nested_key in {"evidence", "evidence_ref", "evidence_refs", "test_evidence_refs"}:
                continue
            if isinstance(nested_value, (list, dict)):
                refs.extend(_audit_evidence_refs_from_value(nested_value))
        return refs
    return []


def _audit_evidence_ref_items(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return _normalize_audit_list(value)


def _normalize_audit_decision_value(value: object) -> str:
    raw = str(value or "").strip().lower()
    normalized = raw.replace("-", "_").replace(" ", "_")
    if normalized.startswith(("pass", "passed", "approve", "approved", "success")):
        return "pass"
    if normalized.startswith((
        "block",
        "blocked",
        "fail",
        "failed",
        "retry",
        "repair",
        "needs_repair",
        "need_repair",
    )):
        return "block"
    if normalized.startswith(("needs_user", "need_user", "ask_user")):
        return "needs_user"
    return ""


def _normalize_audit_risk_value(value: object, *, decision: str) -> str:
    raw = str(value or "").strip().lower()
    normalized = raw.replace("-", "_").replace(" ", "_")
    for risk in ("low", "medium", "high", "critical"):
        if normalized.startswith(risk):
            return risk
    return "low" if decision == "pass" else "medium"


def _json_string_literal(value: str) -> str:
    try:
        parsed = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value
    return str(parsed)


def _audit_evidence_refs_from_text(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(
        r'"(?:evidence|evidence_ref|test_evidence_refs)"\s*:\s*"((?:\\.|[^"\\])*)"',
        text,
        re.IGNORECASE,
    ):
        value = _json_string_literal(match.group(1)).strip()
        if value:
            refs.append(value)
    for match in re.finditer(
        r'"(?:evidence|evidence_refs|test_evidence_refs)"\s*:\s*\[(.*?)\]',
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        for item in re.finditer(r'"((?:\\.|[^"\\])*)"', match.group(1)):
            value = _json_string_literal(item.group(1)).strip()
            if value:
                refs.append(value)
    return list(dict.fromkeys(refs))


def _parse_audit_report_payload(text: str) -> dict[str, object]:
    json_report = _extract_audit_report_json(text)
    if json_report is not None:
        return json_report
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lowered = text.lower()
    decision = ""
    decision_field = re.search(
        r'"(?:decision|verdict|status|conclusion)"\s*:\s*"([^"]+)"',
        text,
        re.IGNORECASE,
    )
    if decision_field:
        decision = _normalize_audit_decision_value(decision_field.group(1))
    if not decision:
        legacy_decision = re.search(
            r"\bdecision\s*:\s*([a-zA-Z0-9_ -]+)",
            text,
            re.IGNORECASE,
        )
        if legacy_decision:
            decision = _normalize_audit_decision_value(legacy_decision.group(1))
    if not decision and "验收通过" in text and "不能验收通过" not in text:
        decision = "pass"
    if not decision and any(token in lowered for token in ("decision: block", "decision:block")):
        decision = "block"
    if not decision and any(token in lowered for token in ("decision: needs_user", "decision:needs_user")):
        decision = "needs_user"
    summary = text[:2000]
    for line in lines:
        if line.lower().startswith("summary:"):
            summary = line.split(":", 1)[1].strip() or summary
            break
    json_summary = re.search(r'"summary"\s*:\s*"([^"]+)"', text)
    if json_summary:
        summary = json_summary.group(1).strip() or summary
    risk_match = re.search(r'"(?:risk_level|risk)"\s*:\s*"([^"]+)"', text, re.IGNORECASE)
    risk_level = _normalize_audit_risk_value(
        risk_match.group(1) if risk_match else "",
        decision=decision,
    )
    evidence_refs = _extract_list_after_heading(lines, "test_evidence_refs")
    evidence_refs.extend(_audit_evidence_refs_from_text(text))
    return {
        "summary": summary,
        "decision": decision,
        "findings": _extract_list_after_heading(lines, "findings")
        or ["No findings reported."],
        "missing_evidence": _extract_list_after_heading(lines, "missing_evidence")
        or ["None"],
        "risk_level": risk_level,
        "recommended_next_action": "close" if decision == "pass" else "repair",
        "test_evidence_refs": list(dict.fromkeys(evidence_refs)),
        "raw_result": text,
    }


def _extract_audit_report_json(text: str) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        has_wrapper = "audit_report" in parsed
        report = parsed.get("audit_report", parsed)
        if not isinstance(report, dict):
            continue
        if not has_wrapper and not any(
            key in report for key in ("decision", "verdict", "status", "conclusion")
        ):
            continue
        decision = str(
            report.get("decision")
            or report.get("verdict")
            or report.get("status")
            or report.get("conclusion")
            or ""
        ).strip().lower()
        normalized_decision = _normalize_audit_decision_value(decision)
        if not normalized_decision:
            continue
        risk_level = _normalize_audit_risk_value(
            report.get("risk_level") or report.get("risk") or "",
            decision=normalized_decision,
        )
        findings = (
            _normalize_audit_list(report.get("findings"))
            or _normalize_audit_list(report.get("issues"))
            or _normalize_audit_list(report.get("blockers"))
            or ["No findings reported."]
        )
        missing_evidence = (
            _normalize_audit_list(report.get("missing_evidence"))
            or _normalize_audit_list(report.get("missing"))
            or ["None"]
        )
        test_evidence_refs = _audit_evidence_refs_from_json(report)
        recommended = str(
            report.get("recommended_next_action")
            or report.get("next_action")
            or report.get("action")
            or ""
        ).strip()
        if not recommended:
            recommended = "close" if normalized_decision == "pass" else "repair"
        return {
            "summary": str(report.get("summary") or text[:2000]),
            "decision": normalized_decision,
            "findings": findings,
            "missing_evidence": missing_evidence,
            "risk_level": risk_level,
            "recommended_next_action": recommended,
            "test_evidence_refs": test_evidence_refs,
            "raw_result": text,
        }
    return None


def _normalize_audit_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for entry in value:
        if isinstance(entry, dict):
            parts = [
                str(entry.get(key, "")).strip()
                for key in ("severity", "title", "detail", "impact", "evidence")
                if str(entry.get(key, "")).strip()
            ]
            if parts:
                items.append(" | ".join(parts))
        else:
            text = str(entry).strip()
            if text:
                items.append(text)
    return items


EXPIRY_SCAN_INTERVAL_SECONDS = 60
TASK_WATCHDOG_INTERVAL_SECONDS = 60
MAX_INTERNAL_TEST_ATTEMPTS = 3
# Callback: send_telegram(chat_id, text, buttons) -> message_id
SendTelegram = Callable[[int, str, list[list[dict[str, str]]] | None], Coroutine[Any, Any, int]]
# Callback: edit_telegram(chat_id, message_id, text, buttons=None) -> None
EditTelegram = Callable[[int, int, str, list[list[dict[str, str]]] | None], Coroutine[Any, Any, None]]


def _try_collect_diagnose_json_sync(bridge: Any, auto_run: Any) -> str:
    """Synchronous: resolve workspace, run diagnose_live.py, store and return JSON.

    Module-level so it can be passed to run_in_executor.
    """
    try:
        conv = bridge._ledger.get_conversation(auto_run.conversation_id)
        if conv is None:
            return ""
        workspace = bridge._service.get_workspace(conv.workspace_alias)
        if workspace is None:
            return ""
        workspace_path = str(workspace.path)
    except Exception:
        return ""

    json_str = _run_diagnose_live(workspace_path, "")
    if json_str:
        try:
            bridge._ledger.update_orchestration_run(
                auto_run.id, diagnose_json=json_str,
            )
        except Exception:
            pass
    return json_str


class EventBridge:
    """Consumes backend events and drives local state + Telegram notifications."""

    def __init__(
        self,
        task_service: TaskService,
        backend: object,
        ledger: Ledger,
        send_telegram: SendTelegram,
        edit_telegram: EditTelegram,
        approval_service: object,
        task_watchdog: object | None = None,
        relay_watchdog: object | None = None,
        watchdog_interval_seconds: int = TASK_WATCHDOG_INTERVAL_SECONDS,
        interaction_renderer: object | None = None,
        runtime_event_store: object | None = None,
        on_workspace_freed: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        codex_implementer_enabled: bool = False,
    ) -> None:
        self._service = task_service
        self._backend = backend
        self._ledger = ledger
        self._send_telegram = send_telegram
        self._edit_telegram = edit_telegram
        self._approval_service = approval_service
        self._task_watchdog = task_watchdog
        self._relay_watchdog = relay_watchdog
        self._watchdog_interval = watchdog_interval_seconds
        self._interaction_renderer = interaction_renderer
        self._runtime_store = runtime_event_store
        self._on_workspace_freed = on_workspace_freed
        self._codex_implementer_enabled = codex_implementer_enabled
        self._runtime_causation_by_agent_run: dict[int, int] = {}
        self._running = False

    def _auto_run_has_team(self, auto_run: Any | None) -> bool:
        if auto_run is None or not hasattr(
            self._ledger,
            "get_team_run_for_orchestration",
        ):
            return False
        return self._ledger.get_team_run_for_orchestration(auto_run.id) is not None

    async def run(self) -> None:
        """Run the event loop until cancelled.

        Processes backend events, periodically gives the approval service a
        chance to handle stale approval bookkeeping without ending live holds,
        and runs the task liveness watchdog when configured.
        """
        self._running = True
        await self._drain_available_workspaces_on_startup()
        expiry_task = asyncio.create_task(
            self._expiry_loop(), name="approval-expiry-scan"
        )
        watchdog_task: asyncio.Task[None] | None = None
        if self._task_watchdog is not None or self._relay_watchdog is not None:
            watchdog_task = asyncio.create_task(
                self._task_watchdog_loop(), name="task-liveness-watchdog"
            )
        try:
            async for event in self._backend.events():
                await self.process_event(event)
        except asyncio.CancelledError:
            pass
        finally:
            expiry_task.cancel()
            try:
                await expiry_task
            except asyncio.CancelledError:
                pass
            if watchdog_task is not None:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            self._running = False

    async def _drain_available_workspaces_on_startup(self) -> None:
        """Kick queued consumers once after process startup.

        A workspace can already be free when the event pump restarts, so no
        terminal backend event may arrive to trigger the normal drain path.
        Running the same per-workspace sequence here keeps both waiting-slot
        tasks and durable ``run.queued`` leases live without globally
        consuming another workspace's queue.
        """

        for workspace_alias in list(self._service._workspaces):
            try:
                await drain_workspace(self._service, self._backend, workspace_alias)
                if self._on_workspace_freed is not None:
                    await self._on_workspace_freed(workspace_alias)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Startup workspace drain failed for %s", workspace_alias
                )

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(EXPIRY_SCAN_INTERVAL_SECONDS)
            await self._expire_stale()

    async def _task_watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self._watchdog_interval)
            try:
                changed = 0
                if self._task_watchdog is not None:
                    changed += self._task_watchdog.scan_once()
                if self._relay_watchdog is not None:
                    changed += await self._relay_watchdog.scan_once()
                if changed > 0:
                    for ws_alias in list(self._service._workspaces):
                        await drain_workspace(self._service, self._backend, ws_alias)
                        if self._on_workspace_freed is not None:
                            await self._on_workspace_freed(ws_alias)
            except Exception:
                logger.exception("Task watchdog scan failed")

    async def _expire_stale(self) -> None:
        """Run non-terminal stale approval maintenance."""
        try:
            await self._approval_service.expire_stale_approvals(
                self._ledger, self._backend
            )
        except Exception:
            logger.exception("Stale approval expiry scan failed")

    async def process_event(self, event: BackendEvent) -> None:
        """Process a single backend event."""
        thread_id = str(event.payload.get("threadId", ""))
        task_before = self._service._find_by_thread(thread_id) if thread_id else None

        try:
            self._service.apply_backend_event(event)
        except Exception:
            logger.exception("Failed to apply backend event: %s", event.event_type)
            return

        task = self._task_for_runtime_event(event, thread_id, task_before)
        self._append_runtime_events(event, task)

        # Forward agent message deltas to the interaction renderer.
        if event.event_type == "agent_message_delta":
            await self._forward_agent_delta(event)

        # Handle approval — send Telegram buttons
        if event.event_type == "approval_requested":
            await self._on_approval_requested(event)

        # Trigger queue drain when a task reaches terminal state
        if event.event_type in ("turn_completed", "thread_status_changed") and task_before:
            task_after = self._service._find_by_thread(thread_id)
            if task_after and task_after.status in (
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.ABORTED,
            ):
                self._sync_direct_agent_run_status(task_after)
                # Check for staged-auto workflow transitions
                advanced_stage = self._advance_staged_auto_on_completion(task_after)
                if advanced_stage:
                    await self._send_auto_stage_buttons(task_after, advanced_stage)
                else:
                    await self._forward_terminal_event(task_after)
                await drain_workspace(
                    self._service, self._backend, task_after.workspace_alias
                )
                if self._on_workspace_freed is not None:
                    await self._on_workspace_freed(task_after.workspace_alias)

    def _task_for_runtime_event(
        self,
        event: BackendEvent,
        thread_id: str,
        task_before: object | None,
    ) -> object | None:
        if thread_id:
            return self._service._find_by_thread(thread_id)
        codex_request_id = str(event.payload.get("codexRequestId", ""))
        if codex_request_id:
            approval = self._ledger.get_approval_by_codex_id(codex_request_id)
            if approval is not None:
                try:
                    return self._ledger.get_task(approval.task_id)
                except KeyError:
                    return None
        return task_before

    def _append_runtime_events(self, event: BackendEvent, task: object | None) -> None:
        """Append Codex backend events to runtime_events for non-orchestrated tasks."""
        if self._runtime_store is None or task is None:
            return
        task_id = getattr(task, "id", None)
        if task_id is None:
            return
        if self._service.is_orchestration_managed_task(int(task_id)):
            return
        context = self._runtime_context_for_task(int(task_id))
        if context is None:
            return
        source = CodexRuntimeSource(
            correlation_id=context["correlation_id"],
            agent_run_id=context["agent_run_id"],
            conversation_id=context["conversation_id"],
            orchestration_run_id=context["orchestration_run_id"],
            task_id=int(task_id),
        )
        last_id = self._runtime_causation_by_agent_run.get(context["agent_run_id"])
        for runtime_event in source.map_event(event, causation_id=last_id):
            stored = self._runtime_store.append(runtime_event)
            self._runtime_causation_by_agent_run[context["agent_run_id"]] = stored.id

    def _runtime_context_for_task(self, task_id: int) -> dict[str, int | str] | None:
        row = self._ledger._conn.execute(
            """
            SELECT c.id AS conversation_id,
                   ar.id AS agent_run_id,
                   ar.role AS role,
                   o.id AS orchestration_run_id
            FROM conversation_sessions AS c
            LEFT JOIN agent_runs AS ar
              ON ar.conversation_id = c.id
             AND ar.agent = 'codex'
             AND (ar.hidden_task_id = ? OR ar.hidden_task_id IS NULL)
            LEFT JOIN orchestration_runs AS o
              ON o.conversation_id = c.id
             AND o.status = 'running'
            WHERE c.active_codex_task_id = ?
            ORDER BY ar.id DESC, o.id DESC
            LIMIT 1
            """,
            (task_id, task_id),
        ).fetchone()
        if row is None or row["agent_run_id"] is None:
            return None
        correlation_id = f"codex-task-{task_id}"
        last_event = self._runtime_store._conn.execute(
            """
            SELECT correlation_id FROM runtime_events
            WHERE task_id = ?
              AND correlation_id NOT LIKE 'telegram-%'
              AND correlation_id NOT LIKE 'watchdog-%'
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if last_event is not None:
            correlation_id = str(last_event["correlation_id"])
        return {
            "conversation_id": int(row["conversation_id"]),
            "agent_run_id": int(row["agent_run_id"]),
            "orchestration_run_id": (
                int(row["orchestration_run_id"])
                if row["orchestration_run_id"] is not None else None
            ),
            "correlation_id": correlation_id,
        }

    async def _on_approval_requested(self, event: BackendEvent) -> None:
        payload = event.payload
        thread_id = str(payload.get("threadId", ""))
        codex_request_id = str(payload.get("codexRequestId", ""))
        kind = str(payload.get("kind", "command"))

        task = self._service._find_by_thread(thread_id)
        if task is None:
            logger.warning("approval_requested for unknown thread: %s", thread_id)
            return

        if task.telegram_chat_id is None:
            logger.warning("task #%d has no telegram_chat_id", task.id)
            return

        # Find the approval row just created by apply_backend_event
        approval = self._ledger.get_approval_by_codex_id(
            codex_request_id, task_id=task.id
        )
        if approval is None:
            logger.warning("No approval row for codex_request_id: %s", codex_request_id)
            return

        from wlcodex.approval import encode_approval_callback

        allow_session = getattr(self._approval_service, "_allow_session_approval", True)

        if allow_session:
            buttons = [[
                {"text": "批准一次", "callback_data": encode_approval_callback(approval.id, "approve_once")},
                {"text": "本会话批准", "callback_data": encode_approval_callback(approval.id, "approve_session")},
            ], [
                {"text": "拒绝", "callback_data": encode_approval_callback(approval.id, "deny")},
                {"text": "取消", "callback_data": encode_approval_callback(approval.id, "cancel")},
            ]]
        else:
            buttons = [[
                {"text": "批准", "callback_data": encode_approval_callback(approval.id, "approve_once")},
                {"text": "拒绝", "callback_data": encode_approval_callback(approval.id, "deny")},
            ], [
                {"text": "取消", "callback_data": encode_approval_callback(approval.id, "cancel")},
            ]]

        card = render_approval_card(task.id, approval.id, kind, approval.summary)
        try:
            msg_id = await self._send_telegram(task.telegram_chat_id, card, buttons)
            # Store the telegram message id on the approval row (via a direct update)
            self._ledger._conn.execute(
                "UPDATE approval_requests SET telegram_message_id = ? WHERE id = ?",
                (msg_id, approval.id),
            )
            self._ledger._conn.commit()
        except Exception:
            logger.exception("Failed to send approval Telegram message")

    async def _forward_agent_delta(self, event: BackendEvent) -> None:
        if self._interaction_renderer is None:
            return
        thread_id = str(event.payload.get("threadId", ""))
        task = self._service._find_by_thread(thread_id)
        if task is None or task.telegram_chat_id is None:
            return
        if (
            self._service.is_orchestration_managed_task(task.id)
            or self._is_staged_auto_agent_task(task.id)
        ):
            return
        delta = str(event.payload.get("delta", ""))
        if not delta:
            return
        from wlcodex.interaction.events import InteractionEvent

        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type="text_delta",
                chat_id=task.telegram_chat_id,
                task_id=task.id,
                thread_id=thread_id,
                text=delta,
            )
        )

    async def _forward_terminal_event(self, task) -> None:
        if self._interaction_renderer is None:
            return
        if task.telegram_chat_id is None:
            return
        if self._is_staged_auto_agent_task(int(task.id)):
            return
        from wlcodex.interaction.events import InteractionEvent

        event_type = "run_completed" if task.status == TaskStatus.DONE else "run_failed"
        await self._interaction_renderer.handle(
            InteractionEvent(
                event_type=event_type,
                chat_id=task.telegram_chat_id,
                task_id=task.id,
                thread_id=task.codex_thread_id or "",
                text=task.last_error or "",
                metadata={"has_diff": bool(task.changed_file_count)},
            )
        )

    def _task_agent_message_summary(self, task: object) -> str:
        """Return the assembled agent message text for a completed direct task."""
        task_id = getattr(task, "id", None)
        if task_id is None:
            return str(getattr(task, "last_summary", "") or "")

        chunks: list[str] = []
        for event in self._ledger.list_events(int(task_id), limit=1000):
            if event.event_type != "agent_message_delta":
                continue
            delta = str(event.payload.get("delta", "") or "")
            if delta:
                chunks.append(delta)

        assembled = "".join(chunks).strip()
        if assembled:
            return assembled
        return str(getattr(task, "last_summary", "") or "").strip()

    def _is_staged_auto_agent_task(self, task_id: int) -> bool:
        row = self._ledger._conn.execute(
            """
            SELECT role FROM agent_runs
            WHERE hidden_task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        return str(row["role"] or "").startswith("auto_")

    def _sync_direct_agent_run_status(self, task: object) -> None:
        task_id = getattr(task, "id", None)
        if task_id is None or self._service.is_orchestration_managed_task(int(task_id)):
            return
        status_by_task = {
            TaskStatus.DONE: "done",
            TaskStatus.FAILED: "failed",
            TaskStatus.ABORTED: "aborted",
        }
        agent_status = status_by_task.get(getattr(task, "status", None))
        if agent_status is None:
            return

        rows = self._ledger._conn.execute(
            """
            SELECT id, status, role FROM agent_runs
            WHERE hidden_task_id = ?
            ORDER BY id ASC
            """,
            (int(task_id),),
        ).fetchall()
        for row in rows:
            if row["status"] in {"done", "failed", "aborted"}:
                continue
            summary = (
                getattr(task, "last_error", "")
                if agent_status in {"failed", "aborted"}
                else self._task_agent_message_summary(task)
            )
            self._ledger.update_agent_run_status(
                int(row["id"]),
                agent_status,
                completion_summary=str(summary),
            )
            self._append_direct_agent_terminal_event(
                task,
                agent_run_id=int(row["id"]),
                agent_status=agent_status,
                role=str(row["role"] or "implementation"),
                summary=str(summary),
            )

    def _append_direct_agent_terminal_event(
        self,
        task: object,
        *,
        agent_run_id: int,
        agent_status: str,
        role: str,
        summary: str,
    ) -> None:
        if self._runtime_store is None:
            return
        task_id = int(getattr(task, "id"))
        context = self._runtime_context_for_task(task_id)
        if context is None:
            return
        from wlcodex.runtime_events import (
            SCHEMA_VERSION,
            AggregateType,
            EventSource,
            EventType,
            RuntimeEvent,
            Visibility,
            now_iso,
        )

        event_type = (
            EventType.AGENT_RUN_COMPLETED
            if agent_status == "done"
            else EventType.AGENT_RUN_FAILED
        )
        last_id = self._runtime_causation_by_agent_run.get(agent_run_id)
        if last_id is None:
            row = self._runtime_store._conn.execute(
                """
                SELECT id FROM runtime_events
                WHERE agent_run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (agent_run_id,),
            ).fetchone()
            if row is not None:
                last_id = int(row["id"])
        stored = self._runtime_store.append(
            RuntimeEvent(
                schema_version=SCHEMA_VERSION,
                event_type=event_type,
                aggregate_type=AggregateType.AGENT_RUN,
                aggregate_id=str(agent_run_id),
                correlation_id=str(context["correlation_id"]),
                causation_id=last_id,
                source=EventSource.CODEX,
                actor="codex",
                visibility=Visibility.OPERATOR,
                payload={
                    "agent": "codex",
                    "role": role,
                    "summary": summary,
                    "completion_summary": summary,
                },
                occurred_at=now_iso(),
                conversation_id=int(context["conversation_id"]),
                orchestration_run_id=(
                    int(context["orchestration_run_id"])
                    if context["orchestration_run_id"] is not None else None
                ),
                agent_run_id=agent_run_id,
                task_id=task_id,
            )
        )
        self._runtime_causation_by_agent_run[agent_run_id] = stored.id

    def _advance_staged_auto_on_completion(self, task: object) -> str | None:
        """When a direct agent run completes, check if it belongs to a staged-auto
        workflow and advance the orchestration run to the next needs_user stage.

        Returns the new current_step if a transition occurred, None otherwise.

        This implements the stage transition logic:
        - auto_analysis/auto_final_plan completion → draft_ready (needs_user)
        - auto_verification pass → completed (needs_user)
        - auto_verification fail → retry_ready (needs_user)
        - auto_implementation/auto_repair completion → tested implementation or retry
        - auto_codex_takeover completion → completed (needs_user)
        """
        from wlcodex.auto_workflow import (
            AUTO_COLLECTING_CONTEXT,
            AUTO_DRAFT_READY,
            AUTO_CLAUDE_DONE,
            AUTO_RETRY_READY,
            AUTO_COMPLETED,
            ROLE_AUTO_ANALYSIS,
            ROLE_AUTO_CONTEXT_SUPPLEMENT,
        )

        task_id = int(getattr(task, "id"))
        rows = self._ledger._conn.execute(
            """
            SELECT id, role, status, completion_summary FROM agent_runs
            WHERE hidden_task_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchall()
        if not rows:
            return None

        agent_run_id = int(rows[0]["id"])
        agent_role = str(rows[0]["role"] or "")
        agent_status = str(rows[0]["status"] or "")
        completion_summary = str(rows[0]["completion_summary"] or "")

        # Find the conversation for this task
        conv_row = self._ledger._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE active_codex_task_id = ? OR active_claude_run_id = (
                SELECT id FROM agent_runs WHERE hidden_task_id = ? LIMIT 1
            )
            ORDER BY id DESC LIMIT 1
            """,
            (task_id, task_id),
        ).fetchone()
        if conv_row is None:
            return None
        conversation_id = int(conv_row["id"])

        # Find the latest active auto run
        auto_run = self._ledger.get_latest_active_auto_run(conversation_id)
        if auto_run is None:
            return None

        new_step: str | None = None

        # Advance based on agent role and completion
        if agent_role in (ROLE_AUTO_ANALYSIS, ROLE_AUTO_CONTEXT_SUPPLEMENT) and agent_status == "done":
            new_step = AUTO_COLLECTING_CONTEXT
            diagnose_json = ""
            diagnose_probe = SimpleNamespace(
                goal=getattr(auto_run, "goal", ""),
                last_codex_analysis=completion_summary,
            )
            if _auto_run_expects_diagnose_json(
                diagnose_probe,
                [SimpleNamespace(role=agent_role)],
            ):
                # Primary: deterministic subprocess. Fallback: regex from model output.
                diagnose_json = _try_collect_diagnose_json_sync(self, auto_run)
                if not diagnose_json:
                    diagnose_json = _extract_diagnose_json(completion_summary)
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="needs_user",
                current_step=new_step,
                last_codex_analysis=completion_summary if completion_summary else "",
                diagnose_json=diagnose_json,
            )

        elif agent_role == "auto_final_plan" and agent_status == "done":
            new_step = AUTO_DRAFT_READY
            diagnose_json = ""
            diagnose_probe = SimpleNamespace(
                goal=getattr(auto_run, "goal", ""),
                last_codex_analysis=completion_summary,
            )
            if _auto_run_expects_diagnose_json(
                diagnose_probe,
                [SimpleNamespace(role=agent_role)],
            ):
                diagnose_json = _try_collect_diagnose_json_sync(self, auto_run)
                if not diagnose_json:
                    diagnose_json = _extract_diagnose_json(completion_summary)
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="needs_user",
                current_step=new_step,
                last_codex_analysis=completion_summary if completion_summary else "",
                diagnose_json=diagnose_json,
            )
            self._record_architecture_plan_artifact(
                auto_run,
                agent_run_id=agent_run_id,
                completion_summary=completion_summary,
            )
            self._mark_architect_team_job_done(auto_run, agent_run_id=agent_run_id)

        elif agent_role in ("auto_implementation", "auto_repair") and agent_status == "done":
            test_gate_passed = self._record_implementation_report_artifact(
                auto_run,
                agent_run_id=agent_run_id,
                task_id=task_id,
                completion_summary=completion_summary,
                source_agent="codex",
            )
            self._mark_team_agent_job_done(
                auto_run,
                role="implementer",
                agent_run_id=agent_run_id,
            )
            if test_gate_passed is False:
                attempt_count = self._tester_attempt_count_for_auto_run(auto_run)
                retry_text = self._internal_test_failure_text(attempt_count)
                new_step = AUTO_RETRY_READY
                self._ledger.update_orchestration_run(
                    auto_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_claude_summary=completion_summary if completion_summary else "",
                    last_verification_result=retry_text,
                )
            else:
                new_step = AUTO_CLAUDE_DONE
                self._ledger.update_orchestration_run(
                    auto_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_claude_summary=completion_summary if completion_summary else "",
                    last_verification_result="开发完成，测试通过。",
                )

        elif agent_role in ("auto_implementation", "auto_repair") and agent_status == "failed":
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="failed",
                last_claude_summary=completion_summary if completion_summary else "",
            )
            self._mark_team_agent_job_failed(
                auto_run,
                role="implementer",
                agent_run_id=agent_run_id,
            )

        elif agent_role == "auto_verification" and agent_status == "done":
            # Codex verification completed → check pass/fail
            verification_passed = (
                _parse_audit_report_payload(completion_summary).get("decision")
                == "pass"
            )
            gate_d_passed = self._record_audit_report_artifact(
                auto_run,
                agent_run_id=agent_run_id,
                completion_summary=completion_summary,
            )
            team_run = self._team_run_for_auto_run(auto_run)
            completion_allowed = verification_passed and (
                gate_d_passed is True
                or (gate_d_passed is None and team_run is None)
            )
            if completion_allowed:
                new_step = AUTO_COMPLETED
                self._ledger.update_orchestration_run(
                    auto_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_verification_result=completion_summary if completion_summary else "",
                )
            else:
                new_step = AUTO_RETRY_READY
                self._ledger.update_orchestration_run(
                    auto_run.id,
                    status="needs_user",
                    current_step=new_step,
                    last_verification_result=completion_summary if completion_summary else "",
                )
            self._mark_team_agent_job_done(
                auto_run,
                role="auditor",
                agent_run_id=agent_run_id,
            )
            if completion_allowed:
                self._complete_team_run(
                    auto_run,
                    agent_run_id=agent_run_id,
                    task_id=task_id,
                )

        elif agent_role == "auto_verification" and agent_status == "failed":
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="failed",
                last_verification_result=completion_summary if completion_summary else "",
            )
            self._mark_team_agent_job_failed(
                auto_run,
                role="auditor",
                agent_run_id=agent_run_id,
            )

        elif agent_role == "auto_codex_takeover" and agent_status == "done":
            # Codex takeover completed → advance to completed
            new_step = AUTO_COMPLETED
            self._ledger.update_orchestration_run(
                auto_run.id,
                status="passed",
                current_step=new_step,
                last_codex_analysis=completion_summary if completion_summary else "",
            )

        return new_step

    def _team_run_for_auto_run(self, auto_run: object) -> object | None:
        if not hasattr(self._ledger, "get_team_run_for_orchestration"):
            return None
        return self._ledger.get_team_run_for_orchestration(auto_run.id)

    def _append_team_runtime_event(
        self,
        auto_run: object,
        event_type: str,
        *,
        team_run_id: int,
        agent_job_id: int | None = None,
        agent_run_id: int | None = None,
        task_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._runtime_store is None:
            return
        from wlcodex.runtime_events import (
            SCHEMA_VERSION,
            AggregateType,
            EventSource,
            RuntimeEvent,
            Visibility,
            now_iso,
        )

        event_payload = {"team_run_id": team_run_id}
        if agent_job_id is not None:
            event_payload["agent_job_id"] = agent_job_id
        if payload:
            event_payload.update(payload)
        self._runtime_store.append(
            RuntimeEvent(
                schema_version=SCHEMA_VERSION,
                event_type=event_type,
                aggregate_type=AggregateType.ORCHESTRATION_RUN,
                aggregate_id=str(auto_run.id),
                correlation_id=f"team-run-{team_run_id}",
                source=EventSource.ORCHESTRATOR,
                actor="adaptive_team",
                visibility=Visibility.OPERATOR,
                payload=event_payload,
                occurred_at=now_iso(),
                conversation_id=auto_run.conversation_id,
                orchestration_run_id=auto_run.id,
                agent_run_id=agent_run_id,
                task_id=task_id,
            )
        )

    def _complete_team_run(
        self,
        auto_run: object,
        *,
        agent_run_id: int | None = None,
        task_id: int | None = None,
    ) -> None:
        team_run = self._team_run_for_auto_run(auto_run)
        if team_run is None or not hasattr(self._ledger, "update_team_run_status"):
            return
        if team_run.status == "completed":
            return
        self._ledger.update_team_run_status(team_run.id, "completed")
        from wlcodex.runtime_events import EventType

        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_RUN_COMPLETED,
            team_run_id=team_run.id,
            agent_run_id=agent_run_id,
            task_id=task_id,
            payload={"status": "completed"},
        )

    def _team_instinct_status(self, instinct_id: str) -> str | None:
        if not hasattr(self._ledger, "list_team_instincts"):
            return None
        for status in ("active", "candidate"):
            for instinct in self._ledger.list_team_instincts(status=status):
                if instinct.instinct_id == instinct_id:
                    return status
        return None

    def _collect_task_evidence(self, task_id: int | None) -> tuple[list[str], str]:
        if not task_id:
            return [], ""
        workspace_path: str | None = None
        try:
            task = self._service.get_task(task_id)
            workspace_path = str(self._service.get_workspace(task.workspace_alias).path)
        except Exception:
            workspace_path = None

        inspector = TaskInspector(self._ledger, Path(""))
        changed_files: list[str] = []
        diff_summary = ""
        try:
            files_result = inspector.files(task_id)
            if files_result and files_result.body:
                changed_files = _changed_files_from_inspection_body(files_result.body)
        except Exception:
            changed_files = []
        try:
            diff_result = inspector.diff(task_id, workspace_path)
            if (
                diff_result
                and diff_result.body
                and _diff_body_has_evidence(diff_result.body)
            ):
                diff_summary = diff_result.body[:1500]
        except Exception:
            diff_summary = ""
        return changed_files[:20], diff_summary[:1500]

    def _running_team_job(
        self,
        team_run: object,
        *,
        role: str,
        agent_run_id: int | None,
    ) -> object | None:
        if not hasattr(self._ledger, "list_team_agent_jobs"):
            return None
        for job in self._ledger.list_team_agent_jobs(team_run.id):
            if job.role != role or job.status != "running":
                continue
            if agent_run_id is not None and job.agent_run_id != agent_run_id:
                continue
            if agent_run_id is None and job.agent_run_id is not None:
                continue
            return job
        return None

    def _single_running_team_job(self, team_run: object, *, role: str) -> object | None:
        if not hasattr(self._ledger, "list_team_agent_jobs"):
            return None
        matches = [
            job
            for job in self._ledger.list_team_agent_jobs(team_run.id)
            if job.role == role and job.status == "running"
        ]
        return matches[0] if len(matches) == 1 else None

    def _has_team_artifact(
        self,
        team_run_id: int,
        *,
        artifact_type: str,
        agent_job_id: int | None,
    ) -> bool:
        if not hasattr(self._ledger, "list_team_artifacts"):
            return False
        return any(
            artifact.artifact_type == artifact_type
            and artifact.agent_job_id == agent_job_id
            for artifact in self._ledger.list_team_artifacts(team_run_id)
        )

    def _record_architecture_plan_artifact(
        self,
        auto_run: object,
        *,
        agent_run_id: int | None,
        completion_summary: str,
    ) -> None:
        if not completion_summary.strip():
            return
        team_run = self._team_run_for_auto_run(auto_run)
        if team_run is None or not hasattr(self._ledger, "record_team_artifact"):
            return
        route_kind = self._team_route_kind(team_run) or "feature"
        first_role = self._team_first_role(team_run) or "architect"
        artifact_type = (
            "diagnosis_report" if route_kind == "bug" else "architecture_plan"
        )
        job = self._running_team_job(
            team_run,
            role=first_role,
            agent_run_id=agent_run_id,
        )
        if job is None and agent_run_id is not None and hasattr(
            self._ledger, "list_team_agent_jobs"
        ):
            for candidate in self._ledger.list_team_agent_jobs(team_run.id):
                if (
                    candidate.role == first_role
                    and candidate.agent_run_id == agent_run_id
                ):
                    job = candidate
                    break
        if job is None:
            job = self._single_running_team_job(team_run, role=first_role)
        agent_job_id = job.id if job is not None else None
        if self._has_team_artifact(
            team_run.id,
            artifact_type=artifact_type,
            agent_job_id=agent_job_id,
        ):
            return
        summary = completion_summary[:2000] or "Final plan completed."
        from wlcodex.team_artifacts import (
            architecture_plan_payload,
            diagnosis_report_payload,
        )

        if artifact_type == "diagnosis_report":
            payload = diagnosis_report_payload(
                summary=summary,
                symptom="Bug route requires diagnosis before implementation.",
                expected_behavior="Implementation starts only after diagnosis handoff.",
                evidence=[summary],
                root_cause=summary,
                confidence="medium",
                minimal_fix_plan=["Follow the accepted diagnosis handoff."],
                regression_tests=["Run focused verification for the reported failure."],
                risk_level="medium",
            )
        else:
            payload = architecture_plan_payload(
                summary=summary,
                risk_level="medium",
                source="auto_final_plan_completion",
            )

        artifact = self._ledger.record_team_artifact(
            team_run_id=team_run.id,
            agent_job_id=agent_job_id,
            artifact_type=artifact_type,
            summary=summary,
            payload=payload,
        )
        from wlcodex.runtime_events import EventType

        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_ARTIFACT_RECORDED,
            team_run_id=team_run.id,
            agent_job_id=agent_job_id,
            agent_run_id=agent_run_id,
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
            },
        )

    def _team_route_kind(self, team_run: object | None) -> str:
        if team_run is None or not hasattr(self._ledger, "list_team_artifacts"):
            return ""
        for artifact in self._ledger.list_team_artifacts(team_run.id):
            if artifact.artifact_type != "routing_decision":
                continue
            payload = getattr(artifact, "payload", {})
            if isinstance(payload, dict):
                route_kind = str(payload.get("route_kind", "")).strip()
                if route_kind:
                    return route_kind
        return ""

    def _team_first_role(self, team_run: object | None) -> str:
        if team_run is None or not hasattr(self._ledger, "list_team_artifacts"):
            return ""
        for artifact in self._ledger.list_team_artifacts(team_run.id):
            if artifact.artifact_type != "routing_decision":
                continue
            payload = getattr(artifact, "payload", {})
            if isinstance(payload, dict):
                first_role = str(payload.get("first_role", "")).strip()
                if first_role:
                    return first_role
        return ""

    def _record_implementation_report_artifact(
        self,
        auto_run: object,
        *,
        agent_run_id: int | None,
        task_id: int | None,
        completion_summary: str,
        source_agent: str,
    ) -> bool | None:
        team_run = self._team_run_for_auto_run(auto_run)
        if team_run is None or not hasattr(self._ledger, "record_team_artifact"):
            return None
        job = self._running_team_job(
            team_run,
            role="implementer",
            agent_run_id=agent_run_id,
        )
        if job is None or self._has_team_artifact(
            team_run.id,
            artifact_type="implementation_report",
            agent_job_id=job.id,
        ):
            return None
        summary = completion_summary or f"{source_agent} implementation completed."
        changed_files, diff_summary = self._collect_task_evidence(task_id)
        from wlcodex.team_artifacts import (
            acceptance_criteria_from_artifacts,
            command_evidence_from_task_events,
            implementation_report_payload,
            structured_implementation_evidence_from_text,
            test_command_evidence,
            test_report_payload_from_implementation,
            validate_test_report,
        )

        task_events = self._ledger.list_events(task_id, limit=1000) if task_id else []
        commands_run = command_evidence_from_task_events(task_events)
        tests_attempted = test_command_evidence(commands_run)
        structured_evidence = structured_implementation_evidence_from_text(
            completion_summary
        )
        changed_files = structured_evidence.get("changed_files") or changed_files
        diff_summary = structured_evidence.get("diff_summary") or diff_summary
        commands_run = structured_evidence.get("commands_run") or commands_run
        tests_attempted = structured_evidence.get("tests_attempted") or tests_attempted

        artifact = self._ledger.record_team_artifact(
            team_run_id=team_run.id,
            agent_job_id=job.id,
            artifact_type="implementation_report",
            summary=summary[:2000],
            payload=implementation_report_payload(
                summary=summary,
                changed_files=changed_files,
                diff_summary=diff_summary,
                source_agent=source_agent,
                commands_run=commands_run,
                tests_attempted=tests_attempted,
            ),
        )
        from wlcodex.runtime_events import EventType

        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_ARTIFACT_RECORDED,
            team_run_id=team_run.id,
            agent_job_id=job.id,
            agent_run_id=agent_run_id,
            task_id=task_id,
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
            },
        )
        tester_job = self._ensure_tester_job(
            auto_run,
            team_run=team_run,
            agent_run_id=agent_run_id,
            model_profile=getattr(job, "model_profile", None) or "codex_gpt",
        )
        test_gate_passed: bool | None = None
        if tester_job is not None and not self._has_team_artifact(
            team_run.id,
            artifact_type="test_report",
            agent_job_id=tester_job.id,
        ):
            acceptance_criteria = acceptance_criteria_from_artifacts(
                self._ledger.list_team_artifacts(team_run.id)
            )
            test_payload = test_report_payload_from_implementation(
                summary="测试工程师已收集测试结果。",
                implementation_artifact_id=artifact.id,
                commands_run=tests_attempted,
                acceptance_criteria=acceptance_criteria,
            )
            test_artifact = self._ledger.record_team_artifact(
                team_run_id=team_run.id,
                agent_job_id=tester_job.id,
                artifact_type="test_report",
                summary="测试工程师已收集测试结果。",
                payload=test_payload,
            )
            self._append_team_runtime_event(
                auto_run,
                EventType.TEAM_ARTIFACT_RECORDED,
                team_run_id=team_run.id,
                agent_job_id=tester_job.id,
                agent_run_id=agent_run_id,
                task_id=task_id,
                payload={
                    "artifact_id": test_artifact.id,
                    "artifact_type": test_artifact.artifact_type,
                },
            )
            test_gate_passed = validate_test_report(test_payload).passed
            self._mark_tester_job_for_gate(
                auto_run,
                team_run=team_run,
                tester_job=tester_job,
                agent_run_id=agent_run_id,
                passed=test_gate_passed,
            )
        return test_gate_passed

    def _ensure_tester_job(
        self,
        auto_run: object,
        *,
        team_run: object,
        agent_run_id: int | None,
        model_profile: str,
    ) -> object | None:
        if not hasattr(self._ledger, "list_team_agent_jobs") or not hasattr(
            self._ledger, "create_team_agent_job"
        ):
            return None
        for job in self._ledger.list_team_agent_jobs(team_run.id):
            if job.role == "tester" and job.agent_run_id == agent_run_id:
                return job
        tester_job = self._ledger.create_team_agent_job(
            team_run_id=team_run.id,
            role="tester",
            model_profile=model_profile,
            status="running",
            agent_run_id=agent_run_id,
        )
        if hasattr(self._ledger, "record_team_assignment"):
            self._ledger.record_team_assignment(
                team_run_id=team_run.id,
                role="tester",
                model_profile=model_profile,
                selected_by="follow_implementer",
            )
        from wlcodex.runtime_events import EventType

        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_AGENT_JOB_STARTED,
            team_run_id=team_run.id,
            agent_job_id=tester_job.id,
            agent_run_id=agent_run_id,
            payload={
                "role": "tester",
                "model_profile": model_profile,
                "follow_role": "implementer",
            },
        )
        return tester_job

    def _mark_tester_job_for_gate(
        self,
        auto_run: object,
        *,
        team_run: object,
        tester_job: object,
        agent_run_id: int | None,
        passed: bool,
    ) -> None:
        if not hasattr(self._ledger, "update_team_agent_job_status"):
            return
        status = "done" if passed else "failed"
        self._ledger.update_team_agent_job_status(tester_job.id, status)
        from wlcodex.runtime_events import EventType

        event_type = (
            EventType.TEAM_AGENT_JOB_COMPLETED
            if passed
            else EventType.TEAM_AGENT_JOB_FAILED
        )
        self._append_team_runtime_event(
            auto_run,
            event_type,
            team_run_id=team_run.id,
            agent_job_id=tester_job.id,
            agent_run_id=agent_run_id,
            payload={
                "role": "tester",
                "status": status,
            },
        )

    def _tester_attempt_count_for_auto_run(self, auto_run: object) -> int:
        team_run = self._team_run_for_auto_run(auto_run)
        if team_run is None or not hasattr(self._ledger, "list_team_agent_jobs"):
            return 0
        return sum(
            1
            for job in self._ledger.list_team_agent_jobs(team_run.id)
            if job.role == "tester"
        )

    def _internal_test_failure_text(self, attempt_count: int) -> str:
        attempt = max(1, attempt_count)
        if attempt >= MAX_INTERNAL_TEST_ATTEMPTS:
            return (
                f"测试连续 {MAX_INTERNAL_TEST_ATTEMPTS} 次未通过或缺少测试证据，"
                "已停止内部返工循环。请查看测试证据后决定返工、接管或结束。"
            )
        return (
            f"测试第 {attempt}/{MAX_INTERNAL_TEST_ATTEMPTS} 次未通过或缺少测试证据，"
            f"最多还会内部返工 {MAX_INTERNAL_TEST_ATTEMPTS - attempt} 次。"
        )

    def _record_audit_report_artifact(
        self,
        auto_run: object,
        *,
        agent_run_id: int | None,
        completion_summary: str,
    ) -> bool | None:
        team_run = self._team_run_for_auto_run(auto_run)
        if team_run is None or not hasattr(self._ledger, "record_team_artifact"):
            return None
        job = self._running_team_job(
            team_run,
            role="auditor",
            agent_run_id=agent_run_id,
        )
        if job is None or self._has_team_artifact(
            team_run.id,
            artifact_type="audit_report",
            agent_job_id=job.id,
        ):
            return False
        payload = _parse_audit_report_payload(completion_summary)
        artifact = self._ledger.record_team_artifact(
            team_run_id=team_run.id,
            agent_job_id=job.id,
            artifact_type="audit_report",
            summary=str(payload["summary"])[:2000],
            payload=payload,
        )
        from wlcodex.runtime_events import EventType

        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_ARTIFACT_RECORDED,
            team_run_id=team_run.id,
            agent_job_id=job.id,
            agent_run_id=agent_run_id,
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
            },
        )
        from wlcodex.team_artifacts import validate_audit_report

        gate_result = validate_audit_report(payload)
        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_GATE_PASSED if gate_result.passed else EventType.TEAM_GATE_FAILED,
            team_run_id=team_run.id,
            agent_job_id=job.id,
            agent_run_id=agent_run_id,
            payload={
                "gate": "Gate D",
                "artifact_id": artifact.id,
                "artifact_type": "audit_report",
                "missing": list(gate_result.missing),
            },
        )
        try:
            from wlcodex.team_observer import (
                candidate_instinct_from_observation,
                observations_from_artifact,
            )

            conversation = self._ledger.get_conversation(auto_run.conversation_id)
            observations = observations_from_artifact(
                team_run_id=team_run.id,
                artifact_type="audit_report",
                payload=payload,
                evidence_ref=f"team_artifact={artifact.id}",
            )
            for observation in observations:
                stored = self._ledger.record_team_observation(
                    team_run_id=observation.team_run_id,
                    domain=observation.domain,
                    summary=observation.summary,
                    evidence_refs=observation.evidence_refs,
                    confidence=observation.confidence,
                )
                self._append_team_runtime_event(
                    auto_run,
                    EventType.TEAM_OBSERVATION_RECORDED,
                    team_run_id=team_run.id,
                    agent_job_id=job.id,
                    agent_run_id=agent_run_id,
                    payload={
                        "observation_id": stored.id,
                        "domain": stored.domain,
                        "confidence": stored.confidence,
                    },
                )
                candidate = candidate_instinct_from_observation(
                    stored,
                    workspace_alias=conversation.workspace_alias,
                    repeated_evidence_count=1,
                )
                existing_status = self._team_instinct_status(candidate.instinct_id)
                repeated_count = 2 if existing_status is not None else 1
                instinct = candidate_instinct_from_observation(
                    stored,
                    workspace_alias=conversation.workspace_alias,
                    repeated_evidence_count=repeated_count,
                )
                stored_instinct = self._ledger.upsert_team_instinct(instinct)
                if existing_status is None:
                    self._append_team_runtime_event(
                        auto_run,
                        EventType.TEAM_INSTINCT_PROPOSED,
                        team_run_id=team_run.id,
                        agent_job_id=job.id,
                        agent_run_id=agent_run_id,
                        payload={
                            "instinct_id": stored_instinct.instinct_id,
                            "status": stored_instinct.status,
                        },
                    )
                elif existing_status != "active" and stored_instinct.status == "active":
                    self._append_team_runtime_event(
                        auto_run,
                        EventType.TEAM_INSTINCT_PROMOTED,
                        team_run_id=team_run.id,
                        agent_job_id=job.id,
                        agent_run_id=agent_run_id,
                        payload={
                            "instinct_id": stored_instinct.instinct_id,
                            "status": stored_instinct.status,
                        },
                    )
        except Exception:
            logger.debug("Unable to record audit observations", exc_info=True)
        return gate_result.passed

    def _mark_architect_team_job_done(
        self,
        auto_run: object,
        *,
        agent_run_id: int | None = None,
    ) -> None:
        team_run = self._team_run_for_auto_run(auto_run)
        role = self._team_first_role(team_run) or "architect"
        marked = self._mark_team_agent_job_done(
            auto_run,
            role=role,
            agent_run_id=agent_run_id,
        )
        if marked:
            return
        if team_run is None or not hasattr(self._ledger, "update_team_agent_job_status"):
            return
        job = self._single_running_team_job(team_run, role=role)
        if job is None:
            return
        self._ledger.update_team_agent_job_status(job.id, "done")
        from wlcodex.runtime_events import EventType

        self._append_team_runtime_event(
            auto_run,
            EventType.TEAM_AGENT_JOB_COMPLETED,
            team_run_id=team_run.id,
            agent_job_id=job.id,
            agent_run_id=agent_run_id,
            payload={
                "role": role,
                "status": "done",
                "linked_agent_run_id": job.agent_run_id,
            },
        )

    def _mark_team_agent_job_done(
        self,
        auto_run: object,
        *,
        role: str,
        agent_run_id: int | None = None,
    ) -> bool:
        if not hasattr(self._ledger, "get_team_run_for_orchestration"):
            return False
        team_run = self._ledger.get_team_run_for_orchestration(auto_run.id)
        if team_run is None or not hasattr(self._ledger, "list_team_agent_jobs"):
            return False
        if not hasattr(self._ledger, "update_team_agent_job_status"):
            return False
        for job in self._ledger.list_team_agent_jobs(team_run.id):
            if job.role != role or job.status != "running":
                continue
            if agent_run_id is not None and job.agent_run_id != agent_run_id:
                continue
            if agent_run_id is None and job.agent_run_id is not None:
                continue
            self._ledger.update_team_agent_job_status(job.id, "done")
            from wlcodex.runtime_events import EventType

            self._append_team_runtime_event(
                auto_run,
                EventType.TEAM_AGENT_JOB_COMPLETED,
                team_run_id=team_run.id,
                agent_job_id=job.id,
                agent_run_id=agent_run_id,
                payload={
                    "role": role,
                    "status": "done",
                },
            )
            return True
        return False

    def _mark_team_agent_job_failed(
        self,
        auto_run: object,
        *,
        role: str,
        agent_run_id: int | None = None,
    ) -> None:
        if not hasattr(self._ledger, "get_team_run_for_orchestration"):
            return
        team_run = self._ledger.get_team_run_for_orchestration(auto_run.id)
        if team_run is None or not hasattr(self._ledger, "list_team_agent_jobs"):
            return
        if hasattr(self._ledger, "update_team_run_status"):
            self._ledger.update_team_run_status(team_run.id, "failed")
        if not hasattr(self._ledger, "update_team_agent_job_status"):
            return
        for job in self._ledger.list_team_agent_jobs(team_run.id):
            if job.role != role or job.status != "running":
                continue
            if agent_run_id is not None and job.agent_run_id != agent_run_id:
                continue
            if agent_run_id is None and job.agent_run_id is not None:
                continue
            self._ledger.update_team_agent_job_status(job.id, "failed")

    def _final_synthesis_text(self, auto_run: object) -> str:
        team_run = self._team_run_for_auto_run(auto_run)
        if team_run is None or not hasattr(self._ledger, "list_team_artifacts"):
            return "最终综合：\n验收通过，任务完成。"

        implementation = None
        test_report = None
        audit_report = None
        for artifact in self._ledger.list_team_artifacts(team_run.id):
            if artifact.artifact_type == "implementation_report":
                implementation = artifact
            elif artifact.artifact_type == "test_report":
                test_report = artifact
            elif artifact.artifact_type == "audit_report":
                audit_report = artifact

        lines = ["最终综合："]
        if implementation is not None:
            files = implementation.payload.get("changed_files", [])
            if isinstance(files, list) and files:
                lines.append("变更文件：" + ", ".join(str(item) for item in files[:8]))
            diff_summary = str(implementation.payload.get("diff_summary", "")).strip()
            if diff_summary:
                lines.append("变更摘要：" + diff_summary[:240])
        if test_report is not None:
            passed = test_report.payload.get("passed", [])
            if isinstance(passed, list) and passed:
                lines.append("测试证据：" + ", ".join(str(item) for item in passed[:4]))
            commands = test_report.payload.get("commands_run", [])
            if isinstance(commands, list) and commands:
                command_names = [
                    str(command.get("command", ""))
                    for command in commands
                    if isinstance(command, dict) and command.get("command")
                ]
                if command_names:
                    lines.append("测试命令：" + ", ".join(command_names[:3]))
        if audit_report is not None:
            decision = str(audit_report.payload.get("decision", "")).strip()
            risk = str(audit_report.payload.get("risk_level", "")).strip()
            summary = str(audit_report.payload.get("summary", "")).strip()
            decision_label = {
                "pass": "验收通过",
                "block": "验收未通过",
                "needs_user": "需要你确认",
            }.get(decision, "未明确")
            risk_label = {
                "low": "低",
                "medium": "中",
                "high": "高",
                "critical": "严重",
            }.get(risk, risk)
            audit_line = "审计结论：" + decision_label
            if risk:
                audit_line += f" / 风险：{risk_label}"
            lines.append(audit_line)
            if summary:
                lines.append("主要结论：" + summary[:240])
        else:
            lines.append("审计结论：验收通过。")
        return "\n".join(lines)

    async def _try_collect_diagnose_json_async(self, auto_run: Any) -> str:
        """Async wrapper: run diagnose_live.py via thread to avoid blocking loop."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, _try_collect_diagnose_json_sync, self, auto_run,
            )
        except Exception:
            return ""

    def _auto_digest_usage_recorder(
        self,
        auto_run: Any,
        task: object,
    ) -> Callable[[DeepSeekDigestUsage], None]:
        def record(usage: DeepSeekDigestUsage) -> None:
            self._record_auto_digest_usage(auto_run, task, usage)

        return record

    def _record_auto_digest_usage(
        self,
        auto_run: Any,
        task: object,
        usage: DeepSeekDigestUsage,
    ) -> None:
        if not hasattr(self._ledger, "record_usage_event"):
            return
        metadata = {
            "digest_kind": usage.digest_kind,
            "source_chars": usage.source_chars,
            "prompt_chars": usage.prompt_chars,
            "response_chars": usage.response_chars,
            "digest_chars": usage.digest_chars,
            "failure_reason": usage.failure_reason,
        }
        try:
            self._ledger.record_usage_event(
                agent="deepseek",
                role="auto_digest",
                phase=usage.digest_kind,
                request_kind="telegram_digest",
                model=usage.model,
                source="exact" if usage.total_tokens else "derived",
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_output_tokens=usage.reasoning_output_tokens,
                total_tokens=usage.total_tokens,
                latency_ms=usage.latency_ms,
                status=usage.status,
                conversation_id=int(getattr(auto_run, "conversation_id")),
                orchestration_run_id=int(getattr(auto_run, "id")),
                task_id=int(getattr(task, "id")),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        except Exception:
            logger.debug("Failed to record DeepSeek digest token usage", exc_info=True)

    async def _send_auto_stage_buttons(
        self, task: object, new_stage: str
    ) -> None:
        """Send stage-appropriate buttons to Telegram after a stage transition."""
        chat_id = getattr(task, "telegram_chat_id", None)
        if chat_id is None:
            return
        task_id = int(getattr(task, "id"))
        # Find conversation
        conv_row = self._ledger._conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE active_codex_task_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if conv_row is None:
            return
        conversation_id = int(conv_row["id"])
        auto_run = self._ledger.get_latest_active_auto_run(conversation_id)
        if auto_run is None:
            return

        from wlcodex.auto_workflow import build_auto_stage_buttons, auto_stage_label

        buttons = build_auto_stage_buttons(
            conversation_id, new_stage,
            last_codex_analysis=auto_run.last_codex_analysis or "",
            codex_implementer_enabled=self._codex_implementer_enabled,
            include_team_controls=self._auto_run_has_team(auto_run),
        )
        # Include orch run data in the message for draft_ready
        stage_label = auto_stage_label(new_stage)

        # Prefer structured diagnose JSON digest when available
        diagnose_json = getattr(auto_run, "diagnose_json", "") or ""
        structured_digest = ""
        if diagnose_json:
            structured_digest = render_auto_diagnose_digest(diagnose_json)

        # Detect whether diagnose JSON was expected: an auto_analysis or
        # auto_final_plan agent ran AND the goal/analysis mentions LightFeeV2
        # production diagnosis keywords.
        diagnose_expected = False
        if not diagnose_json:
            try:
                agent_runs = self._ledger.list_agent_runs(
                    auto_run.conversation_id,
                )
                diagnose_expected = _auto_run_expects_diagnose_json(
                    auto_run,
                    agent_runs,
                )
            except Exception:
                pass

        # Deterministic collection: when diagnose is expected but not present,
        # run diagnose_live.py ourselves — no model involvement.
        if diagnose_expected and not diagnose_json:
            collected = await self._try_collect_diagnose_json_async(auto_run)
            if collected:
                diagnose_json = collected
                structured_digest = render_auto_diagnose_digest(diagnose_json)
                diagnose_expected = False  # no longer missing

        if new_stage == "collecting_context":
            if auto_run.last_codex_analysis:
                if structured_digest:
                    digest = structured_digest
                elif diagnose_expected:
                    digest = render_missing_diagnose_digest()
                else:
                    digest = await render_auto_draft_digest_with_llm(
                        auto_run.last_codex_analysis,
                        fallback_next="继续补充信息，或点击生成最终方案。",
                        usage_recorder=self._auto_digest_usage_recorder(
                            auto_run,
                            task,
                        ),
                    )
                text = "Codex 已更新分析。\n\n{}\n\n请选择下一步：".format(digest)
            else:
                text = (
                    "Codex 已完成上下文收集。\n\n"
                    "你可以继续补充信息，或生成最终方案。"
                )
        elif new_stage == "draft_ready" and auto_run.last_codex_analysis:
            # Primary: always render the human-readable plan from
            # last_codex_analysis. Diagnose JSON, if present, is only a
            # supplementary evidence note — it must never replace the plan.
            team_run = self._team_run_for_auto_run(auto_run)
            route_kind = self._team_route_kind(team_run)
            digest = await render_auto_draft_digest_with_llm(
                auto_run.last_codex_analysis,
                digest_kind="diagnosis" if route_kind == "bug" else "design",
                usage_recorder=self._auto_digest_usage_recorder(auto_run, task),
            )
            supplement = ""
            if structured_digest:
                supplement = _brief_diagnose_supplement(structured_digest)
            elif diagnose_expected:
                supplement = (
                    "结构化诊断证据未采集到；最终方案缺少自动诊断旁证。"
                    "请重新触发诊断采集，或检查诊断日志后再继续。"
                )
            if supplement:
                text = "最终方案已生成。\n\n{}\n\n{}\n\n请选择下一步：".format(digest, supplement)
            else:
                text = "最终方案已生成。\n\n{}\n\n请选择下一步：".format(digest)
        elif new_stage == "draft_ready":
            text = (
                "最终方案生成完成，但没有收到方案正文。\n\n"
                "为避免黑盒执行，暂不提供开发工程师执行入口。\n"
                "请继续补充上下文。"
            )
        elif new_stage == "claude_done":
            digest = await render_auto_draft_digest_with_llm(
                auto_run.last_claude_summary or "结论：完成。",
                digest_kind="implementation",
                usage_recorder=self._auto_digest_usage_recorder(auto_run, task),
            )
            text = f"开发完成，测试通过。\n\n{digest}\n\n请选择下一步："
        elif new_stage == "completed":
            text = self._final_synthesis_text(auto_run)
        elif new_stage == "retry_ready":
            digest = await render_auto_draft_digest_with_llm(
                auto_run.last_verification_result or "结论：验收未通过。",
                usage_recorder=self._auto_digest_usage_recorder(auto_run, task),
            )
            title = (
                "测试未通过。"
                if "测试" in (auto_run.last_verification_result or "")
                else "验收未通过。"
            )
            text = f"{title}\n\n{digest}\n\n请选择下一步："
        else:
            text = f"阶段：{stage_label}\n\n请选择下一步："

        try:
            await self._send_telegram(chat_id, text, buttons)
        except Exception:
            logger.exception("Failed to send auto stage buttons")
