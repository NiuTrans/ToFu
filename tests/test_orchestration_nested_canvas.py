"""Nested orchestration contracts after the Studio moved to typed owners."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration._role_axes import resolve_scope
from lib.orchestration._subflow_expansion import expand_subflows
from lib.orchestration._validate import validate_definition
from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
STYLES_CSS = ROOT / 'static/styles.css'


def _graph(name: str, middle: dict) -> dict:
    return {
        'schema': 'tofu.orchestration/v1',
        'name': name,
        'nodes': [
            {'id': f'{name}-start', 'type': 'control', 'kind': 'start',
             'pos': {'x': 0, 'y': 0}, 'params': {}},
            middle,
            {'id': f'{name}-stop', 'type': 'control', 'kind': 'stop',
             'pos': {'x': 0, 'y': 200}, 'params': {}},
        ],
        'edges': [
            {'from': f'{name}-start', 'to': middle['id']},
            {'from': middle['id'], 'to': f'{name}-stop'},
        ],
    }


def _nested_definition() -> dict:
    inner = _graph('inner', {
        'id': 'writer', 'type': 'role', 'role': 'writer',
        'pos': {'x': 0, 'y': 100}, 'params': {'objective': 'Write'},
    })
    child = {
        'schema': 'tofu.orchestration/v1', 'name': 'child',
        'nodes': [
            {'id': 'child-start', 'type': 'control', 'kind': 'start',
             'pos': {'x': 0, 'y': 0}, 'params': {}},
            {'id': 'coder', 'type': 'role', 'role': 'coder',
             'pos': {'x': 0, 'y': 80}, 'params': {'objective': 'Code'}},
            {'id': 'inner-group', 'type': 'subflow', 'name': 'Inner',
             'pos': {'x': 0, 'y': 150},
             'params': {'scope': 'isolated', 'definition': inner}},
            {'id': 'child-stop', 'type': 'control', 'kind': 'stop',
             'pos': {'x': 0, 'y': 240}, 'params': {}},
        ],
        'edges': [
            {'from': 'child-start', 'to': 'coder'},
            {'from': 'coder', 'to': 'inner-group'},
            {'from': 'inner-group', 'to': 'child-stop'},
        ],
    }
    return _graph('root', {
        'id': 'group', 'type': 'subflow', 'name': 'Group',
        'pos': {'x': 0, 'y': 100},
        'params': {'scope': 'isolated', 'definition': child},
    })


def _top_level_rule_heads() -> list[str]:
    heads: list[str] = []
    depth = 0
    current = ''
    for char in STYLES_CSS.read_text(encoding='utf-8'):
        if char == '{':
            if depth == 0:
                heads.append(current.strip())
                current = ''
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                current = ''
        elif depth == 0:
            current += char
    return heads


def test_no_duplicate_base_css_selectors():
    simple = [
        head for head in _top_level_rule_heads()
        if re.fullmatch(r'\.orch-[a-z0-9-]+', head)
    ]
    duplicates = {key: count for key, count in Counter(simple).items() if count > 1}
    assert not duplicates


def test_nested_definition_validates_at_every_depth():
    root = _nested_definition()
    child = root['nodes'][1]['params']['definition']
    inner = child['nodes'][2]['params']['definition']
    for definition in (root, child, inner):
        verdict = validate_definition(definition)
        assert verdict['ok'], verdict['errors']
    assert any(node.get('role') == 'coder' for node in child['nodes'])
    assert any(node.get('role') == 'writer' for node in inner['nodes'])


def test_isolated_group_survives_expansion_as_a_black_box():
    root = _nested_definition()
    group = root['nodes'][1]
    assert resolve_scope(group) == 'isolated'
    expanded = expand_subflows(root)
    assert any(node.get('id') == group['id'] for node in expanded['nodes'])


def test_structured_fields_validate_and_render_into_the_engine_brief():
    definition = _graph('structured', {
        'id': 'worker', 'type': 'role', 'role': 'worker',
        'pos': {'x': 0, 'y': 100},
        'params': {
            'objective': 'Build the widget.',
            'must_do': ['ship it', 'write tests'],
        },
    })
    verdict = validate_definition(definition)
    assert verdict['ok'], verdict['errors']
    brief = render_role_brief(definition['nodes'][1])
    assert brief.startswith('Build the widget.')
    assert '### Must Do' in brief
    assert '- ship it' in brief and '- write tests' in brief
    assert 'Must Not Do' not in brief


def test_typed_io_definition_serializes_with_backend_shape():
    definition = {
        'schema': 'tofu.orchestration/v1', 'name': 'typed-io',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start',
             'pos': {'x': 0, 'y': 0}, 'params': {}},
            {'id': 'worker', 'type': 'role', 'role': 'worker',
             'pos': {'x': 0, 'y': 100}, 'params': {'objective': 'Work', 'io': {
                 'outputs': [{'name': 'summary', 'type': 'text'},
                             {'name': 'changes', 'type': 'artifact'}],
             }}},
            {'id': 'writer', 'type': 'role', 'role': 'writer',
             'pos': {'x': 0, 'y': 200}, 'params': {'objective': 'Write', 'io': {
                 'inputs': [{'name': 'changes', 'type': 'artifact',
                             'from': 'worker.changes'}],
             }}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop',
             'pos': {'x': 0, 'y': 300}, 'params': {}},
        ],
        'edges': [
            {'from': 'start', 'to': 'worker'},
            {'from': 'worker', 'to': 'writer'},
            {'from': 'writer', 'to': 'stop'},
        ],
    }
    verdict = validate_definition(definition)
    assert verdict['ok'], verdict['errors']
    outputs = definition['nodes'][1]['params']['io']['outputs']
    assert [item['name'] for item in outputs] == ['summary', 'changes']
    assert definition['nodes'][2]['params']['io']['inputs'][0] == {
        'name': 'changes', 'type': 'artifact', 'from': 'worker.changes',
    }


def test_typed_navigation_and_graph_owners_are_in_the_vite_graph():
    studio = (ROOT / 'frontend/src/features/orchestration-studio-view-owners.ts').read_text(
        encoding='utf-8')
    assert "import './orchestration/graph'" in studio
    assert "import './orchestration/editor-controller-hub'" in studio
    hub = (ROOT / 'frontend/src/features/orchestration/editor-controller-hub.ts').read_text(
        encoding='utf-8')
    assert 'createOrchestrationNavigationController' in hub
    graph = (ROOT / 'frontend/src/features/orchestration/graph.ts').read_text(
        encoding='utf-8')
    assert 'createOrchestrationGraphTools' in graph
    navigation = runtime_section('orchestration-navigation.js', scope_prelude=False)
    assert 'function createOrchestrationNavigationController' in navigation
    assert not (ROOT / 'lib/js_bundler.py').exists()
