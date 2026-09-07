"""Generic authenticated-site reconnaissance and deep content collection.

The extension owns the temporary tab, logged-in browser context, scrolling,
pagination and raw transport capture.  This module is the sole model-context
boundary: it re-authorizes the final page and every response URL, classifies
the observed data source, redacts structured state, and applies one output
budget.  Nothing captured here is durable.

Entry point: ``research_page``.
"""

from __future__ import annotations

import json
import re

from lib.log import get_logger

from .network_evidence import (
    analyze_network_evidence,
    merge_page_and_network,
    redact_url,
    redact_value,
    render_network_evidence,
)

logger = get_logger(__name__)

_MAX_CONTEXT_CHARS = 80_000
_MAX_INITIAL_STATE_CHARS = 20_000
_WAF_COOKIE_NAMES = {
    'aliyun_waf': {'acw_sc__v2', 'acw_tc', 'ssxmod_itna'},
    'cloudflare': {'__cf_bm', 'cf_clearance', '__cfduid'},
    'akamai': {'_abck', 'bm_sz', 'bm_sv'},
}
_WAF_BODY_MARKERS = {
    'aliyun_waf': (r"arg1\s*=\s*['\"][0-9A-F]{30,}", r'/ntc_captcha/'),
    'cloudflare': (r'Cloudflare Ray ID', r'Checking your browser', r'cf-chl-'),
    'akamai': (r'akamai',),
    'geetest': (r'geetest', r'gt_captcha'),
}


def _bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(maximum, normalized))


def _anti_bot_report(payload: dict, *, owner_user_id: str) -> dict:
    names = {
        str(name) for name in payload.get('cookieNames', [])[:80]
        if isinstance(name, str)
    } if isinstance(payload, dict) else set()
    for vendor, signals in _WAF_COOKIE_NAMES.items():
        matches = sorted(names & signals)
        if matches:
            return {
                'detected': True, 'vendor': vendor,
                'evidence': [f'cookie:{name}' for name in matches],
            }
    previews = []
    policy_error = None
    network = payload.get('network') if isinstance(payload, dict) else None
    if isinstance(network, dict):
        for row in (network.get('responses') or [])[:80]:
            if not isinstance(row, dict):
                continue
            try:
                from .access import is_read_allowed
                if not is_read_allowed(
                        owner_user_id, str(row.get('url') or '')):
                    continue
            except Exception as exc:
                if policy_error is None:
                    policy_error = exc
                continue
            preview = row.get('responsePreview')
            if isinstance(preview, str):
                previews.append(preview[:8_000])
    if policy_error is not None:
        logger.debug(
            '[BrowserResearch] response policy check failed closed; '
            'captured evidence was skipped: %s',
            policy_error,
        )
    body_sample = '\n'.join(previews)
    for vendor, patterns in _WAF_BODY_MARKERS.items():
        if any(re.search(pattern, body_sample, re.I) for pattern in patterns):
            return {
                'detected': True, 'vendor': vendor,
                'evidence': ['captured response marker'],
            }
    return {'detected': False, 'vendor': None, 'evidence': []}


def analyze_research_payload(payload: dict, *, owner_user_id: str) -> dict:
    """Classify the most stable observed content source for one research run."""
    network = analyze_network_evidence(
        payload, owner_user_id=owner_user_id, max_entries=12)
    initial = payload.get('initialState') if isinstance(payload, dict) else {}
    initial = initial if isinstance(initial, dict) else {}
    present_state = sorted(
        str(name) for name, present in initial.items() if bool(present))
    candidates = network['candidates']
    blocked = [row for row in candidates if row['verdict'] == 'blocked']
    useful = [row for row in candidates
              if row['verdict'] in ('likely_data', 'maybe_data')]
    if len(blocked) >= 2:
        strategy = 'token_gated_api'
        reason = f'{len(blocked)} captured endpoints require authentication'
        next_step = 'Finish sign-in in this browser profile, then retry the same URL.'
    elif present_state:
        strategy = 'hydrated_state'
        reason = 'Page exposes structured initial state: ' + ', '.join(present_state)
        next_step = 'Use the captured initial-state payload; network data remains available for later pages.'
    elif useful:
        strategy = 'captured_api'
        reason = f'{len(useful)} business-shaped API endpoint(s) were captured'
        next_step = 'Use the ranked API bodies; their endpoint shapes are listed below.'
    else:
        strategy = 'rendered_dom'
        reason = 'No validated structured endpoint or hydration state was observed'
        next_step = 'Use the accumulated rendered text and stable DOM snapshot tools for targeted interaction.'
    return {
        'strategy': strategy,
        'reason': reason,
        'recommended_next_step': next_step,
        'initial_state': present_state,
        'anti_bot': _anti_bot_report(
            payload, owner_user_id=owner_user_id),
        'network': network,
    }


def _render_shape(shape: dict) -> str:
    if not isinstance(shape, dict) or not shape:
        return '(body unavailable)'
    return ', '.join(
        f'{path}={descriptor}' for path, descriptor in list(shape.items())[:18])


def _render_analysis(analysis: dict, payload: dict) -> str:
    lines = [
        'Browser research report',
        f'Requested URL: {redact_url(payload.get("requestedUrl") or "")}',
        f'Final URL: {redact_url(payload.get("url") or "")}',
        f'Title: {payload.get("title") or ""}',
        f'Strategy: {analysis["strategy"]} — {analysis["reason"]}',
        f'Next step: {analysis["recommended_next_step"]}',
    ]
    stats = payload.get('research') if isinstance(payload.get('research'), dict) else {}
    lines.append(
        'Traversal: '
        f'{int(stats.get("pagesVisited") or 1)} page(s), '
        f'{int(stats.get("scrollsCompleted") or 0)} scroll(s), '
        f'stop={stats.get("stopReason") or "complete"}'
    )
    anti_bot = analysis['anti_bot']
    lines.append(
        f'Anti-bot: {anti_bot["vendor"]}' if anti_bot['detected']
        else 'Anti-bot: no known signature observed')
    capture = analysis['network']['capture']
    lines.append(
        'Network capture: '
        f'{capture["response_count"]} response(s), '
        f'{capture["websocket_frames"]} WebSocket frame(s), '
        f'{capture["dropped_entries"]} dropped metadata row(s), '
        f'{capture["dropped_bodies"]} dropped body/bodies')
    candidates = analysis['network']['candidates']
    if candidates:
        lines.append('API candidates:')
        for row in candidates:
            lines.append(
                f'- {row["verdict"]} {row["key"]} '
                f'[{row["status"]} {row["content_type"]}] '
                f'score={row["real_data_score"]:.2f} '
                f'observations={row["observations"]}')
            lines.append(f'  shape: {_render_shape(row["shape"])}')
    return '\n'.join(lines)


def _render_initial_state(payload: dict) -> str:
    previews = payload.get('initialStatePayloads')
    if not isinstance(previews, dict):
        return ''
    parts = []
    used = 0
    for name, raw in list(previews.items())[:8]:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = raw
        serialized = json.dumps(
            redact_value(value), ensure_ascii=False, indent=2, default=str)
        header = f'[{name}]\n'
        remaining = _MAX_INITIAL_STATE_CHARS - used - len(header)
        if remaining <= 64:
            break
        serialized = serialized[:remaining]
        parts.append(header + serialized)
        used += len(header) + len(serialized)
    return '\n\n'.join(parts)


def render_research_payload(
    payload: dict,
    *,
    owner_user_id: str,
    mode: str = 'both',
    max_chars: int = 60_000,
    analysis: dict | None = None,
    prior_observation: dict | None = None,
    current_observation: dict | None = None,
) -> str:
    """Render one already-authorized deep-capture result for model context."""
    budget = _bounded_int(
        max_chars, default=60_000, minimum=1_000, maximum=_MAX_CONTEXT_CHARS)
    analysis = analysis or analyze_research_payload(
        payload, owner_user_id=owner_user_id)
    analysis_text = _render_analysis(analysis, payload)
    if mode != 'content' and prior_observation:
        from .site_observations import (
            render_adapter_promotion, render_site_observation,
        )

        prior_text = render_site_observation(prior_observation)
        if prior_text:
            analysis_text = prior_text + '\n\n' + analysis_text
        promotion_text = render_adapter_promotion(current_observation)
        if promotion_text:
            analysis_text += '\n\n' + promotion_text
    if mode == 'analysis':
        return analysis_text[:budget]
    page_text = str(
        payload.get('collectedText') or payload.get('text') or '').strip()
    initial_text = _render_initial_state(payload)
    if initial_text:
        page_text = (
            (page_text + '\n\n--- Captured initial page state ---\n' + initial_text)
            if page_text else initial_text)
    network_text = render_network_evidence(
        payload, owner_user_id=owner_user_id, max_chars=budget)
    content = merge_page_and_network(
        page_text, network_text, max_chars=budget)
    if mode == 'content':
        return content or analysis_text[:budget]
    separator = '\n\n--- Collected content ---\n'
    remaining = max(0, budget - len(analysis_text) - len(separator))
    if not content or remaining <= 0:
        return analysis_text[:budget]
    return (analysis_text + separator + content[:remaining])[:budget]


def research_page(fn_args: dict, runtime) -> str:
    """Execute the extension's bounded background research command."""
    url = str(fn_args.get('url') or '').strip()
    if not url:
        return 'Error: url is required.'
    mode = str(fn_args.get('mode') or 'both').strip().lower()
    if mode not in ('both', 'analysis', 'content'):
        return 'Error: mode must be both, analysis, or content.'
    pagination = str(fn_args.get('pagination') or 'auto').strip().lower()
    if pagination not in ('auto', 'links', 'none'):
        return 'Error: pagination must be auto, links, or none.'
    try:
        from .access import is_read_allowed
        if not is_read_allowed(runtime.owner_user_id, url):
            return 'Error: browser research URL was denied by domain policy.'
    except Exception as exc:
        logger.debug('[BrowserResearch] requested URL policy failed: %s', exc)
        return 'Error: browser research URL was denied by domain policy.'
    from .site_observations import load_site_observation
    prior_observation = load_site_observation(runtime.owner_user_id, url)
    max_chars = _bounded_int(
        fn_args.get('maxChars'), default=60_000,
        minimum=1_000, maximum=_MAX_CONTEXT_CHARS)
    max_scrolls = _bounded_int(
        fn_args.get('maxScrolls'), default=4, minimum=0, maximum=8)
    max_pages = _bounded_int(
        fn_args.get('maxPages'), default=3, minimum=1, maximum=5)
    try:
        from .protocol import (
            BrowserCapability,
            BrowserUpgradeRequired,
            require_capabilities,
        )
        protocol_info = require_capabilities(runtime.client_id, (
            BrowserCapability.DEEP_COLLECT,
            BrowserCapability.NETWORK_BODY,
        ))
    except BrowserUpgradeRequired as exc:
        return (
            'Error: browser extension upgrade required for deep website '
            f'research; missing capabilities: {", ".join(exc.missing)}')
    command_params = {
        'url': url,
        'maxChars': max_chars,
        'maxScrolls': max_scrolls,
        'maxPages': max_pages,
        'pagination': pagination,
        'timeoutMs': 65_000,
    }
    available_capabilities = set(
        protocol_info.get('capabilities') or []) \
        if isinstance(protocol_info, dict) else set()
    if (isinstance(prior_observation, dict)
            and prior_observation.get('status') == 'active'
            # Three consistent successful observations are required before a
            # stale hint may reserve any of the bounded transient body budget.
            and int(prior_observation.get('confidence_milli') or 0) >= 700
            and BrowserCapability.RESEARCH_HINTS.value in available_capabilities):
        wire_hints = []
        for hint in (prior_observation.get('api_hints') or [])[:5]:
            if not isinstance(hint, dict) or hint.get('passive_only') is not True:
                continue
            wire_hints.append({
                'method': hint.get('method'),
                'origin': hint.get('origin'),
                'pathTemplate': hint.get('path_template'),
            })
        if wire_hints:
            command_params['captureHints'] = wire_hints
    result, error = runtime.send('research_url', command_params, timeout=80)
    if error:
        return f'Error researching URL: {error}'
    if not isinstance(result, dict):
        return 'Error: browser returned an invalid research result.'
    final_url = str(result.get('url') or '')
    try:
        from .access import is_read_allowed
        if not final_url or not is_read_allowed(
                runtime.owner_user_id, final_url):
            return 'Error: browser research result was denied by domain policy.'
    except Exception as exc:
        logger.debug('[BrowserResearch] final URL policy failed: %s', exc)
        return 'Error: browser research result was denied by domain policy.'
    try:
        from .cookie_capture import looks_like_login_wall
        if looks_like_login_wall(
                url, final_url, str(result.get('title') or '')):
            from .site_observations import record_site_observation

            record_site_observation(
                runtime.owner_user_id, url, outcome='auth_challenge')
            return (
                'Error: the page redirected to a sign-in screen. Finish '
                f'signing in at {final_url}, then retry this research call.')
    except Exception as exc:
        logger.debug('[BrowserResearch] login-wall classification degraded: %s', exc)
    analysis = analyze_research_payload(
        result, owner_user_id=runtime.owner_user_id)
    from .site_observations import (
        distill_site_observation,
        record_site_observation,
    )
    research_stats = result.get('research')
    elapsed_ms = _bounded_int(
        research_stats.get('elapsedMs') if isinstance(research_stats, dict) else 0,
        default=0, minimum=0, maximum=120_000)
    try:
        observation = distill_site_observation(analysis, elapsed_ms=elapsed_ms)
        current_observation = record_site_observation(
            runtime.owner_user_id, url, observation=observation,
            outcome='success')
    except Exception as exc:
        # Site observations are reconstructible advisory state. A malformed
        # extension payload or unavailable cache must not hide live page data.
        logger.debug('[BrowserResearch] site observation distillation skipped: %s', exc)
        current_observation = None
    return render_research_payload(
        result, owner_user_id=runtime.owner_user_id,
        mode=mode, max_chars=max_chars, analysis=analysis,
        prior_observation=prior_observation,
        current_observation=current_observation)


__all__ = [
    'analyze_research_payload', 'render_research_payload', 'research_page',
]
