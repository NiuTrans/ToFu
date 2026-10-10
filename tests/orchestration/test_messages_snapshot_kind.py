"""One request-evidence builder and explicit state mirrors share a frozen schema."""
import ast
import os
from copy import deepcopy
from pathlib import Path
import pytest
from tests.tasks_pkg.test_lib_orchestrator_messages_snapshot_wire_parity import emit_snapshot

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SITES = {'lib/tasks_pkg/round_evidence.py',
    'lib/tasks_pkg/orchestrator/_finalize.py', 'lib/tasks_pkg/orchestrator/_post_loop.py'}
PARAMS = {'maxTokens', 'temperature', 'thinkingEnabled', 'thinkingDepth', 'preset', 'responseFormat', 'stream'}


def authored_sources():
    for directory, children, files in os.walk(ROOT / 'lib'):
        children[:] = [name for name in children
                       if not name.startswith('.') and name not in {'__pycache__', 'node_modules'}]
        for name in files:
            if name.endswith('.py'):
                yield Path(directory) / name


def violations(event):
    errors = []
    if event.get('kind') not in {'request', 'state'}:
        errors.append('missing kind')
    if event.get('kind') == 'request' and set(event.get('params') or {}) != PARAMS:
        errors.append('params schema drifted')
    return errors


def test_snapshot_sites_all_stamped_with_kind():
    found = set()
    for path in authored_sources():
        source = path.read_text()
        if 'MESSAGES_SNAPSHOT' not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'build_event' and node.args
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == 'MESSAGES_SNAPSHOT'):
                continue
            relative = str(path.relative_to(ROOT))
            found.add(relative)
            kind = next((kw.value for kw in node.keywords if kw.arg == 'kind'), None)
            if relative == 'lib/tasks_pkg/round_evidence.py':
                assert isinstance(kind, ast.Name) and kind.id == 'kind'
            else:
                assert isinstance(kind, ast.Constant) and kind.value == 'state'
    assert found == EXPECTED_SITES


def test_neuter_guard_bites_when_kind_dropped(monkeypatch):
    event, = emit_snapshot(monkeypatch)
    assert not violations(event)
    poisoned = deepcopy(event)
    poisoned.pop('kind')
    assert 'missing kind' in violations(poisoned)


def test_neuter_guard_bites_when_params_dropped(monkeypatch):
    event, = emit_snapshot(monkeypatch)
    assert not violations(event)
    poisoned = deepcopy(event)
    poisoned['params'].pop('maxTokens')
    assert 'params schema drifted' in violations(poisoned)
