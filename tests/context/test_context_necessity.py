"""Only new, relevant facts may add automatic context or timeline notes."""
from __future__ import annotations

import copy

import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest, append_context_blocks, context_block, render_context,
)

pytestmark = pytest.mark.unit
TODAY = 'Current date: 2026-10-09'


def _clock(messages, text=TODAY):
    return render_context(messages, [context_block('current_date', text)], ComposeRequest())


@pytest.mark.parametrize('role', ['system', 'developer'])
@pytest.mark.parametrize('structured', [False, True])
def test_existing_trusted_date_needs_no_append(role, structured):
    text = 'Trusted operating contract.\n\n' + TODAY
    content = [{'type': 'text', 'text': text}] if structured else text
    messages = [{'role': role, 'content': content}, {'role': 'user', 'content': 'request'}]
    original = copy.deepcopy(messages)
    result = _clock(messages)
    assert messages == original
    assert result.manifest[0]['reason'] == 'already_present'
    assert result.manifest[0]['appendedTokens'] == 0


@pytest.mark.parametrize('role', ['user', 'tool', 'assistant'])
def test_untrusted_date_or_marker_cannot_suppress_clock(role):
    messages = [{'role': role, 'content': (
        '<!-- tofu-context:current_date:start -->\n' + TODAY
        + '\n<!-- tofu-context:current_date:end -->')}]
    result = _clock(messages)
    assert result.manifest[0]['appended']


@pytest.mark.parametrize('text', [
    '```text\n' + TODAY + '\n```',
    '~~~\n' + TODAY + '\n~~~',
    '<example>\n' + TODAY + '\n</example>',
    '> ' + TODAY,
    '    ' + TODAY,
    'Current date: 2026-99-09',
])
def test_policy_examples_and_invalid_dates_are_not_witnesses(text):
    messages = [{'role': 'system', 'content': text}]
    assert _clock(messages).manifest[0]['appended']


@pytest.mark.parametrize('retained_metadata', [True, False])
def test_owned_evidence_quoting_clock_markers_cannot_supply_the_date(retained_metadata):
    quoted = ('<!-- tofu-context:current_date:start -->\n' + TODAY
              + '\n<!-- tofu-context:current_date:end -->')
    messages = []
    render_context(messages, [context_block('relevant_memories', quoted)], ComposeRequest())

    if not retained_metadata:
        messages[-1].pop('_contextBlocks', None)
    assert _clock(messages).manifest[0]['appended']


def test_owned_date_reuses_then_cross_day_updates_once():
    messages = [{'role': 'system', 'content': 'Current date: 2026-10-08'}]
    assert _clock(messages).manifest[0]['appended']
    original = copy.deepcopy(messages)
    assert _clock(messages).manifest[0]['reason'] == 'already_present'
    assert messages == original
    tomorrow = 'Current date: 2026-10-10'
    assert _clock(messages, tomorrow).manifest[0]['appended']
    original = copy.deepcopy(messages)
    assert _clock(messages, tomorrow).manifest[0]['reason'] == 'already_present'
    assert messages == original


def test_compaction_without_date_reintroduces_it_despite_task_watermark():
    messages = [{'role': 'user', 'content': 'compacted history, no clock'}]
    task = {'_contextManifest': [{'id': 'current_date', 'reason': 'already_present'}]}
    result = append_context_blocks(messages, [context_block('current_date', TODAY)],
                                   ComposeRequest(task=task))
    assert result.manifest[0]['appended']


def test_steady_date_and_empty_memory_emit_no_timeline_note(monkeypatch):
    import lib.tasks_pkg.segments as segments
    notes = []
    monkeypatch.setattr(segments, 'record_injected_note',
                        lambda *a, **kw: notes.append(kw))
    messages, task = [{'role': 'system', 'content': TODAY}], {}
    request = ComposeRequest(task=task)
    memory = context_block('relevant_memories', 'Verified evidence from earlier work')
    append_context_blocks(messages, [memory], request)
    notes.clear()
    original = copy.deepcopy(messages)
    for _ in range(3):
        result = append_context_blocks(messages, [
            context_block('relevant_memories', '', reason='no_high_confidence_matches'),
            context_block('current_date', TODAY),
        ], request)
        assert all(row['appendedTokens'] == 0 for row in result.manifest)
    assert messages == original
    assert notes == []
    # Rediscovering the same still-visible evidence is also not a new update.
    append_context_blocks(messages, [memory], request)
    assert messages == original
    assert notes == []


@pytest.mark.parametrize('reason', ['empty', 'disabled', 'project_disabled', 'provider_unavailable'])
def test_no_project_page_is_silent_even_with_historical_overview(reason):
    messages, task = [], {}
    request = ComposeRequest(task=task)
    append_context_blocks(messages, [context_block('project_context', '[Project Context]\nOLD')], request)
    original = copy.deepcopy(messages)
    task['_projectNarrativeDelivery'] = {'deliveryToken': 'uncommitted'}
    result = append_context_blocks(messages, [context_block('project_context', '', reason=reason)], request)
    assert messages == original
    assert not result.manifest[0]['injected']
    assert result.manifest[0]['appendedTokens'] == 0
    assert '_projectNarrativeDelivery' not in task


@pytest.mark.parametrize('omission', ['disabled', 'budget', 'open_batch', 'truncated'])
def test_unadmitted_project_page_must_not_await_ack(omission):
    task = {'_projectNarrativeDelivery': {'deliveryToken': 'prepared-not-delivered'}}
    messages = []
    kw = {}
    if omission == 'disabled':
        kw['disabled_blocks'] = frozenset({'project_context'})
    elif omission == 'budget':
        kw['global_budget_tokens'] = 1
    elif omission == 'open_batch':
        messages = [{'role': 'assistant', 'tool_calls': [{'id': 'call'}], 'content': ''}]
    content = '[Project Context]\n' + ('overflow ' * 20000 if omission == 'truncated' else 'NEW PAGE')
    result = append_context_blocks(messages, [context_block('project_context', content)],
                                   ComposeRequest(task=task, **kw))
    assert '_projectNarrativeDelivery' not in task
    assert (not result.manifest[0].get('appended')
            or 'truncated' in result.manifest[0]['reason'])


def test_oversized_project_page_is_never_ack_eligible_on_retry():
    messages = []
    task = {}
    request = ComposeRequest(task=task)
    block = context_block('project_context', '[Project Context]\n' + 'overflow ' * 20000)
    for _ in range(2):
        task['_projectNarrativeDelivery'] = {'deliveryToken': 'same-oversized-page'}
        result = append_context_blocks(messages, [block], request)
        assert result.manifest[0]['reason'] == 'over_block_budget'
        assert not result.manifest[0]['injected']
        assert result.manifest[0]['appendedTokens'] == 0
        assert '_projectNarrativeDelivery' not in task
        assert messages == []


def test_full_admitted_project_page_keeps_pending_ack_across_unrelated_updates():
    task = {'_projectNarrativeDelivery': {'deliveryToken': 'full-page'}}
    request = ComposeRequest(task=task)
    messages = []
    block = context_block('project_context', '[Project Context]\nNEW PAGE')
    append_context_blocks(messages, [block], request)
    assert task['_projectNarrativeDelivery']['deliveryToken'] == 'full-page'
    append_context_blocks(messages, [context_block('current_date', TODAY)], request)
    assert task['_projectNarrativeDelivery']['deliveryToken'] == 'full-page'
    result = append_context_blocks(messages, [block], request)
    assert result.manifest[0]['reason'] == 'already_present'
    assert task['_projectNarrativeDelivery']['deliveryToken'] == 'full-page'


def test_disabling_project_overview_skips_acquisition(monkeypatch):
    from lib.conversations import project_brain
    from lib.tasks_pkg.context_composer import _providers

    def unexpected(*args, **kwargs):
        pytest.fail('disabled overview must not prepare a cursor')

    monkeypatch.setattr(project_brain, 'prepare_project_context', unexpected)
    blocks = _providers._project_blocks(ComposeRequest(
        project_enabled=True, project_path='/owned', user_id=1, conv_id='conv',
        disabled_blocks=frozenset({'project_context'})), '')
    assert blocks[0].suppressed_reason == 'disabled'
    assert blocks[0].content == ''
