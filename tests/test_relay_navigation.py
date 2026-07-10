from wlcodex.live_stream.relay_navigation import (
    marvis_relay_bottom_nav,
    relay_task_view_href,
    relay_workspace_href,
)
from wlcodex.live_stream.relay_composer import _marvis_relay_task_composer


def test_relay_navigation_preserves_workspace_token_and_active_state() -> None:
    href = relay_workspace_href("/repo a", "token/value", status="blocked")

    assert href == (
        "/native/workflows/relay?token=token/value&workspace=/repo%20a&status=blocked"
    )
    nav = marvis_relay_bottom_nav(
        "settings",
        access_token="token/value",
        selected_workspace="/repo a",
    )
    assert 'data-marvis-nav="settings" aria-current="page"' in nav
    assert "token=token%2Fvalue&amp;workspace=/repo%20a" in nav


def test_relay_task_view_href_normalizes_unknown_view_to_conversation() -> None:
    assert relay_task_view_href(42, "", "unexpected") == (
        "/native/workflows/relay/tasks/42?view=conversation"
    )


def test_relay_composer_is_a_pure_template_with_the_accessible_attachment_dialog() -> None:
    html = _marvis_relay_task_composer(
        token_suffix="?token=abc",
        selected_workspace="/repo-a",
        access_token="abc",
    )

    assert 'action="/api/relay/tasks?token=abc"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
