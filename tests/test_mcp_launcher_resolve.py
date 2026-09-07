"""tests/test_mcp_launcher_resolve.py — MCP launcher self-heal.

Covers the auto-recovery for the most common "launcher X is not on PATH"
report: a console script that lives next to the running interpreter but
whose ``bin/`` dir isn't on the spawned subprocess's ``$PATH``.

Since pt_9345a80f417d43ca the vendored-server half of this flow is ISOLATION:
``vendored_launch_argv`` maps a bare vendored name to
``uv run --no-project --with-editable <src>`` instead of pip-installing into
the shared interpreter. The pip-era auto-install tests were replaced by the
uniform-launch contract below (and by tests/test_mcp_vendored_isolation.py).

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
    _launcher_install_hint,
    _prepend_interpreter_bin_to_path,
    _resolve_launcher,
    vendored_launch_argv,
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
    # Fresh install-error state for each test.
    monkeypatch.setattr(mc, '_install_last_error', {})
    # Freeze the hot-reload baseline far in the future so _reload_vendored_if_changed
    # early-returns: monkeypatched entries must NOT be clobbered by a real
    # vendored.py reload triggered by an unrelated edit during the test run.
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    return str(src), str(repo)


def test_find_vendored_source_skips_missing(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / 'tools').mkdir(parents=True)
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'gone-mcp',
                        {'sources': ['tools/gone-mcp']})
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
    assert _find_vendored_source('dual-mcp') == (str(sib), True)


def test_launch_argv_is_uniform_editable_for_any_source_layout(tmp_path, monkeypatch):
    """Sibling AND tools/ sources both launch with --with-editable.

    The pip era distinguished editable (sibling) from non-editable (tools/)
    because a cached copy was acceptable for a snapshot. Measured 2026-07-31:
    uv's cached local-dir builds serve STALE content (a file added to the
    source was missing from the installed package even with --refresh), so a
    non-editable snapshot silently runs old code after `make vendor-mcp`.
    The launch is therefore uniformly editable; pin it so nobody reintroduces
    a cached-build "optimization" for tools/.
    """
    src_v, _ = _register_fake_vendor(tmp_path, monkeypatch, 'vend-mcp', 'vendored')
    argv = vendored_launch_argv('vend-mcp')
    assert argv is not None
    assert argv[:4] == ['uv', 'run', '--no-project', '--with-editable']
    assert argv[4] == src_v and argv[-1] == 'vend-mcp'

    src_s, _ = _register_fake_vendor(tmp_path, monkeypatch, 'sib-mcp', 'sibling')
    argv = vendored_launch_argv('sib-mcp')
    assert argv is not None
    assert argv[:4] == ['uv', 'run', '--no-project', '--with-editable']
    assert argv[4] == src_s and argv[-1] == 'sib-mcp'


def test_launch_argv_unregistered_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    assert vendored_launch_argv('totally-unknown-mcp') is None


def test_prewarm_failure_is_retryable_not_wedged(tmp_path, monkeypatch):
    """A failed warm must NOT poison the command for the process lifetime.

    The pip era needed an explicit one-attempt guard because a wedged command
    meant "install always fails until restart". The uv-warm era keeps the
    retryable half of that contract: a transient failure records the reason
    and a later call may try again.
    """
    _register_fake_vendor(tmp_path, monkeypatch)
    calls = {'n': 0}

    def _fail_run(args, **kw):
        calls['n'] += 1
        class R:
            returncode = 1
            stdout = ''
            stderr = 'network unreachable'
        return R()

    monkeypatch.setattr(mc.subprocess, 'run', _fail_run)
    ready, detail = mc.prewarm_vendored_launcher('fake-mcp')
    assert ready is False
    assert 'network unreachable' in mc._install_last_error['fake-mcp']
    ready, detail = mc.prewarm_vendored_launcher('fake-mcp')
    assert ready is False
    assert calls['n'] == 2, 'a second attempt must be allowed to retry'


def test_launcher_hint_vendored_surfaces_reason(tmp_path, monkeypatch):
    # The hint for a registered-but-failed vendored server must name the real
    # reason + the exact warm command, NOT the generic "package manager" line.
    src, _ = _register_fake_vendor(tmp_path, monkeypatch, 'sib-mcp', 'sibling')
    mc._install_last_error['sib-mcp'] = 'uv warm exited 1: boom'

    hint = _launcher_install_hint('sib-mcp')
    assert 'boom' in hint
    assert 'uv run' in hint and '--with-editable' in hint
    assert src in hint
    assert 'package manager' not in hint


def test_launcher_hint_unregistered_is_generic(tmp_path, monkeypatch):
    # An unregistered, non-bundled launcher still gets the generic fallback.
    monkeypatch.setattr(mc, '_repo_root', lambda: str(tmp_path))
    monkeypatch.setattr(mc, '_install_last_error', {})
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    hint = _launcher_install_hint('totally-unknown-mcp')
    assert 'package manager' in hint


# ── Hot-reload of vendored.py (no restart needed) ────────

import types as _types  # noqa: E402


_FAKE_VENDORED_SEQ = [0]


def _fake_vendored_module(tmp_path, monkeypatch, launchers):
    """Build a throwaway module standing in for lib.mcp.vendored on disk.

    Returns (module, path). The module's VENDORED_LAUNCHERS is `launchers`;
    its source file is written so importlib.reload picks up edits.

    ``importlib.reload`` re-finds the spec via the meta-path finders by module
    NAME, so the module must be genuinely importable from a dir on sys.path —
    not merely present in sys.modules. We give it a unique name + put tmp_path
    on sys.path so reload resolves it the same way it resolves the real
    ``lib.mcp.vendored`` in production.
    """
    import importlib as _il
    import sys as _sys
    _FAKE_VENDORED_SEQ[0] += 1
    name = f'fake_vendored_mod_{_FAKE_VENDORED_SEQ[0]}'
    path = tmp_path / f'{name}.py'
    path.write_text('VENDORED_LAUNCHERS = ' + repr(launchers) + '\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = _il.import_module(name)
    monkeypatch.setitem(_sys.modules, name, mod)
    return mod, str(path)


def test_hot_reload_picks_up_new_row(tmp_path, monkeypatch):
    # A row added to vendored.py while running must be merged into the LIVE
    # registry on the next _find_vendored_source, without a restart.
    live = {'hope-mcp': {'sources': ['../hope-mcp', 'tools/hope-mcp']}}
    mod, path = _fake_vendored_module(tmp_path, monkeypatch, live)
    monkeypatch.setattr(mc, '_vendored_mod', mod)
    monkeypatch.setattr(mc, '_VENDORED_LAUNCHERS', live)
    monkeypatch.setattr(mc, '_vendored_mtime', os.path.getmtime(path))

    assert 'newrow-mcp' not in mc._VENDORED_LAUNCHERS

    # Simulate an on-disk edit adding a new server, bump mtime to guarantee
    # detection regardless of filesystem timestamp granularity.
    import time
    new_src = "VENDORED_LAUNCHERS = {'hope-mcp': {'sources': ['../hope-mcp']}, " \
              "'newrow-mcp': {'sources': ['../newrow-mcp', 'tools/newrow-mcp']}}\n"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_src)
    os.utime(path, (time.time() + 10, time.time() + 10))

    mc._reload_vendored_if_changed()

    # Live dict identity preserved AND new row merged in.
    assert mc._VENDORED_LAUNCHERS is live
    assert 'newrow-mcp' in mc._VENDORED_LAUNCHERS
    assert 'hope-mcp' in mc._VENDORED_LAUNCHERS       # existing row kept


def test_hot_reload_survives_broken_edit(tmp_path, monkeypatch):
    # A syntactically broken mid-save must NOT wipe the registry — keep
    # last-good and log, so connects don't all start failing.
    live = {'hope-mcp': {'sources': ['../hope-mcp']},
            'llm-mcp': {'sources': ['../llm-mcp']}}
    mod, path = _fake_vendored_module(tmp_path, monkeypatch, live)
    monkeypatch.setattr(mc, '_vendored_mod', mod)
    monkeypatch.setattr(mc, '_VENDORED_LAUNCHERS', live)
    monkeypatch.setattr(mc, '_vendored_mtime', os.path.getmtime(path))

    import time
    with open(path, 'w', encoding='utf-8') as f:
        f.write('VENDORED_LAUNCHERS = { this is not valid python {{{\n')
    os.utime(path, (time.time() + 10, time.time() + 10))

    mc._reload_vendored_if_changed()   # must swallow the SyntaxError

    assert mc._VENDORED_LAUNCHERS is live
    assert {'hope-mcp', 'llm-mcp'} <= set(mc._VENDORED_LAUNCHERS)


def test_hot_reload_noop_when_unchanged(tmp_path, monkeypatch):
    # No mtime advance → no reload (cheap path, registry untouched).
    live = {'hope-mcp': {'sources': ['../hope-mcp']}}
    mod, path = _fake_vendored_module(tmp_path, monkeypatch, live)
    monkeypatch.setattr(mc, '_vendored_mod', mod)
    monkeypatch.setattr(mc, '_VENDORED_LAUNCHERS', live)
    monkeypatch.setattr(mc, '_vendored_mtime', os.path.getmtime(path) + 100)

    reloaded = {'n': 0}
    real_reload = mc.importlib.reload
    monkeypatch.setattr(mc.importlib, 'reload',
                        lambda m: (reloaded.__setitem__('n', reloaded['n'] + 1),
                                   real_reload(m))[1])
    mc._reload_vendored_if_changed()
    assert reloaded['n'] == 0                          # never reloaded
    assert mc._VENDORED_LAUNCHERS == live


# ── Vendored-snapshot staleness detection (+ opt-in auto-vendor) ──

def _make_vendor_pair(tmp_path, monkeypatch, name='pair-mcp', synced=True):
    """Build a repo with a sibling dev checkout + a tools/ snapshot.

    Registers ``name`` with both sources and points _repo_root at the repo.
    When ``synced`` the snapshot mirrors the sibling (rsync -a semantics: same
    relpaths, size, mtime); otherwise the snapshot omits the file so it drifts.
    Returns (repo, sibling_dir, snapshot_dir).
    """
    import shutil
    repo = tmp_path / 'repo'
    (repo / 'tools').mkdir(parents=True, exist_ok=True)
    sibling = tmp_path / name                          # outside tools/
    snapshot = repo / 'tools' / name
    # Sibling content.
    (sibling / 'src').mkdir(parents=True)
    (sibling / 'pyproject.toml').write_text('[project]\nname="x"\n')
    (sibling / 'src' / 'mod.py').write_text('print("v1")\n')
    if synced:
        # Mirror exactly, preserving mtimes (what rsync -a / vendor_mcp.sh do).
        snapshot.mkdir(parents=True)
        shutil.copytree(sibling, snapshot, dirs_exist_ok=True)
        for root, _dirs, files in os.walk(sibling):
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(snapshot, os.path.relpath(s, sibling))
                st = os.stat(s)
                os.utime(d, (st.st_atime, st.st_mtime))
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, name,
                        {'sources': [f'../{name}', f'tools/{name}']})
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))   # freeze hot-reload
    monkeypatch.setattr(mc, '_snapshot_reported', {})
    return str(repo), str(sibling), str(snapshot)


def test_snapshot_fresh_when_synced(tmp_path, monkeypatch):
    _repo, sib, snap = _make_vendor_pair(tmp_path, monkeypatch, synced=True)
    assert mc._snapshot_stale_reason(sib, snap) == ''


def test_snapshot_ignores_runtime_artifacts(tmp_path, monkeypatch):
    # Excluded artifacts present only on one side must NOT count as drift.
    _repo, sib, snap = _make_vendor_pair(tmp_path, monkeypatch, synced=True)
    os.makedirs(os.path.join(sib, 'src', '__pycache__'))
    with open(os.path.join(sib, 'src', '__pycache__', 'mod.cpython-312.pyc'), 'w') as f:
        f.write('bytecode')
    os.makedirs(os.path.join(sib, 'x.egg-info'))
    with open(os.path.join(sib, 'x.egg-info', 'PKG-INFO'), 'w') as f:
        f.write('meta')
    os.makedirs(os.path.join(sib, '.tofu_trash', 'recovered'), exist_ok=True)
    with open(os.path.join(sib, '.tofu_trash', 'recovered', 'old.py'), 'w') as f:
        f.write('not source')
    os.makedirs(os.path.join(sib, '.oa-skills-ref'), exist_ok=True)
    with open(os.path.join(sib, '.oa-skills-ref', 'client.js'), 'w') as f:
        f.write('local reference')
    with open(os.path.join(sib, 'JOURNAL.md'), 'w') as f:
        f.write('personal notes')
    with open(os.path.join(sib, 'citadel (1).zip'), 'w') as f:
        f.write('local archive')
    assert mc._snapshot_stale_reason(sib, snap) == ''


def test_snapshot_detects_changed_file(tmp_path, monkeypatch):
    _repo, sib, snap = _make_vendor_pair(tmp_path, monkeypatch, synced=True)
    # Edit the sibling so content + mtime drift.
    import time
    p = os.path.join(sib, 'src', 'mod.py')
    with open(p, 'w') as f:
        f.write('print("v2 changed")\n')
    os.utime(p, (time.time() + 5, time.time() + 5))
    reason = mc._snapshot_stale_reason(sib, snap)
    assert 'changed' in reason
    assert 'src/mod.py' in reason


def test_snapshot_detects_missing(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    (repo / 'tools').mkdir(parents=True)
    sibling = tmp_path / 'gone-mcp'
    (sibling).mkdir()
    (sibling / 'pyproject.toml').write_text('[project]\nname="x"\n')
    assert mc._snapshot_stale_reason(str(sibling),
                                     str(repo / 'tools' / 'gone-mcp')) == 'snapshot missing'


def test_check_staleness_detect_only_no_write(tmp_path, monkeypatch):
    # Default (TOFU_MCP_AUTO_VENDOR unset): warn, never rebuild.
    _repo, sib, snap = _make_vendor_pair(tmp_path, monkeypatch, name='detect-mcp',
                                         synced=True)
    monkeypatch.delenv('TOFU_MCP_AUTO_VENDOR', raising=False)
    ran = {'n': 0}
    monkeypatch.setattr(mc, '_run_vendor_script',
                        lambda *a, **k: ran.__setitem__('n', ran['n'] + 1) or True)
    # Drift the sibling.
    import time
    p = os.path.join(sib, 'src', 'mod.py')
    with open(p, 'w') as f:
        f.write('drifted\n')
    os.utime(p, (time.time() + 5, time.time() + 5))

    mc._check_snapshot_staleness('detect-mcp')
    assert ran['n'] == 0                               # NEVER vendored
    assert mc._snapshot_stale_reason(sib, snap) != ''  # snapshot untouched


def test_check_staleness_optin_rebuilds(tmp_path, monkeypatch):
    # TOFU_MCP_AUTO_VENDOR=1: detection triggers _run_vendor_script.
    _repo, sib, snap = _make_vendor_pair(tmp_path, monkeypatch, name='auto-mcp',
                                         synced=True)
    monkeypatch.setenv('TOFU_MCP_AUTO_VENDOR', '1')
    called = {'cmd': None, 'root': None}

    def _fake_vendor(command, root):
        called['cmd'] = command
        called['root'] = root
        return True

    monkeypatch.setattr(mc, '_run_vendor_script', _fake_vendor)
    import time
    p = os.path.join(sib, 'src', 'mod.py')
    with open(p, 'w') as f:
        f.write('drifted\n')
    os.utime(p, (time.time() + 5, time.time() + 5))

    mc._check_snapshot_staleness('auto-mcp')
    assert called['cmd'] == 'auto-mcp'                 # rebuild invoked
    assert called['root'] == _repo


def test_check_staleness_noop_without_sibling(tmp_path, monkeypatch):
    # Deploy layout: only a tools/ snapshot, no sibling → silent no-op (nothing
    # to compare against / vendor from).
    repo = tmp_path / 'repo'
    snap = repo / 'tools' / 'deploy-mcp'
    snap.mkdir(parents=True)
    (snap / 'pyproject.toml').write_text('[project]\nname="x"\n')
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, 'deploy-mcp',
                        {'sources': ['../deploy-mcp', 'tools/deploy-mcp']})
    monkeypatch.setattr(mc, '_snapshot_reported', {})
    monkeypatch.setenv('TOFU_MCP_AUTO_VENDOR', '1')    # even with opt-in on
    ran = {'n': 0}
    monkeypatch.setattr(mc, '_run_vendor_script',
                        lambda *a, **k: ran.__setitem__('n', ran['n'] + 1) or True)
    mc._check_snapshot_staleness('deploy-mcp')
    assert ran['n'] == 0                               # nothing to vendor from


def test_check_staleness_noop_for_non_vendored(tmp_path, monkeypatch):
    # A plain npx/uvx command not in the registry is ignored entirely.
    monkeypatch.setattr(mc, '_snapshot_reported', {})
    ran = {'n': 0}
    monkeypatch.setattr(mc, '_run_vendor_script',
                        lambda *a, **k: ran.__setitem__('n', ran['n'] + 1) or True)
    mc._check_snapshot_staleness('npx')                # not registered
    assert ran['n'] == 0
