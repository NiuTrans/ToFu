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

import base64
import json
import os
import re
import time

import requests as _requests
from flask import Blueprint, Response, jsonify, request, send_file

from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
)
from lib.database import db_execute_with_retry, get_db, get_thread_db
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
    _TRANSLATE_CHUNK_SIZE,
    _TRANSLATE_TASK_TTL,
    _append_report_event,
    _append_translate_event,
    _build_image_manifest,
    _cleanup_stale_report_tasks,
    _cleanup_stale_translate_tasks,
    _ensure_paper_images,
    _ensure_title_heading,
    _execute_report_tool,
    _extract_arxiv_id,
    _extract_paper_figures,
    _inject_images_into_report,
    _lib_row_to_dict,
    _load_image_manifest,
    _lookup_paper_title,
    _new_report_task,
    _new_translate_task,
    _paper_hash,
    _report_dedup_index,
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
from lib.request_parser import parse_body
from routes.common import DEFAULT_USER_ID

logger = get_logger(__name__)

paper_bp = Blueprint('paper', __name__)
# v1 blueprint for the JSON routes (the 5 carve-outs above stay on paper_bp).
from routes.api_v1.paper import api_v1_paper_bp  # noqa: E402


# ══════════════════════════════════════════════════════
#  API Endpoints
# ══════════════════════════════════════════════════════

@paper_bp.route('/api/paper/chat', methods=['POST'])
def paper_chat():
    """Streaming LLM chat for paper Q&A / translation.

    Body JSON:
        messages: list — OpenAI-format messages [{role, content}, ...]
        model: str (optional) — LLM model to use
    Returns:
        SSE stream of chat completion deltas.
    """
    data = parse_body()
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


@api_v1_paper_bp.route('/api/v1/paper/extract-images', methods=['POST'])
def extract_images():
    """Extract figure/table images from a previously uploaded PDF.

    Body JSON:
        filename: str — the filename returned by /api/paper/upload or /api/paper/fetch-arxiv
        paper_hash: str (optional) — if omitted, computed from filename bytes
        max_images: int (optional) — cap, default 30
        max_image_width: int (optional) — default 900

    Returns:
        { ok: true, paper_hash: str, images: [{url, caption, page, source, width, height}] }
    """
    data = parse_body()
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
    images_out = _extract_paper_figures(
        filepath, phash, max_images=max_images, max_image_width=max_image_width,
    )
    return api_ok({'paper_hash': phash, 'images': images_out})


@paper_bp.route('/api/paper/images/<phash>/<filename>')
def serve_paper_image(phash, filename):
    """Serve an extracted paper figure image."""
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
    return send_file(filepath, mimetype=mt)


@api_v1_paper_bp.route('/api/v1/paper/report/start', methods=['POST'])
def start_report_task():
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
    data = parse_body()
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
            images = _ensure_paper_images(derived_fn, phash)

    # DB cache check (unless force) — no task needed, report is already done
    if not force:
        try:
            db = get_db()
            row = db.execute(
                "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
                (phash, lang),
            ).fetchone()
            if row and row['report']:
                logger.info('[Paper:Report] DB cache hit — hash=%s lang=%s %d chars',
                            phash, lang, len(row['report']))
                enriched = _inject_images_into_report(row['report'], images, lang=lang)
                enriched = _ensure_title_heading(enriched, phash)
                return jsonify({
                    'ok': True, 'cached': True,
                    'report': enriched, 'paper_hash': phash,
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

    # Build prompt for new task (runs for both new and force-regen paths)
    prompt_template = _REPORT_PROMPT_ZH if lang == 'zh' else _REPORT_PROMPT_EN
    max_text = 120000
    truncated_text = paper_text[:max_text]
    if len(paper_text) > max_text:
        logger.info('[Paper:Report] Truncating paper text from %d to %d chars', len(paper_text), max_text)
    manifest = _build_image_manifest(images, lang=lang)
    if manifest:
        truncated_text = truncated_text + '\n\n---\n\n' + manifest
        logger.info('[Paper:Report] Injected image manifest — %d images, hash=%s', len(images), phash)
    prompt = prompt_template.replace('{paper_text}', truncated_text)
    tool_instruction = (
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
    task = _new_report_task(task_id, phash, lang, model, client_title=client_title)

    logger.info('[Paper:Report] Starting task %s — model=%s lang=%s text_len=%d hash=%s',
                task_id, model, lang, len(paper_text), phash)
    _report_runtime.spawn(task_id, _run_report_task, task, messages, images)

    return jsonify({
        'ok': True, 'task_id': task_id, 'paper_hash': phash,
        'running': True, 'existed': False,
    })


@api_v1_paper_bp.route('/api/v1/paper/report/poll', methods=['GET'])
def poll_report_task():
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
    if task['status'] == 'error':
        resp['error'] = task.get('error', '')
    return jsonify(resp)


@api_v1_paper_bp.route('/api/v1/paper/report/abort', methods=['POST'])
def abort_report_task():
    """Abort a running report task (best-effort)."""
    data = parse_body()
    task_id = (data.get('task_id') or '').strip()
    if not task_id:
        return api_bad_request('task_id required')
    task = _report_runtime.get(task_id)
    if not task:
        return api_not_found('task not found')
    task['abort_event'].set()
    logger.info('[Paper:Report] Abort requested for task %s', task_id)
    return api_ok()


@api_v1_paper_bp.route('/api/v1/paper/report/lookup', methods=['POST'])
def lookup_report_task():
    """Find an existing running task by (paper_hash, lang).

    Used by the frontend on tab re-entry / mode re-enter to see whether a
    task is already running server-side for this paper — so it can resume
    polling without starting a new one.

    Body JSON: {paper_hash: str, lang: str}
    Returns: {ok: true, task_id: str, status: str} or {ok: false}
    """
    data = parse_body()
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
def export_report():
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
        db = get_db()
        row = db.execute(
            'SELECT report FROM paper_reports WHERE paper_hash=? AND lang=?',
            (phash, lang),
        ).fetchone()
    except Exception as e:
        logger.error('[Paper:Report:Export] Lookup failed: %s', e, exc_info=True)
        return api_internal_error('lookup failed')
    if not row or not row['report']:
        return api_not_found('report not found')

    images = _load_image_manifest(phash)
    body_md = _inject_images_into_report(row['report'], images, lang=lang)
    body_md = _ensure_title_heading(body_md, phash)

    # Get the paper title for the export filename / page title
    title = 'Paper Report'
    try:
        trow = db.execute(
            'SELECT title, arxiv_id FROM paper_library '
            'WHERE paper_hash=? AND user_id=? ORDER BY updated_at DESC LIMIT 1',
            (phash, DEFAULT_USER_ID),
        ).fetchone()
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
def get_report_cache():
    """Lookup cached report by paper hash.

    Body JSON:
        paper_hash: str — precomputed hash (preferred, avoids re-sending full text)
        paper_text: str — full text of the paper (fallback, used to compute hash)
        lang: str (optional) — language. Default 'en'.
    Returns:
        { ok: true, report: str, paper_hash: str } or { ok: false }
    """
    data = parse_body()
    phash = data.get('paper_hash', '').strip()
    lang = data.get('lang', 'en') or 'en'

    # Prefer pre-computed hash; fall back to computing from text
    if not phash:
        paper_text = data.get('paper_text', '').strip()
        if not paper_text:
            return api_bad_request('No paper_hash or paper_text')
        phash = _paper_hash(paper_text)

    try:
        db = get_db()
        row = db.execute(
            "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
            (phash, lang),
        ).fetchone()
        if row and row['report']:
            logger.debug('[Paper:Report:Cache] Hit — hash=%s lang=%s', phash, lang)
            # Server-side enrichment: load the manifest from disk (the client
            # is no longer trusted to forward image URLs).
            images = _load_image_manifest(phash)
            enriched = _inject_images_into_report(row['report'], images, lang=lang)
            enriched = _ensure_title_heading(enriched, phash)
            return api_ok({'report': enriched, 'paper_hash': phash})
    except Exception as e:
        logger.warning('[Paper:Report:Cache] Lookup failed: %s', e)

    return jsonify({'ok': False})


@api_v1_paper_bp.route('/api/v1/paper/translate/start', methods=['POST'])
def start_translate_task():
    """Start (or join) a Babel-mode whole-paper translation task.

    Body JSON:
        paper_text: str
        lang: str — target language (e.g. 'zh', 'en', 'ja')
        paper_hash: str (optional) — used as cache key; computed if missing.
        model: str (optional)
        force: bool (optional)
    """
    data = parse_body()
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
            db = get_db()
            row = db.execute(
                'SELECT text FROM paper_translations WHERE paper_hash=? AND lang=?',
                (phash, lang),
            ).fetchone()
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

    task_id = f'tr_{int(time.time() * 1000)}_{phash[:8]}_{lang}'
    task = _new_translate_task(task_id, phash, lang, model)

    _translate_runtime.spawn(task_id, _run_translate_task, task, paper_text)

    return api_ok({'task_id': task_id, 'paper_hash': phash,
                    'running': True, 'existed': False})


@api_v1_paper_bp.route('/api/v1/paper/translate/poll', methods=['GET'])
def poll_translate_task():
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


@api_v1_paper_bp.route('/api/v1/paper/translate/abort', methods=['POST'])
def abort_translate_task():
    data = parse_body()
    task_id = (data.get('task_id') or '').strip()
    if not task_id:
        return api_bad_request('task_id required')
    task = _translate_runtime.get(task_id)
    if not task:
        return api_not_found('task not found')
    task['abort_event'].set()
    logger.info('[Paper:Translate] Abort requested for task %s', task_id)
    return api_ok()


@api_v1_paper_bp.route('/api/v1/paper/translate/lookup', methods=['POST'])
def lookup_translate_task():
    data = parse_body()
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
def get_translate_cache():
    data = parse_body()
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
        db = get_db()
        row = db.execute(
            'SELECT text FROM paper_translations WHERE paper_hash=? AND lang=?',
            (phash, lang),
        ).fetchone()
        if row and row['text']:
            return api_ok({'text': row['text'], 'paper_hash': phash})
    except Exception as e:
        logger.warning('[Paper:Translate:Cache] Lookup failed: %s', e)
    return jsonify({'ok': False})


@api_v1_paper_bp.route('/api/v1/paper/fetch-arxiv', methods=['POST'])
def fetch_arxiv():
    """Download PDF from arXiv URL and serve it locally.

    Body JSON:
        url: str — arXiv URL (abs page, pdf link, or just the ID like 2301.12345)
    Returns:
        { ok: true, pdf_url: str, title: str, arxiv_id: str }
    """
    data = parse_body()
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

    try:
        logger.info('[Paper:arXiv] Downloading PDF: %s', pdf_url)
        t0 = time.time()
        resp = _requests.get(pdf_url, timeout=60, stream=True,
                             headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' not in content_type and 'octet-stream' not in content_type:
            logger.warning('[Paper:arXiv] Unexpected content type: %s for %s', content_type, pdf_url)

        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(filepath)
        elapsed = time.time() - t0
        logger.info('[Paper:arXiv] Downloaded %s: %d bytes in %.1fs', arxiv_id, file_size, elapsed)

        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'file_size': file_size,
        })

    except _requests.Timeout:
        logger.warning('[Paper:arXiv] Download timeout (60s): %s', pdf_url)
        return api_error('Download timed out (60s)', status=504)
    except _requests.RequestException as e:
        logger.warning('[Paper:arXiv] Download failed: %s — %s', pdf_url, e)
        return api_error(f'Download failed: {str(e)}', status=502)


@paper_bp.route('/api/paper/fetch-arxiv-stream', methods=['POST'])
def fetch_arxiv_stream():
    """Download PDF from arXiv and parse it — SSE stream of progress events.

    Body JSON:
        url: str — arXiv URL or ID

    SSE events (each one JSON on a ``data:`` line):
        {stage: 'resolve', arxiv_id: str, pdf_url: str}  — URL parsed
        {stage: 'download', downloaded: int, total: int}  — download progress
        {stage: 'download_done', file_size: int, elapsed: float}
        {stage: 'parse_start'}
        {stage: 'parse_done', total_pages: int, text_length: int, elapsed: float}
        {stage: 'done', ok: true, pdf_url: str, arxiv_id: str,
               parsed_text: str, total_pages: int, text_length: int, cached: bool}
        {stage: 'error', error: str}
    """
    data = parse_body()
    url_input = (data.get('url') or '').strip()
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
        yield _sse({'stage': 'resolve', 'arxiv_id': arxiv_id,
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
                resp = _requests.get(pdf_url, timeout=60, stream=True,
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

        # ── Done — return everything the client needs ──
        yield _sse({'stage': 'done', 'ok': True,
                    'pdf_url': f'/api/paper/pdf/{filename}',
                    'arxiv_id': arxiv_id,
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


@paper_bp.route('/api/paper/pdf/<filename>')
def serve_paper_pdf(filename):
    """Serve a downloaded paper PDF."""
    filename = os.path.basename(filename)
    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.debug('[Paper] PDF not found: %s', filename)
        return api_not_found('PDF not found')
    return send_file(filepath, mimetype='application/pdf')


@api_v1_paper_bp.route('/api/v1/paper/reparse', methods=['POST'])
def reparse_paper():
    """Re-parse an already-stored paper PDF to recover its text.

    Used to recover library entries that were saved before server-side parsing
    (or whose parse step failed). Given a filename already under PAPER_DIR,
    reads it and returns extracted text + page count.

    Body JSON:
        filename: str — basename of the PDF under PAPER_DIR

    Returns:
        { ok: true, text: str, total_pages: int, text_length: int }
    """
    data = parse_body()
    filename = os.path.basename((data.get('filename') or '').strip())
    if not filename:
        logger.warning('[Paper:Reparse] No filename provided')
        return api_bad_request('No filename')

    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        logger.warning('[Paper:Reparse] PDF not found: %s', filename)
        return api_not_found('PDF not found')

    try:
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
        return jsonify({
            'ok': True,
            'text': text,
            'total_pages': total_pages,
            'text_length': text_length,
        })
    except Exception as e:
        logger.error('[Paper:Reparse] Failed for %s: %s', filename, e, exc_info=True)
        return api_internal_error(f'Reparse failed: {e}')


@paper_bp.route('/api/paper/upload', methods=['POST'])
def upload_paper():
    """Upload a PDF and run the full server-side ingestion pipeline.

    Single round-trip: save PDF → parse text → extract figures →
    return everything the frontend needs to populate library state.

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
    filename = f"{int(time.time() * 1000)}_{original_name}"
    filename = re.sub(r'[^\w\-.]', '_', filename)
    filepath = os.path.join(PAPER_DIR, filename)

    try:
        file.save(filepath)
        file_size = os.path.getsize(filepath)
        logger.info('[Paper:Upload] Saved: %s (%d bytes) — original=%s',
                    filename, file_size, original_name)
    except Exception as e:
        logger.error('[Paper:Upload] Failed to save %s: %s', filename, e, exc_info=True)
        return api_internal_error(f'Upload failed: {str(e)}')

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

    resp = {
        'ok': True,
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
def list_library():
    """Return all papers on the current user's bookshelf, newest first.

    Each entry includes a ``hasReport`` flag computed from ``paper_reports``
    so the UI can show a "· report" badge without a second round-trip.
    """
    try:
        db = get_db()
        rows = db.execute(
            'SELECT ' + ', '.join(_PAPER_LIB_COLUMNS) +
            ' FROM paper_library WHERE user_id=? ORDER BY updated_at DESC',
            (DEFAULT_USER_ID,),
        ).fetchall()
        papers = [_lib_row_to_dict(r) for r in rows]

        # Single-query JOIN-ish: collect hashes, ask paper_reports which exist
        hashes = [p['paperHash'] for p in papers if p['paperHash']]
        reported = set()
        if hashes:
            try:
                placeholders = ','.join(['?'] * len(hashes))
                rrows = db.execute(
                    'SELECT DISTINCT paper_hash FROM paper_reports '
                    'WHERE paper_hash IN (' + placeholders + ')',
                    tuple(hashes),
                ).fetchall()
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
def upsert_library_entry(paper_id):
    """Create or update a paper on the bookshelf.

    Body JSON mirrors the shape returned by ``GET /api/paper/library``:
        title, pdfUrl, pdfFilename, arxivId, paperHash, parsedText,
        qaHistory (list), images (list), babelCache (dict), pageCount, createdAt
    """
    paper_id = (paper_id or '').strip()
    if not paper_id or len(paper_id) > 128 or not re.fullmatch(r'[\w.\-]+', paper_id):
        logger.warning('[Paper:Library] Upsert rejected bad id: %.60s', paper_id)
        return api_bad_request('invalid id')

    data = parse_body()
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

    try:
        db = get_thread_db()
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
        if not page_count and existing:
            page_count = int(existing['page_count'] or 0)

        db_execute_with_retry(
            db,
            'INSERT OR REPLACE INTO paper_library '
            '(id, user_id, title, pdf_url, pdf_filename, arxiv_id, paper_hash, '
            ' parsed_text, qa_history, images, babel_cache, page_count, '
            ' created_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                paper_id, DEFAULT_USER_ID, title, pdf_url, pdf_filename,
                arxiv_id, paper_hash, parsed_text,
                json.dumps(qa, ensure_ascii=False),
                json.dumps(images, ensure_ascii=False),
                json.dumps(babel, ensure_ascii=False),
                page_count, created_at, now_ms,
            ),
        )
        logger.info('[Paper:Library] Upserted %s — title=%.60s qa=%d imgs=%d',
                    paper_id[:16], title, len(qa), len(images))
        return api_ok({'id': paper_id, 'updatedAt': now_ms})
    except Exception as e:
        logger.error('[Paper:Library] Upsert failed for %s: %s', paper_id[:16], e, exc_info=True)
        return api_internal_error(e)


@api_v1_paper_bp.route('/api/v1/paper/library/<paper_id>', methods=['DELETE'])
def delete_library_entry(paper_id):
    """Remove a paper from the bookshelf.

    The underlying PDF file under uploads/papers is left in place — other
    bookshelf entries (or cached reports keyed by paper_hash) may still
    reference the same file.
    """
    paper_id = (paper_id or '').strip()
    if not paper_id:
        return api_bad_request('invalid id')
    try:
        db = get_thread_db()
        db_execute_with_retry(
            db,
            'DELETE FROM paper_library WHERE id=? AND user_id=?',
            (paper_id, DEFAULT_USER_ID),
        )
        logger.info('[Paper:Library] Deleted %s', paper_id[:16])
        return api_ok()
    except Exception as e:
        logger.error('[Paper:Library] Delete failed for %s: %s', paper_id[:16], e, exc_info=True)
        return api_internal_error(e)
