"""Public composition preserves history across snapshot lifecycle transitions."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context

pytestmark = pytest.mark.unit


def test_snapshot_reuse_failure_replacement_and_withdrawal_preserve_sent_bytes():
    messages = [{'role': 'system', 'content': 'Owner policy'},
                {'role': 'user', 'content': 'Owner request'}]
    request = ComposeRequest()

    def compose(content='', reason=''):
        result = render_context(messages, [context_block(
            'user_context', content, reason=reason)], request)
        return result.manifest[0]

    first = compose('Preference A')
    sent = deepcopy(messages)
    reused = compose('Preference A')
    assert messages == sent
    assert reused['reason'] == 'already_present' and reused['appendedTokens'] == 0

    failed = compose(reason='provider_timeout')
    assert messages == sent
    assert failed['reason'] == 'retained_after:provider_timeout'
    assert failed['hash'] == first['hash'] and not failed['appended']

    changed = compose('Preference B')
    assert messages[:len(sent)] == sent
    assert changed['reason'] == 'supersedes:' + first['hash']
    assert changed['hash'] != first['hash'] and changed['appended']
    sent = deepcopy(messages)

    withdrawn = compose(reason='profile_empty')
    assert messages[:len(sent)] == sent
    assert withdrawn['reason'].endswith(';retracted:profile_empty')
    assert withdrawn['appended']
    sent = deepcopy(messages)
    compose(reason='profile_empty')
    assert messages == sent


def test_authoritative_policy_withdrawal_only_removes_its_owned_carrier():
    messages = [{'role': 'system', 'content': 'Owner policy'},
                {'role': 'user', 'content': 'Owner request'}]
    request = ComposeRequest()
    render_context(messages, [context_block('platform_static', 'Platform policy'),
                              context_block('ui_nav_directory', 'UI directory')], request)
    retained = deepcopy([m for m in messages if not any(
        row['id'] == 'ui_nav_directory' for row in m.get('_contextBlocks', []))])
    result = render_context(messages, [context_block(
        'ui_nav_directory', '', reason='non_browser_surface')], request)
    assert messages == retained
    assert not result.manifest[0]['injected']


def test_open_tool_batch_defers_snapshot_until_all_results_arrive():
    messages = [{'role': 'user', 'content': 'Read both files'}, {
        'role': 'assistant', 'tool_calls': [
            {'id': 'one', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}},
            {'id': 'two', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}},
        ]}, {'role': 'tool', 'tool_call_id': 'one', 'content': 'File one'}]
    sent = deepcopy(messages)
    blocks = [context_block('user_context', 'Preference A')]
    deferred = render_context(messages, blocks, ComposeRequest()).manifest[0]
    assert messages == sent
    assert deferred['reason'] == 'deferred:open_tool_batch'
    assert not deferred['injected'] and deferred['appendedTokens'] == 0
    messages.append({'role': 'tool', 'tool_call_id': 'two', 'content': 'File two'})
    sent = deepcopy(messages)
    admitted = render_context(messages, blocks, ComposeRequest()).manifest[0]
    assert messages[:len(sent)] == sent
    assert admitted['appended'] and admitted['appendedTokens'] > 0
