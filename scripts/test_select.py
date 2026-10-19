#!/usr/bin/env python3
"""Select affected Python tests for the inner loop; not a release certificate.

Entry point: main (--base REF, --print, --run, --jobs N). Git enumerates current
tracked/untracked tests recursively; AST imports and literal paths give an
advisory reverse index, supplemented by blast-radius rules and live guards.
The pytest configuration owns execution plugins. docs/TESTING_STRATEGY.md owns
the verification ladder; domain, browser and release gates remain mandatory.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
TESTS_DIR = os.path.join(ROOT, 'tests')
CACHE_PATH = os.path.join(ROOT, '.tofu', 'test_select_index.json')

_REPO_MODULE_ROOTS = ('lib', 'routes', 'tests')

_LITERAL_REF_RE = re.compile(
    r"""(?x)['"`]((?:frontend/src|static/vite|lib|routes|docs|scripts|tests|contracts|\.github)/[\w.\-]+(?:/[\w.\-]+)*\.(?:js|mjs|ts|py|md|json|yaml|yml|toml))['"`]""")

# Files whose change invalidates whole FAMILIES of tests — their dependents
# are structural (eval'd, session-wide, or manifest-driven), so no per-file
# reference edge exists to select on.
#   value: 'ALL' or a filename prefix family under tests/
BLAST_RADIUS = {
    'tests/conftest.py': 'ALL',
    'tests/_jsdom.py': 'test_frontend_',
    'tests/_jsdom_harness.js': 'test_frontend_',
    'tests/_runtime_sections.py': 'test_frontend_',
    'pyproject.toml': 'ALL',
    'Makefile': 'ALL',
    'scripts/test_select.py': 'ALL',
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
                for alias in node.names:
                    refs.update(_module_to_path_candidates(f'{node.module}.{alias.name}'))
    refs.update(_LITERAL_REF_RE.findall(source))
    return refs


def build_index(tests_dir: str = TESTS_DIR, cache_path: str | None = CACHE_PATH) -> dict:
    """Enumerate via Git (also on FUSE); a failed/empty census cannot pass.

    The advisory cache has a versioned stat key and atomic, unique replacement.
    Removed/ignored tests never survive merely because they were cached.
    """
    root = os.path.dirname(os.path.abspath(tests_dir))
    out = subprocess.run(
        ['git', 'ls-files', '--cached', '--others', '--exclude-standard',
         '-z', '--', os.path.basename(tests_dir)],
        cwd=root, capture_output=True, text=True, check=True, timeout=30).stdout
    paths = sorted({p for p in out.split('\0') if p
                    and os.path.basename(p).startswith('test_') and p.endswith('.py')
                    and os.path.isfile(os.path.join(root, p))})
    if not paths:
        raise ValueError('test census is empty; check the repository and tests directory')
    cache = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as fh:
                saved = json.load(fh)
            if isinstance(saved, dict) and saved.get('version') == 2:
                cache = saved.get('index', {})
                if not isinstance(cache, dict):
                    cache = {}
        except (json.JSONDecodeError, OSError):
            pass  # Cache is reconstructible; discovery/source failures are not.
    index = {}
    for rel in paths:
        path = os.path.join(root, rel)
        stat = os.stat(path)
        fingerprint = [stat.st_mtime_ns, stat.st_size]
        cached = cache.get(rel)
        if (isinstance(cached, dict) and cached.get('fingerprint') == fingerprint
                and isinstance(cached.get('refs'), list)
                and all(isinstance(ref, str) for ref in cached['refs'])):
            index[rel] = cached
        else:
            with open(path, encoding='utf-8') as fh:
                refs = sorted(refs_of_test_file(fh.read()))
            index[rel] = {'fingerprint': fingerprint, 'refs': refs}
    if cache_path and index != cache:
        parent = os.path.dirname(os.path.abspath(cache_path))
        os.makedirs(parent, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=parent, delete=False) as fh:
            tmp = fh.name
            try:
                json.dump({'version': 2, 'index': index}, fh)
                fh.close()
                os.replace(tmp, cache_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
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
        if f in all_tests:
            selected.setdefault(f, 'self')
        if f.endswith('/conftest.py'):
            for t in all_tests:
                if t.startswith(f.rsplit('/', 1)[0] + '/'):
                    selected.setdefault(t, f'fixture:{f}')
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
        matches = [path for path in all_tests if os.path.basename(path) == name]
        if not matches:
            raise ValueError(f'required guard is missing: {name}; update the selector')
        for path in matches:
            selected.setdefault(path, 'guard-core')

    return set(selected), selected


# ─── CLI ───────────────────────────────────────────────────────────────

def changed_files(base: str | None) -> list[str]:
    if base:
        revision = subprocess.run(
            ['git', 'rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}'],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        out = subprocess.run(
            ['git', 'diff', '--name-only', '--no-renames', '-z', f'{revision}...HEAD', '--'],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=30).stdout
        return sorted(set(filter(None, out.split('\0'))))
    # Include both rename endpoints; deleted sources still select their importers.
    paths = set()
    for args in (['diff', 'HEAD', '--name-only', '--no-renames', '-z', '--'],
                 ['ls-files', '--others', '--exclude-standard', '-z']):
        out = subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                             text=True, check=True, timeout=30).stdout
        paths.update(filter(None, out.split('\0')))
    return sorted(paths)


def _parallel_workers(existing: list[str] | None,
                      jobs: str | None) -> str | None:
    """Return the requested/adaptive xdist worker count, or ``None``.

    A pytest controller and every worker import the full test bootstrap. For a
    one-file inner loop that startup costs more than the tests, so the implicit
    default stays serial. Larger selections request ``auto``; the repository's
    xdist hook then applies the shared resource probe and selected-file cap. An
    explicit ``--jobs`` remains authoritative.
    """
    if jobs is not None:
        return None if jobs == '0' else jobs
    if existing is not None and len(existing) <= 1:
        return None
    return 'auto'


def build_pytest_command(existing: list[str] | None,
                         jobs: str | None) -> list[str]:
    """Build the hermetic selected-test command.

    ``main`` disables pytest entry-point autoload to keep host-only plugins out
    of the inner loop. pyproject.toml explicitly loads xdist and owns the
    ``worksteal`` scheduler, so this command only selects the worker count.
    """
    if existing == []:
        raise ValueError('empty selection must not launch unscoped pytest')
    workers = _parallel_workers(existing, jobs)
    cmd = [sys.executable, '-m', 'pytest', '-p', 'no:napari']
    if existing is not None:
        cmd += existing
    cmd += ['-m', '(unit or api) and not (visual or slow or live_llm)']
    if workers is not None:
        cmd += ['-n', workers]
    cmd += ['--timeout=300', '--tb=short', '-q']
    return cmd


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--jobs')
    parser.add_argument('--print', action='store_true', dest='print_selection')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    base, jobs, do_run = args.base, args.jobs, args.run
    if jobs is not None and jobs != 'auto' and not (jobs.isdigit() and int(jobs) >= 0):
        parser.error('--jobs must be auto or a nonnegative integer')
    changed = changed_files(base)
    if not changed:
        print('[test-select] no changes vs', base or 'working tree',
              '— nothing to select. Run the guard core manually if in doubt.')
        return 0
    print('[test-select] advisory inner-loop selection; domain/release gates still required')
    index = build_index()
    selected, reasons = select_tests(index, changed)
    existing = sorted(selected)
    n_all = len(index)
    if len(existing) > _RUN_ALL_FRACTION * n_all:
        print(f'[test-select] selection is {len(existing)}/{n_all} suites '
              f'(>{_RUN_ALL_FRACTION:.0%}) — broadening to all offline unit/API test files')
        existing = sorted(index)
        reasons.update({f: reasons.get(f, 'broad') for f in existing})
    print(f'[test-select] changed: {len(changed)} file(s)')
    for f in changed[:12]:
        print(f'  ~ {f}')
    if existing is not None:
        by_reason: dict[str, int] = {}
        for f in existing:
            by_reason[reasons.get(f, '?')] = by_reason.get(reasons.get(f, '?'), 0) + 1
        print(f'[test-select] selected {len(existing)}/{n_all} test files '
              f'({", ".join(f"{k}×{v}" for k, v in sorted(by_reason.items()))})')
        if args.print_selection:
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
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'[test-select] failed: {error}', file=sys.stderr)
        raise SystemExit(2)
