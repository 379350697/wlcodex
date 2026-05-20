"""Workbench event constants.

Event names follow the spec workbench event concepts.  Where existing
runtime event constants already carry the same semantics they should
be reused; this module defines only the new workbench-level events.
"""

# View / execution mode changes
WORKBENCH_CREATED = "workbench.created"
WORKBENCH_VIEW_CHANGED = "workbench.view.changed"
WORKBENCH_EXECUTION_MODE_SELECTED = "workbench.execution_mode.selected"
WORKBENCH_ROUTE_DECIDED = "workbench.route.decided"

# Onsite session lifecycle
ONSITE_SESSION_STARTED = "onsite.session.started"
ONSITE_SESSION_ATTACHED = "onsite.session.attached"
ONSITE_SESSION_DETACHED = "onsite.session.detached"
ONSITE_SESSION_ORPHANED = "onsite.session.orphaned"
ONSITE_INPUT_SENT = "onsite.input.sent"
ONSITE_OUTPUT_FRAME = "onsite.output.frame"
ONSITE_CURSOR_ADVANCED = "onsite.cursor.advanced"

# Cockpit
COCKPIT_CURSOR_ADVANCED = "cockpit.cursor.advanced"
COCKPIT_SUMMARY_RENDERED = "cockpit.summary.rendered"

# Shared workbench facts
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_RESOLVED = "approval.resolved"
DIFF_UPDATED = "diff.updated"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
