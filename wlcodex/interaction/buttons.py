from __future__ import annotations

from wlcodex.conversation_callback import (
    CONTINUE,
    DIFF,
    NEW_CONVO,
    encode_conversation_callback,
)


def natural_completion_buttons(
    *,
    conversation_id: int,
    has_diff: bool = False,
    include_new: bool = True,
) -> list[list[dict[str, str]]]:
    row = [
        {
            "text": "继续",
            "callback_data": encode_conversation_callback(conversation_id, CONTINUE),
        },
    ]
    if has_diff:
        row.append(
            {
                "text": "查看 diff",
                "callback_data": encode_conversation_callback(conversation_id, DIFF),
            }
        )
    row.append(
        {
            "text": "状态",
            "callback_data": encode_conversation_callback(conversation_id, CONTINUE),
        }
    )
    if include_new:
        row.append(
            {
                "text": "新对话",
                "callback_data": encode_conversation_callback(conversation_id, NEW_CONVO),
            }
        )
    return [row]
