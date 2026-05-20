"""Pure plain-text routing.

Decides which WorkbenchRoute a plain-text message should follow
based on the current WorkbenchState.
"""

from .models import ExecutionMode, ViewMode, WorkbenchRoute, WorkbenchState


def route_plain_text(state: WorkbenchState, _text: str) -> WorkbenchRoute:
    """Return the route a plain-text message takes from *state*.

    * Onsite view: always ONSITE_INPUT (text goes to the selected
      live session).
    * Cockpit view: depends on the active execution mode.
    """
    if state.view is ViewMode.ONSITE:
        return WorkbenchRoute.ONSITE_INPUT

    if state.execution_mode is ExecutionMode.CODEX_DIRECT:
        return WorkbenchRoute.CODEX_DIRECT_COCKPIT

    if state.execution_mode is ExecutionMode.CLAUDE_DIRECT:
        return WorkbenchRoute.CLAUDE_DIRECT_COCKPIT

    return WorkbenchRoute.ORCHESTRATED_COCKPIT
