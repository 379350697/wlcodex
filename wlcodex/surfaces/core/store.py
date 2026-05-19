"""Surface event store — pure replay entry point.

replay_surface_state is the single function both Product and Terminal
surfaces call to reconstruct shared conversation facts.  It delegates to
the pure reducer in runtime_state so that tests do not need SQLite.

RecoveryManager is exported here for daemon startup so callers get both
replay and recovery from one import.
"""

from wlcodex.recovery import RecoveryManager
from wlcodex.runtime_state import replay_surface_events as replay_surface_state

__all__ = ["replay_surface_state", "RecoveryManager"]
