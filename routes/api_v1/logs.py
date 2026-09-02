"""routes/api_v1/logs.py — Log-noise endpoints + tool-change extraction.

  POST /api/v1/logs/clean
    body: {"text": "..."}
    returns: ``CleaningResult`` dict, or ``{ok:true, no_noise:true}``
             when nothing actionable is detected.

  POST /api/v1/messages/extract-file-changes
    body: {"toolRounds": [...]}
    returns: list of {path, action, ok, count, pending, root}

Both routes are thin facades over ``lib/log_clean.py`` and
``lib/tool_changes.py`` so the UI, SDKs, and CI pipelines all see
exactly the same heuristic / extraction logic.
"""

from __future__ import annotations

import logging
import os

from quart import Blueprint

from lib.api_response import api_bad_request, api_ok
from lib.cost import compute_cost
from lib.log import LOG_DIR, get_logger
from lib.log_clean import detect_log_noise
from lib.openapi import api_meta
from lib.request_parser import BadRequest, optional_dict, optional_str, parse_body, require_list, require_str
from lib.text_lang import (
    cjk_ratio, detect_language, guess_language, is_predominantly_chinese,
    latin_ratio,
)
from lib.tool_changes import extract_file_changes_dicts

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_logs_bp = Blueprint('api_v1_logs', __name__)


@api_v1_logs_bp.route('/api/v1/logs/clean', methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Detect log noise and return a cleaning report',
    description=(
        'Pure-function analysis of a log blob. Identifies and proposes '
        'removal of: per-line log prefixes, HTTP access lines, pointer '
        'underlines (^^^), long absolute paths, tqdm progress bars, '
        'duplicated worker tracebacks, repeated similar lines, and '
        'consecutive blank lines.\n\n'
        'Returns ``{ok:true, no_noise:true}`` when savings would be '
        '< 8% or < 80 chars (mirrors the UI banner threshold).'),
    tags=['logs'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['text'],
            'properties': {
                'text': {'type': 'string',
                          'description': 'Raw log text. Up to ~2 MB.'},
            },
        },
    }}},
    responses={
        '200': {'description': 'OK',
                 'content': {'application/json': {
                     'schema': {
                         'oneOf': [
                             {'type': 'object',
                              'properties': {
                                  'ok': {'type': 'boolean'},
                                  'no_noise': {'type': 'boolean'},
                              }},
                             {'type': 'object',
                              'properties': {
                                  'ok': {'type': 'boolean'},
                                  'cleanedText': {'type': 'string'},
                                  'savedChars': {'type': 'integer'},
                                  'savedPct': {'type': 'integer'},
                                  'ops': {'type': 'array',
                                           'items': {'type': 'object'}},
                              }},
                         ],
                     },
                 }}},
    },
)
def logs_clean():
    body = parse_body()
    try:
        text = require_str(body, 'text', max_len=2_000_000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'text')
    result = detect_log_noise(text)
    if result is None:
        return api_ok(no_noise=True)
    return api_ok(result.to_dict())


@api_v1_logs_bp.route('/api/v1/messages/extract-file-changes',
                       methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Extract file-change list from a tool-rounds blob',
    description=(
        'Given a list of tool rounds (the same shape the UI sees in '
        '``msg.toolRounds``), return a deduplicated file-change '
        'summary: ``[{path, action, ok, count, pending, root}, ...]``.\n\n'
        'This is the same derivation the UI uses for its file-changes '
        'bar when the orchestrator has not yet emitted a '
        'git-history-based ``modifiedFileList`` (mid-stream, or when '
        'project tracking is off). Exposing it ensures every caller — '
        'UI, SDK, CI — sees identical results.'),
    tags=['logs'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['toolRounds'],
            'properties': {
                'toolRounds': {'type': 'array',
                                'items': {'type': 'object'}},
            },
        },
    }}},
)
def extract_file_changes_route():
    body = parse_body()
    try:
        rounds = require_list(body, 'toolRounds', max_len=10000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'toolRounds')
    return api_ok(files=extract_file_changes_dicts(rounds))


@api_v1_logs_bp.route('/api/v1/messages/extract-file-changes/batch',
                       methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Batch extract file-change lists for many messages',
    description=(
        'Batch variant of `/api/v1/messages/extract-file-changes`. Pass '
        '`items: [{toolRounds: [...]}, ...]` and receive `results: [...]` '
        'aligned by index, where each entry is the same `[{path, action, '
        'ok, count, pending, root}, ...]` array the single-message route '
        'returns. Used by the UI to seed the file-changes-bar cache for a '
        'whole conversation in one round-trip on `renderChat()`, instead '
        'of firing one POST per message.'),
    tags=['logs'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['items'],
            'properties': {
                'items': {
                    'type': 'array',
                    'maxItems': 1000,
                    'items': {
                        'type': 'object',
                        'required': ['toolRounds'],
                        'properties': {
                            'toolRounds': {'type': 'array',
                                            'items': {'type': 'object'}},
                        },
                    },
                },
            },
        },
    }}},
)
def extract_file_changes_batch_route():
    body = parse_body()
    try:
        items = require_list(body, 'items', max_len=1000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'items')
    results = []
    for item in items:
        if not isinstance(item, dict):
            results.append([])
            continue
        rounds = item.get('toolRounds') or []
        if not isinstance(rounds, list):
            results.append([])
            continue
        try:
            results.append(extract_file_changes_dicts(rounds))
        except Exception as e:
            logger.warning('[FileChanges] batch item failed: %s', e)
            results.append([])
    return api_ok(results=results)


@api_v1_logs_bp.route('/api/v1/text/detect-language', methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Detect predominant language of a text blob',
    description=(
        'Cascade language detection: Tier-0 script fast-path → Tier-1 '
        'fastText lid.176 (guarded-optional, ``TOFU_LANGDETECT_BACKEND='
        'fasttext``) → heuristic fallback. Returns the legacy coarse '
        '``{language, cjk_ratio, latin_ratio, is_chinese}`` (unchanged '
        'contract) PLUS a richer ``detected: {code, confidence, source}`` '
        'from the cascade.\n\n'
        '``language`` is one of ``zh / en / mixed / unknown``; '
        '``detected.code`` is a full BCP-47-ish code (``en / de / es / '
        'ja / …``). The LLM-correction tier is never fired from this '
        'endpoint (it is gated per-request via personal_scope elsewhere).'),
    tags=['logs'], scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['text'],
            'properties': {'text': {'type': 'string'}},
        },
    }}},
)
def detect_text_language():
    body = parse_body()
    try:
        text = require_str(body, 'text', max_len=2_000_000,
                            allow_empty=True)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'text')
    # ``forceFasttext`` forces Tier-1 fastText on regardless of the env backend
    # — for the frontend auto-translate skip gate, which (like the server-side
    # safety net) MUST tell kanji-heavy Japanese apart from Chinese; the
    # script+heuristic tier cannot, so without this a JP reply is wrongly
    # skipped as "already Chinese". ``detected.source`` lets the caller/test
    # verify the statistical model actually ran.
    force_ft = bool(body.get('forceFasttext') or body.get('force_fasttext'))
    # Cascade detection (Tier-0/1 only — never the billed LLM tier from an
    # unauthenticated detection endpoint).
    det = detect_language(text, force_fasttext=force_ft)
    return api_ok({
        'language': guess_language(text),
        'cjk_ratio': round(cjk_ratio(text), 4),
        'latin_ratio': round(latin_ratio(text), 4),
        'is_chinese': is_predominantly_chinese(text),
        'detected': {
            'code': det.code,
            'confidence': round(det.confidence, 4),
            'source': det.source,
        },
    })


@api_v1_logs_bp.route('/api/v1/messages/cost', methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Compute USD + CNY cost from a usage dict',
    description=(
        'Centralised pricing-policy port of the JS `calcCostCny` '
        'helper. Handles Anthropic-vs-OpenAI cache-token convention '
        'detection, Qwen tiered CNY pricing, and provider-scoped '
        'pricing overrides. Returns the same fields the UI '
        'finish-info bar displays so SDK callers can render identical '
        'cost summaries.\n\n'
        'Returns ``{ok:true, no_charge:true}`` when the usage is '
        'empty / all zeros (matches the JS function returning ``null``).'),
    tags=['logs'], scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['usage'],
            'properties': {
                'usage': {'type': 'object'},
                'model': {'type': 'string'},
                'provider_id': {'type': 'string'},
            },
        },
    }}},
)
def message_cost():
    body = parse_body()
    usage = optional_dict(body, 'usage', default={}) or {}
    model = optional_str(body, 'model', default='', max_len=200) or ''
    provider_id = (optional_str(body, 'provider_id', default='', max_len=80)
                    or optional_str(body, 'providerId', default='', max_len=80)
                    or None)
    result = compute_cost(usage, model_id=model, provider_id=provider_id)
    if result is None:
        return api_ok(no_charge=True)
    return api_ok(result)


@api_v1_logs_bp.route('/api/v1/messages/cost/batch', methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Compute cost for many usages in one round-trip',
    description=(
        'Batch variant of `/api/v1/messages/cost` for whole-conversation '
        'aggregation paths. Pass `items: [{usage, model?, provider_id?}, ...]` '
        'and receive `costs: [...]` aligned by index. Each entry is the '
        'same shape `compute_cost` returns; entries with no charge are '
        '`null` in the returned array.\n\n'
        'The UI uses this for `calcConversationCost` (per-conversation '
        'cost rollup) so the JS doesn\'t have to re-implement pricing '
        'policy. SDK callers building cost dashboards over the message '
        'log get the same answer.'),
    tags=['logs'], scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['items'],
            'properties': {
                'items': {
                    'type': 'array',
                    'maxItems': 5000,
                    'items': {
                        'type': 'object',
                        'required': ['usage'],
                        'properties': {
                            'usage': {'type': 'object'},
                            'model': {'type': 'string'},
                            'provider_id': {'type': 'string'},
                        },
                    },
                },
            },
        },
    }}},
)
def message_cost_batch():
    body = parse_body()
    try:
        items = require_list(body, 'items', max_len=5000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'items')
    costs = []
    for item in items:
        if not isinstance(item, dict):
            costs.append(None)
            continue
        usage = item.get('usage') or {}
        model = item.get('model') or ''
        provider = item.get('provider_id') or item.get('providerId') or None
        try:
            costs.append(compute_cost(
                usage if isinstance(usage, dict) else {},
                model_id=str(model) if model else '',
                provider_id=str(provider) if provider else None))
        except Exception as e:
            logger.warning('[Cost] batch item failed: %s', e)
            costs.append(None)
    return api_ok(costs=costs)


@api_v1_logs_bp.route('/api/v1/logs/client', methods=['POST'])
@require_scope('chat')
@api_meta(
    summary='Relay browser console lines into logs/frontend.log',
    description=(
        'Batch sink for the client-side log relay '
        '(static/js/core/client_log_relay.js): the browser patches '
        'console.{log,info,warn,error} into a bounded ring buffer and '
        'POSTs it here every 15 s (sendBeacon on pagehide), closing the '
        'gap where live-view diagnostics (console.info breadcrumbs) never '
        'reached the server.\n\n'
        'Body: ``{session, url, entries: [{t, lv, msg, n?}]}`` — capped '
        'at 200 entries × 1000 chars; consecutive-duplicate folds arrive '
        'as ``n``. Lines land on the ``frontend`` logger → '
        'logs/frontend.log (daily rotation). Set '
        '``TOFU_CLIENT_LOG_RELAY=0`` to drop everything server-side.'),
    tags=['logs'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['entries'],
            'properties': {
                'session': {'type': 'string'},
                'url': {'type': 'string'},
                'entries': {'type': 'array',
                             'items': {'type': 'object'}},
            },
        },
    }}},
    responses={'200': {'description': 'OK'}},
)
def client_logs_relay():
    if os.environ.get('TOFU_CLIENT_LOG_RELAY', '1').strip().lower() in (
            '0', 'false', 'no', 'off'):
        return api_ok(relayed=0, disabled=True)
    body = parse_body()
    try:
        entries = require_list(body, 'entries')
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'entries')
    # A log SINK must never reject a burst: the browser ring buffer keeps
    # growing while it awaits a 2xx, so a 400 here made the same oversized
    # batch loop forever and every line was lost — exactly the incident
    # diagnostics this relay exists to capture (2026-08-19: recurring 400s
    # while the backend was degraded and the console was noisiest).  Honor
    # the documented 200-entry cap by keeping the NEWEST entries.
    dropped = max(0, len(entries) - 200)
    if dropped:
        entries = entries[-200:]
    session = (optional_str(body, 'session', default='', max_len=64) or '')[:16]
    fe = logging.getLogger('frontend')
    relayed = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        lv = str(e.get('lv') or 'info').lower()
        # A client-supplied line must never forge a log record — collapse
        # CR/LF so one entry is always one physical line.
        msg = str(e.get('msg') or '')[:1000].replace('\r', ' ').replace('\n', ' ⏎ ')
        if not msg:
            continue
        n = e.get('n')
        if isinstance(n, int) and n > 1:
            msg = '%s (×%d)' % (msg, min(n, 100000))
        line = '[client:%s] %s' % (session or '?', msg)
        if lv == 'error':
            fe.error('%s', line)
        elif lv == 'warn':
            fe.warning('%s', line)
        else:
            fe.info('%s', line)
        relayed += 1
    if dropped:
        fe.warning('[client:%s] relay batch truncated: dropped %d older '
                   'entr%s over the 200-entry cap',
                   session or '?', dropped, 'y' if dropped == 1 else 'ies')
    return api_ok(relayed=relayed, dropped=dropped)


@api_v1_logs_bp.route('/api/v1/logs/diagnostics', methods=['GET'])
@require_scope('admin')
@api_meta(
    summary='Storage-independent, model-sized incident diagnosis',
    description=(
        'Reads the bounded incident JSONL index (or a bounded legacy '
        'error.log tail) without contacting SQLite, PostgreSQL, or the '
        'storage sidecar. Returns ranked fingerprints, true coalesced '
        'occurrence counts, correlation ids, retention health and short '
        'redacted samples under a strict output-byte budget. Admin-only '
        'because diagnostics can span users and contain operational '
        'metadata. Query filters: request_id, conversation_id, task_id, '
        'trace_id; window_hours (0.05..720), max_items (1..100), '
        'max_bytes (4096..131072).'),
    tags=['logs'],
    scope='admin',
)
def log_diagnostics_view():
    from quart import request

    from lib.log_diagnostics import diagnose_logs
    from lib.runtime_paths import data_root

    try:
        window_hours = float(request.args.get('window_hours') or 24)
        max_items = int(request.args.get('max_items') or 20)
        max_bytes = int(request.args.get('max_bytes') or 32 * 1024)
    except (TypeError, ValueError):
        return api_bad_request('invalid numeric parameter')
    if not (0.05 <= window_hours <= 720):
        return api_bad_request('invalid window_hours (0.05..720)',
                               field='window_hours')
    if not (1 <= max_items <= 100):
        return api_bad_request('invalid max_items (1..100)', field='max_items')
    if not (4 * 1024 <= max_bytes <= 128 * 1024):
        return api_bad_request('invalid max_bytes (4096..131072)',
                               field='max_bytes')
    selectors = {
        key: (request.args.get(key) or '').strip()[:128]
        for key in ('request_id', 'conversation_id', 'task_id', 'trace_id')
    }
    ctx = current_auth()
    result = diagnose_logs(
        LOG_DIR,
        data_dir=data_root(),
        window_hours=window_hours,
        max_items=max_items,
        max_output_bytes=max_bytes,
        requesting_user_id=str(getattr(ctx, 'owner_user_id', '') or ''),
        # This endpoint is admin-only. Keep the ownership decision explicit so
        # a future per-user diagnostic route cannot inherit a global shortcut.
        include_all_users=True,
        **selectors,
    )
    return api_ok(result)


@api_v1_logs_bp.route('/api/v1/logs/aggregates', methods=['GET'])
@require_scope('chat')
@api_meta(
    summary='error.log fingerprint rollup, sorted by frequency',
    description=(
        'Read-only view over the ``log_aggregates`` table (layer ③ of the '
        'error.log dedup design, ). Each row is one '
        '``(level, logger, message-template, exc-signature)`` fingerprint '
        'with ``count`` / ``first_seen`` / ``last_seen`` (epoch-ms + ISO) '
        'and one recent ``sample`` — the text logs stay the source of '
        'truth; this table only answers "which warnings/errors are '
        'spamming, and since when".\n\n'
        'Query params: ``level`` (DEBUG|INFO|WARNING|ERROR|CRITICAL), '
        '``sort`` (count|last_seen|level, default count), ``limit`` '
        '(1..500, default 100), ``q`` (substring on template).'),
    tags=['logs'],
    scope='chat',
)
def log_aggregates_view():
    from quart import request

    from lib.log_aggregates import query_aggregates

    level = (request.args.get('level') or '').strip().upper()
    if level and level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR',
                               'CRITICAL'):
        return api_bad_request('invalid level', field='level')
    sort = (request.args.get('sort') or 'count').strip()
    if sort not in ('count', 'last_seen', 'level'):
        return api_bad_request('invalid sort (count|last_seen|level)',
                               field='sort')
    try:
        limit = int(request.args.get('limit') or 100)
    except (TypeError, ValueError):
        return api_bad_request('invalid limit', field='limit')
    limit = max(1, min(limit, 500))
    q = (request.args.get('q') or '')[:100]
    try:
        result = query_aggregates(level=level, sort=sort, limit=limit, q=q)
    except Exception as e:
        # 聚合层是只读旁路:表可能尚未创建(老库未重启/新装首启前),绝不
        # 因此 500 掉日志页面——返回空表并显式标记,比报错更可诊断。
        logger.warning('[Logs.aggregates] query failed (returning empty): %s', e)
        return api_ok(items=[], total_rows=0, total_events=0,
                      unavailable=True)
    return api_ok(result)


@api_v1_logs_bp.route('/api/v1/logs/digest', methods=['GET'])
@require_scope('chat')
@api_meta(
    summary='LLM-facing differential log digest (NEW/ESCALATING/RECURRING/RESOLVED)',
    description=(
        'The bounded, LLM-consumable answer to "what needs fixing right '
        'now" (layer ③ of the log-signal design, lib/log_signals.py). '
        'Built on the log_aggregates fingerprint table plus an hourly '
        'count baseline (data/config/log_digest_baseline.json): each '
        'fingerprint is labelled NEW (first seen inside ``window_hours``), '
        'ESCALATING (recent rate > escalate_factor × lifetime rate), '
        'RECURRING (still firing) or RESOLVED (silent for 24h). Every '
        'entry carries ``rid``/``grep_hint`` pointers back into the raw '
        'text logs, which remain the source of truth. Without a baseline '
        'snapshot yet, ESCALATING is honestly never reported (cold start).\n\n'
        'Query params: ``window_hours`` (1..168, default 24), '
        '``max_items`` (1..100 per section, default 20), '
        '``escalate_factor`` (default 3.0).'),
    tags=['logs'],
    scope='chat',
)
def log_digest_view():
    from quart import request

    from lib.log_signals import compute_digest

    try:
        window_hours = float(request.args.get('window_hours') or 24)
        max_items = int(request.args.get('max_items') or 20)
        escalate_factor = float(request.args.get('escalate_factor') or 3.0)
    except (TypeError, ValueError):
        return api_bad_request('invalid numeric parameter')
    if not (1 <= window_hours <= 168):
        return api_bad_request('invalid window_hours (1..168)',
                               field='window_hours')
    if not (1 <= max_items <= 100):
        return api_bad_request('invalid max_items (1..100)',
                               field='max_items')
    if not (1.5 <= escalate_factor <= 100):
        return api_bad_request('invalid escalate_factor (1.5..100)',
                               field='escalate_factor')
    try:
        result = compute_digest(
            window_hours=window_hours, max_items=max_items,
            escalate_factor=escalate_factor)
    except Exception as e:
        # Storage failure is precisely when operators need diagnostics most.
        # Fall back to the file-only incident index instead of returning an
        # empty dashboard whose root cause is impossible to inspect.
        logger.warning('[Logs.digest] aggregate store unavailable; using '
                       'incident journal fallback: %s', e)
        try:
            from lib.log_diagnostics import diagnose_logs
            from lib.runtime_paths import data_root

            ctx = current_auth()
            fallback = diagnose_logs(
                LOG_DIR, data_dir=data_root(), window_hours=window_hours,
                max_items=max_items, max_output_bytes=32 * 1024,
                requesting_user_id=str(getattr(ctx, 'owner_user_id', '') or ''),
                include_all_users=bool(
                    ctx is not None and ctx.has_scope('admin')))
            return api_ok(
                new=[], escalating=[],
                recurring_top=fallback.get('incidents') or [], resolved=[],
                summary=fallback.get('summary') or {}, unavailable=True,
                fallback='incident_journal', diagnostics=fallback)
        except Exception as fallback_error:
            logger.warning('[Logs.digest] incident fallback failed: %s',
                           fallback_error)
            return api_ok(new=[], escalating=[], recurring_top=[], resolved=[],
                          summary={}, unavailable=True,
                          fallback='unavailable')
    return api_ok(result)

__all__ = ['api_v1_logs_bp']
