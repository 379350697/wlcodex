# Codex App-Server Protocol Spike

## Purpose

This document records the exact app-server messages WLCodex uses so the rest of the app remains insulated from protocol changes.

## Local Codex CLI

- Command: `codex --version`
- Result: `codex-cli 0.121.0`
- App-server help command: `codex app-server --help`
- Schema command: `codex app-server generate-json-schema --out runtime/protocol`
- Generation result: schema files were written successfully. The CLI printed a warning that it could not update `PATH` because the filesystem is read-only, but schema generation completed with exit code 0.

## Required V1 Operations

- create or start a thread for a workspace
- start a turn with a user prompt
- continue an existing thread with a user prompt
- receive streamed events for status display
- receive approval requests
- resolve approval requests

## Validation Command

```bash
codex app-server generate-json-schema --out runtime/protocol
```

## Schema Files

- `runtime/protocol/ApplyPatchApprovalParams.json`
- `runtime/protocol/ApplyPatchApprovalResponse.json`
- `runtime/protocol/ChatgptAuthTokensRefreshParams.json`
- `runtime/protocol/ChatgptAuthTokensRefreshResponse.json`
- `runtime/protocol/ClientNotification.json`
- `runtime/protocol/ClientRequest.json`
- `runtime/protocol/CommandExecutionRequestApprovalParams.json`
- `runtime/protocol/CommandExecutionRequestApprovalResponse.json`
- `runtime/protocol/DynamicToolCallParams.json`
- `runtime/protocol/DynamicToolCallResponse.json`
- `runtime/protocol/ExecCommandApprovalParams.json`
- `runtime/protocol/ExecCommandApprovalResponse.json`
- `runtime/protocol/FileChangeRequestApprovalParams.json`
- `runtime/protocol/FileChangeRequestApprovalResponse.json`
- `runtime/protocol/FuzzyFileSearchParams.json`
- `runtime/protocol/FuzzyFileSearchResponse.json`
- `runtime/protocol/FuzzyFileSearchSessionCompletedNotification.json`
- `runtime/protocol/FuzzyFileSearchSessionUpdatedNotification.json`
- `runtime/protocol/JSONRPCError.json`
- `runtime/protocol/JSONRPCErrorError.json`
- `runtime/protocol/JSONRPCMessage.json`
- `runtime/protocol/JSONRPCNotification.json`
- `runtime/protocol/JSONRPCRequest.json`
- `runtime/protocol/JSONRPCResponse.json`
- `runtime/protocol/McpServerElicitationRequestParams.json`
- `runtime/protocol/McpServerElicitationRequestResponse.json`
- `runtime/protocol/PermissionsRequestApprovalParams.json`
- `runtime/protocol/PermissionsRequestApprovalResponse.json`
- `runtime/protocol/RequestId.json`
- `runtime/protocol/ServerNotification.json`
- `runtime/protocol/ServerRequest.json`
- `runtime/protocol/ToolRequestUserInputParams.json`
- `runtime/protocol/ToolRequestUserInputResponse.json`
- `runtime/protocol/codex_app_server_protocol.schemas.json`
- `runtime/protocol/codex_app_server_protocol.v2.schemas.json`
- `runtime/protocol/v1/InitializeParams.json`
- `runtime/protocol/v1/InitializeResponse.json`
- `runtime/protocol/v2/AccountLoginCompletedNotification.json`
- `runtime/protocol/v2/AccountRateLimitsUpdatedNotification.json`
- `runtime/protocol/v2/AccountUpdatedNotification.json`
- `runtime/protocol/v2/AgentMessageDeltaNotification.json`
- `runtime/protocol/v2/AppListUpdatedNotification.json`
- `runtime/protocol/v2/AppsListParams.json`
- `runtime/protocol/v2/AppsListResponse.json`
- `runtime/protocol/v2/CancelLoginAccountParams.json`
- `runtime/protocol/v2/CancelLoginAccountResponse.json`
- `runtime/protocol/v2/CommandExecOutputDeltaNotification.json`
- `runtime/protocol/v2/CommandExecParams.json`
- `runtime/protocol/v2/CommandExecResizeParams.json`
- `runtime/protocol/v2/CommandExecResizeResponse.json`
- `runtime/protocol/v2/CommandExecResponse.json`
- `runtime/protocol/v2/CommandExecTerminateParams.json`
- `runtime/protocol/v2/CommandExecTerminateResponse.json`
- `runtime/protocol/v2/CommandExecWriteParams.json`
- `runtime/protocol/v2/CommandExecWriteResponse.json`
- `runtime/protocol/v2/CommandExecutionOutputDeltaNotification.json`
- `runtime/protocol/v2/ConfigBatchWriteParams.json`
- `runtime/protocol/v2/ConfigReadParams.json`
- `runtime/protocol/v2/ConfigReadResponse.json`
- `runtime/protocol/v2/ConfigRequirementsReadResponse.json`
- `runtime/protocol/v2/ConfigValueWriteParams.json`
- `runtime/protocol/v2/ConfigWarningNotification.json`
- `runtime/protocol/v2/ConfigWriteResponse.json`
- `runtime/protocol/v2/ContextCompactedNotification.json`
- `runtime/protocol/v2/DeprecationNoticeNotification.json`
- `runtime/protocol/v2/ErrorNotification.json`
- `runtime/protocol/v2/ExperimentalFeatureEnablementSetParams.json`
- `runtime/protocol/v2/ExperimentalFeatureEnablementSetResponse.json`
- `runtime/protocol/v2/ExperimentalFeatureListParams.json`
- `runtime/protocol/v2/ExperimentalFeatureListResponse.json`
- `runtime/protocol/v2/ExternalAgentConfigDetectParams.json`
- `runtime/protocol/v2/ExternalAgentConfigDetectResponse.json`
- `runtime/protocol/v2/ExternalAgentConfigImportParams.json`
- `runtime/protocol/v2/ExternalAgentConfigImportResponse.json`
- `runtime/protocol/v2/FeedbackUploadParams.json`
- `runtime/protocol/v2/FeedbackUploadResponse.json`
- `runtime/protocol/v2/FileChangeOutputDeltaNotification.json`
- `runtime/protocol/v2/FsChangedNotification.json`
- `runtime/protocol/v2/FsCopyParams.json`
- `runtime/protocol/v2/FsCopyResponse.json`
- `runtime/protocol/v2/FsCreateDirectoryParams.json`
- `runtime/protocol/v2/FsCreateDirectoryResponse.json`
- `runtime/protocol/v2/FsGetMetadataParams.json`
- `runtime/protocol/v2/FsGetMetadataResponse.json`
- `runtime/protocol/v2/FsReadDirectoryParams.json`
- `runtime/protocol/v2/FsReadDirectoryResponse.json`
- `runtime/protocol/v2/FsReadFileParams.json`
- `runtime/protocol/v2/FsReadFileResponse.json`
- `runtime/protocol/v2/FsRemoveParams.json`
- `runtime/protocol/v2/FsRemoveResponse.json`
- `runtime/protocol/v2/FsUnwatchParams.json`
- `runtime/protocol/v2/FsUnwatchResponse.json`
- `runtime/protocol/v2/FsWatchParams.json`
- `runtime/protocol/v2/FsWatchResponse.json`
- `runtime/protocol/v2/FsWriteFileParams.json`
- `runtime/protocol/v2/FsWriteFileResponse.json`
- `runtime/protocol/v2/GetAccountParams.json`
- `runtime/protocol/v2/GetAccountRateLimitsResponse.json`
- `runtime/protocol/v2/GetAccountResponse.json`
- `runtime/protocol/v2/HookCompletedNotification.json`
- `runtime/protocol/v2/HookStartedNotification.json`
- `runtime/protocol/v2/ItemCompletedNotification.json`
- `runtime/protocol/v2/ItemGuardianApprovalReviewCompletedNotification.json`
- `runtime/protocol/v2/ItemGuardianApprovalReviewStartedNotification.json`
- `runtime/protocol/v2/ItemStartedNotification.json`
- `runtime/protocol/v2/ListMcpServerStatusParams.json`
- `runtime/protocol/v2/ListMcpServerStatusResponse.json`
- `runtime/protocol/v2/LoginAccountParams.json`
- `runtime/protocol/v2/LoginAccountResponse.json`
- `runtime/protocol/v2/LogoutAccountResponse.json`
- `runtime/protocol/v2/MarketplaceAddParams.json`
- `runtime/protocol/v2/MarketplaceAddResponse.json`
- `runtime/protocol/v2/McpResourceReadParams.json`
- `runtime/protocol/v2/McpResourceReadResponse.json`
- `runtime/protocol/v2/McpServerOauthLoginCompletedNotification.json`
- `runtime/protocol/v2/McpServerOauthLoginParams.json`
- `runtime/protocol/v2/McpServerOauthLoginResponse.json`
- `runtime/protocol/v2/McpServerRefreshResponse.json`
- `runtime/protocol/v2/McpServerStatusUpdatedNotification.json`
- `runtime/protocol/v2/McpServerToolCallParams.json`
- `runtime/protocol/v2/McpServerToolCallResponse.json`
- `runtime/protocol/v2/McpToolCallProgressNotification.json`
- `runtime/protocol/v2/ModelListParams.json`
- `runtime/protocol/v2/ModelListResponse.json`
- `runtime/protocol/v2/ModelReroutedNotification.json`
- `runtime/protocol/v2/PlanDeltaNotification.json`
- `runtime/protocol/v2/PluginInstallParams.json`
- `runtime/protocol/v2/PluginInstallResponse.json`
- `runtime/protocol/v2/PluginListParams.json`
- `runtime/protocol/v2/PluginListResponse.json`
- `runtime/protocol/v2/PluginReadParams.json`
- `runtime/protocol/v2/PluginReadResponse.json`
- `runtime/protocol/v2/PluginUninstallParams.json`
- `runtime/protocol/v2/PluginUninstallResponse.json`
- `runtime/protocol/v2/RawResponseItemCompletedNotification.json`
- `runtime/protocol/v2/ReasoningSummaryPartAddedNotification.json`
- `runtime/protocol/v2/ReasoningSummaryTextDeltaNotification.json`
- `runtime/protocol/v2/ReasoningTextDeltaNotification.json`
- `runtime/protocol/v2/ReviewStartParams.json`
- `runtime/protocol/v2/ReviewStartResponse.json`
- `runtime/protocol/v2/ServerRequestResolvedNotification.json`
- `runtime/protocol/v2/SkillsChangedNotification.json`
- `runtime/protocol/v2/SkillsConfigWriteParams.json`
- `runtime/protocol/v2/SkillsConfigWriteResponse.json`
- `runtime/protocol/v2/SkillsListParams.json`
- `runtime/protocol/v2/SkillsListResponse.json`
- `runtime/protocol/v2/TerminalInteractionNotification.json`
- `runtime/protocol/v2/ThreadArchiveParams.json`
- `runtime/protocol/v2/ThreadArchiveResponse.json`
- `runtime/protocol/v2/ThreadArchivedNotification.json`
- `runtime/protocol/v2/ThreadClosedNotification.json`
- `runtime/protocol/v2/ThreadCompactStartParams.json`
- `runtime/protocol/v2/ThreadCompactStartResponse.json`
- `runtime/protocol/v2/ThreadForkParams.json`
- `runtime/protocol/v2/ThreadForkResponse.json`
- `runtime/protocol/v2/ThreadInjectItemsParams.json`
- `runtime/protocol/v2/ThreadInjectItemsResponse.json`
- `runtime/protocol/v2/ThreadListParams.json`
- `runtime/protocol/v2/ThreadListResponse.json`
- `runtime/protocol/v2/ThreadLoadedListParams.json`
- `runtime/protocol/v2/ThreadLoadedListResponse.json`
- `runtime/protocol/v2/ThreadMetadataUpdateParams.json`
- `runtime/protocol/v2/ThreadMetadataUpdateResponse.json`
- `runtime/protocol/v2/ThreadNameUpdatedNotification.json`
- `runtime/protocol/v2/ThreadReadParams.json`
- `runtime/protocol/v2/ThreadReadResponse.json`
- `runtime/protocol/v2/ThreadRealtimeClosedNotification.json`
- `runtime/protocol/v2/ThreadRealtimeErrorNotification.json`
- `runtime/protocol/v2/ThreadRealtimeItemAddedNotification.json`
- `runtime/protocol/v2/ThreadRealtimeOutputAudioDeltaNotification.json`
- `runtime/protocol/v2/ThreadRealtimeSdpNotification.json`
- `runtime/protocol/v2/ThreadRealtimeStartedNotification.json`
- `runtime/protocol/v2/ThreadRealtimeTranscriptDeltaNotification.json`
- `runtime/protocol/v2/ThreadRealtimeTranscriptDoneNotification.json`
- `runtime/protocol/v2/ThreadResumeParams.json`
- `runtime/protocol/v2/ThreadResumeResponse.json`
- `runtime/protocol/v2/ThreadRollbackParams.json`
- `runtime/protocol/v2/ThreadRollbackResponse.json`
- `runtime/protocol/v2/ThreadSetNameParams.json`
- `runtime/protocol/v2/ThreadSetNameResponse.json`
- `runtime/protocol/v2/ThreadShellCommandParams.json`
- `runtime/protocol/v2/ThreadShellCommandResponse.json`
- `runtime/protocol/v2/ThreadStartParams.json`
- `runtime/protocol/v2/ThreadStartResponse.json`
- `runtime/protocol/v2/ThreadStartedNotification.json`
- `runtime/protocol/v2/ThreadStatusChangedNotification.json`
- `runtime/protocol/v2/ThreadTokenUsageUpdatedNotification.json`
- `runtime/protocol/v2/ThreadUnarchiveParams.json`
- `runtime/protocol/v2/ThreadUnarchiveResponse.json`
- `runtime/protocol/v2/ThreadUnarchivedNotification.json`
- `runtime/protocol/v2/ThreadUnsubscribeParams.json`
- `runtime/protocol/v2/ThreadUnsubscribeResponse.json`
- `runtime/protocol/v2/TurnCompletedNotification.json`
- `runtime/protocol/v2/TurnDiffUpdatedNotification.json`
- `runtime/protocol/v2/TurnInterruptParams.json`
- `runtime/protocol/v2/TurnInterruptResponse.json`
- `runtime/protocol/v2/TurnPlanUpdatedNotification.json`
- `runtime/protocol/v2/TurnStartParams.json`
- `runtime/protocol/v2/TurnStartResponse.json`
- `runtime/protocol/v2/TurnStartedNotification.json`
- `runtime/protocol/v2/TurnSteerParams.json`
- `runtime/protocol/v2/TurnSteerResponse.json`
- `runtime/protocol/v2/WindowsSandboxSetupCompletedNotification.json`
- `runtime/protocol/v2/WindowsSandboxSetupStartParams.json`
- `runtime/protocol/v2/WindowsSandboxSetupStartResponse.json`
- `runtime/protocol/v2/WindowsWorldWritableWarningNotification.json`

## V1 Message Mapping

| WLCodex operation | Codex app-server method/event | Request fields | Response fields | Notes |
| --- | --- | --- | --- | --- |
| create thread | `thread/start` | `cwd`, `approvalPolicy`, `sandbox`, optional `model`, `config`, `developerInstructions`, `baseInstructions` | `thread.id`, `thread.status`, `cwd`, `approvalPolicy`, `approvalsReviewer`, `sandbox`, `model`, `modelProvider` | Use for new `/task`; do not include Telegram status, logs, or local memory. |
| start turn | `turn/start` | `threadId`, `input`; optional `cwd`, `approvalPolicy`, `approvalsReviewer`, `model`, `sandboxPolicy` | `turn.id`, `turn.status`, `turn.items` | `input` is an array of user input items, e.g. `{ "type": "text", "text": prompt }`. |
| continue turn | `thread/resume` then `turn/start` | `thread/resume` requires `threadId`; `turn/start` requires `threadId`, `input` | `thread.id` from resume; `turn.id` from turn start | Only use for explicit `/continue`. Do not inject local ledger summaries automatically. |
| steer active turn | `turn/steer` | `threadId`, `expectedTurnId`, `input` | response schema `TurnSteerResponse` | Only use for explicit `/steer`; needs current active turn id. |
| stream status event | server notifications: `thread/status/changed`, `turn/started`, `turn/completed`, `turn/diff/updated`, `turn/plan/updated`, `item/started`, `item/completed`, `item/agentMessage/delta`, `item/commandExecution/outputDelta`, `item/fileChange/outputDelta` | server notification params | no response | Render locally into Telegram status cards and ledger events only. |
| receive approval request | server requests: `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval` | command approval: `itemId`, `threadId`, `turnId`, optional `approvalId`, `command`, `cwd`; file approval: `itemId`, `threadId`, `turnId`, optional `grantRoot`, `reason`; permissions approval: `itemId`, `threadId`, `turnId`, `permissions`, optional `reason` | client must respond to the JSON-RPC request id | Store pending approval in SQLite and show Telegram buttons. |
| resolve approval | JSON-RPC response to the server request id | command/file result shape contains `decision`; permissions result shape contains `permissions` and optional `scope` | `serverRequest/resolved` notification may follow | Command/file decisions include `accept`, `acceptForSession`, `decline`, `cancel`; command approval also supports `acceptWithExecpolicyAmendment`. |

## Backend Decision

Decision: app-server backend is ready for V1 implementation.
Reason: the generated schema exposes thread creation, explicit thread resume, turn start, same-turn steering, streamed thread/turn/item events, approval request events, and approval resolution through JSON-RPC responses.
