"""Segmented project mailbox history on real SQLite, PostgreSQL and native authorities.

The mailbox uses only generic record point reads and expected-version writes, so
both storage authorities serve it unchanged: no listing, deletion or eviction.
The fixture never opens or switches the user's selected live authority.
"""
from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

from lib.conversations import project_messages as pm

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api

SEGMENT = 3


@pytest.fixture
def authority(semantic_authority, monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', SEGMENT)
    factory, owner, _ = semantic_authority
    project = '/project/' + uuid.uuid4().hex

    def mailbox(*, user=owner, path=project):
        return pm.ProjectMailbox(user, path, 'recipient', client=factory)

    return mailbox, factory, owner


def submit(box, sender, key, content=None):
    return box.submit(sender, {'content': content or f'{sender} {key}', 'idempotency_key': key}, 'task-' + sender)


def every_page(box, sender=''):
    rows, cursor = [], ''
    while True:
        page = box.page(cursor, sender)
        rows[:0] = page['messages']
        if page['total'] == len(page['messages']):
            return rows
        cursor = page['messages'][0]['id']


def test_many_segment_history_pages_replays_and_receipts_old_originals(authority):
    mailbox, _, _ = authority
    box = mailbox()
    sent = [submit(box, 'alpha' if i % 4 == 1 else 'beta', str(i)) for i in range(100)]
    head, _ = box.head()
    assert head['sealed'] == 33 and len(head['messages']) == 1
    assert every_page(box) == sent
    alpha = [m for m in sent if m['sender'] == 'alpha']
    assert box.page('', 'alpha')['total'] == len(alpha) == 25
    assert every_page(box, 'alpha') == alpha
    first = sent[0]
    assert submit(box, 'beta', '0') == first  # replay after an uncertain ACK finds the sealed original
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        submit(box, 'beta', '0', 'Rewritten')
    considered = box.consider({'message_id': first['id'], 'decision': 'accepted', 'note': 'Old original',
                               'expected_revision': 1}, 'task-r')
    assert considered['revision'] == 2 and box.get(first['id']) == considered
    with pytest.raises(pm.ProjectMessageError, match='revision'):
        box.consider({'message_id': first['id'], 'decision': 'rejected', 'note': 'stale',
                      'expected_revision': 1}, 'task-r')
    box.confirm([sent[1]['id'], sent[2]['id'], sent[-1]['id']], 'task-r', 0)
    assert [box.get(m['id'])['state'] for m in (sent[1], sent[2], sent[-1])] == ['input_confirmed'] * 3
    head, _ = box.head()
    assert [m['id'] for _, m in box.pending(head, lambda sender: True, set())] == [m['id'] for m in sent[3:-1]]
    assert [m['id'] for _, m in box.pending(head, lambda sender: sender == 'alpha', set())] == [
        m['id'] for m in alpha if m['id'] not in {sent[1]['id']}]


def test_concurrent_appends_cross_segment_boundaries_exactly_once(authority):
    mailbox, _, _ = authority

    def run(sender):
        for i in range(12):
            for _ in range(64):
                try:
                    submit(mailbox(), sender, str(i))
                    break
                except pm.ProjectMessageError:
                    continue  # uncertain: retry the same identity
            else:
                raise AssertionError('send never committed')

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(run, ['alpha', 'beta', 'gamma']))
    box = mailbox()
    history = box.read()
    assert len(history) == len({m['id'] for m in history}) == 36
    for sender in ('alpha', 'beta', 'gamma'):
        own = [m for m in history if m['sender'] == sender]
        assert [m['content'] for m in own] == [f'{sender} {i}' for i in range(12)]
        assert every_page(box, sender) == own
    assert box.find([m['id'] for m in history]) == {m['id']: m for m in history}


def test_legacy_single_record_mailbox_is_read_and_sealed_without_loss(authority, monkeypatch):
    mailbox, factory, _ = authority
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', 128)
    box = mailbox()
    # The pre-segmentation single record: one capped list, no locators, same message IDs.
    legacy = [{'sender': sender, 'content': f'{sender} {i}', 'kind': 'reference', 'replyTo': '',
               'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{box.key}:{sender}:{i}')), 'state': 'submitted',
               'revision': 1, 'senderTaskId': 'old-task', 'submittedAt': 1_700_000_000_000 + i, 'decision': None}
              for i, sender in ((i, 'alpha' if i % 2 else 'beta') for i in range(128))]
    record = {'namespace': pm.CONTRACT['namespace'], 'key': box.key}
    factory(write=True).command('record.put', {**record, 'expected_version': 0,
                                               'value': {'binding': box.binding, 'messages': legacy}},
                                'legacy-' + uuid.uuid4().hex)
    assert box.read() == legacy and every_page(box) == legacy
    grown = submit(box, 'gamma', 'after the old lifetime count')
    assert box.head()[0]['sealed'] == 1
    assert every_page(box) == [*legacy, grown]
    assert box.get(legacy[0]['id']) == legacy[0] and submit(box, 'beta', '0') == legacy[0]


def test_owner_and_project_histories_are_isolated(authority):
    mailbox, _, owner = authority
    mine = mailbox()
    sent = [submit(mine, 'alpha', str(i)) for i in range(2 * SEGMENT + 1)]
    for other in (mailbox(path='/project/' + uuid.uuid4().hex), mailbox(user=owner + 1)):
        assert other.read() == [] and all(other.get(m['id']) is None for m in sent)
        with pytest.raises(pm.ProjectMessageError, match='Pagination'):
            other.page(sent[0]['id'])
        with pytest.raises(pm.ProjectMessageError, match='not found'):
            other.consider({'message_id': sent[0]['id'], 'decision': 'accepted', 'note': '',
                            'expected_revision': 1}, 'task-r')
        same_key = submit(other, 'alpha', '0')
        assert same_key['id'] != sent[0]['id'] and mine.get(same_key['id']) is None
    assert mine.read() == sent
