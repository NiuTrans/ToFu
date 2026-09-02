"""routes/api_v1/agents.py — Higher-level agent endpoints.

Exposes paper-report, translate, memory, browser-fetch, image-gen, search,
and swarm under a uniform v1 shape with proper scopes. All actual logic
lives in the existing modules — these routes are stable façades so
external callers don't depend on legacy paths.

Streaming / polling discoverability
-----------------------------------
Every long-running agent task exposes THREE poll surfaces under a
uniform v1 path so SDK callers can pick whichever fits their workflow:

  * Cursor-based event replay (low-latency long-poll):
      GET /api/v1/tasks/{task_id}/events?cursor=N

  * Server-Sent Events stream (push):
      GET /api/v1/tasks/{task_id}/stream?cursor=N

  * Feature-shaped poll (flat-result format the UI uses):
      GET  /api/v1/agents/translate/poll/{task_id}
      POST /api/v1/agents/translate/poll/batch   (façade; body {taskIds:[…]})
      GET  /api/v1/agents/paper/report/poll?task_id=…&cursor=…
      GET  /api/v1/agents/paper/translate/poll?task_id=…&cursor=…

  The bare ``/api/v1/translate/*`` routes (``…/poll/<id>`` and
  ``…/poll-batch``) are the underlying implementation these façades
  delegate to; new SDK callers should prefer the ``/agents/translate/*``
  paths above.

The cursor / SSE routes work for ANY TaskRuntime-backed task uniformly
(see ``routes/api_v1/tasks.py``). The feature-shaped routes preserve
the structured response format the UI needs (translate returns
``{status, translated, partial, statusMessage, statusKind}``; paper
returns ``{events, next_cursor, status, …feature-specific}``).
"""

from __future__ import annotations

import time

from quart import Blueprint

from lib.api_response import api_bad_request, api_internal_error, api_ok, safe_route
from lib.log import get_logger, log_context
from lib.openapi import api_meta
from lib.request_parser import (
    BadRequest, optional_bool, optional_int, optional_list, optional_str,
    parse_body, require_str,
)
from lib.agent_core.task_runtime import TaskRuntime

from .auth import request_user_id, require_scope

logger = get_logger(__name__)

api_v1_agents_bp = Blueprint('api_v1_agents', __name__)


# ── Search task runtime (async variant) ────────────────────────────
# Push channel 'search' is wired up so /api/push subscribers see events
# without a per-feature WebSocket.
_search_runtime = TaskRuntime('search', ttl=1800, push_channel='search',
                              error_source='api_v1.agents.search')


# ── Paper ───────────────────────────────────────────────────────────

@api_v1_agents_bp.route('/api/v1/agents/paper/report', methods=['POST'])
@require_scope('agents:paper')
@api_meta(summary='Start a paper-report task', tags=['agents'],
          scope='agents:paper')
def paper_report_start():
    try:
        from routes.paper_pkg._report import start_report_task
    except ImportError as e:
        return api_internal_error(e, context='Paper module unavailable',
                                  source='api_v1.agents.paper_report_start')
    # Mark this as a HEADLESS/BYO entry so the shared report handler stamps the
    # fail-closed personal-scope default (no operator library/memories spliced
    # into the insight pass) unless the caller explicitly opts in. The
    # interactive route reaches start_report_task WITHOUT this flag → owner
    # keeps the personal transfer moat.
    from quart import g
    g.paper_report_headless = True
    return start_report_task()


@api_v1_agents_bp.route('/api/v1/agents/paper/translate', methods=['POST'])
@require_scope('agents:paper')
@api_meta(summary='Start a paper-translate task', tags=['agents'],
          scope='agents:paper')
def paper_translate_start():
    try:
        from routes.paper_pkg._qa_translate import start_translate_task
    except ImportError as e:
        return api_internal_error(e, context='Paper module unavailable',
                                  source='api_v1.agents.paper_translate_start')
    return start_translate_task()


# ── Translate ──────────────────────────────────────────────────────

@api_v1_agents_bp.route('/api/v1/agents/translate', methods=['POST'])
@require_scope('agents:translate')
@api_meta(summary='Start a translation task', tags=['agents'],
          scope='agents:translate',
          request_body={'required': True, 'content': {'application/json': {
              'schema': {'type': 'object',
                          'required': ['text'],
                          'properties': {
                              'text': {'type': 'string'},
                              'source_lang': {'type': 'string'},
                              'target_lang': {'type': 'string',
                                               'default': 'English'},
                              'model': {'type': 'string'},
                          }}}}})
def translate_start():
    body = parse_body()
    try:
        require_str(body, 'text')
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'text')
    try:
        from routes.api_v1.translate import translate_start_v1 as _ts
    except ImportError as e:
        return api_internal_error(e, context='Translate module unavailable',
                                  source='api_v1.agents.translate_start')
    return _ts()


@api_v1_agents_bp.route('/api/v1/agents/translate/poll/<task_id>',
                         methods=['GET'])
@require_scope('agents:translate')
@api_meta(
    summary='Poll a translation task (flat-result format)',
    description=(
        'Stable v1 path for the translate-shaped poll response. '
        'Returns ``{status, translated?, partial?, statusMessage?, '
        'statusKind?, error?}`` — the structured shape the UI uses for '
        'its bilingual rendering. For the generic event-replay path '
        'use ``GET /api/v1/tasks/{task_id}/events`` (cursor) or '
        '``/api/v1/tasks/{task_id}/stream`` (SSE).'),
    tags=['agents'], scope='agents:translate',
)
def translate_poll_v1(task_id):
    try:
        from routes.api_v1.translate import translate_poll_v1 as _tp
    except ImportError as e:
        return api_internal_error(e, context='Translate module unavailable',
                                  source='api_v1.agents.translate_poll_v1')
    return _tp(task_id)


@api_v1_agents_bp.route('/api/v1/agents/translate/poll/batch',
                         methods=['POST'])
@require_scope('agents:translate')
@api_meta(
    summary='Batch-poll multiple translation tasks',
    description='Same flat-result shape as `/poll/{task_id}`, '
                 'returned as a list aligned with the input `taskIds`.',
    tags=['agents'], scope='agents:translate',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['taskIds'],
            'properties': {
                'taskIds': {'type': 'array',
                             'items': {'type': 'string'},
                             'maxItems': 200},
            },
        },
    }}},
)
def translate_poll_batch_v1():
    try:
        from routes.api_v1.translate import translate_poll_batch_v1 as _tpb
    except ImportError as e:
        return api_internal_error(e, context='Translate module unavailable',
                                  source='api_v1.agents.translate_poll_batch_v1')
    return _tpb()


# ── Paper poll façades ────────────────────────────────────────────

@api_v1_agents_bp.route('/api/v1/agents/paper/report/poll', methods=['GET'])
@require_scope('agents:paper')
@api_meta(
    summary='Poll a paper-report task (cursor-based event replay)',
    description=(
        'Returns ``{ok, status, events: [...], next_cursor, ...}`` — '
        'the same shape ``/api/paper/report/poll`` produces. Use '
        '``/api/v1/tasks/{task_id}/stream`` for the SSE variant.'),
    tags=['agents'], scope='agents:paper',
    parameters=[
        {'name': 'task_id', 'in': 'query', 'required': True,
         'schema': {'type': 'string'}},
        {'name': 'cursor', 'in': 'query',
         'schema': {'type': 'integer', 'default': 0}},
    ],
)
def paper_report_poll_v1():
    try:
        from routes.paper_pkg._report import poll_report_task
    except ImportError as e:
        return api_internal_error(e, context='Paper module unavailable',
                                  source='api_v1.agents.paper_report_poll_v1')
    return poll_report_task()


@api_v1_agents_bp.route('/api/v1/agents/paper/translate/poll',
                         methods=['GET'])
@require_scope('agents:paper')
@api_meta(
    summary='Poll a paper-translate task (cursor-based event replay)',
    tags=['agents'], scope='agents:paper',
    parameters=[
        {'name': 'task_id', 'in': 'query', 'required': True,
         'schema': {'type': 'string'}},
        {'name': 'cursor', 'in': 'query',
         'schema': {'type': 'integer', 'default': 0}},
    ],
)
def paper_translate_poll_v1():
    try:
        from routes.paper_pkg._qa_translate import poll_translate_task
    except ImportError as e:
        return api_internal_error(e, context='Paper module unavailable',
                                  source='api_v1.agents.paper_translate_poll_v1')
    return poll_translate_task()


# ── Memory ──────────────────────────────────────────────────────────

@api_v1_agents_bp.route('/api/v1/agents/memory/search', methods=['POST'])
@require_scope('agents:memory')
@api_meta(summary='Search memories', tags=['agents'],
          scope='agents:memory')
@safe_route
def memory_search():
    """Search memories.

    @safe_route wraps the FINAL search_memories(...) call — the ad-hoc
    ``except Exception as e: logger.exception + api_internal_error(e)``
    was pure (no distinct context, no side effects) so the decorator
    reproduces its behaviour with the correct qualname source. The
    other two try/except blocks in this function are DELIBERATELY
    RETAINED because they are NOT the same pattern:
      * ``except ImportError as e:`` (module-availability check) →
        distinct 'Memory unavailable' context that must survive.
      * ``except (ValueError, TypeError)`` for ``int(top_k)`` → local
        recovery-with-default, not a 500 escape hatch.
    """
    body = parse_body()
    query = optional_str(body, 'query', default='', max_len=500)
    if not query:
        return api_bad_request('query is required', field='query')
    try:
        from lib.memory.relevance import search_memories
    except ImportError as e:
        return api_internal_error(e, context='Memory unavailable',
                                  source='api_v1.agents.memory_search')
    try:
        top_k = int(body.get('top_k') or 30)
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[agents] memory_search caught %s: %s', type(_e_audit).__name__, _e_audit)
        top_k = 30
    results = search_memories(query, top_k=top_k)
    return api_ok(results=results, count=len(results))


# ── Search ─────────────────────────────────────────────────────────

# Engine tags accepted by the orchestrator. Surfaced here so OpenAPI
# can advertise them; kept narrow because the engine pool churns.
_SEARCH_ENGINES = ['DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG']

_SEARCH_REQUEST_SCHEMA = {
    'type': 'object',
    'required': ['query'],
    'properties': {
        'query': {'type': 'string', 'maxLength': 500,
                  'description': 'Search query.'},
        'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                        'description': 'Number of results to return after rerank.'},
        'freshness': {'type': 'string',
                      'enum': ['', 'day', 'week', 'month', 'year'],
                      'description': 'Time filter applied to engines that support it.'},
        'user_question': {'type': 'string', 'maxLength': 2000,
                          'description': "User's original question (improves LLM relevance filter)."},
        'fetch_pages': {'type': 'boolean', 'default': True,
                        'description': 'Fetch full page bodies. Disable for fast/cheap snippet-only mode (~1-3s).'},
        'filter': {'type': 'boolean', 'default': True,
                   'description': 'Run LLM relevance filter + cleaning on fetched pages.'},
        'rerank': {'type': 'boolean', 'default': True,
                   'description': 'Apply BM25 rerank to fetched results.'},
        'engines': {'type': 'array', 'items': {'type': 'string',
                                                'enum': _SEARCH_ENGINES},
                    'description': 'Optional engine allowlist. Omit ⇒ all enabled.'},
        'max_chars_per_page': {'type': 'integer', 'minimum': 1000, 'maximum': 200000,
                               'description': 'Per-page extracted-text cap.'},
    },
}


def _serialize_search_result(item: dict) -> dict:
    """Translate orchestrator result dict → public JSON shape."""
    full = item.get('full_content') or ''
    return {
        'title': item.get('title') or '',
        'url': item.get('url') or '',
        'source': item.get('source') or '',
        'snippet': item.get('snippet') or '',
        'full_content': full,
        'fetch_failed': not bool(full),
    }


def _run_search(query: str, *, max_results: int, freshness: str,
                user_question: str, fetch_pages: bool, filter_pages: bool,
                rerank: bool, engines, max_chars_per_page) -> dict:
    """Run the orchestrator and return a JSON-serialisable envelope.

    Pure function; no Flask dependencies, so the same body powers both
    the sync route and the async worker.
    """
    from lib.search_runtime import ensure_search_runtime
    perform_web_search = ensure_search_runtime().perform_web_search

    t0 = time.time()
    with log_context('api_v1.search.perform', logger=logger):
        results = perform_web_search(
            query,
            max_results=max_results,
            user_question=user_question,
            freshness=freshness,
            fetch_pages=fetch_pages,
            filter_pages=filter_pages,
            rerank=rerank,
            engines=engines,
            max_chars_per_page=max_chars_per_page,
        )
    elapsed_ms = int((time.time() - t0) * 1000)

    breakdown = getattr(results, '_engine_breakdown', None) or {}
    diag = getattr(results, '_search_diag', None)
    raw_total = sum(len(v) for v in breakdown.values())

    return {
        'query': query,
        'count': len(results),
        'took_ms': elapsed_ms,
        'pipeline': {
            'engines': sorted(breakdown.keys()),
            'raw_results': raw_total,
            'final': len(results),
        },
        'results': [_serialize_search_result(r) for r in results],
        'diagnostics': diag,
    }


def _parse_search_body() -> dict:
    """Extract & validate search params from the request body.

    Raises BadRequest on validation failure (auto-converted to 400 by
    @safe_route or the global handler).
    """
    body = parse_body()
    query = require_str(body, 'query', max_len=500)
    return {
        'query': query,
        'max_results': optional_int(body, 'max_results', default=None,
                                    min=1, max=50),
        'freshness': optional_str(body, 'freshness', default='', max_len=10),
        'user_question': optional_str(body, 'user_question', default='',
                                       max_len=2000),
        'fetch_pages': optional_bool(body, 'fetch_pages', default=True),
        'filter_pages': optional_bool(body, 'filter', default=True),
        'rerank': optional_bool(body, 'rerank', default=True),
        'engines': optional_list(body, 'engines', default=None,
                                 item_type=str, max_len=10) or None,
        'max_chars_per_page': optional_int(body, 'max_chars_per_page',
                                           default=None,
                                           min=1000, max=200000),
    }


@api_v1_agents_bp.route('/api/v1/agents/search', methods=['POST'])
@require_scope('agents:search')
@api_meta(summary='Run a web search (synchronous, full pipeline)',
          description='Multi-engine search with optional page fetch, '
                       'LLM relevance filter, and BM25 rerank. Synchronous '
                       'call — typical latency 10-30 s with defaults; set '
                       '`fetch_pages=false` for sub-second snippet-only mode. '
                       'For long-running calls use POST /agents/search/async '
                       'instead.',
          tags=['agents'], scope='agents:search',
          request_body={'required': True, 'content': {'application/json': {
              'schema': _SEARCH_REQUEST_SCHEMA}}})
def search_run():
    try:
        params = _parse_search_body()
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field)
    logger.info('[api_v1.search] sync query=%r fetch_pages=%s filter=%s rerank=%s',
                params['query'][:100], params['fetch_pages'],
                params['filter_pages'], params['rerank'])
    try:
        result = _run_search(**params)
    except Exception as e:
        logger.exception('[api_v1.search] sync failed')
        return api_internal_error(e, context='api_v1.search',
                                  source='api_v1.agents.search_run')
    return api_ok(result)


@api_v1_agents_bp.route('/api/v1/agents/search/async', methods=['POST'])
@require_scope('agents:search')
@api_meta(summary='Run a web search asynchronously (returns task_id)',
          description='Same parameters as POST /agents/search but returns '
                       'immediately with a `task_id`. Poll '
                       '`GET /api/v1/tasks/{id}/events` or subscribe via the '
                       'unified `/api/push` WebSocket on channel `search`. '
                       'Recommended for callers behind proxies with <30 s timeouts.',
          tags=['agents'], scope='agents:search',
          request_body={'required': True, 'content': {'application/json': {
              'schema': _SEARCH_REQUEST_SCHEMA}}})
def search_run_async():
    try:
        params = _parse_search_body()
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field)
    task = _search_runtime.create(user_id=int(request_user_id()), meta={
        'query': params['query'][:200],
        'fetch_pages': params['fetch_pages'],
        'filter_pages': params['filter_pages'],
        'rerank': params['rerank'],
    })
    task_id = task['id']
    logger.info('[api_v1.search] async task=%s query=%r',
                task_id[:8], params['query'][:100])

    def _worker():
        _search_runtime.append_event(task_id, {
            'type': 'progress', 'phase': 'started',
            'query': params['query'][:200],
        })
        try:
            result = _run_search(**params)
        except Exception as e:
            logger.exception('[api_v1.search] async task=%s failed', task_id[:8])
            _search_runtime.finish(task_id, error=e,
                                   error_context='api_v1.search:async')
            return
        _search_runtime.append_event(task_id, {
            'type': 'progress', 'phase': 'finished',
            'count': result['count'], 'took_ms': result['took_ms'],
        })
        _search_runtime.finish(task_id, result=result)

    _search_runtime.spawn(task_id, _worker)
    return api_ok(task_id=task_id, kind='search', status='pending',
                   poll_url=f'/api/v1/tasks/{task_id}/events',
                   stream_url=f'/api/v1/tasks/{task_id}/stream',
                   abort_url=f'/api/v1/tasks/{task_id}/abort',
                   push_channel='search')


# ── Browser fetch ──────────────────────────────────────────────────

@api_v1_agents_bp.route('/api/v1/agents/browser/fetch', methods=['POST'])
@require_scope('agents:browser')
@api_meta(summary='Fetch a URL via the server-side browser pipeline',
          tags=['agents'], scope='agents:browser')
def browser_fetch():
    body = parse_body()
    try:
        url = require_str(body, 'url', max_len=2000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'url')
    try:
        from lib.search_runtime import ensure_search_runtime
        fetch_page_content = ensure_search_runtime().fetch_page_content
    except Exception as e:
        return api_internal_error(e, context='Fetch pipeline unavailable',
                                  source='api_v1.agents.browser_fetch')
    try:
        text = fetch_page_content(url) or ''
    except Exception as e:
        logger.warning('[api_v1.browser] fetch failed for url=%s: %s',
                       url, e, exc_info=True)
        return api_internal_error(e, context='browser_fetch',
                                  source='api_v1.agents.browser_fetch',
                                  log_traceback=False)
    return api_ok(url=url, text=text, length=len(text))


# ── Image generation ───────────────────────────────────────────────

@api_v1_agents_bp.route('/api/v1/agents/image-gen', methods=['POST'])
@require_scope('agents:image')
@api_meta(summary='Generate an image (delegates to the image-gen tool)',
          tags=['agents'], scope='agents:image',
          request_body={'required': True, 'content': {'application/json': {
              'schema': {'type': 'object',
                          'required': ['prompt'],
                          'properties': {
                              'prompt': {'type': 'string'},
                              'model': {'type': 'string'},
                              'size': {'type': 'string'},
                          }}}}})
def image_gen():
    body = parse_body()
    try:
        prompt = require_str(body, 'prompt', max_len=4000)
    except BadRequest as e:
        return api_bad_request(str(e), field=e.field or 'prompt')
    try:
        from lib.image_gen import generate_image
    except ImportError as e:
        return api_internal_error(e, context='Image-gen unavailable',
                                  source='api_v1.agents.image_gen')
    aspect = body.get('aspect_ratio') or body.get('size') or '1:1'
    if aspect in ('1024x1024', '1024x1536', '1536x1024'):
        # OpenAI-style sizes — map onto our aspect_ratio enum.
        aspect = {'1024x1024': '1:1',
                  '1024x1536': '9:16',
                  '1536x1024': '16:9'}.get(aspect, '1:1')
    try:
        out = generate_image(
            prompt=prompt,
            model=body.get('model') or '',
            aspect_ratio=aspect,
            resolution=body.get('resolution') or '1K',
        )
    except Exception as e:
        logger.exception('[api_v1.image-gen] failed')
        return api_internal_error(e)
    return api_ok(out)


# ── Swarm ──────────────────────────────────────────────────────────
# Swarm runs as a sub-orchestration within a chat task (the swarm tools
# are default tools — the model calls spawn_agents on any turn).  The
# routes below only expose status/abort, mirroring the legacy
# /api/swarm/* surface.

@api_v1_agents_bp.route('/api/v1/agents/swarm/status/<task_id>',
                         methods=['GET'])
@require_scope('agents:swarm')
@api_meta(summary='Get swarm status for a task', tags=['agents'],
          scope='agents:swarm')
def swarm_status(task_id):
    try:
        from lib.swarm.integration import get_swarm_status
    except ImportError as e:
        return api_internal_error(e, context='Swarm unavailable',
                                  source='api_v1.agents.swarm_status')
    status = get_swarm_status(
        task_id,
        user_id=int(request_user_id()),
    )
    if status is None:
        # Three-state contract (see routes/api_v1/swarm.py): None here means
        # "no record anywhere", which is ambiguous — never a settle signal.
        return api_ok({'active': None, 'known': False})
    return api_ok(status)


@api_v1_agents_bp.route('/api/v1/agents/swarm/abort/<task_id>',
                         methods=['POST'])
@require_scope('agents:swarm')
@api_meta(summary='Abort a swarm', tags=['agents'], scope='agents:swarm')
def swarm_abort(task_id):
    try:
        from lib.swarm.integration import abort_swarm
    except ImportError as e:
        return api_internal_error(e, context='Swarm unavailable',
                                  source='api_v1.agents.swarm_abort')
    try:
        result = abort_swarm(
            task_id,
            user_id=int(request_user_id()),
        )
    except Exception as e:
        logger.warning('[api_v1.swarm] abort failed task=%s: %s',
                       task_id, e, exc_info=True)
        return api_internal_error(e, context='Swarm abort failed',
                                  source='api_v1.agents.swarm_abort',
                                  log_traceback=False)
    if not result.get('success'):
        from lib.api_response import api_not_found
        return api_not_found('Swarm not found')
    return api_ok({'aborted': task_id})


__all__ = ['api_v1_agents_bp']
