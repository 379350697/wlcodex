from __future__ import annotations

from dataclasses import dataclass


DEFAULT_CLAUDE_PERMISSION_MODE = "acceptEdits"
RUNTIME_CLAUDE_PERMISSION_MODE_KEY = "claude.permission_mode"

CLAUDE_PERMISSION_MODE_ORDER: tuple[str, ...] = (
    "acceptEdits",
    "auto",
    "plan",
    "default",
    "dontAsk",
    "bypassPermissions",
)

CLAUDE_PERMISSION_MODE_LABELS: dict[str, str] = {
    "acceptEdits": "允许编辑",
    "auto": "自动模式",
    "plan": "只规划",
    "default": "默认确认",
    "dontAsk": "不询问",
    "bypassPermissions": "跳过权限检查",
}

CLAUDE_PERMISSION_MODE_DESCRIPTIONS: dict[str, str] = {
    "acceptEdits": "允许 DeepSeek 开发工程师直接修改文件，适合日常实现闭环。",
    "auto": "让 DeepSeek 开发工程师自动判断哪些操作可执行。",
    "plan": "只让 DeepSeek 开发工程师给方案，不实际改文件。",
    "default": "使用 DeepSeek 开发工程师默认确认策略。",
    "dontAsk": "遇到需要确认的操作直接不询问。",
    "bypassPermissions": "跳过权限检查，高风险，仅用于可信隔离环境。",
}

_ALIASES: dict[str, str] = {
    "允许编辑": "acceptEdits",
    "接受编辑": "acceptEdits",
    "编辑": "acceptEdits",
    "自动模式": "auto",
    "自动": "auto",
    "只规划": "plan",
    "规划": "plan",
    "计划": "plan",
    "默认确认": "default",
    "默认": "default",
    "不询问": "dontAsk",
    "不要问": "dontAsk",
    "跳过权限检查": "bypassPermissions",
    "跳过权限": "bypassPermissions",
    "高权限": "bypassPermissions",
}

for _mode, _label in CLAUDE_PERMISSION_MODE_LABELS.items():
    _ALIASES[_mode] = _mode
    _ALIASES[_mode.lower()] = _mode
    _ALIASES[_label] = _mode


def normalize_claude_permission_mode(value: str) -> str:
    raw = str(value).strip()
    if not raw:
        return DEFAULT_CLAUDE_PERMISSION_MODE
    mode = _ALIASES.get(raw) or _ALIASES.get(raw.lower())
    if mode is None:
        choices = "、".join(CLAUDE_PERMISSION_MODE_LABELS[m] for m in CLAUDE_PERMISSION_MODE_ORDER)
        raise ValueError(f"未知 DeepSeek 开发工程师权限模式：{raw}。可选：{choices}")
    return mode


def claude_permission_label(mode: str) -> str:
    normalized = normalize_claude_permission_mode(mode)
    return CLAUDE_PERMISSION_MODE_LABELS[normalized]


@dataclass
class ClaudePermissionState:
    _mode: str = DEFAULT_CLAUDE_PERMISSION_MODE

    def __post_init__(self) -> None:
        self._mode = normalize_claude_permission_mode(self._mode)

    def get(self) -> str:
        return self._mode

    def set(self, mode: str) -> str:
        self._mode = normalize_claude_permission_mode(mode)
        return self._mode


def render_claude_permission_status(mode: str) -> str:
    current = normalize_claude_permission_mode(mode)
    lines = [
        "DeepSeek 开发工程师权限模式",
        f"当前模式：{CLAUDE_PERMISSION_MODE_LABELS[current]}",
        "",
        "可选模式：",
    ]
    for item in CLAUDE_PERMISSION_MODE_ORDER:
        mark = "✓ " if item == current else "  "
        lines.append(
            f"{mark}{CLAUDE_PERMISSION_MODE_LABELS[item]}："
            f"{CLAUDE_PERMISSION_MODE_DESCRIPTIONS[item]}"
        )
    lines.extend([
        "",
        "发送 /claude_mode <中文模式名> 切换，例如：/claude_mode 允许编辑",
    ])
    return "\n".join(lines)


def build_claude_permission_buttons(mode: str) -> list[list[dict[str, str]]]:
    current = normalize_claude_permission_mode(mode)
    rows: list[list[dict[str, str]]] = []
    for item in CLAUDE_PERMISSION_MODE_ORDER:
        prefix = "✓ " if item == current else ""
        rows.append([
            {
                "text": f"{prefix}{CLAUDE_PERMISSION_MODE_LABELS[item]}",
                "callback_data": f"settings:claude_permission:{item}",
            }
        ])
    return rows
