"""routes/paper.py — Paper Reading Mode HTTP endpoints (thin route layer).

Every piece of business logic (hashing, prompts, LLM streaming, tool
execution, figure extraction + injection, report TaskRuntime + engine,
translate TaskRuntime + engine, arxiv ID parser, library schema) lives
in ``lib.paper.*``.

Endpoints:
  POST /api/paper/chat                   — Streaming LLM chat (Q&A / translate)
  POST /api/paper/extract-images         — Force figure/table extraction
  GET  /api/paper/images/<phash>/<file>  — Serve an extracted image
  POST /api/paper/report/start           — Start (or join) report task
  GET  /api/paper/report/poll            — Poll report events
  POST /api/paper/report/abort           — Abort running report
  POST /api/paper/report/lookup          — Find by (paper_hash, lang)
  GET  /api/paper/report/export          — MD / HTML / PDF export
  POST /api/paper/report/cache           — DB cache lookup
  POST /api/paper/translate/start        — Start (or join) translate task
  GET  /api/paper/translate/poll         — Poll translate events
  POST /api/paper/translate/abort        — Abort running translate
  POST /api/paper/translate/lookup       — Find by (paper_hash, lang)
  POST /api/paper/translate/cache        — DB cache lookup
  POST /api/paper/search-arxiv           — Search arXiv by title/keywords
  POST /api/paper/fetch-arxiv            — Download PDF (sync)
  POST /api/paper/fetch-arxiv-stream     — Download + parse + extract (SSE)
  GET  /api/paper/pdf/<file>             — Serve a stored PDF
  POST /api/paper/reparse                — Re-parse stored PDF
  POST /api/paper/upload                 — Upload + parse + extract
  GET  /api/paper/library                — List bookshelf
  PUT  /api/paper/library/<id>           — Upsert a library entry
  DELETE /api/paper/library/<id>         — Remove from bookshelf

Back-compat: ``routes/api_v1/agents.py`` imports ``start_report_task``,
``start_translate_task``, ``poll_report_task``, ``poll_translate_task``
from this module; ``tests/test_paper_migration.py`` imports the private
runtime symbols (``_report_runtime``, ``_translate_runtime``, ``_new_*_task``,
``_append_*_event``, ``_cleanup_stale_*_tasks``, ``_*_index_get``, …).
All those names are re-exported here.
"""

import asyncio
import base64
import json
import os
import re
import time
from urllib.parse import unquote

import requests as _requests
from flask import Blueprint, Response, jsonify, request, send_file

from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
)
from lib.database import (
    DOMAIN_CHAT,
    async_fetchall,
    async_fetchone,
    db_execute_with_retry,
)
from lib.http_client import http_get
from lib.log import get_logger
from lib.paper import (  # noqa: F401  — back-compat re-exports
    BASE_DIR,
    PAPER_DIR,
    PAPER_IMG_DIR,
    _FIG_EXTRACT_VERSION,
    _LANG_NAMES,
    _LIB_IMAGES_CAP,
    _LIB_PARSED_TEXT_CAP,
    _LIB_QA_HISTORY_CAP,
    _LIB_TITLE_CAP,
    _MAX_REPORT_TOOL_ROUNDS,
    _PAPER_LIB_COLUMNS,
    _REPORT_PROMPT_EN,
    _REPORT_PROMPT_ZH,
    _REPORT_TASK_TTL,
    _REPORT_TOOLS,
    build_rebuttal_prompt,
    build_rebuttal_tool_instruction,
    build_review_prompt,
    build_review_tool_instruction,
    date_anchor_clause,
    injection_notice,
    is_rebuttal_lang,
    is_review_family,
    is_review_lang,
    list_venues,
    make_review_lang,
    parse_report_lang,
    sanitize_paper_text,
    wrap_untrusted,
    _TRANSLATE_CHUNK_SIZE,
    _TRANSLATE_TASK_TTL,
    _append_report_event,
    _append_translate_event,
    _build_image_manifest,
    _cleanup_stale_report_tasks,
    _cleanup_stale_translate_tasks,
    _ensure_paper_images,
    _backfill_library_title,
    _ensure_title_heading,
    _extract_title_from_report,
    _execute_report_tool,
    _extract_arxiv_id,
    _extract_paper_figures,
    _inject_images_into_report,
    _lib_row_to_dict,
    _load_image_manifest,
    _lookup_paper_title,
    _new_report_task,
    _new_translate_task,
    _new_qa_task,
    _append_qa_event,
    _cleanup_stale_qa_tasks,
    _qa_latest_for,
    _qa_runtime,
    _run_qa_task,
    build_qa_messages,
    _paper_hash,
    _report_dedup_index,
    fetch_arxiv_title,
    search_arxiv,
    recommend_papers,
    _new_recommend_task,
    _append_recommend_event,
    _cleanup_stale_recommend_tasks,
    _recommend_key,
    _recommend_latest_for,
    _recommend_runtime,
    _run_recommend_task,
    _report_dedup_lock,
    _report_index_get,
    _report_index_register,
    _report_runtime,
    _report_tasks,
    _report_tasks_lock,
    _run_report_task,
    _run_translate_task,
    _safe_hash_dir,
    _stream_llm_sse,
    _translate_dedup_index,
    _translate_dedup_lock,
    _translate_index_get,
    _translate_index_register,
    _translate_runtime,
    _translate_tasks,
    _translate_tasks_lock,
)
from lib.request_parser import async_parse_body
from routes._task_routes import register_task_routes
from routes.common import DEFAULT_USER_ID

logger = get_logger(__name__)

paper_bp = Blueprint('paper', __name__)
# v1 blueprint for the JSON routes (the 5 carve-outs above stay on paper_bp).
from routes.api_v1.paper import api_v1_paper_bp  # noqa: E402


def _parse_report_meta(row):
    """Decode the stored ``paper_reports.meta`` JSON for the finish-tag badge.

    Returns the parsed dict, or None when the column is absent (legacy rows
    persisted before the column existed) or malformed.
    """
    raw = row.get('meta') if hasattr(row, 'get') else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Paper:Report] Bad meta JSON: %s', e)
        return None



async def _append_cached_insight(body, phash, lang):
    """Merge the sibling persisted ``insight:<ui>`` row into a cached report body.

    Read-path only — NEVER triggers a new insight generation. When a plain
    report is served from the DB cache, look up the separately-persisted insight
    section (key ``insight:<ui_lang>``) and append its markdown so a reopened
    paper shows the insight the reader generated earlier, instead of it silently
    vanishing until a forced regenerate.

    Guards (so this is byte-identical to today for papers without an insight):
      * skips Review Mode entirely (insight is only produced for plain reports);
      * no-op when no insight row exists / it is empty;
      * never double-appends if ``body`` already contains the section (a cache
        row that was persisted with the insight baked in, or a re-entry).
    """
    if is_review_lang(lang):
        return body
    parsed = parse_report_lang(lang)
    ui_lang = parsed['ui_lang']
    try:
        from lib.paper.insight_engine import insight_lang_key
        ins_row = await async_fetchone(
            "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, insight_lang_key(ui_lang)), domain=DOMAIN_CHAT,
        )
    except Exception as e:
        logger.warning('[Paper:Report] Cached-insight lookup failed hash=%s: %s', phash, e)
        return body
    if not ins_row or not ins_row['report']:
        return body
    section = ins_row['report'].strip()
    if not section:
        return body
    # Idempotency: the insight section header is a stable marker. If the body
    # already carries it (baked-in cache row / prior append), do not duplicate.
    marker = '## 💡'
    header_line = section.splitlines()[0].strip() if section else ''
    if (header_line and header_line in body) or (marker in body and marker in section):
        return body
    logger.info('[Paper:Report] Merged cached insight into reopened report — '
                'hash=%s key=%s (+%d chars)', phash, insight_lang_key(ui_lang), len(section))
    return body.rstrip() + '\n\n' + section + '\n'


async def _merge_cached_termfill(body, meta, phash, lang):
    """Merge the sibling persisted ``termfill:<ui>`` addendum into a reopened
    report, and — since the addendum was only persisted after a re-audit proved
    it closes the gaps — downgrade the meta's terminology warning card.

    Read-path only; never regenerates. Returns ``(body, meta)``. Byte-identical
    to today for papers without a backfill row: skips Review Mode, no-ops when no
    row exists, and never double-appends (idempotent on the addendum header).
    """
    if is_review_lang(lang):
        return body, meta
    parsed = parse_report_lang(lang)
    ui_lang = parsed['ui_lang']
    try:
        from lib.paper.terminology_backfill import termfill_lang_key
        tf_row = await async_fetchone(
            "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, termfill_lang_key(ui_lang)), domain=DOMAIN_CHAT,
        )
    except Exception as e:
        logger.warning('[Paper:Report] Cached-termfill lookup failed hash=%s: %s', phash, e)
        return body, meta
    if not tf_row or not tf_row['report']:
        return body, meta
    addendum = tf_row['report'].strip()
    if not addendum:
        return body, meta
    # The addendum's persistence is proof the glossary is now complete — drop the
    # stale warning card so a reopened report doesn't contradict its own glossary.
    if isinstance(meta, dict) and meta.get('terminologyAudit'):
        meta = dict(meta)
        meta['terminologyAudit'] = None
    header_line = addendum.splitlines()[0].strip() if addendum else ''
    if header_line and header_line in body:
        return body, meta  # already merged / baked in
    logger.info('[Paper:Report] Merged cached termfill addendum into reopened report — '
                'hash=%s key=%s (+%d chars)', phash, termfill_lang_key(ui_lang), len(addendum))
    return body.rstrip() + '\n\n' + addendum + '\n', meta


# ══════════════════════════════════════════════════════
#  API Endpoints
# ══════════════════════════════════════════════════════

@paper_bp.route('/api/paper/chat', methods=['POST'])
async def paper_chat():
    """Streaming LLM chat for paper Q&A / translation.

    Body JSON:
        messages: list — OpenAI-format messages [{role, content}, ...]
        model: str (optional) — LLM model to use
    Returns:
        SSE stream of chat completion deltas.
    """
    data = await async_parse_body()
    messages = data.get('messages', [])
    model = data.get('model') or None

    if not messages:
        logger.warning('[Paper:Chat] Request with no messages')
        return api_bad_request('No messages provided')

    # Log the request (truncate user message for privacy)
    last_msg = messages[-1] if messages else {}
    last_content_preview = str(last_msg.get('content', ''))[:200]
    logger.info('[Paper:Chat] Request — %d messages, model=%s, last_msg_role=%s, preview=%.200s',
                len(messages), model, last_msg.get('role', '?'), last_content_preview)

    def generate():
        yield from _stream_llm_sse(messages, model=model)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})



@api_v1_paper_bp.route('/api/v1/paper/openreview/autofill', methods=['POST'])
async def openreview_autofill():
    """Auto-fill the review form on the reviewer's active OpenReview tab.

    The killer feature: one Tofu button → the browser bridge reads the review
    form on the CURRENT OpenReview page, and the reviewer's already-generated
    Review-Mode output (review prose + OA + confidence) is typed into the
    matching fields. It STOPS there — it NEVER clicks any Submit/Post/Confirm
    control; the human reviews the filled form and submits it themselves.

    Body JSON:
        paper_hash: str — the paper whose review to fill from.
        venue: str (optional) — review venue key (default resolved to generic).
        ui_lang: 'en'|'zh' (optional, default 'en') — which stored review row.
        client_id: str (optional) — target extension client.

    Returns:
        JSON report: which fields were filled/skipped, how many submit controls
        were detected-and-avoided, and an actionable message. 4xx (never a
        silent success) when the bridge is not connected, the tab is not an
        OpenReview page, or no review exists yet.
    """
    from lib.browser import is_extension_connected, send_browser_command as _send_cmd
    from lib.browser.queue import _get_active_client
    from lib.paper import (autofill_openreview_review, extract_review_values,
                           make_review_lang)

    data = await async_parse_body()
    phash = (data.get('paper_hash') or '').strip()
    venue = (data.get('venue') or 'generic').strip().lower()
    ui_lang = (data.get('ui_lang') or 'en').strip().lower()
    client_id = (data.get('client_id') or '').strip() or None
    if not phash:
        return api_bad_request('No paper_hash provided')

    # Require a connected extension up front — a clear failure, not a hang.
    if not is_extension_connected(client_id or _get_active_client()):
        logger.info('[OpenReview] Autofill requested but no extension connected (hash=%s)', phash)
        return api_error('Browser extension is not connected. Install/enable the Tofu '
                         'Browser Bridge extension, open the OpenReview page, and retry.',
                         status=409)

    # Fetch the finished review for this paper+venue+lang.
    review_key = make_review_lang(venue, ui_lang)
    review_body = ''
    try:
        row = await async_fetchone(
            "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, review_key), domain=DOMAIN_CHAT,
        )
        if row and row['report']:
            review_body = row['report']
    except Exception as e:
        logger.warning('[OpenReview] Review lookup failed hash=%s: %s', phash, e)
    if not review_body.strip():
        return api_error('No review found for this paper yet. Generate the review first, '
                         'then auto-fill.', status=409)

    # Try to carry the paper title into the review-title field.
    title = ''
    try:
        title = _lookup_paper_title(phash) or ''
    except Exception as e:
        logger.debug('[OpenReview] title lookup failed (non-fatal): %s', e)
    values = extract_review_values(review_body, title=title)

    logger.info('[OpenReview] Autofill start hash=%s venue=%s ui=%s oa=%s conf=%s client=%s',
                phash, venue, ui_lang, values.get('overall'), values.get('confidence'),
                (client_id or 'active')[:12])

    # send_browser_command is synchronous (blocks on the extension round-trip);
    # run the whole orchestration off the event loop so Hypercorn isn't blocked.
    import lib.browser as _bridge

    def _run():
        return autofill_openreview_review(_bridge, values, client_id=client_id, timeout=20)

    try:
        report = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error('[OpenReview] Autofill orchestration failed hash=%s: %s', phash, e, exc_info=True)
        return api_internal_error('Auto-fill failed unexpectedly — the form was not submitted.')

    logger.info('[OpenReview] Autofill done hash=%s ok=%s stage=%s filled=%d avoided_submit=%d',
                phash, report.get('ok'), report.get('stage'),
                len(report.get('filled', [])), report.get('submit_controls_detected', 0))
    status = 200 if report.get('ok') else 409
    return jsonify(report), status


@api_v1_paper_bp.route('/api/v1/paper/extract-images', methods=['POST'])
async def extract_images():
    """Extract figure/table images from a previously uploaded PDF.

    Body JSON:
        filename: str — the filename returned by /api/paper/upload or /api/paper/fetch-arxiv
        paper_hash: str (optional) — if omitted, computed from filename bytes
        max_images: int (optional) — cap, default 30
        max_image_width: int (optional) — default 900

    Returns:
        { ok: true, paper_hash: str, images: [{url, caption, page, source, width, height}] }
    """
    data = await async_parse_body()
    filename = os.path.basename((data.get('filename') or '').strip())
    if not filename:
        logger.warning('[Paper:Images] Request with no filename')
        return api_bad_request('No filename')

    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.isfile(filepath):
        logger.warning('[Paper:Images] PDF not found: %s', filename)
        return api_not_found('PDF not found')

    try:
        max_images = int(data.get('max_images', 30))
        max_image_width = int(data.get('max_image_width', 900))
    except (ValueError, TypeError) as e:
        logger.warning('[Paper:Images] Invalid numeric parameter: %s', e)
        return api_bad_request(f'Invalid parameter: {e}')

    # Cache key — prefer client-provided hash (matches the report cache key),
    # fall back to filename-based hash.
    phash = _safe_hash_dir(data.get('paper_hash', '').strip()) or _paper_hash(filename)
    # Figure extraction is CPU/IO-heavy (pymupdf) — offload off the loop.
    images_out = await asyncio.to_thread(
        _extract_paper_figures, filepath, phash,
        max_images=max_images, max_image_width=max_image_width,
    )
    return api_ok({'paper_hash': phash, 'images': images_out})


@paper_bp.route('/api/paper/images/<phash>/<filename>')
def serve_paper_image(phash, filename):
    """Serve an extracted paper figure image.

    SKIPPED from the native-async conversion: pure file-serving endpoint whose
    only blocking call is ``send_file``, which the server.py Flask→Quart shim
    replaces with a *sync-safe* wrapper. That wrapper, invoked from the event
    loop, schedules the genuine coroutine via ``run_coroutine_threadsafe`` and
    blocks on ``.result()`` — a deadlock inside an ``async def`` handler. Kept
    sync (no DB / no body parse) so the shim runs it safely in an executor.
    """
    phash_safe = _safe_hash_dir(phash)
    if not phash_safe:
        logger.debug('[Paper:Images] Invalid hash: %.40s', phash)
        return api_bad_request('Invalid hash')
    filename = os.path.basename(filename)
    # Only allow our known filename pattern
    if not re.fullmatch(r'fig_\d+_p\d+\.(jpg|jpeg|png)', filename, re.IGNORECASE):
        logger.debug('[Paper:Images] Invalid filename: %s', filename)
        return api_bad_request('Invalid filename')
    filepath = os.path.join(PAPER_IMG_DIR, phash_safe, filename)
    if not os.path.isfile(filepath):
        return api_not_found('Image not found')
    mt = 'image/jpeg' if filename.lower().endswith(('.jpg', '.jpeg')) else 'image/png'
    return send_file(filepath, mimetype=mt, conditional=True)


@api_v1_paper_bp.route('/api/v1/paper/report/start', methods=['POST'])
async def start_report_task():
    """Start (or join) a background paper-report generation task.

    The task is keyed by (paper_hash, lang). If a task is already running,
    the same task is joined — no duplicate work.

    Body JSON:
        paper_text: str — full text of the paper
        model: str (optional) — LLM model to use
        lang: str (optional) — 'zh' for Chinese prompt, else English. Default 'en'.
        force: bool (optional) — bypass DB cache AND restart any running task.
        images: list (optional) — figure/table manifest to inject.

    Returns JSON:
        - DB cache hit: {ok: true, cached: true, report: str, paper_hash: str}
        - Task started/joined: {ok: true, task_id: str, paper_hash: str,
                                running: bool, existed: bool}
    """
    data = await async_parse_body()
    paper_text = data.get('paper_text', '').strip()
    if not paper_text:
        logger.warning('[Paper:Report] Start request with no paper_text')
        return api_bad_request('No paper_text provided')
    if len(paper_text) < 100:
        logger.warning('[Paper:Report] Paper text too short: %d chars', len(paper_text))
        return api_bad_request('Paper text too short (< 100 chars)')

    model = data.get('model') or None
    lang = data.get('lang', 'en') or 'en'
    force = bool(data.get('force'))
    # Client-supplied title — sent so the title prepend works even when the
    # paper_library row hasn't been upserted yet (the frontend's
    # _saveActivePaperState() PUT is fire-and-forget and may race with the
    # report start). Stripped of trailing ``.pdf`` for cleanliness.
    client_title = (data.get('title') or '').strip()
    if client_title.lower().endswith('.pdf'):
        client_title = client_title[:-4].strip()

    phash = _paper_hash(paper_text)
    # Server is the source of truth for figure manifests. The client never
    # forwards the images list any more — we load (or extract) it here.
    images = _load_image_manifest(phash)
    if not images:
        # Manifest missing — try to derive a filename from the request and
        # extract on-the-fly. Otherwise the report renders without figures.
        derived_fn = os.path.basename((data.get('filename') or '').strip())
        if derived_fn:
            images = await asyncio.to_thread(_ensure_paper_images, derived_fn, phash)

    # DB cache check (unless force) — no task needed, report is already done
    if not force:
        try:
            row = await async_fetchone(
                "SELECT report, meta FROM paper_reports WHERE paper_hash = ? AND lang = ?",
                (phash, lang), domain=DOMAIN_CHAT,
            )
            if row and row['report']:
                logger.info('[Paper:Report] DB cache hit — hash=%s lang=%s %d chars',
                            phash, lang, len(row['report']))
                enriched = _inject_images_into_report(
                    row['report'], images, lang=parse_report_lang(lang)['ui_lang'],
                    appendix=not is_review_family(lang),
                    allow_images=not is_review_family(lang))
                enriched = _ensure_title_heading(enriched, phash)
                # Merge the sibling persisted insight section so a reopened
                # paper shows it (read-only; never regenerates).
                enriched = await _append_cached_insight(enriched, phash, lang)
                # Merge the gap-closing backfill addendum + downgrade the stale
                # terminology warning card (read-only; never regenerates).
                _cached_meta = _parse_report_meta(row)
                enriched, _cached_meta = await _merge_cached_termfill(
                    enriched, _cached_meta, phash, lang)
                # Self-heal a sidebar title still stuck at the bare arXiv:<id>
                # from the cached report's Paper Card (cached reports never go
                # through the engine's backfill). Only placeholder rows change.
                resolved_title = ''
                card_title = _extract_title_from_report(row['report'])
                if card_title:
                    try:
                        resolved_title = await asyncio.to_thread(
                            _backfill_library_title, phash, card_title)
                    except Exception as e:
                        logger.warning('[Paper:Report] Cache-path title backfill failed '
                                       'hash=%s: %s', phash, e)
                return jsonify({
                    'ok': True, 'cached': True,
                    'report': enriched, 'paper_hash': phash,
                    'meta': _cached_meta,
                    'resolvedTitle': resolved_title,
                })
        except Exception as e:
            logger.warning('[Paper:Report] DB cache lookup failed (will start task): %s', e)

    # Task dedup via the dedup index ((paper_hash, lang) → task_id)
    existing = _report_index_get(phash, lang)
    if existing and not force and existing['status'] in ('pending', 'running', 'done'):
        logger.info('[Paper:Report] Joining existing task %s (status=%s) — hash=%s lang=%s',
                    existing['task_id'], existing['status'], phash, lang)
        return jsonify({
            'ok': True, 'task_id': existing['task_id'], 'paper_hash': phash,
            'running': existing['status'] in ('pending', 'running'), 'existed': True,
        })

    # Force: abort the old task if any, then create a new one
    if existing and force:
        logger.info('[Paper:Report] Force regen — aborting old task %s', existing['task_id'])
        existing['abort_event'].set()
        existing['status'] = 'error'
        existing['finished_at'] = time.time()

    # Decode the cache key. For ordinary reports this is {'kind':'report',
    # 'ui_lang': 'en'|'zh'}; for Review Mode the key is the composite
    # ``review:<venue>:<uilang>`` → {'kind':'review','venue':...,'ui_lang':...}.
    # The composite key flows UNCHANGED through the cache lookup + dedup index
    # above, so reviews never collide with the plain (paper_hash,'en') report.
    parsed = parse_report_lang(lang)
    ui_lang = parsed['ui_lang']
    is_review = parsed['kind'] == 'review'
    is_rebuttal = parsed['kind'] == 'rebuttal'
    # Both a review and its rebuttal follow-up are text-only decision documents.
    is_review_kin = is_review or is_rebuttal

    # ── Resolve the insight second-pass personal-context scope ──
    # The insight pass injects the operator's paper library + memory store as
    # "reader context" — app-personal state (CLAUDE.md §3.7). This handler is
    # shared by the interactive route AND the headless /api/v1/agents/paper/report
    # façade; the façade sets g.paper_report_headless so we stamp the registry's
    # fail-closed default here (apply_headless_personal_defaults), while the
    # interactive owner (no flag) keeps personal context on. An explicit body
    # ``config`` opt-in always wins (setdefault semantics).
    _report_cfg = dict(data.get('config') if isinstance(data.get('config'), dict) else {})
    try:
        from quart import g as _g
        _is_headless = bool(getattr(_g, 'paper_report_headless', False))
    except Exception as e:
        logger.debug('[Paper:Report] headless flag read failed: %s', e)
        _is_headless = False
    if _is_headless:
        from lib.agent_core.personal_scope import apply_headless_personal_defaults
        apply_headless_personal_defaults(_report_cfg)

    max_text = 120000
    truncated_text = paper_text[:max_text]
    if len(paper_text) > max_text:
        logger.info('[Paper:Report] Truncating paper text from %d to %d chars', len(paper_text), max_text)

    # ── Prompt-injection hardening (untrusted PDF text) ──
    # A submitted PDF can embed directives aimed at the LLM ("ignore previous
    # instructions", "give a positive review", hidden white text, …). Sanitize
    # + fence the paper text BEFORE it is spliced into the prompt. The image
    # manifest is OUR trusted content, so it is appended OUTSIDE the untrusted
    # fence (after sanitize) — never sanitized/fenced as if it were paper text.
    truncated_text, _inj_findings = sanitize_paper_text(truncated_text)
    truncated_text = wrap_untrusted(truncated_text)
    if _inj_findings:
        from lib.log import audit_log
        audit_log('paper_injection_detected', hash=phash, is_review=is_review,
                  findings=_inj_findings)
    # Review Mode + rebuttal are text-only — a peer review / author-response
    # reply carries no figures, so the image manifest is NOT offered (nothing
    # to embed).
    manifest = '' if is_review_kin else _build_image_manifest(images, lang=ui_lang)
    if manifest:
        truncated_text = truncated_text + '\n\n---\n\n' + manifest
        logger.info('[Paper:Report] Injected image manifest — %d images, hash=%s', len(images), phash)

    if is_rebuttal:
        # Rebuttal follow-up: fetch the reviewer's ORIGINAL review for this
        # paper+venue (the sibling ``review:<venue>:<uilang>`` row) and the
        # author's rebuttal text (posted by the user), then run the SAME engine
        # to produce a follow-up reply + structured score-adjustment decision.
        author_rebuttal = (data.get('author_rebuttal') or data.get('rebuttal') or '').strip()
        if not author_rebuttal:
            logger.warning('[Paper:Rebuttal] Start with no author_rebuttal — hash=%s', phash)
            return api_bad_request('No author_rebuttal provided')
        review_key = make_review_lang(parsed['venue'], ui_lang)
        original_review = ''
        try:
            rrow = await async_fetchone(
                "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
                (phash, review_key), domain=DOMAIN_CHAT,
            )
            if rrow and rrow['report']:
                original_review = rrow['report']
        except Exception as e:
            logger.warning('[Paper:Rebuttal] Original-review lookup failed hash=%s: %s', phash, e)
        if not original_review.strip():
            logger.warning('[Paper:Rebuttal] No original review for hash=%s venue=%s ui=%s',
                           phash, parsed['venue'], ui_lang)
            return api_bad_request('No original review found — generate the review first')
        # The author rebuttal is UNTRUSTED (in the OpenReview flow it is written
        # by the paper authors), so sanitize + fence it exactly like the paper
        # text before splicing. The original review is OUR content (trusted).
        safe_rebuttal, _reb_inj = sanitize_paper_text(author_rebuttal[:40000])
        safe_rebuttal = wrap_untrusted(safe_rebuttal)
        if _reb_inj:
            from lib.log import audit_log
            audit_log('paper_injection_detected', hash=phash, is_rebuttal=True,
                      findings=_reb_inj)
        # Fill slots. paper_text (already truncated+fenced above) goes LAST so a
        # brace inside the review/rebuttal is never mistaken for a later slot.
        prompt = (build_rebuttal_prompt(parsed['venue'], ui_lang)
                  .replace('{original_review}', original_review)
                  .replace('{author_rebuttal}', safe_rebuttal)
                  .replace('{paper_text}', truncated_text))
        tool_instruction = (date_anchor_clause(ui_lang)
                            + injection_notice(ui_lang, _inj_findings or _reb_inj)
                            + build_rebuttal_tool_instruction(ui_lang))
        messages = [
            {'role': 'system', 'content': tool_instruction},
            {'role': 'user', 'content': prompt},
        ]
        task_id = f'reb_{int(time.time() * 1000)}_{phash[:8]}_{parsed["venue"]}_{ui_lang}'
        task = _new_report_task(task_id, phash, lang, model,
                                client_title=client_title, ui_lang=ui_lang,
                                config=_report_cfg)
        logger.info('[Paper:Rebuttal] Starting task %s — venue=%s model=%s ui_lang=%s '
                    'rebuttal_len=%d hash=%s', task_id, parsed['venue'], model, ui_lang,
                    len(author_rebuttal), phash)
        _report_runtime.spawn(task_id, _run_report_task, task, messages, images)
        return jsonify({
            'ok': True, 'task_id': task_id, 'paper_hash': phash,
            'running': True, 'existed': False,
        })

    if is_review:
        # Review Mode: venue-aware peer-review prompt (different output
        # structure + scorecard), but the SAME engine/tools/runtime.
        prompt_template = build_review_prompt(parsed['venue'], ui_lang)
        prompt = prompt_template.replace('{paper_text}', truncated_text)
        # Prepend the input-safety clause so the reviewer treats the fenced
        # paper block as data, and flags (never obeys) any embedded directive.
        tool_instruction = (date_anchor_clause(ui_lang)
                            + injection_notice(ui_lang, _inj_findings)
                            + build_review_tool_instruction(ui_lang))
        messages = [
            {'role': 'system', 'content': tool_instruction},
            {'role': 'user', 'content': prompt},
        ]
        task_id = f'rvw_{int(time.time() * 1000)}_{phash[:8]}_{parsed["venue"]}_{ui_lang}'
        task = _new_report_task(task_id, phash, lang, model,
                                client_title=client_title, ui_lang=ui_lang,
                                config=_report_cfg)
        logger.info('[Paper:Review] Starting task %s — venue=%s model=%s ui_lang=%s '
                    'text_len=%d hash=%s', task_id, parsed['venue'], model, ui_lang,
                    len(paper_text), phash)
        _report_runtime.spawn(task_id, _run_report_task, task, messages, images)
        return jsonify({
            'ok': True, 'task_id': task_id, 'paper_hash': phash,
            'running': True, 'existed': False,
        })

    # ── Ordinary explainer report (unchanged path) ──
    # truncated_text is already sanitized + fenced above, so the report path
    # inherits the same injection hardening as review; prepend the input-safety
    # clause so the model treats the fenced block as data.
    prompt_template = _REPORT_PROMPT_ZH if ui_lang == 'zh' else _REPORT_PROMPT_EN
    prompt = prompt_template.replace('{paper_text}', truncated_text)
    tool_instruction = (
        date_anchor_clause(ui_lang) +
        injection_notice(ui_lang, _inj_findings) +
        "You have access to web_search (batch) and fetch_url (batch) tools.\n\n"
        "BEFORE writing any of the report, you are EXPECTED to do a research-grade "
        "literature scan. The reader's most common complaint is that follow-up work "
        "is missing — do not let that happen.\n\n"
        "Recommended search plan (run several batches in parallel for speed):\n"
        "  1. Identify the paper's title, first author, and approximate year. Then search:\n"
        "     - '<title> citing OR follow-up' to surface later papers that built on it.\n"
        "     - '<title> survey' / '<key method name> survey' for review articles that "
        "place it in context (these are gold for related-work).\n"
        "     - '<key method name> vs <closest competitor>' to find direct comparisons.\n"
        "  2. For the 2-3 closest prior methods named in the paper, search "
        "'<method> limitations' / '<method> improvement' to find what came after.\n"
        "  3. If the paper is older than 12 months, search for its successor / scaled-up "
        "versions explicitly (e.g. 'BERT successors', 'Transformer follow-ups', "
        "'<paper-name> extension 2023 2024'). At least 3-5 concrete follow-up papers must end up "
        "in your Research Landscape section.\n"
        "  4. Verify any specific quantitative claim you find ambiguous (citation counts, "
        "benchmark records, who first proposed an idea) via fetch_url on arXiv abstracts, "
        "Papers-with-Code, or the original paper page.\n\n"
        "Tool-call budget: up to "
        f"{_MAX_REPORT_TOOL_ROUNDS} rounds. You may batch many queries per round — "
        "prefer a few wide rounds over many narrow ones. Once you've gathered enough, "
        "stop calling tools and write the FULL structured report in one pass.\n\n"
        "Quality reminder: methodology must be reproduction-grade (the *why* of every "
        "design choice, not just the *what*). Related-work survey must include "
        "predecessors, contemporaries, AND post-publication follow-ups.\n\n"
        "Output discipline: when you start writing the report, begin IMMEDIATELY with "
        "the first heading (`## ⚡ TL;DR` or `## ⚡ 一句话总结`). Do NOT emit ANY text "
        "before that heading — no 'I'll research...', no 'I have enough material...', "
        "no 'Now I'll write...', no transition sentences. The reader sees your raw "
        "output verbatim, and ANY pre-heading chatter is a bug. The very first "
        "characters of your final response MUST be `## ⚡`.\n\n"
    )
    messages = [
        {'role': 'system', 'content': tool_instruction},
        {'role': 'user', 'content': prompt},
    ]

    task_id = f'rpt_{int(time.time() * 1000)}_{phash[:8]}_{lang}'
    task = _new_report_task(task_id, phash, lang, model,
                            client_title=client_title, ui_lang=ui_lang,
                            config=_report_cfg)

    logger.info('[Paper:Report] Starting task %s — model=%s lang=%s text_len=%d hash=%s',
                task_id, model, lang, len(paper_text), phash)
    _report_runtime.spawn(task_id, _run_report_task, task, messages, images)

    return jsonify({
        'ok': True, 'task_id': task_id, 'paper_hash': phash,
        'running': True, 'existed': False,
    })


@api_v1_paper_bp.route('/api/v1/paper/report/poll', methods=['GET'])
async def poll_report_task():
    """Poll a report task for new events.

    Query params:
        task_id: str — from /api/paper/report/start
        cursor: int (optional, default 0) — resume from this seq; 0 replays all.

    Returns JSON:
        {
          ok: true,
          status: 'running' | 'done' | 'error',
          events: [ {seq, type, ...}, ... ],    # newer than cursor
          next_cursor: int,
          report: str (optional, if done),
          paper_hash: str,
          error: str (optional, if status=error),
        }

    Events have the same schema as chat tool events so the frontend can
    feed them directly to its existing `renderToolRoundsHTML` pipeline.
    """
    task_id = request.args.get('task_id', '').strip()
    try:
        cursor = int(request.args.get('cursor', 0))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[paper] poll_report_task caught %s: %s', type(_e_audit).__name__, _e_audit)
        cursor = 0

    if not task_id:
        return api_bad_request('task_id required')

    # Direct lookup by task_id (runtime is keyed by task_id; dedup index
    # maps (paper_hash, lang) → task_id for the start endpoint).
    task = _report_runtime.get(task_id)
    if not task:
        logger.debug('[Paper:Report:Poll] Unknown task_id=%s', task_id)
        return api_not_found('task not found (may have expired)')

    # Snapshot events since cursor
    with task['events_lock']:
        total = len(task['events'])
        cursor = max(0, min(cursor, total))
        new_events = list(task['events'][cursor:])

    resp = {
        'ok': True,
        'status': task['status'],
        'events': new_events,
        'next_cursor': total,
        'paper_hash': task['paper_hash'],
    }
    if task['status'] == 'done':
        resp['report'] = task.get('enriched_text') or task.get('full_text', '')
        if task.get('report_meta'):
            resp['meta'] = task['report_meta']
        if task.get('resolved_title'):
            resp['resolvedTitle'] = task['resolved_title']
    if task['status'] == 'aborted':
        # User stopped generation — return whatever partial text was produced
        # so the frontend can show it read-only under a "stopped" banner.
        resp['partial'] = task.get('full_text', '')
    if task['status'] == 'error':
        resp['error'] = task.get('error', '')
    return jsonify(resp)


@api_v1_paper_bp.route('/api/v1/paper/review/venues', methods=['GET'])
async def list_review_venues():
    """List the peer-review venues Review Mode supports.

    Returns: {ok: true, venues: [{key, name}, ...]} — registry order. The
    frontend uses this to populate the venue dropdown; the single source of
    truth is ``REVIEW_VENUES`` in ``lib/paper/review.py``.
    """
    return jsonify({'ok': True, 'venues': list_venues()})


@api_v1_paper_bp.route('/api/v1/paper/report/lookup', methods=['POST'])
async def lookup_report_task():
    """Find an existing running task by (paper_hash, lang).

    Used by the frontend on tab re-entry / mode re-enter to see whether a
    task is already running server-side for this paper — so it can resume
    polling without starting a new one.

    Body JSON: {paper_hash: str, lang: str}
    Returns: {ok: true, task_id: str, status: str} or {ok: false}
    """
    data = await async_parse_body()
    phash = (data.get('paper_hash') or '').strip()
    lang = data.get('lang', 'en') or 'en'
    if not phash:
        return api_bad_request('paper_hash required')
    task = _report_index_get(phash, lang)
    if task:
        return jsonify({
            'ok': True,
            'task_id': task['task_id'],
            'status': task['status'],
            'paper_hash': phash,
        })
    return jsonify({'ok': False})


@api_v1_paper_bp.route('/api/v1/paper/report/export', methods=['GET'])
async def export_report():
    """Download a stored report as Markdown or standalone HTML.

    Query string:
        paper_hash: str (required)
        lang: str (default 'en')
        format: 'md' | 'html' (default 'md')

    Returns the file inline as a download. The HTML variant is a self-
    contained document — figure URLs are rewritten to absolute so the file
    works when opened from disk while the Tofu server is running.
    """
    phash = (request.args.get('paper_hash') or '').strip()
    lang = (request.args.get('lang') or 'en').strip() or 'en'
    # Some reverse proxies (e.g. the VS Code web proxy) double-encode
    # percent-escapes in the query string: the client sends the composite
    # Review-Mode key ``review:neurips:en`` as ``review%3Aneurips%3Aen``, the
    # proxy re-encodes the ``%`` → ``review%253Aneurips%253Aen``, and Quart
    # decodes only once so the handler sees a literal ``review%3A…`` that
    # matches no stored row. Plain-language report exports (``en``/``zh``) have
    # no reserved chars so they're unaffected — only review exports 404'd.
    # Undo one extra decode layer when the value still carries ``%XX`` escapes.
    if '%' in lang:
        try:
            _decoded = unquote(lang)
            if _decoded != lang:
                logger.debug('[Paper:Report:Export] Decoded double-encoded lang %r -> %r',
                             lang, _decoded)
                lang = _decoded
        except Exception as e:
            logger.debug('[Paper:Report:Export] lang unquote failed: %s', e)
    fmt = (request.args.get('format') or 'md').strip().lower()
    # `pdf` is a client-side rendering of the HTML body via window.print() —
    # the server emits the same HTML doc but inline (no attachment) and with
    # an auto-print bootstrap so the new tab opens the print dialog.
    if fmt not in ('md', 'html', 'pdf'):
        return api_bad_request('format must be md, html, or pdf')
    if not _safe_hash_dir(phash):
        return api_bad_request('invalid paper_hash')
    inline_html = (fmt == 'pdf') or (request.args.get('inline') in ('1', 'true', 'yes'))

    try:
        row = await async_fetchone(
            'SELECT report FROM paper_reports WHERE paper_hash=? AND lang=?',
            (phash, lang), domain=DOMAIN_CHAT,
        )
    except Exception as e:
        logger.error('[Paper:Report:Export] Lookup failed: %s', e, exc_info=True)
        return api_internal_error('lookup failed')
    if not row or not row['report']:
        return api_not_found('report not found')

    images = _load_image_manifest(phash)
    # Review-Mode rows carry a composite lang key; image injection / appendix
    # headings need the REAL UI language, not the raw cache key.
    _inj_lang = parse_report_lang(lang)['ui_lang']
    body_md = _inject_images_into_report(row['report'], images, lang=_inj_lang,
                                         appendix=not is_review_family(lang),
                                         allow_images=not is_review_family(lang))
    body_md = _ensure_title_heading(body_md, phash)

    # Get the paper title for the export filename / page title
    title = 'Paper Report'
    try:
        trow = await async_fetchone(
            'SELECT title, arxiv_id FROM paper_library '
            'WHERE paper_hash=? AND user_id=? ORDER BY updated_at DESC LIMIT 1',
            (phash, DEFAULT_USER_ID), domain=DOMAIN_CHAT,
        )
        if trow:
            title = trow['title'] or (f'arXiv:{trow["arxiv_id"]}' if trow['arxiv_id'] else title)
    except Exception as e:
        logger.debug('[Paper:Report:Export] Title lookup failed: %s', e)

    safe_slug = re.sub(r'[^\w\-]+', '_', title)[:80] or 'paper'

    if fmt == 'md':
        return Response(
            body_md,
            mimetype='text/markdown; charset=utf-8',
            headers={'Content-Disposition':
                     f'attachment; filename="paper_report_{safe_slug}.md"'},
        )

    # HTML — render Markdown to HTML and wrap in a self-contained document.
    # Protect math delimiters from Python's markdown processor: $...$ inline
    # math contains underscores and asterisks (e.g. $a_i$, $f^*$) that
    # markdown otherwise interprets as emphasis, mangling the LaTeX. We
    # extract math regions, swap in placeholders, run markdown, then put
    # the original math back so KaTeX's auto-render can find it client-side.
    math_store: list[str] = []

    def _stash_math(m):
        math_store.append(m.group(0))
        return f'\x02MATH{len(math_store) - 1}\x03'

    md_protected = body_md
    # Display math first ($$...$$ and \[...\]). Order matters — $$ would
    # otherwise be eaten by the inline $ pattern.
    md_protected = re.sub(r'\$\$[\s\S]+?\$\$', _stash_math, md_protected)
    md_protected = re.sub(r'\\\[[\s\S]+?\\\]', _stash_math, md_protected)
    # Inline math: $...$ on a single line, no $ inside, no | (table cell
    # separator) to avoid swallowing rows when a cell holds a literal $.
    md_protected = re.sub(
        r'\$(?!\$)((?:[^$\\\n|]|\\.)+?)\$(?!\$)', _stash_math, md_protected,
    )
    md_protected = re.sub(r'\\\(.+?\\\)', _stash_math, md_protected)

    try:
        import markdown as _md
        body_html = _md.markdown(
            md_protected,
            extensions=['tables', 'fenced_code', 'attr_list', 'sane_lists'],
            output_format='html5',
        )
    except Exception as e:
        logger.error('[Paper:Report:Export] markdown render failed: %s', e, exc_info=True)
        return api_internal_error('render failed')

    # Restore math placeholders. KaTeX's auto-render extension (loaded
    # below) will scan for $...$ / $$...$$ on the client and replace with
    # rendered formulas — this works for both Standalone HTML download and
    # the PDF print preview.
    def _unstash(m):
        idx = int(m.group(1))
        return math_store[idx] if 0 <= idx < len(math_store) else m.group(0)

    body_html = re.sub(r'\x02MATH(\d+)\x03', _unstash, body_html)

    # Embed paper-image URLs as base64 data: URIs so the standalone HTML
    # file works offline (no server reachability required) — this is the
    # common case for users who download the report to share or archive.
    # Other root-anchored URLs (e.g. third-party `/static/...`) are
    # rewritten to absolute http(s) URLs against the server origin.
    origin = request.host_url.rstrip('/')

    def _embed_paper_image(match):
        attr = match.group(1)
        url = match.group(2)
        m = re.match(r'^/api/paper/images/([a-f0-9]{8,64})/([\w\-.]+)$', url)
        if not m:
            return attr + origin + url
        ph, fn = m.group(1), m.group(2)
        ph_safe = _safe_hash_dir(ph)
        if not ph_safe:
            return attr + origin + url
        fpath = os.path.join(PAPER_IMG_DIR, ph_safe, os.path.basename(fn))
        if not os.path.isfile(fpath):
            logger.debug('[Paper:Report:Export] Image missing on disk, '
                         'falling back to URL: %s', fpath)
            return attr + origin + url
        try:
            with open(fpath, 'rb') as f:
                raw = f.read()
        except Exception as e:
            logger.warning('[Paper:Report:Export] Image read failed for %s: %s', fpath, e)
            return attr + origin + url
        ext = os.path.splitext(fn)[1].lower()
        mime = 'image/png' if ext == '.png' else 'image/jpeg'
        b64 = base64.b64encode(raw).decode('ascii')
        return f'{attr}data:{mime};base64,{b64}'

    body_html = re.sub(
        r'((?:src|href)=["\'])(/[^"\']+)',
        _embed_paper_image,
        body_html,
    )

    safe_title = (title.replace('&', '&amp;').replace('<', '&lt;')
                       .replace('>', '&gt;').replace('"', '&quot;'))
    css = (
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
        'max-width:820px;margin:32px auto;padding:0 24px;line-height:1.7;color:#222;background:#fff}'
        'h1,h2,h3{margin-top:1.6em;line-height:1.3}'
        'h2{border-bottom:1px solid #eee;padding-bottom:6px}'
        'img{max-width:100%;height:auto;display:block;margin:14px auto;border:1px solid #eaeaea;'
        'border-radius:6px;padding:4px;background:#fff}'
        'pre{background:#f6f8fa;padding:12px 14px;border-radius:6px;overflow:auto;font-size:13px}'
        'code{background:#f1f1f1;padding:1px 5px;border-radius:3px;font-size:90%}'
        'pre code{background:none;padding:0}'
        'blockquote{border-left:3px solid #6366f1;padding-left:12px;margin:8px 0;color:#555}'
        'table{border-collapse:collapse;margin:8px 0;font-size:13px}'
        'th,td{border:1px solid #e0e0e0;padding:6px 10px}th{background:#fafafa}'
        '@media print{body{margin:0;max-width:none}img{break-inside:avoid}h2,h3{break-after:avoid}}'
    )
    # Avoid duplicate H1: the report body itself now starts with `# Title`
    # (prepended in _run_report_task). For older cached reports without it,
    # fall back to the wrapper H1.
    body_starts_with_h1 = bool(re.match(r'\s*<h1\b', body_html))
    title_block = '' if body_starts_with_h1 else f'<h1>{safe_title}</h1>'

    # KaTeX auto-render — paper reports are math-heavy. The client-side
    # reading view uses KaTeX too (lib/static/js/core.js renderMarkdown),
    # so the exported HTML/PDF must match. We load KaTeX from a public CDN
    # so the file works offline (cached) and renders math even when opened
    # by `file://`. ``displayMode: 'block'`` for $$ and ``\[`` only.
    # CDN URLs only (no SRI hashes — they pin the bundle to one version, and
    # cdn.jsdelivr.net already serves over HTTPS with reasonable caching).
    katex_assets = (
        '<link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" '
        'crossorigin="anonymous">'
        '<script defer '
        'src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" '
        'crossorigin="anonymous"></script>'
        '<script defer '
        'src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" '
        'crossorigin="anonymous" '
        'onload="renderMathInElement(document.body,{'
        "delimiters:["
        "{left:'$$',right:'$$',display:true},"
        "{left:'\\\\[',right:'\\\\]',display:true},"
        "{left:'$',right:'$',display:false},"
        "{left:'\\\\(',right:'\\\\)',display:false}"
        "],"
        "throwOnError:false,"
        "errorColor:'#d33'"
        '});window.__katexReady=true;"></script>'
    )

    # PDF flow: bootstrap an auto-print on load (waits for images AND for
    # KaTeX to render so figures + formulas show up in the printed PDF).
    # Standalone HTML download has no print script.
    auto_print_js = (
        '<script>window.addEventListener("load",function(){'
        'function waitKatex(cb){'
        'if(window.__katexReady||!document.querySelector("script[src*=\\"auto-render\\"]"))cb();'
        'else setTimeout(function(){waitKatex(cb);},120);}'
        'var imgs=document.images,pending=imgs.length?0:0;'
        'function go(){setTimeout(function(){'
        'try{window.focus();window.print();}catch(e){}},400);}'
        'function r(){pending--;if(pending<=0)waitKatex(go);}'
        'for(var i=0;i<imgs.length;i++){if(!imgs[i].complete){pending++;'
        'imgs[i].addEventListener("load",r);imgs[i].addEventListener("error",r);}}'
        'if(pending===0)waitKatex(go);'
        '});</script>'
    ) if fmt == 'pdf' else ''
    html_doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{safe_title}</title><style>{css}</style>{katex_assets}{auto_print_js}</head><body>'
        f'{title_block}{body_html}</body></html>'
    )
    if inline_html:
        return Response(html_doc, mimetype='text/html; charset=utf-8')
    return Response(
        html_doc,
        mimetype='text/html; charset=utf-8',
        headers={'Content-Disposition':
                 f'attachment; filename="paper_report_{safe_slug}.html"'},
    )


@api_v1_paper_bp.route('/api/v1/paper/report/cache', methods=['POST'])
async def get_report_cache():
    """Lookup cached report by paper hash.

    Body JSON:
        paper_hash: str — precomputed hash (preferred, avoids re-sending full text)
        paper_text: str — full text of the paper (fallback, used to compute hash)
        lang: str (optional) — language. Default 'en'.
    Returns:
        { ok: true, report: str, paper_hash: str } or { ok: false }
    """
    data = await async_parse_body()
    phash = data.get('paper_hash', '').strip()
    lang = data.get('lang', 'en') or 'en'

    # Prefer pre-computed hash; fall back to computing from text
    if not phash:
        paper_text = data.get('paper_text', '').strip()
        if not paper_text:
            return api_bad_request('No paper_hash or paper_text')
        phash = _paper_hash(paper_text)

    try:
        row = await async_fetchone(
            "SELECT report, meta FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, lang), domain=DOMAIN_CHAT,
        )
        if row and row['report']:
            logger.debug('[Paper:Report:Cache] Hit — hash=%s lang=%s', phash, lang)
            # Server-side enrichment: load the manifest from disk (the client
            # is no longer trusted to forward image URLs).
            images = _load_image_manifest(phash)
            _inj_lang = parse_report_lang(lang)['ui_lang']
            enriched = _inject_images_into_report(row['report'], images, lang=_inj_lang,
                                                  appendix=not is_review_family(lang),
                                                  allow_images=not is_review_family(lang))
            enriched = _ensure_title_heading(enriched, phash)
            enriched = await _append_cached_insight(enriched, phash, lang)
            _cache_meta = _parse_report_meta(row)
            enriched, _cache_meta = await _merge_cached_termfill(
                enriched, _cache_meta, phash, lang)
            return api_ok({'report': enriched, 'paper_hash': phash,
                           'meta': _cache_meta})
    except Exception as e:
        logger.warning('[Paper:Report:Cache] Lookup failed: %s', e)

    return jsonify({'ok': False})


# ══════════════════════════════════════════════════════
#  Agentic Q&A (server-owned TaskRuntime task)
# ══════════════════════════════════════════════════════

@api_v1_paper_bp.route('/api/v1/paper/qa/start', methods=['POST'])
async def start_qa_task():
    """Start a background agentic Q&A task for one question.

    Unlike the legacy stateless ``/api/paper/chat``, this runs a TaskRuntime
    tool-calling loop (web_search / fetch_url) with section-aware context: the
    full generated report + the question-relevant paper sections (no blind
    100k truncation). The frontend polls ``/api/v1/paper/qa/poll``.

    Body JSON:
        question: str — the user's question (required)
        paper_text: str — full parsed paper text (required)
        paper_hash: str (optional) — cache key; computed from text if missing.
        lang: str (optional) — 'zh' for Chinese answer, else 'en'. Default 'en'.
        history: list (optional) — prior [{role, content}, ...] dialogue turns.
        model: str (optional)
        title: str (optional) — client title (race fallback for logging).

    Returns: {ok: true, task_id, paper_hash, running: true}
    """
    data = await async_parse_body()
    question = (data.get('question') or '').strip()
    paper_text = (data.get('paper_text') or '').strip()
    if not question:
        return api_bad_request('No question provided')
    if not paper_text:
        return api_bad_request('No paper_text provided')

    lang = data.get('lang', 'en') or 'en'
    model = data.get('model') or None
    phash = (data.get('paper_hash') or '').strip() or _paper_hash(paper_text)
    history = data.get('history') if isinstance(data.get('history'), list) else []
    client_title = (data.get('title') or '').strip()

    # Look up the generated report for this paper (so the model can answer
    # questions about report-only claims). Best-effort — Q&A still works
    # without a report (model answers from the paper sections alone).
    report_md = ''
    try:
        row = await async_fetchone(
            "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, lang), domain=DOMAIN_CHAT,
        )
        if row and row['report']:
            report_md = row['report']
        else:
            # Fall back to the report in the other language if the requested
            # one isn't generated yet — a report in any language still helps.
            row2 = await async_fetchone(
                "SELECT report FROM paper_reports WHERE paper_hash = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (phash,), domain=DOMAIN_CHAT,
            )
            if row2 and row2['report']:
                report_md = row2['report']
    except Exception as e:
        logger.warning('[Paper:QA] Report lookup failed for hash=%s (Q&A continues '
                       'without report): %s', phash, e)

    messages, diag = build_qa_messages(
        question, paper_text, report_md, history=history, lang=lang)

    task_id = f'qa_{int(time.time() * 1000)}_{phash[:8]}_{lang}'
    task = _new_qa_task(task_id, phash, lang, model,
                        question=question, client_title=client_title)
    logger.info('[Paper:QA] Starting task %s — hash=%s lang=%s sections=%d/%d '
                'report=%s q=%.80s',
                task_id, phash, lang, diag['n_sections_selected'],
                diag['n_sections_total'], diag['report_present'], question)
    _qa_runtime.spawn(task_id, _run_qa_task, task, messages)

    return jsonify({
        'ok': True, 'task_id': task_id, 'paper_hash': phash,
        'running': True, 'reportPresent': diag['report_present'],
    })


@api_v1_paper_bp.route('/api/v1/paper/qa/poll', methods=['GET'])
async def poll_qa_task():
    """Poll a Q&A task for new events (same shape as the report poll).

    Query params: task_id, cursor (default 0).
    Returns: {ok, status, events, next_cursor, paper_hash, answer? (if done)}.
    """
    task_id = request.args.get('task_id', '').strip()
    try:
        cursor = int(request.args.get('cursor', 0))
    except (ValueError, TypeError) as e:
        logger.debug('[Paper:QA:Poll] bad cursor: %s', e)
        cursor = 0
    if not task_id:
        return api_bad_request('task_id required')

    task = _qa_runtime.get(task_id)
    if not task:
        logger.debug('[Paper:QA:Poll] Unknown task_id=%s', task_id)
        return api_not_found('task not found (may have expired)')

    with task['events_lock']:
        total = len(task['events'])
        cursor = max(0, min(cursor, total))
        new_events = list(task['events'][cursor:])

    resp = {
        'ok': True,
        'status': task['status'],
        'events': new_events,
        'next_cursor': total,
        'paper_hash': task['paper_hash'],
    }
    if task['status'] == 'done':
        resp['answer'] = task.get('full_text', '')
    if task['status'] == 'error':
        resp['error'] = task.get('error', '')
    return jsonify(resp)


@api_v1_paper_bp.route('/api/v1/paper/translate/start', methods=['POST'])
async def start_translate_task():
    """Start (or join) a Babel-mode whole-paper translation task.

    Body JSON:
        paper_text: str
        lang: str — target language (e.g. 'zh', 'en', 'ja')
        paper_hash: str (optional) — used as cache key; computed if missing.
        model: str (optional)
        force: bool (optional)
    """
    data = await async_parse_body()
    paper_text = (data.get('paper_text') or '').strip()
    lang = (data.get('lang') or '').strip()
    if not paper_text:
        return api_bad_request('No paper_text')
    if not lang:
        return api_bad_request('lang required')

    phash = (data.get('paper_hash') or '').strip() or _paper_hash(paper_text)
    model = data.get('model') or None
    force = bool(data.get('force'))

    if not force:
        try:
            row = await async_fetchone(
                'SELECT text FROM paper_translations WHERE paper_hash=? AND lang=?',
                (phash, lang), domain=DOMAIN_CHAT,
            )
            if row and row['text']:
                logger.info('[Paper:Translate] DB cache hit — hash=%s lang=%s %d chars',
                            phash, lang, len(row['text']))
                return api_ok({'cached': True,
                                'text': row['text'], 'paper_hash': phash})
        except Exception as e:
            logger.warning('[Paper:Translate] Cache lookup failed: %s', e)

    existing = _translate_index_get(phash, lang)
    if existing and not force and existing['status'] in ('pending', 'running', 'done'):
        return api_ok({'task_id': existing['task_id'],
                        'paper_hash': phash, 'existed': True,
                        'running': existing['status'] in ('pending', 'running')})
    if existing and force:
        existing['abort_event'].set()
        existing['status'] = 'error'
        existing['finished_at'] = time.time()

    # The task_id is an OPAQUE handle echoed back verbatim in the poll/abort
    # URL — it must be URL-safe. A composite review key (e.g. 'review:neurips:zh')
    # carries colons that, over a proxy tunnel that re-encodes '%', arrive
    # double-encoded ('%253A') and never match the runtime's dict key → the poll
    # 404s forever and the UI reports "translation failed". Sanitize the lang
    # segment for the id only; the real composite `lang` still keys the cache,
    # dedup index, and DB row unchanged.
    lang_slug = re.sub(r'[^A-Za-z0-9]+', '_', lang).strip('_') or 'x'
    task_id = f'tr_{int(time.time() * 1000)}_{phash[:8]}_{lang_slug}'
    task = _new_translate_task(task_id, phash, lang, model)

    _translate_runtime.spawn(task_id, _run_translate_task, task, paper_text)

    return api_ok({'task_id': task_id, 'paper_hash': phash,
                    'running': True, 'existed': False})


@api_v1_paper_bp.route('/api/v1/paper/translate/poll', methods=['GET'])
async def poll_translate_task():
    """Poll a translation task for new events."""
    task_id = request.args.get('task_id', '').strip()
    try:
        cursor = int(request.args.get('cursor', 0))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[paper] poll_translate_task caught %s: %s', type(_e_audit).__name__, _e_audit)
        cursor = 0
    if not task_id:
        return api_bad_request('task_id required')

    task = _translate_runtime.get(task_id)
    if not task:
        return api_not_found('task not found (expired?)')

    with task['events_lock']:
        total = len(task['events'])
        cursor = max(0, min(cursor, total))
        new_events = list(task['events'][cursor:])

    resp = {
        'ok': True,
        'status': task['status'],
        'events': new_events,
        'next_cursor': total,
        'paper_hash': task['paper_hash'],
        'progress': dict(task['progress']),
    }
    if task['status'] == 'done':
        resp['text'] = task.get('full_text', '')
    if task['status'] == 'error':
        resp['error'] = task.get('error', '')
    return jsonify(resp)


@api_v1_paper_bp.route('/api/v1/paper/translate/lookup', methods=['POST'])
async def lookup_translate_task():
    data = await async_parse_body()
    phash = (data.get('paper_hash') or '').strip()
    lang = (data.get('lang') or '').strip()
    if not phash or not lang:
        return api_bad_request('paper_hash and lang required')
    task = _translate_index_get(phash, lang)
    if task:
        return api_ok({'task_id': task['task_id'],
                        'status': task['status'], 'paper_hash': phash})
    return jsonify({'ok': False})


@api_v1_paper_bp.route('/api/v1/paper/translate/cache', methods=['POST'])
async def get_translate_cache():
    data = await async_parse_body()
    phash = (data.get('paper_hash') or '').strip()
    lang = (data.get('lang') or '').strip()
    if not phash:
        paper_text = (data.get('paper_text') or '').strip()
        if not paper_text:
            return api_bad_request('paper_hash or paper_text required')
        phash = _paper_hash(paper_text)
    if not lang:
        return api_bad_request('lang required')
    try:
        row = await async_fetchone(
            'SELECT text FROM paper_translations WHERE paper_hash=? AND lang=?',
            (phash, lang), domain=DOMAIN_CHAT,
        )
        if row and row['text']:
            return api_ok({'text': row['text'], 'paper_hash': phash})
    except Exception as e:
        logger.warning('[Paper:Translate:Cache] Lookup failed: %s', e)
    return jsonify({'ok': False})


@api_v1_paper_bp.route('/api/v1/paper/search-arxiv', methods=['POST'])
async def search_arxiv_route():
    """Search arXiv by free-text title / keyword query.

    Body JSON:
        query: str — paper title, keywords, or author names
        max_results: int (optional, default 10, capped at 25)
    Returns:
        { ok: true, query: str, results: [
            { arxiv_id, title, authors: [str], summary, published,
              primary_category, pdf_url, abs_url } ] }
    """
    data = await async_parse_body()
    query = (data.get('query') or '').strip()
    if not query:
        logger.warning('[Paper:arXiv:Search] Empty query')
        return api_bad_request('No query provided')

    try:
        max_results = int(data.get('max_results') or 10)
    except (ValueError, TypeError) as e:
        logger.debug('[Paper:arXiv:Search] non-int max_results (%s) — defaulting to 10', e)
        max_results = 10

    results = await asyncio.to_thread(search_arxiv, query, max_results)
    return api_ok({'query': query, 'results': results})


@api_v1_paper_bp.route('/api/v1/paper/recommend', methods=['POST'])
async def recommend_papers_route():
    """Recommend real arXiv papers from a fuzzy free-text description.

    An LLM interprets the description; every surfaced card is verified against
    real arXiv (see ``lib.paper.recommend_engine``) so a hallucinated title is
    never returned. When the description encodes a false premise, a grounded
    ``correction`` block is included.

    Body JSON:
        description: str — free-text description of the paper(s) recalled
        max_results: int (optional, default 6, capped at 12)
    Returns:
        { ok: true, query: str, llmError: bool,
          correction: { note: str, paper: <card>|null } | null,
          results: [ { arxiv_id, title, authors, summary, published,
                       primary_category, pdf_url, abs_url, why, venue } ] }
    """
    data = await async_parse_body()
    description = (data.get('description') or '').strip()
    if not description:
        logger.warning('[Paper:Recommend] Empty description')
        return api_bad_request('No description provided')

    try:
        max_results = int(data.get('max_results') or 6)
    except (ValueError, TypeError) as e:
        logger.debug('[Paper:Recommend] non-int max_results (%s) — defaulting to 6', e)
        max_results = 6

    out = await asyncio.to_thread(recommend_papers, description, max_results)
    return api_ok(out)


@api_v1_paper_bp.route('/api/v1/paper/recommend/start', methods=['POST'])
async def start_recommend_task():
    """Start a background STREAMING describe-to-recommend task.

    Same grounded-only contract as the blocking ``/recommend`` route, but the
    two-phase pipeline (LLM interpretation → per-candidate arXiv grounding) is
    run as a server-owned TaskRuntime task so the frontend can reveal each
    grounded card the instant it resolves. Poll ``/api/v1/paper/recommend/poll``
    (mirrors the Q&A transport — no SSE). Grounding is metadata-only
    (``search_arxiv`` / ``fetch_arxiv_title``): it never triggers a PDF fetch.

    Body JSON:
        description: str — free-text description of the paper(s) recalled
        max_results: int (optional, default 6, capped at 12)
    Returns: { ok: true, task_id, running: true }
    """
    data = await async_parse_body()
    description = (data.get('description') or '').strip()
    if not description:
        logger.warning('[Paper:Recommend] Empty description (stream)')
        return api_bad_request('No description provided')

    try:
        max_results = int(data.get('max_results') or 6)
    except (ValueError, TypeError) as e:
        logger.debug('[Paper:Recommend] non-int max_results (stream) (%s) — defaulting to 6', e)
        max_results = 6

    task_id = f'rec_{int(time.time() * 1000)}_{_recommend_key(description)}'
    task = _new_recommend_task(task_id, description, max_results)
    logger.info('[Paper:Recommend] Starting stream task %s — max=%d desc=%.80s',
                task_id, max_results, description)
    _recommend_runtime.spawn(task_id, _run_recommend_task, task)

    return jsonify({'ok': True, 'task_id': task_id, 'running': True})


@api_v1_paper_bp.route('/api/v1/paper/recommend/poll', methods=['GET'])
async def poll_recommend_task():
    """Poll a streaming recommend task for new events (same shape as QA poll).

    Query params: task_id, cursor (default 0).
    Returns: {ok, status, events, next_cursor, results? / correction? (if done)}.
    """
    task_id = request.args.get('task_id', '').strip()
    try:
        cursor = int(request.args.get('cursor', 0))
    except (ValueError, TypeError) as e:
        logger.debug('[Paper:Recommend:Poll] bad cursor: %s', e)
        cursor = 0
    if not task_id:
        return api_bad_request('task_id required')

    task = _recommend_runtime.get(task_id)
    if not task:
        logger.debug('[Paper:Recommend:Poll] Unknown task_id=%s', task_id)
        return api_not_found('task not found (may have expired)')

    with task['events_lock']:
        total = len(task['events'])
        cursor = max(0, min(cursor, total))
        new_events = list(task['events'][cursor:])

    resp = {
        'ok': True,
        'status': task['status'],
        'events': new_events,
        'next_cursor': total,
    }
    if task['status'] == 'done':
        resp['results'] = task.get('results', [])
        resp['correction'] = task.get('correction')
        resp['llmError'] = bool(task.get('llmError'))
    if task['status'] == 'error':
        resp['error'] = task.get('error', '')
        resp['llmError'] = bool(task.get('llmError'))
    return jsonify(resp)


@api_v1_paper_bp.route('/api/v1/paper/recommend/abort', methods=['POST'])
async def abort_recommend_task():
    """Abort a running streaming recommend task (best-effort cooperative stop)."""
    data = await async_parse_body()
    task_id = (data.get('task_id') or '').strip()
    if not task_id:
        return api_bad_request('task_id required')
    task = _recommend_runtime.get(task_id)
    if not task:
        return api_not_found('task not found')
    task['abort_event'].set()
    logger.info('[Paper:Recommend] Abort requested for task %s', task_id)
    _cleanup_stale_recommend_tasks()
    return api_ok({'aborted': True})


@api_v1_paper_bp.route('/api/v1/paper/fetch-arxiv', methods=['POST'])
async def fetch_arxiv():
    """Download PDF from arXiv URL and serve it locally.

    Body JSON:
        url: str — arXiv URL (abs page, pdf link, or just the ID like 2301.12345)
    Returns:
        { ok: true, pdf_url: str, title: str, arxiv_id: str }
    """
    data = await async_parse_body()
    url_input = data.get('url', '').strip()
    if not url_input:
        logger.warning('[Paper:arXiv] Fetch request with no URL')
        return api_bad_request('No URL provided')

    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        logger.warning('[Paper:arXiv] Could not parse arXiv ID from: %.200s', url_input)
        return api_bad_request('Could not parse arXiv ID from URL')

    pdf_url = f'https://arxiv.org/pdf/{arxiv_id}.pdf'
    filename = f'arxiv_{arxiv_id.replace("/", "_")}.pdf'
    filepath = os.path.join(PAPER_DIR, filename)

    if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
        file_size = os.path.getsize(filepath)
        logger.info('[Paper:arXiv] Cache hit for %s — %d bytes at %s', arxiv_id, file_size, filepath)
        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'cached': True,
        })

    # Blocking network download + disk write — offload off the event loop.
    def _download():
        logger.info('[Paper:arXiv] Downloading PDF: %s', pdf_url)
        t0 = time.time()
        resp = http_get(pdf_url, timeout=60, stream=True,
                        headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' not in content_type and 'octet-stream' not in content_type:
            logger.warning('[Paper:arXiv] Unexpected content type: %s for %s', content_type, pdf_url)

        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        chunks = []
        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                f.write(chunk)
                chunks.append(chunk)

        # Validity gate: reject a truncated/aborted download that left a stub.
        from lib.pdf_parser import validate_pdf_bytes as _validate_pdf_bytes
        _ok, _np, _verr = _validate_pdf_bytes(b''.join(chunks))
        if not _ok:
            try:
                os.remove(filepath)
            except OSError as e:
                logger.debug('[Paper:arXiv] cleanup of rejected %s failed: %s', filepath, e)
            raise ValueError('Downloaded file is not a readable PDF: ' + _verr)

        size = os.path.getsize(filepath)
        elapsed = time.time() - t0
        logger.info('[Paper:arXiv] Downloaded %s: %d bytes in %.1fs', arxiv_id, size, elapsed)
        return size

    try:
        file_size = await asyncio.to_thread(_download)
        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'file_size': file_size,
        })

    except ValueError as e:
        logger.warning('[Paper:arXiv] Rejected invalid PDF for %s: %s', arxiv_id, e)
        return api_bad_request(str(e))
    except _requests.Timeout:
        logger.warning('[Paper:arXiv] Download timeout (60s): %s', pdf_url)
        return api_error('Download timed out (60s)', status=504)
    except _requests.RequestException as e:
        logger.warning('[Paper:arXiv] Download failed: %s — %s', pdf_url, e)
        return api_error(f'Download failed: {str(e)}', status=502)


@paper_bp.route('/api/paper/fetch-arxiv-stream', methods=['POST'])
async def fetch_arxiv_stream():
    """Download PDF from arXiv and parse it — SSE stream of progress events.

    Body JSON:
        url: str — arXiv URL or ID

    SSE events (each one JSON on a ``data:`` line):
        {stage: 'resolve', arxiv_id: str, title: str, pdf_url: str}  — URL parsed
        {stage: 'download', downloaded: int, total: int}  — download progress
        {stage: 'download_done', file_size: int, elapsed: float}
        {stage: 'parse_start'}
        {stage: 'parse_done', total_pages: int, text_length: int, elapsed: float}
        {stage: 'done', ok: true, pdf_url: str, arxiv_id: str, title: str,
               parsed_text: str, total_pages: int, text_length: int, cached: bool}
        {stage: 'error', error: str}
    """
    data = await async_parse_body()
    url_input = (data.get('url') or '').strip()
    # Client-generated bookshelf id — the server persists the library row itself
    # at the 'done' stage (server-authoritative ingest) so a fetched paper
    # survives a tab-close/refresh that races the client PUT.
    client_paper_id = (data.get('paper_id') or '').strip()
    if not url_input:
        logger.warning('[Paper:arXiv:Stream] Fetch request with no URL')
        return api_bad_request('No URL provided')

    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        logger.warning('[Paper:arXiv:Stream] Could not parse arXiv ID from: %.200s', url_input)
        return api_bad_request('Could not parse arXiv ID from URL')

    pdf_url = f'https://arxiv.org/pdf/{arxiv_id}.pdf'
    filename = f'arxiv_{arxiv_id.replace("/", "_")}.pdf'
    filepath = os.path.join(PAPER_DIR, filename)

    def _sse(obj):
        return f'data: {json.dumps(obj)}\n\n'

    def generate():
        # SSE padding: flush proxy/gateway buffers (VSCode port-forward, nginx, etc.)
        # so the first real event reaches the client immediately. Without this,
        # small events (~60B each) get buffered and the UI appears stuck on the
        # initial 'resolve' state until the buffer fills. See also trading_brain.py.
        yield ':' + (' ' * 2048) + '\n\n'
        yield ':' + (' ' * 2048) + '\n\n'
        # Resolve the real paper title up front so the UI can label the
        # paper by title instead of the bare arXiv ID. Best-effort: an empty
        # string just falls back to "arXiv:<id>" on the client.
        paper_title = fetch_arxiv_title(arxiv_id)
        yield _sse({'stage': 'resolve', 'arxiv_id': arxiv_id,
                    'title': paper_title,
                    'pdf_url': f'/api/paper/pdf/{filename}'})

        # ── Step 1: Download PDF (cached or fresh) ──
        pdf_bytes = None
        cached = False
        try:
            if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                cached = True
                with open(filepath, 'rb') as f:
                    pdf_bytes = f.read()
                file_size = len(pdf_bytes)
                logger.info('[Paper:arXiv:Stream] Cache hit for %s — %d bytes', arxiv_id, file_size)
                yield _sse({'stage': 'download_done', 'file_size': file_size,
                            'elapsed': 0.0, 'cached': True})
            else:
                logger.info('[Paper:arXiv:Stream] Downloading PDF: %s', pdf_url)
                t0 = time.time()
                resp = http_get(pdf_url, timeout=60, stream=True,
                                headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
                resp.raise_for_status()
                content_type = resp.headers.get('Content-Type', '')
                if 'pdf' not in content_type and 'octet-stream' not in content_type:
                    logger.warning('[Paper:arXiv:Stream] Unexpected content type: %s for %s',
                                   content_type, pdf_url)

                total = 0
                try:
                    total = int(resp.headers.get('Content-Length') or 0)
                except (ValueError, TypeError) as e:
                    logger.debug('[Paper:arXiv:Stream] Bad Content-Length: %s', e)

                downloaded = 0
                last_progress_ts = 0.0
                chunks = []
                # Re-ensure PAPER_DIR (FUSE/cross-DC mounts can drop it after
                # the import-time makedirs) so the write can't ENOENT.
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=32768):
                        if not chunk:
                            continue
                        f.write(chunk)
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        # Emit at most ~10 progress events per second
                        now = time.time()
                        if now - last_progress_ts >= 0.1:
                            last_progress_ts = now
                            yield _sse({'stage': 'download',
                                        'downloaded': downloaded,
                                        'total': total})
                pdf_bytes = b''.join(chunks)
                file_size = len(pdf_bytes)
                elapsed = time.time() - t0
                logger.info('[Paper:arXiv:Stream] Downloaded %s: %d bytes in %.1fs',
                            arxiv_id, file_size, elapsed)
                yield _sse({'stage': 'download_done', 'file_size': file_size,
                            'elapsed': round(elapsed, 2), 'cached': False})
        except _requests.Timeout:
            logger.warning('[Paper:arXiv:Stream] Download timeout (60s): %s', pdf_url)
            yield _sse({'stage': 'error', 'error': 'Download timed out (60s)'})
            return
        except _requests.RequestException as e:
            logger.warning('[Paper:arXiv:Stream] Download failed: %s — %s', pdf_url, e)
            yield _sse({'stage': 'error', 'error': f'Download failed: {e}'})
            return
        except OSError as e:
            logger.error('[Paper:arXiv:Stream] Disk write failed for %s: %s',
                         filepath, e, exc_info=True)
            yield _sse({'stage': 'error', 'error': f'Disk write failed: {e}'})
            return

        # ── Step 2: Parse PDF text on server (no second client round-trip) ──
        if not pdf_bytes:
            logger.warning('[Paper:arXiv:Stream] No PDF bytes after download for %s', arxiv_id)
            yield _sse({'stage': 'error', 'error': 'PDF body was empty after download'})
            return

        # Validity gate: an aborted/blocked download can leave a truncated stub
        # that exists on disk yet is not an openable PDF. Reject it (delete the
        # file, emit error) so it never gets parsed or seeded as a ghost row.
        from lib.pdf_parser import validate_pdf_bytes as _validate_pdf_bytes
        _ok, _np, _verr = _validate_pdf_bytes(pdf_bytes)
        if not _ok:
            logger.warning('[Paper:arXiv:Stream] Rejected invalid PDF for %s (%d bytes): %s',
                           arxiv_id, file_size, _verr)
            try:
                os.remove(filepath)
            except OSError as e:
                logger.debug('[Paper:arXiv:Stream] cleanup of rejected %s failed: %s', filepath, e)
            yield _sse({'stage': 'error',
                        'error': 'Downloaded file is not a readable PDF (truncated or '
                                 'corrupted): ' + _verr})
            return

        yield _sse({'stage': 'parse_start'})
        try:
            from lib.pdf_parser import parse_pdf as _parse_pdf
            import queue as _queue
            import threading as _threading

            # Run the blocking parse in a worker thread and bridge its
            # per-page progress callback to SSE events via a queue. This
            # turns pymupdf4llm's opaque multi-second call into a
            # streaming "page N/M" progress bar in the UI.
            progress_q: "_queue.Queue" = _queue.Queue()
            result_holder = {'result': None, 'error': None}

            def _on_progress(stage, done, total):
                progress_q.put(('progress', stage, done, total))

            def _worker():
                try:
                    result_holder['result'] = _parse_pdf(
                        pdf_bytes, max_text_chars=0, max_images=0,
                        progress_callback=_on_progress,
                    )
                except Exception as ex:
                    # Surface the failure via shared state — parent thread
                    # logs and re-raises with full context.
                    logger.debug('[Paper] PDF parse worker captured exception: %s', ex)
                    result_holder['error'] = ex
                finally:
                    progress_q.put(('done', None, None, None))

            t0 = time.time()
            worker = _threading.Thread(target=_worker,
                                       name=f'pdf-parse-{arxiv_id}',
                                       daemon=True)
            worker.start()

            last_emit = 0.0
            last_done = -1
            while True:
                try:
                    msg = progress_q.get(timeout=1.0)
                except _queue.Empty:
                    # Heartbeat comment — keeps connection alive through
                    # proxies during a long silent stretch.
                    yield ':hb\n\n'
                    continue
                kind = msg[0]
                if kind == 'done':
                    break
                _, stage, done, total = msg
                # Throttle: emit at most ~10 events/sec, but always emit
                # the first and last page.
                now = time.time()
                is_last = (total and done >= total)
                if (now - last_emit >= 0.1 or is_last or last_done < 0) and done != last_done:
                    last_emit = now
                    last_done = done
                    yield _sse({'stage': 'parse_progress',
                                'parse_stage': stage,
                                'page': done,
                                'total_pages': total})

            worker.join(timeout=5.0)
            if result_holder['error'] is not None:
                raise result_holder['error']
            result = result_holder['result'] or {}
            elapsed = time.time() - t0
            parsed_text = result.get('text') or ''
            total_pages = result.get('totalPages', 0)
            text_length = result.get('textLength', len(parsed_text))
            logger.info('[Paper:arXiv:Stream] Parsed %s — %d pages, %d chars in %.1fs',
                        arxiv_id, total_pages, text_length, elapsed)
            yield _sse({'stage': 'parse_done',
                        'total_pages': total_pages,
                        'text_length': text_length,
                        'elapsed': round(elapsed, 2)})
        except Exception as e:
            # Parsing failed — still return the PDF URL so the viewer can render it,
            # but surface the error so the UI can warn the user.
            logger.error('[Paper:arXiv:Stream] PDF parse failed for %s: %s',
                         arxiv_id, e, exc_info=True)
            yield _sse({'stage': 'done', 'ok': True,
                        'pdf_url': f'/api/paper/pdf/{filename}',
                        'arxiv_id': arxiv_id,
                        'title': paper_title,
                        'parsed_text': '',
                        'total_pages': 0,
                        'text_length': 0,
                        'paper_hash': '',
                        'images': [],
                        'cached': cached,
                        'parse_error': f'PDF parse failed: {e}'})
            return

        # ── Step 3: Extract figure/table images (server-side, before
        #     handing control back to the client — eliminates the race where
        #     the user clicks Report before background extraction finishes).
        phash = _paper_hash(parsed_text) if parsed_text else ''
        images = []
        if phash:
            yield _sse({'stage': 'extract_start'})
            t_ex = time.time()
            try:
                images = _extract_paper_figures(filepath, phash)
            except Exception as e:
                logger.warning('[Paper:arXiv:Stream] Image extraction failed: %s', e)
            yield _sse({'stage': 'extract_done',
                        'images_count': len(images),
                        'elapsed': round(time.time() - t_ex, 2)})

        # Server-authoritative persist: write the bookshelf row before handing
        # control back, so the fetched paper survives even if the client's PUT
        # never lands (tab closed mid-stream). Runs on the SSE generator thread.
        if client_paper_id:
            _persist_ingested_library_row(
                client_paper_id, title=(paper_title or f'arXiv:{arxiv_id}'),
                pdf_url=f'/api/paper/pdf/{filename}', pdf_filename=filename,
                arxiv_id=arxiv_id, paper_hash=phash, parsed_text=parsed_text,
                images=images, page_count=total_pages)

        # ── Done — return everything the client needs ──
        yield _sse({'stage': 'done', 'ok': True,
                    'pdf_url': f'/api/paper/pdf/{filename}',
                    'arxiv_id': arxiv_id,
                    'title': paper_title,
                    'parsed_text': parsed_text,
                    'total_pages': total_pages,
                    'text_length': text_length,
                    'paper_hash': phash,
                    'images': images,
                    'cached': cached})

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Content-Encoding': 'identity'})


def _stream_file_response(filepath, mimetype, chunk_size=262144):
    """Stream a file from disk in fixed chunks, honouring Range ourselves.

    FALLBACK for when a buffering cloud-IDE proxy defeats ``send_file``'s ranged
    serving (i.e. the transport log shows one ``range=False -> 200`` full GET
    instead of many ``range=True -> 206``). Instead of handing the proxy a
    single tens-of-MB body it can buffer into a timeout, we yield the bytes in
    ``chunk_size`` pieces through a ``Response`` generator (the same proven
    sync-generator pattern the SSE endpoints use) and set the anti-buffering
    headers from the proxy-buffering lesson: ``no-transform`` +
    ``Content-Encoding: identity`` + ``X-Accel-Buffering: no``. We also parse
    ``Range`` manually so this path stays range-capable (206 with the exact
    slice) when the proxy DOES forward Range.

    Dormant by default — wired in only when ``TOFU_PAPER_PDF_STREAM=1`` so a
    single-box install stays byte-identical to the ``send_file`` path.
    """
    file_size = os.path.getsize(filepath)
    start, end = 0, file_size - 1
    status = 200
    m = re.match(r'bytes=(\d*)-(\d*)$', request.headers.get('Range', '') or '')
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
        else:  # suffix range: bytes=-N → last N bytes
            start = max(0, file_size - int(m.group(2)))
            end = file_size - 1
        start = max(0, start)
        end = min(end, file_size - 1)
        if start > end:
            resp = Response(status=416)
            resp.headers['Content-Range'] = 'bytes */%d' % file_size
            return resp
        status = 206
    length = end - start + 1

    def generate():
        remaining = length
        with open(filepath, 'rb') as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate(), status=status, mimetype=mimetype)
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Length'] = str(length)
    resp.headers['Cache-Control'] = 'public, max-age=43200, no-transform'
    resp.headers['Content-Encoding'] = 'identity'
    resp.headers['X-Accel-Buffering'] = 'no'
    if status == 206:
        resp.headers['Content-Range'] = 'bytes %d-%d/%d' % (start, end, file_size)
    return resp


@paper_bp.route('/api/paper/pdf/<filename>')
def serve_paper_pdf(filename):
    """Serve a downloaded paper PDF.

    SKIPPED from the native-async conversion for the same reason as
    ``serve_paper_image`` — its only blocking call is the sync-safe
    ``send_file`` shim, which would deadlock the event loop if invoked from
    an ``async def`` handler. No DB / no body parse here.
    """
    filename = os.path.basename(filename)
    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.debug('[Paper] PDF not found: %s', filename)
        return api_not_found('PDF not found')
    # FALLBACK (opt-in): if the transport log proves the proxy buffers the
    # whole-file 200 (single ``range=False -> 200``), flip TOFU_PAPER_PDF_STREAM=1
    # to serve the PDF as a chunked generator the proxy can't buffer into a
    # timeout. Default off → byte-identical to the send_file path below.
    if os.environ.get('TOFU_PAPER_PDF_STREAM') == '1':
        resp = _stream_file_response(filepath, 'application/pdf')
        logger.info('[Paper] serve pdf=%s range=%s -> %s (stream)',
                    filename, bool(request.headers.get('Range')), resp.status_code)
        return resp
    # conditional=True → make_conditional(accept_ranges=True): honour HTTP
    # Range so pdf.js can range-load a large PDF in small chunks. Without it
    # send_file always returns 200 + the whole file (tens of MB); a buffering
    # cloud-IDE proxy can truncate/time-out that single response, which pdf.js
    # surfaces as "Missing PDF" or per-page "failed to render".
    resp = send_file(filepath, mimetype='application/pdf', conditional=True)
    # Advertise ranged capability on the INITIAL (non-Range) 200 too. pdf.js's
    # validateRangeRequestCapabilities only switches to ranged loading when the
    # FIRST response carries ``Accept-Ranges: bytes`` — Quart's make_conditional
    # sets it only on the 206 (Range-present) path, so without this the viewer
    # does one giant full GET and conditional=True is inert for it.
    resp.headers.setdefault('Accept-Ranges', 'bytes')
    # Transport diagnostic (acceptance gate): after a restart+refresh, opening a
    # large PDF through the proxy should log many ``range=True -> 206`` lines
    # (pdf.js is range-loading and the proxy passes it through). A single
    # ``range=False -> 200`` means the proxy did one buffered full GET → ranged
    # loading is moot and we fall back to chunked streaming (see _stream_pdf).
    logger.info('[Paper] serve pdf=%s range=%s -> %s',
                filename, bool(request.headers.get('Range')), resp.status_code)
    return resp


@api_v1_paper_bp.route('/api/v1/paper/reparse', methods=['POST'])
async def reparse_paper():
    """Re-parse an already-stored paper PDF to recover its text.

    Used to recover library entries that were saved before server-side parsing
    (or whose parse step failed). Given a filename already under PAPER_DIR,
    reads it and returns extracted text + page count.

    Body JSON:
        filename: str — basename of the PDF under PAPER_DIR

    Returns:
        { ok: true, text: str, total_pages: int, text_length: int }
    """
    data = await async_parse_body()
    filename = os.path.basename((data.get('filename') or '').strip())
    if not filename:
        logger.warning('[Paper:Reparse] No filename provided')
        return api_bad_request('No filename')

    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.warning('[Paper:Reparse] PDF not found: %s', filename)
        return api_not_found('PDF not found')

    # Blocking read + pymupdf parse — offload off the event loop.
    def _reparse():
        with open(filepath, 'rb') as f:
            pdf_bytes = f.read()
        from lib.pdf_parser import parse_pdf as _parse_pdf
        t0 = time.time()
        result = _parse_pdf(pdf_bytes, max_text_chars=0, max_images=0)
        elapsed = time.time() - t0
        text = result.get('text') or ''
        total_pages = result.get('totalPages', 0)
        text_length = result.get('textLength', len(text))
        logger.info('[Paper:Reparse] %s — %d pages, %d chars in %.1fs',
                    filename, total_pages, text_length, elapsed)
        return text, total_pages, text_length

    try:
        text, total_pages, text_length = await asyncio.to_thread(_reparse)
        return jsonify({
            'ok': True,
            'text': text,
            'total_pages': total_pages,
            'text_length': text_length,
        })
    except Exception as e:
        logger.error('[Paper:Reparse] Failed for %s: %s', filename, e, exc_info=True)
        return api_internal_error(f'Reparse failed: {e}')


# A PDF at or above this size is assumed real and never re-opened during a
# listing (validating every large PDF on every list would be wasteful). Only
# small files — plausible truncation stubs like the 15-byte ``%PDF-1.4`` header
# — are validity-checked. Generous vs the ~15-byte stubs actually seen.
_GHOST_PDF_MAX_STUB_BYTES = 2048


def _is_ghost_library_row(paper):
    """A bookshelf row is a GHOST (non-viewable) when it has no usable PDF:
    an empty ``pdfFilename``, or a filename whose file is missing from
    PAPER_DIR. Left by the OLD fire-and-forget persistence (a client PUT that
    raced/replaced a failed upload). A transient stat error (FUSE hiccup) is
    treated as NOT-ghost so a real paper is never hidden by a flaky mount.

    EXCEPTION — a saved *recommendation* is a legitimate empty-PDF row: it has
    no ``pdfFilename`` yet (never ingested) but carries an ``arxivId``, which
    makes it re-openable via lazy ingest. Keep it, otherwise the auto-persisted
    describe-to-recommend cards would silently vanish on reload.
    """
    fn = (paper.get('pdfFilename') or '').strip()
    if not fn:
        if (paper.get('arxivId') or '').strip():
            return False
        return True
    try:
        path = os.path.join(PAPER_DIR, os.path.basename(fn))
        if not os.path.exists(path):
            return True
        # File is present — but a truncated / aborted upload leaves a stub
        # (e.g. a 15-byte ``%PDF-1.4`` header) that EXISTS yet is not an
        # openable PDF. Such a row dead-ends the reader on "load a PDF first",
        # so treat a present-but-unopenable PDF as a ghost too. Only stubs small
        # enough to be a plausible truncation are validated (a large real PDF is
        # never re-opened on every listing — that would be needless work and a
        # transient FUSE read error must not hide a real paper).
        try:
            size = os.path.getsize(path)
        except OSError:
            return False  # transient stat error — never hide a real paper
        if size < _GHOST_PDF_MAX_STUB_BYTES:
            from lib.pdf_parser import validate_pdf_bytes
            with open(path, 'rb') as f:
                ok, _pages, _err = validate_pdf_bytes(f.read())
            if not ok:
                logger.debug('[Paper:Library] row %s has a present-but-invalid PDF '
                             '(%d bytes) — treating as ghost',
                             (paper.get('id') or '')[:16], size)
                return True
        return False
    except OSError as e:
        logger.debug('[Paper:Library] pdf existence check failed for %s: %s',
                     (paper.get('id') or '')[:16], e)
        return False


def _is_broken_stub_row(paper):
    """A row that is DEFINITIVELY broken and safe to hard-delete: its
    ``pdfFilename`` points at a file that is PRESENT on disk, small, and fails
    ``validate_pdf_bytes`` (a truncated / aborted-upload stub — e.g. the 15-byte
    ``%PDF-1.4`` header). Deliberately NARROWER than ``_is_ghost_library_row``:
    it does NOT include a MISSING file (which can be a transient FUSE hiccup) nor
    an empty-pdfFilename recommendation row — only a proven-unopenable file. Used
    by the opt-in prune endpoint so a destructive cleanup can never remove a row
    that might still be a real (transiently-unreachable) paper.
    """
    fn = (paper.get('pdfFilename') or '').strip()
    if not fn:
        return False
    try:
        path = os.path.join(PAPER_DIR, os.path.basename(fn))
        if not os.path.exists(path):
            return False  # missing != broken (could be a flaky mount)
        size = os.path.getsize(path)
        if size >= _GHOST_PDF_MAX_STUB_BYTES:
            return False  # a large file is not a truncation stub
        from lib.pdf_parser import validate_pdf_bytes
        with open(path, 'rb') as f:
            ok, _pages, _err = validate_pdf_bytes(f.read())
        return not ok
    except OSError as e:
        logger.debug('[Paper:Library] stub check failed for %s: %s',
                     (paper.get('id') or '')[:16], e)
        return False


def _persist_ingested_library_row(paper_id, *, title, pdf_url, pdf_filename,
                                  arxiv_id, paper_hash, parsed_text, images,
                                  page_count):
    r"""Create/refresh a ``paper_library`` row at INGEST time (server-authoritative).

    The ingestion endpoints (``/api/paper/upload``, ``/api/paper/fetch-arxiv-stream``)
    already hold every server-derived column, so they persist the bookshelf row
    THEMSELVES rather than relying on the client's fire-and-forget PUT. This is
    what makes an uploaded/fetched paper survive a tab-close / refresh that races
    (or never fires) the client save — the durable fix for the ``qa=0 imgs=0``
    ghost-row / vanishing-paper bug.

    Preserves an existing row's ``created_at`` / ``qa_history`` / ``babel_cache``
    (a rare re-ingest of the same id must not wipe the user's Q&A). Best-effort:
    logs and returns False on failure, never raises into the ingest path.

    Args:
        paper_id: client-generated bookshelf id (``[\w.\-]{1,128}``).
    Returns:
        True on a successful write, else False.
    """
    paper_id = (paper_id or '').strip()
    if not paper_id or len(paper_id) > 128 or not re.fullmatch(r'[\w.\-]+', paper_id):
        logger.warning('[Paper:Ingest] Skip library persist — bad paper_id: %.60s', paper_id)
        return False
    now_ms = int(time.time() * 1000)
    try:
        from lib.database._core import _pool_get, _pool_put
        from lib.database._core_schema import PAPER_LIBRARY, upsert
        db = _pool_get()
        try:
            existing = db.execute(
                'SELECT created_at, qa_history, babel_cache FROM paper_library '
                'WHERE id=? AND user_id=?', (paper_id, DEFAULT_USER_ID),
            ).fetchone()
            created_at = (int(existing['created_at'])
                          if (existing and existing['created_at']) else now_ms)
            qa_history = (existing['qa_history'] if existing else '[]') or '[]'
            babel_cache = (existing['babel_cache'] if existing else '{}') or '{}'
            imgs = images[:_LIB_IMAGES_CAP] if isinstance(images, list) else []
            upsert(db, PAPER_LIBRARY, {
                'id': paper_id, 'user_id': DEFAULT_USER_ID,
                'title': (title or '')[:_LIB_TITLE_CAP],
                'pdf_url': (pdf_url or '')[:2000],
                'pdf_filename': os.path.basename(pdf_filename or '')[:500],
                'arxiv_id': (arxiv_id or '')[:64],
                'paper_hash': (paper_hash or '')[:64],
                'parsed_text': (parsed_text or '')[:_LIB_PARSED_TEXT_CAP],
                'qa_history': qa_history,
                'images': json.dumps(imgs, ensure_ascii=False),
                'babel_cache': babel_cache,
                'page_count': int(page_count or 0),
                'created_at': created_at, 'updated_at': now_ms,
            }, retry=True)
            logger.info('[Paper:Ingest] Persisted library row %s — hash=%s imgs=%d',
                        paper_id[:16], (paper_hash or '')[:12], len(imgs))
            return True
        finally:
            _pool_put(db)
    except Exception as e:
        logger.error('[Paper:Ingest] Library persist failed for %s: %s',
                     paper_id[:16], e, exc_info=True)
        return False


@paper_bp.route('/api/paper/upload', methods=['POST'])
def upload_paper():
    """Upload a PDF and run the full server-side ingestion pipeline.

    Single round-trip: save PDF → parse text → extract figures →
    return everything the frontend needs to populate library state.

    SKIPPED from the native-async conversion: this multipart upload reads
    ``request.files`` and calls ``file.save``. Under the server.py Flask→Quart
    shim, ``request.files`` is a *sync-safe* property that drives Quart's async
    body reader via ``run_coroutine_threadsafe(...).result()`` — safe from an
    executor thread (sync handler) but a guaranteed deadlock if invoked from
    the event loop inside an ``async def`` handler. Kept sync so the shim
    parses the multipart body correctly.

    Returns:
        {
            ok: true,
            pdf_url: str,
            filename: str,
            file_size: int,
            parsed_text: str,
            total_pages: int,
            text_length: int,
            paper_hash: str,
            images: [{url, caption, page, source, width, height}],
            parse_error: str (only on parse failure — PDF is still served)
        }
    """
    if 'file' not in request.files:
        logger.warning('[Paper:Upload] No file in request')
        return api_bad_request('No file')
    file = request.files['file']
    if not file.filename:
        logger.warning('[Paper:Upload] Empty filename')
        return api_bad_request('No filename')
    if not file.filename.lower().endswith('.pdf'):
        logger.warning('[Paper:Upload] Non-PDF file rejected: %s', file.filename)
        return api_bad_request('Only PDF files are supported')

    original_name = file.filename
    # Client-generated bookshelf id — the server persists the library row itself
    # (server-authoritative ingest), so a paper survives even if the client's
    # PUT never lands. Absent → skip persist (back-compat) but still serve.
    client_paper_id = (request.form.get('paper_id') or '').strip()
    filename = f"{int(time.time() * 1000)}_{original_name}"
    filename = re.sub(r'[^\w\-.]', '_', filename)
    filepath = os.path.join(PAPER_DIR, filename)

    try:
        # NOTE: Quart's FileStorage.save is an async coroutine. This is a SYNC
        # handler (see docstring), so `file.save(...)` would return an un-awaited
        # coroutine and silently write nothing → the next getsize() 500s. Read
        # the bytes and write them ourselves, matching routes/upload.py.
        file.stream.seek(0)
        pdf_bytes = file.stream.read()
        # PAPER_DIR is created once at import (lib/paper/hashing.py), but on a
        # FUSE/cross-DC mount it can be missing at write time — ``open('wb')``
        # then 500s with ENOENT and the PDF bytes are lost (the vanishing-paper
        # bug). Re-ensure the dir on every write.
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'wb') as out:
            out.write(pdf_bytes)
        file_size = os.path.getsize(filepath)
        logger.info('[Paper:Upload] Saved: %s (%d bytes) — original=%s',
                    filename, file_size, original_name)
    except Exception as e:
        logger.error('[Paper:Upload] Failed to save %s: %s', filename, e, exc_info=True)
        return api_internal_error(f'Upload failed: {str(e)}')

    # Validity gate: a truncated / aborted / empty upload can land a file that
    # exists (getsize > 0) but is not an openable PDF — e.g. a 15-byte
    # ``%PDF-1.4`` header-only stub. Committing it seeds a permanent non-viewable
    # ghost row the reader dead-ends on. Reject it HERE: delete the stub and
    # return a real error instead of ok:true, so a ghost is never created.
    from lib.pdf_parser import validate_pdf_bytes
    _valid, _npages, _verr = validate_pdf_bytes(pdf_bytes)
    if not _valid:
        logger.warning('[Paper:Upload] Rejected invalid PDF %s (%d bytes): %s',
                       filename, file_size, _verr)
        try:
            os.remove(filepath)
        except OSError as e:
            logger.debug('[Paper:Upload] cleanup of rejected %s failed: %s', filename, e)
        return api_bad_request(
            'The uploaded file is not a readable PDF (it may be truncated or '
            'corrupted). Please re-upload. [' + _verr + ']')

    parsed_text = ''
    total_pages = 0
    text_length = 0
    parse_error = ''
    try:
        from lib.pdf_parser import parse_pdf as _parse_pdf
        with open(filepath, 'rb') as f:
            pdf_bytes = f.read()
        t0 = time.time()
        result = _parse_pdf(pdf_bytes, max_text_chars=0, max_images=0)
        parsed_text = result.get('text') or ''
        total_pages = result.get('totalPages', 0)
        text_length = result.get('textLength', len(parsed_text))
        logger.info('[Paper:Upload] Parsed %s — %d pages, %d chars in %.1fs',
                    filename, total_pages, text_length, time.time() - t0)
    except Exception as e:
        logger.warning('[Paper:Upload] PDF parse failed for %s: %s', filename, e, exc_info=True)
        parse_error = f'PDF parse failed: {e}'

    phash = _paper_hash(parsed_text) if parsed_text else ''
    images = _extract_paper_figures(filepath, phash) if phash else []

    # Server-authoritative persist: the PDF saved fine, so the paper is real —
    # write the bookshelf row NOW (don't wait on the client's PUT). The PDF is
    # viewable even when parsing failed, so we persist regardless of parse_error.
    if client_paper_id:
        _persist_ingested_library_row(
            client_paper_id, title=original_name,
            pdf_url=f'/api/paper/pdf/{filename}', pdf_filename=filename,
            arxiv_id='', paper_hash=phash, parsed_text=parsed_text,
            images=images, page_count=total_pages)

    resp = {
        'ok': True,
        'id': client_paper_id,
        'pdf_url': f'/api/paper/pdf/{filename}',
        'filename': filename,
        'file_size': file_size,
        'parsed_text': parsed_text,
        'total_pages': total_pages,
        'text_length': text_length,
        'paper_hash': phash,
        'images': images,
    }
    if parse_error:
        resp['parse_error'] = parse_error
    return jsonify(resp)


# ══════════════════════════════════════════════════════
#  Paper Library — server-side bookshelf
# ══════════════════════════════════════════════════════

@api_v1_paper_bp.route('/api/v1/paper/library', methods=['GET'])
async def list_library():
    """Return all papers on the current user's bookshelf, newest first.

    Each entry includes a ``hasReport`` flag computed from ``paper_reports``
    so the UI can show a "· report" badge without a second round-trip.
    """
    try:
        rows = await async_fetchall(
            'SELECT ' + ', '.join(_PAPER_LIB_COLUMNS) +
            ' FROM paper_library WHERE user_id=? ORDER BY updated_at DESC',
            (DEFAULT_USER_ID,), domain=DOMAIN_CHAT,
        )
        papers = [_lib_row_to_dict(r) for r in rows]

        # Reap GHOST rows — non-viewable bookshelf entries left by the OLD
        # fire-and-forget persistence (a client PUT that raced/replaced a failed
        # upload wrote a row with no PDF). A row is a ghost when its
        # ``pdfFilename`` is empty OR the referenced file is missing from
        # PAPER_DIR. We HARD-SKIP them from the listing (never return them —
        # returning one reproduces the vanishing-paper ghost the user saw) but
        # do NOT delete: a FUSE/cross-DC mount can transiently report a real
        # file missing, and a listing must never be a destructive operation.
        # A recovered mount makes a real paper reappear on the next listing.
        _kept = [p for p in papers if not _is_ghost_library_row(p)]
        _reaped = len(papers) - len(_kept)
        if _reaped:
            logger.info('[Paper:Library] Reaped %d ghost row(s) (empty/missing PDF) '
                        'from listing (kept in DB, non-destructive)', _reaped)
        papers = _kept

        # Single-query JOIN-ish: collect hashes, ask paper_reports which exist
        hashes = [p['paperHash'] for p in papers if p['paperHash']]
        reported = set()
        if hashes:
            try:
                placeholders = ','.join(['?'] * len(hashes))
                rrows = await async_fetchall(
                    'SELECT DISTINCT paper_hash FROM paper_reports '
                    'WHERE paper_hash IN (' + placeholders + ')',
                    tuple(hashes), domain=DOMAIN_CHAT,
                )
                reported = {r['paper_hash'] for r in rrows}
            except Exception as e:
                logger.debug('[Paper:Library] hasReport lookup failed: %s', e)
        for p in papers:
            p['hasReport'] = bool(p['paperHash'] and p['paperHash'] in reported)

        logger.debug('[Paper:Library] Listed %d papers (%d with reports)',
                     len(papers), len(reported))
        return api_ok({'papers': papers})
    except Exception as e:
        logger.error('[Paper:Library] List failed: %s', e, exc_info=True)
        return api_internal_error(e)


@api_v1_paper_bp.route('/api/v1/paper/library/<paper_id>', methods=['PUT'])
async def upsert_library_entry(paper_id):
    """Create or update a paper on the bookshelf.

    Body JSON mirrors the shape returned by ``GET /api/paper/library``:
        title, pdfUrl, pdfFilename, arxivId, paperHash, parsedText,
        qaHistory (list), images (list), babelCache (dict), pageCount, createdAt
    """
    paper_id = (paper_id or '').strip()
    if not paper_id or len(paper_id) > 128 or not re.fullmatch(r'[\w.\-]+', paper_id):
        logger.warning('[Paper:Library] Upsert rejected bad id: %.60s', paper_id)
        return api_bad_request('invalid id')

    data = await async_parse_body()
    now_ms = int(time.time() * 1000)

    qa = data.get('qaHistory') or []
    if not isinstance(qa, list):
        qa = []
    qa = qa[-_LIB_QA_HISTORY_CAP:]

    babel = data.get('babelCache') or {}
    if not isinstance(babel, dict):
        babel = {}

    try:
        page_count = int(data.get('pageCount') or 0)
    except (ValueError, TypeError) as e:
        logger.debug('[Paper:Library] Non-numeric pageCount, defaulting to 0: %s', e)
        page_count = 0

    # The upsert mixes a SELECT-then-INSERT-OR-REPLACE on a sync helper
    # (db_execute_with_retry) that takes a raw connection. The facade can't
    # express the retry helper, so run the whole DB block on a borrowed pool
    # connection in a worker thread (checkout→use→return). DOMAIN_CHAT is the
    # default domain for the paper library tables (get_thread_db() default).
    def _do_upsert():
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
        try:
            # Pull existing row so the client only has to send the small mutable
            # state (qaHistory, babelCache, pageCount, title) — it doesn't need
            # to re-ship parsed_text / images / paperHash on every save. The
            # ingestion endpoints (/api/paper/upload, /api/paper/fetch-arxiv-stream)
            # are the only places that originate those big columns.
            existing = db.execute(
                'SELECT title, pdf_url, pdf_filename, arxiv_id, paper_hash, '
                '       parsed_text, images, page_count, created_at '
                'FROM paper_library WHERE id=? AND user_id=?',
                (paper_id, DEFAULT_USER_ID),
            ).fetchone()

            def _take(client_key, exist_key, *, cap=None, sanitize=None, default=''):
                """Use client value if provided AND non-empty, else preserve existing."""
                v = data.get(client_key)
                if v is None or v == '':
                    v = existing[exist_key] if existing else default
                v = '' if v is None else str(v)
                if sanitize:
                    v = sanitize(v)
                if cap is not None:
                    v = v[:cap]
                return v

            title = _take('title', 'title', cap=_LIB_TITLE_CAP)
            pdf_url = _take('pdfUrl', 'pdf_url', cap=2000)
            pdf_filename = _take('pdfFilename', 'pdf_filename', cap=500,
                                 sanitize=os.path.basename)
            arxiv_id = _take('arxivId', 'arxiv_id', cap=64)
            paper_hash = _take('paperHash', 'paper_hash', cap=64)
            parsed_text = _take('parsedText', 'parsed_text', cap=_LIB_PARSED_TEXT_CAP)

            # Images: accept client list on first write; fall back to disk
            # manifest (server source of truth) so the row always reflects reality.
            if isinstance(data.get('images'), list):
                images = data['images'][:_LIB_IMAGES_CAP]
            elif paper_hash:
                images = _load_image_manifest(paper_hash)[:_LIB_IMAGES_CAP]
            elif existing:
                try:
                    images = json.loads(existing['images'] or '[]')[:_LIB_IMAGES_CAP]
                except (json.JSONDecodeError, TypeError) as _e_audit:
                    logger.debug('[paper] upsert_library_entry caught %s: %s', type(_e_audit).__name__, _e_audit)
                    images = []
            else:
                images = []

            if existing and existing['created_at']:
                created_at = int(existing['created_at'])
            else:
                created_at = int(data.get('createdAt') or now_ms)
            _page_count = page_count
            if not _page_count and existing:
                _page_count = int(existing['page_count'] or 0)

            from lib.database._core_schema import PAPER_LIBRARY, upsert
            upsert(db, PAPER_LIBRARY, {
                'id': paper_id, 'user_id': DEFAULT_USER_ID, 'title': title,
                'pdf_url': pdf_url, 'pdf_filename': pdf_filename,
                'arxiv_id': arxiv_id, 'paper_hash': paper_hash,
                'parsed_text': parsed_text,
                'qa_history': json.dumps(qa, ensure_ascii=False),
                'images': json.dumps(images, ensure_ascii=False),
                'babel_cache': json.dumps(babel, ensure_ascii=False),
                'page_count': _page_count, 'created_at': created_at,
                'updated_at': now_ms,
            }, retry=True)
            logger.info('[Paper:Library] Upserted %s — title=%.60s qa=%d imgs=%d',
                        paper_id[:16], title, len(qa), len(images))
        finally:
            _pool_put(db)

    try:
        await asyncio.to_thread(_do_upsert)
        return api_ok({'id': paper_id, 'updatedAt': now_ms})
    except Exception as e:
        logger.error('[Paper:Library] Upsert failed for %s: %s', paper_id[:16], e, exc_info=True)
        return api_internal_error(e)


@api_v1_paper_bp.route('/api/v1/paper/library/<paper_id>', methods=['DELETE'])
async def delete_library_entry(paper_id):
    """Remove a paper from the bookshelf.

    The underlying PDF file under uploads/papers is left in place — other
    bookshelf entries (or cached reports keyed by paper_hash) may still
    reference the same file.
    """
    paper_id = (paper_id or '').strip()
    if not paper_id:
        return api_bad_request('invalid id')

    # db_execute_with_retry takes a raw connection — run on a borrowed pool
    # connection in a worker thread (DOMAIN_CHAT default for paper tables).
    def _do_delete():
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
        try:
            db_execute_with_retry(
                db,
                'DELETE FROM paper_library WHERE id=? AND user_id=?',
                (paper_id, DEFAULT_USER_ID),
            )
            logger.info('[Paper:Library] Deleted %s', paper_id[:16])
        finally:
            _pool_put(db)

    try:
        await asyncio.to_thread(_do_delete)
        return api_ok()
    except Exception as e:
        logger.error('[Paper:Library] Delete failed for %s: %s', paper_id[:16], e, exc_info=True)
        return api_internal_error(e)


@api_v1_paper_bp.route('/api/v1/paper/library/prune-broken', methods=['POST'])
async def prune_broken_library_rows():
    """One-time cleanup of DEFINITIVELY-broken stub rows (opt-in, destructive).

    A truncated / aborted upload used to leave a 15-byte ``%PDF-1.4`` stub +
    seed a bookshelf row. The listing now HIDES such rows (non-destructively),
    but that leaves them permanent + unselectable in the DB. This endpoint hard-
    deletes ONLY rows that ``_is_broken_stub_row`` proves broken (file present,
    small, unopenable) and removes the orphaned stub file. It never touches a
    row whose file is merely missing (possible FUSE hiccup) or a recommendation
    row. Must be invoked explicitly (POST) — never runs on a plain listing.

    Returns: { ok, pruned: int, ids: [str] }
    """
    def _prune():
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
        pruned_ids = []
        try:
            rows = db.execute(
                'SELECT ' + ', '.join(_PAPER_LIB_COLUMNS) +
                ' FROM paper_library WHERE user_id=?', (DEFAULT_USER_ID,),
            ).fetchall()
            for r in rows:
                paper = _lib_row_to_dict(r)
                if not _is_broken_stub_row(paper):
                    continue
                pid = paper['id']
                fn = os.path.basename((paper.get('pdfFilename') or '').strip())
                db_execute_with_retry(
                    db, 'DELETE FROM paper_library WHERE id=? AND user_id=?',
                    (pid, DEFAULT_USER_ID))
                pruned_ids.append(pid)
                # Remove the orphaned stub file (best-effort — it is proven a
                # non-PDF, so nothing else can legitimately reference it).
                if fn:
                    try:
                        os.remove(os.path.join(PAPER_DIR, fn))
                    except OSError as e:
                        logger.debug('[Paper:Prune] stub file remove failed %s: %s', fn, e)
            if pruned_ids:
                logger.info('[Paper:Prune] Hard-deleted %d broken stub row(s)', len(pruned_ids))
        finally:
            _pool_put(db)
        return pruned_ids

    try:
        ids = await asyncio.to_thread(_prune)
        return api_ok({'pruned': len(ids), 'ids': ids})
    except Exception as e:
        logger.error('[Paper:Library] Prune failed: %s', e, exc_info=True)
        return api_internal_error(e)



# ── Abort routes (factory-minted) ───────────────────────────────────
#
# The report / Q&A / translate ABORT endpoints are uniform — set the task's
# abort_event and return ok/404 — so they use the shared
# ``register_task_routes`` factory instead of three hand-rolled handlers.
# The factory's ``runtime.abort(task_id)`` sets exactly the same
# ``task['abort_event']`` the engine loops read (via
# ``AbortSignal.from_event`` in lib/agent_loop.py), so abort semantics are
# unchanged; the atomic status-check + set() (under the runtime lock) is
# actually STRONGER than the old handler's bare ``.set()`` (it can't mark a
# racing finish 'done').
#
# Route shape changes from ``POST …/abort {task_id}`` (body) to the factory's
# ``POST …/abort/<task_id>`` (path segment) — matching the orchestrations
# ``/run/abort/<id>`` convention. The frontend api.js clients are updated to
# match.
#
# POLL stays custom (enable_poll=False): the paper poll responses carry
# engine-specific keys (report / answer / text / partial / progress / meta /
# resolvedTitle / paper_hash) that the generic ``runtime.poll()`` doesn't
# emit — the workers set task['full_text']/status directly and never call
# runtime.finish(), so task['result'] is None. The agents-v1 façade also
# name-calls poll_report_task / poll_translate_task. Migrating poll would
# need a factory response-enricher hook; deferred to a later slice.
register_task_routes(api_v1_paper_bp, _report_runtime,
                     url_prefix='/api/v1/paper/report', enable_poll=False)
register_task_routes(api_v1_paper_bp, _qa_runtime,
                     url_prefix='/api/v1/paper/qa', enable_poll=False)
register_task_routes(api_v1_paper_bp, _translate_runtime,
                     url_prefix='/api/v1/paper/translate', enable_poll=False)
