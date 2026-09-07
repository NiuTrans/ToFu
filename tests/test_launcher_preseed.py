"""tests/test_launcher_preseed.py — the installer's zero-paste attachment.

WHAT THIS GUARDS
----------------
A server-built installer (lib/desktop_dist/winbuilder.py) bakes
``preseed_server.json`` next to Tofu.exe so the first run attaches to
the server it was built FROM without the user pasting anything.
``desktop/launcher.py::_import_preseed`` imports it with four rules that
must each survive contact with reality:

  * ONE-SHOT — the file is deleted after ANY attempt (a stale preseed
    must never override a later user choice);
  * NEVER overrides an existing attachment (the user's own connect wins);
  * a malformed preseed is logged and REMOVED, never wedging first run;
  * no file → no-op (the normal, non-preseeded path).

The config store is isolated via TOFU_DESKTOP_CONFIG.

Run:  pytest tests/test_launcher_preseed.py -q
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolate the exe dir (preseed location) and the agent config store."""
    import desktop.launcher as launcher
    from lib.desktop_agent import config as agent_config
    exe_dir = tmp_path / 'exe'
    exe_dir.mkdir()
    monkeypatch.setattr(launcher, '_EXE_DIR', str(exe_dir))
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG',
                       str(tmp_path / 'desktop_agent.json'))
    return launcher, agent_config, exe_dir


def _write_preseed(exe_dir, data):
    with open(exe_dir / 'preseed_server.json', 'w', encoding='utf-8') as f:
        json.dump(data, f)


def test_a_valid_preseed_attaches_and_is_deleted(env):
    launcher, agent_config, exe_dir = env
    _write_preseed(exe_dir, {'v': 1, 'url': 'https://tofu.example.com/p/'})
    launcher._import_preseed()
    url, secret = agent_config.remote_server()
    assert url == 'https://tofu.example.com/p', (
        'the url is attached with the trailing slash normalised')
    assert secret == '', 'phase 1 preseeds no secret'
    assert not (exe_dir / 'preseed_server.json').exists(), (
        'ONE-SHOT: the file must be deleted after import')


def test_an_existing_attachment_is_never_overridden(env):
    launcher, agent_config, exe_dir = env
    agent_config.save_remote_server('https://user-chosen.example.com', 'tok')
    _write_preseed(exe_dir, {'v': 1, 'url': 'https://installer.example.com'})
    launcher._import_preseed()
    url, secret = agent_config.remote_server()
    assert url == 'https://user-chosen.example.com', (
        "the user's own connect wins over the install-time default")
    assert secret == 'tok'
    assert not (exe_dir / 'preseed_server.json').exists(), (
        'even when ignored, the file is consumed (one-shot)')


def test_a_malformed_preseed_is_removed_and_never_attaches(env):
    launcher, agent_config, exe_dir = env
    _write_preseed(exe_dir, {'v': 1, 'url': 'not-a-url'})
    launcher._import_preseed()
    url, _ = agent_config.remote_server()
    assert url == '', 'a bad preseed must not attach'
    assert not (exe_dir / 'preseed_server.json').exists(), (
        'a bad preseed must not wedge first run — it is removed')


def test_no_preseed_file_is_a_noop(env):
    launcher, agent_config, exe_dir = env
    launcher._import_preseed()   # must not raise
    url, _ = agent_config.remote_server()
    assert url == ''


def test_the_import_runs_in_main_before_the_tray():
    """The wiring, not just the function: main()'s first-launch block must
    call _import_preseed, or the file the installer ships is dead weight."""
    import inspect
    import desktop.launcher as launcher
    src = inspect.getsource(launcher.main)
    assert '_import_preseed()' in src
