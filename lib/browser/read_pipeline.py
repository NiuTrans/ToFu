"""Shared read pipeline for native site operations.

These helpers are the acceptance tail of every site read: ensure the page
stayed inside the adapter's login domain, unwrap execution receipts, run the
collected payload through content.py acceptance, and shape search/detail
results. Built-in adapters (lib/browser/adapters.py) and the site-pack
interpreter (lib/site_packs/) both execute on top of them; content.py remains
the sole acceptance owner for DOM, snapshot and captured data.
"""

from __future__ import annotations

from lib.log import get_logger

from .access import normalize_domain
from .adapter_errors import AdapterExecutionError
from .page import BrowserPage

logger = get_logger(__name__)


def extract_result(receipt: dict):
    result = receipt.get('result') if isinstance(receipt, dict) else None
    if isinstance(result, dict) and 'value' in result:
        return result['value']
    return result


def ensure_site_page(page: BrowserPage, domains: tuple[str, ...], *, site: str,
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


def page_content(page: BrowserPage, *, target=None, max_chars=60000,
                 candidates=None, payload=None) -> dict:
    from .content import collect_content
    if payload is None:
        receipt = page.read(max_chars=max_chars)
        payload = dict(receipt.get('result') or {})
        payload.setdefault('url', (receipt.get('page') or {}).get('url') or page.current_url)
    if candidates:
        payload = dict(payload)
        snapshot = dict(payload.get('contentSnapshot') or {})
        snapshot['records'] = (list(snapshot.get('records') or []) + candidates)[:100]
        snapshot.setdefault('text', payload.get('text') or payload.get('collectedText') or '')
        payload['contentSnapshot'] = snapshot
    content = collect_content(payload, owner_user_id=page.lease.owner_user_id,
                              target=target, max_chars=max_chars)
    if content['status'] in ('blocked', 'unavailable'):
        raise AdapterExecutionError(
            'content_' + content['status'],
            'Target content was not acquired: ' + content['coverage']['stop_reason'],
            retryable=content['status'] == 'unavailable',
            details={'coverage': content['coverage']})
    return content


def read_search_records(page, query, limit, source_id, source_name, *,
                        candidates=None, payload=None):
    content = page_content(page, target={'kind': 'list', 'query': query},
                           candidates=candidates, payload=payload)
    if not content['records'] and content['status'] != 'empty':
        raise AdapterExecutionError('extraction_incomplete',
                                    'Page loaded without validated result records', retryable=True)
    return [{**record, 'source': source_name,
             'metadata': {'adapter': source_id, 'coverage': content['coverage']}}
            for record in content['records'][:limit]]


def read_detail(page, params, *, fields=None):
    content = page_content(page, target={'kind': 'article'},
                           max_chars=int(params.get('max_chars') or 60000))
    return {**(fields or {}), 'title': content['title'], 'url': content['url'],
            'content': content['content'], 'status': content['status'],
            'coverage': content['coverage']}
