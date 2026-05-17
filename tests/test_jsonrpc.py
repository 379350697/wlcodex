"""JSON-RPC client tests."""

import asyncio

import pytest
import pytest_asyncio

from wlcodex.jsonrpc import JsonRpcClient, JsonRpcError, JsonRpcTimeout


@pytest_asyncio.fixture
async def client_and_sent():
    sent: list[dict] = []

    async def fake_send(msg: dict) -> None:
        sent.append(msg)

    client = JsonRpcClient(send_json=fake_send)
    return client, sent


@pytest.mark.asyncio
async def test_request_ids_increase_monotonically(client_and_sent) -> None:
    client, sent = client_and_sent

    t1 = asyncio.create_task(client.request("test/method", {"a": 1}))
    t2 = asyncio.create_task(client.request("test/method", {"a": 2}))
    await asyncio.sleep(0)

    assert len(sent) >= 2
    assert sent[0]["id"] == 1
    assert sent[1]["id"] == 2
    assert sent[0]["id"] != sent[1]["id"]

    await client.receive_message({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    await client.receive_message({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}})

    r1, r2 = await t1, await t2
    assert r1 == {"ok": True}
    assert r2 == {"ok": True}


@pytest.mark.asyncio
async def test_successful_response_resolves_future(client_and_sent) -> None:
    client, _ = client_and_sent

    task = asyncio.create_task(client.request("thread/start", {"cwd": "/tmp"}))
    await asyncio.sleep(0)

    await client.receive_message({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"threadId": "th-42", "status": "idle"},
    })

    result = await task
    assert result["threadId"] == "th-42"


@pytest.mark.asyncio
async def test_error_response_raises_jsonrpc_error(client_and_sent) -> None:
    client, _ = client_and_sent

    task = asyncio.create_task(client.request("bad/method", {}))
    await asyncio.sleep(0)

    await client.receive_message({
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "something went wrong"},
    })

    with pytest.raises(JsonRpcError) as exc_info:
        await task
    assert exc_info.value.code == -32000
    assert "something went wrong" in str(exc_info.value)


@pytest.mark.asyncio
async def test_notification_dispatches_to_handler(client_and_sent) -> None:
    client, _ = client_and_sent
    received: list[dict] = []

    async def handler(params: dict) -> None:
        received.append(params)

    client.on_notification("turn/started", handler)

    await client.receive_message({
        "jsonrpc": "2.0",
        "method": "turn/started",
        "params": {"threadId": "th-1", "turnId": "turn-1"},
    })

    assert len(received) == 1
    assert received[0]["turnId"] == "turn-1"


@pytest.mark.asyncio
async def test_server_request_held_until_resolved(client_and_sent) -> None:
    """Server request must NOT send response until resolve_server_request is called."""
    client, sent = client_and_sent
    received_request_id: str | None = None

    async def approval_handler(params: dict, request_id: str) -> None:
        nonlocal received_request_id
        received_request_id = request_id
        # Do NOT resolve here — wait for external trigger

    client.on_server_request("item/commandExecution/requestApproval", approval_handler)

    # Start processing the server request (this will block waiting for resolution)
    asyncio.create_task(
        client.receive_message({
            "jsonrpc": "2.0",
            "id": "req-5",
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "rm -rf /"},
        })
    )

    # Let the handler fire
    await asyncio.sleep(0)
    assert received_request_id == "req-5"
    assert len(sent) == 0  # No response yet!

    # Now resolve
    client.resolve_server_request("req-5", {"decision": "accept"})
    await asyncio.sleep(0)

    assert len(sent) == 1
    assert sent[0]["id"] == "req-5"
    assert sent[0]["result"] == {"decision": "accept"}


@pytest.mark.asyncio
async def test_server_request_response_preserves_numeric_id_type(client_and_sent) -> None:
    """Codex app-server may send numeric server-request ids.

    JSON-RPC responses must echo the original id value and type; responding
    with string "0" to numeric id 0 leaves Codex waiting on approval.
    """
    client, sent = client_and_sent
    received_request_id: str | None = None

    async def approval_handler(params: dict, request_id: str) -> None:
        nonlocal received_request_id
        received_request_id = request_id

    client.on_server_request("item/commandExecution/requestApproval", approval_handler)

    asyncio.create_task(
        client.receive_message({
            "jsonrpc": "2.0",
            "id": 0,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "touch /tmp/probe"},
        })
    )
    await asyncio.sleep(0)

    assert received_request_id == "0"
    client.resolve_server_request("0", {"decision": "accept"})
    await asyncio.sleep(0)

    assert sent[0]["id"] == 0
    assert isinstance(sent[0]["id"], int)
    assert sent[0]["result"] == {"decision": "accept"}


@pytest.mark.asyncio
async def test_server_request_unknown_method_returns_error(client_and_sent) -> None:
    client, sent = client_and_sent

    await client.receive_message({
        "jsonrpc": "2.0",
        "id": "req-9",
        "method": "unknown/method",
        "params": {},
    })

    assert len(sent) == 1
    assert sent[0]["id"] == "req-9"
    assert "error" in sent[0]
    assert sent[0]["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_close_cancels_pending_and_held(client_and_sent) -> None:
    client, _ = client_and_sent

    asyncio.create_task(client.request("slow/method", {}))
    await asyncio.sleep(0)

    await client.close()
    assert len(client._pending) == 0
    assert len(client._held_requests) == 0


@pytest.mark.asyncio
async def test_request_times_out() -> None:
    """Request futures must not hang forever when no response arrives."""
    sent = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    client = JsonRpcClient(send_json=send_json, request_timeout_seconds=0.01)

    with pytest.raises(JsonRpcTimeout):
        await client.request("thread/start", {"cwd": "/tmp/work"})

    assert sent[0]["method"] == "thread/start"


@pytest.mark.asyncio
async def test_server_request_does_not_block_next_response() -> None:
    """Server request handling must not block the receive loop from
    processing normal responses."""
    sent = []

    async def send_json(message: dict) -> None:
        sent.append(message)

    client = JsonRpcClient(send_json=send_json, request_timeout_seconds=1)
    approval_handled: list[dict] = []

    async def approval_handler(params: dict, request_id: str) -> None:
        approval_handled.append(params)

    client.on_server_request(
        "item/commandExecution/requestApproval", approval_handler
    )

    # Deliver a server request — handler fires, future is created,
    # but receive_message returns immediately (non-blocking).
    receive_task = asyncio.create_task(
        client.receive_message({
            "jsonrpc": "2.0",
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
        })
    )
    await asyncio.sleep(0.05)
    # receive_message should have returned by now (non-blocking)
    assert receive_task.done()

    # A normal request should still work immediately
    rpc_task = asyncio.create_task(
        client.request("thread/start", {"cwd": "/tmp/work"})
    )
    await asyncio.sleep(0)
    await client.receive_message({
        "jsonrpc": "2.0", "id": 1, "result": {"ok": True},
    })

    assert await rpc_task == {"ok": True}

    # Resolve the held approval — response is sent via background task
    client.resolve_server_request("approval-1", {"decision": "accept"})
    await asyncio.sleep(0.05)

    # The background task should have sent the approval response
    assert any(
        m.get("id") == "approval-1" and m.get("result") == {"decision": "accept"}
        for m in sent
    ), f"Expected approval response not found in {sent}"


@pytest.mark.asyncio
async def test_reject_server_request(client_and_sent) -> None:
    client, sent = client_and_sent

    async def handler(params: dict, request_id: str) -> None:
        pass  # Will be rejected externally

    client.on_server_request("item/commandExecution/requestApproval", handler)

    asyncio.create_task(
        client.receive_message({
            "jsonrpc": "2.0",
            "id": "req-err",
            "method": "item/commandExecution/requestApproval",
            "params": {},
        })
    )
    await asyncio.sleep(0)

    client.reject_server_request("req-err", -32000, "User cancelled")
    await asyncio.sleep(0)

    assert len(sent) == 1
    assert sent[0]["id"] == "req-err"
    assert "error" in sent[0]
