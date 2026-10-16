"""Tests for the log viewer and extended system information (issue #26)."""

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sc_foundation import SCCommon, SCLogger

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from webapp import create_asgi_app


class FakeConfig:
    """Minimal stand-in for SCConfigManager backed by a nested dict."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {"General": {"AppName": "Test Lights"}}
        self.config_path = None

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.values.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        self.values.setdefault(section, {})[key] = value


class FakeController:
    """Minimal stand-in for LightingController."""

    @staticmethod
    def get_webapp_data() -> dict[str, Any]:
        return {"groups": {"Garden": {"switches": {"Path": {}}}}}


def _make_logger(logfile: Path | None, max_lines: int = 1000) -> SCLogger:
    return SCLogger(
        {
            "logfile_name": str(logfile) if logfile else None,
            "file_verbosity": "debug",
            "console_verbosity": "none",
            "max_lines": max_lines,
        }
    )


@pytest.fixture(autouse=True)
def _no_env_access_key(monkeypatch):
    monkeypatch.delenv("WEBAPP_ACCESS_KEY", raising=False)


@pytest.fixture
def config():
    return FakeConfig()


@pytest.fixture
def log_client(config, tmp_path):
    logger = _make_logger(tmp_path / "logs" / "app.log", max_lines=5)
    logger.log_message("first log line", "summary")
    logger.log_message("a <b>tagged</b> warning", "warning")
    app, _ = create_asgi_app(FakeController(), config, logger)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client, logger


# ---- System page ----

_DEPLOYMENT_INFO = {
    "name": "LightingControl",
    "version": "9.8.7",
    "description": "Test",
    "git_branch": "feature/test-branch",
    "git_tag": None,
    "git_remote": "https://secret-token@github.com/example/repo.git",
    "git_status": "Ahead",
    "git_changes": 3,
}


def test_system_page_shows_version_and_git(log_client, monkeypatch):
    """The system page shows the app version and git branch/status/changes."""
    monkeypatch.setattr(SCCommon, "get_deployment_info", lambda: dict(_DEPLOYMENT_INFO))
    test_client, _ = log_client
    body = test_client.get("/system").text

    assert "Application version" in body
    assert "9.8.7" in body
    assert "feature/test-branch" in body
    assert "Ahead" in body
    assert "Uncommitted changes" in body
    # The remote URL can embed credentials, so it must never be shown.
    assert "secret-token" not in body
    assert 'href="/log"' in body


def test_system_page_without_git(log_client, monkeypatch):
    """Git rows are omitted when the app isn't running from a git checkout."""
    info = dict(_DEPLOYMENT_INFO, git_branch=None, git_status="Unknown", git_changes=None)
    monkeypatch.setattr(SCCommon, "get_deployment_info", lambda: info)
    test_client, _ = log_client
    body = test_client.get("/system").text

    assert "9.8.7" in body
    assert "Git branch" not in body
    assert "Uncommitted changes" not in body


# ---- Log viewer ----


def test_log_page_shows_contents(log_client):
    """The log page renders the log file contents, HTML-escaped."""
    test_client, logger = log_client
    response = test_client.get("/log")

    assert response.status_code == 200
    body = response.text
    assert "first log line" in body
    assert "&lt;b&gt;tagged&lt;/b&gt;" in body
    assert logger.get_logfile_path() in body
    assert 'id="log-live"' in body
    assert "/log/download" in body
    assert "/static/log.js" in body


def test_log_content_incremental(log_client):
    """/log/content returns only new lines when given the previous position."""
    test_client, logger = log_client
    first = test_client.get("/log/content").json()
    assert first["reset"] is True
    assert "first log line" in first["text"]

    logger.log_message("a newer line", "summary")
    second = test_client.get(
        "/log/content",
        params={"generation": first["generation"], "offset": first["offset"]},
    ).json()

    assert second["reset"] is False
    assert "a newer line" in second["text"]
    assert "first log line" not in second["text"]


def test_log_content_resets_after_trim(log_client):
    """After the log is trimmed the client is told to replace its content."""
    test_client, logger = log_client
    first = test_client.get("/log/content").json()

    for i in range(10):
        logger.log_message(f"filler {i}", "summary")
    logger.trim_logfile()

    second = test_client.get(
        "/log/content",
        params={"generation": first["generation"], "offset": first["offset"]},
    ).json()

    assert second["reset"] is True
    assert second["text"] == logger.read_logfile()


def test_log_download(log_client):
    """/log/download returns the log file as an attachment."""
    test_client, logger = log_client
    response = test_client.get("/log/download")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="app.log"'
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == logger.read_logfile()


def test_log_pages_when_file_logging_disabled(config):
    """With file logging off the page explains why and download is a 404."""
    logger = _make_logger(None)
    app, _ = create_asgi_app(FakeController(), config, logger)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        page = test_client.get("/log")
        assert page.status_code == 200
        assert "File logging is not enabled" in page.text
        assert "/static/log.js" not in page.text
        assert test_client.get("/log/download").status_code == 404
        content = test_client.get("/log/content").json()
        assert not content["text"]


def test_log_endpoints_require_access_key(log_client, config):
    """Every log endpoint is gated by the access key."""
    test_client, _ = log_client
    config.set("Website", "AccessKey", "letmein")

    for path in ("/log", "/log/content", "/log/download"):
        assert test_client.get(path).status_code == 403
        assert test_client.get(path, params={"key": "wrong"}).status_code == 403
        assert test_client.get(path, params={"key": "letmein"}).status_code == 200


def test_log_page_links_carry_access_key(log_client, config):
    """Download and back links keep the access key."""
    test_client, _ = log_client
    config.set("Website", "AccessKey", "letmein")
    body = test_client.get("/log", params={"key": "letmein"}).text

    assert "/log/download?key=letmein" in body
    assert "/system?key=letmein" in body
