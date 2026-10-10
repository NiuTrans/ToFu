"""Owner-scoped browser capability evidence for registry, context and search.

Entry points: browser_extension_availability and browser_search_availability.
The live bridge client registry owns connection truth; these read-only
projections grant no tool execution authority and retain no client identities.
"""
from __future__ import annotations

from collections.abc import Iterable


def browser_extension_availability(owner_user_id: int) -> dict:
    """Observe only the authenticated owner's usable extension connections."""
    if owner_user_id <= 0:
        return {
            'capability': 'browser_extension', 'namespace': 'browser',
            'available': False, 'reason_code': 'browser_owner_required',
            'message': 'Browser extension tools require an authenticated owner.',
            'recovery_hint': 'Use an authenticated browser session.',
        }
    from lib.browser.queue import get_connected_clients

    try:
        connected = bool(get_connected_clients(owner_user_id=str(owner_user_id)))
    except Exception as exc:
        from lib.log import get_logger
        get_logger(__name__).warning(
            'Browser availability probe failed (%s)', type(exc).__name__)
        return {
            'capability': 'browser_extension', 'namespace': 'browser',
            'available': False, 'reason_code': 'browser_extension_status_unknown',
            'message': 'Browser extension connection status could not be read; '
                       'this does not prove that the extension is disconnected.',
            'recovery_hint': 'Check the server browser-bridge diagnostics and '
                             'retry after recovery. Tool authority is unchanged.',
        }
    return {
        'capability': 'browser_extension', 'namespace': 'browser',
        'available': connected,
        'reason_code': ('browser_extension_connected' if connected
                        else 'browser_extension_disconnected'),
        'message': ('Browser extension connected for this user.' if connected else
                    'Browser extension tools exist, but no usable extension is '
                    'connected for this user.'),
        'recovery_hint': ('' if connected else
                          'Ask the user to check the Tofu server URL and '
                          'authentication in the browser extension, then reconnect. '
                          'The backend cannot read the URL configured in a '
                          'disconnected extension. Availability is checked again '
                          'on the next model round; do not repeat the same search '
                          'until the connection changes.'),
    }


def browser_search_availability(
    owner_user_id: int, query: str, namespace: str, catalog_names: Iterable[str],
) -> dict | None:
    """Explain a known extension capability without inventing callable tools."""
    normalized = query.strip().lower().removeprefix('+')
    if not (namespace.strip().lower() == 'browser'
            or 'browser' in normalized or '浏览器' in normalized):
        return None
    from lib.tools.browser import BROWSER_TOOLS, PAGE_PREVIEW_TOOL_NAMES
    from lib.browser.advanced import ADVANCED_BROWSER_TOOLS

    known_names = {
        tool['function']['name'] for tool in
        (*BROWSER_TOOLS, *ADVANCED_BROWSER_TOOLS)
    }
    if normalized in PAGE_PREVIEW_TOOL_NAMES:
        return None
    names = set(catalog_names)
    if (normalized in names or
            (normalized not in known_names and known_names.intersection(names))):
        return None
    evidence = browser_extension_availability(owner_user_id)
    if evidence['available']:
        evidence.update({
            'available': False,
            'reason_code': 'browser_tools_outside_task_catalog',
            'message': 'The browser extension is connected, but its tools are '
                       'not authorized in this task catalog.',
            'recovery_hint': 'Check this task\'s tool settings and execution '
                             'policy. A connection does not grant tool authority.',
        })
    if normalized in known_names:
        evidence['tool_name'] = normalized
    return evidence
