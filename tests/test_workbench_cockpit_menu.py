"""Task 2: Cockpit Menu and Help UX tests.

Validates that the natural-profile menu exposes only daily phone-cockpit
actions and that help text uses product language, not implementation
details.
"""

from wlcodex.menu import build_bot_commands

# ── Menu shape ──────────────────────────────────────────────────────────

EXPECTED_NATURAL_MENU = ["new", "status", "terminal", "diff", "settings", "help"]

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
]

# ── Help semantics ──────────────────────────────────────────────────────

REQUIRED_HELP_PHRASES = [
    "默认流程：Codex -> Claude -> Codex",
    "当前视图：驾驶舱",
    "接管现场",
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


def test_natural_menu_has_exactly_six_daily_actions():
    """The natural menu exposes only cockpit daily actions."""
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


def test_legacy_menu_is_preserved():
    """The legacy menu must still carry the full operator command set."""
    commands = build_bot_commands(profile="legacy")
    names = [cmd[0] for cmd in commands]
    # legacy must still include the typed-direct commands
    assert "codex" in names
    assert "claude" in names
    assert "auto" in names
    assert "task" not in names  # diagnostic commands still hidden everywhere


def test_natural_help_contains_cockpit_language():
    """Natural help must describe the cockpit, workflow, and onsite entry."""
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="natural")
    for phrase in REQUIRED_HELP_PHRASES:
        assert phrase in text, f"help must contain {phrase!r}"

    assert "工作区" in text
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


def test_menu_labels_use_cockpit_product_language():
    """Descriptions in the natural menu should use cockpit language."""
    commands = build_bot_commands(profile="natural")
    cmd_map = dict(commands)
    assert cmd_map.get("terminal") == "接管现场"
    assert cmd_map.get("new") == "新工作台"
    assert cmd_map.get("status") == "状态"
    assert cmd_map.get("diff") == "变更"
    assert cmd_map.get("settings") == "设置"
    assert cmd_map.get("help") == "帮助"


def test_legacy_help_keeps_advanced_diagnostics_hidden():
    """Legacy help still avoids teaching the old task command flow."""
    from wlcodex.status import render_conversation_help

    text = render_conversation_help(profile="legacy")
    assert "/task" not in text
    assert "/continue" not in text
    assert "/steer" not in text
