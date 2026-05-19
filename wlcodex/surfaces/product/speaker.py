"""Speaker label formatting for the product surface.

Rules:
  raw_kind="diff"        -> "{agent}: 代码改动已记录，可点 查看 diff。"
  raw_kind="tool_output" -> "{agent}: 工具输出已记录，可点 日志。"
  ordinary text          -> "{agent}: {text}"
"""

from wlcodex.surfaces.product.events import ProductDisplayEvent

_DIFF_LINE = "代码改动已记录，可点 查看 diff。"
_TOOL_OUTPUT_LINE = "工具输出已记录，可点 日志。"


def product_speaker_line(event: ProductDisplayEvent) -> str:
    """Format a ProductDisplayEvent as a speaker-labeled line."""
    if event.raw_kind == "diff":
        return f"{event.agent}: {_DIFF_LINE}"
    if event.raw_kind == "tool_output":
        return f"{event.agent}: {_TOOL_OUTPUT_LINE}"
    return f"{event.agent}: {event.text}"
