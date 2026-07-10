from __future__ import annotations

from pathlib import Path

from wlcodex.live_stream.native_templates.timeline_v2 import (
    render_timeline_v2_template,
)


def test_timeline_v2_template_uses_conditional_scroll_and_accessible_status() -> None:
    page = render_timeline_v2_template("codex", {"initial_events": []})

    assert "function scrollToBottom()" in page
    assert "function shouldAutoScroll()" in page
    assert "const followTail = shouldAutoScroll();" in page
    assert "if (followTail) scrollToBottom();" in page
    assert 'role="log"' in page
    assert 'role="status"' in page
    assert '<label class="sr-only" for="prompt">' in page


def test_shared_native_css_keeps_controls_and_forced_colors_accessible() -> None:
    css = Path("wlcodex/live_stream/static/base.css").read_text(encoding="utf-8")

    assert "--control-touch-target: 44px;" in css
    assert "@media (forced-colors: active)" in css
    assert "forced-color-adjust: auto;" in css
