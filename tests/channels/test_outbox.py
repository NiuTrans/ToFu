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
    return outbox.enqueue(OWNER, thread, text, idempotency_key=key or text,
                          origin=kwargs.pop('origin', ORIGIN), store=store, **kwargs)


def test_policy_is_decided_once_at_enqueue(channel, records):
    _policy(channel, GROUP, 'auto')
    _policy(channel, OTHER, 'never')
    command = _enqueue(channel, COMMAND, 'to me')
    auto = _enqueue(channel, GROUP, 'to group')
    draft = _enqueue(channel, 'dx:uid:2002', 'to person')  # default policy notify: a draft
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


def test_a_deferred_send_holds_its_thread_until_the_platforms_time_then_goes_once(channel, monkeypatch):
    """Channel protocol P4: a deferral is definitely not sent, so it is neither reconciled nor a fault;
    it is not attempted again before retryAfterMs and nothing later in its thread overtakes it."""
    _policy(channel, GROUP, 'auto')
    limited = _enqueue(channel, COMMAND, 'limited')
    behind = _enqueue(channel, COMMAND, 'behind it')
    elsewhere = _enqueue(channel, GROUP, 'independent')
    clock, faults, reads = [1_800_000_000_000], [], []
    monkeypatch.setattr(outbox, 'now_ms', lambda: clock[0])
    monkeypatch.setattr('lib.router.watchdog.signals.report_outbox_fault', lambda owner, item: faults.append(item.id))
    sender = Sender()
    sender.history_since = lambda thread, since_ms: reads.append(str(thread)) or []
    sender.results['limited'] = {'ok': False, 'error': {'code': 'rate_limited'}, 'retryAfterMs': 60_000}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    held = channel.outbox_item(limited.id)[0]
    assert (held.state, held.error, held.extra['notBeforeMs']) == ('queued', 'rate_limited', clock[0] + 60_000)
    assert channel.outbox_item(behind.id)[0].state == 'queued'
    assert channel.outbox_item(elsewhere.id)[0].state == 'sent'
    clock[0] += 59_999
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    command_chat = [text for thread, text, _, _ in sender.sent if thread == COMMAND]
    assert command_chat == ['limited']
    sender.results.clear()
    clock[0] += 1
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    command_chat = [text for thread, text, _, _ in sender.sent if thread == COMMAND]
    assert command_chat == ['limited', 'limited', 'behind it']
    sent = channel.outbox_item(limited.id)[0]
    assert (sent.state, sent.error) == ('sent', None) and 'notBeforeMs' not in sent.extra
    assert faults == [] and reads == []


def test_a_deferral_without_a_time_is_tried_again_at_the_next_pass(channel):
    item = _enqueue(channel, COMMAND, 'offline')
    sender = Sender()
    sender.results['offline'] = {'ok': False, 'error': {'code': 'not_connected'}}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert channel.outbox_item(item.id)[0].state == 'queued'
    sender.results.clear()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert channel.outbox_item(item.id)[0].state == 'sent'
    assert [text for _, text, _, _ in sender.sent] == ['offline', 'offline']


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
    # Nothing reaches the person; the owner gets one notice per draft (C11).
    assert [thread for thread, _, _, _ in sender.sent] == [COMMAND, COMMAND]
    assert outbox.approve(OWNER, keep.id, store=channel).state == 'queued'
    assert outbox.discard(OWNER, drop.id, store=channel).state == 'discarded'
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert [text for thread, text, _, _ in sender.sent if thread != COMMAND] == [f'{DISCLOSURE_LABEL} approve me']
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


def test_router_text_is_translated_in_queue_order_and_saved_before_send(channel, monkeypatch):
    import json
    class Snapshot:
        metadata = {'settings': {'uiLang': 'zh'}}
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', lambda *a, **k: Snapshot())
    calls = []
    def translate(texts, target, *, log_tag, abort_check=None):
        [text] = list(texts)
        calls.append(text)
        return {text: {'first': '第一条', 'second': '第二条'}.get(text, text)}
    monkeypatch.setattr('lib.translate.display_texts.translate_display_texts', translate)
    _enqueue(channel, COMMAND, 'first', key='router-1',
             origin={'kind': 'router_content', 'conversationId': ROUTER, 'turnId': 'out-1'})
    _enqueue(channel, COMMAND, 'second', key='router-2',
             origin={'kind': 'router_reply', 'conversationId': ROUTER, 'turnId': 'out-1'})
    sender = Sender()
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 2
    assert calls == ['first', 'second']
    assert [text for _, text, _, _ in sender.sent] == ['第一条', '第二条']
    from lib.channels.store import ChannelStore
    first_id = ChannelStore.outbox_item_id(OWNER, COMMAND, 'router-1')
    assert channel.outbox_item(first_id)[0].text == '第一条'


def test_router_translation_failure_sends_the_original(channel, monkeypatch):
    class Snapshot:
        metadata = {'settings': {'uiLang': 'zh'}}
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', lambda *a, **k: Snapshot())
    monkeypatch.setattr('lib.translate.display_texts.translate_display_texts',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('translator unavailable')))
    item = _enqueue(channel, COMMAND, 'English progress', key='router-failed',
                    origin={'kind': 'router_progress', 'conversationId': ROUTER, 'turnId': 'out-1'})
    sender = Sender()
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 1
    assert sender.sent[0][1] == 'English progress'
    assert channel.outbox_item(item.id)[0].state == 'sent'


def test_translated_unknown_send_retries_the_same_durable_text(channel, monkeypatch):
    class Snapshot:
        metadata = {'settings': {'uiLang': 'zh'}}
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', lambda *a, **k: Snapshot())
    calls = []
    def translate(texts, target, *, log_tag, abort_check=None):
        calls.append(list(texts))
        return {'English answer': '中文回答'}
    monkeypatch.setattr('lib.translate.display_texts.translate_display_texts', translate)
    item = _enqueue(channel, COMMAND, 'English answer', key='router-unknown',
                    origin={'kind': 'router_reply', 'conversationId': ROUTER, 'turnId': 'out-1'})
    sender = Sender()
    sender.results['中文回答'] = {'ok': False, 'error': {'code': 'ack_timeout'}}
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    sender.results.clear()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert [text for _, text, _, _ in sender.sent] == ['中文回答', '中文回答']
    assert calls == [['English answer']]
    assert channel.outbox_item(item.id)[0].state == 'sent'


@pytest.mark.parametrize('reply_to', [None, f'{GROUP}/7000000000000000001'])
@pytest.mark.parametrize('translations, expected', [
    ({'已完成': 'Done'}, f'{DISCLOSURE_LABEL} Done'),
    ({}, f'{DISCLOSURE_LABEL} 已完成'),  # nothing translated: the original, labelled once
    ({'已完成': f'{DISCLOSURE_LABEL} Done'}, f'{DISCLOSURE_LABEL} Done'),  # already labelled
], ids=['translated', 'untranslated', 'labelled-translation'])
def test_router_text_in_a_labelled_thread_keeps_exactly_one_label(
        channel, monkeypatch, reply_to, translations, expected):
    class Snapshot:
        metadata = {'settings': {'uiLang': 'en'}}
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', lambda *a, **k: Snapshot())
    monkeypatch.setattr('lib.translate.display_texts.translate_display_texts',
                        lambda texts, target, **kwargs: dict(translations))
    _policy(channel, GROUP, 'auto')
    item = _enqueue(channel, GROUP, '已完成', key='router-label', reply_to=reply_to,
                    origin={'kind': 'router_reply', 'conversationId': ROUTER, 'turnId': 'out-1'})
    assert item.text == f'{DISCLOSURE_LABEL} 已完成'
    sender = Sender()
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 1
    [(_, text, _, reply_external)] = sender.sent
    assert text == expected
    assert reply_external == (reply_to.rsplit('/', 1)[1] if reply_to else None)
    assert channel.outbox_item(item.id)[0].text == expected


def test_manual_channel_send_keeps_its_text(channel, monkeypatch):
    monkeypatch.setattr('lib.translate.display_texts.translate_display_texts',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not translate tool text')))
    _enqueue(channel, COMMAND, 'manual English')
    sender = Sender()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert sender.sent[0][1] == 'manual English'


def test_reply_to_must_be_an_event_of_the_same_thread(channel):
    item = _enqueue(channel, COMMAND, 'quoted', reply_to=f'{COMMAND}/7000000000000000001')
    assert item.extra['replyToExternalId'] == '7000000000000000001'
    with pytest.raises(ChannelError):
        _enqueue(channel, COMMAND, 'cross', reply_to=f'{GROUP}/7000000000000000001')
    sender = Sender()
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert sender.sent[0][3] == '7000000000000000001'


class LimitedSender(Sender):
    """A sender whose identity documents a text limit (descriptor ``maxTextBytes``); results scripted per marker."""

    def __init__(self, limit):
        super().__init__()
        self.limit = limit
        self.by_marker = {}

    def max_text_bytes(self, thread):
        return self.limit

    def send(self, thread, text, *, marker, reply_to_external_id):
        if self.by_marker.get(marker):
            self.sent.append((str(thread), text, marker, reply_to_external_id))
            return self.by_marker[marker].pop(0)
        return super().send(thread, text, marker=marker, reply_to_external_id=reply_to_external_id)

    def markers(self, item):
        return [marker for _, _, marker, _ in self.sent if marker.startswith(item.id)]


LONG = ''.join(f'part {n}\n' for n in range(6))  # six 7-byte lines: three 14-byte chunks under a 16-byte limit


def test_text_beyond_the_senders_limit_goes_out_as_chunks_each_its_own_intent(channel):
    """Channel protocol P4/P5: the core chunks a text beyond the sending identity's maxTextBytes, at line
    breaks where possible and otherwise at UTF-8 character boundaries; each chunk is one platform message
    with its own marker ``<item>/t<n>``. A text within the limit stays one message under the item's marker."""
    long_item = _enqueue(channel, COMMAND, LONG)
    short_item = _enqueue(channel, COMMAND, 'fits')
    wide = _enqueue(channel, COMMAND, '中' * 20)  # 60 bytes without a line break
    sender = LimitedSender(16)
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 3
    sent = [(text, marker) for _, text, marker, _ in sender.sent]
    assert sent[:4] == [('part 0\npart 1\n', f'{long_item.id}/t0'), ('part 2\npart 3\n', f'{long_item.id}/t1'),
                        ('part 4\npart 5\n', f'{long_item.id}/t2'), ('fits', short_item.id)]
    assert sent[4:] == [('中' * 5, f'{wide.id}/t{n}') for n in range(4)]
    for item in (long_item, short_item, wide):
        assert channel.outbox_item(item.id)[0].state == 'sent'
    # Each chunk's provider message ID is remembered, so its echo is never input.
    assert channel.echo_item('dx', external_id='7100000000000000002') == f'{long_item.id}/t1'


@pytest.mark.parametrize('failure, found_in_history', [
    ({'ok': False, 'error': {'code': 'ack_timeout'}}, False),
    ({'ok': False, 'error': {'code': 'ack_timeout'}}, True),
    ({'ok': False, 'error': {'code': 'not_connected'}}, False),
], ids=['unknown-then-sent-again', 'unknown-then-found-by-marker', 'deferred'])
def test_a_failure_at_chunk_k_never_sends_an_earlier_chunk_again(channel, failure, found_in_history):
    """A failure while sending chunk k never sends a chunk before k again. Chunk k is found in the history
    by its marker, or sent again once (an unknown send the history does not show, or a deferral), and every
    chunk after it goes out exactly once. The plan made at the first attempt holds on later passes."""
    item = _enqueue(channel, COMMAND, LONG)
    sender = LimitedSender(16)
    t0, t1, t2 = (f'{item.id}/t{n}' for n in range(3))
    sender.by_marker[t1] = [failure]
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    deferred = failure['error']['code'] == 'not_connected'
    assert channel.outbox_item(item.id)[0].state == ('queued' if deferred else 'unknown')
    if found_in_history:
        sender.history[COMMAND] = [make_event(COMMAND, '7200000000000000001', sender=OWNER_UID, outbox_marker=t1)]
    sender.limit = 64
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert sender.markers(item) == ([t0, t1, t2] if found_in_history else [t0, t1, t1, t2])
    assert channel.outbox_item(item.id)[0].state == 'sent'
    assert outbox.execute(OWNER, sender, channel='dx', store=channel) == 0
    assert sender.markers(item) == ([t0, t1, t2] if found_in_history else [t0, t1, t1, t2])


def test_a_refused_chunk_fails_the_item_and_the_rest_is_never_sent(channel, monkeypatch):
    faults = []
    monkeypatch.setattr('lib.router.watchdog.signals.report_outbox_fault', lambda owner, item: faults.append(item.id))
    item = _enqueue(channel, COMMAND, LONG)
    later = _enqueue(channel, COMMAND, 'after it')
    sender = LimitedSender(16)
    sender.by_marker[f'{item.id}/t1'] = [{'ok': False, 'error': {'code': 'invalid_input'}}]
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    stored = channel.outbox_item(item.id)[0]
    assert (stored.state, stored.error) == ('failed', 'invalid_input') and faults == [item.id]
    assert channel.outbox_item(later.id)[0].state == 'sent'  # a refusal does not hold its thread
    outbox.execute(OWNER, sender, channel='dx', store=channel)
    assert sender.markers(item) == [f'{item.id}/t0', f'{item.id}/t1']
