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


def test_load_config_ok(monkeypatch):
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
        monkeypatch.setenv("DEMO_SECRET", "shhh")
        importlib.reload(config_mod)
        cfg = config_mod.load_config()
        assert cfg["host"] == "127.0.0.1"
        assert cfg["port"] == 8788
        assert cfg["sources"]["demo"]["secret"] == b"shhh"
        assert cfg["sources"]["demo"]["callback_url"] == "http://example.com/cb"


def test_load_config_missing_secret(monkeypatch):
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
        with pytest.raises(config_mod.ConfigError):
            config_mod.load_config()


def test_load_config_bad_version(monkeypatch):
    config_mod._cached_config = None
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        _write_config(path, {"version": 99, "sources": {}})
        monkeypatch.setenv("AGENT_WAKE_CONFIG", path)
        importlib.reload(config_mod)
        with pytest.raises(config_mod.ConfigError):
            config_mod.load_config()
