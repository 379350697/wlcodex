from pathlib import Path

from wlcodex.config import WorkspaceConfig
from wlcodex.db import Ledger
from wlcodex.execution_scheduler import ExecutionScheduler, RunIntent
from wlcodex.task_service import TaskService


def test_scheduler_reserves_internal_execution_lease(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    service = TaskService(
        ledger,
        (WorkspaceConfig("demo", Path("/tmp/demo"), True),),
    )
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="Workbench",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    scheduler = ExecutionScheduler(service, ledger)

    lease = scheduler.reserve(RunIntent(
        conversation_id=conversation.id,
        workspace_alias="demo",
        prompt="Implement feature",
        telegram_chat_id=123,
        purpose="codex_analysis",
    ))

    task = service.get_task(lease.hidden_task_id)
    refreshed = ledger.get_conversation(conversation.id)
    assert lease.workspace_alias == "demo"
    assert lease.prompt == "Implement feature"
    assert task.title == "Implement feature"
    assert refreshed.active_codex_task_id == lease.hidden_task_id
