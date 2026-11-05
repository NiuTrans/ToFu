"""Segmented project mailbox history: no lifetime count, stable cursors, exact receipts.

A three-original segment size makes many-segment histories cheap; the shipped
size is exercised by the legacy-record and >128 cases.
"""
from copy import deepcopy
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from lib.conversations import project_messages as pm
from lib.storage.errors import StorageError
from lib.tasks_pkg.message_input_receipts import matched_ids
from tests.swarm.test_retained_agents import RecordClient

pytestmark = pytest.mark.unit

SEGMENT = 3


class LockedRecordClient(RecordClient):
    """Atomic record operations that yield between calls so writers interleave."""
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.reads = []
        self.conflicts = 0

    def query(self, operation, payload):
        time.sleep(0)
        with self.lock:
            self.reads.append(payload['key'])
            return super().query(operation, payload)

    def command(self, operation, payload, command_id):
        time.sleep(0)
        with self.lock:
            try:
                return super().command(operation, payload, command_id)
            except StorageError:
                self.conflicts += 1
                raise


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', SEGMENT)
    client = LockedRecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: client)
    conversations = {(owner, name): '/project' for owner in (1, 2) for name in ('alpha', 'beta', 'gamma', 'recipient')}

    def get(self, conv, *, user_id, include_messages):
        assert include_messages is False
        project = conversations.get((user_id, conv))
        return SimpleNamespace(metadata={'settings': {'projectPath': project}}) if project else None

    monkeypatch.setattr(pm.ConversationRepository, 'get', get)

    def task(conv, owner=1):
        return {'id': 'task-' + conv, 'convId': conv, '_userId': owner,
                'config': {'projectPath': conversations[(owner, conv)]}}

    return SimpleNamespace(client=client, conversations=conversations, task=task,
                           mailbox=pm.ProjectMailbox(1, '/project', 'recipient'))


def legacy_record(mailbox, count):
    """The pre-segmentation single record: one capped list, no locators, same message IDs."""
    def original(i):
        sender = 'alpha' if i % 2 else 'beta'
        return {'sender': sender, 'content': f'Legacy {i}', 'kind': 'reference', 'replyTo': '',
                'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{mailbox.key}:{sender}:{i}')), 'state': 'submitted',
                'revision': 1, 'senderTaskId': 'old-task', 'submittedAt': 1_700_000_000_000 + i, 'decision': None}
    return {'binding': mailbox.binding, 'messages': [original(i) for i in range(count)]}


def send(setup, sender, key, content=None, owner=1):
    return pm.execute('send_project_message', {
        'conversation_id': 'recipient', 'content': content or f'{sender} {key}', 'idempotency_key': key},
        setup.task(sender, owner))['message']


def walk(setup, conv, **args):
    """Every directory page, newest first, by following next_before_id."""
    pages, cursor = [], None
    while True:
        page = pm.execute('get_project_messages', {**args, **({'before_id': cursor} if cursor else {})},
                          setup.task(conv))
        pages.append(page)
        cursor = page['next_before_id']
        if cursor is None:
            return pages


def ids(pages):
    return [row['id'] for page in reversed(pages) for row in page['messages']]


def test_many_segments_page_back_completely_with_stable_cursors(setup):
    sent = [send(setup, 'alpha', str(i))['id'] for i in range(50)]
    head, _ = setup.mailbox.head()
    assert head['sealed'] == 16 and len(head['messages']) == 2
    pages = walk(setup, 'recipient')
    assert ids(pages) == sent
    assert [page['total_messages'] for page in pages] == [50, 30, 10]
    assert [page['older_messages_omitted'] for page in pages] == [30, 10, 0]
    cursor = pages[0]['next_before_id']
    for i in range(50, 60):
        send(setup, 'alpha', str(i))
    later = pm.execute('get_project_messages', {'before_id': cursor}, setup.task('recipient'))
    assert later['messages'] == pages[1]['messages'] and later['total_messages'] == 30
    view = pm.page_view(1, '/project', 'recipient', cursor)
    assert [row['id'] for row in view['messages']] == [row['id'] for row in pages[1]['messages']]
    assert view['next_before_id'] == pages[1]['next_before_id']
    history = setup.mailbox.read()
    assert len(history) == 60 and [m['id'] for m in history[:50]] == sent
    assert all(setup.mailbox.get(m['id']) == m for m in history)


def test_outgoing_history_follows_one_sender_across_segments(setup):
    mine, theirs = [], []
    for i in range(120):
        sender = 'alpha' if i % 4 == 1 else 'beta'
        (mine if sender == 'alpha' else theirs).append(send(setup, sender, str(i))['id'])
    pages = walk(setup, 'alpha', direction='outgoing', conversation_id='recipient')
    assert ids(pages) == mine
    assert [page['total_messages'] for page in pages] == [30, 10]
    assert [page['older_messages_omitted'] for page in pages] == [10, 0]
    last = send(setup, 'alpha', 'open')  # cursor inside the open segment
    page = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                               'before_id': last['id']}, setup.task('alpha'))
    assert [row['id'] for row in page['messages']] == mine[-20:] and page['total_messages'] == 30
    with pytest.raises(pm.ProjectMessageError, match='Pagination'):
        pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                            'before_id': theirs[0]}, setup.task('alpha'))
    with pytest.raises(pm.ProjectMessageError, match='not found'):
        pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                            'message_id': theirs[0]}, setup.task('alpha'))
    assert ids(walk(setup, 'beta', direction='outgoing', conversation_id='recipient')) == theirs


def test_replay_after_uncertainty_returns_the_sealed_original(setup, monkeypatch):
    first = send(setup, 'alpha', 'first', 'Original evidence')
    for i in range(10):
        send(setup, 'beta', str(i))
    assert first['id'] not in {m['id'] for m in setup.mailbox.head()[0]['messages']}
    rows = deepcopy(setup.client.rows)
    assert send(setup, 'alpha', 'first', 'Original evidence') == first
    assert setup.client.rows == rows  # an exact replay writes nothing
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        send(setup, 'alpha', 'first', 'Rewritten evidence')
    while len(setup.mailbox.head()[0]['messages']) < SEGMENT:
        send(setup, 'beta', 'fill-' + str(len(setup.client.rows)))
    sealed = setup.mailbox.head()[0]['sealed']
    original, lost = setup.client.command, []

    def command(operation, payload, command_id):
        result = original(operation, payload, command_id)
        if payload['key'] == setup.mailbox.key and not lost:
            lost.append(payload['value']['sealed'])
            raise StorageError('database_timeout', 'ACK lost after commit', retryable=True)
        return result

    monkeypatch.setattr(setup.client, 'command', command)
    sealing = send(setup, 'gamma', 'sealing')
    assert lost == [sealed + 1]
    assert send(setup, 'gamma', 'sealing') == sealing
    history = [m['id'] for m in setup.mailbox.read()]
    assert len(history) == len(set(history)) and history[0] == first['id'] and history[-1] == sealing['id']


def test_interrupted_seal_is_completed_by_the_next_append(setup, monkeypatch):
    held = [send(setup, 'alpha', str(i)) for i in range(SEGMENT)]
    original = setup.client.command

    def crash(operation, payload, command_id):
        if payload['key'].endswith(':pending'):  # the seal's last write before its head CAS
            raise StorageError('database_unavailable', 'process lost mid-seal')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', crash)
    with pytest.raises(StorageError):
        send(setup, 'beta', 'interrupted')
    monkeypatch.setattr(setup.client, 'command', original)
    # The partial copy is not authoritative: the head still owns its originals.
    assert setup.mailbox.head()[0]['sealed'] == 0
    assert [m['id'] for m in setup.mailbox.read()] == [m['id'] for m in held]
    considered = setup.mailbox.consider({'message_id': held[0]['id'], 'decision': 'accepted',
                                         'note': 'Recorded after the partial copy', 'expected_revision': 1}, 'task-r')
    retried = send(setup, 'beta', 'interrupted')
    assert setup.mailbox.head()[0]['sealed'] == 1
    assert setup.mailbox.read() == [considered, *held[1:], retried]
    assert setup.mailbox.get(held[0]['id']) == considered
    outgoing = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient'},
                          setup.task('alpha'))
    assert outgoing['total_messages'] == SEGMENT


def test_uncommitted_claim_never_hides_or_misplaces_a_later_append(setup, monkeypatch):
    original = setup.client.command

    def crash(operation, payload, command_id):
        if payload['key'] == setup.mailbox.key:
            raise StorageError('database_unavailable', 'process lost before the head CAS')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', crash)
    with pytest.raises(StorageError):
        send(setup, 'alpha', 'lost')
    monkeypatch.setattr(setup.client, 'command', original)
    [claim] = [key for _, key in setup.client.rows if key.startswith(setup.mailbox.key + ':message:')]
    lost = claim.rsplit(':', 1)[1]
    assert setup.mailbox.get(lost) is None and setup.mailbox.read() == []
    others = [send(setup, 'beta', str(i)) for i in range(2 * SEGMENT)]
    assert setup.mailbox.head()[0]['sealed'] == 1
    assert setup.mailbox.get(lost) is None  # its dangling locator names a segment without it
    retried = send(setup, 'alpha', 'lost')
    assert retried['id'] == lost and setup.mailbox.read() == [*others, retried]  # appended, not in the past
    for i in range(SEGMENT):
        send(setup, 'beta', 'later-' + str(i))
    assert setup.mailbox.head()[0]['sealed'] == 3 and setup.mailbox.get(lost) == retried
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        send(setup, 'alpha', 'lost', 'Rewritten')


def test_same_identity_racing_across_a_seal_commits_once_where_its_locator_points(setup, monkeypatch):
    held = [send(setup, 'beta', str(i)) for i in range(SEGMENT - 1)]
    original, raced = setup.client.command, []

    def command(operation, payload, command_id):
        if payload['key'] == setup.mailbox.key and not raced:
            raced.append(1)
            # Before this append's head CAS another sender fills the segment, then
            # the same identity retries from that full head and seals it first.
            send(setup, 'gamma', 'filler')
            raced.append(send(setup, 'alpha', 'duplicate'))
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', command)
    first = send(setup, 'alpha', 'duplicate')
    assert first == raced[1]
    history = setup.mailbox.read()
    assert [m['sender'] for m in history] == ['beta'] * (SEGMENT - 1) + ['gamma', 'alpha']
    assert history[:SEGMENT - 1] == held and history[-1] == first
    for i in range(SEGMENT):
        send(setup, 'beta', 'later-' + str(i))
    assert setup.mailbox.head()[0]['sealed'] == 2 and setup.mailbox.get(first['id']) == first
    assert send(setup, 'alpha', 'duplicate') == first


def test_competing_writers_during_a_seal_keep_every_receipt(setup, monkeypatch):
    held = [send(setup, 'alpha', str(i)) for i in range(SEGMENT)]
    original, raced = setup.client.command, []

    def command(operation, payload, command_id):
        if payload['key'] == setup.mailbox.key and payload['value']['sealed'] and not raced:
            raced.append(1)
            # Another recipient turn decides an original after this copy was taken,
            # then a second sender seals the same segment first.
            setup.mailbox.consider({'message_id': held[1]['id'], 'decision': 'rejected',
                                    'note': 'Raced', 'expected_revision': 1}, 'task-r')
            send(setup, 'gamma', 'winner')
        return original(operation, payload, command_id)

    monkeypatch.setattr(setup.client, 'command', command)
    loser = send(setup, 'beta', 'loser')
    assert raced and setup.client.conflicts
    history = setup.mailbox.read()
    assert [m['sender'] for m in history] == ['alpha'] * SEGMENT + ['gamma', 'beta']
    assert [m['id'] for m in history[:SEGMENT]] == [m['id'] for m in held] and history[-1] == loser
    assert history[1]['decision'] == 'rejected' and history[1]['revision'] == 2


def test_old_originals_keep_explicit_consideration_and_input_receipts(setup):
    old = send(setup, 'alpha', 'old', 'Old request')
    backlog = [send(setup, 'beta', str(i))['id'] for i in range(3 * SEGMENT)]
    recipient = setup.task('recipient')
    detail = pm.execute('get_project_messages', {'message_id': old['id']}, recipient)['messages'][0]
    assert detail == old
    head = setup.mailbox.head()
    decision = {'message_id': old['id'], 'decision': 'deferred', 'note': 'Later', 'expected_revision': 1}
    considered = pm.execute('consider_project_message', decision, recipient)['message']
    assert considered['decision'] == 'deferred' and considered['revision'] == 2
    assert setup.mailbox.head() == head  # a sealed receipt never rewrites the open segment
    assert pm.execute('consider_project_message', decision, recipient)['message'] == considered
    with pytest.raises(pm.ProjectMessageError, match='revision'):
        pm.execute('consider_project_message', {**decision, 'decision': 'accepted'}, recipient)
    sent = pm.execute('get_project_messages', {'direction': 'outgoing', 'conversation_id': 'recipient',
                                               'message_id': old['id']}, setup.task('alpha'))
    assert sent['messages'][0]['decision'] == 'deferred'
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(recipient, messages, 0)
    assert messages[-1]['_projectMessageIds'] == backlog[:pm.LIMITS['batchSize']]
    pm.inject(recipient, messages, 1)
    assert messages[-1]['_projectMessageIds'] == backlog[pm.LIMITS['batchSize']:]
    assert recipient['_projectMessageAttempt']['ids'] == backlog
    recipient['_verifiedMessageInputIds'] = matched_ids(recipient, {'messages': messages})
    pm.confirm(recipient, 1)
    states = {m['id']: m['state'] for m in setup.mailbox.read()}
    assert all(states[item] == 'input_confirmed' for item in backlog) and states[old['id']] == 'submitted'
    pending = setup.client.rows[(pm.CONTRACT['namespace'], setup.mailbox.key + ':pending')]['value']
    assert pending['senders'] == {}
    pm.inject(recipient, messages, 2)
    assert len(messages) == 3 and recipient['_projectMessageAttempt']['ids'] == []


def test_departed_sender_backlog_is_kept_and_skipped_without_reading_its_segments(setup):
    departed = [send(setup, 'gamma', str(i))['id'] for i in range(4 * SEGMENT)]
    current = [send(setup, 'alpha', str(i))['id'] for i in range(2 * SEGMENT)]
    setup.conversations[(1, 'gamma')] = '/other'
    departed_segments = {f'{setup.mailbox.key}:segment:{n}' for n in range(4)}
    setup.client.reads.clear()
    recipient = setup.task('recipient')
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(recipient, messages, 0)
    assert messages[-1]['_projectMessageIds'] == current
    assert not departed_segments & set(setup.client.reads)
    assert all(m['state'] == 'submitted' for m in setup.mailbox.read() if m['sender'] == 'gamma')
    setup.conversations[(1, 'gamma')] = '/project'  # returning restores eligibility
    returned = setup.task('recipient')
    messages = [{'role': 'user', 'content': 'Objective'}]
    pm.inject(returned, messages, 0)
    assert messages[-1]['_projectMessageIds'] == departed[:pm.LIMITS['batchSize']]


def test_legacy_single_record_mailbox_stays_readable_and_grows_into_segments(setup, monkeypatch):
    monkeypatch.setitem(pm.LIMITS, 'messagesPerSegment', 128)
    mailbox = pm.ProjectMailbox(1, '/project', 'recipient')
    record = legacy_record(mailbox, 128)
    setup.client.rows[(pm.CONTRACT['namespace'], mailbox.key)] = {'value': deepcopy(record), 'version': 7}
    legacy = record['messages']
    assert ids(walk(setup, 'recipient')) == [m['id'] for m in legacy]
    assert mailbox.get(legacy[0]['id']) == legacy[0]
    considered = mailbox.consider({'message_id': legacy[5]['id'], 'decision': 'accepted', 'note': 'Kept',
                                   'expected_revision': 1}, 'task-r')
    grown = send(setup, 'gamma', 'after the old lifetime count')
    head, _ = mailbox.head()
    assert head['sealed'] == 1 and head['messages'] == [grown] and head['unlocated'] == 0
    assert mailbox.read() == [*legacy[:5], considered, *legacy[6:], grown]
    assert mailbox.submit('alpha', {'content': 'Legacy 5', 'idempotency_key': '5'}, 'replay') == considered
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        mailbox.submit('alpha', {'content': 'Rewritten', 'idempotency_key': '5'}, 'replay')
    assert ids(walk(setup, 'recipient')) == [m['id'] for m in mailbox.read()]


def test_concurrent_senders_commit_every_original_exactly_once(setup):
    senders, failures = ('alpha', 'beta', 'gamma'), []

    def run(sender):
        try:
            for i in range(15):
                for _ in range(64):
                    try:
                        send(setup, sender, str(i))
                        break
                    except pm.ProjectMessageError as exc:
                        assert 'Concurrent/uncertain' in str(exc)  # retry the same identity
                else:
                    raise AssertionError('send never committed')
        except BaseException as exc:  # surfaced on the main thread
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(sender,)) for sender in senders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures
    history = setup.mailbox.read()
    assert len(history) == len({m['id'] for m in history}) == 45
    for sender in senders:
        own = [m for m in history if m['sender'] == sender]
        assert [m['content'] for m in own] == [f'{sender} {i}' for i in range(15)]
        assert ids(walk(setup, sender, direction='outgoing', conversation_id='recipient')) == [m['id'] for m in own]
    assert all(setup.mailbox.get(m['id']) == m for m in history)


def test_owner_and_project_histories_are_isolated(setup):
    mine = [send(setup, 'alpha', str(i))['id'] for i in range(2 * SEGMENT + 1)]
    foreign = send(setup, 'alpha', '0', owner=2)
    assert foreign['id'] not in mine
    for mailbox in (pm.ProjectMailbox(2, '/project', 'recipient'), pm.ProjectMailbox(1, '/other', 'recipient')):
        assert all(mailbox.get(item) is None for item in mine)
        with pytest.raises(pm.ProjectMessageError, match='Pagination'):
            mailbox.page(mine[0])
        with pytest.raises(pm.ProjectMessageError, match='not found'):
            mailbox.consider({'message_id': mine[0], 'decision': 'accepted', 'note': '', 'expected_revision': 1}, 't')
    assert [m['id'] for m in pm.ProjectMailbox(2, '/project', 'recipient').read()] == [foreign['id']]
    assert setup.mailbox.get(foreign['id']) is None
    with pytest.raises(pm.ProjectMessageError, match='not found'):
        pm.execute('get_project_messages', {'message_id': mine[0]}, setup.task('recipient', owner=2))
