"""Impact selection: run only the test files a target tree can affect.

Responsibility
    Turn the recorder's observations (``impact_recorder.py``) into
    content-addressed records and decide, for a target tree, which test files
    must run. Each record maps a test file to the Git blob of every repository
    file whose code it executed, imported or read, the code units that matter
    in each Python file, the contents of the directories it listed and the
    outcome of its run. A recorded test file is skipped when none of its
    dependencies changed and it passed, or when it failed both at its recorded
    tree and at the current branch tip. A guard-core test file that starts a
    program the recorder cannot observe always runs: what that program read
    is unknown. A test file with no record is selected statically by the
    repository's ``scripts/test_select.py``.

Global inputs
    The selector's blast-radius files (``BLAST_RADIUS``: pytest configuration,
    shared harnesses, the selector itself) can change any test in their scope
    in ways a run cannot observe, so every record they scope depends on them:
    by code for Python (comments and layout do not count), byte for byte
    otherwise. A real change re-runs every scoped file once; the records of
    that run answer the landings after it, including the re-verification when
    the current branch moves while the run is in progress.

Units
    ``<module>`` is everything that runs at import time: imports, globals,
    class bodies, decorators and docstrings (a decorator may read them at
    import time). Every function or method body is its own unit, named by its
    definition path (``f``, ``C.m``); closures and lambdas belong to the
    outermost definition. A definition with no decorators, only constant
    defaults and no evaluated annotations binds nothing but its name, so its
    signature joins its own unit; the module unit keeps the name, and adding
    or removing a top-level name still invalidates importers that discover it
    dynamically. The same applies to a class whose body only binds method
    names and constants. ASTs are compared without positions,
    so moving code or editing comments changes nothing. For a file whose code
    a test executed or which it imports, the module unit and the executed units
    count; for a file whose functions only ran while a module the test depends
    on was imported, those functions count. Data files, the test file and
    anything an opaque test references count byte for byte. A conftest counts
    by unit too: ``conftest_units`` names the session/collection code every
    file in scope shares, and a helper only some runs execute counts only for
    those runs.

Records are reconstructible cache, not authority: they live in one SQLite file
    per schema version beside the warm merge checkout, so code versions running
    side by side never discard each other's records, and they are discarded
    when the project interpreter's environment changes. One file lock
    serializes access, so a landing waits for another's short write instead
    of losing its own. A process that dies leaves no
    observations, and a test file whose collected node ids did not all execute
    is never recorded, so a partial run cannot install a partial dependency set.
    Known blind spot: a test observing a module-level value of a module it
    neither imports nor executes code in (for example a registry entry another
    module adds at import) is rerun only when its own dependencies change; full
    recording runs (``landing_verification record``) re-check everything.

Entry points
    ``Tree``, ``ImpactStore``, ``records_from_observations``, ``affected``,
    ``plan``, ``same_code``, ``plugin_source``, ``unit_fingerprints``,
    ``unit_of``.

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
ALL_UNITS = ['*']  # a Python dependency whose code, in any unit, counts
CONFTEST_UNIT = '<conftest>'  # the conftest code that runs for every collected file


def _constant(node) -> bool:
    """Whether evaluating NODE can only produce a literal (no lookup or call)."""
    return isinstance(node, ast.Constant) or (
        isinstance(node, (ast.Tuple, ast.List, ast.Set)) and all(_constant(item) for item in node.elts)) or (
        isinstance(node, ast.Dict) and all(key is None or _constant(key) for key in node.keys)
        and all(_constant(value) for value in node.values)) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not))
        and _constant(node.operand))


def _postponed_annotations(tree) -> bool:
    return any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
               and any(alias.name == 'annotations' for alias in node.names) for node in tree.body)


def _inert_definition(node, postponed: bool) -> bool:
    """A definition whose header does nothing but bind its own name.

    Such a body can be its own dependency unit: editing it cannot change what
    an importer that never calls it observes. The name stays in the enclosing
    unit, because adding or removing a top-level name changes duck-typed
    discovery (``getattr(module, 'hook', None)``) even for code that never
    calls it. Decorators, defaults and evaluated annotations run at import
    time and therefore keep the header where it was.
    """
    if node.decorator_list:
        return False
    args = node.args
    defaults = [*args.defaults, *(value for value in args.kw_defaults if value is not None)]
    if not all(_constant(value) for value in defaults):
        return False
    annotations = [arg.annotation for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg is not None:
        annotations.append(args.vararg.annotation)
    if args.kwarg is not None:
        annotations.append(args.kwarg.annotation)
    annotations.append(node.returns)
    return postponed or all(value is None or _constant(value) for value in annotations)


def _inert_class(node, postponed: bool, methods: set[str]) -> bool:
    """Whether a class body only binds method names and constants.

    Class-level calls and computed attributes execute at import time and are
    attributed to the class body, so such a class keeps its own code in the
    module unit. A plain method table can be reduced to the method names:
    their bodies then change only their own units.
    """
    if node.decorator_list or node.keywords:
        return False
    for base in node.bases:
        if not isinstance(base, ast.Name):
            return False
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _inert_definition(item, postponed):
                return False
        elif isinstance(item, ast.Pass):
            continue
        elif isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant):
            continue  # the class docstring
        elif isinstance(item, (ast.Assign, ast.AnnAssign)):
            value = item.value
            if value is not None and not _constant(value):
                return False
        else:
            return False
    return True


def _name_only(node):
    """The definition reduced to the name it binds (its inert header's share)."""
    copy = ast.FunctionDef() if isinstance(node, ast.FunctionDef) else ast.AsyncFunctionDef()
    copy.name, copy.args, copy.body = node.name, node.args, [ast.Pass()]
    copy.decorator_list = []
    copy.returns = None
    copy.type_comment = None
    copy.type_params = []
    return copy


def _conftest_classes(tree):
    """Methods of conftest classes, and whether their bodies stay shared.

    A class defined in a conftest is reachable through the objects the session
    builds, so its methods can run at fixture teardown outside any test's
    bucket. Its method names are part of the class's import-time shape (a
    ``hasattr`` on a hook object sees them), while the bodies stay separate
    units. Class-level statements that compute or call are dynamic: they keep
    the whole class shared, since the recorder attributes them to the class
    body rather than to a method.
    """
    methods, shared = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        names = {item.name for item in node.body
                 if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        methods |= {f'{node.name}.{name}' for name in names}
        dynamic = not _inert_class(node, _postponed_annotations(tree), names)
        if dynamic:
            shared |= {f'{node.name}.{name}' for name in names}
            shared.add(node.name)
    return methods, shared


def conftest_units(source: str, path: str) -> set[str]:
    """The conftest units that execute outside any single test file.

    Module-level statements, class bodies and their local calls run at import
    time, outside the per-test buckets; their transitive local helpers run
    there too. A test file depends on those units always, on the names of the
    conftest's hooks, fixtures and helper classes (pytest discovers them by
    name), and only on the further units its own run actually executed.
    Anything dynamic keeps the whole file as an input.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {ALL_UNITS[0]}
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    module = [node for node in tree.body
              if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    # Hooks and decorated fixtures can also finish outside a test's bucket
    # (teardown, session end), so their transitive local helpers are shared
    # inputs even when no test observed them directly.
    module.extend(node for node in functions.values()
                  if node.name.startswith('pytest_') or node.decorator_list)
    reached: dict[int, object] = {}

    def close(nodes):
        pending = list(nodes)
        while pending:
            node = pending.pop()
            reached[id(node)] = node
            for item in ast.walk(node):
                helper = functions.get(item.id) if isinstance(item, ast.Name) else None
                if helper is not None and id(helper) not in reached:
                    pending.append(helper)
    close(module)  # module-level calls run at import, outside every test bucket
    units = {CONFTEST_UNIT}
    for node in reached.values():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.add(node.name)
    methods, dynamic = _conftest_classes(tree)
    # Hook and fixture names are read by pytest at collection, so adding or
    # removing one changes every run in scope even when no body is edited.
    names = {name for name, node in functions.items()
             if name.startswith('pytest_') or node.decorator_list}
    return units | methods | dynamic | names


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

    postponed = _postponed_annotations(tree)

    def visit(statements, prefix):
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                header = ast.dump(ast.FunctionDef(name=node.name, args=node.args, body=[ast.Pass()],
                                                  decorator_list=node.decorator_list,
                                                  returns=node.returns, type_params=[]))
                bodies.setdefault(prefix + node.name, []).append(
                    header + '\n' + ast.dump(ast.Module(body=node.body, type_ignores=[])))
                # The docstring stays in the module unit: decorators may read it at import time.
                node.body = node.body[:1] if docstring(node.body) is not None else [ast.Pass()]
                if _inert_definition(node, postponed):
                    # Only the bound name is import-time behaviour; the header
                    # fingerprint above already moved into the function's unit.
                    # Only the bound name is import-time behaviour; the header
                    # fingerprint above already moved into the function's unit.
                    node.args = ast.arguments(posonlyargs=[], args=[], vararg=None,
                                              kwonlyargs=[], kw_defaults=[], kwarg=None,
                                              defaults=[])
                    node.returns = None
            elif isinstance(node, ast.ClassDef):
                before_methods = {item.name for item in node.body
                                  if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
                visit(node.body, f'{prefix}{node.name}.')
                if _inert_class(node, postponed, before_methods):
                    # Method names are the class's import-time shape; their
                    # headers/bodies are separate units (see above).
                    node.body = [_name_only(item) if isinstance(item, (ast.FunctionDef,
                                                                       ast.AsyncFunctionDef))
                                 else item for item in node.body]
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

    def __init__(self, blobs: dict[str, str], root=None, source=None):
        self.blobs = blobs
        self.root = root or '.'
        # Optional text loader for trees that are not Git revisions (synthetic
        # trees in the recorder's own specification). None means unavailable.
        self.source = source
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

    def __init__(self, directory, environment_key: str):
        self._cache = ImpactCache(directory, environment_key)
        self.path = self._cache.path
        self._fingerprints: dict[str, dict[str, str] | None] = {}

    def close(self):
        self._cache.close()

    def records(self) -> dict[str, Record]:
        return self._cache.records()

    def save(self, records: list[Record], *, replace_tests=()) -> None:
        self._cache.save(records, replace_tests=replace_tests)

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


def same_code(root, before_blob: str | None, after_blob: str | None,
              store: ImpactStore | None) -> bool:
    """Whether two Python blobs define the same code: only comments or layout differ."""
    if not before_blob or not after_blob:
        return False
    if before_blob == after_blob:
        return True

    def units(blob):
        if store is not None:
            return store.fingerprints(root, blob)
        shown = _git(root, 'cat-file', 'blob', blob)
        return unit_fingerprints(shown.stdout) if shown.returncode == 0 else None

    before = units(before_blob)
    return before is not None and before == units(after_blob)


def conftest_chain(test: str, paths=None) -> list[str]:
    """The conftest.py files pytest loads for TEST, from the repository root down."""
    chain, directory = [], os.path.dirname(test)
    while True:
        candidate = f'{directory}/conftest.py' if directory else 'conftest.py'
        if paths is None or candidate in paths:
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
                              refs: dict[str, set[str]],
                              global_inputs: dict[str, str] | None = None) -> list[Record]:
    """Records for whole-file runs; incomplete observations keep a file unrecorded.

    REFS are each test file's direct static references (imports and literal
    paths). A referenced Python file counts through its module body even when
    another test file triggered its import; a test that starts a program whose
    reads are unobserved, or forks, is opaque, so every referenced file counts
    byte for byte. GLOBAL_INPUTS maps each blast-radius path to its scope:
    ``ALL`` or the basename prefix of the test files it can change.
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
                failed=set(), fixtures=set(), collected=set(), executed=set(), skipped=set()))
            for field in ('failed', 'fixtures', 'collected', 'executed', 'skipped'):
                merged[field].update(line.get(field) or ())
            _merge(merged, line)
    records = []
    for test in tests:
        observed = files.get(test)
        if (observed is None or not observed['collected'] or observed['skipped']
                or not observed['collected'] <= observed['executed']):
            continue
        merged = _empty()
        _merge(merged, {'units': observed['units'], 'data': observed['data'],
                        'dirs': observed['dirs'], 'opaque': observed['opaque']})
        for key in observed['fixtures']:
            if key in fixtures:
                _merge(merged, {'units': fixtures[key]['units'], 'data': fixtures[key]['data'],
                                'dirs': fixtures[key]['dirs'], 'opaque': fixtures[key]['opaque']})
        executed = set(merged['units'])
        for module in executed | set(refs.get(test, ())):
            executed.update(import_time.get(module, {}))
        if executed - tree.blobs.keys():
            merged['opaque'] = True
        deps = _dependencies(test, merged, tree, refs.get(test, ()), import_time,
                             global_inputs or {})
        failed = sorted(observed['failed'])
        records.append(Record(test, tree_id, 'failed' if failed else 'passed', failed,
                              merged['opaque'], deps,
                              {directory: tree.listing(directory) for directory in merged['dirs']}))
    return records


def _dependencies(test: str, observed: dict, tree: Tree, refs, import_time,
                  global_inputs: dict[str, str]) -> dict:
    deps: dict[str, tuple[str, list[str] | None]] = {}

    def need(path, names):
        # Strongest wins: bytes (WHOLE_FILE), then any code (ALL_UNITS), then named units.
        # Absence is an input too: creating an optional read/import or a new
        # conftest must invalidate the run that observed it missing.
        blob, known = deps.get(path, (tree.blobs.get(path, ''), []))
        if known is WHOLE_FILE or names is WHOLE_FILE:
            deps[path] = (blob, WHOLE_FILE)
        elif known == ALL_UNITS or names == ALL_UNITS:
            deps[path] = (blob, ALL_UNITS)
        else:
            deps[path] = (blob, sorted(set(known) | set(names)))

    def by_code(path):
        return ALL_UNITS if path.endswith('.py') else WHOLE_FILE

    # The conftest chain is loaded for every test file in a run, but which files
    # share a session depends on how the run is split. Keep it out of the units
    # a test file's own session observed; the directory listing below covers
    # shared state such as a registry whose entries a conftest imports.
    own_units = {path: names for path, names in observed['units'].items()
                 if not path.endswith('conftest.py')}
    own_data = {path for path in observed['data'] if not path.endswith('conftest.py')}
    for path, names in own_units.items():
        need(path, {MODULE_UNIT, *names})  # executed code reads its module's globals
    for path in refs:
        need(path, WHOLE_FILE if observed['opaque'] or not path.endswith('.py')
             else {MODULE_UNIT})
    for path in [*own_data, test]:
        need(path, WHOLE_FILE)
    for path in conftest_chain(test):
        if path not in tree.blobs or not path.endswith('.py'):
            need(path, by_code(path))
            continue
        # A conftest's session/collection code is an input to every test in its
        # scope; the helpers a particular run executed are inputs to that run
        # only. Its own module unit keeps the imports and the hook/fixture
        # declarations that pytest reads at collection, while helper bodies
        # stay separate units: a signature-only edit to one helper must not
        # re-run the whole suite.
        source = tree.source(path) if tree.source is not None else _git(
            tree.root, 'cat-file', 'blob', tree.blobs[path]).stdout
        names = conftest_units(source, path) if source is not None else {ALL_UNITS[0]}
        need(path, {MODULE_UNIT, *names} | observed['units'].get(path, set()))
    basename = os.path.basename(test)
    for path, scope in global_inputs.items():
        if scope == 'ALL' or basename.startswith(scope):
            need(path, by_code(path))
    # Functions that ran while a module this test depends on was imported built
    # state the test observes, whichever test file triggered the import.
    for module in [path for path in deps if path.endswith('.py')]:
        for path, names in import_time.get(module, {}).items():
            need(path, names)
    return deps


def affected(record: Record, tree: Tree, store: ImpactStore, root) -> str:
    """The first dependency of RECORD that differs in TREE, or '' when none differs."""
    for path, (recorded_blob, names) in record.deps.items():
        current = tree.blobs.get(path, '')
        if current == recorded_blob:
            continue
        if not current or not recorded_blob or names is WHOLE_FILE or not path.endswith('.py'):
            return path
        before = store.fingerprints(root, recorded_blob)
        after = store.fingerprints(root, current)
        if before is None or after is None:
            return path
        if names == ALL_UNITS:
            if before != after:
                return path
        elif any(before.get(name) != after.get(name) for name in names):
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
         tip: Tree, merged: Tree, store: ImpactStore, root) -> Plan:
    """Which of TESTS must run on the merged tree, and why.

    A recorded file runs when a dependency differs in the merged tree; when
    it failed and the current branch differs from its recorded tree (so it may
    pass there); or when static selection chooses an opaque file, whose
    external inputs cannot be proved unchanged. An unrecorded file runs when
    the static selector chose it.
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
        elif record.opaque and reason is not None:
            run[test] = 'selected test has inputs outside the recorded snapshot'
        else:
            skipped += 1
    return Plan(run, skipped, sum(1 for test in tests if test in records))


def plugin_source(snapshot_paths=None) -> str:
    """The recorder, copied next to each run so the repository under test need not ship it."""
    source = (Path(__file__).with_name('impact_recorder.py')).read_text(encoding='utf-8')
    if snapshot_paths is not None:
        source = source.replace('_SNAPSHOT_PATHS = None',
                                f'_SNAPSHOT_PATHS = frozenset({sorted(snapshot_paths)!r})', 1)
    return source
