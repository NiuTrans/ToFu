"""Execute pack operation step pipelines over BrowserPage primitives.

The step set is closed (contracts/site_pack_v1.schema.json). Every step maps
to existing BrowserPage calls, so owner/device addressing, grant checks,
redaction, and boundedness come from the browser layer unchanged. Mutation
steps (fill/press/click) run with trusted_read=False under access="write"
operations, which invoke_adapter gates through require_access before the
handler runs; inside a read operation only fill/press are permitted and run
under the trusted_read form-fill semantics used by the built-in adapters.
"""

from __future__ import annotations

from lib.browser.access import BrowserAccessDenied, BrowserWriteAuthorizationRequired
from lib.browser.adapter_errors import AdapterExecutionError
from lib.browser.page import BrowserOperationStopped
from lib.browser.protocol import BrowserUpgradeRequired, RESEARCH_CAPABILITIES
from lib.browser.read_pipeline import (
    ensure_site_page,
    extract_result,
    page_content,
    read_detail,
    read_json_payload,
    read_search_records,
    receipt_document_url,
)
from lib.log import get_logger

from ._inputs import parameter_value, prepare_steps, walk_steps
from ._manifest import PackManifest, PackOperation
from ._templates import render_template, render_value

logger = get_logger(__name__)

_PIPELINE_FAILURES = (
    AdapterExecutionError, BrowserAccessDenied, BrowserWriteAuthorizationRequired,
    BrowserUpgradeRequired, BrowserOperationStopped,
)

#: Capability derivation: steps, not hand-maintained lists, own the required
#: browser capabilities, so declarations cannot drift from behavior.
_STEP_CAPABILITIES = {
    'navigate': ('tabs',),
    'ensure_auth': (),
    'fill': ('fill',),
    'press': ('press',),
    'click': ('click',),
    'wait': ('wait',),
    'paginate': ('scroll',),
    'research': tuple(capability.value for capability in RESEARCH_CAPABILITIES),
    'extract': ('execute',),
    'normalize': ('read', 'snapshot'),
    'try': (),
}


def _step_caps(step: dict, *, self_supplied_payload: bool) -> tuple[str, ...]:
    if step['do'] == 'normalize':
        # A research step supplies the payload itself, and json_payload cleans
        # extract/research data locally: neither reads the page again.
        if self_supplied_payload or step.get('kind') == 'json_payload':
            return ()
    return _STEP_CAPABILITIES[step['do']]


def operation_capabilities(operation: PackOperation) -> tuple[str, ...]:
    steps = tuple(walk_steps(operation.steps))
    supplied = any(step['do'] == 'research' for step in steps)
    caps: list[str] = []
    for step in steps:
        for cap in _step_caps(step, self_supplied_payload=supplied):
            if cap not in caps:
                caps.append(cap)
    return tuple(caps)


def _truncate_fields(declared: dict, raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for name, rule in declared.items():
        if isinstance(rule, int):
            out[name] = str(raw.get(name) or '')[:rule]
        else:
            items = raw.get(name)
            items = items if isinstance(items, list) else []
            out[name] = [
                str(v)[:int(rule.get('max_length') or 4096)]
                for v in items[:int(rule.get('max_items') or 100)]
            ]
    return out


def _run_step(pack: PackManifest, operation: PackOperation, step: dict,
              page, params: dict, slots: dict):
    do = step['do']
    mutation_read = operation.access == 'read'
    if do == 'navigate':
        page.new_tab(render_template(step['url'], params))
        return None
    if do == 'ensure_auth':
        ensure_site_page(page, pack.domains, site=pack.name,
                         login_url=pack.auth.login_url)
        return None
    if do == 'fill':
        page.fill(render_value(step['value'], params),
                  selector=step.get('selector') or '',
                  trusted_read=mutation_read)
        return None
    if do == 'press':
        page.press(step['keys'], selector=step.get('selector') or '',
                   trusted_read=mutation_read)
        return None
    if do == 'click':
        # Manifest validation restricts click to access="write" operations.
        page.click(selector=step['selector'], trusted_read=False)
        return None
    if do == 'try':
        result = None
        try:
            for sub in step.get('steps') or []:
                result = _run_step(pack, operation, sub, page, params, slots)
        except _PIPELINE_FAILURES:
            # Scope, live authority and protocol failures remain terminal even
            # inside a best-effort group.
            raise
        except Exception as exc:
            logger.debug('[SitePacks] best-effort step group degraded for '
                         '%s: %s', pack.id, exc)
        return result
    if do == 'wait':
        try:
            page.wait(selector=step['selector'],
                      condition=step['condition'], timeout=step['timeout'])
        except _PIPELINE_FAILURES:
            raise
        except Exception as exc:
            if not step.get('optional'):
                raise
            logger.debug('[SitePacks] optional wait degraded for %s: %s',
                         pack.id, exc)
        return None
    if do == 'paginate':
        pages = parameter_value(step, params, 'pages')
        for _ in range(pages - 1):
            page.scroll(direction='down',
                        amount=step['amount'],
                        trusted_read=True)
        return None
    if do == 'research':
        url = render_template(step['url'], params)
        max_pages = parameter_value(step, params, 'max_pages')
        max_chars = parameter_value(step, params, 'max_chars')
        receipt = page.research(
            url, max_chars=max_chars,
            max_scrolls=step['max_scrolls'],
            max_pages=max_pages,
            pagination=step['pagination'])
        payload = receipt.get('result') if isinstance(receipt, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        payload.setdefault('url', url)
        slots['payload'] = payload
        slots['payload_document_url'] = receipt_document_url(receipt)
        return None
    if do == 'extract':
        names = step.get('args_from_params') or []
        args = {name: params[name] for name in names if name in params} or None
        receipt = page.execute(step['js'], args=args, trusted_read=True)
        slots[step['into']] = extract_result(receipt)
        if step['into'] == 'payload':
            slots['payload_document_url'] = receipt_document_url(receipt)
        return None
    if do == 'normalize':
        kind = step['kind']
        if kind == 'search_records':
            query = str(params.get(step['query_from_param']) or '').strip()
            limit = parameter_value(step, params, 'limit')
            candidates = slots.get('candidates')
            return read_search_records(
                page, query, limit, pack.id, step['source_name'],
                candidates=candidates if isinstance(candidates, list) else [],
                payload=slots.get('payload'))
        if kind == 'json_payload':
            return read_json_payload(
                page, slots.get('payload'), document_url=slots.get('payload_document_url'),
                site=pack.name,
                data_field=step['data_field'], coverage_scope=step['coverage_scope'],
                max_output_bytes=step['max_output_bytes'])
        payload = slots.get('payload')
        max_chars = parameter_value(step, params, 'max_chars')
        if payload is not None:
            return page_content(page, target={'kind': 'article'},
                                payload=payload, max_chars=max_chars)
        fields = _truncate_fields(step.get('fields') or {}, slots.get('fields'))
        return read_detail(page, {**params, 'max_chars': max_chars}, fields=fields)
    raise ValueError(f'unknown step {do!r}')  # schema validation prevents this


def operation_handler(pack: PackManifest, operation: PackOperation):
    """Capture steps for the shared adapter's validated-input execution port."""
    steps = prepare_steps(operation.steps)
    def handler(page, params):
        slots: dict = {}
        result = None
        for step in steps:
            result = _run_step(pack, operation, step, page, params, slots)
        return result
    return handler
