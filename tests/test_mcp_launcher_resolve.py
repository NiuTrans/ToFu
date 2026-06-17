"""tests/test_mcp_launcher_resolve.py — MCP launcher self-heal.

Covers the auto-recovery for the most common "launcher X is not on PATH"
report: a pip-installed console script (e.g. ``hope-mcp``) that lives next
to the running interpreter but whose ``bin/`` dir isn't on the spawned
subprocess's ``$PATH``. Tofu should resolve it (and propagate the env's
``bin/`` to the child) instead of telling the user to install something
that's already installed.

Run:  pytest tests/test_mcp_launcher_resolve.py -m unit
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

import lib.mcp.client as mc
from lib.mcp.client import (
    _find_vendored_source,
    _prepend_interpreter_bin_to_path,
    _resolve_launcher,
    _try_autoinstall_launcher,
)

pytestmark = pytest.mark.unit


def _make_exe(path: str) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n')
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_resolve_finds_script_next_to_interpreter(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    _make_exe(str(fake_bin / 'my-mcp'))
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))

    resolved = _resolve_launcher('my-mcp')
    assert resolved == str(fake_bin / 'my-mcp')


def test_resolve_returns_none_for_missing(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))
    assert _resolve_launcher('definitely-not-here-xyz') is None


def test_resolve_ignores_pathful_command():
    # Anything with a separator is the caller's responsibility, not ours.
    assert _resolve_launcher('/usr/bin/env') is None
    assert _resolve_launcher('./foo') is None


def test_resolve_skips_non_executable(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    # Present but NOT executable → must not be resolved.
    (fake_bin / 'not-exec').write_text('#!/bin/sh\n')
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))
    assert _resolve_launcher('not-exec') is None


def test_resolve_checks_base_prefix_bin(tmp_path, monkeypatch):
    # Script installed against base interpreter, exe_dir empty of it.
    exe_dir = tmp_path / 'venv' / 'bin'
    exe_dir.mkdir(parents=True)
    base = tmp_path / 'base'
    (base / 'bin').mkdir(parents=True)
    _make_exe(str(base / 'bin' / 'base-mcp'))
    monkeypatch.setattr(sys, 'executable', str(exe_dir / 'python'))
    monkeypatch.setattr(sys, 'base_prefix', str(base))

    assert _resolve_launcher('base-mcp') == str(base / 'bin' / 'base-mcp')


def test_prepend_interpreter_bin_to_path(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))

    env = {'PATH': '/usr/bin:/bin'}
    _prepend_interpreter_bin_to_path(env)
    parts = env['PATH'].split(os.pathsep)
    assert parts[0] == str(fake_bin)
    assert '/usr/bin' in parts and '/bin' in parts


def test_prepend_is_idempotent(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))

    env = {'PATH': '/usr/bin'}
    _prepend_interpreter_bin_to_path(env)
    once = env['PATH']
    _prepend_interpreter_bin_to_path(env)
    assert env['PATH'] == once                       # no duplicate prepend
    assert env['PATH'].split(os.pathsep).count(str(fake_bin)) == 1


def test_prepend_dedupes_existing_entry(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))

    # bin dir already present mid-PATH → should move to front, not duplicate.
    env = {'PATH': os.pathsep.join(['/usr/bin', str(fake_bin), '/bin'])}
    _prepend_interpreter_bin_to_path(env)
    parts = env['PATH'].split(os.pathsep)
    assert parts[0] == str(fake_bin)
    assert parts.count(str(fake_bin)) == 1


def test_prepend_noop_without_path_key(tmp_path, monkeypatch):
    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    monkeypatch.setattr(sys, 'executable', str(fake_bin / 'python'))
    env: dict[str, str] = {}
    _prepend_interpreter_bin_to_path(env)
    assert env['PATH'] == str(fake_bin)


# ── First-connect auto-install ───────────────────────────

def _register_fake_vendor(tmp_path, monkeypatch, command='fake-mcp', layout='vendored'):
    """Create a source dir + register it; return (src_dir, repo_root).

    layout='vendored' → repo/tools/<command> (deploy snapshot, non-editable).
    layout='sibling'  → repo/../<command> (dev checkout, editable).
    """
    repo = tmp_path / 'repo'
    repo.mkdir(exist_ok=True)
    if layout == 'sibling':
        src = tmp_path / command            # sibling of repo, outside tools/
        rel = f'../{command}'
    else:
        src = repo / 'tools' / command
        rel = f'tools/{command}'
    src.mkdir(parents=True)
    (src / 'pyproject.toml').write_text('[project]\nname="x"\n')
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, command, {'sources': [rel]})
    # Fresh install-guard state for each test.
    monkeypatch.setattr(mc, '_install_attempted', set())
    return str(src), str(repo)


def test_find_vendored_source_skips_missing(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / 'tools').mkdir(parents=True)
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'gone-mcp',
                        {'sources': ['tools/gone-mcp']})
    monkeypatch.setattr(mc, '_install_attempted', set())
    assert _find_vendored_source('gone-mcp') is None
    assert _find_vendored_source('not-registered') is None


def test_find_vendored_source_editability(tmp_path, monkeypatch):
    # Vendored snapshot under tools/ → non-editable.
    src_v, _ = _register_fake_vendor(tmp_path, monkeypatch, 'vend-mcp', 'vendored')
    found = _find_vendored_source('vend-mcp')
    assert found == (src_v, False)
    # Sibling dev checkout outside tools/ → editable.
    src_s, _ = _register_fake_vendor(tmp_path, monkeypatch, 'sib-mcp', 'sibling')
    found = _find_vendored_source('sib-mcp')
    assert found == (src_s, True)


def test_find_vendored_source_prefers_sibling_editable(tmp_path, monkeypatch):
    # Both layouts present: sibling listed first → wins, editable=True.
    repo = tmp_path / 'repo'
    (repo / 'tools' / 'dual-mcp').mkdir(parents=True)
    (repo / 'tools' / 'dual-mcp' / 'pyproject.toml').write_text('x')
    sib = tmp_path / 'dual-mcp'
    sib.mkdir()
    (sib / 'pyproject.toml').write_text('x')
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'dual-mcp',
                        {'sources': ['../dual-mcp', 'tools/dual-mcp']})
    monkeypatch.setattr(mc, '_install_attempted', set())
    assert _find_vendored_source('dual-mcp') == (str(sib), True)


def test_autoinstall_vendored_is_non_editable(tmp_path, monkeypatch):
    src, repo = _register_fake_vendor(tmp_path, monkeypatch, layout='vendored')

    captured = {}

    def _fake_run(args, **kw):
        captured['args'] = args
        captured['env'] = kw.get('env', {})
        class R:
            returncode = 0
            stdout = 'ok'
            stderr = ''
        return R()

    monkeypatch.setattr(mc.subprocess, 'run', _fake_run)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: f'/env/bin/{c}')

    out = _try_autoinstall_launcher('fake-mcp')
    assert out == '/env/bin/fake-mcp'
    assert captured['args'][:5] == [sys.executable, '-m', 'pip', 'install', '--no-input']
    # Vendored snapshot → NON-editable.
    assert '-e' not in captured['args']
    assert src in captured['args']
    assert captured['env'].get('PIP_REQUIRE_VIRTUALENV') == 'false'


def test_autoinstall_sibling_is_editable(tmp_path, monkeypatch):
    src, repo = _register_fake_vendor(tmp_path, monkeypatch, layout='sibling')

    captured = {}

    def _fake_run(args, **kw):
        captured['args'] = args
        class R:
            returncode = 0
            stdout = 'ok'
            stderr = ''
        return R()

    monkeypatch.setattr(mc.subprocess, 'run', _fake_run)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: f'/env/bin/{c}')

    out = _try_autoinstall_launcher('fake-mcp')
    assert out == '/env/bin/fake-mcp'
    # Sibling dev checkout → editable.
    assert '-e' in captured['args']
    assert src in captured['args']


def test_autoinstall_failure_returns_none(tmp_path, monkeypatch):
    _register_fake_vendor(tmp_path, monkeypatch)

    def _fail_run(args, **kw):
        class R:
            returncode = 1
            stdout = ''
            stderr = 'boom'
        return R()

    monkeypatch.setattr(mc.subprocess, 'run', _fail_run)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: None)
    assert _try_autoinstall_launcher('fake-mcp') is None


def test_autoinstall_attempted_at_most_once(tmp_path, monkeypatch):
    _register_fake_vendor(tmp_path, monkeypatch)
    calls = {'n': 0}

    def _count_run(args, **kw):
        calls['n'] += 1
        class R:
            returncode = 1
            stdout = ''
            stderr = 'boom'
        return R()

    monkeypatch.setattr(mc.subprocess, 'run', _count_run)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: None)

    _try_autoinstall_launcher('fake-mcp')
    _try_autoinstall_launcher('fake-mcp')      # second call must NOT re-pip
    assert calls['n'] == 1


def test_autoinstall_unregistered_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, '_install_attempted', set())
    ran = {'n': 0}
    monkeypatch.setattr(mc.subprocess, 'run',
                        lambda *a, **k: ran.__setitem__('n', ran['n'] + 1))
    assert _try_autoinstall_launcher('totally-unknown-mcp') is None
    assert ran['n'] == 0                        # never even invoked pip
