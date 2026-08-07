#!/usr/bin/env python3
"""export.py — Project export with three-level sanitization.

Usage:
  python3 export.py --mode personal --dest /path/to/output   # self-use copy
  python3 export.py --mode internal --dest /path/to/output
  python3 export.py --mode opensource --dest /path/to/output
  python3 export.py --mode personal                          # defaults to ../tofu-personal
  python3 export.py --mode internal                          # defaults to ../tofu-internal
  python3 export.py --mode opensource                        # defaults to ../tofu-opensource
  python3 export.py --dry-run --mode opensource              # preview only, no copy

Sanitization levels:
  personal   — For yourself (another machine, backup, etc.)
               ✓ Keeps your config + credentials: API keys, providers,
                 MCP server tokens (data/config/), skills, uploads.
               ✗ Does NOT carry chat history: the PG cluster (data/pgdata/),
                 SQLite *.db files, and any SQL dumps (data/pg_backup.sql,
                 data/pg_backups/) are all skipped, so the destination boots
                 with a fresh empty database.
               ✗ Also skips bulky regenerable junk: __pycache__, .git,
                 node_modules, offline_pkgs, *.pyc, logs/, and other large
                 runtime artifacts.
               No other sanitization applied.

  internal   — For colleagues within the company.
               ✓ Keeps API keys, internal endpoints, Feishu credentials.
               ✗ Removes personal data: databases, chat history, skills,
                 logs, session caches, uploaded files, personal notes,
                 ~/.chatui references are cleaned (legacy filename retained for back-compat).

  opensource — For public release (GitHub, etc.)
               ✗ Removes ALL of the above, PLUS:
               ✗ Hardcoded API keys → placeholder
               ✗ Internal endpoints (aigc.sankuai.com) → env-var-only
               ✗ Feishu credentials → empty
               ✗ Internal domain references (.sankuai.com, .meituan.com)
               ✗ Internal model names (LongCat-*, aws.claude-*) retained
                 as working defaults (they're just model strings)
               ✗ Hardcoded absolute paths (/mnt/dolphinfs/...)
               ✗ Security audit report (reveals attack surface)
"""

import argparse
import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

# Central registry of agent-written project-local artifacts (.tofu*). Used so
# the export sanitizer recognises present + future artifacts by their reserved
# prefix instead of re-listing each name (see lib/agent_artifacts.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_artifacts import GITIGNORE_PATTERN as _AGENT_ARTIFACT_GLOB
from lib.agent_artifacts import is_agent_artifact

# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

ROOT = Path(__file__).resolve().parent

# ANSI — only emit color codes when writing to a real terminal. When stdout
# is piped/captured (e.g. run_command, CI, redirected to a file) the escape
# sequences would otherwise show up as literal "[2m…[0m" garbage, since
# nothing interprets them. Honor the NO_COLOR convention (https://no-color.org).
_USE_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
if _USE_COLOR:
    C_RED    = '\033[91m'
    C_GREEN  = '\033[92m'
    C_YELLOW = '\033[93m'
    C_CYAN   = '\033[96m'
    C_BOLD   = '\033[1m'
    C_DIM    = '\033[2m'
    C_END    = '\033[0m'
else:
    C_RED = C_GREEN = C_YELLOW = C_CYAN = C_BOLD = C_DIM = C_END = ''

# ── Logger ──
logger = logging.getLogger('export')
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# Set by _stage_head_snapshot: the exact commit the export copied. The
# post-copy integrity check must list files from THIS commit, not HEAD —
# a sibling commit landing mid-export would otherwise flag files the
# snapshot legitimately predates.
_EXPORT_SOURCE_SHA: str | None = None


# ═══════════════════════════════════════════════════════════
#  Version
# ═══════════════════════════════════════════════════════════

_VERSION_FILE = ROOT / 'VERSION'


def _read_version() -> str:
    """Read the current version from VERSION file."""
    try:
        return _VERSION_FILE.read_text(encoding='utf-8').strip()
    except Exception as e:
        logger.debug('[export] VERSION file read failed (%s), defaulting: %s',
                     _VERSION_FILE, e)
        return '0.0.0'


def _next_version(current: str, part: str = 'patch') -> str:
    """Compute the next version from ``current`` (pure — no file I/O).

    Args:
        current: The current version string (e.g. ``'0.13.0'``).
        part: 'major', 'minor', or 'patch'.

    Returns:
        The bumped version string.
    """
    parts = current.split('.')
    if len(parts) != 3:
        parts = ['0', '0', '0']
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

    if part == 'major':
        major += 1; minor = 0; patch = 0
    elif part == 'minor':
        minor += 1; patch = 0
    else:
        patch += 1

    return f'{major}.{minor}.{patch}'


def _bump_version(part: str = 'patch') -> str:
    """Bump version (major/minor/patch) and write back to VERSION file.

    Args:
        part: 'major', 'minor', or 'patch'.

    Returns:
        The new version string.
    """
    current = _read_version()
    new_ver = _next_version(current, part)
    _VERSION_FILE.write_text(new_ver + '\n', encoding='utf-8')
    logger.info('Version bumped: %s → %s', current, new_ver)
    return new_ver


def _commit_version_to_origin(version: str) -> None:
    """Commit the bumped ``VERSION`` file into THIS (origin) git tree.

    ``_bump_version`` writes the new number into the origin ``VERSION`` file,
    but only the *exported* copy was ever committed + tagged. That left the
    origin's VERSION change uncommitted and the published tag drifting ahead
    of origin's last commit — so the origin machine's own self-update checker
    would later see ``latest_tag > origin_VERSION`` and nag about an update it
    itself produced. Committing the bump here keeps origin and the published
    tag in lockstep.

    Commits ONLY the ``VERSION`` file (``git commit VERSION``), so any other
    in-progress edits in the origin tree are left untouched. Never raises —
    the export itself must not fail because the origin commit didn't go
    through; failures are logged and surfaced to the operator.

    Args:
        version: The new version string (without the leading ``v``).
    """
    if not (ROOT / '.git').exists():
        logger.info('Origin is not a git checkout — skipping VERSION commit '
                    '(VERSION file was still bumped to %s)', version)
        return

    def _git(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(['git', *cmd], cwd=str(ROOT),
                              capture_output=True, text=True)

    try:
        # Nothing staged/changed for VERSION? Then it's already committed
        # (e.g. a no-op re-run) — don't create an empty commit.
        diff = _git(['diff', '--quiet', '--', 'VERSION'])
        if diff.returncode == 0:
            logger.info('Origin VERSION already committed at %s — nothing to '
                        'commit', version)
            return
        commit = _git(['commit', 'VERSION', '-m', f'v{version}'])
        if commit.returncode != 0:
            err = (commit.stderr or commit.stdout or '').strip()
            logger.error('Failed to commit VERSION to origin: %s', err)
            print(f"  {C_YELLOW}\u26a0 Could not commit VERSION to origin "
                  f"(commit it manually): {err}{C_END}")
            return
        logger.info('Committed VERSION bump v%s to origin tree', version)
        print(f"  {C_GREEN}\u2713 Committed VERSION v{version} to origin{C_END}")
    except (FileNotFoundError, OSError) as e:
        logger.error('git unavailable while committing origin VERSION: %s', e)
        print(f"  {C_YELLOW}\u26a0 git unavailable — VERSION v{version} bumped "
              f"but not committed to origin{C_END}")


# ═══════════════════════════════════════════════════════════
#  Exclusion lists — what to NOT copy
# ═══════════════════════════════════════════════════════════

# ── Level 0: Personal — only skip bulky regenerable junk ──
PERSONAL_EXCLUDE_DIRS = {
    '__pycache__',                 # Python bytecode
    '.pytest_cache',               # pytest cache
    'build',                       # setuptools build artifacts (regenerable)
    'dist',                        # wheel/sdist build output (regenerable)
    'tofu.egg-info',               # setuptools egg metadata (regenerable)
    '.git',                        # git history (large, clone fresh is better)
    'node_modules',                # (shouldn't exist, but just in case)
    'offline_pkgs',                # offline pip packages
    '.migrate_backup',             # migration backups
    '.update_backup',              # self-update tarball backups (per-host safety net)
    '.project_indexes',            # project index caches (auto-rebuilt)
    'logs',                        # application logs (regenerable)
    # NOTE: pgdata/ holds the chat-history database and is NEVER copied —
    # personal mode intentionally ships config + credentials only, so the
    # destination boots with a fresh empty DB. (Raw-copying a live pgdata
    # across FUSE/NFS would also cause TOAST chunk corruption regardless.)
    'pgdata',                      # PG cluster directory (chat history — never copied)
    'pg_backups',                  # timestamped pg_dumpall SQL dumps (chat history)
    'sundries',                    # unrelated scripts/files (not part of tofu)
    'swebench_workdir',            # SWE-bench working directory (large, transient)
    'swebench_rerun_workdir',      # SWE-bench rerun results (thousands of venvs)
    'swebench_pilot_workdir',      # SWE-bench pilot working directory (transient)
    'swebench_glm_ab_workdir',     # SWE-bench GLM A/B working directory (transient)
    'swebench_full',               # SWE-bench full dataset working directory
    'abtest_workdir',              # A/B-test working directory (many conda envs + repos)
    'overleaf_cache',              # Overleaf project cache (~89MB, regenerable)
    'niumark',                     # unrelated project (not part of tofu)
    'outputs',                     # smoke-test / eval output (thousands of files, transient)
    'promo',                       # promotional slides/screenshots (~10MB, not runtime)
    'django',                      # stray empty dir (not part of tofu)
    '.tofu_trash',                 # trash/undo bin (transient, per-host)
    '_candidates',                 # raw AI-gen asset candidates + proof strips (review-only, multi-MB)
}

# Dirs excluded ONLY at the project root (not inside subdirs like lib/)
_TOP_LEVEL_ONLY_EXCLUDE_DIRS = {
    'tools',                       # separate project (md2cards), NOT lib/tools/
    'paper',                       # academic paper draft, NOT lib/paper/ (core package)
}

# Like _TOP_LEVEL_ONLY_EXCLUDE_DIRS but honored in internal + opensource modes
# (personal deliberately KEEPS its own data/, so it is NOT unioned into the
# personal sets). 'data' MUST be root-anchored: as a nested (path-component)
# exclusion it strips legitimate nested packages — the android/ subtree's
# Room package android/app/src/.../client/data/ lost 3 tracked files to it
# (the lib/paper drop-bug class). Root data/ (databases, configs, runtime
# state) stays fully excluded in both modes.
ALWAYS_EXCLUDE_ROOT_ONLY_DIRS = {
    'data',                        # databases (*.db), configs, runtime state — ROOT only
}

PERSONAL_EXCLUDE_GLOBS = {
    '*.pyc',
    '*.pyo',
    '*.log',                       # log files anywhere
}

PERSONAL_EXCLUDE_FILES = {
    'pg_backup.sql',               # staged PG restore dump (chat history)
    'nohup.out',
    'server.log',
    'server.log.1',
    'copy_pid.txt',
    'import_pid.txt',
    'import_status.txt',
    'import.log',
}

PERSONAL_EXCLUDE_GLOBS_HEAVY = {
    # Old/broken database files and SQL dumps (can be hundreds of MB)
    '*.db',
    '*.db-journal',
    '*.db-wal',
    '*.db-shm',
    '*.db.tmp',
    '*.db.tmp-journal',
    '*.db.corrupted_*',
    'dump.sql',
    'recover.sql',
}

# ── Level 1: Always excluded (internal & opensource) ──
# These contain personal data, chat history, runtime state
ALWAYS_EXCLUDE_DIRS = {
    # Personal data / runtime
    'logs',                        # application logs (contain queries, API calls, personal content)
    '.chatui',                     # skills, error resolutions (personal)
    '.tofu',                       # agent state: file-history undo store, skills, error resolutions (personal)
    '.project_sessions',           # session caches (contain conversation context)
    '.project_indexes',            # project index caches
    'uploads',                     # uploaded images (personal)
    '__pycache__',                 # Python bytecode
    '.pytest_cache',               # pytest cache
    '.ruff_cache',                 # ruff linter cache
    'build',                       # setuptools build artifacts (regenerable)
    'dist',                        # wheel/sdist build output (regenerable)
    'tofu.egg-info',               # setuptools egg metadata (regenerable)
    '.git',                        # git history (may contain old secrets)
    '.migrate_backup',             # migration backups
    '.update_backup',              # self-update tarball backups (per-host safety net)
    'node_modules',                # (shouldn't exist, but just in case)
    'offline_pkgs',                # offline pip packages
    'sundries',                    # unrelated scripts/files (not part of tofu)
    'swebench_workdir',            # SWE-bench working directory (large, transient)
    'swebench_rerun_workdir',      # SWE-bench rerun results (thousands of venvs)
    'swebench_pilot_workdir',      # SWE-bench pilot working directory (transient)
    'swebench_glm_ab_workdir',     # SWE-bench GLM A/B working directory (transient)
    'swebench_full',               # SWE-bench full dataset working directory
    'abtest_workdir',              # A/B-test working directory (many conda envs + repos)
    'overleaf_cache',              # Overleaf project cache (~89MB, regenerable)
    'niumark',                     # unrelated project (not part of tofu)
    'outputs',                     # smoke-test / eval output (thousands of files, transient)
    'promo',                       # promotional slides/screenshots (~10MB, not runtime)
    'django',                      # stray empty dir (not part of tofu)
    '.tofu_trash',                 # trash/undo bin (transient, per-host)
    '_candidates',                 # raw AI-gen asset candidates + proof strips (review-only, multi-MB)
}

ALWAYS_EXCLUDE_FILES = {
    # Export infra (contains sanitization patterns that reveal secrets / project intelligence)
    'export.py',
    'CLAUDE.md',
    # Legacy database file remnants (if any)
    'claude_dialogue.db.broken_20260324',
    'claude_dialogue.db.corrupt',
    'claude_dialogue.db.corrupt.bak',
    # Personal notes / scratch / research (moved to sundries/ but keep exclusion for safety)
    'rebuttal_2Bg4.md',
    'rebuttal_ruxi.md',
    'rebuttal_ruxi_fixed.md',
    'rebuttal_ysym.md',
    'rebuttal_ysym_fixed.md',
    'fix_openreview_latex.py',
    'split_long_image.py',
    'markdown-image-243521.png',
    'a.md',
    'o.md',
    'a.py',
    'output.md',
    'dump_errors.txt',
    'recover_errors.txt',
    'bench_1B.log',
    'bench_sgla',
    # Build artifacts
    'tofu-browser-extension.zip',
    'chatui-browser-extension.zip',  # legacy filename
    # Runtime state
    '.env',
    '.tofu_env.json',     # per-host conda env marker (absolute paths inside)
    'load_env.py',
    'copy_pid.txt',
    # NOTE: setup_bashrc.sh is intentionally NOT excluded here — it's kept for
    # personal + internal exports (proxy + conda auto-activate helper). It IS
    # stripped from opensource via OPENSOURCE_EXTRA_EXCLUDE_FILES (hardcoded
    # corp proxy IP must not ship publicly).
    'copy_to_dst.sh',     # per-host copy helper (hardcoded internal mount paths)
    'copy_to_chenxin.sh', # per-host copy helper (hardcoded internal mount paths)
    'server.log',
    'server.log.1',
    'prompt_output.txt',
    'nohup.out',
    # Scratch / personal artifacts
    'overleaf-mcp:cat_dog_manor.png',          # stray scratch image
    'claude_code_run_command_report.html',     # generated scratch report
    'thesis_references.bib',                   # personal academic file
    '解答260505.pdf',                          # scratch answer PDF (personal)
    '解答260505_v2.pdf',                       # scratch answer PDF (personal)
}

# Glob patterns always excluded
ALWAYS_EXCLUDE_GLOBS = {
    '*.pyc',
    '*.pyo',
    'bundle-*.js',    # auto-generated JS bundle (rebuilt at startup)
    'node_modules*',  # npm deps AND timestamped local backups (node_modules.local_bak.*) — regenerable, not Tofu source
    '*.db',
    '*.db-journal',
    '*.db-wal',
    '*.db-shm',
    '*.db.tmp',
    '*.db.tmp-journal',
    '*.db.corrupted_*',
    'dump.sql',
    '*.log',
    '.coverage',      # pytest coverage DB (regenerable)
    '*.bib',          # personal bibliography files
}

# Level 2: Additional exclusions for opensource only
OPENSOURCE_EXTRA_EXCLUDE_FILES = {
    # Security reports (now in docs/ — reveals attack surface)
    'SECURITY_AUDIT_REPORT.md',
    'RATE_LIMITING_DOS_AUDIT_REPORT.md',
    # Internal dev summaries / project intelligence (now in docs/)
    'BROWSER_ENHANCEMENTS_SUMMARY.md',
    'README_BROWSER_ENHANCEMENTS.md',
    'README_EXTENSION.md',
}

OPENSOURCE_EXTRA_EXCLUDE_DIRS = {
    'scripts',                  # bench_real_datasets.py etc. with hardcoded internal paths
    'benchmarks',               # benchmark scripts with internal API keys
    'outputs',                  # evaluation reports
    'posters',                  # personal scratch poster images (not part of the app)
    'debug',                    # standalone scratch/benchmark scripts (never imported by the
                                # app); 148 files that trip `ruff check .` in CI lint
}

# Files that live INSIDE an opensource-excluded dir but are still required by
# the public build (e.g. the desktop-installer CI calls
# ``python scripts/gen_desktop_icons.py``). The whole dir is excluded for
# safety, then these specific files are restored verbatim after the tar copy.
# Each entry MUST be a project-root-relative path that is verified clean of
# secrets/internal paths before being added here.
_OPENSOURCE_KEEP_FILES = {
    'scripts/gen_desktop_icons.py',   # CI icon generation (PIL only, no secrets)
    # Regenerates static/js/globals.generated.d.ts, the ambient type surface
    # for the concatenated-bundle global scope. CI runs it with --check
    # (tests/test_frontend_globals_generated.py), so the public build needs it
    # too. Pure regex scan of static/js -- no secrets, no internal paths.
    'scripts/gen_frontend_globals.py',
    # Derives docs/TOOL_INVENTORY.md — one row per tool × its facets (label,
    # approval enricher, gates, dispatch path). CI pins it with --check
    # (tests/test_tool_inventory_generated.py), so the public build needs the
    # generator. Introspective import of lib.tools.registry + tool_dispatch
    # tables -- no secrets, no internal paths.
    'scripts/gen_tool_inventory.py',
    # Test-suite health census (AST-only scan of tests/, no imports/execution).
    # CI gates on it via tests/test_suite_health_ratchet.py, so the public build
    # needs the tool too. Reads only tests/*.py + lib//routes source paths --
    # no secrets, no internal paths.
    'scripts/audit_tests.py',
    # Affected-test selection (P2-3, docs/TESTING_STRATEGY.md). Loaded by
    # tests/test_test_select.py via spec_from_file_location and driven by the
    # Makefile test-affected target — the public tree ships both consumers, so
    # it must ship the tool. Pure stdlib AST/git subprocess -- no secrets, no
    # internal paths.
    'scripts/test_select.py',
    # Ratchet funeral audit (P2-1). Loaded by tests/test_ratchet_incident_link.py
    # the same way, and regenerates docs/RATCHET_AUDIT.md. Pure stdlib regex
    # scan of tests/ -- no secrets, no internal paths.
    'scripts/ratchet_audit.py',
    # Builds the extension upload zip (dev or --store trimmed manifest). The
    # docs/chrome-web-store/ kit SHIPS in the opensource export and its
    # checklist step 2 is "bash scripts/package_extension.sh --store", so
    # without this the public tree documents a command it does not contain.
    # Pure REPO_ROOT-relative bash + zip -- no secrets, no internal paths.
    'scripts/package_extension.sh',
    # Server-side installer entry point. Same shape as the packaging script:
    # a documented command a user is told to run, so it must exist in both a
    # clean clone and the public tree. Pure bash -- no secrets, no internal
    # paths, no absolute host paths.
    'scripts/install_on_server.sh',
    # The single source of truth for "is a Tofu release complete?". Both gates
    # in .github/workflows/build-desktop.yml call it, and that workflow ships
    # in the opensource export — so without this the public tree would carry a
    # release pipeline that dies on a missing file the first time it runs.
    # Pure stdlib (argparse/fnmatch/json/pathlib), no secrets, no internal paths.
    'scripts/release_assets.py',
    # The single source of truth for "is this VERSION documented?". The version
    # gate in that same workflow calls it before any build, so it ships for the
    # same reason as release_assets.py above: the public tree would otherwise
    # carry a release pipeline that dies on a missing file the first time it
    # runs. Pure stdlib (argparse/re/pathlib), no secrets, no internal paths.
    'scripts/changelog_gate.py',
    # The CI wizard pixel probe (windows runner: build the TOFU_DIAG
    # installer + PrintWindow frames + BM_CLICK page turns). The shipped
    # .github/workflows/installer-ui-probe.yml drives it — without this the
    # public tree carries a workflow that dies on a missing file. Pure
    # stdlib + PIL ctypes, no secrets, no internal paths.
    'debug/win_ci_shot.py',
}

OPENSOURCE_EXTRA_EXCLUDE_FILES = OPENSOURCE_EXTRA_EXCLUDE_FILES | {
    'meituan.json',             # internal provider template with corp gateway URL
    'setup_bashrc.sh',          # per-host .bashrc helper (hardcoded corp proxy IP)
    # Unreferenced marketing assets in static/images/ (~12.4MB total) — the
    # runtime UI uses static/icons/ (onigiri.svg etc.); NOTHING references
    # these (verified 2026-08-01: zero project-wide hits for the filenames /
    # 'images/<name>' paths). Dead weight in every source tarball and clone.
    'tofu-cache-article-cover.png',
    'tofu-cache-article-cover-zh.png',
    'tofu-poster-core-strength.png',
    'tofu-poster-v2.png',
    'attach-icon.png',
    'attach-icon.svg',
    'onigiri-icon.png',
    'onigiri-icon.svg',
}


# ═══════════════════════════════════════════════════════════
#  Source code sanitization patterns (opensource only)
# ═══════════════════════════════════════════════════════════

# Hardcoded secrets to redact
_SECRETS = {
    # API keys
    '2003386484270948427':      'YOUR_API_KEY_HERE',
    '2031327690221944861':      'YOUR_API_KEY_HERE',
    # Feishu credentials
    'q2E1KDjuDhasILLK0urXCgqbss6vhmXf': '',
    'cli_a924e60a7cb85bcc':     '',
}

# Internal endpoints to replace
_ENDPOINTS = {
    'https://aigc.sankuai.com/v1/openai/native': 'https://api.openai.com/v1',
    'https://aigc.sankuai.com':                   'https://api.openai.com',
}

# Internal domain patterns (for proxy bypass code)
_INTERNAL_DOMAIN_LITERALS = [
    '.sankuai.com',
    '.meituan.com',
]

# Plain-substring internal identifiers scrubbed verbatim in opensource exports,
# each paired with its opensource-safe replacement. SINGLE SOURCE OF TRUTH: both
# the scrub (``_sanitize_source_opensource``) and the file-open trigger list
# (``_opensource_sanitize_triggers``) are driven from this tuple, so they can
# never drift. A drift would be a silent leak — a file whose secret IS scrubbed
# but whose trigger substring is missing never gets OPENED, so the scrub rule
# never runs. Add a new internal identifier HERE and it is both scrubbed and
# triggered automatically. (Endpoints/domains/paths handled by regex live in
# ``_ENDPOINTS`` / ``_INTERNAL_DOMAIN_LITERALS`` / the path regexes below.)
_INTERNAL_IDENTIFIER_REPLACEMENTS = (
    ('hadoop-aipnlp',              'your-user'),
    ('ruanjunhao04',              'your-username'),
    ('M-TransferContext-INF-CELL', 'X-Custom-Header'),
    ('gray-release-ai-gpt-test',  'your-header-value'),
)


def _sanitize_defaults_for_export(content: str, filepath: str,
                                    version: str = '') -> str:
    """Sanitize default settings for exported projects (internal & opensource).

    Changes the default Thinking Depth from 'medium' to 'off' so exported
    copies start with thinking disabled.  Also stamps the version into
    cache-bust parameters (?v=...) for consistent versioning.

    NOTE: The JS source already removed most ``|| 'medium'`` fallbacks in
    core.js and main.js (they rely on config.defaultThinkingDepth being
    always initialized).  The remaining targets are:
      - core.js: the single initialization line ``= 'medium'``
      - settings.js: fallbacks already use ``|| 'off'`` (no-op here)
      - model_config.py: server-side default ``'medium'``
      - index.html: ``<select>`` default ``selected`` attribute
    """
    # ── Version stamp: replace cache-bust ?v= parameters with the release version ──
    if version and (filepath.endswith('.html') or filepath.endswith('.js')):
        # Replace ?v=YYYYMMDD[a-z] style cache busters → ?v=0.5.0
        content = re.sub(r'\?v=\d{8}[a-z]?', f'?v={version}', content)
        # Also replace _ICON_V = 'YYYYMMDD[a-z]' in settings.js
        if filepath.endswith('settings.js'):
            content = re.sub(
                r"const _ICON_V = '[^']+';",
                f"const _ICON_V = '{version}';",
                content,
            )
    # ── JS: core.js — change the single init default from 'medium' to 'off' ──
    if filepath.endswith('core.js'):
        content = content.replace(
            "config.defaultThinkingDepth = 'medium';",
            "config.defaultThinkingDepth = 'off';",
        )
        # ── core.js — change hardcoded serverModel from opus to gpt-4o ──
        # The JS validation in main.js will auto-correct to an available model
        # on first load, but the initial value should be a reasonable default.
        content = content.replace(
            'let serverModel = "aws.claude-opus-4.7";',
            'let serverModel = "gpt-4o";',
        )

    # ── Python: model_config.py — change server-side default from 'medium' to 'off' ──
    if filepath.endswith('model_config.py'):
        content = content.replace(
            "_default_depth = cfg.get('defaultThinkingDepth', 'medium')",
            "_default_depth = cfg.get('defaultThinkingDepth', 'off')",
        )

    # ── HTML: index.html — change <select> default from medium to off ──
    if filepath.endswith('index.html'):
        # Robust against attribute reordering / extra attrs (e.g. data-i18n)
        # by using regex. Use ``[\w-]`` (NOT ``[a-z\-]``) so attribute names
        # containing hyphens like ``data-i18n`` match.
        # Pattern 1: strip ``selected`` from <option value="medium" …>.
        content = re.sub(
            r'(<option\s+value="medium"(?:\s+[\w-]+="[^"]*")*)\s+selected(\b)',
            r'\1\2',
            content, flags=re.IGNORECASE,
        )
        # Pattern 2: add ``selected`` to <option value="off" …> if not present.
        def _add_selected(m):
            if 'selected' in m.group(0):
                return m.group(0)
            return m.group(0).replace('value="off"', 'value="off" selected', 1)
        content = re.sub(
            r'<option\s+value="off"(?:\s+[\w-]+="[^"]*")*\s*>',
            _add_selected, content, flags=re.IGNORECASE,
        )

    return content


def _sanitize_source_opensource(content: str, filepath: str) -> str:
    """Apply all opensource sanitization to a source file's content.

    Order matters: replace longer strings first to avoid partial matches.
    """
    # 1. Replace hardcoded secrets
    for secret, replacement in _SECRETS.items():
        content = content.replace(secret, replacement)

    # 2. Replace internal endpoints (both code and comments)
    for endpoint, replacement in _ENDPOINTS.items():
        content = content.replace(endpoint, replacement)

    # 2b. Also clean bare domain references in comments/strings
    #     e.g. "aigc.sankuai.com" in comments
    content = re.sub(r'aigc\.sankuai\.com', 'your-llm-gateway.example.com', content)

    # 3. Clean _DEFAULT_KEYS list in lib/__init__.py
    if filepath.endswith('lib/__init__.py') or filepath.endswith('lib' + os.sep + '__init__.py'):
        content = re.sub(
            r"_DEFAULT_KEYS\s*=\s*\[.*?\]",
            "_DEFAULT_KEYS = [\n    # Add your API keys here, or set LLM_API_KEYS env var\n]",
            content,
            flags=re.DOTALL,
        )

    # 4. Clean bootstrap.py default API key (defense-in-depth; source already clean)
    if filepath.endswith('bootstrap.py'):
        content = content.replace(
            "api_keys = [single] if single else ['2003386484270948427']",
            "api_keys = [single] if single else []",
        )

    # 5. Proxy bypass is now fully auto-managed by lib/proxy.py — server.py
    #    no longer has _INTERNAL_DOMAINS hardcoded.  No sanitization needed.

    # 6. Clean model-specific comments in lib/model_info.py
    #    (Was previously in llm_client.py; moved with the 2026-05-21 split.)
    if filepath.endswith('model_info.py'):
        content = content.replace(
            'def is_longcat(model: str) -> bool:\n    """Internal LongCat models (Flash, MoE, etc.).',
            'def is_longcat(model: str) -> bool:\n    """LongCat models (Flash, MoE, etc.).',
        )

    # 6b. Clean _HEADER_MIGRATIONS in dispatcher.py — contains internal
    #     infrastructure routing details (header names + values)
    if 'dispatcher.py' in filepath:
        content = content.replace(
            "'sankuai.com': {'M-TransferContext-INF-CELL': 'gray-release-ai-gpt-test'},",
            "# 'your-domain.com': {'X-Custom-Header': 'value'},  # per-provider custom headers",
        )

    # 7. (migrate.py was removed — no longer needs sanitization)

    # 8. Clean hardcoded absolute paths (/mnt/dolphinfs/...)
    content = re.sub(
        r'/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/[^\s\'"]+',
        '/path/to/your/project',
        content,
    )
    content = re.sub(
        r'/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/[^\s\'"]+',
        '/path/to/your/data',
        content,
    )
    # Catch bare /mnt/dolphinfs/... references in docstrings/comments
    content = re.sub(
        r'/mnt/dolphinfs/[^\s\'"`]*',
        '/mnt/your-fs',
        content,
    )

    # 9. Clean FEISHU default project path in _state.py
    if '_state.py' in filepath and 'feishu' in filepath:
        content = content.replace(
            "os.path.expanduser('~/Projects/tofu')",
            "os.path.expanduser('~/tofu')",
        )

    # 10. Clean default provider ID 'sankuai' → 'default' across all files
    #     Defense-in-depth: source already uses 'default' provider ID
    content = content.replace("provider_id='sankuai'", "provider_id='default'")
    content = content.replace("provider_id: str = 'sankuai'", "provider_id: str = 'default'")
    content = content.replace("'id': 'sankuai'", "'id': 'default'")
    content = content.replace("'name': 'Meituan'", "'name': 'Default'")
    content = content.replace("prov_id = provider.get('id', 'sankuai')", "prov_id = provider.get('id', 'default')")
    content = content.replace("m.get('provider_id', 'sankuai')", "m.get('provider_id', 'default')")

    # 11. Clean settings.js brand detection and provider name display
    if filepath.endswith('settings.js'):
        content = re.sub(
            r"\[/sankuai\|longcat\|meituan\|三快\|龙猫/i,\s*'meituan'\],",
            "// [/your-org-pattern/i, 'your-org'],  // Add org detection pattern",
            content,
        )
        content = content.replace("meituan:'Meituan'", "default:'Default'")

    if filepath.endswith('main.js'):
        content = content.replace("meituan:'Meituan'", "default:'Default'")

    # 12. Clean .sankuai.com / .meituan.com domain literals in any context
    #     (comments, strings, etc.) — but not in regex patterns already
    #     handled above
    for domain in _INTERNAL_DOMAIN_LITERALS:
        content = content.replace(domain, '.internal.example.com')

    # 13. Clean plain-substring internal identifiers (usernames, HTTP header /
    #     gray-release tokens that leak via probe scripts + dispatcher header
    #     migrations). Driven from the single-source-of-truth tuple so the scrub
    #     and the file-open trigger list in _opensource_sanitize_triggers can
    #     never drift apart.
    for ident, replacement in _INTERNAL_IDENTIFIER_REPLACEMENTS:
        content = content.replace(ident, replacement)

    # 14. Strip internal-only MCP catalog entries from lib/mcp/registry.py.
    #     Entries inside the '# ── Meituan Internal' block reference
    #     internal-only services (hope CLI, Xuecheng/学城 docs, …) and
    #     must NOT appear in opensource exports. The block runs from the
    #     '# ── Meituan Internal' banner to (but not including) the next
    #     '    # ── ' section header at outer-dict (4-space) indent.
    if filepath.endswith('lib/mcp/registry.py') or filepath.endswith('lib' + os.sep + 'mcp' + os.sep + 'registry.py'):
        content = re.sub(
            r"\n    # ── Meituan Internal.*?─\n\n"
            r"(?:.*?\n)*?"            # any number of lines (entries)…
            r"(?=    # ── )",          # …up to (but not including) the next section header
            "\n",
            content,
        )
        # Belt-and-braces: hard-default the runtime build flag to True so any
        # ``internal_only`` entry that ever escapes the source strip above is
        # still filtered out by get_catalog() at runtime — independent of the
        # env var or entry ordering.
        content = content.replace(
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '').strip().lower() in {",
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '1').strip().lower() in {",
        )

    # 15. Clean remaining internal identifiers.
    #     'sankuai' (三快) is an internal corp name — always replace.
    #     Inside an identifier (word char on either side) the hyphenated form
    #     breaks Python/JS syntax — `def test_sankuai_…` became
    #     `def test_example-corp_…` and the exported file no longer parsed
    #     (bitten 2026-08-05, tests/test_gateway_sanitize.py). Identifiers get
    #     the underscore form; prose/domains keep the hyphen.
    content = re.sub(r'(?<=[A-Za-z0-9_])sankuai|sankuai(?=[A-Za-z0-9_])',
                     'example_corp', content)
    content = content.replace('sankuai', 'example-corp')
    content = content.replace('三快', 'your-corp')
    content = content.replace('龙猫', 'your-mascot')

    #     'meituan' / 'Meituan' is a public brand (HKEX:3690). Keep in brand
    #     icon registries, CSS selectors, and brand detection arrays — same
    #     category as 'openai', 'google'. Replace elsewhere as provider name.
    _is_brand_asset = (
        filepath.endswith('settings.js')
        or filepath.endswith('main.js')
        or filepath.endswith('.css')
    )
    if not _is_brand_asset:
        content = content.replace('Meituan', 'YourProvider')
        # NOTE: the replacement token MUST be a valid JS identifier. `meituan`
        # appears as an UNQUOTED object key (`meituan: '<svg…>'`) in brand
        # registries like static/js/settings/branding.js and
        # visibility_defaults.js. A hyphenated token (`your-provider`) turns
        # that into `your-provider:` which JS parses as subtraction
        # (`your - provider`) → "Uncaught SyntaxError: Unexpected token '-'",
        # white-screening the whole exported app. `yourprovider` is a bareword
        # key wherever `meituan` was, so the exported JS always parses.
        # (Do NOT gate this on a per-file allowlist — that list drifts on every
        # refactor and is exactly what orphaned the brand registries before.)
        content = content.replace('meituan', 'yourprovider')

    return content


# ═══════════════════════════════════════════════════════════
#  File copy logic
# ═══════════════════════════════════════════════════════════

def _should_exclude(relpath: str, filename: str, mode: str) -> str | None:
    """Check if a file/dir should be excluded. Returns reason or None."""
    parts = Path(relpath).parts

    # ── Agent-written artifacts (.tofu*) — all modes ──
    # Prefix-based via the central registry so CURRENT and FUTURE
    # ``.tofu``-prefixed artifacts (.tofu/, .tofu_trash/, .tofu_sandbox/,
    # .tofu_env.json, …) are always stripped without re-listing each name.
    for part in parts:
        if is_agent_artifact(part):
            return f'agent artifact: {part}'
    if is_agent_artifact(filename):
        return f'agent artifact: {filename}'

    # ── Top-level-only exclusions (all modes) ──
    if parts and parts[0] in _TOP_LEVEL_ONLY_EXCLUDE_DIRS:
        return f'excluded dir: {parts[0]} (top-level only)'

    # ── Personal mode: minimal exclusions ──
    if mode == 'personal':
        for part in parts:
            if part in PERSONAL_EXCLUDE_DIRS:
                return f'excluded dir: {part}'
        if filename in PERSONAL_EXCLUDE_FILES:
            return f'excluded file: {filename}'
        for glob_pat in PERSONAL_EXCLUDE_GLOBS:
            if _glob_match(filename, glob_pat):
                return f'excluded glob: {glob_pat}'
        for glob_pat in PERSONAL_EXCLUDE_GLOBS_HEAVY:
            if _glob_match(filename, glob_pat):
                return f'excluded glob: {glob_pat}'
        return None

    # ── Internal / Opensource: full exclusions ──
    # Root-anchored dir exclusions (e.g. 'data' — nested data/ dirs ship).
    if parts and parts[0] in ALWAYS_EXCLUDE_ROOT_ONLY_DIRS:
        return f'excluded dir: {parts[0]} (top-level only)'

    # Check dir exclusions
    for part in parts:
        if part in ALWAYS_EXCLUDE_DIRS:
            return f'excluded dir: {part}'
        if mode == 'opensource' and part in OPENSOURCE_EXTRA_EXCLUDE_DIRS:
            return f'opensource excluded dir: {part}'

    # Check file exclusions
    if filename in ALWAYS_EXCLUDE_FILES:
        return f'excluded file: {filename}'
    if mode == 'opensource' and filename in OPENSOURCE_EXTRA_EXCLUDE_FILES:
        return f'opensource excluded file: {filename}'

    # Check glob patterns
    for glob_pat in ALWAYS_EXCLUDE_GLOBS:
        if _glob_match(filename, glob_pat):
            return f'excluded glob: {glob_pat}'

    return None


def _glob_match(filename: str, pattern: str) -> bool:
    """Glob matching for file names (supports *.ext, prefix*, and *mid* patterns)."""
    if '*' not in pattern:
        return filename == pattern
    # Use fnmatch for full glob support
    from fnmatch import fnmatch
    return fnmatch(filename, pattern)


def _is_text_file(filepath: str) -> bool:
    """Heuristic: is this file likely text (safe to do string replacements)?"""
    text_exts = {
        '.py', '.js', '.html', '.css', '.md', '.txt', '.json', '.yaml', '.yml',
        '.toml', '.cfg', '.conf', '.ini', '.sh', '.bash', '.env', '.example',
        '.gitignore', '.xml', '.csv', '.rst',
    }
    ext = os.path.splitext(filepath)[1].lower()
    if ext in text_exts:
        return True
    # Files with no extension: check name
    basename = os.path.basename(filepath)
    if basename in {'Makefile', 'Dockerfile', 'LICENSE', 'Procfile', '.gitignore'}:
        return True
    return False




# Module-level version for sanitization pipeline (set in export_project)
_EXPORT_VERSION = ''


def _scrub_dest_pgdata_identity(dest: Path) -> None:
    """Remove host-identity markers from ``dest/data/pgdata/`` if present.

    These files tell PostgreSQL bootstrap "this cluster belongs to host X
    on port Y".  If carried over from a source machine, the destination
    would silently connect to the source's PG via FUSE cross-machine
    routing and read the source's data — a privacy / correctness bug.

    This is idempotent: missing files are fine.  No-op if there is no
    pgdata at the destination.
    """
    pgd = dest / 'data' / 'pgdata'
    if not pgd.is_dir():
        return
    for name in ('.pg_owner_host', 'postmaster.pid', 'postmaster.opts'):
        f = pgd / name
        try:
            if f.exists():
                f.unlink()
                logger.info('Scrubbed dest pgdata identity file: %s', name)
        except OSError as e:
            logger.warning('Could not scrub dest pgdata/%s: %s', name, e)


def _gitignored_excludes(src: Path) -> list[str]:
    """Return tar ``--exclude=`` args for every path git ignores in ``src``.

    Runs ``git ls-files -o -i --exclude-standard --directory`` inside the
    source tree, which honours ``.gitignore`` / ``.git/info/exclude`` /
    the global excludesfile AND collapses a fully-ignored directory to a
    single entry — so we never enumerate the 800K+ files inside eval
    ``*_workdir/`` trees. This keeps the export's exclusions in lock-step
    with ``.gitignore`` instead of drifting against the hand-maintained
    ``ALWAYS_EXCLUDE_DIRS`` set.

    Best-effort: returns ``[]`` if ``src`` is not a git repo or git is
    unavailable. Used for internal/opensource exports only — personal mode
    deliberately keeps gitignored data (``.env``, ``data/``, ``uploads/``).

    Args:
        src: Source project root (the git work tree).

    Returns:
        A list of ``--exclude=./<path>`` args, one per ignored entry.
    """
    if not _have_tool('git'):
        logger.debug('git not on PATH; skipping gitignore-based excludes')
        return []
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '-o', '-i',
             '--exclude-standard', '--directory'],
            cwd=str(src), capture_output=True, text=True,
        )
    except Exception as e:
        logger.warning('git ls-files for gitignore excludes failed: %s', e)
        return []
    if proc.returncode != 0:
        logger.debug('git ls-files rc=%s (src not a repo?); '
                     'skipping gitignore excludes: %.200s',
                     proc.returncode, proc.stderr)
        return []

    excludes: list[str] = []
    for raw in proc.stdout.split('\0'):
        path = raw.strip().rstrip('/')
        if not path:
            continue
        excludes.append(f'--exclude=./{path}')
    logger.info('[Export] Honouring .gitignore: %d ignored path(s) excluded',
                len(excludes))
    return excludes


def _untracked_root_dirs(src: Path) -> set[str]:
    """Return the names of untracked, NON-gitignored ROOT directories.

    ``_gitignored_excludes`` handles paths git IGNORES, and the hand-maintained
    ``ALWAYS_EXCLUDE_*`` sets handle known product dirs. But a directory that is
    BOTH untracked AND not gitignored falls through both — e.g. agent scratch a
    code-exec run leaves at the repo root (``module1/``, ``module2/``), a stray
    ``scratchpad/`` / ``fix-xr/``, a ``.pytest_cache/`` or ``*.local_bak.*``
    backup. tar copies it verbatim, leaking non-product junk into the export
    (and, for opensource, breaking the ruff gate on unformatted scratch Python).

    We target ROOT-LEVEL DIRECTORIES only: ``git ls-files -o --directory``
    collapses a fully-untracked directory to a single ``dir/`` entry, while
    listing untracked files INSIDE a tracked dir (a new ``lib/foo.py``, a new
    ``tests/test_*.py``) individually — those are legitimate new source and MUST
    NOT be excluded. So we keep only depth-1 ``dir/`` entries and ignore every
    file entry.

    Pure query (no printing / no side effects) so BOTH the real tar-copy path
    (via ``_untracked_root_excludes``) and the ``--dry-run`` preview walk can
    consult the SAME set — otherwise the preview would list ``module1/`` etc. as
    "will copy" while the real run drops them, and an operator trusting the
    pre-flight gets bitten at release time.

    Best-effort: returns an empty set if ``src`` is not a git repo or git is
    unavailable. Used for internal/opensource exports only — personal mode is a
    full self-use backup and keeps everything.

    Args:
        src: Source project root (the git work tree).

    Returns:
        A set of root directory names (no ``./`` prefix, no trailing ``/``).
    """
    if not _have_tool('git'):
        logger.debug('git not on PATH; skipping untracked-root scan')
        return set()
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '-o',
             '--exclude-standard', '--directory'],
            cwd=str(src), capture_output=True, text=True,
        )
    except Exception as e:
        logger.warning('git ls-files for untracked-root scan failed: %s', e)
        return set()
    if proc.returncode != 0:
        logger.debug('git ls-files -o rc=%s (src not a repo?); '
                     'skipping untracked-root scan: %.200s',
                     proc.returncode, proc.stderr)
        return set()

    stray_dirs: set[str] = set()
    for raw in proc.stdout.split('\0'):
        entry = raw.strip()
        # Only fully-untracked DIRECTORIES (git appends a trailing '/'). File
        # entries are untracked files inside tracked dirs — legit new source.
        if not entry or not entry.endswith('/'):
            continue
        path = entry.rstrip('/')
        # Root-level only: a nested collapsed dir means its parent is tracked,
        # so it's a new source subtree, not a stray top-level scratch dir.
        if '/' in path:
            continue
        stray_dirs.add(path)
    return stray_dirs


def _untracked_nested_files(src: Path, stray_root_dirs: set[str]) -> set[str]:
    """Untracked, non-gitignored FILES that would otherwise ship verbatim.

    The root-dir rule collapses stray top-level dirs, but an untracked FILE
    inside a TRACKED dir used to slip straight into the public repo —
    measured 2026-08-05: the uncommitted tests/test_frontend_autopilot_run_notice.py
    shipped to rangehow/ToFu and red-filed the public CI, and the
    tests/tmp*.js NC-harness temp copies rode along the same way. The
    published tree is a MIRROR OF COMMITTED SOURCE: if a file is worth
    publishing it is worth ``git add``ing — tracking IS the keeper
    mechanism, so there is deliberately no second allowlist here.

    ``git ls-files -o --exclude-standard`` (no ``--directory``) enumerates
    every untracked file individually; entries under ``stray_root_dirs`` are
    already covered by the root-dir rule and filtered out so the exclude
    list stays short. Root-level untracked FILES (``_fe.tmp.js``,
    ``JOURNAL.md.lock``, …) are included — the same argument applies.

    Best-effort: empty set when git is unavailable / src is not a repo.
    Personal mode never consults this (full self-use backup).
    """
    if not _have_tool('git'):
        return set()
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '-o', '--exclude-standard'],
            cwd=str(src), capture_output=True, text=True,
        )
    except Exception as e:
        logger.warning('git ls-files for untracked-file scan failed: %s', e)
        return set()
    if proc.returncode != 0:
        logger.debug('git ls-files -o (files) rc=%s — skipping: %.200s',
                     proc.returncode, proc.stderr)
        return set()
    out: set[str] = set()
    for raw in proc.stdout.split('\0'):
        entry = raw.strip()
        if not entry or entry.endswith('/'):
            continue
        top = entry.split('/', 1)[0]
        if '/' in entry and top in stray_root_dirs:
            continue  # already excluded by the root-dir rule
        out.add(entry)
    return out


def _untracked_file_excludes(src: Path) -> list[str]:
    """tar excludes for :func:`_untracked_nested_files`, with the same loud
    operator-facing summary the root-dir rule gives."""
    stray = _untracked_nested_files(src, _untracked_root_dirs(src))
    if stray:
        shown = sorted(stray)
        head = ', '.join(shown[:8]) + (f' … (+{len(shown) - 8} more)' if len(shown) > 8 else '')
        logger.warning('[Export] Skipping %d untracked, non-gitignored '
                       'file(s) in tracked dirs: %s', len(shown), head)
        print(f"  {C_YELLOW}\u26a0 Skipping {len(shown)} untracked file(s) "
              f"(not committed — the export mirrors committed source): {head}{C_END}")
        print(f"  {C_DIM}  \u2192 if any is real source, `git add` it before "
              f"exporting (or `.gitignore` it if it's junk).{C_END}")
    return [f'--exclude=./{f}' for f in sorted(stray)]


def _untracked_root_excludes(src: Path) -> list[str]:
    """Build tar ``--exclude=./<dir>`` args for the stray root dirs.

    Thin wrapper over :func:`_untracked_root_dirs` that formats each name as a
    tar exclude AND surfaces the set loudly (log + terminal warning), so a
    genuinely-new source directory the operator forgot to ``git add`` is not
    silently dropped from the real copy.
    """
    stray_dirs = _untracked_root_dirs(src)
    if stray_dirs:
        listed = ', '.join(sorted(stray_dirs))
        logger.warning('[Export] Skipping %d untracked, non-gitignored '
                       'root dir(s): %s', len(stray_dirs), listed)
        print(f"  {C_YELLOW}\u26a0 Skipping {len(stray_dirs)} untracked, "
              f"non-gitignored root dir(s) (agent scratch?): {listed}{C_END}")
        print(f"  {C_DIM}  \u2192 if any is real source, `git add` it before "
              f"exporting (or `.gitignore` it if it's junk).{C_END}")
    return [f'--exclude=./{d}' for d in sorted(stray_dirs)]


def _dirty_tracked_files(src: Path) -> list[str]:
    """Tracked files whose worktree content differs from HEAD (staged or not).

    The export copies the WORKTREE but skips untracked files, so a dirty
    tracked file is the only way content that is not in HEAD (and may
    reference skipped untracked files) reaches the published tree.
    """
    if not _have_tool('git'):
        return []
    try:
        proc = subprocess.run(
            ['git', 'diff', '--name-only', '-z', 'HEAD'],
            cwd=str(src), capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.debug('git diff for torn-snapshot guard failed: %s', e)
        return []
    if proc.returncode != 0:
        return []
    return [e for e in proc.stdout.split('\0') if e.strip()]


def _torn_snapshot_pairs(src: Path) -> list[tuple[str, str]]:
    """(dirty tracked file, skipped untracked file it references) pairs.

    Incident 2026-08-06: a sibling's uncommitted server.py edit imported
    lib/log_aggregates.py while that module was still untracked. The export
    shipped the dirty server.py (tracked) but dropped the module (untracked
    skip) — a TORN snapshot that red-filed public CI with 800+
    ModuleNotFoundError cascades. The untracked-file skip ("the export
    mirrors committed source") is only sound when dirty tracked files do
    not reference the files being skipped; this guard detects exactly that
    cross-reference so the operator can commit (or pass --allow-dirty)
    instead of publishing a tree that cannot even be imported.
    """
    skipped = _untracked_nested_files(src, _untracked_root_dirs(src))
    if not skipped:
        return []
    # Reference token per skipped file: its repo-relative path
    # ('lib/design_sys/fonts.py') — matched as a substring it also covers
    # leading-slash ('/static/…') web references — and, for Python files,
    # the dotted module ('lib.design_sys.fonts'; a package's __init__.py
    # contributes the package name 'lib.design_sys'). Bare stems ('fonts',
    # 'index') are far too generic — measured 2026-08-06: stem matching
    # false-positived on every preview/index/fonts word in the tree.
    tokens: dict[str, str] = {}
    for rel in sorted(skipped):
        tokens.setdefault(rel, rel)
        p = Path(rel)
        if p.suffix == '.py':
            dotted = rel[:-3].replace('/', '.')
            if p.stem == '__init__':
                dotted = dotted.rsplit('.', 1)[0]
            tokens.setdefault(dotted, rel)
    pairs: list[tuple[str, str]] = []
    for dirty in _dirty_tracked_files(src):
        # Prose never breaks an import: JOURNAL.md / docs routinely name
        # in-flight file paths (this project's JOURNAL convention), which
        # would otherwise false-trip the guard on every documented WIP.
        if Path(dirty).suffix.lower() in ('.md', '.rst', '.txt'):
            continue
        fpath = src / dirty
        try:
            if not fpath.is_file() or fpath.stat().st_size > 2_000_000:
                continue
            text = fpath.read_text(encoding='utf-8', errors='strict')
        except (OSError, UnicodeDecodeError):
            continue  # binary / unreadable — no textual reference possible
        for token, rel in tokens.items():
            if token in text and (dirty, rel) not in pairs:
                pairs.append((dirty, rel))
    return pairs


def _torn_snapshot_guard(src: Path, allow_dirty: bool,
                         dry_run: bool = False) -> None:
    """Abort (or loudly warn) when the export would publish a torn snapshot.

    See _torn_snapshot_pairs for the incident class. Best-effort: silent
    when git is unavailable or the tree is clean.
    """
    pairs = _torn_snapshot_pairs(src)
    if not pairs:
        return
    lines = '\n'.join(f'      {d}  →  references skipped untracked {u}'
                      for d, u in pairs[:12])
    more = f'\n      … (+{len(pairs) - 12} more)' if len(pairs) > 12 else ''
    head = ('DRY-RUN: torn snapshot detected'
            if dry_run
            else 'Torn snapshot detected (publishing anyway — --worktree)'
            if allow_dirty else 'Export aborted — torn snapshot detected')
    print(f"  {C_RED}✗ {head}:{C_END}\n"
          f"    Dirty tracked file(s) reference untracked file(s) that the\n"
          f"    export skips — the published tree would not even import:\n"
          f"{lines}{more}\n"
          f"    {C_DIM}→ `git add` the untracked file(s) (or commit the dirty\n"
          f"      ones away), or re-run with --worktree to publish the\n"
          f"      uncommitted state as-is.{C_END}")
    logger.error('Torn snapshot guard tripped: %s', pairs)
    if not dry_run and not allow_dirty:
        raise SystemExit(2)


def _stage_head_snapshot(src: Path) -> Path | None:
    """Extract committed HEAD content into a local temp dir for copy source.

    The internal/opensource export publishes COMMITTED source: the shared
    worktree routinely carries siblings' in-flight edits, and copying the
    worktree ships half-written code (2026-08-06, twice in one day: a dirty
    server.py importing an untracked module red-filed public CI with 800+
    cascades; a dirty mid-edit motion_video module followed with F821
    NameErrors). ``git archive HEAD`` is the exact committed tree — immune
    to dirty/untracked worktree state by construction. Returns None (caller
    falls back to the worktree + torn-snapshot guard) when git/tar fail.
    """
    if not _have_tool('git'):
        return None
    try:
        tmp = Path(tempfile.mkdtemp(prefix='tofu-export-head-'))
        # Pin the sha FIRST and archive that exact commit: a sibling can land
        # a commit mid-export, and the post-copy integrity check must compare
        # against the SAME tree the snapshot was taken from (2026-08-06: a
        # mid-export design_sys commit made the HEAD-based check flag a file
        # the snapshot legitimately lacked).
        rev = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=str(src),
                             capture_output=True, text=True, timeout=30)
        if rev.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        sha = rev.stdout.strip()
        arc = subprocess.Popen(['git', 'archive', '--format=tar', sha],
                               cwd=str(src), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        ext = subprocess.Popen(['tar', 'xf', '-'], cwd=str(tmp),
                               stdin=arc.stdout, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        if arc.stdout is not None:
            arc.stdout.close()
        _, xerr = ext.communicate()
        _, aerr = arc.communicate()
        if arc.returncode != 0 or ext.returncode != 0:
            logger.warning('HEAD snapshot failed (arc rc=%s, tar rc=%s): %.200s %.200s',
                           arc.returncode, ext.returncode,
                           (aerr or b'').decode('utf-8', 'replace'),
                           (xerr or b'').decode('utf-8', 'replace'))
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        global _EXPORT_SOURCE_SHA
        _EXPORT_SOURCE_SHA = sha
        return tmp
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning('HEAD snapshot unavailable (%s) — worktree fallback', e)
        return None


def _build_tar_excludes_for_mode(mode: str, dest: Path) -> tuple[list[str], list[str]]:
    """Build ``--exclude=`` args for the streaming tar copy, per mode.

    Returns ``(excludes, preserved)`` where:
      - ``excludes`` is the list of ``--exclude=…`` args (in the order
        wanted by GNU tar — top-level anchored entries first, then
        unanchored names that match anywhere in the tree).
      - ``preserved`` is the list of dest top-level dirs (e.g.
        ``data``, ``uploads``, ``.git``) that already exist and are
        being preserved (NOT re-copied from source).

    Modes:
      personal    — `PERSONAL_EXCLUDE_*` only (loosest)
      internal    — full ALWAYS_EXCLUDE_* set
      opensource  — full ALWAYS_EXCLUDE_* + OPENSOURCE_EXTRA_EXCLUDE_*
    """
    tar_excludes: list[str] = []

    if mode == 'personal':
        dirs = PERSONAL_EXCLUDE_DIRS | _TOP_LEVEL_ONLY_EXCLUDE_DIRS
        files = PERSONAL_EXCLUDE_FILES
        globs = PERSONAL_EXCLUDE_GLOBS | PERSONAL_EXCLUDE_GLOBS_HEAVY
    elif mode == 'internal':
        dirs = (ALWAYS_EXCLUDE_DIRS | _TOP_LEVEL_ONLY_EXCLUDE_DIRS
                | ALWAYS_EXCLUDE_ROOT_ONLY_DIRS)
        files = ALWAYS_EXCLUDE_FILES
        globs = ALWAYS_EXCLUDE_GLOBS
    else:  # opensource
        dirs = (ALWAYS_EXCLUDE_DIRS | OPENSOURCE_EXTRA_EXCLUDE_DIRS
                | _TOP_LEVEL_ONLY_EXCLUDE_DIRS | ALWAYS_EXCLUDE_ROOT_ONLY_DIRS)
        files = ALWAYS_EXCLUDE_FILES | OPENSOURCE_EXTRA_EXCLUDE_FILES
        globs = ALWAYS_EXCLUDE_GLOBS

    for d in dirs:
        # Top-level-only entries (e.g. 'paper', 'sundries', root 'data')
        # anchored at './' so a nested subdir of the same name elsewhere
        # isn't pruned.
        if d in _TOP_LEVEL_ONLY_EXCLUDE_DIRS or d in ALWAYS_EXCLUDE_ROOT_ONLY_DIRS:
            tar_excludes.append(f'--exclude=./{d}')
        else:
            # Anchored AND nested — applies wherever it appears in the tree.
            tar_excludes.append(f'--exclude=./{d}')
            tar_excludes.append(f'--exclude={d}')
    for g in globs:
        tar_excludes.append(f'--exclude={g}')
    for f in files:
        tar_excludes.append(f'--exclude={f}')

    # Agent-written artifacts (.tofu*) — one glob covers every current AND
    # future ``.tofu``-prefixed artifact in every mode, so a new artifact
    # never silently leaks into an export (the historical drift failure mode).
    tar_excludes.append(f'--exclude={_AGENT_ARTIFACT_GLOB}')

    # Honour .gitignore for internal/opensource so the export stays in
    # lock-step with the repo's ignore rules (covers new eval/bench
    # *_workdir/ dirs, caches, etc. without touching the hand-maintained
    # lists). Personal mode is INTENTIONALLY skipped — it's a full self-use
    # backup that keeps gitignored data (.env, data/, uploads/, .chatui/).
    if mode != 'personal':
        tar_excludes.extend(_gitignored_excludes(ROOT))
        # Also skip untracked, NON-gitignored root dirs (agent scratch that
        # neither git ignores nor the hand-maintained lists cover) — see
        # _untracked_root_excludes. Prevents stray code-exec dirs (module1/,
        # scratchpad/, …) leaking into the export and breaking the ruff gate.
        tar_excludes.extend(_untracked_root_excludes(ROOT))
        # …and untracked FILES inside tracked dirs (uncommitted new/WIP
        # source, tests/tmp*.js NC-temp copies) — the export mirrors
        # committed source; `git add` is the keeper. See
        # _untracked_nested_files for the incident that forced this.
        tar_excludes.extend(_untracked_file_excludes(ROOT))

    # Preserve dest user data + git history across re-exports. Same set for
    # all modes — these dirs belong to the destination user, not source.
    preserved: list[str] = []
    for p in ('data', 'uploads', '.git'):
        if (dest / p).exists():
            tar_excludes.append(f'--exclude=./{p}')
            preserved.append(p)
    return tar_excludes, preserved


def _ellipsize(text: str, width: int) -> str:
    """Trim ``text`` to ``width`` chars, keeping the tail (the filename)."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return '…' + text[-(width - 1):]


def _tar_progress_consumer(pipe, dst: Path) -> None:
    """Render live per-file progress from ``tar -v`` verbose output.

    ``tar cvf -`` writes the archive to stdout and the name of each member
    to stderr, one path per line, as it descends the tree. We read that
    stream and show:
      - a permanent line the first time tar enters each TOP-LEVEL entry
        (``./lib``, ``./static``, …) so an unwanted dir that slipped past
        the ``--exclude`` rules is immediately visible; and
      - a live, in-place ``transferred N · <current file>`` counter.

    The counter is only animated on a real TTY; when output is piped/
    captured we just emit the per-top-level lines (no carriage-return
    spam in logs).
    """
    is_tty = sys.stdout.isatty()
    seen_top: set[str] = set()
    count = 0
    last_paint = 0.0
    try:
        term_w = shutil.get_terminal_size((100, 24)).columns
    except OSError:
        term_w = 100

    for raw in iter(pipe.readline, b''):
        line = raw.decode('utf-8', 'replace').rstrip('\n')
        if not line:
            continue
        rel = line[2:] if line.startswith('./') else line
        if not rel:
            # tar emits the archive root ('./') as its first member — skip.
            continue
        count += 1
        top = rel.split('/', 1)[0]
        if top and top not in seen_top:
            seen_top.add(top)
            src_item = (ROOT / top)
            kind = 'dir ' if src_item.is_dir() else 'file'
            # Clear any live counter line before printing a permanent row.
            if is_tty:
                sys.stdout.write('\r\033[K')
            print(f"    {C_DIM}↳ {kind} {C_END}{C_CYAN}{top}{C_END}")
            if is_tty:
                sys.stdout.flush()
        if is_tty:
            now = time.monotonic()
            if now - last_paint >= 0.08:
                last_paint = now
                shown = _ellipsize(rel, max(20, term_w - 24))
                sys.stdout.write(
                    f"\r\033[K    {C_DIM}transferred {count} · {shown}{C_END}")
                sys.stdout.flush()

    if is_tty:
        sys.stdout.write(f"\r\033[K    {C_DIM}transferred {count} files{C_END}\n")
        sys.stdout.flush()
    else:
        print(f"    {C_DIM}transferred {count} files{C_END}")


def _stream_tar_copy(src: Path, dst: Path, excludes: list[str],
                     progress: bool = False) -> None:
    """Run ``tar c <excludes> . | tar x`` from src into dst.

    When ``progress`` is True, ``tar`` runs in verbose mode and each
    archived path is streamed live (see ``_tar_progress_consumer``) so the
    operator can watch the transfer and spot files that shouldn't be
    carried. Without it, the copy is silent (the historical behavior).

    Raises RuntimeError on real failure. GNU tar rc=1 ("file changed as
    we read it") is treated as a non-fatal warning — the source tree may
    be live (running server appending to logs, .project_sessions, etc.).
    """
    dst.mkdir(parents=True, exist_ok=True)
    # Verbose mode writes member names to stderr (stdout carries the archive).
    create_cmd = ['tar', 'cvf', '-', *excludes, '.'] if progress \
        else ['tar', 'cf', '-', *excludes, '.']
    try:
        tar_c = subprocess.Popen(
            create_cmd,
            cwd=str(src),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tar_x = subprocess.Popen(
            ['tar', 'xf', '-'],
            cwd=str(dst),
            stdin=tar_c.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if tar_c.stdout is not None:
            tar_c.stdout.close()

        # When showing progress, drain tar_c's stderr (the verbose file
        # list) in a thread so it can't block on a full pipe buffer while
        # we wait on the extract side. We capture the rendered lines back
        # into ``cerr`` only as needed for the rc=1 warning below.
        progress_thread = None
        if progress and tar_c.stderr is not None:
            progress_thread = threading.Thread(
                target=_tar_progress_consumer,
                args=(tar_c.stderr, src),
                daemon=True,
            )
            progress_thread.start()

        # No timeout — cross-FUSE copies can take minutes; user requested "no timeout".
        _, xerr = tar_x.communicate()
        if progress_thread is not None:
            # stderr is consumed by the thread; just reap the process.
            tar_c.wait()
            progress_thread.join(timeout=5)
            cerr = b''
        else:
            _, cerr = tar_c.communicate()
        if tar_c.returncode not in (0, 1, None):
            raise RuntimeError(
                f'tar create failed (rc={tar_c.returncode}): '
                f'{(cerr or b"").decode("utf-8", "replace")[:500]}'
            )
        elif tar_c.returncode == 1 and cerr:
            logger.info('tar warnings (non-fatal): %s',
                        (cerr or b'').decode('utf-8', 'replace')[:500])
        if tar_x.returncode != 0:
            raise RuntimeError(
                f'tar extract failed (rc={tar_x.returncode}): '
                f'{(xerr or b"").decode("utf-8", "replace")[:500]}'
            )
    except FileNotFoundError:
        raise RuntimeError(
            'tar binary not found — fast copy unavailable. '
            'Install GNU tar (conda install -c conda-forge tar).'
        ) from None


def _have_tool(name: str) -> bool:
    """Return True if `name` is on PATH (cheap check, used by rg/fd fast paths)."""
    return shutil.which(name) is not None


def _fast_count_files(root: Path) -> int:
    """Count regular files under ``root``. Uses Rust ``fd`` if available,
    else falls back to Python ``rglob``.

    On FUSE/DolphinFS, ``Path.rglob('*')`` is dramatically slower than fd
    (which uses parallel directory walking via the Rust ``ignore`` crate).
    For a tree with ~7k files we've measured 10-20× speedups.
    """
    if _have_tool('fd'):
        try:
            r = subprocess.run(
                ['fd', '--type', 'f', '--hidden', '--no-ignore', '.', str(root)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                return r.stdout.count('\n')
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug('fd count failed, falling back to rglob: %s', e)
    try:
        return sum(1 for _ in root.rglob('*') if _.is_file())
    except OSError as e:
        logger.debug('File count failed (non-fatal): %s', e)
        return 0


# ═══════════════════════════════════════════════════════════
#  Targeted post-copy sanitization (rg-driven)
# ═══════════════════════════════════════════════════════════
# After bulk-copying with tar (which preserves bytes 1:1), we walk over
# ONLY the files that contain something the sanitizer would touch.  This
# replaces a full ``os.walk`` + ``read_text`` + ``write_text`` over every
# text file in the tree — a huge win on FUSE since we avoid hammering the
# filesystem with thousands of pointless reads.
#
# The trigger list is the union of literal patterns that any sanitization
# rule looks for. As long as a file does NOT contain any of these
# substrings, the sanitize functions return unchanged content — so it's
# safe to skip.  rg picks up the matches in one parallel scan.

# Trigger substrings for `_sanitize_defaults_for_export` (internal mode).
# These are tiny — just enough to identify files that would be modified.
_INTERNAL_SANITIZE_TRIGGERS = [
    r'\?v=\d{8}',                                          # cache busters
    r"const _ICON_V = ",                                   # settings.js icon ver
    r"config\.defaultThinkingDepth = 'medium'",            # core.js default
    r'let serverModel = "aws\.claude-opus-4\.7"',          # core.js model
    r"_default_depth = cfg\.get\('defaultThinkingDepth', 'medium'\)",  # model_config.py
    r'<option value="medium"',                             # index.html medium option
    r'<option value="off"',                                # index.html off option
]

# Additional triggers for opensource mode (`_sanitize_source_opensource`).
# Anything in _SECRETS / _ENDPOINTS / _INTERNAL_DOMAIN_LITERALS forces
# the file open. We escape each literal for use in a regex alternation.
def _opensource_sanitize_triggers() -> list[str]:
    triggers = list(_INTERNAL_SANITIZE_TRIGGERS)
    for s in _SECRETS:
        triggers.append(re.escape(s))
    for s in _ENDPOINTS:
        triggers.append(re.escape(s))
    for s in _INTERNAL_DOMAIN_LITERALS:
        triggers.append(re.escape(s))
    # Internal identifiers that the secret-scan also catches —
    # MUST be in the sanitize trigger list so the rules below run. The
    # plain-substring identifiers come from the single-source-of-truth
    # _INTERNAL_IDENTIFIER_REPLACEMENTS tuple (so scrub + trigger can't drift);
    # the path/domain literals stay explicit here.
    for s in ('/mnt/dolphinfs', 'sankuai.com', 'meituan.com'):
        triggers.append(re.escape(s))
    for ident, _replacement in _INTERNAL_IDENTIFIER_REPLACEMENTS:
        triggers.append(re.escape(ident))
    return triggers


# rg's per-file size ceiling for the secret/endpoint scan. rg SKIPS files
# above this — which is a leak if a large text file carries a secret — so
# _scan_oversized_text_files scans anything above it in-process. Kept as a
# constant so the rg flag and the supplement guard can never drift apart.
_RG_MAX_FILESIZE = '5M'
_RG_MAX_FILESIZE_BYTES = 5 * 1024 * 1024


def _scan_oversized_text_files(root: Path, combined_rx: str,
                               extra_globs: list[str] | None = None) -> set[str]:
    """Regex-scan TEXT files larger than the rg ``--max-filesize`` cap.

    rg silently skips files above ``_RG_MAX_FILESIZE``; on the opensource
    export that means an oversized text file with a secret / internal hostname
    would ship un-sanitized. This walks ``root`` for text files strictly above
    the byte cap and returns those whose content matches ``combined_rx`` —
    closing the leak rg leaves open. Bounded work: only files ABOVE the cap are
    read (rare in a source tree). ``extra_globs`` are advisory only here (the
    heavy dirs they exclude are almost never text), so we simply skip obvious
    non-text via ``_is_text_file``.
    """
    try:
        rx = re.compile(combined_rx)
    except re.error as e:
        logger.warning('[export] oversized-scan regex compile failed: %s', e)
        return set()
    out: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fpath = Path(dirpath) / fn
            if not _is_text_file(str(fpath)):
                continue
            try:
                if fpath.stat().st_size <= _RG_MAX_FILESIZE_BYTES:
                    continue  # rg already covered it
                content = fpath.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if rx.search(content):
                out.add(str(fpath.relative_to(root)))
                logger.warning('[export] oversized text file matched sanitize '
                               'trigger (rg would have SKIPPED it): %s',
                               fpath.relative_to(root))
    return out


def _rg_files_with_matches(root: Path, patterns: list[str],
                            extra_globs: list[str] | None = None) -> set[str]:
    """Return relative paths under ``root`` that match any of ``patterns``.

    Uses a single ``rg --files-with-matches`` invocation with regex
    alternation. Falls back to a Python walk when rg is unavailable.

    Patterns are joined with ``|``; callers are responsible for ensuring
    each pattern is a valid PCRE2-compatible regex (literals should be
    pre-escaped via ``re.escape``).

    ``extra_globs`` is a list of ``rg --glob`` patterns (``!path`` syntax
    excludes a path) — used to keep rg out of huge dirs like
    ``swebench_workdir/`` that aren't part of the export anyway.
    """
    if not patterns:
        return set()
    combined = '|'.join(f'(?:{p})' for p in patterns)
    if _have_tool('rg'):
        try:
            cmd = ['rg', '--files-with-matches', '--no-messages',
                   '--hidden', '--no-ignore-vcs',
                   '--max-filesize', _RG_MAX_FILESIZE]
            for g in (extra_globs or []):
                cmd.extend(['--glob', g])
            cmd.extend(['-e', combined, str(root)])
            r = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=180)
            # rg exits 1 when there are no matches — that's not an error.
            if r.returncode in (0, 1):
                hits = {
                    str(Path(p).relative_to(root))
                    for p in r.stdout.splitlines() if p.strip()
                }
                # ── Oversized-text-file leak guard (SECURITY) ──
                # rg silently SKIPS files above --max-filesize, so a >cap text
                # file carrying a secret / internal hostname would never enter
                # the candidate set and would ship UN-sanitized. rg only scans
                # files it did NOT skip, so we must scan the oversized ones
                # ourselves: walk for text files above the cap and regex them
                # in-process (rare, so the cost is bounded). A match adds the
                # file to the candidate set exactly as the rg path would.
                hits |= _scan_oversized_text_files(root, combined, extra_globs)
                return hits
            logger.warning('rg returned rc=%s: %s', r.returncode,
                           (r.stderr or '')[:300])
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning('rg search failed: %s', e)

    # Python fallback — slower but never wrong.
    rx = re.compile(combined)
    out: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fpath = Path(dirpath) / fn
            if not _is_text_file(str(fpath)):
                continue
            try:
                content = fpath.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if rx.search(content):
                out.add(str(fpath.relative_to(root)))
    return out


def _rg_excluded_globs() -> list[str]:
    """Build a tight ``rg --glob !X`` exclude list for the post-copy scan.

    Kept minimal: the tar copy has already excluded most heavy dirs, so
    we only need to name the few that might still appear in `dest`
    because the re-export preserved them (e.g. the destination's own
    ``data/`` with a pgdata/, ``.git/``, ``uploads/``).  We also skip
    common binary file types that can never match text triggers.
    """
    # Dirs most likely to exist in `dest` (preserved from prior run) and
    # whose contents we never sanitize anyway.
    heavy_dirs = ('.git', 'data', 'uploads', 'logs',
                  '.project_sessions', '.project_indexes', '.chatui',
                  'node_modules', '__pycache__', '.pytest_cache',
                  '.ruff_cache', 'swebench_workdir', 'swebench_full',
                  './paper', 'overleaf_cache', 'sundries')
    globs = [f'!{d}/**' for d in heavy_dirs] + [f'!{d}' for d in heavy_dirs]
    for ext in ('*.png', '*.jpg', '*.jpeg', '*.gif', '*.zip', '*.pdf',
                '*.db', '*.db-*', '*.log', '*.pyc', '*.pyo',
                '*.ttf', '*.woff', '*.woff2'):
        globs.append(f'!{ext}')
    return globs


def _post_copy_sanitize(dest: Path, mode: str) -> dict:
    """Run the sanitizer on the destination tree, in place.

    Strategy:
      * Internal mode — `_sanitize_defaults_for_export` only modifies a
        SHORT, FIXED list of files (core.js, settings.js, model_config.py,
        index.html) plus any .html / .js file that has a ``?v=`` cache
        buster.  We build the candidate list directly (no rg needed).
      * Opensource mode — sanitization can touch secrets anywhere in the
        tree.  Use ``rg --files-with-matches`` to locate trigger files.
        Falls back to a Python walk if rg is absent or slow (e.g.
        pathological FUSE + parallel walker interaction — rg from Python
        subprocess can be dramatically slower than from an interactive
        shell on certain mounts; the Python fallback is predictable).

    Returns ``{'sanitized': n, 'scanned_files': m}``.
    """
    if mode not in ('internal', 'opensource'):
        return {'sanitized': 0, 'scanned_files': 0}

    if mode == 'internal':
        candidates = _collect_internal_candidates(dest)
    else:
        candidates = _collect_opensource_candidates(dest)

    sanitized = 0
    for relpath in sorted(candidates):
        fpath = dest / relpath
        if not fpath.is_file() or not _is_text_file(str(fpath)):
            continue
        try:
            content = fpath.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            logger.warning('Could not read %s for sanitize: %s', relpath, e)
            continue
        new = _sanitize_defaults_for_export(content, relpath, version=_EXPORT_VERSION)
        if mode == 'opensource':
            new = _sanitize_source_opensource(new, relpath)
        if new != content:
            try:
                fpath.write_text(new, encoding='utf-8')
                sanitized += 1
                print(f"  {C_YELLOW}\U0001f527{C_END} {relpath}")
            except OSError as e:
                logger.error('Could not write sanitized %s: %s', relpath, e)
    return {'sanitized': sanitized, 'scanned_files': len(candidates)}


# ── Internal-mode candidate collection (no rg — fixed list) ──
#
# `_sanitize_defaults_for_export` only modifies content that matches one of:
#   - `.html` / `.js` files with a `?v=YYYYMMDD[a-z]` cache-buster substring
#   - settings.js  (_ICON_V constant + cache busters)
#   - core.js      (thinking-depth default + serverModel init)
#   - model_config.py (server-side default thinking depth)
#   - index.html   (<select> default)
#
# The explicit filenames are known; cache-buster patterns live in any HTML/JS
# that imports static assets (currently just index.html). We build the
# candidate set directly and avoid a full-tree search.

_INTERNAL_FIXED_CANDIDATES = (
    'static/js/core.js',
    'static/js/settings.js',
    'lib/tasks_pkg/model_config.py',
    'index.html',
)


def _collect_internal_candidates(dest: Path) -> set[str]:
    """Return the relative paths of files sanitize might touch in internal mode."""
    out: set[str] = set()
    # 1. Hardcoded whitelist.
    for rel in _INTERNAL_FIXED_CANDIDATES:
        if (dest / rel).is_file():
            out.add(rel)
    # 2. All .html / .js under static/ and the top-level .html (for cache busters).
    for sub in ('static',):
        sub_dir = dest / sub
        if not sub_dir.is_dir():
            continue
        for p in sub_dir.rglob('*.js'):
            out.add(str(p.relative_to(dest)))
        for p in sub_dir.rglob('*.html'):
            out.add(str(p.relative_to(dest)))
    # Top-level HTML files (index.html) — already in fixed set but rglob above
    # misses them since they live at the root.
    for p in dest.glob('*.html'):
        out.add(str(p.relative_to(dest)))
    return out


def _collect_opensource_candidates(dest: Path) -> set[str]:
    """Locate files possibly needing opensource sanitization (secrets/endpoints)."""
    triggers = _opensource_sanitize_triggers()
    return _rg_files_with_matches(
        dest, triggers, extra_globs=_rg_excluded_globs())


def _export_personal_via_tar(dest: Path, progress: bool = False) -> dict:
    """Fast personal-mode export using a streaming ``tar c | tar x`` pipe.

    Per-file ``shutil.copy2`` is painfully slow on FUSE / network filesystems
    (DolphinFS, NFS, etc.) because every file incurs multiple round-trips
    (create, stat, chmod, utime).  Piping a single tar stream instead lets
    the kernel batch I/O and the destination side writes sequentially.

    Preservation: if the destination already has ``data/``, ``uploads/``, or
    ``.git/``, those are EXCLUDED from the source tar — so the destination's
    own data, uploaded files, and git history are never clobbered by a
    re-export.  Personal mode deliberately includes its own ``data/`` and
    ``uploads/`` only when the destination is empty / fresh.

    Chat history is NOT carried: the PG cluster, SQLite DBs, and SQL dumps
    are all excluded (see PERSONAL_EXCLUDE_*), so a fresh destination boots
    with an empty database while config + credentials (data/config/) come
    across intact.

    Returns a stats dict compatible with the walk-based path.
    """
    dest.mkdir(parents=True, exist_ok=True)
    tar_excludes, preserved = _build_tar_excludes_for_mode('personal', dest)
    if preserved:
        print(f"  {C_CYAN}\U0001f4be Preserving dest: {', '.join(preserved + ['/'])}"
              f"  (not overwritten from source){C_END}")

    # Personal mode does NOT carry chat history: pgdata/, *.db, and any
    # SQL dumps are excluded (see PERSONAL_EXCLUDE_*), so a fresh dest boots
    # with an empty DB. Config + credentials (data/config/) still copy over.

    print(f"  {C_DIM}Streaming tar pipe (src → dst) — much faster on FUSE…{C_END}")

    stats = {'copied': 0, 'sanitized': 0, 'excluded': 0, 'excluded_reasons': {}}

    _stream_tar_copy(ROOT, dest, tar_excludes, progress=progress)

    # Count files at dest for the summary (best-effort, fast path via fd).
    stats['copied'] = _fast_count_files(dest)

    # ── Scrub host-identity files from any pgdata the dest may already have ──
    # Belt-and-braces: if a previous (pre-dump) export left a pgdata there, or
    # a user hand-copied one in, make sure the dest PG bootstrap never silently
    # connects to the source machine via the old ``.pg_owner_host`` marker.
    _scrub_dest_pgdata_identity(dest)

    return stats


def _copy_file(src: Path, dst: Path, mode: str, relpath: str) -> str:
    """Copy a single file, applying sanitization if needed.

    Returns a status string: 'copied', 'sanitized', or 'skipped:reason'.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if mode in ('internal', 'opensource') and _is_text_file(str(src)):
        try:
            content = src.read_text(encoding='utf-8', errors='replace')
            # Apply default-settings sanitization (internal & opensource)
            sanitized = _sanitize_defaults_for_export(content, relpath, version=_EXPORT_VERSION)
            # Apply full secret/endpoint sanitization (opensource only)
            if mode == 'opensource':
                sanitized = _sanitize_source_opensource(sanitized, relpath)
            dst.write_text(sanitized, encoding='utf-8')
            if sanitized != content:
                return 'sanitized'
            return 'copied'
        except Exception as e:
            logger.warning('Failed to sanitize %s: %s — copying as-is', relpath, e)
            shutil.copy2(src, dst)
            return 'copied (sanitize failed)'
    else:
        shutil.copy2(src, dst)
        return 'copied'



# ═══════════════════════════════════════════════════════════
#  Internal mode: seed shared config (providers, proxy, models)
# ═══════════════════════════════════════════════════════════

# Keys from server_config.json that are safe to share internally
# (API keys, endpoints, proxy, models — all company-shared resources)
_INTERNAL_CONFIG_KEYS = {
    'providers',             # API keys + endpoints (the whole point)
    'models',                # default model assignments
    'presets',               # quality-of-life model presets
    'search',                # search/fetch settings
    'proxy_config',          # internal proxy (needed for API access)
    'proxy_bypass_domains',  # proxy bypass list
    'model_defaults',        # fallback model config
    'hidden_models',         # UI cleanup
    'hidden_ig_models',      # UI cleanup
}

# Keys explicitly excluded from internal sharing:
#   feishu         — personal app_id, workspace paths, allowed_users
#   model_limits   — learned from personal usage patterns
#   mcp_servers    — stored separately, contains personal tokens (e.g. GitHub PAT)


def _seed_internal_config(dest: Path):
    """Seed server_config.json for internal exports.

    Copies shared settings (providers with API keys, proxy, models) from the
    source project so colleagues can run the app immediately.  Personal data
    (feishu config, learned model limits) is stripped.

    If the destination already has a server_config.json, it is NOT overwritten —
    the user may have customized it.
    """
    dest_config = dest / 'data' / 'config' / 'server_config.json'
    # Always re-seed: the destination's server_config.json is a derived artifact
    # of the source, not a user-edited file. Keeping a stale copy across
    # re-exports means new models / providers in source never reach the
    # exported copy (e.g. adding a model to the Meituan provider would be
    # silently dropped on re-export). Overwrite every time to guarantee the
    # exported config reflects current source state.
    if dest_config.exists():
        logger.info('Overwriting existing server_config.json to reflect current source state')

    src_config = ROOT / 'data' / 'config' / 'server_config.json'
    if not src_config.exists():
        logger.warning('Source server_config.json not found — skipping internal config seed')
        return

    try:
        with open(src_config, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('Failed to read source server_config.json: %s', e)
        return

    # Extract only the shared keys
    seeded = {}
    for key in _INTERNAL_CONFIG_KEYS:
        if key in cfg:
            seeded[key] = cfg[key]

    if not seeded:
        logger.warning('No shared config keys found in source — skipping seed')
        return

    # ── Filter providers: only keep Meituan (sankuai) for internal exports ──
    # All api_keys on the Meituan provider are shared with colleagues as-is.
    if 'providers' in seeded:
        seeded['providers'] = [
            p for p in seeded['providers']
            if p.get('id') == 'sankuai'
        ]

    # Write the seeded config
    dest_config.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_config, 'w', encoding='utf-8') as f:
        json.dump(seeded, f, indent=2, ensure_ascii=False)
        f.write('\n')

    provider_names = [p.get('name', '?') for p in seeded.get('providers', [])]
    logger.info('Seeded internal server_config.json with %d keys, providers: %s',
                len(seeded), provider_names)
    print(f"  {C_GREEN}🔑 Seeded server_config.json with {len(seeded)} shared settings{C_END}")
    print(f"     {C_DIM}Providers: {', '.join(provider_names)}{C_END}")


# ═══════════════════════════════════════════════════════════
#  Internal / Personal mode: bake the corp proxy into install.sh
# ═══════════════════════════════════════════════════════════
#
# install.sh runs BEFORE server.py ever boots, so it cannot read
# ``data/config/server_config.json``.  Yet its first acts (``git clone``,
# ``conda install``, ``pip install``, ``playwright install``) all need
# outbound HTTPS — which on the corp network requires the internal proxy.
#
# For ``personal`` and ``internal`` exports we therefore patch the
# destination's install.sh in place: a small ``export http_proxy=… ;
# export https_proxy=… ; export no_proxy=…`` block is injected right
# after ``set -euo pipefail``.  The block is wrapped in BEGIN/END markers
# so re-exports replace the previous block instead of accumulating dupes.
#
# ``opensource`` mode never touches install.sh — its install.sh ships
# stock for the public.

_INSTALL_SH_PROXY_BEGIN = '# ── BEGIN tofu-export injected proxy ──'
_INSTALL_SH_PROXY_END = '# ── END tofu-export injected proxy ──'


def _patch_install_sh_proxy(dest: Path, mode: str):
    """Bake corp proxy env-vars into ``install.sh`` for personal/internal exports.

    Reads ``proxy_config`` and ``proxy_bypass_domains`` from the source
    ``server_config.json`` and prepends a small ``export http_proxy=…``
    block to the destination's ``install.sh``.  Idempotent — re-runs strip
    the prior block before injecting the fresh one.
    """
    if mode not in ('personal', 'internal'):
        return

    install_sh = dest / 'install.sh'
    if not install_sh.exists():
        logger.debug('No install.sh in dest — skipping proxy injection')
        return

    src_config = ROOT / 'data' / 'config' / 'server_config.json'
    if not src_config.exists():
        logger.warning('Source server_config.json missing — cannot bake proxy into install.sh')
        return

    try:
        with open(src_config, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('Failed to read source server_config.json for proxy injection: %s', e)
        return

    proxy_cfg = cfg.get('proxy_config') or {}
    http_proxy = proxy_cfg.get('http_proxy') or ''
    https_proxy = proxy_cfg.get('https_proxy') or http_proxy
    if not http_proxy and not https_proxy:
        logger.info('No proxy_config in source — skipping install.sh proxy injection')
        return

    # Bypass list → comma-separated no_proxy. Always include localhost.
    bypass = list(cfg.get('proxy_bypass_domains') or [])
    no_proxy_parts = ['localhost', '127.0.0.1', '::1'] + bypass
    no_proxy_value = ','.join(dict.fromkeys(no_proxy_parts))  # de-dupe, keep order

    block_lines = [
        _INSTALL_SH_PROXY_BEGIN,
        '# Auto-injected by export.py (mode=' + mode + ').  Required by',
        '# git clone / conda / pip / playwright on the corp network.',
        '# Remove or comment out if you do NOT need the internal proxy.',
    ]
    if http_proxy:
        block_lines.append(f'export http_proxy="{http_proxy}"')
        block_lines.append(f'export HTTP_PROXY="{http_proxy}"')
    if https_proxy:
        block_lines.append(f'export https_proxy="{https_proxy}"')
        block_lines.append(f'export HTTPS_PROXY="{https_proxy}"')
    if no_proxy_value:
        block_lines.append(f'export no_proxy="{no_proxy_value}"')
        block_lines.append(f'export NO_PROXY="{no_proxy_value}"')
    # Conda-forge mirror redirect: many corp proxies 403 conda.anaconda.org.
    # If `conda_mirror` is set in server_config.json, export it so install.sh
    # writes a sibling .condarc that redirects conda-forge to the mirror.
    # Defaults to the Sankuai internal mirror for any host inside the corp
    # network — safe because it's only consulted when CONDA_OWNED_BY_US=1.
    conda_mirror = cfg.get('conda_mirror') or 'https://mirrors.sankuai.com/conda/cloud'
    if conda_mirror:
        block_lines.append(f'export TOFU_CONDA_MIRROR="{conda_mirror}"')
    # PyPI mirror redirect — same story as conda-forge; proxy 403s pypi.org.
    pypi_index = cfg.get('pypi_index') or 'https://mirrors.sankuai.com/pypi/web/simple/'
    if pypi_index:
        block_lines.append(f'export TOFU_PYPI_INDEX="{pypi_index}"')
    # npm registry redirect — same story again; proxy blocks
    # registry.npmjs.org, and npm's install of the OPTIONAL esbuild/typecheck
    # devDependencies would otherwise hang for minutes. Only emitted when a
    # mirror is configured; without it install.sh still fail-fasts + falls
    # back to the Python minifier.
    npm_registry = cfg.get('npm_registry') or ''
    if npm_registry:
        block_lines.append(f'export TOFU_NPM_REGISTRY="{npm_registry}"')
    block_lines.append(_INSTALL_SH_PROXY_END)
    block = '\n'.join(block_lines) + '\n'

    try:
        text = install_sh.read_text(encoding='utf-8')
    except OSError as e:
        logger.warning('Failed to read dest install.sh: %s', e)
        return

    # Strip any prior injected block (idempotent).
    if _INSTALL_SH_PROXY_BEGIN in text and _INSTALL_SH_PROXY_END in text:
        start = text.index(_INSTALL_SH_PROXY_BEGIN)
        end = text.index(_INSTALL_SH_PROXY_END) + len(_INSTALL_SH_PROXY_END)
        # Also swallow the trailing newline so we don't pile up blanks.
        if end < len(text) and text[end] == '\n':
            end += 1
        text = text[:start] + text[end:]

    # Anchor: insert right after `set -euo pipefail` so the proxy is in
    # effect for every subsequent step.  Fallback: prepend after shebang.
    anchor = 'set -euo pipefail\n'
    if anchor in text:
        idx = text.index(anchor) + len(anchor)
        new_text = text[:idx] + '\n' + block + text[idx:]
    else:
        # Insert after the first line (shebang) if anchor is missing.
        first_nl = text.find('\n')
        if first_nl < 0:
            new_text = block + text
        else:
            new_text = text[:first_nl + 1] + block + text[first_nl + 1:]

    try:
        install_sh.write_text(new_text, encoding='utf-8')
        # Preserve executable bit (write_text loses no perms, but be explicit).
        install_sh.chmod(install_sh.stat().st_mode | 0o111)
    except OSError as e:
        logger.warning('Failed to write patched install.sh: %s', e)
        return

    logger.info('Patched install.sh with proxy http=%s https=%s no_proxy=%s',
                http_proxy or '(none)', https_proxy or '(none)', no_proxy_value or '(none)')
    print(f"  {C_GREEN}🌐 Baked corp proxy into install.sh{C_END}")
    print(f"     {C_DIM}{http_proxy or https_proxy}  bypass: {no_proxy_value}{C_END}")


# ═══════════════════════════════════════════════════════════
#  Post-export setup: create empty required dirs & templates
# ═══════════════════════════════════════════════════════════

def _bundle_internal_mcp_repos(dest: Path, mode: str):
    """Copy sibling ``hope-mcp`` / ``xuecheng-mcp`` repos into ``<dest>/vendor/``.

    These are private Meituan-internal MCP servers not published to PyPI,
    so a fresh install otherwise has no ``hope-mcp`` / ``xuecheng-mcp``
    on PATH and the MCP tab's "Install" button fails with
    ``launcher '...-mcp' is not on PATH``.

    Only bundled in ``personal`` and ``internal`` modes — opensource
    exports never ship internal repos.

    Skips cleanly if a sibling repo isn't found (developer ran export
    from a dir that doesn't have both siblings checked out).
    """
    if mode not in ('personal', 'internal'):
        return

    import shutil

    vendor_dir = dest / 'vendor'
    vendor_dir.mkdir(parents=True, exist_ok=True)

    # Preserve a .gitignore'd marker so the rest of the codebase can opt
    # to ignore vendor/ if desired (it's intentionally shipped, not built).
    (vendor_dir / '.gitkeep').touch()

    bundled = []
    for sibling_name in ('hope-mcp', 'xuecheng-mcp', 'llm-mcp'):
        src = ROOT.parent / sibling_name
        if not src.exists() or not (src / 'pyproject.toml').exists():
            logger.info('Sibling MCP repo not found — skipping: %s', src)
            continue

        target = vendor_dir / sibling_name
        if target.exists():
            # Clean re-copy so stale files don't linger across re-exports.
            shutil.rmtree(target, ignore_errors=True)

        # Copy sources only — skip build artifacts / caches / VCS.
        def _ignore(_root, names):
            drop = set()
            for n in names:
                if n in {'__pycache__', '.git', '.venv', 'venv', 'dist',
                          'build', '.pytest_cache', '.mypy_cache',
                          '.tox', 'node_modules', '.eggs',
                          '.chatui', '.tofu'}:  # per-host agent state / file-history
                    drop.add(n)
                elif n.endswith(('.pyc', '.pyo', '.egg-info')):
                    drop.add(n)
            return drop

        try:
            shutil.copytree(src, target, ignore=_ignore, symlinks=False)
            bundled.append(sibling_name)
            logger.info('Bundled MCP repo: %s → %s', src, target)
        except OSError as e:
            logger.warning('Failed to bundle %s: %s', sibling_name, e)

    if bundled:
        print(f"  {C_GREEN}📦 Bundled internal MCP repos → vendor/{C_END}")
        print(f"     {C_DIM}{', '.join(bundled)}{C_END}")


def _bundle_tofu_search_wheel(dest: Path, mode: str):
    """Copy the prebuilt ``tofu-search`` wheel into ``<dest>/vendor/``.

    Bundled for EVERY mode, opensource included. The previous opensource
    skip rested on "a vanilla host reaches public PyPI fine" — measured
    false since the floor moved to 0.5.3: public PyPI tops at 0.5.1, so an
    opensource export's ``pip install -r requirements.txt`` (and the CI
    legs' identical step) could not resolve at all. The wheel is this
    project's own MIT artifact, so shipping it is license-clean; once a
    floor-satisfying release IS published the bundle is simply redundant.
    ``tests/test_requirements_public_resolvable.py`` is the guard that
    keeps the publish honest in the meantime.

    The wheel is sourced from the sibling repo's ``dist/`` directory
    (``<ROOT>/../tofu-search/dist/tofu_search-*.whl``). Skips cleanly if the
    sibling repo or a built wheel isn't present.
    """

    import shutil

    src_repo = ROOT.parent / 'tofu-search'
    dist_dir = src_repo / 'dist'
    if not dist_dir.is_dir():
        logger.info('tofu-search dist/ not found — skipping wheel bundle: %s', dist_dir)
        return

    wheels = sorted(dist_dir.glob('tofu_search-*.whl'))
    if not wheels:
        logger.info('No tofu_search-*.whl in %s — skipping wheel bundle', dist_dir)
        return

    # Highest version wins (sorted lexically is good enough for 0.x; the
    # install.sh side also `sort -V | tail -1` if several land in vendor/).
    wheel = wheels[-1]

    # ── Staleness guard ──────────────────────────────────────────────────
    # The wheel in dist/ is whatever was LAST built; if someone bumped the
    # tofu-search source version (pyproject.toml) or raised the floor in our
    # requirements.txt but forgot to `python -m build`, dist/ still holds an
    # OLD wheel. Bundling it silently ships a build below our own floor →
    # server boots and may crash on a symbol that only exists in the newer
    # release (this exact trap shipped a 0.3.0 wheel against a >=0.3.2 floor /
    # 0.3.2 source). Don't fail the export (the wheel may still be usable),
    # but make the drift IMPOSSIBLE to miss so the operator rebuilds dist/.
    def _wheel_ver(name: str) -> tuple:
        m = re.search(r'tofu_search-([0-9]+(?:\.[0-9]+)*)', name)
        parts = (m.group(1).split('.') if m else ['0']) + ['0', '0', '0']
        return tuple(int(''.join(c for c in p if c.isdigit()) or 0) for p in parts[:3])

    def _ver_tuple(s: str) -> tuple:
        parts = (str(s).split('+')[0].split('.') if s else ['0']) + ['0', '0', '0']
        return tuple(int(''.join(c for c in p if c.isdigit()) or 0) for p in parts[:3])

    wheel_v = _wheel_ver(wheel.name)
    expected = []  # (label, version_tuple, raw)
    try:
        pyproj = (src_repo / 'pyproject.toml').read_text(encoding='utf-8', errors='ignore')
        m = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', pyproj)
        if m:
            expected.append(('tofu-search source pyproject.toml', _ver_tuple(m.group(1)), m.group(1)))
    except OSError as e:
        logger.debug('Could not read tofu-search pyproject.toml for staleness check: %s', e)
    try:
        reqs = (ROOT / 'requirements.txt').read_text(encoding='utf-8', errors='ignore')
        m = re.search(r'(?mi)^\s*tofu-search\s*>=\s*([0-9][0-9.]*)', reqs)
        if m:
            expected.append(("chatui requirements.txt floor (>=)", _ver_tuple(m.group(1)), m.group(1)))
    except OSError as e:
        logger.debug('Could not read requirements.txt for staleness check: %s', e)

    for label, ev, raw in expected:
        if wheel_v < ev:
            msg = (f"bundled tofu-search wheel {wheel.name} is STALE: "
                   f"{'.'.join(map(str, wheel_v))} < {raw} ({label})")
            logger.warning('[Export] %s', msg)
            print(f"  {C_RED}⚠ STALE WHEEL — {msg}{C_END}")
            print(f"     {C_YELLOW}Rebuild it first:  (cd {src_repo} && python -m build --wheel) "
                  f"then re-run this export.{C_END}")

    vendor_dir = dest / 'vendor'
    vendor_dir.mkdir(parents=True, exist_ok=True)
    (vendor_dir / '.gitkeep').touch()

    # Drop any stale tofu_search wheels from a prior export so install.sh's
    # glob doesn't pick an older version.
    for old in vendor_dir.glob('tofu_search-*.whl'):
        try:
            old.unlink()
        except OSError as e:
            logger.debug('Could not remove stale wheel %s: %s', old, e)

    target = vendor_dir / wheel.name
    try:
        shutil.copy2(wheel, target)
        logger.info('Bundled tofu-search wheel: %s → %s', wheel, target)
        print(f"  {C_GREEN}📦 Bundled tofu-search wheel → vendor/{C_END}")
        print(f"     {C_DIM}{wheel.name}{C_END}")
    except OSError as e:
        logger.warning('Failed to bundle tofu-search wheel: %s', e)


def _portablize_bundled_mcp_config(dest: Path, mode: str):
    """Rewrite hope/xuecheng entries in the exported ``mcp_servers.json``.

    Personal mode copies the live ``data/config/mcp_servers.json`` verbatim,
    and on the source machine those two servers are pinned to absolute
    ``uvx --from /mnt/.../hope-mcp`` paths.  Those paths don't exist on a
    different destination machine, so the launch fails there even though
    ``install.sh`` pip-installs the bundled ``vendor/`` repos and puts bare
    ``hope-mcp`` / ``xuecheng-mcp`` launchers on PATH.

    Rewrite the entries to the bare command (matching the registry default
    and the vendor install) so the export is portable to any machine. The
    ``env`` block (credentials) is preserved untouched.

    Only runs for ``personal`` and ``internal`` modes — opensource never
    bundles these repos.
    """
    if mode not in ('personal', 'internal'):
        return

    cfg_path = dest / 'data' / 'config' / 'mcp_servers.json'
    if not cfg_path.exists():
        return

    try:
        config = json.loads(cfg_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('Could not read %s for MCP portablization: %s', cfg_path, e)
        return

    if not isinstance(config, dict):
        return

    rewritten = []
    for name in ('hope', 'xuecheng', 'llm'):
        entry = config.get(name)
        if not isinstance(entry, dict):
            continue
        bare_cmd = f'{name}-mcp'
        if entry.get('command') == bare_cmd and not entry.get('args'):
            continue
        entry['command'] = bare_cmd
        entry['args'] = []
        rewritten.append(name)

    if not rewritten:
        return

    try:
        cfg_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')
    except OSError as e:
        logger.warning('Could not write portablized MCP config %s: %s', cfg_path, e)
        return

    logger.info('Portablized bundled MCP config entries: %s', ', '.join(rewritten))
    print(f"  {C_GREEN}🔧 Made bundled MCP config portable → bare launchers{C_END}")
    print(f"     {C_DIM}{', '.join(rewritten)}{C_END}")


def _purge_runtime_artifacts(dest: Path):
    """Delete transient runtime artifacts that may exist in the dest dir.

    If someone ran ``python server.py`` inside a previous export, log files,
    lockfiles, and runtime stats will have been created.  These are
    regenerable noise and safe to remove on every re-export.

    **Chat history is PRESERVED**:
      - ``data/chatui.db`` (SQLite) and its side files
      - ``data/pgdata/`` (Postgres cluster)
      - ``data/config/server_config.json`` and other user configs
    These belong to whoever is running the destination copy and must NOT
    be destroyed by a re-export.  We only wipe things that are either
    pure noise (logs, lockfiles) or tied to the source machine's identity
    (API-key usage stats).
    """
    def _rm_tree(p: Path):
        if not p.exists():
            return
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            else:
                import shutil
                shutil.rmtree(p)
        except OSError as e:
            logger.warning('Could not purge runtime artifact %s: %s', p, e)

    # Whole-tree nukes — logs only.  Databases and pgdata are preserved.
    _rm_tree(dest / 'logs')

    # Under data/: clean lockfile + stray log files, but LEAVE *.db alone.
    data_dir = dest / 'data'
    if data_dir.exists():
        for pattern in ('.server.lock', '*.log'):
            for f in data_dir.glob(pattern):
                _rm_tree(f)
        # Runtime API-key stats are tied to the SOURCE machine's key usage
        # counters — they don't belong in a fresh export.  Still wipe them.
        runtime_files = ('key_stats.json', '.key_stats.json')
        config_dir = data_dir / 'config'
        if config_dir.exists():
            for rf in runtime_files:
                _rm_tree(config_dir / rf)

    # NOTE: uploads/ is also preserved now.  It belongs to the user of the
    # destination copy; re-exporting must not wipe their uploaded images.


def _create_skeleton(dest: Path, mode: str):
    """Create empty directories and placeholder files that the app expects."""
    # ── FIRST: purge transient runtime artifacts from a previous run ──
    # Logs, lockfiles, and key-stats counters are regenerable noise and
    # safe to remove.  Databases (chatui.db, pgdata/) and uploads/ are
    # PRESERVED so the destination user's chat history survives re-exports.
    _purge_runtime_artifacts(dest)

    # Empty dirs the app needs
    for d in ['data', 'data/config', 'logs', 'uploads/images']:
        (dest / d).mkdir(parents=True, exist_ok=True)
        gitkeep = dest / d / '.gitkeep'
        if not gitkeep.exists():
            gitkeep.touch()

    # ── Internal mode: seed server_config.json with providers & shared settings ──
    # Without this, colleagues get an empty config and "API not found" errors.
    # Only seeds if the destination doesn't already have one (don't overwrite
    # user's own config on re-export).
    if mode == 'internal':
        _seed_internal_config(dest)

    # ── Personal & internal: bundle sibling MCP server repos (hope-mcp,
    # xuecheng-mcp) under vendor/ so install.sh can pip-install them.
    # These are private Meituan-internal repos not published to PyPI, so
    # a fresh destination install has no way to fetch them otherwise.
    # Opensource mode skips this entirely (repos are internal-only).
    _bundle_internal_mcp_repos(dest, mode)

    # ── Personal & internal: bundle the prebuilt tofu-search wheel under
    # vendor/ so install.sh can pip-install it behind a corp mirror that
    # lacks the package. Opensource installs it from public PyPI instead.
    _bundle_tofu_search_wheel(dest, mode)

    # ── Personal & internal: rewrite hope/xuecheng entries in the copied
    # mcp_servers.json from absolute source-machine `uvx --from /path` to
    # bare `hope-mcp` / `xuecheng-mcp` launchers, so the persisted config
    # matches the vendor/ pip-install and works on any destination machine.
    _portablize_bundled_mcp_config(dest, mode)

    # ── Personal & internal: bake corp proxy into install.sh ──
    # install.sh runs BEFORE server.py boots and can't read server_config.json,
    # but every step needs outbound HTTPS.  Inject the proxy env-vars near the
    # top of install.sh so colleagues / a fresh personal copy can install
    # behind the corp firewall without manually exporting http_proxy first.
    _patch_install_sh_proxy(dest, mode)

    # .gitignore for uploads/images
    gi = dest / 'uploads' / 'images' / '.gitignore'
    gi.write_text('*\n!.gitkeep\n')

    # Ensure tests/screenshots dir exists
    ss_dir = dest / 'tests' / 'screenshots'
    if ss_dir.exists():
        gi_ss = ss_dir / '.gitignore'
        if not gi_ss.exists():
            gi_ss.write_text('*.png\n')

    # ── Create .env ──
    # The trading module is no longer part of core (it ships as the separate
    # ``tofu-trading`` package); exports therefore no longer set
    # TRADING_ENABLED. Optional core modules default OFF here.
    env_file = dest / '.env'
    env_lines = [
        '# Auto-generated by export.py',
        '# PPTX translation module is OFF by default.',
        '# Set PPTX_TRANSLATE_ENABLED=1 to enable it.',
        'PPTX_TRANSLATE_ENABLED=0',
        '',
        '# This is a standalone single-machine copy. Never defer to a remote',
        '# PostgreSQL owner recorded in an inherited pgdata: on shared storage a',
        '# copy sees pgdata at the same absolute path as the source, so the',
        '# .pg_owner_host / heartbeat left by the source machine (or a previous',
        '# container) would otherwise route every DB call across a dead link and',
        '# crash with "connection ... timeout expired". With this flag set, the',
        '# copy clears the inherited marker on startup and owns PG locally.',
        '# Unset it ONLY for a same-path multi-host failover deployment.',
        'TOFU_PG_STANDALONE=1',
        '',
    ]
    env_file.write_text('\n'.join(env_lines))
    logger.info('Created .env (PPTX_TRANSLATE_ENABLED=0, TOFU_PG_STANDALONE=1)')

    # .env.example should have been copied from source.  Create a minimal
    # fallback only if the copy is missing (e.g. excluded by a filter).
    env_example = dest / '.env.example'
    if not env_example.exists():
        env_example.write_text(textwrap.dedent("""\
            # Tofu — Environment Configuration
            # Copy this file to .env and fill in your values:
            #   cp .env.example .env
            #
            # See README.md "Environment Variables" section for full reference.

            # LLM_API_KEYS=sk-key1,sk-key2
            # LLM_BASE_URL=https://api.openai.com/v1
            # LLM_MODEL=gpt-4o
            # PORT=15000
            # TRADING_ENABLED=0
            # PPTX_TRANSLATE_ENABLED=0
        """))

    # ── Create example provider template for opensource mode ──
    if mode == 'opensource':
        tpl_dir = dest / 'static' / 'provider_templates'
        tpl_dir.mkdir(parents=True, exist_ok=True)
        example_tpl = tpl_dir / 'openai.json'
        example_tpl.write_text(json.dumps({
            "key": "openai",
            "brand": "openai",
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "models": [
                {"model_id": "gpt-4.1",      "capabilities": ["text", "vision", "thinking", "cheap"], "rpm": 30,  "cost": 0.008},
                {"model_id": "gpt-4.1-mini",  "capabilities": ["text", "vision", "cheap"],    "rpm": 100, "cost": 0.002},
                {"model_id": "gpt-4.1-nano",  "capabilities": ["text", "cheap"],              "rpm": 200, "cost": 0.001},
                {"model_id": "gpt-image-2",   "capabilities": ["image_gen"],                  "rpm": 10,  "cost": 0.065},
                {"model_id": "gpt-image-1.5", "capabilities": ["image_gen"],                  "rpm": 10,  "cost": 0.043},
            ],
        }, indent=2, ensure_ascii=False) + '\n')
        logger.info('Created example provider template: openai.json')


# ═══════════════════════════════════════════════════════════
#  Fast path: tar-pipe copy + targeted rg-driven sanitize
# ═══════════════════════════════════════════════════════════

def _export_via_tar_with_sanitize(mode: str, dest: Path,
                                  progress: bool = False,
                                  worktree: bool = False) -> dict:
    """Fast non-personal export: bulk tar copy + post-pass sanitize.

    This is the internal/opensource analogue of ``_export_personal_via_tar``.
    Workflow:
      1. Stream the source tree through ``tar c | tar x`` with all the
         per-mode exclude flags (NOT the legacy os.walk/copy_file loop).
      2. After bulk copy, run a single ``rg`` search for sanitization
         triggers; for each hit, read+rewrite that file in place.
      3. Purge runtime artifacts the legacy path would have skipped (logs/,
         .lock files, key_stats.json, etc.) — same logic as before.

    Returns a stats dict matching the legacy walk path's shape so the
    summary printer doesn't need to special-case anything.
    """
    dest.mkdir(parents=True, exist_ok=True)
    snapshot: Path | None = None
    src = ROOT
    if worktree:
        logger.warning('--worktree: exporting UNCOMMITTED worktree state')
        _torn_snapshot_guard(ROOT, allow_dirty=True)  # warn, never block
    else:
        snapshot = _stage_head_snapshot(ROOT)
        if snapshot is not None:
            src = snapshot
            dirty = _dirty_tracked_files(ROOT)
            if dirty:
                shown = ', '.join(dirty[:8]) + (f' … (+{len(dirty) - 8})' if len(dirty) > 8 else '')
                print(f"  {C_DIM}ℹ Export source = committed HEAD; {len(dirty)} dirty "
                      f"tracked file(s) NOT published (commit to include, or\n"
                      f"  --worktree to export uncommitted state): {shown}{C_END}")
        else:
            # Worktree fallback — the torn-snapshot hazard applies, so guard hard.
            _torn_snapshot_guard(ROOT, allow_dirty=False)
    tar_excludes, preserved = _build_tar_excludes_for_mode(mode, dest)
    if preserved:
        print(f"  {C_CYAN}\U0001f4be Preserving dest: {', '.join(preserved + ['/'])}"
              f"  (not overwritten from source){C_END}")

    print(f"  {C_DIM}Streaming tar pipe (src → dst) — fast path…{C_END}")
    try:
        _stream_tar_copy(src, dest, tar_excludes, progress=progress)
    finally:
        if snapshot is not None:
            shutil.rmtree(snapshot, ignore_errors=True)

    # Targeted sanitize pass — only opens files containing trigger patterns.
    print(f"  {C_DIM}Running targeted sanitize pass (rg-driven)…{C_END}")
    san_stats = _post_copy_sanitize(dest, mode)

    copied = _fast_count_files(dest)
    return {
        'copied': copied - san_stats['sanitized'],
        'sanitized': san_stats['sanitized'],
        'excluded': 0,            # tar handles exclusions silently — no per-file count
        'excluded_reasons': {},
    }


def _print_export_summary(dest: Path, mode: str, stats: dict,
                           excluded_log: list, all_excluded_dirs: set) -> None:
    """Print the standard end-of-export summary block.

    Extracted from ``export_project`` so both the fast path and the
    dry-run walk path produce identical output.
    """
    print(f"\n{C_BOLD}{chr(0x2500) * 64}{C_END}")
    print(f"  {C_GREEN}\u2713 Copied:    {stats['copied']}{C_END}")
    if stats.get('sanitized'):
        print(f"  {C_YELLOW}\U0001f527 Sanitized: {stats['sanitized']}{C_END}")
    if stats.get('excluded'):
        print(f"  {C_RED}\u2717 Excluded:  {stats['excluded']}{C_END}")

    if stats.get('excluded_reasons'):
        print(f"\n  {C_DIM}Exclusion breakdown:{C_END}")
        for reason, count in sorted(stats['excluded_reasons'].items(),
                                     key=lambda x: -x[1]):
            print(f"    {C_DIM}{reason}: {count}{C_END}")

    if excluded_log:
        print(f"\n  {C_DIM}Key excluded items:{C_END}")
        shown_dirs: set = set()
        for relpath, reason in excluded_log:
            parts = Path(relpath).parts
            for part in parts:
                if part in all_excluded_dirs or (
                    mode == 'opensource' and part in OPENSOURCE_EXTRA_EXCLUDE_DIRS
                ):
                    if part not in shown_dirs:
                        shown_dirs.add(part)
                        print(f"    {C_RED}\u2717{C_END} "
                              f"{C_DIM}{part}/ (entire directory){C_END}")
                    break
            else:
                print(f"    {C_RED}\u2717{C_END} {C_DIM}{relpath} \u2014 {reason}{C_END}")

    print(f"\n  {C_GREEN}{chr(0x2550) * 50}{C_END}")
    print(f"  {C_GREEN}\u2705 Export complete!{C_END}")
    print(f"  {C_GREEN}   {dest}{C_END}")
    print(f"  {C_GREEN}{chr(0x2550) * 50}{C_END}")
    print(f"\n  {C_BOLD}Next steps:{C_END}")
    print(f"  1. cd {dest}")
    if mode == 'personal':
        print(f"  2. python3 server.py          {C_DIM}# everything is ready as-is{C_END}")
    elif mode == 'internal':
        print(f"  2. python3 server.py          {C_DIM}# auto-creates SQLite database{C_END}")
    else:
        print(f"  2. cp .env.example .env       {C_DIM}# fill in your API key{C_END}")
        print(f"  3. python3 server.py          {C_DIM}# auto-creates SQLite database{C_END}")
    print(f"\n  {C_DIM}If running on the same machine as the original:{C_END}")
    print(f"  {C_DIM}  PORT=15001 python3 server.py  # use a different web port{C_END}")
    print(f"  {C_DIM}  (SQLite — no port conflicts possible){C_END}")
    print()


# Runtime/operator state that may exist in a dest doubling as a live
# install — preserved even by the opensource mirror cleanup. Everything else
# the export excludes is CONTENT (promo/, overleaf_cache/, workdirs, caches):
# content must not linger in a published mirror.
_OPENSOURCE_DEST_PRESERVE = {
    '.git',          # VCS metadata — required by the push flow itself
    'data',          # chat DB / configs (a dest doubling as a live install)
    'uploads',       # user uploads
    '.tofu',         # memories / skills / file-history (user data)
    '.chatui',       # legacy agent state (user data)
    'pgdata',        # PostgreSQL cluster (chat history)
    'pg_backups',    # SQL dumps (chat history)
    'logs',          # runtime diagnostics
}

# Per-host files that must NEVER survive into a re-exported dest, even though
# they're not in the source tree (so the source-name check would otherwise
# skip them). .tofu_env.json pins an absolute interpreter path; if a stale
# one from another host/user lingers, server.py re-execs into the WRONG conda
# env (missing deps → ModuleNotFoundError). It's in ALWAYS_EXCLUDE_FILES so
# it's never copied — the cleanup actively strips a pre-existing one.
_FORCE_STRIP_FROM_DEST = {'.tofu_env.json'}


def _dest_cleanup_targets(dest: Path, mode: str, source_names: set) -> list:
    """Top-level items of ``dest`` to delete before the tar copy.

    Non-opensource dests are LIVE INSTALLS: preserve user data AND every
    export-excluded dir (rm -rf'ing a 5k-file workdir is 100% wasted FUSE
    I/O — the next tar copy wouldn't put anything back there anyway), plus
    anything not in source (the user may have created it post-export).

    An opensource dest is a PUBLISH MIRROR of the export set, not a live
    install: preserve ONLY operator/runtime state
    (:data:`_OPENSOURCE_DEST_PRESERVE`); every other top-level item —
    including export-EXCLUDED content dirs (promo/, overleaf_cache/) and
    stale entries no longer in source — is deleted so the published tree
    equals the export set. Without this, excluded content committed once
    rides every subsequent ``git add -A`` forever: this is exactly how
    promo/ (~26MB) stayed on GitHub years after entering
    ALWAYS_EXCLUDE_DIRS (2026-06-10), doubling the update tarball.
    """
    if mode == 'opensource':
        preserve = _OPENSOURCE_DEST_PRESERVE
        mirror_delete = True
    else:
        preserve = ({'.git', 'data', 'uploads'} | ALWAYS_EXCLUDE_DIRS
                    | OPENSOURCE_EXTRA_EXCLUDE_DIRS
                    | _TOP_LEVEL_ONLY_EXCLUDE_DIRS)
        mirror_delete = False
    targets = []
    for item in dest.iterdir():
        if item.name in preserve:
            continue
        if item.name in _FORCE_STRIP_FROM_DEST:
            targets.append(item)
            continue
        if not mirror_delete and item.name not in source_names:
            # Not in source → tar won't write here → no need to delete.
            continue
        targets.append(item)
    return targets


# ═══════════════════════════════════════════════════════════
#  Main export logic
# ═══════════════════════════════════════════════════════════

def export_project(mode: str, dest: Path, dry_run: bool = False,
                   push: bool = False, commit_msg: str | None = None,
                   is_release: bool = False, branch_override: str | None = None,
                   progress: bool = False, force: bool = False,
                   worktree: bool = False):
    """Main export function.

    Args:
        mode: Sanitization level ('personal', 'internal', 'opensource').
        dest: Destination directory.
        dry_run: If True, preview only — no files written.
        push: If True, auto-push to the configured upstream git repo after export.
        commit_msg: Custom git commit message (only used when push=True).
        is_release: If True, create a git tag for the current version.
        progress: If True, stream a live per-file transfer view during the
            tar copy so the operator can watch the transfer and spot any
            files that shouldn't be carried.
    """
    if dest.exists():
        print(f"\n  {C_YELLOW}\u26a0 Destination already exists: {dest} — overwriting{C_END}")
        print(f"  {C_CYAN}\U0001f4be Destination .git/, data/, and uploads/ will be preserved{C_END}")
        if not dry_run:
            # ── Selective cleanup: delete everything EXCEPT preserved dirs ──
            # The preserve/delete policy lives in _dest_cleanup_targets (mode-
            # aware): live-install dests preserve user data + excluded dirs +
            # non-source items; an opensource publish dest is a MIRROR of the
            # export set and keeps only _OPENSOURCE_DEST_PRESERVE.
            try:
                source_names = {p.name for p in ROOT.iterdir()}
            except OSError:
                source_names = set()
            targets = _dest_cleanup_targets(dest, mode, source_names)

            if targets:
                print(f"  {C_DIM}Cleaning {len(targets)} stale items from prior export…{C_END}")
                if _have_tool('rm'):
                    try:
                        subprocess.run(
                            ['rm', '-rf', '--', *[str(t) for t in targets]],
                            check=False,
                        )
                    except OSError as e:
                        logger.warning('rm -rf failed, falling back to rmtree: %s', e)
                        for t in targets:
                            if t.is_dir():
                                shutil.rmtree(t, ignore_errors=True)
                            else:
                                t.unlink(missing_ok=True)
                else:
                    for t in targets:
                        try:
                            if t.is_dir():
                                shutil.rmtree(t, ignore_errors=True)
                            else:
                                t.unlink(missing_ok=True)
                        except OSError as e:
                            logger.warning('Failed to remove %s: %s', t, e)
            logger.info('Cleanup: removed %d items from dest (mode=%s)',
                        len(targets), mode)

    print(f"\n{C_BOLD}{chr(0x2550) * 64}{C_END}")
    print(f"{C_BOLD}  \U0001f4e6 Tofu Export \u2014 Mode: {mode.upper()}  (v{_read_version()}){C_END}")
    print(f"{C_BOLD}{chr(0x2550) * 64}{C_END}")
    print(f"  Source:  {C_DIM}{ROOT}{C_END}")
    print(f"  Dest:    {C_DIM}{dest}{C_END}")
    if dry_run:
        print(f"  {C_YELLOW}\U0001f50d DRY RUN — no files will be written{C_END}")
        if mode != 'personal' and not worktree:
            print(f"  {C_DIM}ℹ The real export copies committed HEAD (git archive), not the\n"
                  f"    worktree walked below — uncommitted files listed here do NOT ship.{C_END}")
            _torn_snapshot_guard(ROOT, allow_dirty=True, dry_run=True)
    print()

    # Set module-level version for sanitization pipeline
    global _EXPORT_VERSION
    _EXPORT_VERSION = _read_version()

    # ── Fast path: ALL non-dry-run modes use streaming tar copy ──
    #
    # Per-file ``shutil.copy2`` is dog-slow on FUSE (~5 round-trips per file:
    # open/read/write/chmod/utime). On a tree of ~7k files across DolphinFS
    # that's the difference between ~5s (tar pipe) and 5+ minutes (per-file
    # walk).  Sanitization is then a TARGETED post-pass over only the files
    # that actually contain a sanitization trigger (located via a single rg
    # invocation), which for internal mode is ~4 files and for opensource
    # ~30-50 files.
    #
    # Personal mode runs the same tar pipe but skips the sanitize pass.
    # Dry-run still uses the legacy walk so users can preview what would
    # be sanitized without writing anything.
    if not dry_run:
        if mode == 'personal':
            stats = _export_personal_via_tar(dest, progress=progress)
            excluded_log: list = []
            _all_excluded_dirs = PERSONAL_EXCLUDE_DIRS
            # Personal mode skips _create_skeleton (it mirrors source data/
            # verbatim), but the sibling MCP repos live OUTSIDE the source
            # tree (ROOT.parent) so the tar copy never picks them up. Bundle
            # them explicitly, then rewrite the copied mcp_servers.json from
            # absolute `uvx --from /path` entries to bare launchers so it's
            # portable to another machine.
            _bundle_internal_mcp_repos(dest, mode)
            _bundle_tofu_search_wheel(dest, mode)
            _portablize_bundled_mcp_config(dest, mode)
        else:
            stats = _export_via_tar_with_sanitize(mode, dest, progress=progress,
                                                  worktree=worktree)
            excluded_log = []
            _all_excluded_dirs = (ALWAYS_EXCLUDE_DIRS
                                  if mode == 'internal'
                                  else ALWAYS_EXCLUDE_DIRS | OPENSOURCE_EXTRA_EXCLUDE_DIRS)

        # ── Skeleton (creates data/, logs/, .env, etc.) ──
        # Personal mode's tar already includes data/ and uploads/ so we skip
        # for it; internal/opensource need it because data/ was excluded.
        if mode != 'personal':
            _create_skeleton(dest, mode)

        # Render summary and exit (skip the legacy walk below).
        _print_export_summary(dest, mode, stats, excluded_log, _all_excluded_dirs)

        # Post-export tasks: lint (opensource only), verify, push.
        if mode == 'opensource':
            _restore_opensource_kept_files(dest)
            _run_ruff_autofix(dest)
            _verify_opensource(dest)
        # JS syntax safety net (internal + opensource both sanitize JS). Runs
        # BEFORE push so a sanitizer regression that corrupts JS fails the
        # export loudly instead of shipping a white-screening tree.
        if mode in ('internal', 'opensource'):
            _verify_exported_js_syntax(dest)
            _verify_exported_py_syntax(dest)
            _verify_exported_py_integrity(dest)
        if push:
            _git_push(dest, mode, commit_msg, is_release=is_release,
                      branch_override=branch_override, force=force)
        return

    # ── Dry-run: walk-based preview (no files written) ──
    stats = {
        'copied': 0,
        'sanitized': 0,
        'excluded': 0,
        'excluded_reasons': {},
    }

    # Collect the right dir set for excluded-items display
    _all_excluded_dirs = (PERSONAL_EXCLUDE_DIRS if mode == 'personal'
                          else ALWAYS_EXCLUDE_DIRS | ALWAYS_EXCLUDE_ROOT_ONLY_DIRS)

    excluded_log = []

    # Mirror the real tar-copy path: internal/opensource also skip untracked,
    # non-gitignored ROOT dirs (agent scratch). ``_should_exclude`` is a pure
    # per-file predicate with no git knowledge, so consult the same set here —
    # otherwise the preview would list ``module1/`` etc. as "will copy" while
    # the real run drops them. Computed once (one git call) before the walk.
    _stray_root_dirs: set[str] = (
        set() if mode == 'personal' else _untracked_root_dirs(ROOT))
    # Same alignment for untracked FILES in tracked dirs — the preview must
    # count exactly what the real copy drops.
    _stray_files: set[str] = (
        set() if mode == 'personal'
        else _untracked_nested_files(ROOT, _stray_root_dirs))

    for dirpath, dirnames, filenames in os.walk(ROOT):
        # Compute relative path
        rel_dir = os.path.relpath(dirpath, ROOT)
        if rel_dir == '.':
            rel_dir = ''

        # Pre-filter directories (modifying dirnames in-place skips subtrees).
        # At the root, also prune stray untracked dirs and log them so the
        # dry-run's exclude count matches what the real copy actually drops.
        kept = []
        for d in sorted(dirnames):
            if not rel_dir and d in _stray_root_dirs:
                stats['excluded'] += 1
                reason = 'untracked non-gitignored root dir'
                stats['excluded_reasons'][reason] = \
                    stats['excluded_reasons'].get(reason, 0) + 1
                excluded_log.append((d, f'{reason}: {d}'))
                continue
            if _should_exclude(
                    os.path.join(rel_dir, d) if rel_dir else d, d, mode):
                continue
            kept.append(d)
        dirnames[:] = kept

        for filename in sorted(filenames):
            relpath = os.path.join(rel_dir, filename) if rel_dir else filename
            src_path = Path(dirpath) / filename

            # Untracked files in tracked dirs (uncommitted WIP, tmp scratch) —
            # the real tar copy drops them; the preview must count them too.
            if relpath in _stray_files:
                stats['excluded'] += 1
                reason = 'untracked non-gitignored file'
                stats['excluded_reasons'][reason] = \
                    stats['excluded_reasons'].get(reason, 0) + 1
                excluded_log.append((relpath, f'{reason}: {relpath}'))
                continue

            # Check exclusion
            reason = _should_exclude(relpath, filename, mode)
            if reason:
                stats['excluded'] += 1
                cat = reason.split(':')[0].strip()
                stats['excluded_reasons'][cat] = stats['excluded_reasons'].get(cat, 0) + 1
                excluded_log.append((relpath, reason))
                continue

            # Dry-run only: show what would happen without writing.
            if mode in ('internal', 'opensource') and _is_text_file(str(src_path)):
                try:
                    content = src_path.read_text(encoding='utf-8', errors='replace')
                    sanitized = _sanitize_defaults_for_export(
                        content, relpath, version=_EXPORT_VERSION)
                    if mode == 'opensource':
                        sanitized = _sanitize_source_opensource(sanitized, relpath)
                    if sanitized != content:
                        print(f"  {C_YELLOW}\U0001f527{C_END} {relpath} "
                              f"{C_DIM}(would sanitize){C_END}")
                        stats['sanitized'] += 1
                        continue
                except Exception as e:
                    logger.debug('[export] dry-run sanitize preview failed for '
                                 '%s: %s', relpath, e)
            print(f"  {C_GREEN}\U0001f4c4{C_END} {relpath}")
            stats['copied'] += 1

    # ── Dry-run summary ──
    print(f"\n{C_BOLD}{chr(0x2500) * 64}{C_END}")
    print(f"  {C_GREEN}\u2713 Would copy:    {stats['copied']}{C_END}")
    if stats['sanitized']:
        print(f"  {C_YELLOW}\U0001f527 Would sanitize: {stats['sanitized']}{C_END}")
    print(f"  {C_RED}\u2717 Would exclude:  {stats['excluded']}{C_END}")
    if stats['excluded_reasons']:
        print(f"\n  {C_DIM}Exclusion breakdown:{C_END}")
        for reason, count in sorted(stats['excluded_reasons'].items(),
                                     key=lambda x: -x[1]):
            print(f"    {C_DIM}{reason}: {count}{C_END}")
    print(f"\n  {C_CYAN}Dry run complete \u2014 no files written.{C_END}\n")


def _restore_opensource_kept_files(dest: Path):
    """Copy whitelisted build-required files back into an opensource export.

    The opensource tar copy excludes whole dirs like ``scripts/`` (they
    contain benchmark scripts with internal paths/keys). But a few files in
    those dirs ARE needed by the public build — see ``_OPENSOURCE_KEEP_FILES``.
    Restore each one verbatim from the source tree after the tar copy.
    """
    restored = []
    for rel in sorted(_OPENSOURCE_KEEP_FILES):
        src = ROOT / rel
        if not src.is_file():
            logger.warning('Keep-file missing from source, skipping: %s', rel)
            continue
        target = dest / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            restored.append(rel)
        except OSError as e:
            logger.warning('Failed to restore keep-file %s: %s', rel, e)
    if restored:
        print(f"  {C_GREEN}📌 Restored build-required files (opensource){C_END}")
        print(f"     {C_DIM}{', '.join(restored)}{C_END}")


def _run_ruff_autofix(dest: Path):
    """Run ruff --fix on the exported code to auto-fix lint issues.

    This ensures the exported code stays clean even if the source has minor
    style drift (import sorting, deprecated typing imports, Optional→X|None,
    etc.).  All auto-fixable rules are applied.

    CRITICAL: ``--unfixable F401,F811`` forbids the autofix from ever DELETING
    imports (or duplicate defs), even though those diagnostics are still
    reported. Tofu's facade-preserving split packages import symbols in a
    submodule purely so the package ``__init__`` can re-export them; ruff sees
    those as "unused" (F401) and, left to its own devices, would delete them —
    silently breaking ``import lib.mcp`` / ``import lib.tasks_pkg.orchestrator``
    at boot in the shipped opensource build. The source tree is already
    lint-clean, so the export never needs to strip imports to stay green;
    removing an import is pure downside here. Keep this guard.
    """
    print(f"\n  {C_BOLD}🔧 Running ruff auto-fix on exported code…{C_END}")
    try:
        result = subprocess.run(
            ['python', '-m', 'ruff', 'check', '--fix', '--unsafe-fixes',
             '--unfixable', 'F401,F811',
             str(dest / 'lib'), str(dest / 'routes'), str(dest / 'tests')],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout or '').strip()
        if 'error' in output.lower() or result.returncode != 0:
            # Some unfixable errors remain — that's OK, just report
            fixed_match = re.search(r'(\d+) fixed', output)
            remain_match = re.search(r'(\d+) remaining', output)
            fixed = fixed_match.group(1) if fixed_match else '0'
            remaining = remain_match.group(1) if remain_match else '?'
            print(f"    {C_YELLOW}Auto-fixed {fixed} issues, {remaining} remaining{C_END}")
        elif 'Found' in output:
            fixed_match = re.search(r'(\d+) fixed', output)
            fixed = fixed_match.group(1) if fixed_match else '0'
            print(f"    {C_GREEN}✅ Auto-fixed {fixed} lint issues{C_END}")
        else:
            print(f"    {C_GREEN}✅ No lint issues found{C_END}")
    except FileNotFoundError:
        print(f"    {C_DIM}ruff not installed — skipping auto-fix{C_END}")
    except subprocess.TimeoutExpired:
        print(f"    {C_YELLOW}ruff timed out — skipping{C_END}")
    except Exception as e:
        print(f"    {C_YELLOW}ruff auto-fix failed: {e}{C_END}")


class ExportSyntaxError(RuntimeError):
    """Raised when the sanitizer produced JavaScript that fails to parse.

    This is a HARD failure: shipping invalid JS white-screens every user of
    the exported build (the served bundle is a naive concat of these files —
    see lib/js_bundler.py). Fail the export loudly at build time rather than
    on some user's fresh install.
    """


def _verify_exported_js_syntax(dest: Path) -> None:
    """Best-effort ``node --check`` over every exported ``.js`` file.

    A safety net for sanitizer-induced corruption: any find-replace in
    ``_sanitize_source_opensource`` / ``_sanitize_defaults_for_export`` that
    rewrites a code identifier into a non-identifier token (the classic
    ``meituan`` → ``your-provider`` unquoted-key bug) produces invalid JS that
    the bundler would happily ship. This catches that class at EXPORT time,
    file-agnostically, so a regression can never reach a user.

    No-op when ``node`` is absent (mirrors ``lib/js_bundler._node_syntax_ok`` —
    fresh-install machines commonly lack node; the class fix in the sanitizer
    is the primary defense, this is defense-in-depth for the build host).

    Raises:
        ExportSyntaxError: if any exported ``.js`` file fails ``node --check``.
    """
    node = shutil.which('node')
    if not node:
        print(f"  {C_DIM}\u23ed node not found \u2014 skipping JS syntax verification{C_END}")
        return

    js_files = [p for p in (dest / 'static' / 'js').rglob('*.js')
                if p.is_file() and not p.name.startswith('bundle-')]
    print(f"  {C_BOLD}\U0001f50d Verifying exported JS syntax ({len(js_files)} files)\u2026{C_END}")

    broken: list[tuple[str, str]] = []
    for p in js_files:
        try:
            r = subprocess.run(
                [node, '--check', str(p)],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning('node --check could not run on %s: %s', p, e)
            continue
        if r.returncode != 0:
            rel = p.relative_to(dest)
            detail = (r.stderr or r.stdout or '').strip().splitlines()
            first = detail[0] if detail else 'syntax error'
            broken.append((str(rel), first))
            print(f"    {C_RED}\u2717 {rel}: {first}{C_END}")

    if broken:
        print(f"\n  {C_RED}\u26a0 {len(broken)} exported JS file(s) FAIL to parse \u2014 "
              f"the sanitizer corrupted them.{C_END}")
        raise ExportSyntaxError(
            'Sanitizer produced %d syntactically-invalid JS file(s): %s'
            % (len(broken), ', '.join(f'{rel} ({err})' for rel, err in broken))
        )
def _verify_exported_py_syntax(dest: Path) -> None:
    """``ast.parse`` every exported ``.py`` — the Python twin of the JS check.

    The 2026-08-05 incident proved the gap: the sanitizer hyphenated an
    identifier in tests/test_gateway_sanitize.py, the JS-only net never looked
    at it, and the corruption was only caught because CI lints the whole tree.
    ast.parse is fast (~1k files in seconds) and needs no toolchain, so this
    runs unconditionally. Scope is the whole exported tree (CI runs
    ``ruff check .`` over everything, including tests/ and scripts/).

    Raises:
        ExportSyntaxError: if any exported ``.py`` file fails to parse.
    """
    py_files = [p for p in dest.rglob('*.py')
                if p.is_file() and '.git' not in p.parts]
    print(f"  {C_BOLD}\U0001f50d Verifying exported Python syntax ({len(py_files)} files)\u2026{C_END}")

    broken: list[tuple[str, str]] = []
    for p in py_files:
        try:
            src = p.read_text(encoding='utf-8')
            ast.parse(src, filename=str(p))
        except SyntaxError as e:
            rel = p.relative_to(dest)
            broken.append((str(rel), f'{e.msg} (line {e.lineno})'))
            print(f"    {C_RED}\u2717 {rel}: {e.msg} (line {e.lineno}){C_END}")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning('py-syntax: could not read %s: %s', p, e)

    if broken:
        print(f"\n  {C_RED}\u26a0 {len(broken)} exported Python file(s) FAIL to parse \u2014 "
              f"the sanitizer corrupted them.{C_END}")
        raise ExportSyntaxError(
            'Sanitizer produced %d syntactically-invalid Python file(s): %s'
            % (len(broken), ', '.join(f'{rel} ({err})' for rel, err in broken))
        )


class ExportIntegrityError(RuntimeError):
    """Raised when the exported Python tree is missing/truncated vs the source.

    The classic failure this guards against: a facade ``__init__.py`` that
    re-exports symbols from a submodule ends up 0 bytes in the export (a
    concurrent export into the same ``--dest`` + a cross-DC FUSE atomic-move
    window can leave a half-written file). A 0-byte file still ``py_compile``s
    cleanly, so a syntax check would NOT catch it — only a byte-size check
    against the source does. The result is a tree that installs "successfully"
    yet crashes at boot with ``ImportError: cannot import name X`` the moment
    ``routes/__init__.py`` touches the empty facade. Fail the export loudly at
    build time instead of shipping that.
    """


def _verify_exported_py_integrity(dest: Path) -> None:
    """Verify every tracked source ``.py`` copied to ``dest`` non-empty.

    Compares the git-tracked Python file list (fast, local) against the export:
    a file that is non-empty in the source but MISSING or 0 bytes in ``dest``
    is a corrupted copy. Each dest stat is retried a few times to absorb
    cross-datacenter FUSE latency / atomic-move windows (a spurious ENOENT on a
    remote mount would otherwise flag a file that is actually fine).

    Raises:
        ExportIntegrityError: if any tracked source ``.py`` is missing or
            truncated to 0 bytes in the export.
    """
    try:
        listed = subprocess.run(
            ['git', 'ls-tree', '-r', '--name-only', _EXPORT_SOURCE_SHA or 'HEAD'],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning('py-integrity: git ls-files failed (%s) — skipping check', e)
        print(f"  {C_DIM}\u23ed git ls-files unavailable \u2014 skipping Python integrity check{C_END}")
        return

    rels = [ln.strip() for ln in (listed.stdout or '').splitlines()
            if ln.strip().endswith('.py')
            and (ln.strip().startswith(('lib/', 'routes/'))
                 or ln.strip() in ('server.py', 'bootstrap.py'))]
    print(f"  {C_BOLD}\U0001f50d Verifying exported Python integrity ({len(rels)} files)\u2026{C_END}")

    def _size_with_retry(p: Path) -> int:
        """Return file size, retrying to ride out FUSE ENOENT flakiness.

        Returns -1 only if every attempt failed (treated as truly missing).
        """
        for attempt in range(5):
            try:
                return p.stat().st_size
            except OSError as e:
                logger.debug('py-integrity stat retry %d for %s: %s', attempt, p, e)
                time.sleep(0.4)
        return -1

    bad: list[tuple[str, str]] = []
    for rel in rels:
        # Only flag files that are genuinely non-empty in the source: an empty
        # source file (rare) copying to empty is not corruption.
        try:
            if (ROOT / rel).stat().st_size == 0:
                continue
        except OSError:
            continue
        size = _size_with_retry(dest / rel)
        if size < 0:
            bad.append((rel, 'missing'))
            print(f"    {C_RED}\u2717 {rel}: missing{C_END}")
        elif size == 0:
            bad.append((rel, '0 bytes'))
            print(f"    {C_RED}\u2717 {rel}: 0 bytes (truncated copy){C_END}")

    if bad:
        print(f"\n  {C_RED}\u26a0 {len(bad)} exported Python file(s) missing/truncated \u2014 "
              f"the export tree is incomplete.{C_END}")
        print(f"  {C_DIM}Likely cause: a concurrent export into the same --dest, or a "
              f"cross-DC FUSE write interrupted mid-copy. Re-run the export (ensure no "
              f"other export.py targets this dest).{C_END}")
        raise ExportIntegrityError(
            'Export tree incomplete: %d tracked .py file(s) missing/empty: %s'
            % (len(bad), ', '.join(f'{rel} ({why})' for rel, why in bad))
        )
    print(f"  {C_GREEN}\u2705 All exported Python files present and non-empty.{C_END}")


def _verify_opensource(dest: Path):
    """Post-export verification: scan exported files for leaked secrets.

    Uses a single ``rg`` invocation per pattern (each one returns
    ``path:line:matched-text`` for direct reporting). Falls back to a
    Python ``os.walk`` + ``re.finditer`` only when rg is unavailable.

    rg is dramatically faster on large trees because it walks the
    filesystem in parallel and uses SIMD-accelerated regex matching.
    """
    print(f"\n  {C_BOLD}\U0001f50d Post-export secret scan\u2026{C_END}")

    leak_patterns = [
        (r'2003386484270948427',              'API key (primary)'),
        (r'2031327690221944861',              'API key (secondary)'),
        (r'q2E1KDjuDhasILLK0urXCgqbss6vhmXf','Feishu APP_SECRET'),
        (r'cli_a924e60a7cb85bcc',             'Feishu APP_ID'),
        (r'aigc\.sankuai\.com',               'Internal API endpoint'),
        (r'\.sankuai\.com',                   'Internal domain (.sankuai.com)'),
        (r'\.meituan\.com',                   'Internal domain (.meituan.com)'),
        (r'hadoop-aipnlp',                    'Internal username (hadoop-aipnlp)'),
        (r'ruanjunhao04',                     'Internal username (ruanjunhao04)'),
        (r'/mnt/dolphinfs/',                  'Internal filesystem path'),
        (r'M-TransferContext-INF-CELL',       'Internal HTTP header name'),
        (r'gray-release-ai-gpt-test',         'Internal gray-release config value'),
    ]

    leaks_found = 0

    if _have_tool('rg'):
        # One rg invocation per pattern — keeps the description neat in
        # the output. We use --json for stable parsing across rg versions.
        for pat, desc in leak_patterns:
            try:
                r = subprocess.run(
                    ['rg', '--no-messages', '--no-ignore-vcs', '--hidden',
                     '--max-filesize', _RG_MAX_FILESIZE,
                     '-n', '-e', pat, str(dest)],
                    capture_output=True, text=True, timeout=120,
                    stdin=subprocess.DEVNULL,
                )
                if r.returncode not in (0, 1):
                    logger.warning('rg verify rc=%s for pattern %s: %s',
                                   r.returncode, pat, (r.stderr or '')[:200])
                    continue
                for line in r.stdout.splitlines():
                    # Format: <abs-path>:<lineno>:<matched line>
                    parts = line.split(':', 2)
                    if len(parts) < 3:
                        continue
                    fpath, lineno = parts[0], parts[1]
                    try:
                        rel = Path(fpath).relative_to(dest)
                    except ValueError:
                        rel = Path(fpath).name
                    print(f"    {C_RED}\u26a0 LEAK:{C_END} {rel}:{lineno} \u2014 {desc}")
                    leaks_found += 1
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning('rg verify failed for %s: %s', pat, e)
        # ── Oversized-file supplement (SECURITY) ──
        # rg skipped every file above _RG_MAX_FILESIZE, so the verify above
        # cannot see a leak living in an oversized text file. Scan those files
        # in-process against the SAME patterns so verification can never give a
        # false "no secrets" all-clear on an oversized leak.
        for dirpath, _, filenames in os.walk(dest):
            for filename in filenames:
                fpath = Path(dirpath) / filename
                if not _is_text_file(str(fpath)):
                    continue
                try:
                    if fpath.stat().st_size <= _RG_MAX_FILESIZE_BYTES:
                        continue  # rg already scanned it above
                    content = fpath.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
                for pat, desc in leak_patterns:
                    for m in re.finditer(pat, content):
                        line_num = content[:m.start()].count('\n') + 1
                        rel = fpath.relative_to(dest)
                        print(f"    {C_RED}\u26a0 LEAK:{C_END} {rel}:{line_num} "
                              f"\u2014 {desc} (oversized file — rg skipped)")
                        leaks_found += 1
    else:
        # Python fallback — slow but always works.
        for dirpath, _, filenames in os.walk(dest):
            for filename in filenames:
                fpath = Path(dirpath) / filename
                if not _is_text_file(str(fpath)):
                    continue
                try:
                    content = fpath.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
                for pat, desc in leak_patterns:
                    for m in re.finditer(pat, content):
                        line_num = content[:m.start()].count('\n') + 1
                        rel = fpath.relative_to(dest)
                        print(f"    {C_RED}\u26a0 LEAK:{C_END} {rel}:{line_num} \u2014 {desc}")
                        leaks_found += 1

    if leaks_found:
        print(f"\n  {C_RED}\u26a0 Found {leaks_found} potential secret leak(s)!{C_END}")
        print(f"  {C_RED}  Please review and fix before publishing.{C_END}\n")
    else:
        print(f"  {C_GREEN}\u2705 No secrets found in exported files.{C_END}\n")


# ═══════════════════════════════════════════════════════════
#  Git Auto-Push
# ═══════════════════════════════════════════════════════════

# Upstream repo config.
# Token lives OUTSIDE the repo (owner directive 2026-08-05): env TOFU_GH_TOKEN,
# else the chmod-600 file in the sibling .secrets/ dir. Never hardcode it here —
# this file is internal-only today, but a credential in any committable file is
# one whitelist mistake away from a public push.
def _load_gh_token() -> str:
    """Resolution order: TOFU_GH_TOKEN env → the credential vault (auto-
    bootstrapping from the legacy .secrets/ file) → the legacy file itself."""
    tok = os.environ.get('TOFU_GH_TOKEN', '').strip()
    if tok:
        return tok
    secrets_dir = Path(__file__).resolve().parent.parent / '.secrets'
    try:
        from lib.credentials_vault import bootstrap_from_legacy, get_entry
        bootstrap_from_legacy(secrets_dir)
        tok = (get_entry('github_token') or '').strip()
        if tok:
            return tok
    except Exception as e:
        logger.warning('[Export] vault read for github_token failed, '
                       'falling back to .secrets file: %s', e)
    p = secrets_dir / 'github_token'
    try:
        tok = p.read_text(encoding='utf-8').strip()
    except OSError as e:
        raise SystemExit(
            f'GitHub token not found: set TOFU_GH_TOKEN, store it in the '
            f'credential vault (Settings → 凭证保管库), or create {p} '
            f'(chmod 600). ({e})')
    if not tok:
        raise SystemExit(f'GitHub token file {p} is empty.')
    return tok


_GH_TOKEN = _load_gh_token()
_GIT_REPOS = {
    'opensource': {
        'remotes': [
            {'name': 'origin',   'url': f'https://{_GH_TOKEN}@github.com/rangehow/ToFu.git'},
            {'name': 'niutrans', 'url': f'https://{_GH_TOKEN}@github.com/NiuTrans/ToFu.git'},
        ],
        'branch': 'main',
    },
    'internal': {
        'remotes': [
            {'name': 'origin', 'url': f'https://{_GH_TOKEN}@github.com/rangehow/tofu-meituan.git'},
        ],
        'branch': 'main',
    },
}


def _is_nonff_push_rejection(err_text: str) -> bool:
    """True when a failed ``git push`` was a non-fast-forward REJECTION.

    A force-push only makes sense when the remote rejected the update because
    its history diverged (the normal case for a freshly re-``git init``'d
    export tree that shares no ancestry with the mirror). For any OTHER push
    failure — authentication, DNS/connection, permission, protocol, missing
    repo — force-pushing is useless at best and destructive at worst, so the
    caller re-raises instead. Matching is on the git stderr text (lower-cased).

    Args:
        err_text: The combined git stderr/stdout from the failed push.

    Returns:
        True iff the text carries a recognised non-fast-forward / rejected
        signal; False for auth/network/other errors (and empty input).
    """
    if not err_text:
        return False
    low = err_text.lower()
    # Signals git emits specifically for a diverged / behind rejection.
    reject_markers = (
        'non-fast-forward',
        '[rejected]',
        'failed to push some refs',
        'fetch first',
        'tip of your current branch is behind',
        'updates were rejected',
    )
    if not any(m in low for m in reject_markers):
        return False
    # Guard against false positives: an auth/network failure never carries a
    # non-ff marker, but if git ever combines messages, an explicit auth/host
    # failure should NOT be treated as a safe-to-force divergence.
    hard_failures = (
        'authentication failed',
        'could not read username',
        'permission denied',
        'could not resolve host',
        'connection refused',
        'repository not found',
    )
    if any(h in low for h in hard_failures):
        return False
    return True


def _tag_push_action(ls_remote_out: str, local_sha: str, force: bool) -> str:
    """Decide what to do about a release tag on a remote (pure, testable).

    Returns one of: 'push' (not on remote), 'skip-same' (already on remote at
    the same commit), 'force' (on remote at a DIFFERENT commit and --force was
    given — the only case where a published tag may move), 'keep' (different
    commit, no --force — keep the published tag, warn loudly).
    """
    if not ls_remote_out.strip():
        return 'push'
    remote_sha = ls_remote_out.split()[0]
    if remote_sha == local_sha:
        return 'skip-same'
    return 'force' if force else 'keep'


def _push_branch(git_run, rname: str, branch: str, force: bool = False) -> None:
    """Push ``branch`` to ``rname`` — the default path NEVER destroys remote history.

    Policy (owner-ratified 2026-07-25, epic pt_6598ae21 — \"most long-term
    robust\"): when the remote rejects the push as non-fast-forward (the
    normal case for a freshly re-``git init``'d export tree, which shares no
    ancestry with the mirror), the default resolution PRESERVES the remote
    history: fetch the diverged tip and merge it with ``-s ours
    --allow-unrelated-histories`` — the merge result tree is byte-identical
    to the export snapshot (the mirror's content is always the latest
    export), while the remote's own commits stay reachable in the DAG,
    turning the retry into a fast-forward. This also works on remotes whose
    branch is PROTECTED against force-push (GitHub protected branches reject
    ``--force`` outright; the preserve-merge pushes fine).

    ``force=True`` (explicit ``--force``) restores the old overwrite behavior
    for a deliberate mirror reset. Any failure that is NOT a divergence
    (auth / network / permission / missing repo) re-raises unchanged —
    forcing or merging there would be useless-or-destructive.

    Args:
        git_run: callable running a git argv list in the export repo,
            raising RuntimeError on non-zero exit (the ``_run`` closure of
            ``_git_push``, or a test harness equivalent).
        rname: remote name. branch: branch name.
        force: explicit overwrite opt-in.
    """
    try:
        git_run(['git', 'push', rname, branch])
        return
    except RuntimeError:
        # First push to a new branch — try setting upstream.
        try:
            git_run(['git', 'push', '-u', rname, branch])
            return
        except RuntimeError as upstream_err:
            if not _is_nonff_push_rejection(str(upstream_err)):
                raise
    if force:
        print(f"  {C_YELLOW}\u26a0 History diverged on {rname}/{branch}. "
              f"--force given: force-pushing (previous remote commits will be lost).{C_END}")
        logger.warning('Force-pushing %s/%s after non-ff rejection (--force).',
                       rname, branch)
        git_run(['git', 'push', '--force', rname, branch])
        return
    # Preserve remote history: graft the diverged tip into our DAG with an
    # empty-content merge (-s ours keeps OUR tree byte-identical; their
    # commits stay reachable), making the retry a plain fast-forward.
    print(f"  {C_CYAN}\u2139 Remote {rname}/{branch} diverged — preserving remote "
          f"history via ours-merge (content = this export; use --force to overwrite).{C_END}")
    logger.info('Non-ff divergence on %s/%s — preserving remote history via '
                'ours-merge (no force).', rname, branch)
    git_run(['git', 'fetch', rname, branch])
    git_run(['git', 'merge', '-s', 'ours', '--allow-unrelated-histories',
             'FETCH_HEAD', '-m',
             f'Preserve {rname}/{branch} history before export (content unchanged)'])
    git_run(['git', 'push', rname, branch])


def _push_tag(git_run, rname: str, tag_name: str, force: bool = False) -> None:
    """Push a release tag — never silently MOVES a published tag.

    A tag that already exists on the remote at a DIFFERENT commit is only
    re-tagged with explicit ``--force`` (moving a published tag breaks every
    downstream pin); without it we keep the remote tag and warn loudly.
    Best-effort: tag-push failures never fail the export (branch already
    pushed), but every skip/failure is logged.
    """
    try:
        out = git_run(['git', 'ls-remote', '--tags', rname, tag_name])
        local = git_run(['git', 'rev-list', '-n', '1', tag_name])
    except RuntimeError as e:
        logger.warning('Tag probe for %s on %s failed: %s — skipping tag push',
                       tag_name, rname, e)
        return
    action = _tag_push_action(out, local, force)
    try:
        if action == 'push':
            git_run(['git', 'push', rname, tag_name])
        elif action == 'skip-same':
            logger.info('Tag %s already on %s at the same commit — nothing to do',
                        tag_name, rname)
        elif action == 'force':
            logger.warning('Remote tag %s on %s moved by --force (local %s)',
                           tag_name, rname, local[:8])
            git_run(['git', 'push', '--force', rname, tag_name])
        else:  # keep
            print(f"  {C_YELLOW}\u26a0 Tag {tag_name} already exists on {rname} at a "
                  f"different commit — keeping the published tag (use --force to move it).{C_END}")
            logger.warning('Tag %s exists on %s at a different commit (local %s) — '
                           'NOT moving it without --force', tag_name, rname, local[:8])
    except RuntimeError as e:
        logger.warning('Tag push to %s failed: %s', rname, e)


def _git_push(dest: Path, mode: str, commit_msg: str | None = None,
              is_release: bool = False, branch_override: str | None = None,
              force: bool = False):
    """Initialize git (if needed), commit all files, and push to upstream(s).

    Supports pushing to multiple remotes (e.g. opensource pushes to both
    rangehow/ToFu and NiuTrans/ToFu).  The commit is created once, then
    pushed to each configured remote in turn.

    Uses normal (non-force) push since .git is preserved across re-exports,
    maintaining proper incremental commit history.

    Git tags are only created when is_release=True (i.e. --bump was used).
    This prevents random version tags from being created on every push.

    Args:
        dest: The exported project directory.
        mode: 'opensource' or 'internal'.
        commit_msg: Custom commit message.  Falls back to a timestamped default.
        is_release: If True, create a git tag for the current version.
        branch_override: Override the default branch name.
    """
    # Internal mode is never pushed to any git remote (user policy).
    if mode == 'internal':
        logger.info('Git push disabled for internal mode; skipping.')
        print(f"  {C_DIM}Internal mode: git push disabled by policy.{C_END}")
        return

    repo_cfg = _GIT_REPOS.get(mode)
    if not repo_cfg:
        logger.info('No git repo configured for mode=%s, skipping push.', mode)
        return

    remotes = repo_cfg.get('remotes', [])
    # Backward compat: single 'remote' key → wrap in list
    if not remotes and 'remote' in repo_cfg:
        remotes = [{'name': 'origin', 'url': repo_cfg['remote']}]
    if not remotes:
        logger.info('No remotes configured for mode=%s, skipping push.', mode)
        return

    branch = branch_override or repo_cfg['branch']

    # Display summary
    display_names = ', '.join(r['name'] for r in remotes)
    print(f"\n  {C_BOLD}\U0001f680 Git push \u2014 {len(remotes)} remote(s): "
          f"{display_names} ({branch}){C_END}")

    def _run(cmd: list[str], **kw):
        """Run a git command in the dest directory, raise on failure."""
        result = subprocess.run(
            cmd, cwd=str(dest), capture_output=True, text=True, **kw
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or '').strip()
            raise RuntimeError(f"git command failed: {' '.join(cmd)}\n{err}")
        return result.stdout.strip()

    try:
        # Init repo if .git doesn't exist
        git_dir = dest / '.git'
        if not git_dir.is_dir():
            _run(['git', 'init'])
            _run(['git', 'checkout', '-b', branch])
            logger.info('Initialized new git repo in %s (branch %s)', dest, branch)
        else:
            # Ensure correct branch
            current = _run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'])
            if current != branch:
                _run(['git', 'checkout', '-B', branch])

        # Configure git identity (needed for fresh repos)
        _run(['git', 'config', 'user.email', 'rangehow@users.noreply.github.com'])
        _run(['git', 'config', 'user.name', 'rangehow'])

        # Configure all remotes
        for remote_cfg in remotes:
            rname = remote_cfg['name']
            rurl = remote_cfg['url']
            try:
                _run(['git', 'remote', 'remove', rname])
            except RuntimeError:
                pass  # no remote yet — fine
            _run(['git', 'remote', 'add', rname, rurl])

        # Stage & commit (single commit, pushed to all remotes)
        _run(['git', 'add', '-A'])
        if not commit_msg:
            ts = datetime.now().strftime('%Y-%m-%d %H:%M')
            commit_msg = f'Export ({mode}) — {ts}'
        try:
            _run(['git', 'commit', '-m', commit_msg])
        except RuntimeError:
            # Nothing to commit (identical content)
            print(f"  {C_DIM}Nothing changed since last push.{C_END}")
            return

        # Tag with version — only when this is an explicit release (--bump)
        tag_name = None
        if is_release:
            version = _read_version()
            tag_name = f'v{version}'
            try:
                # Delete existing local tag if present (allows re-tagging same version)
                try:
                    _run(['git', 'tag', '-d', tag_name])
                except RuntimeError:
                    pass
                _run(['git', 'tag', '-a', tag_name, '-m', f'Release {tag_name}'])
                print(f"  {C_GREEN}\U0001f3f7\ufe0f  Tagged: {tag_name}{C_END}")
            except RuntimeError as e:
                logger.warning('Git tag failed: %s', e)
                tag_name = None  # don't try to push a tag that wasn't created

        # Push to each remote
        for remote_cfg in remotes:
            rname = remote_cfg['name']
            display_url = re.sub(r'https://[^@]+@', 'https://***@', remote_cfg['url'])
            _push_branch(_run, rname, branch, force=force)

            # Push tag to this remote (never silently moves a published tag)
            if tag_name:
                _push_tag(_run, rname, tag_name, force=force)

            print(f"  {C_GREEN}\u2705 Pushed to {display_url} ({branch}){C_END}")

        print()  # blank line after all remotes
    except RuntimeError as e:
        print(f"  {C_RED}\u26a0 Git push failed: {e}{C_END}\n")
        logger.error('Git push failed for mode=%s: %s', mode, e)
    except FileNotFoundError:
        print(f"  {C_RED}\u26a0 git not found \u2014 install git to enable auto-push.{C_END}\n")


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Export Tofu project with sanitization.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 export.py --mode personal --dest ~/tofu-backup
              python3 export.py --mode internal --dest ~/tofu-team
              python3 export.py --mode opensource --dest ~/tofu-public
              python3 export.py --mode opensource --dry-run
              python3 export.py --mode opensource --push --bump patch -m 'bug fixes'
              python3 export.py --mode opensource --push --bump minor -m 'new feature'
              python3 export.py --mode internal --push
        """),
    )
    parser.add_argument(
        '--mode', required=True, choices=['personal', 'internal', 'opensource'],
        help='Sanitization level: personal (self-use), internal (company), or opensource (public)',
    )
    parser.add_argument(
        '--dest',
        help='Destination directory (default: ../tofu-<mode>)',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Preview what would be copied/excluded without writing files',
    )
    parser.add_argument(
        '--push', action='store_true',
        help='Auto-push to upstream git repo after export (opensource only; internal never pushes)',
    )
    parser.add_argument(
        '--msg', '-m',
        help='Git commit message (used with --push)',
    )
    parser.add_argument(
        '--bump', choices=['major', 'minor', 'patch'],
        help='Bump version before export (major/minor/patch)',
    )
    parser.add_argument(
        '--branch',
        help='Override git branch name (default: from repo config)',
    )
    parser.add_argument(
        '--progress', action='store_true',
        help='Stream a live per-file view during the copy (shows each '
             'top-level dir entered + a running transferred-files counter) '
             'so you can watch the transfer and catch files that should be excluded',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='With --push: on a non-fast-forward rejection, OVERWRITE the remote '
             '(force-push branch + move existing release tags). Default preserves '
             'remote history via an ours-merge fast-forward instead',
    )
    parser.add_argument(
        '--worktree', action='store_true',
        help='Export the WORKTREE including uncommitted changes (legacy behavior). '
             'Default internal/opensource exports copy committed HEAD via git '
             'archive, so sibling in-flight edits never tear the published tree',
    )

    args = parser.parse_args()

    # ── Internal mode NEVER pushes to GitHub (user policy) ──
    # Internal is for local/self-use sharing only; silently disable --push.
    if args.mode == 'internal' and args.push:
        print(f"  {C_YELLOW}\u26a0 --push is disabled for internal mode; skipping git push.{C_END}")
        logger.info('--push ignored for internal mode (disabled by policy)')
        args.push = False

    # ── Handle version bumping ──
    version = _read_version()
    if args.bump:
        if args.dry_run:
            # Preview only — compute the next version WITHOUT writing/committing.
            version = _next_version(version, args.bump)
            print(f"  {C_CYAN}\U0001f4cc Version: v{version} "
                  f"{C_DIM}(dry run — VERSION not written){C_END}")
        else:
            version = _bump_version(args.bump)
            print(f"  {C_CYAN}\U0001f4cc Version: v{version}{C_END}")
            # Commit the bump into THIS origin tree so origin's VERSION and the
            # published release tag never drift.
            _commit_version_to_origin(version)
    else:
        print(f"  {C_DIM}Version: v{version} (use --bump patch/minor/major to increment){C_END}")

    # Only prefix commit message with version tag on explicit releases (--bump)
    commit_msg = args.msg
    is_release = bool(args.bump)
    if args.push and not args.dry_run:
        if is_release:
            # Release commit: prefix with version tag
            if commit_msg:
                commit_msg = f'v{version}: {commit_msg}'
            else:
                commit_msg = f'v{version}'
        else:
            # Regular push: use message as-is, or a timestamped default
            if not commit_msg:
                from datetime import datetime as _dt
                commit_msg = f'update {_dt.now().strftime("%Y-%m-%d %H:%M")}'

    if args.dest:
        dest = Path(args.dest).resolve()
    else:
        _default_dirs = {
            'personal': 'tofu-personal',
            'internal': 'tofu-meituan',
            'opensource': 'tofu-open',
        }
        base_dir = _default_dirs[args.mode]
        # When using a non-default branch, isolate to a branch-specific
        # export directory.  This prevents .git history contamination when
        # the same repo is pushed to different branches from different
        # source projects (e.g. main from tofu/, cli_switch from tofu_branch/).
        default_branch = _GIT_REPOS.get(args.mode, {}).get('branch')
        if args.branch and args.branch != default_branch:
            base_dir = f'{base_dir}--{args.branch}'
        dest = ROOT.parent / base_dir

    export_project(args.mode, dest, dry_run=args.dry_run,
                   push=args.push, commit_msg=commit_msg,
                   is_release=is_release, branch_override=args.branch,
                   progress=args.progress, force=args.force,
                   worktree=args.worktree)


if __name__ == '__main__':
    main()
