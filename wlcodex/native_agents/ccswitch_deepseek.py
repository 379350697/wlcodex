from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CCSWITCH_DB_PATH = Path("~/.cc-switch/cc-switch.db")
DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


@dataclass(frozen=True)
class DeepSeekCredentials:
    api_key: str
    base_url: str
    source: str
    provider_id: str = ""
    provider_name: str = ""

    def safe_metadata(self) -> dict[str, str]:
        metadata = {"auth_source": self.source}
        if self.provider_id:
            metadata["ccswitch_provider_id"] = self.provider_id
        if self.provider_name:
            metadata["ccswitch_provider_name"] = self.provider_name
        return metadata


def resolve_deepseek_credentials(
    *,
    env: Mapping[str, str] | None = None,
    db_path: str | Path | None = None,
    api_key_env: str = "DEEPSEEK_API_KEY",
) -> DeepSeekCredentials | None:
    source_env = env if env is not None else os.environ
    env_key = str(source_env.get(api_key_env, "") or "").strip()
    if env_key:
        return DeepSeekCredentials(
            api_key=env_key,
            base_url=DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL,
            source="env",
        )

    if db_path == "":
        return None

    resolved_db_path = Path(db_path or DEFAULT_CCSWITCH_DB_PATH).expanduser()
    if not resolved_db_path.exists():
        return None

    for row in _provider_rows(resolved_db_path):
        credentials = _credentials_from_provider_row(row)
        if credentials is not None:
            return credentials
    return None


def _provider_rows(db_path: Path) -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, app_type, name, settings_config, meta, is_current
            FROM providers
            WHERE app_type IN ('claude', 'claude-desktop')
            ORDER BY is_current DESC, app_type ASC, id ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass
    return [dict(row) for row in rows]


def _credentials_from_provider_row(row: dict[str, Any]) -> DeepSeekCredentials | None:
    settings = _json_object(row.get("settings_config"))
    env = _json_object(settings.get("env"))
    base_url = _first_string(
        env.get("ANTHROPIC_BASE_URL"),
        settings.get("ANTHROPIC_BASE_URL"),
        settings.get("base_url"),
        settings.get("baseUrl"),
    )
    provider_name = str(row.get("name") or "")
    provider_id = str(row.get("id") or "")
    if not _is_deepseek(base_url, provider_name, provider_id):
        return None
    api_key = _first_string(
        env.get("ANTHROPIC_AUTH_TOKEN"),
        env.get("ANTHROPIC_API_KEY"),
        env.get("DEEPSEEK_API_KEY"),
        settings.get("ANTHROPIC_AUTH_TOKEN"),
        settings.get("ANTHROPIC_API_KEY"),
        settings.get("apiKey"),
        settings.get("api_key"),
    )
    if not api_key:
        return None
    return DeepSeekCredentials(
        api_key=api_key,
        base_url=base_url or DEFAULT_DEEPSEEK_ANTHROPIC_BASE_URL,
        source="ccswitch",
        provider_id=provider_id,
        provider_name=provider_name,
    )


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_deepseek(base_url: str, provider_name: str, provider_id: str) -> bool:
    haystack = " ".join((base_url, provider_name, provider_id)).lower()
    return "deepseek" in haystack
