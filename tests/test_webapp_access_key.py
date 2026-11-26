"""Tests for web app access key validation (issue #14) and graceful shutdown."""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import webapp
from webapp import _validate_access_key


@pytest.fixture()
def config():
    m = MagicMock()
    m.get.return_value = None  # No AccessKey in config by default
    return m


@pytest.fixture()
def logger():
    return MagicMock()


def test_no_key_configured_allows_all(config, logger, monkeypatch):
    """When no access key is configured, all requests are allowed."""
    monkeypatch.delenv("WEBAPP_ACCESS_KEY", raising=False)
    assert _validate_access_key(config, logger, None) is True
    assert _validate_access_key(config, logger, "anything") is True


def test_request_blocked_without_key(config, logger, monkeypatch):
    """When access key is required, a request without a key is blocked."""
    monkeypatch.setenv("WEBAPP_ACCESS_KEY", "secret123")
    assert _validate_access_key(config, logger, None) is False


def test_request_blocked_with_wrong_key(config, logger, monkeypatch):
    """When access key is required, a request with an incorrect key is blocked."""
    monkeypatch.setenv("WEBAPP_ACCESS_KEY", "secret123")
    assert _validate_access_key(config, logger, "wrongkey") is False


def test_request_allowed_with_correct_key(config, logger, monkeypatch):
    """When access key is required, a request with the correct key is allowed."""
    monkeypatch.setenv("WEBAPP_ACCESS_KEY", "secret123")
    assert _validate_access_key(config, logger, "secret123") is True


def test_access_key_from_config_honoured(monkeypatch):
    """Access key read from config (not env var) is enforced."""
    monkeypatch.delenv("WEBAPP_ACCESS_KEY", raising=False)
    config = MagicMock()
    config.get.return_value = "configkey"
    logger = MagicMock()
    assert _validate_access_key(config, logger, None) is False
    assert _validate_access_key(config, logger, "configkey") is True


def test_blank_env_key_allows_all(config, logger, monkeypatch):
    """A blank/whitespace-only WEBAPP_ACCESS_KEY means no protection."""
    monkeypatch.setenv("WEBAPP_ACCESS_KEY", "   ")
    assert _validate_access_key(config, logger, None) is True


def _make_config() -> MagicMock:
    cfg = MagicMock()

    def _get(*_args: object, default: object = None) -> object:
        return default

    cfg.get.side_effect = _get
    return cfg


def test_serve_configures_bounded_graceful_shutdown(monkeypatch):
    """serve_asgi_blocking must give uvicorn a bounded graceful-shutdown timeout.

    The web server runs in a non-daemon thread. Without a bounded timeout, a
    lingering WebSocket connection (e.g. an open dashboard) keeps that thread alive
    indefinitely and blocks the whole process from exiting until it is SIGKILLed.
    """
    captured: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, config: object) -> None:
            captured["config"] = config
            self.should_exit = False

        async def serve(self) -> None:
            self.should_exit = True

    monkeypatch.setattr(webapp.uvicorn, "Server", _FakeServer)

    stop_event = threading.Event()
    stop_event.set()  # resolve the stop watcher immediately

    webapp.serve_asgi_blocking(MagicMock(), _make_config(), MagicMock(), stop_event)

    config = captured["config"]
    assert config.timeout_graceful_shutdown is not None, (
        "uvicorn must have a bounded graceful-shutdown timeout"
    )
    assert config.timeout_graceful_shutdown == int(
        webapp.WEB_SERVER_GRACEFUL_SHUTDOWN_TIMEOUT
    )
    assert 0 < config.timeout_graceful_shutdown <= 30
