"""The channel protocol's conformance cases (docs/CHANNEL_PROTOCOL.md, Conformance).

Each case states one protocol promise as platform stimuli (driver verbs) and
core observations (what was recorded, routed and received). A case raises
``NotApplicable`` when the platform offers nothing to exercise and
``AssertionError`` when the promise is broken. Cases are written against the
protocol, never against one adapter: the same case runs for every adapter.
"""
from __future__ import annotations

from lib.channels.model import ChannelError
from lib.channels.protocol.contract import health_entry
from tests.channels.conformance.harness import Driver, NotApplicable, attachment_record, record_outbox_faults, unique


def _one(driver: Driver, thread, text):
    records = driver.core.with_text(thread, text)
    assert len(records) == 1, f'{text!r} recorded {len(records)} times'
    return records[0]


def duplicate_delivery(d: Driver) -> None:
    """At-least-once delivery: the platform delivers a message twice; the core records and routes it once."""
    d.connect()
    msg = d.post(d.watched_thread(), unique('duplicate'))
    d.redeliver(msg)
    d.settle()
    record = _one(d, msg.thread, msg.text)
    assert d.core.routed(record) == 1, f'routed {d.core.routed(record)} times'


def out_of_order(d: Driver) -> None:
    """An older message that reaches the core after a newer one of its thread is still recorded and routed."""
    d.connect()
    thread = d.watched_thread()
    older, newer = d.pair(thread)
    d.deliver(newer)
    d.deliver(older)
    d.settle()
    for msg in (older, newer):
        record = _one(d, thread, msg.text)
        assert d.core.routed(record) == 1, f'{msg.text!r} routed {d.core.routed(record)} times'


def edit(d: Driver) -> None:
    """An edit is recorded as a revision of the original event: never dropped, never a new message."""
    d.connect()
    thread = d.watched_thread()
    msg = d.post(thread, unique('original'))
    d.settle()
    revised = unique('edited')
    d.edit(msg, revised)
    d.settle()
    record = _one(d, thread, msg.text)
    assert not d.core.with_text(thread, revised) or record.get('revisions'), 'the edit became a new message'
    assert any(revised in (revision.get('text') or '') for revision in record.get('revisions') or ()), \
        'the edit was not recorded on the original event'


def recall(d: Driver) -> None:
    """A recall marks the original event recalled and removes its text from Tofu (owner default)."""
    d.connect()
    thread = d.watched_thread()
    msg = d.post(thread, unique('recalled'))
    d.settle()
    original = _one(d, thread, msg.text)
    d.recall(msg)
    d.settle()
    record = next((row for row in d.core.events(thread) if row['eventId'] == original['eventId']), None)
    assert record is not None and record.get('recalled') is True, 'the recall was not recorded'
    assert msg.text not in (record.get('text') or ''), 'the recalled text stayed in Tofu'


def auth_expiry(d: Driver) -> None:
    """A refused credential is auth_required with an owner remedy, and the supervisor stops restarting."""
    d.connect()
    d.expire_auth()
    health = d.health()
    assert health and health['state'] == 'auth_required', health
    entry = health_entry(health['code'])
    assert entry['retry'] == 'owner' and entry['remedy'] not in ('none', 'wait'), entry
    d.assert_no_restart()


def displaced_stays_down(d: Driver) -> None:
    """Another login taking the session is displaced: no reconnect or login push, across a restart, until restored."""
    d.connect()
    d.displace()
    health = d.health()
    assert health and health['state'] == 'displaced', health
    entry = health_entry(health['code'])
    assert (entry['retry'], entry['remedy']) == ('owner', 'restore_session'), entry
    d.assert_no_restart()
    d.assert_no_restart(boot=True)


def rate_limited(d: Driver) -> None:
    """A rate-limited send is not attempted again inside the platform's window, then goes out exactly once."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    d.rate_limit_sends()
    item = d.enqueue(thread, unique('limited'))
    d.execute()
    first = d.attempts(item)
    assert first >= 1, 'the send was never attempted'
    d.execute()
    assert d.attempts(item) == first, 'sent again inside the platform\'s rate-limit window'
    d.end_rate_limit()
    d.execute()
    d.execute()
    assert d.received(thread).count(item.text) == 1, d.received(thread)
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state


def partial_history_failure(d: Driver) -> None:
    """A failed history read keeps the connection and live delivery; the gap is replayed later."""
    d.connect()
    thread = d.watched_thread()
    gap = d.post_unseen(thread, unique('gap'))
    d.fail_next_history()
    d.reconnect()
    live = d.post(thread, unique('live'))
    d.settle()
    assert d.alive(), 'a failed history read tore the connection down'
    assert d.core.routed(_one(d, thread, live.text)) == 1, 'live events stopped after a failed history read'
    d.tick()
    d.settle()
    assert d.core.routed(_one(d, thread, gap.text)) == 1, 'the gap was not replayed'


def degraded_is_typed(d: Driver) -> None:
    """A failed history read is reported as degraded (history_failed), not as connected or disconnected."""
    d.connect()
    thread = d.watched_thread()
    d.post_unseen(thread, unique('gap'))
    d.fail_next_history()
    d.reconnect()
    d.settle()
    health = d.health()
    assert health and (health['state'], health['code']) == ('degraded', 'history_failed'), health


def runtime_fault_is_typed(d: Driver) -> None:
    """The adapter's own runtime refusing is runtime_missing: no login is offered (SSO_NODE_UNAVAILABLE lesson)."""
    d.runtime_fault()
    health = d.health()
    assert health and health['state'] == 'runtime_missing', health
    assert health_entry(health['code'])['remedy'] in ('repair_runtime', 'install_adapter', 'update_adapter'), health


def network_fault_is_typed(d: Driver) -> None:
    """A network failure is disconnected with a network or transport code, not an adapter error or a login."""
    d.connect()
    d.network_fault()
    health = d.health()
    assert health and health['state'] == 'disconnected', health
    assert health['code'] in ('network_unreachable', 'transport_lost', 'platform_unavailable'), health


def _reconcilable(d: Driver) -> bool:
    """Whether the platform offers any way to learn the outcome of a send (P4's ladder)."""
    descriptor = d.descriptor
    return bool(descriptor['outbound']['lookup'] or descriptor['history']['read']
                or descriptor['outbound']['idempotentResend'] != 'none')


def _settled_once(d: Driver, thread, item) -> None:
    """Exactly one copy reached the platform; the item is sent, or held unknown for the owner where
    the platform offers no way to reconcile (never re-sent blindly)."""
    copies = d.received(thread).count(item.text)
    assert copies == 1, f'{copies} copies reached the platform'
    expected = 'sent' if _reconcilable(d) else 'unknown'
    assert d.core.item(item.id).state == expected, d.core.item(item.id).state


def restart_mid_send(d: Driver) -> None:
    """The platform accepted a send and the process died before the receipt: after restart, no second copy."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    item = d.enqueue(thread, unique('restart'))
    d.deliver_without_receipt(item)
    d.restart()
    d.execute()
    d.execute()
    _settled_once(d, thread, item)


def unknown_reconciliation(d: Driver) -> None:
    """A delivered send whose answer was lost is reconciled, never sent blindly again."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    d.lose_next_answer()
    item = d.enqueue(thread, unique('unknown'))
    d.execute()
    assert d.core.item(item.id).state == 'unknown', d.core.item(item.id).state
    d.execute()
    d.execute()
    _settled_once(d, thread, item)


def _readable(d: Driver) -> bool:
    """Whether the platform offers a read that could prove a send missing (P4's lookup or history rung)."""
    return bool(d.descriptor['outbound']['lookup'] or d.descriptor['history']['read'])


def reconciliation_read_failure(d: Driver) -> None:
    """A failed or incomplete reconciliation read is no evidence that a send is missing: the item stays
    unknown and nothing is sent again until a complete read settles it (P4). OpenClaw 2026.11.1 replays an
    unknown send only when a fresh send is provably safe (docs/plugins/sdk-channel-outbound.md)."""
    if not _readable(d):
        raise NotApplicable('the platform offers no read that could prove a send missing')
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    d.lose_next_answer()
    item = d.enqueue(thread, unique('unproven'))
    d.execute()
    assert d.core.item(item.id).state == 'unknown', d.core.item(item.id).state
    d.fail_reconciliation_reads(thread)
    d.execute()
    d.execute()
    copies = d.received(thread).count(item.text)
    assert copies == 1, f'{copies} copies after failed reconciliation reads'
    assert d.core.item(item.id).state in ('unknown', 'sending'), d.core.item(item.id).state
    d.heal_reconciliation_reads(thread)
    d.execute()
    _settled_once(d, thread, item)


def stalled_thread_isolation(d: Driver) -> None:
    """An unknown item whose reconciliation read keeps failing holds only its own thread: another thread's
    queued item goes out in the same pass (C5). The stalled thread is the one the executor visits first
    (the store's order: thread key, then queue position), so the stall cannot hide behind the order."""
    if not _readable(d):
        raise NotApplicable('the platform offers no read that could fail during reconciliation')
    d.connect()
    stalled, other = sorted(d.send_threads(), key=str)
    for thread in (stalled, other):
        d.prepare_send(thread)
    d.lose_next_answer()
    held = d.enqueue(stalled, unique('held'))
    d.execute()
    assert d.core.item(held.id).state == 'unknown', d.core.item(held.id).state
    d.fail_reconciliation_reads(stalled)
    item = d.enqueue(other, unique('independent'))
    d.execute()
    assert d.received(other).count(item.text) == 1, 'a stalled reconciliation in another thread held this send'
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state


def accepted_confirmation(d: Driver) -> None:
    """A file the platform accepted (receipt ``accepted``) is confirmed only by its echo or the thread (P4,
    C10). When the next pass cannot confirm it, it never waits silently or forever: a failed read holds it
    as history_failed, a history that answers without it holds it as needs_owner_confirmation, and each
    raises one outbox fault. It is never sent again and holds no other thread, and the first read that
    shows it settles it. On 2026-11-09 an accepted PDF sat awaiting_confirmation for good behind an
    expired SSO ticket."""
    d.connect()
    thread, other = sorted(d.send_threads(), key=str)
    if 'file' not in d.descriptor['identities'][d.host.identity_for(thread)]['attachments']:
        raise NotApplicable('the platform carries no files natively')
    for each in (thread, other):
        d.prepare_send(each)
    faults = record_outbox_faults(d.env)
    d.accept_files_unseen(thread)
    item = d.enqueue(thread, unique('with a file'), attachments=[attachment_record(d.env.tmp_path)])
    d.execute()
    accepted = d.core.item(item.id)
    assert (accepted.state, accepted.error) == ('unknown', 'awaiting_confirmation'), (accepted.state, accepted.error)
    assert faults == [], 'a file awaiting its confirmation point was reported'
    d.fail_reconciliation_reads(thread)
    independent = d.enqueue(other, unique('independent'))
    d.execute()
    held = d.core.item(item.id)
    assert (held.state, held.error) == ('unknown', 'history_failed'), (held.state, held.error)
    assert d.received(other).count(independent.text) == 1, 'an unconfirmed file held another thread'
    d.heal_reconciliation_reads(thread)
    d.execute()
    held = d.core.item(item.id)
    assert (held.state, held.error) == ('unknown', 'needs_owner_confirmation'), (held.state, held.error)
    assert [fault[0] for fault in faults] == [item.id, item.id], faults
    d.show_files(thread)
    d.execute()
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state
    assert d.file_sends(thread) == 1, f'the file went {d.file_sends(thread)} times'


def unknown_keeps_its_identity(d: Driver) -> None:
    """A bot send whose answer was lost is reconciled under the bot and never repeated as the owner: only a
    definite refusal moves the rest of an item to the next identity (P7). OpenClaw 2026.11.1: failure is not
    permission to send the same payload through another path (docs/plugins/sdk-channel-outbound.md)."""
    if not {'bot', 'self'} <= set(d.descriptor['identities']):
        raise NotApplicable('the platform has one outbound identity')
    d.connect()
    thread = d.bot_thread()
    d.prepare_send(thread)
    d.lose_next_bot_answer()
    item = d.enqueue(thread, unique('bot answer lost'))
    d.execute()
    assert d.core.item(item.id).state == 'unknown', d.core.item(item.id).state
    d.execute()
    d.execute()
    senders = [identity for identity, text in d.received_as(thread) if text == item.text]
    assert senders == ['bot'], f'the item reached the platform as {senders}'
    assert d.core.item(item.id).state == 'sent', d.core.item(item.id).state


def attachment_degradation(d: Driver) -> None:
    """A file the platform cannot carry is degraded to a note naming it: never refused, never dropped."""
    d.connect()
    thread = d.send_thread()
    identity = d.descriptor['identities'][d.host.identity_for(thread)]
    if 'file' in identity['attachments']:
        raise NotApplicable('the platform carries files natively')
    d.prepare_send(thread)
    record = attachment_record(d.env.tmp_path)
    try:
        item = d.enqueue(thread, unique('with a file'), attachments=[record])
    except ChannelError as error:
        raise AssertionError(f'refused instead of degraded: {error.code}') from error
    d.execute()
    sent = [text for text in d.received(thread) if item.text.split()[-1] in text]
    assert sent and any(record['name'] in text for text in sent), 'the file was dropped without a note'


def echo_loop(d: Driver) -> None:
    """This account's own send coming back from the platform is never input (no loop)."""
    d.connect()
    thread = d.send_thread()
    d.prepare_send(thread)
    item = d.enqueue(thread, unique('echoed'))
    d.execute()
    assert d.core.item(item.id).state == 'sent'
    d.echo(item)
    d.settle()
    routed = [record for record in d.core.with_text(thread, item.text) if d.core.routed(record)]
    assert not routed, 'an echo of our own send was routed as input'


def self_send_outside_outbox(d: Driver) -> None:
    """A message this account sent through another Tofu path (no outbox item, Tofu's marker), replayed by
    backfill or delivered live, is never owner input: the 2026-11-09 restore replayed 19 such CLI sends
    from the command chat as owner commands."""
    d.connect()
    msg = d.self_send_outside_outbox(unique('sent by a Tofu tool'))
    d.settle()
    for record in d.core.with_text(msg.thread, msg.text):
        assert record.get('disposition') not in ('command', 'candidate'), record.get('disposition')


def other_bots(d: Driver) -> None:
    """Another bot's message is never a command and never a candidate for review or auto-reply."""
    d.connect()
    msg = d.bot_post(unique('bot says'))
    d.settle()
    for record in d.core.with_text(msg.thread, msg.text):
        assert record.get('disposition') not in ('command', 'candidate'), record.get('disposition')


CASES = {
    'duplicate_delivery': duplicate_delivery,
    'out_of_order': out_of_order,
    'edit': edit,
    'recall': recall,
    'auth_expiry': auth_expiry,
    'displaced_stays_down': displaced_stays_down,
    'rate_limited': rate_limited,
    'partial_history_failure': partial_history_failure,
    'degraded_is_typed': degraded_is_typed,
    'runtime_fault_is_typed': runtime_fault_is_typed,
    'network_fault_is_typed': network_fault_is_typed,
    'restart_mid_send': restart_mid_send,
    'unknown_reconciliation': unknown_reconciliation,
    'reconciliation_read_failure': reconciliation_read_failure,
    'stalled_thread_isolation': stalled_thread_isolation,
    'accepted_confirmation': accepted_confirmation,
    'unknown_keeps_its_identity': unknown_keeps_its_identity,
    'attachment_degradation': attachment_degradation,
    'echo_loop': echo_loop,
    'self_send_outside_outbox': self_send_outside_outbox,
    'other_bots': other_bots,
}

__all__ = ['CASES']
