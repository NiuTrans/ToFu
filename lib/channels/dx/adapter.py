"""大象 (Daxiang) channel adapter: bridge process, normalization and catch-up.

Responsibility: run ``bridge.mjs`` through the owner's ``dx-pura-api``
launcher (credentials stay in the child environment), translate its JSON-lines
events into ``ChannelEvent`` facts, replay history after each thread
watermark and for sessions updated since the account frontier on every
(re)connect, and serve the outbox sender port (``send``, ``history_since``).
Policy and state stay in ``lib.channels`` (ingress, outbox, store).

Entry points: ``DxAdapter`` (one live session), ``resolve_identity`` and
``create_command_group`` (one-shot launcher commands), ``launch_spec``,
``event_from_live``, ``event_from_history``. Dependencies: the installed skill
under ``lib.skills.paths.owner_global_skills_dir(owner)``, ``subprocess`` and
``lib.channels``.
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.channels.model import (
    BRIDGE_CONTRACT, OUTBOX_MARKER_FIELD, PROVIDER_LIMITS, ChannelError, ChannelEvent, ThreadKey,
    external_id_after,
)
from lib.channels.store import ChannelStore, now_ms
from lib.log import get_logger
from lib.runtime.pinned_helper_source import PinnedHelperSource

logger = get_logger(__name__)

SKILL_NAME = 'dx-pura-api'
_BRIDGE_SOURCE = PinnedHelperSource(Path(__file__).with_name('bridge.mjs'))
SESSION_REQUIRED_EXIT = BRIDGE_CONTRACT['launcherSessionRequiredExit']
KICKED_EXIT = BRIDGE_CONTRACT['kickedExit']
HISTORY_PAGE_LIMIT = PROVIDER_LIMITS['dxHistoryPageLimit']
SESSION_LIST_LIMIT = PROVIDER_LIMITS['dxSessionListLimit']
_MID_EPOCH_MS = PROVIDER_LIMITS['dxMidEpochMs']
_MID_SHIFT = PROVIDER_LIMITS['dxMidTimestampShift']
_DIGITS = re.compile(r'^[0-9]{1,32}$')
# Protocol mention encodings of the engine (mention.js): text and markdown.
_TEXT_MENTION = re.compile(r'profile\?uid=([0-9]{1,32})&isAt=true')
_MARKDOWN_MENTION = re.compile(r'\[@[^\]:|]*:([0-9]{1,32})\]')


class BridgeClosed(ChannelError):
    def __init__(self, message: str = 'The 大象 bridge is not running'):
        super().__init__('bridge_closed', message)


@dataclass(frozen=True)
class LaunchSpec:
    skill_dir: str
    launcher: str
    engine_dir: str

    @property
    def installed(self) -> bool:
        return (os.path.isfile(self.launcher)
                and os.path.isfile(os.path.join(self.engine_dir, 'handler.js')))


def launch_spec(owner: int) -> LaunchSpec:
    from lib.skills.paths import owner_global_skills_dir
    skill_dir = os.path.join(owner_global_skills_dir(owner), SKILL_NAME)
    return LaunchSpec(skill_dir=skill_dir,
                      launcher=os.path.join(skill_dir, 'scripts', 'tofu', 'dx-tofu.sh'),
                      engine_dir=os.path.join(skill_dir, 'vendor', 'node_modules', 'dx-engine'))


def _child_environment(owner: int) -> dict[str, str]:
    """Inherit network/runtime configuration; never ambient secrets."""
    from lib.project_mod.run_command import _is_sensitive_inherited_env
    env = {key: value for key, value in os.environ.items() if not _is_sensitive_inherited_env(key)}
    # TODO(enterprise): one shared SSO session per Tofu owner; the launcher
    # takes the owner explicitly instead of assuming the personal user.
    env['DX_TOFU_OWNER'] = f'tofu:{int(owner)}'
    return env


def _argv(spec: LaunchSpec, *extra: str) -> list[str]:
    return ['bash', spec.launcher, 'node', _BRIDGE_SOURCE.path(), '--engine', spec.engine_dir, *extra]


# ── normalization ─────────────────────────────────────────────────────

def svr_time_from_mid(mid: str) -> int | None:
    """Server time encoded in a 大象 snowflake message ID (engine messages.js)."""
    if not _DIGITS.fullmatch(mid or '') or len(mid) < 15:
        return None
    return (int(mid) >> _MID_SHIFT) + _MID_EPOCH_MS


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    return ''


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ''


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith('{'):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _ids(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, dict):
                item = item.get('uid') or item.get('id')
            text = _text(item)
            if _DIGITS.fullmatch(text):
                out.append(text)
        return out
    return []


def _mentions(text: str, body: dict[str, Any], extension: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    ids: list[str] = []
    for source in (extension, body):
        for key in ('at', 'atUids', 'atList', 'atUsers', 'mentionedUids', 'remindUids', 'ats'):
            ids.extend(_ids(source.get(key)))
    ids.extend(_TEXT_MENTION.findall(text))
    ids.extend(_MARKDOWN_MENTION.findall(text))
    every = any(bool(source.get(key)) for source in (extension, body)
                for key in ('atAll', 'isAtAll', 'mentionAll', 'remindAll'))
    return tuple(dict.fromkeys(ids)), every


def _body_text(row: dict[str, Any], body: dict[str, Any], message_type: int) -> str:
    text = _first(body.get('searchText'), row.get('searchText'), body.get('text'), row.get('text'),
                  body.get('content'), row.get('content'), body.get('summary'), body.get('title'),
                  row.get('title'), body.get('link'), body.get('url'))
    return text or f'[type:{message_type}]'


def event_from_live(message: dict[str, Any], *, source: str, self_uid: str,
                    quotes: list[dict[str, Any]] | None = None,
                    own_connection: bool = False) -> ChannelEvent | None:
    """Normalize one SDK push (IM_message / PUB_MESSAGE) into facts, or None."""
    mid = _first(message.get('mid'), message.get('msgId'), message.get('serverMsgId'), message.get('id'))
    if not _DIGITS.fullmatch(mid):
        return None
    body = message.get('message') if isinstance(message.get('message'), dict) else (
        message.get('body') if isinstance(message.get('body'), dict) else _json_object(message.get('message')))
    sender = _first(message.get('from'), message.get('fromUid'), message.get('uid'))
    receiver = _first(message.get('to'), message.get('toUid'))
    gid = _first(message.get('gid'), message.get('toGuid'), message.get('guid'), message.get('groupId'))
    pub = _first(message.get('pubUid'), message.get('pid'), message.get('fromPubId'),
                 message.get('toPubId'), body.get('pubUid'))
    if source == 'pub':
        peer = pub or (sender if sender != self_uid else receiver)
        kind = 'pid'
    elif gid and gid != '0':
        peer, kind = gid, 'gid'
    else:
        peer, kind = (receiver if sender == self_uid else sender), 'uid'
    if not _DIGITS.fullmatch(peer or ''):
        return None
    message_type = int(_text(message.get('type') or message.get('msgType')) or 1)
    if message.get('isCancel') is True:
        return None
    extension = _json_object(message.get('extension'))
    text = _body_text(message, body, message_type)
    mentioned, every = _mentions(text, body, extension)
    reply = next((q for q in quotes or [] if _DIGITS.fullmatch(_text(q.get('mid')))), {})
    cts = int(_text(message.get('cts') or message.get('time')) or 0)
    sender_name = _first(message.get('fromName'), message.get('name'))
    if kind == 'uid':
        title = sender_name if sender != self_uid else ''
    else:
        title = _first(message.get('groupName'), message.get('pubName'))
    return ChannelEvent(
        thread=ThreadKey('dx', kind, peer), external_id=mid, sender_id=sender,
        sender_name=sender_name, text=text,
        sent_at_ms=svr_time_from_mid(mid) or cts,
        thread_title=title,
        message_type=message_type, mentioned_ids=mentioned, mentions_all=every,
        reply_to_external_id=_text(reply.get('mid')), reply_to_sender_id=_text(reply.get('from')),
        client_id=_first(message.get('uuid'), message.get('msgUuid')),
        outbox_marker=_text(extension.get(OUTBOX_MARKER_FIELD)), own_connection=own_connection)


def event_from_history(row: dict[str, Any], thread: ThreadKey, *, title: str = '') -> ChannelEvent | None:
    """Normalize one engine ``get_history_messages`` row of ``thread``, or None."""
    raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
    mid = _first(raw.get('mid'), raw.get('msgId'), row.get('id'))
    if not _DIGITS.fullmatch(mid) or raw.get('isCancel') is True:
        return None
    body = raw.get('message') if isinstance(raw.get('message'), dict) else {}
    message_type = int(_text(raw.get('type')) or 1)
    extension = _json_object(raw.get('extension'))
    text = _first(row.get('text')) or _body_text(raw, body, message_type)
    mentioned, every = _mentions(text, body, extension)
    quoted = raw.get('quotedMessage') if isinstance(raw.get('quotedMessage'), list) else []
    candidates = [part.get('message') for part in quoted if isinstance(part, dict)]
    last = raw.get('lastReplyMessage')
    if isinstance(last, dict):
        candidates.append(last.get('message'))
    reply = next((m for m in candidates if isinstance(m, dict) and _DIGITS.fullmatch(_text(m.get('mid')))), {})
    cts = int(_text(raw.get('cts') or row.get('timestamp')) or 0)
    return ChannelEvent(
        thread=thread, external_id=mid, sender_id=_first(raw.get('from'), raw.get('fromUid')),
        sender_name=_first(raw.get('fromName'), row.get('name')), text=text,
        sent_at_ms=svr_time_from_mid(mid) or cts, thread_title=title,
        message_type=message_type, mentioned_ids=mentioned, mentions_all=every,
        reply_to_external_id=_text(reply.get('mid')),
        reply_to_sender_id=_first(reply.get('fromUid'), reply.get('from')),
        client_id=_first(raw.get('uuid'), raw.get('msgUuid')),
        outbox_marker=_text(extension.get(OUTBOX_MARKER_FIELD)))


def thread_from_session(session: dict[str, Any]) -> ThreadKey | None:
    kind = {'chat': 'uid', 'group': 'gid', 'pub': 'pid'}.get(str(session.get('type') or ''))
    external = _text(session.get('id'))
    if not kind or not _DIGITS.fullmatch(external):
        return None
    return ThreadKey('dx', kind, external)


def _target(thread: ThreadKey) -> dict[str, str]:
    return {thread.kind: thread.external_id}


def _row_mid(row: dict[str, Any]) -> str:
    raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
    mid = _first(raw.get('mid'), raw.get('msgId'), row.get('id'))
    return mid if _DIGITS.fullmatch(mid) else ''


def history_pages(bridge, thread: ThreadKey):
    """Yield a thread's history pages from newest to oldest.

    The provider returns group/private pages oldest-first and public-account
    pages newest-first, and its ``nextCursor`` is the page's *last* row; the
    range query's ``cursorMsg`` is an exclusive upper bound. Like the skill's
    own exporter, the next cursor is therefore the page's smallest message ID;
    ``-1`` marks the end, and a page without unseen IDs ends the walk.
    """
    cursor = ''
    seen: set[str] = set()
    while True:
        response = bridge.request('history', **_target(thread), limit=HISTORY_PAGE_LIMIT, cursor=cursor)
        if not response.get('ok'):
            raise ChannelError('history_failed', str((response.get('error') or {}).get('code')))
        result = response.get('result') or {}
        rows = [row for row in result.get('messages') or [] if isinstance(row, dict)]
        ids = [mid for mid in map(_row_mid, rows) if mid]
        if not any(mid not in seen for mid in ids):
            return
        seen.update(ids)
        yield rows
        if _text(result.get('nextCursor')) == '-1':
            return
        cursor = min(ids, key=int)


# ── one-shot launcher commands ────────────────────────────────────────

def _one_shot(owner: int, *extra: str) -> dict[str, Any]:
    spec = launch_spec(owner)
    if not spec.installed:
        raise ChannelError('skill_missing', f'The {SKILL_NAME} skill is not installed for this owner')
    completed = subprocess.run(_argv(spec, *extra), cwd=spec.skill_dir, env=_child_environment(owner),
                               stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if completed.returncode == SESSION_REQUIRED_EXIT:
        raise ChannelError('needs_login', '大象 SSO session needs the owner to log in again')
    for line in completed.stdout.decode('utf-8', 'replace').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue  # engine diagnostics on stdout are not protocol lines
        if isinstance(value, dict) and value.get('type') == 'response':
            if not value.get('ok'):
                error = value.get('error') or {}
                raise ChannelError(str(error.get('code') or 'engine_error'),
                                   str(error.get('message') or 'The 大象 engine refused the command'))
            return value.get('result') or {}
    raise ChannelError('bridge_failed', f'The 大象 bridge exited {completed.returncode} without a result')


def resolve_identity(owner: int) -> dict[str, str]:
    """``whoami`` through the launcher: the bound owner account (C6 identity)."""
    result = _one_shot(owner, '--command', 'whoami')
    uid = _text(result.get('uid'))
    if not _DIGITS.fullmatch(uid):
        raise ChannelError('identity_unavailable', '大象 whoami returned no uid')
    return {'uid': uid, 'mis': _text(result.get('mis')), 'name': _text(result.get('name'))}


def create_command_group(owner: int, *, name: str, member_uid: str) -> str:
    """Create the single-member command chat group; returns its gid."""
    result = _one_shot(owner, '--command', 'create_group', '--name', name, '--member', member_uid)
    gid = _text(result.get('gid'))
    if not _DIGITS.fullmatch(gid):
        raise ChannelError('group_create_failed', '大象 group creation returned no gid')
    return gid


# ── bridge process ────────────────────────────────────────────────────

class DxBridge:
    """One bridge child: request/response by ID plus an ordered event queue."""

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str):
        self._proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      start_new_session=True)
        self._pending: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._closed = False
        self.events: queue.Queue = queue.Queue()
        self.stderr_tail = ''
        self._reader = threading.Thread(target=self._read, name='dx-bridge-reader', daemon=True)
        self._stderr = threading.Thread(target=self._drain, name='dx-bridge-stderr', daemon=True)
        self._reader.start()
        self._stderr.start()

    def _read(self) -> None:
        try:
            for raw in self._proc.stdout:
                try:
                    value = json.loads(raw.decode('utf-8', 'replace'))
                except ValueError:
                    continue  # engine diagnostics on stdout are not protocol lines
                if not isinstance(value, dict):
                    continue
                if value.get('type') == 'response':
                    with self._lock:
                        future = self._pending.pop(str(value.get('id')), None)
                    if future is not None:
                        future.set_result(value)
                else:
                    self.events.put(value)
        finally:
            with self._lock:
                self._closed = True
                pending, self._pending = list(self._pending.values()), {}
            for future in pending:
                future.set_exception(BridgeClosed())
            self.events.put(None)

    def _drain(self) -> None:
        for raw in self._proc.stderr:
            # Diagnostics only; the bridge never writes message content here.
            self.stderr_tail = raw.decode('utf-8', 'replace').strip() or self.stderr_tail

    def request(self, op: str, **arguments: Any) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise BridgeClosed()
            self._pending[request_id] = future
        try:
            self._proc.stdin.write((json.dumps({'id': request_id, 'op': op, **arguments},
                                               ensure_ascii=False) + '\n').encode('utf-8'))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            with self._lock:
                self._pending.pop(request_id, None)
            raise BridgeClosed() from error
        return future.result()

    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        if self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return  # already exited between poll and signal

    def close(self, timeout: float) -> bool:
        """Close stdin (the bridge's stop signal), then escalate; always reap."""
        try:
            self._proc.stdin.close()
        except OSError:
            pass  # the pipe is already broken: the child is gone or going
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # exited after the second wait expired
                self._proc.wait()
        self._reader.join(timeout)
        self._stderr.join(timeout)
        return self._proc.poll() is not None


# ── live session ──────────────────────────────────────────────────────

class DxAdapter:
    """One live 大象 session for one owner: bridge + ingest + outbox executor."""

    def __init__(self, owner: int, *, store: ChannelStore | None = None,
                 bridge_factory=None):
        self.owner = int(owner)
        self.store = store or ChannelStore(owner)
        self._bridge_factory = bridge_factory or self._spawn
        self._bridge: DxBridge | None = None
        self._self_uid = ''
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._kicked = False
        self._failure = ''
        self._frontier_ms: int | None = None
        self._wake = threading.Condition()
        self._wake_pending = False
        self._threads: list[threading.Thread] = []

    @staticmethod
    def _spawn(owner: int) -> DxBridge:
        spec = launch_spec(owner)
        if not spec.installed:
            raise ChannelError('skill_missing', f'The {SKILL_NAME} skill is not installed for this owner')
        return DxBridge(_argv(spec), env=_child_environment(owner), cwd=spec.skill_dir)

    # lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._bridge = self._bridge_factory(self.owner)
        from lib.channels import outbox
        outbox.register_executor(self.owner, self.wake_executor, channel='dx')
        for name, target in (('dx-ingest', self._process), ('dx-outbox', self._execute_outbox)):
            thread = threading.Thread(target=target, name=f'{name}-{self.owner}', daemon=True)
            thread.start()
            self._threads.append(thread)

    def alive(self) -> bool:
        return self._bridge is not None and self._bridge.alive()

    def outcome(self) -> tuple[str, str]:
        """``(state, detail)`` after the bridge exited (supervisor restart policy)."""
        code = self._bridge.returncode if self._bridge else None
        if code == SESSION_REQUIRED_EXIT:
            return 'needs_login', '大象 SSO session needs the owner to log in (launcher exit 42)'
        if self._kicked or code == KICKED_EXIT:
            return 'kicked', '大象 kicked this session off (another login took it)'
        if self._failure:
            return 'error', self._failure
        return 'disconnected', f'bridge exited ({code})'

    def stop(self, timeout: float) -> bool:
        self._stopping.set()
        from lib.channels import outbox
        outbox.register_executor(self.owner, None, channel='dx')
        self.wake_executor()
        stopped = self._bridge.close(timeout) if self._bridge else True
        for thread in self._threads:
            thread.join(timeout)
        self.checkpoint()
        return stopped and not any(thread.is_alive() for thread in self._threads)

    def tick(self) -> None:
        """Called from the supervisor's existing maintenance tick."""
        self.checkpoint()
        self.wake_executor()

    def checkpoint(self) -> None:
        """Persist the account frontier: the start of the last completed catch-up.

        Live pushes never move it: pushes of different threads can arrive out
        of server-time order, so only a completed replay proves that every
        session updated before its start was processed.
        """
        frontier = self._frontier_ms
        if frontier is None:
            return

        def change(account):
            if (account.get('frontierAtMs') or 0) < frontier:
                account['frontierAtMs'] = frontier
            return account
        self.store.update_account('dx', change)

    # ingest ----------------------------------------------------------
    def _process(self) -> None:
        from lib.channels.ingress import ingest
        bridge = self._bridge
        try:
            while True:
                item = bridge.events.get()
                if item is None:
                    return
                kind = item.get('type')
                if kind == 'status':
                    self._on_status(item)
                elif kind == 'message':
                    event = event_from_live(item.get('message') or {}, source=str(item.get('source') or 'im'),
                                            self_uid=self._self_uid, quotes=item.get('quotes'),
                                            own_connection=bool(item.get('ownConnection')))
                    if event is None:
                        continue
                    ingest(self.owner, event, store=self.store)
        except Exception as error:
            if self._stopping.is_set():
                return
            code = getattr(error, 'code', type(error).__name__)
            logger.warning('[Channels] dx session failed owner=%s: %s', self.owner, code, exc_info=True)
            self._failure = f'ingest failed: {code}'
            self.store.set_status('dx', 'error', self._failure)
            # Halting before the watermark moves makes the next session replay the gap.
            bridge.terminate()

    def _on_status(self, item: dict[str, Any]) -> None:
        status = item.get('status')
        if status == 'connected':
            self._self_uid = str(item.get('selfUid') or '')
            self.store.set_status('dx', 'connected', '', connected_at_ms=now_ms())
            self.catch_up()
            self._connected.set()
            self.wake_executor()
        elif status == 'kicked':
            self._kicked = True
            self._connected.clear()
            self.store.set_status('dx', 'kicked', str(item.get('detail') or ''))
        elif status in ('disconnected', 'failed'):
            self._connected.clear()
            self.store.set_status('dx', 'disconnected' if status == 'disconnected' else 'error',
                                  str(item.get('detail') or ''))

    def catch_up(self) -> None:
        """Replay history after each watermark for sessions updated since the frontier."""
        from lib.channels.ingress import ingest_batch
        from lib.channels.policy import effective_mode
        started = now_ms()
        account = self.store.account('dx') or {}
        frontier = account.get('frontierAtMs')
        if frontier is None:
            self._frontier_ms = started
            self.checkpoint()
            return
        replay: list[ChannelEvent] = []
        for session in sorted(self._sessions(int(frontier), started),
                              key=lambda s: int(s.get('lastMessageServerTime') or 0)):
            thread = thread_from_session(session)
            if thread is None:
                continue
            record = self.store.thread(thread) or {}
            title = _text(session.get('name'))
            if effective_mode(thread, record, account) == 'ignore':
                if title and record.get('title') != title:
                    self.store.update_thread(thread, lambda value: {**value, 'title': title})
                continue
            since = None if record.get('watermark') else max(int(frontier), int(record.get('sinceMs') or 0))
            replay.extend(self._history_after(thread, record.get('watermark'), since, title))
        if replay:
            replay.sort(key=lambda event: (event.sent_at_ms, int(event.external_id)))
            ingest_batch(self.owner, replay, store=self.store)
        self._frontier_ms = max(self._frontier_ms or 0, started)
        self.checkpoint()

    def _sessions(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        response = self._bridge.request('sessions', startMs=start_ms, endMs=end_ms,
                                        limit=SESSION_LIST_LIMIT)
        if not response.get('ok'):
            raise ChannelError('sessions_failed', str((response.get('error') or {}).get('code')))
        sessions = list((response.get('result') or {}).get('sessions') or [])
        if len(sessions) >= SESSION_LIST_LIMIT and end_ms - start_ms > 1:
            middle = (start_ms + end_ms) // 2
            merged = {(s.get('type'), s.get('id')): s for s in self._sessions(start_ms, middle)}
            merged.update({(s.get('type'), s.get('id')): s for s in self._sessions(middle, end_ms)})
            return list(merged.values())
        return sessions

    def _history_pages(self, thread: ThreadKey):
        return history_pages(self._bridge, thread)

    def _history_after(self, thread: ThreadKey, watermark: str | None, since_ms: int | None,
                       title: str) -> list[ChannelEvent]:
        collected: list[ChannelEvent] = []
        for rows in self._history_pages(thread):
            reached = False
            for row in rows:
                event = event_from_history(row, thread, title=title)
                if event is None:
                    continue
                if watermark and not external_id_after(event.external_id, watermark):
                    reached = True
                elif since_ms is not None and event.sent_at_ms <= since_ms:
                    reached = True
                else:
                    collected.append(event)
            if reached:
                break
        return collected

    # outbox sender port ---------------------------------------------
    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]:
        if not self._connected.is_set():
            raise BridgeClosed('The 大象 session is not connected')
        extension = json.dumps({'at': [], 'botAt': [], 'fileType': 'markdown',
                                OUTBOX_MARKER_FIELD: marker}, ensure_ascii=False)
        if reply_to_external_id:
            response = self._bridge.request('reply_message', **_target(thread), text=text,
                                            replyTo={'mid': reply_to_external_id}, markdown=True,
                                            extension=extension)
        else:
            response = self._bridge.request('send_markdown', **_target(thread), text=text,
                                            extension=extension)
        result = response.get('result') or {}
        return {'ok': bool(response.get('ok')), 'externalId': _text(result.get('serverMsgId')),
                'clientId': _text(result.get('msgUuid')), 'error': response.get('error')}

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]:
        if not self._connected.is_set():
            raise BridgeClosed('The 大象 session is not connected')
        found: list[ChannelEvent] = []
        for rows in self._history_pages(thread):
            older = False
            for row in rows:
                event = event_from_history(row, thread)
                if event is None:
                    continue
                if event.sent_at_ms < since_ms:
                    older = True
                else:
                    found.append(event)
            if older:
                break
        return found

    # outbox executor ------------------------------------------------
    def wake_executor(self) -> None:
        with self._wake:
            self._wake_pending = True
            self._wake.notify_all()

    def _execute_outbox(self) -> None:
        from lib.channels import outbox
        while True:
            with self._wake:
                while not self._wake_pending and not self._stopping.is_set():
                    self._wake.wait()
                self._wake_pending = False
            if self._stopping.is_set():
                return
            if not self._connected.is_set():
                continue
            try:
                outbox.execute(self.owner, self, channel='dx', store=self.store)
            except Exception:
                logger.warning('[Channels] outbox pass failed owner=%s', self.owner, exc_info=True)


__all__ = ['BridgeClosed', 'DxAdapter', 'DxBridge', 'LaunchSpec', 'create_command_group',
           'event_from_history', 'event_from_live', 'history_pages', 'launch_spec',
           'resolve_identity', 'svr_time_from_mid', 'thread_from_session']
