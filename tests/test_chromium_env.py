#!/usr/bin/env python3
"""Guard tests: headless Chromium must be launchable from ANY entry point.

WHAT BROKE (measured 2026-07-28, not inferred)
----------------------------------------------
A plain ``python3`` in this repo could not screenshot at all::

    chrome-headless-shell: error while loading shared libraries:
    libatk-1.0.so.0: cannot open shared object file

...while all 12 GUI libs were present in the env prefix the whole time. The
libs were never missing; ``LD_LIBRARY_PATH`` simply was not exported, because
each of the four hand-copied exporters keyed off a signal weaker than
``sys.prefix``:

  * ``server.py`` / ``bootstrap.py`` → ``.tofu_env.json``, a gitignored per-host
    marker that export.py:348 deliberately STRIPS from every bundle. So the
    exported product — what other users install — had no working browser.
  * ``tests/conftest.py`` and ``tofu_search``'s pool → ``$CONDA_PREFIX``, unset
    in any non-activated shell and never set on the uv/venv path. Measured:
    with it unset, the self-heal is a no-op and launch still dies.

WHY THESE ASSERT RESULTS, NOT IMPLEMENTATION
--------------------------------------------
Charter: "行为守卫 MUST 断言结果". The pre-existing guards in
``test_install_uv_fastpath.py`` assert that server.py CONTAINS certain source
text. Those are legitimate ratchets, but they cannot notice that the exported
product has no marker to read — they were all GREEN while screenshots were
100% dead from a bare shell. So the tests here launch a REAL browser in a
SCRUBBED environment and assert bytes come back.

Judged by: "if the production logic were deleted today, would this go red?"
Yes — remove the export and the scrubbed-env launch fails.
"""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chromium_env  # noqa: E402


# ── The env a fresh user actually has ────────────────────────────────
# Scrubbing these reproduces the three real-world shapes in ONE fixture:
# a fresh clone / exported bundle (no marker), a non-activated shell (no
# CONDA_PREFIX), and a Docker/pip layout (neither).
_SCRUBBED = ('LD_LIBRARY_PATH', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV',
             'FONTCONFIG_PATH', 'FONTCONFIG_FILE', 'CHROMIUM_EXTRA_LIB_DIRS')


def _scrubbed_env():
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env['PYTHONPATH'] = ROOT
    return env


def _run_probe(body, env=None):
    """Run ``body`` in a subprocess with a scrubbed env; return (rc, out)."""
    proc = subprocess.run(
        [sys.executable, '-c', textwrap.dedent(body)],
        cwd=ROOT, env=env if env is not None else _scrubbed_env(),
        capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr)


def _have_playwright():
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def _chromium_present():
    """True when a Playwright browser build is actually on disk.

    Distinguishes "not installed" (legitimate skip) from "installed but cannot
    launch" (the defect this file guards). Without this split a broken env
    would silently skip instead of failing.
    """
    base = os.path.expanduser('~/.cache/ms-playwright')
    if not os.path.isdir(base):
        return False
    for entry in os.listdir(base):
        if not entry.startswith(('chromium-', 'chromium_headless_shell-')):
            continue
        for rel in ('chrome-linux64/chrome', 'chrome-linux/chrome',
                    'chrome-headless-shell-linux64/chrome-headless-shell'):
            if os.path.isfile(os.path.join(base, entry, rel)):
                return True
    return False


requires_browser = pytest.mark.skipif(
    not _have_playwright() or not _chromium_present(),
    reason='playwright/chromium not installed (run: python -m playwright '
           'install chromium) — nothing to guard on this host')


# ── Scan-surface report (charter: print what you actually cover) ─────

def test_scan_surface_report():
    """Print the resolved env BEFORE any assertion.

    Charter ("扫描类守卫必须先验证扫描面"): a guard whose inputs are silently
    empty passes while covering nothing. This makes the coverage auditable.
    """
    desc = chromium_env.describe_chromium_env()
    print('\n  sys.prefix          :', sys.prefix)
    print('  GUI lib dirs found  :', desc['lib_dirs'] or '<none>')
    print('  system /etc/fonts   :', desc['system_fontconfig'])
    print('  fontconfig fallback :', desc['fontconfig'] or '<none>')
    print('  issues              :', desc['issues'] or '<none>')
    print('  playwright installed:', _have_playwright())
    print('  chromium on disk    :', _chromium_present())
    assert isinstance(desc['lib_dirs'], list)


# ── The behavioural core ─────────────────────────────────────────────

@requires_browser
def test_screenshot_works_from_a_scrubbed_environment():
    """THE regression: a real screenshot with no marker and no CONDA_PREFIX.

    This is the exact shape a user of the EXPORTED bundle has. It fails on the
    pre-fix tree with "libatk-1.0.so.0: cannot open shared object file".
    """
    rc, out = _run_probe('''
        import os, sys
        assert not os.environ.get('LD_LIBRARY_PATH'), 'fixture leaked LD_LIBRARY_PATH'
        assert not os.environ.get('CONDA_PREFIX'), 'fixture leaked CONDA_PREFIX'
        from chromium_env import ensure_chromium_env
        ensure_chromium_env()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=['--no-sandbox'])
            pg = b.new_page(viewport={'width': 400, 'height': 200})
            pg.set_content("<b style='font:28px sans-serif'>tofu</b>")
            png = pg.screenshot()
            b.close()
        assert len(png) > 500, 'screenshot suspiciously small: %d' % len(png)
        print('SCREENSHOT_BYTES=%d' % len(png))
    ''')
    assert rc == 0, f'screenshot failed in a scrubbed env:\n{out[-2500:]}'
    assert 'SCREENSHOT_BYTES=' in out, out[-2000:]


@requires_browser
def test_text_actually_renders_not_blank_but_styled():
    """Fonts must resolve — the silent half of the failure.

    With no ``/etc/fonts`` and no fontconfig fallback, Chromium launches and
    paints CSS but draws every glyph as nothing: screenshots come back
    blank-but-styled, which reads as "the page didn't load" rather than as an
    error. A launch-only assertion cannot see this, so measure a glyph.
    """
    rc, out = _run_probe('''
        from chromium_env import ensure_chromium_env
        ensure_chromium_env()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=['--no-sandbox'])
            pg = b.new_page()
            pg.set_content('<h1>tofu</h1>')
            w = pg.evaluate("(()=>{const c=document.createElement('canvas')"
                            ".getContext('2d');c.font='60px sans-serif';"
                            "return c.measureText('tofu').width;})()")
            b.close()
        assert w and w > 0, 'zero-width glyphs: Chromium found NO fonts'
        print('GLYPH_WIDTH=%.1f' % w)
    ''')
    assert rc == 0, f'font probe failed:\n{out[-2500:]}'
    assert 'GLYPH_WIDTH=' in out, out[-2000:]


def test_resolution_does_not_depend_on_the_env_marker():
    """``.tofu_env.json`` is stripped from every export (export.py:348).

    So resolution must work from ``sys.prefix`` alone. Asserted as a RESULT:
    with the marker made unreadable, the same dirs still resolve.
    """
    from_prefix = chromium_env.chromium_lib_dirs()
    with_bogus = chromium_env.chromium_lib_dirs(env_prefix='/nonexistent/prefix')
    assert from_prefix == with_bogus, (
        'a bogus marker prefix changed resolution — the marker is not '
        f'supposed to be load-bearing: {from_prefix} != {with_bogus}')
    if chromium_env._dir_carries_gui_libs(os.path.join(sys.prefix, 'lib')):
        assert os.path.join(sys.prefix, 'lib') in from_prefix, \
            'sys.prefix/lib carries the GUI libs but was not resolved'


def test_only_directories_with_real_libs_are_added(tmp_path):
    """Evidence, not inference: a lib-less prefix must NOT be added.

    Prevents the pattern that let the four copies drift — assuming a
    conda-shaped prefix must have usable libs.
    """
    empty = tmp_path / 'empty-prefix'
    (empty / 'lib').mkdir(parents=True)
    env = {}
    chromium_env.ensure_chromium_env(env=env, env_prefix=str(empty))
    assert str(empty / 'lib') not in env.get('LD_LIBRARY_PATH', ''), \
        'a directory with no GUI libs was added to LD_LIBRARY_PATH'

    fake = tmp_path / 'real-prefix'
    (fake / 'lib').mkdir(parents=True)
    (fake / 'lib' / 'libatk-1.0.so.0').write_bytes(b'\x7fELF')
    env2 = {}
    chromium_env.ensure_chromium_env(env=env2, env_prefix=str(fake))
    if sys.platform.startswith('linux'):
        assert str(fake / 'lib') in env2.get('LD_LIBRARY_PATH', ''), \
            'a directory that DOES carry libatk was not added'


def test_existing_paths_are_preserved_and_idempotent(tmp_path):
    """Never clobber an operator's own LD_LIBRARY_PATH; never grow unbounded."""
    fake = tmp_path / 'p'
    (fake / 'lib').mkdir(parents=True)
    (fake / 'lib' / 'libnss3.so').write_bytes(b'\x7fELF')
    env = {'LD_LIBRARY_PATH': '/opt/mine/lib'}
    chromium_env.ensure_chromium_env(env=env, env_prefix=str(fake))
    first = env['LD_LIBRARY_PATH']
    assert '/opt/mine/lib' in first, 'operator LD_LIBRARY_PATH was dropped'
    chromium_env.ensure_chromium_env(env=env, env_prefix=str(fake))
    assert env['LD_LIBRARY_PATH'] == first, 'not idempotent — path grew'


def test_system_fontconfig_is_never_overridden(tmp_path, monkeypatch):
    """A host WITH /etc/fonts must keep using it (regression direction #2).

    The complement of the fallback test: without this, "always export our own
    config" would also pass, and we would break every normal Linux desktop.
    """
    monkeypatch.setattr(os.path, 'isdir',
                        lambda p: True if p == '/etc/fonts' else os.path.lexists(p))
    conf_dir, conf_file = chromium_env.fontconfig_paths()
    assert conf_dir is None and conf_file is None, \
        'fontconfig fallback fired even though /etc/fonts exists'


def test_operator_fontconfig_file_wins(tmp_path):
    """An explicitly-set FONTCONFIG_FILE must not be replaced."""
    env = {'FONTCONFIG_FILE': '/my/own/fonts.conf'}
    chromium_env.ensure_chromium_env(env=env)
    assert env['FONTCONFIG_FILE'] == '/my/own/fonts.conf'


def test_module_imports_without_dragging_third_party():
    """``server.py`` calls this BEFORE its third-party imports.

    ``import lib.log`` transitively pulls ``lib/__init__`` (measured 0.44 s,
    405 modules, incl. requests), so this module must stay stdlib-only at
    import time. Asserted as a RESULT: import it in a virgin interpreter and
    check what landed in sys.modules.
    """
    rc, out = _run_probe('''
        import sys
        import chromium_env  # noqa: F401
        heavy = [m for m in ('requests', 'quart', 'httpx', 'sqlalchemy',
                             'playwright', 'fitz', 'PIL', 'lib', 'lib.log')
                 if m in sys.modules]
        assert not heavy, 'chromium_env dragged in %s at import time' % heavy
        print('CLEAN_IMPORT')
    ''')
    assert rc == 0, out[-2000:]
    assert 'CLEAN_IMPORT' in out, out[-1500:]


def test_conftest_browser_fixture_helper_actually_runs():
    """The visual-E2E ``browser`` fixture's helper must RUN, not just parse.

    Found the hard way: my first rewrite of ``_ensure_chromium_library_path``
    used ``sys`` without importing it in conftest.py, so every
    ``@pytest.mark.visual`` test ERRORed with ``NameError: name 'sys' is not
    defined``. None of the other tests here caught it, because they exercise
    the shared module directly and never go through conftest's wrapper.

    So call the wrapper for real. A NameError / ImportError inside it fails
    here instead of taking out the whole visual suite.

    Loaded by PATH rather than by module name: ``conftest`` is not importable
    as a top-level module under this rootdir, and a guard that dies on
    ImportError is red in every state, which is just as useless as one that is
    green in every state.
    """
    import importlib.util
    path = os.path.join(ROOT, 'tests', 'conftest.py')
    spec = importlib.util.spec_from_file_location('_cf_probe', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    added = mod._ensure_chromium_library_path()
    assert isinstance(added, list), \
        f'_ensure_chromium_library_path returned {type(added).__name__}, not a list'


def test_all_entry_points_use_the_shared_module():
    """Ratchet: no entry point may re-grow its own copy.

    Anchored on the AST \u2014 a real ``import chromium_env`` / ``from chromium_env
    import \u2026`` node \u2014 NOT on the substring. NEUTER-4 caught the substring
    version passing after the CALL was deleted, because four mentions survived
    in comments and docstrings; a comment must never be able to satisfy a
    guard. (Charter: "\u6d4b\u4e86 helper \u4e0d\u7b49\u4e8e\u6d4b\u4e86\u8c03\u7528\u70b9" \u2014 judged by "delete this call:
    does the guard go red?")

    This is the one implementation-facing assertion in this file and it is a
    ratchet by design: four independent drifting copies is the exact failure
    being closed here.
    """
    import ast

    consumers = {
        'server.py': 'boots the app',
        'bootstrap.py': 'smart launcher',
        'tests/conftest.py': 'visual E2E browser fixture',
        'lib/motion_video/_env.py': 'video render chain',
    }
    missing = []
    for rel, why in consumers.items():
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=rel)
        imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'chromium_env':
                imported = True
            elif isinstance(node, ast.Import):
                if any(a.name == 'chromium_env' for a in node.names):
                    imported = True
        if not imported:
            missing.append(f'{rel} ({why})')
    assert not missing, (
        'these entry points contain no real import of chromium_env.py, so they '
        f'have their own drifting copy: {missing}')


# ── The OUT-OF-TREE consumer (the one the ratchet above cannot see) ──
#
# tofu_search ships as an independent package/repo, so it is not reachable by a
# repo-relative path and the ratchet above structurally cannot cover it. That
# blind spot is exactly how it survived: chromium_env.py's own docstring named
# it as one of the four hand-copied copies, three were consolidated, and this
# one stayed on `$CONDA_PREFIX` — measured EMPTY on this host — so every
# browser fetch died with TargetClosedError from a bare shell. It is the
# fetch_url / JS-render entry point, i.e. the copy users hit most.

def _tofu_search_fetch_dir():
    """Directory of the installed tofu_search.fetch package, or '' if absent."""
    try:
        import tofu_search.fetch as _f
        return os.path.dirname(os.path.abspath(_f.__file__))
    except Exception as e:
        print(f'  tofu_search not importable: {e}')
        return ''


requires_tofu_search = pytest.mark.skipif(
    not _tofu_search_fetch_dir(),
    reason='tofu_search not installed — nothing to guard here')


@requires_tofu_search
def test_tofu_search_pool_resolves_from_sys_prefix_not_conda_prefix():
    """The pool's self-heal must survive an un-activated shell.

    Asserted as a RESULT, in a subprocess with CONDA_PREFIX scrubbed: importing
    the pool must populate LD_LIBRARY_PATH. Before the fix this printed '' and
    the subsequent launch raised TargetClosedError.

    Runs with the repo on PYTHONPATH, i.e. the DELEGATING path (chromium_env
    importable). The standalone fallback is covered separately below.
    """
    rc, out = _run_probe('''
        import os, sys
        assert not os.environ.get('CONDA_PREFIX'), 'fixture leaked CONDA_PREFIX'
        assert not os.environ.get('LD_LIBRARY_PATH'), 'fixture leaked LD_LIBRARY_PATH'
        import tofu_search.fetch.playwright_pool  # noqa: F401  (import side effect)
        ld = os.environ.get('LD_LIBRARY_PATH', '')
        assert ld, 'importing the pool did NOT populate LD_LIBRARY_PATH'
        print('POOL_LD_OK=%s' % ld.split(os.pathsep)[0])
    ''')
    assert rc == 0, f'pool self-heal is a no-op without CONDA_PREFIX:\n{out[-2500:]}'
    assert 'POOL_LD_OK=' in out, out[-2000:]


@requires_tofu_search
def test_tofu_search_pool_fallback_works_without_the_host_module():
    """tofu_search must NOT hard-depend on the Tofu app to self-heal.

    It ships independently, so the delegation has to degrade to a built-in
    rule. Proven by removing the repo from the import path entirely: the host
    module is then unimportable and only the fallback can populate the env.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
    env.pop('PYTHONPATH', None)          # <- the repo is NOT importable here
    # cwd must ALSO leave the repo: Python puts the script's directory ('' for
    # -c) at the front of sys.path, so running with cwd=ROOT would keep
    # chromium_env importable and the fallback would never be exercised. The
    # test caught this itself by refusing to pass.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='tofu-fallback-') as neutral_cwd:
        proc = subprocess.run(
            [sys.executable, '-c', textwrap.dedent('''
                import os, sys
                try:
                    import chromium_env  # noqa: F401
                    raise SystemExit('chromium_env WAS importable — fallback not exercised')
                except ImportError:
                    pass
                import tofu_search.fetch.playwright_pool  # noqa: F401
                ld = os.environ.get('LD_LIBRARY_PATH', '')
                assert ld, 'the standalone fallback did not populate LD_LIBRARY_PATH'
                print('FALLBACK_LD_OK=%s' % ld.split(os.pathsep)[0])
            ''')],
            cwd=neutral_cwd, env=env, capture_output=True, text=True, timeout=300)
    rc, out = proc.returncode, (proc.stdout + proc.stderr)
    assert rc == 0, f'standalone fallback failed:\n{out[-2500:]}'
    assert 'FALLBACK_LD_OK=' in out, out[-2000:]


@requires_tofu_search
@requires_browser
def test_tofu_search_pool_renders_glyphs_not_a_blank_page():
    """The pool had NO fontconfig half at all (measured: zero FONTCONFIG refs).

    On a host with no /etc/fonts that means a fetch returns a
    blank-but-styled page: the browser launches, paints CSS, and draws every
    glyph as nothing. That is strictly harder to diagnose than a crash — it
    reads as "the site didn't load". Fixing only LD_LIBRARY_PATH would have
    swapped a loud failure for a silent one, so measure a glyph.
    """
    rc, out = _run_probe('''
        import os
        import tofu_search.fetch.playwright_pool  # noqa: F401  (import side effect)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=['--no-sandbox'])
            pg = b.new_page()
            pg.set_content('<h1>tofu</h1>')
            w = pg.evaluate("(()=>{const c=document.createElement('canvas')"
                            ".getContext('2d');c.font='60px sans-serif';"
                            "return c.measureText('tofu').width;})()")
            b.close()
        assert w and w > 0, 'pool-configured Chromium found NO fonts (blank page)'
        print('POOL_GLYPH_WIDTH=%.1f' % w)
    ''')
    assert rc == 0, f'pool font probe failed:\n{out[-2500:]}'
    assert 'POOL_GLYPH_WIDTH=' in out, out[-2000:]


# ── Headed capability: --only-shell's premise, stated honestly ───────

def test_headless_shell_is_recognised_as_headed_incapable():
    """chrome-headless-shell has NO headed mode — it is a separate binary.

    The installers pass ``--only-shell`` on the stated premise that there are
    "zero headless=False call sites". That premise was FALSE
    (tofu_search/fetch/interactive_login.py launches headed), so the resolver
    must be able to answer "can anything here open a window?" rather than
    letting a headed feature discover it at launch.
    """
    assert chromium_env.is_headless_shell(
        '/x/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell')
    assert chromium_env.is_headless_shell('/x/chrome-linux/headless_shell')
    assert not chromium_env.is_headless_shell('/x/chromium-1223/chrome-linux64/chrome')
    assert not chromium_env.is_headless_shell('/usr/bin/google-chrome')


def test_headed_executable_excludes_shell_only_installs(tmp_path, monkeypatch):
    """A shell-only install must report NO headed-capable browser.

    The complement matters just as much: with a full build present it must
    report one, or a correct install would be told to reinstall.
    """
    root = tmp_path / 'ms-playwright'
    shell = root / 'chromium_headless_shell-1223' / 'chrome-headless-shell-linux64'
    shell.mkdir(parents=True)
    (shell / 'chrome-headless-shell').write_bytes(b'\x7fELF')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(root))
    monkeypatch.delenv('HYPERFRAMES_BROWSER_PATH', raising=False)
    monkeypatch.setattr('shutil.which', lambda _n: None)

    assert chromium_env.chromium_executable(), 'shell should still count as installed'
    assert chromium_env.headed_chromium_executable() == '', \
        'a shell-only install wrongly reported a headed-capable browser'

    full = root / 'chromium-1223' / 'chrome-linux64'
    full.mkdir(parents=True)
    (full / 'chrome').write_bytes(b'\x7fELF')
    assert chromium_env.headed_chromium_executable() == str(full / 'chrome'), \
        'a full build present but not reported as headed-capable'


@requires_tofu_search
def test_interactive_login_degrades_with_an_actionable_reason():
    """The headed feature must say WHAT is wrong and WHICH command fixes it.

    Raw Playwright emits "Looks like Playwright was just installed or updated"
    here, which is actively misleading: running ``playwright install`` again is
    precisely what produced a shell-only install. And the old availability
    check tested ``HAS_PLAYWRIGHT`` alone — constitutionally incapable of
    failing for the reason that actually breaks this feature.
    """
    rc, out = _run_probe('''
        import os
        os.environ.pop('HYPERFRAMES_BROWSER_PATH', None)
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/nonexistent-browser-root'
        from tofu_search.fetch.interactive_login import (
            is_interactive_login_available, capture_login_cookies)
        assert is_interactive_login_available() is False, (
            'availability said True with no headed-capable browser on disk')
        r = capture_login_cookies('example.com', 'https://example.invalid/login',
                                  timeout_s=1)
        assert r.get('ok') is False
        assert r.get('reason') == 'headed_unavailable', r
        err = r.get('error') or ''
        assert 'playwright install chromium' in err, err
        assert 'headless shell' in err, err
        print('LOGIN_DEGRADE_OK')
    ''')
    assert rc == 0, f'interactive-login degradation failed:\n{out[-2500:]}'
    assert 'LOGIN_DEGRADE_OK' in out, out[-2000:]
