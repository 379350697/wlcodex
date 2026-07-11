"""Read-only Relay work-log HTML fragments."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from typing import Any

from wlcodex.relay.display import replace_legacy_role_identifiers


def render_work_log_segment(
    segment: Any,
    *,
    index: int,
    render_avatar: Callable[..., str],
) -> str:
    timeline_items = "".join(
        render_work_log_entry(entry)
        for entry in segment.entries
        if entry.text or entry.chip or entry.output
    )
    if not timeline_items:
        return ""
    return f"""
      <section class="marvis-work-log-role marvis-work-log-segment" data-marvis-work-log-role="{escape(segment.role)}" data-marvis-work-log-segment="{escape(segment.role)}" data-marvis-work-log-segment-index="{index}">
        {render_avatar(segment.persona, label=segment.display_name)}
        <div class="marvis-work-log-role-main">
          <h3>{escape(segment.display_name)}</h3>
          <div class="marvis-work-log-line">{timeline_items}</div>
        </div>
      </section>
    """


def render_work_log_entry(entry: Any) -> str:
    classes = ["marvis-work-log-entry"]
    if entry.failed:
        classes.append("is-failed")
    key_attr = f' data-marvis-work-log-entry-key="{escape(entry.key)}"' if entry.key else ""
    chip = replace_legacy_role_identifiers(entry.chip)
    text = replace_legacy_role_identifiers(entry.text)
    output = replace_legacy_role_identifiers(entry.output)
    chip_html = f'<span class="marvis-work-log-tool-chip">{escape(chip)}</span>' if chip else ""
    output_html = ""
    if output:
        output_html = (
            '<details class="marvis-work-log-output" data-marvis-work-log-output>'
            "<summary>查看输出</summary>"
            f"<pre>{escape(output)}</pre>"
            "</details>"
        )
    return f"""
      <div class="{" ".join(classes)}" data-marvis-work-log-entry="{escape(entry.kind)}"{key_attr}>
        {chip_html}
        <p>{escape(text)}</p>
        {output_html}
      </div>
    """
