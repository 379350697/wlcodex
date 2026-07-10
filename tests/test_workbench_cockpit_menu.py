"""Telegram compatibility-menu and help-contract tests."""

from wlcodex.menu import build_bot_commands

# ── Menu shape ──────────────────────────────────────────────────────────

EXPECTED_NATURAL_MENU = [
    "native", "relay", "new", "help",
]

HIDDEN_FROM_NATURAL = [
    "codex",
    "claude",
    "auto",
    "model",
    "claude_mode",
    "sessions",
    "health",
    "files",
    "stop",
    "switch",
    "verify",
    "terminal",
    "settings",
]

# ── Help semantics ──────────────────────────────────────────────────────

REQUIRED_HELP_PHRASES = [
    "Telegram 历史兼容入口",
    "/native",
    "/relay",
    "不会为新消息\n创建旧 Workbench 主状态",
]

FORBIDDEN_HELP_PHRASES = [
    "terminal.enabled",
    "external_session_id",
    "新任务",
    "任务 #",
    "/task",
    "/continue",
    "/steer",
]


def test_natural_menu_has_only_primary_surface_actions():
    commands = build_bot_commands(profile="natural")
    names = [cmd[0] for cmd in commands]
    assert names == EXPECTED_NATURAL_MENU


def test_natural_menu_accepts_no_command_prefix():
    """Bot menu entries must not include a leading slash."""
    commands = build_bot_commands(profile="natural")
    for cmd, desc in commands:
        assert cmd, f"empty command paired with {desc!r}"
        assert desc, f"empty description for {cmd!r}"
        assert not cmd.startswith("/"), f"{cmd!r} starts with /"


def test_natural_menu_hides_typed_only_commands():
    """Commands that remain as typed-only should not appear in the menu."""
    commands = build_bot_commands(profile="natural")
    names = [cmd[0] for cmd in commands]
    for hidden in HIDDEN_FROM_NATURAL:
        assert hidden not in names, f"{hidden!r} must be hidden from natural menu"


def test_legacy_menu_uses_the_same_global_compatibility_boundary():
    commands = build_bot_commands(profile="legacy")
    names = [cmd[0] for cmd in commands]
    assert names == EXPECTED_NATURAL_MENU


def test_natural_help_describes_the_compatibility_boundary():
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="natural")
    for phrase in REQUIRED_HELP_PHRASES:
        assert phrase in text, f"help must contain {phrase!r}"

    assert "WLCodex" in text


def test_natural_help_does_not_leak_implementation_keys():
    """Natural help must never expose terminal.enabled or session ids."""
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="natural")
    for phrase in FORBIDDEN_HELP_PHRASES:
        assert phrase not in text, f"help must NOT contain {phrase!r}"

    assert "session id" not in text.lower()
    assert "thread id" not in text.lower()


def test_natural_help_does_not_reference_product_terminal_split():
    """Natural help must not talk about /product or /terminal as dual modes."""
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="natural")
    assert "双面模式" not in text
    assert "产品模式" not in text
    assert "远程终端模式" not in text
    # /terminal remains a command but the narrative is "接管现场"
    # /product should also not be the primary framing
    assert "手机端模式" not in text


def test_menu_labels_describe_the_real_primary_surfaces():
    commands = build_bot_commands(profile="natural")
    cmd_map = dict(commands)
    assert cmd_map.get("native") == "开始直接会话"
    assert cmd_map.get("relay") == "创建协作任务"
    assert cmd_map.get("new") == "打开新入口"
    assert cmd_map.get("help") == "兼容说明"


def test_legacy_help_preserves_recoverable_historical_actions():
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="legacy")
    assert "/task" not in text
    assert "/continue" not in text
    assert "/steer" not in text
    assert "/auto <提示>" in text
    assert "/codex <提示>" in text
    assert "/terminal" in text
