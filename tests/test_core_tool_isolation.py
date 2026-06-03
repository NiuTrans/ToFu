"""tests/test_core_tool_isolation.py — Core/plugin separation guard.

Goal
----
Tool assembly is now declarative: tools (built-in or third-party) register
:class:`~lib.tools.registry.ToolSpec` objects, and
``lib/tasks_pkg/model_config.py::_assemble_tool_list`` simply iterates the
registry.  The whole point is that **adding or removing a tool needs ZERO
edits to core orchestration code**.

This test fails the moment ``model_config.py`` regrows the old hand-maintained
``if feature: tool_list.append(SOME_TOOL)`` ladder — i.e. the moment a future
change starts hardcoding concrete tools back into core again.

If you are adding a tool: define a ``ToolSpec`` in ``lib/tools/registry.py``
(or ship one from a plugin via the ``tofu.tools`` entry point).  Do NOT add
a branch here.
"""

from __future__ import annotations

import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_CONFIG = os.path.normpath(
    os.path.join(HERE, '..', 'lib', 'tasks_pkg', 'model_config.py'))

# Concrete tool-schema symbols that used to be imported into model_config.
# These now live behind the registry; core must NOT import them again.
_FORBIDDEN_IMPORT_SYMBOLS = {
    'SEARCH_TOOL_MULTI', 'FETCH_URL_TOOL',
    'PROJECT_TOOLS', 'READ_FILES_TOOL', 'CODE_EXEC_TOOL',
    'BROWSER_TOOLS', 'ADVANCED_BROWSER_TOOLS', 'DESKTOP_TOOLS',
    'GENERATE_IMAGE_TOOL', 'CONV_REF_TOOLS', 'ASK_HUMAN_TOOL',
    'ALL_MEMORY_TOOLS', 'SCHEDULER_TOOLS',
    'SPAWN_AGENTS_TOOL', 'AWAIT_AGENTS_TOOL', 'GET_AGENT_RESULT_TOOL',
}


def _read_source() -> str:
    with open(MODEL_CONFIG, 'r', encoding='utf-8') as f:
        return f.read()


def test_model_config_does_not_import_concrete_tool_schemas():
    """Core tool-assembly file must not import concrete tool-schema symbols.

    It should depend ONLY on the registry seam (ToolContext / assemble_tool_list).
    """
    tree = ast.parse(_read_source(), filename=MODEL_CONFIG)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORT_SYMBOLS:
                    offenders.append(f'{node.module}.{alias.name}')
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORT_SYMBOLS:
                    offenders.append(alias.name)
    if offenders:
        pytest.fail(
            'model_config.py re-imported concrete tool schemas — these belong '
            'behind ToolSpec in lib/tools/registry.py, not in core:\n  '
            + '\n  '.join(sorted(set(offenders)))
            + '\n\nFix: register a ToolSpec instead of importing the schema here.'
        )


def test_model_config_has_no_tool_list_ladder():
    """The per-feature ``tool_list.append/extend`` ladder must not return.

    The only list-building of tools allowed in this file is the
    caller-supplied-override validation loop (``ok.append(t)``).  Any
    ``tool_list.append`` / ``tool_list.extend`` indicates the if-ladder is
    growing back.
    """
    src = _read_source()
    assert 'tool_list.append' not in src and 'tool_list.extend' not in src, (
        'model_config.py is rebuilding a tool if-ladder (tool_list.append/extend). '
        'Tool gating now lives in lib/tools/registry.py ToolSpec.build(). '
        'Add a ToolSpec there instead.'
    )


def test_assembly_delegates_to_registry():
    """Positive assertion: the file routes through the registry seam."""
    src = _read_source()
    assert 'assemble_tool_list(ctx)' in src, (
        'model_config.py no longer delegates to the registry assemble_tool_list. '
        'Tool assembly must go through lib/tools/registry.py.'
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
