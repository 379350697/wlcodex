"""Shared Relay document head and versioned browser assets."""

from __future__ import annotations

from html import escape


RELAY_MARVIS_CSS_HREF = "/static/relay_marvis.css?v=20260710-dialog-a11y"
RELAY_MOBILE_JS_HREF = "/static/relay_mobile.js?v=20260701-mobile-web"


def render_relay_mobile_web_head(title: str) -> str:
    return f"""  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light only">
  <meta name="theme-color" content="#FAF8F5">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/static/base.css">
  <link rel="stylesheet" href="{RELAY_MARVIS_CSS_HREF}">
  <script src="/static/surface_runtime.js?v=20260710-semantic-closure"></script>
  <script src="{RELAY_MOBILE_JS_HREF}" defer></script>"""
