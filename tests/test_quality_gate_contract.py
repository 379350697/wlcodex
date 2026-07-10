from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_default_pytest_gate_includes_integration_but_excludes_slow_and_live() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]

    assert "not slow" in addopts
    assert "not live" in addopts
    assert "not integration" not in addopts


def test_ci_uses_the_default_quality_gate_and_ruff() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert 'pytest -q -m "not slow and not live"' in workflow
    assert "ruff check ." in workflow


def test_current_docs_state_native_relay_navigation_and_telegram_boundary() -> None:
    readme = (ROOT / "README.md").read_text()
    contract = (ROOT / "docs" / "product-semantics.md").read_text()

    assert "/native  →  /native/workflows  →  /native/workflows/relay" in readme
    assert "legacy_compatible" in readme
    assert "不改变**公网环境的 token/cookie/认证传递方式" in contract
    assert "hot_retention_days = 7" in readme
    assert "archive_retention_days = 90" in readme


def test_historical_specs_and_reviews_are_explicitly_superseded() -> None:
    for directory in (ROOT / "docs" / "superpowers" / "specs", ROOT / "docs" / "superpowers" / "reviews"):
        for document in directory.glob("20*.md"):
            assert "SUPERSEDED" in document.read_text(), document
