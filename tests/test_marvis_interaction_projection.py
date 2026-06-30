from __future__ import annotations

import json

from wlcodex.relay.marvis_interaction import (
    chat_events,
    project_marvis_agui_events,
    project_relay_rows_to_marvis_interactions,
    worklog_events,
)


def _kinds(events: list[object]) -> list[str]:
    return [str(getattr(event, "kind")) for event in events]


def _bodies(events: list[object]) -> list[str]:
    return [str(getattr(event, "body")) for event in events]


def test_project_marvis_agui_trace_to_unified_interaction_semantics() -> None:
    events = project_marvis_agui_events(
        [
            {
                "event_type": "HUMAN_MESSAGE",
                "seq": 1,
                "data": {"content": "【Golden Trace 3】先给出计划，继续前向我确认展示风格。"},
            },
            {"event_type": "RUN_STARTED", "seq": 2, "data": {}},
            {
                "event_type": "TEXT_MESSAGE_END",
                "seq": 373,
                "data": {
                    "role": "assistant",
                    "text": "我先给一个简短计划，继续前请你选 A/B/C。",
                },
            },
            {
                "event_type": "TOOL_CALL_RESULT",
                "seq": 478,
                "data": {
                    "toolName": "web_fetch",
                    "content": "GitHub Trending 页面是 JS 动态渲染的，换浏览器引擎重新抓。",
                },
            },
            {
                "event_type": "CUSTOM",
                "seq": 902,
                "data": {
                    "name": "subagent_start",
                    "value": {"agentName": "Search Agent"},
                },
            },
            {
                "event_type": "CUSTOM",
                "seq": 1240,
                "data": {
                    "name": "subagent_end",
                    "value": {
                        "agentName": "Search Agent",
                        "status": "completed",
                        "resultSummary": "搞定，有请下一位",
                    },
                },
            },
            {"event_type": "RUN_FINISHED", "seq": 1515, "data": {}},
        ]
    )

    assert _kinds(chat_events(events)) == [
        "user.message.accepted",
        "assistant.feedback.started",
        "assistant.message.completed",
        "tool.completed",
        "agent.handoff",
        "agent.dispatch.completed",
    ]
    assert _kinds(worklog_events(events)) == [
        "tool.completed",
        "agent.dispatch.completed",
    ]
    assert _kinds(events)[-1] == "task.completed"
    assert "Marvis 拍了拍 Search Agent 说， 别等了，这就开始" in _bodies(
        chat_events(events)
    )
    assert "web_fetch" in {getattr(event, "tool_name") for event in worklog_events(events)}


def test_project_marvis_agui_text_content_is_delta_and_end_is_completed() -> None:
    events = project_marvis_agui_events(
        [
            {
                "event_type": "TEXT_MESSAGE_CONTENT",
                "seq": 10,
                "data": {"role": "assistant", "text": "第一段"},
            },
            {
                "event_type": "TEXT_MESSAGE_CONTENT",
                "seq": 11,
                "data": {"role": "assistant", "text": "第二段"},
            },
            {
                "event_type": "TEXT_MESSAGE_END",
                "seq": 12,
                "data": {"role": "assistant", "text": "完整回复"},
            },
        ]
    )

    chat = chat_events(events)
    assert _kinds(chat) == [
        "assistant.message.delta",
        "assistant.message.delta",
        "assistant.message.completed",
    ]
    assert _bodies(chat) == ["第一段", "第二段", "完整回复"]


def test_project_relay_rows_keeps_protocol_artifacts_out_of_chat_semantics() -> None:
    protocol_payload = {
        "artifact_type": "final_summary",
        "role": "director",
        "status": "passed",
        "summary": "该角色已返回结构化结果，详情见结构化数据。",
        "next_action": "等待用户确认",
    }

    events = project_relay_rows_to_marvis_interactions(
        [
            {
                "role": "user",
                "kind": "user_message",
                "body": "选 A 极简排行风，继续。",
                "key": "user_followup:1",
            },
            {"role": "director", "kind": "waiting", "body": "...", "key": "waiting:1"},
            {
                "role": "director",
                "kind": "followup_response",
                "body": "行，皇上一句话定了 A。我先去 GitHub Trending 把数据抓回来。",
                "key": "followup_response:1",
            },
            {
                "role": "director",
                "kind": "followup_response",
                "body": json.dumps(protocol_payload, ensure_ascii=False),
                "key": "followup_response:protocol",
            },
            {
                "role": "director",
                "kind": "role_process",
                "artifact_type": "routing_decision",
                "handoff_to": "tester",
                "body": "交给 Search Agent 查询候选项目。",
                "key": "process:director:routing_decision:1",
            },
            {
                "role": "tester",
                "kind": "handoff",
                "from_role": "director",
                "to_role": "tester",
                "body": "",
                "key": "handoff:director:tester:1",
            },
            {
                "role": "director",
                "kind": "role_artifact_invalid",
                "body": "结构化结果缺少必填字段：status",
                "key": "invalid:1",
            },
        ]
    )

    chat = chat_events(events)
    chat_body = "\n".join(_bodies(chat))
    assert "选 A 极简排行风" in chat_body
    assert "行，皇上一句话定了 A" in chat_body
    assert "交给 Search Agent 查询候选项目。" in chat_body
    assert "Marvis 拍了拍 测试工程师 说， 别等了，这就开始" in chat_body
    assert "结构化结果缺少必填字段" not in chat_body
    assert "详情见结构化数据" not in chat_body
    assert '"artifact_type": "final_summary"' not in chat_body

    log_body = "\n".join(_bodies(worklog_events(events)))
    assert "结构化结果缺少必填字段：status" in log_body
    assert '"artifact_type": "final_summary"' in log_body
