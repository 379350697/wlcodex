from __future__ import annotations

from wlcodex.team_model_settings import (
    encode_assignment,
    load_runtime_assignments,
    runtime_assignment_key,
)


class FakeLedger:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get_runtime_setting(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


def test_load_runtime_assignments_restores_role_models() -> None:
    defaults = {
        "director": ("codex_gpt",),
        "implementer": ("claude_deepseek", "codex_gpt"),
        "auditor": ("codex_gpt",),
    }
    profiles = {"codex_gpt": "codex", "claude_deepseek": "claude"}
    ledger = FakeLedger({
        runtime_assignment_key("implementer"): encode_assignment(("codex_gpt",)),
        runtime_assignment_key("auditor"): encode_assignment(("claude_deepseek",)),
    })

    assignments = load_runtime_assignments(ledger, defaults, profiles)

    assert assignments["director"] == ("codex_gpt",)
    assert assignments["implementer"] == ("codex_gpt",)
    assert assignments["auditor"] == ("claude_deepseek",)


def test_load_runtime_assignments_ignores_invalid_or_unknown_profiles() -> None:
    defaults = {
        "implementer": ("claude_deepseek", "codex_gpt"),
        "auditor": ("codex_gpt",),
    }
    profiles = {"codex_gpt": "codex", "claude_deepseek": "claude"}
    ledger = FakeLedger({
        runtime_assignment_key("implementer"): '["unknown"]',
        runtime_assignment_key("auditor"): '"not-a-list"',
    })

    assignments = load_runtime_assignments(ledger, defaults, profiles)

    assert assignments["implementer"] == ("claude_deepseek", "codex_gpt")
    assert assignments["auditor"] == ("codex_gpt",)
