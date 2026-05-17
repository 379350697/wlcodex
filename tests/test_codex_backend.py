import pytest

from wlcodex.codex_backend import FakeCodexBackend


@pytest.mark.asyncio
async def test_fake_backend_creates_unique_threads() -> None:
    backend = FakeCodexBackend()

    first = await backend.create_thread("/tmp/demo")
    second = await backend.create_thread("/tmp/demo")

    assert first != second


@pytest.mark.asyncio
async def test_fake_backend_records_turns_without_status_noise() -> None:
    backend = FakeCodexBackend()
    thread_id = await backend.create_thread("/tmp/demo")

    await backend.start_turn(thread_id, "Fix bug")

    assert backend.turns == [(thread_id, "Fix bug")]
