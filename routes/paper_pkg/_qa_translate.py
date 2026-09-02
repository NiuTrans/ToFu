"""Paper routes — Q&A and translation task lifecycle endpoints."""

import asyncio
import re
import time

from quart import request


from lib.api_response import (
    api_bad_request,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.log import get_logger
from lib.paper.qa_context import build_qa_messages
from lib.paper.qa_runtime import (
    _new_qa_task,
    _qa_runtime,
)
from lib.paper.translate_runtime import (
    _new_translate_task,
    _translate_index_get,
    _translate_runtime,
)
from lib.paper_identity import _paper_hash
from lib.request_parser import async_parse_body
from lib.paper.artifact_repository import PaperArtifactRepository
from routes.task_http import task_replay_cursor, task_replay_response

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    _task_poll_fields,
    api_v1_paper_bp,
)
from routes.api_v1.auth import request_user_id


def _run_qa_task(*args, **kwargs):
    """Activate the paper QA agent only inside its background task."""
    from lib.paper.qa_engine import _run_qa_task as implementation
    return implementation(*args, **kwargs)


def _run_translate_task(*args, **kwargs):
    """Activate the translation engine only inside its background task."""
    from lib.paper.translate_engine import _run_translate_task as implementation
    return implementation(*args, **kwargs)


# ══════════════════════════════════════════════════════
#  Agentic Q&A (server-owned TaskRuntime task)
# ══════════════════════════════════════════════════════


@api_v1_paper_bp.route("/api/v1/paper/qa/start", methods=["POST"])
async def start_qa_task():
    """Start a background agentic Q&A task for one question.

    This runs a TaskRuntime tool-calling loop (web_search / fetch_url) with
    section-aware context: the
    full generated report + the question-relevant paper sections (no blind
    100k truncation). The frontend polls ``/api/v1/paper/qa/poll``.

    Body JSON:
        question: str — the user's question (required)
        paper_text: str — full parsed paper text (required)
        paper_hash: str (optional) — cache key; computed from text if missing.
        lang: str (optional) — 'zh' for Chinese answer, else 'en'. Default 'en'.
        history: list (optional) — prior [{role, content}, ...] dialogue turns.
        model: str (optional)
        config: object (optional) — request-local experiment/runtime policy
        title: str (optional) — client title (race fallback for logging).

    Returns: {ok: true, task_id, paper_hash, running: true}
    """
    owner_user_id = int(request_user_id())
    artifacts = PaperArtifactRepository(owner_user_id)
    data = await async_parse_body()
    question = (data.get("question") or "").strip()
    paper_text = (data.get("paper_text") or "").strip()
    if not question:
        return api_bad_request("No question provided")
    if not paper_text:
        return api_bad_request("No paper_text provided")

    lang = data.get("lang", "en") or "en"
    model = data.get("model") or None
    phash = (data.get("paper_hash") or "").strip() or _paper_hash(paper_text)
    history = data.get("history") if isinstance(data.get("history"), list) else []
    client_title = (data.get("title") or "").strip()
    request_config = (
        dict(data.get("config")) if isinstance(data.get("config"), dict)
        else {})

    # Look up the generated report for this paper (so the model can answer
    # questions about report-only claims). Best-effort — Q&A still works
    # without a report (model answers from the paper sections alone).
    report_md = ""
    try:
        row = await asyncio.to_thread(artifacts.get_report, phash, lang)
        if row and row.report:
            report_md = row.report
        else:
            # Fall back to the report in the other language if the requested
            # one isn't generated yet — a report in any language still helps.
            row2 = await asyncio.to_thread(artifacts.latest_report, phash)
            if row2 and row2.report:
                report_md = row2.report
    except Exception as e:
        logger.warning(
            "[Paper:QA] Report lookup failed for hash=%s (Q&A continues "
            "without report): %s",
            phash,
            e,
        )

    messages, diag = build_qa_messages(
        question, paper_text, report_md, history=history, lang=lang
    )

    task_id = f"qa_{int(time.time() * 1000)}_{phash[:8]}_{lang}"
    task = _new_qa_task(
        task_id, phash, lang, model, user_id=owner_user_id,
        question=question, client_title=client_title, config=request_config,
    )
    logger.info(
        "[Paper:QA] Starting task %s — hash=%s lang=%s sections=%d/%d "
        "report=%s q=%.80s",
        task_id,
        phash,
        lang,
        diag["n_sections_selected"],
        diag["n_sections_total"],
        diag["report_present"],
        question,
    )
    _qa_runtime.spawn(task_id, _run_qa_task, task, messages)

    return api_ok(
        {
            "task_id": task_id,
            "paper_hash": phash,
            "running": True,
            "reportPresent": diag["report_present"],
        }
    )


@api_v1_paper_bp.route("/api/v1/paper/qa/poll", methods=["GET"])
async def poll_qa_task():
    """Poll a Q&A task for new events (same shape as the report poll).

    Query params: task_id, cursor (default 0).
    Returns: {ok, status, events, next_cursor, paper_hash, answer? (if done)}.
    """
    task_id = request.args.get("task_id", "").strip()
    cursor = task_replay_cursor(request.args)
    if not task_id:
        return api_bad_request("task_id required")

    owner_user_id = int(request_user_id())
    task = _qa_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        logger.debug("[Paper:QA:Poll] Unknown task_id=%s", task_id)
        return api_not_found("task not found (may have expired)")

    resp = _task_poll_fields(_qa_runtime, task_id, cursor)
    resp["paper_hash"] = task["paper_hash"]
    if task["status"] == "done":
        resp["answer"] = task.get("full_text", "")
    if task["status"] == "error":
        resp["error"] = task.get("error", "")
    return task_replay_response(resp)


@api_v1_paper_bp.route("/api/v1/paper/translate/start", methods=["POST"])
async def start_translate_task():
    """Start (or join) a Babel-mode whole-paper translation task.

    Body JSON:
        paper_text: str
        lang: str — target language (e.g. 'zh', 'en', 'ja')
        paper_hash: str (optional) — used as cache key; computed if missing.
        model: str (optional)
        force: bool (optional)
    """
    owner_user_id = int(request_user_id())
    artifacts = PaperArtifactRepository(owner_user_id)
    data = await async_parse_body()
    paper_text = (data.get("paper_text") or "").strip()
    lang = (data.get("lang") or "").strip()
    if not paper_text:
        return api_bad_request("No paper_text")
    if not lang:
        return api_bad_request("lang required")

    phash = (data.get("paper_hash") or "").strip() or _paper_hash(paper_text)
    model = data.get("model") or None
    force = bool(data.get("force"))

    if not force:
        try:
            row = await asyncio.to_thread(
                artifacts.get_translation, phash, lang)
            if row and row.text:
                logger.info(
                    "[Paper:Translate] DB cache hit — hash=%s lang=%s %d chars",
                    phash,
                    lang,
                    len(row.text),
                )
                return api_ok(
                    {"cached": True, "text": row.text, "paper_hash": phash}
                )
        except Exception as e:
            logger.warning("[Paper:Translate] Cache lookup failed: %s", e)

    existing = _translate_index_get(
        phash, lang, user_id=owner_user_id)
    if existing and not force and existing["status"] in ("pending", "running", "done"):
        return api_ok(
            {
                "task_id": existing["task_id"],
                "paper_hash": phash,
                "existed": True,
                "running": existing["status"] in ("pending", "running"),
            }
        )
    if existing and force:
        existing["abort_event"].set()
        existing["status"] = "error"
        existing["finished_at"] = time.time()

    # The task_id is an OPAQUE handle echoed back verbatim in the poll/abort
    # URL — it must be URL-safe. A composite review key (e.g. 'review:neurips:zh')
    # carries colons that, over a proxy tunnel that re-encodes '%', arrive
    # double-encoded ('%253A') and never match the runtime's dict key → the poll
    # 404s forever and the UI reports "translation failed". Sanitize the lang
    # segment for the id only; the real composite `lang` still keys the cache,
    # dedup index, and DB row unchanged.
    lang_slug = re.sub(r"[^A-Za-z0-9]+", "_", lang).strip("_") or "x"
    task_id = f"tr_{int(time.time() * 1000)}_{phash[:8]}_{lang_slug}"
    task = _new_translate_task(
        task_id, phash, lang, model, user_id=owner_user_id)

    _translate_runtime.spawn(task_id, _run_translate_task, task, paper_text)

    return api_ok(
        {"task_id": task_id, "paper_hash": phash, "running": True, "existed": False}
    )


@api_v1_paper_bp.route("/api/v1/paper/translate/poll", methods=["GET"])
async def poll_translate_task():
    """Poll a translation task for new events."""
    task_id = request.args.get("task_id", "").strip()
    cursor = task_replay_cursor(request.args)
    if not task_id:
        return api_bad_request("task_id required")

    owner_user_id = int(request_user_id())
    task = _translate_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        return api_not_found("task not found (expired?)")

    resp = _task_poll_fields(_translate_runtime, task_id, cursor)
    resp.update(
        paper_hash=task["paper_hash"],
        progress=dict(task["progress"]),
    )
    if task["status"] == "done":
        resp["text"] = task.get("full_text", "")
    if task["status"] == "error":
        resp["error"] = task.get("error", "")
    return task_replay_response(resp)


@api_v1_paper_bp.route("/api/v1/paper/translate/lookup", methods=["POST"])
async def lookup_translate_task():
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    lang = (data.get("lang") or "").strip()
    if not phash or not lang:
        return api_bad_request("paper_hash and lang required")
    task = _translate_index_get(
        phash, lang, user_id=owner_user_id)
    if task:
        return api_ok(
            {"task_id": task["task_id"], "status": task["status"], "paper_hash": phash}
        )
    return api_payload({"ok": False}, 200)


@api_v1_paper_bp.route("/api/v1/paper/translate/cache", methods=["POST"])
async def get_translate_cache():
    owner_user_id = int(request_user_id())
    artifacts = PaperArtifactRepository(owner_user_id)
    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    lang = (data.get("lang") or "").strip()
    if not phash:
        paper_text = (data.get("paper_text") or "").strip()
        if not paper_text:
            return api_bad_request("paper_hash or paper_text required")
        phash = _paper_hash(paper_text)
    if not lang:
        return api_bad_request("lang required")
    try:
        row = await asyncio.to_thread(artifacts.get_translation, phash, lang)
        if row and row.text:
            return api_ok({"text": row.text, "paper_hash": phash})
    except Exception as e:
        logger.warning("[Paper:Translate:Cache] Lookup failed: %s", e)
    return api_payload({"ok": False}, 200)
