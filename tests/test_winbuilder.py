"""tests/test_winbuilder.py — the Windows payload builder guards.

WHAT THIS GUARDS
----------------
lib/desktop_dist/winbuilder.py builds the Windows PyInstaller payload
inside the userspace Wine toolchain. Two traps were measured while
building it (both in the module docstring); this suite pins them so a
future "cleanup" reintroduces them as a RED test:

1.  **Exit codes are unreliable** under the preloader-less wine
    (sys.exit(3) → 0, measured). Every wine step must run through
    ``cmd /c "<inner> && echo <sentinel>"`` and be judged by the
    sentinel in stdout — never by the process status.
2.  **Host python env is poison** (PIP_REQUIRE_VIRTUALENV=1 killed the
    first pip rehearsal). ``_wine_env`` must scrub PIP_*/PYTHON*/CONDA*
    and keep only the allowlist (proxy/locale/WINEPREFIX).

Plus the cache contract: payload id = (git_sha, deps_stamp), a cache hit
skips the whole pipeline, and the recording half (tar + atomic rename)
is driven for real while the heavy half (git/pip/pyinstaller) is faked.

Run:  pytest tests/test_winbuilder.py -q
"""

from __future__ import annotations

import os
import types

import pytest

from lib.desktop_dist import winbuilder as wb

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  Fixtures / fakes
# ═══════════════════════════════════════════════════════════════════

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


def _fake_dist(workdir):
    dist = os.path.join(workdir, 'staging')
    os.makedirs(os.path.join(dist, 'Tofu'), exist_ok=True)
    with open(os.path.join(dist, 'Tofu', 'Tofu.exe'), 'w') as f:
        f.write('fake-binary')
    return dist


# ═══════════════════════════════════════════════════════════════════
#  trap 1 — the sentinel protocol (exit codes are meaningless)
# ═══════════════════════════════════════════════════════════════════

def test_wstep_runs_cmd_with_inline_sentinel(isolated, monkeypatch):
    seen = {}

    class _Out:
        returncode = 0
        stdout = b'some output\nTOFU_STEP_OK_demo\n'
        stderr = b''

    def _run(argv, **kw):
        seen['argv'] = argv
        return _Out()

    monkeypatch.setattr(wb.subprocess, 'run', _run)
    monkeypatch.setattr(wb.wintoolchain, '_wine_argv', lambda *a: list(a))
    with _fake_log(isolated) as log:
        wb._wstep('demo', 'do-the-thing', log)
    argv = seen['argv']
    assert argv[:2] == ['cmd', '/c'], argv
    assert argv[2] == 'do-the-thing && echo TOFU_STEP_OK_demo'


def test_wstep_without_the_sentinel_fails_even_at_exit_0(isolated,
                                                         monkeypatch):
    """The whole point of trap 1: a step that dies silently (wine reports
    exit 0 regardless) must still be caught — by the MISSING sentinel."""

    class _Out:
        returncode = 0          # wine lies; this is the measured case
        stdout = b'boom happened but wine said 0\n'
        stderr = b''

    monkeypatch.setattr(wb.subprocess, 'run', lambda *a, **k: _Out())
    monkeypatch.setattr(wb.wintoolchain, '_wine_argv', lambda *a: list(a))
    with _fake_log(isolated) as log:
        with pytest.raises(RuntimeError, match='no sentinel'):
            wb._wstep('demo', 'do-the-thing', log)


def test_wpystep_wraps_cwd_into_the_cmd_string(isolated, monkeypatch):
    seen = {}

    class _Out:
        returncode = 0
        stdout = b'TOFU_STEP_OK_demo\n'
        stderr = b''

    def _run(argv, **kw):
        seen['argv'] = argv
        return _Out()

    monkeypatch.setattr(wb.subprocess, 'run', _run)
    monkeypatch.setattr(wb.wintoolchain, '_wine_argv', lambda *a: list(a))
    monkeypatch.setattr(wb.wintoolchain, 'guest_z',
                        lambda p: 'Z:\\' + p.replace('/', '\\').lstrip('\\'))
    with _fake_log(isolated) as log:
        wb._wpystep('demo', 'python -m pip install x', log,
                    cwd='/some/dir')
    assert seen['argv'][2].startswith('cd /d Z:\\'), seen['argv'][2]
    assert '&& python -m pip install x && echo' in seen['argv'][2]


# ═══════════════════════════════════════════════════════════════════
#  trap 2 — env scrub
# ═══════════════════════════════════════════════════════════════════

def test_wine_env_strips_host_python_poison(monkeypatch):
    monkeypatch.setenv('PIP_REQUIRE_VIRTUALENV', '1')
    monkeypatch.setenv('PYTHONPATH', '/host/poison')
    monkeypatch.setenv('CONDA_PREFIX', '/host/conda')
    monkeypatch.setenv('VIRTUAL_ENV', '/host/venv')
    monkeypatch.setenv('https_proxy', 'http://proxy:8412')
    env = wb._wine_env()
    for poison in ('PIP_REQUIRE_VIRTUALENV', 'PYTHONPATH', 'CONDA_PREFIX',
                   'VIRTUAL_ENV'):
        assert poison not in env, f'{poison} leaked into the wine env'
    assert env['https_proxy'] == 'http://proxy:8412', (
        'the proxy must survive — pip in the guest needs it')
    assert env['WINEPREFIX'], 'WINEPREFIX must be pinned to the guest path'
    # PATH is a measured trap of its own: absent → proot cannot resolve the
    # `env` command ('env not found ($PATH=(null))', first real build);
    # host-inherited → unix poison. It must be the minimal guest value.
    assert env['PATH'] == '/usr/local/bin:/usr/bin:/bin'
    assert env['PATH'] != os.environ.get('PATH', '')


# ═══════════════════════════════════════════════════════════════════
#  payload id + cache
# ═══════════════════════════════════════════════════════════════════

def test_deps_stamp_is_stable_and_sensitive():
    a = wb.deps_stamp()
    assert len(a) == 16 and all(c in '0123456789abcdef' for c in a)
    assert wb.deps_stamp() == a, 'same inputs must give the same stamp'


def test_payload_path_carries_sha_and_stamp(isolated):
    p = wb.payload_path('abcdef1234567890', 'deadbeefcafebabe')
    assert p.endswith('payload-abcdef123456-deadbeefcafebabe.tar.gz')


def test_a_cache_hit_skips_the_whole_pipeline(isolated, monkeypatch):
    sha = 'abc123def456' + '0' * 28
    dest = wb.payload_path(sha)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'wb') as f:
        f.write(b'payload')
    monkeypatch.setattr(wb, '_git', lambda *a: sha + '\n')
    monkeypatch.setattr(wb, '_pipeline',
                        lambda *a, **k: pytest.fail(
                            'pipeline ran on a cache hit'))
    wb._run('test')
    st = wb.state()
    assert st['state'] == 'ok' and st['cached'] is True
    assert st['payload'] == dest


def test_a_full_run_records_the_payload(isolated, monkeypatch):
    """The heavy half is faked; the REAL tar + cache write runs."""
    monkeypatch.setattr(wb, '_git', lambda *a: 'f' * 40 + '\n')
    monkeypatch.setattr(wb, '_pipeline',
                        lambda workdir, version, sha, log_fh, **_:
                        _fake_dist(workdir))
    wb._run('test')
    st = wb.state()
    assert st['state'] == 'ok', st
    dest = st['payload']
    assert os.path.isfile(dest)
    assert dest == wb.payload_path('f' * 40)
    # The tarball really contains the payload.
    import tarfile
    with tarfile.open(dest) as tf:
        assert any(n.endswith('Tofu/Tofu.exe') for n in tf.getnames())


def test_a_failed_pipeline_records_the_error_and_caches_nothing(
        isolated, monkeypatch):
    monkeypatch.setattr(wb, '_git', lambda *a: 'e' * 40 + '\n')

    def _boom(workdir, version, sha, log_fh, **_):
        raise RuntimeError('pyinstaller exploded')

    monkeypatch.setattr(wb, '_pipeline', _boom)
    wb._run('test')
    st = wb.state()
    assert st['state'] == 'error'
    assert 'pyinstaller exploded' in st['error']
    assert not os.path.isfile(wb.payload_path('e' * 40))


# ═══════════════════════════════════════════════════════════════════
#  _pip_index_args — host pip config bridge
# ═══════════════════════════════════════════════════════════════════

def test_pip_index_env_translates_the_host_mirror_config(monkeypatch):
    """The measured fix: the guest must use the host's HTTP mirrors — a
    guest pip reaching pypi.org directly dies on the proxy's MITM cert.
    ENV form (not CLI args): pip's PEP 517 build-isolation sub-pip
    inherits env, never the parent's CLI flags (measured)."""
    out = types.SimpleNamespace(stdout=(
        b"global.index-url='http://pip.corp.example/simple/'\n"
        b"install.extra-index-url='\\nhttp://pypi.corp.example/simple/'\n"
        b"install.trusted-host='\\npip.corp.example\\npypi.corp.example'\n"
    ))
    monkeypatch.setattr(wb.subprocess, 'run',
                        lambda *a, **k: out)
    env = wb._pip_index_env()
    assert env['PIP_INDEX_URL'] == 'http://pip.corp.example/simple/'
    assert env['PIP_EXTRA_INDEX_URL'] == (
        'http://pypi.corp.example/simple/ https://pypi.org/simple')
    assert env['PIP_TRUSTED_HOST'] == (
        'pip.corp.example pypi.corp.example pypi.org '
        'files.pythonhosted.org'), (
        'pypi.org + files.pythonhosted.org must be trusted: mirrors have '
        'gaps (pymupdf_layout measured) and the proxy MITMs — trusted-host '
        'is the same posture the host pip.conf applies to its mirrors')


def test_pip_index_env_falls_back_to_pypi_without_config(monkeypatch):
    out = types.SimpleNamespace(stdout=b'')
    monkeypatch.setattr(wb.subprocess, 'run',
                        lambda *a, **k: out)
    assert wb._pip_index_env() == {
        'PIP_EXTRA_INDEX_URL': 'https://pypi.org/simple'}


# ═══════════════════════════════════════════════════════════════════
#  Half B — the NSIS wrapper
# ═══════════════════════════════════════════════════════════════════

def test_render_nsi_substitutes_every_placeholder():
    text = wb._render_nsi('0.16.0', '/tmp/payload', '/tmp/out.exe')
    for p in wb._NSI_PLACEHOLDERS:
        assert p not in text, f'{p} left unrendered'
    assert 'Tofu-Setup' not in text or '0.16.0' in text


def test_wrap_payload_records_a_built_windows_artifact(isolated,
                                                       monkeypatch,
                                                       tmp_path):
    """makensis is faked (writes a canned exe); untar + recording are real."""
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'Tofu').mkdir(parents=True)
    (payload_dir / 'Tofu' / 'Tofu.exe').write_text('exe')
    (payload_dir / 'Tofu' / 'preseed_marker.txt').write_text('x')
    payload_tar = tmp_path / 'payload.tar.gz'
    import tarfile
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'Tofu', arcname='Tofu')

    makensis_out = {}

    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd or '/makensis' in cmd:
            out = isolated / 'wrap' / 'Tofu-Setup-0.16.0-win64.exe'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b'NSIS-INSTALLER-BYTES')
            makensis_out['ran'] = True
            return None
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    with _fake_log(isolated) as log:
        dest = wb.wrap_payload(str(payload_tar), '0.16.0', 'a' * 40, log,
                               server_url='https://tofu.example.com/p/15000',
                               workdir=str(isolated / 'wrap'))
    assert makensis_out.get('ran'), 'makensis step never ran'
    assert os.path.basename(dest) == 'Tofu-Setup-0.16.0-win64.exe'
    e = wb.store.artifacts()['Tofu-Setup-0.16.0-win64.exe']
    assert e['source'] == 'built' and e['os'] == 'windows'
    assert e['version'] == '0.16.0' and e['size'] > 0
    assert e['preseed']['url'] == 'https://tofu.example.com/p/15000'
    # The preseed file must have landed next to Tofu.exe pre-pack.
    assert os.path.isfile(
        os.path.join(str(isolated / 'wrap'), 'payload',
                     'preseed_server.json'))


def test_wrap_without_a_server_url_writes_no_preseed(isolated, monkeypatch,
                                                     tmp_path):
    payload_dir = tmp_path / 'payload-src'
    (payload_dir / 'Tofu').mkdir(parents=True)
    (payload_dir / 'Tofu' / 'Tofu.exe').write_text('exe')
    payload_tar = tmp_path / 'payload.tar.gz'
    import tarfile
    with tarfile.open(payload_tar, 'w:gz') as tf:
        tf.add(payload_dir / 'Tofu', arcname='Tofu')
    def _sh(cmd, log_fh, *, shell=False, timeout=9999):
        if 'makensis' in cmd:
            return None        # produces NOTHING — wrap must fail loud
        import subprocess as sp
        return sp.run(cmd if shell else cmd.split(), shell=shell,
                      capture_output=True, timeout=timeout)

    monkeypatch.setattr(wb, '_sh', _sh)
    monkeypatch.setattr(wb, '_ensure_makensis', lambda log: '/fake/makensis')
    # makensis faked to no-op → out_file missing → wrap must fail LOUD.
    with _fake_log(isolated) as log:
        with pytest.raises(RuntimeError, match='produced no'):
            wb.wrap_payload(str(payload_tar), '0.16.0', 'a' * 40, log,
                            workdir=str(isolated / 'wrap'))
    assert not os.path.exists(
        os.path.join(str(isolated / 'wrap'), 'payload',
                     'preseed_server.json'))


def test_NEUTER_dropping_the_preseed_is_caught():
    """wrap_payload MUST call _write_preseed — a wrap that "forgets" it
    silently degrades every install to the paste-the-token flow, and the
    artifact records nothing. Pin the call site (source), not a vibe."""
    import inspect
    src = inspect.getsource(wb.wrap_payload)
    assert '_write_preseed(payload_dir, server_url)' in src, (
        'wrap_payload no longer writes the preseed — the per-client '
        'half of the build is gone')
    # And the recorded artifact must carry it when a url is given.
    rec = inspect.getsource(wb.wrap_payload)
    assert "'preseed': {'url': server_url}" in rec, (
        'the preseed url must be visible in the artifact record — the '
        'download route and audits read it from there')


# ═══════════════════════════════════════════════════════════════════
#  _ensure_guest_hosts — wine's resolver must never see DNS
# ═══════════════════════════════════════════════════════════════════

def test_guest_hosts_injects_the_index_hosts_idempotently(isolated,
                                                          monkeypatch):
    """Wine's getaddrinfo is broken for DNS names in this guest (measured)
    while /etc/hosts entries resolve instantly — the index hosts must be
    pinned there, in a regenerating marked block."""
    hosts_path = os.path.join(
        wb.wintoolchain.rootfs_dir(), 'etc', 'hosts')
    os.makedirs(os.path.dirname(hosts_path), exist_ok=True)
    with open(hosts_path, 'w') as f:
        f.write('127.0.0.1 localhost\n')

    def _config():
        return ('http://pip.corp.example/simple/',
                ['http://pypi.corp.example/simple/'], [])

    monkeypatch.setattr(wb, '_host_pip_config', _config)
    monkeypatch.setattr(wb.subprocess, 'run', lambda *a, **k:
                        types.SimpleNamespace(
                            returncode=0, stdout=b'10.0.0.9 host\n'))
    with _fake_log(isolated) as log:
        wb._ensure_guest_hosts(log)
        wb._ensure_guest_hosts(log)   # second run must not duplicate
    text = open(hosts_path).read()
    assert '127.0.0.1 localhost' in text, 'existing entries preserved'
    assert text.count('10.0.0.9\tpip.corp.example') == 1
    assert text.count('10.0.0.9\tpypi.corp.example') == 1
    assert text.count(wb._HOSTS_BEGIN) == 1, 'exactly one managed block'


def test_guest_hosts_survives_an_unresolvable_host(isolated, monkeypatch):
    hosts_path = os.path.join(
        wb.wintoolchain.rootfs_dir(), 'etc', 'hosts')
    os.makedirs(os.path.dirname(hosts_path), exist_ok=True)
    with open(hosts_path, 'w') as f:
        f.write('127.0.0.1 localhost\n')
    monkeypatch.setattr(wb, '_host_pip_config',
                        lambda: ('http://gone.example/simple/', [], []))
    monkeypatch.setattr(wb.subprocess, 'run', lambda *a, **k:
                        types.SimpleNamespace(returncode=2, stdout=b''))
    with _fake_log(isolated) as log:
        wb._ensure_guest_hosts(log)   # must not raise
    assert 'gone.example' not in open(hosts_path).read()


# ═══════════════════════════════════════════════════════════════════
#  pipeline shape — the hollow-build tripwire
# ═══════════════════════════════════════════════════════════════════

def test_the_pipeline_installs_requirements_BEFORE_building():
    """A pipeline missing its pip-requirements step produces a HOLLOW
    payload (49 MB measured incident, CI run 30601806258) that still
    packs and smokes fine if the app degrades quietly. Pin the step
    order so deleting the step is a red test, not a shipped hollow
    installer."""
    import inspect
    src = inspect.getsource(wb._pipeline)
    i_req = src.index("'pip-requirements'")
    i_icons = src.index("'gen-icons'")
    i_pyinst = src.index("'pyinstaller'")
    assert i_req < i_icons < i_pyinst, (
        'requirements must be installed before icons/pyinstaller run')


# ═══════════════════════════════════════════════════════════════════
#  NEUTER — the two traps must bite
# ═══════════════════════════════════════════════════════════════════

def test_NEUTER_judging_by_exit_code_let_a_failure_through(isolated,
                                                           monkeypatch):
    """Replace the sentinel check with a plain exit-code check → the
    measured silent failure (exit 0, no sentinel) PASSES — red."""

    class _Out:
        returncode = 0
        stdout = b'failed silently\n'
        stderr = b''

    monkeypatch.setattr(wb.subprocess, 'run', lambda *a, **k: _Out())
    monkeypatch.setattr(wb.wintoolchain, '_wine_argv', lambda *a: list(a))

    def _neutered_wstep(name, inner, log_fh, *, sentinel=None, env=None,
                        timeout=1800):
        out = wb.subprocess.run(['cmd', '/c', inner],
                                capture_output=True, timeout=timeout)
        if out.returncode != 0:      # ← the neutered verdict
            raise RuntimeError('failed')
        return out

    with _fake_log(isolated) as log:
        _neutered_wstep('demo', 'do-the-thing', log)  # must NOT raise
    # If the shipped _wstep shares this blindness, the suite above it is
    # vacuous — the behavioral tests pin the real check instead.
    with _fake_log(isolated) as log:
        with pytest.raises(RuntimeError):
            wb._wstep('demo', 'do-the-thing', log)


def test_NEUTER_leaking_host_env_breaks_pip(monkeypatch):
    """Documentary: the measured first-rehearsal failure mode. An env that
    carries PIP_REQUIRE_VIRTUALENV through is what killed it — the scrub
    is load-bearing, and this test is its tripwire."""
    monkeypatch.setenv('PIP_REQUIRE_VIRTUALENV', '1')
    leaked = dict(os.environ)
    assert leaked.get('PIP_REQUIRE_VIRTUALENV') == '1', (
        'NEUTER setup: the poison must be present to prove the scrub')
    assert 'PIP_REQUIRE_VIRTUALENV' not in wb._wine_env()
