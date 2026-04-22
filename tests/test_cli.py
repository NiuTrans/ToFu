"""Tests for the async subprocess wrapper.

Uses plain ``asyncio.run`` to remain compatible with environments where
pytest-asyncio is unavailable.
"""

from __future__ import annotations

import asyncio
import json


def test_run_hope_echoes_args(fake_hope, monkeypatch):
    from hope_mcp.cli import run_hope
    result = asyncio.run(run_hope(["run", "--xml", "my.xml"]))
    assert result.ok, result.stderr
    line = [ln for ln in result.stdout.splitlines() if ln.startswith("CMD_ARGS=")]
    assert line, result.stdout
    parsed = json.loads(line[0][len("CMD_ARGS="):])
    assert parsed == ["run", "--xml", "my.xml"]


def test_run_hope_captures_nonzero(fake_hope, monkeypatch):
    from hope_mcp.cli import run_hope
    monkeypatch.setenv("HOPE_FAKE_RC", "2")
    monkeypatch.setenv("HOPE_FAKE_STDERR", "boom")
    result = asyncio.run(run_hope(["status"]))
    assert not result.ok
    assert result.returncode == 2
    assert "boom" in result.stderr


def test_run_hope_parses_json(fake_hope, monkeypatch):
    from hope_mcp.cli import run_hope
    monkeypatch.setenv("HOPE_FAKE_STDOUT", '{"runid": 42, "status": "RUNNING"}\n')
    result = asyncio.run(run_hope(["status", "--json"], parse_json=True))
    assert result.ok
    assert result.json == {"runid": 42, "status": "RUNNING"}


def test_run_hope_missing_bin(monkeypatch):
    monkeypatch.setenv("HOPE_BIN", "/definitely/not/a/real/hope")
    import importlib
    from hope_mcp import config as cfg_mod
    importlib.reload(cfg_mod)
    from hope_mcp import cli as cli_mod
    importlib.reload(cli_mod)
    result = asyncio.run(cli_mod.run_hope(["status"]))
    assert not result.ok
    assert result.returncode == 127
    assert "not found" in result.stderr
