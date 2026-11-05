"""Suites that consume the built frontend graph or node_modules name it first.

Without ``static/vite`` the live app answers its index with 503, and every
browser test used to wait out its own selector timeout instead of reporting
the missing build. ``browser`` and ``live_server`` are replaced here so the
prerequisite is observable without launching anything. Suites that read the
checkout's graph directly (manifest, emitted assets) used to fail with
``ViteAssetError`` or ``FileNotFoundError`` instead of naming the remedy.
Suites that run repository node_modules without ``npm ci`` failed on a
bundler or ``require`` error, or hand-skipped silently even where
``TOFU_REQUIRE_FRONTEND=1`` promises a red lane.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests import _jsdom

pytestmark = pytest.mark.unit


class _UnopenableBrowser:
    def new_context(self):
        raise AssertionError('page opened a browser context without the built frontend graph')


@pytest.fixture
def browser():
    return _UnopenableBrowser()


@pytest.fixture
def live_server():
    return 'http://127.0.0.1:9'


def test_page_skips_without_the_built_frontend_graph(request, monkeypatch):
    monkeypatch.setattr(_jsdom, 'shipped_graph_available', lambda: False)
    monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
    with pytest.raises(pytest.skip.Exception, match='built frontend graph missing'):
        request.getfixturevalue('page')


class _Item:
    def __init__(self, *fixturenames):
        self.fixturenames = fixturenames


def test_browser_on_the_live_app_skips_before_its_fixtures_start(monkeypatch):
    from tests import conftest
    monkeypatch.setattr(_jsdom, 'shipped_graph_available', lambda: False)
    monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
    with pytest.raises(pytest.skip.Exception, match='built frontend graph missing'):
        next(conftest.pytest_runtest_setup(item=_Item('browser', 'live_server')))
    monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', '1')
    with pytest.raises(pytest.fail.Exception, match='TOFU_REQUIRE_FRONTEND=1'):
        next(conftest.pytest_runtest_setup(item=_Item('browser', 'live_server')))


@pytest.mark.parametrize('fixturenames', [('browser',), ('live_server',), ()])
def test_graph_check_needs_both_the_browser_and_the_live_app(monkeypatch, fixturenames):
    from tests import conftest
    monkeypatch.setattr(_jsdom, 'shipped_graph_available', lambda: False)
    monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
    hook = conftest.pytest_runtest_setup(item=_Item(*fixturenames))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(None)


def test_frontend_lanes_fail_when_page_lacks_the_built_graph(request, monkeypatch):
    monkeypatch.setattr(_jsdom, 'shipped_graph_available', lambda: False)
    monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', '1')
    with pytest.raises(pytest.fail.Exception, match='TOFU_REQUIRE_FRONTEND=1'):
        request.getfixturevalue('page')



def test_direct_graph_reader_skips_with_the_remedy(monkeypatch):
    from tests.i18n import test_i18n_pack_emission as reader
    monkeypatch.setattr(_jsdom, 'shipped_graph_available', lambda: False)
    monkeypatch.delenv('TOFU_REQUIRE_FRONTEND', raising=False)
    with pytest.raises(pytest.skip.Exception, match='npm run build:frontend'):
        reader._manifest()
    monkeypatch.setenv('TOFU_REQUIRE_FRONTEND', '1')
    with pytest.raises(pytest.fail.Exception, match='TOFU_REQUIRE_FRONTEND=1'):
        reader._manifest()


ROOT = Path(__file__).resolve().parents[2]
_GRAPH_NAMES = frozenset({'VITE_MANIFEST', 'VITE_OUT_DIR'})
_GRAPH_READS = frozenset({'validate_vite_artifact', 'validate_published_vite_artifact'})
_CHECKOUT_ROOTS = frozenset({'ROOT', 'root'})


def _called(function) -> str:
    return getattr(function, 'id', None) or getattr(function, 'attr', '')


def _fallback_nodes(function) -> set[int]:
    """A read whose own handler falls back on any failure is optional."""
    optional = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Try) and any(
                handler.type is None or _called(handler.type) in ('Exception', 'BaseException')
                for handler in node.handlers):
            optional.update(id(inner) for statement in node.body for inner in ast.walk(statement))
    return optional


def _names_checkout_graph(node) -> bool:
    if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
        return (getattr(node, 'id', None) or getattr(node, 'attr', '')) in _GRAPH_NAMES
    return (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name) and node.left.id in _CHECKOUT_ROOTS
            and isinstance(node.right, ast.Constant)
            and str(node.right.value).startswith('static/vite'))


def _graph_facts(function):
    """(reads the checkout graph, checks for it first, local callees)."""
    parameters = {argument.arg for argument in function.args.args}
    checked = 'page' in parameters or {'browser', 'live_server'} <= parameters
    reads = False
    callees = set()
    optional = _fallback_nodes(function)
    for node in ast.walk(function):
        if id(node) in optional:
            continue
        reads |= _names_checkout_graph(node)
        if isinstance(node, ast.Call):
            name = _called(node.func)
            reads |= name in _GRAPH_READS
            checked |= name == 'require_shipped_graph'
            # A synthetic graph is its own prerequisite.
            checked |= name == 'setattr' and any(
                isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                and argument.value.rsplit('.', 1)[-1] in _GRAPH_NAMES
                for argument in node.args)
            if isinstance(node.func, ast.Name):
                callees.add(node.func.id)
        elif isinstance(node, ast.Assert):
            # An explicit lane may assert the checkout graph's presence itself.
            checked |= any(
                isinstance(inner, ast.Call) and _called(inner.func) in ('is_file', 'exists')
                and isinstance(inner.func, ast.Attribute)
                and any(_names_checkout_graph(part) for part in ast.walk(inner.func.value))
                for inner in ast.walk(node.test))
    return reads, checked, callees


def _unchecked_graph_readers(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    if any(isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
           and _called(statement.value.func) == 'require_shipped_graph'
           for statement in tree.body):
        return []
    facts = {node.name: _graph_facts(node) for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def resolve(name, seen):
        reads, checked, callees = facts[name]
        for callee in (callees & set(facts)) - seen:
            callee_reads, callee_checked = resolve(callee, seen | {name})
            reads |= callee_reads
            checked |= callee_checked
        return reads, checked

    return [name for name in facts if name.startswith('test_')
            and resolve(name, frozenset()) == (True, False)]


def test_suites_reading_the_checkout_graph_check_for_it_first():
    """Fresh clones and worktrees have no ``static/vite`` until a build.

    A suite that reads the checkout's built graph calls
    ``require_shipped_graph()`` first (or runs through ``page`` / the live-app
    browser hook, installs a synthetic graph, or asserts presence itself), so a
    missing build skips with the remedy and ``TOFU_REQUIRE_FRONTEND=1`` lanes
    still fail.
    """
    files = subprocess.run(['git', 'ls-files', '-z', 'tests'], cwd=ROOT, check=True,
                           capture_output=True, text=True).stdout.split('\0')
    unchecked = []
    for relative in files:
        if not relative.endswith('.py') or relative.startswith('tests/meta/'):
            continue
        path = ROOT / relative
        text = path.read_text(encoding='utf-8')
        if any(token in text for token in (*_GRAPH_NAMES, *_GRAPH_READS, 'static/vite')):
            unchecked += [f'{relative}::{name}' for name in _unchecked_graph_readers(path)]
    assert not unchecked, unchecked



# ── Suites that run repository node_modules ──────────────────────────────────
#
# A test runs repository node_modules when its call tree — local helpers,
# ``self`` methods, fixtures (module, conftest chain, autouse, usefixtures) and
# helpers imported from other ``tests`` modules — starts a subprocess and names
# node_modules code: a repository JS entry point whose import closure loads a
# ``package.json`` package (``scripts/vite_test_bundle.mjs``,
# ``tests/_jsdom_harness.js``), inline JS importing such a package
# (``require('jsdom')``), a ``node_modules/<package>`` path, or ``npm``/``npx``.
# Such a test must reach a ``tests/_jsdom.py`` / ``tests/_runtime_sections.py``
# prerequisite helper, in its call tree or at module import, and must not skip
# on the dependency by hand: a ``skipif(not node_deps_available())`` stays
# silent under ``TOFU_REQUIRE_FRONTEND=1``, where the helpers fail.
_ARGUMENT_GUARDS = frozenset({'require_node_modules', 'require_node_test_runner'})
_NODE_GUARDS = frozenset({*_ARGUMENT_GUARDS, '_require_frontend_bundler', 'run_harness',
                          'skip_or_fail', 'frontend_module_guard'})
_PROCESS_CALLS = frozenset({'run', 'Popen', 'check_output', 'check_call', 'call',
                            'create_subprocess_exec', 'create_subprocess_shell'})
_SKIP_CALLS = frozenset({'skip', 'skipif', 'importorskip'})
_DEPENDENCY_SKIP = re.compile(r'node|jsdom|\bnp[mx]\b|\btsc\b|vite|esbuild|typescript|postcss',
                              re.IGNORECASE)
_JS_SPECIFIER = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|^\s*import\s+|\brequire(?:\.resolve)?\(\s*)"""
    r"""['"]([^'"\n]+)['"]""", re.MULTILINE)
_INSTALLED_PATH = re.compile(
    r"""node_modules['"]?\s*(?:[/\\]|,\s*['"])\s*(\.bin|@[\w.-]+/[\w.-]+|[\w.-]+)""")
# Node resolves these before node_modules, so a lockfile package of the same
# name (a polyfill such as ``punycode``) is never what ``require`` loads.
_NODE_BUILTINS = frozenset((
    'assert buffer child_process cluster console constants crypto dgram '
    'diagnostics_channel dns domain events fs http http2 https inspector module '
    'net os path perf_hooks process punycode querystring readline repl stream '
    'string_decoder sys timers tls trace_events tty url util v8 vm wasi '
    'worker_threads zlib').split())


def _worktree_paths(root: Path, *patterns: str) -> list[str]:
    listed = subprocess.run(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', *patterns],
        cwd=root, check=True, capture_output=True, text=True, timeout=120,
    ).stdout.split('\0')
    # An unstaged deletion stays listed but is absent from the worktree.
    return sorted(path for path in set(listed) if path and (root / path).is_file())


class _NodeVocabulary:
    """Text that names repository node_modules code."""

    def __init__(self, root: Path):
        # Everything ``npm ci`` installs, transitive packages (esbuild) included.
        manifest = json.loads((root / 'package.json').read_text(encoding='utf-8'))
        lock_path = root / 'package-lock.json'
        locked = (json.loads(lock_path.read_text(encoding='utf-8')).get('packages', {})
                  if lock_path.is_file() else {})
        self.packages = frozenset({
            *manifest.get('dependencies', {}), *manifest.get('devDependencies', {}),
            *(key.rsplit('node_modules/', 1)[-1] for key in locked if key),
        } - _NODE_BUILTINS)
        scripts = _worktree_paths(root, '*.mjs', '*.cjs', 'scripts/*.js', 'tests/*.js')
        self.entry_points = frozenset(
            Path(path).name for path in scripts if self._loads_package(root / path, set()))

    def _package(self, specifier: str) -> bool:
        if specifier.startswith(('.', '/', 'node:')):
            return False
        parts = specifier.split('/')
        name = '/'.join(parts[:2]) if specifier.startswith('@') else parts[0]
        return name in self.packages

    def _installed(self, text: str) -> bool:
        return any(name == '.bin' or name in self.packages
                   for name in _INSTALLED_PATH.findall(text))

    def _loads_package(self, path: Path, seen: set[Path]) -> bool:
        if path in seen or not path.is_file():
            return False
        seen.add(path)
        text = path.read_text(encoding='utf-8', errors='replace')
        if self._installed(text):
            return True
        for specifier in _JS_SPECIFIER.findall(text):
            if self._package(specifier):
                return True
            if specifier.startswith('.'):
                target = (path.parent / specifier).resolve()
                candidates = (target, *(target.with_name(target.name + suffix)
                                        for suffix in ('.js', '.mjs', '.cjs')),
                              target / 'index.js')
                if any(self._loads_package(candidate, seen) for candidate in candidates):
                    return True
        return False

    def names_node_modules(self, text: str) -> bool:
        return (text in ('npm', 'npx')
                or any(name in text for name in self.entry_points)
                or self._installed(text)
                or any(self._package(specifier) for specifier in _JS_SPECIFIER.findall(text)))


def _texts(node):
    """String constants plus the literal parts of ``a / 'b' / 'c'`` and
    ``os.path.join('a', 'b')`` path assemblies."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            yield inner.value
        elif isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div):
            parts, side = [], inner
            while isinstance(side, ast.BinOp) and isinstance(side.op, ast.Div):
                if isinstance(side.right, ast.Constant) and isinstance(side.right.value, str):
                    parts.append(side.right.value)
                side = side.left
            if isinstance(side, ast.Constant) and isinstance(side.value, str):
                parts.append(side.value)
            yield '/'.join(reversed(parts))
        elif isinstance(inner, ast.Call) and _called(inner.func) == 'join':
            yield '/'.join(argument.value for argument in inner.args
                           if isinstance(argument, ast.Constant)
                           and isinstance(argument.value, str))


def _replaces_process_launcher(call) -> bool:
    """``monkeypatch.setattr(module.subprocess, 'run', fake)`` and friends."""
    if _called(call.func) not in ('setattr', 'patch', 'object'):
        return False
    texts = [ast.unparse(argument) for argument in call.args[:2]]
    return (any(text.strip('\'"').rsplit('.', 1)[-1] in _PROCESS_CALLS
                and 'subprocess' in text for text in texts)
            or (len(texts) == 2 and texts[0].endswith('subprocess')
                and texts[1].strip('\'"') in _PROCESS_CALLS))


def _may_launch_node(call) -> bool:
    """False only for a launch that provably starts something else
    (``['git', ...]``, ``[sys.executable, ...]``)."""
    command = call.args[0] if call.args else next(
        (keyword.value for keyword in call.keywords if keyword.arg == 'args'), None)
    if isinstance(command, (ast.List, ast.Tuple)) and command.elts:
        command = command.elts[0]
    if isinstance(command, ast.Constant) and isinstance(command.value, str):
        program = command.value.split()[0] if command.value.split() else ''
        return bool(re.search(r'node|\bnp[mx]\b|\.[cm]?js$', program))
    return not (isinstance(command, ast.Attribute) and ast.unparse(command) == 'sys.executable')


def _launches_node(call) -> bool:
    # Tests import the process launchers by module; a bare ``run(...)`` is a
    # local helper.
    return (isinstance(call.func, ast.Attribute) and call.func.attr in _PROCESS_CALLS
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in ('subprocess', 'asyncio', 'sp')
            and _may_launch_node(call))


def _main_guard(statement) -> bool:
    """``if __name__ == '__main__':`` never runs when pytest imports a module."""
    return (isinstance(statement, ast.If) and isinstance(statement.test, ast.Compare)
            and ast.unparse(statement.test).replace('"', "'") == "__name__ == '__main__'")


def _is_guard(call) -> bool:
    name = _called(call.func)
    if name in _ARGUMENT_GUARDS:
        # Without module names the probe only proves that ``node`` exists.
        return bool(call.args)
    if name == 'frontend_module_guard':
        return any(keyword.arg == 'need_jsdom' and isinstance(keyword.value, ast.Constant)
                   and keyword.value.value for keyword in call.keywords)
    return name in _NODE_GUARDS


def _certain_expression_calls(node):
    """Calls an expression always evaluates, in evaluation order (arguments
    before the call): not a later short-circuit operand, a conditional
    expression branch, a lambda or a comprehension."""
    if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return
    if isinstance(node, ast.IfExp):
        yield from _certain_expression_calls(node.test)
        return
    if isinstance(node, ast.BoolOp):
        yield from _certain_expression_calls(node.values[0])
        return
    for child in ast.iter_child_nodes(node):
        yield from _certain_expression_calls(child)
    if isinstance(node, ast.Call):
        yield node


def _certain_calls(statements) -> list:
    """Calls that run whenever these statements run (memoizing early returns
    aside): branch and loop bodies, handlers and nested definitions are
    excluded, except the guard idiom ``if <check>: skip_or_fail(...)``."""
    calls = []
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(statement, ast.If):
            calls += _certain_expression_calls(statement.test)
            if not statement.orelse and all(
                    isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Call)
                    and _is_guard(inner.value) for inner in statement.body):
                calls += [inner.value for inner in statement.body]
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            calls += _certain_expression_calls(statement.iter)
        elif isinstance(statement, ast.While):
            calls += _certain_expression_calls(statement.test)
        elif isinstance(statement, ast.Match):
            calls += _certain_expression_calls(statement.subject)
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            calls += _certain_calls([*statement.body, *statement.finalbody])
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                calls += _certain_expression_calls(item.context_expr)
            calls += _certain_calls(statement.body)
        else:
            calls += _certain_expression_calls(statement)
    return calls


def _without_docstring(function):
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return [*body, *function.args.defaults,
            *(default for default in function.args.kw_defaults if default is not None)]


def _dependency_skips(nodes) -> list[str]:
    found = []
    for node in nodes:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _called(inner.func) in _SKIP_CALLS:
                source = ast.unparse(inner)
                if _DEPENDENCY_SKIP.search(source):
                    found.append(source if len(source) <= 100 else source[:97] + '...')
    return found


def _fixture_names(function) -> list[str]:
    names = []
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _called(target) == 'fixture':
            custom = [keyword.value.value for keyword in getattr(decorator, 'keywords', ())
                      if keyword.arg == 'name' and isinstance(keyword.value, ast.Constant)]
            names.append(custom[0] if custom else function.name)
    return names


def _autouse(function) -> bool:
    return any(isinstance(decorator, ast.Call) and _called(decorator.func) == 'fixture'
               and any(keyword.arg == 'autouse' and isinstance(keyword.value, ast.Constant)
                       and keyword.value.value for keyword in decorator.keywords)
               for decorator in function.decorator_list)


def _used_fixtures(nodes) -> set[str]:
    return {argument.value for node in nodes for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and _called(inner.func) == 'usefixtures'
            for argument in inner.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)}


class _NodeConsumerScan:
    """Interprocedural census of ``tests`` modules under ``root``."""

    def __init__(self, root: Path):
        self.root = root
        self.vocabulary = _NodeVocabulary(root)
        self.modules: dict[str, dict] = {}

    def _module_path(self, dotted: str, relative: str, level: int) -> str | None:
        if level:
            base = Path(relative).parents[level - 1]
            parts = [*base.parts, *(dotted.split('.') if dotted else [])]
        else:
            parts = dotted.split('.')
            if parts[0] != 'tests':
                parts = ['tests', *parts]  # rootdir-relative ``from _jsdom import``
        target = self.root.joinpath(*parts)
        for candidate in (target.with_name(target.name + '.py'), target / '__init__.py'):
            if candidate.is_file():
                return candidate.relative_to(self.root).as_posix()
        return None

    def module(self, relative: str) -> dict:
        if relative in self.modules:
            return self.modules[relative]
        tree = ast.parse((self.root / relative).read_text(encoding='utf-8'))
        functions, owners = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owners[id(child)] = node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, node)
        imports, module_aliases = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = self._module_path(node.module or '', relative, node.level)
                for alias in node.names:
                    name = alias.asname or alias.name
                    if target:
                        imports[name] = (target, alias.name)
                    submodule = self._module_path(
                        f'{node.module}.{alias.name}' if node.module else alias.name,
                        relative, node.level)
                    if submodule:
                        module_aliases[name] = submodule
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = self._module_path(alias.name, relative, 0)
                    if target and alias.asname:
                        module_aliases[alias.asname] = target
        bound: set[str] = set()
        module_skips: list[str] = []
        skip_aliases: dict[str, list[str]] = {}
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                targets = (statement.targets if isinstance(statement, ast.Assign)
                           else [statement.target])
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                aliased = _dependency_skips([statement.value]) + [
                    skip for inner in ast.walk(statement.value) if isinstance(inner, ast.Name)
                    for skip in skip_aliases.get(inner.id, ())]
                if 'pytestmark' in names:
                    module_skips += aliased
                elif aliased:
                    # ``needs_node = pytest.mark.skipif(...)`` applied as ``@needs_node``.
                    skip_aliases.update(dict.fromkeys(names, aliased))
                elif (any(self.vocabulary.names_node_modules(text) for text in _texts(statement.value))
                      or any(isinstance(inner, ast.Name) and inner.id in bound
                             for inner in ast.walk(statement.value))):
                    bound |= names
            elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef,
                                            ast.ClassDef, ast.Import, ast.ImportFrom)):
                module_skips += _dependency_skips([statement])
        fixtures = {name: function for function in functions.values()
                    for name in _fixture_names(function)}
        self.modules[relative] = {
            'functions': functions, 'owners': owners, 'imports': imports,
            'module_aliases': module_aliases, 'bound': bound, 'fixtures': fixtures,
            'autouse': [function.name for function in functions.values() if _autouse(function)],
            'import_time': [statement for statement in tree.body if not isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                            ast.Import, ast.ImportFrom)) and not _main_guard(statement)],
            'skips': module_skips, 'skip_aliases': skip_aliases,
            'pytestmark_fixtures': _used_fixtures(
                [statement for statement in tree.body if isinstance(statement, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'pytestmark'
                         for target in statement.targets)]),
        }
        return self.modules[relative]

    def _conftests(self, relative: str) -> list[str]:
        parts = Path(relative).parent.parts
        chain = [Path(*parts[:depth], 'conftest.py').as_posix() for depth in range(1, len(parts) + 1)]
        return [path for path in chain if (self.root / path).is_file()]

    def _fixture(self, relative: str, name: str):
        module = self.module(relative)
        if name in module['fixtures']:
            return relative, module['fixtures'][name].name
        if name in module['imports']:
            target, original = module['imports'][name]
            if original in self.module(target)['functions']:
                return target, original
        for conftest in reversed(self._conftests(relative)):
            if name in self.module(conftest)['fixtures']:
                return conftest, self.module(conftest)['fixtures'][name].name
        return None

    def _edges(self, relative: str, function) -> set[tuple[str, str]]:
        arguments = function.args
        parameters = {argument.arg for argument in
                      (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
        return self._call_edges(relative, [function],
                                {*parameters, *_used_fixtures(function.decorator_list)})

    def _call_edges(self, relative: str, nodes, fixture_names=(), *,
                    nested: bool = True) -> set[tuple[str, str]]:
        module = self.module(relative)
        edges = set()
        for node in (inner for root in nodes for inner in (ast.walk(root) if nested else [root])):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in module['functions']:
                    edges.add((relative, func.id))
                elif func.id in module['imports']:
                    target, original = module['imports'][func.id]
                    if original in self.module(target)['functions']:
                        edges.add((target, original))
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                owner = func.value.id
                if owner in ('self', 'cls') and func.attr in module['functions']:
                    edges.add((relative, func.attr))
                elif owner in module['module_aliases']:
                    target = module['module_aliases'][owner]
                    if func.attr in self.module(target)['functions']:
                        edges.add((target, func.attr))
        for name in fixture_names:
            fixture = self._fixture(relative, name)
            if fixture:
                edges.add(fixture)
        return {edge for edge in edges if edge[1] not in _NODE_GUARDS}

    def _facts(self, relative: str, name: str, cache: dict):
        key = (relative, name)
        if key not in cache:
            module = self.module(relative)
            function = module['functions'][name]
            body = _without_docstring(function)
            mentions = any(self.vocabulary.names_node_modules(text)
                           for node in body for text in _texts(node)) or any(
                isinstance(inner, ast.Name) and inner.id in module['bound']
                for node in body for inner in ast.walk(node))
            calls = [inner for node in body for inner in ast.walk(node)
                     if isinstance(inner, ast.Call)]
            executes = any(_launches_node(call) for call in calls)
            guarded = any(_is_guard(call) for call in _certain_calls(function.body))
            fakes = any(_replaces_process_launcher(call) for call in calls)
            skips = _dependency_skips([*body, *function.decorator_list]) + [
                skip for decorator in function.decorator_list for inner in ast.walk(decorator)
                if isinstance(inner, ast.Name) for skip in module['skip_aliases'].get(inner.id, ())]
            cache[key] = (mentions, executes, guarded, fakes, skips,
                          self._edges(relative, function))
        return cache[key]

    def _consumes(self, roots, cache, *, stop_at_guards: bool):
        """May-reachability: (runs node_modules, hand-written skips)."""
        mentions = executes = fakes = False
        skips, seen, pending = [], set(), list(roots)
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            m, e, guarded, f, s, edges = self._facts(*key, cache)
            skips += s
            if guarded and stop_at_guards:
                continue
            mentions, executes, fakes = mentions or m, executes or e, fakes or f
            pending += edges - seen
        # A test that replaces the process launcher never starts node.
        return mentions and executes and not fakes, skips

    def _closure(self, roots, cache, memo):
        """(runs node_modules, every such run is guarded, hand-written skips).

        Running node_modules is a may-property of every reachable call. A
        guard covers it only when it certainly runs first: fixtures, then the
        body's always-executed calls in order, so neither a guard behind a
        branch (a helper that bundles only some sections) nor one after the
        bundler call vouches for the test.
        """
        needs, skips = self._consumes(roots, cache, stop_at_guards=False)
        order = 'neutral'
        for root in roots:
            order = self._guard_order(root, cache, memo, ())
            if order != 'neutral':
                break
        return needs, order != 'exposed', skips

    def _guard_order(self, key, cache, memo, stack) -> str:
        """'guarded' or 'exposed' by whichever certainly happens first, else
        'exposed' if any path may run node_modules unguarded, else 'neutral'."""
        if key in memo:
            return memo[key]
        if key in stack:
            return 'neutral'
        relative, name = key
        function = self.module(relative)['functions'][name]
        mentions = self._facts(relative, name, cache)[0]
        arguments = function.args
        fixtures = [self._fixture(relative, fixture_name) for fixture_name in (
            *(a.arg for a in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)),
            *sorted(_used_fixtures(function.decorator_list)))]
        order = 'neutral'
        for fixture in fixtures:
            if fixture and fixture != key and fixture[1] not in _NODE_GUARDS:
                order = self._guard_order(fixture, cache, memo, (*stack, key))
                if order != 'neutral':
                    break
        if order == 'neutral':
            order = self._calls_guard_order(relative, _certain_calls(function.body), mentions,
                                            cache, memo, (*stack, key))
        if order == 'neutral' and self._consumes([key], cache, stop_at_guards=True)[0]:
            order = 'exposed'
        memo[key] = order
        return order

    def _calls_guard_order(self, relative, calls, mentions, cache, memo, stack) -> str:
        for call in calls:
            if _is_guard(call):
                return 'guarded'
            for target in self._call_edges(relative, [call], nested=False):
                order = self._guard_order(target, cache, memo, stack)
                if order != 'neutral':
                    return order
            if mentions and _launches_node(call):
                return 'exposed'
        return 'neutral'

    def findings(self, relative: str) -> list[str]:
        module = self.module(relative)
        cache: dict = {}
        shared = [(relative, name) for name in module['autouse']]
        for conftest in self._conftests(relative):
            shared += [(conftest, name) for name in self.module(conftest)['autouse']]
        for name in module['pytestmark_fixtures']:
            fixture = self._fixture(relative, name)
            if fixture:
                shared.append(fixture)
        # ``BUNDLE = native_module_path(...)`` guards every test at import.
        memo: dict = {}
        import_guarded = self._calls_guard_order(
            relative, _certain_calls(module['import_time']), False, cache, memo, ()) == 'guarded'
        problems = []
        for name, function in module['functions'].items():
            owner = module['owners'].get(id(function))
            if not name.startswith('test_') or (owner and not owner.name.startswith('Test')):
                continue
            roots = [(relative, name), *shared]
            class_nodes = []
            if owner is not None:
                class_nodes = [*owner.decorator_list, *(
                    statement for statement in owner.body if isinstance(statement, ast.Assign))]
                for fixture_name in _used_fixtures(class_nodes):
                    fixture = self._fixture(relative, fixture_name)
                    if fixture:
                        roots.append(fixture)
            needs, guarded, skips = self._closure(roots, cache, memo)
            if not needs:
                continue
            label = f'{relative}::{owner.name + "." if owner else ""}{name}'
            if not (guarded or import_guarded):
                problems.append(f'{label}: runs node_modules without a prerequisite helper '
                                '(require_node_modules / require_node_test_runner)')
            class_skips = _dependency_skips(class_nodes) + [
                skip for node in class_nodes for inner in ast.walk(node)
                if isinstance(inner, ast.Name) for skip in module['skip_aliases'].get(inner.id, ())]
            for skip in [*module['skips'], *class_skips, *skips]:
                problems.append(f'{label}: hand-written dependency skip {skip}')
        return problems

    def unguarded(self) -> list[str]:
        sources = {relative: (self.root / relative).read_text(encoding='utf-8')
                   for relative in _worktree_paths(self.root, 'tests/*.py')}
        helper_stems = {Path(relative).stem for relative, text in sources.items()
                        if not Path(relative).name.startswith('test_')
                        and self.vocabulary.names_node_modules(text)}
        problems = []
        for relative, text in sources.items():
            if not Path(relative).name.startswith('test_'):
                continue
            if self.vocabulary.names_node_modules(text) or any(
                    stem in text for stem in helper_stems):
                problems += self.findings(relative)
        return problems


def test_suites_running_node_modules_declare_the_prerequisite():
    """Fresh clones and worktrees have no ``node_modules`` until ``npm ci``.

    A suite that runs repository node_modules reaches
    ``require_node_modules(<modules>)`` (or ``require_node_test_runner``,
    ``_require_frontend_bundler``, ``run_harness``,
    ``frontend_module_guard(need_jsdom=True)``) in its call tree or at import,
    so a checkout without ``npm ci`` skips with the remedy and
    ``TOFU_REQUIRE_FRONTEND=1`` lanes fail instead of skipping.
    """
    problems = _NodeConsumerScan(ROOT).unguarded()
    assert not problems, '\n'.join(problems)


def test_node_module_census_flags_unguarded_and_hand_skipped_suites(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, timeout=60)
    (tmp_path / 'package.json').write_text('{"devDependencies": {"jsdom": "1"}}')
    tests = tmp_path / 'tests'
    tests.mkdir()
    (tests / '_harness.js').write_text("const { JSDOM } = require('jsdom');\n")
    (tests / '_helpers.py').write_text(
        'import subprocess\n'
        'from tests._jsdom import _require_frontend_bundler, require_node_modules\n\n'
        'def run_guarded():\n'
        "    require_node_modules('jsdom')\n"
        "    return subprocess.run(['node', '-e', \"require('jsdom')\"])\n\n"
        'def build_bundle():\n'
        '    _require_frontend_bundler()\n'
        "    return 'bundle.js'\n\n"
        'def maybe_bundle(name):\n'
        "    if name == 'special':\n"
        '        build_bundle()\n'
        '    return name\n\n'
        'def render_unguarded():\n'
        "    return subprocess.run(['node', '-e', \"require('jsdom')\"])\n")
    (tests / 'test_cases.py').write_text(
        'import shutil\nimport subprocess\n\nimport pytest\n\n'
        'from tests._helpers import maybe_bundle, render_unguarded, run_guarded\n'
        'from tests._jsdom import node_deps_available, require_node_modules, skip_or_fail\n\n'
        'def test_inline():\n'
        "    subprocess.run(['node', '-e', \"require('jsdom')\"])\n\n"
        'def test_entry_point():\n'
        "    subprocess.run(['node', 'tests/_harness.js'])\n\n"
        'def test_node_presence_only():\n'
        '    require_node_modules()\n'
        "    subprocess.run(['node', '-e', \"require('jsdom')\"])\n\n"
        "@pytest.mark.skipif(not shutil.which('node'), reason='node missing')\n"
        'def test_hand_skip():\n'
        '    run_guarded()\n\n'
        'def test_guarded_helper():\n'
        '    run_guarded()\n\n'
        'def test_fake_launcher(monkeypatch):\n'
        "    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: None)\n"
        "    subprocess.run(['node', '-e', \"require('jsdom')\"])\n\n"
        "@pytest.mark.skipif(not shutil.which('node'), reason='node missing')\n"
        'def test_builtins_only():\n'
        "    subprocess.run(['node', '-e', \"require('fs')\"])\n\n"
        'def test_guard_after_the_run():\n'
        '    render_unguarded()\n'
        '    run_guarded()\n\n'
        'def test_guard_behind_a_branch():\n'
        "    maybe_bundle('upload')\n"
        "    subprocess.run(['node', '-e', \"require('jsdom')\"])\n\n"
        'def test_skip_or_fail_idiom():\n'
        '    if not node_deps_available():\n'
        "        skip_or_fail('jsdom missing')\n"
        "    subprocess.run(['node', '-e', \"require('jsdom')\"])\n\n"
        'def test_other_program(tmp_path):\n'
        "    (tmp_path / 'x.js').write_text(\"require('jsdom')\")\n"
        "    subprocess.run(['git', 'init', str(tmp_path)])\n")
    (tests / 'test_import_time.py').write_text(
        'import subprocess\n\nfrom tests._helpers import build_bundle\n\n'
        'BUNDLE = build_bundle()\n\n'
        'def test_bundle():\n'
        "    subprocess.run(['node', '-e', \"require('jsdom')\", BUNDLE])\n")
    (tests / 'test_conditional_import.py').write_text(
        'import os\nimport subprocess\n\nfrom tests._helpers import build_bundle\n\n'
        "if os.environ.get('BUNDLE'):\n"
        '    build_bundle()\n\n'
        'def test_after_a_conditional_import():\n'
        "    subprocess.run(['node', '-e', \"require('jsdom')\"])\n")

    unguarded = ': runs node_modules without a prerequisite helper'
    assert sorted(problem.split(' (')[0] for problem in _NodeConsumerScan(tmp_path).unguarded()) == [
        'tests/test_cases.py::test_entry_point' + unguarded,
        'tests/test_cases.py::test_guard_after_the_run' + unguarded,
        'tests/test_cases.py::test_guard_behind_a_branch' + unguarded,
        "tests/test_cases.py::test_hand_skip: hand-written dependency skip "
        "pytest.mark.skipif(not shutil.which('node'), reason='node missing')",
        'tests/test_cases.py::test_inline' + unguarded,
        'tests/test_cases.py::test_node_presence_only' + unguarded,
        'tests/test_conditional_import.py::test_after_a_conditional_import' + unguarded,
    ]
