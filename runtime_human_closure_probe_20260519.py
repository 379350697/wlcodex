def format_runtime_probe_status(name: str, passed: bool) -> str:
    name = name.strip()
    if not name:
        name = "runtime-probe"
    return f"{name}: {'PASS' if passed else 'FAIL'}"
