"""Run-bound, shell-free goal acceptance test execution.

Provider output is never treated as a command string.  The only accepted
input is the normalized ``goal_acceptance.test`` structure produced by
``normalize_goal_acceptance_declaration``.  This module translates that small
allow-list into argv and executes it in the task workspace with ``shell=False``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import Any

from wlcodex.runtime_events import redact_text_content


_SHELL_CONTROL_CHARS = frozenset(";|&><`$")
_PYTEST_FLAGS = frozenset(
    {
        "-q",
        "-v",
        "-vv",
        "-x",
        "-s",
        "--disable-warnings",
        "--no-header",
        "--no-summary",
    }
)
_PYTEST_VALUE_FLAGS = frozenset({"-k"})


class ControlledGoalTestExecutor:
    """Execute a deliberately small, workspace-confined test declaration."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 300.0,
        max_output_chars: int = 12_000,
    ) -> None:
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._max_output_chars = max(1_000, int(max_output_chars))

    async def execute(
        self,
        *,
        workspace: str,
        declaration: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a durable result rather than raising for unsafe declarations."""

        try:
            cwd, argv = self._build_argv(workspace=workspace, declaration=declaration)
        except ValueError as exc:
            return _not_run_result(str(exc))
        return await asyncio.to_thread(self._run, cwd, argv)

    def _build_argv(
        self,
        *,
        workspace: str,
        declaration: dict[str, Any],
    ) -> tuple[Path, list[str]]:
        raw_workspace = str(workspace or "").strip()
        if not raw_workspace:
            raise ValueError("goal acceptance workspace is missing")
        cwd = Path(raw_workspace).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError("goal acceptance workspace is not an existing directory")
        raw_test = declaration.get("test") if isinstance(declaration, dict) else None
        if not isinstance(raw_test, dict):
            raise ValueError("goal acceptance test declaration is missing")
        kind = str(raw_test.get("kind") or "").strip()
        args = raw_test.get("args")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("goal acceptance test args are invalid")
        clean_args = [arg.strip() for arg in args]
        for arg in clean_args:
            _assert_shell_free(arg)
        if kind == "pytest":
            _validate_pytest_args(cwd, clean_args)
            return cwd, [sys.executable, "-m", "pytest", *clean_args]
        if kind == "unittest":
            _validate_unittest_args(cwd, clean_args)
            return cwd, [sys.executable, "-m", "unittest", *clean_args]
        if kind in {"npm_test", "pnpm_test"}:
            if clean_args:
                raise ValueError("package-manager goal tests do not accept free-form args")
            script = str(raw_test.get("script") or "").strip()
            if script not in {"test", "test:e2e"}:
                raise ValueError("package-manager goal tests require an approved script")
            if not (cwd / "package.json").is_file():
                raise ValueError("package-manager test requires package.json in the task workspace")
            executable = "npm" if kind == "npm_test" else "pnpm"
            return cwd, [executable, "run", script]
        raise ValueError("goal acceptance test kind is not approved")

    def _run(self, cwd: Path, argv: list[str]) -> dict[str, Any]:
        started_at = _now_text()
        env = os.environ.copy()
        # A parent process can otherwise smuggle arbitrary pytest switches
        # (including an external config path) around the structured contract.
        env["PYTEST_ADDOPTS"] = ""
        env.pop("PYTHONSTARTUP", None)
        env.pop("PYTHONINSPECT", None)
        # ``subprocess.run(capture_output=True)`` retains unbounded output in
        # RAM before a caller can truncate it.  Drain each pipe concurrently
        # into a bounded buffer instead, so a noisy test cannot exhaust the
        # Relay worker while still leaving useful diagnostic evidence.
        output_limit_bytes = self._max_output_chars * 4
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_truncated = [False]
        stderr_truncated = [False]
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                shell=False,
                close_fds=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as exc:
            return _not_run_result(
                f"approved test executable is unavailable: {exc.filename or argv[0]}",
                argv=argv,
                started_at=started_at,
            )
        except OSError as exc:
            return _not_run_result(
                f"approved test could not start: {exc}",
                argv=argv,
                started_at=started_at,
            )
        if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
            _terminate_process_tree(process)
            return _not_run_result(
                "approved test did not expose capture pipes",
                argv=argv,
                started_at=started_at,
            )
        stdout_thread = threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout_buffer, stdout_truncated, output_limit_bytes),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr_buffer, stderr_truncated, output_limit_bytes),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            process.wait(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            process.wait()
        finally:
            stdout_thread.join()
            stderr_thread.join()
        exit_code = int(process.returncode) if process.returncode is not None else None
        if timed_out:
            return {
                "executed": True,
                "status": "failed",
                "argv": argv,
                "started_at": started_at,
                "finished_at": _now_text(),
                "timed_out": True,
                "exit_code": exit_code,
                "stdout": _clip_output(
                    bytes(stdout_buffer),
                    self._max_output_chars,
                    truncated=stdout_truncated[0],
                ),
                "stderr": _clip_output(
                    bytes(stderr_buffer),
                    self._max_output_chars,
                    truncated=stderr_truncated[0],
                ),
                "reason": f"approved test timed out after {self._timeout_seconds:g}s",
            }
        return {
            "executed": True,
            "status": "passed" if exit_code == 0 else "failed",
            "argv": argv,
            "started_at": started_at,
            "finished_at": _now_text(),
            "timed_out": False,
            "exit_code": exit_code,
            "stdout": _clip_output(
                bytes(stdout_buffer),
                self._max_output_chars,
                truncated=stdout_truncated[0],
            ),
            "stderr": _clip_output(
                bytes(stderr_buffer),
                self._max_output_chars,
                truncated=stderr_truncated[0],
            ),
            "reason": "" if exit_code == 0 else f"approved test exited with {exit_code}",
        }


def _validate_pytest_args(cwd: Path, args: list[str]) -> None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _PYTEST_FLAGS:
            index += 1
            continue
        if arg in _PYTEST_VALUE_FLAGS:
            if index + 1 >= len(args):
                raise ValueError(f"pytest option {arg} requires a value")
            _assert_shell_free(args[index + 1])
            index += 2
            continue
        if arg.startswith("--maxfail="):
            value = arg.partition("=")[2]
            if not value.isdigit() or int(value) < 1:
                raise ValueError("pytest --maxfail must be a positive integer")
            index += 1
            continue
        if arg.startswith("-"):
            raise ValueError(f"pytest option is not approved: {arg}")
        _assert_workspace_target(cwd, arg)
        index += 1


def _validate_unittest_args(cwd: Path, args: list[str]) -> None:
    if not args:
        return
    if args[0] != "discover":
        raise ValueError("unittest goal tests only allow discovery mode")
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in {"-v", "-q"}:
            index += 1
            continue
        if arg in {"-s", "-t"}:
            if index + 1 >= len(args):
                raise ValueError(f"unittest option {arg} requires a workspace path")
            _assert_workspace_target(cwd, args[index + 1])
            index += 2
            continue
        if arg == "-p":
            if index + 1 >= len(args):
                raise ValueError("unittest option -p requires a pattern")
            pattern = args[index + 1]
            _assert_shell_free(pattern)
            if "/" in pattern or "\\" in pattern or ".." in pattern:
                raise ValueError("unittest discovery pattern may not escape the workspace")
            index += 2
            continue
        raise ValueError(f"unittest argument is not approved: {arg}")


def _assert_workspace_target(cwd: Path, raw_target: str) -> None:
    target = str(raw_target or "").strip()
    _assert_shell_free(target)
    path_text = target.split("::", 1)[0]
    if not path_text:
        raise ValueError("test target is missing")
    path = Path(path_text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("test target must remain inside the task workspace")
    candidate = (cwd / path).resolve()
    try:
        candidate.relative_to(cwd)
    except ValueError as exc:
        raise ValueError("test target escapes the task workspace") from exc


def _assert_shell_free(value: str) -> None:
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("test declaration contains a control character")
    if any(char in _SHELL_CONTROL_CHARS for char in value):
        raise ValueError("test declaration contains shell or redirection syntax")


def _drain_bounded(
    stream: Any,
    buffer: bytearray,
    truncated: list[bool],
    limit: int,
) -> None:
    """Drain a subprocess pipe without retaining unbounded provider-visible output."""

    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            data = bytes(chunk)
            remaining = max(0, limit - len(buffer))
            if remaining:
                buffer.extend(data[:remaining])
            if len(data) > remaining:
                truncated[0] = True
    except OSError:
        truncated[0] = True
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _not_run_result(
    reason: str,
    *,
    argv: list[str] | None = None,
    started_at: str = "",
) -> dict[str, Any]:
    now = _now_text()
    return {
        "executed": False,
        "status": "not_run",
        "argv": list(argv or []),
        "started_at": started_at or now,
        "finished_at": now,
        "timed_out": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "reason": str(reason or "approved test was not run"),
    }


def _clip_output(
    value: str | bytes | None,
    limit: int,
    *,
    truncated: bool = False,
) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    # Test output is retained as evidence.  Apply the same content-level
    # redaction used for runtime events before it becomes a durable artifact.
    text = redact_text_content(text)
    if len(text) <= limit and not truncated:
        return text
    return text[:limit] + "\n[output truncated]"


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
