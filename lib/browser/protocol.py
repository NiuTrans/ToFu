"""Versioned Browser Bridge protocol and capability negotiation.

The command queue predates capability negotiation.  Protocol v2 keeps that
wire format intact and adds an advertised capability set to each poll.  A
legacy extension therefore continues to receive the old read commands, while
new callers can fail *before* enqueueing a command that the client cannot run.
"""

from __future__ import annotations

from enum import Enum


PROTOCOL_VERSION = 2
MIN_PROTOCOL_VERSION = 1


class BrowserCapability(str, Enum):
    TABS = 'tabs'
    NAVIGATE = 'navigate'
    READ = 'read'
    SNAPSHOT = 'snapshot'
    CLICK = 'click'
    FILL = 'fill'
    PRESS = 'press'
    SELECT = 'select'
    SCROLL = 'scroll'
    WAIT = 'wait'
    EXECUTE = 'execute'
    IFRAMES = 'iframes'
    NETWORK_CAPTURE = 'network_capture'
    UPLOAD = 'upload'
    DOWNLOADS = 'downloads'
    SCREENSHOT = 'screenshot'


# What an extension that does not advertise v2 is known to support.  This is
# deliberately conservative: only shipped v1 commands belong here.
LEGACY_CAPABILITIES = frozenset({
    BrowserCapability.TABS.value,
    BrowserCapability.NAVIGATE.value,
    BrowserCapability.READ.value,
    BrowserCapability.CLICK.value,
    BrowserCapability.FILL.value,
    BrowserCapability.PRESS.value,
    BrowserCapability.SCROLL.value,
    BrowserCapability.WAIT.value,
    BrowserCapability.EXECUTE.value,
    BrowserCapability.DOWNLOADS.value,
    BrowserCapability.SCREENSHOT.value,
})

ALL_CAPABILITIES = frozenset(item.value for item in BrowserCapability)


class BrowserUpgradeRequired(RuntimeError):
    """Raised before dispatch when a connected extension lacks capabilities."""

    def __init__(self, missing, *, client_id: str = '', protocol_version: int = 1):
        self.missing = tuple(sorted({str(getattr(v, 'value', v)) for v in missing}))
        self.client_id = str(client_id or '')
        self.protocol_version = int(protocol_version or 1)
        super().__init__(
            'Browser extension upgrade required; missing capabilities: '
            + ', '.join(self.missing))


def normalize_capabilities(values, *, protocol_version: int = 1) -> frozenset[str]:
    """Return a bounded, normalized capability set for a client poll."""
    if int(protocol_version or 1) < 2 or not isinstance(values, (list, tuple, set)):
        return LEGACY_CAPABILITIES
    return frozenset(str(v).strip() for v in values if str(v).strip() in ALL_CAPABILITIES)


def client_protocol(client_id: str | None) -> dict:
    """Return the negotiated protocol snapshot for ``client_id``.

    ``client_id=None`` selects the freshest connected client, mirroring the
    queue's ordinary unrouted-command behaviour.
    """
    import time

    from .queue import _state

    with _state._clients_lock:
        if client_id is not None:
            row = dict(_state._clients.get(client_id) or {})
            cid = str(client_id or '')
        else:
            live = [(cid, info) for cid, info in _state._clients.items()
                    if time.time() - info.get('last_poll', 0) < 15]
            cid, info = max(live, key=lambda pair: pair[1].get('last_poll', 0)) \
                if live else ('', {})
            row = dict(info)
    version = int(row.get('protocol_version') or 1)
    caps = normalize_capabilities(row.get('capabilities'), protocol_version=version)
    return {
        'client_id': cid,
        'protocol_version': version,
        'capabilities': sorted(caps),
        'profile': str(row.get('profile') or ''),
    }


def require_capabilities(client_id: str | None, required) -> dict:
    """Return protocol info or raise :class:`BrowserUpgradeRequired`."""
    info = client_protocol(client_id)
    wanted = {str(getattr(v, 'value', v)) for v in (required or ())}
    missing = wanted - set(info['capabilities'])
    if missing:
        raise BrowserUpgradeRequired(
            missing, client_id=info['client_id'],
            protocol_version=info['protocol_version'])
    return info


__all__ = [
    'PROTOCOL_VERSION', 'MIN_PROTOCOL_VERSION', 'BrowserCapability',
    'BrowserUpgradeRequired', 'LEGACY_CAPABILITIES', 'ALL_CAPABILITIES',
    'normalize_capabilities', 'client_protocol', 'require_capabilities',
]
