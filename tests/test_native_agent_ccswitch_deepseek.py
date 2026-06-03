from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from wlcodex.native_agents.ccswitch_deepseek import resolve_deepseek_credentials


def _create_ccswitch_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE providers (
            id TEXT NOT NULL,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            PRIMARY KEY (id, app_type)
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_provider(
    path: Path,
    *,
    provider_id: str,
    app_type: str = "claude",
    name: str = "DeepSeek",
    settings_config: dict,
    is_current: bool = False,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO providers (
            id, app_type, name, settings_config, meta, is_current
        )
        VALUES (?, ?, ?, ?, '{}', ?)
        """,
        (
            provider_id,
            app_type,
            name,
            json.dumps(settings_config),
            1 if is_current else 0,
        ),
    )
    conn.commit()
    conn.close()


def test_resolver_prefers_deepseek_api_key_env(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccswitch_db(db_path)

    credentials = resolve_deepseek_credentials(
        env={"DEEPSEEK_API_KEY": "sk-env"},
        db_path=db_path,
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert credentials is not None
    assert credentials.api_key == "sk-env"
    assert credentials.base_url == "https://api.deepseek.com/anthropic"
    assert credentials.source == "env"
    assert credentials.safe_metadata() == {"auth_source": "env"}


def test_resolver_reads_current_ccswitch_deepseek_provider(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccswitch_db(db_path)
    _insert_provider(
        db_path,
        provider_id="deepseek-current",
        app_type="claude-desktop",
        name="DeepSeek",
        is_current=True,
        settings_config={
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "sk-ccswitch",
            }
        },
    )

    credentials = resolve_deepseek_credentials(
        env={},
        db_path=db_path,
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert credentials is not None
    assert credentials.api_key == "sk-ccswitch"
    assert credentials.base_url == "https://api.deepseek.com/anthropic"
    assert credentials.source == "ccswitch"
    assert credentials.provider_id == "deepseek-current"
    assert credentials.provider_name == "DeepSeek"
    assert credentials.safe_metadata() == {
        "auth_source": "ccswitch",
        "ccswitch_provider_id": "deepseek-current",
        "ccswitch_provider_name": "DeepSeek",
    }


def test_resolver_ignores_non_deepseek_or_missing_auth(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccswitch_db(db_path)
    _insert_provider(
        db_path,
        provider_id="anthropic",
        name="Anthropic",
        is_current=True,
        settings_config={
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "ANTHROPIC_AUTH_TOKEN": "sk-anthropic",
            }
        },
    )
    _insert_provider(
        db_path,
        provider_id="deepseek-no-auth",
        name="DeepSeek No Auth",
        settings_config={
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            }
        },
    )

    credentials = resolve_deepseek_credentials(
        env={},
        db_path=db_path,
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert credentials is None


def test_safe_metadata_never_exposes_secret(tmp_path: Path) -> None:
    db_path = tmp_path / "cc-switch.db"
    _create_ccswitch_db(db_path)
    _insert_provider(
        db_path,
        provider_id="deepseek",
        name="DeepSeek",
        is_current=True,
        settings_config={
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                "ANTHROPIC_API_KEY": "sk-secret-never-print",
            }
        },
    )

    credentials = resolve_deepseek_credentials(
        env={},
        db_path=db_path,
        api_key_env="DEEPSEEK_API_KEY",
    )

    assert credentials is not None
    assert "sk-secret" not in repr(credentials.safe_metadata())
    assert "api_key" not in credentials.safe_metadata()
