"""Replaced by test_real_app_server_integration.py.

The previous test manually injected a WebSocket into the backend,
bypassing the _recv_loop and approval handlers.  The replacement
uses the full AppServerProcess + AppServerCodexBackend lifecycle.
"""

from tests.test_real_app_server_integration import (
    pytestmark,
    test_real_app_server_thread_turn_and_events,
)

__all__ = ["pytestmark", "test_real_app_server_thread_turn_and_events"]
