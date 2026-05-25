import pytest

from wlcodex.claude_permissions import (
    CLAUDE_PERMISSION_MODE_LABELS,
    ClaudePermissionState,
    normalize_claude_permission_mode,
    render_claude_permission_status,
)


def test_normalize_claude_permission_mode_accepts_chinese_names() -> None:
    assert normalize_claude_permission_mode("允许编辑") == "acceptEdits"
    assert normalize_claude_permission_mode("自动模式") == "auto"
    assert normalize_claude_permission_mode("只规划") == "plan"
    assert normalize_claude_permission_mode("默认确认") == "default"
    assert normalize_claude_permission_mode("不询问") == "dontAsk"
    assert normalize_claude_permission_mode("跳过权限检查") == "bypassPermissions"


def test_claude_permission_state_rejects_unknown_mode() -> None:
    state = ClaudePermissionState("允许编辑")

    with pytest.raises(ValueError, match="未知 DeepSeek 开发工程师权限模式"):
        state.set("随便执行")

    assert state.get() == "acceptEdits"


def test_render_claude_permission_status_uses_chinese_labels_only() -> None:
    text = render_claude_permission_status("acceptEdits")

    assert "当前模式：允许编辑" in text
    for mode in CLAUDE_PERMISSION_MODE_LABELS:
        assert mode not in text
