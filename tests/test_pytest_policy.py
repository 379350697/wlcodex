from __future__ import annotations

from pathlib import Path
import tomllib


def test_default_pytest_policy_runs_integration_but_excludes_slow_and_live() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    pytest_options = data["tool"]["pytest"]["ini_options"]

    addopts = pytest_options["addopts"]
    assert "not slow" in addopts
    assert "not integration" not in addopts
    assert "not live" in addopts

    markers = "\n".join(pytest_options["markers"])
    assert "slow:" in markers
    assert "integration:" in markers
    assert "live:" in markers


def test_pytest_profile_script_uses_native_pytest_and_full_marker_selection() -> None:
    script = Path("scripts/pytest-profile")
    text = script.read_text()

    assert ".venv/bin/python -m pytest" in text
    assert "-m \"\"" in text
    assert "--durations" in text
    assert "rtk pytest" not in text
