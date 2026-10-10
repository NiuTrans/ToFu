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

import json
import re
from urllib.parse import quote

from lib.browser.adapter_errors import AdapterExecutionError
from lib.browser.read_pipeline import (
    ensure_site_page,
    extract_result,
    page_content,
    read_detail,
    read_search_records,
)
from lib.log import get_logger

from ._manifest import PackManifest, PackOperation

logger = get_logger(__name__)

_TEMPLATE_RE = re.compile(
    r'\{([a-zA-Z_][a-zA-Z0-9_]*)(\|raw|\|floor:(\d+))?\}')

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
    'research': ('deep_collect', 'network_body'),
    'extract': ('execute',),
    'normalize': ('read', 'snapshot'),
    'try': (),
}


def _step_caps(step: dict, *, self_supplied_payload: bool) -> tuple[str, ...]:
    if step['do'] == 'try':
        caps: list[str] = []
        for sub in step.get('steps') or []:
            for cap in _step_caps(sub, self_supplied_payload=self_supplied_payload):
                if cap not in caps:
                    caps.append(cap)
        return tuple(caps)
    if step['do'] == 'normalize':
        # A research step supplies the payload itself, and json_payload cleans
        # extract/research data locally: neither reads the page again.
        if self_supplied_payload or step.get('kind') == 'json_payload':
            return ()
    return _STEP_CAPABILITIES[step['do']]


def operation_capabilities(operation: PackOperation) -> tuple[str, ...]:
    def has_research(steps) -> bool:
        return any(
            s['do'] == 'research'
            or (s['do'] == 'try' and has_research(s.get('steps')))
            for s in steps or ())
    supplied = has_research(operation.steps)
    caps: list[str] = []
    for step in operation.steps:
        for cap in _step_caps(step, self_supplied_payload=supplied):
            if cap not in caps:
                caps.append(cap)
    return tuple(caps)


def render_template(template: str, params: dict) -> str:
    """Interpolate {param} URL-quoted, {param|raw} verbatim, and
    {param|floor:N} as an integer clamped to a lower bound N."""
    def repl(match: re.Match) -> str:
        name, modifier, floor = match.group(1), match.group(2), match.group(3)
        value = params.get(name)
        if floor is not None:
            try:
                return str(max(int(floor), int(value)))
            except (TypeError, ValueError):
                return str(int(floor))
        text = str(value or '')
        return text if modifier else quote(text)
    return _TEMPLATE_RE.sub(repl, template or '')


def render_value(template: str, params: dict) -> str:
    """Form-value interpolation: {param} is verbatim (never URL-quoted)."""
    return _TEMPLATE_RE.sub(
        lambda m: str(params.get(m.group(1)) or ''), template or '')


def _clamp_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


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
        except AdapterExecutionError:
            # Typed failures (login_required, …) are pipeline-significant and
            # never swallowed by a best-effort group.
            raise
        except Exception as exc:
            logger.debug('[SitePacks] best-effort step group degraded for '
                         '%s: %s', pack.id, exc)
        return result
    if do == 'wait':
        try:
            page.wait(selector=step['selector'],
                      condition=step.get('condition') or 'visible',
                      timeout=int(step.get('timeout') or 10))
        except Exception as exc:
            if not step.get('optional'):
                raise
            logger.debug('[SitePacks] optional wait degraded for %s: %s',
                         pack.id, exc)
        return None
    if do == 'paginate':
        pages = _clamp_int(params.get(step.get('times_from_param') or ''),
                           default=1, minimum=1,
                           maximum=int(step.get('max_times') or 1) + 1)
        for _ in range(pages - 1):
            page.scroll(direction='down',
                        amount=int(step.get('amount') or 1200),
                        trusted_read=True)
        return None
    if do == 'research':
        url = render_template(step['url'], params)
        max_pages = _clamp_int(
            params.get(step.get('max_pages_from_param') or ''),
            default=int(step.get('max_pages') or 1), minimum=1, maximum=8) \
            if step.get('max_pages_from_param') \
            else int(step.get('max_pages') or 1)
        max_chars = _clamp_int(
            params.get(step.get('max_chars_from_param') or ''),
            default=int(step.get('max_chars') or 80_000),
            minimum=1000, maximum=200_000) \
            if step.get('max_chars_from_param') \
            else int(step.get('max_chars') or 80_000)
        receipt = page.research(
            url, max_chars=max_chars,
            max_scrolls=int(step.get('max_scrolls') or 4),
            max_pages=max_pages,
            pagination=step.get('pagination') or 'auto')
        payload = receipt.get('result') if isinstance(receipt, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        payload.setdefault('url', url)
        slots['payload'] = payload
        return None
    if do == 'extract':
        names = step.get('args_from_params') or []
        args = {name: params[name] for name in names if name in params} or None
        slots[step['into']] = extract_result(
            page.execute(step['js'], args=args, trusted_read=True))
        return None
    if do == 'normalize':
        kind = step['kind']
        if kind == 'search_records':
            query = str(params.get(step['query_from_param']) or '').strip()
            limit = _clamp_int(params.get(step['limit_from_param']),
                               default=20, minimum=1,
                               maximum=int(step.get('limit_max') or 100))
            candidates = slots.get('candidates')
            return read_search_records(
                page, query, limit, pack.id, step['source_name'],
                candidates=candidates if isinstance(candidates, list) else [],
                payload=slots.get('payload'))
        if kind == 'json_payload':
            return _normalize_json_payload(pack, step, page, slots)
        payload = slots.get('payload')
        max_chars = int(
            params.get(step.get('max_chars_from_param') or '')
            or step.get('max_chars_default') or 60000)
        if payload is not None:
            return page_content(page, target={'kind': 'article'},
                                payload=payload, max_chars=max_chars)
        fields = _truncate_fields(step.get('fields') or {}, slots.get('fields'))
        if step.get('max_chars_from_param') or 'max_chars_default' in step:
            params = {**params, 'max_chars': max_chars}
        return read_detail(page, params, fields=fields)
    raise ValueError(f'unknown step {do!r}')  # schema validation prevents this


def _normalize_json_payload(pack: PackManifest, step: dict, page,
                            slots: dict) -> dict:
    """Acceptance tail for same-origin JSON data APIs read in page context."""
    payload = slots.get('payload')
    payload = payload if isinstance(payload, dict) else {}
    if not payload.get('ok'):
        raise AdapterExecutionError(
            'fetch_failed',
            '%s data API returned HTTP %s'
            % (pack.name, payload.get('status') or 'unknown'),
            retryable=True)
    from lib.browser.content import WorkBudget
    from lib.browser.content_contract import ReadTarget
    work = WorkBudget()
    data = work.clean(payload.get(step.get('data_field') or 'data', []),
                      ReadTarget(kind='record'), page.current_url)
    truncated = bool(work.truncated or payload.get('truncated'))
    result = {
        'status': 'partial' if truncated else 'ok',
        'data': data,
        'coverage': {
            'scope': step.get('coverage_scope') or 'requested_attributes',
            'truncated': truncated,
        },
    }
    budget = int(step.get('max_output_bytes') or 60000)
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        raise AdapterExecutionError(
            'output_budget_exhausted',
            f'{pack.name} attributes exceeded the output budget; '
            'request fewer items')
    return result


def operation_handler(pack: PackManifest, operation: PackOperation):
    """Compile an operation's steps into an adapter command handler."""
    def handler(page, params):
        slots: dict = {}
        result = None
        for step in operation.steps:
            result = _run_step(pack, operation, step, page, params, slots)
        return result
    return handler
