"""Setup guidance for curated MCP connectors an owner cannot use yet.

Entry points:
  * ``connector_search_availability`` — Tool Search evidence when a search
    names a curated connector (namespace equal to its catalog id, or one of
    its ``aliases``) whose tools are absent from the task's executable
    catalog.
  * ``connector_for_document_url`` / ``document_link_guidance`` — the fetch
    reader's answer for a document link only a connector can read
    (``document_url_prefixes``): setup steps while the connector is not
    installed, a pointer to its tools once it is.

Evidence is projected from catalog data (name, ``obtain_url``,
``obtain_steps``) and the owner's MCP configuration. It is read-only: it
never starts a bridge, never grants tool authority and never echoes stored
credentials. Depends on ``lib.mcp.registry`` and ``lib.mcp.config``.
"""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from lib.log import get_logger
from lib.mcp.config import load_mcp_config
from lib.mcp.registry import CatalogEntry, get_catalog, obtain_credential_spec

logger = get_logger(__name__)

_NOT_INSTALLED = 'not_installed'
_DISABLED = 'disabled'
_INSTALLED = 'installed'


def _owner_id(value) -> int:
    try:
        owner = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return owner if owner > 0 else 0


def _owner_states(owner_user_id: int) -> dict[str, str] | None:
    """Map configured server ids to their install state; None when unreadable."""
    try:
        config = load_mcp_config(user_id=owner_user_id)
    except Exception as exc:
        logger.warning('[MCP:Guidance] owner MCP configuration unreadable (%s)',
                       type(exc).__name__)
        return None
    return {
        server_id: (_DISABLED if isinstance(row, dict)
                    and row.get('enabled') is False else _INSTALLED)
        for server_id, row in config.items()
    }


def _token_route(entry: CatalogEntry) -> str:
    spec = obtain_credential_spec(entry)
    if spec is None:
        return ''
    steps = '; '.join(spec.get('obtain_steps') or ())
    return (f"get a personal token at {spec['obtain_url']}"
            + (f' ({steps})' if steps else ''))


def _setup_texts(entry: CatalogEntry, state: str) -> tuple[str, str]:
    """Return ``(message, recovery_hint)`` for a connector the owner cannot use."""
    name, server_id = entry['name'], entry['id']
    route = _token_route(entry)
    if state == _NOT_INSTALLED:
        return (
            f"The {name} connector (MCP catalog id '{server_id}') is not "
            'installed for this user, so its tools are unavailable.',
            f"Ask the user to {route + ', then ' if route else ''}install "
            f"'{name}' in Settings → MCP{' with that token' if route else ''}. "
            'Its tools appear on the next model round after installation.')
    if state == _DISABLED:
        return (
            f'The {name} connector is installed but disabled for this user.',
            'Ask the user to enable it in Settings → MCP; its stored '
            'credentials are kept.')
    return (
        f'The {name} connector is installed, but its tools are not available '
        'in this task.',
        'Ask the user to check it in Settings → MCP, which shows a failed '
        f"connection or an expired token{' (' + route + ', then reinstall)' if route else ''}. "
        "This task's tool settings may also exclude it.")


def _names_connector(entry: CatalogEntry, query: str, namespace: str) -> bool:
    if namespace and namespace == entry['id'].lower():
        return True
    return any(alias.casefold() in query for alias in entry.get('aliases') or ())


def connector_search_availability(
    owner_user_id: int, query: str, namespace: str,
    usable_namespaces: Iterable[str],
) -> list[dict]:
    """Explain curated connectors a search names but this task cannot call.

    Matching is deliberately narrow: an exact catalog id as the namespace
    filter, or a distinctive product alias in the query. Generic capability
    words never match, and a connector whose namespace is in the task's
    executable catalog produces no evidence.
    """
    owner = _owner_id(owner_user_id)
    if not owner:
        return []
    normalized_namespace = str(namespace or '').strip().lower()
    normalized_query = str(query or '').strip().casefold()
    usable = {str(value).strip().lower() for value in usable_namespaces}
    named = [
        entry for entry in get_catalog()
        if entry['id'].lower() not in usable
        and _names_connector(entry, normalized_query, normalized_namespace)
    ]
    if not named:
        return []
    states = _owner_states(owner)
    if states is None:
        return []
    evidence = []
    for entry in named:
        state = states.get(entry['id'], _NOT_INSTALLED)
        message, recovery = _setup_texts(entry, state)
        evidence.append({
            'capability': 'mcp_connector', 'namespace': entry['id'],
            'server_id': entry['id'], 'available': False,
            'reason_code': ('mcp_connector_unavailable' if state == _INSTALLED
                            else f'mcp_connector_{state}'),
            'message': message,
            'recovery_hint': (f'{recovery} Do not repeat this search until '
                              'the user confirms the change.'),
        })
    return evidence


def connector_for_document_url(url: str) -> CatalogEntry | None:
    """Return the catalog connector that owns this document link, if any."""
    try:
        parts = urlsplit(str(url or '').strip())
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None
    normalized = f'https://{parts.hostname.lower()}{parts.path}'
    for entry in get_catalog():
        if any(normalized.startswith(prefix)
               for prefix in entry.get('document_url_prefixes') or ()):
            return entry
    return None


def document_link_guidance(owner_user_id, url: str) -> str | None:
    """Answer a connector-only document link instead of its login page.

    Returns None (the anonymous fetch pipeline proceeds unchanged) when the
    URL belongs to no connector or the owner or their configuration is
    unknown.
    """
    entry = connector_for_document_url(url)
    owner = _owner_id(owner_user_id)
    if entry is None or not owner:
        return None
    states = _owner_states(owner)
    if states is None:
        return None
    name, server_id = entry['name'], entry['id']
    head = (f'[{name}] {url} is a {name} document. An anonymous fetch returns '
            f"only a login page; it is readable only through the user's {name} "
            'account.')
    state = states.get(server_id, _NOT_INSTALLED)
    if state == _INSTALLED:
        return (f'{head} Read it with the {name} connector tools (search_tools '
                f"namespace '{server_id}') instead of fetch_url. If those tools "
                'are missing from this task, ask the user to check the '
                'connector in Settings → MCP.')
    message, recovery = _setup_texts(entry, state)
    return (f'{head} {message} {recovery} Do not fetch this link again until '
            'the user confirms the change.')


__all__ = [
    'connector_for_document_url',
    'connector_search_availability',
    'document_link_guidance',
]
