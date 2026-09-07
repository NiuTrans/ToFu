"""Paper routes — arXiv search and recommendation task endpoints."""

import asyncio
import time

from quart import request


from lib.api_response import (
    api_bad_request,
    api_error,
    api_not_found,
    api_ok,
)
from lib.log import get_logger
from lib.paper.arxiv_errors import ArxivQuerySyntaxError
from lib.paper.recommend_runtime import (
    _cleanup_stale_recommend_tasks,
    _new_recommend_task,
    _recommend_key,
    _recommend_runtime,
)
from lib.request_parser import async_parse_body
from routes.task_http import task_replay_cursor, task_replay_response

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    _task_poll_fields,
    api_v1_paper_bp,
)
from routes.api_v1.auth import request_user_id


def search_arxiv_explained(*args, **kwargs):
    """Load search activation and Atom policy for a real query only."""
    from lib.paper.arxiv import search_arxiv_explained as implementation

    return implementation(*args, **kwargs)


def recommend_papers(*args, **kwargs):
    """Load the grounded recommendation engine for a blocking request only."""
    from lib.paper.recommend_engine._events import recommend_papers as implementation
    return implementation(*args, **kwargs)


def _run_recommend_task(*args, **kwargs):
    """Load the streaming recommendation worker inside its task thread."""
    from lib.paper.recommend_task import _run_recommend_task as implementation
    return implementation(*args, **kwargs)


@api_v1_paper_bp.route("/api/v1/paper/search-arxiv", methods=["POST"])
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
    query = (data.get("query") or "").strip()
    if not query:
        logger.warning("[Paper:arXiv:Search] Empty query")
        return api_bad_request("No query provided")

    try:
        max_results = int(data.get("max_results") or 10)
    except (ValueError, TypeError) as e:
        logger.debug(
            "[Paper:arXiv:Search] non-int max_results (%s) — defaulting to 10", e
        )
        max_results = 10

    # A failure MUST surface as an error, never as an empty result list:
    # 2026-07-28 the live server 500'd every search for ~1h (stale process
    # holding a pre-search_by_query tofu_search) and the frontend rendered
    # every one of them as "no papers found". Three failure shapes, three
    # explicit exits — the ok:[] payload is reserved for a query that ran
    # clean and legitimately matched nothing.
    try:
        results, search_error = await asyncio.to_thread(
            search_arxiv_explained, query, max_results
        )
    except ArxivQuerySyntaxError as e:
        logger.warning(
            "[Paper:arXiv:Search] rejected built-syntax query %.120s: %s", query, e
        )
        return api_bad_request(str(e))
    except Exception as e:
        logger.error(
            "[Paper:arXiv:Search] route failed for %.120s: %s", query, e, exc_info=True
        )
        return api_error("arXiv search failed: %s" % e, status=502)
    if search_error:
        return api_error("arXiv search failed: %s" % search_error, status=502)
    return api_ok({"query": query, "results": results})


@api_v1_paper_bp.route("/api/v1/paper/recommend", methods=["POST"])
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
    description = (data.get("description") or "").strip()
    if not description:
        logger.warning("[Paper:Recommend] Empty description")
        return api_bad_request("No description provided")

    try:
        max_results = int(data.get("max_results") or 6)
    except (ValueError, TypeError) as e:
        logger.debug("[Paper:Recommend] non-int max_results (%s) — defaulting to 6", e)
        max_results = 6

    owner_user_id = int(request_user_id())
    out = await asyncio.to_thread(
        recommend_papers, description, max_results, user_id=owner_user_id)
    return api_ok(out)


@api_v1_paper_bp.route("/api/v1/paper/recommend/start", methods=["POST"])
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
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    description = (data.get("description") or "").strip()
    if not description:
        logger.warning("[Paper:Recommend] Empty description (stream)")
        return api_bad_request("No description provided")

    try:
        max_results = int(data.get("max_results") or 6)
    except (ValueError, TypeError) as e:
        logger.debug(
            "[Paper:Recommend] non-int max_results (stream) (%s) — defaulting to 6", e
        )
        max_results = 6

    task_id = f"rec_{int(time.time() * 1000)}_{_recommend_key(description)}"
    task = _new_recommend_task(
        task_id, description, max_results, user_id=owner_user_id)
    logger.info(
        "[Paper:Recommend] Starting stream task %s — max=%d desc=%.80s",
        task_id,
        max_results,
        description,
    )
    _recommend_runtime.spawn(task_id, _run_recommend_task, task)

    return api_ok({"task_id": task_id, "running": True})


@api_v1_paper_bp.route("/api/v1/paper/recommend/poll", methods=["GET"])
async def poll_recommend_task():
    """Poll a streaming recommend task for new events (same shape as QA poll).

    Query params: task_id, cursor (default 0).
    Returns: {ok, status, events, next_cursor, results? / correction? (if done)}.
    """
    task_id = request.args.get("task_id", "").strip()
    cursor = task_replay_cursor(request.args)
    if not task_id:
        return api_bad_request("task_id required")

    owner_user_id = int(request_user_id())
    task = _recommend_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        logger.debug("[Paper:Recommend:Poll] Unknown task_id=%s", task_id)
        return api_not_found("task not found (may have expired)")

    resp = _task_poll_fields(_recommend_runtime, task_id, cursor)
    if task["status"] == "done":
        resp["results"] = task.get("results", [])
        resp["correction"] = task.get("correction")
        resp["llmError"] = bool(task.get("llmError"))
    if task["status"] == "error":
        resp["error"] = task.get("error", "")
        resp["llmError"] = bool(task.get("llmError"))
    return task_replay_response(resp)


@api_v1_paper_bp.route("/api/v1/paper/recommend/abort", methods=["POST"])
async def abort_recommend_task():
    """Abort a running streaming recommend task (best-effort cooperative stop)."""
    data = await async_parse_body()
    task_id = (data.get("task_id") or "").strip()
    if not task_id:
        return api_bad_request("task_id required")
    owner_user_id = int(request_user_id())
    task = _recommend_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        return api_not_found("task not found")
    # Keep abort idempotent for already-terminal tasks while ensuring the
    # ownership check and mutation are atomic for live work.
    _recommend_runtime.abort_owned(task_id, user_id=owner_user_id)
    logger.info("[Paper:Recommend] Abort requested for task %s", task_id)
    _cleanup_stale_recommend_tasks()
    return api_ok({"aborted": True})
