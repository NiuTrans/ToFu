"""Checklist visibility uses real gateway serialization, never invented childResults.

Only child execution and final event delivery are stubbed. ToolScript execution,
receipt construction, canonical composition and the checklist consumer stay real.
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from lib.tasks_pkg.context_composer.checklist import (
    CHECKLIST_HEADING,
    restore_checklist_context,
)
from lib.tasks_pkg.handlers import tool_gateway as gateway
from lib.tools import todo
from lib.tools.gateway import EXECUTE_TOOLS_NAME
from lib.tools.result_value import CanonicalReceipt, capture_result

pytestmark = pytest.mark.unit


@pytest.fixture
def invoke_gateway(monkeypatch):
    task = {'model': 'test-model', '_userId': 7}
    todo.apply_todo_write_to_task(task, {'todos': [
        {'content': 'Inspect gateway evidence', 'status': 'in_progress'},
        {'content': 'Verify restoration', 'status': 'pending'},
    ]})
    task['_executable_tool_catalog'] = [
        {'type': 'function', 'function': {
            'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': {}},
        }} for name in ('todo_write', 'todo_list', 'foreign_tool')
    ]
    faults = {}
    executed = []

    def execute_children(task, calls, execution, **kwargs):
        receipts = []
        for call in calls:
            name = call['function']['name']
            _, output, _ = todo.apply_todo_write_to_task(task, {'command': 'load'})
            if faults.get('output'):
                output = faults['output'](output)
            delivery = CanonicalReceipt({
                'content': output, 'truncated': faults.get('truncated', False),
                'resultId': f'result-{len(executed)}',
            }, canonical=capture_result(output))
            if faults.get('retention_error'):
                delivery['retentionError'] = 'unavailable'
            receipt = gateway._receipt_from_round(
                call, {'status': faults.get('status', 'done')}, output,
                time.monotonic(), delivery)
            receipts.append(receipt)
            executed.append((name, receipt))
        return receipts

    monkeypatch.setattr(gateway, '_execute_normalized', execute_children)
    monkeypatch.setattr(gateway, '_finalize', lambda *a, **k: None)

    def invoke(arguments, **overrides):
        faults.update(overrides)
        call_id, content, aborted = gateway.handle_execute_tools(
            task, {}, EXECUTE_TOOLS_NAME, 'gateway', arguments,
            1, {'llmRound': 1}, {}, None, False)
        assert call_id == 'gateway'
        assert not aborted
        payload = json.loads(content)
        messages = [
            {'role': 'assistant', 'tool_calls': [{'id': call_id, 'function': {
                'name': EXECUTE_TOOLS_NAME, 'arguments': json.dumps(arguments)}}]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': str(content)},
        ]
        return task, messages, payload, content, executed

    return invoke


def _assert_visibility(task, messages, *, visible):
    before = copy.deepcopy(messages)
    restore_checklist_context(messages, task=task)
    if visible:
        assert messages == before
    else:
        assert messages[:2] == before
        content = messages[-1]['content']
        text = content if isinstance(content, str) else '\n'.join(
            part.get('text', '') for part in content)
        assert CHECKLIST_HEADING in text
        assert todo.render_todo_list(task['_todos']) in text


@pytest.mark.parametrize('name', ['todo_write', 'todo_list'])
@pytest.mark.parametrize('mode', ['calls', 'program'])
def test_real_gateway_complete_receipt_is_visible(invoke_gateway, name, mode):
    args = ({'calls': [{'name': name, 'arguments': {}}]} if mode == 'calls' else
            {'program': f'return tools.call("{name}", {{}});'})
    task, messages, payload, content, executed = invoke_gateway(args)
    assert payload['status'] == 'ok'
    assert len(content.canonical.children) == 1
    _assert_visibility(task, messages, visible=True)
    if mode == 'program':
        child = payload['program']['childResults'][0]
        assert child['output'] == executed[0][1]['output']
        assert child['status'] == 'done'
        assert child['output_truncated'] is False


@pytest.mark.parametrize('expression', [
    'receipt', '[receipt]', '{nested: [receipt]}',
])
def test_structured_program_receipt_preserves_visibility(invoke_gateway, expression):
    task, messages, _, _, _ = invoke_gateway({'program': (
        'const receipt = await tools.call("todo_write", {}); '
        f'return {expression};')})
    _assert_visibility(task, messages, visible=True)


def test_program_batch_receipts_preserve_visibility(invoke_gateway):
    task, messages, payload, _, _ = invoke_gateway({'program': (
        'return tools.callMany([{name: "todo_write", arguments: {}}, '
        '{name: "foreign_tool", arguments: {}}]);')})
    _assert_visibility(task, messages, visible=True)
    assert 'output' in payload['program']['childResults'][0]
    assert 'output' not in payload['program']['childResults'][1]


@pytest.mark.parametrize('expression', [
    'null', '"updated"', '{status: receipt.status}', 'receipt.output',
    'JSON.stringify(receipt)',
    'JSON.parse(JSON.stringify(receipt))',
    '{name: "todo_write", status: "done", output: receipt.output}',
])
def test_omitted_reduced_or_copied_return_restores(invoke_gateway, expression):
    task, messages, payload, content, _ = invoke_gateway({'program': (
        'const receipt = await tools.call("todo_write", {}); '
        f'return {expression};')})
    assert payload['status'] == 'ok'
    assert 'output' not in payload['program']['childResults'][0]
    # Retaining canonical evidence privately does not prove model visibility.
    assert len(content.canonical.children) == 1
    _assert_visibility(task, messages, visible=False)


def test_foreign_complete_output_cannot_become_todo_witness(invoke_gateway):
    task, messages, payload, _, _ = invoke_gateway({'program': (
        'const hidden = await tools.call("todo_write", {}); '
        'return tools.call("foreign_tool", {});')})
    assert payload['program']['result']['status'] == 'done'
    assert all('output' not in row for row in payload['program']['childResults'])
    _assert_visibility(task, messages, visible=False)


@pytest.mark.parametrize('mode', ['calls', 'program'])
@pytest.mark.parametrize('fault', ['truncated', 'status', 'foreign_snapshot', 'reduced'])
def test_incomplete_receipts_still_restore(invoke_gateway, mode, fault):
    overrides = {
        'truncated': {'truncated': True},
        'status': {'status': 'error'},
        'foreign_snapshot': {'output': lambda text: text.replace(
            'Inspect gateway evidence', 'Unrelated checklist item')},
        'reduced': {'output': lambda text: text.split('\n', 1)[0]},
    }[fault]
    args = ({'calls': [{'name': 'todo_write', 'arguments': {}}]} if mode == 'calls' else
            {'program': 'return tools.call("todo_write", {});'})
    task, messages, _, _, _ = invoke_gateway(args, **overrides)
    _assert_visibility(task, messages, visible=False)


def test_program_retention_failure_does_not_publish_witness(invoke_gateway):
    task, messages, payload, _, _ = invoke_gateway(
        {'program': 'return tools.call("todo_write", {});'}, retention_error=True)
    assert 'output' not in payload['program']['childResults'][0]
    _assert_visibility(task, messages, visible=False)


def test_program_error_after_child_does_not_publish_witness(invoke_gateway):
    task, messages, payload, _, _ = invoke_gateway({'program': (
        'const receipt = await tools.call("todo_write", {}); return missing;')})
    assert payload['program']['status'] == 'error'
    assert 'output' not in payload['program']['childResults'][0]
    _assert_visibility(task, messages, visible=False)


def test_program_output_limit_does_not_publish_witness(invoke_gateway, monkeypatch):
    import lib.tools.javascript as javascript
    monkeypatch.setattr(javascript, 'MAX_OUTPUT_BYTES', 32)
    task, messages, payload, _, _ = invoke_gateway(
        {'program': 'return tools.call("todo_write", {});'})
    assert payload['program']['error']['code'] == 'output_limit'
    assert 'output' not in payload['program']['childResults'][0]
    _assert_visibility(task, messages, visible=False)


@pytest.mark.parametrize('damage', ['omitted', 'reduced', 'truncated', 'foreign'])
def test_only_surviving_canonical_witness_suppresses_restore(invoke_gateway, damage):
    task, messages, payload, _, _ = invoke_gateway(
        {'program': 'return tools.call("todo_write", {});'})
    witness = payload['program']['childResults'][0]
    if damage == 'omitted':
        payload['program']['childResults'] = []
    elif damage == 'reduced':
        witness['output'] = witness['output'].split('\n', 1)[0]
    elif damage == 'truncated':
        witness['output_truncated'] = True
    else:
        witness['name'] = 'foreign_tool'
    # Even a complete original program.result cannot replace the trusted row.
    assert payload['program']['result']['output']
    messages[-1]['content'] = json.dumps(payload)
    _assert_visibility(task, messages, visible=False)


def test_witness_duplication_is_bounded(invoke_gateway, monkeypatch):
    monkeypatch.setattr(gateway, 'MAX_OUTPUT_BYTES', 32)
    task, messages, payload, _, _ = invoke_gateway(
        {'program': 'return tools.call("todo_write", {});'})
    assert payload['program']['status'] == 'ok'
    assert 'output' not in payload['program']['childResults'][0]
    _assert_visibility(task, messages, visible=False)
