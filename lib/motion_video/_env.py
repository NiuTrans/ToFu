"""lib/motion_video/_env.py — Render-chain environment manager.

Single source of truth for locating (and, when missing, bootstrapping) the
four external binaries the HyperFrames render chain needs:

  * **node/npm**   — HyperFrames is an npm CLI; Node >= 22 required.
  * **hyperframes**— the ``hyperframes`` CLI itself. Resolution order:
    ``TOFU_HYPERFRAMES_BIN`` env → the managed install at
    ``<data>/motion_video/node/node_modules/.bin/hyperframes`` → PATH.
    :func:`ensure_hyperframes` auto-installs a pinned version into the
    managed dir with ``npm install --ignore-scripts`` (the postinstall of
    the transitive ``onnxruntime-node`` dep hits api.nuget.org, which is
    unreachable on some corporate networks; the scripts only matter for
    the transcribe/remove-background features we do not use).
  * **ffmpeg**     — ``TOFU_FFMPEG`` env → ``imageio_ffmpeg``'s bundled
    static binary (pip package, no root needed) → PATH.
  * **ffprobe**    — ``TOFU_FFPROBE`` env → PATH. OPTIONAL: ``probe_video``
    falls back to parsing ``ffmpeg -i`` stderr when absent.
  * **chrome**     — resolved by ``chromium_env.chromium_executable()``
    (the shared single source of truth): ``HYPERFRAMES_BROWSER_PATH``
    override → the Playwright cache (honouring ``PLAYWRIGHT_BROWSERS_PATH``,
    which install.sh exports) → a system browser. Accepts the
    headless-shell build, which is the only one install.sh downloads.

The headless-Chromium shared-library trap (libatk/libgbm missing on
minimal servers) is handled by prepending the running Python's
``sys.prefix/lib`` to ``LD_LIBRARY_PATH`` when that directory actually
carries the GUI libs (the conda-env recipe) — see
:func:`build_render_env`.

Everything is probed lazily and logged; nothing here raises on a missing
piece — :func:`probe_env` reports an ``issues`` list instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'PINNED_HYPERFRAMES',
    'motion_root',
    'node_root',
    'node_bin',
    'npm_bin',
    'hyperframes_bin',
    'ensure_hyperframes',
    'ffmpeg_bin',
    'ensure_ffmpeg',
    'ffprobe_bin',
    'ensure_ffprobe',
    'media_shim_dir',
    'chrome_bin',
    'build_render_env',
    'probe_env',
]

#: HyperFrames version pinned for the managed install (P0-verified).
PINNED_HYPERFRAMES = '0.7.71'

#: Minimum Node major version required by the HyperFrames CLI.
_MIN_NODE_MAJOR = 22


# ── Roots ─────────────────────────────────────────────────

def motion_root() -> str:
    """Writable root for motion-video state (managed node install, workdirs)."""
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'motion_video')
    os.makedirs(path, exist_ok=True)
    return path


def node_root() -> str:
    """Directory holding the managed npm project for the HyperFrames CLI."""
    path = os.path.join(motion_root(), 'node')
    os.makedirs(path, exist_ok=True)
    return path


# ── Binary resolution ─────────────────────────────────────

def node_bin() -> str:
    return shutil.which('node') or ''


def npm_bin() -> str:
    return shutil.which('npm') or ''


def hyperframes_bin() -> str:
    """Locate the hyperframes CLI (env → managed install → PATH)."""
    override = os.environ.get('TOFU_HYPERFRAMES_BIN', '').strip()
    if override and os.path.isfile(override):
        return override
    managed = os.path.join(node_root(), 'node_modules', '.bin', 'hyperframes')
    if os.path.isfile(managed):
        return managed
    return shutil.which('hyperframes') or ''


def ffmpeg_bin() -> str:
    """Locate an ffmpeg binary (env → imageio-ffmpeg static build → PATH)."""
    override = os.environ.get('TOFU_FFMPEG', '').strip()
    if override and os.path.isfile(override):
        return override
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception as e:
        logger.debug('[MotionVideo] imageio_ffmpeg unavailable: %s', e)
    return shutil.which('ffmpeg') or ''


def ffprobe_bin() -> str:
    """Locate ffprobe (env → PATH → shim dir from a previous bootstrap)."""
    override = os.environ.get('TOFU_FFPROBE', '').strip()
    if override and os.path.isfile(override):
        return override
    found = shutil.which('ffprobe')
    if found:
        return found
    shim = os.path.join(media_shim_dir(), 'ffprobe')
    return shim if os.path.isfile(shim) else ''


def chrome_bin() -> str:
    """Locate a Chromium executable for HyperFrames capture.

    Delegates to the shared resolver (operator override → Playwright cache →
    system browsers) so this path can never disagree with the desktop
    installer or the healthcheck about whether a browser is present.

    There is deliberately no local candidate-enumerating helper any more: the
    previous ``_playwright_chrome_candidates`` hard-coded ``~/.cache/
    ms-playwright`` (going blind whenever install.sh pinned a shared cache via
    ``PLAYWRIGHT_BROWSERS_PATH``) and listed only the Linux layouts. Keeping it
    as a delegating wrapper would leave a function that LOOKS authoritative but
    is called by nothing — the next reader would edit it and see no effect.
    """
    from chromium_env import chromium_executable
    return chromium_executable()


def ensure_hyperframes(*, install: bool = True, timeout: int = 900) -> str:
    """Return the hyperframes CLI path, installing the pinned version if absent.

    The managed install lives in ``<data>/motion_video/node`` and deliberately
    uses ``--ignore-scripts`` — the transitive ``onnxruntime-node`` postinstall
    downloads from api.nuget.org (unreachable on some networks) and is only
    needed for the transcribe / remove-background features, not rendering.

    Returns the CLI path ('' when unavailable and ``install`` is False or the
    install failed). Never raises; failures are logged + reported as ''.
    """
    existing = hyperframes_bin()
    if existing:
        return existing
    if not install:
        return ''
    npm = npm_bin()
    if not npm:
        logger.warning('[MotionVideo] cannot install hyperframes — npm not found')
        return ''
    root = node_root()
    try:
        if not os.path.isfile(os.path.join(root, 'package.json')):
            subprocess.run([npm, 'init', '-y'], cwd=root, check=True,
                           capture_output=True, timeout=120)
        logger.info('[MotionVideo] installing hyperframes@%s into %s '
                    '(this is a one-time bootstrap)', PINNED_HYPERFRAMES, root)
        proc = subprocess.run(
            [npm, 'install', f'hyperframes@{PINNED_HYPERFRAMES}',
             '--no-fund', '--no-audit', '--ignore-scripts'],
            cwd=root, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            logger.warning('[MotionVideo] hyperframes install failed rc=%s: %.500s',
                           proc.returncode, proc.stderr)
            return ''
    except subprocess.TimeoutExpired:
        logger.warning('[MotionVideo] hyperframes install timed out after %ss', timeout)
        return ''
    except Exception as e:
        logger.warning('[MotionVideo] hyperframes install error: %s', e, exc_info=True)
        return ''
    path = hyperframes_bin()
    if path:
        logger.info('[MotionVideo] hyperframes installed at %s', path)
    else:
        logger.warning('[MotionVideo] hyperframes install finished but CLI not found in %s',
                       root)
    return path


#: Static ffprobe source (same build family imageio-ffmpeg's ffmpeg comes
#: from). Downloaded once on demand — the pip package ships ffmpeg only.
_FFPROBE_TARBALL_URL = ('https://johnvansickle.com/ffmpeg/releases/'
                        'ffmpeg-release-amd64-static.tar.xz')


def media_shim_dir() -> str:
    """Directory holding ``ffmpeg`` / ``ffprobe`` shims (created on demand).

    The HyperFrames CLI searches PATH for executables literally NAMED
    ``ffmpeg`` / ``ffprobe``, but the resolved binaries carry versioned
    names (``ffmpeg-linux-x86_64-v7.0.2``) — so we symlink them under
    canonical names here and prepend THIS dir to PATH in
    :func:`build_render_env`.
    """
    path = os.path.join(motion_root(), 'bin')
    os.makedirs(path, exist_ok=True)
    return path


def _refresh_shim(name: str, target: str) -> str:
    """(Re)point shim ``name`` at ``target`` inside the shim dir; return path."""
    if not target or not os.path.isfile(target):
        return ''
    if os.path.basename(target) == name:
        return target  # already canonically named — no shim needed
    link = os.path.join(media_shim_dir(), name)
    try:
        if os.path.islink(link) or os.path.exists(link):
            if os.path.realpath(link) == os.path.realpath(target):
                return link
            os.unlink(link)
        os.symlink(target, link)
    except OSError as e:
        logger.warning('[MotionVideo] shim %s → %s failed: %s', name, target, e)
        return ''
    return link


def ensure_ffprobe(*, install: bool = True, timeout: int = 600) -> str:
    """Return an ffprobe path, downloading a static build when absent.

    ``imageio-ffmpeg`` ships ffmpeg only, and the HyperFrames CLI hard-
    requires ffprobe to probe media assets — so we fetch the matching
    static build (johnvansickle, same family as the imageio binary) once
    and extract just the ``ffprobe`` member into the shim dir.
    Never raises; failures are logged + reported as ''.
    """
    existing = ffprobe_bin()
    if existing:
        return existing
    if not install:
        return ''
    dest = os.path.join(media_shim_dir(), 'ffprobe')
    logger.info('[MotionVideo] downloading static ffprobe (one-time bootstrap)')
    import tarfile
    import tempfile
    import urllib.request
    try:
        with tempfile.TemporaryDirectory(prefix='mv-ffprobe-') as tmp:
            tar_path = os.path.join(tmp, 'ff.tar.xz')
            urllib.request.urlretrieve(_FFPROBE_TARBALL_URL, tar_path)
            with tarfile.open(tar_path) as tf:
                member = next((m for m in tf.getmembers()
                               if m.name.endswith('/ffprobe')), None)
                if member is None:
                    logger.warning('[MotionVideo] ffprobe member not found in tarball')
                    return ''
                src = tf.extractfile(member)
                if src is None:
                    return ''
                with open(dest, 'wb') as out:
                    out.write(src.read())
        os.chmod(dest, 0o755)
    except Exception as e:
        logger.warning('[MotionVideo] ffprobe download failed: %s', e, exc_info=True)
        return ''
    try:
        out = subprocess.run([dest, '-version'], capture_output=True,
                             text=True, timeout=30)
        if out.returncode != 0:
            logger.warning('[MotionVideo] downloaded ffprobe failed to run: %.300s',
                           out.stderr)
            return ''
    except Exception as e:
        logger.warning('[MotionVideo] downloaded ffprobe verify failed: %s', e)
        return ''
    logger.info('[MotionVideo] ffprobe available at %s', dest)
    return dest


def ensure_ffmpeg(*, install: bool = True, timeout: int = 300) -> str:
    """Return an ffmpeg path, pip-installing ``imageio-ffmpeg`` if absent.

    ``imageio-ffmpeg`` is a zero-dependency wheel bundling a full static
    ffmpeg (libx264/aac/mp3/png) — no root needed, ``pip uninstall``
    reverses it. Never raises; failures are logged + reported as ''.
    """
    existing = ffmpeg_bin()
    if existing:
        return existing
    if not install:
        return ''
    logger.info('[MotionVideo] installing imageio-ffmpeg into %s '
                '(one-time ffmpeg bootstrap)', sys.prefix)
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', 'imageio-ffmpeg'],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, 'PIP_REQUIRE_VIRTUALENV': 'false'})
        if proc.returncode != 0:
            logger.warning('[MotionVideo] imageio-ffmpeg install failed rc=%s: %.500s',
                           proc.returncode, proc.stderr)
            return ''
    except subprocess.TimeoutExpired:
        logger.warning('[MotionVideo] imageio-ffmpeg install timed out after %ss', timeout)
        return ''
    except Exception as e:
        logger.warning('[MotionVideo] imageio-ffmpeg install error: %s', e, exc_info=True)
        return ''
    # The install landed in site-packages WHILE this process is running —
    # the import system's directory caches must be invalidated or the new
    # package stays invisible to ``import imageio_ffmpeg``.
    import importlib
    importlib.invalidate_caches()
    path = ffmpeg_bin()
    if path:
        logger.info('[MotionVideo] ffmpeg available at %s', path)
    else:
        logger.warning('[MotionVideo] imageio-ffmpeg installed but no ffmpeg resolved')
    return path


# ── Render environment ────────────────────────────────────

def _conda_gui_lib_dir() -> str:
    """``sys.prefix/lib`` when it actually carries the headless-Chrome GUI libs.

    Thin wrapper over the shared resolver so this module keeps its own name for
    the concept while the RULE (which dirs count, and why) lives in one place.
    """
    from chromium_env import chromium_lib_dirs
    dirs = chromium_lib_dirs()
    return dirs[0] if dirs else ''


def build_render_env(base: dict | None = None) -> dict:
    """Build the subprocess environment for hyperframes / ffmpeg calls.

    On top of the inherited environment:

    * ``PATH`` is prepended with the resolved ffmpeg's directory so the CLI
      finds it (and ffprobe when it sits alongside).
    * ``HYPERFRAMES_BROWSER_PATH`` points at the resolved Chrome so the CLI
      skips its own (potentially blocked) browser download.
    * ``LD_LIBRARY_PATH`` is prepended with ``sys.prefix/lib`` when that dir
      carries the GUI libs headless Chrome needs (libatk & friends) — the
      rootless-conda recipe.
    * ``FONTCONFIG_FILE`` points at ``sys.prefix/etc/fonts/fonts.conf`` when
      the system config (``/etc/fonts/fonts.conf``) is absent — without ANY
      fontconfig config the static ffmpeg's libass resolves zero fonts and
      every subtitles burn **silently renders nothing** (2026-07-26 root
      cause of test_burn_in_real_render: the burn exited 0 with a
      byte-identical frame). An operator-set FONTCONFIG_FILE always wins.
    """
    env = dict(base if base is not None else os.environ)
    ff = ffmpeg_bin()
    probe = ffprobe_bin()
    if ff or probe:
        # Canonical-name shims: the CLI looks for executables literally
        # named ``ffmpeg`` / ``ffprobe`` on PATH.
        ff_shim = _refresh_shim('ffmpeg', ff)
        probe_shim = _refresh_shim('ffprobe', probe)
        dirs: list[str] = []
        for p in (ff_shim, probe_shim):
            if p and os.path.dirname(p) not in dirs:
                dirs.append(os.path.dirname(p))
        if dirs:
            env['PATH'] = os.pathsep.join(dirs) + os.pathsep + env.get('PATH', '')
    chrome = chrome_bin()
    if chrome:
        env['HYPERFRAMES_BROWSER_PATH'] = chrome
    # Headless-Chrome GUI libs. Resolution itself lives in the shared
    # chromium_env module (see _conda_gui_lib_dir), but the injection stays
    # here so _conda_gui_lib_dir remains a monkeypatchable seam for tests.
    gui_lib = _conda_gui_lib_dir()
    if gui_lib:
        existing = env.get('LD_LIBRARY_PATH', '')
        env['LD_LIBRARY_PATH'] = (gui_lib + os.pathsep + existing) if existing else gui_lib
    # Font bootstrap, delegated to the same shared module. NOTE this variable
    # serves TWO consumers here: headless Chrome (blank-but-styled frames
    # without it) and the static ffmpeg's libass \u2014 with no fontconfig config
    # libass resolves zero fonts and every subtitle burn SILENTLY renders
    # nothing (2026-07-26 root cause of test_burn_in_real_render: the burn
    # exited 0 with a byte-identical frame). ensure_chromium_env honours an
    # operator-set FONTCONFIG_FILE and no-ops when /etc/fonts exists, which is
    # the behaviour both consumers want.
    try:
        from chromium_env import fontconfig_paths
        _fc_dir, _fc_file = fontconfig_paths()
        if _fc_file and 'FONTCONFIG_FILE' not in env:
            env['FONTCONFIG_PATH'] = _fc_dir
            env['FONTCONFIG_FILE'] = _fc_file
    except Exception as e:
        logger.warning('[MotionVideo] fontconfig resolution skipped: %s', e)
    return env


# ── Full probe ────────────────────────────────────────────

def _node_major() -> int:
    node = node_bin()
    if not node:
        return 0
    try:
        out = subprocess.run([node, '--version'], capture_output=True,
                             text=True, timeout=15)
        ver = out.stdout.strip().lstrip('v')
        return int(ver.split('.', 1)[0])
    except Exception as e:
        logger.debug('[MotionVideo] node --version failed: %s', e)
        return 0


def probe_env() -> dict:
    """Probe every render-chain dependency. Returns a status dict::

        {'ok': bool, 'node': 'v24.13.0'|'',
         'hyperframes': path|'', 'ffmpeg': path|'', 'ffprobe': path|'',
         'chrome': path|'', 'issues': [str, ...]}

    ``ok`` is True only when the four REQUIRED pieces (node>=22, hyperframes,
    ffmpeg, chrome) all resolve. Never raises; never installs (that is
    :func:`ensure_hyperframes`'s job).
    """
    issues: list[str] = []
    major = _node_major()
    node_ver = ''
    if major:
        node_ver = f'v{major}'  # major is enough for the gate; full string unnecessary
    if not node_bin():
        issues.append('node not found on PATH (Node.js >= 22 required)')
    elif major < _MIN_NODE_MAJOR:
        issues.append(f'node too old (need >= {_MIN_NODE_MAJOR}, found major {major})')

    hf = hyperframes_bin()
    if not hf:
        issues.append('hyperframes CLI not found (call motion_video_env_check '
                      'with install=true, or set TOFU_HYPERFRAMES_BIN)')
    ff = ffmpeg_bin()
    if not ff:
        issues.append('ffmpeg not found (pip install imageio-ffmpeg, or set TOFU_FFMPEG)')
    chrome = chrome_bin()
    if not chrome:
        issues.append('no Chrome/Chromium found (set HYPERFRAMES_BROWSER_PATH, '
                      'or install Playwright chromium)')
    probe = ffprobe_bin()
    return {
        'ok': not issues,
        'node': node_ver,
        'hyperframes': hf,
        'ffmpeg': ff,
        'ffprobe': probe,
        'chrome': chrome,
        'issues': issues,
    }
