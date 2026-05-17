"""Tests for waiting callback encode/decode and worktree done callbacks."""

from wlcodex.waiting_callback import (
    ABORT_BLOCKER_START_NEXT,
    FORCE_PARALLEL_CONFIRM,
    FORCE_PARALLEL_REQUEST,
    KEEP,
    SHOW_BLOCKER,
    WORKTREE_DIFF,
    WORKTREE_DISCARD,
    WORKTREE_ISOLATED,
    WORKTREE_KEEP,
    WORKTREE_MERGE,
    WaitingCallback,
    decode_waiting_callback,
    decode_worktree_done_callback,
    encode_waiting_callback,
    encode_worktree_done_callback,
)


def test_encode_decode_waiting_keep() -> None:
    data = encode_waiting_callback(42, KEEP)
    cb = decode_waiting_callback(data)
    assert cb is not None
    assert cb.task_id == 42
    assert cb.action == KEEP


def test_encode_decode_waiting_show_blocker() -> None:
    data = encode_waiting_callback(7, SHOW_BLOCKER)
    cb = decode_waiting_callback(data)
    assert cb is not None
    assert cb.task_id == 7
    assert cb.action == SHOW_BLOCKER


def test_encode_decode_waiting_abort_blocker() -> None:
    data = encode_waiting_callback(3, ABORT_BLOCKER_START_NEXT)
    cb = decode_waiting_callback(data)
    assert cb is not None
    assert cb.task_id == 3
    assert cb.action == ABORT_BLOCKER_START_NEXT


def test_encode_decode_waiting_force_parallel_request() -> None:
    data = encode_waiting_callback(5, FORCE_PARALLEL_REQUEST)
    cb = decode_waiting_callback(data)
    assert cb is not None
    assert cb.task_id == 5
    assert cb.action == FORCE_PARALLEL_REQUEST


def test_encode_decode_waiting_force_parallel_confirm() -> None:
    data = encode_waiting_callback(5, FORCE_PARALLEL_CONFIRM)
    cb = decode_waiting_callback(data)
    assert cb is not None
    assert cb.task_id == 5
    assert cb.action == FORCE_PARALLEL_CONFIRM


def test_encode_decode_waiting_worktree_isolated() -> None:
    data = encode_waiting_callback(8, WORKTREE_ISOLATED)
    cb = decode_waiting_callback(data)
    assert cb is not None
    assert cb.task_id == 8
    assert cb.action == WORKTREE_ISOLATED


def test_decode_approval_callback_returns_none_from_waiting() -> None:
    """Approval callback data should not be decoded as waiting callback."""
    from wlcodex.approval import encode_approval_callback
    approval_data = encode_approval_callback(1, "approve_once")
    assert decode_waiting_callback(approval_data) is None
    assert decode_worktree_done_callback(approval_data) is None


def test_decode_waiting_returns_none_from_approval_decoder() -> None:
    """Waiting callback data should not be decoded as approval callback."""
    from wlcodex.approval import decode_approval_callback
    waiting_data = encode_waiting_callback(1, KEEP)
    assert decode_approval_callback(waiting_data) is None


def test_decode_invalid_data_returns_none() -> None:
    assert decode_waiting_callback("") is None
    assert decode_waiting_callback("garbage") is None
    assert decode_waiting_callback("waiting") is None
    assert decode_waiting_callback("waiting:abc:keep") is None  # non-int task_id
    assert decode_worktree_done_callback("") is None
    assert decode_worktree_done_callback("worktree_done:1") is None


def test_encode_decode_worktree_done_diff() -> None:
    data = encode_worktree_done_callback(10, WORKTREE_DIFF)
    cb = decode_worktree_done_callback(data)
    assert cb is not None
    assert cb.task_id == 10
    assert cb.action == WORKTREE_DIFF


def test_encode_decode_worktree_done_merge() -> None:
    data = encode_worktree_done_callback(10, WORKTREE_MERGE)
    cb = decode_worktree_done_callback(data)
    assert cb is not None
    assert cb.action == WORKTREE_MERGE


def test_encode_decode_worktree_done_discard() -> None:
    data = encode_worktree_done_callback(10, WORKTREE_DISCARD)
    cb = decode_worktree_done_callback(data)
    assert cb is not None
    assert cb.action == WORKTREE_DISCARD


def test_encode_decode_worktree_done_keep() -> None:
    data = encode_worktree_done_callback(10, WORKTREE_KEEP)
    cb = decode_worktree_done_callback(data)
    assert cb is not None
    assert cb.action == WORKTREE_KEEP


def test_waiting_callback_is_frozen() -> None:
    cb = WaitingCallback(task_id=1, action=KEEP)
    assert cb.task_id == 1
    assert cb.action == KEEP
