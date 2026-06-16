import json

from wlcodex.relay.envelopes import default_handoff_target, parse_role_envelope


def _envelope(role: str, handoff_to: str) -> str:
    return json.dumps(
        {
            "status": "passed",
            "reason": "completed",
            "role": role,
            "artifact_type": "architecture_plan",
            "handoff_to": handoff_to,
            "summary": "Ready for next role",
            "evidence_refs": ["tests/test_relay_envelopes.py"],
            "open_questions": [],
            "next_action": "continue",
        }
    )


def test_default_handoff_transitions_match_design_spec() -> None:
    assert default_handoff_target("architect") == "implementer"
    assert default_handoff_target("implementer") == "tester"
    assert default_handoff_target("tester") == "auditor"
    assert default_handoff_target("auditor") == "director"


def test_parse_role_envelope_validates_required_fields() -> None:
    result = parse_role_envelope(_envelope("implementer", "tester"))

    assert result.ok is True
    assert result.envelope is not None
    assert result.envelope.role == "implementer"
    assert result.envelope.handoff_to == "tester"


def test_parse_role_envelope_accepts_role_envelope_wrapper() -> None:
    result = parse_role_envelope(
        {
            "role_envelope": {
                "status": "passed",
                "reason": "implemented",
                "role": "implementer",
                "artifact_type": "implementation_report",
                "handoff_to": "tester",
                "summary": "Implementation ready",
                "evidence_refs": ["x"],
                "open_questions": [],
                "next_action": "test",
            }
        }
    )

    assert result.ok is True
    assert result.envelope is not None
    assert result.envelope.summary == "Implementation ready"
    assert result.next_role == "tester"


def test_parse_role_envelope_extracts_json_from_provider_chatter() -> None:
    text = """
Let me inspect the relay files first.

```json
{
  "status": "passed",
  "reason": "implemented",
  "role": "implementer",
  "artifact_type": "implementation_report",
  "handoff_to": "tester",
  "summary": "Implementation ready",
  "evidence_refs": ["wlcodex/relay/envelopes.py"],
  "open_questions": [],
  "next_action": "测试工程师继续验证"
}
```

{"status":"passed","reason":"duplicate raw envelope","role":"implementer","artifact_type":"implementation_report","handoff_to":"tester","summary":"Duplicate raw envelope","evidence_refs":["wlcodex/relay/envelopes.py"],"open_questions":[],"next_action":"test"}
"""

    result = parse_role_envelope(text)

    assert result.ok is True
    assert result.envelope is not None
    assert result.envelope.role == "implementer"
    assert result.envelope.summary == "Implementation ready"
    assert result.next_role == "tester"


def test_invalid_role_envelope_returns_validation_error_without_advancement() -> None:
    result = parse_role_envelope('{"status": "passed", "role": "implementer"}')

    assert result.ok is False
    assert result.envelope is None
    assert result.next_role is None
    assert "missing required fields" in result.error


def test_unparseable_role_envelope_returns_validation_error() -> None:
    result = parse_role_envelope("not json")

    assert result.ok is False
    assert result.envelope is None
    assert result.next_role is None
    assert "invalid json" in result.error
