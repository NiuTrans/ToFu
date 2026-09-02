#!/usr/bin/env python3
"""
healthcheck.py — Automated project diagnostics for tofu.

Run:  python3 healthcheck.py            — dev lint (source-tree checks)
      python3 healthcheck.py --runtime [--port N] [--wait SEC]
                                    [--require-browser]
                                        — probe a RUNNING server (post-install
                                          self-check: reachable? DB? index page?
                                          LLM key? browser engine?)
Exit code 0 = all green, 1 = issues found.

Checks:
  1. Python syntax          — All .py files compile
  2. Top-level imports      — Server + all blueprints load
  3. Lazy imports           — Every `from X import Y` inside route functions resolves
  4. Storage schema         — Required Sidecar tables are declared
  5. Static vendor files    — All local JS/CSS deps exist and are non-trivial
  6. HTML references        — Every src/href in HTML points to a real file
  7. CDN leak detection     — No external CDN URLs remain in served files
  8. JS defensive guards  — Core JS libraries have typeof guards
"""

import ast
import argparse
import importlib
import importlib.util
import logging
import os
import py_compile
import re
import sys
from pathlib import Path

from tofu_dotenv import read_dotenv_values


def _cli_port(value: str) -> int:
    """Parse a TCP port without silently turning typos into port 15000."""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('must be an integer from 1 to 65535') from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('must be an integer from 1 to 65535')
    return port


def _cli_wait(value: str) -> float:
    """Parse a bounded, non-negative runtime-probe wait."""
    try:
        wait = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError('must be a non-negative number of seconds') from exc
    if wait < 0 or wait > 3600:
        raise argparse.ArgumentTypeError('must be between 0 and 3600 seconds')
    return wait


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='healthcheck.py',
        description=(
            'Check Tofu. With no flags, audit a source checkout; with '
            '--runtime, probe an already-running installation.'),
        epilog=(
            'Lifecycle and port diagnostics: python serverctl.py doctor. '
            'Managed logs: python serverctl.py logs.'),
    )
    parser.add_argument(
        '--runtime', action='store_true',
        help='probe the running server instead of auditing source files')
    parser.add_argument(
        '--port', type=_cli_port, metavar='N',
        help='runtime server port (default: PORT, project .env, or 15000)')
    parser.add_argument(
        '--wait', type=_cli_wait, metavar='SECONDS',
        help='wait up to this long for runtime startup (default: 0)')
    parser.add_argument(
        '--require-browser', action='store_true',
        help='treat an unavailable browser engine as a runtime failure')
    return parser


# Parse before changing directories, loading project modules, or running any
# checks. `--help` and invalid arguments are therefore fast and side-effect
# free, which matters for both humans and automation that probe a CLI first.
_CLI_PARSER = _build_cli_parser()
_CLI_ARGS = _CLI_PARSER.parse_args()
if not _CLI_ARGS.runtime and (
        _CLI_ARGS.port is not None or _CLI_ARGS.wait is not None
        or _CLI_ARGS.require_browser):
    _CLI_PARSER.error('--port, --wait, and --require-browser require --runtime')

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
_USE_COLOR = bool(getattr(sys.stdout, 'isatty', lambda: False)()) \
    and 'NO_COLOR' not in os.environ


class C:
    OK   = '\033[92m✓\033[0m' if _USE_COLOR else '✓'
    FAIL = '\033[91m✗\033[0m' if _USE_COLOR else '✗'
    WARN = '\033[93m⚠\033[0m' if _USE_COLOR else '⚠'
    BOLD = '\033[1m' if _USE_COLOR else ''
    END  = '\033[0m' if _USE_COLOR else ''

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
    import ssl as _ssl
    import time as _time
    import urllib.request as _urlreq

    try:
        _project_dotenv = read_dotenv_values(ROOT / '.env')
    except OSError as _dotenv_error:
        _project_dotenv = {}
        warn(f"project .env could not be read: {_dotenv_error}")

    if _CLI_ARGS.port is not None:
        _port = _CLI_ARGS.port
    else:
        _raw_port = os.environ.get('PORT', '') \
            or _project_dotenv.get('PORT', '') or '15000'
        try:
            _port = _cli_port(_raw_port)
        except argparse.ArgumentTypeError as _exc:
            _CLI_PARSER.error(f'invalid PORT={_raw_port!r}: {_exc}')
    _wait = _CLI_ARGS.wait if _CLI_ARGS.wait is not None else 0.0
    _raw_tls = (os.environ.get('TOFU_TLS') if 'TOFU_TLS' in os.environ
                else _project_dotenv.get('TOFU_TLS', ''))
    _prefer_tls = str(_raw_tls).strip().lower() in {'1', 'true', 'yes', 'on'}
    _schemes = ('https', 'http') if _prefer_tls else ('http', 'https')
    _candidate_bases = [f'{scheme}://127.0.0.1:{_port}' for scheme in _schemes]
    _base = _candidate_bases[0]

    # Tofu's direct HTTPS mode commonly uses its generated local certificate.
    # This context is used only for the fixed loopback target above; runtime
    # diagnostics never disable verification for an external request.
    _loopback_tls_context = _ssl.create_default_context()
    _loopback_tls_context.check_hostname = False
    _loopback_tls_context.verify_mode = _ssl.CERT_NONE

    section(f"Runtime Probe — server on port {_port}")

    def _get_from(base, path, timeout=4):
        try:
            kwargs = {'context': _loopback_tls_context} \
                if base.startswith('https://') else {}
            with _urlreq.urlopen(base + path, timeout=timeout, **kwargs) as r:
                return r.status, r.read()
        except Exception:
            return None, None

    def _get(path, timeout=4):
        return _get_from(_base, path, timeout=timeout)

    # 1. /api/health — optionally polling until the server finishes booting
    #    (imports + DB init + first bundle build take a few seconds).
    _deadline = _time.monotonic() + _wait
    _status = _body = None
    while True:
        for _candidate_base in _candidate_bases:
            _status, _body = _get_from(_candidate_base, '/api/health')
            if _status == 200:
                _base = _candidate_base
                break
        if _status == 200 or _time.monotonic() >= _deadline:
            break
        _time.sleep(2)

    if _status != 200:
        _targets = ' or '.join(base + '/api/health' for base in _candidate_bases)
        fail(f"server not answering {_targets}"
             + (f" after {_wait:.0f}s wait" if _wait else ""))
        print(f"\n{C.BOLD}  RESULT: {C.FAIL} server unreachable{C.END}")
        sys.exit(1)

    _health = {}
    try:
        _health = _json.loads(_body.decode('utf-8', 'replace'))
    except Exception as e:
        fail(f"/api/health returned non-JSON: {e}")

    ok(f"server reachable at {_base} (version {_health.get('version', '?')}, "
       f"bootId {str(_health.get('bootId', '?'))[:8]})")

    # 2. Required storage authority.  The nested snapshot is the sole health
    # contract: accepting historical top-level database projections could let
    # an old/stale field mask a fenced or restarting Sidecar.
    _storage = _health.get('storage') or {}
    if _storage.get('ready') is True:
        ok(f"storage sidecar ready ({_storage.get('backend', '?')}, "
           f"pid {_storage.get('pid', '?')})")
    else:
        fail(f"storage sidecar NOT ready (state={_storage.get('state', '?')}): "
             f"{_storage.get('last_error', 'unknown') or 'unknown'}")

    # 3. Index page actually serves HTML (bundle injection / static serving)
    _s2, _b2 = _get('/')
    if _s2 == 200 and _b2 and b'<html' in _b2[:2000].lower():
        ok("index page serves HTML")
    else:
        fail(f"index page did not serve HTML (status={_s2})")

    # 5. At least one LLM credential somewhere the server reads:
    #    env vars → .env → server_config providers (api_keys or oauth slot).
    _has_key = bool(os.environ.get('LLM_API_KEY') or os.environ.get('LLM_API_KEYS'))
    if not _has_key:
        _has_key = bool(
            _project_dotenv.get('LLM_API_KEY')
            or _project_dotenv.get('LLM_API_KEYS'))
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
    _browser_issue = fail if _CLI_ARGS.require_browser else warn
    try:
        import playwright  # noqa: F401
    except ImportError:
        _browser_issue(
            "playwright not importable — JS-rendered page fetching disabled")
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
                _browser_issue(
                    "Chromium launches but renders NO text (zero fonts) — "
                    "screenshots will come back blank-but-styled. Install "
                    "fontconfig + a font family into the env "
                    "(conda install -c conda-forge "
                    "fontconfig font-ttf-dejavu-sans-mono)")
        except Exception as _e:
            _msg = str(_e).replace('\n', ' ')[:200]
            # Report the RESOLVED cause when we know it, not just the symptom:
            # "cannot launch" alone sends people hunting the wrong thing.
            _hint = '; '.join((_diag or {}).get('issues') or [])
            _browser_issue(
                f"playwright imports but Chromium cannot launch — browser "
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
    ("lib.storage",       ["get_storage_client", "start_storage", "stop_storage"]),
    ("lib",               ["LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"]),
    ("lib.llm",           ["chat", "build_body", "stream_chat"]),
    ("lib.memory.storage", ["list_memories", "create_memory", "update_memory", "delete_memory", "toggle_memory"]),
    ("lib.browser.queue", ["wait_for_commands", "mark_poll", "resolve_batch",
                           "resolve_command", "is_extension_connected", "send_browser_command"]),
    # search/fetch were extracted into the standalone tofu_search package
    # (consumed via lib/search_bridge.py); the public entrypoint lives there.
    ("tofu_search",       ["perform_web_search"]),
    ("lib.pricing",       ["get_pricing_data"]),
    ("lib.tasks_pkg.manager", ["chat_task_runtime", "create_task", "cleanup_old_tasks"]),
    ("lib.tasks_pkg.orchestrator.api", ["run_task"]),
    ("lib.tasks_pkg.spawn", ["spawn_task", "set_agent_executor", "set_serving_loop"]),
    ("lib.project_mod",   ["set_project", "clear_project", "get_state", "get_project_path",
                           "get_recent_projects", "save_recent_project", "clear_recent_projects",
                           "tool_list_dir", "tool_read_files", "tool_grep", "tool_find_files",
                           "tool_write_file", "tool_apply_diff", "tool_run_command",
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
# 4. Storage Schema Contract
# ═══════════════════════════════════════════════════════════════════════
section("4. Storage Schema")

required_tables = [
    'storage_command_receipts', 'storage_events', 'storage_conversations',
    'storage_conversation_turns', 'tenant_users',
    'storage_knowledge_documents',
]

try:
    from lib.storage_sidecar.schema import declared_table_names
    _defined_tables = declared_table_names()
except Exception as e:
    logger.warning('Failed to load Sidecar schema catalogue: %s', e, exc_info=True)
    fail(f"Cannot import Sidecar schema catalogue: {e}")
    _defined_tables = None

if _defined_tables is not None:
    for table in required_tables:
        if table in _defined_tables:
            ok(f"Table '{table}' declared by the Sidecar schema")
        else:
            fail(f"Table '{table}' NOT declared by the Sidecar schema")


# ═══════════════════════════════════════════════════════════════════════
# 5. Static Vendor Files
# ═══════════════════════════════════════════════════════════════════════
section("5. Static Vendor Files")

# marked/purify/highlight/html2canvas/pdfjs, the code-highlight theme and the
# @font-face stack all ship inside the content-hashed Vite graph now
# (frontend/src → static/vite/, guarded by lib/vite_assets.validate_vite_artifact).
# Only the KaTeX pair remains a hand-vendored file because the standalone
# artifact export view (routes/artifacts.py) loads it directly.
vendor_files = {
    'static/vendor/katex/katex.min.js':    100000,
    'static/vendor/katex/katex.min.css':   10000,
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
        "Highlight.js imported as ESM": r"from\s+['\"]highlight\.js/lib/core['\"]",
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
