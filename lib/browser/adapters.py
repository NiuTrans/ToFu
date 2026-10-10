"""Native site-adapter contract and built-in read adapters."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from lib.log import audit_log, get_logger

from .access import (
    BrowserAccessDenied,
    BrowserWriteAuthorizationRequired,
    normalize_domain,
    require_access,
    summarize_parameters,
)
from .adapter_errors import AdapterExecutionError, AdapterValidationError
from .page import BrowserPage
from .protocol import (
    ALL_CAPABILITIES,
    BrowserUpgradeRequired,
    client_protocol,
)
from .sessions import acquire_browser_lease

logger = get_logger(__name__)



@dataclass(frozen=True)
class AdapterCommand:
    name: str
    description: str
    access: str = 'read'
    input_schema: dict = field(default_factory=lambda: {'type': 'object'})
    output_schema: dict = field(default_factory=lambda: {'type': 'object'})
    required_capabilities: tuple[str, ...] = ()

    min_interval_s: int = 0
    cooldown_s: int = 0
    session: str = 'ephemeral'
    window_mode: str = 'background'
    timeout: int = 30
    handler: Callable | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.access not in ('read', 'write'):
            raise AdapterValidationError('command access must be read or write')
        if self.session not in ('ephemeral', 'persistent'):
            raise AdapterValidationError('command session must be ephemeral or persistent')
        if not self.name or not isinstance(self.input_schema, dict) \
                or not isinstance(self.output_schema, dict):
            raise AdapterValidationError('command name and schemas are required')
        if self.input_schema.get('type', 'object') != 'object':
            raise AdapterValidationError('command input schema must be an object')
        if self.output_schema.get('type') not in (
                'object', 'array', 'string', 'number', 'integer', 'boolean'):
            raise AdapterValidationError('command output schema needs a supported type')
        unknown_caps = set(self.required_capabilities) - set(ALL_CAPABILITIES)
        if unknown_caps:
            raise AdapterValidationError(
                'unknown browser capabilities: ' + ', '.join(sorted(unknown_caps)))
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= cap
               for v, cap in ((self.min_interval_s, 60), (self.cooldown_s, 600))):
            raise AdapterValidationError('invalid native pacing bounds')
        if self.window_mode not in ('background', 'active', 'current'):
            raise AdapterValidationError('unsupported adapter window mode')
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, int):
            raise AdapterValidationError('command timeout must be an integer')
        if not 1 <= self.timeout <= 300:
            raise AdapterValidationError('command timeout must be between 1 and 300 seconds')

    def public_dict(self) -> dict:
        return {
            'name': self.name, 'description': self.description,
            'access': self.access, 'input_schema': self.input_schema,
            'output_schema': self.output_schema,
            'required_capabilities': list(self.required_capabilities),
            'session': self.session, 'window_mode': self.window_mode,
            'timeout': self.timeout,

            'min_interval_s': self.min_interval_s, 'cooldown_s': self.cooldown_s,
        }


@dataclass(frozen=True)
class SiteAdapter:
    id: str
    name: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    commands: tuple[AdapterCommand, ...] = ()
    login_url: str = ''
    risk_notice: str = ''
    builtin: bool = False
    version: int = 1
    #: One short model-facing routing clause for generated tool descriptions
    #: ('note keywords'). Empty means the site stays out of the prompt and is
    #: still discoverable through the no-site discovery call.
    routing_hint: str = ''

    def __post_init__(self):
        if not self.id or not self.domains or not self.commands:
            raise AdapterValidationError('adapter id, domains, and commands are required')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', self.id):
            raise AdapterValidationError('adapter id must be a lowercase slug')
        if any(not normalize_domain(domain) for domain in self.domains):
            raise AdapterValidationError('adapter domains must be valid hostnames')
        names = [cmd.name for cmd in self.commands]
        if len(names) != len(set(names)):
            raise AdapterValidationError(f'duplicate command in adapter {self.id}')

    def command(self, name: str) -> AdapterCommand:
        for command in self.commands:
            if command.name == name:
                return command
        raise AdapterValidationError(f'unknown command {self.id}.{name}')

    def public_dict(self) -> dict:
        return {
            'id': self.id, 'name': self.name, 'domains': list(self.domains),
            'aliases': list(self.aliases),
            'commands': [cmd.public_dict() for cmd in self.commands],
            'login_url': self.login_url, 'risk_notice': self.risk_notice,
            'builtin': self.builtin, 'version': self.version,
            'routing_hint': self.routing_hint,
        }


_registry: dict[str, SiteAdapter] = {}

#: Ids currently projected from site packs, so sync only ever replaces or
#: removes its own projections and never a hand-registered adapter.
_PACK_ADAPTER_IDS: set[str] = set()


def sync_pack_adapters() -> None:
    """Refresh registry entries projected from site packs (hot reload).

    Pack layering (core < bundle < user) is resolved inside the loader. A
    pack whose id collides with a built-in or hand-registered adapter is
    skipped with a warning: projections never shadow non-pack registrations.
    """
    try:
        from lib.site_packs import load_packs
        from lib.site_packs._projection import pack_to_adapter
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('[Browser] site-pack sync unavailable: %s', exc)
        return
    packs = load_packs()
    seen: set[str] = set()
    for pack in packs.values():
        seen.add(pack.id)
        existing = _registry.get(pack.id)
        if existing is not None and pack.id not in _PACK_ADAPTER_IDS:
            logger.warning(
                '[Browser] site pack %s ignored: id already registered by a '
                'non-pack adapter', pack.id)
            continue
        try:
            register_adapter(pack_to_adapter(pack), replace=True)
        except AdapterValidationError as exc:
            logger.warning('[Browser] invalid site pack projection %s: %s',
                           pack.id, exc)
            continue
        _PACK_ADAPTER_IDS.add(pack.id)
    for stale in sorted(_PACK_ADAPTER_IDS - seen):
        unregister_adapter(stale)
        _PACK_ADAPTER_IDS.discard(stale)


def register_adapter(adapter: SiteAdapter, *, replace=False) -> None:
    if not isinstance(adapter, SiteAdapter):
        raise AdapterValidationError('adapter must be a SiteAdapter')
    if adapter.id in _registry and not replace:
        raise AdapterValidationError(f'adapter already registered: {adapter.id}')
    _registry[adapter.id] = adapter


def unregister_adapter(adapter_id: str) -> bool:
    return _registry.pop(adapter_id, None) is not None


def get_adapter(adapter_id_or_alias: str) -> SiteAdapter | None:
    sync_pack_adapters()
    needle = str(adapter_id_or_alias or '').strip().lower()
    if needle in _registry:
        return _registry[needle]
    for adapter in _registry.values():
        if needle in {a.lower() for a in adapter.aliases}:
            return adapter
    return None


def list_adapters() -> list[SiteAdapter]:
    sync_pack_adapters()
    return sorted(_registry.values(), key=lambda a: a.id)


def _validate_value(schema: dict, value, *, path='value') -> None:
    """Execute the declared JSON schema at input AND output boundaries."""
    from jsonschema import Draft202012Validator
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        raise AdapterValidationError(f'{path}: {error.message}')


def _validate_params(schema: dict, params: dict) -> None:
    if not isinstance(params, dict):
        raise AdapterValidationError('adapter params must be an object')
    _validate_value(schema, params, path='params')


def adapter_health(adapter: SiteAdapter, *, client_id: str | None = None,
                   command_name: str = '') -> dict:
    try:
        info = client_protocol(client_id)
        if not info.get('client_id'):
            return {'status': 'offline', 'healthy': False,
                    'missing_capabilities': [], 'client_id': ''}
        available = set(info.get('capabilities') or [])
        commands = (adapter.command(command_name),) if command_name \
            else adapter.commands
        required = {cap for cmd in commands for cap in cmd.required_capabilities}
        missing = sorted(required - available)
        return {
            'status': 'upgrade_required' if missing else 'ready',
            'healthy': not missing, 'missing_capabilities': missing,
            'client_id': info.get('client_id'),
            'protocol_version': info.get('protocol_version'),
            'profile': info.get('profile', ''),
        }
    except Exception as exc:
        logger.debug('[Browser] adapter health check failed: %s', exc)
        return {'status': 'error', 'healthy': False,
                'missing_capabilities': [], 'error': str(exc)}


def adapters_payload(*, client_id: str | None = None) -> dict:
    rows = []
    for adapter in list_adapters():
        row = adapter.public_dict()
        row['health'] = adapter_health(adapter, client_id=client_id)
        rows.append(row)
    return {'adapters': rows, 'count': len(rows),
            'available_count': sum(1 for row in rows if row['health']['healthy'])}


def invoke_adapter(adapter_id: str, command_name: str, params: dict | None = None,
                   *, owner_user_id: str, client_id: str | None = None,
                   task_id: str = '') -> dict:
    adapter = get_adapter(adapter_id)
    if adapter is None:
        raise AdapterValidationError(f'unknown adapter: {adapter_id}')
    command = adapter.command(command_name)
    params = dict(params or {})
    _validate_params(command.input_schema, params)

    if command_name == 'search' and 'query' in params:
        query = str(params.get('query') or '')
        if not query.strip() or re.search(
                r'(?i)(?<!\w)(?:site|filetype|ext|inurl|allinurl|intitle|allintitle|'
                r'intext|allintext|before|after|cache|related|source):', query):
            raise AdapterValidationError(
                'Native site search does not accept web operators; use native keywords '
                'or a scoped web engine. Constraints are never silently stripped.')
    target_domain = normalize_domain(params.get('url') or adapter.domains[0])
    if params.get('url') and not any(
            target_domain == normalize_domain(domain)
            or target_domain.endswith('.' + normalize_domain(domain))
            for domain in adapter.domains):
        raise AdapterValidationError(
            f'URL is outside adapter domains: {target_domain or "invalid URL"}')
    started = time.time()
    outcome = 'error'
    used_client = str(client_id or '')
    try:
        lease = acquire_browser_lease(
            owner_user_id=owner_user_id, client_id=client_id, task_id=task_id,
            session=command.session, timeout=command.timeout + 15)
        used_client = lease.client_id
        require_caps = command.required_capabilities
        with BrowserPage(
                lease, default_active=command.window_mode == 'active') as page:
            if require_caps:
                from .protocol import require_capabilities
                require_capabilities(lease.client_id, require_caps)
            if command.access == 'write':
                require_access(
                    lease.owner_user_id, target_domain, access='write',
                    client_id=lease.client_id, profile=lease.profile)
            if command.window_mode == 'current':
                page.bind_active()
            if command.handler is None:
                raise AdapterExecutionError('not_implemented',
                                            'Adapter command has no handler')
            ticket = None
            if command.min_interval_s:
                from .site_pacing import admit, settle, SiteReadThrottled
                try:
                    ticket = admit(lease.owner_user_id, lease.client_id, adapter.id,
                                   interval_s=command.min_interval_s,
                                   cooldown_s=command.cooldown_s)
                except SiteReadThrottled as exc:
                    raise AdapterExecutionError('rate_limited', str(exc), retryable=True) from exc
            try:
                result = command.handler(page, params)
            except AdapterExecutionError as exc:
                if ticket:
                    settle(ticket, blocked=exc.code in ('login_required', 'content_blocked'), empty=True)
                raise
            else:
                if ticket:
                    settle(ticket, empty=result == [])
        try:
            _validate_value(command.output_schema, result, path='result')
        except AdapterValidationError as exc:
            raise AdapterExecutionError(
                'invalid_output', str(exc), retryable=False) from exc
        if time.time() - started > command.timeout:
            raise AdapterExecutionError(
                'timeout', f'Adapter command exceeded {command.timeout}s',
                retryable=True)
        outcome = 'ok'
        return {'ok': True, 'site': adapter.id, 'command': command.name,
                'access': command.access, 'result': result,
                'browser_client': used_client,
                'duration_ms': round((time.time() - started) * 1000)}
    except BrowserUpgradeRequired:
        outcome = 'upgrade_required'
        raise
    except BrowserWriteAuthorizationRequired as exc:
        outcome = 'authorization_required'
        raise AdapterExecutionError(
            'write_authorization_required', str(exc), retryable=False,
            details={'domain': exc.domain, 'client_id': exc.client_id,
                     'profile': exc.profile}) from exc
    except BrowserAccessDenied as exc:
        outcome = 'access_denied'
        raise AdapterExecutionError(
            'access_denied', str(exc), retryable=False) from exc
    except AdapterExecutionError:
        raise
    except Exception as exc:
        raise AdapterExecutionError('execution_failed', str(exc),
                                    retryable=True) from exc
    finally:
        audit_log(
            'browser_adapter_call', owner_user_id=str(owner_user_id),
            site=adapter.id, command=command.name, access=command.access,
            params=summarize_parameters(params), result=outcome,
            duration_ms=round((time.time() - started) * 1000),
            browser_client=used_client)


__all__ = [
    'AdapterValidationError', 'AdapterExecutionError', 'AdapterCommand',
    'SiteAdapter', 'register_adapter', 'unregister_adapter', 'get_adapter',
    'list_adapters', 'adapter_health', 'adapters_payload', 'invoke_adapter',
    'sync_pack_adapters',
]
