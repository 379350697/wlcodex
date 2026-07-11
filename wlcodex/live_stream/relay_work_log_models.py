"""Relay work-log view models.

These small models sit between the read-only projection and HTML renderer.
Keeping them out of the HTTP server prevents the same mutable shape from being
redeclared by each Relay surface.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkLogEntry:
    kind: str
    key: str
    text: str = ""
    chip: str = ""
    output: str = ""
    failed: bool = False
    replace_text: bool = False


@dataclass
class WorkLogSegment:
    role: str
    persona: str
    display_name: str
    entries: list[WorkLogEntry]
