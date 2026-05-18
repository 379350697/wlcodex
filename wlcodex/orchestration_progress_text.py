from __future__ import annotations


def render_user_progress_text(phase: str, *, first_impl_delta: bool = False) -> str:
    if phase == "analysis_started":
        return "我先看需求和改动范围。"
    if phase == "analysis_complete":
        return "方案看完了，交给 Claude 改。"
    if phase == "implementation_delta" and first_impl_delta:
        return "Claude 正在改代码，我会在关键节点更新。"
    if phase == "implementation_complete":
        return "Claude 改完了，我开始验收。"
    if phase == "verification_started":
        return "我在验收改动。"
    if phase == "verification_complete":
        return "验收结果出来了。"
    return ""
