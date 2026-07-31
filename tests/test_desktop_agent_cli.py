#!/usr/bin/env python3
"""Unit tests for the desktop-agent ``--root NAME=PATH`` CLI flag.

The flag is the ONLY supported entry point for declaring share_roots short of
hand-editing the agent config, so the contract under test is:

  * merge semantics (pure): add / update-in-place / order preserved /
    malformed refused / ``~`` expanded / ``=`` inside the path survives;
  * persistence (load-bearing): roots declared via --root land in the agent
    config file and are still there on the NEXT start without the flag —
    drop the save and the second-start test goes RED;
  * main() wiring: run_agent is reached with the flag's roots already in the
    config it reads.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_desktop_agent_cli.py -q
"""

import json
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  1. merge_cli_roots — pure merge semantics
# ══════════════════════════════════════════════════════════

def test_merge_adds_to_empty():
    from lib.desktop_agent.config import merge_cli_roots
    out = merge_cli_roots([], ['app=/code/app'])
    assert out == [{'name': 'app', 'path': '/code/app'}]


def test_merge_accumulates_in_declaration_order():
    from lib.desktop_agent.config import merge_cli_roots
    out = merge_cli_roots([], ['b=/two', 'a=/one'])
    assert [r['name'] for r in out] == ['b', 'a']


def test_merge_same_name_updates_path_keeps_position():
    from lib.desktop_agent.config import merge_cli_roots
    existing = [{'name': 'app', 'path': '/old'}, {'name': 'x', 'path': '/x'}]
    out = merge_cli_roots(existing, ['app=/new'])
    assert out == [{'name': 'app', 'path': '/new'}, {'name': 'x', 'path': '/x'}]


def test_merge_expands_user_home():
    from lib.desktop_agent.config import merge_cli_roots
    out = merge_cli_roots([], ['app=~/code/app'])
    assert out[0]['path'] == os.path.expanduser('~/code/app')
    assert '~' not in out[0]['path']


def test_merge_path_may_contain_equals_sign():
    """Split on the FIRST '=' only — a path holding '=' must survive intact."""
    from lib.desktop_agent.config import merge_cli_roots
    out = merge_cli_roots([], ['app=/code/a=b'])
    assert out[0]['path'] == '/code/a=b'


@pytest.mark.parametrize('bad', ['no-equals-sign', '=noname', 'name=', ''])
def test_merge_malformed_specs_raise(bad):
    from lib.desktop_agent.config import merge_cli_roots
    with pytest.raises(ValueError):
        merge_cli_roots([], [bad])


def test_merge_ignores_junk_existing_entries():
    """A hand-edited config with malformed entries must not crash the merge."""
    from lib.desktop_agent.config import merge_cli_roots
    out = merge_cli_roots(['junk', {'no_name': True}, None], ['app=/a'])
    assert out == [{'name': 'app', 'path': '/a'}]


# ══════════════════════════════════════════════════════════
#  2. main() wiring + persistence (the load-bearing half)
# ══════════════════════════════════════════════════════════

def _run_main(argv, cfg_path, monkeypatch):
    """Invoke main() with run_agent stubbed out; returns the captured call."""
    import lib.desktop_agent._run as ar
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(cfg_path))
    captured = {}

    def _fake_run_agent(server, permissions, poll_interval=1.0,
                        bridge_secret='', stop_event=None):
        captured['server'] = server
        captured['permissions'] = permissions

    monkeypatch.setattr(ar, 'run_agent', _fake_run_agent)
    ar.main(argv)
    return captured


def test_main_persists_roots_to_config(tmp_path, monkeypatch):
    cfg = tmp_path / 'agent.json'
    root = tmp_path / 'app'
    root.mkdir()
    _run_main(['--server', 'http://x', '--root', f'app={root}'],
              cfg, monkeypatch)
    data = json.loads(cfg.read_text(encoding='utf-8'))
    assert data['share_roots'] == [{'name': 'app', 'path': str(root)}]


def test_main_roots_survive_restart_without_flag(tmp_path, monkeypatch):
    """PERSISTENCE proof — declare once, restart flagless, roots still there.
    Goes RED the moment the save_config call is dropped (NEUTER-equivalent)."""
    cfg = tmp_path / 'agent.json'
    root = tmp_path / 'app'
    root.mkdir()
    _run_main(['--server', 'http://x', '--root', f'app={root}'],
              cfg, monkeypatch)
    _run_main(['--server', 'http://x'], cfg, monkeypatch)  # no --root
    data = json.loads(cfg.read_text(encoding='utf-8'))
    assert data['share_roots'] == [{'name': 'app', 'path': str(root)}]


def test_main_second_declaration_updates_not_duplicates(tmp_path, monkeypatch):
    cfg = tmp_path / 'agent.json'
    _run_main(['--server', 'http://x', '--root', 'app=/old'],
              cfg, monkeypatch)
    _run_main(['--server', 'http://x', '--root', 'app=/new'],
              cfg, monkeypatch)
    data = json.loads(cfg.read_text(encoding='utf-8'))
    assert data['share_roots'] == [{'name': 'app', 'path': '/new'}]


def test_main_malformed_root_exits_with_usage_error(tmp_path, monkeypatch):
    import lib.desktop_agent._run as ar
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    monkeypatch.setattr(ar, 'run_agent', lambda *a, **k: None)
    with pytest.raises(SystemExit) as ei:
        ar.main(['--server', 'http://x', '--root', 'garbage'])
    assert ei.value.code == 2  # argparse usage error
    # Nothing may be persisted on a rejected declaration.
    assert not (tmp_path / 'agent.json').exists()


def test_main_run_agent_still_called_and_perms_flow(tmp_path, monkeypatch):
    captured = _run_main(['--server', 'http://x', '--allow-write',
                          '--root', 'app=/tmp/anywhere'],
                         tmp_path / 'agent.json', monkeypatch)
    assert captured['server'] == 'http://x'
    assert captured['permissions']['allow_write'] is True
    assert captured['permissions']['allow_exec'] is False
