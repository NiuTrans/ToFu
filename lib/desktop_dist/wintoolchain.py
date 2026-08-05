"""lib/desktop_dist/wintoolchain.py — the userspace Wine toolchain.

Provisions and runs a Windows-build-capable Wine on THIS host: no root,
old glibc, and a corporate seccomp profile. Everything here is the
PROBE-PROVEN recipe (2026-08-01, design: docs/DESKTOP_CLIENT_BUILD_DESIGN.md,
memory: userspace-wine-toolchain-recipe) — each stage answers a measured
trap, and the tests pin them so a future "simplification" reintroduces the
trap loudly rather than silently.

The four measured traps (do not re-learn):

1.  The container's seccomp profile kills the legacy ``access(2)`` syscall
    with ENOSYS (host ``Seccomp: 2``). dash's ``[ -r ]`` therefore always
    misfires, and noble ``apt-key --readonly``'s
    ``create_new_keyring()`` then rewrote the forced keyring to
    ``/dev/null`` → apt died ``NO_PUBKEY`` on keys that ARE in the keyring
    (gpgv verifies fine directly). → ``_patch_apt_key``.
2.  ``proot -R``'s "recommended bindings" include the HOST ``/etc/group``
    (a 9268-line corporate file with NO ``staff`` group), so
    ``fontconfig-config.postinst``'s ``chown root:staff`` failed and the
    whole wine dependency tree cascaded unconfigured. → bare ``-r`` +
    explicit binds only (``_PROOT_BINDS``).
3.  proot does NOT translate ``faccessat2`` paths (its syscall table
    predates the syscall): a guest path hits the host kernel verbatim.
    Wine's loader checks its own directory exactly that way and
    ``ntdll.so`` issues raw syscalls, so LD_PRELOAD cannot interpose.
    → the **host-mirror-path trick**: the wine tree lives at an
    INDEPENDENT host path (``wine_dir()``), bound at the IDENTICAL guest
    path, and wine is exec'd by that absolute path — untranslated
    syscalls then hit a real file (``_wine_argv``).
4.  The container's seccomp SIGSYS-kills ``wine-preloader`` (both
    bitnesses) — silent exit 255 with zero wine diagnostics. → the
    preloaders are renamed away (``_strip_preloaders``); the loader falls
    back to direct exec, measured working for 64-bit Windows apps
    (``wineboot --init`` exit 0, Windows ``Python 3.12.10`` OK).

Layout
------
  ``<data_root>/desktop_toolchain/cache/``  downloaded tarballs (FUSE —
                                             persistent, re-used across
                                             reprovisions)
  ``/tmp/tofu_win_tc/``                     rootfs/ + wine-k/ + wineprefix/
                                             (LOCAL disk — volatile by
                                             design, a cache not state;
                                             TOFU_WIN_TC_DIR overrides)
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
import time

from lib.log import get_logger
from lib.runtime_paths import data_root

from . import store

logger = get_logger(__name__)

# ── Pinned inputs (sha256 measured on the first provision, 2026-08-01) ──
_PROOT_URL = 'https://proot.gitlab.io/proot/bin/proot'
_PROOT_SHA256 = ('b7f2adf5a225000a164f4905aabefeeb'
                 'e11c4c1d5bedff5e1fe8866c48dd70d2')
_UBUNTU_BASE_URL = ('https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/'
                    'release/ubuntu-base-24.04.3-base-amd64.tar.gz')
_UBUNTU_BASE_SHA256 = ('6bc2cde3930ad088b3bb46fa45279e96'
                       'd25bc3810f209850ecbe4722711874f9')
_WINE_URL = ('https://github.com/Kron4ek/Wine-Builds/releases/download/'
             '11.14/wine-11.14-amd64.tar.xz')
_WINE_SHA256 = ('0c1082b9c1b2ab0a85645ed3c83191dd'
                '60b557b28169733546d81fe599ad88b3')
_WINE_VERSION = '11.14'

# The apt packages wine needs at runtime (freetype/X11/glib/sound). The
# Kron4ek tree itself is self-contained; these are the guest-side libs it
# dynamically loads. Curated minimal — the probe's full ``wine64`` apt
# install proved the CLASS but is ~1.3 GB of packages we never call.
_APT_PACKAGES = (
    'libfreetype6', 'libpng16-16t64', 'libx11-6', 'libxext6', 'libxcb1',
    'libxrandr2', 'libxi6', 'libxcursor1', 'libxinerama1', 'libgl1',
    'libglib2.0-0t64', 'libasound2t64', 'libfontconfig1', 'unzip',
)

# noble apt-key's --readonly redefinition — the anchor and its replacement
# (trap 1). The patched function must NEVER consult `[ -r ]`.
_APT_KEY_ANCHOR = (
    "create_new_keyring() { if [ ! -r \"$FORCED_KEYRING\" ]; then "
    "TRUSTEDFILE='/dev/null'; FORCED_KEYRING=\"$TRUSTEDFILE\"; fi; }")
_APT_KEY_PATCHED = (
    "create_new_keyring() { true; }  # TOFU-TOOLCHAIN-PATCH: container "
    "seccomp blocks access(2) (ENOSYS); dash [ -r ] misfires -> forced "
    "keyring rewritten to /dev/null -> apt NO_PUBKEY")
_APT_KEY_MARKER = 'TOFU-TOOLCHAIN-PATCH'

# Guest invocation shapes (traps 2 & 3). Bare -r, explicit binds, and the
# wine tree bound at its IDENTICAL host path — never -R, never a wine path
# inside the rootfs.
_PROOT_BINDS = ('/dev', '/proc', '/sys')

# The guest-visible wineprefix; both the translated side
# (rootfs/tmp/…) and the untranslated side (host /tmp/…) must exist —
# proot's untranslated faccessat2 checks hit the host path verbatim.
_GUEST_WINEPREFIX = '/tmp/tofu_win_tc_wineprefix'

_provision_lock = threading.Lock()


# ── Layout ────────────────────────────────────────────────────────────

def cache_dir() -> str:
    """Download cache (FUSE, persistent). Created on demand."""
    d = os.path.join(data_root(), 'desktop_toolchain', 'cache')
    os.makedirs(d, exist_ok=True)
    return d


def local_root() -> str:
    """Volatile local-disk toolchain root (env-overridable for tests)."""
    return os.environ.get('TOFU_WIN_TC_DIR', '/tmp/tofu_win_tc')


def rootfs_dir() -> str:
    return os.path.join(local_root(), 'rootfs')


def wine_dir() -> str:
    """The wine tree's HOST path — bound at the identical guest path.

    INDEPENDENT of the rootfs path on purpose: bound under the rootfs,
    proot's -r root binding wins the /proc/self/exe translation and the
    guest sees the rootfs path again, defeating the mirror trick
    (measured). Outside it, the mirrored bind is the only translation.
    """
    return os.path.join(local_root(), 'wine-k')


def _guest_wineprefix_host_sides() -> tuple[str, str]:
    """(translated side inside rootfs, untranslated side on the host)."""
    return (os.path.join(rootfs_dir(),
                         _GUEST_WINEPREFIX.lstrip('/').replace('/', os.sep)),
            _GUEST_WINEPREFIX)


# ── State ─────────────────────────────────────────────────────────────

def state() -> dict:
    w = store.load_manifest().get('wintc')
    return w if isinstance(w, dict) else {'state': 'idle'}


def _set_state(**kw) -> None:
    m = store.load_manifest()
    w = m.get('wintc') if isinstance(m.get('wintc'), dict) else {}
    w.update(kw)
    m['wintc'] = w
    store.save_manifest(m)


def is_provisioned() -> bool:
    """Artifact-level check — survives restarts and /tmp wipes alike."""
    return (os.access(os.path.join(cache_dir(), 'proot'), os.X_OK)
            and os.path.isfile(
                os.path.join(rootfs_dir(), 'usr', 'bin', 'sh'))
            and _apt_key_patched()
            and os.path.isfile(os.path.join(wine_dir(), 'bin', 'wine'))
            and not _preloaders_present())


# ── Provisioning ──────────────────────────────────────────────────────

def provision(force: bool = False) -> dict:
    """Idempotent toolchain provisioning. Returns the persisted state.

    Single-flight; each stage checks its own artifact before doing work,
    so a killed provision re-runs cleanly and /tmp volatility is just a
    slower next call (downloads come from the FUSE cache).
    """
    with _provision_lock:
        if not force and is_provisioned():
            w = state()
            if w.get('state') == 'ok':
                return w
        t0 = time.time()
        _set_state(state='provisioning', wine_version=_WINE_VERSION,
                   started_at=t0, error=None)
        try:
            _ensure_proot()
            _ensure_rootfs()
            _patch_apt_key()
            _ensure_guest_libs()
            _ensure_wine_tree()
            _ensure_wineprefix()
            smoke()
        except Exception as e:
            logger.error('[WinTC] provision failed: %s', e, exc_info=True)
            _set_state(state='error', error=str(e),
                       finished_at=time.time())
            raise
        _set_state(state='ok', finished_at=time.time(), error=None)
        logger.info('[WinTC] provisioned in %.0fs (wine %s)',
                    time.time() - t0, _WINE_VERSION)
        return state()


def _download(url: str, dest: str, sha256: str) -> None:
    """Fetch ``url`` to ``dest`` (.part + atomic rename) and pin-verify."""
    import requests
    part = dest + '.part'
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(part, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    h = hashlib.sha256()
    with open(part, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    if h.hexdigest() != sha256:
        os.remove(part)
        raise RuntimeError(
            f'sha256 mismatch for {url}: got {h.hexdigest()[:16]}…, '
            f'pinned {sha256[:16]}…')
    os.replace(part, dest)


def _run(argv: list[str], *, timeout: int = 1800,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """One provisioning step; logs loudly, raises on failure."""
    logger.info('[WinTC] step: %s', ' '.join(argv[:3]) + (' …'
                if len(argv) > 3 else ''))
    out = subprocess.run(argv, capture_output=True, timeout=timeout,
                         env=env)
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or b'')[-400:]
        raise RuntimeError(
            f'step failed (rc={out.returncode}): {argv[0]} … '
            f'{tail.decode("utf-8", "replace")}')
    return out


def _ensure_proot() -> None:
    dest = os.path.join(cache_dir(), 'proot')
    if not os.access(dest, os.X_OK):
        _download(_PROOT_URL, dest, _PROOT_SHA256)
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR)


def _ensure_rootfs() -> None:
    rootfs = rootfs_dir()
    if os.path.isfile(os.path.join(rootfs, 'usr', 'bin', 'sh')):
        return
    tarball = os.path.join(cache_dir(), 'ubuntu-base.tar.gz')
    if not os.path.isfile(tarball):
        _download(_UBUNTU_BASE_URL, tarball, _UBUNTU_BASE_SHA256)
    os.makedirs(rootfs, exist_ok=True)
    _run(['tar', '-xzf', tarball, '-C', rootfs])


def _apt_key_patched() -> bool:
    path = os.path.join(rootfs_dir(), 'usr', 'bin', 'apt-key')
    try:
        with open(path, encoding='utf-8') as f:
            return _APT_KEY_MARKER in f.read()
    except OSError as e:
        logger.debug('[WinTC] apt-key unreadable (%s) — treating as unpatched', e)
        return False


def _patch_apt_key(path: str | None = None) -> None:
    """Neutralize noble apt-key's `[ ! -r ]` fallback (trap 1).

    Idempotent; fails LOUD on anchor drift (a different apt-key version
    needs a human look, not a silent skip into NO_PUBKEY).
    """
    path = path or os.path.join(rootfs_dir(), 'usr', 'bin', 'apt-key')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    if _APT_KEY_MARKER in src:
        return
    if _APT_KEY_ANCHOR not in src:
        raise RuntimeError(
            'apt-key anchor missing (version drift) — the seccomp '
            'workaround needs re-pointing, not skipping')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src.replace(_APT_KEY_ANCHOR, _APT_KEY_PATCHED))
    logger.info('[WinTC] guest apt-key patched (seccomp access(2) trap)')


def _guest_proot_argv(fake_root: bool) -> list[str]:
    """Base proot argv for GUEST commands (trap 2: -r, never -R)."""
    argv = [os.path.join(cache_dir(), 'proot')]
    if fake_root:
        argv.append('-0')
    argv += ['-r', rootfs_dir()]
    for b in _PROOT_BINDS:
        argv += ['-b', b]
    argv += ['-b', '/etc/resolv.conf']
    return argv


def _ensure_guest_libs() -> None:
    sentinel = os.path.join(local_root(), '.apt-libs-ok')
    if os.path.isfile(sentinel):
        return
    base = _guest_proot_argv(fake_root=True)
    _run(base + ['/usr/bin/apt-get', 'update'], timeout=900)
    _run(base + ['/usr/bin/apt-get', 'install', '-y',
                 '--no-install-recommends', *_APT_PACKAGES], timeout=2400)
    with open(sentinel, 'w') as f:
        f.write(str(time.time()))


def _ensure_wine_tree() -> None:
    dest = wine_dir()
    if os.path.isfile(os.path.join(dest, 'bin', 'wine')):
        _strip_preloaders(dest)
        return
    tarball = os.path.join(cache_dir(), 'wine-kron4ek.tar.xz')
    if not os.path.isfile(tarball):
        _download(_WINE_URL, tarball, _WINE_SHA256)
    os.makedirs(dest, exist_ok=True)
    _run(['tar', '-xJf', tarball, '-C', dest, '--strip-components=1'])
    _strip_preloaders(dest)


def _preloaders_present(wine_tree: str | None = None) -> bool:
    tree = wine_tree or wine_dir()
    for arch in ('i386-unix', 'x86_64-unix'):
        if os.path.isfile(os.path.join(tree, 'lib', 'wine', arch,
                                       'wine-preloader')):
            return True
    return False


def _strip_preloaders(wine_tree: str | None = None) -> None:
    """Rename wine-preloader away (trap 4: seccomp SIGSYS)."""
    tree = wine_tree or wine_dir()
    for arch in ('i386-unix', 'x86_64-unix'):
        p = os.path.join(tree, 'lib', 'wine', arch, 'wine-preloader')
        if os.path.isfile(p):
            os.rename(p, p + '.bak')
            logger.info('[WinTC] preloader disabled: %s', p)


def _ensure_wineprefix() -> None:
    for d in _guest_wineprefix_host_sides():
        os.makedirs(d, exist_ok=True)


# ── Running wine ──────────────────────────────────────────────────────

def _wine_argv(*args: str) -> list[str]:
    """The probe-proven wine invocation (traps 2 & 3 baked in).

    Bare ``-r`` + explicit binds; the wine tree bound at its IDENTICAL
    host path and exec'd by that absolute path — the only arrangement
    where the loader's untranslated ``faccessat2`` hits a real file.
    """
    tree = wine_dir()
    argv = [os.path.join(cache_dir(), 'proot'),
            '-r', rootfs_dir()]
    for b in _PROOT_BINDS:
        argv += ['-b', b]
    argv += ['-b', f'{tree}:{tree}',
             'env', f'WINEPREFIX={_GUEST_WINEPREFIX}',
             os.path.join(tree, 'bin', 'wine')]
    argv += list(args)
    return argv


def wine(*args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run ``wine <args>`` in the guest; raise on non-zero."""
    return _run(_wine_argv(*args), timeout=timeout)


def smoke() -> None:
    """The provision verdict: wine must PRINT its version."""
    out = wine('--version', timeout=300)
    text = (out.stdout or b'').decode('utf-8', 'replace')
    if 'wine-' not in text:
        raise RuntimeError(f'wine smoke failed — no version in: {text!r}')
    logger.info('[WinTC] smoke ok: %s', text.strip().splitlines()[-1])


def guest_z(host_path: str) -> str:
    """Map a host path INSIDE the rootfs to its wine ``Z:\\`` path.

    Wine sees the guest root as Z:, so a build tree at
    ``rootfs/tmp/work`` is ``Z:\\tmp\\work`` to Windows processes.
    Anything outside the rootfs is unreachable to wine — a loud error,
    never a silently wrong path (S2's builds must place work trees here).
    """
    rootfs = os.path.abspath(rootfs_dir())
    ap = os.path.abspath(host_path)
    if not (ap == rootfs or ap.startswith(rootfs + os.sep)):
        raise ValueError(
            f'{host_path} is outside the guest rootfs ({rootfs}) — wine '
            'cannot reach it')
    rel = os.path.relpath(ap, rootfs)
    return 'Z:' if rel == '.' else 'Z:\\' + rel.replace(os.sep, '\\')


__all__ = ['provision', 'state', 'is_provisioned', 'wine', 'smoke',
           'guest_z', 'cache_dir', 'local_root', 'rootfs_dir', 'wine_dir']
