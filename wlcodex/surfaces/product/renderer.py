"""Product surface renderer — maps runtime events to product display events.

This is a placeholder. The full renderer will consume runtime events and
produce ProductDisplayEvent instances for Telegram delivery.

Rules (from design spec):
  - no raw JSON
  - no full diff
  - no long stdout/stderr
  - no internal id unless requested
  - one compact stream message per active run
  - speaker label is required for agent-originated updates
"""
