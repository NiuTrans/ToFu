"""tests/test_agent_winbuilder.py — the AGENT target of the Windows builder.

WHAT THIS GUARDS
----------------
docs/modules/remote_execution.md: winbuilder builds a SECOND
payload (target='agent') carrying only the desktop-agent closure. The
contract:

  * payload identity — the agent has its OWN deps stamp (tofu-agent.spec
    + the agent pip recipe; server requirements are NOT inputs) and its
    own payload name (payload-agent-…), so it never collides with or
    invalidates the full payload's cache;
  * the full target is byte-identical to before (name, stamp inputs);
  * the agent pipeline installs the AGENT pip recipe — never
    requirements.txt, which IS the server stack this target exists to
    leave behind (NEUTER: swapping in the full recipe must go red);
  * the agent smoke gate is the agent's own env var + sentinel
    (TOFU_AGENT_SMOKE / TOFU_AGENT_SMOKE_OK);
  * the registration frame carries a version (owner amendment ② — the
    drift-detection half);
  * the smoke gate's banned-list assertion is wired (source pin).

The heavy half (git/pip/pyinstaller) is faked exactly like
test_winbuilder.py; the recording half (tar + atomic rename) runs real.

Run:  pytest tests/test_agent_winbuilder.py -q
"""

from __future__ import annotations

import inspect
import os
import tarfile

import pytest

from lib.desktop_dist import winbuilder as wb

pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Isolate the store, the payload cache and the toolchain layout."""
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path / 'store'))
    monkeypatch.setenv('TOFU_WIN_TC_DIR', str(tmp_path / 'local'))
    payloads = tmp_path / 'payloads'
    monkeypatch.setattr(wb, '_payloads_dir',
                        lambda: (os.makedirs(payloads, exist_ok=True),
                                 str(payloads))[1])
    monkeypatch.setattr(wb.wintoolchain, 'provision', lambda: None)
    return tmp_path


def _fake_log(tmp_path):
    return open(tmp_path / 'log.txt', 'w', encoding='utf-8')


def _fake_agent_dist(workdir):
    dist = os.path.join(workdir, 'staging')
    os.makedirs(os.path.join(dist, 'TofuAgent'), exist_ok=True)
    with open(os.path.join(dist, 'TofuAgent', 'TofuAgent.exe'), 'w') as f:
        f.write('fake-binary')
    return dist


# ═══════════════════════════════════════════════════════════════════
#  payload identity — two targets, two independent caches
# ═══════════════════════════════════════════════════════════════════

def test_agent_stamp_differs_from_full_and_is_stable():
    a = wb.deps_stamp('agent')
    assert len(a) == 16 and all(c in '0123456789abcdef' for c in a)
    assert wb.deps_stamp('agent') == a, 'same inputs, same stamp'
    assert a != wb.deps_stamp('full'), (
        'the agent stamp must be its own identity — sharing the full '
        'stamp would cross-invalidate both caches')


def test_full_payload_name_is_byte_identical_to_before(isolated):
    p = wb.payload_path('abcdef1234567890', 'deadbeefcafebabe')
    assert p.endswith('payload-abcdef123456-deadbeefcafebabe.tar.gz'), (
        'the full payload name must NOT change — existing caches and '
        'every audit referencing it stay valid')


def test_agent_payload_name_is_namespaced(isolated):
    p = wb.payload_path('abcdef1234567890', 'deadbeefcafebabe',
                        target='agent')
    assert p.endswith(
        'payload-agent-abcdef123456-deadbeefcafebabe.tar.gz')


def test_the_two_caches_do_not_satisfy_each_other(isolated):
    sha = 'abc123def456' + '0' * 28
    for target, prefix in (('full', 'payload'), ('agent', 'payload-agent')):
        dest = wb.payload_path(sha, target=target)
        assert os.path.basename(dest).startswith(prefix + '-')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(b'payload')
    # Each target sees ONLY its own file.
    assert wb.cached_payload(sha, target='full') == wb.payload_path(sha)
    assert (wb.cached_payload(sha, target='agent')
            == wb.payload_path(sha, target='agent'))


def test_agent_cache_hit_skips_the_pipeline(isolated, monkeypatch):
    sha = 'abc123def456' + '0' * 28
    dest = wb.payload_path(sha, target='agent')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'wb') as f:
        f.write(b'payload')
    monkeypatch.setattr(wb, '_git', lambda *a: sha + '\n')
    monkeypatch.setattr(wb, '_pipeline',
                        lambda *a, **k: pytest.fail(
                            'pipeline ran on an agent cache hit'))
    wb._run('test', 'agent')
    st = wb.state()
    assert st['state'] == 'ok' and st['cached'] is True
    assert st['target'] == 'agent'
    assert st['payload'] == dest


def test_NEUTER_agent_run_records_its_own_payload(isolated, monkeypatch):
    """A real agent run (pipeline faked) must tar the TofuAgent dir into
    the AGENT cache name — dropping the target threading anywhere
    (stamp, path, tar dir) is caught by the name or the contents."""
    monkeypatch.setattr(wb, '_git', lambda *a: 'a' * 40 + '\n')
    monkeypatch.setattr(wb, '_pipeline',
                        lambda workdir, version, sha, log_fh, **_:
                        _fake_agent_dist(workdir))
    wb._run('test', 'agent')
    st = wb.state()
    assert st['state'] == 'ok', st
    dest = st['payload']
    assert dest == wb.payload_path('a' * 40, target='agent')
    assert os.path.basename(dest).startswith('payload-agent-')
    with tarfile.open(dest) as tf:
        assert any(n.endswith('TofuAgent/TofuAgent.exe')
                   for n in tf.getnames()), (
            'the tarball must carry the TofuAgent app dir — tarring '
            "'Tofu' here would ship an empty or wrong payload")


def test_agent_stamp_ignores_requirements_txt(isolated, monkeypatch,
                                              tmp_path):
    """A server-only dependency bump must NOT rebuild the agent payload:
    requirements.txt is not an agent stamp input."""
    import hashlib
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'tofu-agent.spec').write_text('# spec v1\n')
    monkeypatch.setattr(wb, '_REPO_ROOT', str(root))
    stamp_before = wb.deps_stamp('agent')
    (root / 'requirements.txt').write_text('a-brand-new-server-dep==9.9\n')
    assert wb.deps_stamp('agent') == stamp_before, (
        'the agent stamp must ignore requirements.txt — the agent '
        'payload does not contain it')
    # …but the spec IS an input.
    (root / 'tofu-agent.spec').write_text('# spec v2 — changed\n')
    assert wb.deps_stamp('agent') != stamp_before


# ═══════════════════════════════════════════════════════════════════
#  the agent pipeline shape (source pins — the heavy half is wine)
# ═══════════════════════════════════════════════════════════════════

def test_agent_pipeline_installs_the_agent_recipe_not_requirements():
    """NEUTER-bait: the agent pipeline's pip step must name the AGENT
    recipe and must NOT touch requirements.txt. Freezing the server
    stack into the agent is the exact defect this component exists to
    kill — a 'harmless simplification' that reuses the full recipe must
    be a red test."""
    src = inspect.getsource(wb._pipeline)
    assert "'pip-agent-deps'" in src
    assert '_AGENT_PIP' in src
    agent_head = src[src.index("if target == 'agent'"):]
    agent_head = agent_head[:agent_head.index('else:')]
    assert "'pip-requirements'" not in agent_head
    assert '_requirements_without_tofu_search' not in agent_head
    # The step itself must install a NAMED LIST, never a requirements file.
    step = src[src.index("'pip-agent-deps'"):]
    step = step[:step.index('log_fh, cwd=src')]
    assert '-r ' not in step, (
        'the agent pip step installs a requirements file — that file IS '
        'the server stack this target exists to leave behind')
    # The full branch keeps its recipe (order pinned by the sibling suite).
    assert "'pip-requirements'" in src


def test_agent_pipeline_uses_the_agent_spec_and_smoke_gate():
    src = inspect.getsource(wb._pipeline).replace("'", '"')
    # The per-target table drives spec/exe/env/sentinel — not literals.
    assert 'tgt["spec"]' in src and 'tgt["exe"]' in src
    assert 'tgt["smoke_env"]' in src and 'tgt["smoke_sentinel"]' in src
    t = wb._TARGETS['agent']
    assert t['spec'] == 'tofu-agent.spec'
    assert t['app_dir'] == 'TofuAgent' and t['exe'] == 'TofuAgent.exe'
    assert t['smoke_env'] == 'TOFU_AGENT_SMOKE'
    assert t['smoke_sentinel'] == 'TOFU_AGENT_SMOKE_OK'
    # …and the full row is the historical shape.
    f = wb._TARGETS['full']
    assert f['spec'] == 'tofu.spec' and f['app_dir'] == 'Tofu'
    assert f['smoke_env'] == 'TOFU_SMOKE'
    assert f['smoke_sentinel'] == 'TOFU_SMOKE_OK'


# ═══════════════════════════════════════════════════════════════════
#  Half B — the agent wrapper (makensis faked; untar + recording real)
# ═══════════════════════════════════════════════════════════════════

def test_agent_wrap_records_a_kind_agent_artifact(isolated, monkeypatch,
                                                  tmp_path):
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'TofuAgent').mkdir(parents=True)
    (payload_dir / 'TofuAgent' / 'TofuAgent.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'TofuAgent', arcname='TofuAgent')

    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd:
            out = isolated / 'wrap' / 'TofuAgent-Setup-0.16.0-win64.exe'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b'NSIS-AGENT-INSTALLER')
            return None
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        dest = wb.wrap_payload(str(payload_tar), '0.16.0', 'b' * 40, log,
                               server_url='https://tofu.example.com/',
                               workdir=str(isolated / 'wrap'),
                               target='agent')
    assert os.path.basename(dest) == 'TofuAgent-Setup-0.16.0-win64.exe'
    e = wb.store.artifacts()['TofuAgent-Setup-0.16.0-win64.exe']
    assert e['kind'] == 'agent', (
        'the store must tell the two components apart — a kindless agent '
        'entry reads as full (absent ⇒ full) and the selector would offer '
        'it to full-app seekers')
    assert e['source'] == 'built' and e['os'] == 'windows'
    assert e['label'] == 'Windows agent installer'
    # The record keeps the url as passed; the FILE gets the rstripped
    # form (the launcher's import contract).
    assert e['preseed']['url'] == 'https://tofu.example.com/'
    import json
    pre = json.loads((isolated / 'wrap' / 'payload'
                      / 'preseed_server.json').read_text())
    assert pre['url'] == 'https://tofu.example.com'


def _wrap_agent_with_fake_makensis(isolated, monkeypatch, tmp_path,
                                   server_url, out_name):
    """Drive the REAL wrap half (untar + record) with makensis faked."""
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'TofuAgent').mkdir(parents=True)
    (payload_dir / 'TofuAgent' / 'TofuAgent.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'TofuAgent', arcname='TofuAgent')

    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd:
            out = isolated / 'wrap' / out_name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b'NSIS-AGENT-INSTALLER')
            return None
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        wb.wrap_payload(str(payload_tar), '0.16.0', 'b' * 40, log,
                        server_url=server_url,
                        workdir=str(isolated / 'wrap'), target='agent')


def test_agent_wrap_drops_a_loopback_preseed(isolated, monkeypatch,
                                             tmp_path):
    """The measured trap (first agent artifact, 2026-08-02): built from a
    server-local request, it shipped preseed http://127.0.0.1:15000 — the
    office PC attaches to its OWN loopback and never reaches the server.
    The agent target drops it; the personalized installer record owns attach."""
    _wrap_agent_with_fake_makensis(isolated, monkeypatch, tmp_path,
                                   'http://127.0.0.1:15000',
                                   'TofuAgent-Setup-0.16.0-win64.exe')
    e = wb.store.artifacts()['TofuAgent-Setup-0.16.0-win64.exe']
    assert 'preseed' not in e, (
        'a loopback preseed must be dropped, not recorded')
    assert not os.path.exists(
        os.path.join(str(isolated / 'wrap'), 'payload',
                     'preseed_server.json')), (
        'the preseed FILE must not ship either — its presence is what '
        'would override the personalized installer attachment')


def test_agent_wrap_keeps_a_reachable_preseed(isolated, monkeypatch,
                                              tmp_path):
    _wrap_agent_with_fake_makensis(isolated, monkeypatch, tmp_path,
                                   'https://tofu.example.com',
                                   'TofuAgent-Setup-0.16.0-win64.exe')
    e = wb.store.artifacts()['TofuAgent-Setup-0.16.0-win64.exe']
    assert e['preseed']['url'] == 'https://tofu.example.com'


def test_full_wrap_keeps_a_loopback_preseed(isolated, monkeypatch,
                                            tmp_path):
    """The FULL target is byte-identical discipline: its primary install
    case is the server's own machine (local_source), where a loopback
    preseed is exactly right. The drop is agent-only."""
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'Tofu').mkdir(parents=True)
    (payload_dir / 'Tofu' / 'Tofu.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'Tofu', arcname='Tofu')

    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd:
            out = isolated / 'wrap' / 'Tofu-Setup-0.16.0-win64.exe'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b'NSIS-FULL-INSTALLER')
            return None
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        wb.wrap_payload(str(payload_tar), '0.16.0', 'c' * 40, log,
                        server_url='http://127.0.0.1:15000',
                        workdir=str(isolated / 'wrap'))
    e = wb.store.artifacts()['Tofu-Setup-0.16.0-win64.exe']
    assert e['preseed']['url'] == 'http://127.0.0.1:15000'


def test_full_wrap_record_has_kind_full(isolated, monkeypatch, tmp_path):
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'Tofu').mkdir(parents=True)
    (payload_dir / 'Tofu' / 'Tofu.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'Tofu', arcname='Tofu')

    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd:
            out = isolated / 'wrap' / 'Tofu-Setup-0.16.0-win64.exe'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b'NSIS-FULL-INSTALLER')
            return None
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        wb.wrap_payload(str(payload_tar), '0.16.0', 'c' * 40, log,
                        workdir=str(isolated / 'wrap'))
    e = wb.store.artifacts()['Tofu-Setup-0.16.0-win64.exe']
    assert e['kind'] == 'full'
    assert e['label'] == 'Windows installer'


def test_NEUTER_agent_wrap_of_a_full_payload_is_refused(isolated,
                                                        monkeypatch,
                                                        tmp_path):
    """Target/payload mismatch must fail LOUD: wrapping the full payload
    as 'agent' would produce an installer with no TofuAgent.exe."""
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'Tofu').mkdir(parents=True)
    (payload_dir / 'Tofu' / 'Tofu.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'Tofu', arcname='Tofu')
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        with pytest.raises(RuntimeError, match='no TofuAgent.exe'):
            wb.wrap_payload(str(payload_tar), '0.16.0', 'd' * 40, log,
                            workdir=str(isolated / 'wrap'), target='agent')


# ═══════════════════════════════════════════════════════════════════
#  the tcl/tk graft — the nuget python ships NO tkinter (measured)
# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
#  the tcl/tk graft — the nuget python ships NO tkinter (measured)
# ═══════════════════════════════════════════════════════════════════

def test_winpython_provisioning_includes_the_tk_graft():
    """The nuget CPython has 0 tcl files (measured in the nupkg) — without
    the graft every built installer's native control panel is dead. Pin the
    call site."""
    src = inspect.getsource(wb._ensure_winpython)
    assert '_ensure_winpython_tk' in src, (
        'the tcl/tk graft fell out of winpython provisioning — every '
        'installer built from now on has a dead native control panel')


def test_tk_graft_short_circuits_on_a_grafted_python(isolated, monkeypatch,
                                                     tmp_path):
    rootfs = tmp_path / 'rootfs'
    tools = rootfs / 'opt' / 'winpy' / 'tools'
    (tools / 'DLLs').mkdir(parents=True)
    (tools / 'DLLs' / '_tkinter.pyd').write_text('x')
    (tools / 'Lib' / 'tkinter').mkdir(parents=True)
    (tools / 'tcl').mkdir(parents=True)
    monkeypatch.setattr(wb.wintoolchain, 'rootfs_dir', lambda: str(rootfs))
    monkeypatch.setattr(wb.wintoolchain, '_download',
                        lambda *a: pytest.fail('download on a grafted python'))
    with _fake_log(isolated) as log:
        wb._ensure_winpython_tk(log)   # must not download, must not raise


def test_tk_graft_copies_the_standard_layout(isolated, monkeypatch, tmp_path):
    rootfs = tmp_path / 'rootfs'
    tools = rootfs / 'opt' / 'winpy' / 'tools'
    (tools / 'DLLs').mkdir(parents=True)
    cache = tmp_path / 'cache'
    (cache).mkdir()
    (cache / 'python-tcltk.msi').write_text('fake-msi')
    staging = rootfs / 'work' / 'tcltk-graft'

    def _fake_sh(cmd, log_fh, *, shell=False, timeout=9999):
        # The msiextract fake: materialise the python.org standard layout.
        for rel in ('DLLs/_tkinter.pyd', 'DLLs/tcl86t.dll',
                    'DLLs/tk86t.dll', 'DLLs/zlib1.dll'):
            p = staging / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('x')
        for rel in ('Lib/tkinter', 'tcl/tcl8.6', 'tcl/tk8.6'):
            (staging / rel).mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(wb.wintoolchain, 'rootfs_dir', lambda: str(rootfs))
    monkeypatch.setattr(wb.wintoolchain, 'cache_dir', lambda: str(cache))
    monkeypatch.setattr(wb, '_ensure_msiextract',
                        lambda log: '/fake/msiextract')
    monkeypatch.setattr(wb, '_sh', _fake_sh)
    with _fake_log(isolated) as log:
        wb._ensure_winpython_tk(log)
    assert (tools / 'DLLs' / '_tkinter.pyd').is_file()
    assert (tools / 'DLLs' / 'tcl86t.dll').is_file()
    assert (tools / 'Lib' / 'tkinter').is_dir()
    assert (tools / 'tcl' / 'tcl8.6').is_dir()
    # The staging dir is cleaned up after a successful graft.
    assert not staging.exists()


def test_agent_stamp_tracks_the_tk_graft(monkeypatch):
    a = wb.deps_stamp('agent')
    monkeypatch.setattr(wb, '_TK_MSI_SHA256', '0' * 64)
    assert wb.deps_stamp('agent') != a, (
        'the tk graft is payload content — its identity must invalidate '
        'the agent payload cache')


# ═══════════════════════════════════════════════════════════════════
#  the agent registration frame carries a version (owner amendment ②)
# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
#  the agent registration frame carries a version (owner amendment ②)
# ═══════════════════════════════════════════════════════════════════

def test_agent_frame_carries_the_version():
    from lib.desktop_agent._run import _build_agent_frame
    frame = _build_agent_frame('a' * 32, {'allow_write': False})
    from lib.version import __version__
    assert frame['version'] == __version__.strip(), (
        'the poll frame must carry the agent version — the server '
        'cannot flag agent↔server protocol drift without it (owner '
        'amendment ②)')
    # The historical fields are untouched.
    assert frame['agent_id'] == 'a' * 32
    assert 'capabilities' in frame and 'share_roots' in frame


def test_agent_frame_version_never_breaks_the_poll(monkeypatch):
    """A version-read failure must degrade to '' — never stop the frame."""
    import lib.desktop_agent._run as run_mod
    monkeypatch.setitem(__import__('sys').modules, 'lib.version', None)
    assert run_mod._agent_version() == ''
    frame = run_mod._build_agent_frame('b' * 32, {})
    assert frame['version'] == ''


# ═══════════════════════════════════════════════════════════════════
#  the smoke gate asserts the small closure (source pins)
# ═══════════════════════════════════════════════════════════════════

def test_agent_smoke_gate_bans_the_server_stack():
    """The frozen-build proof of the size claim: the smoke gate must
    assert the server stack is absent from BOTH sys.modules and the
    bundle tree, and that tkinter (the native control panel) ships."""
    import desktop.agent_launcher as al
    src = inspect.getsource(al._smoke_main)
    for banned in ('quart', 'flask', 'hypercorn'):
        assert banned in src, f'smoke gate lost its {banned} assertion'
    assert '_MEIPASS' in src, 'the bundle-tree assertion is gone'
    assert 'import tkinter' in src, (
        'the tkinter assertion is gone — a build without the control-panel '
        "toolkit would ship and only fail on the user's machine")
    assert 'TOFU_AGENT_SMOKE_OK' in src


def test_launcher_delegates_the_preseed_import():
    """The full launcher imports installer configuration through connect_ui."""
    import desktop.launcher as launcher
    src_pre = inspect.getsource(launcher._import_preseed)
    assert 'connect_ui' in src_pre
    assert 'os.remove' not in src_pre and 'os.path.isfile' not in src_pre, (
        'the preseed logic is back in launcher.py — the move became a copy')


def test_connect_ui_owns_the_wire_format_parse():
    """Already-shipped callers may still invoke the legacy repair dialog."""
    import desktop.connect_ui as cui
    src = inspect.getsource(cui.prompt_connect_line)
    assert 'parse_connect_line' in src, (
        'the dialog must parse through lib.desktop_agent.config — the '
        'single owner of the connect-line format')
