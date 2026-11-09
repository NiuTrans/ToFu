"""Bot platform accounts: Settings views and commands, credential custody (C12).

Responsibility: the owner-facing surface of the 企业微信, QQ and 微信 bot
accounts used by the HTTP routes. The view reports status, the bound owner
identity, the command chat, the credential's vault name and redacted hint, a
pending pairing code and a pending 微信 login QR code. The update enables or
disables the account, sets the 企业微信 BotID / QQ AppID and its secret (or
an existing vault entry), issues or cancels the pairing code, and starts or
cancels a 微信 QR login. A pasted secret is stored only in the owner-scoped
vault entry ``channel.u<owner>.<channel>.secret`` (a 微信 login's bot token
in ``….wechat.token``); ``lib.credentials_vault`` reserves ``channel.``
entries from agents. A QQ secret is verified with a token request before it
is stored; 企业微信 offers no side-effect-free check (a probe connection
would replace the running one), so its session reports a refusal as
``needs_login``.

Pairing (企业微信, QQ): the platforms' user IDs are pseudonymous per bot, so
the owner proves their account by sending the one-time code shown here to the
bot in a private chat (``lib.channels.bots.session``). The code is 40 random
bits from an alphabet without look-alike characters; it stays valid until it
is used or replaced.

Entry points: ``account_view``, ``update_account``, ``vault_entry_name``.
Dependencies: ``ChannelStore``, ``lib.credentials_vault``, ``lib.qr``,
``delivery.global_router_binding``, ``supervisor.notify_account_changed``,
the QQ token probe and the 微信 login (lazily).
"""
from __future__ import annotations

import secrets
from typing import Any

from lib.channels.model import BOT_CHANNELS, ChannelError, session_owns_status
from lib.channels.store import ChannelStore, now_ms

_UPDATE_FIELDS = frozenset({'enabled', 'appId', 'secret', 'credentialName', 'pair', 'login'})
_PAIRING_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
_UNSET = object()
_LABELS = {'wecom': '企业微信', 'qq': 'QQ', 'wechat': '微信'}


def vault_entry_name(owner: int, channel: str) -> str:
    return f'channel.u{int(owner)}.{channel}.' + ('token' if channel == 'wechat' else 'secret')


def new_pairing_code() -> str:
    chars = ''.join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))
    return f'{chars[:4]}-{chars[4:]}'


def _require_channel(channel: str) -> None:
    if channel not in BOT_CHANNELS:
        raise ChannelError('invalid_channel', f'channel must be one of {list(BOT_CHANNELS)}')


def _vault_hint(name: str) -> str:
    from lib.credentials_vault import list_entries
    return next((str(entry.get('hint') or '') for entry in list_entries() if entry.get('name') == name), '')


def _login_view(login: dict[str, Any] | None) -> dict[str, Any] | None:
    if not login:
        return None
    state = login.get('state') or 'failed'
    qr = None
    if state == 'waiting' and login.get('content'):
        from lib.qr import qr_png_data_uri
        qr = qr_png_data_uri(str(login['content']))
    return {'state': state, 'qrDataUri': qr, 'detail': str(login.get('detail') or '')}


def account_view(owner: int, channel: str, *, store: ChannelStore | None = None) -> dict[str, Any]:
    """Contract ``BotAccountView``."""
    from lib.channels.delivery import RouterUnavailable, global_router_binding
    _require_channel(channel)
    store = store or ChannelStore(owner)
    account = store.account(channel) or {}
    status = dict(account.get('status') or {})
    credential = account.get('credential') or None
    identity = account.get('identity') or None
    try:
        router_id = global_router_binding(store.owner, create=False)['conversationId']
    except RouterUnavailable:
        router_id = None
    return {
        'channel': channel,
        'enabled': bool(account.get('enabled')),
        'appId': account.get('appId') or None,
        'identity': None if not identity else {'uid': str(identity['uid']), 'name': str(identity.get('name') or '')},
        'commandChat': account.get('commandChat') or None,
        'status': {'state': status.get('state') or 'disabled', 'detail': status.get('detail') or '',
                   'updatedAtMs': int(status.get('updatedAtMs') or 0),
                   'connectedAtMs': status.get('connectedAtMs')},
        'credential': None if not credential else {
            'name': str(credential['name']), 'hint': _vault_hint(str(credential['name'])),
            'managed': bool(credential.get('managed'))},
        'pairingCode': (account.get('pairing') or {}).get('code') or None,
        'login': _login_view(account.get('login')),
        'routerConversationId': router_id,
    }


def _chosen_credential(owner: int, channel: str, name: Any) -> tuple[dict[str, Any], str]:
    from lib.credentials_vault import get_entry, normalize_name
    from lib.error_envelope import BadRequest
    try:
        entry = normalize_name(name)
    except BadRequest as error:
        raise ChannelError('invalid_update', str(error)) from error
    own = vault_entry_name(owner, channel)
    if entry.startswith(('skill.', 'channel.')) and entry != own:
        raise ChannelError('invalid_update', 'That credential vault entry is reserved')
    secret = get_entry(entry)
    if not secret:
        raise ChannelError('credential_missing', f'No credential vault entry named {entry}')
    return {'name': entry, 'managed': entry == own}, secret


def _text_field(update: dict[str, Any], name: str, limit: int) -> str:
    value = update[name]
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(c.isspace() for c in value.strip()):
        raise ChannelError('invalid_update', f'{name} must be a non-empty value without spaces')
    return value.strip()


def _probe(channel: str, app_id: str, secret: str) -> None:
    if channel == 'qq':
        from lib.channels.qq.transport import probe
        probe(app_id, secret)


def update_account(owner: int, channel: str, update: dict[str, Any], *, store: ChannelStore | None = None,
                   prober=None, login_starter=None) -> dict[str, Any]:
    """Apply ``BotAccountUpdate``; returns at once (the supervisor starts the session)."""
    _require_channel(channel)
    store = store or ChannelStore(owner)
    unknown = set(update) - _UPDATE_FIELDS
    if unknown:
        raise ChannelError('invalid_update', f'Unknown fields: {sorted(unknown)}')
    for flag in ('enabled', 'pair', 'login'):
        if flag in update and not isinstance(update[flag], bool):
            raise ChannelError('invalid_update', f'{flag} must be a boolean')
    label = _LABELS[channel]
    if channel == 'wechat' and set(update) & {'appId', 'secret', 'pair'}:
        raise ChannelError('invalid_update', '微信 binds its bot and owner by the ClawBot QR login; use login')
    if channel != 'wechat' and 'login' in update:
        raise ChannelError('invalid_update', f'{label} binds its owner by pairing; use pair')
    if channel == 'wechat' and update.get('credentialName'):
        raise ChannelError('invalid_update', '微信 bot tokens come only from the ClawBot QR login')
    enabled = update.get('enabled')
    account = store.account(channel) or {}
    app_id = account.get('appId')
    if 'appId' in update:
        if account.get('enabled') and enabled is not False:
            raise ChannelError('invalid_update', f'Disable the {label} account before changing its app ID')
        app_id = _text_field(update, 'appId', 128)
    credential: Any = _UNSET
    secret = None
    if 'secret' in update:
        secret = _text_field(update, 'secret', 255)
        credential = {'name': vault_entry_name(store.owner, channel), 'managed': True}
    elif 'credentialName' in update:
        if update['credentialName'] is None:
            credential = None
        else:
            credential, secret = _chosen_credential(store.owner, channel, update['credentialName'])
    current = account.get('credential') or {}
    has_credential = bool(current.get('name')) if credential is _UNSET else credential is not None
    if enabled is True and not update.get('login'):
        if channel != 'wechat' and not app_id:
            raise ChannelError('invalid_update', f'Enter the {label} app ID before enabling the account')
        if not has_credential:
            raise ChannelError('credential_missing', f'Add the {label} secret before enabling the account')
    if secret is not None and app_id:
        (prober or _probe)(channel, app_id, secret)
    login = None
    if update.get('login') is True:
        if login_starter is None:
            from lib.channels.wechat.login import request_qr

            def login_starter(_owner):
                return request_qr()
        login = login_starter(store.owner)
    if 'secret' in update:
        from lib.credentials_vault import set_entry
        set_entry(vault_entry_name(store.owner, channel), secret,
                  note=f'{label} bot channel secret (Tofu owner {store.owner})')
    if credential is not _UNSET and current.get('managed') and (credential is None or credential['name'] != current.get('name')):
        from lib.credentials_vault import delete_entry
        delete_entry(str(current['name']))
    moment = now_ms()

    def status(state: str) -> dict[str, Any]:
        return {'state': state, 'detail': '', 'updatedAtMs': moment, 'connectedAtMs': None}

    def change(value):
        keep_status = session_owns_status(value)
        if 'appId' in update:
            value['appId'] = app_id
        if credential is not _UNSET:
            value['credential'] = credential
        if update.get('pair') is False:
            value['pairing'] = None
        elif channel != 'wechat' and (update.get('pair') is True or (
                not value.get('identity') and not value.get('pairing') and (secret is not None or enabled is True))):
            value['pairing'] = {'code': new_pairing_code(), 'createdAtMs': moment}
        if login is not None:
            value.update(enabled=True, login={'state': 'waiting', 'qrcode': login['qrcode'],
                                              'content': login.get('content') or '', 'detail': '',
                                              'startedAtMs': moment},
                         status=status('authorizing'))
            if value.get('frontierAtMs') is None:
                value['frontierAtMs'] = moment
            return value
        if update.get('login') is False:
            value['login'] = None
        if enabled is True:
            value['enabled'] = True
            if value.get('frontierAtMs') is None:
                value['frontierAtMs'] = moment  # ingress starts now; earlier messages are not replayed
            if not keep_status:
                value['status'] = status('starting')
        elif enabled is False or (credential is None and value.get('enabled')) or (
                update.get('login') is False and not (value.get('credential') or {}).get('name')):
            value['enabled'] = False
            value['status'] = status('disabled')
        elif (secret is not None or 'appId' in update) and value.get('enabled') and not keep_status:
            value['status'] = status('starting')  # a new secret clears needs_login; the session restarts
        return value
    store.update_account(channel, change)
    from lib.channels.bots.supervisor import notify_account_changed
    notify_account_changed(store.owner, channel)
    return account_view(store.owner, channel, store=store)


__all__ = ['account_view', 'new_pairing_code', 'update_account', 'vault_entry_name']
