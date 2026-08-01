"""lib/desktop_dist/builder.py — build the desktop app ON the server.

The user-facing ask: the "Download Desktop" button should serve an artifact
this server BUILT, tailored to the visitor's platform, instead of depending
on the public GitHub network. The honest constraint (measured, not assumed):
PyInstaller cannot cross-compile, so a Linux server can build only the
Linux artifact natively. Windows/macOS come from the mirror (mirror.py);
this module covers the platform the server CAN truly build — its own.

Why building here is still worth it
-----------------------------------
  * The artifact tracks THIS server's code. A source checkout routinely runs
    ahead of the newest published release (measured: VERSION 0.16.0 vs
    release v0.14.2), so the mirrored installer is older than the server it
    connects to. A locally-built one cannot drift from it.
  * The desktop app's own update check only fires on a NEWER release tag
    (desktop/launcher.py compares versions), so a newer local build is never
    "updated" backwards.

Pipeline (mirrors .github/workflows/build-desktop.yml's Linux leg):
  git archive HEAD        — build the COMMITTED tree, not the shared dirty
                            working tree (a sibling's half-written file must
                            never ship in an installer)
  venv --system-site-packages + pip install pyinstaller
  scripts/gen_desktop_icons.py
  pyinstaller tofu.spec
  TOFU_SMOKE=1 dist/Tofu/Tofu — the same boot smoke the CI gates on
  tar czf → the artifact store, recorded with source='built'

State lives in the manifest under ``build`` so it survives restarts and is
visible to the route layer. Single-flight; the trigger is explicit
(POST /api/v1/desktop/build) or env-gated automatic
(TOFU_DESKTOP_DIST_AUTOBUILD=1 lets the status path kick a build for Linux
visitors when no built artifact exists).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

from lib.log import get_logger, log_context

from . import store

logger = get_logger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

_BUILD_TIMEOUT_S = int(os.environ.get('TOFU_DESKTOP_BUILD_TIMEOUT_S', 3600))

_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def is_running() -> bool:
    t = _worker
    return bool(t and t.is_alive())


def state() -> dict:
    """The persisted build state (idle when nothing ever ran)."""
    b = store.load_manifest().get('build')
    if not isinstance(b, dict):
        return {'state': 'idle'}
    return b


def artifact_name(version: str) -> str:
    """The filename the Linux leg publishes for ``version``.

    Matches the CI's own naming (build-desktop.yml's Create archive step and
    the 'Tofu-*-linux*.tar.gz' glob in scripts/release_assets.py) so a
    server-built artifact is indistinguishable in shape from a mirrored one.
    """
    return f'Tofu-{version}-linux-x86_64.tar.gz'


def start(reason: str = 'manual') -> dict:
    """Kick a background build (single-flight). Returns the current state."""
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return state()
        _worker = threading.Thread(target=_run_safe, args=(reason,),
                                   name='desktop-dist-builder', daemon=True)
        _worker.start()
    return state()


def _run_safe(reason: str) -> None:
    try:
        _run(reason)
    except Exception as e:
        logger.error('[DesktopDist] build crashed: %s', e, exc_info=True)
        _set_state(state='error', error=f'crash: {e}')


def _set_state(**kw) -> None:
    m = store.load_manifest()
    b = m.get('build') if isinstance(m.get('build'), dict) else {}
    b.update(kw)
    m['build'] = b
    store.save_manifest(m)


def _run(reason: str) -> None:
    version = _read_version()
    sha = _git('rev-parse', 'HEAD').strip()
    t0 = time.time()
    _set_state(state='running', version=version, git_sha=sha,
               reason=reason, started_at=t0, finished_at=None, error=None)
    workdir = os.path.join(store._store_dir(), 'build',
                           f'{sha[:12]}-{int(t0)}')
    os.makedirs(workdir, exist_ok=True)
    # The log lives in the store ROOT, not the workdir: the workdir is
    # deleted on success (the venv + PyInstaller cache are several GB), and
    # the log is the only build forensics that must survive.
    log_path = os.path.join(store._store_dir(), 'build.log')
    with open(log_path, 'w', encoding='utf-8') as log_fh, \
            log_context('desktop_dist_build', logger=logger):
        _set_state(log=log_path)
        try:
            dist = _pipeline(workdir, version, sha, log_fh)
            _record_built_artifact(dist, version, sha, log_fh)
        except Exception as e:
            logger.error('[DesktopDist] build failed (log: %s): %s',
                         log_path, e)
            _set_state(state='error', error=str(e),
                       finished_at=time.time())
            return
    # Success. The workdir is NOT kept: the artifact + log are the only
    # products (the venv + PyInstaller cache are several GB).
    shutil.rmtree(workdir, ignore_errors=True)
    _set_state(state='ok', finished_at=time.time(), error=None)
    logger.info('[DesktopDist] build ok in %.0fs (log: %s)',
                time.time() - t0, log_path)


def _pipeline(workdir: str, version: str, sha: str, log_fh) -> str:
    """The heavy half: snapshot → venv → icons → PyInstaller → boot smoke.

    Returns the ``dist`` directory containing ``Tofu/``. Separated from
    :func:`_record_built_artifact` so tests can fake THIS half (minutes of
    CPU) while driving the REAL recording half (tar + manifest), which is
    where user-visible correctness actually lives."""
    src = os.path.join(workdir, 'src')
    os.makedirs(src, exist_ok=True)

    # 1. Clean-tree snapshot of HEAD — never the dirty working tree.
    _sh(f'git -C {_REPO_ROOT} archive HEAD | tar -x -C {src}', log_fh,
        shell=True)

    # 2. Clean venv + the CI's EXACT pip recipe (build-desktop.yml's
    #    "Install dependencies" step, both lines). Measured reason for NOT
    #    using --system-site-packages: the server's own env carries extra
    #    packages (e.g. django) whose PyInstaller hooks then fail the build
    #    (ImportErrorWhenRunningHook on hook-django.db.backends, hit on the
    #    first real run), and PyInstaller would bundle whatever cruft its
    #    analysis reaches into the installer. A clean venv reproduces the
    #    CI inputs bit-for-bit and eliminates that whole drift class.
    #
    #    tofu-search is EXEMPT from the requirements solve — the same split
    #    install.sh makes (its _EXEMPT list): no pip index carries the
    #    requirements floor (measured 2026-08-01: public PyPI tops at 0.5.1,
    #    the internal mirror carries none), so a naive solve dies on it.
    #    install.sh's own order is honoured: sibling checkout → vendor wheel
    #    → index name (works wherever the floor IS published).
    venv = os.path.join(workdir, 'venv')
    vpy = os.path.join(venv, 'bin', 'python')
    _sh(f'{sys.executable} -m venv {venv}', log_fh)
    ts_src = _tofu_search_source()
    _sh(f'{vpy} -m pip install --quiet --no-deps {shlex.quote(ts_src)}',
        log_fh)
    build_reqs = _requirements_without_tofu_search(
        os.path.join(src, 'requirements.txt'), workdir)
    # --extra-index-url pypi.org: requirements.txt itself prescribes this
    # fallback for packages the internal mirror does not carry (measured on
    # build attempt 3: pymupdf_layout==1.27.2.3 — the file's own comment
    # says "install it from pypi.org if your index lacks it"). The
    # configured mirror stays primary; this only fills its gaps.
    _sh(f'{vpy} -m pip install --quiet -r {build_reqs} '
        f'--extra-index-url https://pypi.org/simple '
        f'pyinstaller pystray pillow psycopg2-binary pyautogui pyperclip '
        f'psutil', log_fh, cwd=src)

    # 3. Icons (hard requirement of the build — CI fails fast without them).
    _sh(f'{vpy} scripts/gen_desktop_icons.py', log_fh, cwd=src)

    # 4. PyInstaller (tofu.spec at the snapshot root).
    dist = os.path.join(workdir, 'dist')
    _sh(f'{vpy} -m PyInstaller tofu.spec --distpath {dist} '
        f'--workpath {os.path.join(workdir, "tmp")} --noconfirm',
        log_fh, cwd=src)

    # 5. Boot smoke — the CI's own verdict: exit code + the explicit OK
    #    marker + NO traceback in stderr (see build-desktop.yml for why all
    #    three; an earlier draft of this builder checked only the first two
    #    and let a numpy-crashing child through to tar, measured 2026-08-01).
    #
    #    HERMETIC on purpose: TOFU_SMOKE=1's import is read-only, but the
    #    launcher ALSO spawns a re-exec'd server child that runs the FULL
    #    startup — including _init_database. With this host's env that child
    #    connects to the PRODUCTION PostgreSQL and runs DDL against it (it
    #    deadlock-crashed against the live server, measured). Strip PG_* so
    #    it falls back to a scratch SQLite, and kill any leftover child
    #    before tar reads the tree (a lingering child writing logs/data next
    #    to the exe is what made tar report "file changed as we read it").
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    env['TOFU_SMOKE'] = '1'
    env['TOFU_DB_PATH'] = os.path.join(workdir, 'smoke.db')
    out = _sh(f'{dist}/Tofu/Tofu', log_fh, env=env, check=False)
    _kill_stragglers(dist)
    err = (out.stderr or b'').decode('utf-8', 'replace')
    import re as _re
    if (out.returncode != 0
            or b'TOFU_SMOKE_OK' not in (out.stdout or b'')
            or _re.search(r'Traceback|ModuleNotFoundError|ImportError',
                          err)):
        raise RuntimeError(
            f'boot smoke failed (rc={out.returncode}) — see build.log')
    # The smoke child writes its runtime state (data/, logs/, uploads/)
    # NEXT TO the exe in a frozen layout — that residue must not ship in
    # the artifact (the CI's own tarballs carry it; we are better than CI
    # here deliberately).
    for residue in ('data', 'logs', 'uploads', 'project_sessions'):
        shutil.rmtree(os.path.join(dist, 'Tofu', residue),
                      ignore_errors=True)
    # Tar from a PRIVATE STAGING copy, not the live dist tree. Measured on
    # build 6: even with every straggler reaped, the smoke child's final
    # shutdown writes raced tar's traversal ('tar: Tofu: file changed as we
    # read it') on this shared FUSE box; the identical tar on a quiesced
    # tree passes. A staging copy is the deterministic form of "quiesced".
    staging = os.path.join(workdir, 'staging')
    shutil.copytree(os.path.join(dist, 'Tofu'),
                    os.path.join(staging, 'Tofu'))
    return staging


def _record_built_artifact(dist: str, version: str, sha: str, log_fh) -> None:
    """Tar ``dist/Tofu`` into the store and record it as ``source='built'`."""
    name = artifact_name(version)
    dest_part = os.path.join(store._store_dir(), name + '.part')
    dest = os.path.join(store._store_dir(), name)
    _sh(f'tar czf {dest_part} -C {dist} Tofu', log_fh)
    os.replace(dest_part, dest)
    size = os.path.getsize(dest)
    sha256 = _sha256_file(dest)
    store.record_artifact({
        'os': 'linux', 'arch': 'x86_64', 'label': 'Linux archive',
        'filename': name, 'size': size, 'sha256': sha256,
        'source': 'built', 'version': version, 'fetched_at': time.time(),
        'git_sha': sha,
    })
    logger.info('[DesktopDist] built %s (%d bytes, sha256 %.12s)',
                name, size, sha256)


def _tofu_search_source() -> str:
    """Where this server gets tofu-search from, in install.sh's order.

    A sibling checkout (the deploy reality here — the running env's own
    tofu-search is an editable install of it), then a bundled vendor wheel,
    then the bare package name for hosts whose index DOES carry the floor.
    """
    import glob
    sibling = os.path.join(_REPO_ROOT, '..', 'tofu-search')
    if os.path.isfile(os.path.join(sibling, 'pyproject.toml')):
        return os.path.abspath(sibling)
    wheels = sorted(glob.glob(
        os.path.join(_REPO_ROOT, 'vendor', 'tofu_search-*.whl')))
    if wheels:
        return wheels[-1]
    return 'tofu-search'


def _requirements_without_tofu_search(req_path: str, workdir: str) -> str:
    """A copy of requirements.txt minus the tofu-search line.

    The pin IS the product's requirement — we do not loosen it, we install
    the package separately from a source that satisfies it (see
    _tofu_search_source), exactly like install.sh's _EXEMPT split. Fails
    loud when the line is absent, so a rename never silently un-exempts it.
    """
    out_path = os.path.join(workdir, 'requirements.build.txt')
    # The exemption names ONE package, not the prefix family: after
    # 'tofu-search' must come a version/extras boundary or EOL — a
    # 'tofu-search-extra' lookalike is NOT exempt (measured by the test
    # that caught a naive startswith dropping it).
    import re
    line_re = re.compile(r'^\s*tofu-search(?:\s|\[|<|>|=|!|~|;|$)', re.I)
    kept, dropped = [], []
    with open(req_path, encoding='utf-8') as f:
        for line in f:
            if line.strip().startswith('#'):
                kept.append(line)
            elif line_re.match(line):
                dropped.append(line.rstrip())
            else:
                kept.append(line)
    if not dropped:
        raise RuntimeError(
            f'no tofu-search line found in {req_path} — the exemption list '
            'is out of sync with requirements.txt')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.writelines(kept)
    logger.info('[DesktopDist] requirements filter dropped: %s', dropped)
    return out_path


def _read_version() -> str:
    with open(os.path.join(_REPO_ROOT, 'VERSION'), encoding='utf-8') as f:
        v = f.read().strip()
    if not v:
        raise RuntimeError('VERSION file is empty')
    return v


def _git(*args) -> str:
    out = subprocess.run(['git', '-C', _REPO_ROOT, *args],
                         capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed: {out.stderr[:200]!r}')
    return out.stdout.decode('utf-8', 'replace')


def _sh(cmd: str, log_fh, *, cwd: str | None = None, env: dict | None = None,
        shell: bool = False, check: bool = True):
    """One pipeline step with its output in build.log (never silent)."""
    logger.info('[DesktopDist] build step: %s', cmd)
    out = subprocess.run(
        cmd if shell else shlex.split(cmd),
        cwd=cwd, env=env, shell=shell, capture_output=True,
        timeout=_BUILD_TIMEOUT_S)
    for stream, tag in ((out.stdout, 'out'), (out.stderr, 'err')):
        if stream:
            log_fh.write(f'── {tag} ({cmd[:60]}) ──\n')
            log_fh.write(stream.decode('utf-8', 'replace') + '\n')
    log_fh.flush()
    if check and out.returncode != 0:
        raise RuntimeError(f'step failed (rc={out.returncode}): {cmd}')
    return out


def _kill_stragglers(dist: str) -> None:
    """Kill any smoke child still running from ``dist`` before tar reads it.

    The launcher's re-exec'd server child detaches from the smoke parent —
    subprocess.run only waits for the parent. Anchored to the EXACT binary
    path so it can never hit the production server (different path) or a
    sibling build (different workdir).
    """
    binary = os.path.join(dist, 'Tofu', 'Tofu')
    for _ in range(20):
        alive = subprocess.run(['pgrep', '-f', binary],
                               capture_output=True).returncode == 0
        if not alive:
            return
        subprocess.run(['pkill', '-TERM', '-f', binary], capture_output=True)
        time.sleep(0.5)
    logger.warning('[DesktopDist] smoke straggler survived 10s of TERM — '
                   'escalating to KILL')
    subprocess.run(['pkill', '-KILL', '-f', binary], capture_output=True)
    time.sleep(0.5)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


__all__ = ['start', 'state', 'is_running', 'artifact_name']
