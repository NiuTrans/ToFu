"""Native site-adapter contract and built-in read adapters."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote, urlencode

from lib.log import audit_log, get_logger

from .access import (
    BrowserAccessDenied,
    BrowserWriteAuthorizationRequired,
    normalize_domain,
    require_access,
    summarize_parameters,
)
from .page import BrowserPage
from .protocol import (
    ALL_CAPABILITIES,
    BrowserCapability,
    BrowserUpgradeRequired,
    client_protocol,
)
from .sessions import acquire_browser_lease

logger = get_logger(__name__)


class AdapterValidationError(ValueError):
    pass


class AdapterExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable=False, details=None):
        self.code = code
        self.retryable = bool(retryable)
        self.details = dict(details or {})
        super().__init__(message)

    def as_dict(self) -> dict:
        return {'code': self.code, 'message': str(self),
                'retryable': self.retryable, 'details': self.details}


@dataclass(frozen=True)
class AdapterCommand:
    name: str
    description: str
    access: str = 'read'
    input_schema: dict = field(default_factory=lambda: {'type': 'object'})
    output_schema: dict = field(default_factory=lambda: {'type': 'object'})
    required_capabilities: tuple[str, ...] = ()
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
        }


_registry: dict[str, SiteAdapter] = {}


def register_adapter(adapter: SiteAdapter, *, replace=False) -> None:
    if not isinstance(adapter, SiteAdapter):
        raise AdapterValidationError('adapter must be a SiteAdapter')
    if adapter.id in _registry and not replace:
        raise AdapterValidationError(f'adapter already registered: {adapter.id}')
    _registry[adapter.id] = adapter


def unregister_adapter(adapter_id: str) -> bool:
    return _registry.pop(adapter_id, None) is not None


def get_adapter(adapter_id_or_alias: str) -> SiteAdapter | None:
    needle = str(adapter_id_or_alias or '').strip().lower()
    if needle in _registry:
        return _registry[needle]
    for adapter in _registry.values():
        if needle in {a.lower() for a in adapter.aliases}:
            return adapter
    return None


def list_adapters() -> list[SiteAdapter]:
    return sorted(_registry.values(), key=lambda a: a.id)


def _validate_value(schema: dict, value, *, path='value') -> None:
    """Validate the bounded JSON-schema subset used by adapter manifests."""
    typ = schema.get('type')
    if typ == 'object':
        if not isinstance(value, dict):
            raise AdapterValidationError(f'{path} must be an object')
        for key in schema.get('required', []):
            if key not in value:
                raise AdapterValidationError(f'missing required parameter: {key}')
        props = schema.get('properties', {})
        for key, child in value.items():
            if key in props:
                _validate_value(props[key], child, path=f'{path}.{key}')
        return
    if typ == 'array':
        if not isinstance(value, list):
            raise AdapterValidationError(f'{path} must be an array')
        child_schema = schema.get('items')
        if isinstance(child_schema, dict):
            for index, child in enumerate(value):
                _validate_value(child_schema, child, path=f'{path}[{index}]')
        return
    if typ == 'string' and not isinstance(value, str):
        raise AdapterValidationError(f'{path} must be a string')
    if typ == 'integer' and (isinstance(value, bool) or not isinstance(value, int)):
        raise AdapterValidationError(f'{path} must be an integer')
    if typ == 'number' and (isinstance(value, bool)
                            or not isinstance(value, (int, float))):
        raise AdapterValidationError(f'{path} must be a number')
    if typ == 'boolean' and not isinstance(value, bool):
        raise AdapterValidationError(f'{path} must be a boolean')


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
            result = command.handler(page, params)
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


_XHS_EXTRACT = r"""
(() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/explore/"],a[href*="/search_result/"]')) {
    const url = a.href || ''; if (!url || seen.has(url.split('?')[0])) continue;
    const card = a.closest('section.note-item,div.note-item,section,div') || a;
    const lines = (card.innerText || '').split('\n').map(x => x.trim()).filter(Boolean);
    if (!lines.length) continue; seen.add(url.split('?')[0]);
    out.push({title: lines[0].slice(0, 200), snippet: lines.slice(1, 3).join(' · ').slice(0, 300), url});
    if (out.length >= 30) break;
  }
  return out;
})()
"""


def _extract_result(receipt: dict):
    result = receipt.get('result') if isinstance(receipt, dict) else None
    if isinstance(result, dict) and 'value' in result:
        return result['value']
    return result


def _ensure_site_page(page: BrowserPage, domains: tuple[str, ...], *, site: str,
                      login_url: str) -> None:
    host = normalize_domain(page.current_url)
    if host and any(host == normalize_domain(domain)
                    or host.endswith('.' + normalize_domain(domain))
                    for domain in domains):
        return
    raise AdapterExecutionError(
        'login_required',
        f'{site} redirected outside its adapter domain; finish sign-in in the browser',
        retryable=True, details={'login_url': login_url, 'redirect_domain': host})


def _xhs_search(page: BrowserPage, params: dict) -> list[dict]:
    query = params['query'].strip()
    limit = max(1, min(30, int(params.get('limit') or 20)))
    page.new_tab('https://www.xiaohongshu.com/search_result?keyword=' + quote(query)
                 + '&source=web_search_result_notes')
    _ensure_site_page(
        page, ('xiaohongshu.com',), site='Xiaohongshu',
        login_url='https://www.xiaohongshu.com/')
    try:
        page.wait(selector='a[href*="/explore/"],a[href*="/search_result/"]', timeout=12)
    except Exception as exc:
        logger.debug('[Browser] Xiaohongshu result wait degraded: %s', exc)
    for _ in range(max(0, min(4, int(params.get('pages') or 1) - 1))):
        page.scroll(direction='down', amount=1200, trusted_read=True)
    raw = _extract_result(page.execute(_XHS_EXTRACT, trusted_read=True))
    items = raw if isinstance(raw, list) else []
    return [{
        'title': str(item.get('title') or '')[:200],
        'url': str(item.get('url') or ''),
        'snippet': str(item.get('snippet') or '')[:300],
        'source': 'Xiaohongshu',
        'metadata': {'adapter': 'xiaohongshu'},
    } for item in items if isinstance(item, dict) and item.get('url')][:limit]


def _xhs_detail(page: BrowserPage, params: dict) -> dict:
    page.new_tab(params['url'])
    _ensure_site_page(
        page, ('xiaohongshu.com',), site='Xiaohongshu',
        login_url='https://www.xiaohongshu.com/')
    raw = _extract_result(page.execute(r"""
(() => {
  const text = (sel) => (document.querySelector(sel)?.innerText || '').trim();
  const tags = Array.from(document.querySelectorAll(
    'a[href*="/search_result?keyword="],.tag,.note-tag'))
    .map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 30);
  return {
    title: text('#detail-title,.title,.note-title,h1'),
    author: text('.author-name,.username,.user-name,[class*="author"]'),
    published_at: text('.date,.time,.publish-date,[class*="date"]'),
    likes: text('.like-wrapper,.like-count,[class*="like"] .count'),
    comments: text('.comments-container .total,.comment-count,[class*="comment"] .count'),
    tags,
    content: text('#detail-desc,.desc,.note-content,[class*="content"]'),
  };
})()
""", trusted_read=True))
    fields = raw if isinstance(raw, dict) else {}
    read = page.read(max_chars=int(params.get('max_chars') or 60_000))
    result = read.get('result') if isinstance(read, dict) else {}
    return {'title': fields.get('title') or (read.get('page') or {}).get('title', ''),
            'url': (read.get('page') or {}).get('url', params['url']),
            'content': fields.get('content') or (result or {}).get('text')
            or (result or {}).get('html') or '',
            'author': str(fields.get('author') or ''),
            'published_at': str(fields.get('published_at') or ''),
            'likes': str(fields.get('likes') or ''),
            'comments': str(fields.get('comments') or ''),
            'tags': fields.get('tags') if isinstance(fields.get('tags'), list) else []}


_MODEL_PLAZA_EXTRACT = r"""
(() => Array.from(document.querySelectorAll('a[href]')).map(a => {
  const card = a.closest('[class*="model"],article,li,tr,div') || a;
  const text = (card.innerText || a.innerText || '').trim();
  return {title: (a.innerText || text.split('\n')[0] || '').trim().slice(0, 200),
          snippet: text.slice(0, 400), url: a.href || ''};
}).filter(x => x.title && /model|模型/i.test(x.snippet + ' ' + x.url)).slice(0, 40))()
"""

_MODEL_PLAZA_URL = 'https://api.openai.com/ml/modelPlaza/modelInfo'

_FRIDAY_MARKET_BASE_URL = 'https://friday.internal.example.com/skills/skills-market'


def _friday_market_url(query: str, *, page_size: int) -> str:
    """Return the canonical skills-market view with an explicit stable filter set."""
    return _FRIDAY_MARKET_BASE_URL + '?' + urlencode({
        'deepSearch': 'false',
        'keyword': str(query or '').strip(),
        'mainView': 'skill',
        'orderByDownloadCount': 'all',
        'orderByTotalCallCount': 'all',
        'orderByTotalCallerCount': 'all',
        'page': 1,
        'pageSize': max(1, min(100, int(page_size))),
        'securityScanStatus': 'all',
        'spaceKeyword': '',
        'spaceSortOrder': 'default',
        'spaceTypeFilter': 'org,project',
        'spaceVerifiedFilter': 'all',
        'supportEnv': 'all',
        'tag': '',
        'verifiedType': 'all',
        'viewMode': 'card',
        'visibility': 'all',
    })


def _friday_search(page: BrowserPage, params: dict) -> list[dict]:
    query = str(params.get('query') or '').strip()
    limit = max(1, min(100, int(params.get('limit') or 30)))
    pages = max(1, min(5, int(params.get('pages') or 1)))
    url = _friday_market_url(query, page_size=max(30, limit))
    receipt = page.research(
        url, max_chars=80_000, max_scrolls=4,
        max_pages=pages, pagination='auto')
    payload = receipt.get('result') if isinstance(receipt, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    from .network_evidence import extract_business_records
    records = extract_business_records(
        payload, owner_user_id=page.lease.owner_user_id,
        source_url=url, query=query, limit=limit)
    return [{
        'title': record['title'],
        'url': record['url'],
        'snippet': record['snippet'],
        'source': 'Friday Skills Market',
        'metadata': {
            **(record.get('metadata') or {}),
            'id': record.get('id') or '',
            'adapter': 'friday',
        },
    } for record in records]


def _friday_detail(page: BrowserPage, params: dict) -> dict:
    receipt = page.research(
        params['url'], max_chars=int(params.get('max_chars') or 80_000),
        max_scrolls=3, max_pages=1, pagination='none')
    payload = receipt.get('result') if isinstance(receipt, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    from .research import render_research_payload
    content = render_research_payload(
        payload, owner_user_id=page.lease.owner_user_id,
        mode='content', max_chars=int(params.get('max_chars') or 80_000))
    return {
        'title': str(payload.get('title') or ''),
        'url': str(payload.get('url') or params['url']),
        'content': content,
    }


def _model_plaza_search(page: BrowserPage, params: dict) -> list[dict]:
    query = params['query'].strip()
    page.new_tab(_MODEL_PLAZA_URL)
    _ensure_site_page(
        page, ('your-llm-gateway.example.com',), site='ModelPlaza',
        login_url=_MODEL_PLAZA_URL)
    search_selector = ('input[type="search"],input[placeholder*="搜索"],'
                       'input[placeholder*="模型"]')
    try:
        page.fill(query, selector=search_selector, trusted_read=True)
        _ensure_site_page(
            page, ('your-llm-gateway.example.com',), site='ModelPlaza',
            login_url=_MODEL_PLAZA_URL)
        page.press('Enter', selector=search_selector, trusted_read=True)
        _ensure_site_page(
            page, ('your-llm-gateway.example.com',), site='ModelPlaza',
            login_url=_MODEL_PLAZA_URL)
        page.wait(selector='a[href]', timeout=8)
    except AdapterExecutionError:
        raise
    except Exception as exc:
        # Some catalogue variants expose filters but no text input. The
        # rendered-list extraction below remains a useful read-only fallback.
        logger.debug('[Browser] ModelPlaza text search unavailable: %s', exc)
    _ensure_site_page(
        page, ('your-llm-gateway.example.com',), site='ModelPlaza',
        login_url=_MODEL_PLAZA_URL)
    # Query the live rendered model catalogue.  The server never resolves or
    # connects to this private hostname; all access remains in the user browser.
    # Expressions receive args as a named value in protocol v2; keep query in
    # the args object rather than interpolating it into executable JS.
    raw = _extract_result(page.execute(
        _MODEL_PLAZA_EXTRACT, args={'query': query}, trusted_read=True))
    items = raw if isinstance(raw, list) else []
    q = query.lower()
    items = [item for item in items if q in
             (str(item.get('title') or '') + ' ' + str(item.get('snippet') or '')).lower()]
    limit = max(1, min(40, int(params.get('limit') or 20)))
    return [{**item, 'source': 'ModelPlaza',
             'metadata': {'adapter': 'modelplaza'}} for item in items[:limit]]


def _model_plaza_detail(page: BrowserPage, params: dict) -> dict:
    page.new_tab(params['url'])
    _ensure_site_page(
        page, ('your-llm-gateway.example.com',), site='ModelPlaza',
        login_url=_MODEL_PLAZA_URL)
    read = page.read(max_chars=int(params.get('max_chars') or 80_000))
    result = read.get('result') or {}
    return {'title': (read.get('page') or {}).get('title', ''),
            'url': (read.get('page') or {}).get('url', params['url']),
            'content': result.get('text') or result.get('html') or ''}


_DETAIL_CAPS = (BrowserCapability.TABS.value, BrowserCapability.READ.value,
                BrowserCapability.SNAPSHOT.value)
_XHS_DETAIL_CAPS = _DETAIL_CAPS + (BrowserCapability.EXECUTE.value,)
_CATALOG_SEARCH_CAPS = (
    BrowserCapability.TABS.value, BrowserCapability.EXECUTE.value,
    BrowserCapability.SNAPSHOT.value,
)
_XHS_SEARCH_CAPS = _CATALOG_SEARCH_CAPS + (
    BrowserCapability.WAIT.value, BrowserCapability.SCROLL.value,
)
_MODEL_SEARCH_CAPS = _CATALOG_SEARCH_CAPS + (
    BrowserCapability.FILL.value, BrowserCapability.PRESS.value,
    BrowserCapability.WAIT.value,
)
_DEEP_RESEARCH_CAPS = (
    BrowserCapability.DEEP_COLLECT.value,
    BrowserCapability.NETWORK_BODY.value,
)
_SEARCH_SCHEMA = {'type': 'object', 'properties': {
    'query': {'type': 'string'}, 'limit': {'type': 'integer'},
    'pages': {'type': 'integer'}}, 'required': ['query']}
_DETAIL_SCHEMA = {'type': 'object', 'properties': {
    'url': {'type': 'string'}, 'max_chars': {'type': 'integer'}},
    'required': ['url']}
_RESULT_LIST_SCHEMA = {'type': 'array', 'items': {'type': 'object'}}
_DETAIL_RESULT_SCHEMA = {'type': 'object', 'properties': {
    'title': {'type': 'string'}, 'url': {'type': 'string'},
    'content': {'type': 'string'}}}


def _register_builtins() -> None:
    register_adapter(SiteAdapter(
        id='xiaohongshu', name='小红书',
        domains=('xiaohongshu.com',), aliases=('xhs', 'red', '小红书'),
        login_url='https://www.xiaohongshu.com/',
        risk_notice='读取带节奏控制；遇到验证或连续空结果时应退避。',
        builtin=True, commands=(
            AdapterCommand('search', '站内搜索、翻页并读取笔记卡片',
                           input_schema=_SEARCH_SCHEMA,
                           output_schema=_RESULT_LIST_SCHEMA,
                           required_capabilities=_XHS_SEARCH_CAPS, timeout=35,
                           handler=_xhs_search),
            AdapterCommand('detail', '读取笔记详情', input_schema=_DETAIL_SCHEMA,
                           output_schema=_DETAIL_RESULT_SCHEMA,
                           required_capabilities=_XHS_DETAIL_CAPS, timeout=35,
                           handler=_xhs_detail),
        )))
    register_adapter(SiteAdapter(
        id='modelplaza', name='ModelPlaza',
        domains=('your-llm-gateway.example.com',), aliases=('model-plaza', '模型广场'),
        login_url=_MODEL_PLAZA_URL,
        risk_notice='使用浏览器中的美团 SSO 活会话；无需服务器私网放行。',
        builtin=True, commands=(
            AdapterCommand('search', '检索、筛选并读取模型列表',
                           input_schema=_SEARCH_SCHEMA,
                           output_schema=_RESULT_LIST_SCHEMA,
                           required_capabilities=_MODEL_SEARCH_CAPS, timeout=35,
                           handler=_model_plaza_search),
            AdapterCommand('detail', '读取模型详情', input_schema=_DETAIL_SCHEMA,
                           output_schema=_DETAIL_RESULT_SCHEMA,
                           required_capabilities=_DETAIL_CAPS, timeout=35,
                           handler=_model_plaza_detail),
        )))
    register_adapter(SiteAdapter(
        id='friday', name='Friday Skills Market',
        domains=('friday.internal.example.com',),
        aliases=('friday-skills', 'skills-market', '技能市场'),
        login_url=_FRIDAY_MARKET_BASE_URL,
        risk_notice='使用浏览器中的美团 SSO 活会话；采集正文仅在内存中短暂保留并按域复核。',
        builtin=True, version=1, commands=(
            AdapterCommand(
                'search', '检索技能市场并跨虚拟滚动/分页读取结构化技能记录',
                input_schema=_SEARCH_SCHEMA,
                output_schema=_RESULT_LIST_SCHEMA,
                required_capabilities=_DEEP_RESEARCH_CAPS,
                timeout=90, handler=_friday_search),
            AdapterCommand(
                'detail', '深层读取技能市场页面或技能详情',
                input_schema=_DETAIL_SCHEMA,
                output_schema=_DETAIL_RESULT_SCHEMA,
                required_capabilities=_DEEP_RESEARCH_CAPS,
                timeout=90, handler=_friday_detail),
        )))


_register_builtins()


__all__ = [
    'AdapterValidationError', 'AdapterExecutionError', 'AdapterCommand',
    'SiteAdapter', 'register_adapter', 'unregister_adapter', 'get_adapter',
    'list_adapters', 'adapter_health', 'adapters_payload', 'invoke_adapter',
]
