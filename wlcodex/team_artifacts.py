from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class GateResult:
    passed: bool
    missing: tuple[str, ...] = ()


_PLACEHOLDER_PREFIXES = (
    "No changed files reported.",
    "No diff evidence collected.",
    "No structured command evidence reported",
    "No structured test attempt reported",
    "No structured test command evidence reported",
    "No passing test evidence reported.",
    "Test evidence requires auditor review.",
    "Acceptance criteria coverage was not reported structurally.",
)


def _is_placeholder(value: Any) -> bool:
    text = str(value).strip()
    return any(text.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def _missing(payload: Mapping[str, Any], required_fields: tuple[str, ...]) -> tuple[str, ...]:
    missing = []
    for field in required_fields:
        value = payload.get(field)
        if value is None or value == "" or value == [] or _is_placeholder(value):
            missing.append(field)
    return tuple(missing)


def _result(missing: tuple[str, ...]) -> GateResult:
    return GateResult(passed=not missing, missing=missing)


def _append_missing_once(missing: list[str], field: str) -> None:
    if field not in missing:
        missing.append(field)


def _valid_command_entries(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for entry in value:
        if not isinstance(entry, Mapping):
            return False
        command = entry.get("command")
        summary = entry.get("summary")
        if not command or not summary or _is_placeholder(command) or _is_placeholder(summary):
            return False
        if "exit_status" not in entry or not isinstance(entry.get("exit_status"), int):
            return False
    return True


def _valid_coverage_entries(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for entry in value:
        if not isinstance(entry, Mapping):
            return False
        if entry.get("status") not in {"covered", "uncovered"}:
            return False
        if (
            not entry.get("criterion")
            or not entry.get("evidence")
            or _is_placeholder(entry.get("evidence"))
        ):
            return False
    return True


def _valid_non_placeholder_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and not any(
        _is_placeholder(item) for item in value
    )


def _missing_command_evidence() -> dict[str, Any]:
    return {
        "command": "No structured command evidence reported by implementer.",
        "exit_status": 1,
        "summary": (
            "No commandExecution event or structured evidence block was available; "
            "Gate B/C must block."
        ),
    }


def _missing_test_evidence() -> dict[str, Any]:
    return {
        "command": "No structured test attempt reported by implementer.",
        "exit_status": 1,
        "summary": (
            "No test command evidence was available; Gate C must block."
        ),
    }


def _normalize_command_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    command = str(entry.get("command", "")).strip()
    if not command:
        return None
    summary = str(entry.get("summary", "")).strip() or "command evidence reported"
    try:
        exit_status = int(entry.get("exit_status", entry.get("exit_code", 0)))
    except (TypeError, ValueError):
        exit_status = 1
    return {
        "command": command,
        "exit_status": exit_status,
        "summary": summary,
    }


def _normalize_changed_file_entry(entry: Any) -> tuple[str | None, str]:
    if isinstance(entry, Mapping):
        path = str(entry.get("path", entry.get("file", ""))).strip()
        action = str(entry.get("action", "")).strip()
        evidence = str(entry.get("evidence", "")).strip()
        details = " ".join(part for part in (action, path) if part)
        if evidence:
            details = f"{details}: {evidence}" if details else evidence
        return (path or None), details
    text = str(entry).strip()
    return (text or None), text


def _acceptance_verification_command(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    command = str(entry.get("command", "")).strip()
    if not command:
        return None
    checks = entry.get("checks")
    result = str(entry.get("result", "")).strip()
    result_lower = result.lower()
    checks_passed = (
        isinstance(checks, Mapping)
        and bool(checks)
        and all(bool(value) for value in checks.values())
    )
    passed = checks_passed or any(
        marker in result_lower
        for marker in ("pass", "passed", "success", "succeeded", "all checks passed")
    )
    summary = result or (
        "acceptance verification passed"
        if passed
        else "acceptance verification failed"
    )
    return {
        "command": command,
        "exit_status": 0 if passed else 1,
        "summary": summary,
    }


def structured_implementation_evidence_from_text(text: str) -> dict[str, Any]:
    """Extract a compact implementation evidence JSON block from result text."""
    if not text:
        return {}
    for match in re.finditer(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL):
        block = match.group(1).strip()
        try:
            parsed = json.loads(block)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(parsed, Mapping):
            continue
        evidence = parsed.get("implementation_evidence")
        if not isinstance(evidence, Mapping):
            evidence = parsed.get("implementation_report")
        if not isinstance(evidence, Mapping):
            evidence = parsed
        if not isinstance(evidence, Mapping):
            continue
        keys = {
            "changed_files",
            "files_changed",
            "diff_summary",
            "commands_run",
            "tests_attempted",
            "acceptance_verification",
        }
        if not keys.intersection(evidence.keys()):
            continue
        normalized: dict[str, Any] = {}
        changed_files = evidence.get("changed_files", evidence.get("files_changed"))
        if isinstance(changed_files, list):
            paths: list[str] = []
            diff_parts: list[str] = []
            for item in changed_files:
                path, details = _normalize_changed_file_entry(item)
                if path:
                    paths.append(path)
                if details:
                    diff_parts.append(details)
            if paths:
                normalized["changed_files"] = paths
            if diff_parts and not evidence.get("diff_summary"):
                normalized["diff_summary"] = "; ".join(diff_parts)
        if evidence.get("diff_summary"):
            normalized["diff_summary"] = str(evidence["diff_summary"]).strip()
        for field in ("commands_run", "tests_attempted"):
            entries = [
                normalized_entry
                for entry in evidence.get(field, [])
                if (normalized_entry := _normalize_command_entry(entry)) is not None
            ]
            if entries:
                normalized[field] = entries
        acceptance_command = _acceptance_verification_command(
            evidence.get("acceptance_verification")
        )
        if acceptance_command is not None:
            normalized.setdefault("commands_run", []).append(acceptance_command)
            normalized.setdefault("tests_attempted", []).append(acceptance_command)
        return normalized
    return {}


def command_evidence_from_task_events(events: Any) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for event in events:
        if getattr(event, "event_type", "") != "item_completed":
            continue
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        if payload.get("type") != "commandExecution":
            continue
        command = str(payload.get("command", "")).strip()
        if not command:
            continue
        status = str(payload.get("status", "completed")).strip().lower()
        exit_status = 0 if status in {"completed", "success", "succeeded", "done"} else 1
        summary = "command completed" if exit_status == 0 else f"command {status or 'failed'}"
        commands.append({
            "command": command,
            "exit_status": exit_status,
            "summary": summary,
        })
    return commands


def test_command_evidence(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    test_markers = ("pytest", "unittest", "tox", "nox", "cargo test", "npm test")
    return [
        command
        for command in commands
        if any(marker in str(command.get("command", "")).lower() for marker in test_markers)
    ]


def acceptance_criteria_from_artifacts(artifacts: Any) -> list[str]:
    for artifact in reversed(list(artifacts or [])):
        if getattr(artifact, "artifact_type", "") != "architecture_plan":
            continue
        payload = getattr(artifact, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        criteria = payload.get("acceptance_criteria")
        if not isinstance(criteria, list):
            continue
        normalized = [str(item).strip() for item in criteria if str(item).strip()]
        if normalized:
            return normalized
    return ["Focused verification command completed."]


def validate_architecture_plan(payload: Mapping[str, Any]) -> GateResult:
    return _result(
        _missing(
            payload,
            (
                "summary",
                "files_or_modules_in_scope",
                "files_or_modules_out_of_scope",
                "impact_notes",
                "risk_level",
                "implementation_steps",
                "acceptance_criteria",
                "parallelization_policy",
            ),
        )
    )


def validate_implementation_report(payload: Mapping[str, Any]) -> GateResult:
    missing = list(
        _missing(
            payload,
            ("summary", "changed_files", "diff_summary", "known_limitations"),
        )
    )
    if not _valid_non_placeholder_list(payload.get("changed_files")):
        _append_missing_once(missing, "changed_files")
    if not payload.get("diff_summary") or _is_placeholder(payload.get("diff_summary")):
        _append_missing_once(missing, "diff_summary")
    if not _valid_command_entries(payload.get("commands_run")):
        _append_missing_once(missing, "commands_run")
    if not _valid_command_entries(payload.get("tests_attempted")):
        _append_missing_once(missing, "tests_attempted")
    return _result(tuple(missing))


def validate_test_report(payload: Mapping[str, Any]) -> GateResult:
    missing = list(
        _missing(
            payload,
            ("summary", "passed", "failed", "failure_evidence"),
        )
    )
    if not _valid_command_entries(payload.get("commands_run")):
        _append_missing_once(missing, "commands_run")
    if not _valid_non_placeholder_list(payload.get("passed")):
        _append_missing_once(missing, "passed")
    if not _valid_coverage_entries(payload.get("coverage_of_acceptance_criteria")):
        _append_missing_once(missing, "coverage_of_acceptance_criteria")
    return _result(tuple(missing))


def validate_audit_report(payload: Mapping[str, Any]) -> GateResult:
    missing = list(
        _missing(
            payload,
            (
                "decision",
                "summary",
                "findings",
                "missing_evidence",
                "risk_level",
                "recommended_next_action",
            ),
        )
    )
    if "decision" not in missing and payload["decision"] not in {"pass", "block", "needs_user"}:
        missing.append("decision")
    if payload.get("decision") == "pass" and not _valid_non_placeholder_list(
        payload.get("test_evidence_refs")
    ):
        missing.append("test_evidence_refs")
    return _result(tuple(missing))


def architecture_plan_payload(
    *,
    summary: str,
    risk_level: str = "medium",
    source: str = "",
) -> dict[str, Any]:
    return {
        "summary": summary,
        "files_or_modules_in_scope": ["See architecture plan summary."],
        "files_or_modules_out_of_scope": ["Unrelated files and modules."],
        "impact_notes": "Impact details are captured in the plan summary.",
        "risk_level": risk_level,
        "implementation_steps": ["Follow the accepted architecture plan."],
        "acceptance_criteria": ["Satisfy the accepted plan and pass focused verification."],
        "parallelization_policy": "single implementer unless the plan explicitly decomposes scopes",
        "investigator_policy": "Architect performs investigator duties in v1",
        "source": source,
    }


def implementation_report_payload(
    *,
    summary: str,
    changed_files: list[str],
    diff_summary: str,
    source_agent: str,
    commands_run: list[dict[str, Any]] | None = None,
    tests_attempted: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "summary": summary,
        "changed_files": changed_files or ["No changed files reported."],
        "diff_summary": diff_summary or "No diff evidence collected.",
        "commands_run": commands_run or [_missing_command_evidence()],
        "tests_attempted": tests_attempted or [_missing_test_evidence()],
        "known_limitations": ["None known"],
        "source_agent": source_agent,
    }


def test_report_payload_from_implementation(
    *,
    summary: str,
    implementation_artifact_id: int | None,
    commands_run: list[dict[str, Any]] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    evidence = (
        f"team_artifact={implementation_artifact_id}"
        if implementation_artifact_id is not None
        else "implementation_report"
    )
    commands = commands_run or [_missing_test_evidence()]
    failed_commands = [command for command in commands if command.get("exit_status") != 0]
    passed_commands = [command for command in commands if command.get("exit_status") == 0]
    coverage_evidence = (
        evidence
        if passed_commands and not failed_commands
        else "Acceptance criteria coverage was not reported structurally."
    )
    coverage = [
        {
            "criterion": criterion,
            "status": "covered" if passed_commands and not failed_commands else "uncovered",
            "evidence": coverage_evidence,
        }
        for criterion in (acceptance_criteria or ["Focused verification command completed."])
    ]
    return {
        "summary": summary,
        "commands_run": commands,
        "passed": [str(command.get("command", "")) for command in passed_commands]
        or ["No passing test evidence reported."],
        "failed": [str(command.get("command", "")) for command in failed_commands] or ["None"],
        "coverage_of_acceptance_criteria": coverage,
        "failure_evidence": [evidence] if failed_commands else ["None"],
    }
