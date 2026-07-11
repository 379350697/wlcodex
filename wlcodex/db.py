from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from wlcodex.models import (
    AgentRun,
    AgentRunStatus,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    BackendRequest,
    BackendRequestStatus,
    CarryoverEvidence,
    ConversationSession,
    OrchestrationDecision,
    OrchestrationRun,
    OrchestrationStatus,
    Task,
    TaskEvent,
    TaskStatus,
    TeamAgentJob,
    TeamArtifact,
    TeamAssignment,
    TeamContextPacketRecord,
    TeamInstinct,
    TeamObservation,
    TeamRun,
    TeamSkillActivation,
    TouchedFile,
    UsageEvent,
    WorkbenchCarryover,
)
from wlcodex.maintenance import (
    MaintenanceWindowStatus,
    assert_submissions_open as _assert_submissions_open,
    begin_maintenance_window as _begin_maintenance_window,
    cancel_maintenance_window as _cancel_maintenance_window,
    ensure_maintenance_schema,
    maintenance_window_status,
)
from wlcodex.runtime_raw_frame_retention import RETENTION_SCHEMA_SQL
from wlcodex.team_memory import InstinctMemory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Ledger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(path))

    def migrate(self) -> None:
        # Create tables idempotently
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_alias TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                codex_thread_id TEXT,
                active_turn_id TEXT,
                parent_task_id INTEGER,
                telegram_chat_id INTEGER,
                telegram_status_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_summary TEXT NOT NULL DEFAULT '',
                last_phase TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                changed_file_count INTEGER NOT NULL DEFAULT 0,
                pending_approval_count INTEGER NOT NULL DEFAULT 0,
                token_input INTEGER NOT NULL DEFAULT 0,
                token_output INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_workspace_status
                ON tasks(workspace_alias, status);

            CREATE TABLE IF NOT EXISTS task_thread_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_bindings_thread_id
                ON task_thread_bindings(thread_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_thread_bindings_task_thread
                ON task_thread_bindings(task_id, thread_id);

            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_task_events_task_id_id
                ON task_events(task_id, id);

            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                codex_request_id TEXT NOT NULL,
                codex_item_id TEXT,
                codex_turn_id TEXT,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                command_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                telegram_message_id INTEGER,
                resolution TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_approval_requests_task_id
                ON approval_requests(task_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_task_codex_id
                ON approval_requests(task_id, codex_request_id);

            CREATE TABLE IF NOT EXISTS touched_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                change_kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_touched_files_task_id
                ON touched_files(task_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_touched_files_unique
                ON touched_files(task_id, path, change_kind);

            CREATE TABLE IF NOT EXISTS backend_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jsonrpc_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                task_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS telegram_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_update_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                update_type TEXT NOT NULL,
                allowed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_telegram_updates_update_id
                ON telegram_updates(telegram_update_id);

            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'chief_engineer',
                workspace_alias TEXT NOT NULL,
                active_codex_task_id INTEGER,
                active_claude_run_id INTEGER,
                conversation_summary TEXT NOT NULL DEFAULT '',
                current_model TEXT NOT NULL DEFAULT '',
                codex_thread_id TEXT NOT NULL DEFAULT '',
                codex_thread_policy TEXT NOT NULL DEFAULT '',
                claude_session_id TEXT NOT NULL DEFAULT '',
                legacy_compatible INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_sessions_chat_id
                ON conversation_sessions(chat_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                agent TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                hidden_task_id INTEGER,
                external_session_id TEXT,
                prompt_packet_summary TEXT NOT NULL DEFAULT '',
                token_input INTEGER NOT NULL DEFAULT 0,
                token_output INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation_id
                ON agent_runs(conversation_id, id);

            CREATE TABLE IF NOT EXISTS native_codex_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                native_thread_id TEXT NOT NULL,
                agent_run_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL DEFAULT 'unknown',
                last_turn_id TEXT NOT NULL DEFAULT '',
                activity_at TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id),
                FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_native_codex_sessions_thread_id
                ON native_codex_sessions(native_thread_id);
            CREATE INDEX IF NOT EXISTS idx_native_codex_sessions_agent_run
                ON native_codex_sessions(agent_run_id);
            CREATE INDEX IF NOT EXISTS idx_native_codex_sessions_updated
                ON native_codex_sessions(updated_at DESC);

            CREATE TABLE IF NOT EXISTS native_agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_engine TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                agent_run_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'unknown',
                status TEXT NOT NULL DEFAULT 'unknown',
                last_turn_id TEXT NOT NULL DEFAULT '',
                activity_at TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(agent_run_id) REFERENCES agent_runs(id),
                FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_native_agent_sessions_identity
                ON native_agent_sessions(provider, provider_engine, native_session_id);
            CREATE INDEX IF NOT EXISTS idx_native_agent_sessions_provider_updated
                ON native_agent_sessions(provider, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_native_agent_sessions_agent_run
                ON native_agent_sessions(agent_run_id);

            CREATE TABLE IF NOT EXISTS collaboration_workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_run_id TEXT NOT NULL UNIQUE,
                workflow_type TEXT NOT NULL,
                status TEXT NOT NULL,
                source_provider TEXT NOT NULL,
                source_thread_id TEXT NOT NULL,
                source_turn_id TEXT NOT NULL DEFAULT '',
                target_provider TEXT NOT NULL DEFAULT '',
                target_thread_id TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_runs_source
                ON collaboration_workflow_runs(source_provider, source_thread_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_runs_target
                ON collaboration_workflow_runs(target_provider, target_thread_id, id DESC);

            CREATE TABLE IF NOT EXISTS collaboration_workflow_previews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preview_id TEXT NOT NULL UNIQUE,
                workflow_run_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                target_provider TEXT NOT NULL,
                prompt TEXT NOT NULL,
                artifacts_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY(workflow_run_id) REFERENCES collaboration_workflow_runs(workflow_run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_previews_run
                ON collaboration_workflow_previews(workflow_run_id, id DESC);

            CREATE TABLE IF NOT EXISTS collaboration_workflow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_id TEXT NOT NULL UNIQUE,
                workflow_run_id TEXT NOT NULL,
                preview_id TEXT NOT NULL DEFAULT '',
                step_type TEXT NOT NULL,
                status TEXT NOT NULL,
                assigned_provider TEXT NOT NULL,
                target_thread_id TEXT NOT NULL DEFAULT '',
                target_agent_run_id INTEGER NOT NULL DEFAULT 0,
                submitted_prompt TEXT NOT NULL DEFAULT '',
                output_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(workflow_run_id) REFERENCES collaboration_workflow_runs(workflow_run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_collaboration_workflow_steps_run
                ON collaboration_workflow_steps(workflow_run_id, id);

            CREATE TABLE IF NOT EXISTS orchestration_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                current_step TEXT NOT NULL DEFAULT '',
                verify_round INTEGER NOT NULL DEFAULT 0,
                max_verify_rounds INTEGER NOT NULL DEFAULT 0,
                last_codex_analysis TEXT NOT NULL DEFAULT '',
                last_claude_summary TEXT NOT NULL DEFAULT '',
                last_verification_result TEXT NOT NULL DEFAULT '',
                diagnose_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_orchestration_runs_conversation_id
                ON orchestration_runs(conversation_id, id);

            CREATE TABLE IF NOT EXISTS orchestration_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                next_agent TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES orchestration_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_orchestration_decisions_run_id
                ON orchestration_decisions(run_id, id);

            CREATE TABLE IF NOT EXISTS team_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                orchestration_run_id INTEGER,
                goal TEXT NOT NULL,
                route TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_agent_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                model_profile TEXT NOT NULL,
                status TEXT NOT NULL,
                agent_run_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_context_packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                agent_job_id INTEGER NOT NULL,
                packet_json TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                agent_job_id INTEGER,
                artifact_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relay_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                round_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                trigger_kind TEXT NOT NULL DEFAULT '',
                trigger_artifact_id INTEGER,
                route TEXT NOT NULL DEFAULT '',
                required_roles_json TEXT NOT NULL DEFAULT '[]',
                execution_mode TEXT NOT NULL DEFAULT 'simple',
                execution_goal TEXT NOT NULL DEFAULT '',
                execution_strategy_json TEXT NOT NULL DEFAULT '{}',
                waiting_reason TEXT NOT NULL DEFAULT 'none',
                confirmation_source TEXT NOT NULL DEFAULT '',
                confirmation_kind TEXT NOT NULL DEFAULT '',
                confirmation_role TEXT NOT NULL DEFAULT '',
                confirmation_provider TEXT NOT NULL DEFAULT '',
                confirmation_provider_request_id TEXT NOT NULL DEFAULT '',
                confirmation_runtime_event_id INTEGER NOT NULL DEFAULT 0,
                confirmation_native_session_id TEXT NOT NULL DEFAULT '',
                confirmation_agent_run_id INTEGER,
                confirmation_turn_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                UNIQUE(team_run_id, round_id),
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_rounds_task_status
                ON relay_rounds(team_run_id, status, round_id);

            CREATE TABLE IF NOT EXISTS relay_stream_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                job_id INTEGER,
                runtime_event_id INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(task_id, sequence),
                FOREIGN KEY(task_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_stream_events_task_sequence
                ON relay_stream_events(task_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_relay_stream_events_runtime_event
                ON relay_stream_events(runtime_event_id);

            CREATE TABLE IF NOT EXISTS relay_role_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                round_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                native_session_id TEXT NOT NULL DEFAULT '',
                agent_run_id INTEGER,
                turn_id TEXT NOT NULL DEFAULT '',
                active_turn_id TEXT NOT NULL DEFAULT '',
                dispatch_artifact_id INTEGER,
                completion_event_id INTEGER,
                completion_artifact_id INTEGER,
                error_artifact_id INTEGER,
                retry_count INTEGER NOT NULL DEFAULT 0,
                execution_mode TEXT NOT NULL DEFAULT 'simple',
                team_strategy TEXT NOT NULL DEFAULT 'none',
                provider_mode_json TEXT NOT NULL DEFAULT '{}',
                provider_child_activity_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                UNIQUE(team_run_id, round_id, role, attempt_no),
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_attempts_task_round_role
                ON relay_role_attempts(team_run_id, round_id, role, attempt_no);
            CREATE INDEX IF NOT EXISTS idx_relay_attempts_agent_run
                ON relay_role_attempts(agent_run_id, active_turn_id);

            -- A durable, crash-safe claim for provider completion events.
            -- Lifecycle reconciliation is a background mutation and must be
            -- restart-safe: an event may be observed repeatedly, but its
            -- artifact/projection may only be applied once.
            CREATE TABLE IF NOT EXISTS relay_completion_claims (
                team_run_id INTEGER NOT NULL,
                round_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                runtime_event_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed',
                artifact_id INTEGER,
                claimed_at TEXT NOT NULL,
                applied_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(team_run_id, round_id, role, runtime_event_id),
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_completion_claims_status
                ON relay_completion_claims(status, updated_at);

            -- ``relay_completion_claims`` predates runtime replay and keys
            -- a claim by the mutable current round.  Keep it for historical
            -- audit compatibility, but make all new projection claims use a
            -- stable event/agent-run identity.  A completion can advance the
            -- task into another round before its final claim write, so round
            -- identity must never be used to decide whether to replay it.
            CREATE TABLE IF NOT EXISTS relay_completion_event_claims (
                team_run_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                runtime_event_id INTEGER NOT NULL DEFAULT 0,
                agent_run_id INTEGER,
                role TEXT NOT NULL,
                claimed_round_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed',
                artifact_id INTEGER,
                claimed_at TEXT NOT NULL,
                applied_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(team_run_id, event_key),
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_completion_event_claims_runtime
                ON relay_completion_event_claims(team_run_id, runtime_event_id)
                WHERE runtime_event_id > 0;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_completion_event_claims_agent
                ON relay_completion_event_claims(team_run_id, agent_run_id)
                WHERE agent_run_id IS NOT NULL AND agent_run_id > 0;
            CREATE INDEX IF NOT EXISTS idx_relay_completion_event_claims_status
                ON relay_completion_event_claims(status, updated_at);

            -- Goal-mode completion is grounded in durable acceptance attempts,
            -- never a model's free-form claim.  Each row binds independent
            -- evidence to one concrete implementation artifact/run and keeps
            -- the normalized declaration plus the actual controlled execution.
            CREATE TABLE IF NOT EXISTS relay_goal_acceptance_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                round_id INTEGER NOT NULL,
                implementation_artifact_id INTEGER,
                implementation_run_id INTEGER,
                verifier_artifact_id INTEGER,
                verifier_role TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                test_declaration_json TEXT NOT NULL DEFAULT '{}',
                test_execution_json TEXT NOT NULL DEFAULT '{}',
                exit_code INTEGER,
                status TEXT NOT NULL DEFAULT 'not_run',
                evidence_status TEXT NOT NULL DEFAULT 'not_run',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id),
                UNIQUE(
                    team_run_id,
                    round_id,
                    implementation_artifact_id,
                    verifier_role,
                    attempt_no
                )
            );

            CREATE INDEX IF NOT EXISTS idx_relay_goal_acceptance_task_round
                ON relay_goal_acceptance_records(team_run_id, round_id, id);
            CREATE INDEX IF NOT EXISTS idx_relay_goal_acceptance_run
                ON relay_goal_acceptance_records(
                    team_run_id,
                    round_id,
                    implementation_run_id,
                    verifier_role,
                    id
                );

            CREATE TABLE IF NOT EXISTS relay_pending_inputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                queued_after_round_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                text TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                consumed_round_id INTEGER,
                steered_round_id INTEGER,
                steered_role TEXT NOT NULL DEFAULT '',
                steered_attempt_no INTEGER,
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_pending_inputs_task_status
                ON relay_pending_inputs(team_run_id, status, queued_after_round_id, id);

            -- A pending follow-up is consumed only after its workspace has
            -- been claimed.  The lease is deliberately separate from the
            -- input record so historical inputs remain replayable and a
            -- crashed worker can be retried after its lease expires.
            CREATE TABLE IF NOT EXISTS relay_workspace_queue_leases (
                pending_input_id INTEGER PRIMARY KEY,
                team_run_id INTEGER NOT NULL,
                workspace TEXT NOT NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(pending_input_id) REFERENCES relay_pending_inputs(id),
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_workspace_queue_leases_claim
                ON relay_workspace_queue_leases(workspace, lease_expires_at, pending_input_id);

            -- One workspace may have only one provider-facing follow-up in
            -- flight.  The per-input lease above remains the retry/audit
            -- record; this separate workspace key is the concurrency fence
            -- that makes the guarantee hold across different Relay tasks.
            CREATE TABLE IF NOT EXISTS relay_workspace_queue_locks (
                workspace TEXT PRIMARY KEY,
                pending_input_id INTEGER NOT NULL UNIQUE,
                team_run_id INTEGER NOT NULL,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(pending_input_id) REFERENCES relay_pending_inputs(id),
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_workspace_queue_locks_expiry
                ON relay_workspace_queue_locks(lease_expires_at, workspace);

            -- Task creation needs the same cross-process workspace fence as
            -- queued follow-ups.  This lease covers only the short critical
            -- section from observing active work through an optional verified
            -- interrupt and durable task creation; provider calls never run
            -- inside its SQLite transaction.
            CREATE TABLE IF NOT EXISTS relay_workspace_creation_leases (
                workspace TEXT PRIMARY KEY,
                lease_owner TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_relay_workspace_creation_leases_expiry
                ON relay_workspace_creation_leases(lease_expires_at, workspace);

            -- Archiving is a view preference, not a lifecycle transition.
            -- It hides a finished or stale Relay task from the default inbox
            -- while retaining every task, event, artifact and timeline row.
            CREATE TABLE IF NOT EXISTS relay_task_archives (
                team_run_id INTEGER PRIMARY KEY,
                archived_at TEXT NOT NULL,
                archived_by TEXT NOT NULL DEFAULT 'user',
                reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(team_run_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_task_archives_archived_at
                ON relay_task_archives(archived_at DESC, team_run_id);

            -- Durable mutation idempotency keeps a network retry from
            -- creating duplicate tasks, inputs or role controls.  A key is
            -- scoped by its mutation name and request fingerprint so an
            -- accidental key reuse is rejected instead of replayed blindly.
            CREATE TABLE IF NOT EXISTS relay_mutation_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                task_id INTEGER,
                request_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                response_status INTEGER,
                response_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES team_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_mutation_idempotency_status
                ON relay_mutation_idempotency(status, updated_at);

            CREATE TABLE IF NOT EXISTS team_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                model_profile TEXT NOT NULL,
                selected_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_skill_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                agent_job_id INTEGER NOT NULL,
                activation_type TEXT NOT NULL,
                activation_id TEXT NOT NULL,
                source TEXT NOT NULL,
                token_cost INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_run_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                summary TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_instincts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instinct_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                workspace_alias TEXT,
                role TEXT NOT NULL,
                domain TEXT NOT NULL,
                trigger TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_validated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_team_runs_conversation
                ON team_runs(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_runs_orchestration
                ON team_runs(orchestration_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_agent_jobs_team
                ON team_agent_jobs(team_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_context_packets_job
                ON team_context_packets(agent_job_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_artifacts_team
                ON team_artifacts(team_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_artifacts_team_created
                ON team_artifacts(team_run_id, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_team_skill_activations_job
                ON team_skill_activations(agent_job_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_observations_team
                ON team_observations(team_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_team_instincts_scope
                ON team_instincts(scope, workspace_alias, role, status);

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                conversation_id INTEGER,
                orchestration_run_id INTEGER,
                agent_run_id INTEGER,
                task_id INTEGER,
                agent TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                phase TEXT NOT NULL DEFAULT '',
                request_kind TEXT NOT NULL DEFAULT '',
                request_index INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT '',
                external_thread_id TEXT,
                external_turn_id TEXT,
                external_session_id TEXT,
                status TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'estimated',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                workflow_overhead_input_tokens INTEGER NOT NULL DEFAULT 0,
                workflow_overhead_output_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_usage_events_conversation_id
                ON usage_events(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_usage_events_orchestration_run_id
                ON usage_events(orchestration_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_usage_events_agent_run_id
                ON usage_events(agent_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_usage_events_task_id
                ON usage_events(task_id, id);
            CREATE INDEX IF NOT EXISTS idx_usage_events_agent
                ON usage_events(agent, created_at);

            CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                conversation_id INTEGER,
                orchestration_run_id INTEGER,
                agent_run_id INTEGER,
                task_id INTEGER,
                correlation_id TEXT NOT NULL,
                causation_id INTEGER,
                source TEXT NOT NULL,
                actor TEXT NOT NULL,
                visibility TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_events_correlation
                ON runtime_events(correlation_id, id);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_aggregate
                ON runtime_events(aggregate_type, aggregate_id, id);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_conversation
                ON runtime_events(conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_orchestration_run
                ON runtime_events(orchestration_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_agent_run
                ON runtime_events(agent_run_id, id);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_event_type
                ON runtime_events(event_type, id);

            CREATE TABLE IF NOT EXISTS provider_raw_frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_engine TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                native_turn_id TEXT NOT NULL DEFAULT '',
                sequence INTEGER NOT NULL,
                raw_kind TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                conversation_id INTEGER,
                orchestration_run_id INTEGER,
                agent_run_id INTEGER,
                task_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_provider_raw_frames_session
                ON provider_raw_frames(
                    provider, provider_engine, native_session_id, native_turn_id, sequence
                );
            CREATE INDEX IF NOT EXISTS idx_provider_raw_frames_agent_run
                ON provider_raw_frames(agent_run_id, id);
            -- Retention walks old frames in this exact order.  Without this
            -- cursor index, the first large migration repeatedly sorts the
            -- entire hot table for each 250-frame page.
            CREATE INDEX IF NOT EXISTS idx_provider_raw_frames_retention_scan
                ON provider_raw_frames(occurred_at, id);

            CREATE TABLE IF NOT EXISTS workbench_carryovers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                source_conversation_id INTEGER NOT NULL,
                target_conversation_id INTEGER,
                workspace_alias TEXT NOT NULL,
                brief_text TEXT NOT NULL DEFAULT '',
                preview_text TEXT NOT NULL DEFAULT '',
                source_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                used_at TEXT,
                FOREIGN KEY(source_conversation_id) REFERENCES conversation_sessions(id),
                FOREIGN KEY(target_conversation_id) REFERENCES conversation_sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_workbench_carryovers_chat_status
                ON workbench_carryovers(chat_id, status, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_workbench_carryovers_source
                ON workbench_carryovers(source_conversation_id, updated_at DESC);
            """
        )

        # Raw provider frames are the only replay payload governed by the
        # retention policy.  These idempotent tables make archive writes
        # discoverable before a hot row is removed and preserve sequence
        # continuity after the hot table has been pruned.
        self._conn.executescript(RETENTION_SCHEMA_SQL)
        self._add_column_if_missing(
            "provider_raw_frame_archives",
            "purge_pending_at",
            "purge_pending_at TEXT NOT NULL DEFAULT ''",
        )
        # The maintenance singleton is deliberately separate from ordinary
        # settings: it is a durable operator gate that freezes submissions
        # before the raw-frame archive / SQLite swap window begins.
        ensure_maintenance_schema(self._conn)

        # Guarded column upgrades for legacy databases that already have
        # a tasks table but lack columns added after the initial schema.
        self._add_column_if_missing(
            "tasks", "active_turn_id", "active_turn_id TEXT"
        )
        self._add_column_if_missing(
            "tasks", "parent_task_id", "parent_task_id INTEGER"
        )
        self._add_column_if_missing(
            "tasks", "telegram_chat_id", "telegram_chat_id INTEGER"
        )
        self._add_column_if_missing(
            "tasks", "telegram_status_message_id", "telegram_status_message_id INTEGER"
        )
        self._add_column_if_missing(
            "tasks", "last_summary", "last_summary TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "last_phase", "last_phase TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "last_error", "last_error TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "changed_file_count", "changed_file_count INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "pending_approval_count", "pending_approval_count INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "token_input", "token_input INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "token_output", "token_output INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing(
            "tasks", "worktree_path", "worktree_path TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "worktree_branch", "worktree_branch TEXT NOT NULL DEFAULT ''"
        )
        self._add_column_if_missing(
            "tasks", "is_force_parallel", "is_force_parallel INTEGER NOT NULL DEFAULT 0"
        )
        # Approval request columns
        self._add_column_if_missing(
            "approval_requests", "telegram_message_id", "telegram_message_id INTEGER"
        )
        self._add_column_if_missing(
            "approval_requests", "resolution", "resolution TEXT"
        )
        self._add_column_if_missing(
            "approval_requests", "resolved_at", "resolved_at TEXT"
        )
        # Agent run completion summary for verification evidence
        self._add_column_if_missing(
            "agent_runs", "completion_summary",
            "completion_summary TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "conversation_sessions", "codex_thread_id",
            "codex_thread_id TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "conversation_sessions", "codex_thread_policy",
            "codex_thread_policy TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "conversation_sessions", "claude_session_id",
            "claude_session_id TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "conversation_sessions", "legacy_compatible",
            "legacy_compatible INTEGER NOT NULL DEFAULT 0",
        )
        legacy_marker = self._conn.execute(
            "SELECT 1 FROM schema_meta WHERE key = 'legacy_conversations_marked'"
        ).fetchone()
        if legacy_marker is None:
            # Rows present at this migration boundary are the old Telegram
            # Workbench contract.  Preserve them; conversations created after
            # this release deliberately default to the new Native/Relay entry.
            self._conn.execute(
                "UPDATE conversation_sessions SET legacy_compatible = 1"
            )
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                ("legacy_conversations_marked", "1"),
            )
        self._add_column_if_missing(
            "native_codex_sessions", "activity_at",
            "activity_at TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "native_codex_sessions", "metadata_json",
            "metadata_json TEXT NOT NULL DEFAULT '{}'",
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_native_codex_sessions_activity
                ON native_codex_sessions(activity_at DESC, id DESC)
            """
        )
        # orchestration_runs.diagnose_json — added for structured LightFeeV2
        # diagnose artifact storage (old DBs created before this column must
        # be upgraded so Telegram digest can consume structured facts).
        self._add_column_if_missing(
            "orchestration_runs", "diagnose_json",
            "diagnose_json TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "execution_mode",
            "execution_mode TEXT NOT NULL DEFAULT 'simple'",
        )
        self._add_column_if_missing(
            "relay_rounds", "execution_goal",
            "execution_goal TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "execution_strategy_json",
            "execution_strategy_json TEXT NOT NULL DEFAULT '{}'",
        )
        self._add_column_if_missing(
            "relay_rounds", "waiting_reason",
            "waiting_reason TEXT NOT NULL DEFAULT 'none'",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_source",
            "confirmation_source TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_kind",
            "confirmation_kind TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_role",
            "confirmation_role TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_provider",
            "confirmation_provider TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_provider_request_id",
            "confirmation_provider_request_id TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_runtime_event_id",
            "confirmation_runtime_event_id INTEGER NOT NULL DEFAULT 0",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_native_session_id",
            "confirmation_native_session_id TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_agent_run_id",
            "confirmation_agent_run_id INTEGER",
        )
        self._add_column_if_missing(
            "relay_rounds", "confirmation_turn_id",
            "confirmation_turn_id TEXT NOT NULL DEFAULT ''",
        )
        self._add_column_if_missing(
            "relay_role_attempts", "execution_mode",
            "execution_mode TEXT NOT NULL DEFAULT 'simple'",
        )
        self._add_column_if_missing(
            "relay_role_attempts", "team_strategy",
            "team_strategy TEXT NOT NULL DEFAULT 'none'",
        )
        self._add_column_if_missing(
            "relay_role_attempts", "provider_mode_json",
            "provider_mode_json TEXT NOT NULL DEFAULT '{}'",
        )
        self._add_column_if_missing(
            "relay_role_attempts", "provider_child_activity_json",
            "provider_child_activity_json TEXT NOT NULL DEFAULT '{}'",
        )
        # Seed the immutable completion-identity table before a replaying
        # worker sees an old, already-applied round-scoped claim.  Runtime
        # event ids are globally durable; when available, use their agent-run
        # id as the primary replay identity as well.
        self._conn.execute(
            """
            INSERT OR IGNORE INTO relay_completion_event_claims (
                team_run_id, event_key, runtime_event_id, agent_run_id, role,
                claimed_round_id, status, artifact_id, claimed_at, applied_at,
                updated_at
            )
            SELECT legacy.team_run_id,
                   CASE
                       WHEN COALESCE(runtime.agent_run_id, 0) > 0
                           THEN 'agent:' || runtime.agent_run_id
                       ELSE 'runtime:' || legacy.runtime_event_id
                   END,
                   legacy.runtime_event_id,
                   runtime.agent_run_id,
                   legacy.role,
                   legacy.round_id,
                   legacy.status,
                   legacy.artifact_id,
                   legacy.claimed_at,
                   legacy.applied_at,
                   legacy.updated_at
            FROM relay_completion_claims AS legacy
            LEFT JOIN runtime_events AS runtime
                ON runtime.id = legacy.runtime_event_id
            WHERE legacy.runtime_event_id > 0
            """
        )
        # codex_request_id values are scoped to a task/thread by the app-server.
        # Older databases had a global unique index, which incorrectly dropped
        # approvals when different tasks reused ids like "0" or "1".
        self._conn.execute("DROP INDEX IF EXISTS idx_approval_requests_codex_id")
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_task_codex_id
                ON approval_requests(task_id, codex_request_id)
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO task_thread_bindings (task_id, thread_id, created_at)
            SELECT id, codex_thread_id, updated_at
            FROM tasks
            WHERE codex_thread_id IS NOT NULL AND codex_thread_id != ''
            """
        )

        # Record schema version
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", "7"),
        )

        self._conn.commit()

    # --- Tasks ---

    def create_task(
        self,
        workspace_alias: str,
        workspace_path: str,
        title: str,
        codex_thread_id: str | None,
        parent_task_id: int | None,
        telegram_chat_id: int | None = None,
        status: TaskStatus = TaskStatus.QUEUED,
    ) -> Task:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO tasks (
                workspace_alias, workspace_path, title, status, codex_thread_id,
                active_turn_id, parent_task_id, telegram_chat_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                workspace_alias,
                workspace_path,
                title,
                status.value,
                codex_thread_id,
                parent_task_id,
                telegram_chat_id,
                now,
                now,
            ),
        )
        task_id = int(cur.lastrowid)
        if codex_thread_id:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO task_thread_bindings (task_id, thread_id, created_at)
                VALUES (?, ?, ?)
                """,
                (task_id, codex_thread_id, now),
            )
        self._conn.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task id: {task_id}")
        return _task(row)

    def list_tasks(
        self, limit: int = 20, include_archived: bool = False
    ) -> list[Task]:
        if include_archived:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status != ? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (TaskStatus.ARCHIVED.value, limit),
            ).fetchall()
        return [_task(row) for row in rows]

    def set_task_status(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        phase: str = "",
        summary: str = "",
        error: str = "",
    ) -> None:
        self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, last_phase = ?, last_summary = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status.value, phase, summary, error, _now(), task_id),
        )
        self._conn.commit()

    def set_active_turn(self, task_id: int, turn_id: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET active_turn_id = ?, updated_at = ? WHERE id = ?",
            (turn_id, _now(), task_id),
        )
        self._conn.commit()

    def clear_active_turn(self, task_id: int) -> None:
        self._conn.execute(
            "UPDATE tasks SET active_turn_id = NULL, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        self._conn.commit()

    def set_status_message(
        self, task_id: int, chat_id: int, message_id: int
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET telegram_chat_id = ?, telegram_status_message_id = ?, updated_at = ? WHERE id = ?",
            (chat_id, message_id, _now(), task_id),
        )
        self._conn.commit()

    def increment_changed_files(self, task_id: int, delta: int = 1) -> None:
        self._conn.execute(
            "UPDATE tasks SET changed_file_count = changed_file_count + ?, updated_at = ? WHERE id = ?",
            (delta, _now(), task_id),
        )
        self._conn.commit()

    def increment_pending_approvals(self, task_id: int, delta: int = 1) -> None:
        self._conn.execute(
            "UPDATE tasks SET pending_approval_count = pending_approval_count + ?, updated_at = ? WHERE id = ?",
            (delta, _now(), task_id),
        )
        self._conn.commit()

    def set_token_usage(self, task_id: int, token_input: int, token_output: int) -> None:
        self._conn.execute(
            "UPDATE tasks SET token_input = ?, token_output = ?, updated_at = ? WHERE id = ?",
            (token_input, token_output, _now(), task_id),
        )
        self._conn.commit()

    # --- Events ---

    def add_event(self, task_id: int, event_type: str, payload: dict[str, Any]) -> TaskEvent:
        self._conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, event_type, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self._conn.commit()
        return self.list_events(task_id)[-1]

    def list_events(self, task_id: int, limit: int = 200) -> list[TaskEvent]:
        rows = self._conn.execute(
            """
            SELECT * FROM task_events
            WHERE task_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        return [_event(row) for row in rows]

    # --- Approval requests ---

    def create_approval(
        self,
        task_id: int,
        codex_request_id: str,
        codex_item_id: str | None,
        codex_turn_id: str | None,
        kind: ApprovalKind | str,
        summary: str,
        command_json: str = "{}",
        telegram_message_id: int | None = None,
    ) -> ApprovalRequest:
        kind_str = kind.value if isinstance(kind, ApprovalKind) else kind
        now = _now()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO approval_requests (
                task_id, codex_request_id, codex_item_id, codex_turn_id,
                kind, summary, command_json, status, telegram_message_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                task_id, codex_request_id, codex_item_id, codex_turn_id,
                kind_str, summary, command_json, telegram_message_id, now,
            ),
        )
        self._conn.commit()
        approval = self.get_approval_by_codex_id(codex_request_id, task_id=task_id)
        if approval is None:
            raise KeyError(
                f"unknown approval for task #{task_id} codex_request_id: {codex_request_id}"
            )
        return approval

    def get_approval(self, approval_id: int) -> ApprovalRequest:
        row = self._conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown approval id: {approval_id}")
        return _approval(row)

    def get_approval_by_codex_id(
        self, codex_request_id: str, *, task_id: int | None = None
    ) -> ApprovalRequest | None:
        if task_id is not None:
            row = self._conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE task_id = ? AND codex_request_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id, codex_request_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM approval_requests
                WHERE codex_request_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (codex_request_id,),
            ).fetchone()
        return _approval(row) if row else None

    def resolve_approval(
        self, approval_id: int, status: ApprovalStatus | str, resolution: str = ""
    ) -> ApprovalRequest:
        status_str = status.value if isinstance(status, ApprovalStatus) else status
        now = _now()
        cur = self._conn.execute(
            """
            UPDATE approval_requests
            SET status = ?, resolution = ?, resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status_str, resolution, now, approval_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            # Already resolved — return existing
            return self.get_approval(approval_id)
        return self.get_approval(approval_id)

    def pending_approvals(self, task_id: int) -> list[ApprovalRequest]:
        rows = self._conn.execute(
            """
            SELECT * FROM approval_requests
            WHERE task_id = ? AND status = 'pending'
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
        return [_approval(row) for row in rows]

    # --- Touched files ---

    def record_touched_file(
        self, task_id: int, path: str, change_kind: str
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO touched_files (task_id, path, change_kind, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, path, change_kind, _now()),
        )
        self._conn.commit()

    def list_touched_files(self, task_id: int) -> list[TouchedFile]:
        rows = self._conn.execute(
            "SELECT * FROM touched_files WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [_touched(row) for row in rows]

    # --- Backend requests ---

    def create_backend_request(
        self, jsonrpc_id: int, method: str, task_id: int | None = None
    ) -> BackendRequest:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO backend_requests (jsonrpc_id, method, task_id, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (jsonrpc_id, method, task_id, BackendRequestStatus.PENDING.value, now),
        )
        self._conn.commit()
        return self.get_backend_request(int(cur.lastrowid))

    def complete_backend_request(
        self, request_id: int, error: str | None = None
    ) -> None:
        status = BackendRequestStatus.FAILED.value if error else BackendRequestStatus.COMPLETED.value
        self._conn.execute(
            "UPDATE backend_requests SET status = ?, completed_at = ?, error = ? WHERE id = ?",
            (status, _now(), error or "", request_id),
        )
        self._conn.commit()

    def get_backend_request(self, request_id: int) -> BackendRequest:
        row = self._conn.execute(
            "SELECT * FROM backend_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown backend request id: {request_id}")
        return _backend_request(row)

    # --- Telegram updates ---

    def record_telegram_update(
        self, update_id: int, user_id: int, chat_id: int, update_type: str, allowed: bool
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO telegram_updates (telegram_update_id, user_id, chat_id, update_type, allowed, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (update_id, user_id, chat_id, update_type, int(allowed), _now()),
        )
        self._conn.commit()

    # --- Runtime settings ---

    def get_runtime_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM runtime_settings WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_runtime_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, _now()),
        )
        self._conn.commit()

    # --- Maintenance window ---

    def maintenance_window_status(self) -> MaintenanceWindowStatus:
        """Read the global submission gate and current drain state."""

        return maintenance_window_status(self._conn)

    def begin_maintenance_window(self, *, operator_note: str = "") -> MaintenanceWindowStatus:
        """Freeze new user submissions and report work still draining."""

        return _begin_maintenance_window(self._conn, operator_note=operator_note)

    def cancel_maintenance_window(self) -> MaintenanceWindowStatus:
        """Cancel a maintenance attempt and re-open normal submissions."""

        return _cancel_maintenance_window(self._conn)

    def assert_submissions_open(self) -> None:
        """Raise before a user-originated execution reservation is created."""

        _assert_submissions_open(self._conn)

    # --- Column upgrade helpers ---

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _add_column_if_missing(self, table: str, column: str, ddl: str) -> None:
        if column not in self._table_columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # --- Thread id helper ---

    def set_thread_id(self, task_id: int, codex_thread_id: str) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE tasks SET codex_thread_id = ?, updated_at = ? WHERE id = ?",
            (codex_thread_id, now, task_id),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO task_thread_bindings (task_id, thread_id, created_at)
            VALUES (?, ?, ?)
            """,
            (task_id, codex_thread_id, now),
        )
        self._conn.commit()

    def list_task_thread_ids(self, task_id: int) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT thread_id FROM task_thread_bindings
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()
        return [str(row["thread_id"]) for row in rows]

    def find_task_by_thread_id(self, thread_id: str) -> Task | None:
        row = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE codex_thread_id = ?
            ORDER BY
                CASE
                    WHEN status IN ('queued', 'running', 'waiting_approval', 'paused') THEN 0
                    ELSE 1
                END,
                updated_at DESC,
                id DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """
                SELECT tasks.* FROM tasks
                JOIN task_thread_bindings ON task_thread_bindings.task_id = tasks.id
                WHERE task_thread_bindings.thread_id = ?
                ORDER BY
                    CASE
                        WHEN tasks.status IN ('queued', 'running', 'waiting_approval', 'paused') THEN 0
                        ELSE 1
                    END,
                    tasks.updated_at DESC,
                    tasks.id DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        return _task(row) if row is not None else None

    def set_worktree_info(
        self, task_id: int, worktree_path: str, worktree_branch: str
    ) -> None:
        self._conn.execute(
            "UPDATE tasks SET worktree_path = ?, worktree_branch = ?, updated_at = ? WHERE id = ?",
            (worktree_path, worktree_branch, _now(), task_id),
        )
        self._conn.commit()

    def set_force_parallel(self, task_id: int) -> None:
        self._conn.execute(
            "UPDATE tasks SET is_force_parallel = 1, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        self._conn.commit()

    # --- Pending approval helpers ---

    def decrement_pending_approvals(self, task_id: int) -> None:
        self._conn.execute(
            """
            UPDATE tasks
            SET pending_approval_count = CASE
                WHEN pending_approval_count > 0 THEN pending_approval_count - 1
                ELSE 0
            END,
            updated_at = ?
            WHERE id = ?
            """,
            (_now(), task_id),
        )
        self._conn.commit()

    def set_approval_error(self, approval_id: int, error: str) -> None:
        self._conn.execute(
            "UPDATE approval_requests SET resolution = ?, resolved_at = ? WHERE id = ?",
            (f"error: {error[:240]}", _now(), approval_id),
        )
        self._conn.commit()

    # --- Recovery ---

    def mark_active_tasks_recovery_paused(self) -> list[int]:
        """Mark running/queued/waiting_approval tasks as paused on startup.

        Returns the list of task ids that were paused.
        """
        rows = self._conn.execute(
            """
            SELECT id, status FROM tasks
            WHERE status IN (?, ?, ?)
            """,
            (TaskStatus.RUNNING.value, TaskStatus.QUEUED.value, TaskStatus.WAITING_APPROVAL.value),
        ).fetchall()

        paused_ids = [int(row["id"]) for row in rows]
        if paused_ids:
            now = _now()
            placeholders = ",".join("?" for _ in paused_ids)
            self._conn.execute(
                f"UPDATE tasks SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                (TaskStatus.PAUSED.value, now, *paused_ids),
            )
            self._conn.commit()

            for task_id in paused_ids:
                self.add_event(
                    task_id,
                    "recovery_paused",
                    {"previous_status": dict(row)["status"] for row in rows if int(row["id"]) == task_id},
                )

        return paused_ids

    def mark_hanging_conversation_runs_recovery(self) -> tuple[int, int]:
        """Mark running orchestration_runs and agent_runs as failed on startup.

        Returns (orchestration_runs_marked, agent_runs_marked).
        """
        now = _now()
        orch_marked = 0
        agent_marked = 0

        orch_rows = self._conn.execute(
            "SELECT id FROM orchestration_runs WHERE status = ?",
            (OrchestrationStatus.RUNNING.value,),
        ).fetchall()
        if orch_rows:
            ids = [int(r["id"]) for r in orch_rows]
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE orchestration_runs SET status = ?, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (OrchestrationStatus.FAILED.value, now, *ids),
            )
            orch_marked = len(ids)

        agent_rows = self._conn.execute(
            "SELECT id FROM agent_runs WHERE status = ?",
            (AgentRunStatus.RUNNING.value,),
        ).fetchall()
        if agent_rows:
            ids = [int(r["id"]) for r in agent_rows]
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE agent_runs SET status = ?, updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (AgentRunStatus.FAILED.value, now, *ids),
            )
            agent_marked = len(ids)

        if orch_marked or agent_marked:
            self._conn.commit()

        return orch_marked, agent_marked

    # --- Liveness helpers ---

    def list_active_tasks(self, limit: int = 100) -> list[Task]:
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE status IN (?, ?, ?, ?)
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
                TaskStatus.WAITING_APPROVAL.value,
                TaskStatus.PAUSED.value,
                limit,
            ),
        ).fetchall()
        return [_task(row) for row in rows]

    def list_waiting_tasks(self, workspace_alias: str) -> list[Task]:
        rows = self._conn.execute(
            """
            SELECT * FROM tasks
            WHERE workspace_alias = ? AND status = ?
            ORDER BY created_at ASC, id ASC
            """,
            (workspace_alias, TaskStatus.WAITING_SLOT.value),
        ).fetchall()
        return [_task(row) for row in rows]

    def mark_task_timeout(
        self,
        task_id: int,
        *,
        status: TaskStatus,
        age_seconds: int,
        threshold_seconds: int,
    ) -> Task:
        error = (
            f"task timed out in {status.value} after "
            f"{age_seconds}s (limit {threshold_seconds}s)"
        )
        self.set_task_status(
            task_id,
            TaskStatus.FAILED,
            phase="timeout",
            error=error[:240],
        )
        self.add_event(
            task_id,
            "task_timeout",
            {
                "status": status.value,
                "age_seconds": age_seconds,
                "threshold_seconds": threshold_seconds,
            },
        )
        self.clear_active_turn(task_id)
        return self.get_task(task_id)

    def mark_backend_dead(self, task_id: int, summary: str) -> Task:
        error = f"backend dead: {summary}"
        self.set_task_status(
            task_id,
            TaskStatus.FAILED,
            phase="backend_dead",
            error=error[:240],
        )
        self.add_event(task_id, "backend_dead", {"summary": summary[:500]})
        self.clear_active_turn(task_id)
        return self.get_task(task_id)


    # --- Conversations ---

    def create_conversation(
        self,
        chat_id: int,
        user_id: int,
        title: str,
        mode: str,
        workspace_alias: str,
        *,
        legacy_compatible: bool = False,
    ) -> ConversationSession:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO conversation_sessions (
                chat_id, user_id, title, mode, workspace_alias, legacy_compatible,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id,
                title,
                mode,
                workspace_alias,
                int(legacy_compatible),
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_conversation(int(cur.lastrowid))

    def get_conversation(self, conversation_id: int) -> ConversationSession:
        row = self._conn.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown conversation id: {conversation_id}")
        return _conversation(row)

    def get_active_conversation(self, chat_id: int) -> ConversationSession | None:
        row = self._conn.execute(
            """
            SELECT * FROM conversation_sessions
            WHERE chat_id = ? AND archived_at IS NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return _conversation(row) if row else None

    def set_active_conversation_mode(
        self, conversation_id: int, mode: str
    ) -> ConversationSession:
        self._conn.execute(
            "UPDATE conversation_sessions SET mode = ?, updated_at = ? WHERE id = ?",
            (mode, _now(), conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def set_conversation_workspace(
        self, conversation_id: int, workspace_alias: str
    ) -> ConversationSession:
        self._conn.execute(
            "UPDATE conversation_sessions SET workspace_alias = ?, updated_at = ? WHERE id = ?",
            (workspace_alias, _now(), conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def update_conversation_summary(
        self, conversation_id: int, summary: str
    ) -> ConversationSession:
        self._conn.execute(
            "UPDATE conversation_sessions SET conversation_summary = ?, updated_at = ? WHERE id = ?",
            (summary, _now(), conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def update_conversation_title(
        self, conversation_id: int, title: str
    ) -> ConversationSession:
        self._conn.execute(
            "UPDATE conversation_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def set_conversation_codex_thread(
        self,
        conversation_id: int,
        thread_id: str,
        policy_fingerprint: str | None = None,
    ) -> ConversationSession:
        if policy_fingerprint is None:
            self._conn.execute(
                """
                UPDATE conversation_sessions
                SET codex_thread_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (thread_id, _now(), conversation_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE conversation_sessions
                SET codex_thread_id = ?, codex_thread_policy = ?, updated_at = ?
                WHERE id = ?
                """,
                (thread_id, policy_fingerprint, _now(), conversation_id),
            )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def set_conversation_claude_session(
        self, conversation_id: int, session_id: str
    ) -> ConversationSession:
        self._conn.execute(
            "UPDATE conversation_sessions SET claude_session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, _now(), conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def archive_conversation(self, conversation_id: int) -> ConversationSession:
        now = _now()
        self._conn.execute(
            "UPDATE conversation_sessions SET archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def list_conversations(self, limit: int = 20) -> list[ConversationSession]:
        rows = self._conn.execute(
            "SELECT * FROM conversation_sessions ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_conversation(row) for row in rows]

    def list_conversations_by_chat(
        self, chat_id: int, limit: int = 20, include_archived: bool = False
    ) -> list[ConversationSession]:
        if include_archived:
            rows = self._conn.execute(
                """
                SELECT * FROM conversation_sessions
                WHERE chat_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM conversation_sessions
                WHERE chat_id = ? AND archived_at IS NULL
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [_conversation(row) for row in rows]

    def restore_conversation(self, conversation_id: int) -> ConversationSession:
        conversation = self.get_conversation(conversation_id)
        now = _now()
        self._conn.execute(
            """
            UPDATE conversation_sessions
            SET archived_at = ?, updated_at = ?
            WHERE chat_id = ? AND id != ? AND archived_at IS NULL
            """,
            (now, now, conversation.chat_id, conversation_id),
        )
        self._conn.execute(
            """
            UPDATE conversation_sessions
            SET archived_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (now, conversation_id),
        )
        self._conn.commit()
        return self.get_conversation(conversation_id)

    def set_conversation_active_task(
        self, conversation_id: int, task_id: int
    ) -> None:
        self._conn.execute(
            "UPDATE conversation_sessions SET active_codex_task_id = ?, updated_at = ? WHERE id = ?",
            (task_id, _now(), conversation_id),
        )
        self._conn.commit()

    def set_conversation_active_claude_run(
        self, conversation_id: int, agent_run_id: int
    ) -> None:
        self._conn.execute(
            "UPDATE conversation_sessions SET active_claude_run_id = ?, updated_at = ? WHERE id = ?",
            (agent_run_id, _now(), conversation_id),
        )
        self._conn.commit()

    # --- Agent runs ---

    def create_agent_run(
        self,
        conversation_id: int,
        agent: str,
        role: str,
        hidden_task_id: int | None = None,
        external_session_id: str | None = None,
        prompt_packet_summary: str = "",
    ) -> AgentRun:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO agent_runs (
                conversation_id, agent, role, status, hidden_task_id,
                external_session_id, prompt_packet_summary, created_at, updated_at
            )
            VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                conversation_id, agent, role, hidden_task_id,
                external_session_id, prompt_packet_summary, now, now,
            ),
        )
        self._conn.commit()
        return self.get_agent_run(int(cur.lastrowid))

    def get_agent_run(self, run_id: int) -> AgentRun:
        row = self._conn.execute(
            "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown agent run id: {run_id}")
        return _agent_run(row)

    def update_agent_run_status(
        self,
        run_id: int,
        status: str,
        token_input: int = 0,
        token_output: int = 0,
        external_session_id: str | None = None,
        completion_summary: str | None = None,
    ) -> AgentRun:
        sets: list[str] = ["status = ?", "token_input = ?", "token_output = ?"]
        params: list[object] = [status, token_input, token_output]

        if external_session_id is not None:
            sets.append("external_session_id = ?")
            params.append(external_session_id)
        if completion_summary is not None:
            sets.append("completion_summary = ?")
            params.append(completion_summary)

        sets.append("updated_at = ?")
        params.append(_now())
        params.append(run_id)

        self._conn.execute(
            f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        return self.get_agent_run(run_id)

    def list_agent_runs(
        self, conversation_id: int, limit: int = 50
    ) -> list[AgentRun]:
        rows = self._conn.execute(
            "SELECT * FROM agent_runs WHERE conversation_id = ? ORDER BY id ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_agent_run(row) for row in rows]

    def list_recent_agent_runs(
        self, conversation_id: int, limit: int = 50
    ) -> list[AgentRun]:
        """Return the most recent agent runs for a conversation (newest first).

        Unlike ``list_agent_runs`` which returns the oldest runs first, this
        method orders by ``id DESC`` so callers looking for the latest
        external_session_id, status, or other fact only see fresh data.
        """
        rows = self._conn.execute(
            "SELECT * FROM agent_runs WHERE conversation_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_agent_run(row) for row in rows]

    # --- Orchestration runs ---

    def create_orchestration_run(
        self,
        conversation_id: int,
        goal: str,
        max_verify_rounds: int = 0,
    ) -> OrchestrationRun:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO orchestration_runs (
                conversation_id, goal, status, current_step, max_verify_rounds, created_at, updated_at
            )
            VALUES (?, ?, 'running', '', ?, ?, ?)
            """,
            (conversation_id, goal, max_verify_rounds, now, now),
        )
        self._conn.commit()
        return self.get_orchestration_run(int(cur.lastrowid))

    def get_orchestration_run(self, run_id: int) -> OrchestrationRun:
        row = self._conn.execute(
            "SELECT * FROM orchestration_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown orchestration run id: {run_id}")
        return _orchestration_run(row)

    def update_orchestration_run(
        self,
        run_id: int,
        status: str | None = None,
        current_step: str | None = None,
        verify_round: int | None = None,
        last_codex_analysis: str | None = None,
        last_claude_summary: str | None = None,
        last_verification_result: str | None = None,
        diagnose_json: str | None = None,
    ) -> OrchestrationRun:
        now = _now()
        sets: list[str] = ["updated_at = ?"]
        params: list[object] = [now]

        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if current_step is not None:
            sets.append("current_step = ?")
            params.append(current_step)
        if verify_round is not None:
            sets.append("verify_round = ?")
            params.append(verify_round)
        if last_codex_analysis is not None:
            sets.append("last_codex_analysis = ?")
            params.append(last_codex_analysis)
        if last_claude_summary is not None:
            sets.append("last_claude_summary = ?")
            params.append(last_claude_summary)
        if last_verification_result is not None:
            sets.append("last_verification_result = ?")
            params.append(last_verification_result)
        if diagnose_json is not None:
            sets.append("diagnose_json = ?")
            params.append(diagnose_json)

        params.append(run_id)
        self._conn.execute(
            f"UPDATE orchestration_runs SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        return self.get_orchestration_run(run_id)

    def list_orchestration_runs(
        self, conversation_id: int, limit: int = 20
    ) -> list[OrchestrationRun]:
        rows = self._conn.execute(
            "SELECT * FROM orchestration_runs WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_orchestration_run(row) for row in rows]

    def task_has_running_orchestration(self, task_id: int) -> bool:
        """Return True if the task is managed by the old eager orchestration runner.

        Staged-auto orchestration runs manage their own task lifecycle — tasks
        under a staged-auto run must reach DONE/FAILED so the event bridge can
        advance stages.  This method therefore returns False when the running
        orchestration run is a staged-auto stage.
        """
        from wlcodex.auto_workflow import AUTO_STAGE_STEPS
        row = self._conn.execute(
            """
            SELECT o.current_step
            FROM conversation_sessions AS c
            JOIN orchestration_runs AS o ON o.conversation_id = c.id
            WHERE c.active_codex_task_id = ?
              AND o.status = 'running'
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        if row["current_step"] in AUTO_STAGE_STEPS:
            return False
        return True

    def get_latest_active_auto_run(
        self, conversation_id: int
    ) -> OrchestrationRun | None:
        """Find the latest orchestration run for this conversation that is
        in an active auto stage (running or needs_user).

        Includes completed steps so that notification helpers can send
        terminal buttons (e.g. after verification passes).

        Returns None if no active auto run exists.
        """
        from wlcodex.auto_workflow import AUTO_STAGE_STEPS

        steps = list(AUTO_STAGE_STEPS)
        placeholders = ", ".join("?" for _ in steps)
        rows = self._conn.execute(
            f"""
            SELECT * FROM orchestration_runs
            WHERE conversation_id = ?
              AND status IN ('running', 'needs_user')
              AND current_step IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (conversation_id, *steps),
        ).fetchall()
        if not rows:
            return None
        return _orchestration_run(rows[0])

    # --- Orchestration decisions ---

    def record_orchestration_decision(
        self,
        run_id: int,
        decision: str,
        reason: str = "",
        next_agent: str = "",
    ) -> OrchestrationDecision:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO orchestration_decisions (run_id, decision, reason, next_agent, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, decision, reason, next_agent, now),
        )
        self._conn.commit()
        return self.get_orchestration_decision(int(cur.lastrowid))

    def get_orchestration_decision(self, decision_id: int) -> OrchestrationDecision:
        row = self._conn.execute(
            "SELECT * FROM orchestration_decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown orchestration decision id: {decision_id}")
        return _orchestration_decision(row)

    def list_orchestration_decisions(
        self, run_id: int, limit: int = 50
    ) -> list[OrchestrationDecision]:
        rows = self._conn.execute(
            "SELECT * FROM orchestration_decisions WHERE run_id = ? ORDER BY id ASC LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [_orchestration_decision(row) for row in rows]


    # --- Team projections ---

    def create_team_run(
        self,
        conversation_id: int,
        orchestration_run_id: int | None,
        goal: str,
        route: str = "staged_auto",
        risk_level: str = "medium",
    ) -> TeamRun:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO team_runs (
                conversation_id, orchestration_run_id, goal, route, risk_level,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                conversation_id, orchestration_run_id, goal, route,
                risk_level, now, now,
            ),
        )
        self._conn.commit()
        team_run = self.get_team_run(int(cur.lastrowid))
        if team_run is None:
            raise KeyError(f"unknown team run id: {cur.lastrowid}")
        return team_run

    def get_team_run(self, team_run_id: int) -> TeamRun | None:
        row = self._conn.execute(
            "SELECT * FROM team_runs WHERE id = ?",
            (team_run_id,),
        ).fetchone()
        return _row_to_team_run(row) if row else None

    def get_team_run_for_orchestration(
        self, orchestration_run_id: int
    ) -> TeamRun | None:
        row = self._conn.execute(
            """
            SELECT * FROM team_runs
            WHERE orchestration_run_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (orchestration_run_id,),
        ).fetchone()
        return _row_to_team_run(row) if row else None

    def update_team_run_status(self, team_run_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE team_runs SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), team_run_id),
        )
        self._conn.commit()

    def create_team_agent_job(
        self,
        *,
        team_run_id: int,
        role: str,
        model_profile: str,
        status: str = "queued",
        agent_run_id: int | None = None,
    ) -> TeamAgentJob:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO team_agent_jobs (
                team_run_id, role, model_profile, status, agent_run_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (team_run_id, role, model_profile, status, agent_run_id, now, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_agent_jobs WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team agent job id: {cur.lastrowid}")
        return _row_to_team_agent_job(row)

    def update_team_agent_job_status(self, job_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE team_agent_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), job_id),
        )
        self._conn.commit()

    def record_team_context_packet(
        self,
        *,
        team_run_id: int,
        agent_job_id: int,
        packet_json: dict[str, Any],
        prompt_text: str,
        prompt_tokens: int,
    ) -> TeamContextPacketRecord:
        cur = self._conn.execute(
            """
            INSERT INTO team_context_packets (
                team_run_id, agent_job_id, packet_json, prompt_text,
                prompt_tokens, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team_run_id, agent_job_id,
                json.dumps(packet_json, ensure_ascii=False),
                prompt_text, prompt_tokens, _now(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_context_packets WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team context packet id: {cur.lastrowid}")
        return _row_to_team_context_packet(row)

    def get_team_context_packet_for_job(
        self, agent_job_id: int
    ) -> TeamContextPacketRecord | None:
        row = self._conn.execute(
            """
            SELECT * FROM team_context_packets
            WHERE agent_job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (agent_job_id,),
        ).fetchone()
        return _row_to_team_context_packet(row) if row else None

    def record_team_artifact(
        self,
        *,
        team_run_id: int,
        agent_job_id: int | None,
        artifact_type: str,
        summary: str,
        payload: dict[str, Any],
    ) -> TeamArtifact:
        cur = self._conn.execute(
            """
            INSERT INTO team_artifacts (
                team_run_id, agent_job_id, artifact_type, summary,
                payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team_run_id, agent_job_id, artifact_type, summary,
                json.dumps(payload, ensure_ascii=False), _now(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_artifacts WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team artifact id: {cur.lastrowid}")
        return _row_to_team_artifact(row)

    def list_team_artifacts(self, team_run_id: int) -> list[TeamArtifact]:
        rows = self._conn.execute(
            "SELECT * FROM team_artifacts WHERE team_run_id = ? ORDER BY id ASC",
            (team_run_id,),
        ).fetchall()
        return [_row_to_team_artifact(row) for row in rows]

    def record_team_assignment(
        self,
        *,
        team_run_id: int,
        role: str,
        model_profile: str,
        selected_by: str,
    ) -> TeamAssignment:
        cur = self._conn.execute(
            """
            INSERT INTO team_assignments (
                team_run_id, role, model_profile, selected_by, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (team_run_id, role, model_profile, selected_by, _now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_assignments WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team assignment id: {cur.lastrowid}")
        return _row_to_team_assignment(row)

    def list_team_agent_jobs(self, team_run_id: int) -> list[TeamAgentJob]:
        rows = self._conn.execute(
            "SELECT * FROM team_agent_jobs WHERE team_run_id = ? ORDER BY id ASC",
            (team_run_id,),
        ).fetchall()
        return [_row_to_team_agent_job(row) for row in rows]

    def record_team_skill_activation(
        self,
        *,
        team_run_id: int,
        agent_job_id: int,
        activation_type: str,
        activation_id: str,
        source: str,
        token_cost: int,
    ) -> TeamSkillActivation:
        cur = self._conn.execute(
            """
            INSERT INTO team_skill_activations (
                team_run_id, agent_job_id, activation_type, activation_id,
                source, token_cost, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team_run_id, agent_job_id, activation_type, activation_id,
                source, token_cost, _now(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_skill_activations WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team skill activation id: {cur.lastrowid}")
        return _row_to_team_skill_activation(row)

    def list_team_skill_activations(
        self, agent_job_id: int
    ) -> list[TeamSkillActivation]:
        rows = self._conn.execute(
            """
            SELECT * FROM team_skill_activations
            WHERE agent_job_id = ?
            ORDER BY id ASC
            """,
            (agent_job_id,),
        ).fetchall()
        return [_row_to_team_skill_activation(row) for row in rows]

    def record_team_observation(
        self,
        *,
        team_run_id: int,
        domain: str,
        summary: str,
        evidence_refs: tuple[str, ...],
        confidence: float,
    ) -> TeamObservation:
        cur = self._conn.execute(
            """
            INSERT INTO team_observations (
                team_run_id, domain, summary, evidence_refs_json, confidence,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                team_run_id,
                domain,
                summary,
                _evidence_refs_json(evidence_refs),
                _clamp_confidence(confidence),
                _now(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_observations WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team observation id: {cur.lastrowid}")
        return _row_to_team_observation(row)

    def list_team_observations(self, team_run_id: int) -> list[TeamObservation]:
        rows = self._conn.execute(
            """
            SELECT * FROM team_observations
            WHERE team_run_id = ?
            ORDER BY id ASC
            """,
            (team_run_id,),
        ).fetchall()
        return [_row_to_team_observation(row) for row in rows]

    def upsert_team_instinct(
        self,
        instinct: InstinctMemory | None = None,
        *,
        instinct_id: str | None = None,
        scope: str | None = None,
        workspace_alias: str | None = None,
        role: str | None = None,
        domain: str | None = None,
        trigger: str | None = None,
        action: str | None = None,
        confidence: float | None = None,
        evidence_refs: tuple[str, ...] | None = None,
        status: str | None = None,
        created_at: datetime | None = None,
        last_validated_at: datetime | None = None,
    ) -> TeamInstinct:
        now = datetime.now(timezone.utc)
        if instinct is not None:
            values = {
                "instinct_id": instinct.instinct_id,
                "scope": instinct.scope,
                "workspace_alias": instinct.workspace_alias,
                "role": instinct.role,
                "domain": instinct.domain,
                "trigger": instinct.trigger,
                "action": instinct.action,
                "confidence": instinct.confidence,
                "evidence_refs": instinct.evidence_refs,
                "status": instinct.status,
                "created_at": _utc_iso(instinct.created_at),
                "last_validated_at": _utc_iso(instinct.last_validated_at),
            }
        else:
            values = {
                "instinct_id": instinct_id,
                "scope": scope,
                "workspace_alias": workspace_alias,
                "role": role,
                "domain": domain,
                "trigger": trigger,
                "action": action,
                "confidence": confidence,
                "evidence_refs": evidence_refs or (),
                "status": status,
                "created_at": _utc_iso(created_at or now),
                "last_validated_at": _utc_iso(last_validated_at or now),
            }
        required = (
            "instinct_id",
            "scope",
            "role",
            "domain",
            "trigger",
            "action",
            "confidence",
            "status",
        )
        missing = [key for key in required if values[key] is None]
        if missing:
            raise ValueError(f"missing instinct fields: {', '.join(missing)}")

        incoming_refs = _normalize_evidence_refs(values["evidence_refs"])
        incoming_confidence = _clamp_confidence(values["confidence"])
        incoming_status = str(values["status"])
        existing = self._conn.execute(
            "SELECT * FROM team_instincts WHERE instinct_id = ?",
            (str(values["instinct_id"]),),
        ).fetchone()
        if existing is not None:
            incoming_refs = _merge_evidence_refs(
                _evidence_refs(existing["evidence_refs_json"]),
                incoming_refs,
            )
            incoming_confidence = max(
                _clamp_confidence(existing["confidence"]),
                incoming_confidence,
            )
            existing_status = str(existing["status"])
            if existing_status == "active" and incoming_status == "candidate":
                incoming_status = "active"

        self._conn.execute(
            """
            INSERT INTO team_instincts (
                instinct_id, scope, workspace_alias, role, domain, trigger,
                action, confidence, evidence_refs_json, status, created_at,
                last_validated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instinct_id) DO UPDATE SET
                scope = excluded.scope,
                workspace_alias = excluded.workspace_alias,
                role = excluded.role,
                domain = excluded.domain,
                trigger = excluded.trigger,
                action = excluded.action,
                confidence = excluded.confidence,
                evidence_refs_json = excluded.evidence_refs_json,
                status = excluded.status,
                last_validated_at = excluded.last_validated_at
            """,
            (
                str(values["instinct_id"]),
                str(values["scope"]),
                values["workspace_alias"],
                str(values["role"]),
                str(values["domain"]),
                str(values["trigger"]),
                str(values["action"]),
                incoming_confidence,
                _evidence_refs_json(incoming_refs),
                incoming_status,
                str(values["created_at"]),
                str(values["last_validated_at"]),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM team_instincts WHERE instinct_id = ?",
            (str(values["instinct_id"]),),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown team instinct id: {values['instinct_id']}")
        return _row_to_team_instinct(row)

    def list_team_instincts(self, status: str = "active") -> list[TeamInstinct]:
        rows = self._conn.execute(
            """
            SELECT * FROM team_instincts
            WHERE status = ?
            ORDER BY id ASC
            """,
            (status,),
        ).fetchall()
        return [_row_to_team_instinct(row) for row in rows]


    # --- Workbench carryovers ---

    def create_workbench_carryover(
        self,
        *,
        chat_id: int,
        source_conversation_id: int,
        workspace_alias: str,
        brief_text: str,
        preview_text: str,
        source_fingerprint: str,
        status: str = "ready",
    ) -> WorkbenchCarryover:
        now = _now()
        cur = self._conn.execute(
            """
            INSERT INTO workbench_carryovers (
                chat_id, source_conversation_id, workspace_alias, brief_text,
                preview_text, source_fingerprint, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id, source_conversation_id, workspace_alias, brief_text,
                preview_text, source_fingerprint, status, now, now,
            ),
        )
        self._conn.commit()
        return self.get_workbench_carryover(int(cur.lastrowid))

    def get_workbench_carryover(self, carryover_id: int) -> WorkbenchCarryover:
        row = self._conn.execute(
            "SELECT * FROM workbench_carryovers WHERE id = ?",
            (carryover_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown workbench carryover id: {carryover_id}")
        return _workbench_carryover(row)

    def get_latest_prepared_carryover(
        self, chat_id: int
    ) -> WorkbenchCarryover | None:
        row = self._conn.execute(
            """
            SELECT * FROM workbench_carryovers
            WHERE chat_id = ? AND status = 'prepared'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        return _workbench_carryover(row) if row else None

    def mark_workbench_carryover_used(
        self, carryover_id: int, target_conversation_id: int
    ) -> WorkbenchCarryover:
        now = _now()
        self._conn.execute(
            """
            UPDATE workbench_carryovers
            SET status = 'used',
                target_conversation_id = ?,
                used_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (target_conversation_id, now, now, carryover_id),
        )
        self._conn.commit()
        return self.get_workbench_carryover(carryover_id)

    def update_workbench_carryover_brief(
        self,
        carryover_id: int,
        *,
        brief_text: str,
        preview_text: str,
        source_fingerprint: str,
    ) -> WorkbenchCarryover:
        now = _now()
        self._conn.execute(
            """
            UPDATE workbench_carryovers
            SET brief_text = ?,
                preview_text = ?,
                source_fingerprint = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (brief_text, preview_text, source_fingerprint, now, carryover_id),
        )
        self._conn.commit()
        return self.get_workbench_carryover(carryover_id)

    def update_workbench_carryover_status(
        self, carryover_id: int, status: str
    ) -> WorkbenchCarryover:
        self._conn.execute(
            """
            UPDATE workbench_carryovers
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, _now(), carryover_id),
        )
        self._conn.commit()
        return self.get_workbench_carryover(carryover_id)

    def list_carryover_evidence(
        self, conversation_id: int, *, limit: int = 5
    ) -> CarryoverEvidence:
        return CarryoverEvidence(
            agent_runs=self.list_recent_agent_runs(
                conversation_id, limit=limit
            ),
            orchestration_runs=self.list_orchestration_runs(
                conversation_id, limit=limit
            ),
        )

    # --- Usage events ---

    def record_usage_event(
        self,
        *,
        agent: str = "",
        role: str = "",
        phase: str = "",
        request_kind: str = "",
        request_index: int = 0,
        model: str = "",
        source: str = "estimated",
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_output_tokens: int = 0,
        total_tokens: int = 0,
        workflow_overhead_input_tokens: int = 0,
        workflow_overhead_output_tokens: int = 0,
        latency_ms: int = 0,
        status: str = "",
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        agent_run_id: int | None = None,
        task_id: int | None = None,
        external_thread_id: str | None = None,
        external_turn_id: str | None = None,
        external_session_id: str | None = None,
        metadata_json: str = "{}",
    ) -> UsageEvent:
        # Use explicit total_tokens when provided (handles reasoning/cached overhead),
        # otherwise compute from input+output for backwards compat.
        _total = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
        try:
            json.loads(metadata_json)
        except (json.JSONDecodeError, TypeError):
            metadata_json = "{}"
        cur = self._conn.execute(
            """
            INSERT INTO usage_events (
                created_at, conversation_id, orchestration_run_id, agent_run_id,
                task_id, agent, role, phase, request_kind, request_index, model,
                external_thread_id, external_turn_id, external_session_id,
                status, source, input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens,
                workflow_overhead_input_tokens, workflow_overhead_output_tokens,
                latency_ms, metadata_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                _now(), conversation_id, orchestration_run_id, agent_run_id,
                task_id, agent, role, phase, request_kind, request_index, model,
                external_thread_id, external_turn_id, external_session_id,
                status, source, input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, _total,
                workflow_overhead_input_tokens, workflow_overhead_output_tokens,
                latency_ms, metadata_json,
            ),
        )
        self._conn.commit()
        return self.get_usage_event(int(cur.lastrowid))

    def get_usage_event(self, event_id: int) -> UsageEvent:
        row = self._conn.execute(
            "SELECT * FROM usage_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown usage event id: {event_id}")
        return _usage_event(row)

    def list_usage_events(
        self,
        *,
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        agent_run_id: int | None = None,
        task_id: int | None = None,
        agent: str | None = None,
        limit: int = 200,
    ) -> list[UsageEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if orchestration_run_id is not None:
            clauses.append("orchestration_run_id = ?")
            params.append(orchestration_run_id)
        if agent_run_id is not None:
            clauses.append("agent_run_id = ?")
            params.append(agent_run_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if agent is not None:
            clauses.append("agent = ?")
            params.append(agent)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM usage_events {where} ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()
        return [_usage_event(row) for row in rows]

    def aggregate_usage(
        self,
        *,
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        task_id: int | None = None,
    ) -> dict:
        """Return aggregated usage split by agent and by source.

        Returns a dict with keys:
          codex -> {requests, input_tokens, output_tokens, cached_input_tokens,
                    reasoning_output_tokens, total_tokens, source_breakdown}
          claude -> same shape
          workflow -> same shape
          totals -> {requests, input_tokens, output_tokens, total_tokens,
                     workflow_overhead_input_tokens, workflow_overhead_output_tokens}
        """
        clauses: list[str] = []
        params: list[object] = []
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if orchestration_run_id is not None:
            clauses.append("orchestration_run_id = ?")
            params.append(orchestration_run_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        def _agent_agg(agent_filter: str) -> dict:
            agent_where = f"{where} AND agent = ?" if where else "WHERE agent = ?"
            query = f"""
                SELECT
                    COUNT(*) as requests,
                    COALESCE(SUM(input_tokens), 0) as input_tokens,
                    COALESCE(SUM(output_tokens), 0) as output_tokens,
                    COALESCE(SUM(cached_input_tokens), 0) as cached_input_tokens,
                    COALESCE(SUM(reasoning_output_tokens), 0) as reasoning_output_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(workflow_overhead_input_tokens), 0) as wf_overhead_input,
                    COALESCE(SUM(workflow_overhead_output_tokens), 0) as wf_overhead_output,
                    source
                FROM usage_events
                {agent_where}
                GROUP BY source
                ORDER BY source
            """
            p = list(params) + [agent_filter]
            rows = self._conn.execute(query, p).fetchall()
            result: dict = {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
                "workflow_overhead_input_tokens": 0,
                "workflow_overhead_output_tokens": 0,
                "source_breakdown": {},
            }
            for row in rows:
                source = str(row["source"] or "estimated")
                r = int(row["requests"] or 0)
                result["requests"] += r
                result["input_tokens"] += int(row["input_tokens"] or 0)
                result["output_tokens"] += int(row["output_tokens"] or 0)
                result["cached_input_tokens"] += int(row["cached_input_tokens"] or 0)
                result["reasoning_output_tokens"] += int(row["reasoning_output_tokens"] or 0)
                result["total_tokens"] += int(row["total_tokens"] or 0)
                result["workflow_overhead_input_tokens"] += int(row["wf_overhead_input"] or 0)
                result["workflow_overhead_output_tokens"] += int(row["wf_overhead_output"] or 0)
                result["source_breakdown"][source] = {
                    "requests": r,
                    "input_tokens": int(row["input_tokens"] or 0),
                    "output_tokens": int(row["output_tokens"] or 0),
                    "total_tokens": int(row["total_tokens"] or 0),
                }
            return result

        codex = _agent_agg("codex")
        claude = _agent_agg("claude")
        workflow = _agent_agg("workflow")

        total_requests = codex["requests"] + claude["requests"] + workflow["requests"]
        total_input = codex["input_tokens"] + claude["input_tokens"] + workflow["input_tokens"]
        total_output = codex["output_tokens"] + claude["output_tokens"] + workflow["output_tokens"]
        total_tokens = codex["total_tokens"] + claude["total_tokens"] + workflow["total_tokens"]
        total_wf_input = (
            codex["workflow_overhead_input_tokens"]
            + claude["workflow_overhead_input_tokens"]
            + workflow["workflow_overhead_input_tokens"]
        )
        total_wf_output = (
            codex["workflow_overhead_output_tokens"]
            + claude["workflow_overhead_output_tokens"]
            + workflow["workflow_overhead_output_tokens"]
        )

        return {
            "codex": codex,
            "claude": claude,
            "workflow": workflow,
            "totals": {
                "requests": total_requests,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_tokens,
                "workflow_overhead_input_tokens": total_wf_input,
                "workflow_overhead_output_tokens": total_wf_output,
            },
        }

    def render_usage_summary(
        self,
        *,
        conversation_id: int | None = None,
        orchestration_run_id: int | None = None,
        task_id: int | None = None,
    ) -> str:
        """Render a human-readable usage summary string for status display."""
        agg = self.aggregate_usage(
            conversation_id=conversation_id,
            orchestration_run_id=orchestration_run_id,
            task_id=task_id,
        )
        lines: list[str] = ["Token 用量摘要：", ""]

        for agent_key, label in [("codex", "Codex"), ("claude", "Claude"), ("workflow", "Workflow")]:
            a = agg[agent_key]
            if a["requests"] == 0:
                continue
            lines.append(f"{label}：{a['requests']} 请求, "
                         f"输入 {a['input_tokens']:,}, "
                         f"输出 {a['output_tokens']:,}, "
                         f"总计 {a['total_tokens']:,}")
            if a.get("cached_input_tokens"):
                lines.append(f"  缓存命中：{a['cached_input_tokens']:,}")
            if a.get("reasoning_output_tokens"):
                lines.append(f"  推理输出：{a['reasoning_output_tokens']:,}")
            if a.get("source_breakdown"):
                sources = ", ".join(
                    f"{s}: {info['requests']} 请求/{info['total_tokens']:,} tokens"
                    for s, info in a["source_breakdown"].items()
                )
                lines.append(f"  来源：{sources}")

        t = agg["totals"]
        lines.append("")
        lines.append(f"总计：{t['requests']} 请求, "
                     f"输入 {t['input_tokens']:,}, "
                     f"输出 {t['output_tokens']:,}, "
                     f"总计 {t['total_tokens']:,}")
        if t["workflow_overhead_input_tokens"] or t["workflow_overhead_output_tokens"]:
            lines.append(f"Workflow overhead："
                         f"输入 {t['workflow_overhead_input_tokens']:,}, "
                         f"输出 {t['workflow_overhead_output_tokens']:,}")

        return "\n".join(lines)


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


# --- Row mappers ---

def _workbench_carryover(row: sqlite3.Row) -> WorkbenchCarryover:
    return WorkbenchCarryover(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        source_conversation_id=int(row["source_conversation_id"]),
        target_conversation_id=(
            int(row["target_conversation_id"])
            if row["target_conversation_id"] is not None
            else None
        ),
        workspace_alias=str(row["workspace_alias"] or ""),
        brief_text=str(row["brief_text"] or ""),
        preview_text=str(row["preview_text"] or ""),
        source_fingerprint=str(row["source_fingerprint"] or ""),
        status=str(row["status"] or ""),
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
        used_at=_parse_dt(row["used_at"]) if row["used_at"] else None,
    )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=int(row["id"]),
        workspace_alias=str(row["workspace_alias"]),
        workspace_path=str(row["workspace_path"]),
        title=str(row["title"]),
        status=TaskStatus(str(row["status"])),
        codex_thread_id=row["codex_thread_id"],
        active_turn_id=row["active_turn_id"],
        parent_task_id=row["parent_task_id"],
        telegram_chat_id=row["telegram_chat_id"],
        telegram_status_message_id=row["telegram_status_message_id"],
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
        last_summary=str(row["last_summary"]),
        last_phase=str(row["last_phase"]),
        last_error=str(row["last_error"]),
        changed_file_count=int(row["changed_file_count"] or 0),
        pending_approval_count=int(row["pending_approval_count"] or 0),
        token_input=int(row["token_input"] or 0),
        token_output=int(row["token_output"] or 0),
        worktree_path=str(row["worktree_path"] or ""),
        worktree_branch=str(row["worktree_branch"] or ""),
        is_force_parallel=bool(row["is_force_parallel"]),
    )


def _event(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        event_type=str(row["event_type"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=_dt(str(row["created_at"])),
    )


def _approval(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        codex_request_id=str(row["codex_request_id"]),
        codex_item_id=row["codex_item_id"],
        codex_turn_id=row["codex_turn_id"],
        kind=ApprovalKind(str(row["kind"])),
        summary=str(row["summary"]),
        command_json=str(row["command_json"]),
        status=ApprovalStatus(str(row["status"])),
        telegram_message_id=row["telegram_message_id"],
        resolution=row["resolution"],
        created_at=_dt(str(row["created_at"])),
        resolved_at=_dt(str(row["resolved_at"])) if row["resolved_at"] else None,
    )


def _touched(row: sqlite3.Row) -> TouchedFile:
    return TouchedFile(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        path=str(row["path"]),
        change_kind=str(row["change_kind"]),
        created_at=_dt(str(row["created_at"])),
    )


def _backend_request(row: sqlite3.Row) -> BackendRequest:
    return BackendRequest(
        id=int(row["id"]),
        jsonrpc_id=int(row["jsonrpc_id"]),
        method=str(row["method"]),
        task_id=row["task_id"] and int(row["task_id"]),
        status=BackendRequestStatus(str(row["status"])),
        created_at=_dt(str(row["created_at"])),
        completed_at=_dt(str(row["completed_at"])) if row["completed_at"] else None,
        error=str(row["error"]) if row["error"] else None,
    )


def _conversation(row: sqlite3.Row) -> ConversationSession:
    return ConversationSession(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        title=str(row["title"]),
        mode=str(row["mode"]),
        workspace_alias=str(row["workspace_alias"]),
        active_codex_task_id=row["active_codex_task_id"],
        active_claude_run_id=row["active_claude_run_id"],
        conversation_summary=str(row["conversation_summary"] or ""),
        current_model=str(row["current_model"] or ""),
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
        archived_at=_dt(str(row["archived_at"])) if row["archived_at"] else None,
        codex_thread_id=str(row["codex_thread_id"] or ""),
        codex_thread_policy=str(row["codex_thread_policy"] or ""),
        claude_session_id=str(row["claude_session_id"] or ""),
        legacy_compatible=bool(row["legacy_compatible"]),
    )


def _agent_run(row: sqlite3.Row) -> AgentRun:
    return AgentRun(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        agent=str(row["agent"]),
        role=str(row["role"]),
        status=str(row["status"]),
        hidden_task_id=row["hidden_task_id"],
        external_session_id=row["external_session_id"],
        prompt_packet_summary=str(row["prompt_packet_summary"] or ""),
        completion_summary=str(row["completion_summary"] or ""),
        token_input=int(row["token_input"] or 0),
        token_output=int(row["token_output"] or 0),
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
    )


def _orchestration_run(row: sqlite3.Row) -> OrchestrationRun:
    return OrchestrationRun(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        goal=str(row["goal"]),
        status=str(row["status"]),
        current_step=str(row["current_step"] or ""),
        verify_round=int(row["verify_round"] or 0),
        max_verify_rounds=int(row["max_verify_rounds"] or 0),
        last_codex_analysis=str(row["last_codex_analysis"] or ""),
        last_claude_summary=str(row["last_claude_summary"] or ""),
        last_verification_result=str(row["last_verification_result"] or ""),
        diagnose_json=str(row["diagnose_json"] or ""),
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
    )


def _orchestration_decision(row: sqlite3.Row) -> OrchestrationDecision:
    return OrchestrationDecision(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        decision=str(row["decision"]),
        reason=str(row["reason"] or ""),
        next_agent=str(row["next_agent"] or ""),
        created_at=_dt(str(row["created_at"])),
    )


def _row_to_team_run(row: sqlite3.Row) -> TeamRun:
    return TeamRun(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        orchestration_run_id=(
            int(row["orchestration_run_id"])
            if row["orchestration_run_id"] is not None
            else None
        ),
        goal=str(row["goal"]),
        route=str(row["route"]),
        risk_level=str(row["risk_level"]),
        status=str(row["status"]),
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
    )


def _row_to_team_agent_job(row: sqlite3.Row) -> TeamAgentJob:
    return TeamAgentJob(
        id=int(row["id"]),
        team_run_id=int(row["team_run_id"]),
        role=str(row["role"]),
        model_profile=str(row["model_profile"]),
        status=str(row["status"]),
        agent_run_id=int(row["agent_run_id"]) if row["agent_run_id"] is not None else None,
        created_at=_dt(str(row["created_at"])),
        updated_at=_dt(str(row["updated_at"])),
    )


def _row_to_team_context_packet(row: sqlite3.Row) -> TeamContextPacketRecord:
    return TeamContextPacketRecord(
        id=int(row["id"]),
        team_run_id=int(row["team_run_id"]),
        agent_job_id=int(row["agent_job_id"]),
        packet=json.loads(str(row["packet_json"] or "{}")),
        prompt_text=str(row["prompt_text"] or ""),
        prompt_tokens=int(row["prompt_tokens"] or 0),
        created_at=_dt(str(row["created_at"])),
    )


def _row_to_team_artifact(row: sqlite3.Row) -> TeamArtifact:
    return TeamArtifact(
        id=int(row["id"]),
        team_run_id=int(row["team_run_id"]),
        agent_job_id=int(row["agent_job_id"]) if row["agent_job_id"] is not None else None,
        artifact_type=str(row["artifact_type"]),
        summary=str(row["summary"] or ""),
        payload=json.loads(str(row["payload_json"] or "{}")),
        created_at=_dt(str(row["created_at"])),
    )


def _row_to_team_assignment(row: sqlite3.Row) -> TeamAssignment:
    return TeamAssignment(
        id=int(row["id"]),
        team_run_id=int(row["team_run_id"]),
        role=str(row["role"]),
        model_profile=str(row["model_profile"]),
        selected_by=str(row["selected_by"]),
        created_at=_dt(str(row["created_at"])),
    )


def _row_to_team_skill_activation(row: sqlite3.Row) -> TeamSkillActivation:
    return TeamSkillActivation(
        id=int(row["id"]),
        team_run_id=int(row["team_run_id"]),
        agent_job_id=int(row["agent_job_id"]),
        activation_type=str(row["activation_type"]),
        activation_id=str(row["activation_id"]),
        source=str(row["source"]),
        token_cost=int(row["token_cost"] or 0),
        created_at=_dt(str(row["created_at"])),
    )


def _clamp_confidence(value: object) -> float:
    confidence = float(value)
    return max(0.0, min(1.0, confidence))


def _normalize_evidence_refs(evidence_refs: object) -> tuple[str, ...]:
    if isinstance(evidence_refs, str):
        raw_refs = (evidence_refs,)
    else:
        raw_refs = tuple(evidence_refs or ())
    refs: list[str] = []
    seen: set[str] = set()
    for item in raw_refs:
        ref = str(item).strip()
        if not ref or ref in seen:
            continue
        refs.append(ref)
        seen.add(ref)
    return tuple(refs)


def _merge_evidence_refs(*groups: object) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for ref in _normalize_evidence_refs(group):
            if ref in seen:
                continue
            merged.append(ref)
            seen.add(ref)
    return tuple(merged)


def _aware_utc(value: datetime | str) -> datetime:
    dt = value if isinstance(value, datetime) else _dt(str(value))
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_iso(value: datetime | str) -> str:
    return _aware_utc(value).isoformat()


def _evidence_refs_json(evidence_refs: object) -> str:
    refs = _normalize_evidence_refs(evidence_refs)
    return json.dumps(list(refs), ensure_ascii=False)


def _evidence_refs(value: object) -> tuple[str, ...]:
    loaded = json.loads(str(value or "[]"))
    if not isinstance(loaded, list):
        return ()
    return _normalize_evidence_refs(loaded)


def _row_to_team_observation(row: sqlite3.Row) -> TeamObservation:
    return TeamObservation(
        id=int(row["id"]),
        team_run_id=int(row["team_run_id"]),
        domain=str(row["domain"]),
        summary=str(row["summary"] or ""),
        evidence_refs=_evidence_refs(row["evidence_refs_json"]),
        confidence=_clamp_confidence(row["confidence"]),
        created_at=_aware_utc(str(row["created_at"])),
    )


def _row_to_team_instinct(row: sqlite3.Row) -> TeamInstinct:
    return TeamInstinct(
        id=int(row["id"]),
        instinct_id=str(row["instinct_id"]),
        scope=str(row["scope"]),
        workspace_alias=(
            str(row["workspace_alias"]) if row["workspace_alias"] is not None else None
        ),
        role=str(row["role"]),
        domain=str(row["domain"]),
        trigger=str(row["trigger"]),
        action=str(row["action"]),
        confidence=_clamp_confidence(row["confidence"]),
        evidence_refs=_evidence_refs(row["evidence_refs_json"]),
        status=str(row["status"]),
        created_at=_aware_utc(str(row["created_at"])),
        last_validated_at=_aware_utc(str(row["last_validated_at"])),
    )


def _usage_event(row: sqlite3.Row) -> UsageEvent:
    return UsageEvent(
        id=int(row["id"]),
        created_at=_dt(str(row["created_at"])),
        conversation_id=row["conversation_id"],
        orchestration_run_id=row["orchestration_run_id"],
        agent_run_id=row["agent_run_id"],
        task_id=row["task_id"],
        agent=str(row["agent"]),
        role=str(row["role"]),
        phase=str(row["phase"]),
        request_kind=str(row["request_kind"]),
        request_index=int(row["request_index"] or 0),
        model=str(row["model"]),
        external_thread_id=row["external_thread_id"],
        external_turn_id=row["external_turn_id"],
        external_session_id=row["external_session_id"],
        status=str(row["status"]),
        source=str(row["source"] or "estimated"),
        input_tokens=int(row["input_tokens"] or 0),
        cached_input_tokens=int(row["cached_input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        reasoning_output_tokens=int(row["reasoning_output_tokens"] or 0),
        total_tokens=int(row["total_tokens"] or 0),
        workflow_overhead_input_tokens=int(row["workflow_overhead_input_tokens"] or 0),
        workflow_overhead_output_tokens=int(row["workflow_overhead_output_tokens"] or 0),
        latency_ms=int(row["latency_ms"] or 0),
        metadata_json=str(row["metadata_json"] or "{}"),
    )
