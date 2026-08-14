#!/usr/bin/env python3
"""Affected-test selection (P2-3 — docs/TESTING_STRATEGY.md §4).

WHY
---
The unit tier measured 19m58s at 16 workers (2026-08-04) — past the ~15min
industry threshold for an inner iteration loop. The full suite stays the GATE
(CI / pre-push); this script is the LOOP: given the files you changed, run the
tests that can actually see them.

HOW (no ML — a transparent, auditable mapping beats a model at 15k tests)
---
  index : test file → source files it references, extracted statically:
          • AST imports    (``import lib.foo.bar`` → lib/foo/bar.py, plus the
                            package/__init__.py candidate)
          • literal paths  (``'static/js/core/api.js'``, ``'docs/X.md'`` …)
  select: changed ∩ referenced, PLUS
          • every changed test file itself,
          • a BLAST-RADIUS table (conftest → whole suite; the jsdom helpers and
            api.js → the frontend family — their dependents are structural,
            not greppable),
          • the GUARD CORE (contract / ratchet guards) — a handful of seconds,
            always worth it: a cross-cutting regression must never slip
            through because the mapping missed an indirect edge.
  If the selection exceeds ~40% of the suite, just run everything — the
  bookkeeping stops paying for itself.

CLI
---
  python scripts/test_select.py [--base REF] [--jobs N] [--print] [--run]
  default change set: working tree vs HEAD (staged + unstaged + untracked);
  --base REF diffs REF...HEAD instead (branch mode).
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
TESTS_DIR = os.path.join(ROOT, 'tests')
CACHE_PATH = os.path.join(ROOT, '.tofu', 'test_select_index.json')

_REPO_MODULE_ROOTS = ('lib', 'routes', 'tests')

_LITERAL_REF_RE = re.compile(
    r"""(?x)['"`]((?:frontend/src|static/vite|lib|routes|docs|scripts|tests)/[\w.\-]+(?:/[\w.\-]+)*\.(?:js|mjs|ts|py|md))['"`]""")

# Files whose change invalidates whole FAMILIES of tests — their dependents
# are structural (eval'd, session-wide, or manifest-driven), so no per-file
# reference edge exists to select on.
#   value: 'ALL' or a filename prefix family under tests/
BLAST_RADIUS = {
    'tests/conftest.py': 'ALL',
    'tests/_jsdom.py': 'test_frontend_',
    'tests/_jsdom_harness.js': 'test_frontend_',
    'tests/_conv_bundle_sources.py': 'test_frontend_',
    'frontend/src/api/transport.ts': 'test_frontend_',
    'lib/vite_assets.py': 'test_frontend_',
}

# Cross-cutting guards that always run — cheap (seconds), and they are the
# net for indirect edges the static index cannot see.
GUARD_CORE_BASENAMES = (
    'test_frontend_backend_contract.py',
    'test_frontend_api_contract.py',
    'test_api_contract_drift.py',
    'test_api_field_contract.py',
    'test_frontend_harness_expect_ratchet.py',
    'test_bundle_manifest_parity.py',
    'test_jsdom_runner_structured.py',
)

_RUN_ALL_FRACTION = 0.40
_DEFAULT_MAX_WORKERS = 4


# ─── Index (pure) ──────────────────────────────────────────────────────

def _module_to_path_candidates(module: str) -> list[str]:
    """'lib.foo.bar' → ['lib/foo/bar.py', 'lib/foo/bar/__init__.py']."""
    parts = module.split('.')
    if not parts or parts[0] not in _REPO_MODULE_ROOTS:
        return []
    base = '/'.join(parts)
    return [f'{base}.py', f'{base}/__init__.py']


def refs_of_test_file(source: str) -> set[str]:
    """Extract the repo files a test file can SEE: AST imports of in-repo
    modules + literal repo-path references. Pure — no resolution, so a
    renamed-away import target stays a (harmless) stale edge: selection errs
    toward running MORE tests, never fewer."""
    refs: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.update(_module_to_path_candidates(alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                refs.update(_module_to_path_candidates(node.module))
    refs.update(_LITERAL_REF_RE.findall(source))
    return refs


def build_index(tests_dir: str = TESTS_DIR, cache_path: str | None = CACHE_PATH) -> dict:
    """test_file (repo-relative) → sorted refs, with an mtime-keyed JSON cache
    so repeat selection costs ~0. Cache is advisory only — a stale entry just
    re-parses that file."""
    cache = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as fh:
                cache = json.load(fh)
        except (json.JSONDecodeError, OSError):
            cache = {}
    index: dict[str, dict] = {}
    dirty = False
    for name in sorted(os.listdir(tests_dir)):
        if not (name.startswith('test_') and name.endswith('.py')):
            continue
        path = os.path.join(tests_dir, name)
        rel = f'tests/{name}'
        mtime = os.path.getmtime(path)
        cached = cache.get(rel)
        if cached and cached.get('mtime') == mtime:
            index[rel] = cached
            continue
        with open(path, encoding='utf-8') as fh:
            refs = sorted(refs_of_test_file(fh.read()))
        index[rel] = {'mtime': mtime, 'refs': refs}
        dirty = True
    if dirty and cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(index, fh)
        os.replace(tmp, cache_path)
    return index


# ─── Selection (pure) ──────────────────────────────────────────────────

def select_tests(index: dict, changed: list[str]):
    """(selected: set[str], reasons: dict[str, str]) for a change set.

    ``index`` maps test file → refs (either the build_index record form
    {'mtime', 'refs'} or a bare set/list of refs). ``changed`` is
    repo-relative paths."""
    def refs_of(v):
        if isinstance(v, dict):
            return set(v.get('refs', ()))
        return set(v)

    selected: dict[str, str] = {}
    changed_set = set(changed)
    all_tests = set(index)

    for f in changed_set:
        if f.startswith('tests/test_'):
            selected.setdefault(f, 'self')
        blast = BLAST_RADIUS.get(f)
        if blast == 'ALL':
            for t in all_tests:
                selected.setdefault(t, f'blast:{f}')
        elif blast:
            for t in all_tests:
                if os.path.basename(t).startswith(blast):
                    selected.setdefault(t, f'blast:{f}')
        for t, v in index.items():
            if f in refs_of(v):
                selected.setdefault(t, 'direct')

    for name in GUARD_CORE_BASENAMES:
        selected.setdefault(f'tests/{name}', 'guard-core')

    return set(selected), selected


# ─── CLI ───────────────────────────────────────────────────────────────

def changed_files(base: str | None) -> list[str]:
    if base:
        out = subprocess.run(
            ['git', 'diff', '--name-only', f'{base}...HEAD'],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    out = subprocess.run(['git', 'status', '--porcelain'],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    files = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        path = ln[3:]
        if ' -> ' in path:  # rename: take the new name
            path = path.split(' -> ', 1)[1]
        files.append(path.strip())
    return files


def _parallel_workers(existing: list[str] | None,
                      jobs: str | None) -> str | None:
    """Return the requested/adaptive xdist worker count, or ``None``.

    A pytest controller and every worker import the full test bootstrap.  For
    a one-file inner loop that startup costs more than the tests, so the
    implicit default stays serial.  Larger selections are capped at the same
    conservative four workers used by the Makefile; an explicit ``--jobs``
    remains authoritative.
    """
    if jobs is not None:
        return None if jobs == '0' else jobs
    if existing is not None and len(existing) <= 1:
        return None
    if existing is None:
        return str(_DEFAULT_MAX_WORKERS)
    return str(min(_DEFAULT_MAX_WORKERS, len(existing)))


def build_pytest_command(existing: list[str] | None,
                         jobs: str | None) -> list[str]:
    """Build the hermetic selected-test command.

    ``main`` disables pytest entry-point autoload to keep host-only plugins
    out of the inner loop.  Parallel mode must therefore load xdist
    explicitly *before* using its ``-n`` / ``--dist`` options.
    """
    workers = _parallel_workers(existing, jobs)
    cmd = [sys.executable, '-m', 'pytest', '-p', 'no:napari']
    if workers is not None:
        cmd += ['-p', 'xdist.plugin']
    if existing is not None:
        cmd += existing
    else:
        cmd += ['-m', 'unit']
    if workers is not None:
        cmd += ['-n', workers, '--dist', 'worksteal']
    cmd += ['--timeout=300', '--tb=short', '-q']
    return cmd


def main(argv: list[str]) -> int:
    base = jobs = None
    do_run = '--run' in argv
    for i, a in enumerate(argv):
        if a == '--base' and i + 1 < len(argv):
            base = argv[i + 1]
        elif a == '--jobs' and i + 1 < len(argv):
            jobs = argv[i + 1]
    changed = [f for f in changed_files(base) if not f.startswith('.github/')]
    if not changed:
        print('[test-select] no changes vs', base or 'working tree',
              '— nothing to select. Run the guard core manually if in doubt.')
        return 0
    index = build_index()
    selected, reasons = select_tests(index, changed)
    existing = sorted(f for f in selected
                      if os.path.isfile(os.path.join(ROOT, f)))
    n_all = len(index)
    if len(existing) > _RUN_ALL_FRACTION * n_all:
        print(f'[test-select] selection is {len(existing)}/{n_all} suites '
              f'(>{_RUN_ALL_FRACTION:.0%}) — running the WHOLE unit tier instead')
        existing = None  # None → whole tier
    print(f'[test-select] changed: {len(changed)} file(s)')
    for f in changed[:12]:
        print(f'  ~ {f}')
    if existing is not None:
        by_reason: dict[str, int] = {}
        for f in existing:
            by_reason[reasons.get(f, '?')] = by_reason.get(reasons.get(f, '?'), 0) + 1
        print(f'[test-select] selected {len(existing)}/{n_all} test files '
              f'({", ".join(f"{k}×{v}" for k, v in sorted(by_reason.items()))})')
        if '--print' in argv:
            for f in existing:
                print(f'  {f}  [{reasons.get(f)}]')
    if not do_run:
        print('[test-select] pass --run to execute, --print to list')
        return 0
    cmd = build_pytest_command(existing, jobs)
    print('[test-select] $', ' '.join(cmd))
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
