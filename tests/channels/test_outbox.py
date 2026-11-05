"""Outbox (C5): policy at enqueue, disclosure, idempotency, ordering, approval, reconciliation."""
import pytest

from lib.channels import outbox
from lib.channels.model import DISCLOSURE_LABEL, ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER, make_event

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
GROUP = 'dx:gid:500'
OTHER = 'dx:gid:600'
ORIGIN = {'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'turn-1'}


class Sender:
    """Fake adapter port: scripted results per text, recorded sends, fake history."""

    def __init__(self):
        self.sent = []
        self.results = {}
        self.history = {}

    def send(self, thread, text, *, marker, reply_to_external_id):
        self.sent.append((str(thread), text, marker, reply_to_external_id))
        result = self.results.get(text)
        if isinstance(result, Exception):
            raise result
        return result or {'ok': True, 'externalId': str(7100000000000000000 + len(self.sent)),
                          'clientId': f'uuid-{len(self.sent)}'}

    def history_since(self, thread, since_ms):
        return list(self.history.get(str(thread), []))


@pytest.fixture
def channel(store, enabled):
    enabled(command_chat=COMMAND)
    return store


def _policy(store, thread, policy):
    store.update_thread(ThreadKey.parse(thread), lambda value: {**value, 'replyPolicy': policy})


def _enqueue(store, thread, text, key=None, **kwargs):
    return outbox.enqueue(OWNER, thread, text, idempotency_key=key or text, origin=ORIGIN,
                          store=store, **kwargs)


def test_policy_is_decided_once_at_enqueue(channel, records):
    _policy(channel, GROUP, 'auto')
    _policy(channel, OTHER, 'never')
    command = _enqueue(channel, COMMAND, 'to me')
    auto = _enqueue(channel, GROUP, 'to group')
    draft = _enqueue(channel, 'dx:uid:2002', 'to person')  # default policy: draft
    assert (command.state, auto.state, draft.state) == ('queued', 'queued', 'draft')
    with pytest.raises(ChannelError) as refused:
        _enqueue(channel, OTHER, 'never sent')
    assert refused.value.code == 'reply_policy_never'
    assert not [key for key in records.keys('/outbox/') if 'never' in key]
    # Changing the policy later does not change an enqueued item's decision.
    _policy(channel, GROUP, 'never')
    assert channel.outbox_item(auto.id)[0].state == 'queued'


def test_disclosure_label_only_in_third_party_threads(channel):
    _policy(channel, GROUP, 'auto')
    assert _enqueue(channel, COMMAND, 'mine').text == 'mine'
    assert _enqueue(channel, GROUP, 'theirs').text == f'{DISCLOSURE_LABEL} theirs'


def test_idempotency_key_replays_or_refuses_changed_content(channel):
    first = _enqueue(channel, COMMAND, 'hello', key='k')
    assert _enqueue(channel, COMMAND, 'hello', key='k').id == first.id
    with pytest.raises(ChannelError) as conflict:
        _enqueue(channel, COMMAND, 'changed', key='k')
    assert conflict.value.code == 'idempotency_conflict'


def test_executor_sends_per_thread_in_order_and_records_echo_ids(channel):
    _policy(channel, GROUP, 'auto')
    a = _enqueue(channel, COMMAND, 'one')
    b = _enqueue(channel, COMMAND, 'two')
    c = _enqueue(channel, GROUP, 'three')
    sender = Sender()
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 3
    by_thread = {}
    for thread, text, marker, _ in sender.sent:
        by_thread.setdefault(thread, []).append(text)
    assert by_thread[COMMAND] == ['one', 'two']
    for item in (a, b, c):
        stored = channel.outbox_item(item.id)[0]
        assert stored.state == 'sent' and stored.external_id
        assert channel.echo_item('dx', external_id=stored.external_id) == item.id
    assert channel.active_outbox() == []
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 0  # exactly once


def test_drafts_and_failures_do_not_block_but_unknown_blocks_only_its_thread(channel):
    _policy(channel, GROUP, 'auto')
    draft = _enqueue(channel, 'dx:uid:2002', 'needs approval')
    blocked = _enqueue(channel, COMMAND, 'lost ack')
    later = _enqueue(channel, COMMAND, 'after it')
    elsewhere = _enqueue(channel, GROUP, 'independent')
    sender = Sender()
    sender.results['lost ack'] = {'ok': False, 'clientId': 'uuid-lost',
                                  'error': {'code': 'ack_timeout'}}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert channel.outbox_item(blocked.id)[0].state == 'unknown'
    assert channel.outbox_item(later.id)[0].state == 'queued'  # waits behind the unknown item
    assert channel.outbox_item(elsewhere.id)[0].state == 'sent'
    assert channel.outbox_item(draft.id)[0].state == 'draft'
    assert [text for _, text, _, _ in sender.sent].count('lost ack') == 1


def test_unknown_result_found_in_history_is_never_resent(channel):
    item = _enqueue(channel, COMMAND, 'maybe sent')
    sender = Sender()
    sender.results['maybe sent'] = RuntimeError('bridge died mid-send')
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert channel.outbox_item(item.id)[0].state == 'unknown'
    sender.history[COMMAND] = [make_event(COMMAND, '7200000000000000001', sender=OWNER_UID,
                                          outbox_marker=item.id)]
    sender.results.clear()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    stored = channel.outbox_item(item.id)[0]
    assert stored.state == 'sent' and stored.external_id == '7200000000000000001'
    assert [text for _, text, _, _ in sender.sent].count('maybe sent') == 1


def test_unknown_result_missing_from_history_is_resent_with_the_same_marker(channel):
    item = _enqueue(channel, COMMAND, 'retry me')
    sender = Sender()
    sender.results['retry me'] = {'ok': False, 'error': {'code': 'ack_timeout'}}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    sender.results.clear()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert channel.outbox_item(item.id)[0].state == 'sent'
    markers = [marker for _, text, marker, _ in sender.sent if text == 'retry me']
    assert markers == [item.id, item.id]


def test_definite_provider_refusal_fails_without_blocking(channel):
    first = _enqueue(channel, COMMAND, 'bad')
    second = _enqueue(channel, COMMAND, 'good')
    sender = Sender()
    sender.results['bad'] = {'ok': False, 'error': {'code': 'invalid_input'}}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert channel.outbox_item(first.id)[0].state == 'failed'
    assert channel.outbox_item(second.id)[0].state == 'sent'
    assert outbox.discard(OWNER, first.id, store=channel).state == 'discarded'


def test_draft_approval_queues_and_discard_removes(channel):
    keep = _enqueue(channel, 'dx:uid:2002', 'approve me')
    drop = _enqueue(channel, 'dx:uid:2002', 'discard me')
    sender = Sender()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert sender.sent == []
    assert outbox.approve(OWNER, keep.id, store=channel).state == 'queued'
    assert outbox.discard(OWNER, drop.id, store=channel).state == 'discarded'
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert [text for _, text, _, _ in sender.sent] == [f'{DISCLOSURE_LABEL} approve me']
    assert outbox.approve(OWNER, keep.id, store=channel).state == 'sent'  # idempotent
    with pytest.raises(ChannelError):
        outbox.discard(OWNER, keep.id, store=channel)


def test_echo_of_an_in_flight_item_confirms_it(channel, router, dispatch, mailbox):
    from lib.channels.ingress import ingest
    item = _enqueue(channel, COMMAND, 'in flight')
    sender = Sender()
    sender.results['in flight'] = {'ok': False, 'error': {'code': 'ack_timeout'}}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    echo = make_event(COMMAND, '7300000000000000001', sender=OWNER_UID, outbox_marker=item.id)
    assert ingest(OWNER, echo, store=channel).outcome == 'echo'
    assert channel.outbox_item(item.id)[0].state == 'sent'
    assert dispatch.calls == []


def test_reply_to_must_be_an_event_of_the_same_thread(channel):
    item = _enqueue(channel, COMMAND, 'quoted', reply_to=f'{COMMAND}/7000000000000000001')
    assert item.extra['replyToExternalId'] == '7000000000000000001'
    with pytest.raises(ChannelError):
        _enqueue(channel, COMMAND, 'cross', reply_to=f'{GROUP}/7000000000000000001')
    sender = Sender()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert sender.sent[0][3] == '7000000000000000001'
