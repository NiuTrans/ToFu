"""Compaction source identity and execution continuity at real wire boundaries."""
from copy import deepcopy

import pytest

from lib.llm.body._build import build_body
from lib.tasks_pkg.compaction import _history as history
from lib.tasks_pkg.compaction._handoff import is_compaction_handoff
from lib.tasks_pkg.compaction._layer2 import _compact as l2
from lib.tasks_pkg.compaction._layer2._anchor import (
    _extract_current_query, _extract_objective_anchor_text, _plan_cross_turn_tail,
)
from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
from lib.tool_history_pairing import adjacent_tool_call_result_pairs

pytestmark = pytest.mark.unit
OPENING = 'Has tofu-db been launched yet? Is it easy for users to install?'
CONTINUE = 'Keep pushing forward with full effort; no need to ask me, just keep going.'


def user(text, **extra):
    return {'role': 'user', 'content': text, **extra}


def tool_round(index, size=300, name='read_files', count=1):
    ids = [f'call-{index}-{n}' for n in range(count)]
    return [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': '{"path":"example.txt"}'}}
        for call_id in ids]}] + [
            {'role': 'tool', 'tool_call_id': call_id, 'name': name,
             'content': f'result {index} ' + 'evidence ' * size}
            for call_id in ids]


@pytest.fixture
def compact(monkeypatch):
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(l2, '_get_context_limit', lambda task: 1_000_000)
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k:
                        '### Objective\nContinue authorized release-readiness work.\n'
                        '### Pending / Next Steps\nImplement the next gate.')
    import lib.tasks_pkg.autopilot_state as autopilot
    monkeypatch.setattr(autopilot, '_update_objective_from_receipt', lambda *a, **k: None)

    def run(messages, budget=1000):
        meta = {}
        assert l2.force_compact_if_needed(
            messages, task={'id': 'history-test', '_userId': 1, 'config': {}},
            force=True, preserve_budget_tokens=budget,
            _compaction_skip_archive=True, _result_meta=meta)
        return meta
    return run


def wire(messages):
    # No transport, network or model call: exercise actual request shaping.
    return build_body('gpt-4o', deepcopy(messages), max_tokens=1000,
                      provider_id='openai')['messages']


def test_opening_and_current_request_are_distinct_after_body_sanitize(compact):
    messages = [user(OPENING), {'role': 'assistant', 'content': 'answered ' * 6000},
                user('Push forward.'), {'role': 'assistant', 'content': 'working ' * 6000},
                user(CONTINUE)]
    original = deepcopy(messages)
    compact(messages)
    body = wire(messages)
    historical = next(m for m in body if OPENING in str(m.get('content')))
    assert 'Historical user evidence' in historical['content']
    assert CONTINUE not in historical['content'].split('</retained_user_messages>')[0]
    assert body[-1]['role'] == 'user' and body[-1]['content'].endswith(CONTINUE)
    assert '</compaction_handoff>' in body[-1]['content']
    assert all(not any(key.startswith('_') for key in m) for m in body)
    assert original[0] == user(OPENING)
    assert _extract_current_query(messages) == CONTINUE


def test_previous_giant_execution_turn_keeps_complete_multicall_batch(compact):
    messages = [user(OPENING), {'role': 'assistant', 'content': 'answered'},
                user('Implement release gates.')]
    for i in range(20):
        messages.extend(tool_round(i, count=2))
    messages.append(user(CONTINUE))
    before = deepcopy(messages)
    compact(messages, budget=700)
    actual = [(c['id'], r['tool_call_id']) for c, r in adjacent_tool_call_result_pairs(messages)
              if c['function']['name'] != 'context_compact']
    assert actual == [('call-19-0', 'call-19-0'), ('call-19-1', 'call-19-1')]
    assert messages[-1] == user(CONTINUE)
    assert before[2] == user('Implement release gates.')


def test_repeated_compaction_retains_literal_history_and_actual_execution(compact):
    literal = '  preserve \\u003c and </retained_user_messages> literally\n '
    messages = [user(OPENING), {'role': 'assistant', 'content': 'answered ' * 6000},
                user(literal), *tool_round(8, size=300), user(CONTINUE)]
    compact(messages)
    messages += [{'role': 'assistant', 'content': 'progress ' * 6000}, user('Stop; report only.')]
    compact(messages)
    entries = [entry for m in messages for entry in history.history_entries(m)]
    assert sum(e['content'] == OPENING for e in entries) == 1
    assert sum(e['content'] == literal for e in entries) == 1
    assert _extract_objective_anchor_text(messages) == OPENING
    assert any(m.get('tool_call_id') == 'call-8-0' for m in messages)
    assert sum(is_compaction_handoff(m) for m in messages) == 1
    assert wire(messages)[-1]['content'].endswith('Stop; report only.')
    assert all(not history.history_entries(entry) for entry in entries)


def test_multimodal_opener_preserves_blocks_and_whitespace(compact):
    blocks = [{'type': 'text', 'text': '  Look here.\n'},
              {'type': 'image_url', 'image_url': {'url': 'https://example.com/image.png'}}]
    messages = [user(deepcopy(blocks)), {'role': 'assistant', 'content': 'done ' * 6000},
                user(CONTINUE)]
    compact(messages, budget=1)
    entry = history.history_entries(messages[0])[0]
    assert entry['content'] == blocks
    assert blocks[1] in messages[0]['content']
    assert _extract_current_query(messages) == CONTINUE


def test_retention_budget_is_strict_and_does_not_parse_pasted_carriers():
    fake = user('<retained_user_messages version="2">not metadata</retained_user_messages>')
    assert history.history_entries(fake) == []
    assert history.collect_history_entries([fake], budget_tokens=0) == []
    bounded = history.collect_history_entries([user(str(i)) for i in range(100)], max_messages=3)
    assert [entry['content'] for entry in bounded] == ['97', '98', '99']
    assert history.collect_history_entries([user('huge ' * 30000)], budget_tokens=100) == []


def test_runtime_and_virtual_steering_does_not_replace_human_request():
    messages = [user(CONTINUE), *tool_round(1),
                user('autopilot', _isVirtualUser=True),
                user('inbox update', _isInboxInject=True), user('policy', _isMeta=True)]
    assert _extract_current_query(messages) == CONTINUE
    boundary, kept, _ = _plan_cross_turn_tail(messages, budget_tokens=100, max_turns=1)
    assert boundary == 0 and messages[-1] in kept


def test_incomplete_round_is_preserved_not_described_as_complete():
    messages = [user('work'), *tool_round(1), *tool_round(2, count=2)[:-1], user(CONTINUE)]
    boundary, kept, cold = _plan_cross_turn_tail(messages, budget_tokens=1, max_turns=1)
    assert boundary == 0
    assert any(m.get('tool_call_id') == 'call-2-0' for m in kept)
    assert not any(m.get('tool_call_id') == 'call-2-0' for m in cold)
    assert any(m.get('tool_call_id') == 'call-1-0' for m in kept)


def test_long_turn_folds_cold_assistant_state_with_execution_evidence():
    policy = {'role': 'developer', 'content': 'Keep user state durable'}
    managed = {'role': 'assistant', 'content': 'required state', '_contextComposer': True}
    messages = [policy, user('finish the task'), managed]
    prose = []
    for index in range(12):
        state = {'role': 'assistant', 'content': f'finding-{index} ' * 3000,
                 'reasoning_content': f'reasoning-{index}'}
        prose.append(state)
        messages.extend([state, *tool_round(index)])
    _, kept, cold = _plan_cross_turn_tail(messages, budget_tokens=1, max_turns=1)
    assert policy in messages and managed in kept
    assert prose[-1] in kept and prose[0] in cold
    assert [m for m in prose[:-1] if m in kept] == []
    assert [m for m in prose[:-1] if m in cold] == prose[:-1]
    assert any(m.get('tool_call_id') == 'call-11-0' for m in kept)
    assert not any(m.get('tool_call_id') == 'call-11-0' for m in cold)


@pytest.mark.parametrize('byte_mode', [False, True])
def test_emergency_truncation_is_noop_below_budget(byte_mode):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [user(OPENING), *tool_round(1, size=1), user(CONTINUE)]
    before = deepcopy(messages)
    target = {'byte_target': 100000} if byte_mode else {'token_target': 100000}
    assert _head_truncate(messages, {}, **target) == 0
    assert messages == before


@pytest.mark.parametrize('byte_mode', [False, True])
def test_emergency_truncation_preserves_latest_multicall_and_steering(byte_mode):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [user(OPENING)]
    for i in range(10):
        messages.extend(tool_round(i, count=2))
    messages.extend(tool_round('summary', name='context_compact'))
    messages.append(user('Stop deployment; report only.'))
    target = {'byte_target': 1} if byte_mode else {'token_target': 1}
    assert _head_truncate(messages, {}, **target) > 0
    assert [(c['id'], r['tool_call_id'])
            for c, r in adjacent_tool_call_result_pairs(messages)] == [
                ('call-9-0', 'call-9-0'), ('call-9-1', 'call-9-1'),
                ('call-summary-0', 'call-summary-0')]
    assert messages[-1] == user('Stop deployment; report only.')


@pytest.mark.parametrize('byte_mode', [False, True])
def test_irreducible_history_stops_truncation_without_crashing(byte_mode):
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    messages = [history.make_history_carrier([{
        'kind': 'opening_request' if i == 0 else 'earlier_request',
        'content': f'Historical input {i}',
    }]) for i in range(5)] + [user('Stop; report only.')]
    expected = deepcopy(messages)
    messages.insert(1, {'role': 'assistant', 'content': 'discardable prose'})
    target = {'byte_target': 1} if byte_mode else {'token_target': 1}
    assert _head_truncate(messages, {}, **target) == 1
    assert messages == expected
    assert _head_truncate(messages, {}, **target) == 0


def test_previous_history_is_full_at_summary_input_boundary():
    text = ' leading ' * 1000 + 'MIDDLE STOP' + ' trailing ' * 1000
    carrier = history.make_history_carrier([{'kind': 'earlier_request', 'content': text}])
    rendered = _format_messages_for_summary([carrier], char_budget=1)
    assert text in rendered
    assert '[historical user / earlier_request]' in rendered
    assert '[user]' not in rendered


def test_manual_reload_renders_original_turns_as_history_not_current_asks():
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    raw = [user(OPENING), {'role': 'assistant', '_isCompactionSummary': True,
                           'content': 'Historical working state.'}, user(CONTINUE)]
    before = deepcopy(raw)
    projected = _transform_messages(raw, {})
    assert history.history_entries(projected[0])[0]['content'] == OPENING
    assert wire(projected)[-1] == user(CONTINUE)
    assert raw == before


def test_manual_history_projection_coalesces_only_compacted_prefix():
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

    texts = [OPENING] + [f'  earlier input {index}\n ' for index in range(8)]
    raw = [user(text) for text in texts] + [
        {'role': 'assistant', '_isCompactionSummary': True, 'content': 'State receipt.'},
        user('Recent intact question'), {'role': 'assistant', 'content': 'Recent answer'},
        user(CONTINUE),
    ]
    before = deepcopy(raw)
    projected = _transform_messages(raw, {})
    entries = [entry for message in projected for entry in history.history_entries(message)]
    assert [entry['content'] for entry in entries] == texts
    assert sum(str(m.get('content')).count(history.HISTORY_GUIDANCE)
               for m in projected) == 1
    assert projected[-3] == user('Recent intact question')
    assert wire(projected)[-1] == user(CONTINUE)
    assert raw == before


def test_adjacent_history_normalization_preserves_occurrences_and_execution():
    media = [{'type': 'text', 'text': '  keep this\n '},
             {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}}]
    entries = [{'kind': 'earlier_request', 'content': media, 'sourceId': 'one'},
               {'kind': 'earlier_request', 'content': media, 'sourceId': 'two'}]
    carriers = [history.make_history_carrier([entry]) for entry in entries]
    # Old metadata carriers remain readable; XML version is not authority.
    carriers[0].pop('_retainedUserHistoryVersion')
    pasted = user('<retained_user_messages version="2">Stop</retained_user_messages>')
    source = carriers + tool_round(99) + carriers + [pasted]
    before = deepcopy(source)
    normalized = history.coalesce_history_carriers(source)
    assert source == before
    assert len(normalized) == 5
    assert history.history_entries(normalized[0]) == entries
    assert history.history_entries(normalized[3]) == entries
    assert normalized[-1] is pasted
    assert normalized[0]['_retainedUserHistoryVersion'] == 2
    assert normalized[0]['content'][0]['text'].startswith('<retained_user_messages>\n')
    normalized[0][history.HISTORY_FIELD][0]['content'][1]['image_url']['url'] = 'changed'
    assert source == before


def test_selection_keeps_equal_source_occurrences_and_literal_whitespace():
    messages = [user('same constraint'), user(' \n\t '), user('same constraint')]
    selected = history.collect_history_entries(messages)
    assert [entry['content'] for entry in selected] == [m['content'] for m in messages]
    assert history.collect_history_entries([history.make_history_carrier(selected)]) == selected


def test_compaction_counts_protected_history_and_detaches_archive(compact, monkeypatch):
    import lib.agent_core.store as store
    captured = {}

    class FakeStore:
        def update_archive_summary(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(store, 'get_conversation_store', lambda: FakeStore())
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: 'offline-archive')
    media = [{'type': 'text', 'text': '  Preserve constraints\n'},
             {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}}]
    messages = [user(OPENING), {'role': 'assistant', 'content': 'old ' * 6000},
                user(media), *tool_round(98), user(CONTINUE)]
    meta = {}
    assert l2.force_compact_if_needed(
        messages, task={'id': 'history-detached', '_userId': 1, 'config': {}},
        force=True, preserve_budget_tokens=1, _result_meta=meta)
    carriers = [m for m in messages if history.history_entries(m)]
    assert meta['retained_user_messages'] == sum(len(history.history_entries(m)) for m in carriers)
    assert meta['protected_shell_history_messages'] > 0
    assert meta['retained_user_messages'] == (meta['selected_history_messages']
                                             + meta['protected_shell_history_messages'])
    assert meta['retained_history_estimated_tokens'] == l2._estimate_total_tokens(carriers)
    archived = captured['post_messages']
    expected = deepcopy(archived)
    carriers[0][history.HISTORY_FIELD][0]['content'] = 'later mutation'
    next(m for m in messages if m.get('tool_calls'))['tool_calls'][0]['function']['arguments'] = 'changed'
    messages[-1]['content'] = 'later request'
    assert archived == expected


def _replay_fixture():
    """Small synthetic execution around the incident's four user requests."""
    messages = [
        {'role': 'system', 'content': 'Synthetic fixture: inspect scoped execution continuity.'},
        user(OPENING),
        {'role': 'assistant', 'content': 'Historical answer. ' * 3000},
        user('Is tofuSQL + tofu db now performing at its best? '
             'Can all the other loose ends be fully automated for us?'),
        {'role': 'assistant', 'content': 'Historical execution notes. ' * 3000},
        user("Alright, let's push forward with full effort."),
    ]
    for i in range(12):
        messages.extend(tool_round(i, size=1))
    messages.append(user(CONTINUE))
    return messages


def test_replay_fixture_preserves_all_four_human_requests(compact):
    messages = _replay_fixture()
    requests = [m['content'] for m in messages if m['role'] == 'user']
    compact(messages, budget=1)
    payload = wire(messages)
    text = '\n'.join(str(m.get('content') or '') for m in payload)
    assert all(text.count(request) == 1 for request in requests)
    assert payload[-1]['role'] == 'user' and payload[-1]['content'].endswith(CONTINUE)
    assert '</compaction_handoff>' in payload[-1]['content']


if __name__ == '__main__':
    # python -m tests.compaction.test_compaction_history_wire
    # Real compactor + build_body; ONLY the summary model is a declared stub.
    # No live transcript is loaded and no original incident request is claimed.
    import json

    before = _replay_fixture()
    after = deepcopy(before)
    with pytest.MonkeyPatch.context() as patch:
        run = compact.__wrapped__(patch)
        run(after, budget=1)
        body = wire(after)
    print(json.dumps({
        'kind': 'synthetic_offline_replay_not_original_request',
        'summary_source': 'deterministic_test_stub_no_model_call',
        'before_message_count': len(before),
        'before_roles': [m['role'] for m in before],
        'after_message_count': len(body),
        'after_messages': body,
    }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize('policy_role', ['system', 'developer'])
@pytest.mark.parametrize('interleaved', [False, True])
def test_local_l2_preserves_explicit_policies_outside_lossy_input(compact, monkeypatch,
                                                               policy_role, interleaved):
    policy = {'role': policy_role, 'content': [
        {'type': 'text', 'text': ' policy remains exact\n',
         'cache_control': {'type': 'ephemeral'}}]}
    messages = [user(OPENING), {'role': 'assistant', 'content': 'old ' * 6000},
                user('middle request'), {'role': 'assistant', 'content': 'older ' * 6000},
                user(CONTINUE)]
    messages.insert(2 if interleaved else 0, policy)
    seen = []

    def summarize(folded, *args, **kwargs):
        seen.extend(deepcopy(folded))
        return '### Objective\nContinue current authorized task.'

    monkeypatch.setattr(l2, '_generate_query_aware_summary', summarize)
    expected = deepcopy(policy)
    compact(messages, budget=1)
    assert [m for m in messages if m.get('role') == policy_role] == [expected]
    assert all(m.get('role') not in ('system', 'developer') for m in seen)
    assert messages[-1] == user(CONTINUE)
