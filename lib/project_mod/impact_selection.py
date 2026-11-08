"""Impact selection: run only the test files a target tree can affect.

Responsibility
    Turn the recorder's observations (``impact_recorder.py``) into
    content-addressed records and decide, for a target tree, which test files
    must run. Each record maps a test file to the Git blob of every repository
    file whose code it executed, imported or read, the code units that matter
    in each Python file, the contents of the directories it listed and the
    outcome of its run. A recorded test file is skipped when none of its
    dependencies changed and it passed, or when it failed both at its recorded
    tree and at the current branch tip. A test file with no record is selected
    statically by the repository's ``scripts/test_select.py``; a change to a
    blast-radius file keeps the selector's selection verbatim.

Units
    ``<module>`` is everything outside function bodies: imports, globals,
    class bodies, decorators, signatures, defaults and docstrings (a decorator
    may read them at import time). Every function or method body is its own
    unit, named by its definition path (``f``, ``C.m``); closures and lambdas
    belong to the outermost definition. ASTs are compared without positions,
    so moving code or editing comments changes nothing. For a file whose code
    a test executed or which it imports, the module unit and the executed units
    count; for a file whose functions only ran while a module the test depends
    on was imported, those functions count. Data files, the test file, its
    conftest chain and anything an opaque test references count byte for byte.

Records are reconstructible cache, not authority: they live in one SQLite file
    beside the warm merge checkout and are discarded when the project
    interpreter's environment changes. A process that dies leaves no
    observations, and a test file whose collected node ids did not all execute
    is never recorded, so a partial run cannot install a partial dependency set.
    Known blind spot: a test observing a module-level value of a module it
    neither imports nor executes code in (for example a registry entry another
    module adds at import) is rerun only when its own dependencies change; full
    recording runs (``landing_verification record``) re-check everything.

Entry points
    ``Tree``, ``ImpactStore``, ``records_from_observations``, ``affected``,
    ``plan``, ``plugin_source``, ``unit_fingerprints``, ``unit_of``.

Dependencies: standard library, the git executable and the private
``lib.storage_authority_sql.impact_cache`` persistence owner.
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import os
import subprocess
from pathlib import Path

from lib.storage_authority_sql.impact_cache import (
    ImpactCache,
    ImpactCacheError as ImpactCacheError,
    Record as Record,
)

MODULE_UNIT = '<module>'
WHOLE_FILE = None  # a dependency whose bytes always count


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]


def unit_fingerprints(source) -> dict[str, str] | None:
    """Fingerprint every unit of a Python source; None when it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    bodies: dict[str, list[str]] = {}

    def docstring(body):
        return ast.get_docstring(ast.Module(body=body, type_ignores=[]))

    def visit(statements, prefix):
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bodies.setdefault(prefix + node.name, []).append(
                    ast.dump(ast.Module(body=node.body, type_ignores=[])))
                # The docstring stays in the module unit: decorators may read it at import time.
                node.body = node.body[:1] if docstring(node.body) is not None else [ast.Pass()]
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f'{prefix}{node.name}.')
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                                   ast.AsyncWith, ast.Try, getattr(ast, 'TryStar', ast.Try))):
                for field in ('body', 'orelse', 'finalbody'):
                    visit(getattr(node, field, None) or [], prefix)
                for handler in getattr(node, 'handlers', None) or []:
                    visit(handler.body, prefix)

    visit(tree.body, '')
    units = {name: _digest('\0'.join(parts)) for name, parts in bodies.items()}
    units[MODULE_UNIT] = _digest(ast.dump(tree))
    return units


def unit_of(qualname: str) -> str:
    """The unit a code object's qualified name belongs to."""
    base = qualname.split('.<locals>', 1)[0]
    if not base or any(part.startswith('<') for part in base.split('.')):
        return MODULE_UNIT
    return base


def _git(root, *args: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(LC_ALL='C', GIT_OPTIONAL_LOCKS='0')
    return subprocess.run(['git', *args], cwd=str(root), env=env, capture_output=True, text=True)


class Tree:
    """The paths and blob ids of one tree; directory listings are derived once, on demand."""

    def __init__(self, blobs: dict[str, str]):
        self.blobs = blobs
        self._children: dict[str, set[str]] | None = None
        self._listings: dict[str, str] = {}

    @classmethod
    def of(cls, root, revision: str) -> 'Tree':
        listing = _git(root, 'ls-tree', '-r', '-z', '--full-tree', revision)
        if listing.returncode:
            raise OSError(f'git ls-tree {revision} failed: {listing.stderr.strip()[:300]}')
        blobs = {}
        for entry in listing.stdout.split('\0'):
            if entry:
                meta, path = entry.split('\t', 1)
                _mode, kind, oid = meta.split()
                if kind == 'blob':
                    blobs[path] = oid
        return cls(blobs)

    def listing(self, directory: str) -> str:
        """Digest of the names directly inside DIRECTORY ('' is the repository root)."""
        digest = self._listings.get(directory)
        if digest is None:
            if self._children is None:
                children: dict[str, set[str]] = {'': set()}
                for path in self.blobs:
                    parts = path.split('/')
                    for depth in range(len(parts)):
                        children.setdefault('/'.join(parts[:depth]), set()).add(parts[depth])
                self._children = children
            digest = _digest('\0'.join(sorted(self._children.get(directory, ()))))
            self._listings[directory] = digest
        return digest


class ImpactStore:
    """Compute Git-blob fingerprints and delegate persistence to the cache owner."""

    def __init__(self, path, environment_key: str):
        self._cache = ImpactCache(path, environment_key)
        self.path = self._cache.path
        self._fingerprints: dict[str, dict[str, str] | None] = {}

    def close(self):
        self._cache.close()

    def records(self) -> dict[str, Record]:
        return self._cache.records()

    def save(self, records: list[Record]) -> None:
        self._cache.save(records)

    def fingerprints(self, root, blob: str) -> dict[str, str] | None:
        if blob in self._fingerprints:
            return self._fingerprints[blob]
        found, units = self._cache.fingerprint(blob)
        if not found:
            shown = _git(root, 'cat-file', 'blob', blob)
            units = unit_fingerprints(shown.stdout) if shown.returncode == 0 else None
            if shown.returncode == 0:
                self._cache.save_fingerprint(blob, units)
        self._fingerprints[blob] = units
        return units


def conftest_chain(test: str, paths) -> list[str]:
    """The conftest.py files pytest loads for TEST, from the repository root down."""
    chain, directory = [], os.path.dirname(test)
    while True:
        candidate = f'{directory}/conftest.py' if directory else 'conftest.py'
        if candidate in paths:
            chain.append(candidate)
        if not directory:
            return chain[::-1]
        directory = os.path.dirname(directory)


def _empty(**extra) -> dict:
    return {'units': {}, 'data': set(), 'dirs': set(), 'opaque': False, **extra}


def _merge(target: dict, line: dict) -> None:
    for path, names in (line.get('units') or {}).items():
        target['units'].setdefault(path, set()).update(unit_of(name) for name in names)
    for field in ('data', 'dirs'):
        target[field].update(line.get(field) or ())
    target['opaque'] = target['opaque'] or bool(line.get('opaque'))


def records_from_observations(observations: list[dict], tests: list[str], tree: Tree, tree_id: str,
                              refs: dict[str, set[str]]) -> list[Record]:
    """Records for whole-file runs; incomplete observations keep a file unrecorded.

    REFS are each test file's direct static references (imports and literal
    paths). A referenced Python file counts through its module body even when
    another test file triggered its import; a test that starts a program whose
    reads are unobserved, or forks, is opaque, so every referenced file counts
    byte for byte.
    """
    fixtures: dict[str, dict] = {}
    files: dict[str, dict] = {}
    import_time: dict[str, dict[str, set[str]]] = {}
    for line in observations:
        kind = line.get('kind')
        if kind == 'import-time':
            executed = import_time.setdefault(line.get('module') or '', {})
            for path, names in (line.get('units') or {}).items():
                executed.setdefault(path, set()).update(unit_of(name) for name in names)
        elif kind == 'fixture':
            _merge(fixtures.setdefault(line['key'], _empty()), line)
        elif kind == 'file':
            merged = files.setdefault(line['test'], _empty(
                failed=set(), fixtures=set(), collected=set(), executed=set()))
            for field in ('failed', 'fixtures', 'collected', 'executed'):
                merged[field].update(line.get(field) or ())
            _merge(merged, line)
    records = []
    for test in tests:
        observed = files.get(test)
        if observed is None or not observed['collected'] <= observed['executed']:
            continue
        merged = _empty()
        _merge(merged, {'units': observed['units'], 'data': observed['data'],
                        'dirs': observed['dirs'], 'opaque': observed['opaque']})
        for key in observed['fixtures']:
            if key in fixtures:
                _merge(merged, {'units': fixtures[key]['units'], 'data': fixtures[key]['data'],
                                'dirs': fixtures[key]['dirs'], 'opaque': fixtures[key]['opaque']})
        deps = _dependencies(test, merged, tree, refs.get(test, ()), import_time)
        failed = sorted(observed['failed'])
        records.append(Record(test, tree_id, 'failed' if failed else 'passed', failed,
                              merged['opaque'], deps,
                              {directory: tree.listing(directory) for directory in merged['dirs']}))
    return records


def _dependencies(test: str, observed: dict, tree: Tree, refs, import_time) -> dict:
    deps: dict[str, tuple[str, list[str] | None]] = {}

    def need(path, names):
        if path not in tree.blobs:
            return
        blob, known = deps.get(path, (tree.blobs[path], []))
        if known is WHOLE_FILE:
            return
        deps[path] = (blob, WHOLE_FILE if names is WHOLE_FILE
                      else sorted(set(known) | set(names)))

    for path, names in observed['units'].items():
        need(path, {MODULE_UNIT, *names})  # executed code reads its module's globals
    for path in refs:
        need(path, WHOLE_FILE if observed['opaque'] or not path.endswith('.py')
             else {MODULE_UNIT})
    for path in [*observed['data'], test, *conftest_chain(test, tree.blobs)]:
        need(path, WHOLE_FILE)
    # Functions that ran while a module this test depends on was imported built
    # state the test observes, whichever test file triggered the import.
    for module in [path for path in deps if path.endswith('.py')]:
        for path, names in import_time.get(module, {}).items():
            need(path, names)
    return deps


def affected(record: Record, tree: Tree, store: ImpactStore, root) -> str:
    """The first dependency of RECORD that differs in TREE, or '' when none differs."""
    for path, (recorded_blob, names) in record.deps.items():
        current = tree.blobs.get(path)
        if current == recorded_blob:
            continue
        if current is None or names is WHOLE_FILE or not path.endswith('.py'):
            return path
        before = store.fingerprints(root, recorded_blob)
        after = store.fingerprints(root, current)
        if before is None or after is None or any(
                before.get(name) != after.get(name) for name in names):
            return path
    for directory, recorded in record.dirs.items():
        if tree.listing(directory) != recorded:
            return directory or 'the repository root'
    return ''


@dataclasses.dataclass
class Plan:
    run: dict[str, str]
    skipped: int
    recorded: int


def plan(records: dict[str, Record], tests: list[str], static: dict[str, str],
         blast: bool, tip: Tree, merged: Tree, store: ImpactStore, root) -> Plan:
    """Which of TESTS must run on the merged tree, and why.

    A recorded file runs when a dependency differs in the merged tree, or when
    it failed and the current branch differs from its recorded tree (so it may
    pass there). An unrecorded file runs when the static selector chose it; a
    blast-radius change keeps the static selection verbatim.
    """
    run, skipped = {}, 0
    for test in tests:
        reason = static.get(test)
        record = records.get(test)
        if record is None:
            if reason is not None:
                run[test] = f'not yet recorded; {reason}'
            continue
        changed = affected(record, merged, store, root)
        if changed:
            run[test] = f'executed code changed in {changed}'
        elif record.outcome == 'failed' and affected(record, tip, store, root):
            run[test] = 'previously failing; the current branch differs from its recorded run'
        elif blast and reason is not None:
            run[test] = f'static blast radius: {reason}'
        elif record.opaque and reason == 'guard-core':
            run[test] = 'guard for edges a run cannot record'
        else:
            skipped += 1
    return Plan(run, skipped, sum(1 for test in tests if test in records))


def plugin_source() -> str:
    """The recorder, copied next to each run so the repository under test need not ship it."""
    return (Path(__file__).with_name('impact_recorder.py')).read_text(encoding='utf-8')
