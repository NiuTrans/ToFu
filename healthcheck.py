#!/usr/bin/env python3
"""
healthcheck.py — Automated project diagnostics for chatui.

Run:  python3 healthcheck.py
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

import ast, os, sys, re, json, importlib, logging, py_compile
from pathlib import Path

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.WARNING)

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

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
# 1. Python Syntax Check
# ═══════════════════════════════════════════════════════════════════════
section("1. Python Syntax Check")
py_files = []
skip_dirs = {'.git', '__pycache__', 'node_modules', 'debug', 'analysis_scripts',
             'offline_pkgs', 'logs', '.project_sessions', '.chatui', 'uploads'}
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
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
    ("lib.llm_client",    ["chat", "build_body", "stream_chat"]),
    ("lib.skills",        ["list_skills", "create_skill", "update_skill", "delete_skill", "toggle_skill"]),
    ("lib.browser",       ["wait_for_commands", "mark_poll", "resolve_batch",
                           "resolve_command", "is_extension_connected", "send_browser_command"]),
    ("lib.search",        ["perform_web_search"]),
    ("lib.pricing",       ["get_pricing_data"]),
    ("lib.trading",          ["fetch_asset_info", "get_latest_price", "search_asset"]),
    ("lib.trading_autopilot", ["get_autopilot_state", "set_autopilot_enabled", "run_autopilot_cycle"]),
    ("lib.tasks_pkg",     ["tasks", "tasks_lock", "create_task", "cleanup_old_tasks", "run_task"]),
    ("lib.project_mod",   ["set_project", "clear_project", "get_state", "get_project_path",
                           "get_recent_projects", "save_recent_project", "clear_recent_projects",
                           "tool_list_dir", "tool_read_file", "tool_grep", "tool_find_files",
                           "tool_write_file", "tool_apply_diff", "tool_run_command",
                           "execute_tool", "browse_directory",
                           "start_indexing", "get_context_for_prompt",
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
    ok(f"All {len(ALL_BLUEPRINTS)} Flask blueprints imported")
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
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.ImportFrom) and child.module:
                        names = [a.name for a in child.names]
                        level = child.level
                        if level > 0:
                            full_module = 'routes.' + child.module
                        else:
                            full_module = child.module
                        lazy_imports.append((path, node.name, child.lineno, full_module, names))

lazy_errors = 0
for filepath, func, lineno, module, names in lazy_imports:
    try:
        mod = importlib.import_module(module)
        for name in names:
            if not hasattr(mod, name):
                fail(f"{filepath}:{lineno} in {func}() — {module}.{name} does NOT exist")
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
    'users', 'conversations', 'task_results', 'pricing',
    'recent_projects', 'trading_holdings', 'trading_transactions',
    'trading_intel_cache', 'trading_strategies',
]

# Schema definitions moved from lib/database.py → lib/database/_schema.py
_schema_file = 'lib/database/_schema.py'
try:
    with open(_schema_file) as f:
        db_source = f.read()
    for table in required_tables:
        if f"CREATE TABLE IF NOT EXISTS {table}" in db_source:
            ok(f"Table '{table}' defined in schema")
        else:
            fail(f"Table '{table}' NOT found in schema")
except Exception as e:
    logger.warning('Failed to read %s: %s', _schema_file, e, exc_info=True)
    fail(f"Cannot read {_schema_file}: {e}")


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

html_files = ['index.html', 'trading.html']
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
for ext in ('*.js',):
    scan_files.extend((ROOT / 'static/js').rglob(ext))
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

try:
    core_js = (ROOT / 'static/js/core.js').read_text()
except FileNotFoundError:
    fail("static/js/core.js not found — cannot check JS defensive guards")
    core_js = None
except Exception as e:
    logger.warning('Failed to read static/js/core.js: %s', e, exc_info=True)
    fail(f"static/js/core.js: could not read file — {e}")
    core_js = None

if core_js is not None:
    checks = {
        "marked.setOptions guarded":    r"typeof\s+marked\s*!==?\s*['\"]undefined['\"]\s*\)\s*marked\.setOptions",
        "renderMarkdown has fallback":  r"typeof\s+marked\s*===?\s*['\"]undefined['\"][\s\S]*?return\s+['\"]?<pre",
        "hljs usage guarded":          r"typeof\s+hljs\s*===?\s*['\"]undefined['\"]",
        "katex usage guarded":         r"typeof\s+katex\s*!==?\s*['\"]undefined['\"]",
        "DOMPurify usage guarded":     r"typeof\s+DOMPurify\s*!==?\s*['\"]undefined['\"]",
    }

    for desc, pattern in checks.items():
        if re.search(pattern, core_js):
            ok(desc)
        else:
            fail(f"core.js: {desc} — guard NOT found")


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════
print(f"\n{C.BOLD}{'═'*60}{C.END}")
if errors:
    print(f"{C.BOLD}  RESULT: {C.FAIL} {len(errors)} error(s), {len(warnings)} warning(s){C.END}")
    print(f"{C.BOLD}{'═'*60}{C.END}")
    print(f"\nErrors:")
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
