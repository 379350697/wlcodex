"""ProductDisplayEvent — a compact display event for the product surface."""

from dataclasses import dataclass

VALID_AGENTS = frozenset({"codex", "claude", "system", "user"})


@dataclass(frozen=True)
class ProductDisplayEvent:
    """A single display-worthy event for the product surface.

    Never exposes raw JSON, internal ids, or full diffs.
    """

    agent: str
    phase: str
    text: str
    raw_kind: str | None = None

    def __post_init__(self):
        if self.agent not in VALID_AGENTS:
            raise ValueError(
                f"agent must be one of {sorted(VALID_AGENTS)}, got {self.agent!r}"
            )
