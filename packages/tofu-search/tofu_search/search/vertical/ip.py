"""IP vertical — IP geolocation / org lookup via ipinfo.io."""

import ipaddress

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, logger

TYPE = 'ip'
DOMAIN = 'network'


def detect(q):
    """Detect a bare IPv4 or IPv6 address as the whole query."""
    try:
        ipaddress.ip_address(q)
    except ValueError:
        return None
    return (TYPE, q, {})


def search(identifier, params):
    """Look up IP address information via ipinfo.io."""
    try:
        d = base._fetch_json(f'https://ipinfo.io/{identifier}/json', label='IP')
        if d is _FETCH_FAILED:
            return None

        parts = [f'## IP: {identifier}']
        if d.get('hostname'):
            parts.append(f'**Hostname**: {d["hostname"]}')
        loc = [p for p in [d.get('city'), d.get('region'), d.get('country')] if p]
        if loc:
            parts.append(f'**Location**: {", ".join(loc)}')
        if d.get('loc'):
            parts.append(f'**Coordinates**: {d["loc"]}')
        if d.get('org'):
            parts.append(f'**Organization**: {d["org"]}')
        if d.get('timezone'):
            parts.append(f'**Timezone**: {d["timezone"]}')

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'ipinfo.io'}
    except Exception as e:
        logger.warning('[Vertical] IP lookup failed for %s: %s', identifier, e)
        return None
