from wlcodex.live_stream.relay_artifact_lifecycle import (
    lifecycle_status_for_payload,
    payload_status_is_success,
    payload_status_is_terminal_failure,
)


def test_lifecycle_status_interprets_legacy_final_summary_read_only() -> None:
    payload = {
        "role": "director",
        "artifact_type": "final_summary",
        "status": "waiting",
    }

    assert lifecycle_status_for_payload(payload, {"auditor"}) == "passed"
    assert payload["status"] == "waiting"


def test_lifecycle_status_does_not_hide_real_handoff_or_failure() -> None:
    assert lifecycle_status_for_payload(
        {
            "role": "director",
            "artifact_type": "final_summary",
            "status": "waiting",
            "handoff_to": "implementer",
        },
        {"auditor"},
    ) == "waiting"
    assert payload_status_is_success("completed")
    assert payload_status_is_terminal_failure("blocked")
