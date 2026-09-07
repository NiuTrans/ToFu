"""chromium_env.py — single source of truth for the headless-Chromium runtime.

Two questions, one module, because getting either wrong produces the same
symptom (a dead browser) and they were each hand-copied into four places:

  1. **Which environment** must a launch inherit? (:func:`ensure_chromium_env`)
  2. **Which binary** counts as an installed Chromium? (:func:`chromium_executable`)

Playwright's Chromium is a CHILD process. It resolves its GUI shared libs
(``libatk-1.0.so.0``, ``libnss3.so``, ``libgbm.so.1``, …) through the dynamic
linker, which knows nothing about ``sys.prefix``; and it resolves fonts through
fontconfig, which finds ZERO fonts when ``/etc/fonts`` is absent. So a rootless
install (the conda-forge recipe: GUI libs + fontconfig + a font family inside
the env prefix) needs two environment variables exported before launch:

  * ``LD_LIBRARY_PATH``  → or the binary dies instantly with
    "libatk-1.0.so.0: cannot open shared object file".
  * ``FONTCONFIG_PATH`` / ``FONTCONFIG_FILE`` → or it launches and paints CSS
    fine but draws every glyph as nothing, so screenshots come back
    blank-but-styled. That reads as "the page didn't load" rather than as an
    error, which is why it went unnoticed for so long.

WHY THIS MODULE EXISTS (the reuse rule)
---------------------------------------
This logic was hand-copied into four places that each keyed off a DIFFERENT,
weaker signal, so each covered a different subset of entry points:

  ``server.py`` / ``bootstrap.py``  keyed off ``.tofu_env.json`` — a gitignored,
      per-host marker that is deliberately stripped from every export. No
      marker (fresh clone, exported bundle, Docker, ``pip install`` layout)
      → zero exports → dead browser.
  ``tests/conftest.py``             keyed off ``$CONDA_PREFIX`` — unset in any
      shell that did not run ``conda activate``, and never set at all on the
      uv/venv install path.
  ``tofu_search``'s pool            also keyed off ``$CONDA_PREFIX`` (measured
      2026-07-28: with it unset, its self-heal is a no-op and launch fails).

``sys.prefix`` is the one signal that is ALWAYS correct — it is a property of
the running interpreter, not of the shell that happened to launch it, and it is
right for conda envs, uv venvs and plain virtualenvs alike. So resolution
starts there.

The other half of the rule: a candidate directory is accepted only when it
actually CONTAINS one of the libs Chromium needs (:data:`_SENTINEL_LIBS`).
We never infer "this looks like a conda env, so its lib/ must be usable" —
that inference is what let the four copies disagree.

Import discipline (WHY THIS IS AT THE REPO ROOT, not in lib/)
-------------------------------------------------------------
Stdlib-only, and it must stay that way. Two hard constraints, both measured
rather than assumed:

  * ``server.py`` calls this BEFORE any third-party import.
  * ``bootstrap.py``'s entire job is to run when dependencies are MISSING and
    install them, so it cannot import anything that needs them.

A first draft lived at ``lib/chromium_env.py``; the guard in
``tests/test_chromium_env.py`` caught that merely importing it executed
``lib/__init__.py``, which drags in ``requests`` (405 modules, 0.44 s) and
touches the database — fatal for both callers above. Hence the root placement,
next to the other pre-dependency modules. :func:`_log` resolves the logger
lazily for the same reason: runtime diagnostics still reach the normal log
files, but the import graph stays clean.
"""

import logging
import os
import sys

__all__ = [
    'chromium_lib_dirs',
    'fontconfig_paths',
    'ensure_chromium_env',
    'describe_chromium_env',
    'browsers_root',
    'chromium_binaries',
    'chromium_executable',
    'is_headless_shell',
    'headed_chromium_executable',
]

#: Libs whose presence proves a directory really carries Chromium's GUI deps.
#: Probed per-directory as EVIDENCE — never assumed from the dir's name/shape.
#: ``libatk`` was the original observed failure; the rest cover partial installs
#: where a solver dropped one package but kept the others.
_SENTINEL_LIBS = (
    'libatk-1.0.so.0',
    'libatk-bridge-2.0.so.0',
    'libnss3.so',
    'libgbm.so.1',
    'libxkbcommon.so.0',
)


def _log():
    """Return the project logger, falling back to a stdlib one.

    Resolved lazily — see the module docstring: importing ``lib.log`` at module
    scope would execute ``lib/__init__``, which both callers must avoid.
    """
    try:
        from lib.log import get_logger
        return get_logger(__name__)
    except Exception:
        return logging.getLogger(__name__)


def _dir_carries_gui_libs(path):
    """True when ``path`` holds at least one lib from :data:`_SENTINEL_LIBS`."""
    if not path or not os.path.isdir(path):
        return False
    for lib in _SENTINEL_LIBS:
        if os.path.isfile(os.path.join(path, lib)):
            return True
    return False


def _prefix_candidates(env_prefix=None):
    """Prefixes that may hold the GUI libs, most-authoritative first."""
    out = []
    # sys.prefix FIRST: a property of the running interpreter, so it is right
    # for conda envs, uv venvs and virtualenvs, and cannot be un-set by the
    # shell. The env-var / marker-derived signals below are only fallbacks for
    # the case where the libs live in a DIFFERENT prefix than the interpreter.
    for cand in (sys.prefix, getattr(sys, 'base_prefix', ''),
                 env_prefix, os.environ.get('CONDA_PREFIX', '')):
        cand = (cand or '').strip()
        if cand and cand not in out:
            out.append(cand)
    return out


def chromium_lib_dirs(env_prefix=None):
    """Return dirs to prepend to ``LD_LIBRARY_PATH`` for headless Chromium.

    Only directories that actually carry the GUI libs are returned, so this is
    safe to call on a host that has them system-wide (returns the explicit
    override only) or on one with no rootless install at all (returns []).

    Args:
        env_prefix: Optional extra prefix to consider (e.g. the one recorded in
            ``.tofu_env.json``), used when the libs live in a prefix other than
            the running interpreter's.

    Returns:
        List of existing directory paths, most-authoritative first.
    """
    dirs = []
    # An explicit operator override always wins and is NOT sentinel-filtered:
    # someone pointing at a hand-built lib dir knows better than our probe.
    for raw in os.environ.get('CHROMIUM_EXTRA_LIB_DIRS', '').split(os.pathsep):
        raw = raw.strip()
        if raw and os.path.isdir(raw) and raw not in dirs:
            dirs.append(raw)
    for prefix in _prefix_candidates(env_prefix):
        lib = os.path.join(prefix, 'lib')
        if lib not in dirs and _dir_carries_gui_libs(lib):
            dirs.append(lib)
            # cos7-compat packages (mesa-libgbm-cos7-x86_64 …) land their libs
            # in the gcc sysroot instead of lib/. Only worth adding next to a
            # prefix we just confirmed.
            sysroot = os.path.join(
                prefix, 'x86_64-conda-linux-gnu', 'sysroot', 'usr', 'lib64')
            if sysroot not in dirs and os.path.isdir(sysroot):
                dirs.append(sysroot)
    return dirs


def fontconfig_paths(env_prefix=None):
    """Return ``(fontconfig_dir, fontconfig_file)`` to use, or ``(None, None)``.

    Returns ``(None, None)`` when the system config exists — a host with a real
    ``/etc/fonts`` must keep using it; overriding it would be a regression.
    """
    if os.path.isdir('/etc/fonts'):
        return None, None
    for prefix in _prefix_candidates(env_prefix):
        conf_dir = os.path.join(prefix, 'etc', 'fonts')
        conf_file = os.path.join(conf_dir, 'fonts.conf')
        if os.path.isfile(conf_file):
            return conf_dir, conf_file
    return None, None


def ensure_chromium_env(env=None, env_prefix=None):
    """Make headless Chromium launchable+legible from ``env``. Idempotent.

    Mutates ``env`` in place (default: ``os.environ``, i.e. inherited by every
    child process spawned afterwards). Safe to call repeatedly and on hosts
    that need nothing — a no-op then.

    ``LD_LIBRARY_PATH`` handling is Linux-only: macOS/Windows use different
    mechanisms that Playwright's own bundled binaries already handle. The
    fontconfig half is applied regardless, since it is keyed on the absence of
    ``/etc/fonts`` rather than on the platform.

    Args:
        env: Mapping to mutate. Defaults to ``os.environ``.
        env_prefix: Optional extra prefix to consider (see
            :func:`chromium_lib_dirs`).

    Returns:
        dict: ``{'lib_dirs_added': [...], 'fontconfig': path|None,
        'ld_library_path': str, 'platform_skipped': bool}``
    """
    target = os.environ if env is None else env
    report = {'lib_dirs_added': [], 'fontconfig': None,
              'ld_library_path': target.get('LD_LIBRARY_PATH', ''),
              'platform_skipped': False}

    if sys.platform.startswith('linux'):
        existing = target.get('LD_LIBRARY_PATH', '')
        have = [p for p in existing.split(os.pathsep) if p]
        add = [d for d in chromium_lib_dirs(env_prefix) if d not in have]
        if add:
            target['LD_LIBRARY_PATH'] = os.pathsep.join(add + have)
            report['lib_dirs_added'] = add
        report['ld_library_path'] = target.get('LD_LIBRARY_PATH', '')
    else:
        report['platform_skipped'] = True

    # An operator-set FONTCONFIG_FILE always wins — hence setdefault semantics.
    conf_dir, conf_file = fontconfig_paths(env_prefix)
    if conf_file and not target.get('FONTCONFIG_FILE'):
        target['FONTCONFIG_PATH'] = conf_dir
        target['FONTCONFIG_FILE'] = conf_file
        report['fontconfig'] = conf_file

    if report['lib_dirs_added'] or report['fontconfig']:
        _log().debug('[ChromiumEnv] libs=%s fontconfig=%s',
                     report['lib_dirs_added'], report['fontconfig'])
    return report


# ── Binary resolution ────────────────────────────────────────────────
#
# WHY THIS LIVES HERE (the second half of the same single-source rule)
# --------------------------------------------------------------------
# Resolving the ENV was centralised above, but "which binary counts" stayed
# hand-copied, and the copies disagreed — measured 2026-07-29 on a host whose
# ONLY build is ``chromium_headless_shell-1223``:
#
#   ``desktop/post_install.py``  asked Playwright for ``chromium.executable_path``
#       and ``os.path.isfile``'d it. That property names the FULL build
#       (``chromium-1223/chrome-linux64/chrome``) even when only the shell is
#       installed, so it reported "not installed" while ``launch()`` succeeded
#       — the desktop app kept offering a ~150 MB download for a working
#       browser. Not a transient skew: ``install.sh`` deliberately installs
#       ``--only-shell``, so the full build is a path it NEVER creates.
#   ``lib/motion_video/_env.py`` probed the shell layout correctly but hard-coded
#       ``~/.cache/ms-playwright``, which ``install.sh`` overrides via
#       ``PLAYWRIGHT_BROWSERS_PATH``.
#
# So: one resolver, evidence-based (a path counts only when it is a real file),
# honouring the same env var the installer sets.

#: Executable paths relative to a build dir, most-capable first.
#: The full build can do headed AND headless, so it wins when both exist; the
#: shell is a complete answer on its own (every launch in this repo is
#: headless). Covers current + legacy layouts on all three platforms.
_CHROMIUM_REL_PATHS = (
    # full build — Linux (new/old), macOS, Windows
    'chrome-linux64/chrome',
    'chrome-linux/chrome',
    'chrome-mac/Chromium.app/Contents/MacOS/Chromium',
    'chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium',
    'chrome-win64/chrome.exe',
    'chrome-win/chrome.exe',
    # headless shell — Linux (new/old), macOS, Windows
    'chrome-headless-shell-linux64/chrome-headless-shell',
    'chrome-headless-shell-mac/chrome-headless-shell',
    'chrome-headless-shell-mac-x64/chrome-headless-shell',
    'chrome-headless-shell-mac-arm64/chrome-headless-shell',
    'chrome-headless-shell-win64/chrome-headless-shell.exe',
    'chrome-headless-shell-win/chrome-headless-shell.exe',
    'chrome-linux/headless_shell',
)

#: System-wide browsers to fall back on, in preference order.
_SYSTEM_BROWSERS = ('google-chrome', 'google-chrome-stable', 'chromium',
                    'chromium-browser', 'microsoft-edge', 'msedge')


def browsers_root():
    """Return Playwright browser-cache roots to search, most-authoritative first.

    ``PLAYWRIGHT_BROWSERS_PATH`` is an OVERRIDE, not an extra candidate — that
    is Playwright's own semantics, verified 2026-07-29 by pointing it at an
    EMPTY directory: ``chromium.executable_path`` still resolved inside that
    directory and never fell back to ``~/.cache/ms-playwright``. Searching both
    would make us report a browser from a cache Playwright will not actually
    launch from.

    The special value ``'0'`` means "store inside the pip package", not a
    directory, so it is ignored rather than treated as a path.
    """
    env_root = (os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or '').strip()
    if env_root and env_root != '0':
        return [env_root] if os.path.isdir(env_root) else []
    home = os.path.expanduser('~')
    roots = [os.path.join(home, '.cache', 'ms-playwright'),
             os.path.join(home, 'Library', 'Caches', 'ms-playwright'),
             os.path.join(home, 'AppData', 'Local', 'ms-playwright')]
    return [r for r in roots if os.path.isdir(r)]


def _build_revision(name):
    """Parse the trailing revision from a build dir name (``chromium-1223`` →
    1223). Unparseable names sort last rather than raising."""
    tail = name.rsplit('-', 1)[-1].rsplit('_', 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return 0


def chromium_binaries(include_system=True):
    """Every usable Chromium executable, most-preferred first.

    A path is returned only when it is a real file — never inferred from a
    directory's name. Ordering: newest Playwright build first; within a build,
    the full build before the headless shell; then system-wide browsers.

    Args:
        include_system: Also consider ``google-chrome`` / ``chromium`` / … on
            PATH. Set False to ask strictly about the managed install.

    Returns:
        List of existing executable paths (may be empty).
    """
    found = []
    for root in browsers_root():
        try:
            entries = os.listdir(root)
        except OSError as e:
            _log().debug('[ChromiumEnv] cannot list %s: %s', root, e)
            continue
        # Rank by (capability tier, revision) so a full build beats the shell at
        # the SAME revision, and a newer build beats an older one regardless of
        # listdir order. Sorting build dirs alone is not enough: the shell lives
        # in its own `chromium_headless_shell-<rev>` directory, so tier is a
        # property of the resolved PATH, not of the directory ordering.
        ranked = []
        for name in entries:
            if not name.startswith(('chromium-', 'chromium_')):
                continue
            rev = _build_revision(name)
            for tier, rel in enumerate(_CHROMIUM_REL_PATHS):
                path = os.path.join(root, name, *rel.split('/'))
                if os.path.isfile(path):
                    ranked.append((-rev, tier, path))
        for _rev, _tier, path in sorted(ranked):
            if path not in found:
                found.append(path)
    if include_system:
        import shutil
        for name in _SYSTEM_BROWSERS:
            path = shutil.which(name)
            if path and path not in found:
                found.append(path)
    return found


def chromium_executable(include_system=True):
    """The Chromium executable to use, or ``''`` when none is installed.

    An explicit ``HYPERFRAMES_BROWSER_PATH`` override always wins (it is what
    the HyperFrames CLI itself honours), so an operator pointing at a
    hand-built binary is never second-guessed.
    """
    override = (os.environ.get('HYPERFRAMES_BROWSER_PATH') or '').strip()
    if override and os.path.isfile(override):
        return override
    cands = chromium_binaries(include_system=include_system)
    return cands[0] if cands else ''


def is_headless_shell(path):
    """True when ``path`` is a chrome-headless-shell build.

    The shell has NO headed mode — it is a separate, smaller binary, not a
    flag on the full build. Measured 2026-07-29: with only the shell
    installed, ``chromium.launch(headless=False)`` fails with "Executable
    doesn't exist at .../chromium-<rev>/chrome-linux64/chrome" plus
    Playwright's "just installed or updated" banner — an error that sends the
    reader off to reinstall something that is already correctly installed.

    So callers that genuinely need a visible window must be able to ASK, and
    degrade with a truthful message. Keyed on the binary's own name, which is
    what distinguishes the two builds in every Playwright layout.
    """
    return 'headless-shell' in os.path.basename(path or '') or \
           os.path.basename(path or '') == 'headless_shell'


def headed_chromium_executable():
    """A Chromium that can open a VISIBLE window, or ``''`` when none exists.

    ``install.sh`` installs ``--only-shell`` on purpose (-60% download) because
    almost every launch in this product is headless. That trade is correct, but
    it means "a browser is installed" and "a browser that can show a window is
    installed" are DIFFERENT questions, and a headed feature that asks the
    first one reports itself available and then dies at launch.

    Returns the first candidate that is not a headless shell (a full Playwright
    build or a system-wide Chrome/Edge/Chromium).
    """
    for path in chromium_binaries():
        if not is_headless_shell(path):
            return path
    return ''


def describe_chromium_env(env_prefix=None):
    """Diagnose the Chromium runtime env WITHOUT mutating anything.

    Used by healthcheck / env-probe surfaces to explain a dead browser instead
    of reporting a bare launch failure.

    Returns:
        dict with ``lib_dirs`` (resolvable GUI-lib dirs), ``fontconfig``
        (config file that would be used, or None), ``system_fontconfig``
        (whether ``/etc/fonts`` exists), ``executable`` (the Chromium binary
        that would be used, or ''), ``binaries`` (all candidates) and
        ``issues`` (human-readable list; empty means nothing is known to be
        missing).
    """
    lib_dirs = chromium_lib_dirs(env_prefix)
    _, conf_file = fontconfig_paths(env_prefix)
    system_fc = os.path.isdir('/etc/fonts')
    binaries = chromium_binaries()
    executable = chromium_executable()
    headed = headed_chromium_executable()
    issues = []
    if not executable:
        issues.append(
            'no Chromium executable found — install one with: python -m '
            'playwright install --only-shell chromium (the headless shell is '
            'sufficient; every launch here is headless)')
    if sys.platform.startswith('linux') and not lib_dirs:
        issues.append(
            'no directory carrying Chromium GUI libs (libatk/libnss/libgbm) '
            'was found under sys.prefix or $CONDA_PREFIX — on a rootless host '
            'install them with: conda install -c conda-forge atk-1.0 '
            'at-spi2-atk at-spi2-core alsa-lib xorg-libxcomposite '
            'xorg-libxdamage xorg-libxfixes xorg-libxrandr libxkbcommon nspr '
            'nss mesa-libgbm-cos7-x86_64 fontconfig font-ttf-dejavu-sans-mono, '
            'or with root: python -m playwright install-deps chromium')
    if not system_fc and not conf_file:
        issues.append(
            'no /etc/fonts and no fontconfig config in the env prefix — '
            'Chromium will render every glyph as nothing (blank-but-styled '
            'screenshots). Install: conda install -c conda-forge fontconfig '
            'font-ttf-dejavu-sans-mono')
    return {'lib_dirs': lib_dirs, 'fontconfig': conf_file,
            'system_fontconfig': system_fc, 'executable': executable,
            'headed_executable': headed, 'binaries': binaries,
            'issues': issues}
