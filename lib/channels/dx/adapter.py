"""大象 (Daxiang) channel adapter: bridge process, normalization and catch-up.

Responsibility: run ``bridge.mjs`` through the owner's ``dx-pura-api``
launcher (credentials stay in the child environment), translate its JSON-lines
events into ``ChannelEvent`` facts (every message type as its own text or a
readable description, plus its image, file or voice originals), replay history
after each thread watermark and for sessions updated since the account
frontier on every (re)connect (a failed catch-up or ingest keeps the live
session and is retried at the supervisor's next tick), and serve the outbox
sender port (``send``, ``history_since``) and the media port (``download_media``).
Policy and state stay in ``lib.channels`` (ingress, outbox, store).

Entry points: ``DxAdapter`` (one live session), ``resolve_identity`` and
``create_command_group`` (one-shot launcher commands), ``launch_spec``,
``launcher_session``, ``event_from_live``, ``event_from_history``. The sender
port posts the group command chat through the account's notification robot
when one is configured (``lib.channels.dx.robot``) and through the bridge
otherwise, or when the robot refuses before posting anything.
Dependencies: the installed skill under
``lib.skills.paths.owner_global_skills_dir(owner)``, ``subprocess``,
``lib.channels`` and ``lib.channels.dx.login`` (the login MIS's SSO session).
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import re
import signal
import subprocess
import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lib.channels.dx.login import LoginSession, login_session, sso_owner
from lib.channels.media import safe_media_name
from lib.channels.model import (
    BRIDGE_CONTRACT, DX, OUTBOX_MARKER_FIELD, PROVIDER_LIMITS, ChannelError, ChannelEvent,
    ChannelMedia, ThreadKey, external_id_after,
)
from lib.channels.store import ChannelStore, now_ms
from lib.log import get_logger
from lib.runtime.pinned_helper_source import PinnedHelperSource
from lib.storage.errors import StorageError

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
_DX_TYPES = {int(number): fact for number, fact in DX['messageTypes'].items()}
_RICH_TEXT = DX['generalRichText']
_GENERAL_LABELS = {int(number): label for number, label in DX['generalLabels'].items()}
_VOICE_MIME = DX['voiceMimeType']
# The Web SDK's file hosts: the only media URLs fetched, with the file headers.
FILE_URL_PREFIXES = tuple(DX['fileUrlPrefixes'])
# Image formats of the image body's ``type`` and of rich-text image nodes.
_IMAGE_FORMATS = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif',
                  'bmp': 'image/bmp', 'webp': 'image/webp'}
# History rows drop the image URL fields but keep the original in the preview
# text (engine api/messages.js forwardPreview: "[image] <original>").
_HISTORY_IMAGE_PREVIEW = re.compile(r'\[image\] (https?://\S+)')
# Adapter-internal ingest item queued by ``DxAdapter.tick``: retry the owed
# catch-up. The bridge never writes this type.
_CATCH_UP_DUE = 'catch_up_due'


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


def launcher_session(owner: int, mis: str | None) -> LoginSession:
    """The launcher child's environment, logged in as ``mis``.

    It inherits network/runtime configuration, never ambient secrets; the
    login MIS selects its SSO session (``lib.channels.dx.login``). The SSO
    step runs on the configured ``TOFU_NODE``, the Node dx-tofu.sh prefers for
    the bridge, unless ``MTSSO_NODE_EXECUTABLE`` names one explicitly; without
    either the SSO adapter picks one from ``PATH``.
    """
    from lib.project_mod.run_command import _is_sensitive_inherited_env
    env = {key: value for key, value in os.environ.items() if not _is_sensitive_inherited_env(key)}
    env['DX_TOFU_OWNER'] = sso_owner(owner)
    if env.get('TOFU_NODE') and not env.get('MTSSO_NODE_EXECUTABLE'):
        env['MTSSO_NODE_EXECUTABLE'] = env['TOFU_NODE']
    return login_session(owner, mis, env)


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


# ── message content (C9) ───────────────────────────────────────────────

def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _size_text(size: int) -> str:
    value, unit = float(size), 'B'
    for larger in ('KB', 'MB', 'GB'):
        if value < 1024:
            break
        value, unit = value / 1024, larger
    return f'{size} B' if unit == 'B' else f'{value:.1f} {unit}'


def _clock(ms: Any) -> str:
    stamp = _int(ms)
    return datetime.fromtimestamp(stamp / 1000).astimezone().strftime('%Y-%m-%d %H:%M') if stamp else ''


def _described(label: str, *details: str) -> str:
    shown = [detail for detail in details if detail]
    return f'[{label}: {", ".join(shown)}]' if shown else f'[{label}]'


def _image(mid: str, ordinal: int, source: str, kind: Any, size: Any, token: Any = '') -> ChannelMedia:
    """An image original: the message itself (``ordinal`` 0) or the n-th rich-text image."""
    fmt = _text(kind).lower()
    mime = fmt if fmt.startswith('image/') else _IMAGE_FORMATS.get(fmt, '')
    # The stored bytes decide the real type; the name only needs an image suffix.
    suffix = next((name for name, known in _IMAGE_FORMATS.items() if known == mime), 'jpg')
    name = f'image-{mid}' + (f'-{ordinal}' if ordinal else '') + f'.{suffix}'
    return ChannelMedia('image', name, mime_type=mime, size_bytes=_int(size), source=source,
                        token=_text(token))


def _image_message(body, label, mid):
    source = _first(body.get('original'), body.get('normal'), body.get('url'))
    return f'[{label}]', (_image(mid, 0, source, body.get('type'), body.get('originSize'),
                                 body.get('token')),)


def _file_message(body, label, mid):
    name = safe_media_name(_first(body.get('name'), body.get('fileName')), f'file-{mid}')
    mime = _first(body.get('format'), body.get('mimeType'))
    if '/' not in mime:
        mime = mimetypes.guess_type(name)[0] or ''
    source = _first(body.get('url'), body.get('fileUrl'), body.get('downloadUrl'), body.get('originUrl'))
    return f'[{label}: {name}]', (ChannelMedia(
        'file', name, mime_type=mime, size_bytes=_int(body.get('size') or body.get('fileSize')),
        source=source, token=_text(body.get('token'))),)


def _voice_message(body, label, mid):
    seconds = _int(body.get('duration'))
    return _described(label, f'{seconds} s' if seconds else ''), (ChannelMedia(
        'voice', f'voice-{mid}.amr', mime_type=_VOICE_MIME, duration_s=seconds,
        source=_first(body.get('url')), token=_text(body.get('token'))),)


def _video_message(body, label, mid):
    seconds = _int(body.get('duration')) // 1000  # milliseconds on the wire
    width, height, size = _int(body.get('width')), _int(body.get('height')), _int(body.get('size'))
    return _described(label, f'{seconds} s' if seconds else '',
                      f'{width}x{height}' if width and height else '',
                      _size_text(size) if size else ''), ()


def _general_data(value: Any) -> dict[str, Any]:
    """A general message's content: JSON on the IM wire, Base64 JSON on the send API."""
    if isinstance(value, dict):
        return value
    text = _text(value)
    parsed = _json_object(text)
    if parsed or not text:
        return parsed
    try:
        return _json_object(base64.b64decode(text, validate=True).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return {}


def _rich_text(nodes: list[Any], mid: str) -> tuple[str, tuple[ChannelMedia, ...]]:
    """Plain text of rich-text nodes (engine rich.js node kinds) and their images."""
    images: list[ChannelMedia] = []

    def render(node: Any) -> str:
        if not isinstance(node, dict):
            return ''
        kind = str(node.get('t') or node.get('type') or '').lower()
        if kind == 'text':
            return str(node.get('c') or node.get('text') or '')
        if kind == 'link':
            url = _first(node.get('url'), node.get('href'), node.get('link'))
            label = _first(node.get('c'), node.get('text'), node.get('title'))
            return f'{label} ({url})' if label and url and label != url else (url or label)
        if kind == 'at':
            return _first(node.get('name'), node.get('c'), node.get('uid'))
        if kind == 'emoji':
            return f'[{_first(node.get("n"), node.get("name"), node.get("c")) or "emoji"}]'
        if kind == 'img':
            images.append(_image(mid, len(images) + 1,
                                 _first(node.get('origin'), node.get('original'), node.get('url')),
                                 _first(node.get('type'), node.get('imgType')),
                                 node.get('originSize') or node.get('size')))
            return '[image]'
        if kind in ('ul', 'ol'):
            lines = []
            for number, line in enumerate(node.get('children') or [], 1):
                marker = f'{number}.' if kind == 'ol' else '-'
                lines.append(f'{marker} ' + ''.join(map(render, line if isinstance(line, list) else [line])))
            return '\n' + '\n'.join(lines) + '\n'
        return ''

    text = ''.join(map(render, nodes))
    return re.sub(r'\n{3,}', '\n\n', text).strip(), tuple(images)


def _general_message(body, label, mid):
    data = _general_data(body.get('data'))
    nodes = data.get('nodes')
    # Live bodies carry the subtype as ``type``; compacted history rows as ``generalType``.
    subtype = _int(body.get('generalType')) or _int(body.get('type'))
    if subtype == _RICH_TEXT or isinstance(nodes, list):
        text, images = _rich_text(nodes if isinstance(nodes, list) else [], mid)
        return text or _first(body.get('summary')) or '[rich text]', images
    return _described(_GENERAL_LABELS.get(subtype, label), _first(body.get('summary'))), ()


def _link_message(body, label, mid):
    title, link, content = _first(body.get('title')), _first(body.get('link'), body.get('url')), _first(
        body.get('content'))
    text = _described(label, title) + (f' {link}' if link else '')
    return (f'{text}\n{content}' if content else text), ()


def _links_message(body, label, mid):
    items = body.get('content')
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            items = []
    link_label = _DX_TYPES[6]['label']
    lines = [_link_message(item, link_label, mid)[0] for item in items if isinstance(item, dict)] if (
        isinstance(items, list)) else []
    count = _int(body.get('num'))
    return '\n'.join(lines) or _described(label, f'{count} items' if count else ''), ()


def _location_message(body, label, mid):
    try:
        place = f'{float(body.get("latitude")):.6f}, {float(body.get("longitude")):.6f}'
    except (TypeError, ValueError):
        place = ''
    return _described(label, _first(body.get('name')), place), ()


def _template_message(body, label, mid):
    lines = [line for line in (_first(body.get('contentTitle')), _first(body.get('content'))) if line]
    if _first(body.get('link')):
        lines.append(f'{_first(body.get("linkName")) or "link"}: {_first(body.get("link"))}')
    head = _described(label, _first(body.get('templateName')))
    return (head + ' ' + '\n'.join(lines) if lines else head), ()


def _calendar_message(body, label, mid):
    start, end = _clock(body.get('dtstart')), _clock(body.get('dtend'))
    return _described(label, _first(body.get('summary')), _first(body.get('location')),
                      f'{start} – {end}' if start and end else start), ()


def _system_message(body, label, mid):
    text = _first(body.get('text'))
    return (f'[{label}] {text}' if text else f'[{label}]'), ()


def _named(*fields: str):
    def describe(body, label, mid):
        return _described(label, _first(*(body.get(name) for name in fields))), ()
    return describe


# Official enum name → describer of its body (消息类型与消息体字段); the other
# types keep their own text and show their label only when they have none.
_DESCRIBERS = {
    'image': _image_message, 'file': _file_message, 'audio': _voice_message, 'video': _video_message,
    'general': _general_message, 'link': _link_message, 'notice': _link_message,
    'multilink': _links_message,
    'gps': _location_message, 'custom': _template_message, 'calendar': _calendar_message,
    'event': _system_message, 'vcard': _named('name', 'account'), 'emotion': _named('name'),
    'newemotion': _named('name'), 'gvcard': _named('name'), 'redpacket': _named('greetings'),
    'dynamic': _named('title'), 'pcall': _named(),
}


def _message_content(message_type: int, body: dict[str, Any], row: dict[str, Any], mid: str
                     ) -> tuple[str, tuple[ChannelMedia, ...]]:
    """The message's own text, or a readable description, and its downloadable media."""
    fact = _DX_TYPES.get(message_type) or {}
    label = fact.get('label') or f'message type {message_type}'
    describe = _DESCRIBERS.get(fact.get('name', ''))
    if describe is not None:
        return describe(body, label, mid)
    text = _first(body.get('searchText'), row.get('searchText'), body.get('text'), row.get('text'),
                  body.get('content'), row.get('content'), body.get('summary'), body.get('title'),
                  row.get('title'), body.get('link'), body.get('url'))
    if fact.get('name') in ('text', 'quote'):
        return text, ()
    return (f'[{label}] {text}' if text else f'[{label}]'), ()


def event_from_live(message: dict[str, Any], *, source: str, self_uid: str,
                    quotes: list[dict[str, Any]] | None = None,
                    own_connection: bool = False, host_sent: bool = False) -> ChannelEvent | None:
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
    text, media = _message_content(message_type, body, message, mid)
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
        outbox_marker=_text(extension.get(OUTBOX_MARKER_FIELD)), own_connection=own_connection,
        host_sent=host_sent, media=media)


def event_from_history(row: dict[str, Any], thread: ThreadKey, *, title: str = '') -> ChannelEvent | None:
    """Normalize one engine ``get_history_messages`` row of ``thread``, or None."""
    raw = row.get('raw') if isinstance(row.get('raw'), dict) else {}
    mid = _first(raw.get('mid'), raw.get('msgId'), row.get('id'))
    if not _DIGITS.fullmatch(mid) or raw.get('isCancel') is True:
        return None
    body = raw.get('message') if isinstance(raw.get('message'), dict) else {}
    message_type = int(_text(raw.get('type')) or 1)
    extension = _json_object(raw.get('extension'))
    text, media = _message_content(message_type, body, raw, mid)
    kind = (_DX_TYPES.get(message_type) or {}).get('name')
    if kind in ('text', 'quote'):
        text = _first(row.get('text')) or text
    elif kind == 'image' and not media[0].source:
        # The engine's compacted row keeps the original only in its preview text.
        preview = _HISTORY_IMAGE_PREVIEW.fullmatch(_text(row.get('text')))
        if preview:
            media = (replace(media[0], source=preview.group(1)),)
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
        outbox_marker=_text(extension.get(OUTBOX_MARKER_FIELD)), host_sent=row.get('hostSent') is True,
        media=media)


# Engine session types the catch-up replays, as thread kinds. The session list request
# names them: an untyped listing answers a failed chatlist source with the other source's
# rows as a success (dx-engine api/sessions.js), and that partial list moved the frontier
# past messages it never replayed.
_SESSION_KINDS = {'chat': 'uid', 'group': 'gid', 'pub': 'pid'}


def thread_from_session(session: dict[str, Any]) -> ThreadKey | None:
    kind = _SESSION_KINDS.get(str(session.get('type') or ''))
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


def _engine_refusal(response: dict[str, Any]) -> str:
    """``code: message`` of an engine refusal relayed by the bridge (no content or credentials)."""
    error = response.get('error') or {}
    return f"{error.get('code')}: {error.get('message') or ''}".strip()


# 大象 refused this session's SSO ticket while the engine minted the IM token (or its dx-sign) for an
# HTTP read: a 92xxx rescode from dx-engine credential-provider.js generateImToken / refreshDxSign. The
# launcher exchanges the ticket once per launch (tofu_mtsso_auth exec), and the bridge runs the engine
# on that ticket alone (MEITUAN_SSO_AUTH_MODE=env). So the live WebSocket keeps sending while every
# history, session-list and download read fails, until a relaunch renews the ticket (C1).
_EXPIRED_TICKET = re.compile(r'DX (?:IM token generation|sign refresh) failed: rescode=92\d{3}\b')
EXPIRED_TICKET_OUTCOME = '大象 SSO ticket expired ({refusal}); the next tick relaunches the bridge for a fresh exchange'


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
            raise ChannelError('history_failed', _engine_refusal(response))
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

# The SSO adapter's ``recovery_action`` when its own runtime or configuration
# refused (packages/tofu-mtsso-auth ``OfficialAuthError.envelope``), for example
# no Node that meets the official engine floor: no 大象 login can fix it.
_RUNTIME_RECOVERY = 'repair_agent_configuration'


@dataclass(frozen=True)
class LauncherRefusal:
    """The SSO adapter's public error envelope on the launcher's stderr.

    ``text`` is ``error_code: error``; ``runtime`` marks a refusal of the SSO
    step's own runtime (``runtime_unavailable``), as opposed to a login the
    owner must approve (exit 42, ``needs_login``) or a failing SSO service.
    """
    text: str
    runtime: bool


def _launcher_refusal(stderr: bytes) -> LauncherRefusal | None:
    """The last public error envelope on the launcher's stderr, if any.

    ``python -m tofu_mtsso_auth exec`` prints only that safe projection when
    it refuses to start the command; it never carries a ticket.
    """
    for line in reversed(stderr.decode('utf-8', 'replace').splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('error_code'):
            return LauncherRefusal(f"{value['error_code']}: {value.get('error') or ''}".strip(),
                                   value.get('recovery_action') == _RUNTIME_RECOVERY)
    return None


def _one_shot(owner: int, session: LoginSession, *extra: str) -> dict[str, Any]:
    spec = launch_spec(owner)
    if not spec.installed:
        raise ChannelError('skill_missing', f'The {SKILL_NAME} skill is not installed for this owner')
    completed = subprocess.run(_argv(spec, *extra), cwd=spec.skill_dir, env=session.environment,
                               stdin=subprocess.DEVNULL, capture_output=True, check=False)
    refusal = _launcher_refusal(completed.stderr)
    named = refusal.text if refusal else ''
    if completed.returncode == SESSION_REQUIRED_EXIT:
        raise ChannelError('needs_login', named or '大象 SSO session needs the owner to log in again')
    if refusal is not None and refusal.runtime:
        raise ChannelError('runtime_unavailable', named)
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
    raise ChannelError('bridge_failed', f'The 大象 bridge exited {completed.returncode} without a result'
                                        + (f' ({named})' if named else ''))


def resolve_identity(owner: int, session: LoginSession) -> dict[str, str]:
    """``whoami`` through the launcher logged in as ``session.mis`` (C6 identity).

    The launcher may first wait for the one 大象 login approval of a MIS
    without a reusable SSO session, so callers run it off request threads.
    """
    result = _one_shot(owner, session, '--command', 'whoami')
    uid = _text(result.get('uid'))
    if not _DIGITS.fullmatch(uid):
        raise ChannelError('identity_unavailable', '大象 whoami returned no uid')
    mis = _text(result.get('mis')).lower()
    if session.mis and mis and mis != session.mis:
        raise ChannelError('identity_mismatch', f'大象 logged in as {mis}, not {session.mis}')
    return {'uid': uid, 'mis': mis or session.mis or '', 'name': _text(result.get('name'))}


def create_command_group(owner: int, *, name: str, member_uid: str, mis: str | None) -> str:
    """Create the single-member command chat group as ``mis``; returns its gid."""
    result = _one_shot(owner, launcher_session(owner, mis), '--command', 'create_group', '--name', name,
                       '--member', member_uid)
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
        # The launcher's public refusal when it refused to start the bridge.
        self.refusal: LauncherRefusal | None = None
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
            self.refusal = _launcher_refusal(raw) or self.refusal

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
                 bridge_factory=None, login_mis: str | None = None, robot_factory=None):
        from lib.channels.dx.robot import DxRobot
        self.owner = int(owner)
        # The account's login MIS this session runs as; a switch replaces the session.
        self.login_mis = login_mis
        self.store = store or ChannelStore(owner)
        self._bridge_factory = bridge_factory or self._spawn
        self._robot_factory = robot_factory or DxRobot
        # (client ID, vault entry) -> the robot holding its cached access token.
        self._robot: tuple[tuple[str, str], Any] | None = None
        self._bridge: DxBridge | None = None
        self._self_uid = ''
        # Set once the first catch-up attempt of a connection ended: the outbox may send.
        self._connected = threading.Event()
        # The bridge's last status was ``connected`` (no loss reported since).
        self._bridge_connected = False
        self._connected_at_ms: int | None = None
        self._stopping = threading.Event()
        self._kicked = False
        # The engine refusal that showed this session's launch ticket expired; the session then ends (C1).
        self._expired_ticket: str | None = None
        self._frontier_ms: int | None = None
        # Owed from each ``connected`` and after any failed ingest until a catch-up
        # completes. While owed, live events are routed at once but leave the thread
        # watermarks to that catch-up, so its replay still covers the gap (C1).
        self._catch_up_owed = False
        # A retry is queued for the ingest thread (guarded by _catch_up_lock).
        self._catch_up_queued = False
        self._catch_up_lock = threading.Lock()
        # The (state, detail) this session last stored, to restore one a storage failure lost.
        self._reported: tuple[str, str] | None = None
        self._wake = threading.Condition()
        self._wake_pending = False
        self._threads: list[threading.Thread] = []

    def _spawn(self, owner: int) -> DxBridge:
        spec = launch_spec(owner)
        if not spec.installed:
            raise ChannelError('skill_missing', f'The {SKILL_NAME} skill is not installed for this owner')
        return DxBridge(_argv(spec), env=launcher_session(owner, self.login_mis).environment,
                        cwd=spec.skill_dir)

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
        return (not self._stopping.is_set() and self._expired_ticket is None and self._bridge is not None
                and self._bridge.alive())

    def outcome(self) -> tuple[str, str]:
        """``(state, detail)`` after the bridge exited (supervisor restart policy).

        The detail names the launcher's public refusal when it refused to start
        the bridge; a refusal of the SSO step's own runtime is
        ``runtime_unavailable``, which no login can fix.
        """
        code = self._bridge.returncode if self._bridge else None
        refusal = self._bridge.refusal if self._bridge else None
        named = f': {refusal.text}' if refusal else ''
        if code == SESSION_REQUIRED_EXIT:
            return 'needs_login', '大象 SSO session needs the owner to log in (launcher exit 42)' + named
        if self._kicked or code == KICKED_EXIT:
            return 'kicked', '大象 kicked this session off (another login took it)'
        if self._expired_ticket is not None:
            return 'disconnected', EXPIRED_TICKET_OUTCOME.format(refusal=self._expired_ticket)
        if refusal is not None and refusal.runtime:
            return 'runtime_unavailable', f'bridge exited ({code}){named}'
        return 'disconnected', f'bridge exited ({code}){named}'

    def stop(self, timeout: float) -> bool:
        self._stopping.set()
        self._connected.clear()
        from lib.channels import outbox
        outbox.register_executor(self.owner, None, channel='dx')
        self.wake_executor()
        if self._bridge is not None:
            self._bridge.events.put(None)
        stopped = self._bridge.close(timeout) if self._bridge else True
        for thread in self._threads:
            thread.join(timeout)
        if not stopped or any(thread.is_alive() for thread in self._threads):
            return False
        self.checkpoint()
        return True

    def tick(self) -> None:
        """Called from the supervisor's existing maintenance tick."""
        self.checkpoint()
        self._queue_owed_catch_up()
        self.wake_executor()

    def _queue_owed_catch_up(self) -> None:
        """Retry an owed catch-up on the ingest thread, behind the events already received.

        The supervisor's tick is the only retry cadence (no timer, backoff or
        retry count); a reconnect's ``connected`` also catches up.
        """
        bridge = self._bridge
        if bridge is None or not self._catch_up_owed or not self._connected.is_set():
            return
        with self._catch_up_lock:
            if self._catch_up_queued:
                return
            self._catch_up_queued = True
        bridge.events.put({'type': _CATCH_UP_DUE})

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
        bridge = self._bridge
        while not self._stopping.is_set():
            item = bridge.events.get()
            if item is None or self._stopping.is_set():
                return
            try:
                self._handle(item)
            except Exception as error:
                if self._stopping.is_set():
                    return
                # The connection is not at fault: the event waits for the owed
                # catch-up, which replays it from the unmoved watermark (C1).
                self._degraded('ingest failed', error)

    def _handle(self, item: dict[str, Any]) -> None:
        from lib.channels.ingress import ingest
        kind = item.get('type')
        if kind == 'status':
            self._on_status(item)
        elif kind == _CATCH_UP_DUE:
            with self._catch_up_lock:
                self._catch_up_queued = False
            if self._catch_up_owed and self._connected.is_set():
                self._attempt_catch_up()
        elif kind == 'message':
            event = event_from_live(item.get('message') or {}, source=str(item.get('source') or 'im'),
                                    self_uid=self._self_uid, quotes=item.get('quotes'),
                                    own_connection=bool(item.get('ownConnection')),
                                    host_sent=item.get('hostSent') is True)
            if event is not None:
                ingest(self.owner, event, store=self.store, media=self,
                       advance_watermark=not self._catch_up_owed)

    def _on_status(self, item: dict[str, Any]) -> None:
        status = item.get('status')
        if status == 'connected':
            self._self_uid = str(item.get('selfUid') or '')
            self._bridge_connected = True
            self._catch_up_owed = True
            self._connected_at_ms = now_ms()
            self._reported = None  # this connection's status is not stored yet
            logger.info('[Channels] dx session connected owner=%s', self.owner)
            self._report('connected', '', connected_at_ms=self._connected_at_ms)
            self._attempt_catch_up()
            self._connected.set()
            self.wake_executor()
        elif status == 'kicked':
            self._kicked = True
            self._bridge_connected = False
            self._connected.clear()
            self._report('kicked', str(item.get('detail') or ''))
        elif status in ('disconnected', 'failed'):
            self._bridge_connected = False
            self._connected.clear()
            self._report('disconnected' if status == 'disconnected' else 'error', str(item.get('detail') or ''))

    def _attempt_catch_up(self) -> None:
        """Catch up now; a failure leaves the catch-up owed and the live session running."""
        try:
            self.catch_up()
        except Exception as error:
            self._degraded('catch-up failed', error)
            return
        if self._reported != ('connected', ''):
            self._report('connected', '', connected_at_ms=self._connected_at_ms)

    def _degraded(self, failure: str, error: Exception) -> None:
        """Keep the live session after ``failure``; the owed catch-up replays what it missed.

        Ending the session would drop its queued pushes and buy a relaunch (SSO
        exchange, login) whose own catch-up meets the same failure (C1).
        """
        self._catch_up_owed = True
        if self._stopping.is_set() or isinstance(error, BridgeClosed):
            return  # stopping, or the bridge ended: its exit is the outcome to report
        typed = isinstance(error, (ChannelError, StorageError))
        cause = f'{error.code}: {error}' if typed else type(error).__name__
        logger.warning('[Channels] dx %s owner=%s: %s; retried at the next tick', failure, self.owner, cause,
                       exc_info=not typed)
        if self._bridge_connected:
            self._report('connected', f'{failure} ({cause}); retried at the next tick')

    def _report(self, state: str, detail: str, *, connected_at_ms: int | None = None) -> None:
        """Store a status for Settings and the Router Watchdog, best effort.

        A storage outage must not end a live session; ``_attempt_catch_up``
        restores a lost ``connected`` once storage answers again.
        """
        try:
            self.store.set_status('dx', state, detail, connected_at_ms=connected_at_ms)
        except Exception as error:
            logger.warning('[Channels] dx status %s not stored owner=%s: %s', state, self.owner,
                           getattr(error, 'code', type(error).__name__))
            return
        self._reported = (state, detail)

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
            self._catch_up_owed = False
            return
        replay: list[ChannelEvent] = []
        sessions = self._sessions(int(frontier), started)
        for session in sorted(sessions, key=lambda s: int(s.get('lastMessageServerTime') or 0)):
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
            ingest_batch(self.owner, replay, store=self.store, media=self)
        self._frontier_ms = max(self._frontier_ms or 0, started)
        self.checkpoint()
        self._catch_up_owed = False
        logger.info('[Channels] dx catch-up completed owner=%s sessions=%d replayed=%d since=%s took_ms=%d',
                    self.owner, len(sessions), len(replay), frontier, now_ms() - started)

    def _sessions(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        response = self._bridge.request('sessions', startMs=start_ms, endMs=end_ms,
                                        limit=SESSION_LIST_LIMIT, types=list(_SESSION_KINDS))
        if not response.get('ok'):
            self._note_refusal(_engine_refusal(response))
            raise ChannelError('sessions_failed', _engine_refusal(response))
        sessions = list((response.get('result') or {}).get('sessions') or [])
        if len(sessions) >= SESSION_LIST_LIMIT and end_ms - start_ms > 1:
            middle = (start_ms + end_ms) // 2
            merged = {(s.get('type'), s.get('id')): s for s in self._sessions(start_ms, middle)}
            merged.update({(s.get('type'), s.get('id')): s for s in self._sessions(middle, end_ms)})
            return list(merged.values())
        return sessions

    def _history_pages(self, thread: ThreadKey):
        try:
            yield from history_pages(self._bridge, thread)
        except ChannelError as error:
            self._note_refusal(str(error))
            raise

    def _note_refusal(self, refusal: str) -> None:
        """End this session when 大象 refused its launch ticket (``_EXPIRED_TICKET``).

        ``alive()`` turns false, and the supervisor's next tick relaunches the
        bridge, whose launcher exchanges a fresh ticket. Sends keep going over
        the live WebSocket until then. Any other refusal keeps the session (C1).
        """
        if self._expired_ticket is None and _EXPIRED_TICKET.search(refusal):
            self._expired_ticket = refusal
            logger.warning('[Channels] dx SSO ticket expired owner=%s: %s; the next tick relaunches the bridge',
                           self.owner, refusal)

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
    def _account_robot(self, thread: ThreadKey):
        """``(robot, account)`` when ``thread`` is the account's robot thread, else ``(None, account)``."""
        from lib.channels.dx.robot import effective_robot, robot_secret
        from lib.channels.policy import robot_thread
        account = self.store.account('dx') or {}
        if robot_thread(account) != str(thread):
            return None, account
        config = effective_robot(account)
        credential = config.get('credential') or {}
        key = (str(config['clientId']), 'built-in' if credential.get('builtIn') else str(credential.get('name') or ''))
        if self._robot is None or self._robot[0] != key:
            self._robot = (key, self._robot_factory(key[0], robot_secret(config)))
        return self._robot[1], account

    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]:
        if not self._connected.is_set():
            raise BridgeClosed('The 大象 session is not connected')
        from lib.channels.dx.robot import (
            RobotRefused, RobotThrottled, binding_refusal, deliver_text, record_robot_error, robot_notice,
        )
        robot, account = self._account_robot(thread)
        if robot is not None:
            # A group send has no documented quote type: the robot posts plain markdown.
            try:
                result = deliver_text(self.store, robot, thread, text, item_id=marker,
                                      refusal=binding_refusal(account, self._self_uid))
            except RobotThrottled as error:
                # The application's quota is shared by every server holding it: send now as the owner.
                logger.warning('[Channels] dx robot rate-limited item=%s; sending as the owner', marker)
                text = error.unsent
            except RobotRefused as error:
                logger.warning('[Channels] dx robot refused item=%s: %s; sending as the owner',
                               marker, error.code)
                if record_robot_error(self.store, error):
                    robot_notice(self.store, thread, error)
                text = error.unsent
            else:
                if result['ok'] and record_robot_error(self.store, None):
                    robot_notice(self.store, thread, None)
                return result
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

    def send_attachment(self, thread: ThreadKey, path: str, *, kind: str, marker: str) -> dict[str, Any]:
        """Upload and send one file as the owner: an image message or a file message (C10).

        The engine uploads ``path`` to 大象's own storage and sends the native
        message over this session; the file keeps its name. An answer of the
        IM server that refuses the send is definite; a missing answer is not.
        """
        if not self._connected.is_set():
            raise BridgeClosed('The 大象 session is not connected')
        response = self._bridge.request('send_image' if kind == 'image' else 'send_attachment',
                                        **_target(thread), filePath=path,
                                        extension=json.dumps({OUTBOX_MARKER_FIELD: marker}))
        result = response.get('result') or {}
        error = response.get('error')
        if not response.get('ok') and isinstance(result.get('ack'), dict):
            error = {'code': 'attachment_rejected',
                     'message': f'大象 refused the message (rescode {result["ack"].get("rescode")})'}
        return {'ok': bool(response.get('ok')), 'externalId': _text(result.get('serverMsgId')),
                'clientId': _text(result.get('msgUuid')), 'error': error}

    def history_from(self, thread: ThreadKey, external_id: str) -> list[ChannelEvent]:
        """The thread's messages from ``external_id`` on, read back to the first older one (C10).

        Message IDs are time-ordered snowflakes issued by 大象, so unlike a
        window by local attempt time this one cannot miss a message to clock
        skew: a message with that ID or a later one is here if 大象 has it.
        """
        if not self._connected.is_set():
            raise BridgeClosed('The 大象 session is not connected')
        floor = int(external_id)
        found: list[ChannelEvent] = []
        for rows in self._history_pages(thread):
            older = False
            for row in rows:
                event = event_from_history(row, thread)
                if event is None:
                    continue
                if int(event.external_id) < floor:
                    older = True
                else:
                    found.append(event)
            if older:
                break
        return found

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

    # media port (lib.channels.media, C9) -----------------------------
    def download_media(self, item: ChannelMedia, path: str) -> dict[str, Any]:
        """Fetch one original through this session's bridge into ``path``.

        Only the Web SDK's own file hosts are fetched; the bridge adds the
        connection's file headers there. The URL and file token never reach a
        log, a record or the returned detail (only a foreign host's name does).
        """
        if not item.source:
            return {'ok': False, 'detail': 'the message carries no download address'}
        if not item.source.startswith(FILE_URL_PREFIXES):
            host = urlsplit(item.source).hostname or 'an unknown host'
            return {'ok': False, 'detail': f'stored outside 大象 ({host}); not downloaded'}
        response = self._bridge.request('download', url=item.source, token=item.token, path=path,
                                        credentialPrefixes=list(FILE_URL_PREFIXES))
        if not response.get('ok'):
            self._note_refusal(_engine_refusal(response))
            code = _text((response.get('error') or {}).get('code')) or 'error'
            return {'ok': False, 'detail': f'download failed ({code})'}
        content_type = _text((response.get('result') or {}).get('contentType'))
        if (content_type.split(';', 1)[0].strip().lower() == 'text/html'
                and not item.name.lower().endswith(('.html', '.htm'))):
            return {'ok': False, 'detail': 'download returned a web page instead of the file '
                                           '(大象 may need a new login)'}
        return {'ok': True, 'contentType': content_type}

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
           'event_from_history', 'event_from_live', 'history_pages', 'launch_spec', 'launcher_session',
           'resolve_identity', 'svr_time_from_mid', 'thread_from_session']
