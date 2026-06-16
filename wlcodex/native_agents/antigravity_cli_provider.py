from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from wlcodex.claude_binary import sanitized_claude_env
from wlcodex.native_agents.antigravity_models import (
    ANTIGRAVITY_QUOTA_FALLBACK_MODEL,
    DEFAULT_ANTIGRAVITY_MODEL,
    antigravity_model_catalog,
)
from wlcodex.native_agents.antigravity_local_sessions import (
    AntigravityLocalSession,
    AntigravityLocalSessionIndex,
)
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentSession,
    NativeAgentStatus,
)
from wlcodex.native_agents.runtime_events import (
    NativeAgentRuntimeEmitter,
    extract_native_agent_text,
)
from wlcodex.native_agents.session_store import NativeAgentSessionStore
from wlcodex.runtime_event_store import RuntimeEventStore
from wlcodex.runtime_events import EventType


@dataclass(frozen=True)
class AntigravityCliConfig:
    binary: str = "auto"
    print_timeout: str = "5m0s"
    default_model: str = DEFAULT_ANTIGRAVITY_MODEL
    request_timeout_seconds: float = 330.0
    dangerously_skip_permissions: bool = False
    sandbox: bool = False


@dataclass(frozen=True)
class _RunOutcome:
    status: str
    antigravity_conversation_id: str = ""
    error: str = ""
    failure_kind: str = ""
    failure_evidence_source: str = ""
    emitted_text: bool = False
    model: str = ""
    fallback_from_model: str = ""
    fallback_reason: str = ""


class AntigravityCliError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "cli_error",
        evidence_source: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.evidence_source = evidence_source


_EXECUTION_CWD_METADATA_KEY = "antigravity_execution_cwd"
_RUNTIME_TURN_EVENT_LIMIT = 50_000
_REPLAY_MATCH_MIN_CHARS = 8
_PRINT_TIMEOUT_GRACE_SECONDS = 30.0
_GO_DURATION_PART_RE = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([hms])")


class AntigravityCliRunner:
    def __init__(self, config: AntigravityCliConfig | None = None) -> None:
        self._config = config or AntigravityCliConfig()
        resolved = _resolve_antigravity_binary(self._config.binary)
        self.binary = resolved or self._config.binary
        self.available = bool(resolved)
        self.error = "" if resolved else _binary_error(self._config.binary)

    async def run(
        self,
        *,
        prompt: str,
        cwd: str,
        conversation_id: str = "",
        model: str = "",
        extra_dirs: tuple[str, ...] = (),
        dangerously_skip_permissions: bool = False,
        sandbox: bool = False,
    ):
        if not self.available:
            raise RuntimeError(self.error)
        log_file = _new_antigravity_cli_log_file()
        args = self._args(
            prompt=prompt,
            cwd=cwd,
            conversation_id=conversation_id,
            model=model,
            extra_dirs=extra_dirs,
            log_file=log_file,
            dangerously_skip_permissions=dangerously_skip_permissions,
            sandbox=sandbox,
        )
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                *args,
                cwd=cwd or None,
                env=sanitized_claude_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout_queue: asyncio.Queue[bytes] = asyncio.Queue()
            communicate_task = asyncio.create_task(
                _communicate_with_auth_detection(
                    proc,
                    stdout_queue=stdout_queue,
                )
            )
            streamed_stdout = False
            async for chunk in _iter_stdout_chunks_until_complete(
                communicate_task,
                stdout_queue,
                timeout=_runner_timeout_seconds(self._config),
            ):
                streamed_stdout = True
                text = chunk.decode("utf-8", errors="replace")
                if text:
                    yield {
                        "type": "assistant",
                        "text": text,
                        "conversation_id": conversation_id,
                    }
            stdout, stderr = communicate_task.result()
        except asyncio.TimeoutError as exc:
            if proc is not None:
                proc.kill()
                await proc.wait()
            raise AntigravityCliError(
                "Antigravity CLI print mode timed out",
                kind="timeout",
                evidence_source="process",
            ) from exc
        except FileNotFoundError as exc:
            raise AntigravityCliError(
                _binary_error(self.binary),
                kind="setup",
                evidence_source="binary",
            ) from exc

        text = stdout.decode("utf-8", errors="replace") if stdout else ""
        error_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        log_text = _read_text_file(log_file)
        auth_error = _authentication_error_message(f"{text}\n{error_text}")
        if auth_error:
            raise AntigravityCliError(
                auth_error,
                kind="authentication",
                evidence_source="stdout_stderr",
            )
        quota_error, quota_source = _quota_or_limit_error_from_sources(
            stderr=error_text,
            stdout=text,
            log=log_text,
        )
        if quota_error:
            raise AntigravityCliError(
                quota_error,
                kind="quota_limit",
                evidence_source=quota_source,
            )
        model_error, model_error_source = _model_unavailable_error_from_sources(
            stderr=error_text,
            stdout=text,
            log=log_text,
        )
        if model_error:
            raise AntigravityCliError(
                model_error,
                kind="model_unavailable",
                evidence_source=model_error_source,
            )
        cli_error = _cli_error_message(error_text or text)
        if cli_error:
            raise AntigravityCliError(
                cli_error,
                kind="cli_error",
                evidence_source="stderr" if error_text else "stdout",
            )
        if proc.returncode not in (0, None):
            raise AntigravityCliError(
                (error_text or text or "Antigravity CLI failed").strip(),
                kind="cli_error",
                evidence_source="returncode",
            )
        if text and not streamed_stdout:
            yield {
                "type": "assistant",
                "text": text,
                "conversation_id": conversation_id,
            }

    def _args(
        self,
        *,
        prompt: str,
        cwd: str,
        conversation_id: str,
        model: str,
        extra_dirs: tuple[str, ...],
        log_file: str = "",
        dangerously_skip_permissions: bool = False,
        sandbox: bool = False,
    ) -> tuple[str, ...]:
        args: list[str] = [
            "--print-timeout",
            self._config.print_timeout,
        ]
        if log_file:
            args.extend(["--log-file", log_file])
        if conversation_id:
            args.extend(["--conversation", conversation_id])
        for directory in (cwd, *extra_dirs):
            if directory:
                args.extend(["--add-dir", directory])
        if dangerously_skip_permissions or self._config.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")
        if sandbox or self._config.sandbox:
            args.append("--sandbox")
        selected_model = model or self._config.default_model
        if selected_model:
            args.extend(["--model", selected_model])
        args.extend(["--print", prompt])
        return tuple(args)


class AntigravityCliLocalProvider:
    provider = "antigravity"
    provider_engine = "cli-local"

    def __init__(
        self,
        *,
        session_store: NativeAgentSessionStore,
        runtime_store: RuntimeEventStore | None = None,
        runner: Any | None = None,
        default_cwd: str = "",
        local_session_index: Any | None = None,
        heartbeat_interval_seconds: float = 15.0,
    ) -> None:
        self._session_store = session_store
        self._runtime_store = runtime_store
        self._runner = runner or AntigravityCliRunner()
        self._default_cwd = default_cwd
        self._local_session_index = (
            local_session_index or AntigravityLocalSessionIndex()
        )
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    async def wait_for_background_tasks(self) -> None:
        while self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)

    async def status(self) -> NativeAgentStatus:
        available = bool(getattr(self._runner, "available", False))
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=available,
            connected=available,
            status_code="ok" if available else "binary_unresolved",
            message="" if available else str(getattr(self._runner, "error", "")),
            metadata={"binary": str(getattr(self._runner, "binary", ""))},
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(
            can_list_sessions=True,
            can_list_models=True,
            can_start_session=True,
            can_resume_session=True,
            can_read_history=True,
            can_stream_events=True,
            can_continue_session=True,
            can_apply_file_edits=True,
            can_run_shell_commands=True,
            disabled_reasons={
                "can_steer_active_turn": (
                    "Antigravity CLI continuation starts a new prompt turn."
                ),
                "can_interrupt": (
                    "Antigravity CLI provider does not hold a long-lived process handle yet."
                ),
                "can_resolve_approval": (
                    "Antigravity CLI permissions are controlled by agy flags."
                ),
            },
        )

    async def list_sessions(self, limit: int = 50) -> list[NativeAgentSession]:
        self._import_local_sessions(limit=max(limit, 50))
        sessions = self._session_store.list_recent(
            provider=self.provider,
            provider_engine=self.provider_engine,
            limit=limit,
        )
        return _hide_linked_local_duplicates(sessions)

    async def list_models(self) -> list[dict[str, Any]]:
        return antigravity_model_catalog(default_model=self._runner_default_model())

    def _runner_default_model(self) -> str:
        config = getattr(self._runner, "_config", None)
        return str(
            getattr(config, "default_model", DEFAULT_ANTIGRAVITY_MODEL)
            or DEFAULT_ANTIGRAVITY_MODEL
        )

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._ensure_enabled()
        native_session_id = str(uuid4())
        native_turn_id = _new_turn_id()
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title=_title_from_prompt(prompt),
            cwd=cwd or self._default_cwd,
            source_kind="antigravity_cli_local",
            status="running",
            last_turn_id=native_turn_id,
            metadata={
                _EXECUTION_CWD_METADATA_KEY: _wlcodex_execution_cwd(
                    native_session_id
                )
            },
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            conversation_id="",
            model=str(kwargs.get("model") or ""),
            dangerously_skip_permissions=bool(
                kwargs.get("dangerously_skip_permissions", False)
            ),
            sandbox=bool(kwargs.get("sandbox", False)),
        )
        return _control_result(
            session,
            status="started",
            turn_id=native_turn_id,
            turn_running=True,
        )

    async def create_session(self, cwd: str, **kwargs: Any) -> NativeAgentControlResult:
        self._ensure_enabled()
        native_session_id = str(uuid4())
        session = self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
            title="Antigravity CLI session",
            cwd=cwd or self._default_cwd,
            source_kind="antigravity_cli_local",
            status="created",
            metadata={
                _EXECUTION_CWD_METADATA_KEY: _wlcodex_execution_cwd(
                    native_session_id
                )
            },
        )
        return _control_result(session, status="created")

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        session = self._lookup_session(native_session_id, import_local=True)
        return {"thread": session.to_json_dict(), "turns": self._runtime_turns(session)}

    async def attach_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._lookup_session(native_session_id, import_local=True)
        return _control_result(session, status="attached")

    async def sync_session(self, native_session_id: str) -> NativeAgentControlResult:
        session = self._lookup_session(native_session_id, import_local=True)
        return _control_result(session, status="synced")

    async def continue_session(
        self,
        native_session_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self._ensure_enabled()
        session = self._lookup_session(native_session_id, import_local=True)
        native_turn_id = _new_turn_id()
        session = self._session_store.update_session(
            session.id,
            status="running",
            last_turn_id=native_turn_id,
        )
        self._start_background_prompt(
            session=session,
            native_turn_id=native_turn_id,
            prompt=prompt,
            conversation_id=_antigravity_conversation_id(session),
            model=str(kwargs.get("model") or ""),
            dangerously_skip_permissions=bool(
                kwargs.get("dangerously_skip_permissions", False)
            ),
            sandbox=bool(kwargs.get("sandbox", False)),
        )
        return _control_result(
            session,
            status="continued",
            turn_id=native_turn_id,
            turn_running=True,
        )

    async def steer_session(
        self,
        native_session_id: str,
        expected_turn_id: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        raise NotImplementedError(
            "Antigravity CLI provider does not support active-turn steering"
        )

    async def interrupt_session(
        self,
        native_session_id: str,
        turn_id: str = "",
    ) -> NativeAgentControlResult:
        raise NotImplementedError("Antigravity CLI provider does not support interrupt yet")

    async def resolve_approval(
        self,
        request_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        raise KeyError(request_id)

    async def _run_prompt(
        self,
        *,
        session: NativeAgentSession,
        prompt: str,
        native_turn_id: str,
        conversation_id: str,
        model: str,
        dangerously_skip_permissions: bool = False,
        sandbox: bool = False,
    ) -> _RunOutcome:
        selected_model = model.strip() or self._runner_default_model()
        outcome = await self._run_prompt_once(
            session=session,
            prompt=prompt,
            native_turn_id=native_turn_id,
            conversation_id=conversation_id,
            model=selected_model,
            dangerously_skip_permissions=dangerously_skip_permissions,
            sandbox=sandbox,
        )
        if not _should_fallback_for_antigravity_quota(
            model=selected_model,
            outcome=outcome,
        ):
            return outcome
        fallback = await self._run_prompt_once(
            session=session,
            prompt=prompt,
            native_turn_id=native_turn_id,
            conversation_id=conversation_id,
            model=ANTIGRAVITY_QUOTA_FALLBACK_MODEL,
            dangerously_skip_permissions=dangerously_skip_permissions,
            sandbox=sandbox,
        )
        return _RunOutcome(
            status=fallback.status,
            antigravity_conversation_id=fallback.antigravity_conversation_id,
            error=fallback.error,
            failure_kind=outcome.failure_kind,
            failure_evidence_source=outcome.failure_evidence_source,
            emitted_text=fallback.emitted_text,
            model=fallback.model,
            fallback_from_model=selected_model,
            fallback_reason=outcome.error,
        )

    async def _run_prompt_once(
        self,
        *,
        session: NativeAgentSession,
        prompt: str,
        native_turn_id: str,
        conversation_id: str,
        model: str,
        dangerously_skip_permissions: bool = False,
        sandbox: bool = False,
    ) -> _RunOutcome:
        latest_conversation_id = conversation_id
        emitted_text = False
        execution_cwd = _session_execution_cwd(session)
        replay_transcript = _assistant_transcript_text(self._runtime_turns(session))
        remaining_replay_prefix = replay_transcript
        Path(execution_cwd).mkdir(parents=True, exist_ok=True)
        try:
            async for event in self._runner.run(
                prompt=prompt,
                cwd=execution_cwd,
                conversation_id=conversation_id,
                model=model,
                dangerously_skip_permissions=dangerously_skip_permissions,
                sandbox=sandbox,
                extra_dirs=_runner_extra_dirs(session, execution_cwd),
            ):
                event_conversation_id = str(event.get("conversation_id") or "")
                latest_conversation_id = event_conversation_id or latest_conversation_id
                text = extract_native_agent_text(event)
                if text:
                    text, remaining_replay_prefix = _strip_replayed_assistant_prefix(
                        text,
                        remaining_replay_prefix,
                        replay_transcript=replay_transcript,
                    )
                if text:
                    emitted_text = True
                    self._emit_text_delta(
                        session=session,
                        native_turn_id=native_turn_id,
                        delta=text,
                    )
        except AntigravityCliError as exc:
            return _RunOutcome(
                status="failed",
                antigravity_conversation_id=latest_conversation_id,
                error=str(exc),
                failure_kind=exc.kind,
                failure_evidence_source=exc.evidence_source,
                emitted_text=emitted_text,
                model=model,
            )
        except Exception as exc:
            return _RunOutcome(
                status="failed",
                antigravity_conversation_id=latest_conversation_id,
                error=str(exc),
                failure_kind="unknown",
                emitted_text=emitted_text,
                model=model,
            )
        if not latest_conversation_id:
            latest = self._local_session_index.latest_for_cwd(execution_cwd)
            if latest is not None and _local_session_started_after(
                latest,
                session.created_at,
            ):
                latest_conversation_id = latest.session_id
        if not emitted_text:
            return _RunOutcome(
                status="failed",
                antigravity_conversation_id=latest_conversation_id,
                error=(
                    "Antigravity CLI completed without assistant output for "
                    f"model {model}. The CLI exited successfully without "
                    "assistant text; investigate Antigravity print-mode/model "
                    "behavior instead of treating this as login/setup."
                ),
                failure_kind="empty_output",
                failure_evidence_source="no_assistant_text",
                model=model,
            )
        return _RunOutcome(
            status="done",
            antigravity_conversation_id=latest_conversation_id,
            emitted_text=emitted_text,
            model=model,
        )

    def _start_background_prompt(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        conversation_id: str,
        model: str,
        dangerously_skip_permissions: bool = False,
        sandbox: bool = False,
    ) -> None:
        emitter = self._emitter()
        if emitter is not None:
            emitter.started(session, native_turn_id=native_turn_id)
            emitter.user_message(session, native_turn_id=native_turn_id, text=prompt)
        task = asyncio.create_task(
            self._run_prompt_to_terminal_state(
                session=session,
                native_turn_id=native_turn_id,
                prompt=prompt,
                conversation_id=conversation_id,
                model=model,
                dangerously_skip_permissions=dangerously_skip_permissions,
                sandbox=sandbox,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_prompt_to_terminal_state(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        prompt: str,
        conversation_id: str,
        model: str,
        dangerously_skip_permissions: bool = False,
        sandbox: bool = False,
    ) -> None:
        done_event = asyncio.Event()
        heartbeat_task = self._heartbeat_task(
            session=session,
            native_turn_id=native_turn_id,
            done_event=done_event,
        )
        try:
            outcome = await self._run_prompt(
                session=session,
                prompt=prompt,
                native_turn_id=native_turn_id,
                conversation_id=conversation_id,
                model=model,
                dangerously_skip_permissions=dangerously_skip_permissions,
                sandbox=sandbox,
            )
        finally:
            done_event.set()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
        updated = self._update_after_run(session, outcome)
        if outcome.antigravity_conversation_id:
            local_session = self._local_session_index.get(
                outcome.antigravity_conversation_id
            )
            if local_session is not None:
                updated = self._import_local_session(local_session)
        emitter = self._emitter()
        if emitter is None:
            return
        if outcome.status == "done":
            emitter.completed(updated, native_turn_id=native_turn_id)
        else:
            emitter.failed(
                updated,
                native_turn_id=native_turn_id,
                error=outcome.error or "Antigravity CLI run failed",
            )

    def _heartbeat_task(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        done_event: asyncio.Event,
    ) -> asyncio.Task[None] | None:
        if self._runtime_store is None or self._heartbeat_interval_seconds <= 0:
            return None
        return asyncio.create_task(
            self._emit_heartbeats(
                session=session,
                native_turn_id=native_turn_id,
                done_event=done_event,
            )
        )

    async def _emit_heartbeats(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        done_event: asyncio.Event,
    ) -> None:
        while not done_event.is_set():
            await asyncio.sleep(self._heartbeat_interval_seconds)
            if done_event.is_set():
                return
            emitter = self._emitter()
            if emitter is not None:
                emitter.heartbeat(session, native_turn_id=native_turn_id)

    def _update_after_run(
        self,
        session: NativeAgentSession,
        outcome: _RunOutcome,
    ) -> NativeAgentSession:
        metadata = dict(session.metadata)
        if outcome.antigravity_conversation_id:
            metadata["antigravity_conversation_id"] = (
                outcome.antigravity_conversation_id
            )
        if outcome.model:
            metadata["antigravity_model"] = outcome.model
        else:
            metadata.pop("antigravity_model", None)
        if outcome.fallback_from_model:
            metadata["antigravity_model_fallback_from"] = outcome.fallback_from_model
            metadata["antigravity_model_fallback_reason"] = outcome.fallback_reason
            if outcome.failure_evidence_source:
                metadata["antigravity_model_fallback_evidence_source"] = (
                    outcome.failure_evidence_source
                )
        else:
            metadata.pop("antigravity_model_fallback_from", None)
            metadata.pop("antigravity_model_fallback_reason", None)
            metadata.pop("antigravity_model_fallback_evidence_source", None)
        if outcome.error:
            metadata["error"] = outcome.error
            if outcome.failure_kind:
                metadata["antigravity_failure_kind"] = outcome.failure_kind
            if outcome.failure_evidence_source:
                metadata["antigravity_failure_evidence_source"] = (
                    outcome.failure_evidence_source
                )
        else:
            metadata.pop("error", None)
            metadata.pop("antigravity_failure_kind", None)
            metadata.pop("antigravity_failure_evidence_source", None)
        return self._session_store.update_session(
            session.id,
            status=outcome.status,
            metadata=metadata,
        )

    def _lookup_session(
        self,
        native_session_id: str,
        *,
        import_local: bool = False,
    ) -> NativeAgentSession:
        session = self._session_store.get_by_native_session_id(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=native_session_id,
        )
        if session is None and import_local:
            local_session = self._local_session_index.get(native_session_id)
            if local_session is not None:
                session = self._import_local_session(local_session)
        if session is None:
            raise KeyError(native_session_id)
        return session

    def _import_local_sessions(self, *, limit: int) -> None:
        for local_session in self._local_session_index.list_recent(limit=limit):
            self._import_local_session(local_session)

    def _import_local_session(
        self,
        local_session: AntigravityLocalSession,
    ) -> NativeAgentSession:
        existing = self._find_session_by_antigravity_conversation_id(
            local_session.session_id
        )
        if existing is None:
            existing = self._session_store.get_by_native_session_id(
                provider=self.provider,
                provider_engine=self.provider_engine,
                native_session_id=local_session.session_id,
            )
        metadata = _local_session_metadata(local_session)
        if existing is not None:
            merged_metadata = dict(existing.metadata)
            merged_metadata.update(metadata)
            return self._session_store.update_session(
                existing.id,
                title=_merged_import_title(existing, local_session),
                cwd=existing.cwd or local_session.cwd or self._default_cwd,
                source_kind=existing.source_kind or "antigravity_cli_local",
                status=existing.status or "done",
                activity_at=local_session.updated_at,
                metadata=merged_metadata,
            )
        return self._session_store.get_or_create_session(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id=local_session.session_id,
            title=local_session.title,
            cwd=local_session.cwd or self._default_cwd,
            source_kind="antigravity_cli_local",
            status="done",
            last_turn_id=f"cli-local-history-{local_session.session_id}",
            activity_at=local_session.updated_at,
            metadata=metadata,
        )

    def _find_session_by_antigravity_conversation_id(
        self,
        conversation_id: str,
    ) -> NativeAgentSession | None:
        matches: list[NativeAgentSession] = []
        for session in self._session_store.list_recent(
            provider=self.provider,
            provider_engine=self.provider_engine,
            limit=500,
        ):
            if _antigravity_conversation_id(session) == conversation_id:
                matches.append(session)
        for session in matches:
            if session.native_session_id != conversation_id:
                return session
        return matches[0] if matches else None

    def _runtime_turns(self, session: NativeAgentSession) -> list[dict[str, str]]:
        if self._runtime_store is None:
            return []
        turns: list[dict[str, str]] = []
        assistant_by_key: OrderedDict[str, dict[str, str]] = OrderedDict()
        for event in self._runtime_store.list_by_agent_run(
            session.agent_run_id,
            limit=_RUNTIME_TURN_EVENT_LIMIT,
        ):
            payload = event.payload
            if str(payload.get("native_thread_id") or "") != session.native_session_id:
                continue
            native_turn_id = str(payload.get("native_turn_id") or "")
            if event.event_type == EventType.USER_MESSAGE_RECEIVED:
                text = str(payload.get("text") or "")
                if text:
                    turns.append(
                        {
                            "role": "user",
                            "text": text,
                            "native_turn_id": native_turn_id,
                        }
                    )
            elif event.event_type == EventType.MODEL_TEXT_DELTA:
                key = str(
                    payload.get("itemId")
                    or payload.get("native_turn_id")
                    or f"assistant:{event.id}"
                )
                item = assistant_by_key.get(key)
                if item is None:
                    item = {
                        "role": "assistant",
                        "text": "",
                        "native_turn_id": native_turn_id,
                    }
                    assistant_by_key[key] = item
                    turns.append(item)
                item["text"] += str(payload.get("delta") or payload.get("text") or "")
        return [turn for turn in turns if turn["text"]]

    def _ensure_enabled(self) -> None:
        if not bool(getattr(self._runner, "available", False)):
            raise RuntimeError(str(getattr(self._runner, "error", "")))

    def _emitter(self) -> NativeAgentRuntimeEmitter | None:
        if self._runtime_store is None:
            return None
        return NativeAgentRuntimeEmitter(
            runtime_store=self._runtime_store,
            provider=self.provider,
            provider_engine=self.provider_engine,
            source_kind="antigravity_cli_local",
        )

    def _emit_text_delta(
        self,
        *,
        session: NativeAgentSession,
        native_turn_id: str,
        delta: str,
    ) -> None:
        emitter = self._emitter()
        if emitter is not None:
            emitter.text_delta(session, native_turn_id=native_turn_id, delta=delta)


def _control_result(
    session: NativeAgentSession,
    *,
    status: str,
    turn_id: str = "",
    turn_running: bool = False,
) -> NativeAgentControlResult:
    return NativeAgentControlResult(
        provider=session.provider,
        provider_engine=session.provider_engine,
        native_session_id=session.native_session_id,
        agent_run_id=session.agent_run_id,
        turn_id=turn_id,
        active_turn_id=turn_id if turn_running else "",
        turn_running=turn_running,
        status=status,
    )


def _hide_linked_local_duplicates(
    sessions: list[NativeAgentSession],
) -> list[NativeAgentSession]:
    claimed_conversation_ids = {
        conversation_id
        for session in sessions
        if (
            conversation_id := _antigravity_conversation_id(session)
        )
        and conversation_id != session.native_session_id
    }
    if not claimed_conversation_ids:
        return sessions
    return [
        session
        for session in sessions
        if session.native_session_id not in claimed_conversation_ids
    ]


def _title_from_prompt(prompt: str) -> str:
    return prompt.strip()[:80] or "Antigravity CLI session"


def _antigravity_conversation_id(session: NativeAgentSession) -> str:
    value = str(session.metadata.get("antigravity_conversation_id", "") or "")
    if value:
        return value
    if session.source_kind == "antigravity_cli_local":
        try:
            UUID(session.native_session_id)
        except (TypeError, ValueError):
            return session.native_session_id
    return ""


def _local_session_metadata(local_session: AntigravityLocalSession) -> dict[str, str]:
    metadata = {
        "antigravity_conversation_id": local_session.session_id,
        "antigravity_source_path": local_session.source_path,
    }
    if local_session.brain_path:
        metadata["antigravity_brain_path"] = local_session.brain_path
    if local_session.source_root:
        metadata["antigravity_source_root"] = local_session.source_root
    return metadata


def _merged_import_title(
    existing: NativeAgentSession,
    local_session: AntigravityLocalSession,
) -> str:
    if not existing.title:
        return local_session.title
    if _is_fallback_title(existing.title, local_session.session_id):
        return local_session.title
    if _is_longer_version_of_title(existing.title, local_session.title):
        return local_session.title
    return existing.title


def _is_fallback_title(title: str, session_id: str) -> bool:
    return title == f"Antigravity {session_id[:8]}"


def _is_longer_version_of_title(existing_title: str, local_title: str) -> bool:
    return existing_title != local_title and existing_title.startswith(local_title)


def _new_turn_id() -> str:
    return f"cli-local-turn-{uuid4()}"


def _wlcodex_execution_cwd(native_session_id: str) -> str:
    return str(
        Path(tempfile.gettempdir())
        / "wlcodex-antigravity-cli"
        / native_session_id
    )


def _session_execution_cwd(session: NativeAgentSession) -> str:
    value = str(session.metadata.get(_EXECUTION_CWD_METADATA_KEY, "") or "").strip()
    return value or session.cwd


def _runner_extra_dirs(
    session: NativeAgentSession,
    execution_cwd: str,
) -> tuple[str, ...]:
    if not session.cwd:
        return ()
    if str(Path(session.cwd).expanduser()) == str(Path(execution_cwd).expanduser()):
        return ()
    return (session.cwd,)


def _assistant_transcript_text(turns: list[dict[str, str]]) -> str:
    return "".join(turn["text"] for turn in turns if turn["role"] == "assistant")


def _strip_replayed_assistant_prefix(
    text: str,
    replay_prefix: str,
    *,
    replay_transcript: str = "",
) -> tuple[str, str]:
    if not text or not replay_prefix:
        return text, replay_prefix
    replay_prefix = _select_replayed_assistant_prefix(
        text,
        replay_prefix,
        replay_transcript,
    )
    if not replay_prefix:
        return text, ""
    if replay_prefix.startswith(text):
        return "", replay_prefix[len(text) :]
    if text.startswith(replay_prefix):
        return text[len(replay_prefix) :], ""
    return text, ""


def _select_replayed_assistant_prefix(
    text: str,
    replay_prefix: str,
    replay_transcript: str,
) -> str:
    if replay_prefix.startswith(text) or text.startswith(replay_prefix):
        return replay_prefix
    if len(text.strip()) < _REPLAY_MATCH_MIN_CHARS:
        return ""
    index = replay_transcript.rfind(text)
    if index >= 0:
        return replay_transcript[index:]
    overlap = min(len(text), len(replay_transcript))
    for length in range(overlap, _REPLAY_MATCH_MIN_CHARS - 1, -1):
        candidate = text[:length]
        if (
            len(candidate.strip()) >= _REPLAY_MATCH_MIN_CHARS
            and replay_transcript.endswith(candidate)
        ):
            return candidate
    return ""


def _local_session_started_after(
    local_session: AntigravityLocalSession,
    started_at: str,
) -> bool:
    return _timestamp_seconds(local_session.updated_at) >= _timestamp_seconds(started_at)


def _timestamp_seconds(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _runner_timeout_seconds(config: AntigravityCliConfig) -> float:
    configured = max(float(config.request_timeout_seconds), 0.0)
    print_timeout = _parse_go_duration_seconds(config.print_timeout)
    if print_timeout is None:
        return configured
    capped = max(print_timeout, 0.0) + _PRINT_TIMEOUT_GRACE_SECONDS
    if configured <= 0:
        return capped
    return min(configured, capped)


def _parse_go_duration_seconds(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    position = 0
    total = 0.0
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    for match in _GO_DURATION_PART_RE.finditer(text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(text) or position == 0:
        return None
    return total


async def _iter_stdout_chunks_until_complete(
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
    stdout_queue: asyncio.Queue[bytes],
    *,
    timeout: float,
):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if not stdout_queue.empty():
            yield stdout_queue.get_nowait()
            continue

        if communicate_task.done() and stdout_queue.empty():
            exc = communicate_task.exception()
            if exc is not None:
                raise exc
            return

        remaining = deadline - loop.time()
        if remaining <= 0:
            communicate_task.cancel()
            await asyncio.gather(communicate_task, return_exceptions=True)
            raise asyncio.TimeoutError

        get_task = asyncio.create_task(stdout_queue.get())
        done, _pending = await asyncio.wait(
            {communicate_task, get_task},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            get_task.cancel()
            communicate_task.cancel()
            await asyncio.gather(get_task, communicate_task, return_exceptions=True)
            raise asyncio.TimeoutError

        if communicate_task in done:
            exc = communicate_task.exception()
            if exc is not None:
                get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
                raise exc

        if get_task in done:
            yield get_task.result()
        else:
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)


async def _communicate_with_auth_detection(
    proc: asyncio.subprocess.Process,
    *,
    stdout_queue: asyncio.Queue[bytes] | None = None,
) -> tuple[bytes, bytes]:
    if getattr(proc, "stdout", None) is None and getattr(proc, "stderr", None) is None:
        return await proc.communicate()

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    tasks = [
        asyncio.create_task(
            _read_process_stream(
                proc.stdout,
                stdout_chunks,
                proc,
                stdout_queue=stdout_queue,
            )
        ),
        asyncio.create_task(_read_process_stream(proc.stderr, stderr_chunks, proc)),
        asyncio.create_task(proc.wait()),
    ]
    try:
        await asyncio.gather(*tasks)
    except RuntimeError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if getattr(proc, "returncode", None) is None:
            proc.kill()
            await proc.wait()
        raise
    return b"".join(stdout_chunks), b"".join(stderr_chunks)


async def _read_process_stream(
    stream: Any,
    chunks: list[bytes],
    proc: asyncio.subprocess.Process,
    *,
    stdout_queue: asyncio.Queue[bytes] | None = None,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.readline()
        if not chunk:
            return
        chunks.append(chunk)
        decoded = _decode_chunks(chunks)
        auth_error = _authentication_error_message(decoded)
        if auth_error:
            if getattr(proc, "returncode", None) is None:
                proc.kill()
            raise RuntimeError(auth_error)
        cli_error = _cli_error_message(decoded)
        if cli_error:
            if getattr(proc, "returncode", None) is None:
                proc.kill()
            raise RuntimeError(cli_error)
        if stdout_queue is not None:
            await stdout_queue.put(chunk)


def _decode_chunks(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")


def _authentication_error_message(text: str) -> str:
    lowered = text.lower()
    if "authentication required" not in lowered and "authentication timed out" not in lowered:
        return ""
    return (
        "Antigravity CLI authentication required. Run agy in a local terminal, "
        "complete Google login, then retry from WLCodex."
    )


def _cli_error_message(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("Error:"):
        return stripped
    return ""


def _quota_or_limit_error_message(text: str) -> str:
    if not _is_antigravity_quota_or_limit_error(text):
        return ""
    for line in text.splitlines():
        if _is_antigravity_quota_or_limit_error(line):
            return _clean_antigravity_log_line(line)
    return text.strip()


def _quota_or_limit_error_from_sources(
    *,
    stderr: str,
    stdout: str,
    log: str,
) -> tuple[str, str]:
    for source, text in (
        ("stderr", stderr),
        ("stdout", stdout),
        ("log", log),
    ):
        message = _quota_or_limit_error_message(text)
        if message:
            return message, source
    return "", ""


def _model_unavailable_error_message(text: str) -> str:
    if not _is_antigravity_model_unavailable_error(text):
        return ""
    for line in text.splitlines():
        if _is_antigravity_model_unavailable_error(line):
            return _clean_antigravity_log_line(line)
    return text.strip()


def _model_unavailable_error_from_sources(
    *,
    stderr: str,
    stdout: str,
    log: str,
) -> tuple[str, str]:
    for source, text in (
        ("stderr", stderr),
        ("stdout", stdout),
        ("log", log),
    ):
        message = _model_unavailable_error_message(text)
        if message:
            return message, source
    return "", ""


def _clean_antigravity_log_line(line: str) -> str:
    stripped = line.strip()
    marker = "RESOURCE_EXHAUSTED"
    marker_index = stripped.find(marker)
    if marker_index >= 0:
        return stripped[marker_index:]
    severity_index = re.search(r"\]\s+", stripped)
    if severity_index:
        return stripped[severity_index.end() :]
    return stripped


def _should_fallback_for_antigravity_quota(
    *,
    model: str,
    outcome: _RunOutcome,
) -> bool:
    if model != DEFAULT_ANTIGRAVITY_MODEL:
        return False
    if outcome.status != "failed" or outcome.emitted_text:
        return False
    return outcome.failure_kind == "quota_limit"


def _is_antigravity_quota_or_limit_error(error: str) -> bool:
    lowered = error.lower()
    quota_markers = (
        "individual quota reached",
        "quota exceeded",
        "quota reached",
        "rate limit",
        "rate-limit",
        "rate_limit",
        "usage limit",
        "limit exceeded",
        "limits exceeded",
        "resource exhausted",
        "insufficient credits",
        "insufficient credit",
        "billing limit",
    )
    return any(marker in lowered for marker in quota_markers)


def _is_antigravity_model_unavailable_error(error: str) -> bool:
    lowered = error.lower()
    model_markers = (
        "not recognized as a known model or custom model",
        "neither planmodel nor requestedmodel specified",
        "you must specify a valid model",
        "failed to resolve model flag",
    )
    return any(marker in lowered for marker in model_markers)


def _new_antigravity_cli_log_file() -> str:
    directory = Path(tempfile.gettempdir()) / "wlcodex-antigravity-cli" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"agy-{uuid4()}.log")


def _read_text_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _resolve_antigravity_binary(binary: str) -> str:
    if binary and binary != "auto":
        if Path(binary).exists():
            return binary
        return shutil.which(binary) or ""
    resolved = shutil.which("agy")
    if resolved:
        return resolved
    fallback = Path.home() / ".local" / "bin" / "agy"
    return str(fallback) if fallback.exists() else ""


def _binary_error(binary: str) -> str:
    if binary and binary != "auto":
        return f"Antigravity CLI binary not found: {binary}"
    return "Antigravity CLI binary not found. Install agy or set cli_local.binary."
