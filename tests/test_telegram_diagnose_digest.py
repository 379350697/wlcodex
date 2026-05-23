"""Tests for render_auto_diagnose_digest — structured diagnose JSON to Telegram digest."""

from __future__ import annotations

import json

from wlcodex.telegram_digest import render_auto_diagnose_digest


def _sample_diagnose_json(**overrides) -> str:
    """Build a minimal valid diagnose JSON string."""
    data = {
        "schema_version": 1,
        "generated_at_ms": 1700000000000,
        "scope": {"symbol": "*"},
        "deploy_status": {
            "git_head": "abc1234",
            "deploy_version": "abc1234",
            "version_mismatch": False,
        },
        "service_status": {
            "lightfee-live": {"active": "active", "unit_exists": True},
            "lightfee-sidecar": {"active": "active", "unit_exists": True},
        },
        "health": {
            "ok": True,
            "critical_count": 0,
            "warning_count": 0,
            "fingerprints": [],
        },
        "local_state": {
            "lifecycle": "running",
            "risk_mode": "running",
            "open_position_count": 0,
            "pending_entry_count": 0,
            "pending_close_count": 0,
            "positions": [],
        },
        "exchange_truth": {
            "available": False,
            "positions": {},
            "open_orders": {},
            "errors": [],
        },
        "state_consistency": {
            "state_mismatch": False,
            "local_open_exchange_flat": False,
            "details": [],
        },
        "order_error_evidence": [],
        "l2_evidence": {
            "missing_l2_or_tick_count": 0,
            "stale_rebuild_count": 0,
            "sequence_gap_count": 0,
            "details": [],
        },
        "runtime_warnings": [],
        "evidence_completeness": {
            "overall": "complete",
            "missing_evidence": [],
            "confidence": "high",
        },
        "conclusion": {
            "status": "healthy",
            "summary": "no issues detected",
            "risk": "low",
            "next_actions": ["no immediate action required"],
        },
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Complete evidence shows exchange_code/exchange_msg
# ---------------------------------------------------------------------------


def test_complete_evidence_shows_exchange_code_and_msg():
    diagnose_json = _sample_diagnose_json(
        order_error_evidence=[
            {
                "kind": "order.rejected",
                "position_id": "pos_001",
                "symbol": "BTCUSDT",
                "venue": "binance",
                "operation": "place_order",
                "error": "HTTP 400",
                "exchange_error": {
                    "exchange_code": "-2022",
                    "exchange_msg": "ReduceOnly order is rejected",
                    "evidence_completeness": "complete",
                    "confidence": "high",
                },
                "request_context": {"symbol": "BTCUSDT", "side": "sell"},
                "count": 3,
                "first_ts_ms": 1700000001000,
                "last_ts_ms": 1700000003000,
            }
        ],
        evidence_completeness={
            "overall": "complete",
            "missing_evidence": [],
            "confidence": "high",
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    assert "-2022" in digest
    assert "ReduceOnly" in digest
    assert "binance" in digest


# ---------------------------------------------------------------------------
# Partial evidence shows missing_evidence and confidence downgrade
# ---------------------------------------------------------------------------


def test_partial_evidence_shows_missing_and_confidence():
    diagnose_json = _sample_diagnose_json(
        order_error_evidence=[
            {
                "kind": "order.rejected",
                "position_id": "pos_002",
                "symbol": "ETHUSDT",
                "venue": "gate",
                "operation": "place_order",
                "error": "HTTP 400",
                "exchange_error": {
                    "exchange_code": "",
                    "exchange_msg": "",
                    "evidence_completeness": "transport_only",
                    "missing_evidence": ["raw_body", "exchange_code_or_msg"],
                    "confidence": "low",
                },
                "request_context": {},
                "count": 1,
                "first_ts_ms": 1700000001000,
                "last_ts_ms": 1700000001000,
            }
        ],
        evidence_completeness={
            "overall": "partial",
            "missing_evidence": ["raw_body", "exchange_code_or_msg", "exchange_truth_unavailable"],
            "confidence": "medium",
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    assert "证据不完整" in digest
    assert "partial" in digest.lower() or "medium" in digest.lower()
    # Digest must warn about partial evidence, not claim it positively as high confidence
    assert "不得标记为high confidence" in digest.lower() or "证据不完整" in digest


# ---------------------------------------------------------------------------
# State mismatch shown in digest
# ---------------------------------------------------------------------------


def test_state_mismatch_shown_in_digest():
    diagnose_json = _sample_diagnose_json(
        state_consistency={
            "state_mismatch": True,
            "local_open_exchange_flat": True,
            "details": [
                {
                    "check": "local_open_exchange_flat",
                    "ok": False,
                    "detail": "local has 1 open position(s) but exchange reports no positions",
                }
            ],
        },
        local_state={
            "lifecycle": "running",
            "risk_mode": "running",
            "open_position_count": 1,
            "pending_entry_count": 0,
            "pending_close_count": 0,
            "positions": [
                {
                    "position_id": "pos_open",
                    "symbol": "BTCUSDT",
                    "long_venue": "binance",
                    "short_venue": "bybit",
                    "quantity": 0.01,
                }
            ],
        },
        conclusion={
            "status": "degraded",
            "risk": "high",
            "summary": "state mismatch detected",
            "next_actions": ["verify position on exchange"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    assert (
        "严重" in digest
        or "状态不一致" in digest
        or "local_open_exchange_flat" in digest
        or "开仓1" in digest
    )


# ---------------------------------------------------------------------------
# Empty JSON returns empty string (graceful degradation)
# ---------------------------------------------------------------------------


def test_empty_json_returns_empty():
    assert render_auto_diagnose_digest("") == ""
    assert render_auto_diagnose_digest("not json") == ""
    assert render_auto_diagnose_digest("{}") == ""


# ---------------------------------------------------------------------------
# Digest preserves health fingerprints
# ---------------------------------------------------------------------------


def test_health_warnings_shown():
    diagnose_json = _sample_diagnose_json(
        health={
            "ok": False,
            "critical_count": 1,
            "warning_count": 0,
            "fingerprints": ["lifecycle_booting"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    assert "lifecycle_booting" in digest or "异常" in digest


# ---------------------------------------------------------------------------
# Service status shown
# ---------------------------------------------------------------------------


def test_service_status_shown():
    diagnose_json = _sample_diagnose_json(
        service_status={
            "lightfee-live": {"active": "inactive", "unit_exists": False},
            "lightfee-sidecar": {"active": "active", "unit_exists": True},
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    assert "lightfee-live" in digest
    assert "inactive" in digest


# ---------------------------------------------------------------------------
# Deploy version mismatch shown
# ---------------------------------------------------------------------------


def test_deploy_version_mismatch_shown():
    diagnose_json = _sample_diagnose_json(
        deploy_status={
            "git_head": "abc1234",
            "deploy_version": "def5678",
            "version_mismatch": True,
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    assert "版本不一致" in digest or "abc1234" in digest


# ---------------------------------------------------------------------------
# Regression: partial evidence must NOT be reported as confirmed/high
# ---------------------------------------------------------------------------


def test_partial_evidence_not_reported_as_high_confidence():
    diagnose_json = _sample_diagnose_json(
        evidence_completeness={
            "overall": "partial",
            "missing_evidence": ["raw_body", "exchange_code_or_msg"],
            "confidence": "medium",
        },
        conclusion={
            "status": "degraded",
            "risk": "medium",
            "summary": "1 order error group(s); evidence: partial",
            "next_actions": ["collect full exchange error bodies"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    # Must not positively claim the evidence is high-confidence
    # Warning text like "不得标记为high confidence" is OK
    positive_high = any(
        phrase in digest.lower()
        for phrase in ("confidence: high", "confidence:high", "high confidence")
    ) and "不得标记为high confidence" not in digest.lower()
    assert not positive_high


# ---------------------------------------------------------------------------
# Regression: state_mismatch and evidence_completeness preserved
# ---------------------------------------------------------------------------


def test_state_mismatch_and_completeness_preserved():
    diagnose_json = _sample_diagnose_json(
        state_consistency={
            "state_mismatch": True,
            "local_open_exchange_flat": True,
            "details": [{"check": "local_open_exchange_flat", "ok": False}],
        },
        evidence_completeness={
            "overall": "missing",
            "missing_evidence": ["raw_body", "exchange_truth_unavailable", "state_consistency_breach"],
            "confidence": "low",
        },
        local_state={
            "lifecycle": "running",
            "risk_mode": "running",
            "open_position_count": 2,
            "pending_entry_count": 0,
            "pending_close_count": 0,
            "positions": [],
        },
        conclusion={
            "status": "degraded",
            "risk": "high",
            "summary": "state mismatch detected; evidence: missing",
            "next_actions": ["verify position on exchange", "collect full exchange error bodies"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    # State mismatch must be visible
    assert (
        "严重" in digest
        or "状态不一致" in digest
        or "开仓2" in digest
        or "state_mismatch" in digest.lower()
    )
    # Evidence incompleteness must be visible
    assert "证据不完整" in digest or "missing" in digest.lower()
    # Must warn about evidence being incomplete, not positively claim "high confidence"
    assert "不得标记为high confidence" in digest.lower() or "证据不完整" in digest or "missing" in digest.lower()


# ---------------------------------------------------------------------------
# Body missing → digest shows exchange_error_body=missing and missing evidence
# ---------------------------------------------------------------------------


def test_body_missing_is_shown_in_digest():
    """When exchange_error has no raw_body, digest shows NO body and missing evidence."""
    diagnose_json = _sample_diagnose_json(
        order_error_evidence=[
            {
                "kind": "exit.passive_close_maker_submit_error",
                "position_id": "pos_no_body",
                "symbol": "BTCUSDT",
                "venue": "binance",
                "operation": "submit_passive_order",
                "error": "HTTP 400 Bad Request",
                "exchange_error": {
                    "http_status": 400,
                    "exchange_code": "",
                    "exchange_msg": "",
                },
                "request_context": {"symbol": "BTCUSDT"},
                "http_status": 400,
                "exchange_code": "",
                "exchange_msg": "",
                "evidence_completeness": "missing_exchange_body",
                "confidence": "medium",
                "raw_body_present": False,
                "missing_evidence": ["exchange_response_body", "exchange_error_code", "exchange_error_msg"],
                "count": 5,
                "first_ts_ms": 1700000001000,
                "last_ts_ms": 1700000002000,
            }
        ],
        top_exchange_errors=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "http_status": 400,
                "exchange_code": "",
                "exchange_msg": "",
                "evidence_completeness": "missing_exchange_body",
                "raw_body_present": False,
                "missing_evidence": ["exchange_response_body", "exchange_error_code", "exchange_error_msg"],
                "count": 5,
                "last_ts_ms": 1700000002000,
            }
        ],
        evidence_completeness={
            "overall": "missing",
            "missing_evidence": ["exchange_response_body", "exchange_truth_unavailable"],
            "confidence": "low",
        },
        conclusion={
            "status": "degraded",
            "risk": "medium",
            "summary": "5 order errors; evidence: missing",
            "next_actions": ["collect full exchange error bodies"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    # Must show NO body indicator
    assert "[NO body]" in digest
    # Must show HTTP status
    assert "HTTP400" in digest.replace(" ", "")
    # Must show missing evidence
    assert "missing" in digest.lower()
    assert "证据不完整" in digest


# ---------------------------------------------------------------------------
# Multiple HTTP status codes all shown in digest
# ---------------------------------------------------------------------------


def test_multiple_http_status_codes_in_digest():
    """Digest must show different HTTP status codes, not just 400."""
    diagnose_json = _sample_diagnose_json(
        order_error_evidence=[
            {
                "kind": "order.rejected",
                "position_id": "pos_401",
                "symbol": "ETHUSDT",
                "venue": "binance",
                "http_status": 401,
                "exchange_code": "-2015",
                "exchange_msg": "Invalid API-key",
                "evidence_completeness": "complete",
                "raw_body_present": True,
                "missing_evidence": [],
                "count": 3,
                "first_ts_ms": 1700000001000,
                "last_ts_ms": 1700000002000,
            },
            {
                "kind": "order.rejected",
                "position_id": "pos_429",
                "symbol": "BTCUSDT",
                "venue": "binance",
                "http_status": 429,
                "exchange_code": "-1015",
                "exchange_msg": "Rate limit exceeded",
                "evidence_completeness": "complete",
                "raw_body_present": True,
                "missing_evidence": [],
                "count": 7,
                "first_ts_ms": 1700000003000,
                "last_ts_ms": 1700000004000,
            },
            {
                "kind": "order.uncertain",
                "position_id": "pos_503",
                "symbol": "BTCUSDT",
                "venue": "bybit",
                "http_status": 503,
                "exchange_code": "",
                "exchange_msg": "",
                "evidence_completeness": "missing_exchange_body",
                "raw_body_present": False,
                "missing_evidence": ["exchange_response_body"],
                "count": 1,
                "first_ts_ms": 1700000005000,
                "last_ts_ms": 1700000005000,
            },
        ],
        top_exchange_errors=[
            {
                "venue": "binance", "symbol": "BTCUSDT",
                "http_status": 429, "exchange_code": "-1015",
                "exchange_msg": "Rate limit exceeded",
                "evidence_completeness": "complete",
                "raw_body_present": True, "count": 7,
            },
            {
                "venue": "binance", "symbol": "ETHUSDT",
                "http_status": 401, "exchange_code": "-2015",
                "exchange_msg": "Invalid API-key",
                "evidence_completeness": "complete",
                "raw_body_present": True, "count": 3,
            },
            {
                "venue": "bybit", "symbol": "BTCUSDT",
                "http_status": 503, "exchange_code": "",
                "exchange_msg": "",
                "evidence_completeness": "missing_exchange_body",
                "raw_body_present": False,
                "missing_evidence": ["exchange_response_body"],
                "count": 1,
            },
        ],
        evidence_completeness={
            "overall": "partial",
            "missing_evidence": ["exchange_truth_unavailable"],
            "confidence": "medium",
        },
        conclusion={
            "status": "degraded",
            "risk": "medium",
            "summary": "order errors: HTTP 401, 429, 503",
            "next_actions": ["review order_error_evidence"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    # Must show at least two different HTTP status codes
    assert "HTTP401" in digest.replace(" ", "") or "401" in digest
    assert "HTTP429" in digest.replace(" ", "") or "429" in digest
    # Must show NO body for body-missing entry (503)
    assert "[NO body]" in digest

    # Evidence is partial, must warn about incomplete evidence
    assert "medium" in digest.lower() or "证据不完整" in digest or "partial" in digest


# ---------------------------------------------------------------------------
# No diagnose JSON → returns empty string (caller must show low confidence)
# ---------------------------------------------------------------------------


def test_no_diagnose_json_returns_empty():
    """When diagnose JSON is missing/unparseable, render returns empty string.

    Caller must detect this and show diagnose_json=missing, confidence=low.
    """
    # Empty string
    assert render_auto_diagnose_digest("") == ""
    # Not valid JSON
    assert render_auto_diagnose_digest("not valid json") == ""
    # Empty object
    assert render_auto_diagnose_digest("{}") == ""
    # Valid JSON but no schema_version
    assert render_auto_diagnose_digest('{"foo":"bar"}') == ""


# ---------------------------------------------------------------------------
# Digest with body present shows [body] indicator
# ---------------------------------------------------------------------------


def test_body_present_shows_body_indicator():
    """When exchange_error has raw_body, digest shows [body] indicator."""
    diagnose_json = _sample_diagnose_json(
        order_error_evidence=[
            {
                "kind": "order.rejected",
                "position_id": "pos_body",
                "symbol": "ALTUSDT",
                "venue": "binance",
                "http_status": 400,
                "exchange_code": "-2022",
                "exchange_msg": "ReduceOnly Order is rejected.",
                "evidence_completeness": "complete",
                "raw_body_present": True,
                "missing_evidence": [],
                "count": 1,
                "first_ts_ms": 1700000001000,
                "last_ts_ms": 1700000001000,
            }
        ],
        top_exchange_errors=[
            {
                "venue": "binance",
                "symbol": "ALTUSDT",
                "http_status": 400,
                "exchange_code": "-2022",
                "exchange_msg": "ReduceOnly Order is rejected.",
                "evidence_completeness": "complete",
                "raw_body_present": True,
                "count": 1,
            }
        ],
        evidence_completeness={
            "overall": "partial",
            "missing_evidence": ["exchange_truth_unavailable"],
            "confidence": "medium",
        },
        conclusion={
            "status": "degraded",
            "risk": "medium",
            "summary": "exchange error -2022 on ALTUSDT",
            "next_actions": ["review order_error_evidence"],
        },
    )

    digest = render_auto_diagnose_digest(diagnose_json)
    # Must show [body] indicator
    assert "[body]" in digest
    # Must show code and msg
    assert "-2022" in digest
    assert "ReduceOnly" in digest
