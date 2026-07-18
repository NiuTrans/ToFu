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

from flask import Blueprint

from lib.api_response import api_bad_request, api_ok
from lib.cost import compute_cost
from lib.log import get_logger
from lib.log_clean import detect_log_noise
from lib.openapi import api_meta
from lib.request_parser import BadRequest, optional_dict, optional_str, parse_body, require_list, require_str
from lib.text_lang import (
    cjk_ratio, detect_language, guess_language, is_predominantly_chinese,
    latin_ratio,
)
from lib.tool_changes import extract_file_changes_dicts

from .auth import require_scope

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


__all__ = ['api_v1_logs_bp']
