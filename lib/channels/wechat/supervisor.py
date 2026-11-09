"""微信 Router Watchdog supervisor port: the shared bot supervisor for ``wechat``."""
from __future__ import annotations

from lib.channels.bots import supervisor as _bots


def bridge_state(owner: int) -> str:
    return _bots.bridge_state(owner, 'wechat')


__all__ = ['bridge_state']
