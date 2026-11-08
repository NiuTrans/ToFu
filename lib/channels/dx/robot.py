"""大象 open-platform robot: the account's outbound notification identity (C6).

Responsibility: post the group command chat's outbox text under the owner's
notification robot, so the owner's devices notify (a message the owner's own
account sends never does). Inbound stays on the owner's bridge. Endpoints,
signing and payloads follow the official documents named in the contract's
``x-dxRobot``: the client_secret_jwt token grant (应用认证接入指引
§3.2.1/§3.2.4), ``sendGroupMsgByRobot`` (大象消息 §2.2), message bodies
(消息类型与消息体字段) and status codes (错误码,
https://km.internal.example.com/collabpage/719464490).

Messages travel as ``RobotMessage``, the documented ``SendMsgInfo``.
``text_messages`` builds text. Image and file sends add builders for the
documented ``image``/``file`` bodies (their URLs come from the caller's own
storage: IM keeps no files for open-platform sends) and post through the same
``DxRobot.post_group``.

Entry points: ``DxRobot`` (token cache, ``post_group``, ``check_credentials``),
``text_parts``, ``text_messages``, ``deliver_text`` (the sender port's robot
path), ``record_robot_error``, ``robot_secret``, ``robot_view``,
``robot_update``, ``vault_entry_name``.
Dependencies: ``lib.http_transport.client.http_request``,
``lib.credentials_vault``, ``lib.channels.outbox`` (robot echo bookkeeping),
the channels contract (``x-dxRobot``, ``x-providerLimits``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

from lib.channels.model import DX_ROBOT, PROVIDER_LIMITS, ChannelError, ThreadKey
from lib.channels.store import ChannelStore, now_ms

CHANNEL = 'dx'
TOKEN_ENDPOINT = DX_ROBOT['tokenEndpoint']
AUDIENCE_CLIENT_ID = DX_ROBOT['audienceClientId']
ASSERTION_LIFETIME_S = DX_ROBOT['assertionLifetimeS']
API_BASE = DX_ROBOT['apiBase']
SEND_GROUP_PATH = DX_ROBOT['sendGroupPath']
TEXT_BYTES = PROVIDER_LIMITS['dxRobotTextBytes']
SEND_INTERVAL_S = 1 / PROVIDER_LIMITS['dxRobotSendsPerSecond']
JWT_BEARER = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'

# What a 错误码 status proves about one send. Every other non-zero code is a
# refusal: nothing was posted and this robot cannot post it (application,
# permission, group membership or parameters).
TOKEN_REJECTED_CODES = frozenset({20004, 20005})  # the token expired or was not accepted
RATE_LIMITED_CODES = frozenset({10003})  # nothing posted; the limit passes
UNKNOWN_OUTCOME_CODES = frozenset({-1, -2, 10001, 10002})  # the server failed mid-way
_UPDATE_FIELDS = frozenset({'clientId', 'secret', 'credentialName'})

Transport = Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]


class RobotRefused(ChannelError):
    """Nothing was posted and this robot cannot post it; the owner's account sends instead."""


class RobotDeferred(ChannelError):
    """Nothing was posted for a passing reason (the rate limit); the item is sent again."""


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
        self._last_post: float | None = None

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

    def _post(self, payload: bytes, authorization: str) -> tuple[int, dict[str, Any]]:
        if self._last_post is not None:
            wait = self._last_post + SEND_INTERVAL_S - self._clock()
            if wait > 0:
                self._sleep(wait)
        try:
            status, raw = self._transport('POST', API_BASE + SEND_GROUP_PATH,
                                          {'Authorization': authorization,
                                           'Content-Type': 'application/json; charset=utf-8'}, payload)
        except Exception as error:
            raise RobotOutcomeUnknown('robot_unknown',
                                      f'The robot send got no response ({type(error).__name__})') from error
        finally:
            self._last_post = self._clock()
        return status, _json(raw)

    def post_group(self, gid: str, message: RobotMessage) -> str:
        """Post ``message`` to group ``gid`` (大象消息 §2.2); returns its message ID."""
        payload = json.dumps({'gid': int(gid), 'sendMsgInfo': message.send_msg_info()},
                             ensure_ascii=False).encode('utf-8')
        renewed = False
        while True:
            kind, token = self._access_token()
            # The token type the endpoint issued ("Bearer") names the Authorization scheme.
            status, body = self._post(payload, f'{kind} {token}')
            result = body.get('status') if isinstance(body.get('status'), dict) else {}
            code = result.get('code') if isinstance(result.get('code'), int) else None
            if code in TOKEN_REJECTED_CODES and not renewed:
                # Expired between our clock and the server's: one fresh token, one more post.
                self._token, renewed = None, True
                continue
            break
        detail = str(result.get('msg') or '')
        if code == 0:
            data = body.get('data') if isinstance(body.get('data'), dict) else {}
            message_id = str((data.get('messageMap') or {}).get(str(int(gid))) or '')
            if message_id:
                return message_id
            reason = (data.get('failMap') or {}).get(str(int(gid))) or 'no message ID'
            raise RobotRefused('robot_not_sent', f'The open platform did not send the message: {reason}')
        if code is None:
            raise RobotOutcomeUnknown('robot_unknown', f'The robot send answered HTTP {status} without a status')
        if code in RATE_LIMITED_CODES:
            raise RobotDeferred('robot_rate_limited', f'The open platform rate limit refused the send: {detail}')
        if code in UNKNOWN_OUTCOME_CODES:
            raise RobotOutcomeUnknown('robot_unknown', f'The open platform failed the send ({code} {detail})')
        raise RobotRefused(f'robot_{code}', f'The open platform refused the robot send: {detail}')


def deliver_text(store: ChannelStore, robot: DxRobot, thread: ThreadKey, text: str, *,
                 item_id: str) -> dict[str, Any]:
    """The outbox sender port's robot path for one item (C5/C6).

    Before posting, the item records the exact texts the owner's connection
    will receive from the robot, and each posted message ID is remembered at
    once, so no echo of them is input (``outbox.robot_echo``). Raises
    ``RobotRefused`` only when nothing was posted, so the caller sends the
    item as the owner instead.
    """
    from lib.channels import outbox
    messages = text_messages(text)
    outbox.record_robot_texts(store, item_id, [posted_text(message) for message in messages])
    posted: list[str] = []
    try:
        for message in messages:
            posted.append(robot.post_group(thread.external_id, message))
            store.remember_echo(thread.channel, outbox.ROBOT_ECHO, external_id=posted[-1], client_id=None)
    except RobotRefused as error:
        if not posted:
            outbox.record_robot_texts(store, item_id, ())
            raise
        failure: ChannelError = error
    except (RobotDeferred, RobotOutcomeUnknown) as error:
        failure = error
    else:
        return {'ok': True, 'externalId': posted[-1], 'clientId': '', 'error': None}
    # A later part failed after earlier parts went out: the item reconciles.
    return {'ok': False, 'externalId': posted[-1] if posted else '', 'clientId': '',
            'error': {'code': failure.code, 'message': str(failure)}}


def record_robot_error(store: ChannelStore, error: ChannelError | None) -> None:
    """Record the robot's last refusal on the account (``DxRobotError``), or clear it."""
    def change(account):
        robot = account.get('robot')
        if robot:
            if error is None:
                robot.pop('error', None)
            else:
                robot['error'] = {'code': error.code, 'detail': str(error), 'atMs': now_ms()}
        return account
    store.update_account(CHANNEL, change)


def vault_entry_name(owner: int) -> str:
    """The owner's reserved vault entry for a pasted robot secret (hidden from agents)."""
    return f'channel.u{int(owner)}.{CHANNEL}.robot'


def robot_secret(robot: dict[str, Any] | None) -> Callable[[], str]:
    """Reads the robot's client secret from its vault entry at each token request."""
    name = str((((robot or {}).get('credential')) or {}).get('name') or '')

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
    """Contract ``DxRobotView`` of the account's robot; never the secret."""
    robot = (account or {}).get('robot') or {}
    if not robot.get('clientId'):
        return None
    credential = robot.get('credential') or {}
    name = str(credential.get('name') or '')
    return {'clientId': str(robot['clientId']), 'credentialName': name, 'credentialHint': _vault_hint(name),
            'managed': bool(credential.get('managed')), 'error': robot.get('error') or None}


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


__all__ = ['API_BASE', 'DxRobot', 'RobotDeferred', 'RobotMessage', 'RobotOutcomeUnknown', 'RobotRefused',
           'SEND_GROUP_PATH', 'TOKEN_ENDPOINT', 'check_credentials', 'deliver_text', 'http_transport',
           'posted_text', 'record_robot_error', 'robot_secret', 'robot_update', 'robot_view', 'text_messages',
           'text_parts', 'vault_entry_name']
