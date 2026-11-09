"""大象 (dx) conformance driver: today's ``DxAdapter`` behind the shim, over a scripted bridge.

``DxWorld`` is 大象 as the bridge reaches it: history rows per thread with
snowflake message IDs, the session list, the owner's sends (their extension
marker kept, as the IM server keeps it) and faults: a lost answer, the engine's
aborted listings (``session_list_failed``, the commonest catch-up failure in
the 2026-11-08 logs), launcher exits. ``DxBridgeFake`` is one bridge process
over that world: an ordered event queue (``status``/``message`` lines) and
request handlers, the interface ``DxAdapter`` drives (``test_dx_adapter.py``
uses the same one). The adapter runs its real ingest thread; its supervisor
runs for exits; the outbox runs through the shim.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
from types import SimpleNamespace

from lib.channels.model import BRIDGE_CONTRACT, OUTBOX_MARKER_FIELD, PROVIDER_LIMITS, ThreadKey
from lib.channels.store import now_ms
from tests.channels.conftest import OWNER, OWNER_UID
from tests.channels.conformance.harness import Driver, Msg, NotApplicable, Shim, eventually

EPOCH = PROVIDER_LIMITS['dxMidEpochMs']
SHIFT = PROVIDER_LIMITS['dxMidTimestampShift']
OTHER_UID = '2002'
ROBOT_UID = '7777'
COMMAND = ThreadKey.parse('dx:gid:900')
GROUP = ThreadKey.parse('dx:gid:500')
CONNECTED = {'type': 'status', 'status': 'connected', 'selfUid': OWNER_UID}
# The engine aborts every HTTP request after 15 s; a listing hit by it fails like this (C1).
SESSIONS_ABORTED = {'ok': False, 'error': {'code': 'session_list_failed', 'message': 'This operation was aborted'}}


def _target(args) -> ThreadKey:
    kind = next(kind for kind in ('gid', 'uid', 'pid') if kind in args)
    return ThreadKey('dx', kind, str(args[kind]))


class DxWorld:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.sent: list[dict] = []
        self.failures: dict[str, list[dict]] = {}
        self.history_down: set[str] = set()
        self.robot_sent: list[dict] = []
        self.files: list[dict] = []
        # Threads whose file messages 大象 accepts but keeps out of the thread, and those messages.
        self.files_unseen: set[str] = set()
        self.unseen: dict[str, list[dict]] = {}
        self.lose_next = False
        self._seq = itertools.count(1)
        self.lock = threading.Lock()

    def mid(self, ms: int | None = None) -> str:
        return str((((ms or now_ms()) - EPOCH) << SHIFT) + next(self._seq))

    def row(self, thread: ThreadKey, *, sender: str, text: str, ms: int | None = None,
            extension: dict | None = None) -> dict:
        mid = self.mid(ms)
        raw = {'mid': mid, 'uuid': 'uu-' + mid, 'from': sender, 'fromName': f'user{sender}', 'cts': 1, 'type': 1,
               'message': {'text': text}, 'extension': json.dumps(extension or {})}
        row = {'id': mid, 'text': text, 'raw': raw}
        with self.lock:
            self.rows.setdefault(str(thread), []).append(row)
        return row

    def send_file(self, thread: ThreadKey, args) -> dict:
        """One file message from the owner's session (C10). 大象 answers with its message ID either way; in
        a thread of ``files_unseen`` the message stays out of the thread (the documented false success)."""
        extension = json.loads(args.get('extension') or '{}')
        name = os.path.basename(str(args['filePath']))
        mid = self.mid()
        raw = {'mid': mid, 'uuid': 'uu-' + mid, 'from': OWNER_UID, 'fromName': f'user{OWNER_UID}', 'cts': 1,
               'type': 8, 'message': {'name': name}, 'extension': json.dumps(extension)}
        row = {'id': mid, 'text': name, 'raw': raw}
        with self.lock:
            held = self.unseen if str(thread) in self.files_unseen else self.rows
            held.setdefault(str(thread), []).append(row)
            self.files.append({'thread': str(thread), 'name': name,
                               'marker': extension.get(OUTBOX_MARKER_FIELD, ''), 'row': row})
        return {'ok': True, 'result': {'serverMsgId': mid, 'msgUuid': raw['uuid']}}

    def sessions(self) -> list[dict]:
        kinds = {'gid': 'group', 'uid': 'chat', 'pid': 'pub'}
        with self.lock:
            return [{'type': kinds[ThreadKey.parse(key).kind], 'id': ThreadKey.parse(key).external_id,
                     'name': key, 'lastMessageServerTime': (int(rows[-1]['id']) >> SHIFT) + EPOCH}
                    for key, rows in self.rows.items() if rows]

    def history(self, args) -> dict:
        """The 大象 range query as observed live (test_dx_adapter.provider_history)."""
        thread = _target(args)
        if str(thread) in self.history_down:
            return {'ok': False, 'error': {'code': 'browserless_runtime_error', 'message': 'This operation was aborted'}}
        with self.lock:
            rows = sorted(self.rows.get(str(thread), []), key=lambda row: int(row['id']))
        bound = int(args['cursor']) if args.get('cursor') else None
        page = [row for row in rows if bound is None or int(row['id']) < bound][-args['limit']:]
        if thread.kind == 'pid':
            page.reverse()
        return {'ok': True, 'result': {'messages': page, 'nextCursor': page[-1]['id'] if page else ''}}


def live(thread: ThreadKey, row: dict, *, own: bool = False, cancel: bool = False) -> dict:
    raw = row['raw']
    message = {'mid': raw['mid'], 'uuid': raw['uuid'], 'from': raw['from'], 'fromName': raw['fromName'],
               'type': 1, 'message': raw['message'], 'extension': raw['extension']}
    if thread.kind == 'gid':
        message['gid'] = thread.external_id
    else:
        message['to'] = OWNER_UID if raw['from'] != OWNER_UID else thread.external_id
    if cancel:
        message['isCancel'] = True
    return {'type': 'message', 'source': 'im', 'message': message, 'ownConnection': own}


class DxRobotFake:
    """The notification robot's open platform (C6): a group post appears in the thread from the robot."""

    def __init__(self, world: DxWorld):
        self.world = world
        self.lose_next = False

    def post_group(self, gid: str, message) -> str:
        from lib.channels.dx.robot import RobotOutcomeUnknown, posted_text
        thread = ThreadKey('dx', 'gid', str(gid))
        row = self.world.row(thread, sender=ROBOT_UID, text=posted_text(message))
        self.world.robot_sent.append({'thread': str(thread), 'text': posted_text(message)})
        if self.lose_next:
            self.lose_next = False
            raise RobotOutcomeUnknown('robot_unknown', 'the open platform answer was lost')
        return row['id']


class DxBridgeFake:
    def __init__(self, world: DxWorld):
        self.world = world
        self.events: queue.Queue = queue.Queue()
        self.returncode = None
        self.refusal = None

    def request(self, op, **args):
        world = self.world
        if world.failures.get(op):
            return world.failures[op].pop(0)
        if op == 'sessions':
            return {'ok': True, 'result': {'sessions': world.sessions()}}
        if op == 'history':
            return world.history(args)
        if op in ('send_markdown', 'reply_message'):
            thread = _target(args)
            extension = json.loads(args.get('extension') or '{}')
            row = world.row(thread, sender=OWNER_UID, text=args['text'], extension=extension)
            world.sent.append({'thread': str(thread), 'text': args['text'],
                               'marker': extension.get(OUTBOX_MARKER_FIELD, ''), 'row': row})
            if world.lose_next:
                world.lose_next = False
                return {'ok': False, 'error': {'code': 'bridge_error', 'message': 'the answer was lost'}}
            return {'ok': True, 'result': {'serverMsgId': row['id'], 'msgUuid': row['raw']['uuid']}}
        if op in ('send_image', 'send_attachment'):
            return world.send_file(_target(args), args)
        raise AssertionError(f'unexpected bridge op {op}')

    def alive(self):
        return self.returncode is None

    def terminate(self):
        self.returncode = -15
        self.events.put(None)

    def close(self, timeout):
        if self.returncode is None:
            self.returncode = 0
        self.events.put(None)
        return True


class DxDriver(Driver):
    name = channel = 'dx'

    def setup(self) -> None:
        from lib.channels.dx import supervisor
        monkeypatch = self.env.monkeypatch
        self.store.update_account('dx', lambda account: {
            **account, 'enabled': True, 'identity': {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'},
            'commandChat': str(COMMAND), 'frontierAtMs': now_ms() - 60_000})
        self.store.update_thread(GROUP, lambda value: {**value, 'mode': 'watch', 'title': 'Team'})
        self.world = DxWorld()
        self.robot = DxRobotFake(self.world)
        # A fresh supervisor (process-local registries) whose launcher counts as installed.
        for name, value in (('_ADAPTERS', {}), ('_CONFIRMING', set()), ('_WORKERS', set()),
                            ('_STOPPING', threading.Event()), ('_HOSTING', threading.Event())):
            monkeypatch.setattr(supervisor, name, value)
        monkeypatch.setattr(supervisor, 'launch_spec', lambda owner: SimpleNamespace(installed=True))
        self.supervisor = supervisor
        self.starts: list = []
        self.shim = Shim(self.env, 'dx', sender_source=lambda: self.adapter)
        self.host = self.shim.host
        self._start()

    def _start(self) -> None:
        from lib.channels.dx.adapter import DxAdapter
        self.bridge = DxBridgeFake(self.world)
        self.adapter = DxAdapter(OWNER, store=self.store, bridge_factory=lambda owner: self.bridge, login_mis='owner',
                                 robot_factory=lambda client_id, secret: self.robot)
        self.adapter._bridge = self.bridge
        self._synced: set[str] = set()
        self._tokens = itertools.count(1)
        handle = self.adapter._on_status

        def on_status(item):
            if str(item.get('status', '')).startswith('sync-'):
                self._synced.add(item['status'])
            else:
                handle(item)
        self.adapter._on_status = on_status
        self.ingest = threading.Thread(target=self.adapter._process, name='dx-conformance-ingest', daemon=True)
        self.ingest.start()
        self.supervisor._ADAPTERS[OWNER] = self.adapter

    def _factory(self, owner, **kwargs):
        """The supervisor's adapter factory: a restart, counted (no network, no launcher)."""
        from lib.channels.dx.adapter import DxAdapter
        self.starts.append(kwargs)
        bridge = DxBridgeFake(self.world)
        return DxAdapter(owner, bridge_factory=lambda _owner: bridge, **kwargs)

    def _tick(self) -> str:
        return self.supervisor.reconcile_owner(OWNER, store=self.store, adapter_factory=self._factory, stop_timeout=1.0)

    def teardown(self) -> None:
        for adapter in list(self.supervisor._ADAPTERS.values()) + [self.adapter]:
            adapter.stop(1.0)
        self.ingest.join(5)

    def settle(self) -> None:
        if not self.ingest.is_alive():
            return
        token = f'sync-{next(self._tokens)}'
        self.bridge.events.put({'type': 'status', 'status': token})
        eventually(lambda: token in self._synced or not self.ingest.is_alive(), what='the dx ingest thread')

    def connect(self) -> None:
        self.bridge.events.put(dict(CONNECTED))
        self.settle()
        assert self.adapter._connected.is_set()

    def alive(self) -> bool:
        return self.ingest.is_alive() and self.bridge.alive()

    def command_thread(self) -> ThreadKey:
        return COMMAND

    def watched_thread(self) -> ThreadKey:
        return GROUP

    # inbound -----------------------------------------------------------
    def post(self, thread: ThreadKey, text: str, *, sender: str = OTHER_UID) -> Msg:
        row = self.world.row(thread, sender=sender, text=text)
        self.bridge.events.put(live(thread, row))
        return Msg(thread, text, row['id'], {'row': row})

    def redeliver(self, msg: Msg) -> None:
        self.bridge.events.put(live(msg.thread, msg.extra['row']))

    def pair(self, thread: ThreadKey) -> tuple[Msg, Msg]:
        older = self.world.row(thread, sender=OTHER_UID, text='older ' + self.world.mid(), ms=now_ms() - 5_000)
        newer = self.world.row(thread, sender=OTHER_UID, text='newer ' + self.world.mid())
        return (Msg(thread, older['text'], older['id'], {'row': older}),
                Msg(thread, newer['text'], newer['id'], {'row': newer}))

    def deliver(self, msg: Msg) -> None:
        self.bridge.events.put(live(msg.thread, msg.extra['row']))

    def edit(self, msg: Msg, text: str) -> None:
        raise NotApplicable('大象 has no message edits (no edit API or event in dx-engine)')

    def recall(self, msg: Msg) -> None:
        row = msg.extra['row']
        row['raw']['isCancel'] = True
        self.bridge.events.put(live(msg.thread, row, cancel=True))

    def bot_post(self, text: str) -> Msg:
        """The robot of another server posting into this account's robot command chat (C6)."""
        self.store.update_account('dx', lambda account: {
            **account, 'robot': {'clientId': 'robot-app', 'credential': {'name': f'channel.u{OWNER}.dx.robot'}}})
        return self.post(COMMAND, text, sender=ROBOT_UID)

    # history -----------------------------------------------------------
    def post_unseen(self, thread: ThreadKey, text: str) -> Msg:
        row = self.world.row(thread, sender=OTHER_UID, text=text)
        return Msg(thread, text, row['id'], {'row': row})

    def fail_next_history(self) -> None:
        self.world.failures.setdefault('sessions', []).append(dict(SESSIONS_ABORTED))

    def reconnect(self) -> None:
        self.bridge.events.put({'type': 'status', 'status': 'disconnected', 'detail': 'status 8 onClose'})
        self.bridge.events.put(dict(CONNECTED))

    def tick(self) -> None:
        self.adapter.tick()

    # outbound ----------------------------------------------------------
    def execute(self) -> None:
        if self.adapter._connected.is_set():
            super().execute()

    def received(self, thread: ThreadKey) -> list[str]:
        return [sent['text'] for sent in self.world.sent if sent['thread'] == str(thread)]

    def lose_next_answer(self) -> None:
        self.world.lose_next = True

    def send_threads(self) -> tuple[ThreadKey, ThreadKey]:
        self.store.update_thread(GROUP, lambda value: {**value, 'replyPolicy': 'auto'})
        return COMMAND, GROUP

    def fail_reconciliation_reads(self, thread: ThreadKey) -> None:
        self.world.history_down.add(str(thread))

    def heal_reconciliation_reads(self, thread: ThreadKey) -> None:
        self.world.history_down.discard(str(thread))

    def accept_files_unseen(self, thread: ThreadKey) -> None:
        self.world.files_unseen.add(str(thread))

    def show_files(self, thread: ThreadKey) -> None:
        with self.world.lock:
            self.world.files_unseen.discard(str(thread))
            self.world.rows.setdefault(str(thread), []).extend(self.world.unseen.pop(str(thread), []))

    def file_sends(self, thread: ThreadKey) -> int:
        return sum(1 for sent in self.world.files if sent['thread'] == str(thread))

    def bot_thread(self) -> ThreadKey:
        """The group command chat with the notification robot bound to this account (C6)."""
        self.store.update_account('dx', lambda account: {
            **account, 'robot': {'clientId': 'robot-app', 'credential': {'name': f'channel.u{OWNER}.dx.robot'}},
            'robotBinding': {'thread': str(COMMAND), 'uid': OWNER_UID, 'proof': 'message'}})
        return COMMAND

    def lose_next_bot_answer(self) -> None:
        self.robot.lose_next = True

    def received_as(self, thread: ThreadKey) -> list[tuple[str, str]]:
        return ([('bot', sent['text']) for sent in self.world.robot_sent if sent['thread'] == str(thread)]
                + [('self', sent['text']) for sent in self.world.sent if sent['thread'] == str(thread)])

    def deliver_without_receipt(self, item) -> None:
        self.claim(item)
        self.adapter.send(ThreadKey.parse(item.thread), item.text, marker=item.id, reply_to_external_id=None)

    def restart(self) -> None:
        self.bridge.events.put(None)
        self.ingest.join(5)
        self._start()
        self.connect()

    def attempts(self, item) -> int:
        return sum(1 for sent in self.world.sent if sent['marker'] == item.id)

    def rate_limit_sends(self) -> None:
        raise NotApplicable('大象 documents no rate limit for the owner\'s own session; the robot\'s shared '
                            '5/s limit falls back to the owner at once (C6, test_dx_robot_shared.py)')

    def echo(self, item) -> None:
        sent = next(sent for sent in self.world.sent if sent['marker'] == item.id)
        self.bridge.events.put(live(ThreadKey.parse(item.thread), sent['row'], own=True))

    def self_send_outside_outbox(self, text: str) -> Msg:
        """A Tofu tool's own-session send into the command chat while the bridge was down, replayed by the
        catch-up on reconnect (the 2026-11-09 restore); it carries Tofu's marker field."""
        row = self.world.row(COMMAND, sender=OWNER_UID, text=text,
                             extension={OUTBOX_MARKER_FIELD: 'tool-' + self.world.mid()})
        self.reconnect()
        return Msg(COMMAND, text, row['id'], {'row': row})

    # faults ------------------------------------------------------------
    def _exit(self, code: int, refusal=None) -> None:
        self.settle()
        self.bridge.returncode = code
        self.bridge.refusal = refusal
        self.bridge.events.put(None)
        self.ingest.join(5)
        self._tick()

    def expire_auth(self) -> None:
        self._exit(BRIDGE_CONTRACT['launcherSessionRequiredExit'])

    def displace(self) -> None:
        self.bridge.events.put({'type': 'status', 'status': 'kicked', 'detail': 'KickOffWithSameDeviceId'})
        self._exit(BRIDGE_CONTRACT['kickedExit'])

    def runtime_fault(self) -> None:
        from lib.channels.dx.adapter import LauncherRefusal
        self._exit(1, LauncherRefusal('SSO_NODE_UNAVAILABLE: no Node.js meets the engine floor', True))

    def network_fault(self) -> None:
        self.bridge.events.put({'type': 'status', 'status': 'disconnected', 'detail': 'status 8 onClose'})
        self._exit(4)

    def assert_no_restart(self, *, boot: bool = False) -> None:
        if boot:
            for name, value in (('_ADAPTERS', {}), ('_HOSTING', threading.Event())):
                self.env.monkeypatch.setattr(self.supervisor, name, value)
        before = len(self.starts)
        outcome = self._tick()
        assert len(self.starts) == before, f'the supervisor started a session ({outcome})'
