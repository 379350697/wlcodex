"""Explicit dependencies shared by Native page templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativePageDependencies:
    provider_display_name: Any
    replace_html_icons: Any
    app_head: Any
    turn_semantics_json: Any
    permission_presets: Any
    plugin_menu_items: Any
    icons_js_literal: Any
