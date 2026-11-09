"""微信 ClawBot iLink HTTP protocol: headers, trusted hosts, message text (C12).

Responsibility: the wire facts shared by the 微信 login and transport, from
Tencent's protocol reference for its MIT-licensed ClawBot client
(https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md):
the application headers on every request, the bot authorization headers
(``AuthorizationType: ilink_bot_token``, ``Authorization: Bearer``, a random
``X-WECHAT-UIN``), the ``base_info`` object, the hosts a returned ``baseurl``
or ``redirect_host`` may name (the configured origin, or https under
``x-wechat.trustedHostSuffix``, so a bot token never leaves 微信's servers),
and the readable text of a message's items.
"""
from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from lib.channels.model import WECHAT


def client_version() -> str:
    """``iLink-App-ClientVersion``: the channel version as 0x00MMNNPP in decimal."""
    major, minor, patch = (int(part) for part in WECHAT['channelVersion'].split('.'))
    return str((major << 16) | (minor << 8) | patch)


def app_headers() -> dict[str, str]:
    return {'iLink-App-Id': WECHAT['appId'], 'iLink-App-ClientVersion': client_version()}


def bot_headers(token: str) -> dict[str, str]:
    uin = base64.b64encode(str(secrets.randbits(32)).encode('ascii')).decode('ascii')
    return {**app_headers(), 'AuthorizationType': 'ilink_bot_token', 'X-WECHAT-UIN': uin,
            **({'Authorization': f'Bearer {token}'} if token else {})}


def base_info() -> dict[str, str]:
    return {'channel_version': WECHAT['channelVersion'], 'bot_agent': 'Tofu/1.0'}


def trusted_base(url: str, configured: str) -> str | None:
    """``url`` without a trailing slash when it may receive the bot token, else None."""
    if not url:
        return None
    if '://' not in url:
        url = 'https://' + url
    parts, home = urlsplit(url), urlsplit(configured)
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    same_origin = (parts.scheme, parts.netloc) == (home.scheme, home.netloc)
    host = (parts.hostname or '').lower()
    suffix = WECHAT['trustedHostSuffix']
    if same_origin or (parts.scheme == 'https' and (host.endswith(suffix) or host == suffix.lstrip('.'))):
        return f'{parts.scheme}://{parts.netloc}'
    return None


def message_text(items: Any) -> str:
    pieces = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        kind = item.get('type')
        if kind == 1:
            pieces.append(str((item.get('text_item') or {}).get('text') or '').strip())
        elif kind == 2:
            pieces.append('[image]')
        elif kind == 3:
            pieces.append(str((item.get('voice_item') or {}).get('text') or '').strip() or '[voice]')
        elif kind == 4:
            pieces.append(f'[file: {(item.get("file_item") or {}).get("file_name") or "attachment"}]')
        elif kind == 5:
            pieces.append('[video]')
    return ' '.join(piece for piece in pieces if piece)


__all__ = ['app_headers', 'base_info', 'bot_headers', 'client_version', 'message_text', 'trusted_base']
