"""Inspection service tests."""

from pathlib import Path

from wlcodex.db import Ledger
from wlcodex.inspection import TaskInspector


def test_events_reads_from_sqlite(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    ledger.add_event(task.id, "test_event", {"key": "value"})

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.events(task.id)

    assert "test_event" in result.body
    assert len(result.body) < 2000


def test_events_empty_task(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.events(task.id)

    assert "暂无事件记录" in result.body


def test_tail_no_log_file(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.tail(task.id)

    assert "没有找到本地日志" in result.body


def test_tail_reads_last_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "1.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(100)))

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    inspector = TaskInspector(ledger, log_dir, tail_lines=10)
    result = inspector.tail(task.id)

    assert "line 99" in result.body


def test_files_list_touched(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)
    ledger.record_touched_file(task.id, "src/a.py", "modified")
    ledger.record_touched_file(task.id, "src/b.py", "added")

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.files(task.id)

    assert "src/a.py" in result.body
    assert "src/b.py" in result.body


def test_files_empty(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", "/tmp/demo", "Test", "thread-1", None)

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.files(task.id)

    assert "暂无文件记录" in result.body


def test_diff_no_information(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", str(tmp_path), "Test", "thread-1", None)

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.diff(task.id, workspace_path=str(tmp_path))

    assert "暂无 diff" in result.body or "没有未提交变更" in result.body


def test_tail_falls_back_to_command_output_events(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", str(tmp_path), "Test", "thread-1", None)
    ledger.add_event(task.id, "command_output", {"delta": "line from event"})

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.tail(task.id)

    assert "line from event" in result.body


def test_diff_reads_stored_diff_event(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    task = ledger.create_task("demo", str(tmp_path), "Test", "thread-1", None)
    ledger.add_event(task.id, "diff_updated", {"diff": "README.md | 1 +"})

    inspector = TaskInspector(ledger, tmp_path / "logs")
    result = inspector.diff(task.id, workspace_path=str(tmp_path))

    assert "README.md" in result.body


def test_inspection_module_does_not_import_backend() -> None:
    """Inspection must not import backend modules."""
    import inspect
    import wlcodex.inspection

    src = inspect.getsource(wlcodex.inspection)
    # Should not contain backend references
    assert "CodexBackend" not in src
