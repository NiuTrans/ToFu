"""Current Browser Bridge protocol and strict capability negotiation.

The bridge has one accepted wire version. A poll that omits or disagrees with
the version/capability contract is rejected before registration or command
settlement; it cannot silently downgrade into a smaller authority surface.
"""

from __future__ import annotations

from enum import Enum


PROTOCOL_VERSION = 2


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
    NETWORK_BODY = 'network_body'
    DEEP_COLLECT = 'deep_collect'
    DEVTOOLS_CONSOLE = 'devtools_console'
    JS_DEBUGGER = 'js_debugger'
    UPLOAD = 'upload'
    # Authenticated response bytes streamed to server staging.  This is
    # intentionally distinct from DOWNLOADS, which writes on the browser
    # device through chrome.downloads.
    FILE_EXPORT = 'file_export'
    DOWNLOADS = 'downloads'
    SCREENSHOT = 'screenshot'


ALL_CAPABILITIES = frozenset(item.value for item in BrowserCapability)


class BrowserProtocolRejected(ValueError):
    """The polling extension does not implement the current wire contract."""


class BrowserUpgradeRequired(RuntimeError):
    """Raised before dispatch when a connected extension lacks capabilities."""

    def __init__(self, missing, *, client_id: str = '', protocol_version: int = 0):
        self.missing = tuple(sorted({str(getattr(v, 'value', v)) for v in missing}))
        self.client_id = str(client_id or '')
        self.protocol_version = int(protocol_version or 0)
        super().__init__(
            'Browser extension upgrade required; missing capabilities: '
            + ', '.join(self.missing))


def normalize_protocol_version(value) -> int:
    """Return the one supported wire version or an actionable rejection."""
    if isinstance(value, bool):
        raise BrowserProtocolRejected(
            f'Browser protocol {PROTOCOL_VERSION} is required')
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise BrowserProtocolRejected(
            f'Browser protocol {PROTOCOL_VERSION} is required') from None
    if normalized != PROTOCOL_VERSION:
        raise BrowserProtocolRejected(
            f'Browser protocol {PROTOCOL_VERSION} is required')
    return normalized


def normalize_capabilities(values, *, protocol_version: int) -> frozenset[str]:
    """Validate and normalize one current-protocol capability declaration."""
    normalize_protocol_version(protocol_version)
    if not isinstance(values, (list, tuple, set)):
        raise BrowserProtocolRejected('Browser capabilities must be an array')
    normalized = frozenset(
        str(value).strip() for value in values if str(value).strip()
    )
    unknown = normalized - ALL_CAPABILITIES
    if unknown:
        raise BrowserProtocolRejected(
            'Unknown browser capabilities: ' + ', '.join(sorted(unknown)))
    return normalized


def client_protocol(client_id: str) -> dict:
    """Return the protocol snapshot for one explicitly addressed client."""
    from .queue import _state

    client_id = str(client_id or '').strip()
    if not client_id:
        return {
            'client_id': '',
            'protocol_version': 0,
            'capabilities': [],
            'profile': '',
        }
    with _state._clients_lock:
        row = dict(_state._clients.get(client_id) or {})
    version = int(row.get('protocol_version') or 0)
    caps = (
        normalize_capabilities(
            row.get('capabilities'), protocol_version=version)
        if row else frozenset()
    )
    return {
        'client_id': client_id if row else '',
        'protocol_version': version,
        'capabilities': sorted(caps),
        'profile': str(row.get('profile') or ''),
    }


def require_capabilities(client_id: str, required) -> dict:
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
    'PROTOCOL_VERSION', 'BrowserCapability', 'BrowserProtocolRejected',
    'BrowserUpgradeRequired', 'ALL_CAPABILITIES',
    'normalize_protocol_version', 'normalize_capabilities', 'client_protocol',
    'require_capabilities',
]
