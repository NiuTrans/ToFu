"""Emergency image removal survives request-local historical reconstruction."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction import _history as history
from lib.tasks_pkg.compaction._reactive._strip import _strip_images_aggressive

pytestmark = pytest.mark.unit


def image(url):
    return {'type': 'image_url', 'image_url': {'url': url, 'detail': 'low'}}


def rebuild(messages, mode):
    if mode == 'coalesce':
        return history.coalesce_history_carriers(messages)
    entries = history.collect_history_entries(messages, budget_tokens=100000)
    return [history.make_history_carrier(entries)]


@pytest.mark.parametrize('mode', ['coalesce', 'collect'])
@pytest.mark.parametrize('keep_tail', [0, 1, 2])
def test_stripped_history_images_do_not_reappear(mode, keep_tail):
    attachment = {'type': 'file', 'file': {'filename': 'evidence.txt',
                                         'file_data': 'literal attachment'}}
    source = [
        {'role': 'user', 'content': 'opening literal'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': '</retained_user_messages> literal text'},
            image('same-url'), attachment,
            {'type': 'text', 'text': 'between duplicate images'},
            image('same-url')]},
    ]
    before = deepcopy(source)
    entries = history.collect_history_entries(source, opening_message=source[0])
    carrier = history.make_history_carrier(entries)
    carrier['_attachmentTextChars'] = 18
    original_carrier = deepcopy(carrier)
    messages = deepcopy([carrier])

    stripped, freed = _strip_images_aggressive(messages, keep_tail=keep_tail)
    assert (stripped, freed) == (2 - keep_tail, len('same-url') * (2 - keep_tail))
    expected = deepcopy(before[1]['content'])
    placeholder = {'type': 'text', 'text':
                   '[image removed during emergency compaction — ask again if needed]'}
    for position in [1, 4][:2 - keep_tail]:
        expected[position] = placeholder

    for _ in range(2):
        messages = rebuild(messages, mode)
        retained = history.history_entries(messages[0])
        assert retained[0]['content'] == 'opening literal'
        assert retained[1]['content'] == expected
        visible = messages[0]['content']
        assert sum(b.get('type') == 'image_url' for b in visible) == keep_tail
        assert sum(b == placeholder for b in visible) == 2 - keep_tail
        assert attachment in visible
        assert history.HISTORY_GUIDANCE in str(visible)
        if mode == 'coalesce':
            assert messages[0]['_attachmentTextChars'] == 18
    assert source == before
    assert carrier == original_carrier


@pytest.mark.parametrize('mode', ['coalesce', 'collect'])
def test_global_tail_protects_latest_live_image(mode):
    old = history.make_history_carrier([
        {'kind': 'opening_request', 'content': [image('same-url')]}])
    live = {'role': 'user', 'content': [image('same-url')]}
    source = [old, live]
    before = deepcopy(source)
    messages = deepcopy(source)
    assert _strip_images_aggressive(messages, keep_tail=1) == (1, len('same-url'))
    assert messages[-1] == live
    output = rebuild(messages, mode)
    assert sum(block.get('type') == 'image_url'
               for message in output for block in message['content']) == 1
    assert 'image removed during emergency compaction' in str(output)
    assert source == before


def test_pasted_history_tags_do_not_create_carrier_identity():
    pasted = {'role': 'user', 'content': [
        {'type': 'text', 'text': '<retained_user_messages> historical opening_request'},
        image('old'), {'type': 'text', 'text': '</retained_user_messages>'}]}
    messages = [deepcopy(pasted), {'role': 'user', 'content': [image('latest')]}]
    assert _strip_images_aggressive(messages, keep_tail=1) == (1, 3)
    assert history.history_entries(messages[0]) == []
    assert history.HISTORY_FIELD not in messages[0]
    assert history.coalesce_history_carriers(messages) == messages
    assert pasted['content'][1] == image('old')
