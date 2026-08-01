import pytest
import os
import json
import tempfile
import importlib

import agent_wake_claude.config as config_mod
from agent_wake_claude.config import load_config, ConfigError


def _write_config(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def test_load_config_v0(monkeypatch):
    config_mod._cached_config = None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        _write_config(path, {
            "version": 0,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "sources": {
                "demo": {"secret_env": "DEMO_SECRET", "callback_url": "http://example.com/cb"},
            },
            "default_callback_url": None,
        })
        monkeypatch.setenv("AGENT_WAKE_CONFIG", path)
        importlib.reload(config_mod)
        cfg = config_mod.load_config()
        assert "demo" in cfg["sources"]
        assert cfg["host"] == "127.0.0.1"
        assert cfg["port"] == 8788
        assert cfg["socket_path"] is None


def test_load_config_v1(monkeypatch):
    config_mod._cached_config = None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        _write_config(path, {
            "version": 1,
            "listen": {"host": "127.0.0.1", "port": 8788},
            "socket_path": "/tmp/test.sock",
            "sources": {
                "github-actions": {"secret_env": "GH_SECRET", "callback_url": None},
            },
            "routing": {},
            "default_callback_url": None,
        })
        monkeypatch.setenv("AGENT_WAKE_CONFIG", path)
        importlib.reload(config_mod)
        cfg = config_mod.load_config()
        assert "github-actions" in cfg["sources"]
        assert cfg["socket_path"] == "/tmp/test.sock"


def test_load_config_no_secret_required(monkeypatch):
    config_mod._cached_config = None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        _write_config(path, {
            "version": 0,
            "listen": {},
            "sources": {
                "demo": {"secret_env": "MISSING_SECRET"},
            },
        })
        monkeypatch.setenv("AGENT_WAKE_CONFIG", path)
        importlib.reload(config_mod)
        cfg = config_mod.load_config()
        assert "demo" in cfg["sources"]


def test_load_config_bad_version(monkeypatch):
    config_mod._cached_config = None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        _write_config(path, {"version": 99, "sources": {"demo": {}}})
        monkeypatch.setenv("AGENT_WAKE_CONFIG", path)
        importlib.reload(config_mod)
        with pytest.raises(config_mod.ConfigError):
            config_mod.load_config()


def test_load_config_no_sources(monkeypatch):
    config_mod._cached_config = None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        _write_config(path, {"version": 0, "sources": {}})
        monkeypatch.setenv("AGENT_WAKE_CONFIG", path)
        importlib.reload(config_mod)
        with pytest.raises(config_mod.ConfigError):
            config_mod.load_config()


def test_reload_config_refreshes_environment_secret(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    _write_config(str(path), {
        "version": 1,
        "sources": {"demo": {"secret_env": "DEMO_SECRET"}},
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(path))
    monkeypatch.setenv("DEMO_SECRET", "first")
    config_mod._cached_config = None

    assert config_mod.load_config()["sources"]["demo"]["secret"] == b"first"
    monkeypatch.setenv("DEMO_SECRET", "second")
    assert config_mod.load_config()["sources"]["demo"]["secret"] == b"first"
    assert config_mod.reload_config()["sources"]["demo"]["secret"] == b"second"


def test_failed_reload_preserves_last_valid_config(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    _write_config(str(path), {
        "version": 1,
        "sources": {"demo": {"secret_env": "DEMO_SECRET"}},
    })
    monkeypatch.setenv("AGENT_WAKE_CONFIG", str(path))
    config_mod._cached_config = None
    original = config_mod.load_config()
    path.write_text("not json")

    with pytest.raises(config_mod.ConfigError):
        config_mod.reload_config()

    assert config_mod.load_config() is original
