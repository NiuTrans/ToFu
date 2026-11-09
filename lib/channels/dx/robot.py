"""大象 open-platform robot: the account's outbound notification identity (C6).

Responsibility: post the group command chat's outbox text under the owner's
notification robot, so the owner's devices notify (a message the owner's own
account sends never does), and create the command chat group with the robot
in it. Inbound stays on the owner's bridge. Endpoints, signing and payloads
follow the official documents named in the contract's ``x-dxRobot``: the
client_secret_jwt token grant (应用认证接入指引 §3.2.1/§3.2.4),
``sendGroupMsgByRobot`` (大象消息 §2.2), ``createGroup`` (大象群管理 §1),
message bodies (消息类型与消息体字段) and status codes (错误码,
https://km.internal.example.com/collabpage/719464490).

The robot is the one configured in Settings, else the Internal package's
built-in application (``builtinCredentialFile``, written only by an Internal
export; source checkouts and the Open edition have none).

One application serves every server that holds it, so the robot posts only
for the 大象 account this server's own live session is logged in as, into the
command chat bound to that account (``robot_binding``: the account created the
group, or its own session saw that account's message there), and the shared
rate limit sends the rest as the owner at once (``RobotThrottled``).

Messages travel as ``RobotMessage``, the documented ``SendMsgInfo``.
``text_messages`` builds text. Image and file sends add builders for the
documented ``image``/``file`` bodies (their URLs come from the caller's own
storage: IM keeps no files for open-platform sends) and post through the same
``DxRobot.post_group``.

Entry points: ``DxRobot`` (token cache, ``post_group``, ``create_group``,
``check_credentials``), ``text_parts``, ``text_messages``, ``deliver_text``
(the sender port's robot path), ``robot_binding``, ``binding_refusal``,
``bind_owner_message``, ``effective_robot``, ``builtin_credential``,
``record_robot_error``, ``robot_notice``, ``robot_secret``, ``robot_view``,
``robot_update``, ``vault_entry_name``.
Dependencies: ``lib.http_transport.client.http_request``,
``lib.credentials_vault``, ``lib.channels.outbox`` (robot echo bookkeeping),
the channels contract (``x-dxRobot``, ``x-providerLimits``).
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from lib.channels.model import DX_ROBOT, PROVIDER_LIMITS, ChannelError, ThreadKey
from lib.channels.store import ChannelStore, now_ms
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'dx'
TOKEN_ENDPOINT = DX_ROBOT['tokenEndpoint']
AUDIENCE_CLIENT_ID = DX_ROBOT['audienceClientId']
ASSERTION_LIFETIME_S = DX_ROBOT['assertionLifetimeS']
API_BASE = DX_ROBOT['apiBase']
SEND_GROUP_PATH = DX_ROBOT['sendGroupPath']
CREATE_GROUP_PATH = DX_ROBOT['createGroupPath']
BUILTIN_CREDENTIAL_FILE = Path(__file__).resolve().parents[3] / DX_ROBOT['builtinCredentialFile']
TEXT_BYTES = PROVIDER_LIMITS['dxRobotTextBytes']
SEND_INTERVAL_S = 1 / PROVIDER_LIMITS['dxRobotSendsPerSecond']
# dxRobotSendsPerSecond (1s5次 per application and path) counts one-second windows: after
# a rate-limit answer this server asks the robot nothing until that window has passed.
RATE_WINDOW_S = 1.0
JWT_BEARER = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'

# What a 错误码 status proves about one send. Every other non-zero code is a
# refusal: nothing was posted and this robot cannot post it (application,
# permission, group membership or parameters).
TOKEN_REJECTED_CODES = frozenset({20004, 20005})  # the token expired or was not accepted
RATE_LIMITED_CODES = frozenset({10003})  # nothing posted; the limit passes
UNKNOWN_OUTCOME_CODES = frozenset({-1, -2, 10001, 10002})  # the server failed mid-way
# The owner's command-chat notices of a robot transition (robot_notice); not translated.
ROBOT_STOPPED_NOTICE = '通知机器人发不出消息了（{code}：{detail}），先改由你自己的账号发送；它恢复后会再告诉你。'
ROBOT_RECOVERED_NOTICE = '通知机器人恢复了，消息重新由它发出。'
# Refusals of the binding (C6, one robot many servers): the robot posts for no one else.
UNBOUND = 'robot_unbound'
STALE_BINDING = 'robot_binding_stale'
_UPDATE_FIELDS = frozenset({'clientId', 'secret', 'credentialName'})

Transport = Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]


class RobotRefused(ChannelError):
    """This robot cannot post it; the owner's account sends what the robot has not posted.

    ``unsent`` is that text: the whole item when the robot posted none of it,
    else the parts after the ones it posted.
    """

    def __init__(self, code: str, message: str, *, unsent: str = ''):
        super().__init__(code, message)
        self.unsent = unsent


class RobotDeferred(ChannelError):
    """Nothing was posted for a passing reason: the application's rate limit (10003)."""


class RobotThrottled(RobotRefused):
    """The application's shared rate limit refused a post: ``unsent`` goes as the owner now.

    One robot application serves every server that holds it, so its quota is
    not this server's to wait for. The robot is not broken: nothing is recorded
    on the account and the owner is not told.
    """


class RobotOutcomeUnknown(ChannelError):
    """The post may have happened; the outbox reconciles the item against the thread history."""


def http_transport(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """Production transport: the shared proxy-aware client, redirects left to the caller."""
    from lib.http_transport.client import http_request
    response = http_request(method, url, headers=headers, data=body, allow_redirects=False)
    try:
        return response.status_code, response.content
    finally:
        response.close()


@dataclass(frozen=True)
class RobotMessage:
    """One documented ``SendMsgInfo``: a message type with its body and extension objects."""

    type: str
    body: dict[str, Any]
    extension: dict[str, Any] = field(default_factory=dict)

    def send_msg_info(self) -> dict[str, Any]:
        return {'type': self.type, 'body': json.dumps(self.body, ensure_ascii=False),
                'extension': json.dumps(self.extension, ensure_ascii=False), 'isDynamicMsg': False}


def text_parts(text: str) -> list[str]:
    """``text`` cut into consecutive parts of at most ``TEXT_BYTES`` UTF-8 bytes.

    A part ends after its last line break when it has one, keeping lines and
    markdown blocks whole; otherwise after the last whole character that fits.
    """
    parts, rest = [], text
    while len(rest.encode('utf-8')) > TEXT_BYTES:
        head = rest.encode('utf-8')[:TEXT_BYTES].decode('utf-8', 'ignore')
        cut = head.rfind('\n') + 1 or len(head)
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest or not parts:
        parts.append(rest)
    return parts


def text_messages(text: str) -> list[RobotMessage]:
    """Markdown text messages (extension ``fileType: markdown``, 消息extension) for ``text``."""
    return [RobotMessage('text', {'text': part}, {'fileType': 'markdown'}) for part in text_parts(text)]


def posted_text(message: RobotMessage) -> str:
    """The text the owner's connection reports for ``message`` (adapters strip it)."""
    return str(message.body.get('text') or '').strip()


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode('utf-8')) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class DxRobot:
    """One robot application: its cached access token and paced posts."""

    def __init__(self, client_id: str, secret: Callable[[], str], *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.client_id = client_id
        self._secret = secret
        self._transport = transport or http_transport
        self._clock, self._wall_clock, self._sleep = clock, wall_clock, sleep
        self._token: tuple[str, str, float] | None = None  # (token type, token, clock expiry)
        # TODO(enterprise): pacing and back-off are per robot object, one per owner session;
        # several owners in one process would share the application's quota without sharing these.
        self._last_post: float | None = None
        self._throttled_until: float | None = None  # no request before this clock time

    def _assertion(self) -> str:
        """client_secret_jwt (§3.2.4): an HS256 JWT signed with the client secret."""
        issued = int(self._wall_clock())

        def encode(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
        header = {'alg': 'HS256', 'typ': 'JWT'}
        claims = {'sub': self.client_id, 'iss': self.client_id, 'aud': [TOKEN_ENDPOINT],
                  'exp': issued + ASSERTION_LIFETIME_S, 'iat': issued, 'jti': str(uuid.uuid4())}
        signing_input = '.'.join(encode(json.dumps(part, separators=(',', ':')).encode('utf-8'))
                                 for part in (header, claims))
        signature = hmac.new(self._secret().encode('utf-8'), signing_input.encode('ascii'),
                             hashlib.sha256).digest()
        return f'{signing_input}.{encode(signature)}'

    def _access_token(self) -> tuple[str, str]:
        """A client_credentials token for the 大象 audience (§3.2.1), reused until ``expires_in``."""
        if self._token is not None and self._clock() < self._token[2]:
            return self._token[0], self._token[1]
        form = urlencode({'grant_type': 'client_credentials', 'scope': f'client_id:{AUDIENCE_CLIENT_ID}',
                          'client_id': self.client_id, 'client_assertion_type': JWT_BEARER,
                          'client_assertion': self._assertion()}).encode('ascii')
        try:
            status, raw = self._transport('POST', TOKEN_ENDPOINT,
                                          {'Content-Type': 'application/x-www-form-urlencoded'}, form)
        except Exception as error:
            raise RobotRefused('token_unavailable',
                               f'The SSO token endpoint is unreachable ({type(error).__name__})') from error
        body = _json(raw)
        token, kind = str(body.get('access_token') or ''), str(body.get('token_type') or '')
        if status != 200 or not token or not kind:
            reason = ' '.join(str(part) for part in (f'HTTP {status}', body.get('error') or '') if part)
            raise RobotRefused('token_rejected', f'The SSO token endpoint refused the robot credentials ({reason})')
        self._token = (kind, token, self._clock() + int(body.get('expires_in') or 0))
        return kind, token

    def check_credentials(self) -> None:
        """Mint a token: proves the client ID and secret without posting anything."""
        self._token = None
        self._access_token()

    def _post(self, path: str, payload: bytes, authorization: str) -> tuple[int, dict[str, Any]]:
        if self._last_post is not None:
            wait = self._last_post + SEND_INTERVAL_S - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            status, raw = self._transport('POST', API_BASE + path,
                                          {'Authorization': authorization,
                                           'Content-Type': 'application/json; charset=utf-8'}, payload)
        except Exception as error:
            raise RobotOutcomeUnknown('robot_unknown',
                                      f'The robot request got no response ({type(error).__name__})') from error
        finally:
            self._last_post = self._clock()
        return status, _json(raw)

    def _call(self, path: str, request: dict[str, Any]) -> tuple[int, dict[str, Any], int | None, str]:
        """POST ``request`` to the open-platform ``path``: ``(HTTP status, body, status code, status msg)``.

        After a rate-limit answer nothing is requested for one documented
        window: the quota belongs to every server holding the application.
        """
        if self._throttled_until is not None and self._clock() < self._throttled_until:
            raise RobotDeferred('robot_rate_limited',
                                'The robot application reached its shared rate limit in this second')
        payload = json.dumps(request, ensure_ascii=False).encode('utf-8')
        renewed = False
        while True:
            kind, token = self._access_token()
            # The token type the endpoint issued ("Bearer") names the Authorization scheme.
            status, body = self._post(path, payload, f'{kind} {token}')
            result = body.get('status') if isinstance(body.get('status'), dict) else {}
            code = result.get('code') if isinstance(result.get('code'), int) else None
            if code in TOKEN_REJECTED_CODES and not renewed:
                # Expired between our clock and the server's: one fresh token, one more request.
                self._token, renewed = None, True
                continue
            if code in RATE_LIMITED_CODES:
                self._throttled_until = self._clock() + RATE_WINDOW_S
            return status, body, code, str(result.get('msg') or '')

    @staticmethod
    def _failure(status: int, code: int | None, detail: str, action: str) -> ChannelError:
        """What a non-success answer to ``action`` proves (错误码)."""
        if code is None:
            return RobotOutcomeUnknown('robot_unknown', f'The robot {action} answered HTTP {status} without a status')
        if code in RATE_LIMITED_CODES:
            return RobotDeferred('robot_rate_limited', f'The open platform rate limit refused the {action}: {detail}')
        if code in UNKNOWN_OUTCOME_CODES:
            return RobotOutcomeUnknown('robot_unknown', f'The open platform failed the {action} ({code} {detail})')
        return RobotRefused(f'robot_{code}', f'The open platform refused the robot {action}: {detail}')

    def create_group(self, name: str, *, owner_uid: str) -> str:
        """Create a group the robot is a member of, owned by ``owner_uid`` (大象群管理 §1); returns its gid.

        Only a real user can be the 群主 (without one the robot would be).
        A success without a gid proves nothing about the group: unknown.
        """
        status, body, code, detail = self._call(CREATE_GROUP_PATH, {'name': name, 'owner': int(owner_uid)})
        if code == 0:
            gid = str(body.get('gid') or '')
            if gid.isdigit():
                return gid
            raise RobotOutcomeUnknown('robot_unknown', 'The open platform answered the group creation without a gid')
        raise self._failure(status, code, detail, 'group creation')

    def post_group(self, gid: str, message: RobotMessage) -> str:
        """Post ``message`` to group ``gid`` (大象消息 §2.2); returns its message ID."""
        status, body, code, detail = self._call(
            SEND_GROUP_PATH, {'gid': int(gid), 'sendMsgInfo': message.send_msg_info()})
        if code == 0:
            data = body.get('data') if isinstance(body.get('data'), dict) else {}
            message_id = str((data.get('messageMap') or {}).get(str(int(gid))) or '')
            if message_id:
                return message_id
            reason = (data.get('failMap') or {}).get(str(int(gid))) or 'no message ID'
            raise RobotRefused('robot_not_sent', f'The open platform did not send the message: {reason}')
        raise self._failure(status, code, detail, 'send')


def deliver_text(store: ChannelStore, robot: DxRobot, thread: ThreadKey, text: str, *,
                 item_id: str, refusal: RobotRefused | None = None) -> dict[str, Any]:
    """The outbox sender port's robot path for one item (C5/C6).

    Before posting, the item records the exact texts the owner's connection
    will receive from the robot; each posted part is recorded at once
    (``robotPosted``) and its message ID remembered, so no echo of them is
    input (``outbox.robot_echo``) and a later attempt continues after them.
    Raises ``RobotRefused`` when the robot cannot post the rest, including
    ``refusal`` (the binding's, ``binding_refusal``), and ``RobotThrottled``
    on the application's shared rate limit: their ``unsent`` text is what the
    caller sends as the owner instead.
    """
    from lib.channels import outbox
    messages = text_messages(text)
    item, _ = store.outbox_item(item_id)
    posted = list(((item.extra.get('robotPosted') if item is not None else None) or ())[:len(messages)])

    def unsent() -> str:
        return ''.join(str(message.body['text']) for message in messages[len(posted):])
    if refusal is not None:
        if not posted and item is not None and item.extra.get('robotTexts'):
            outbox.record_robot_texts(store, item_id, ())  # an earlier attempt's texts: it goes as the owner
        raise RobotRefused(refusal.code, str(refusal), unsent=unsent())
    outbox.record_robot_texts(store, item_id, [posted_text(message) for message in messages])
    try:
        for message in messages[len(posted):]:
            posted.append(robot.post_group(thread.external_id, message))
            outbox.record_robot_posted(store, item_id, posted)
            store.remember_echo(thread.channel, outbox.ROBOT_ECHO, external_id=posted[-1], client_id=None)
    except (RobotRefused, RobotDeferred) as error:
        if not posted:
            outbox.record_robot_texts(store, item_id, ())
        refused = RobotThrottled if isinstance(error, RobotDeferred) else RobotRefused
        raise refused(error.code, str(error), unsent=unsent()) from error
    except RobotOutcomeUnknown as error:
        # The item reconciles; its next attempt continues after the posted parts.
        return {'ok': False, 'externalId': posted[-1] if posted else '', 'clientId': '',
                'error': {'code': error.code, 'message': str(error)}}
    return {'ok': True, 'externalId': posted[-1], 'clientId': '', 'error': None}


def robot_binding(account: dict[str, Any] | None) -> dict[str, Any] | None:
    """What binds the account's command chat to its 大象 identity for the robot, or None (C6).

    Both proofs come from this server's own session, so the identity is a
    member of the chat: the account created the group for its identity
    (``commandChatCreation``, cleared with the identity when the account
    switches 大象 accounts), or its session received that identity's own
    message there (``robotBinding``, written by ``bind_owner_message``).
    """
    account = account or {}
    chat = str(account.get('commandChat') or '')
    uid = str((account.get('identity') or {}).get('uid') or '')
    if not chat or not uid:
        return None
    creation = account.get('commandChatCreation') or {}
    if creation.get('state') == 'created' and chat == f"{CHANNEL}:gid:{creation.get('gid')}":
        return {'thread': chat, 'uid': uid, 'proof': 'created'}
    binding = account.get('robotBinding') or {}
    if binding.get('thread') == chat and binding.get('uid') == uid:
        return binding
    return None


def binding_refusal(account: dict[str, Any] | None, session_uid: str) -> RobotRefused | None:
    """Why the robot may not post the account's command chat for this server now, or None (C6).

    One robot application serves every server that holds it, so it posts
    only for the 大象 account this server's own live session is logged in as
    (``session_uid``, the bridge's ``connected`` uid), and only into the
    command chat bound to that account.
    """
    account = account or {}
    uid = str((account.get('identity') or {}).get('uid') or '')
    if not uid or session_uid != uid:
        return RobotRefused(STALE_BINDING, "This server's 大象 session is not logged in as the account "
                                           'the robot is bound to')
    if robot_binding(account) is not None:
        return None
    recorded = account.get('robotBinding') or {}
    if recorded.get('uid') and recorded.get('uid') != uid:
        return RobotRefused(STALE_BINDING, "The robot is bound to another 大象 account than this server's")
    return RobotRefused(UNBOUND, 'The robot posts into this chat once it is bound to your 大象 account: '
                                 'send one message here')


def bind_owner_message(store: ChannelStore, account: dict[str, Any], event) -> dict[str, Any]:
    """Bind the command chat to the account's identity on that identity's own message there (C6).

    ``event`` is an owner command in the robot's thread that this server's own
    session received, which proves its sender is a member of the chat.
    Returns the account, unchanged when the chat is already bound.
    """
    if robot_binding(account) is not None:
        return account
    thread = str(event.thread)

    def change(current):
        identity = current.get('identity') or {}
        if (current.get('commandChat') != thread or str(identity.get('uid') or '') != event.sender_id
                or robot_binding(current) is not None):
            return current
        current['robotBinding'] = {'thread': thread, 'uid': event.sender_id, 'mis': str(identity.get('mis') or ''),
                                   'proof': 'owner_message', 'eventId': event.event_id, 'atMs': now_ms()}
        return current
    return store.update_account(CHANNEL, change)


def record_robot_error(store: ChannelStore, error: ChannelError | None) -> bool:
    """Record the robot's last refusal on the account (``DxRobotError``), or clear it.

    Returns whether this stopped a working robot or cleared a recorded
    refusal: the transitions the owner is told about (``robot_notice``). A
    built-in robot keeps its refusal on an otherwise empty ``robot`` record.
    """
    changed = {'value': False}

    def change(account):
        robot = account.get('robot') or {}
        changed['value'] = bool(robot.get('error')) == (error is None)
        if error is None:
            robot.pop('error', None)
        else:
            robot['error'] = {'code': error.code, 'detail': str(error), 'atMs': now_ms()}
        account['robot'] = robot or None
        return account
    store.update_account(CHANNEL, change)
    return changed['value']


def robot_notice(store: ChannelStore, thread: ThreadKey, error: ChannelError | None) -> None:
    """Tell the owner in the command chat that the robot stopped posting, and why, or posts again.

    Called once per ``record_robot_error`` transition. The notice is an
    ordinary command-chat item, so it too tries the robot first; nothing
    retries or repeats it.
    """
    from lib.channels import outbox
    if error is None:
        text, key = ROBOT_RECOVERED_NOTICE, f'robot-notice:up:{now_ms()}'
    else:
        text = ROBOT_STOPPED_NOTICE.format(code=error.code, detail=str(error))
        key = f'robot-notice:down:{now_ms()}'
    try:
        outbox.enqueue(store.owner, str(thread), text, idempotency_key=key, store=store, authority='nonhuman',
                       origin={'kind': 'robot_notice', 'conversationId': '', 'turnId': ''})
    except ChannelError as refused:
        logger.warning('[Channels] robot notice not enqueued: %s', refused.code)


@functools.lru_cache(maxsize=None)
def _read_builtin(path: Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not (isinstance(value, dict) and _plain(value.get('clientId')) and _plain(value.get('secret'))):
        raise ChannelError('builtin_robot_invalid', f'{path.name} must hold the built-in clientId and secret')
    return {'clientId': value['clientId'], 'secret': value['secret']}


def builtin_credential() -> dict[str, str] | None:
    """The Internal package's built-in robot application ``{clientId, secret}``, or None.

    The package file changes only with a new release (and process), so it is
    read once per path.
    """
    return _read_builtin(BUILTIN_CREDENTIAL_FILE)


def effective_robot(account: dict[str, Any] | None) -> dict[str, Any] | None:
    """The account's robot record: the Settings one, else the built-in application, else None.

    A built-in record has ``credential.builtIn`` and the account's recorded refusal.
    """
    robot = (account or {}).get('robot') or {}
    if robot.get('clientId'):
        return robot
    builtin = builtin_credential()
    if builtin is None:
        return None
    return {'clientId': builtin['clientId'], 'credential': {'builtIn': True},
            **({'error': robot['error']} if robot.get('error') else {})}


def vault_entry_name(owner: int) -> str:
    """The owner's reserved vault entry for a pasted robot secret (hidden from agents)."""
    return f'channel.u{int(owner)}.{CHANNEL}.robot'


def robot_secret(robot: dict[str, Any] | None) -> Callable[[], str]:
    """Reads the robot's client secret (its vault entry, or the built-in package) at each token request."""
    credential = ((robot or {}).get('credential')) or {}
    if credential.get('builtIn'):
        def builtin_secret() -> str:
            builtin = builtin_credential()
            if builtin is None:
                raise RobotRefused('robot_credential_missing', 'The built-in robot credential is missing')
            return builtin['secret']
        return builtin_secret
    name = str(credential.get('name') or '')

    def read() -> str:
        from lib.credentials_vault import get_entry
        value = get_entry(name) if name else None
        if not value:
            raise RobotRefused('robot_credential_missing', f'No credential vault entry named {name or "(none)"}')
        return value
    return read


def _vault_hint(name: str) -> str:
    from lib.credentials_vault import list_entries
    return next((str(entry.get('hint') or '') for entry in list_entries() if entry.get('name') == name), '')


def robot_view(account: dict[str, Any] | None) -> dict[str, Any] | None:
    """Contract ``DxRobotView`` of the account's effective robot; never the secret."""
    robot = effective_robot(account)
    if robot is None:
        return None
    credential = robot.get('credential') or {}
    if credential.get('builtIn'):
        return {'clientId': str(robot['clientId']), 'credentialName': '', 'credentialHint': '',
                'managed': False, 'builtIn': True, 'error': robot.get('error') or None}
    name = str(credential.get('name') or '')
    return {'clientId': str(robot['clientId']), 'credentialName': name, 'credentialHint': _vault_hint(name),
            'managed': bool(credential.get('managed')), 'builtIn': False, 'error': robot.get('error') or None}


def _plain(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 255 and not any(char.isspace() for char in value)


def check_credentials(client_id: str, secret: str) -> None:
    DxRobot(client_id, lambda: secret).check_credentials()


def robot_update(owner: int, value: Any, current: dict[str, Any] | None, *,
                 probe: Callable[[str, str], None] | None = None) -> dict[str, Any] | None:
    """Apply ``DxRobotUpdate`` (or null); returns the account's new ``robot`` record.

    The secret is verified with a token request before it is stored; a pasted
    one goes to the owner's reserved vault entry, and removing or replacing
    the robot deletes that entry. Account records never hold a secret.
    """
    from lib.credentials_vault import delete_entry, get_entry, normalize_name, set_entry
    from lib.error_envelope import BadRequest
    current = current or {}
    previous = current.get('credential') or {}
    own = vault_entry_name(owner)
    if value is None:
        if previous.get('managed'):
            delete_entry(str(previous['name']))
        return None
    if not isinstance(value, dict) or set(value) - _UPDATE_FIELDS:
        raise ChannelError('invalid_update', 'robot takes clientId plus a secret or a credentialName')
    if not _plain(value.get('clientId')):
        raise ChannelError('invalid_update', 'robot.clientId must be the application client_id')
    if 'secret' in value and 'credentialName' in value:
        raise ChannelError('invalid_update', 'Give either robot.secret or robot.credentialName')
    if 'secret' in value:
        if not _plain(value['secret']):
            raise ChannelError('invalid_update', 'robot.secret must be the application client secret')
        credential, secret = {'name': own, 'managed': True}, value['secret']
    elif 'credentialName' in value:
        try:
            name = normalize_name(value['credentialName'])
        except BadRequest as error:
            raise ChannelError('invalid_update', str(error)) from error
        if name.startswith(('skill.', 'channel.')) and name != own:
            raise ChannelError('invalid_update', 'That credential vault entry is reserved')
        secret = get_entry(name)
        if not secret:
            raise ChannelError('credential_missing', f'No credential vault entry named {name}')
        credential = {'name': name, 'managed': name == own}
    else:
        credential = previous or None
        secret = get_entry(str(previous['name'])) if previous.get('name') else None
        if not credential or not secret:
            raise ChannelError('invalid_update', 'robot needs the application client secret')
    (probe or check_credentials)(value['clientId'], secret)
    if 'secret' in value:
        set_entry(own, secret, note=f'大象 notification robot secret (Tofu owner {int(owner)})')
    if previous.get('managed') and previous.get('name') != credential['name']:
        delete_entry(str(previous['name']))
    return {'clientId': value['clientId'], 'credential': credential}


__all__ = ['API_BASE', 'BUILTIN_CREDENTIAL_FILE', 'CREATE_GROUP_PATH', 'DxRobot', 'RATE_WINDOW_S', 'RobotDeferred',
           'RobotMessage', 'RobotOutcomeUnknown', 'RobotRefused', 'RobotThrottled', 'SEND_GROUP_PATH',
           'STALE_BINDING', 'TOKEN_ENDPOINT', 'UNBOUND', 'bind_owner_message', 'binding_refusal',
           'builtin_credential', 'check_credentials', 'deliver_text', 'effective_robot', 'http_transport',
           'posted_text', 'record_robot_error', 'robot_binding', 'robot_notice', 'robot_secret', 'robot_update',
           'robot_view', 'text_messages', 'text_parts', 'vault_entry_name']
