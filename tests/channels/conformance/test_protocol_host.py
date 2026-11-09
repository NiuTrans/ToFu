"""The core's endpoint of an adapter session (lib/channels/protocol/host.py) on its own.

Specifies the core behaviours behind the conformance cases at the protocol
boundary itself, with a minimal in-process adapter: what an envelope's facts
make of a message, and what the core does with receipts and health, without
any platform fake in between.
"""
from __future__ import annotations

import pytest

from lib.channels.model import ThreadKey
from lib.channels.protocol.adapter import ProtocolAdapter
from lib.channels.protocol.contract import adapter_descriptor
from lib.channels.protocol.host import AdapterHost, InProcessLink
from tests.channels.conftest import OWNER, OWNER_UID

pytestmark = pytest.mark.unit

COMMAND = {'kind': 'gid', 'id': '900'}


class Adapter(ProtocolAdapter):
    """A dx-shaped adapter whose session the test drives by hand."""

    def __init__(self, channel='dx'):
        super().__init__()
        self.channel = channel

    def capabilities(self):
        return adapter_descriptor(self.channel)

    def start(self, params):
        return {}


@pytest.fixture
def host(store, enabled, router, dispatch, mailbox):
    enabled(command_chat='dx:gid:900')
    host = AdapterHost(OWNER, 'dx', InProcessLink(Adapter()), store=store)
    host.open()
    return host


def envelope(external_id, text, *, sender=OWNER_UID, thread=COMMAND, echo=None, source='live'):
    event = {'type': 'message', 'eventId': f'{thread["kind"]}:{thread["id"]}/{external_id}', 'thread': thread,
             'message': {'id': external_id, 'seq': external_id, 'timeMs': 1_800_000_000_000},
             'sender': {'id': sender, 'kind': 'user'}, 'parts': [{'type': 'text', 'text': text}],
             'source': source}
    if echo:
        event['echo'] = echo
    return event


def outcomes(host, *events):
    return [result['outcome'] for result in host.events({'events': list(events)})['results']]


def change(kind, target, number, text='', *, thread=COMMAND, sender='2002', seq=True):
    event = {'type': kind, 'eventId': f'{thread["kind"]}:{thread["id"]}/{target}#{kind}{number}', 'thread': thread,
             'message': {'id': f'{target}#{kind}{number}', 'timeMs': 1_800_000_000_500 + number},
             'sender': {'id': sender, 'kind': 'user'},
             'parts': [{'type': 'text', 'text': text}] if text else [], 'source': 'live',
             'target': {'messageId': target, **({'seq': target} if seq else {})}}
    return event


def test_a_host_sent_owner_message_is_an_echo_and_its_replay_stays_one(host, store, dispatch):
    """DX-KICK's hostSent fact crosses the protocol: a send another engine process of this host made as
    the account is never an owner command, also when the catch-up replays it after the engine's
    bounded record forgot it (the 2026-11-09 restore replayed 19 such sends as commands)."""
    sent = envelope('7000000000000000001', 'sent by the skill CLI', echo={'hostSent': True}, source='history')
    assert outcomes(host, sent) == ['echo']
    assert store.echo_item('dx', external_id='7000000000000000001') == '-'
    replay = envelope('7000000000000000001', 'sent by the skill CLI', source='history')
    assert outcomes(host, replay) == ['echo']
    assert dispatch.calls == []
    assert store.event(ThreadKey.parse('dx:gid:900'), '7000000000000000001') is None


GROUP = {'kind': 'gid', 'id': '500'}


def test_an_edit_is_a_revision_of_its_message_never_a_new_message_or_command(host, store, dispatch):
    """P2: the original stays, the edit is recorded once on it, and nothing is routed again
    (an edited owner command is never re-executed)."""
    command = envelope('7000000000000000001', 'summarize the group')
    assert outcomes(host, command) == ['recorded'] and len(dispatch.calls) == 1
    edited = change('edit', '7000000000000000001', 1, 'summarize the group and DM Ann', sender=OWNER_UID)
    assert outcomes(host, edited, edited) == ['recorded', 'duplicate']
    record = store.event(ThreadKey.parse('dx:gid:900'), '7000000000000000001')
    assert record['text'] == 'summarize the group'
    assert [(r['text'], r['senderId']) for r in record['revisions']] == [('summarize the group and DM Ann', OWNER_UID)]
    assert len(dispatch.calls) == 1


def test_a_recall_removes_the_text_and_wins_over_later_edits(host, store, dispatch):
    """P2 (owner default): a recalled message is marked recalled and its text, revisions included,
    leaves Tofu's records; a later edit of it is dropped and a redelivered recall is a duplicate."""
    first = envelope('7000000000000000002', 'my phone number is 138...', sender='2002', thread=GROUP)
    store.update_thread(ThreadKey.parse('dx:gid:500'), lambda value: {**value, 'mode': 'watch'})
    assert outcomes(host, first, change('edit', '7000000000000000002', 1, 'my number: 139...', thread=GROUP),
                    change('recall', '7000000000000000002', 2, thread=GROUP)) == ['recorded', 'recorded', 'recorded']
    record = store.event(ThreadKey.parse('dx:gid:500'), '7000000000000000002')
    assert record['recalled'] is True and record['text'] == '' and record['revisions'] == []
    assert '138' not in str(record) and '139' not in str(record)
    assert outcomes(host, change('recall', '7000000000000000002', 2, thread=GROUP),
                    change('edit', '7000000000000000002', 3, 'again', thread=GROUP)) == ['duplicate', 'dropped']


def test_a_change_of_nothing_recorded_is_dropped_and_an_ignored_thread_stays_metadata_only(
        host, store, records):
    assert outcomes(host, change('edit', '7000000000000000009', 1, 'never seen')) == ['dropped']
    # Without seq the target resolves only through an ID the core minted at first sighting.
    assert outcomes(host, change('recall', '7000000000000000009', 1, seq=False)) == ['dropped']
    ignored = {'kind': 'gid', 'id': '600'}
    assert outcomes(host, change('edit', '7000000000000000010', 1, 'x', thread=ignored)) == ['ignored']
    assert not records.keys('/event/dx:gid:600/')


class BotAdapter(Adapter):
    """A 企业微信-shaped adapter (one bot identity, documented per-chat windows) with scripted receipts."""

    def __init__(self):
        super().__init__('wecom')
        self.intents, self.receipts = [], []

    def send(self, intent):
        self.intents.append(intent)
        scripted = self.receipts.pop(0) if self.receipts else {'status': 'sent', 'platformMessageId': 'm'}
        return {'intentId': intent['intentId'], 'identity': intent['identity'], **scripted}


@pytest.fixture
def bot_host(store, router, dispatch, mailbox, monkeypatch):
    from lib.channels.bots import ids
    command = ids.thread_key('wecom', 'uid', 'owner-1')
    store.update_account('wecom', lambda account: {
        **account, 'enabled': True, 'identity': {'uid': 'owner-1', 'name': 'Owner'}, 'commandChat': str(command)})
    clock = [1_800_000_000_000]
    monkeypatch.setattr('lib.channels.outbox.now_ms', lambda: clock[0])
    adapter = BotAdapter()
    host = AdapterHost(OWNER, 'wecom', InProcessLink(adapter), store=store, clock=lambda: clock[0] / 1000)
    host.open()
    return host, adapter, command, clock


def _send_once(store, host, command, text):
    from lib.channels import outbox
    item = outbox.enqueue(OWNER, str(command), text, idempotency_key=text,
                          origin={'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 't'}, store=store)
    outbox.execute(OWNER, host.sender(), channel='wecom', store=store)
    return store.outbox_item(item.id)[0]


def test_a_rate_limit_refusal_that_names_no_time_waits_out_the_documented_window(bot_host, store):
    """P4/P5: the platform's documented window is a fact the core honours once; 企业微信's shortest
    per-chat window is a minute (x-adapters wecom rateLimits)."""
    host, adapter, command, clock = bot_host
    adapter.receipts.append({'status': 'deferred', 'code': 'rate_limited'})
    item = _send_once(store, host, command, 'too fast')
    assert (item.state, item.extra['notBeforeMs']) == ('queued', clock[0] + 60_000)


def test_a_reported_rate_limit_holds_sends_until_its_resume_time_without_attempting(bot_host, store):
    host, adapter, command, clock = bot_host
    host.health({'state': 'rate_limited', 'code': 'platform_rate_limited', 'resumeAtMs': clock[0] + 5_000,
                 'atMs': clock[0]})
    item = _send_once(store, host, command, 'held')
    assert adapter.intents == []
    assert (item.state, item.error, item.extra['notBeforeMs']) == ('queued', 'rate_limited', clock[0] + 5_000)
    clock[0] += 5_000
    host.health({'state': 'connected', 'code': 'ok', 'atMs': clock[0]})
    from lib.channels import outbox
    outbox.execute(OWNER, host.sender(), channel='wecom', store=store)
    assert [intent['part']['text'] for intent in adapter.intents] == ['held']
    assert store.outbox_item(item.id)[0].state == 'sent'


def test_text_beyond_the_identitys_max_text_bytes_is_one_intent_per_chunk(bot_host, store):
    """P4/P5 through the protocol: the core chunks by the chosen identity's descriptor maxTextBytes
    (企业微信's markdown limit, x-adapters wecom), one intent and receipt per chunk; a receipt lost at
    chunk 1 sends chunk 1 once more, then chunk 2, and chunk 0 never again."""
    from lib.channels import outbox
    host, adapter, command, clock = bot_host
    limit = host.descriptor['identities']['bot']['maxTextBytes']
    text = ''.join(f'{n:04d} {"x" * 1000}\n' for n in range(50))  # 50 lines of 1006 bytes: three chunks
    item = outbox.enqueue(OWNER, str(command), text, idempotency_key='long report',
                          origin={'kind': 'tool', 'conversationId': 'router-conv', 'turnId': 't'}, store=store)
    adapter.receipts += [{'status': 'sent', 'platformMessageId': 'm0'}, {'status': 'unknown', 'code': 'answer_lost'}]
    outbox.execute(OWNER, host.sender(), channel='wecom', store=store)
    assert store.outbox_item(item.id)[0].state == 'unknown'
    outbox.execute(OWNER, host.sender(), channel='wecom', store=store)
    assert [intent['intentId'] for intent in adapter.intents] == [f'{item.id}/t{n}' for n in (0, 1, 1, 2)]
    assert all(intent['idempotencyKey'] == intent['intentId'] for intent in adapter.intents)
    parts = [intent['part']['text'] for intent in adapter.intents]
    assert all(len(part.encode('utf-8')) <= limit for part in parts)
    assert parts[1] == parts[2] and parts[0] + parts[1] + parts[3] == text
    assert store.outbox_item(item.id)[0].state == 'sent'


def test_a_markerless_identity_on_a_platform_with_history_gets_no_core_chunking(host, store):
    """P5: 大象's robot (markers false, history read) is reconciled by its exact posted texts on its own send
    path (C6 robotTexts), so the core cuts no marker-reconciled chunks for it; the owner's own identity
    documents no limit."""
    store.update_account('dx', lambda account: {**account, 'robot': {'clientId': 'robot-1'}})
    robot_chat = ThreadKey.parse('dx:gid:900')
    assert host.identity_for(robot_chat) == 'bot' and host.descriptor['identities']['bot']['maxTextBytes']
    assert host.sender().max_text_bytes(robot_chat) is None
    assert host.sender().max_text_bytes(ThreadKey.parse('dx:gid:500')) is None


class HistoryAdapter(Adapter):
    """A dx-shaped adapter whose platform lists threads and pages history as scripted (an exception fails a read)."""

    def __init__(self):
        super().__init__()
        self.thread_list, self.pages = [], []
        self.thread_queries, self.history_queries = [], []
        self.on_threads = None

    def threads(self, query):
        self.thread_queries.append(query)
        if self.on_threads:
            self.on_threads()
        return {'threads': [{'thread': ref, 'updatedMs': None} for ref in self.thread_list]}

    def history(self, query):
        self.history_queries.append(query)
        page = self.pages.pop(0) if self.pages else []
        if isinstance(page, Exception):
            raise page
        return {'events': page}


GROUP_KEY = ThreadKey.parse('dx:gid:500')
CONNECTED = {'state': 'connected', 'code': 'ok', 'atMs': 1_800_000_000_000}


@pytest.fixture
def history_host(store, enabled, router, dispatch, mailbox):
    enabled(command_chat='dx:gid:900')
    store.update_thread(GROUP_KEY, lambda value: {**value, 'mode': 'watch'})
    adapter = HistoryAdapter()
    host = AdapterHost(OWNER, 'dx', InProcessLink(adapter), store=store)
    host.open()
    yield host, adapter
    host.close(2.0)


def caught_up(host):
    from tests.channels.conformance.harness import eventually
    eventually(lambda: not host.catch_up.busy, what='the core catching up')


def test_a_failed_catch_up_is_degraded_keeps_live_delivery_and_the_tick_replays_the_gap(
        history_host, store, dispatch):
    """P3: a failed read keeps the session; health is degraded/history_failed with no content; live
    events still route but leave the watermark to the replay; the tick replays the gap; the frontier
    moves only when a catch-up completes."""
    from lib.channels.protocol.contract import ERRORS, ProtocolError
    host, adapter = history_host
    gap = envelope('7000000000000000005', 'sent while we were away', sender='2002', thread=GROUP, source='history')
    live = envelope('7000000000000000006', 'live again', sender='2002', thread=GROUP)
    adapter.thread_list = [GROUP]
    adapter.pages = [ProtocolError(ERRORS['platformFailure'], 'history of "sent while we were away" failed'),
                     [gap, {**live, 'source': 'history'}]]
    host.health(CONNECTED)
    caught_up(host)
    health = host.last_health
    assert (health['state'], health['code']) == ('degraded', 'history_failed') and 'away' not in health['detail']
    assert host.catch_up.owed and store.account('dx')['frontierAtMs'] == 1
    assert outcomes(host, live) == ['recorded']
    assert store.thread(GROUP_KEY).get('watermark') is None
    host.tick()
    caught_up(host)
    assert store.event(GROUP_KEY, '7000000000000000005')['state'] == 'routed'
    assert store.thread(GROUP_KEY)['watermark'] == '7000000000000000006'
    assert store.account('dx')['frontierAtMs'] > 1 and not host.catch_up.owed
    assert (host.last_health['state'], host.last_health['code']) == ('connected', 'ok')
    assert adapter.history_queries == [{'thread': GROUP, 'sinceMs': 1}] * 2
    assert len([call for call in dispatch.calls if 'away' in call['message']['content']]) == 1


def test_the_catch_up_reads_after_each_watermark_and_skips_ignored_threads(history_host, store):
    host, adapter = history_host
    store.update_thread(GROUP_KEY, lambda value: {**value, 'watermark': '7000000000000000003'})
    adapter.thread_list = [GROUP, {'kind': 'gid', 'id': '600'}]  # dx:gid:600 is ignored (the group default)
    host.health(CONNECTED)
    caught_up(host)
    assert adapter.thread_queries[0]['sinceMs'] == 1
    assert adapter.history_queries == [{'thread': GROUP, 'afterSeq': '7000000000000000003'}]


def test_a_connection_that_begins_during_a_catch_up_owes_its_own(history_host):
    host, adapter = history_host
    adapter.on_threads = lambda: (setattr(adapter, 'on_threads', None), host.health(CONNECTED))
    host.health(CONNECTED)
    caught_up(host)
    assert len(adapter.thread_queries) == 2 and not host.catch_up.owed


def test_a_closed_session_runs_no_further_catch_up(history_host):
    from lib.channels.protocol.contract import ERRORS, ProtocolError
    host, adapter = history_host
    adapter.thread_list = [GROUP]
    adapter.pages = [ProtocolError(ERRORS['platformFailure'], 'failed')]
    host.health(CONNECTED)
    caught_up(host)
    assert host.close(2.0) and host.catch_up.owed
    host.tick()
    assert not host.catch_up.busy and len(adapter.thread_queries) == 1


def test_changes_apply_in_delivery_order_after_the_messages_before_them(host, store):
    batch = [envelope('7000000000000000003', 'v1', sender='2002', thread=GROUP),
             change('edit', '7000000000000000003', 1, 'v2', thread=GROUP),
             envelope('7000000000000000004', 'next', sender='2002', thread=GROUP)]
    store.update_thread(ThreadKey.parse('dx:gid:500'), lambda value: {**value, 'mode': 'watch'})
    assert outcomes(host, *batch) == ['recorded', 'recorded', 'recorded']
    assert [r['text'] for r in store.event(ThreadKey.parse('dx:gid:500'), '7000000000000000003')['revisions']] == ['v2']
