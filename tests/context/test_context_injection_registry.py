"""Automatic context has one closed policy/writer and bounded debug contract."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, append_context_blocks
from lib.tasks_pkg.context_composer.injections import (
    MANIFEST_BYTES_LIMIT, MANIFEST_LIMIT, append_injection, can_append_injection,
    record_injection_diagnostic, record_manifest_rows,
)
from lib.tasks_pkg.context_composer.registry import (
    INJECTION_REGISTRY, context_block, injection_spec,
)
from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD

pytestmark = pytest.mark.unit


def test_registry_rejects_unknown_ids_and_telemetry_cannot_reach_model():
    messages = [{'role': 'user', 'content': 'real request'}]
    original = copy.deepcopy(messages)
    with pytest.raises(KeyError):
        append_injection(messages, 'unregistered_reminder', 'oops')
    with pytest.raises(ValueError):
        append_injection(messages, 'context_budget', 'stop soon')
    assert messages == original
    assert all(spec.trigger and spec.necessity and spec.source
               for spec in INJECTION_REGISTRY.values())


def test_event_preserves_prefix_and_records_note_and_exact_provenance():
    task = {'id': 'task', '_attemptId': 'attempt', 'model': ''}
    messages = [{'role': 'system', 'content': 'operator system'},
                {'role': 'user', 'content': 'real request'}]
    original = copy.deepcopy(messages)
    append_injection(messages, 'tool_progress', 'Use actual evidence.', task=task,
                     round_num=3, reason='explicit_noop')
    assert messages[:2] == original
    assert messages[-1]['content'] == 'Use actual evidence.'
    assert messages[-1]['_isMeta'] is True
    assert messages[-1]['_contextInjectionId'] == 'tool_progress'
    row = task['_contextManifest'][-1]
    assert row['registryId'] == 'tool_progress'
    assert row['appended'] and row['hash'] and row['eventId']
    assert row['tokens'] > 0 and row['appendedTokens'] == row['tokens']
    assert row['delivery'] == 'appended_not_acknowledged'
    assert 'Use actual evidence.' not in json.dumps(row)
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD
    assert task[INJECTED_NOTES_FIELD][-1]['reason'] == 'explicit_noop'


def test_inbox_delivery_carrier_emits_no_duplicate_context_note():
    """``inbox_delivery`` renders through the ``_inboxInjects`` chip (the
    ``<swarm-update>`` card). Emitting a SECOND ``context-injection``
    ``system_note`` with the same payload rendered the intervention twice —
    the carrier carries no note_kind, so it stays note-silent."""
    task = {'id': 'task', '_attemptId': 'attempt', 'model': ''}
    messages = [{'role': 'user', 'content': 'real request'}]
    append_injection(messages, 'inbox_delivery', '<swarm-update>done</swarm-update>',
                     task=task, round_num=2)
    assert messages[-1]['role'] == 'user'
    assert messages[-1]['_isInboxInject'] is True
    assert not task.get(INJECTED_NOTES_FIELD)


def test_identical_event_after_history_rebuild_gets_new_receipt_identity():
    task, messages = {}, []
    append_injection(messages, 'terminal_recovery', 'continue', task=task)
    messages.clear()
    append_injection(messages, 'terminal_recovery', 'continue', task=task)
    assert len(task['_contextManifest']) == 2
    assert len({row['eventId'] for row in task['_contextManifest']}) == 2


def test_bootstrap_policy_retains_authority_and_cannot_elevate_mid_dialogue():
    messages = [{'role': 'system', 'content': 'operator'}]
    append_injection(messages, 'worker_policy', 'read-only worker')
    assert messages[-1]['role'] == 'system'
    messages.append({'role': 'user', 'content': 'objective'})
    with pytest.raises(ValueError, match='precede dialogue'):
        append_injection(messages, 'worker_policy', 'late elevation')


def test_reuse_diagnostics_retain_original_cost_and_latest_failure_reason():
    task, messages = {}, []
    request = ComposeRequest(task=task)
    append_context_blocks(messages, [context_block('skills_index', 'tool hint')], request)
    original = copy.deepcopy(task['_contextManifest'][0])
    for _ in range(300):
        append_context_blocks(messages, [context_block('skills_index', '', reason='provider_unavailable')], request)
    assert task['_contextManifest'][0] == original
    assert len(task['_contextManifest']) == 2
    assert task['_contextManifest'][-1]['reason'] == 'retained_after:provider_unavailable'
    assert task['_contextManifest'][-1]['appendedTokens'] == 0


def test_same_text_in_distinct_rounds_is_not_deduplicated_as_a_snapshot():
    task, messages = {}, []
    for round_num in (1, 2):
        append_injection(messages, 'terminal_recovery', 'continue', task=task,
                         round_num=round_num, action='continue')
    rows = task['_contextManifest']
    assert len(messages) == len(rows) == 2
    assert rows[0]['eventId'] != rows[1]['eventId']
    record_manifest_rows(task, [messages[-1]['_contextInjection']])
    assert len(task['_contextManifest']) == 2


def test_partial_tool_batch_defers_before_inbox_drain():
    messages = [{'role': 'user', 'content': 'work'}, {
        'role': 'assistant', 'content': '',
        'tool_calls': [{'id': 'a'}, {'id': 'b'}],
    }, {'role': 'tool', 'tool_call_id': 'a', 'content': 'done'}]
    original = copy.deepcopy(messages)
    assert not can_append_injection(messages)
    with pytest.raises(ValueError, match='tool batch'):
        append_injection(messages, 'inbox_delivery', 'result')
    assert messages == original
    messages.append({'role': 'tool', 'tool_call_id': 'b', 'content': 'done'})
    assert can_append_injection(messages)
    append_injection(messages, 'inbox_delivery', 'human steer', contains_human_steer=True)
    assert messages[-1]['_isInboxInject']
    assert messages[-1]['_containsHumanSteer']
    assert '_isMeta' not in messages[-1]


def test_no_identity_or_authority_override_through_arbitrary_flags():
    with pytest.raises(ValueError, match='classification'):
        append_injection([], 'url_prefetch', 'evidence', flags={'role': 'system'})


def test_runtime_snapshot_suppression_is_visible_and_repetition_is_bounded():
    task, messages = {}, []
    request = ComposeRequest(task=task)
    suppressed = context_block('modified_files', '', reason='no_new_writes')
    for _ in range(300):
        append_context_blocks(messages, [suppressed], request)
    assert messages == []
    assert len(task['_contextManifest']) == 1
    assert task['_contextManifest'][0]['reason'] == 'no_new_writes'
    assert not task['_contextManifest'][0]['injected']


def test_diagnostic_limits_and_content_free_budget_receipt():
    task = {}
    for index in range(MANIFEST_LIMIT * 2):
        record_injection_diagnostic(task, 'context_budget', reason=str(index), metrics={
            'remainingTokens': index, 'secret': 'do not copy', 'nan': float('nan'),
        })
    rows = task['_contextManifest']
    assert len(rows) <= MANIFEST_LIMIT
    assert all(not row['injected'] and row['tokens'] == 0 for row in rows)
    assert 'do not copy' not in json.dumps(rows)
    assert 'nan' not in rows[-1]['metrics']
    record_manifest_rows(task, [{'id': 'test', 'provenance': 'x' * (MANIFEST_BYTES_LIMIT + 1)}])
    assert len(json.dumps(task['_contextManifest'], ensure_ascii=True).encode()) <= MANIFEST_BYTES_LIMIT
    assert task['_contextManifestDropped'] > 0


def test_project_rules_and_checklist_are_not_clipped_by_optional_budget(monkeypatch):
    from lib.tasks_pkg.context_composer import _render
    monkeypatch.setattr(_render, '_count_tokens', lambda text, model: len(text))
    rules = 'rule\n' * 9000 + 'FINAL SAFETY RULE'
    task, messages = {}, []
    append_context_blocks(messages, [context_block('project_rules', rules)],
                          ComposeRequest(task=task, global_budget_tokens=10))
    assert 'FINAL SAFETY RULE' in messages[-1]['content'][0]['text']
    assert injection_spec('project_rules').required
    assert injection_spec('active_checklist').max_tokens is None


def test_all_production_block_constructors_use_registry():
    """Durable ownership ratchet: providers may supply evidence, not new policy."""
    root = Path(__file__).resolve().parents[2]
    paths = [root / 'lib/tasks_pkg/context_composer/_providers.py',
             root / 'lib/tasks_pkg/context_composer/checklist.py',
             root / 'lib/tasks_pkg/attachments.py']
    for path in paths:
        tree = ast.parse(path.read_text())
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id == 'ContextBlock' for node in ast.walk(tree)), path
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ('_block', 'context_block') or not node.args:
                continue
            if isinstance(node.args[0], ast.Constant):
                assert node.args[0].value in INJECTION_REGISTRY, path
            assert not ({kw.arg for kw in node.keywords} & {
                'authority', 'placement', 'required', 'priority', 'max_tokens'}), path


def test_shared_optional_controls_preserve_safety_user_input_and_recovery():
    disabled = frozenset({'memory_guidance', 'url_prefetch', 'project_rules',
                          'tool_progress', 'inbox_delivery', 'branch_context'})
    task = {'config': {'systemPromptBlocks': {'disabled': sorted(disabled)}}}
    messages = [{'role': 'user', 'content': 'real request'}]
    append_context_blocks(messages, [context_block('memory_guidance', 'optional'),
                                   context_block('project_rules', 'required rule')],
                          ComposeRequest(task=task, disabled_blocks=disabled))
    text = messages[-1]['content'][0]['text']
    assert 'required rule' in text and 'optional' not in text
    rows = {row['id']: row for row in task['_contextManifest']}
    assert rows['memory_guidance']['reason'] == 'disabled'
    assert append_injection(messages, 'url_prefetch', 'optional evidence', task=task) is None
    for key in ('tool_progress', 'inbox_delivery', 'branch_context'):
        assert append_injection(messages, key, 'protected payload', task=task) is not None


@pytest.mark.parametrize('entry_point', ['compose_context', 'compose_task_context', 'append_context_blocks'])
def test_public_facades_inherit_optional_policy_from_task_config(monkeypatch, entry_point):
    import lib.tasks_pkg.context_composer as composer
    disabled = ['memory_guidance', 'project_rules']
    task = {'config': {'systemPromptBlocks': {'disabled': disabled}}}
    request = ComposeRequest(task=task)
    blocks = [context_block('memory_guidance', 'optional hint'),
              context_block('project_rules', 'required rule')]
    seen = []

    def collect(messages, active_request):
        seen.append(active_request.disabled_blocks)
        return blocks

    monkeypatch.setattr(composer, 'collect_context_blocks', collect)
    messages = []
    if entry_point == 'append_context_blocks':
        composer.append_context_blocks(messages, blocks, request)
    elif entry_point == 'compose_task_context':
        composer.compose_task_context(messages, user_id=1, task=task)
    else:
        composer.compose_context(messages, request)
    text = '\n'.join(part['text'] for part in messages[-1]['content'])
    assert 'required rule' in text and 'optional hint' not in text
    rows = {row['id']: row for row in task['_contextManifest']}
    assert rows['memory_guidance']['reason'] == 'disabled'
    assert not request.disabled_blocks  # Caller input remains immutable.
    if seen:
        assert seen == [frozenset(disabled)]


def test_snapshot_waits_for_closed_tool_batch_and_remains_eligible():
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'a'}, {'id': 'b'}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': 'done'}]
    task, original = {}, copy.deepcopy(messages)
    blocks = [context_block('active_checklist', 'required checklist')]
    request = ComposeRequest(task=task)
    append_context_blocks(messages, blocks, request)
    assert messages == original
    row = task['_contextManifest'][-1]
    assert not row['injected'] and row['reason'] == 'deferred:open_tool_batch'
    messages.append({'role': 'tool', 'tool_call_id': 'b', 'content': 'done'})
    append_context_blocks(messages, blocks, request)
    assert 'required checklist' in messages[-1]['content'][0]['text']
    assert task['_contextManifest'][-1]['appended']


@pytest.mark.parametrize(('reason', 'withdraw'), [
    ('no_deferred_tools', True), ('discovery_unavailable', False)])
def test_capability_absence_retracts_but_transient_discovery_retains(reason, withdraw):
    task, messages = {}, []
    request = ComposeRequest(task=task)
    append_context_blocks(messages, [context_block('disclosed_tools', 'old tool hint')], request)
    prior_count = len(messages)
    append_context_blocks(messages, [context_block('disclosed_tools', '', reason=reason)], request)
    if withdraw:
        assert 'no longer active' in messages[-1]['content'][0]['text']
        assert 'retracted:' + reason in task['_contextManifest'][-1]['reason']
    else:
        assert len(messages) == prior_count
        assert 'old tool hint' in messages[-1]['content'][0]['text']
        assert 'retained_after:' + reason in task['_contextManifest'][-1]['reason']


def test_guidance_never_promises_absent_or_disabled_execution_lanes():
    from lib.tasks_pkg.context_composer import _providers
    request = ComposeRequest(has_real_tools=True, tool_names=frozenset({'read_files'}),
                             task={'config': {'orchestration': {'multiAgent': 'read_only'}}})
    assert _providers._swarm_guidance(request, 'two independent audits') == ''
    assert _providers._programmatic_guidance(request) == ''


def _raw_prompt_writes(source, *, allow_user_projection=False):
    """Catch literal/aliased carriers and list addition, not just append(dict)."""
    tree = ast.parse(source)
    assignments = {target.id: node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Assign) for target in node.targets
                   if isinstance(target, ast.Name)}

    def is_carrier(node, seen=frozenset()):
        if isinstance(node, ast.Name) and node.id in assignments and node.id not in seen:
            return is_carrier(assignments[node.id], seen | {node.id})
        if isinstance(node, (ast.List, ast.Tuple)):
            return any(is_carrier(child, seen) for child in node.elts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return is_carrier(node.left, seen) or is_carrier(node.right, seen)
        if not isinstance(node, ast.Dict):
            return False
        pairs = {key.value: value.value for key, value in zip(node.keys, node.values)
                 if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)}
        automatic = any(str(key).startswith(('_isMeta', '_isInboxInject', '_contextInjection'))
                        for key in pairs)
        return pairs.get('role') == 'user' and (automatic or not allow_user_projection)

    violations = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('append', 'insert', 'extend')
                and any(is_carrier(arg) for arg in node.args)):
            violations.append(node.lineno)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and is_carrier(node.value):
            violations.append(node.lineno)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.BinOp) and is_carrier(node.value):
            violations.append(node.lineno)
    return violations


@pytest.mark.parametrize('mutation', [
    "messages.append({'role': 'user', 'content': 'nudge'})",
    "carrier = {'role': 'user', 'content': 'nudge'}; messages.append(carrier)",
    "messages += [{'role': 'user', 'content': 'nudge'}]",
    "messages = messages + [{'role': 'user', 'content': 'nudge'}]",
])
def test_no_bypass_guard_detects_literal_alias_and_list_addition(mutation):
    assert _raw_prompt_writes(mutation)


def test_runtime_prompt_writers_cannot_bypass_shared_interface():
    """Recovery, inbox and auxiliary guidance cannot hand-append user dicts."""
    root = Path(__file__).resolve().parents[2]
    paths = [
        'lib/tasks_pkg/orchestrator/_tool_loop_breaker.py',
        'lib/tasks_pkg/orchestrator/_swarm_inbox.py',
        'lib/tasks_pkg/orchestrator/_turn.py',
        'lib/tasks_pkg/orchestrator/_finalize.py',
        'lib/tasks_pkg/stream_handler/_analyse.py',
        'lib/tasks_pkg/stream_handler/_terminal_recovery.py',
        'lib/tasks_pkg/message_builder/_prefetch.py',
        'lib/tasks_pkg/compaction/_pipeline.py',
        'lib/tasks_pkg/conv_message_builder/_load.py',
        'lib/paper/tools.py',
    ]
    violations = [(relative, line) for relative in paths
                  for line in _raw_prompt_writes((root / relative).read_text())]
    # Worker objective/context are genuine delegated input projections. Even
    # there, synthetic/tagged guidance must use the registered writer.
    for relative in ('lib/swarm/agent.py', 'lib/swarm/master.py', 'lib/swarm/scheduler.py'):
        violations.extend((relative, line) for line in _raw_prompt_writes(
            (root / relative).read_text(), allow_user_projection=True))
    assert not violations, violations
