"""Module-level imports of tofu_search form no cycle, counting CPython's hidden waits.

CPython initializes each module under a per-module lock. When two threads
first-import different members of one import cycle, its deadlock avoidance hands
one thread a partially initialized module, which fails as ``cannot import name
... (most likely due to a circular import)``. A host races entry points this way
(a readiness import of the facade beside a task thread's
``from tofu_search.search.vertical import describe_domains``).

Import statements reach ``__import__``, which returns the *top-level* package
and therefore waits until ``tofu_search`` (the facade) is initialized when it

* is a dotted ``import package.module``;
* is ``from package import submodule`` for a not yet bound submodule; or
* targets a module whose intermediate package is still absent: CPython loads
  that parent through ``__import__`` first.

The facade eagerly imports almost every module, so such a statement inside one
of them closes a cycle through the facade. Names imported from a concrete module
whose packages are already loaded, and ``importlib.import_module`` (which returns
the leaf), wait only for the modules they name. A first import also runs absent
ancestor packages in the importing thread; that parent edge is included.

CPython runs that ancestor import while holding the imported module's lock, so a
subpackage must itself load every child the facade needs (second rule below).
"""

from __future__ import annotations

import ast
from itertools import pairwise
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / 'tofu_search'


def _module_files(package_dir: Path) -> dict[str, Path]:
    modules = {}
    for path in sorted(package_dir.rglob('*.py')):
        parts = path.relative_to(package_dir.parent).with_suffix('').parts
        if parts[-1] == '__init__':
            parts = parts[:-1]
        modules['.'.join(parts)] = path
    return modules


def _ancestors(name: str) -> list[str]:
    parts = name.split('.')
    return ['.'.join(parts[:index]) for index in range(1, len(parts))]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING') or (
        isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')


def _import_time_nodes(node: ast.AST):
    """Yield nodes executed while the module body runs (not function bodies)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            for statement in child.orelse:
                yield statement
                yield from _import_time_nodes(statement)
            continue
        yield child
        yield from _import_time_nodes(child)


def _import_records(module: str, path: Path, modules: dict[str, Path]):
    """Yield ``(node, kind, target, submodules, unconditional)`` in source order."""
    tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
    is_package = path.name == '__init__.py'
    unconditional_nodes = {
        id(child) for statement in tree.body
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Expr))
        for child in ast.walk(statement)}
    records = []
    for node in _import_time_nodes(tree):
        if isinstance(node, ast.Import):
            entries = [('import', alias.name, []) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module if is_package else module.rpartition('.')[0]
                for _ in range(node.level - 1):
                    base = base.rpartition('.')[0]
                target = f'{base}.{node.module}' if node.module else base
            else:
                target = node.module
            submodules = [f'{target}.{alias.name}' for alias in node.names
                          if f'{target}.{alias.name}' in modules]
            entries = [('from', target, submodules)]
        elif isinstance(node, ast.Call) and node.args and isinstance(
                node.args[0], ast.Constant) and isinstance(node.args[0].value, str) and (
                getattr(node.func, 'id', None) == 'import_module'
                or getattr(node.func, 'attr', None) == 'import_module'):
            entries = [('leaf', node.args[0].value, [])]
        else:
            continue
        for kind, target, submodules in entries:
            if target in modules:
                records.append(
                    (node, kind, target, submodules, id(node) in unconditional_nodes))
    return sorted(records, key=lambda record: (record[0].lineno, record[0].col_offset))


def _import_graph(package_dir: Path) -> dict[str, dict[str, str]]:
    """Map each module to the modules its body may wait for, with the import site."""
    modules = _module_files(package_dir)
    records = {name: _import_records(name, path, modules) for name, path in modules.items()}
    loads = {name: {module for _, _, target, submodules, unconditional in entries
                    if unconditional for module in (target, *submodules)}
             for name, entries in records.items()}

    def present_after(name: str) -> set[str]:
        """Modules certainly in ``sys.modules`` once ``name`` finished importing."""
        present, pending = set(), [name]
        while pending:
            current = pending.pop()
            if current in present or current not in modules:
                continue
            present.add(current)
            pending.extend(loads[current])
            pending.extend(_ancestors(current))
        return present

    return {module: _module_waits(module, entries, modules, present_after, package_dir)
            for module, entries in records.items()}


def _module_waits(module, entries, modules, present_after, package_dir) -> dict[str, str]:
    """Modules that one module's body may wait for, keyed to the import site."""
    path = modules[module]
    lines = path.read_text(encoding='utf-8').splitlines()
    established = {module, *_ancestors(module)}
    waits: dict[str, str] = {}

    def add(target: str, node: ast.AST, why: str = '') -> None:
        if target in modules and target != module:
            where = f'{path.relative_to(package_dir.parent)}:{node.lineno}'
            waits.setdefault(target, f'{where}: {lines[node.lineno - 1].strip()}{why}')

    for node, kind, target, submodules, unconditional in entries:
        top = target.split('.')[0]
        add(target, node)
        for ancestor in _ancestors(target):
            if ancestor not in established:
                add(ancestor, node, '  [loads absent parent]')
        for submodule in submodules:
            add(submodule, node)
            add(top, node, '  [waits for top-level package: submodule via package]')
        if kind == 'import' and '.' in target:
            add(top, node, '  [waits for top-level package: dotted import]')
        if kind != 'leaf' and any(
                ancestor not in established for ancestor in _ancestors(target)[1:]):
            add(top, node, '  [waits for top-level package: absent parent package]')
        if unconditional:
            for loaded in (target, *submodules):
                established |= present_after(loaded)
    return waits


def _cyclic_components(graph) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return components


def _shortest_cycle(graph, component) -> list[str]:
    members = set(component)
    best = None
    for start in component:
        previous = {start: None}
        frontier = [start]
        closing = None
        while frontier and closing is None:
            following = []
            for node in frontier:
                if start in graph[node]:
                    closing = node
                    break
                for target in graph[node]:
                    if target in members and target not in previous:
                        previous[target] = node
                        following.append(target)
            frontier = following
        if closing is None:
            continue
        path = [closing]
        while previous[path[-1]] is not None:
            path.append(previous[path[-1]])
        cycle = list(reversed(path)) + [start]
        if best is None or len(cycle) < len(best):
            best = cycle
    return best


def _cycle_reports(graph) -> list[str]:
    reports = []
    for component in _cyclic_components(graph):
        cycle = _shortest_cycle(graph, component)
        steps = '\n'.join(
            f'    {source} -> {target}\n        {graph[source][target]}'
            for source, target in pairwise(cycle))
        reports.append(f'  {len(component)} modules, e.g.:\n{steps}')
    return reports


@pytest.mark.parametrize(('registry', 'cyclic'), [
    ('from pkg.sub.leaf import value\n', False),
    ('from pkg.sub import leaf\nvalue = leaf.value\n', True),
    ('import pkg.sub.leaf\nvalue = 1\n', True),
    ('from pkg.other.leaf import value\n', True),
    ("from importlib import import_module\nvalue = import_module('pkg.other.leaf').value\n", False),
    ("from importlib import import_module\nimport_module('pkg.other.leaf')\n"
     'from pkg.other.leaf import value\n', False),
])
def test_model_counts_hidden_top_level_package_waits(tmp_path, registry, cyclic):
    package = tmp_path / 'pkg'
    for subpackage in ('sub', 'other'):
        (package / subpackage).mkdir(parents=True)
        (package / subpackage / '__init__.py').write_text('')
        (package / subpackage / 'leaf.py').write_text('value = 1\n')
    (package / '__init__.py').write_text('from pkg.sub.registry import value\n')
    (package / 'sub' / 'registry.py').write_text(registry)
    reports = _cycle_reports(_import_graph(package))
    assert bool(reports) is cyclic, reports
    if cyclic:
        assert 'pkg.sub.registry -> pkg' in reports[0], reports


def test_subpackages_load_every_child_the_facade_needs():
    """A first ``import package.child`` with ``package`` absent holds the child's
    lock while CPython loads the package, then waits for the facade. The facade
    needs that child too, so the package's own unconditional imports complete it.
    """
    modules = _module_files(_PACKAGE_DIR)
    records = {name: _import_records(name, path, modules) for name, path in modules.items()}
    every = {name: {module for _, _, target, submodules, _ in entries
                    for module in (target, *submodules)} for name, entries in records.items()}
    unconditional = {name: {module for _, _, target, submodules, always in entries if always
                            for module in (target, *submodules)}
                     for name, entries in records.items()}

    def loaded_by(start, edges, *, ancestors):
        seen, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current in seen or current not in modules:
                continue
            seen.add(current)
            pending.extend(edges[current])
            pending.extend(name for name in _ancestors(current)
                           if ancestors or name.startswith(f'{start}.'))
        return seen

    facade_needs = loaded_by('tofu_search', every, ancestors=True)
    exposed = {}
    for package, path in modules.items():
        if path.name != '__init__.py' or '.' not in package:
            continue
        children = {name for name in facade_needs if name.startswith(f'{package}.')}
        missing = children - loaded_by(package, unconditional, ancestors=False)
        if missing:
            exposed[package] = sorted(missing)
    assert not exposed, f'subpackage __init__ must load these facade dependencies: {exposed}'


def test_module_level_imports_are_acyclic():
    graph = _import_graph(_PACKAGE_DIR)
    assert 'tofu_search.search.vertical' in graph['tofu_search.search']
    reports = _cycle_reports(graph)
    assert not reports, (
        'module-level import cycle(s); concurrent first imports from different '
        'entry points can receive a partially initialized module:\n'
        + '\n'.join(reports))
