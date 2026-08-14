#!/usr/bin/env python3
"""
healthcheck.py — Automated project diagnostics for tofu.

Run:  python3 healthcheck.py            — dev lint (source-tree checks)
      python3 healthcheck.py --runtime [--port N] [--wait SEC]
                                        — probe a RUNNING server (post-install
                                          self-check: reachable? DB? index page?
                                          LLM key? browser engine?)
Exit code 0 = all green, 1 = issues found.

Checks:
  1. Python syntax          — All .py files compile
  2. Top-level imports      — Server + all blueprints load
  3. Lazy imports           — Every `from X import Y` inside route functions resolves
  4. Database schema        — Required tables exist in init_db()
  5. Static vendor files    — All local JS/CSS deps exist and are non-trivial
  6. HTML references        — Every src/href in HTML points to a real file
  7. CDN leak detection     — No external CDN URLs remain in served files
  8. JS defensive guards  — Core JS libraries have typeof guards
"""

import ast
import importlib
import importlib.util
import logging
import os
import py_compile
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

# tofu_search imports pymupdf4llm as part of its public facade.  The optional
# layout backend is both unsupported by our RapidOCR version and unnecessary
# for an import audit; without the process policy, a healthcheck alone retains
# a host-sized ONNX thread pool and floods stderr on cpuset-restricted hosts.
try:
    from runtime_guards import install_pymupdf_classic_policy
    install_pymupdf_classic_policy()
except Exception as e:
    logger.debug('PyMuPDF classic policy unavailable: %s', e)

# ─── Helpers ─────────────────────────────────────────────────────────
class C:
    OK   = '\033[92m✓\033[0m'
    FAIL = '\033[91m✗\033[0m'
    WARN = '\033[93m⚠\033[0m'
    BOLD = '\033[1m'
    END  = '\033[0m'

errors = []
warnings = []

def section(title):
    print(f"\n{C.BOLD}{'─'*60}{C.END}")
    print(f"{C.BOLD}  {title}{C.END}")
    print(f"{C.BOLD}{'─'*60}{C.END}")

def ok(msg):
    print(f"  {C.OK} {msg}")

def fail(msg):
    errors.append(msg)
    print(f"  {C.FAIL} {msg}")

def warn(msg):
    warnings.append(msg)
    print(f"  {C.WARN} {msg}")


# ═══════════════════════════════════════════════════════════════════════
# Runtime mode (--runtime): probe a RUNNING server instead of linting source.
#
# The dev lint below proves the source tree is coherent; it says NOTHING about
# whether a freshly-installed server actually came up. install.sh launches
# `python server.py` and previously never verified the result — a failed boot
# (port busy, DB unwritable, missing wheel) was left for the user to spot in
# raw startup logs. This mode is the post-install self-check: poll
# /api/health (reachable + DB responsive), check the index page serves HTML,
# then report the two things a new user needs next (an LLM credential, the
# optional browser engine). Exits 0 on usable / 1 on broken, so install.sh can
# surface the verdict automatically.
# ═══════════════════════════════════════════════════════════════════════
if '--runtime' in sys.argv:
    import json as _json
    import time as _time
    import urllib.request as _urlreq

    def _arg(flag, default=None):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
        return default

    try:
        _port = int(_arg('--port', os.environ.get('PORT', '')) or 15000)
    except (TypeError, ValueError):
        _port = 15000
    try:
        _wait = float(_arg('--wait', '0') or 0)
    except (TypeError, ValueError):
        _wait = 0.0
    _base = f'http://127.0.0.1:{_port}'

    section(f"Runtime Probe — server on port {_port}")

    def _get(path, timeout=4):
        try:
            with _urlreq.urlopen(_base + path, timeout=timeout) as r:
                return r.status, r.read()
        except Exception:
            return None, None

    # 1. /api/health — optionally polling until the server finishes booting
    #    (imports + DB init + first bundle build take a few seconds).
    _deadline = _time.monotonic() + _wait
    _status = _body = None
    while True:
        _status, _body = _get('/api/health')
        if _status == 200 or _time.monotonic() >= _deadline:
            break
        _time.sleep(2)

    if _status != 200:
        fail(f"server not answering {_base}/api/health"
             + (f" after {_wait:.0f}s wait" if _wait else ""))
        print(f"\n{C.BOLD}  RESULT: {C.FAIL} server unreachable{C.END}")
        sys.exit(1)

    _health = {}
    try:
        _health = _json.loads(_body.decode('utf-8', 'replace'))
    except Exception as e:
        fail(f"/api/health returned non-JSON: {e}")

    ok(f"server reachable (version {_health.get('version', '?')}, "
       f"bootId {str(_health.get('bootId', '?'))[:8]})")

    # 2. Database
    if _health.get('db_responsive'):
        ok(f"database responsive ({_health.get('db_engine', '?')})")
    else:
        fail(f"database NOT responsive ({_health.get('db_engine', '?')}): "
             f"{_health.get('db_error', 'unknown')}")

    # 3. Required storage sidecar.  Older servers did not expose this field, so
    # keep their runtime probe compatible while making the new production
    # contract fail loudly whenever the sidecar is fenced or restarting.
    if 'storage_ready' not in _health:
        warn('server does not report storage sidecar readiness (legacy build)')
    elif _health.get('storage_ready'):
        _storage = _health.get('storage') or {}
        ok(f"storage sidecar ready ({_storage.get('backend', '?')}, "
           f"pid {_storage.get('pid', '?')})")
    else:
        _storage = _health.get('storage') or {}
        fail(f"storage sidecar NOT ready (state={_storage.get('state', '?')}): "
             f"{_storage.get('last_error', 'unknown') or 'unknown'}")

    # 4. Index page actually serves HTML (bundle injection / static serving)
    _s2, _b2 = _get('/')
    if _s2 == 200 and _b2 and b'<html' in _b2[:2000].lower():
        ok("index page serves HTML")
    else:
        fail(f"index page did not serve HTML (status={_s2})")

    # 5. At least one LLM credential somewhere the server reads:
    #    env vars → .env → server_config providers (api_keys or oauth slot).
    _has_key = bool(os.environ.get('LLM_API_KEY') or os.environ.get('LLM_API_KEYS'))
    if not _has_key:
        try:
            with open(ROOT / '.env', encoding='utf-8', errors='ignore') as _f:
                for _line in _f:
                    _ls = _line.strip()
                    if _ls.startswith('#'):
                        continue
                    if _ls.startswith(('LLM_API_KEYS=', 'LLM_API_KEY=')) \
                            and _ls.split('=', 1)[1].strip().strip('"\''):
                        _has_key = True
                        break
        except OSError:
            pass
    if not _has_key:
        try:
            with open(ROOT / 'data/config/server_config.json', encoding='utf-8') as _f:
                _cfg = _json.load(_f)
            for _p in (_cfg.get('providers') or []):
                if _p.get('oauth'):
                    _has_key = True
                    break
                for _k in (_p.get('api_keys') or []):
                    if (isinstance(_k, str) and _k.strip()) or \
                            (isinstance(_k, dict) and (_k.get('key') or '').strip()):
                        _has_key = True
                        break
                if _has_key:
                    break
        except Exception:
            pass
    if _has_key:
        ok("at least one LLM credential is configured")
    else:
        warn("no LLM API key found (env / .env / server_config) — the server "
             "is up but chat will not answer until you add one in "
             "Settings → Providers")

    # 6. Optional browser engine (JS-rendered page fetching)
    #
    # `import playwright` is NOT evidence the browser works: it stays green when
    # Chromium cannot launch at all (missing libatk/libnss → the binary dies
    # instantly), and ALSO when Chromium launches but has zero fonts, which
    # renders every glyph as nothing so screenshots come back blank-but-styled.
    # Both were live defects here. Actually launch it and measure a glyph.
    try:
        import playwright  # noqa: F401
    except ImportError:
        warn("playwright not importable — JS-rendered page fetching disabled (optional)")
    else:
        try:
            from playwright.sync_api import sync_playwright
            # Chromium is a CHILD process: it needs the env's lib dir on
            # LD_LIBRARY_PATH (libatk/libnss) and, on hosts without /etc/fonts,
            # FONTCONFIG_* pointing at the env's config — or it either refuses
            # to start or renders zero glyphs. chromium_env resolves both from
            # sys.prefix, so this works with no .tofu_env.json marker (a bare
            # `python3 healthcheck.py`, a fresh clone, or an exported bundle);
            # the marker is only consulted as an extra candidate prefix.
            _diag = {}
            try:
                from chromium_env import describe_chromium_env, ensure_chromium_env
                _prefix = ''
                try:
                    import json as _j
                    with open(ROOT / '.tofu_env.json', encoding='utf-8') as _mf:
                        _prefix = (_j.load(_mf).get('env_prefix') or '')
                except Exception as _me:
                    logger.debug('no .tofu_env.json marker (fine): %s', _me)
                ensure_chromium_env(env_prefix=_prefix)
                _diag = describe_chromium_env(env_prefix=_prefix)
            except Exception as _ce:
                logger.debug('chromium_env unavailable: %s', _ce)
            with sync_playwright() as _pw:
                _br = _pw.chromium.launch(headless=True, args=['--no-sandbox'])
                try:
                    _pg = _br.new_page()
                    _pg.set_content('<h1>tofu</h1>')
                    _gw = _pg.evaluate(
                        "(()=>{const c=document.createElement('canvas')"
                        ".getContext('2d');c.font='60px sans-serif';"
                        "return c.measureText('tofu').width;})()")
                finally:
                    _br.close()
            if _gw and _gw > 0:
                _exe = (_diag or {}).get('executable') or ''
                _which = ''
                if _exe:
                    _which = (' [headless shell]' if 'headless' in _exe
                              else ' [full build]')
                ok(f"browser engine launches and renders text "
                   f"(glyph width {_gw:.0f}px){_which}")
                if _exe:
                    print(f"      binary: {_exe}")
            else:
                fail("Chromium launches but renders NO text (zero fonts) — "
                     "screenshots will come back blank-but-styled. Install "
                     "fontconfig + a font family into the env "
                     "(conda install -c conda-forge fontconfig font-ttf-dejavu-sans-mono)")
        except Exception as _e:
            _msg = str(_e).replace('\n', ' ')[:200]
            # Report the RESOLVED cause when we know it, not just the symptom:
            # "cannot launch" alone sends people hunting the wrong thing.
            _hint = '; '.join((_diag or {}).get('issues') or [])
            fail(f"playwright imports but Chromium cannot launch — browser "
                 f"screenshots unavailable: {_msg}"
                 + (f" [diagnosis: {_hint}]" if _hint else ""))

    print(f"\n{C.BOLD}{'═'*60}{C.END}")
    if errors:
        print(f"{C.BOLD}  RESULT: {C.FAIL} {len(errors)} error(s), {len(warnings)} warning(s){C.END}")
        sys.exit(1)
    elif warnings:
        print(f"{C.BOLD}  RESULT: {C.WARN} 0 errors, {len(warnings)} warning(s) — server usable{C.END}")
        sys.exit(0)
    else:
        print(f"{C.BOLD}  RESULT: {C.OK} SERVER HEALTHY{C.END}")
        sys.exit(0)


# ═══════════════════════════════════════════════════════════════════════
# 1. Python Syntax Check
# ═══════════════════════════════════════════════════════════════════════
section("1. Python Syntax Check")
py_files = []
# Only these top-level directories ship executable Python as part of Tofu.
# A user's project, a self-update rollback snapshot, or an audit worktree may
# legitimately sit beside them; a project health check must never compile the
# contents of those unrelated trees.
source_dirs = {
    'android', 'benchmarks', 'browser_extension', 'clients', 'deploy',
    'desktop', 'lib', 'promo', 'propaganda', 'routes', 'scripts', 'static',
    'tests', 'tofu', 'tools',
}
skip_dirs = {
    '.git', '__pycache__', 'node_modules', 'debug', 'analysis_scripts',
    'offline_pkgs', 'logs', '.project_sessions', '.project_indexes',
    '.chatui', '.pytest_cache', '.ruff_cache', '.tofu', '.tofu_cache_probe',
    '.tofu_chrome_deps', '.tofu_trash', '.icon-backups', 'uploads',
    'overleaf_cache', 'build', 'dist',
}
# Install/update recovery and audit tooling deliberately leave whole worktree
# snapshots beside the source tree. They are not Tofu source, and walking a
# node_modules backup or nested benchmark checkout across shared/FUSE storage
# can turn this health check from seconds into minutes (or make it appear
# hung). Match generated families, not just today's timestamped directory.
skip_dir_prefixes = ('node_modules.', 'node_modules_', 'swebench_',
                     'abtest_', 'remote:')
for root, dirs, files in os.walk('.'):
    if root == '.':
        dirs[:] = [d for d in dirs if d in source_dirs]
    else:
        dirs[:] = [
            d for d in dirs
            if d not in skip_dirs
            and not d.startswith(skip_dir_prefixes)
            and not d.endswith('_workdir')
        ]
    for f in files:
        if f.endswith('.py'):
            py_files.append(os.path.join(root, f))

syntax_errors = []
for path in py_files:
    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as e:
        syntax_errors.append(str(e))
    except Exception as e:
        logger.debug('Unexpected error compiling %s', path, exc_info=True)
        syntax_errors.append(f"{path}: {type(e).__name__}: {e}")

if syntax_errors:
    for e in syntax_errors:
        fail(f"Syntax error: {e}")
else:
    ok(f"All {len(py_files)} .py files pass syntax check")


# ═══════════════════════════════════════════════════════════════════════
# 2. Top-Level Imports (Server Bootstrap)
# ═══════════════════════════════════════════════════════════════════════
section("2. Top-Level Imports")

tl_checks = [
    ("lib.database",      ["get_db", "close_db", "init_db"]),
    ("lib",               ["LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"]),
    ("lib.llm",           ["chat", "build_body", "stream_chat"]),
    ("lib.memory",        ["list_memories", "create_memory", "update_memory", "delete_memory", "toggle_memory"]),
    ("lib.browser",       ["wait_for_commands", "mark_poll", "resolve_batch",
                           "resolve_command", "is_extension_connected", "send_browser_command"]),
    # search/fetch were extracted into the standalone tofu_search package
    # (consumed via lib/search_bridge.py); the public entrypoint lives there.
    ("tofu_search",       ["perform_web_search"]),
    ("lib.pricing",       ["get_pricing_data"]),
    ("lib.tasks_pkg",     ["tasks", "tasks_lock", "create_task", "cleanup_old_tasks", "run_task"]),
    ("lib.project_mod",   ["set_project", "clear_project", "get_state", "get_project_path",
                           "get_recent_projects", "save_recent_project", "clear_recent_projects",
                           "tool_list_dir", "tool_read_files", "tool_grep", "tool_find_files",
                           "tool_write_file", "tool_apply_diff", "tool_run_command",
                           "tool_create_project",
                           "execute_tool", "browse_directory",
                           "get_context_for_prompt",
                           "get_modifications", "undo_conv_modifications"]),
]

for module_name, names in tl_checks:
    try:
        mod = importlib.import_module(module_name)
        missing = [n for n in names if not hasattr(mod, n)]
        if missing:
            fail(f"{module_name}: missing exports: {missing}")
        else:
            ok(f"{module_name} — {len(names)} exports verified")
    except Exception as e:
        logger.debug('Import failed for %s', module_name, exc_info=True)
        fail(f"{module_name}: import failed — {e}")

# Blueprint loading
try:
    from routes import ALL_BLUEPRINTS
    ok(f"All {len(ALL_BLUEPRINTS)} Quart blueprints imported")
except Exception as e:
    logger.debug('Blueprint import failed', exc_info=True)
    fail(f"routes/__init__.py: {e}")


# ═══════════════════════════════════════════════════════════════════════
# 3. Lazy Import Audit (in-function imports across routes/)
# ═══════════════════════════════════════════════════════════════════════
section("3. Lazy Import Audit (routes/)")

lazy_imports = []
for root, dirs, files in os.walk('routes'):
    for f in files:
        if not f.endswith('.py') or f == '__init__.py':
            continue
        path = os.path.join(root, f)
        with open(path) as fh:
            try:
                tree = ast.parse(fh.read(), filename=path)
            except SyntaxError as syn_err:
                logger.debug('SyntaxError in %s at line %s', path,
                             getattr(syn_err, 'lineno', '?'), exc_info=True)
                warn(f"{path}: SyntaxError — skipped in lazy import scan (should be caught by section 1)")
                continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom):
                        names = [a.name for a in child.names]
                        level = child.level
                        if level > 0:
                            # Resolve relative to the file's real package.
                            # ``routes/api_v1/foo.py: from .auth`` means
                            # routes.api_v1.auth, not routes.auth.
                            parts = Path(path).with_suffix('').parts
                            package = '.'.join(parts[:-1])
                            relative = '.' * level + (child.module or '')
                            try:
                                full_module = importlib.util.resolve_name(
                                    relative, package)
                            except (ImportError, ValueError):
                                full_module = relative
                        else:
                            full_module = child.module or ''
                        lazy_imports.append((path, node.name, child.lineno, full_module, names))

lazy_errors = 0
for filepath, func, lineno, module, names in lazy_imports:
    try:
        mod = importlib.import_module(module)
        for name in names:
            if name == '*' or hasattr(mod, name):
                continue
            # Python's ``from package import child`` also tries to import a
            # child submodule even when package.__init__ does not eagerly
            # expose it. Mirror that behavior before declaring the import
            # broken (lib.presence, lib.boot_identity, ... are all valid).
            try:
                importlib.import_module(f'{module}.{name}')
            except ModuleNotFoundError as e:
                fail(f"{filepath}:{lineno} in {func}() — "
                     f"{module}.{name} does NOT exist ({e})")
                lazy_errors += 1
            except Exception as e:
                fail(f"{filepath}:{lineno} in {func}() — "
                     f"{module}.{name} import failed: {e}")
                lazy_errors += 1
    except ModuleNotFoundError as e:
        logger.debug('Module import failed: %s', module, exc_info=True)
        fail(f"{filepath}:{lineno} in {func}() — from {module} import {names}: {e}")
        lazy_errors += 1
    except Exception as e:
        logger.debug('Unexpected error importing %s', module, exc_info=True)
        fail(f"{filepath}:{lineno} in {func}() — from {module} import {names}: {e}")
        lazy_errors += 1

if lazy_errors == 0:
    ok(f"All {len(lazy_imports)} lazy imports verified")


# ═══════════════════════════════════════════════════════════════════════
# 4. Database Schema Check
# ═══════════════════════════════════════════════════════════════════════
section("4. Database Schema")

required_tables = [
    'users', 'conversations', 'task_results', 'pricing_cache',
    'recent_projects',
]

# Core tables are now declared ONCE, declaratively, on lib/database/_core_schema.py's
# private SQLAlchemy MetaData (define_table → ddl_for compiles the per-backend
# CREATE TABLE at install time). The old per-backend literal DDL strings in
# _schema_{pg,sqlite}.py are gone, so check the declarative registry — the real
# source of truth — instead of grepping for "CREATE TABLE IF NOT EXISTS <name>".
# Optional-domain tables (e.g. trading_*) live in their plugin package now and
# are intentionally NOT required here.
try:
    from lib.database import _core_schema
    _defined_tables = set(_core_schema.metadata.tables.keys())
except Exception as e:
    logger.warning('Failed to load _core_schema: %s', e, exc_info=True)
    fail(f"Cannot import lib.database._core_schema: {e}")
    _defined_tables = None

if _defined_tables is not None:
    for table in required_tables:
        if table in _defined_tables:
            ok(f"Table '{table}' defined in _core_schema")
        else:
            fail(f"Table '{table}' NOT defined in _core_schema metadata")


# ═══════════════════════════════════════════════════════════════════════
# 5. Static Vendor Files
# ═══════════════════════════════════════════════════════════════════════
section("5. Static Vendor Files")

vendor_files = {
    'static/vendor/marked.min.js':         30000,
    'static/vendor/purify.min.js':         15000,
    'static/vendor/highlight.min.js':      50000,
    'static/vendor/github-dark.min.css':   500,
    'static/vendor/katex/katex.min.js':    100000,
    'static/vendor/katex/katex.min.css':   10000,
    'static/vendor/pdf.min.js':            100000,
    'static/vendor/pdf.worker.min.js':     100000,
    'static/vendor/google-fonts-local.css': 500,
}

for path, min_size in vendor_files.items():
    p = ROOT / path
    if not p.exists():
        fail(f"MISSING: {path}")
    else:
        sz = p.stat().st_size
        if sz < min_size:
            fail(f"{path}: suspiciously small ({sz} bytes, expected >{min_size})")
        else:
            ok(f"{path} ({sz:,} bytes)")

# Check KaTeX fonts
katex_font_dir = ROOT / 'static/vendor/katex/fonts'
if katex_font_dir.exists():
    font_count = len(list(katex_font_dir.glob('*.woff2')))
    if font_count >= 10:
        ok(f"KaTeX fonts: {font_count} .woff2 files")
    else:
        warn(f"KaTeX fonts: only {font_count} .woff2 files (expected ≥10)")
else:
    warn("KaTeX fonts directory missing — math rendering may have broken glyphs")


# ═══════════════════════════════════════════════════════════════════════
# 6. HTML Reference Check
# ═══════════════════════════════════════════════════════════════════════
section("6. HTML Asset References")

html_files = ['index.html']
src_href_re = re.compile(r'(?:src|href)=["\'](?!data:|#|javascript:|mailto:|https?://|//)(.*?)["\']')

for html_file in html_files:
    p = ROOT / html_file
    if not p.exists():
        warn(f"{html_file} not found")
        continue

    try:
        content = p.read_text()
    except Exception as e:
        logger.warning('Failed to read %s: %s', html_file, e, exc_info=True)
        fail(f"{html_file}: could not read file — {e}")
        continue
    refs = src_href_re.findall(content)
    broken = []
    for ref in refs:
        # Strip query params
        clean = ref.split('?')[0].split('#')[0]
        if not clean:
            continue
        target = ROOT / clean
        if not target.exists():
            broken.append(clean)

    if broken:
        for b in broken:
            fail(f"{html_file}: broken reference → {b}")
    else:
        ok(f"{html_file}: all {len(refs)} local refs resolve")


# ═══════════════════════════════════════════════════════════════════════
# 7. CDN Leak Detection
# ═══════════════════════════════════════════════════════════════════════
section("7. CDN Leak Detection")

cdn_patterns = [
    r'cdnjs\.cloudflare\.com',
    r'cdn\.jsdelivr\.net',
    r'unpkg\.com',
    r'fonts\.googleapis\.com',
    r'fonts\.gstatic\.com',
]
cdn_re = re.compile('|'.join(cdn_patterns))

scan_files = []
for ext in ('*.html', '*.css'):
    scan_files.extend(ROOT.glob(ext))
for ext in ('*.js', '*.ts'):
    scan_files.extend((ROOT / 'frontend/src').rglob(ext))
for ext in ('*.css',):
    scan_files.extend((ROOT / 'static/css').rglob(ext))

cdn_leaks = 0
for fp in scan_files:
    # Skip vendor directory — those files naturally contain internal references
    if 'vendor' in str(fp):
        continue
    try:
        content = fp.read_text(errors='ignore')
    except Exception as e:
        logger.warning('Failed to read %s: %s', fp, e, exc_info=True)
        fail(f"{fp.relative_to(ROOT)}: could not read file — {e}")
        continue
    for i, line in enumerate(content.split('\n'), 1):
        if cdn_re.search(line):
            # Ignore comments
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('/*') or stripped.startswith('*'):
                continue
            fail(f"{fp.relative_to(ROOT)}:{i} — CDN reference: {stripped[:120]}")
            cdn_leaks += 1

if cdn_leaks == 0:
    ok("No CDN references found in served files")


# ═══════════════════════════════════════════════════════════════════════
# 8. JS Defensive Guards
# ═══════════════════════════════════════════════════════════════════════
section("8. JS Defensive Guards")

# Rendering libraries are Vite-owned ESM imports. Verify that the owner module
# retains every package seam and that HTML shells do not load CDN scripts.
_guard_files = [
    'frontend/src/vendor-runtime.ts',
    'frontend/src/features/paper/pdf-viewer.ts',
    'frontend/src/runtime/app-runtime.js',
]
core_js_parts = []
for _gf in _guard_files:
    try:
        core_js_parts.append((ROOT / _gf).read_text())
    except FileNotFoundError:
        logger.debug('JS guard source not present (ok): %s', _gf)
    except Exception as e:
        logger.warning('Failed to read %s: %s', _gf, e, exc_info=True)

if not core_js_parts:
    fail("No JS guard source files found — cannot check JS defensive guards")
else:
    core_js = '\n'.join(core_js_parts)
    checks = {
        "Marked imported as ESM": r"from\s+['\"]marked['\"]",
        "Highlight.js imported as ESM": r"from\s+['\"]highlight\.js/lib/common['\"]",
        "DOMPurify imported as ESM": r"from\s+['\"]dompurify['\"]",
        "KaTeX remains a lazy import": r"import\(['\"]katex['\"]\)",
        "PDF.js remains a lazy import": r"import\(['\"]pdfjs-dist/legacy/build/pdf\.mjs['\"]\)",
        "html2canvas remains a lazy import": r"import\(['\"]html2canvas['\"]\)",
    }

    for desc, pattern in checks.items():
        if re.search(pattern, core_js):
            ok(desc)
        else:
            fail(f"frontend ESM owner: {desc} — owner NOT found")


# ═══════════════════════════════════════════════════════════════════════
# 9. Half-Overwritten Package Detection (site-packages integrity)
# ═══════════════════════════════════════════════════════════════════════
section("9. Half-Overwritten Package Detection")

# A second version installed on top of another without cleanup leaves the
# wrong files shadowing the intended ones (duplicate dist-info + orphaned
# .so shadowing a sibling .py). This env has hit that twice (scipy, pydantic).
try:
    from lib.env_health import scan_current_env
    env_issues = scan_current_env()
    errs = [i for i in env_issues if i.severity == 'error']
    warns = [i for i in env_issues if i.severity != 'error']
    if not errs:
        ok("No half-overwritten packages detected (no shadow .so)")
    for iss in errs:
        fail(f"{iss}  (paths: {', '.join(iss.paths[:4])}"
             f"{'…' if len(iss.paths) > 4 else ''})")
    # Lone duplicate dist-info is benign leftover metadata → warn, don't fail.
    if warns:
        warn(f"{len(warns)} package(s) have leftover duplicate dist-info dirs "
             f"(harmless unless paired with a shadow .so): "
             f"{', '.join(w.package for w in warns[:10])}"
             f"{'…' if len(warns) > 10 else ''}")
except Exception as e:
    logger.debug('env_health scan failed', exc_info=True)
    warn(f"env_health scan could not run — {e}")


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{C.BOLD}{'═'*60}{C.END}")
if errors:
    print(f"{C.BOLD}  RESULT: {C.FAIL} {len(errors)} error(s), {len(warnings)} warning(s){C.END}")
    print(f"{C.BOLD}{'═'*60}{C.END}")
    print("\nErrors:")
    for i, e in enumerate(errors, 1):
        print(f"  {i}. {e}")
    sys.exit(1)
elif warnings:
    print(f"{C.BOLD}  RESULT: {C.WARN} 0 errors, {len(warnings)} warning(s) — OK{C.END}")
    print(f"{C.BOLD}{'═'*60}{C.END}")
    sys.exit(0)
else:
    print(f"{C.BOLD}  RESULT: {C.OK} ALL CHECKS PASSED{C.END}")
    print(f"{C.BOLD}{'═'*60}{C.END}")
    sys.exit(0)
