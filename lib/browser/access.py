"""Per-user browser domain access policy.

Browser reads are allowed unless the user denied the domain.  Browser writes
need one long-lived grant tied to the user, browser client/profile, and exact
registrable host.  A redirect never carries authority to another host because
every action resolves the current URL again.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_STORE_PATH = config_path('browser_access.json')
_SENSITIVE_KEYS = (
    'password', 'passwd', 'secret', 'token', 'cookie', 'authorization',
    'credential', 'body', 'content', 'html', 'text', 'data', 'upload',
    'code', 'expression', 'condition',
)


class BrowserAccessDenied(PermissionError):
    pass


class BrowserWriteAuthorizationRequired(PermissionError):
    def __init__(self, domain: str, *, client_id: str = '', profile: str = ''):
        self.domain = domain
        self.client_id = client_id
        self.profile = profile
        super().__init__(
            f'Long-term browser write authorization required for {domain}')


def normalize_domain(value: str) -> str:
    raw = str(value or '').strip().lower()
    if not raw:
        return ''
    try:
        parsed = urlsplit(raw if '://' in raw else 'https://' + raw)
        host = (parsed.hostname or '').lower().rstrip('.')
    except ValueError as exc:
        logger.debug('[Browser] invalid domain value: %s', exc)
        return ''
    if host.startswith('www.'):
        host = host[4:]
    return host


def _user_key(user_id) -> str:
    return str(user_id or '_local')


def _empty_user() -> dict:
    return {'read_denied_domains': [], 'write_grants': []}


def get_access_policy(user_id='') -> dict:
    data = read_json(_STORE_PATH, default={})
    users = data.get('users') if isinstance(data, dict) else {}
    row = (users or {}).get(_user_key(user_id))
    if not isinstance(row, dict):
        row = _empty_user()
    denies = sorted({normalize_domain(v) for v in row.get('read_denied_domains', [])
                     if normalize_domain(v)})
    grants = []
    for grant in row.get('write_grants', []):
        if not isinstance(grant, dict) or not normalize_domain(grant.get('domain')):
            continue
        grants.append({
            'domain': normalize_domain(grant.get('domain')),
            'client_id': str(grant.get('client_id') or ''),
            'profile': str(grant.get('profile') or ''),
            'granted_by': str(grant.get('granted_by') or user_id or ''),
            'granted_at': float(grant.get('granted_at') or 0),
            'last_used_at': float(grant.get('last_used_at') or 0),
        })
    return {'read_denied_domains': denies, 'write_grants': grants}


def _domain_matches(host: str, rule: str) -> bool:
    return bool(host and rule and (host == rule or host.endswith('.' + rule)))


def is_read_allowed(user_id, url_or_domain: str) -> bool:
    host = normalize_domain(url_or_domain)
    if not host:
        return False
    policy = get_access_policy(user_id)
    return not any(_domain_matches(host, rule)
                   for rule in policy['read_denied_domains'])


def _grant_matches(grant: dict, host: str, client_id: str, profile: str) -> bool:
    # Write grants are exact-domain, exact-browser identity.  Parent-domain
    # matching is intentionally *not* used for writes.
    return (grant.get('domain') == host
            and grant.get('client_id', '') == str(client_id or '')
            and grant.get('profile', '') == str(profile or ''))


def has_write_grant(user_id, url_or_domain: str, *, client_id='', profile='',
                    touch: bool = False) -> bool:
    host = normalize_domain(url_or_domain)
    if not host or not is_read_allowed(user_id, host):
        return False
    policy = get_access_policy(user_id)
    found = any(_grant_matches(g, host, client_id, profile)
                for g in policy['write_grants'])
    if found and touch:
        now = time.time()

        def _mut(data):
            data = data if isinstance(data, dict) else {}
            users = data.setdefault('users', {})
            row = users.setdefault(_user_key(user_id), _empty_user())
            for grant in row.setdefault('write_grants', []):
                if isinstance(grant, dict) and _grant_matches(
                        grant, host, client_id, profile):
                    grant['last_used_at'] = now
            return data

        update_json_atomic(_STORE_PATH, _mut, default={})
    return found


def require_access(user_id, url_or_domain: str, *, access='read', client_id='',
                   profile='') -> str:
    host = normalize_domain(url_or_domain)
    if not host or not is_read_allowed(user_id, host):
        raise BrowserAccessDenied(f'Browser access denied for {host or "invalid domain"}')
    if access == 'write' and not has_write_grant(
            user_id, host, client_id=client_id, profile=profile, touch=True):
        # Unattended deployment: auto-create the durable write grant on first
        # use instead of raising for interactive approval.  Read denials above
        # still fail closed; grant_write requires a concrete browser client.
        if not client_id:
            raise BrowserWriteAuthorizationRequired(
                host, client_id=client_id, profile=profile)
        grant_write(user_id, host, client_id=client_id, profile=profile,
                    granted_by=user_id)
    return host


def replace_read_denials(user_id, domains) -> dict:
    normalized = sorted({normalize_domain(v) for v in (domains or [])
                         if normalize_domain(v)})

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        row = data.setdefault('users', {}).setdefault(
            _user_key(user_id), _empty_user())
        row['read_denied_domains'] = normalized
        return data

    update_json_atomic(_STORE_PATH, _mut, default={})
    audit_log('browser_read_policy_updated', user_id=str(user_id or ''),
              domain_count=len(normalized))
    return get_access_policy(user_id)


def grant_write(user_id, domain: str, *, client_id='', profile='',
                granted_by='') -> dict:
    host = normalize_domain(domain)
    if not host:
        raise ValueError('valid domain is required')
    if not client_id:
        raise ValueError('browser client_id is required')
    now = time.time()

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        row = data.setdefault('users', {}).setdefault(
            _user_key(user_id), _empty_user())
        grants = row.setdefault('write_grants', [])
        grants[:] = [g for g in grants if not (
            isinstance(g, dict) and _grant_matches(
                g, host, client_id, profile))]
        grants.append({
            'domain': host, 'client_id': str(client_id),
            'profile': str(profile or ''),
            'granted_by': str(granted_by or user_id or ''),
            'granted_at': now, 'last_used_at': now,
        })
        return data

    update_json_atomic(_STORE_PATH, _mut, default={})
    audit_log('browser_write_granted', user_id=str(user_id or ''), domain=host,
              browser_client=str(client_id), profile=str(profile or ''),
              granted_by=str(granted_by or user_id or ''))
    return get_access_policy(user_id)


def revoke_write(user_id, domain: str, *, client_id='', profile='') -> dict:
    host = normalize_domain(domain)

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        row = data.setdefault('users', {}).setdefault(
            _user_key(user_id), _empty_user())
        grants = row.setdefault('write_grants', [])
        grants[:] = [g for g in grants if not (
            isinstance(g, dict) and g.get('domain') == host
            and (not client_id or g.get('client_id', '') == str(client_id))
            and (not profile or g.get('profile', '') == str(profile)))]
        return data

    update_json_atomic(_STORE_PATH, _mut, default={})
    audit_log('browser_write_revoked', user_id=str(user_id or ''), domain=host,
              browser_client=str(client_id or ''), profile=str(profile or ''))
    return get_access_policy(user_id)


def replace_write_grants(user_id, grants, *, granted_by='') -> dict:
    """Replace durable grants for a user (the PUT full-state form)."""
    if not isinstance(grants, list):
        raise ValueError('write_grants must be an array')
    now = time.time()
    normalized = []
    for grant in grants:
        if not isinstance(grant, dict):
            raise ValueError('each write grant must be an object')
        domain = normalize_domain(grant.get('domain'))
        client_id = str(grant.get('client_id') or grant.get('clientId') or '')
        if not domain or not client_id:
            raise ValueError('each write grant requires domain and client_id')
        normalized.append({
            'domain': domain, 'client_id': client_id,
            'profile': str(grant.get('profile') or ''),
            'granted_by': str(grant.get('granted_by') or granted_by or user_id or ''),
            'granted_at': float(grant.get('granted_at') or now),
            'last_used_at': float(grant.get('last_used_at') or now),
        })

    def _mut(data):
        data = data if isinstance(data, dict) else {}
        row = data.setdefault('users', {}).setdefault(
            _user_key(user_id), _empty_user())
        row['write_grants'] = normalized
        return data

    update_json_atomic(_STORE_PATH, _mut, default={})
    audit_log('browser_write_grants_replaced', user_id=str(user_id or ''),
              grant_count=len(normalized), granted_by=str(granted_by or ''))
    return get_access_policy(user_id)


def summarize_parameters(params: dict | None) -> dict:
    """Bounded audit summary that never records secrets or page bodies."""
    out = {}
    for key, value in (params or {}).items():
        lowered = str(key).lower()
        if any(part in lowered for part in _SENSITIVE_KEYS):
            out[key] = '[redacted]'
        elif isinstance(value, str):
            out[key] = value[:160] + ('…' if len(value) > 160 else '')
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = f'[{len(value)} items]'
        elif isinstance(value, dict):
            out[key] = f'{{{len(value)} fields}}'
        else:
            out[key] = type(value).__name__
    return out


_BROWSER_DOMAIN_WRITE_TOOLS = frozenset({
    'browser_click', 'browser_type', 'browser_press_key',
    'browser_execute_js', 'browser_fill_form', 'browser_menu_click',
    'browser_devtools',
})

_DEVTOOLS_READ_OR_LIFECYCLE_ACTIONS = frozenset({
    'console_read', 'console_clear', 'context_list',
    'debug_start', 'debug_state', 'debug_stop',
    'breakpoint_remove', 'script_source',
})


def browser_tool_requires_write(fn_name: str, fn_args: dict | None) -> bool:
    """Return the action-aware write classification for browser access.

    ``browser_devtools`` deliberately remains in the serial write partition:
    one tab must never receive concurrent CDP state changes.  This finer
    classification controls only domain grants and attended approval, allowing
    console/source diagnostics and debugger cleanup without a write grant.
    Expressions are writes because JavaScript syntax cannot prove purity.
    """
    if fn_name != 'browser_devtools':
        return fn_name in _BROWSER_DOMAIN_WRITE_TOOLS
    action = str((fn_args or {}).get('action') or 'console_read').lower()
    return action not in _DEVTOOLS_READ_OR_LIFECYCLE_ACTIONS


def browser_tool_domain(
    fn_name: str,
    fn_args: dict | None,
    *,
    client_id: str,
    owner_user_id: str,
) -> str:
    """Best-effort current domain for a generic browser tool call."""
    args = dict(fn_args or {})
    if fn_name in ('browser_navigate', 'browser_research_page') \
            and args.get('url'):
        return normalize_domain(args['url'])
    if fn_name == 'browser_get_cookies':
        target = args.get('url') or args.get('domain')
        if target:
            return normalize_domain(target)
    tab_id = args.get('tab_id', args.get('tabId'))
    try:
        from .queue import send_browser_command
        result, error = send_browser_command(
            'list_tabs',
            {},
            timeout=8,
            client_id=client_id,
            owner_user_id=owner_user_id,
        )
        if error or not isinstance(result, list):
            return ''
        selected = None
        if tab_id is not None:
            selected = next((t for t in result
                             if str(t.get('id')) == str(tab_id)), None)
        if selected is None:
            selected = next((t for t in result if t.get('active')), None)
        return normalize_domain((selected or {}).get('url', ''))
    except Exception as exc:
        logger.debug('[Browser] active-tab domain lookup failed: %s', exc)
        return ''


def browser_tool_access(
    fn_name: str,
    fn_args: dict | None,
    *,
    owner_user_id: str,
    client_id: str = '',
    grant_on_success: bool = False,
) -> str:
    """Enforce or grant domain policy for the browser tool surface.

    Returns the resolved domain.  Lifecycle/navigation calls are reads;
    arbitrary page interactions are writes.
    """
    owner_user_id = str(owner_user_id or '').strip()
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise BrowserAccessDenied(
            'Browser tool requires an authenticated owner')

    from .protocol import client_protocol
    from .queue import client_owner_user_id, get_connected_clients
    if not client_id:
        own_clients = get_connected_clients(
            owner_user_id=owner_user_id)
        if own_clients:
            client_id = str(max(
                own_clients, key=lambda row: row.get('last_poll', 0)
            ).get('client_id') or '')
    info = client_protocol(client_id)
    resolved_client = str(info.get('client_id') or client_id or '')
    if (
        resolved_client
        and client_owner_user_id(resolved_client) != owner_user_id
    ):
        raise BrowserAccessDenied(
            'Browser client is not connected for this user')
    if fn_name in ('browser_list_tabs', 'browser_close_tab'):
        # list_tabs filters rows itself; closing is cleanup, not page access.
        return ''
    requires_write = browser_tool_requires_write(fn_name, fn_args)
    if not resolved_client:
        if requires_write:
            raise BrowserWriteAuthorizationRequired(
                'unknown-domain', client_id='')
        return ''
    domain = browser_tool_domain(
        fn_name,
        fn_args,
        client_id=resolved_client,
        owner_user_id=owner_user_id,
    )
    if not domain:
        # Fail closed only for write actions. A read may be the operation that
        # discovers the URL of a newly-bound tab.
        if requires_write:
            raise BrowserWriteAuthorizationRequired(
                'unknown-domain', client_id=resolved_client)
        return ''
    profile = info.get('profile', '')
    source_url = str(
        (fn_args or {}).get('source_url')
        or (fn_args or {}).get('sourceUrl')
        or ''
    ).strip()
    if fn_name == 'browser_devtools' and source_url:
        try:
            source_scheme = urlsplit(source_url).scheme.lower()
        except ValueError:
            source_scheme = ''
        if source_scheme in ('http', 'https'):
            require_access(
                owner_user_id, source_url, access='read',
                client_id=resolved_client, profile=profile)
    if grant_on_success and requires_write:
        require_access(owner_user_id, domain, access='read',
                       client_id=resolved_client, profile=profile)
        grant_write(
            owner_user_id,
            domain,
            client_id=resolved_client,
            profile=profile,
            granted_by=owner_user_id,
        )
    else:
        access = 'write' if requires_write else 'read'
        require_access(
            owner_user_id,
            domain,
            access=access,
            client_id=resolved_client,
            profile=profile,
        )
    return domain


__all__ = [
    'BrowserAccessDenied', 'BrowserWriteAuthorizationRequired',
    'normalize_domain', 'get_access_policy', 'is_read_allowed',
    'has_write_grant', 'require_access', 'replace_read_denials',
    'grant_write', 'revoke_write', 'replace_write_grants',
    'summarize_parameters',
    'browser_tool_domain', 'browser_tool_access',
    'browser_tool_requires_write',
]
