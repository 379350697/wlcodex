from __future__ import annotations


ROLE_ENVELOPE_ARTIFACT_TYPES = (
    "relay_board",
    "routing_decision",
    "role_dispatch_metadata",
    "architecture_plan",
    "implementation_report",
    "test_report",
    "audit_report",
    "handoff_packet",
    "final_summary",
)

INTERNAL_RELAY_ARTIFACT_TYPES = (
    "role_error",
    "role_resume",
    "user_attachments",
    "user_followup",
    "followup_response",
)

ALL_RELAY_ARTIFACT_TYPES = (
    *ROLE_ENVELOPE_ARTIFACT_TYPES,
    *INTERNAL_RELAY_ARTIFACT_TYPES,
)


def is_relay_artifact_type(value: str) -> bool:
    return value in ALL_RELAY_ARTIFACT_TYPES


def is_role_envelope_artifact_type(value: str) -> bool:
    return value in ROLE_ENVELOPE_ARTIFACT_TYPES
