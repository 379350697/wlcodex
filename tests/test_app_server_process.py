"""App-server process manager tests."""

import pytest

from wlcodex.app_server_process import AppServerProcess, AppServerProcessConfig, BackendHealth


def test_command_includes_binary_and_listen_endpoint() -> None:
    config = AppServerProcessConfig(
        binary="codex", host="127.0.0.1", port=17431
    )
    assert config.command == ["codex", "app-server", "--listen", "ws://127.0.0.1:17431"]


def test_command_binds_to_loopback() -> None:
    config = AppServerProcessConfig(
        binary="/usr/local/bin/codex", host="127.0.0.1", port=9999
    )
    assert config.endpoint == "ws://127.0.0.1:9999"
    assert "127.0.0.1" in config.endpoint
    assert "0.0.0.0" not in config.endpoint


def test_non_loopback_host_rejected() -> None:
    config = AppServerProcessConfig(
        binary="codex", host="0.0.0.0", port=17431
    )
    with pytest.raises(ValueError, match="loopback"):
        AppServerProcess(config)


def test_startup_timeout_produces_health_error() -> None:
    health = BackendHealth(
        process_alive=False,
        websocket_connected=False,
        error="startup timed out",
    )
    assert not health.is_healthy
    assert "timed out" in health.summary()


def test_shutdown_terminates_process() -> None:
    config = AppServerProcessConfig(
        binary="codex", host="127.0.0.1", port=17431, startup_timeout_seconds=5
    )
    proc = AppServerProcess(config)

    # Without starting, shutdown is safe no-op
    proc.shutdown()
    assert not proc.is_alive


def test_backend_health_reports_all_dims() -> None:
    health = BackendHealth(process_alive=True, websocket_connected=True)
    assert health.is_healthy
    assert "healthy" in health.summary()

    unhealthy = BackendHealth(process_alive=True, websocket_connected=False)
    assert not unhealthy.is_healthy
    assert "websocket" in unhealthy.summary()


def test_process_config_endpoint_is_loopback() -> None:
    cfg = AppServerProcessConfig(binary="codex", host="127.0.0.1", port=17431)
    assert cfg.endpoint == "ws://127.0.0.1:17431"


def test_backend_health_external_process_can_be_healthy() -> None:
    health = BackendHealth(
        process_alive=True,
        websocket_connected=True,
        external_process=True,
    )
    assert health.is_healthy
