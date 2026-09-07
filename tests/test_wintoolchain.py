"""tests/test_wintoolchain.py — the userspace Wine toolchain recipe guards.

WHAT THIS GUARDS
----------------
The toolchain recipe in lib/desktop_dist/wintoolchain.py was measured the
hard way (four probe rounds on 2026-08-01, each defeat disguised as a
different disease — see the module docstring). Every stage of
``provision()`` answers ONE measured trap, and this suite pins them so a
future cleanup that "simplifies" the recipe reintroduces the trap as a
RED test, not as a silent broken build:

1.  apt-key patch (seccomp kills access(2) → apt NO_PUBKEY chain):
    real-file patch application, idempotency, loud failure on anchor
    drift.
2.  ``proot -r``, never ``-R`` (host /etc/group bind has no 'staff'):
    the guest argv and the wine argv are both scanned for the poison
    form.
3.  The host-mirror-path trick (proot doesn't translate faccessat2):
    the wine argv MUST bind the wine tree at its identical host path and
    exec by that absolute path.
4.  Preloader strip (seccomp SIGSYS): renamed, not deleted (forensics),
    and is_provisioned() refuses a tree that still has them.

Plus the provision orchestration itself: stage ordering, artifact-based
skip (a second provision downloads nothing and runs no guest command),
and the manifest state record. The heavy seams (_download, _run) are
faked; the skip/patch/argv logic under test is the REAL code.

Run:  pytest tests/test_wintoolchain.py -q
"""

from __future__ import annotations

import os
import stat

import pytest

from lib.desktop_dist import wintoolchain as tc

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  Fixtures / fakes
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def tcdir(tmp_path, monkeypatch):
    """Isolated local toolchain root + cache (no real /tmp, no real FUSE)."""
    local = tmp_path / 'local'
    cache = tmp_path / 'cache'
    monkeypatch.setenv('TOFU_WIN_TC_DIR', str(local))
    # Isolate the artifact store too — provision() records state into the
    # manifest; tests must never write the production one.
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path / 'store'))
    monkeypatch.setattr(tc, 'cache_dir',
                        lambda: (os.makedirs(cache, exist_ok=True),
                                 str(cache))[1])
    return {'local': str(local), 'cache': str(cache),
            'rootfs': str(local / 'rootfs'), 'wine': str(local / 'wine-k')}


@pytest.fixture()
def fake_heavy(monkeypatch):
    """Fake the two heavy seams (network + subprocess) with recorders."""
    state = {'downloads': [], 'runs': []}

    def _dl(url, dest, sha256):
        state['downloads'].append(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(b'fake-bytes')

    class _Out:
        returncode = 0
        stdout = b''
        stderr = b''

    def _run(argv, *, timeout=1800, env=None):
        state['runs'].append(list(argv))
        return _Out()

    monkeypatch.setattr(tc, '_download', _dl)
    monkeypatch.setattr(tc, '_run', _run)
    return state


def _seed_rootfs(rootfs, *, with_apt_key=True):
    """A minimal fake guest rootfs: sh + the REAL noble apt-key anchor."""
    os.makedirs(os.path.join(rootfs, 'usr', 'bin'), exist_ok=True)
    open(os.path.join(rootfs, 'usr', 'bin', 'sh'), 'w').write('x')
    if with_apt_key:
        with open(os.path.join(rootfs, 'usr', 'bin', 'apt-key'), 'w') as f:
            f.write('#!/bin/sh\n\t' + tc._APT_KEY_ANCHOR + '\n')


def _seed_wine_tree(wine, *, preloaders=True):
    for arch in ('i386-unix', 'x86_64-unix'):
        d = os.path.join(wine, 'lib', 'wine', arch)
        os.makedirs(d, exist_ok=True)
        if preloaders:
            open(os.path.join(d, 'wine-preloader'), 'w').write('x')
    os.makedirs(os.path.join(wine, 'bin'), exist_ok=True)
    open(os.path.join(wine, 'bin', 'wine'), 'w').write('x')


# ═══════════════════════════════════════════════════════════════════
#  trap 1 — apt-key patch
# ═══════════════════════════════════════════════════════════════════

def test_apt_key_patch_applies_and_is_idempotent(tcdir):
    _seed_rootfs(tcdir['rootfs'])
    path = os.path.join(tcdir['rootfs'], 'usr', 'bin', 'apt-key')
    tc._patch_apt_key(path)
    text = open(path).read()
    assert tc._APT_KEY_MARKER in text
    assert tc._APT_KEY_ANCHOR not in text
    # Second call is a no-op (idempotent across reprovisions).
    tc._patch_apt_key(path)
    assert open(path).read() == text


def test_apt_key_patch_fails_loud_on_anchor_drift(tcdir):
    """A new apt-key version must stop the line, not slide into NO_PUBKEY."""
    _seed_rootfs(tcdir['rootfs'], with_apt_key=False)
    path = os.path.join(tcdir['rootfs'], 'usr', 'bin', 'apt-key')
    with open(path, 'w') as f:
        f.write('#!/bin/sh\n# totally different apt-key\n')
    with pytest.raises(RuntimeError, match='anchor'):
        tc._patch_apt_key(path)


# ═══════════════════════════════════════════════════════════════════
#  trap 2 — proot invocation shape: bare -r, never -R
# ═══════════════════════════════════════════════════════════════════

def test_guest_argv_uses_bare_r_and_explicit_binds(tcdir):
    argv = tc._guest_proot_argv(fake_root=True)
    assert '-0' in argv, 'guest package steps need fake root for dpkg'
    r_count = argv.count('-r')
    assert r_count == 1, argv
    assert '-R' not in argv, (
        'proot -R binds the HOST /etc/group (no staff) — the chown '
        'postinst trap, measured 2026-08-01')
    for b in ('/dev', '/proc', '/sys', '/etc/resolv.conf'):
        assert b in argv, f'missing bind {b}'


def test_wine_argv_has_the_mirror_bind_and_no_dash_R(tcdir):
    """Trap 3: the wine tree must be bound at its IDENTICAL host path."""
    argv = tc._wine_argv('--version')
    tree = tc.wine_dir()
    assert '-R' not in argv
    assert f'{tree}:{tree}' in argv, (
        'the mirror bind is the whole fix for the untranslated '
        'faccessat2 — without it the loader ENOENTs on its own dir')
    # Exec by the mirrored ABSOLUTE path, not a rootfs path.
    wine_bin = os.path.join(tree, 'bin', 'wine')
    assert wine_bin in argv
    assert not any(a == os.path.join(tc.rootfs_dir(), 'opt', 'wine')
                 for a in argv), 'wine must not run from inside the rootfs'
    # The prefix is pinned to the both-sides-existing guest path.
    assert any(a.startswith('WINEPREFIX=') for a in argv), argv


# ═══════════════════════════════════════════════════════════════════
#  trap 4 — preloader strip
# ═══════════════════════════════════════════════════════════════════

def test_preloaders_are_renamed_not_deleted(tcdir):
    _seed_wine_tree(tcdir['wine'])
    assert tc._preloaders_present()
    tc._strip_preloaders()
    assert not tc._preloaders_present()
    for arch in ('i386-unix', 'x86_64-unix'):
        bak = os.path.join(tcdir['wine'], 'lib', 'wine', arch,
                           'wine-preloader.bak')
        assert os.path.isfile(bak), (
            'renamed, not deleted — the .bak is the forensic record')


def test_is_provisioned_refuses_a_tree_with_preloaders(tcdir):
    """A tree that still has preloaders is the SIGSYS trap — not ready."""
    _seed_rootfs(tcdir['rootfs'])
    tc._patch_apt_key(os.path.join(tcdir['rootfs'], 'usr', 'bin', 'apt-key'))
    proot = os.path.join(tcdir['cache'], 'proot')
    os.makedirs(tcdir['cache'], exist_ok=True)
    with open(proot, 'w') as f:
        f.write('x')
    os.chmod(proot, os.stat(proot).st_mode | stat.S_IXUSR)
    _seed_wine_tree(tcdir['wine'], preloaders=True)
    assert not tc.is_provisioned()
    tc._strip_preloaders()
    assert tc.is_provisioned()


# ═══════════════════════════════════════════════════════════════════
#  provision orchestration
# ═══════════════════════════════════════════════════════════════════

def _full_provision(tcdir, fake_heavy, monkeypatch):
    """Drive provision() with the smoke faked (no real wine to run)."""
    monkeypatch.setattr(tc, 'smoke', lambda: None)
    # Pre-seed what the real stages would produce so skip-checks see
    # artifacts after the faked _run/_download "happen".
    orig_run = fake_heavy['runs']

    class _Out:
        returncode = 0
        stdout = b''
        stderr = b''

    def _producing_run(argv, *, timeout=1800, env=None):
        orig_run.append(list(argv))
        # The tar steps must produce their artifacts for later stages.
        if argv[0] == 'tar' and '-xzf' in argv:
            _seed_rootfs(tcdir['rootfs'])
        if argv[0] == 'tar' and '-xJf' in argv:
            _seed_wine_tree(tcdir['wine'])
        return _Out()

    monkeypatch.setattr(tc, '_run', _producing_run)
    return tc.provision(force=True)


def test_provision_runs_every_stage_and_records_state(tcdir, fake_heavy,
                                                      monkeypatch):
    st = _full_provision(tcdir, fake_heavy, monkeypatch)
    assert st['state'] == 'ok', st
    assert st['wine_version'] == tc._WINE_VERSION
    # All three downloads happened (proot, ubuntu-base, wine).
    assert len(fake_heavy['downloads']) == 3
    # apt update + install ran as guest commands with fake root.
    apt_runs = [r for r in fake_heavy['runs']
                if any('apt-get' in a for a in r)]
    assert len(apt_runs) == 2, fake_heavy['runs']
    assert all('-0' in r and '-R' not in r for r in apt_runs)
    # apt-key got patched as part of provisioning (trap 1 wired in).
    assert tc._apt_key_patched()
    # Preloaders are gone from the provisioned tree (trap 4 wired in).
    assert not tc._preloaders_present()


def test_second_provision_downloads_and_runs_nothing(tcdir, fake_heavy,
                                                     monkeypatch):
    _full_provision(tcdir, fake_heavy, monkeypatch)
    dl_before = len(fake_heavy['downloads'])
    runs_before = len(fake_heavy['runs'])
    monkeypatch.setattr(tc, 'smoke', lambda: None)
    st = tc.provision()
    assert st['state'] == 'ok'
    assert len(fake_heavy['downloads']) == dl_before, (
        'a second provision re-downloaded — the artifact skip is broken')
    assert len(fake_heavy['runs']) == runs_before, (
        'a second provision re-ran guest steps — not idempotent')


def test_provision_failure_records_error_state(tcdir, fake_heavy,
                                               monkeypatch):
    def _boom(argv, *, timeout=1800, env=None):
        raise RuntimeError('network is a lie')

    monkeypatch.setattr(tc, '_run', _boom)
    monkeypatch.setattr(tc, 'smoke', lambda: None)
    with pytest.raises(RuntimeError, match='network is a lie'):
        tc.provision(force=True)
    st = tc.state()
    assert st['state'] == 'error'
    assert 'network is a lie' in st['error']


# ═══════════════════════════════════════════════════════════════════
#  guest_z
# ═══════════════════════════════════════════════════════════════════

def test_guest_z_maps_rootfs_paths_and_refuses_outsiders(tcdir):
    inside = os.path.join(tcdir['rootfs'], 'tmp', 'work')
    os.makedirs(inside)
    assert tc.guest_z(inside) == 'Z:\\tmp\\work'
    assert tc.guest_z(tcdir['rootfs']) == 'Z:'
    with pytest.raises(ValueError, match='outside'):
        tc.guest_z('/etc/hostname')


# ═══════════════════════════════════════════════════════════════════
#  NEUTER — the traps must bite when the recipe is broken
# ═══════════════════════════════════════════════════════════════════

def test_NEUTER_dropping_the_mirror_bind_is_caught(tcdir):
    """Strip the mirror bind from the shipped source → argv test goes red."""
    import inspect
    src = inspect.getsource(tc._wine_argv)
    neutered = src.replace("argv += ['-b', f'{tree}:{tree}',",
                           "argv += [")
    assert neutered != src, 'NEUTER anchor drifted'
    # Execute the neutered function body and re-run the assertion.
    ns = {'os': os, 'cache_dir': tc.cache_dir, 'rootfs_dir': tc.rootfs_dir,
          'wine_dir': tc.wine_dir, '_GUEST_WINEPREFIX': tc._GUEST_WINEPREFIX,
          '_PROOT_BINDS': tc._PROOT_BINDS}
    exec(neutered.replace('def _wine_argv', 'def _neutered_wine_argv'), ns)
    argv = ns['_neutered_wine_argv']('--version')
    assert f'{tc.wine_dir()}:{tc.wine_dir()}' not in argv, (
        'NEUTER did not remove the mirror bind')


def test_NEUTER_skipping_the_apt_key_patch_is_caught(tcdir, fake_heavy,
                                                     monkeypatch):
    """provision() without the patch step leaves the guest broken (trap 1)."""
    monkeypatch.setattr(tc, '_patch_apt_key', lambda path=None: None)
    _full_provision(tcdir, fake_heavy, monkeypatch)
    assert not tc._apt_key_patched(), (
        'NEUTER did not skip the patch — the wiring assertion above it '
        'proves nothing')
