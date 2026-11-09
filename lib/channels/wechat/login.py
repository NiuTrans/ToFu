"""微信 ClawBot QR login (docs/CHANNELS.md C12).

Responsibility: ``request_qr`` asks iLink for a login QR code (``POST
/ilink/bot/get_bot_qrcode?bot_type=3``); the Settings command stores it on the
account and shows it. ``complete`` runs inside the account's session and
follows the code's status (``GET /ilink/bot/get_qrcode_status``, a long poll)
until the owner scanned and confirmed it in 微信: then the bot token goes to
the owner's reserved vault entry, the scanning user becomes the bound owner,
and that private chat the command chat. A redirect or ``baseurl`` naming a
host outside ``protocol.trusted_base`` is refused, so the token never leaves
微信's servers. An expired code is exposed as refreshable; a verification-code challenge (Settings has no
input for it) or a bot already bound elsewhere fails the login with a reason
the owner can act on (start again).

Entry points: ``request_qr``, ``complete``. Dependencies:
``lib.channels.bots.http``, ``protocol``, ``ChannelStore``, the vault.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from lib.channels.bots import ids
from lib.channels.bots.http import is_timeout, request_json
from lib.channels.bots.platform import SessionEnded
from lib.channels.model import WECHAT, ChannelError
from lib.channels.store import ChannelStore, now_ms
from lib.channels.wechat import protocol
from lib.log import get_logger

logger = get_logger(__name__)

_FAILURES = {
    'expired': 'The login code expired; start the 微信 login again',
    'need_verifycode': '微信 asked for a verification code, which Tofu cannot enter; start the login again',
    'verify_code_blocked': 'Too many verification attempts; start the 微信 login again later',
    'binded_redirect': 'This ClawBot is already connected to another service; disconnect it in 微信 first',
}


def request_qr(*, api_base: str = WECHAT['apiBase'], http=request_json) -> dict[str, str]:
    import requests
    try:
        status, body = http('POST', f'{api_base.rstrip("/")}/ilink/bot/get_bot_qrcode?bot_type={WECHAT["botType"]}',
                            headers=protocol.bot_headers(''), body={'local_token_list': []})
    except requests.exceptions.RequestException as error:
        detail = 'The connection to 微信 timed out' if is_timeout(error) else 'Could not connect to 微信'
        raise ChannelError('wechat_unavailable', f'{detail}; retry fetching the QR code') from None
    if status != 200 or not body.get('qrcode'):
        raise ChannelError('wechat_unavailable', f'微信 did not issue a login code (HTTP {status})')
    return {'qrcode': str(body['qrcode']), 'content': str(body.get('qrcode_img_content') or '')}


def _matches(account: dict[str, Any], qrcode: str) -> bool:
    login = account.get('login') or {}
    return bool(account.get('enabled') and login.get('qrcode') == qrcode
                and login.get('state') in ('waiting', 'scanned'))


def _fail(store: ChannelStore, qrcode: str, detail: str, *, state: str = 'failed') -> SessionEnded:
    def change(account):
        if not _matches(account, qrcode):
            return account
        return {**account, 'login': {**account['login'], 'state': state, 'detail': detail, 'content': ''}}
    store.update_account('wechat', change)
    return SessionEnded('needs_login', detail)


def complete(owner: int, *, store: ChannelStore | None = None, api_base: str = WECHAT['apiBase'],
             http=request_json, stop=None, expected_qrcode: str | None = None) -> bool:
    """Follow the pending login until confirmed (True) or stopped (False); raises on failure."""
    import requests
    store = store or ChannelStore(owner)
    login = (store.account('wechat') or {}).get('login') or {}
    if login.get('state') not in ('waiting', 'scanned') or not login.get('qrcode'):
        return False
    qrcode = str(login['qrcode'])
    if expected_qrcode is not None and qrcode != expected_qrcode:
        return False
    configured = api_base.rstrip('/')
    host = configured
    while not (stop is not None and stop.is_set()):
        if not _matches(store.account('wechat') or {}, qrcode):
            return False
        try:
            status, body = http('GET', f'{host}/ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}',
                                headers=protocol.app_headers(), timeout=WECHAT['longPollMs'] / 1000)
        except requests.exceptions.RequestException as error:
            if is_timeout(error):
                continue
            # The request URL carries the QR key, which claims the bot token once confirmed.
            raise ChannelError('wechat_unavailable',
                               f'The 微信 login status request failed ({type(error).__name__})') from None
        if (stop is not None and stop.is_set()) or not _matches(store.account('wechat') or {}, qrcode):
            return False
        if status != 200:
            raise _fail(store, qrcode, f'微信 login status answered HTTP {status}; refresh the QR code')
        state = str(body.get('status') or '')
        if state == 'wait':
            continue
        if state == 'scaned':
            store.update_account('wechat', lambda account: account if not _matches(account, qrcode) else {
                **account, 'login': {**account['login'], 'state': 'scanned', 'content': ''}})
            continue
        if state == 'scaned_but_redirect':
            redirect = protocol.trusted_base(str(body.get('redirect_host') or ''), configured)
            if redirect is None:
                raise _fail(store, qrcode, '微信 redirected the login to an unexpected host; it was refused')
            host = redirect
            continue
        if state == 'confirmed':
            return _confirm(store, body, configured, qrcode)
        raise _fail(store, qrcode, _FAILURES.get(state, f'微信 login ended ({state or "no status"}); start it again'),
                    state='expired' if state == 'expired' else 'failed')
    return False


def _confirm(store: ChannelStore, body: dict[str, Any], configured: str, qrcode: str) -> bool:
    base = protocol.trusted_base(str(body.get('baseurl') or configured), configured)
    token, user = str(body.get('bot_token') or ''), str(body.get('ilink_user_id') or '')
    if base is None:
        raise _fail(store, qrcode, '微信 named an unexpected API host for this bot; the login was refused')
    if not token or not user:
        raise _fail(store, qrcode, '微信 confirmed the login without a bot token or user; start it again')
    from lib.channels.bots.account import vault_entry_name
    from lib.credentials_vault import set_entry
    if not _matches(store.account('wechat') or {}, qrcode):
        return False
    entry = vault_entry_name(store.owner, 'wechat')
    set_entry(entry, token, note=f'微信 ClawBot token (Tofu owner {store.owner})')
    chat = str(ids.thread_key('wechat', 'uid', user))
    moment = now_ms()

    accepted = False

    def change(account):
        nonlocal accepted
        accepted = _matches(account, qrcode)
        if not accepted:
            return account
        account.update(credential={'name': entry, 'managed': True}, appId=str(body.get('ilink_bot_id') or '') or None,
                       apiBaseUrl=None if base == WECHAT['apiBase'] else base,
                       identity={'uid': user, 'name': ''}, commandChat=chat, login=None, cursor='',
                       status={'state': 'starting', 'detail': '', 'updatedAtMs': moment, 'connectedAtMs': None})
        return account
    store.update_account('wechat', change)
    if not accepted:
        return False
    store.update_thread(ids.thread_key('wechat', 'uid', user),
                        lambda record: record if record.get('title') else {**record, 'title': '微信'})
    logger.info('[Channels] wechat login confirmed owner=%s', store.owner)
    return True


__all__ = ['complete', 'request_qr']
