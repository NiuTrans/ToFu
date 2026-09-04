"""Paper routes — report task lifecycle (start/poll/venues/lookup/export/cache)."""

import asyncio
import base64
import os
import re
import time
from urllib.parse import unquote

from quart import Response, request


from lib.api_response import (
    api_bad_request,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.log import get_logger
from lib.paper.images.figures import (
    build_image_manifest,
    ensure_paper_images,
    load_image_manifest,
)
from lib.paper.images.injection import inject_images_into_report
from lib.paper.images.titles import (
    backfill_library_title,
    ensure_title_heading,
    extract_title_from_report,
)
from lib.paper.injection_guard import (
    injection_notice,
    sanitize_paper_text,
    wrap_untrusted,
)
from lib.paper.prompts import (
    _REPORT_PROMPT_EN,
    _REPORT_PROMPT_ZH,
    date_anchor_clause,
)
from lib.paper.report_runtime import (
    _new_report_task,
    _report_index_get,
    _report_runtime,
)
from lib.paper.review import (
    build_rebuttal_prompt,
    build_rebuttal_tool_instruction,
    build_review_prompt,
    build_review_tool_instruction,
    is_review_family,
    list_venues,
    make_review_lang,
    parse_report_lang,
)
from lib.paper_identity import (
    PAPER_IMG_DIR,
    _paper_hash,
    resolve_paper_hash,
    _safe_hash_dir,
)
from lib.request_parser import async_parse_body
from lib.paper.artifact_repository import PaperArtifactRepository
from lib.paper.contracts import PAPER_REPORT_MAX_SOURCE_CHARS
from lib.paper.library_repository import PaperLibraryRepository
from lib.paper.request_policy import paper_request_policy_telemetry
from routes.task_http import task_replay_cursor, task_replay_response

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    _parse_report_meta,
    _project_prefetched_report_siblings,
    _report_reopen_sibling_groups,
    _task_poll_fields,
    api_v1_paper_bp,
)
from routes.api_v1.auth import request_user_id


def run_report_task(*args, **kwargs):
    """Load the report agent/tool execution graph inside its task thread."""
    from lib.paper.report_engine.worker import run_report_task as implementation
    return implementation(*args, **kwargs)


def _cached_report_payload(
    row,
    phash,
    lang,
    *,
    user_id: int,
    siblings,
    images=None,
):
    """Project one prefetched report bundle without repository I/O."""
    if images is None:
        images = load_image_manifest(phash)
    enriched = inject_images_into_report(
        row.report,
        images,
        lang=parse_report_lang(lang)["ui_lang"],
        appendix=not is_review_family(lang),
        allow_images=not is_review_family(lang),
    )
    enriched = ensure_title_heading(enriched, phash, user_id=user_id)
    cache_meta = _parse_report_meta(row)
    enriched, cache_meta, insight_payload, checkpoints_payload = (
        _project_prefetched_report_siblings(
            enriched, cache_meta, phash, lang, siblings)
    )
    payload = {
        "cached": True,
        "report": enriched,
        "paper_hash": phash,
        "lang": lang,
        "meta": cache_meta,
    }
    if insight_payload:
        payload["insight"] = insight_payload
    if checkpoints_payload:
        payload["checkpoints"] = checkpoints_payload
    return payload


def _resolve_cached_report_payload(
    artifacts,
    phash,
    preferred_lang,
    *,
    user_id: int,
    fallback_lang=None,
    images=None,
    repair_library_title=False,
):
    """Read and project one report reopen through one Sidecar round-trip."""
    reopened = artifacts.reopen_report(
        phash,
        preferred_lang,
        fallback_lang,
        sibling_langs_by_base=_report_reopen_sibling_groups(
            preferred_lang, fallback_lang),
    )
    row = reopened.report
    if not row or not row.report:
        return None
    payload = _cached_report_payload(
        row,
        phash,
        row.lang,
        user_id=user_id,
        siblings=reopened.siblings,
        images=images,
    )
    if repair_library_title:
        resolved_title = ""
        card_title = extract_title_from_report(row.report)
        if card_title:
            try:
                resolved_title = backfill_library_title(
                    phash, card_title, user_id=user_id)
            except Exception as e:
                logger.warning(
                    "[Paper:Report] Cache-path title backfill failed "
                    "hash=%s: %s",
                    phash,
                    e,
                )
        payload["resolvedTitle"] = resolved_title
    return payload


@api_v1_paper_bp.route("/api/v1/paper/report/start", methods=["POST"])
async def start_report_task():
    """Start (or join) a background paper-report generation task.

    The task is keyed by owner, paper, language, model, and request-config
    fingerprint. An identical request joins live work; different experiment
    arms remain independent.

    Body JSON:
        paper_hash: str — preferred ingest-minted identity; a cache/live hit
            needs no paper body, and a new task resolves its bounded source
            from this owner's library row.
        paper_text: str — compatibility/fallback full text when no stored
            source is available or no valid paper_hash exists.
        model: str (optional) — LLM model to use
        lang: str (optional) — 'zh' for Chinese prompt, else English. Default 'en'.
        force: bool (optional) — bypass DB cache AND restart any running task.
        config: object (optional) — request-local experiment/runtime policy;
            explicit long-agent fields bypass canonical cache reads/writes.
        images: list (optional) — figure/table manifest to inject.

    Returns JSON:
        - DB cache hit: {ok: true, cached: true, report: str, paper_hash: str}
        - Task started/joined: {ok: true, task_id: str, paper_hash: str,
                                running: bool, existed: bool}
    """
    owner_user_id = int(request_user_id())
    artifacts = PaperArtifactRepository(owner_user_id)
    data = await async_parse_body()
    offered_text = data.get("paper_text", "")
    if offered_text is not None and not isinstance(offered_text, str):
        return api_bad_request("paper_text must be a string")
    paper_text = str(offered_text or "").strip()
    offered_hash = _safe_hash_dir(str(data.get("paper_hash") or "").strip())
    if not paper_text and not offered_hash:
        logger.warning(
            "[Paper:Report] Start request with no paper_hash or paper_text")
        return api_bad_request(
            "No paper_hash or paper_text provided",
            error_code="paper_source_required",
        )
    if paper_text and len(paper_text) < 100:
        logger.warning("[Paper:Report] Paper text too short: %d chars", len(paper_text))
        return api_bad_request("Paper text too short (< 100 chars)")

    model = data.get("model") or None
    lang = data.get("lang", "en") or "en"
    force = bool(data.get("force"))
    # Client-supplied title — sent so the title prepend works even when the
    # paper_library row hasn't been upserted yet (the frontend's
    # _saveActivePaperState() PUT is fire-and-forget and may race with the
    # report start). Stripped of trailing ``.pdf`` for cleanliness.
    client_title = (data.get("title") or "").strip()
    if client_title.lower().endswith(".pdf"):
        client_title = client_title[:-4].strip()

    # Resolve request-local policy before cache lookup and in-flight dedup.
    # Long-agent experiment controls must not read a canonical result produced
    # under another arm, and two different model/config requests must never
    # join the same worker. Personal-scope defaults are part of that identity.
    _report_cfg = dict(
        data.get("config") if isinstance(data.get("config"), dict) else {}
    )
    try:
        from quart import g as _g

        _is_headless = bool(getattr(_g, "paper_report_headless", False))
    except Exception as e:
        logger.debug("[Paper:Report] headless flag read failed: %s", e)
        _is_headless = False
    if _is_headless:
        from lib.agent_core.personal_scope import apply_headless_personal_defaults

        apply_headless_personal_defaults(_report_cfg)
    _request_policy = paper_request_policy_telemetry(
        model=model, config=_report_cfg)
    _execution_fingerprint = _request_policy["executionFingerprint"]
    _cache_isolated = _request_policy["cacheMode"] == "request_local"

    phash = resolve_paper_hash(offered_hash, paper_text)

    # Live work wins over a stale persisted result and needs neither figure
    # extraction nor a paper-body projection. The exact model/config identity
    # still keeps benchmark arms independent.
    existing = _report_index_get(
        phash,
        lang,
        user_id=owner_user_id,
        execution_fingerprint=_execution_fingerprint,
    )
    if existing and not force and existing["status"] in ("pending", "running", "done"):
        logger.info(
            "[Paper:Report] Joining existing task %s (status=%s) — hash=%s lang=%s",
            existing["task_id"],
            existing["status"],
            phash,
            lang,
        )
        return api_ok(
            {
                "task_id": existing["task_id"],
                "paper_hash": phash,
                "running": existing["status"] in ("pending", "running"),
                "existed": True,
            }
        )

    # Server is the source of truth for figure manifests. The client never
    # forwards the images list any more — we load (or extract) it here.
    images = load_image_manifest(phash)
    if not images:
        # Manifest missing — try to derive a filename from the request and
        # extract on-the-fly. Otherwise the report renders without figures.
        derived_fn = os.path.basename((data.get("filename") or "").strip())
        if derived_fn:
            images = await asyncio.to_thread(ensure_paper_images, derived_fn, phash)

    # DB cache check (unless force/request-local policy) — no task needed when
    # the canonical report was produced under the same shipped runtime.
    if not force and not _cache_isolated:
        try:
            cached_payload = await asyncio.to_thread(
                _resolve_cached_report_payload,
                artifacts,
                phash,
                lang,
                user_id=owner_user_id,
                images=images,
                repair_library_title=True,
            )
            if cached_payload:
                logger.info(
                    "[Paper:Report] DB cache hit — hash=%s lang=%s %d chars",
                    phash,
                    lang,
                    len(cached_payload["report"]),
                )
                return api_ok(cached_payload)
        except Exception as e:
            logger.warning(
                "[Paper:Report] DB cache lookup failed (will start task): %s", e
            )

    # Current clients send only the ingest-minted hash. Resolve the exact
    # owner-scoped source only after live/cache fast paths miss, and project no
    # more than the prompt can consume. Rolling clients may still send text.
    source_text_length = len(paper_text)
    if not paper_text:
        try:
            identity = await asyncio.to_thread(
                PaperLibraryRepository(owner_user_id).identity,
                phash,
                max_text_chars=PAPER_REPORT_MAX_SOURCE_CHARS,
            )
        except Exception as e:
            logger.warning(
                "[Paper:Report] Stored source lookup failed hash=%s: %s",
                phash,
                e,
            )
            identity = None
        if identity is not None:
            paper_text = identity.parsed_text.strip()
            source_text_length = identity.parsed_text_length
            if not client_title:
                client_title = identity.title.strip()
        if len(paper_text) < 100:
            logger.warning(
                "[Paper:Report] No usable stored source for hash=%s", phash)
            return api_bad_request(
                "Stored paper text unavailable; retry with paper_text",
                error_code="paper_source_required",
            )

    # Force: abort only after every fallible source-preparation gate succeeds,
    # so a missing legacy source cannot destroy the task it meant to replace.
    if existing and force:
        logger.info(
            "[Paper:Report] Force regen — aborting old task %s", existing["task_id"]
        )
        existing["abort_event"].set()
        existing["status"] = "error"
        existing["finished_at"] = time.time()

    # Decode the cache key. For ordinary reports this is {'kind':'report',
    # 'ui_lang': 'en'|'zh'}; for Review Mode the key is the composite
    # ``review:<venue>:<uilang>`` → {'kind':'review','venue':...,'ui_lang':...}.
    # The composite key flows UNCHANGED through the cache lookup + dedup index
    # above, so reviews never collide with the plain (paper_hash,'en') report.
    parsed = parse_report_lang(lang)
    ui_lang = parsed["ui_lang"]
    is_review = parsed["kind"] == "review"
    is_rebuttal = parsed["kind"] == "rebuttal"
    # Both a review and its rebuttal follow-up are text-only decision documents.
    is_review_kin = is_review or is_rebuttal

    max_text = PAPER_REPORT_MAX_SOURCE_CHARS
    truncated_text = paper_text[:max_text]
    if source_text_length > max_text:
        logger.info(
            "[Paper:Report] Truncating paper text from %d to %d chars",
            source_text_length,
            max_text,
        )

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

        audit_log(
            "paper_injection_detected",
            hash=phash,
            is_review=is_review,
            findings=_inj_findings,
        )
    # Review Mode + rebuttal are text-only — a peer review / author-response
    # reply carries no figures, so the image manifest is NOT offered (nothing
    # to embed).
    manifest = "" if is_review_kin else build_image_manifest(images, lang=ui_lang)
    if manifest:
        truncated_text = truncated_text + "\n\n---\n\n" + manifest
        logger.info(
            "[Paper:Report] Injected image manifest — %d images, hash=%s",
            len(images),
            phash,
        )

    if is_rebuttal:
        # Rebuttal follow-up: fetch the reviewer's ORIGINAL review for this
        # paper+venue (the sibling ``review:<venue>:<uilang>`` row) and the
        # author's rebuttal text (posted by the user), then run the SAME engine
        # to produce a follow-up reply + structured score-adjustment decision.
        author_rebuttal = (
            data.get("author_rebuttal") or data.get("rebuttal") or ""
        ).strip()
        if not author_rebuttal:
            logger.warning(
                "[Paper:Rebuttal] Start with no author_rebuttal — hash=%s", phash
            )
            return api_bad_request("No author_rebuttal provided")
        review_key = make_review_lang(parsed["venue"], ui_lang)
        original_review = ""
        try:
            rrow = await asyncio.to_thread(
                artifacts.get_report, phash, review_key)
            if rrow and rrow.report:
                original_review = rrow.report
        except Exception as e:
            logger.warning(
                "[Paper:Rebuttal] Original-review lookup failed hash=%s: %s", phash, e
            )
        if not original_review.strip():
            logger.warning(
                "[Paper:Rebuttal] No original review for hash=%s venue=%s ui=%s",
                phash,
                parsed["venue"],
                ui_lang,
            )
            return api_bad_request(
                "No original review found — generate the review first"
            )
        # The author rebuttal is UNTRUSTED (in the OpenReview flow it is written
        # by the paper authors), so sanitize + fence it exactly like the paper
        # text before splicing. The original review is OUR content (trusted).
        safe_rebuttal, _reb_inj = sanitize_paper_text(author_rebuttal[:40000])
        safe_rebuttal = wrap_untrusted(safe_rebuttal)
        if _reb_inj:
            from lib.log import audit_log

            audit_log(
                "paper_injection_detected",
                hash=phash,
                is_rebuttal=True,
                findings=_reb_inj,
            )
        # Fill slots. paper_text (already truncated+fenced above) goes LAST so a
        # brace inside the review/rebuttal is never mistaken for a later slot.
        prompt = (
            build_rebuttal_prompt(parsed["venue"], ui_lang)
            .replace("{original_review}", original_review)
            .replace("{author_rebuttal}", safe_rebuttal)
            .replace("{paper_text}", truncated_text)
        )
        tool_instruction = (
            date_anchor_clause(ui_lang)
            + injection_notice(ui_lang, _inj_findings or _reb_inj)
            + build_rebuttal_tool_instruction(ui_lang)
        )
        messages = [
            {"role": "system", "content": tool_instruction},
            {"role": "user", "content": prompt},
        ]
        task_id = (
            f"reb_{int(time.time() * 1000)}_{phash[:8]}_"
            f"{parsed['venue']}_{ui_lang}_{_execution_fingerprint[:8]}"
        )
        task = _new_report_task(
            task_id,
            phash,
            lang,
            model,
            client_title=client_title,
            ui_lang=ui_lang,
            config=_report_cfg,
            user_id=owner_user_id,
        )
        logger.info(
            "[Paper:Rebuttal] Starting task %s — venue=%s model=%s ui_lang=%s "
            "rebuttal_len=%d hash=%s",
            task_id,
            parsed["venue"],
            model,
            ui_lang,
            len(author_rebuttal),
            phash,
        )
        _report_runtime.spawn(task_id, run_report_task, task, messages, images)
        return api_ok(
            {
                "task_id": task_id,
                "paper_hash": phash,
                "running": True,
                "existed": False,
            }
        )

    if is_review:
        # Review Mode: venue-aware peer-review prompt (different output
        # structure + scorecard), but the SAME engine/tools/runtime.
        prompt_template = build_review_prompt(parsed["venue"], ui_lang)
        prompt = prompt_template.replace("{paper_text}", truncated_text)
        # Prepend the input-safety clause so the reviewer treats the fenced
        # paper block as data, and flags (never obeys) any embedded directive.
        tool_instruction = (
            date_anchor_clause(ui_lang)
            + injection_notice(ui_lang, _inj_findings)
            + build_review_tool_instruction(ui_lang)
        )
        messages = [
            {"role": "system", "content": tool_instruction},
            {"role": "user", "content": prompt},
        ]
        task_id = (
            f"rvw_{int(time.time() * 1000)}_{phash[:8]}_"
            f"{parsed['venue']}_{ui_lang}_{_execution_fingerprint[:8]}"
        )
        task = _new_report_task(
            task_id,
            phash,
            lang,
            model,
            client_title=client_title,
            ui_lang=ui_lang,
            config=_report_cfg,
            user_id=owner_user_id,
        )
        logger.info(
            "[Paper:Review] Starting task %s — venue=%s model=%s ui_lang=%s "
            "text_len=%d hash=%s",
            task_id,
            parsed["venue"],
            model,
            ui_lang,
            len(paper_text),
            phash,
        )
        _report_runtime.spawn(task_id, run_report_task, task, messages, images)
        return api_ok(
            {
                "task_id": task_id,
                "paper_hash": phash,
                "running": True,
                "existed": False,
            }
        )

    # ── Ordinary explainer report (unchanged path) ──
    # truncated_text is already sanitized + fenced above, so the report path
    # inherits the same injection hardening as review; prepend the input-safety
    # clause so the model treats the fenced block as data.
    prompt_template = _REPORT_PROMPT_ZH if ui_lang == "zh" else _REPORT_PROMPT_EN
    prompt = prompt_template.replace("{paper_text}", truncated_text)
    tool_instruction = (
        date_anchor_clause(ui_lang)
        + injection_notice(ui_lang, _inj_findings)
        + "You have access to the full standard tool set — web_search (batch) and "
        "fetch_url (batch) are your primary research instruments; read_files opens "
        "any local file a fetch stages (or an oversized tool result spills to "
        "disk); code_exec runs numeric checks.\n\n"
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
        "You may batch many queries per round for efficiency. Once you've gathered enough, "
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
        {"role": "system", "content": tool_instruction},
        {"role": "user", "content": prompt},
    ]

    task_id = (
        f"rpt_{int(time.time() * 1000)}_{phash[:8]}_{lang}_"
        f"{_execution_fingerprint[:8]}"
    )
    task = _new_report_task(
        task_id,
        phash,
        lang,
        model,
        client_title=client_title,
        ui_lang=ui_lang,
        config=_report_cfg,
        user_id=owner_user_id,
    )

    logger.info(
        "[Paper:Report] Starting task %s — model=%s lang=%s text_len=%d hash=%s",
        task_id,
        model,
        lang,
        len(paper_text),
        phash,
    )
    _report_runtime.spawn(task_id, run_report_task, task, messages, images)

    return api_ok(
        {
            "task_id": task_id,
            "paper_hash": phash,
            "running": True,
            "existed": False,
        }
    )


@api_v1_paper_bp.route("/api/v1/paper/report/poll", methods=["GET"])
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
    task_id = request.args.get("task_id", "").strip()
    cursor = task_replay_cursor(request.args)

    if not task_id:
        return api_bad_request("task_id required")

    # Direct lookup by task_id (runtime is keyed by task_id; dedup index
    # maps (paper_hash, lang) → task_id for the start endpoint).
    owner_user_id = int(request_user_id())
    task = _report_runtime.get_owned(task_id, user_id=owner_user_id)
    if not task:
        logger.debug("[Paper:Report:Poll] Unknown task_id=%s", task_id)
        return api_not_found("task not found (may have expired)")

    resp = _task_poll_fields(_report_runtime, task_id, cursor)
    resp["paper_hash"] = task["paper_hash"]
    if task["status"] == "done":
        resp["report"] = task.get("enriched_text") or task.get("full_text", "")
        if task.get("report_meta"):
            resp["meta"] = task["report_meta"]
        if task.get("resolved_title"):
            resp["resolvedTitle"] = task["resolved_title"]
    if task["status"] == "aborted":
        # User stopped generation — return whatever partial text was produced
        # so the frontend can show it read-only under a "stopped" banner.
        resp["partial"] = task.get("full_text", "")
    if task["status"] == "error":
        resp["error"] = task.get("error", "")
    return task_replay_response(resp)


@api_v1_paper_bp.route("/api/v1/paper/review/venues", methods=["GET"])
async def list_review_venues():
    """List the peer-review venues Review Mode supports.

    Returns: {ok: true, venues: [{key, name}, ...]} — registry order. The
    frontend uses this to populate the venue dropdown; the single source of
    truth is ``REVIEW_VENUES`` in ``lib/paper/review.py``.
    """
    return api_ok({"venues": list_venues()})


@api_v1_paper_bp.route("/api/v1/paper/report/lookup", methods=["POST"])
async def lookup_report_task():
    """Find live work and optionally resolve a persisted report in one request.

    Used by the frontend on tab re-entry / mode re-enter to see whether a
    task is already running server-side for this paper — so it can resume
    polling without starting a new one. New clients may set ``include_cache``;
    after live-task precedence, one owner-scoped storage aggregate resolves the
    requested/other plain-report language and only the selected row's additive
    artifacts. Composite Review/Rebuttal keys never receive a language fallback.

    Body JSON: {paper_hash: str, lang: str, include_cache: bool = false,
                paper_text: str (cache-resolution fallback)}
    Returns a task, a cached report with its resolved ``lang``, or {ok: false}.
    """
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    lang = data.get("lang", "en") or "en"
    include_cache = data.get("include_cache") is True
    if not phash:
        paper_text = (data.get("paper_text") or "").strip()
        if not include_cache or not paper_text:
            return api_bad_request("paper_hash required")
        phash = await asyncio.to_thread(_paper_hash, paper_text)
    task = _report_index_get(phash, lang, user_id=owner_user_id)
    if task and (
        not include_cache or task["status"] in ("pending", "running")
    ):
        return api_ok(
            {
                "task_id": task["task_id"],
                "status": task["status"],
                "paper_hash": phash,
            }
        )
    if include_cache:
        fallback_lang = None
        if lang in ("en", "zh"):
            fallback_lang = "zh" if lang == "en" else "en"
        try:
            cached_payload = await asyncio.to_thread(
                _resolve_cached_report_payload,
                PaperArtifactRepository(owner_user_id),
                phash,
                lang,
                user_id=owner_user_id,
                fallback_lang=fallback_lang,
            )
            if cached_payload:
                logger.debug(
                    "[Paper:Report:Lookup] Cache hit — hash=%s requested=%s resolved=%s",
                    phash,
                    lang,
                    cached_payload["lang"],
                )
                return api_ok(cached_payload)
        except Exception as e:
            logger.warning("[Paper:Report:Lookup] Cache resolution failed: %s", e)
    return api_payload({"ok": False}, 200)


@api_v1_paper_bp.route("/api/v1/paper/report/export", methods=["GET"])
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
    owner_user_id = int(request_user_id())
    artifacts = PaperArtifactRepository(owner_user_id)
    phash = (request.args.get("paper_hash") or "").strip()
    lang = (request.args.get("lang") or "en").strip() or "en"
    # Some reverse proxies (e.g. the VS Code web proxy) double-encode
    # percent-escapes in the query string: the client sends the composite
    # Review-Mode key ``review:neurips:en`` as ``review%3Aneurips%3Aen``, the
    # proxy re-encodes the ``%`` → ``review%253Aneurips%253Aen``, and Quart
    # decodes only once so the handler sees a literal ``review%3A…`` that
    # matches no stored row. Plain-language report exports (``en``/``zh``) have
    # no reserved chars so they're unaffected — only review exports 404'd.
    # Undo one extra decode layer when the value still carries ``%XX`` escapes.
    if "%" in lang:
        try:
            _decoded = unquote(lang)
            if _decoded != lang:
                logger.debug(
                    "[Paper:Report:Export] Decoded double-encoded lang %r -> %r",
                    lang,
                    _decoded,
                )
                lang = _decoded
        except Exception as e:
            logger.debug("[Paper:Report:Export] lang unquote failed: %s", e)
    fmt = (request.args.get("format") or "md").strip().lower()
    # `pdf` is a client-side rendering of the HTML body via window.print() —
    # the server emits the same HTML doc but inline (no attachment) and with
    # an auto-print bootstrap so the new tab opens the print dialog.
    if fmt not in ("md", "html", "pdf"):
        return api_bad_request("format must be md, html, or pdf")
    if not _safe_hash_dir(phash):
        return api_bad_request("invalid paper_hash")
    inline_html = (fmt == "pdf") or (request.args.get("inline") in ("1", "true", "yes"))

    try:
        row = await asyncio.to_thread(artifacts.get_report, phash, lang)
    except Exception as e:
        logger.error("[Paper:Report:Export] Lookup failed: %s", e, exc_info=True)
        return api_internal_error("lookup failed")
    if not row or not row.report:
        return api_not_found("report not found")

    images = load_image_manifest(phash)
    # Review-Mode rows carry a composite lang key; image injection / appendix
    # headings need the REAL UI language, not the raw cache key.
    _inj_lang = parse_report_lang(lang)["ui_lang"]
    body_md = inject_images_into_report(
        row.report,
        images,
        lang=_inj_lang,
        appendix=not is_review_family(lang),
        allow_images=not is_review_family(lang),
    )
    body_md = ensure_title_heading(
        body_md, phash, user_id=owner_user_id)

    # Get the paper title for the export filename / page title
    title = "Paper Report"
    try:
        trow = await asyncio.to_thread(
            PaperLibraryRepository(owner_user_id).identity,
            phash,
            max_text_chars=0,
        )
        if trow:
            title = trow.title or (
                f"arXiv:{trow.arxiv_id}" if trow.arxiv_id else title
            )
    except Exception as e:
        logger.debug("[Paper:Report:Export] Title lookup failed: %s", e)

    safe_slug = re.sub(r"[^\w\-]+", "_", title)[:80] or "paper"

    if fmt == "md":
        return Response(
            body_md,
            mimetype="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="paper_report_{safe_slug}.md"'
            },
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
        return f"\x02MATH{len(math_store) - 1}\x03"

    md_protected = body_md
    # Display math first ($$...$$ and \[...\]). Order matters — $$ would
    # otherwise be eaten by the inline $ pattern.
    md_protected = re.sub(r"\$\$[\s\S]+?\$\$", _stash_math, md_protected)
    md_protected = re.sub(r"\\\[[\s\S]+?\\\]", _stash_math, md_protected)
    # Inline math: $...$ on a single line, no $ inside, no | (table cell
    # separator) to avoid swallowing rows when a cell holds a literal $.
    md_protected = re.sub(
        r"\$(?!\$)((?:[^$\\\n|]|\\.)+?)\$(?!\$)",
        _stash_math,
        md_protected,
    )
    md_protected = re.sub(r"\\\(.+?\\\)", _stash_math, md_protected)

    try:
        import markdown as _md

        body_html = _md.markdown(
            md_protected,
            extensions=["tables", "fenced_code", "attr_list", "sane_lists"],
            output_format="html5",
        )
    except Exception as e:
        logger.error(
            "[Paper:Report:Export] markdown render failed: %s", e, exc_info=True
        )
        return api_internal_error("render failed")

    # Restore math placeholders. KaTeX's auto-render extension (loaded
    # below) will scan for $...$ / $$...$$ on the client and replace with
    # rendered formulas — this works for both Standalone HTML download and
    # the PDF print preview.
    def _unstash(m):
        idx = int(m.group(1))
        return math_store[idx] if 0 <= idx < len(math_store) else m.group(0)

    body_html = re.sub(r"\x02MATH(\d+)\x03", _unstash, body_html)

    # Embed paper-image URLs as base64 data: URIs so the standalone HTML
    # file works offline (no server reachability required) — this is the
    # common case for users who download the report to share or archive.
    # Other root-anchored URLs (e.g. third-party `/static/...`) are
    # rewritten to absolute http(s) URLs against the server origin.
    origin = request.host_url.rstrip("/")

    def _embed_paper_image(match):
        attr = match.group(1)
        url = match.group(2)
        m = re.match(r"^/api/paper/images/([a-f0-9]{8,64})/([\w\-.]+)$", url)
        if not m:
            return attr + origin + url
        ph, fn = m.group(1), m.group(2)
        ph_safe = _safe_hash_dir(ph)
        if not ph_safe:
            return attr + origin + url
        fpath = os.path.join(PAPER_IMG_DIR, ph_safe, os.path.basename(fn))
        if not os.path.isfile(fpath):
            logger.debug(
                "[Paper:Report:Export] Image missing on disk, falling back to URL: %s",
                fpath,
            )
            return attr + origin + url
        try:
            with open(fpath, "rb") as f:
                raw = f.read()
        except Exception as e:
            logger.warning(
                "[Paper:Report:Export] Image read failed for %s: %s", fpath, e
            )
            return attr + origin + url
        ext = os.path.splitext(fn)[1].lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"{attr}data:{mime};base64,{b64}"

    body_html = re.sub(
        r'((?:src|href)=["\'])(/[^"\']+)',
        _embed_paper_image,
        body_html,
    )

    safe_title = (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    css = (
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
        "max-width:820px;margin:32px auto;padding:0 24px;line-height:1.7;color:#222;background:#fff}"
        "h1,h2,h3{margin-top:1.6em;line-height:1.3}"
        "h2{border-bottom:1px solid #eee;padding-bottom:6px}"
        "img{max-width:100%;height:auto;display:block;margin:14px auto;border:1px solid #eaeaea;"
        "border-radius:6px;padding:4px;background:#fff}"
        "pre{background:#f6f8fa;padding:12px 14px;border-radius:6px;overflow:auto;font-size:13px}"
        "code{background:#f1f1f1;padding:1px 5px;border-radius:3px;font-size:90%}"
        "pre code{background:none;padding:0}"
        "blockquote{border-left:3px solid #6366f1;padding-left:12px;margin:8px 0;color:#555}"
        "table{border-collapse:collapse;margin:8px 0;font-size:13px}"
        "th,td{border:1px solid #e0e0e0;padding:6px 10px}th{background:#fafafa}"
        "@media print{body{margin:0;max-width:none}img{break-inside:avoid}h2,h3{break-after:avoid}}"
    )
    # Avoid duplicate H1: the report body itself now starts with `# Title`
    # (prepended in run_report_task). For older cached reports without it,
    # fall back to the wrapper H1.
    body_starts_with_h1 = bool(re.match(r"\s*<h1\b", body_html))
    title_block = "" if body_starts_with_h1 else f"<h1>{safe_title}</h1>"

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
        "<script defer "
        'src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" '
        'crossorigin="anonymous"></script>'
        "<script defer "
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
        (
            '<script>window.addEventListener("load",function(){'
            "function waitKatex(cb){"
            'if(window.__katexReady||!document.querySelector("script[src*=\\"auto-render\\"]"))cb();'
            "else setTimeout(function(){waitKatex(cb);},120);}"
            "var imgs=document.images,pending=imgs.length?0:0;"
            "function go(){setTimeout(function(){"
            "try{window.focus();window.print();}catch(e){}},400);}"
            "function r(){pending--;if(pending<=0)waitKatex(go);}"
            "for(var i=0;i<imgs.length;i++){if(!imgs[i].complete){pending++;"
            'imgs[i].addEventListener("load",r);imgs[i].addEventListener("error",r);}}'
            "if(pending===0)waitKatex(go);"
            "});</script>"
        )
        if fmt == "pdf"
        else ""
    )
    html_doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>{safe_title}</title><style>{css}</style>{katex_assets}{auto_print_js}</head><body>"
        f"{title_block}{body_html}</body></html>"
    )
    if inline_html:
        return Response(html_doc, mimetype="text/html; charset=utf-8")
    return Response(
        html_doc,
        mimetype="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="paper_report_{safe_slug}.html"'
        },
    )


@api_v1_paper_bp.route("/api/v1/paper/report/cache", methods=["POST"])
async def get_report_cache():
    """Lookup cached report by paper hash.

    Body JSON:
        paper_hash: str — precomputed hash (preferred, avoids re-sending full text)
        paper_text: str — full text of the paper (fallback, used to compute hash)
        lang: str (optional) — language. Default 'en'.
    Returns:
        { ok: true, report: str, paper_hash: str } or { ok: false }
    """
    owner_user_id = int(request_user_id())
    artifacts = PaperArtifactRepository(owner_user_id)
    data = await async_parse_body()
    phash = data.get("paper_hash", "").strip()
    lang = data.get("lang", "en") or "en"

    # Prefer pre-computed hash; fall back to computing from text
    if not phash:
        paper_text = data.get("paper_text", "").strip()
        if not paper_text:
            return api_bad_request("No paper_hash or paper_text")
        phash = _paper_hash(paper_text)

    try:
        cached_payload = await asyncio.to_thread(
            _resolve_cached_report_payload,
            artifacts,
            phash,
            lang,
            user_id=owner_user_id,
        )
        if cached_payload:
            logger.debug("[Paper:Report:Cache] Hit — hash=%s lang=%s", phash, lang)
            return api_ok(cached_payload)
    except Exception as e:
        logger.warning("[Paper:Report:Cache] Lookup failed: %s", e)

    return api_payload({"ok": False}, 200)
