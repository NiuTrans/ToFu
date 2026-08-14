"""Legacy connect-line compatibility and the current zero-input surface.

Personalized installers now carry the server address and bridge credential.
The old parser stays readable for already-shipped clients, but current web and
desktop launchers must not ask a user to mint, copy, or paste authentication.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent


def _parse(line: str):
    from lib.desktop_agent.config import parse_connect_line
    return parse_connect_line(line)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("https://tofu.example.com  tok_ABC123",
         ("https://tofu.example.com", "tok_ABC123")),
        (" https://tofu.example.com/\ttok_XYZ ",
         ("https://tofu.example.com", "tok_XYZ")),
        ("https://corp.example.com/tofu/\ntok_P",
         ("https://corp.example.com/tofu", "tok_P")),
    ],
)
def test_already_shipped_connect_lines_remain_parseable(line, expected):
    assert _parse(line) == expected


@pytest.mark.parametrize("bad", [
    "tok_ONLY",
    "https://tofu.example.com",
    "",
    "ftp://tofu.example.com tok_X",
])
def test_incomplete_legacy_lines_are_refused_without_echoing_secrets(bad):
    with pytest.raises(ValueError) as exc:
        _parse(bad)
    assert str(exc.value).strip()
    assert "tok_X" not in str(exc.value)


def test_no_attachment_means_the_local_server(tmp_path, monkeypatch):
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import remote_server
    assert remote_server() == ("", "")


def test_installer_attachment_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import remote_server, save_remote_server
    save_remote_server("https://tofu.example.com", "tok_PERSIST")
    assert remote_server() == ("https://tofu.example.com", "tok_PERSIST")


def test_saving_attachment_preserves_agent_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TOFU_DESKTOP_CONFIG", str(tmp_path / "agent.json"))
    from lib.desktop_agent.config import load_config, save_config, save_remote_server
    save_config({"agent_id": "abc123",
                 "share_roots": [{"name": "p", "path": "/tmp/p"}]})
    save_remote_server("https://tofu.example.com", "tok_1")
    cfg = load_config()
    assert cfg["agent_id"] == "abc123"
    assert cfg["share_roots"] == [{"name": "p", "path": "/tmp/p"}]


def test_current_web_surface_exposes_no_authentication_workflow():
    src = (ROOT / "frontend" / "src" / "runtime" / "app-runtime.js").read_text(
        encoding="utf-8")
    for forbidden in (
        "function _lcMintToken(",
        "function _lcConnectLine(",
        "Api.desktop.mintToken(",
        "lcMintBtn",
        "lcTokenBox",
    ):
        assert forbidden not in src


@pytest.mark.parametrize("launcher", [
    ROOT / "desktop" / "launcher.py",
    ROOT / "desktop" / "agent_launcher.py",
])
def test_current_desktop_surface_exposes_no_manual_authentication(launcher):
    src = launcher.read_text(encoding="utf-8")
    for forbidden in (
        "on_connect_remote",
        "def on_connect(",
        "prompt_connect_line(",
        "desktop.tray.connectRemote",
        "desktop.tray.connectDifferent",
        "'connect': lambda",
    ):
        assert forbidden not in src


def test_role_window_has_no_connect_or_repair_button():
    src = (ROOT / "desktop" / "role_window.py").read_text(encoding="utf-8")
    assert "_act('connect')" not in src
    assert "desktop.tray.connectRemote" not in src
    assert "desktop.tray.connectDifferent" not in src
