from runtime_human_closure_probe_20260519 import format_runtime_probe_status


def test_passed():
    assert format_runtime_probe_status("smoke", True) == "smoke: PASS"


def test_failed():
    assert format_runtime_probe_status("smoke", False) == "smoke: FAIL"


def test_whitespace_name():
    assert format_runtime_probe_status("  hello  ", True) == "hello: PASS"


def test_empty_name():
    assert format_runtime_probe_status("", True) == "runtime-probe: PASS"


def test_empty_name_fail():
    assert format_runtime_probe_status("   ", False) == "runtime-probe: FAIL"
