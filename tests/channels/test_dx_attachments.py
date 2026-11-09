"""Outbound 大象 attachments (docs/CHANNELS.md C10): admission, delivery, confirmation, previews.

A fake bridge plays the owner's session (uploads plus native image/file
messages, history), a fake open platform the notification robot. Files live
in a per-test workspace that is the sending conversation's only root. No
network, no 大象 process, no real converter: LibreOffice is a stub script and
the slide renderer a stub, both installed per test.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue

import pytest

from lib.channels import outbox
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
GROUP = 'dx:gid:500'
PNG = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 32)
ORIGIN = {'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'turn-1'}


class Bridge:
    """The owner's session: texts, native image/file messages and thread history."""

    def __init__(self):
        self.events = queue.Queue()
        self.sends: list[tuple[str, str]] = []  # ('text', text) | ('image'|'file', file name)
        self.uploads: list[dict] = []
        self.history: list[dict] = []
        self.results: dict[str, object] = {}  # file name or text -> response or exception
        self.history_failure: dict | None = None  # the engine's answer to every history read while set
        self.mid = 7400000000000000100

    def _next(self):
        self.mid += 1
        return str(self.mid)

    def request(self, op, **args):
        if op in ('send_markdown', 'reply_message'):
            self.sends.append(('text', args['text']))
            scripted = self.results.get(args['text'])
            if isinstance(scripted, Exception):
                raise scripted
            mid = self._next()
            return scripted or {'ok': True, 'result': {'serverMsgId': mid, 'msgUuid': f'uu-{mid}'}}
        if op in ('send_image', 'send_attachment'):
            name = os.path.basename(args['filePath'])
            with open(args['filePath'], 'rb') as handle:
                data = handle.read()
            marker = json.loads(args['extension'])['tofuOutbox']
            self.sends.append(('image' if op == 'send_image' else 'file', name))
            mid = self._next()
            self.uploads.append({'op': op, 'name': name, 'data': data, 'marker': marker, 'mid': mid,
                                 'target': {k: args[k] for k in ('gid', 'uid', 'pid') if k in args}})
            scripted = self.results.get(name)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted or {'ok': True, 'result': {'serverMsgId': mid, 'msgUuid': f'uu-{mid}'}}
        if op == 'history':
            if self.history_failure:
                return self.history_failure
            return {'ok': True, 'result': {'messages': [] if args.get('cursor') else list(self.history),
                                           'nextCursor': '-1'}}
        raise AssertionError(op)

    def alive(self):
        return True

    def show(self):
        """Put the session's own attachment messages into the thread history (what 大象 stored)."""
        for upload in self.uploads:
            self.history.append(row(upload['marker'], upload['name'], mid=upload['mid']))


def row(marker, name, *, mid, sender=OWNER_UID):
    return {'id': mid, 'text': name, 'raw': {'mid': mid, 'from': sender, 'type': 8,
                                             'message': {'name': name},
                                             'extension': json.dumps({'tofuOutbox': marker})}}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'router-home'
    (root / 'out').mkdir(parents=True)
    (root / 'out' / 'chart.png').write_bytes(PNG)
    (root / 'out' / 'report.pdf').write_bytes(b'%PDF-1.7 report')
    (root / 'out' / 'deck.pptx').write_bytes(b'PK\x03\x04 deck')
    (root / 'out' / 'notes.md').write_text('# notes')
    return root


@pytest.fixture
def channel(store, enabled, router, dispatch, mailbox, turns):
    enabled(command_chat=COMMAND)
    return store


def task_for(workspace, conversation=ROUTER, turn='out-1'):
    from lib.identity import PrincipalContext
    principal = PrincipalContext.user(subject_id=f'user:{OWNER}', owner_user_id=OWNER)
    return {'id': 'task-1', 'convId': conversation, '_userId': OWNER, '_turnId': turn,
            'config': {'projectPath': str(workspace)}, '_principalContext': principal.to_payload(),
            '_attended': True}


def admitted(workspace, *paths):
    from lib.channels import attachments
    return attachments.from_tool(task_for(workspace), OWNER, ROUTER, [{'path': p} for p in paths])


def make_adapter(store, bridge):
    from lib.channels.dx.adapter import DxAdapter
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge)
    adapter._bridge = bridge
    adapter._connected.set()
    return adapter


def enqueue(store, workspace, *paths, thread=COMMAND, text='见附件', key='k1'):
    return outbox.enqueue(OWNER, thread, text, idempotency_key=key, origin=ORIGIN,
                          attachments=admitted(workspace, *paths), store=store)


def stored(store, item):
    return store.outbox_item(item.id)[0]


def echo(store, bridge, upload):
    """The owner's connection receives its own attachment message (C2 echo)."""
    from lib.channels.ingress import ingest
    from tests.channels.conftest import make_event
    event = make_event(COMMAND, external_id=upload['mid'], sender=OWNER_UID, text=upload['name'],
                       outbox_marker=upload['marker'])
    return ingest(OWNER, event, store=store)


# ── admission: only this conversation's files, only what 大象 documents ────────

def test_workspace_files_become_records_with_digest_and_kind(workspace):
    image, document = admitted(workspace, 'out/chart.png', str(workspace / 'out' / 'report.pdf'))
    assert (image['name'], image['kind'], image['type'], image['size']) == ('chart.png', 'image', 'image/png',
                                                                            len(PNG))
    assert image['sha256'] == hashlib.sha256(PNG).hexdigest() and image['renditionOf'] is None
    assert (document['kind'], document['type']) == ('file', 'application/pdf')


@pytest.mark.parametrize('path, code', [
    ('../outside.png', 'attachment_path_invalid'),
    ('out/../../outside.png', 'attachment_path_invalid'),
    ('/etc/hostname.txt', 'attachment_outside_workspace'),
    ('out/missing.png', 'attachment_not_found'),
    ('out', 'attachment_type_unsupported'),
    ('out/notes.md', 'attachment_type_unsupported'),
])
def test_paths_outside_the_workspace_or_of_undocumented_types_are_refused(workspace, path, code):
    (workspace.parent / 'outside.png').write_bytes(PNG)
    with pytest.raises(ChannelError) as refused:
        admitted(workspace, path)
    assert refused.value.code == code
    assert str(refused.value)  # actionable text, not just a code


def test_symlinks_never_reach_outside_files(workspace):
    from lib.channels import attachments
    secret = workspace.parent / 'secret.png'
    secret.write_bytes(PNG)
    huge = workspace.parent / 'huge.zip'
    with open(huge, 'wb') as handle:
        handle.truncate(attachments.SIZE_BELOW_BYTES)
    (workspace / 'out' / 'link.png').symlink_to(secret)
    (workspace / 'linked-dir').symlink_to(workspace.parent)
    for path in ('out/link.png', 'linked-dir/secret.png', 'linked-dir/huge.zip'):
        with pytest.raises(ChannelError) as refused:
            admitted(workspace, path)
        assert refused.value.code == 'attachment_symlink'  # never the size of a file outside


def test_absolute_path_outside_every_root_is_refused(workspace, tmp_path):
    other = tmp_path / 'other-project'
    other.mkdir()
    (other / 'a.png').write_bytes(PNG)
    with pytest.raises(ChannelError) as refused:
        admitted(workspace, str(other / 'a.png'))
    assert refused.value.code == 'attachment_outside_workspace'


def test_files_at_or_over_the_documented_size_are_refused_before_hashing(workspace, monkeypatch):
    from lib.channels import attachments
    big = workspace / 'out' / 'big.zip'
    with open(big, 'wb') as handle:
        handle.truncate(attachments.SIZE_BELOW_BYTES)
    read = []
    monkeypatch.setattr('lib.artifacts.file_snapshot.read_project_snapshot',
                        lambda *args, **kwargs: read.append(args) or pytest.fail('hashed'))
    with pytest.raises(ChannelError) as refused:
        admitted(workspace, 'out/big.zip')
    assert refused.value.code == 'attachment_too_large' and '30 MiB' in str(refused.value)
    assert read == []


def test_a_conversation_without_a_workspace_has_nothing_to_attach(workspace):
    from lib.channels import attachments
    task = task_for(workspace)
    task['config'] = {}
    with pytest.raises(ChannelError) as refused:
        attachments.from_tool(task, OWNER, ROUTER, [{'path': 'out/chart.png'}])
    assert refused.value.code == 'attachment_outside_workspace'


def test_a_platform_without_files_gets_each_one_named_in_the_text_instead(store, github_enabled, workspace):
    """Channel protocol P5: degraded, never refused or dropped. The item carries only text, the note is in
    it from the start (a draft shows it), and its records keep the request's idempotency."""
    from lib.channels import attachments
    github_enabled()
    files = attachments.from_tool(task_for(workspace), OWNER, ROUTER,
                                  [{'path': 'out/chart.png'}, {'path': 'out/notes.md'}], carried=False)
    item = outbox.enqueue(OWNER, 'github:octo/repo#1', 'see file', idempotency_key='g', origin=ORIGIN,
                          attachments=files, store=store)
    assert 'attachments' not in item.extra and item.view()['attachments'] == []
    assert [record['name'] for record in item.extra['degradedAttachments']] == ['chart.png', 'notes.md']
    assert item.text == ('[Tofu 代发] see file\n\n（未附上 chart.png：GitHub 不支持发送文件。）\n'
                         '（未附上 notes.md：GitHub 不支持发送文件。）')
    again = outbox.enqueue(OWNER, 'github:octo/repo#1', 'see file', idempotency_key='g', origin=ORIGIN,
                           attachments=files, store=store)
    assert again.id == item.id
    with pytest.raises(ChannelError) as conflict:
        outbox.enqueue(OWNER, 'github:octo/repo#1', 'see file', idempotency_key='g', origin=ORIGIN,
                       attachments=files[:1], store=store)
    assert conflict.value.code == 'idempotency_conflict'


def test_the_send_tool_names_files_a_platform_cannot_carry_without_its_upload_limits(
        channel, github_enabled, workspace):
    """An undocumented 大象 type is only named on GitHub, never refused; it still has to be this
    conversation's file, and an artifact keeps its delivery URL in the note."""
    from lib.channels import tools
    github_enabled()
    result = tools.execute('channel_send', {'thread': 'github:octo/repo#1', 'text': 'notes', 'idempotency_key': 'k',
                                            'attachments': [{'path': 'out/notes.md'}]}, task_for(workspace))
    assert '（未附上 notes.md：GitHub 不支持发送文件。）' in result['item']['text']
    assert result['item']['attachments'] == [] and 'previews' not in result
    answer = json.loads(tools.execute_json(
        'channel_send', {'thread': 'github:octo/repo#1', 'text': 'x', 'idempotency_key': 'k2',
                         'attachments': [{'path': '../../etc/passwd'}]}, task_for(workspace)))
    assert answer['code'] == 'attachment_path_invalid'
    from lib.channels.attachments import degraded_note
    note = degraded_note({'name': 'r.pdf', 'source': {'artifact': 'a1', 'store': 'file'}}, 'GitHub')
    assert note == '（未附上 r.pdf：GitHub 不支持发送文件；在 Tofu 中打开：/api/artifacts/a1/download。）'


# ── delivery: native messages, order, confirmation ───────────────────────────

def test_image_and_file_go_as_native_messages_after_the_text(channel, workspace):
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/chart.png', 'out/report.pdf')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert bridge.sends == [('text', '见附件'), ('image', 'chart.png'), ('file', 'report.pdf')]
    assert [u['data'] for u in bridge.uploads] == [PNG, b'%PDF-1.7 report']
    assert [u['target'] for u in bridge.uploads] == [{'gid': '900'}] * 2
    assert [u['marker'] for u in bridge.uploads] == [f'{item.id}/0', f'{item.id}/1']
    # Accepted by the server is not delivered: the item waits for the thread to show the files.
    waiting = stored(channel, item)
    assert waiting.state == 'unknown' and waiting.error == 'awaiting_confirmation'
    assert [a['state'] for a in waiting.view()['attachments']] == ['pending', 'pending']


def test_echoes_of_the_files_confirm_the_item(channel, workspace):
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/chart.png')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert echo(channel, bridge, bridge.uploads[0]).outcome == 'echo'
    done = stored(channel, item)
    assert done.state == 'sent' and done.view()['attachments'][0]['state'] == 'sent'
    assert done.view()['attachments'][0]['index'] == 0


def test_history_confirms_files_whose_echo_did_not_arrive(channel, workspace):
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    bridge.show()
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert stored(channel, item).state == 'sent'
    assert bridge.sends == [('text', '见附件'), ('file', 'report.pdf')]  # nothing sent twice


def test_a_file_the_server_accepted_but_the_thread_never_shows_fails_without_resend(channel, workspace):
    """The skill's known send-file false success: an ack without delivery is reported, not claimed."""
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    # A later message of the thread is in the history; the accepted file is not.
    bridge.history.append(row('', 'later text', mid=str(bridge.mid + 50), sender='2002'))
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    failed = stored(channel, item)
    assert failed.state == 'failed' and failed.error == 'attachment_failed'
    assert {'state': 'failed', 'error': 'not_delivered'}.items() <= failed.view()['attachments'][0].items()
    assert bridge.sends.count(('file', 'report.pdf')) == 1


def test_clock_skew_never_turns_a_delivered_file_into_a_failure(channel, workspace):
    """大象's clock runs behind Tofu's: the file's message predates the local attempt time."""
    from lib.channels.dx import adapter as dx
    from lib.channels.store import now_ms

    def mid_at(ms):
        return ((ms - dx._MID_EPOCH_MS) << dx._MID_SHIFT)
    bridge = Bridge()
    bridge.mid = mid_at(now_ms() - 10_000)  # server time 10 s behind this host
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    bridge.show()
    bridge.history.append(row('', 'later text', mid=str(mid_at(now_ms() + 20_000)), sender='2002'))
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    done = stored(channel, item)
    assert done.state == 'sent' and done.view()['attachments'][0]['state'] == 'sent'


def test_waiting_for_confirmation_does_not_hold_back_the_thread(channel, workspace):
    bridge = Bridge()
    enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    outbox.enqueue(OWNER, COMMAND, '处理完成', idempotency_key='status', origin=ORIGIN, store=channel)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert bridge.sends[-1] == ('text', '处理完成')


def test_an_unknown_upload_outcome_blocks_the_thread_and_is_found_by_its_marker(channel, workspace):
    bridge = Bridge()
    bridge.results['report.pdf'] = TimeoutError('ack timeout')
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    outbox.enqueue(OWNER, COMMAND, 'next', idempotency_key='next', origin=ORIGIN, store=channel)
    unknown = stored(channel, item)
    assert unknown.state == 'unknown' and unknown.error == 'unknown'
    # The thread waits: the next item is not sent before the file's fate is known.
    bridge.results.pop('report.pdf')
    bridge.history.append(row(f'{item.id}/0', 'report.pdf', mid='7400000000000000777'))
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    done = stored(channel, item)
    assert done.state == 'sent' and done.view()['attachments'][0]['state'] == 'sent'
    assert bridge.sends == [('text', '见附件'), ('file', 'report.pdf'), ('text', 'next')]


def test_an_unknown_upload_absent_from_history_is_sent_again_with_the_same_marker(channel, workspace):
    bridge = Bridge()
    bridge.results['report.pdf'] = TimeoutError('ack timeout')
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    bridge.results.pop('report.pdf')
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert bridge.sends == [('text', '见附件'), ('file', 'report.pdf'), ('file', 'report.pdf')]
    assert {u['marker'] for u in bridge.uploads} == {f'{item.id}/0'}  # the text is not repeated


def test_a_failed_history_read_proves_nothing_and_holds_only_the_uploads_thread(channel, workspace):
    """Only a complete read settles an unknown upload (docs/CHANNEL_PROTOCOL.md P4): while the thread's
    history fails, the file is neither sent again nor settled, and a thread the executor visits after it
    still sends in the same pass (C5)."""
    other = 'dx:uid:2002'
    channel.update_thread(ThreadKey.parse(other), lambda value: {**value, 'replyPolicy': 'auto'})
    bridge = Bridge()
    bridge.results['report.pdf'] = TimeoutError('ack timeout')
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert stored(channel, item).state == 'unknown'
    bridge.results.pop('report.pdf')
    bridge.history_failure = {'ok': False, 'error': {'code': 'browserless_runtime_error',
                                                     'message': 'This operation was aborted'}}
    independent = outbox.enqueue(OWNER, other, 'independent', idempotency_key='other', origin=ORIGIN,
                                 store=channel)
    assert [queued.thread for queued in channel.active_outbox(channel='dx')] == [COMMAND, other]
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert stored(channel, independent).state == 'sent'
    assert stored(channel, item).state == 'unknown'
    assert bridge.sends == [('text', '见附件'), ('file', 'report.pdf'), ('text', '[Tofu 代发] independent')]
    # The file had reached the thread: the first complete read settles it, and it never goes twice.
    bridge.history_failure = None
    bridge.show()
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert stored(channel, item).state == 'sent'
    assert [send for send in bridge.sends if send[0] == 'file'] == [('file', 'report.pdf')]


# The engine's answer to every history read once the bridge's launch-time SSO ticket had expired
# (production, 2026-11-09 from 06:14): the session still sent, but nothing could read the thread.
TOKEN_REFUSAL = {'ok': False, 'error': {'code': 'browserless_runtime_error',
                                        'message': 'DX IM token generation failed: rescode=92002'}}


def _faults(monkeypatch):
    faults = []
    monkeypatch.setattr('lib.router.watchdog.signals.report_outbox_fault',
                        lambda owner, item: faults.append((item.id, item.state, item.error)))
    return faults


def test_an_unreadable_history_holds_no_later_item_of_the_files_thread_or_another(channel, workspace):
    """2026-11-09: the text went out with its message ID, 大象 accepted the PDF, and every history read
    failed. The outbox then running raised out of each pass at that item, so the two items queued behind
    it in the command chat and every thread after it were never sent. Parts that only await confirmation
    never block (C5), and a read that fails holds nothing (P4): the same pass sends all three."""
    other = 'dx:uid:2002'
    channel.update_thread(ThreadKey.parse(other), lambda value: {**value, 'replyPolicy': 'auto'})
    bridge = Bridge()
    bridge.history_failure = TOKEN_REFUSAL
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert (stored(channel, item).state, stored(channel, item).error) == ('unknown', 'awaiting_confirmation')
    behind = [outbox.enqueue(OWNER, COMMAND, text, idempotency_key=text, origin=ORIGIN, store=channel)
              for text in ('论文还要不要坚持新方法？', '结果不明时怎么办？')]
    independent = outbox.enqueue(OWNER, other, 'independent', idempotency_key='other', origin=ORIGIN,
                                 store=channel)
    assert [queued.thread for queued in channel.active_outbox(channel='dx')] == [COMMAND] * 3 + [other]
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert [stored(channel, queued).state for queued in (*behind, independent)] == ['sent'] * 3
    assert stored(channel, item).state == 'unknown'
    assert [send for send in bridge.sends if send[0] == 'file'] == [('file', 'report.pdf')]


def test_an_accepted_file_whose_history_stays_unreadable_is_held_with_a_typed_reason(channel, workspace,
                                                                                     monkeypatch):
    """The 2026-11-09 shape (text sent with its message ID, the file accepted and pending, the item
    unknown) sat silently awaiting_confirmation for good. An unreadable history is no evidence (P4),
    but the next pass is C10's confirmation point: from then on the item is held with the typed reason
    history_failed and reported once (R7), the file is never sent again, and the first readable history
    settles it."""
    faults = _faults(monkeypatch)
    bridge = Bridge()
    bridge.history_failure = TOKEN_REFUSAL
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    shape = stored(channel, item)
    assert (shape.state, shape.error) == ('unknown', 'awaiting_confirmation')
    assert shape.extra['textPart']['state'] == 'sent' and shape.external_id == shape.extra['textPart']['externalId']
    assert {'state': 'pending', 'acked': True}.items() <= shape.extra['attachments'][0].items()
    assert faults == []
    for _ in range(3):
        outbox.execute(OWNER, adapter, channel='dx', store=channel)
    held = stored(channel, item)
    assert (held.state, held.error) == ('unknown', 'history_failed')
    assert {'state': 'pending', 'error': 'history_failed'}.items() <= held.view()['attachments'][0].items()
    assert faults == [(item.id, 'unknown', 'history_failed')]
    bridge.history_failure = None
    bridge.show()
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    done = stored(channel, item)
    assert (done.state, done.error) == ('sent', None)
    assert {'state': 'sent', 'error': None}.items() <= done.view()['attachments'][0].items()
    assert bridge.sends == [('text', '见附件'), ('file', 'report.pdf')]


def test_an_accepted_file_the_readable_history_cannot_place_needs_the_owner(channel, workspace, monkeypatch):
    """A history that answers but shows neither the accepted file nor any later message proves nothing
    either way (C10). From the next pass the item needs the owner's confirmation, is reported once (R7)
    and is never sent again. The reconciler keeps reading, so the first later message settles the file:
    here absent, so it is failed (not_delivered), which a new send may retry."""
    faults = _faults(monkeypatch)
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert (stored(channel, item).state, stored(channel, item).error) == ('unknown', 'awaiting_confirmation')
    for _ in range(3):
        outbox.execute(OWNER, adapter, channel='dx', store=channel)
    held = stored(channel, item)
    assert (held.state, held.error) == ('unknown', 'needs_owner_confirmation')
    assert {'state': 'pending', 'error': 'needs_owner_confirmation'}.items() <= held.view()['attachments'][0].items()
    assert faults == [(item.id, 'unknown', 'needs_owner_confirmation')]
    bridge.history.append(row('', 'later text', mid=str(bridge.mid + 50), sender='2002'))
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    failed = stored(channel, item)
    assert (failed.state, failed.error) == ('failed', 'attachment_failed')
    assert {'state': 'failed', 'error': 'not_delivered'}.items() <= failed.view()['attachments'][0].items()
    assert faults[-1] == (item.id, 'failed', 'attachment_failed')
    assert bridge.sends == [('text', '见附件'), ('file', 'report.pdf')]


def test_resume_after_a_crash_sends_only_the_parts_not_yet_sent(channel, workspace):
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/chart.png', 'out/report.pdf')
    adapter = make_adapter(channel, bridge)

    class Crash(BaseException):
        pass
    original = bridge.request

    def crash_on_report(op, **args):
        if op == 'send_attachment':
            raise Crash()
        return original(op, **args)
    bridge.request = crash_on_report
    with pytest.raises(Crash):
        outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert stored(channel, item).state == 'sending'  # the process died mid-item
    bridge.request = original
    bridge.show()  # 大象 has the text and the image already
    bridge.history.append({'id': '7400000000000000101', 'text': '见附件',
                           'raw': {'mid': '7400000000000000101', 'from': OWNER_UID, 'type': 1,
                                   'message': {'text': '见附件'},
                                   'extension': json.dumps({'tofuOutbox': item.id})}})
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert bridge.sends == [('text', '见附件'), ('image', 'chart.png'), ('file', 'report.pdf')]


def test_an_echo_during_a_resumed_attempt_is_not_lost(channel, workspace, monkeypatch):
    from lib.channels import attachments
    bridge = Bridge()
    bridge.results['report.pdf'] = TimeoutError('ack timeout')
    item = enqueue(channel, workspace, 'out/chart.png', 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    bridge.results.pop('report.pdf')
    chart = bridge.uploads[0]
    read = attachments.read_original

    def read_while_the_chart_echo_arrives(record, *, owner):
        if record['name'] == 'report.pdf':
            echo(channel, bridge, chart)  # the earlier attempt's image shows up meanwhile
        return read(record, owner=owner)
    monkeypatch.setattr(attachments, 'read_original', read_while_the_chart_echo_arrives)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    states = [a['state'] for a in stored(channel, item).view()['attachments']]
    assert states == ['sent', 'pending']  # the confirmation survives the resumed attempt's plan
    assert bridge.sends.count(('image', 'chart.png')) == 1


def test_a_deferred_upload_queues_the_item_again_and_sends_that_file_next_without_a_lookup(channel, workspace):
    """A deferral (channel protocol P4) is no attempt: the part stays unsent, the item is queued again,
    and the next pass sends the file before any history read."""
    bridge = Bridge()
    bridge.results['chart.png'] = {'ok': False, 'error': {'code': 'rate_limited', 'message': 'slow down'}}
    item = enqueue(channel, workspace, 'out/chart.png')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    waiting = stored(channel, item)
    assert (waiting.state, waiting.error) == ('queued', 'rate_limited')
    assert waiting.extra['textPart']['state'] == 'sent'
    part = waiting.extra['attachments'][0]
    assert part['state'] == 'pending' and not part.get('attempted') and not part.get('acked')
    del bridge.results['chart.png']
    ops, request = [], bridge.request
    bridge.request = lambda op, **args: ops.append(op) or request(op, **args)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert ops[0] == 'send_image'
    assert bridge.sends == [('text', '见附件'), ('image', 'chart.png'), ('image', 'chart.png')]


def test_a_definite_upload_refusal_fails_that_file_only(channel, workspace):
    bridge = Bridge()
    bridge.results['chart.png'] = {'ok': False, 'error': {'code': 'invalid_input', 'message': 'upload failed'}}
    item = enqueue(channel, workspace, 'out/chart.png', 'out/report.pdf')
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    echo(channel, bridge, bridge.uploads[-1])
    done = stored(channel, item)
    assert [a['state'] for a in done.view()['attachments']] == ['failed', 'sent']
    assert done.state == 'failed' and done.error == 'attachment_failed'


def test_an_im_server_refusal_after_upload_is_definite(channel, workspace):
    bridge = Bridge()
    bridge.results['report.pdf'] = {'ok': False, 'error': {'code': 'engine_error', 'message': ''},
                                    'result': {'status': 'failed', 'msgUuid': 'u9', 'ack': {'rescode': 5}}}
    item = enqueue(channel, workspace, 'out/report.pdf')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    attachment = stored(channel, item).view()['attachments'][0]
    assert (attachment['state'], attachment['error']) == ('failed', 'attachment_rejected')


def test_a_file_changed_after_it_was_queued_is_not_sent(channel, workspace):
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/report.pdf')
    (workspace / 'out' / 'report.pdf').write_bytes(b'%PDF-1.7 rewritten')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert ('file', 'report.pdf') not in bridge.sends
    attachment = stored(channel, item).view()['attachments'][0]
    assert (attachment['state'], attachment['error']) == ('failed', 'attachment_changed')


def test_third_party_threads_keep_the_label_first_and_need_approval(channel, workspace):
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/chart.png', thread=GROUP, text='数据图')
    assert item.state == 'draft' and item.text == '[Tofu 代发] 数据图'
    adapter = make_adapter(channel, bridge)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    # Drafts wait for the owner; only their notice reaches the command chat (C11).
    [(kind, notice)] = bridge.sends
    assert kind == 'text' and notice.startswith('[待批草稿 ') and '附件：chart.png' in notice
    outbox.approve(OWNER, item.id, store=channel)
    outbox.execute(OWNER, adapter, channel='dx', store=channel)
    assert bridge.sends[1:] == [('text', '[Tofu 代发] 数据图'), ('image', 'chart.png')]
    assert bridge.uploads[0]['target'] == {'gid': '500'}


def test_never_policy_refuses_attachments_too(channel, workspace):
    channel.update_thread(ThreadKey.parse(GROUP), lambda value: {**value, 'replyPolicy': 'never'})
    with pytest.raises(ChannelError) as refused:
        enqueue(channel, workspace, 'out/chart.png', thread=GROUP)
    assert refused.value.code == 'reply_policy_never'


def test_the_idempotency_key_covers_the_attachments(channel, workspace):
    first = enqueue(channel, workspace, 'out/chart.png')
    assert enqueue(channel, workspace, 'out/chart.png').id == first.id
    with pytest.raises(ChannelError) as conflict:
        enqueue(channel, workspace, 'out/report.pdf')
    assert conflict.value.code == 'idempotency_conflict'


# ── previews for the phone ───────────────────────────────────────────────

@pytest.fixture
def slide_renderer(monkeypatch):
    """Tofu's slide renderer as a stub: three white pages for any deck."""
    import pymupdf
    rendered = []

    def render_previews(deck, out_dir, **kwargs):
        rendered.append((deck, out_dir))
        pages = os.path.join(out_dir, 'pages')
        os.makedirs(pages, exist_ok=True)
        entries = []
        for index in range(3):
            path = os.path.join(pages, f'{index + 1:02d}.png')
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 9), False)
            pixmap.clear_with(255)
            pixmap.save(path)
            entries.append({'index': index, 'png': path})
        return {'ok': True, 'pages': entries, 'failed': []}
    monkeypatch.setattr('lib.slides.pptd.parse_deck', lambda manifest: ('deck', manifest))
    monkeypatch.setattr('lib.slides.render_png.render_previews', render_previews)
    return rendered


def tofu_deck(deck_dir, pptx='deck.pptx'):
    """A Tofu deck directory: deck.pptd and page sources, then the pptx exported from them."""
    (deck_dir / 'pages').mkdir(parents=True, exist_ok=True)
    (deck_dir / 'pages' / '01.page').write_text('title: one\n')
    (deck_dir / 'deck.pptd').write_text('title: Q3\n')
    exported = deck_dir / pptx
    exported.write_bytes(b'PK\x03\x04 exported deck')
    newest = max(path.stat().st_mtime_ns for path in (deck_dir / 'deck.pptd', deck_dir / 'pages' / '01.page'))
    os.utime(exported, ns=(newest + 1_000_000, newest + 1_000_000))
    return exported


def test_a_tofu_deck_gets_a_pdf_copy_and_slide_images_after_the_original(channel, workspace, slide_renderer):
    deck_dir = workspace / 'out'
    tofu_deck(deck_dir)
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/deck.pptx')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert slide_renderer[0][0] == ('deck', str(deck_dir / 'deck.pptd'))
    assert bridge.sends == [('text', '见附件'), ('file', 'deck.pptx'), ('file', 'deck.pdf'),
                            ('image', 'deck-1.png'), ('image', 'deck-2.png'), ('image', 'deck-3.png')]
    assert bridge.uploads[1]['data'].startswith(b'%PDF') and bridge.uploads[2]['data'].startswith(b'\x89PNG')
    import pymupdf
    with pymupdf.open(stream=bridge.uploads[1]['data'], filetype='pdf') as pdf:
        assert pdf.page_count == 3
    views = stored(channel, item).view()['attachments']
    assert [(v['name'], v['renditionOf'], v['kind']) for v in views] == [
        ('deck.pptx', None, 'file'), ('deck.pdf', 0, 'file'), ('deck-1.png', 0, 'image'),
        ('deck-2.png', 0, 'image'), ('deck-3.png', 0, 'image')]
    assert stored(channel, item).text == '见附件'  # previews made: nothing to explain


def test_a_deck_changed_after_export_gets_no_misleading_preview(channel, workspace, slide_renderer):
    deck_dir = workspace / 'out'
    exported = tofu_deck(deck_dir)
    later = exported.stat().st_mtime_ns + 1_000_000
    os.utime(deck_dir / 'pages' / '01.page', ns=(later, later))  # edited without a new export
    bridge = Bridge()
    enqueue(channel, workspace, 'out/deck.pptx')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert slide_renderer == []
    assert bridge.sends == [
        ('text', '见附件\n\n（deck.pptx 未附预览副本：它的 Tofu 幻灯片源文件在导出后改过，预览会与文件不符。）'),
        ('file', 'deck.pptx')]


def test_without_a_converter_the_original_goes_and_the_text_says_so(channel, workspace, slide_renderer):
    (workspace / 'out' / 'memo.docx').write_bytes(b'PK\x03\x04 memo')
    bridge = Bridge()
    item = enqueue(channel, workspace, 'out/deck.pptx', 'out/memo.docx')
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    notes = ['（deck.pptx 未附预览副本：Tofu 服务器上没有可用的文档转换器。）',
             '（memo.docx 未附预览副本：Tofu 服务器上没有可用的文档转换器。）']
    assert bridge.sends == [('text', '见附件\n\n' + '\n'.join(notes)), ('file', 'deck.pptx'), ('file', 'memo.docx')]
    assert stored(channel, item).text.endswith(notes[-1]) and slide_renderer == []


def test_the_send_tool_reports_the_preview_plan(channel, workspace):
    from lib.channels import tools
    tofu_deck(workspace / 'slides', 'q3.pptx')
    result = tools.execute('channel_send', {'thread': COMMAND, 'text': 'deck', 'idempotency_key': 'k',
                                            'attachments': [{'path': 'out/deck.pptx'},
                                                            {'path': 'slides/q3.pptx'}]}, task_for(workspace))
    assert [a['name'] for a in result['item']['attachments']] == ['deck.pptx', 'q3.pptx']
    plain, deck = result['previews']
    assert plain['name'] == 'deck.pptx' and plain['preview'].startswith('none (')
    assert deck['name'] == 'q3.pptx' and deck['preview'].startswith('a PDF copy and one image per slide')


# ── slide decks this conversation produced (/api/v4/slides/<task>/file) ────────

@pytest.fixture
def slides_jobs(monkeypatch, tmp_path):
    """The slides job root with one finished deck per (task, conversation)."""
    from lib.production.jobs import write_manifest
    root = tmp_path / 'slides-root'
    monkeypatch.setattr('lib.slides.engine.slides_root', lambda: str(root))

    def job(task_id, conversation, owner=OWNER):
        workdir = root / 'jobs' / task_id
        tofu_deck(workdir / 'deck', '季度复盘.pptx')
        write_manifest(str(workdir), {'task_id': task_id, 'conv_id': conversation, 'user_id': owner,
                                      'workdir': str(workdir)},
                       fields=('task_id', 'conv_id', 'user_id', 'workdir'), kind='slides-deck', state='done',
                       log_label='Slides')
        return workdir
    return job


def test_a_slide_deck_of_this_conversation_is_attached_by_its_download_url(channel, workspace, slides_jobs,
                                                                           slide_renderer):
    workdir = slides_jobs('deck1', ROUTER)
    [record] = admitted(workspace, '/api/v4/slides/deck1/file')
    assert (record['name'], record['kind']) == ('季度复盘.pptx', 'file')
    assert record['source']['path'] == str(workdir / 'deck' / '季度复盘.pptx')
    bridge = Bridge()
    outbox.enqueue(OWNER, COMMAND, '演示文稿', idempotency_key='deck', origin=ORIGIN, attachments=[record],
                   store=channel)
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert bridge.sends[:3] == [('text', '演示文稿'), ('file', '季度复盘.pptx'), ('file', '季度复盘.pdf')]
    assert [name for kind, name in bridge.sends[3:]] == ['季度复盘-1.png', '季度复盘-2.png', '季度复盘-3.png']


@pytest.mark.parametrize('task_id, conversation, owner', [
    ('other-conv', 'conv-elsewhere', OWNER), ('other-owner', ROUTER, OWNER + 1)])
def test_a_slide_deck_of_another_conversation_or_owner_is_refused(workspace, slides_jobs, task_id,
                                                                  conversation, owner):
    slides_jobs(task_id, conversation, owner=owner)
    for reference in (f'/api/v4/slides/{task_id}/file', '/api/v4/slides/missing/file'):
        with pytest.raises(ChannelError) as refused:
            admitted(workspace, reference)
        assert refused.value.code == 'attachment_not_found'


def test_a_final_answer_attaches_the_slide_deck_it_links(channel, workspace, slides_jobs, reply_origin):
    from lib.channels import autoreply
    slides_jobs('deck1', ROUTER)
    slides_jobs('theirs', 'conv-elsewhere')
    content = '做好了：[季度复盘](/api/v4/slides/deck1/file)，另见 [别人的](/api/v4/slides/theirs/file)'
    autoreply.on_task_terminal({**task_for(workspace), 'status': 'done', 'finishReason': 'stop', 'content': content})
    reply, _ = channel.active_outbox(COMMAND)
    assert [a['name'] for a in reply.view()['attachments']] == ['季度复盘.pptx'] and reply.text == content


@pytest.fixture
def projects(monkeypatch, tmp_path):
    """The owner's registered projects (get_recent_projects): one real root, and one registered through a link."""
    root = tmp_path / 'projects' / 'deck-lane'
    (root / 'out').mkdir(parents=True)
    (root / 'out' / 'report.pdf').write_bytes(b'%PDF-1.7 project report')
    linked = tmp_path / 'projects-link'
    linked.symlink_to(tmp_path / 'projects')
    registered = [str(root), str(linked / 'deck-lane')]
    monkeypatch.setattr('lib.project_mod.config.get_recent_projects',
                        lambda *, user_id: [{'path': path} for path in registered] if user_id == OWNER else [])
    return root, linked / 'deck-lane'


def test_a_registered_projects_file_is_attached_with_the_project_as_provenance(channel, workspace, projects):
    """Channel protocol P13: how a project's deliverable reaches a chat, through the Global Router."""
    from lib.channels import tools
    root, _ = projects
    result = tools.execute('channel_send', {
        'thread': COMMAND, 'text': '报告', 'idempotency_key': 'p1',
        'attachments': [{'path': 'out/report.pdf', 'project': str(root)},
                        {'path': str(root / 'out' / 'report.pdf'), 'project': str(root) + '/'}]}, task_for(workspace))
    [attachment] = result['item']['attachments']
    assert (attachment['name'], attachment['project']) == ('report.pdf', str(root))
    bridge = Bridge()
    outbox.execute(OWNER, make_adapter(channel, bridge), channel='dx', store=channel)
    assert bridge.sends == [('text', '报告'), ('file', 'report.pdf')]
    assert bridge.uploads[0]['data'] == b'%PDF-1.7 project report'


def test_a_project_registered_through_a_link_resolves_once_and_stays_no_follow_inside(workspace, projects):
    from lib.channels import attachments
    root, linked = projects
    [record] = attachments.from_tool(task_for(workspace), OWNER, ROUTER,
                                     [{'path': str(linked / 'out' / 'report.pdf'), 'project': str(linked)}])
    assert record['source'] == {'path': str(root / 'out' / 'report.pdf'), 'roots': [str(root)],
                                'project': str(linked)}
    (root / 'out' / 'escape.pdf').symlink_to(workspace / 'out' / 'report.pdf')
    with pytest.raises(ChannelError) as refused:
        attachments.from_tool(task_for(workspace), OWNER, ROUTER,
                              [{'path': 'out/escape.pdf', 'project': str(root)}])
    assert refused.value.code == 'attachment_symlink'


@pytest.mark.parametrize('entry, code', [
    ({'path': 'out/report.pdf', 'project': '/srv/not-registered'}, 'attachment_project_unknown'),
    ({'path': 'out/missing.pdf', 'project': None}, 'invalid_attachments'),
    ({'path': '/etc/hosts.txt'}, 'attachment_outside_workspace'),
    ({'path': 'out/report.pdf', 'project': 'deck-lane', 'extra': 1}, 'invalid_attachments'),
])
def test_every_send_tool_attachment_refusal_names_the_supported_path(channel, workspace, projects, entry, code):
    """On 2026-11-08 a refusal that named no supported path sent a Worker looking for a second login."""
    from lib.channels import tools
    answer = json.loads(tools.execute_json('channel_send', {
        'thread': COMMAND, 'text': 'x', 'idempotency_key': 'k', 'attachments': [entry]}, task_for(workspace)))
    assert (answer['executed'], answer['code']) == (False, code)
    assert '"project"' in answer['error'] and 'get_router_projects' in answer['error']
    assert 'asks it to send; nothing sends through a platform login of its own' in answer['error']
    assert channel.active_outbox(COMMAND) == []


def test_the_send_tool_returns_refusals_as_actionable_results(channel, workspace):
    from lib.channels import tools
    answer = json.loads(tools.execute_json(
        'channel_send', {'thread': COMMAND, 'text': 'x', 'idempotency_key': 'k',
                         'attachments': [{'path': '../../etc/passwd.txt'}]}, task_for(workspace)))
    assert answer['executed'] is False and answer['code'] == 'attachment_path_invalid'
    assert channel.active_outbox(COMMAND) == []  # nothing recorded


# ── the final answer to a command-chat message carries what it links ─────────

@pytest.fixture
def reply_origin(monkeypatch):
    rows = {(ROUTER, 'in-1'): {'conversationId': ROUTER, 'turnId': 'in-1', 'actor': 'human',
                               'projection': {'content': 'q', 'origin': {
                                   'blockId': 'origin', 'initiator': 'human',
                                   'channel': {'channel': 'dx', 'thread': COMMAND,
                                               'eventIds': [f'{COMMAND}/1']}}}},
            (ROUTER, 'out-1'): {'conversationId': ROUTER, 'turnId': 'out-1', 'actor': 'assistant',
                                'parentTurnId': 'in-1'}}
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conv, turn, *, user_id, include_projection=True: rows.get((conv, turn)))


def test_a_final_answer_attaches_only_the_workspace_files_it_links(channel, workspace, reply_origin):
    from lib.channels import autoreply
    secret = workspace.parent / 'secret.png'
    secret.write_bytes(PNG)
    (workspace / 'out' / 'link.png').symlink_to(secret)
    answer = '\n'.join([
        '季度复盘已完成：[演示文稿](out/deck.pptx)，概览图：![chart](<out/chart.png> "概览")',
        '再看一遍 [chart again](out/chart.png) 和 [官网](https://example.com/logo.png)。',
        '外部文件 [x](../secret.png)、[link](out/link.png)、[missing](out/gone.pdf)、[note](out/notes.md)',
        '[ghost](out/ghost.md) [elsewhere](/etc/hosts.md)',
        '`[inline](out/report.pdf)`',
        '```', '[fenced](out/report.pdf)', '```',
    ])
    task = {**task_for(workspace), 'status': 'done', 'finishReason': 'stop', 'content': answer}
    autoreply.on_task_terminal(task)
    reply, status = channel.active_outbox(COMMAND)
    assert reply.origin['kind'] == 'router_reply' and status.origin['kind'] == 'router_status'
    assert [a['name'] for a in reply.view()['attachments']] == ['deck.pptx', 'chart.png']
    assert reply.text.startswith(answer)
    notes = reply.text.split(answer, 1)[1]
    assert 'notes.md' in notes  # a workspace file 大象 does not accept is explained
    for absent in ('secret', 'ghost', 'hosts', 'link.png', 'gone.pdf'):
        assert absent not in notes  # not a file of this workspace: silently left alone


def test_a_final_answer_never_decodes_artifacts_it_links(channel, workspace, reply_origin, monkeypatch):
    from lib.channels import autoreply
    monkeypatch.setattr('lib.artifacts.response_files.load_file',
                        lambda *args, **kwargs: pytest.fail('artifact read on the terminal seam'))
    monkeypatch.setattr('lib.response_assets.load_asset',
                        lambda *args, **kwargs: pytest.fail('artifact read on the terminal seam'))
    content = '报告 [pdf](/api/artifacts/a1/download) 与 ![图](/api/response-assets/a2) 以及 [文件](out/report.pdf)'
    autoreply.on_task_terminal({**task_for(workspace), 'status': 'done', 'finishReason': 'stop', 'content': content})
    reply, _ = channel.active_outbox(COMMAND)
    assert [a['name'] for a in reply.view()['attachments']] == ['report.pdf'] and reply.text == content


def test_a_final_answer_without_links_or_workspace_stays_text_only(channel, workspace, reply_origin):
    from lib.channels import autoreply
    task = {**task_for(workspace), 'status': 'done', 'finishReason': 'stop', 'content': 'plain [web](https://example.com/a.pdf)'}
    autoreply.on_task_terminal(task)
    reply, _ = channel.active_outbox(COMMAND)
    assert reply.view()['attachments'] == [] and reply.text == 'plain [web](https://example.com/a.pdf)'


def test_attachment_trouble_never_drops_the_final_answer(channel, workspace, reply_origin, monkeypatch):
    from lib.channels import autoreply
    monkeypatch.setattr('lib.channels.attachments.for_final_answer',
                        lambda *args: (_ for _ in ()).throw(RuntimeError('roots')))
    autoreply.on_task_terminal({**task_for(workspace), 'status': 'done', 'finishReason': 'stop', 'content': 'see [d](out/deck.pptx)'})
    reply, _ = channel.active_outbox(COMMAND)
    assert reply.text == 'see [d](out/deck.pptx)' and reply.view()['attachments'] == []


def test_another_conversations_artifact_is_never_attached(channel, workspace, monkeypatch):
    from lib.artifacts.core import ArtifactNotFoundError
    asked = []

    def load_file(artifact_id, *, user_id, conv_id=None):
        asked.append((artifact_id, user_id, conv_id))
        if conv_id != ROUTER or artifact_id != 'mine':
            raise ArtifactNotFoundError('Unavailable')
        return b'%PDF-1.7 published', {'filename': 'published.pdf'}
    monkeypatch.setattr('lib.artifacts.response_files.load_file', load_file)
    [record] = admitted(workspace, '/api/artifacts/mine/download')
    assert (record['name'], record['size']) == ('published.pdf', len(b'%PDF-1.7 published'))
    with pytest.raises(ChannelError) as refused:
        admitted(workspace, '/api/artifacts/theirs/download')
    assert refused.value.code == 'attachment_not_found'
    assert {call[2] for call in asked} == {ROUTER}  # always scoped to the sending conversation


# ── the owner's view: drafts list their attachments, images have thumbnails ─────

from tests.channels.test_routes import _validate, app  # noqa: E402,F401  (the HTTP fixture)


def test_a_draft_lists_its_attachments_and_serves_image_thumbnails(app, channel, workspace):  # noqa: F811
    import asyncio
    item = enqueue(channel, workspace, 'out/chart.png', 'out/report.pdf', thread=GROUP)
    headers = {'X-Test-Owner': str(OWNER)}

    async def exercise():
        client = app.test_client()
        listing = (await (await client.get('/api/v4/channels/outbox?channel=dx', headers=headers)).get_json())
        _validate(listing['data'], 'OutboxPage')
        [draft] = [entry for entry in listing['data']['items'] if entry['id'] == item.id]
        assert draft['state'] == 'draft' and [
            (a['name'], a['type'], a['size'], a['kind'], a['state']) for a in draft['attachments']] == [
            ('chart.png', 'image/png', len(PNG), 'image', 'pending'),
            ('report.pdf', 'application/pdf', len(b'%PDF-1.7 report'), 'file', 'pending')]
        thumbnail = await client.get(f'/api/v4/channels/outbox/{item.id}/attachments/0', headers=headers)
        assert thumbnail.status_code == 200 and thumbnail.content_type == 'image/png'
        assert await thumbnail.get_data() == PNG
        assert thumbnail.headers['X-Content-Type-Options'] == 'nosniff'
        assert (await client.get(f'/api/v4/channels/outbox/{item.id}/attachments/1',
                                 headers=headers)).status_code == 404  # documents have no thumbnail
        assert (await client.get(f'/api/v4/channels/outbox/{item.id}/attachments/0',
                                 headers={'X-Test-Owner': str(OWNER + 1)})).status_code == 404
        assert (await client.get(f'/api/v4/channels/outbox/{item.id}/attachments/0')).status_code == 401
        (workspace / 'out' / 'chart.png').write_bytes(PNG + b'changed')
        changed = await client.get(f'/api/v4/channels/outbox/{item.id}/attachments/0', headers=headers)
        assert changed.status_code == 409
    asyncio.run(exercise())


# ── the notification robot: files first, its text (the alert) last ─────────────

def test_with_a_robot_the_files_come_before_the_text_that_notifies(store, enabled, router, dispatch, mailbox,
                                                                   vault, workspace):
    from tests.channels.test_dx_robot import CLIENT_ID, SECRET, FakeOpenPlatform, bind
    from lib.channels.dx import robot as robot_module
    enabled(command_chat=COMMAND)
    entry = f'channel.u{OWNER}.dx.robot'
    vault.set_entry(entry, SECRET)
    store.update_account('dx', lambda account: {
        **account, 'robot': {'clientId': CLIENT_ID, 'credential': {'name': entry, 'managed': True}}})
    bind(store, COMMAND)
    platform = FakeOpenPlatform(robot_module)
    bridge = Bridge()
    from lib.channels.dx.adapter import DxAdapter
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge,
                        robot_factory=lambda client_id, secret: robot_module.DxRobot(
                            client_id, secret, transport=platform, clock=lambda: 0.0,
                            wall_clock=lambda: platform.now, sleep=lambda seconds: None))
    adapter._bridge = bridge
    adapter._self_uid = OWNER_UID  # the uid the live session logged in as
    adapter._connected.set()
    item = enqueue(store, workspace, 'out/chart.png', 'out/report.pdf', text='报告好了')
    outbox.execute(OWNER, adapter, channel='dx', store=store)
    assert bridge.sends == [('image', 'chart.png'), ('file', 'report.pdf')]  # the owner's session
    assert platform.sent_texts() == ['报告好了']  # the robot's post, after the files
    for upload in bridge.uploads:
        echo(store, bridge, upload)
    assert stored(store, item).state == 'sent'
