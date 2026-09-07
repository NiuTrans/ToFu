"""tests/test_agent_core_boundary.py — Core/plugin boundary enforcement.

The reusable agent base (orchestrator, llm, llm_dispatch, swarm scheduling,
compaction, push, task_runtime — see ``lib/agent_core_manifest.py``) must
NEVER import a concrete plugin (an individual tool family or provider dialect).
It may only reach plugins through the registry seams (``lib.tools.registry``,
``lib.llm_dispatch.provider_registry``).

This is the long-term guarantee that the base stays a clean foundation: a
directory layout can't enforce it, but this AST test does.  It generalizes
``tests/test_core_tool_isolation.py`` (which guards the single file
``model_config.py``) to the whole declared core surface.

If this test fails
------------------
A core module grew a hard import of a swappable plugin.  Don't add the import —
register the tool via ``ToolSpec`` (``lib/tools/registry/``) or the provider
dialect via ``BodyDialect`` (``lib/llm_dispatch/provider_registry.py``) and let
the registry seam wire it in.  That's how the base reaches plugins without
depending on them.
"""

from __future__ import annotations

import ast
import importlib.util
import os

import pytest

from lib.agent_core_manifest import (
    CORE_MODULES,
    is_concrete_plugin_import,
    is_forbidden_persistence_import,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, '..'))


def _module_to_paths(dotted: str) -> list[str]:
    """Resolve a dotted module prefix to the .py file(s) it covers.

    A leaf module (``lib.llm.body``) → its single file.
    A package (``lib.llm``) → every ``.py`` under that package dir.
    """
    rel = dotted.replace('.', os.sep)
    pkg_dir = os.path.join(REPO, rel)
    single = pkg_dir + '.py'
    paths: list[str] = []
    if os.path.isdir(pkg_dir):
        for root, _dirs, files in os.walk(pkg_dir):
            for f in files:
                if f.endswith('.py'):
                    paths.append(os.path.join(root, f))
    elif os.path.isfile(single):
        paths.append(single)
    return paths


def _path_to_dotted(path: str) -> str:
    rel = os.path.relpath(path, REPO)
    return rel[:-3].replace(os.sep, '.') if rel.endswith('.py') else rel


def _imports_of(path: str) -> list[tuple[str, int]]:
    """Return (imported_module_dotted, lineno) for every import in *path*.

    ``from a.b import c`` yields ``a.b`` AND ``a.b.c`` (the latter covers
    ``from lib.tools import search`` where ``search`` is a submodule).
    """
    with open(path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — resolve against the file's package
                continue
            mod = node.module or ''
            out.append((mod, node.lineno))
            for alias in node.names:
                out.append((f'{mod}.{alias.name}', node.lineno))
    return out


def _collect_core_files() -> list[str]:
    seen: set[str] = set()
    for prefix in CORE_MODULES:
        for p in _module_to_paths(prefix):
            seen.add(os.path.normpath(p))
    return sorted(seen)


def test_core_modules_resolve_to_files():
    """Sanity: every CORE_MODULES prefix maps to at least one real file."""
    missing = [p for p in CORE_MODULES if not _module_to_paths(p)]
    assert not missing, (
        'agent_core_manifest.CORE_MODULES references non-existent modules: '
        + ', '.join(missing))


def test_core_does_not_import_concrete_plugins():
    """No core module may import a concrete plugin tool/provider module."""
    violations: list[str] = []
    for path in _collect_core_files():
        dotted_self = _path_to_dotted(path)
        for imported, lineno in _imports_of(path):
            if is_concrete_plugin_import(imported):
                violations.append(
                    f'{dotted_self}:{lineno} imports concrete plugin '
                    f'{imported!r}')
    if violations:
        pytest.fail(
            'Core/plugin boundary violated — core must reach plugins ONLY via '
            'the registry seams (lib.tools.registry / '
            'lib.llm_dispatch.provider_registry):\n  '
            + '\n  '.join(violations)
            + '\n\nFix: register a ToolSpec / BodyDialect instead of importing '
            'the concrete plugin module in core.')


def test_core_does_not_import_host_persistence():
    """No core file may import a host persistence implementation.

    The agent base reaches all persistence through the ConversationStore seam
    (``lib.agent_core.store.get_conversation_store``). Fix a failure by routing
    the operation through that protocol, not by adding an exception.
    """
    violations: list[str] = []
    for path in _collect_core_files():
        dotted_self = _path_to_dotted(path)
        for imported, lineno in _imports_of(path):
            if is_forbidden_persistence_import(imported):
                violations.append(
                    f'{dotted_self}:{lineno} imports host persistence '
                    f'{imported!r}')

    if violations:
        pytest.fail(
            'Core/persistence boundary violated — route persistence through '
            'lib.agent_core.store.get_conversation_store():\n  '
            + '\n  '.join(violations))


def test_agent_core_root_is_a_namespace_not_a_runtime_router():
    """Core symbols must have one searchable, concrete module owner."""
    import lib.agent_core as ac

    assert ac.__all__ == ()
    assert '__getattr__' not in vars(ac)
    assert 'CORE_MEMBERS' not in vars(ac)


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
