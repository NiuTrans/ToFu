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
register the tool via ``ToolSpec`` (``lib/tools/registry.py``) or the provider
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
)

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


def test_facade_members_are_within_core():
    """Every lib.agent_core re-export must come from a CORE_MODULES module.

    Keeps the human-readable facade (lib/agent_core/__init__.py) honest: it may
    only surface symbols that the machine-readable manifest agrees are core.
    """
    from lib.agent_core import CORE_MEMBERS
    from lib.agent_core_manifest import is_core_module

    offenders = {
        sym: mod for sym, mod in CORE_MEMBERS.items()
        if not is_core_module(mod)
    }
    assert not offenders, (
        'lib/agent_core re-exports symbols whose defining module is NOT in '
        'agent_core_manifest.CORE_MODULES:\n  '
        + '\n  '.join(f'{s} ← {m}' for s, m in sorted(offenders.items()))
        + '\n\nFix: either add the module to CORE_MODULES or drop the symbol '
        'from the facade.')


def test_facade_reexports_resolve():
    """Every facade __all__ symbol must actually be importable."""
    import lib.agent_core as ac
    missing = [s for s in ac.__all__ if not hasattr(ac, s)]
    assert not missing, f'lib.agent_core declares but does not export: {missing}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
