"""Architecture ratchets for provider-stream terminal semantics."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOTS = (_ROOT / 'lib', _ROOT / 'routes')
_STREAM_CALLS = {
    '_dispatch_stream',
    'async_dispatch_stream',
    'async_stream_chat',
    'dispatch_stream',
    'stream_chat',
    'stream_llm_response',
}
_SEMANTIC_ADAPTERS = {
    'ensure_provider_stream_result',
    'require_verified_provider_stream_result',
}


def _production_python_paths():
    """Yield source files without descending into caches or retained trash.

    ``Path.rglob`` is intentionally avoided here: this repository can retain
    very large hidden recovery trees, and an architecture ratchet must have a
    bounded resource footprint of its own.
    """
    for root in _PRODUCTION_ROOTS:
        for directory, child_directories, filenames in os.walk(root):
            child_directories[:] = sorted(
                name
                for name in child_directories
                if not name.startswith('.') and name != '__pycache__'
            )
            directory_path = Path(directory)
            for filename in sorted(filenames):
                if filename.endswith('.py'):
                    yield directory_path / filename


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _is_semantically_adapted(
    node: ast.Call,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    current: ast.AST = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Call) and _call_name(current) in _SEMANTIC_ADAPTERS:
            return True
        if isinstance(current, (ast.Assign, ast.AnnAssign, ast.Return, ast.Expr)):
            return False
    return False


def test_production_stream_calls_cross_the_typed_adapter_boundary():
    """No production caller may consume a raw stream tuple directly."""
    violations = []
    for path in _production_python_paths():
        source = path.read_text(encoding='utf-8')
        if not any(call_name in source for call_name in _STREAM_CALLS):
            continue
        tree = ast.parse(source, filename=str(path))
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in _STREAM_CALLS:
                continue
            # Definitions of the low-level typed shell call their private
            # one-attempt implementation; every public/consumer call in
            # this vocabulary must visibly cross an adapter.
            if not _is_semantically_adapted(node, parents):
                violations.append(
                    f'{path.relative_to(_ROOT)}:{node.lineno} '
                    f'{_call_name(node)}')
    assert not violations, (
        'provider stream calls bypass typed semantic adaptation:\n'
        + '\n'.join(violations))


def test_terminal_http_exporters_never_default_missing_finish_to_stop():
    """Missing terminal evidence must use an error channel, never fake stop."""
    paths = [
        _ROOT / 'routes/api_v1/chat.py',
        _ROOT / 'routes/api_v1/chat_direct.py',
        _ROOT / 'lib/compat/openai.py',
        _ROOT / 'lib/compat/anthropic.py',
    ]
    violations = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
                continue
            has_stop = any(
                isinstance(value, ast.Constant) and value.value == 'stop'
                for value in node.values)
            names = {
                child.id.lower()
                for child in ast.walk(node)
                if isinstance(child, ast.Name)
            }
            attrs = {
                child.attr.lower()
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute)
            }
            if has_stop and any('finish' in name for name in names | attrs):
                violations.append(f'{path.relative_to(_ROOT)}:{node.lineno}')
    assert not violations, (
        'terminal exporter invents finish_reason=stop:\n'
        + '\n'.join(violations))


def test_terminal_evidence_is_constructed_only_at_semantic_authority():
    """Wire consumers must use named adapters, not rebuild verdict inputs."""
    violations = []
    authority_path = _ROOT / 'lib/turn_verdict.py'
    for path in _production_python_paths():
        if path == authority_path:
            continue
        source = path.read_text(encoding='utf-8')
        if 'TurnTerminalEvidence' not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and _call_name(node) == 'TurnTerminalEvidence'):
                violations.append(
                    f'{path.relative_to(_ROOT)}:{node.lineno}')
    assert not violations, (
        'terminal evidence constructed outside lib.turn_verdict:\n'
        + '\n'.join(violations))
