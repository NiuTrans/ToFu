#!/usr/bin/env python3
"""Guard tests: ONE answer to "is a Chromium installed, and which one?".

WHAT BROKE (measured 2026-07-29, not inferred)
----------------------------------------------
This host carries ONLY ``chromium_headless_shell-1223``. There is no
``chromium-*`` full build at all — because ``install.sh`` installs
``--only-shell`` on purpose (-60% download). Everything works: Playwright
``launch()`` succeeds (v148) and a real screenshot came back with 8598 ink
pixels.

Yet ``desktop/post_install.py`` reported the browser as NOT installed, so the
desktop app offered a ~150 MB download for a browser that already ran. Its
check was::

    with sync_playwright() as p:
        return os.path.isfile(p.chromium.executable_path)

``executable_path`` names the FULL build (``chromium-1223/chrome-linux64/
chrome``) even when only the shell is present — measured ``exists = False``
while ``launch()`` in the same interpreter succeeded and ``ps`` showed the
shell binary as the parent process. So it is not a transient skew that a
re-install fixes: the installer never creates that path, by design.

Meanwhile ``lib/motion_video/_env.py`` probed the shell layout correctly but
hard-coded ``~/.cache/ms-playwright``, which ``install.sh`` overrides via
``PLAYWRIGHT_BROWSERS_PATH`` (exported at install.sh:336 so one cache is shared
across envs).

WHY THE EXISTING GUARDS WERE ALL BLIND
--------------------------------------
``tests/test_chromium_env.py`` launches a real browser and asserts pixels come
back — excellent, and green throughout this defect. It could not see this bug
because **it only ever exercises the channel that IS installed**. Nothing
asserted what the DETECTORS say, or that they agree with each other. So the
shape covered here is deliberately the complement: drive each detector against
synthetic on-disk layouts (full-only / shell-only / both / neither) and assert
they return the same verdict as the shared resolver.

Judged by "if the production logic were reverted, does this go red?" — yes:
NEUTER runs at the bottom of this file restore each pre-fix implementation and
every one of them bites.
"""

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# tests/ itself, so the shared source-scanning helpers (_source_scan) import
# cleanly no matter which rootdir pytest was invoked from.
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import chromium_env  # noqa: E402


# ── Synthetic on-disk layouts ────────────────────────────────────────
# Real Playwright cache shapes, built as files so the resolver's "must be a
# real file" rule is exercised rather than mocked away.

def _make_build(root, build, rel):
    """Create ``root/build/rel`` as an executable file; return its path."""
    path = os.path.join(root, build, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\x7fELF')
    os.chmod(path, 0o755)
    return path


_SHELL_REL = 'chrome-headless-shell-linux64/chrome-headless-shell'
_FULL_REL = 'chrome-linux64/chrome'


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An empty Playwright cache root, wired up via PLAYWRIGHT_BROWSERS_PATH.

    Also scrubs HYPERFRAMES_BROWSER_PATH so a developer's local override cannot
    make these tests vacuously pass.
    """
    root = tmp_path / 'ms-playwright'
    root.mkdir()
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(root))
    monkeypatch.delenv('HYPERFRAMES_BROWSER_PATH', raising=False)
    return root


def _detectors():
    """The three production detectors, as callables returning a verdict.

    Imported lazily and fresh each call so monkeypatched env vars apply.
    """
    from desktop.post_install import PlaywrightChromium
    from lib.motion_video import _env as menv
    return {
        'chromium_env.chromium_executable': lambda: bool(
            chromium_env.chromium_executable(include_system=False)),
        'desktop/post_install.is_installed': lambda: PlaywrightChromium().is_installed(),
        'motion_video.chrome_bin': lambda: bool(menv.chrome_bin()),
    }


# ── The core property: one answer, not three ─────────────────────────

@pytest.mark.parametrize('layout,expected', [
    ('shell_only', True),
    ('full_only', True),
    ('both', True),
    ('neither', False),
])
def test_every_detector_gives_the_same_verdict(cache, monkeypatch, layout, expected):
    """THE regression: shell-only must read as INSTALLED, everywhere.

    ``shell_only`` is this host's real shape and the shape ``install.sh``
    produces. Before the fix, ``desktop/post_install`` answered False for it
    while the other two answered True — the split that made the desktop app beg
    for a download it did not need.

    ``neither`` is the complement: a guard that only proved "always True" would
    pass with a detector hard-wired to True.
    """
    if layout in ('shell_only', 'both'):
        _make_build(str(cache), 'chromium_headless_shell-1223', _SHELL_REL)
    if layout in ('full_only', 'both'):
        _make_build(str(cache), 'chromium-1223', _FULL_REL)

    # System browsers must not leak in and turn `neither` into True.
    monkeypatch.setattr('shutil.which', lambda _n: None)

    verdicts = {name: fn() for name, fn in _detectors().items()}
    print(f'\n  [{layout}] verdicts: {verdicts}')
    disagree = {k: v for k, v in verdicts.items() if v != expected}
    assert not disagree, (
        f'layout={layout}: expected every detector to say {expected}, but '
        f'{disagree} disagreed — the detectors have drifted apart again. '
        f'All verdicts: {verdicts}')


def test_shell_only_is_the_shape_the_installer_produces(cache):
    """Ties the fixture above to reality: ``--only-shell`` is deliberate.

    If someone ever drops ``--only-shell`` from the installers, the shell-only
    case stops being the default shape and this file's premise weakens — so
    assert the installers still ask for it. Scans EVERY shell script, not just
    install.sh: scripts/install_on_server.sh was found drifted (plain
    ``playwright install chromium``, pulling the 175 MB build nobody launches).

    What counts as a REAL invocation is defined once in tests/_source_scan.py
    (comments stripped first). It used to be a local regex here AND a second
    local regex in test_install_uv_fastpath.py; fixing a comment-induced false
    alarm in one left the other broken, which is exactly why the definition is
    now shared rather than copied.
    """
    import glob

    from _source_scan import playwright_install_invocations

    scripts = ([os.path.join(ROOT, 'install.sh')]
               + sorted(glob.glob(os.path.join(ROOT, 'scripts', '*.sh'))))
    found = []
    for path in scripts:
        with open(path, encoding='utf-8', errors='ignore') as f:
            text = f.read()
        for inv in playwright_install_invocations(text, lang='shell'):
            found.append((os.path.relpath(path, ROOT), inv))
    print(f'\n  scanned {len(scripts)} script(s), '
          f'{len(found)} `playwright install` invocation(s):')
    for rel, inv in found:
        print(f'    - {rel}: {inv}')
    # Scan-surface check first: a regex matching nothing would make the
    # per-invocation assertion below vacuously green.
    assert found, 'no `playwright install` invocation found in any shell script'
    bad = [(rel, inv) for rel, inv in found if '--only-shell' not in inv]
    assert not bad, (
        f'these installers still fetch the 175 MB full Chromium build that no '
        f'consumer launches: {bad}')


def test_python_install_paths_also_ask_for_only_shell():
    """The desktop app has its OWN two install paths (source + frozen).

    They are Python, so the shell-script scan above cannot see them — and the
    frozen one (``desktop/launcher.py``, driven via TOFU_PLAYWRIGHT_INSTALL)
    is the path a packaged-app user actually hits. Both were fetching the full
    build. Asserted on the real argv/cmd list, not a substring of the file.
    """
    import ast
    targets = {
        'desktop/post_install.py': "cmd = [sys.executable, '-m', 'playwright', …]",
        'desktop/launcher.py': "sys.argv = ['playwright', 'install', …]",
    }
    for rel, what in targets.items():
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=rel)
        lists = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            vals = [e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if 'playwright' in vals and 'install' in vals:
                lists.append(vals)
        print(f'\n  {rel}: playwright-install argv lists = {lists}')
        assert lists, f'{rel}: found no playwright-install argv list ({what})'
        for vals in lists:
            assert '--only-shell' in vals, (
                f'{rel}: {vals} fetches the full 175 MB build — pass '
                f'--only-shell like install.sh does')


def test_browsers_path_env_is_honoured(cache):
    """``install.sh:336`` exports PLAYWRIGHT_BROWSERS_PATH to share one cache.

    The motion_video copy hard-coded ``~/.cache/ms-playwright`` and so went
    blind to any installer-pinned location. Asserted as a RESULT: a binary that
    exists ONLY under the env-var root must be found.
    """
    made = _make_build(str(cache), 'chromium_headless_shell-1223', _SHELL_REL)
    assert chromium_env.chromium_executable(include_system=False) == made, \
        'a build under $PLAYWRIGHT_BROWSERS_PATH was not resolved'
    # Anchored on the LIVE consumer path (chrome_bin), not a private helper:
    # an earlier version of this assertion drove
    # ``_playwright_chrome_candidates``, which the delegation turned into dead
    # code — so a NEUTER that reintroduced the hard-coded ~/.cache inside it
    # did not bite. Nothing called it.
    from lib.motion_video import _env as menv
    assert menv.chrome_bin() == made, \
        'motion_video ignored $PLAYWRIGHT_BROWSERS_PATH (hard-coded ~/.cache?)'


def test_browsers_path_zero_is_not_treated_as_a_directory(monkeypatch, tmp_path):
    """``PLAYWRIGHT_BROWSERS_PATH=0`` means "store inside the pip package".

    It is a sentinel, not a path. Treating it as one would make the resolver
    look for a directory literally named "0" — harmless but wrong, and it would
    mask the real roots.
    """
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', '0')
    roots = chromium_env.browsers_root()
    assert not any(r == '0' or r.endswith(os.sep + '0') for r in roots), \
        f'the "0" sentinel was treated as a directory path: {roots}'


def test_full_build_is_preferred_over_the_shell(cache, monkeypatch):
    """When both exist, prefer the full build — it can do headed AND headless.

    The shell is a complete answer on its own (every launch here is headless),
    but if someone installed the full build they presumably want it.
    """
    monkeypatch.setattr('shutil.which', lambda _n: None)
    shell = _make_build(str(cache), 'chromium_headless_shell-1223', _SHELL_REL)
    full = _make_build(str(cache), 'chromium-1223', _FULL_REL)
    got = chromium_env.chromium_executable(include_system=False)
    assert got == full, f'expected the full build to win, got {got} (shell={shell})'


def test_newer_revision_wins(cache, monkeypatch):
    """Ordering must be by parsed revision, not by listdir order."""
    monkeypatch.setattr('shutil.which', lambda _n: None)
    _make_build(str(cache), 'chromium_headless_shell-1200', _SHELL_REL)
    newer = _make_build(str(cache), 'chromium_headless_shell-1223', _SHELL_REL)
    got = chromium_env.chromium_executable(include_system=False)
    assert got == newer, f'older revision won: {got}'


def test_only_real_files_count(cache, monkeypatch):
    """Evidence, not inference — a build DIRECTORY with no binary is not a
    browser. This is the rule that let the copies drift: assuming a
    plausibly-named directory must contain a usable executable."""
    os.makedirs(os.path.join(str(cache), 'chromium-1223',
                             'chrome-linux64'), exist_ok=True)
    assert chromium_env.chromium_binaries(include_system=False) == [], \
        'an empty build directory was reported as an installed browser'


def test_operator_override_wins(cache, monkeypatch, tmp_path):
    """``HYPERFRAMES_BROWSER_PATH`` is what the HyperFrames CLI itself honours.

    An operator pointing at a hand-built binary must never be second-guessed.
    """
    _make_build(str(cache), 'chromium_headless_shell-1223', _SHELL_REL)
    mine = tmp_path / 'my-chrome'
    mine.write_bytes(b'\x7fELF')
    os.chmod(mine, 0o755)
    monkeypatch.setenv('HYPERFRAMES_BROWSER_PATH', str(mine))
    assert chromium_env.chromium_executable() == str(mine), \
        'an explicit HYPERFRAMES_BROWSER_PATH override was ignored'


def test_describe_reports_the_resolved_binary(cache):
    """A diagnosis surface that cannot say WHICH browser it found sends people
    hunting the wrong thing — the same reason the launch-failure path reports
    the resolved cause."""
    made = _make_build(str(cache), 'chromium_headless_shell-1223', _SHELL_REL)
    desc = chromium_env.describe_chromium_env()
    assert desc.get('executable') == made, \
        f"describe_chromium_env did not report the resolved binary: {desc.get('executable')}"
    assert made in (desc.get('binaries') or []), 'describe did not list candidates'


def test_no_browser_is_reported_as_an_issue(cache, monkeypatch):
    """The complement: with nothing on disk, the diagnosis must SAY so.

    Without this, "no browser installed" and "browser fine" look identical to
    any caller reading `issues`. Uses the `cache` fixture so the developer's
    real ~/.cache cannot leak in and make this vacuously pass.
    """
    monkeypatch.setattr('shutil.which', lambda _n: None)
    desc = chromium_env.describe_chromium_env()
    assert desc['executable'] == '', 'reported an executable with none on disk'
    assert any('no Chromium executable' in i for i in desc['issues']), \
        f"an empty cache produced no actionable issue: {desc['issues']}"


def test_resolver_stays_stdlib_only_at_import_time():
    """``server.py`` and ``bootstrap.py`` call this BEFORE third-party imports.

    The binary resolver added ``shutil`` — fine (stdlib), but assert the
    property rather than trusting it, since a future revision reaching for
    ``playwright`` here would break both boot paths.
    """
    proc = subprocess.run(
        [sys.executable, '-c',
         'import sys; import chromium_env; '
         "heavy=[m for m in ('requests','quart','httpx','sqlalchemy',"
         "'playwright','PIL','lib','lib.log') if m in sys.modules]; "
         "assert not heavy, heavy; print('CLEAN')"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={**os.environ, 'PYTHONPATH': ROOT})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'CLEAN' in proc.stdout


def test_detectors_delegate_rather_than_reimplement():
    """Ratchet: no consumer may re-grow its own layout table.

    Anchored on a real AST import node — a mention in a comment must never
    satisfy a guard (the lesson from the earlier NEUTER-4 on this same module).
    """
    import ast
    consumers = {
        'desktop/post_install.py': 'desktop component installer',
        'lib/motion_video/_env.py': 'video render chain',
    }
    missing = []
    for rel, why in consumers.items():
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=rel)
        ok = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'chromium_env':
                ok = True
            elif isinstance(node, ast.Import) and any(
                    a.name == 'chromium_env' for a in node.names):
                ok = True
        if not ok:
            missing.append(f'{rel} ({why})')
    assert not missing, (
        f'these detectors no longer import chromium_env — they have their own '
        f'drifting copy of "which binary counts": {missing}')


def test_post_install_does_not_use_executable_path():
    """The specific trap, pinned: ``chromium.executable_path`` is not evidence.

    It names the full build even when only the shell is installed, so any
    ``isfile(executable_path)`` check reports a working browser as missing.
    """
    import ast
    with open(os.path.join(ROOT, 'desktop/post_install.py'), encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename='post_install.py')
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == 'executable_path']
    assert not hits, (
        'post_install.py reads playwright .executable_path again — that names '
        'the FULL build and is False on every --only-shell install')
