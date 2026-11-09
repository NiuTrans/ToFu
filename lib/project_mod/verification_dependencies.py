"""Expand changed inputs through the candidate's static source dependency graph.

Entry point: expand_changes. Sources come from immutable Git blobs; reference
policy belongs to scripts/test_select.py. One replaced, checksummed cache holds
the current Python inventory. Missing/corrupt caches only cost reparsing.
Contract: docs/modules/project_integration.md, Merge verification policy.
"""
from __future__ import annotations

from collections import defaultdict, deque
import ast
import hashlib
from pathlib import Path
import sys

from lib.project_mod.verification_index import _checksum, _publish, _read_cache, _sources


def _fixture_scopes(path):
    parts = path.split('/')[:-1]
    return ['/'.join([*parts[:end], 'conftest.py']) for end in range(len(parts) + 1)]


def _requested_fixtures(nodes):
    names = set()
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.update(arg.arg for arg in [*node.args.posonlyargs, *node.args.args,
                                                 *node.args.kwonlyargs])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ('usefixtures', 'getfixturevalue'):
                    for arg in node.args:
                        names.add(arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else '*')
    return names


def _fixture_refs(path, names):
    return {f'{scope}::fixture:{name}' for scope in _fixture_scopes(path) for name in names}


def _dependency_nodes(source, path, selector):
    """Separate lazy fixtures from the conftest hooks and autouse execution."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {path: set()}
    imports = selector.source_references(source, path, tree=tree)
    if not path.endswith('/conftest.py') and path != 'conftest.py':
        if path.startswith('tests/'):
            imports |= set(_fixture_scopes(path))
            imports |= {f'{scope}::autouse' for scope in _fixture_scopes(path)}
            imports |= _fixture_refs(path, _requested_fixtures(tree.body))
        return {path: imports}
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    aliases = {'fixture'}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in ('pytest', 'pytest_asyncio'):
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name == 'fixture')
    fixtures, autouse, hooks = {}, set(), []
    for name, function in functions.items():
        fixture = None
        for decorator in function.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (isinstance(target, ast.Attribute) and target.attr == 'fixture') or (
                    isinstance(target, ast.Name) and target.id in aliases):
                fixture = decorator
        if fixture is None:
            if name.startswith('pytest_') or function.decorator_list:
                hooks.append(function)
            continue
        exposed, automatic = name, False
        for keyword in fixture.keywords if isinstance(fixture, ast.Call) else ():
            if keyword.arg is None:
                return {path: imports}  # dynamic registration may install an autouse fixture
            if keyword.arg == 'name':
                if not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, str):
                    return {path: imports}  # unknown fixture registration: whole conftest input
                exposed = keyword.value.value
            if keyword.arg == 'autouse':
                automatic = not isinstance(keyword.value, ast.Constant) or bool(keyword.value.value)
        fixtures[exposed] = function
        if automatic:
            autouse.add(f'{path}::fixture:{exposed}')

    def requirements(roots):
        reached, pending = {}, list(roots)
        while pending:
            node = pending.pop()
            reached[id(node)] = node
            for item in ast.walk(node):
                helper = functions.get(item.id) if isinstance(item, ast.Name) else None
                if helper is not None and id(helper) not in reached:
                    reached[id(helper)] = helper
                    pending.append(helper)
        nodes = list(reached.values())
        code = ast.Module(body=nodes, type_ignores=[])
        return selector.source_references('', path, tree=code) | _fixture_refs(path, _requested_fixtures(nodes))

    module = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions.values():
        # Decorators and defaults execute while conftest imports, even when
        # no test requests the fixture body itself.
        module.extend(function.decorator_list)
        module.extend(function.args.defaults)
        module.extend(value for value in function.args.kw_defaults if value is not None)
    result = {path: requirements([*module, *hooks]), f'{path}::autouse': autouse,
              f'{path}::fixture:*': {f'{path}::fixture:{name}' for name in fixtures}}
    result.update({f'{path}::fixture:{name}': requirements([function])
                   for name, function in fixtures.items()})
    return result


def _valid_nodes(entry):
    nodes = entry.get('nodes')
    return isinstance(nodes, dict) and all(isinstance(name, str) and isinstance(refs, list)
        and all(isinstance(ref, str) for ref in refs) for name, refs in nodes.items())


def expand_changes(root, tree, changed, selector, cache_path):
    """Include all transitive importers, including cycles and removed targets."""
    extract = getattr(selector, 'source_references', None)
    if extract is None or not any(path.endswith('.py') for path in changed):
        return list(changed)  # other repositories retain their declared selector
    inventory = {path: blob for path, blob in tree.blobs.items() if path.endswith('.py')}
    identity = hashlib.sha256(Path(selector.__file__).read_bytes()
                              + Path(__file__).read_bytes()
                              + sys.implementation.cache_tag.encode()).hexdigest()
    saved = _read_cache(cache_path, identity) if cache_path else {}
    missing = {path: blob for path, blob in inventory.items()
               if saved.get(path, {}).get('blob') != blob or not _valid_nodes(saved[path])}
    sources = _sources(root, sorted(set(missing.values())))
    entries = {}
    for path, blob in inventory.items():
        if path in missing:
            nodes = {name: sorted(refs) for name, refs in
                     _dependency_nodes(sources[blob], path, selector).items()}
            entries[path] = {'blob': blob, 'refs': nodes[path], 'nodes': nodes}
        else:
            entries[path] = saved[path]
    if cache_path and entries != saved:
        error = _publish(cache_path, {'version': 1, 'extractor': identity, 'entries': entries,
                                     'checksum': _checksum(entries)})
        if error:
            print(f'[verification] dependency cache unavailable: {error}', file=sys.stderr)
    importers = defaultdict(set)
    for entry in entries.values():
        for node, refs in entry['nodes'].items():
            for dependency in refs:
                importers[dependency].add(node)
    reached = set(changed)
    unknown_plugin = getattr(selector, 'UNKNOWN_PLUGIN_INPUT', None)
    if unknown_plugin:
        reached.add(unknown_plugin)
    pending = deque(reached)
    while pending:
        for importer in importers[pending.popleft()] - reached:
            reached.add(importer)
            pending.append(importer)
    return sorted(reached)
