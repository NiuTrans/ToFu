"""Paper routes — section-deepening task and paper notes CRUD endpoints."""

import asyncio
import time
import uuid

from quart import request


from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
)
from lib.log import get_logger
from lib.paper.artifact_repository import PaperArtifactRepository, PaperNote
from lib.paper.review import parse_report_lang
from lib.request_parser import async_parse_body

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    api_v1_paper_bp,
)
from routes.api_v1.auth import request_user_id


@api_v1_paper_bp.route("/api/v1/paper/deepen/start", methods=["POST"])
async def start_deepen_task():
    """Start (or join) an on-demand section-deepening task.

    Body JSON:
        paper_hash: str (required)
        section_idx: int (required) — h2/h3 index in the stored report body
        mode: 'deeper' | 'derive' | 'eli5' (required)
        lang: str (optional, default 'en') — the report's language key
        paper_text: str (optional) — parsed paper text for context
        model: str (optional)
        config: object (optional) — request-local experiment/runtime policy

    Returns {ok, cached, content} on a fresh cache hit (never re-bills), or
    {ok, task_id, running} for a live task to poll via the generic
    /api/v1/paper/deepen/poll/<task_id>.
    """
    from lib.paper.deepen_engine import start_deepen

    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    mode = (data.get("mode") or "").strip()
    lang = (data.get("lang") or "en").strip() or "en"
    paper_text = data.get("paper_text") or ""
    model = (data.get("model") or "").strip() or None
    request_config = (
        dict(data.get("config")) if isinstance(data.get("config"), dict)
        else {})
    try:
        section_idx = int(data.get("section_idx"))
    except (TypeError, ValueError) as e:
        logger.debug(
            "[Paper] bad section_idx %r — defaulting to -1: %s",
            data.get("section_idx"),
            e,
        )
        section_idx = -1
    if not phash:
        return api_bad_request("No paper_hash provided")
    if not mode:
        return api_bad_request("No mode provided")

    ui_lang = parse_report_lang(lang)["ui_lang"]
    result = await asyncio.to_thread(
        start_deepen,
        phash,
        lang,
        mode,
        section_idx,
        paper_text,
        model=model,
        ui_lang=ui_lang,
        user_id=owner_user_id,
        config=request_config,
    )

    if "error" in result:
        msg, status = result["error"]
        logger.info(
            "[Paper:Deepen] start rejected — hash=%s mode=%s sec=%s: %s",
            phash,
            mode,
            section_idx,
            msg,
        )
        return api_error(msg, status=status)
    if result.get("cached"):
        logger.info(
            "[Paper:Deepen] cache hit — hash=%s mode=%s sec=%d",
            phash,
            mode,
            section_idx,
        )
        return api_ok(
            {
                "cached": True,
                "content": result["content"],
                "usage": result.get("usage"),
                "section": result.get("section"),
                "mode": mode,
                "sectionIdx": section_idx,
            }
        )
    task = result.get("joined") or result.get("task")
    return api_ok(
        {
            "task_id": task["task_id"],
            "paper_hash": phash,
            "running": task["status"] in ("pending", "running"),
            "existed": bool(result.get("joined")),
            "mode": mode,
            "sectionIdx": section_idx,
        }
    )


# ══════════════════════════════════════════════════════
#  Reader margin notes (reading-xp P4)
# ══════════════════════════════════════════════════════


@api_v1_paper_bp.route("/api/v1/paper/notes", methods=["GET"])
async def list_paper_notes():
    """List the reader's margin notes for one paper+language (oldest first)."""
    owner_user_id = int(request_user_id())
    phash = (request.args.get("paper_hash") or "").strip()
    lang = (request.args.get("lang") or "").strip()
    if not phash:
        return api_bad_request("No paper_hash provided")
    try:
        rows = await asyncio.to_thread(
            PaperArtifactRepository(owner_user_id).list_notes, phash, lang)
    except Exception as e:
        logger.warning("[Paper:Notes] list failed hash=%s: %s", phash, e)
        return api_internal_error("failed to load notes")
    return api_ok({"notes": [row.to_projection() for row in rows]})


@api_v1_paper_bp.route("/api/v1/paper/notes", methods=["POST"])
async def create_paper_note():
    """Create a margin note. Body: {paper_hash, lang, anchor{…}, note}."""
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    lang = (data.get("lang") or "").strip()
    note_text = (data.get("note") or "").strip()
    anchor = data.get("anchor") if isinstance(data.get("anchor"), dict) else {}
    if not phash:
        return api_bad_request("No paper_hash provided")
    if not note_text:
        return api_bad_request("Empty note")
    # Bound the anchor payload: only the three addressing fields, quote capped.
    safe_anchor = {
        "heading_idx": anchor.get("heading_idx"),
        "char_offset": anchor.get("char_offset"),
        "quote": str(anchor.get("quote") or "")[:400],
    }
    note_id = f"pn_{uuid.uuid4().hex}"
    now = int(time.time())
    note = PaperNote(
        note_id=note_id,
        paper_hash=phash,
        lang=lang,
        anchor=safe_anchor,
        note=note_text,
        created_at=now,
        updated_at=now,
    )
    try:
        await asyncio.to_thread(
            PaperArtifactRepository(owner_user_id).create_note,
            note,
            command_id=f'paper.note.create:{uuid.uuid4().hex}',
        )
    except Exception as e:
        logger.error("[Paper:Notes] create failed hash=%s: %s", phash, e, exc_info=True)
        return api_internal_error("failed to save note")
    logger.info(
        "[Paper:Notes] created %s — hash=%s lang=%s %d chars",
        note_id,
        phash,
        lang,
        len(note_text),
    )
    return api_ok(
        {
            "note": note.to_projection()
        }
    )


@api_v1_paper_bp.route("/api/v1/paper/notes/<note_id>", methods=["PATCH"])
async def update_paper_note(note_id):
    """Edit a note's text (the anchor never moves)."""
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    note_text = (data.get("note") or "").strip()
    if not note_text:
        return api_bad_request("Empty note")
    try:
        updated = await asyncio.to_thread(
            PaperArtifactRepository(owner_user_id).update_note,
            note_id,
            note_text,
            int(time.time()),
            command_id=f'paper.note.update:{uuid.uuid4().hex}',
        )
        if not updated:
            return api_not_found("note not found")
    except Exception as e:
        logger.error("[Paper:Notes] update failed %s: %s", note_id, e, exc_info=True)
        return api_internal_error("failed to update note")
    return api_ok({"id": note_id, "note": note_text})


@api_v1_paper_bp.route("/api/v1/paper/notes/<note_id>", methods=["DELETE"])
async def delete_paper_note(note_id):
    """Delete a note (idempotent — deleting a missing id still returns ok)."""
    owner_user_id = int(request_user_id())
    try:
        await asyncio.to_thread(
            PaperArtifactRepository(owner_user_id).delete_note,
            note_id,
            command_id=f'paper.note.delete:{uuid.uuid4().hex}',
        )
    except Exception as e:
        logger.error("[Paper:Notes] delete failed %s: %s", note_id, e, exc_info=True)
        return api_internal_error("failed to delete note")
    logger.info("[Paper:Notes] deleted %s", note_id)
    return api_ok({"deleted": note_id})
