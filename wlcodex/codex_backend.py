"""Codex app-server backend — typed protocol, fake backend, and real implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
import logging
import uuid

from wlcodex.jsonrpc import JsonRpcClient

logger = logging.getLogger(__name__)

_PLANNING_THREAD_CONFIG: dict[str, object] = {
    "model": "gpt-5.5",
    "model_reasoning_effort": "xhigh",
    "model_reasoning_summary": "none",
    "model_verbosity": "high",
}

_CHIEF_ENGINEER_SANDBOX_POLICY: dict[str, object] = {
    "type": "workspaceWrite",
    "networkAccess": False,
    "writableRoots": [],
}

_CODEX_ANALYSIS_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "summary",
        "needs_implementation",
        "files_to_touch",
        "implementation_steps",
        "acceptance_criteria",
        "prohibited_changes",
    ],
    "properties": {
        "summary": {"type": "string"},
        "needs_implementation": {"type": "boolean"},
        "files_to_touch": {"type": "array", "items": {"type": "string"}},
        "implementation_steps": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "prohibited_changes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _planning_developer_instructions(interaction_mode: str) -> str:
    if interaction_mode == "verification":
        return (
            "你是 WLCodex 的 Codex 总工程师验收子流程。可以调用 skill、"
            "GitNexus、只读检索和必要的验收/测试/部署/review 工具；可以生成或写入"
            "设计、评审、部署、验收类文档。不要抢 Claude 的代码实现职责："
            "不要修改业务代码、测试代码、依赖锁或配置来完成实现补丁；发现实现"
            "问题时输出 retry/required_fix 交给 Claude 返工。验收结论必须使用 "
            "decision: pass/retry/stop/need_user 格式。"
        )
    return (
        "你是 WLCodex 的 Codex 总工程师分析子流程。可以调用 skill、"
        "GitNexus、只读上下文检索和必要的方案验证工具；可以生成或写入设计、"
        "评审、部署、验收类文档。不要抢 Claude 的代码实现职责：不要修改业务"
        "代码、测试代码、依赖锁或配置来完成实现补丁，不要运行由 Claude 实现"
        "阶段负责的改代码/跑实现测试闭环。输出交给 Claude 执行的结构化实现"
        "交接包。"
    )


def _planning_turn_options(interaction_mode: str) -> dict[str, object]:
    options: dict[str, object] = {
        "effort": "xhigh",
        "approval_policy": "on-request",
        "sandbox_policy": _CHIEF_ENGINEER_SANDBOX_POLICY,
        "model": "gpt-5.5",
        "summary": "none",
        "personality": "pragmatic",
        "service_tier": "fast",
    }
    if interaction_mode == "analysis":
        options["output_schema"] = _CODEX_ANALYSIS_OUTPUT_SCHEMA
    return options


# ---------------------------------------------------------------------------
# Protocol helpers — encode/decode app-server JSON-RPC payloads
# ---------------------------------------------------------------------------


def _text_input(prompt: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": prompt}]


def build_thread_start_params(
    workspace_path: str,
    approval_policy: str,
    sandbox: str,
    *,
    developer_instructions: str | None = None,
    base_instructions: str | None = None,
    config: dict[str, object] | None = None,
    model: str | None = None,
    personality: str | None = None,
    service_tier: str | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {
        "cwd": workspace_path,
        "approvalPolicy": approval_policy,
        "sandbox": sandbox,
    }
    if developer_instructions is not None:
        params["developerInstructions"] = developer_instructions
    if base_instructions is not None:
        params["baseInstructions"] = base_instructions
    if config is not None:
        params["config"] = config
    if model is not None:
        params["model"] = model
    if personality is not None:
        params["personality"] = personality
    if service_tier is not None:
        params["serviceTier"] = service_tier
    return params


def build_turn_start_params(
    thread_id: str,
    prompt: str,
    *,
    effort: str | None = None,
    approval_policy: str | None = None,
    sandbox_policy: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
    model: str | None = None,
    summary: str | None = None,
    personality: str | None = None,
    service_tier: str | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {"threadId": thread_id, "input": _text_input(prompt)}
    if effort is not None:
        params["effort"] = effort
    if approval_policy is not None:
        params["approvalPolicy"] = approval_policy
    if sandbox_policy is not None:
        params["sandboxPolicy"] = sandbox_policy
    if output_schema is not None:
        params["outputSchema"] = output_schema
    if model is not None:
        params["model"] = model
    if summary is not None:
        params["summary"] = summary
    if personality is not None:
        params["personality"] = personality
    if service_tier is not None:
        params["serviceTier"] = service_tier
    return params


def build_turn_steer_params(
    thread_id: str, expected_turn_id: str, prompt: str
) -> dict[str, object]:
    return {
        "threadId": thread_id,
        "expectedTurnId": expected_turn_id,
        "input": _text_input(prompt),
    }


def parse_thread_start_response(result: dict[str, object]) -> str:
    thread = result.get("thread")
    if isinstance(thread, dict) and thread.get("id"):
        return str(thread["id"])
    if result.get("threadId"):
        return str(result["threadId"])
    if result.get("id"):
        return str(result["id"])
    raise RuntimeError(f"thread/start response missing thread id: {result}")


def parse_turn_response(result: dict[str, object]) -> str:
    turn = result.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    if result.get("turnId"):
        return str(result["turnId"])
    if result.get("id"):
        return str(result["id"])
    raise RuntimeError(f"turn response missing turn id: {result}")


def parse_turn_notification_ids(payload: dict[str, object]) -> tuple[str, str]:
    thread_id = str(payload.get("threadId", ""))
    turn = payload.get("turn")
    turn_id = ""
    if isinstance(turn, dict):
        turn_id = str(turn.get("id", ""))
    if not turn_id:
        turn_id = str(payload.get("turnId", ""))
    if not thread_id or not turn_id:
        raise RuntimeError(f"turn notification missing ids: {payload}")
    return thread_id, turn_id


def build_approval_response(
    *,
    kind,
    action: str,
    requested_permissions: dict[str, object],
    allow_session: bool,
) -> dict[str, object]:
    kind_value = kind.value if hasattr(kind, "value") else str(kind)
    if kind_value == "permissions":
        scope = "session" if action == "approve_session" and allow_session else "turn"
        if action in ("approve_once", "approve_session"):
            return {"permissions": requested_permissions, "scope": scope}
        return {"permissions": {}, "scope": "turn"}

    decision_map = {
        "approve_once": "accept",
        "approve_session": "acceptForSession" if allow_session else "accept",
        "deny": "decline",
        "cancel": "cancel",
    }
    return {"decision": decision_map[action]}


def build_legacy_approval_response(
    *, action: str, allow_session: bool
) -> dict[str, object]:
    """Build the deprecated Codex review-decision approval shape."""
    decision_map = {
        "approve_once": "approved",
        "approve_session": "approved_for_session" if allow_session else "approved",
        "deny": "denied",
        "cancel": "abort",
    }
    return {"decision": decision_map[action]}


# ---------------------------------------------------------------------------
# Typed events the backend emits to consumers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendEvent:
    event_type: str
    payload: dict[str, object]


# ---------------------------------------------------------------------------
# Fake backend — deterministic for testing
# ---------------------------------------------------------------------------


class FakeCodexBackend:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.steers: list[tuple[str, str, str]] = []
        self.threads: dict[str, str] = {}
        self._injected_events: list[BackendEvent] = []
        self._turn_counter: int = 0
        self._approval_resolutions: list[tuple[str, str]] = []
        self._interrupts: list[tuple[str, str]] = []
        self._archive_thread_ids: list[str] = []
        self._force_health_error: str | None = None
        # Held server requests for approval flow testing
        self._held_approval_requests: dict[str, dict] = {}
        # Programmable responses for send_codex_prompt
        self._codex_responses: list[str] = []
        self._codex_call_count: int = 0

    async def create_thread(self, workspace_path: str) -> str:
        tid = f"fake-{uuid.uuid4()}"
        self.threads[tid] = workspace_path
        return tid

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        self._turn_counter += 1
        turn_id = f"fake-turn-{self._turn_counter}"
        self.turns.append((thread_id, prompt))
        return turn_id

    async def continue_turn(self, thread_id: str, prompt: str) -> str:
        self._turn_counter += 1
        turn_id = f"fake-turn-{self._turn_counter}"
        self.turns.append((thread_id, prompt))
        return turn_id

    async def steer_turn(self, thread_id: str, expected_turn_id: str, prompt: str) -> None:
        self.steers.append((thread_id, expected_turn_id, prompt))

    async def steer_thread(self, thread_id: str, text: str) -> None:
        """Send raw terminal input to an active Codex thread (terminal surface).

        V1 stub — real implementation will route through app-server
        or exec --json stream.
        """
        self.steers.append((thread_id, "", text))

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self._interrupts.append((thread_id, turn_id))

    async def fork_thread(self, thread_id: str, workspace_path: str) -> str:
        tid = f"fake-fork-{uuid.uuid4()}"
        self.threads[tid] = workspace_path
        return tid

    async def archive_thread(self, thread_id: str) -> None:
        self._archive_thread_ids.append(thread_id)

    async def resolve_approval(self, codex_request_id: str, response: dict[str, object]) -> None:
        self._approval_resolutions.append((codex_request_id, response))

    def hold_approval_request(self, codex_request_id: str, params: dict) -> None:
        """Simulate a held server request (for testing approval flow)."""
        self._held_approval_requests[codex_request_id] = params

    def health(self) -> object:
        from wlcodex.app_server_process import BackendHealth
        if self._force_health_error:
            return BackendHealth(
                process_alive=False,
                websocket_connected=False,
                error=self._force_health_error,
            )
        return BackendHealth(process_alive=True, websocket_connected=True)

    def inject_event(self, event: BackendEvent) -> None:
        self._injected_events.append(event)

    async def events(self) -> AsyncIterator[BackendEvent]:
        for event in self._injected_events:
            yield event
        self._injected_events.clear()

    async def send_codex_prompt(
        self,
        workspace_path: str,
        prompt: str,
        *,
        on_thread_created: Callable[[str], None] | None = None,
        interaction_mode: str = "general",
    ) -> str:
        """Send a prompt to Codex and return the response text synchronously.

        For the fake backend, returns programmed responses from
        `_codex_responses` or a default analysis result.
        """
        self._codex_call_count += 1
        # Record turn like real backend does
        thread_id = await self.create_thread(workspace_path)
        if on_thread_created is not None:
            on_thread_created(thread_id)
        await self.start_turn(thread_id, prompt)
        if self._codex_call_count <= len(self._codex_responses):
            return self._codex_responses[self._codex_call_count - 1]
        return (
            "decision: pass\n"
            "summary: Codex determined the implementation meets requirements.\n"
            "confidence: high"
        )


# ---------------------------------------------------------------------------
# Real app-server backend via JSON-RPC over WebSocket
# ---------------------------------------------------------------------------


class AppServerCodexBackend:
    def __init__(
        self,
        endpoint: str,
        approval_policy: str = "on-request",
        sandbox: str = "workspace-write",
        request_timeout_seconds: float = 60.0,
        codex_prompt_idle_timeout_seconds: float = 300.0,
        codex_analysis_hard_timeout_seconds: float = 1200.0,
        codex_verification_hard_timeout_seconds: float = 1200.0,
    ) -> None:
        self.endpoint = endpoint
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self._request_timeout_seconds = request_timeout_seconds
        self._codex_prompt_idle_timeout_seconds = codex_prompt_idle_timeout_seconds
        self._codex_analysis_hard_timeout_seconds = codex_analysis_hard_timeout_seconds
        self._codex_verification_hard_timeout_seconds = (
            codex_verification_hard_timeout_seconds
        )
        self._client: JsonRpcClient | None = None
        # Durable queue for EventBridge plus ephemeral per-turn subscribers.
        # Prompt aggregation must not consume the EventBridge stream.
        self._bridge_event_queue: list[BackendEvent] = []
        self._bridge_event_wakeup: asyncio.Event | None = None
        self._event_subscribers: list[asyncio.Queue[BackendEvent]] = []
        self._websocket = None
        self._process_manager: object = None
        self._transport_inject: tuple | None = None
        self._last_health_error: str | None = None
        self._ever_connected: bool = False
        self._runtime_event_callback: Callable[[BackendEvent], None] | None = None
        # Terminal surface: track active turn per thread for steer_thread()
        self._active_turn_ids: dict[str, str] = {}

    def set_transport(self, send_fn, recv_fn) -> None:
        self._transport_inject = (send_fn, recv_fn)

    def set_runtime_event_callback(
        self, callback: Callable[[BackendEvent], None]
    ) -> None:
        """Register a callback that receives every BackendEvent.

        The callback is invoked synchronously during ``_emit``.  Errors
        in the callback are logged and do not block the main event flow.
        """
        self._runtime_event_callback = callback

    def set_process_manager(self, pm: object) -> None:
        self._process_manager = pm

    async def _ensure_client(self) -> JsonRpcClient:
        if self._client is not None:
            return self._client
        if self._transport_inject:
            send_fn, recv_fn = self._transport_inject
            client = JsonRpcClient(
                send_json=send_fn,
                request_timeout_seconds=self._request_timeout_seconds,
            )
            self._client = client
            self._register_handlers(client)
            return client

        import asyncio
        import websockets

        ws = await websockets.connect(self.endpoint)
        self._websocket = ws
        self._ever_connected = True

        async def ws_send(msg: dict) -> None:
            import json
            await ws.send(json.dumps(msg))

        client = JsonRpcClient(
            send_json=ws_send,
            request_timeout_seconds=self._request_timeout_seconds,
        )
        self._client = client
        self._register_handlers(client)
        asyncio.create_task(self._recv_loop(ws, client))

        # Codex app-server requires an initialize handshake before any
        # other request.  This follows the LSP/JSON-RPC lifecycle pattern.
        await client.request("initialize", {
            "clientInfo": {"name": "wlcodex", "version": "1.0.0"},
        })
        logger.info("App-server initialized at %s", self.endpoint)
        await client.notify("initialized", {})

        return client

    async def _recv_loop(self, ws, client: JsonRpcClient) -> None:
        import json
        try:
            async for raw in ws:
                msg = json.loads(raw)
                await client.receive_message(msg)
        except Exception:
            logger.exception("WebSocket receive loop error")
            # Cancel pending requests and clear state so the next
            # _ensure_client() call will reconnect.
            await client.close()
            self._client = None
        finally:
            self._websocket = None

    def _register_handlers(self, client: JsonRpcClient) -> None:
        client.on_notification("thread/started", self._on_thread_started)
        client.on_notification("thread/status/changed", self._on_thread_status_changed)
        client.on_notification("thread/tokenUsage/updated", self._on_token_usage)
        client.on_notification("turn/started", self._on_turn_started)
        client.on_notification("turn/completed", self._on_turn_completed)
        client.on_notification("turn/diff/updated", self._on_diff_updated)
        client.on_notification("turn/plan/updated", self._on_plan_updated)
        client.on_notification("item/started", self._on_item_started)
        client.on_notification("item/completed", self._on_item_completed)
        client.on_notification("item/agentMessage/delta", self._on_agent_message_delta)
        client.on_notification("item/commandExecution/outputDelta", self._on_command_output_delta)
        client.on_notification("item/fileChange/outputDelta", self._on_file_change_delta)

        # Server requests — held until Telegram button resolves them
        client.on_server_request(
            "item/commandExecution/requestApproval", self._on_command_approval_request
        )
        client.on_server_request(
            "item/fileChange/requestApproval", self._on_file_change_approval_request
        )
        client.on_server_request(
            "item/permissions/requestApproval", self._on_permissions_approval_request
        )
        client.on_server_request(
            "execCommandApproval", self._on_legacy_exec_approval_request
        )
        client.on_server_request(
            "applyPatchApproval", self._on_legacy_patch_approval_request
        )

    # --- Notification handlers ---

    async def _on_thread_started(self, params: dict) -> None:
        self._emit(BackendEvent("thread_started", params))

    async def _on_thread_status_changed(self, params: dict) -> None:
        self._emit(BackendEvent("thread_status_changed", params))

    async def _on_token_usage(self, params: dict) -> None:
        self._emit(BackendEvent("token_usage_updated", params))

    async def _on_turn_started(self, params: dict) -> None:
        thread_id, turn_id = parse_turn_notification_ids(params)
        self._active_turn_ids[thread_id] = turn_id
        self._emit(BackendEvent("turn_started", {**params, "threadId": thread_id, "turnId": turn_id}))

    async def _on_turn_completed(self, params: dict) -> None:
        thread_id, turn_id = parse_turn_notification_ids(params)
        # Clear the active turn for this thread so future steer_thread
        # calls know there is no active turn to steer.
        if self._active_turn_ids.get(thread_id) == turn_id:
            del self._active_turn_ids[thread_id]
        self._emit(BackendEvent("turn_completed", {**params, "threadId": thread_id, "turnId": turn_id}))

    async def _on_diff_updated(self, params: dict) -> None:
        self._emit(BackendEvent("diff_updated", params))

    async def _on_plan_updated(self, params: dict) -> None:
        self._emit(BackendEvent("plan_updated", params))

    async def _on_item_started(self, params: dict) -> None:
        self._emit(BackendEvent("item_started", params))

    async def _on_item_completed(self, params: dict) -> None:
        self._emit(BackendEvent("item_completed", params))

    async def _on_agent_message_delta(self, params: dict) -> None:
        self._emit(BackendEvent("agent_message_delta", params))

    async def _on_command_output_delta(self, params: dict) -> None:
        self._emit(BackendEvent("command_output_delta", params))

    async def _on_file_change_delta(self, params: dict) -> None:
        self._emit(BackendEvent("file_change_delta", params))

    # --- Server request handlers (approval) — held until resolve_approval ---

    async def _on_command_approval_request(self, params: dict, request_id: str) -> None:
        command = params.get("command", "")
        if isinstance(command, list):
            command_text = " ".join(str(part) for part in command)
        else:
            command_text = str(command)
        reason = params.get("reason")
        summary = f"Run: {command_text}".strip() if command_text else "Run command"
        if reason:
            summary = f"{summary}\nReason: {reason}"
        self._emit(BackendEvent("approval_requested", {
            **{k: v for k, v in params.items()},
            "codexRequestId": request_id,
            "kind": "command",
            "summary": summary,
            "command": command_text,
        }))

    async def _on_file_change_approval_request(self, params: dict, request_id: str) -> None:
        file_changes = params.get("fileChanges", params.get("changedFiles", {}))
        if isinstance(file_changes, dict):
            changed_files = sorted(file_changes.keys())
        elif isinstance(file_changes, list):
            changed_files = [str(f) for f in file_changes]
        else:
            changed_files = []
        reason = params.get("reason")
        files = ", ".join(str(path) for path in changed_files[:6])
        if len(changed_files) > 6:
            files = f"{files}, +{len(changed_files) - 6} more"
        summary = f"Apply patch: {files}" if files else "Apply patch"
        if reason:
            summary = f"{summary}\nReason: {reason}"
        self._emit(BackendEvent("approval_requested", {
            **{k: v for k, v in params.items()},
            "codexRequestId": request_id,
            "kind": "file_change",
            "summary": summary,
        }))

    async def _on_permissions_approval_request(self, params: dict, request_id: str) -> None:
        self._emit(BackendEvent("approval_requested", {
            **{k: v for k, v in params.items()},
            "codexRequestId": request_id,
            "kind": "permissions",
        }))

    async def _on_legacy_exec_approval_request(self, params: dict, request_id: str) -> None:
        command = params.get("command", "")
        if isinstance(command, list):
            command_text = " ".join(str(part) for part in command)
        else:
            command_text = str(command)
        reason = params.get("reason")
        summary = f"Run: {command_text}".strip()
        if reason:
            summary = f"{summary}\nReason: {reason}"
        self._emit(BackendEvent("approval_requested", {
            "threadId": str(params.get("conversationId", "")),
            "codexRequestId": request_id,
            "codexItemId": str(params.get("callId", "")),
            "codexTurnId": "",
            "kind": "command",
            "summary": summary,
            "command": command_text,
            "responseSchema": "legacy_review_decision",
        }))

    async def _on_legacy_patch_approval_request(self, params: dict, request_id: str) -> None:
        file_changes = params.get("fileChanges", {})
        changed_files = sorted(file_changes.keys()) if isinstance(file_changes, dict) else []
        reason = params.get("reason")
        files = ", ".join(str(path) for path in changed_files[:6])
        if len(changed_files) > 6:
            files = f"{files}, +{len(changed_files) - 6} more"
        summary = f"Apply patch: {files}" if files else "Apply patch"
        if reason:
            summary = f"{summary}\nReason: {reason}"
        self._emit(BackendEvent("approval_requested", {
            "threadId": str(params.get("conversationId", "")),
            "codexRequestId": request_id,
            "codexItemId": str(params.get("callId", "")),
            "codexTurnId": "",
            "kind": "file_change",
            "summary": summary,
            "command": file_changes if isinstance(file_changes, dict) else {},
            "responseSchema": "legacy_review_decision",
        }))

    # --- Public methods ---

    async def create_thread(self, workspace_path: str) -> str:
        client = await self._ensure_client()
        result = await client.request(
            "thread/start",
            build_thread_start_params(
                workspace_path,
                self.approval_policy,
                self.sandbox,
            ),
        )
        return parse_thread_start_response(result)

    async def _create_prompt_thread(
        self, workspace_path: str, interaction_mode: str
    ) -> str:
        if interaction_mode not in ("analysis", "verification"):
            return await self.create_thread(workspace_path)
        client = await self._ensure_client()
        result = await client.request(
            "thread/start",
            build_thread_start_params(
                workspace_path,
                "on-request",
                "workspace-write",
                developer_instructions=_planning_developer_instructions(
                    interaction_mode
                ),
                config=_PLANNING_THREAD_CONFIG,
                model="gpt-5.5",
                personality="pragmatic",
                service_tier="fast",
            ),
        )
        return parse_thread_start_response(result)

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        client = await self._ensure_client()
        result = await client.request(
            "turn/start",
            build_turn_start_params(thread_id, prompt),
        )
        return parse_turn_response(result)

    async def _start_prompt_turn(
        self, thread_id: str, prompt: str, interaction_mode: str
    ) -> str:
        if interaction_mode not in ("analysis", "verification"):
            return await self.start_turn(thread_id, prompt)
        client = await self._ensure_client()
        result = await client.request(
            "turn/start",
            build_turn_start_params(
                thread_id,
                prompt,
                **_planning_turn_options(interaction_mode),
            ),
        )
        return parse_turn_response(result)

    async def continue_turn(self, thread_id: str, prompt: str) -> str:
        client = await self._ensure_client()
        await client.request("thread/resume", {"threadId": thread_id})
        result = await client.request(
            "turn/start",
            build_turn_start_params(thread_id, prompt),
        )
        return parse_turn_response(result)

    async def steer_turn(self, thread_id: str, expected_turn_id: str, prompt: str) -> None:
        client = await self._ensure_client()
        await client.request(
            "turn/steer",
            build_turn_steer_params(thread_id, expected_turn_id, prompt),
        )

    async def steer_thread(self, thread_id: str, text: str) -> None:
        """Send raw terminal input to an active Codex thread (terminal surface).

        Resolves the active turn_id for *thread_id* from turn/started
        notifications and delegates to ``steer_turn``.  When no active
        turn is known, the method raises ``ValueError`` so the terminal
        surface can tell the user there is no turn to steer — this is
        not a silent failure.
        """
        active_turn_id = self._active_turn_ids.get(thread_id)
        if active_turn_id is None:
            raise ValueError(
                f"No active turn found for thread {thread_id}. "
                "Wait for a turn to start before sending terminal input, "
                "or start a new turn via /codex."
            )
        await self.steer_turn(thread_id, active_turn_id, text)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        client = await self._ensure_client()
        await client.request("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        })

    def _hard_timeout_seconds(self, interaction_mode: str) -> float:
        if interaction_mode == "analysis":
            return self._codex_analysis_hard_timeout_seconds
        if interaction_mode == "verification":
            return self._codex_verification_hard_timeout_seconds
        return self._request_timeout_seconds

    def _idle_timeout_seconds(self, interaction_mode: str) -> float | None:
        if interaction_mode in ("analysis", "verification"):
            return self._codex_prompt_idle_timeout_seconds
        return None

    def _timeout_message(
        self,
        turn_id: str,
        *,
        reason: str,
        hard_timeout_seconds: float,
        idle_timeout_seconds: float | None,
    ) -> str:
        if reason == "idle" and idle_timeout_seconds is not None:
            return (
                f"Codex turn {turn_id} was idle for "
                f"{idle_timeout_seconds:g} seconds"
            )
        if reason == "hard" and idle_timeout_seconds is not None:
            return (
                f"Codex turn {turn_id} hit hard timeout after "
                f"{hard_timeout_seconds:g} seconds"
            )
        return (
            f"Codex turn {turn_id} did not complete within "
            f"{hard_timeout_seconds:g} seconds"
        )

    async def send_codex_prompt(
        self,
        workspace_path: str,
        prompt: str,
        *,
        on_thread_created: Callable[[str], None] | None = None,
        interaction_mode: str = "general",
    ) -> str:
        """Send prompt to Codex and block until its turn completes. Returns response text.

        Filters events by the specific thread_id/turn_id created here to avoid
        consuming events from concurrent tasks (multi-task race condition).

        Exits immediately when the matching turn_completed or turn_failed event
        is consumed — does NOT wait for the full request_timeout_seconds.
        """
        prompt_events = self._subscribe_events()
        try:
            thread_id = await self._create_prompt_thread(
                workspace_path,
                interaction_mode,
            )
            if on_thread_created is not None:
                on_thread_created(thread_id)
            turn_id = await self._start_prompt_turn(
                thread_id,
                prompt,
                interaction_mode,
            )
        except Exception:
            self._unsubscribe_events(prompt_events)
            raise

        deltas: list[str] = []
        loop = asyncio.get_running_loop()
        hard_timeout_seconds = self._hard_timeout_seconds(interaction_mode)
        idle_timeout_seconds = self._idle_timeout_seconds(interaction_mode)
        hard_deadline = loop.time() + hard_timeout_seconds
        idle_deadline = (
            loop.time() + idle_timeout_seconds
            if idle_timeout_seconds is not None
            else None
        )
        turn_ended = False
        timeout_reason = "request"

        def _event_turn_id(event: BackendEvent) -> str:
            turn = event.payload.get("turn")
            if isinstance(turn, dict) and turn.get("id"):
                return str(turn["id"])
            return str(event.payload.get("turnId", ""))

        def _event_turn_status(event: BackendEvent) -> str:
            turn = event.payload.get("turn")
            if isinstance(turn, dict) and turn.get("status"):
                return str(turn["status"])
            return str(event.payload.get("status", ""))

        def _event_error_message(event: BackendEvent) -> str:
            turn = event.payload.get("turn")
            error = None
            if isinstance(turn, dict):
                error = turn.get("error")
            if error is None:
                error = event.payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error.get("codexErrorInfo") or error)
            return str(error or "unknown error")

        def _event_matches_turn(event: BackendEvent) -> bool:
            event_turn_id = _event_turn_id(event)
            if event_turn_id:
                return event_turn_id == turn_id
            return str(event.payload.get("threadId", "")) == thread_id

        turn_error: str | None = None
        try:
            while True:
                now = loop.time()
                if now >= hard_deadline:
                    timeout_reason = "hard"
                    break
                next_deadline = hard_deadline
                if idle_deadline is not None:
                    next_deadline = min(next_deadline, idle_deadline)
                remaining = next_deadline - now
                if remaining <= 0:
                    timeout_reason = (
                        "idle"
                        if idle_deadline is not None
                        and next_deadline == idle_deadline
                        else "hard"
                    )
                    break
                try:
                    event = await asyncio.wait_for(prompt_events.get(), timeout=remaining)
                except TimeoutError:
                    now = loop.time()
                    timeout_reason = (
                        "hard"
                        if now >= hard_deadline
                        else "idle"
                        if idle_deadline is not None and now >= idle_deadline
                        else "request"
                    )
                    break
                if not _event_matches_turn(event):
                    # Approval requests may not carry turnId — match by threadId
                    # or disable idle timeout for any approval event on this thread.
                    if event.event_type == "approval_requested":
                        event_thread_id = str(event.payload.get("threadId", ""))
                        if event_thread_id == thread_id:
                            idle_deadline = None  # waiting for human — pause idle
                    continue

                if idle_timeout_seconds is not None:
                    idle_deadline = loop.time() + idle_timeout_seconds

                if event.event_type == "agent_message_delta":
                    delta = event.payload.get("delta", "")
                    if isinstance(delta, str):
                        deltas.append(delta)
                elif event.event_type == "approval_requested":
                    # Codex is waiting for human approval — pause idle timeout.
                    # The hard_deadline still protects against infinite hangs.
                    idle_deadline = None
                elif event.event_type == "turn_completed":
                    status = _event_turn_status(event)
                    if status in ("failed", "interrupted", "cancelled", "canceled"):
                        turn_error = _event_error_message(event)
                    turn_ended = True
                elif event.event_type in ("turn_failed",):
                    turn_error = _event_error_message(event)
                    turn_ended = True

                if turn_ended:
                    break
        finally:
            self._unsubscribe_events(prompt_events)

        if turn_error is not None:
            raise RuntimeError(f"Codex turn {turn_id} failed: {turn_error}")

        if not turn_ended:
            try:
                await self.interrupt_turn(thread_id, turn_id)
            except Exception as exc:
                logger.warning(
                    "Failed to interrupt timed-out Codex turn %s/%s: %s",
                    thread_id,
                    turn_id,
                    exc,
                )
            raise TimeoutError(
                self._timeout_message(
                    turn_id,
                    reason=timeout_reason,
                    hard_timeout_seconds=hard_timeout_seconds,
                    idle_timeout_seconds=idle_timeout_seconds,
                )
            )

        if not deltas:
            return "(no Codex response)"
        return "".join(deltas)

    async def fork_thread(self, thread_id: str, workspace_path: str) -> str:
        client = await self._ensure_client()
        result = await client.request("thread/fork", {
            "threadId": thread_id,
            "cwd": workspace_path,
        })
        return parse_thread_start_response(result)

    async def archive_thread(self, thread_id: str) -> None:
        client = await self._ensure_client()
        await client.request("thread/archive", {"threadId": thread_id})

    async def resolve_approval(self, codex_request_id: str, response: dict[str, object]) -> None:
        """Resolve a held approval server request.

        Sends the JSON-RPC response to the app-server with the given response dict.
        Also emits a BackendEvent so runtime event sources can record approval.resolved.
        """
        client = await self._ensure_client()
        client.resolve_server_request(codex_request_id, response)
        self._emit(BackendEvent("approval_resolved", {
            "codexRequestId": codex_request_id,
            "response": response,
        }))

    def set_health_error(self, error: str) -> None:
        self._last_health_error = error

    def health(self) -> object:
        from wlcodex.app_server_process import BackendHealth
        if self._process_manager:
            pm = self._process_manager
            ws_connected = self._websocket is not None and not getattr(
                self._websocket, "close_code", None
            )
            # If we've never connected, don't require a persistent WebSocket
            # for health — the process being alive is sufficient.  This
            # prevents /health from reporting unhealthy before the first
            # task starts (lazy-connect backend).
            if not self._ever_connected and not ws_connected:
                ws_connected = True  # defer to process check
            return BackendHealth(
                process_alive=pm.is_alive if hasattr(pm, "is_alive") else False,
                websocket_connected=ws_connected,
                external_process=pm.external_process if hasattr(pm, "external_process") else False,
                error=self._last_health_error,
            )
        return BackendHealth(
            process_alive=False,
            websocket_connected=self._websocket is not None,
            error=self._last_health_error or "no process manager configured",
        )

    async def events(self) -> AsyncIterator[BackendEvent]:
        while True:
            if self._bridge_event_queue:
                yield self._bridge_event_queue.pop(0)
            else:
                if self._bridge_event_wakeup is None:
                    self._bridge_event_wakeup = asyncio.Event()
                await self._bridge_event_wakeup.wait()
                self._bridge_event_wakeup.clear()

    def _emit(self, event: BackendEvent) -> None:
        self._bridge_event_queue.append(event)
        if self._bridge_event_wakeup is not None:
            self._bridge_event_wakeup.set()
        for queue in list(self._event_subscribers):
            queue.put_nowait(event)
        # Runtime event callback — fire-and-forget, errors must not
        # break the main event fan-out.
        if self._runtime_event_callback is not None:
            try:
                self._runtime_event_callback(event)
            except Exception:
                logger.exception("Runtime event callback failed")

    def _subscribe_events(self) -> asyncio.Queue[BackendEvent]:
        queue: asyncio.Queue[BackendEvent] = asyncio.Queue()
        self._event_subscribers.append(queue)
        return queue

    def _unsubscribe_events(self, queue: asyncio.Queue[BackendEvent]) -> None:
        try:
            self._event_subscribers.remove(queue)
        except ValueError:
            pass

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
        self._ever_connected = False
