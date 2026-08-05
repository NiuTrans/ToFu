#!/usr/bin/env python3
"""Guard tests: install.sh's uv fast path + clean conda fallback.

Background — the "install is too slow" optimization (2026-07):
  The historical bottleneck was the conda-forge solve (build a full env +
  resolve 40+ packages, minutes). Measured data showed `uv venv` + `uv pip
  install -r requirements.txt` installs the same stack from prebuilt
  manylinux wheels in ~1-2 min with ZERO from-source builds. So install.sh
  now defaults to a uv fast path and falls back to the (unchanged) conda
  path when it can't or shouldn't use uv.

  The compatibility FLOOR is that the conda fallback must always remain
  reachable — CentOS7 / old-glibc hosts (where PyMuPDF/Pillow ship no
  manylinux2014 wheel) must land on conda cleanly. These tests pin the
  branch structure by static analysis (no network, no uv, no conda, no
  server) so the fast path can never silently swallow the fallback.
"""

import os
import re
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _install_sh() -> str:
    with open(os.path.join(ROOT, 'install.sh'), 'r', encoding='utf-8') as f:
        return f.read()


def _server_py() -> str:
    with open(os.path.join(ROOT, 'server.py'), 'r', encoding='utf-8') as f:
        return f.read()


def _bootstrap_py() -> str:
    with open(os.path.join(ROOT, 'bootstrap.py'), 'r', encoding='utf-8') as f:
        return f.read()


def _chromium_env_py() -> str:
    """Source of chromium_env.py — where the LD_LIBRARY_PATH/fontconfig logic
    lives after the 2026-07-28 de-duplication (it used to be hand-copied into
    server.py, bootstrap.py, tests/conftest.py and lib/motion_video/_env.py,
    each keyed on a different, weaker signal)."""
    with open(os.path.join(ROOT, 'chromium_env.py'), 'r', encoding='utf-8') as f:
        return f.read()


def _really_imports_chromium_env(rel: str) -> bool:
    """True when ``rel`` contains a REAL import of chromium_env (AST node).

    Deliberately not a substring check: after the call site was deleted in a
    NEUTER run, four mentions survived in comments/docstrings and a
    substring-based guard stayed green. A comment must never satisfy a guard.
    """
    import ast
    with open(os.path.join(ROOT, rel), 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'chromium_env':
            return True
        if isinstance(node, ast.Import) and any(
                a.name == 'chromium_env' for a in node.names):
            return True
    return False


def test_use_conda_flag_parses_and_defaults_off():
    """--use-conda must parse to USE_CONDA=1 and default to 0."""
    text = _install_sh()
    assert 'USE_CONDA=0' in text, 'USE_CONDA has no default-off init'
    assert '--use-conda)' in text, 'no --use-conda argument parser'
    assert re.search(r'--use-conda\)\s+USE_CONDA=1', text), \
        '--use-conda does not set USE_CONDA=1'
    _ok('--use-conda parses to USE_CONDA=1 and defaults off')


def test_uv_path_gated_on_glibc_and_flags():
    """The uv attempt must be gated: --use-conda / --with-postgres / glibc<2.28
    all force the conda path; only otherwise is _try_uv_install called."""
    text = _install_sh()
    # A glibc>=2.28 probe exists.
    assert '_glibc_ge_228' in text, 'no _glibc_ge_228 probe defined'
    assert re.search(r'a\[1\]>2\|\|\(a\[1\]==2&&a\[2\]>=28\)', text), \
        '_glibc_ge_228 does not compare against 2.28'
    # The decision chain: USE_CONDA short-circuits; --with-postgres → conda;
    # glibc<2.28 → conda; else run _try_uv_install.
    chain = text[text.index('if [[ "$USE_CONDA" -eq 1 ]]; then'):]
    assert re.search(r'if \[\[ "\$USE_CONDA" -eq 1 \]\]; then', chain), \
        'decision chain does not start with the USE_CONDA short-circuit'
    assert re.search(r'elif \[\[ "\$WITH_POSTGRES" -eq 1 \]\]; then.*?USE_CONDA=1',
                     chain, re.S), '--with-postgres does not auto-switch to conda'
    assert re.search(r'elif ! _glibc_ge_228; then.*?USE_CONDA=1', chain, re.S), \
        'glibc<2.28 does not force the conda path'
    assert re.search(r'else\s+.*?_try_uv_install', chain, re.S), \
        '_try_uv_install is not the else (default) branch'
    _ok('uv attempt is gated on --use-conda / --with-postgres / glibc>=2.28')


def test_uv_fallback_is_clean_and_smoke_tests_glibc_canaries():
    """_try_uv_install must return non-zero on failure (→ conda) and its import
    smoke-test must include the glibc-floor canaries fitz (PyMuPDF) + PIL."""
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    # Every failure path returns 1 (never fail()/exit).
    assert fn.count('return 1') >= 4, \
        '_try_uv_install has too few `return 1` fallbacks (failures must fall to conda)'
    assert 'fail ' not in fn and 'exit 1' not in fn, \
        '_try_uv_install must never fail()/exit — a uv failure is recoverable'
    # The smoke-test line imports the glibc-floor canaries.
    assert re.search(r"-c 'import [^']*\bfitz\b[^']*\bPIL\b", fn) or \
           re.search(r"-c 'import [^']*\bPIL\b[^']*\bfitz\b", fn), \
        'import smoke-test does not exercise BOTH fitz (PyMuPDF) and PIL (Pillow)'
    # A failed smoke-test triggers the fallback.
    assert re.search(r'import smoke-test.*?return 1', fn, re.S) or \
           re.search(r'falling back to conda"\s*\n\s*return 1', fn), \
        'a failed import smoke-test does not return 1 (fall back to conda)'
    # The caller treats a non-zero return as "continue with conda".
    assert re.search(r'if _try_uv_install; then\s*\n\s*_FAST_PATH_DONE=1',
                     text), 'caller does not set _FAST_PATH_DONE only on uv success'
    assert re.search(r'else\s*\n\s*warn "uv fast path did not complete', text), \
        'caller does not warn+continue to conda when uv fails'
    _ok('uv fallback is clean: return 1 on any failure incl. fitz/PIL smoke-test')


def test_conda_pipeline_guarded_by_fast_path_flag():
    """Steps 1–8 (the conda pipeline) must sit under the _FAST_PATH_DONE guard
    and the guard must close before Step 8.5, so uv skips conda wholesale."""
    text = _install_sh()
    open_idx = text.index('if [[ "$_FAST_PATH_DONE" -ne 1 ]]; then')
    close_idx = text.index('fi  # ── end legacy conda path')
    step1 = text.index('#  Step 1: Locate, version-check, or install conda')
    step85 = text.index('#  Step 8.5: Validate data/pgdata')
    # Ordering: guard-open < Step1 < guard-close < Step 8.5.
    assert open_idx < step1 < close_idx < step85, \
        'conda pipeline is not fully wrapped by the _FAST_PATH_DONE guard'
    # The heavy conda solves live INSIDE the guarded region.
    region = text[open_idx:close_idx]
    assert 'conda create -n "$ENV_NAME"' in region, \
        'conda env creation escaped the guard'
    assert 'Installing Python dependencies from conda-forge' in region, \
        'the conda-forge dep install escaped the guard'
    _ok('the conda pipeline (Steps 1–8) is guarded by _FAST_PATH_DONE')


def test_conda_only_globals_preseeded_for_set_u():
    """CONDA_BASE / CONDA_OWNED_BY_US are referenced by the shared launch tail;
    they must be pre-seeded before the guard so `set -u` never trips on the uv
    path (where the conda block that sets them is skipped)."""
    text = _install_sh()
    guard = text.index('if [[ "$_FAST_PATH_DONE" -ne 1 ]]; then')
    pre = text[:guard]
    assert 'CONDA_BASE="${CONDA_BASE:-}"' in pre, \
        'CONDA_BASE not pre-seeded before the conda guard (set -u hazard on uv path)'
    assert 'CONDA_OWNED_BY_US="${CONDA_OWNED_BY_US:-0}"' in pre, \
        'CONDA_OWNED_BY_US not pre-seeded before the conda guard'
    _ok('conda-only globals are pre-seeded so the uv path is set -u-safe')


def test_uv_venv_uses_managed_python():
    """uv venv must be seeded from uv's OWN managed CPython so .venv/bin/python
    resolves to a distinct base binary (avoids the re-exec symlink-collision)."""
    text = _install_sh()
    assert '--python-preference only-managed' in text, \
        'uv venv does not force --python-preference only-managed (symlink-collision hazard)'
    _ok('uv venv is seeded from a managed standalone CPython')


def test_reexec_uses_prefix_not_just_executable():
    """server.py + bootstrap.py must decide 'already in env' by comparing
    sys.prefix to env_prefix (a venv's bin/python is a symlink to a base
    interpreter, so a bare executable compare can false-positive)."""
    for src, name in ((_server_py(), 'server.py'), (_bootstrap_py(), 'bootstrap.py')):
        assert re.search(r'os\.path\.realpath\(sys\.prefix\) == os\.path\.realpath\(env_prefix\)',
                         src), f'{name} does not use a sys.prefix vs env_prefix re-exec check'
    _ok('re-exec guard compares sys.prefix to env_prefix (symlink-safe) in both consumers')


def test_uv_marker_has_backend_field():
    """The uv path must write .tofu_env.json with backend='uv' + venv prefix."""
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    assert "'backend': 'uv'" in fn, "uv marker does not record backend='uv'"
    assert "'python': env_python" in fn, 'uv marker does not record the venv python'
    assert "'env_prefix': env_prefix" in fn, 'uv marker does not record env_prefix'
    _ok("uv path writes .tofu_env.json with backend='uv'")


def test_uv_ripgrep_no_source_build():
    """The uv path must NOT pip-install ripgrep (needs cargo); it detects the
    system binary and degrades to the Python fallback otherwise."""
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    assert 'pip install ripgrep' not in fn and 'cargo' not in fn, \
        'uv path must not build ripgrep from source (cargo) — zero-compile goal'
    assert 'command -v rg' in fn, 'uv path does not detect a system rg'
    assert 'command -v fd' in fn or 'command -v fdfind' in fn, \
        'uv path does not detect a system fd/fdfind'
    _ok('uv path detects system rg/fd, never builds ripgrep from source')


def test_server_reexec_respects_uv_backend():
    """server.py must only setdefault CONDA_PREFIX when backend != 'uv'."""
    text = _server_py()
    assert "backend = cfg.get('backend')" in text, \
        "server.py re-exec does not read the marker's backend field"
    assert re.search(
        r"if backend != 'uv':\s*\n\s*os\.environ\.setdefault\('CONDA_PREFIX', env_prefix\)",
        text), 'server.py setdefaults CONDA_PREFIX unconditionally (venv would misfire)'
    _ok("server.py skips the CONDA_PREFIX shim for a uv-backed venv")


def test_bootstrap_reexec_respects_uv_backend():
    """bootstrap.py (the other marker consumer) must also skip CONDA_PREFIX /
    CONDA_DEFAULT_ENV for a uv venv, else _running_in_conda_env() misfires."""
    text = _bootstrap_py()
    assert "backend = cfg.get('backend')" in text, \
        "bootstrap.py re-exec does not read the marker's backend field"
    assert re.search(
        r"if backend != 'uv':\s*\n\s*os\.environ\.setdefault\('CONDA_PREFIX', env_prefix\)",
        text), 'bootstrap.py setdefaults CONDA_PREFIX unconditionally (venv would misfire)'
    # CONDA_DEFAULT_ENV must be nested under the same backend != 'uv' guard.
    # Asserted structurally, not as one literal line: the two setdefaults are
    # not textually adjacent. The end anchor is the function's own end (the next
    # top-level `def`) rather than a neighbouring implementation literal — the
    # previous anchor was the fontconfig probe, which MOVED to chromium_env.py
    # and broke this guard with an unreadable "substring not found".
    guard = text.index("if backend != 'uv':")
    nxt = text.index('\ndef ', guard)
    assert re.search(r"if env_name:\s*\n\s*os\.environ\.setdefault\('CONDA_DEFAULT_ENV', env_name\)",
                     text[guard:nxt]), \
        'bootstrap.py sets CONDA_DEFAULT_ENV outside the backend != \'uv\' guard'
    _ok("bootstrap.py skips the conda shim for a uv-backed venv")


def test_playwright_env_exported_before_reexec_early_return():
    """Regression (2026-07-27): Chromium is a CHILD process and resolves libatk /
    libatk-bridge / libnss out of $env_prefix/lib, which is not on the default
    linker path. The export used to live INSIDE the re-exec branch, so a server
    launched directly with the env interpreter (the documented way) hit the
    `already_in_env` early return and never got LD_LIBRARY_PATH — every
    Playwright launch died with "libatk-1.0.so.0: cannot open shared object
    file". The export MUST happen before that early return, in both consumers.

    2026-07-28: the SETTER moved into chromium_env.py (single source of truth),
    so the LD_LIBRARY_PATH assertion follows it there. The call-ORDER assertion
    stays here — that is the property this test uniquely protects. The
    behavioural counterpart (a real screenshot from a scrubbed env) lives in
    tests/test_chromium_env.py.
    """
    for src, name, early_var in ((_server_py(), 'server.py', 'already_in_env'),
                                 (_bootstrap_py(), 'bootstrap.py', 'same')):
        assert '_tofu_export_env_native_paths' in src, \
            f'{name} has no native-path export helper'
        # Anchor on the CALL site (leading indent), not the bare name: the `def`
        # line always precedes the early return, so indexing the name alone
        # makes this assertion vacuously true even with the bug reintroduced.
        call = src.index('\n    _tofu_export_env_native_paths(env_prefix')
        early = src.index(f'    if {early_var}:\n        return')
        assert call < early, (
            f'{name}: the native-path export runs AFTER the {early_var} early '
            f'return — a directly launched server gets no LD_LIBRARY_PATH and '
            f'Chromium dies on libatk')
        assert _really_imports_chromium_env(name), (
            f'{name} no longer imports chromium_env.py — it has grown its '
            f'own copy of the LD_LIBRARY_PATH logic again')
    assert re.search(r"target\['LD_LIBRARY_PATH'\]", _chromium_env_py()), \
        'chromium_env.py never sets LD_LIBRARY_PATH'
    _ok('LD_LIBRARY_PATH is exported before the re-exec early return (both consumers)')


def test_fontconfig_exported_when_no_system_config():
    """Regression (2026-07-27): this host has no /etc/fonts, so fontconfig falls
    back to a builtin config that finds ZERO fonts. Chromium then launches and
    paints CSS backgrounds fine but draws every glyph as nothing — screenshots
    come back blank-but-styled, which reads as "the page didn't load" rather
    than as an error, so it fails silently.

    2026-07-28: this logic now lives once in chromium_env.py, so assert it
    there. Both directions still matter: the fallback must exist, AND it must
    stay conditional so a host WITH a real /etc/fonts keeps using it.
    """
    src = _chromium_env_py()
    assert "os.path.isdir('/etc/fonts')" in src, \
        'chromium_env.py does not probe for a system fontconfig config'
    assert 'FONTCONFIG_PATH' in src and 'FONTCONFIG_FILE' in src, \
        'chromium_env.py does not export FONTCONFIG_PATH/FONTCONFIG_FILE'
    # Must be conditional: a host WITH /etc/fonts keeps using it. The probe
    # returns (None, None) in that case, which is the guard.
    assert re.search(r"if os\.path\.isdir\('/etc/fonts'\):\s*\n\s*return None, None", src), \
        ('chromium_env.py does not bail out when /etc/fonts exists — it would '
         'override a real system fontconfig config')
    # And both entry points must actually route through it.
    for name in ('server.py', 'bootstrap.py'):
        assert _really_imports_chromium_env(name), \
            f'{name} does not import chromium_env.py for the fontconfig fallback'
    _ok('fontconfig falls back to the env config only when /etc/fonts is absent')


def test_conda_path_installs_fonts_for_chromium():
    """Chromium needs fontconfig + at least one real font family to render text.
    These were previously only present as transitive deps of other packages; a
    solver change could silently drop them and text rendering would break with
    no error. Pin them explicitly in CHROMIUM_LIBS.
    """
    text = _install_sh()
    libs = text[text.index('CHROMIUM_LIBS=('):]
    libs = libs[:libs.index(')')]
    assert 'atk-1.0' in libs, 'CHROMIUM_LIBS lost atk-1.0 (the original libatk failure)'
    assert 'fontconfig' in libs, 'CHROMIUM_LIBS does not install fontconfig'
    assert re.search(r'font-ttf-\S+', libs), \
        'CHROMIUM_LIBS installs no font family — Chromium renders text as nothing'
    _ok('conda path pins fontconfig + a real font family for Chromium')


def test_uv_path_verifies_chromium_actually_launches():
    """Downloading the browser != being able to RUN it. The uv venv has no
    conda-forge to source Chromium's GUI libs/fonts from, so on a bare host the
    binary lands but every launch dies on a missing .so. The fast path must
    prove launch+text-render at install time and print actionable recovery,
    instead of deferring the failure to a dead browser tool much later.
    """
    text = _install_sh()
    fn = text[text.index('_try_uv_install() {'):text.index('\nif [[ "$USE_CONDA" -eq 1 ]]; then')]
    assert 'chromium.launch' in fn, \
        'uv path never verifies Chromium can actually launch'
    assert 'measureText' in fn, \
        'uv path does not verify text rendering (a fontless browser screenshots blank)'
    assert 'install-deps chromium' in fn, \
        'uv path gives no root recovery command'
    assert '--use-conda' in fn, 'uv path gives no rootless recovery command'
    # The verification must never abort the install — browser tools degrade.
    assert 'fail ' not in fn and 'exit 1' not in fn, \
        'Chromium verification must not abort the install (browser tools degrade)'
    _ok('uv path verifies Chromium launches + renders text, with actionable recovery')


def test_healthcheck_probe_actually_launches_chromium():
    """`import playwright` is not evidence the browser WORKS: it stays green both
    when Chromium cannot launch (missing libatk) and when it launches with zero
    fonts (blank-but-styled screenshots). The runtime probe must really launch
    it and measure a glyph, and must self-export the env's native paths — a
    bare `python3 healthcheck.py` never ran server.py's boot exports, so
    without that it would report a FALSE failure.
    """
    with open(os.path.join(ROOT, 'healthcheck.py'), 'r', encoding='utf-8') as f:
        text = f.read()
    probe = text[text.index('# 5. Optional browser engine'):]
    probe = probe[:probe.index("print(f\"\\n{C.BOLD}")]
    assert 'chromium.launch' in probe, \
        'healthcheck browser probe never launches Chromium (import-only check)'
    assert 'measureText' in probe, \
        'healthcheck probe does not measure a glyph (misses the zero-fonts failure)'
    assert 'chromium_env' in probe, \
        'healthcheck probe does not set up the Chromium env — false failure'
    # The two distinct failure modes must be reported distinctly, not merged.
    assert 'renders NO text' in probe, 'no distinct zero-fonts diagnosis'
    assert 'cannot launch' in probe, 'no distinct launch-failure diagnosis'
    # A bare "cannot launch" sends people hunting the wrong thing, so when the
    # resolver already knows WHY (no GUI libs / no fontconfig), say so.
    assert 'diagnosis' in probe, \
        'healthcheck reports a launch failure without the resolved cause'
    _ok('healthcheck probe really launches Chromium + measures a glyph')


def _fork_index(text: str) -> int:
    """Index of the uv-vs-conda fork — the line that decides which backend runs.

    Anchored on the decision chain's first line rather than a line number so a
    reindent or an inserted step cannot silently relocate it.
    """
    return text.index('if [[ "$USE_CONDA" -eq 1 ]]; then')


def test_mirror_and_index_config_precede_the_backend_fork():
    """Regression (2026-07-28): the accelerants were unreachable on the FAST path.

    ``TOFU_CONDA_MIRROR`` / ``TOFU_PYPI_INDEX`` are baked into install.sh by
    export.py for corp hosts (mirrors.internal.example.com et al). Their consumers used
    to live at ~L784, i.e. INSIDE the ``_FAST_PATH_DONE != 1`` conda-only block
    that spans L476–L1823. But ``_try_uv_install`` runs BEFORE that block and
    returns on success — so on the default (uv) path the mirror was never read
    and every wheel came from the public PyPI. The faster route was the one
    with no acceleration at all.

    Assert the RESULT that matters: the index/mirror configuration happens
    before the fork, so BOTH backends inherit it from one source. Deliberately
    not asserting a line number or which variable name a backend consumes —
    a reasonable rewrite that keeps the ordering must stay green.
    """
    text = _install_sh()
    fork = _fork_index(text)
    pre = text[:fork]

    def _exports(var: str) -> list:
        """Real `export VAR=...` assignment lines only — NOT mentions in prose.

        A substring check let a NEUTER pass: deleting the two uv export lines
        left the word UV_DEFAULT_INDEX alive in a comment, and the guard stayed
        green. A comment must never satisfy a guard.
        """
        return re.findall(rf'^\s*export\s+{var}=.*$', pre, re.M)

    print(f'\n    PIP_INDEX_URL exports before fork : {_exports("PIP_INDEX_URL")}')
    print(f'    uv index exports before fork     : '
          f'{_exports("UV_INDEX_URL") + _exports("UV_DEFAULT_INDEX")}')

    assert 'TOFU_PYPI_INDEX' in pre, (
        'TOFU_PYPI_INDEX is consumed only after the uv-vs-conda fork — the uv '
        'fast path never sees the mirror, so corp hosts get zero acceleration '
        'on the DEFAULT path')
    assert _exports('PIP_INDEX_URL'), \
        'PIP_INDEX_URL is not exported before the backend fork'
    # uv reads its own env vars, not pip.conf. Without these the hoist is
    # cosmetic: pip's var alone does not redirect `uv pip install`.
    assert _exports('UV_INDEX_URL') or _exports('UV_DEFAULT_INDEX'), (
        'no uv-specific index var EXPORTED before the fork — uv ignores '
        "PIP_INDEX_URL, so hoisting pip's variable alone changes nothing")
    _ok('mirror/index config precedes the backend fork (both paths inherit it)')


def test_playwright_downloads_only_the_headless_shell():
    """Measured 2026-07-28: a default ``playwright install chromium`` fetches
    BOTH full Chromium (175.4 MB) AND chrome-headless-shell (113.2 MB) plus
    ffmpeg (2.3 MB) = 290.9 MB. ``--only-shell`` cuts the fetch to 115.5 MB
    (-60%), so this ratchet keeps every installer on the shell-only download.

    THE TRADE-OFF THIS RATCHET ENFORCES (read before widening or reverting it)
    -------------------------------------------------------------------------
    An earlier version of this docstring justified the flag with "zero
    ``headless=False`` / ``record_video`` / ``channel=`` call sites". That was
    measured FALSE on 2026-07-29 and is NOT why the flag is correct. The real
    picture:

      * There is EXACTLY ONE headed call site in the product:
        ``tofu_search/fetch/interactive_login.py`` (login-wall cookie capture,
        a rare, user-initiated action).
      * ``chrome-headless-shell`` has NO headed mode — it is a separate,
        smaller binary, not a flag on the full build. So on a shell-only
        install that ONE feature genuinely cannot work; measured, a headed
        launch fails with "Executable doesn't exist at
        .../chromium-<rev>/chrome-linux64/chrome".
      * Every OTHER browser path here (fetch, JS render, screenshots, the
        video render chain, the visual test ring) is headless and fully served
        by the shell.

    So the flag buys -60% download for all users at the cost of one rare
    feature — and that feature no longer DIES at launch: availability is
    decided by ``chromium_env.headed_chromium_executable()`` and the caller
    returns ``reason='headed_unavailable'`` naming the recovery command
    (``python -m playwright install chromium``). Guards for that live in
    tests/test_chromium_env.py.

    Evidence the shell is sufficient for the headless paths: the dev host
    carries ONLY ``chromium_headless_shell-1223`` (no ``chromium-*`` dir at
    all), ``chromium_env.chromium_binaries`` resolves it, ``chrome_bin`` in
    lib/motion_video/_env.py delegates to that resolver, and every screenshot
    path works including the real-browser visual ring.
    """
    text = _install_sh()
    # What counts as a REAL invocation is defined once in tests/_source_scan.py
    # (comments stripped first). The recovery instruction we document for the
    # one headed feature is literally `python -m playwright install chromium`
    # (the full build, on purpose), and a scanner that cannot tell a comment
    # from a command would flag it. A comment must never satisfy OR violate a
    # guard. This logic was duplicated in test_chromium_binary_resolution.py;
    # fixing it in only one place left the other red, hence the shared helper.
    sys.path.insert(0, os.path.join(ROOT, 'tests'))
    from _source_scan import playwright_install_invocations
    installs = playwright_install_invocations(text, lang='shell')
    # Print the scan surface BEFORE asserting — a regex that silently matches
    # nothing would otherwise make this guard vacuously green (charter:
    # "verify the scan surface first").
    print(f'\n    scanned {len(installs)} `playwright install` invocation(s):')
    for inv in installs:
        print('      -', inv.strip())
    assert installs, 'no `playwright install` invocation found at all'
    for inv in installs:
        assert '--only-shell' in inv, (
            f'`{inv.strip()}` still pulls the full 175 MB Chromium '
            f'build that no consumer launches — pass --only-shell')
    _ok(f'all {len(installs)} playwright installs fetch only the headless shell')


def test_docstrings_do_not_cite_deleted_symbols():
    """A guard's REASONING must not cite symbols that no longer exist.

    Found 2026-07-29: this file's ratchet cited a helper in
    lib/motion_video/_env.py (the private ``_playwright_chrome_candidates``
    enumerator, named here in prose rather than in ``module::symbol`` form so
    this very docstring does not become the dangling citation it forbids) as
    its evidence that the shell binary is accepted — but that function had been
    DELETED when its logic moved into chromium_env (it became dead code after
    the delegation). The assertions stayed green while the argument chain
    pointed at
    a symbol that was gone, so a reader auditing "why is --only-shell safe?"
    would follow the citation into nothing.

    That is the same failure mode as a stale premise: the conclusion may be
    right, but the stated reason cannot be checked. So verify that any
    ``module.py::symbol`` citation in this file's docstrings still resolves.
    """
    import ast
    import re

    with open(os.path.abspath(__file__), 'r', encoding='utf-8') as f:
        own_src = f.read()

    # Citations of the documented form `path/to/mod.py::symbol`.
    # Placeholder spellings used to DESCRIBE the form (in this very docstring)
    # are not citations of anything and must not be resolved.
    _PLACEHOLDERS = {'path/to/mod.py', 'module.py'}
    cites = {(rel, sym) for rel, sym in re.findall(r'([\w/]+\.py)::(\w+)', own_src)
             if rel not in _PLACEHOLDERS}
    print(f'\n    scanned {len(cites)} `module.py::symbol` citation(s):')
    for rel, sym in sorted(cites):
        print(f'      - {rel}::{sym}')
    if not cites:
        _ok('no module::symbol citations to verify')
        return

    dangling = []
    for rel, sym in sorted(cites):
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            dangling.append(f'{rel}::{sym} (file missing)')
            continue
        with open(path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=rel)
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))}
        names |= {t.id for n in ast.walk(tree)
                  if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name)}
        if sym not in names:
            dangling.append(f'{rel}::{sym} (symbol deleted)')
    assert not dangling, (
        'these docstrings cite symbols that no longer exist, so the reasoning '
        f'behind the assertions cannot be audited: {dangling}')
    _ok(f'all {len(cites)} module::symbol citations still resolve')


def test_browser_and_uv_caches_are_persistent_and_shared():
    """A re-run, a second env, or a rebuilt venv must not re-download 115 MB of
    browser and the whole wheel set again. Both caches must be pinned to a
    stable location OUTSIDE the venv/env dir (which gets cleared on --clear).

    Asserted as a result — the vars are exported before the fork so both
    backends share one cache — rather than pinning an exact path.
    """
    text = _install_sh()
    fork = _fork_index(text)
    pre = text[:fork]

    def _export_line(var: str) -> str:
        m = re.search(rf'^\s*export\s+{var}=(.*)$', pre, re.M)
        return m.group(1).strip() if m else ''

    pw_line = _export_line('PLAYWRIGHT_BROWSERS_PATH')
    uv_line = _export_line('UV_CACHE_DIR')
    print(f'\n    PLAYWRIGHT_BROWSERS_PATH = {pw_line or "<not exported before fork>"}')
    print(f'    UV_CACHE_DIR             = {uv_line or "<not exported before fork>"}')

    assert pw_line, (
        'PLAYWRIGHT_BROWSERS_PATH is not exported before the backend fork — the '
        'browser cache defaults per-env and gets re-downloaded per install')
    assert uv_line, (
        'UV_CACHE_DIR is not exported before the fork — uv re-fetches every '
        'wheel when the venv is rebuilt')
    # Must NOT live inside the venv, or `uv venv --clear` throws it away.
    # Scan the WHOLE assignment: an earlier regex stopped at the first '}' and
    # so never saw a '.venv' later in the same line (NEUTER did not bite).
    for var, line in (('PLAYWRIGHT_BROWSERS_PATH', pw_line), ('UV_CACHE_DIR', uv_line)):
        assert '.venv' not in line, (
            f'{var} points inside .venv ({line}) — that cache is destroyed on '
            f'every venv rebuild, so the download happens again')
    _ok('browser + wheel caches are persistent and shared across backends')


def test_force_reinstall_is_conditional_not_unconditional():
    """``--force-reinstall`` on the main conda solve re-lays-down all 30
    CONDA_PKGS on EVERY run, including a re-run where the env is already
    correct. It exists to compensate for the purge immediately above it (conda's
    metadata goes stale right after a pip-uninstall), so it is genuinely needed
    WHEN a purge actually removed something — and pure overhead when nothing
    was purged.

    Do not just delete it (that reintroduces the stale-metadata bug). Assert it
    is gated on the purge having done something.
    """
    text = _install_sh()
    assert '_install_main_deps() {' in text, 'the main-solve helper vanished'
    fn = text[text.index('_install_main_deps() {'):]
    fn = fn[:fn.index('\n}')]
    assert '--force-reinstall' in text, (
        '--force-reinstall was removed outright — that reintroduces the stale '
        'conda-metadata bug it was added for; make it CONDITIONAL instead')
    assert re.search(r'\$\{?_FORCE_REINSTALL', fn), (
        'the main solve hard-codes --force-reinstall — it re-lays-down all 30 '
        'conda packages even on a clean re-run where nothing was purged')
    _ok('--force-reinstall is gated on the purge actually removing something')


def test_chromium_env_survives_the_export():
    """Regression class (charter 2026-07-28): "export product is a first-class
    acceptance target". chromium_env.py is what makes Chromium launch in the
    bundle OTHER PEOPLE install, and the root cause it fixed was precisely that
    the old logic keyed off ``.tofu_env.json`` — a file export.py deliberately
    STRIPS. Nothing currently stops the same thing happening to chromium_env.py
    itself: add it to an exclusion set (or let .gitignore eat it) and the whole
    browser test ring stays green while every exported bundle ships a dead
    browser again.

    Drives the PRODUCTION predicate (export._should_exclude) plus the
    git-tracking door, so it cannot pass by testing a copy of the rules.
    """
    sys.path.insert(0, ROOT)
    import importlib
    pytest.importorskip('export', reason='export.py not shipped in opensource')
    export = importlib.import_module('export')

    target = 'chromium_env.py'
    for mode in ('personal', 'internal', 'opensource'):
        reason = export._should_exclude(target, target, mode)
        assert reason is None, (
            f'chromium_env.py is excluded from the {mode} export '
            f'(reason={reason!r}) — every bundle built this way ships a '
            f'browser that cannot launch, exactly the bug it was added to fix')

    # Second door: an untracked root file is dropped even if no rule names it.
    import subprocess
    r = subprocess.run(['git', 'ls-files', '--error-unmatch', target],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (
        'chromium_env.py is not tracked by git — _untracked_root_excludes drops '
        'untracked root files from the tarball, so it would never ship')
    _ok('chromium_env.py survives all three export modes and is git-tracked')


def main():
    print()
    print(_color('═══ install.sh uv fast-path / conda-fallback Guard Tests ═══', '36'))
    print()
    tests = [
        test_use_conda_flag_parses_and_defaults_off,
        test_uv_path_gated_on_glibc_and_flags,
        test_uv_fallback_is_clean_and_smoke_tests_glibc_canaries,
        test_conda_pipeline_guarded_by_fast_path_flag,
        test_conda_only_globals_preseeded_for_set_u,
        test_uv_marker_has_backend_field,
        test_uv_ripgrep_no_source_build,
        test_uv_venv_uses_managed_python,
        test_reexec_uses_prefix_not_just_executable,
        test_server_reexec_respects_uv_backend,
        test_bootstrap_reexec_respects_uv_backend,
        test_playwright_env_exported_before_reexec_early_return,
        test_fontconfig_exported_when_no_system_config,
        test_conda_path_installs_fonts_for_chromium,
        test_uv_path_verifies_chromium_actually_launches,
        test_healthcheck_probe_actually_launches_chromium,
        test_mirror_and_index_config_precede_the_backend_fork,
        test_playwright_downloads_only_the_headless_shell,
        test_docstrings_do_not_cite_deleted_symbols,
        test_browser_and_uv_caches_are_persistent_and_shared,
        test_force_reinstall_is_conditional_not_unconditional,
        test_chromium_env_survives_the_export,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
