"""Shared read pipeline for native site operations.

These helpers are the acceptance tail of every site read: ensure the page
stayed inside the adapter's login domain, unwrap execution receipts, run the
collected payload through content.py acceptance, and shape search/detail
results. Built-in adapters (lib/browser/adapters.py) and the site-pack
interpreter (lib/site_packs/) both execute on top of them; content.py remains
the sole acceptance owner for DOM, snapshot and captured data. Dedicated JSON
attributes use read_json_payload with JsonReadSource and the same work cleaner;
receipt_document_url preserves the capture envelope's actual document context.
"""

from __future__ import annotations

import json
from urllib.parse import unquote, unquote_plus

from lib.log import get_logger

from .access import normalize_domain
from .adapter_errors import AdapterExecutionError
from .page import BrowserPage
from .site_scope import matches_site_domain

logger = get_logger(__name__)


def extract_result(receipt: dict):
    result = receipt.get('result') if isinstance(receipt, dict) else None
    if isinstance(result, dict) and 'value' in result:
        return result['value']
    return result


def receipt_document_url(receipt) -> str:
    """Read capture context from the BrowserPage envelope, never the payload."""
    page = receipt.get('page') if isinstance(receipt, dict) else None
    url = page.get('url') if isinstance(page, dict) else None
    return url if isinstance(url, str) else ''


def ensure_site_page(page: BrowserPage, domains: tuple[str, ...], *, site: str,
                     login_url: str) -> None:
    host = normalize_domain(page.current_url)
    if matches_site_domain(page.current_url, domains):
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
        query = target.get('query', '') if isinstance(target, dict) else ''
        if _names_query(str(payload.get('finalUrl') or payload.get('url') or ''), query):
            # The site already ranked these cards for this query. They replace
            # the generic link inventory and incidental API/hydration data and
            # need not repeat a compound query verbatim.
            for key in ('network', 'networkResponses', 'initialStatePayloads'):
                payload.pop(key, None)
            snapshot['records'] = list(candidates)[:100]
            target = {**target, 'query': ''}
        else:
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


def _names_query(url: str, query: str) -> bool:
    """Whether the accepted page URL still carries the searched query."""
    needle = query.strip().casefold()
    return bool(needle) and any(needle in form.casefold()
                                for form in (unquote(url), unquote_plus(url)))


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


def read_json_payload(page: BrowserPage, payload, *, document_url: str, site: str, data_field: str,
                      coverage_scope: str, max_output_bytes: int) -> dict:
    """Admit observed same-origin attributes through shared policy and cleaning."""
    from .content import WorkBudget
    from .content_contract import JsonReadSource, ReadTarget

    payload = payload if isinstance(payload, dict) else {}
    source = None
    if payload.get('source') is not None:
        try:
            source = JsonReadSource.from_value(
                payload['source'], expected_document_url=document_url)
        except ValueError:
            raise AdapterExecutionError('source_unverified',
                                        f'{site} JSON source could not be verified') from None
    status = payload.get('status')
    if payload.get('ok') is not True or type(status) is not int or not 200 <= status < 300:
        raise AdapterExecutionError('fetch_failed',
            f'{site} data API returned HTTP {status if type(status) is int else "unknown"}',
            retryable=True)
    if source is None:
        raise AdapterExecutionError('source_unverified',
            f'{site} JSON payload must identify its document, request and final response URLs')
    for url in dict.fromkeys(source.urls):
        page.authorize_read_source(url)
    if data_field not in payload:
        raise AdapterExecutionError('invalid_json_payload',
            f'{site} JSON payload is missing its declared data field')
    work = WorkBudget()
    data = work.clean(payload[data_field], ReadTarget(kind='record'), source.document_url)
    truncated = bool(work.truncated or payload.get('truncated'))
    result = {'status': 'partial' if truncated else 'ok', 'data': data,
              'coverage': {'scope': coverage_scope, 'truncated': truncated}}
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > max_output_bytes:
        raise AdapterExecutionError('output_budget_exhausted',
            f'{site} attributes exceeded the output budget; request fewer items')
    return result
