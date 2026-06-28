from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class RawWorkLogEntry:
    kind: str
    key: str
    text: str = ""
    chip: str = ""
    output: str = ""
    failed: bool = False
    replace_text: bool = False


@dataclass(frozen=True)
class WorkLogProjectionProfile:
    name: str
    tool_batch_threshold: int
    folded_process_chip: str
    tool_batch_chip_prefix: str
    tool_batch_suffix: str


MARVIS_WORK_LOG_PROFILE = WorkLogProjectionProfile(
    name="marvis",
    tool_batch_threshold=4,
    folded_process_chip="过程输出 已折叠",
    tool_batch_chip_prefix="工具调用",
    tool_batch_suffix="原始输出已折叠。",
)
WORK_LOG_PROJECTION_PROFILES: dict[str, WorkLogProjectionProfile] = {
    MARVIS_WORK_LOG_PROFILE.name: MARVIS_WORK_LOG_PROFILE,
}


_COMMAND_CATEGORY_LABELS: tuple[tuple[set[str], str], ...] = (
    ({"rg", "grep", "find", "fd", "ag"}, "检索"),
    ({"sed", "nl", "cat", "head", "tail", "less"}, "读取"),
    ({"git"}, "检查变更"),
    ({"pytest", "unittest", "coverage"}, "测试"),
    ({"node", "npm", "npx", "pnpm", "yarn"}, "前端工具"),
    ({"sqlite3", "psql", "mysql"}, "查询状态"),
    ({"curl", "wget", "http"}, "网络检查"),
)

_AGENT_META_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bThe file\b.+\bhas been updated successfully\b", re.IGNORECASE),
    re.compile(r"\bAll changes are in place\b", re.IGNORECASE),
    re.compile(r"\bNo matches found\b", re.IGNORECASE),
    re.compile(r"\bFound \d+ files?\b", re.IGNORECASE),
    re.compile(r"\bTask not found\b", re.IGNORECASE),
    re.compile(r"位于分支|尚未暂存|修改尚未加入提交|未跟踪的文件"),
)


def compress_work_log_entries(
    entries: Iterable[RawWorkLogEntry],
    *,
    role: str,
    profile: str | WorkLogProjectionProfile = "marvis",
) -> list[RawWorkLogEntry]:
    projection_profile = resolve_work_log_projection_profile(profile)
    if projection_profile is None:
        return list(entries)

    normalized = [_normalize_entry(entry) for entry in entries]
    projected: list[RawWorkLogEntry] = []
    tool_buffer: list[RawWorkLogEntry] = []
    folded_buffer: list[RawWorkLogEntry] = []
    has_artifact_summary = any(
        entry.kind == "artifact" and bool(str(entry.text or "").strip()) for entry in normalized
    )

    def flush_tools() -> None:
        nonlocal tool_buffer
        if not tool_buffer:
            return
        if len(tool_buffer) >= projection_profile.tool_batch_threshold:
            projected.append(_tool_batch_entry(tool_buffer, role=role, profile=projection_profile))
        else:
            projected.extend(tool_buffer)
        tool_buffer = []

    def flush_folded() -> None:
        nonlocal folded_buffer
        if not folded_buffer:
            return
        if not has_artifact_summary:
            projected.append(
                _folded_process_entry(folded_buffer, role=role, profile=projection_profile)
            )
        elif projected and projected[-1].output:
            extra = _join_entry_outputs(folded_buffer)
            if extra and extra not in projected[-1].output:
                projected[-1].output = f"{projected[-1].output}\n{extra}"
        elif projected:
            projected[-1].output = _join_entry_outputs(folded_buffer)
        else:
            projected.append(
                _folded_process_entry(folded_buffer, role=role, profile=projection_profile)
            )
        folded_buffer = []

    for entry in normalized:
        if entry.kind in {"command", "tool", "file"} and entry.chip:
            flush_folded()
            tool_buffer.append(entry)
            continue
        if _entry_should_be_raw_evidence(entry):
            flush_tools()
            folded_buffer.append(entry)
            continue
        flush_tools()
        flush_folded()
        projected.append(_humanize_entry(entry))

    flush_tools()
    flush_folded()
    return _drop_empty_entries(projected)


def resolve_work_log_projection_profile(
    profile: str | WorkLogProjectionProfile,
) -> WorkLogProjectionProfile | None:
    if isinstance(profile, WorkLogProjectionProfile):
        return profile
    return WORK_LOG_PROJECTION_PROFILES.get(str(profile or "").strip())


def text_looks_like_agent_dump(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if any(pattern.search(value) for pattern in _AGENT_META_PATTERNS):
        return True
    return _looks_like_machine_output(value)


def humanize_work_log_error(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    prefix = ""
    match = re.match(r"^(.+?执行问题：)(.+)$", value)
    if match:
        prefix = match.group(1)
        value = match.group(2).strip()
    lowered = value.lower()
    if "invalid json" in lowered or "jsondecodeerror" in lowered:
        return f"{prefix}结构化结果不是合法 JSON，系统无法直接收口。"
    if "missing required fields" in lowered:
        fields = value.split(":", 1)[1].strip() if ":" in value else ""
        fields = fields.rstrip(".")
        if fields:
            return f"{prefix}结构化结果缺少必填字段：{fields}。"
        return f"{prefix}结构化结果缺少必填字段。"
    if "handoff_to" in lowered and "before required role" in lowered:
        return f"{prefix}交接顺序不符合固定接力链路，不能跳过必需角色。"
    return f"{prefix}{value}" if prefix else value


def _normalize_entry(entry: RawWorkLogEntry) -> RawWorkLogEntry:
    return RawWorkLogEntry(
        kind=str(entry.kind or ""),
        key=str(entry.key or ""),
        text=str(entry.text or ""),
        chip=str(entry.chip or ""),
        output=str(entry.output or ""),
        failed=bool(entry.failed),
        replace_text=bool(entry.replace_text),
    )


def _entry_should_be_raw_evidence(entry: RawWorkLogEntry) -> bool:
    if entry.kind == "message" and text_looks_like_agent_dump(entry.text):
        return True
    if entry.kind in {"activity", "lifecycle"} and text_looks_like_agent_dump(entry.text):
        return True
    return False


def _humanize_entry(entry: RawWorkLogEntry) -> RawWorkLogEntry:
    if entry.kind == "error" or entry.failed:
        entry.text = humanize_work_log_error(entry.text)
    return entry


def _tool_batch_entry(
    entries: list[RawWorkLogEntry],
    *,
    role: str,
    profile: WorkLogProjectionProfile,
) -> RawWorkLogEntry:
    counts: dict[str, int] = {}
    for entry in entries:
        category = _tool_category(entry)
        counts[category] = counts.get(category, 0) + 1
    summary = "、".join(f"{label} {count} 次" for label, count in counts.items())
    if summary:
        summary = f"{summary}。{profile.tool_batch_suffix}"
    else:
        summary = profile.tool_batch_suffix
    return RawWorkLogEntry(
        kind="tool_batch",
        key=f"tool-batch:{role}:{entries[0].key}:{len(entries)}",
        chip=f"{profile.tool_batch_chip_prefix} {len(entries)} 次",
        text=summary,
        output=_join_entry_outputs(entries),
        failed=any(entry.failed for entry in entries),
    )


def _folded_process_entry(
    entries: list[RawWorkLogEntry],
    *,
    role: str,
    profile: WorkLogProjectionProfile,
) -> RawWorkLogEntry:
    return RawWorkLogEntry(
        kind="raw_output",
        key=f"raw-output:{role}:{entries[0].key}:{len(entries)}",
        chip=profile.folded_process_chip,
        output=_join_entry_outputs(entries),
        failed=any(entry.failed for entry in entries),
    )


def _join_entry_outputs(entries: list[RawWorkLogEntry]) -> str:
    chunks: list[str] = []
    for entry in entries:
        label = entry.chip or entry.kind
        body = entry.output or entry.text
        if body:
            chunks.append(f"{label}\n{body}" if label else body)
        elif label:
            chunks.append(label)
    return "\n\n".join(chunks).strip()


def _tool_category(entry: RawWorkLogEntry) -> str:
    command = _entry_command_name(entry)
    for commands, label in _COMMAND_CATEGORY_LABELS:
        if command in commands:
            return label
    if entry.kind == "file":
        return "文件变更"
    return "工具"


def _entry_command_name(entry: RawWorkLogEntry) -> str:
    label = (entry.chip or entry.key or "").strip()
    token = label.split(maxsplit=1)[0] if label else ""
    if ":" in token:
        token = token.rsplit(":", 1)[-1]
    return Path(token).name.lower()


def _drop_empty_entries(entries: list[RawWorkLogEntry]) -> list[RawWorkLogEntry]:
    return [
        entry
        for entry in entries
        if entry.text.strip() or entry.chip.strip() or entry.output.strip()
    ]


def _looks_like_machine_output(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    normalized = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) == 1:
        line = lines[0]
        return bool(
            re.match(r"^\d{2,}[:\t ]+\S", line)
            or re.match(r"^(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+$", line)
        )
    path_like = 0
    line_hit_like = 0
    code_like = 0
    for line in lines:
        if re.match(r"^(?:/[^:\s]+|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)$", line):
            path_like += 1
        if re.match(r"^(?:\d{2,}|[^:\s]+:\d{1,5})[:\t ]+\S", line):
            line_hit_like += 1
        if re.search(r"\b(?:def|class|const|let|var|return|import|from)\b|[{}();=]", line):
            code_like += 1
    if path_like >= 2 or line_hit_like >= 2:
        return True
    return len(lines) >= 4 and (path_like + line_hit_like + code_like) / len(lines) >= 0.5
