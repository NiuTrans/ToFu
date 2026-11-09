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
import importlib.util
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
UNKNOWN_PLUGIN_INPUT = '::pytest_plugins:unknown'

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
    'scripts/test_execution.py': 'ALL',
    'frontend/src/api/transport.ts': 'test_frontend_',
    'lib/vite_assets.py': 'test_frontend_',
}

# Cross-cutting guards the inner loop always runs. Landing verification skips a
# recorded guard only when its whole run was observed and none of its
# dependencies changed; a guard that starts other programs always runs.
GUARD_CORE_BASENAMES = (
    'test_frontend_backend_contract.py',
    'test_frontend_api_contract.py',
    'test_api_contract_drift.py',
    'test_api_field_contract.py',
    'test_frontend_harness_expect_ratchet.py',
    'test_bundle_manifest_parity.py',
    'test_jsdom_runner_structured.py',
)

# Execution inputs are separate from selection implementation. Legacy selectors
# without this declaration retain their whole-file execution dependency.
EXECUTION_POLICY = 'scripts/test_execution.py'
_execution_spec = importlib.util.spec_from_file_location(
    '_tofu_test_execution', os.path.join(os.path.dirname(__file__), 'test_execution.py'))
_execution = importlib.util.module_from_spec(_execution_spec)
_execution_spec.loader.exec_module(_execution)
build_pytest_command = _execution.build_pytest_command
_parallel_workers = _execution._parallel_workers


# ─── Index (pure) ──────────────────────────────────────────────────────

def _module_to_path_candidates(module: str) -> list[str]:
    """'lib.foo.bar' → ['lib/foo/bar.py', 'lib/foo/bar/__init__.py']."""
    parts = module.split('.')
    if not parts or parts[0] not in _REPO_MODULE_ROOTS:
        return []
    base = '/'.join(parts)
    return [f'{base}.py', f'{base}/__init__.py']


def _import_statements(tree):
    """Visit statement containers; expression payloads cannot contain imports."""
    pending = [tree]
    containers = (ast.stmt, ast.ExceptHandler, ast.match_case)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        else:
            pending.extend(child for child in ast.iter_child_nodes(node)
                           if isinstance(child, containers))


def _plugin_modules(tree):
    """Literal pytest plugin imports; None means unknown Python dependencies."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == 'pytest_plugins'
                   for target in targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError):
            yield None
            continue
        if isinstance(value, str):
            value = value.split(',')
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
            yield None
        else:
            yield from value


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
        for plugin in _plugin_modules(tree):
            refs.update(_module_to_path_candidates(plugin) if plugin is not None
                        else (UNKNOWN_PLUGIN_INPUT,))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                command = node.value.split()
                if node.value == 'Makefile' or (command and command[0] in ('make', 'gmake')):
                    refs.add('Makefile')
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.update(_module_to_path_candidates(alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                refs.update(_module_to_path_candidates(node.module))
                for alias in node.names:
                    refs.update(_module_to_path_candidates(f'{node.module}.{alias.name}'))
    refs.update(_LITERAL_REF_RE.findall(source))
    return refs


def source_references(source: str, path: str, *, tree=None) -> set[str]:
    """Static inputs of any Python owner, including relative imports and parents.

    Keep absent candidates: deleted modules still affect their importers. No
    imports execute, and external modules simply have no in-repository edges.
    """
    refs = set()
    if tree is None:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return refs

    def module(parts):
        if not parts:
            return
        name = '/'.join(parts)
        refs.update((name + '.py', name + '/__init__.py'))
        refs.update('/'.join(parts[:end]) + '/__init__.py' for end in range(1, len(parts)))

    package = path.split('/')[:-1]
    for plugin in _plugin_modules(tree):
        if plugin is None:
            refs.add(UNKNOWN_PLUGIN_INPUT)
        else:
            module(plugin.split('.'))
    for node in _import_statements(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module(alias.name.split('.'))
        elif isinstance(node, ast.ImportFrom):
            prefix = package[:len(package) - node.level + 1] if node.level else []
            parts = prefix + (node.module.split('.') if node.module else [])
            module(parts)
            for alias in node.names:
                if alias.name != '*':
                    module(parts + alias.name.split('.'))
    return refs


def expand_dependencies(root, tree, changed, cache_path):
    """Opt this repository into cached import and declared-fixture selection."""
    from types import SimpleNamespace
    from lib.project_mod.verification_dependencies import expand_changes
    policy = SimpleNamespace(__file__=__file__, source_references=source_references,
                             UNKNOWN_PLUGIN_INPUT=UNKNOWN_PLUGIN_INPUT)
    return expand_changes(root, tree, changed, policy, cache_path)


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
            if isinstance(saved, dict) and saved.get('version') == 3:
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
                json.dump({'version': 3, 'index': index}, fh)
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
    if any(path.endswith('.py') for path in changed):
        changed_set.add(UNKNOWN_PLUGIN_INPUT)
    all_tests = set(index)

    for f in all_tests & changed_set:
        selected[f] = 'self'
    for f in sorted(changed_set):
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
    if changed_set:
        for test, entry in index.items():
            if changed_set & refs_of(entry):
                selected.setdefault(test, 'direct')

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
