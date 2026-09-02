"""Paper routes — OpenReview autofill, image extraction, and image serving."""

import asyncio
import os
import re

from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.log import get_logger
from lib.paper.artifact_repository import PaperArtifactRepository
from lib.paper.images.figures import extract_paper_figures
from lib.paper.images.titles import lookup_paper_title
from lib.paper.review import make_review_lang
from lib.paper_identity import (
    PAPER_DIR,
    PAPER_IMG_DIR,
    _paper_hash,
    _safe_hash_dir,
)
from lib.request_parser import async_parse_body

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    api_v1_paper_bp,
    paper_bp,
)


# ══════════════════════════════════════════════════════
#  API Endpoints
# ══════════════════════════════════════════════════════


@api_v1_paper_bp.route("/api/v1/paper/openreview/autofill", methods=["POST"])
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
    from lib.browser.queue import get_connected_clients, is_extension_connected
    from lib.paper.openreview_autofill import (
        autofill_openreview_review,
        extract_review_values,
    )
    from routes.api_v1.auth import current_auth

    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    venue = (data.get("venue") or "generic").strip().lower()
    ui_lang = (data.get("ui_lang") or "en").strip().lower()
    requested_client_id = (data.get("client_id") or "").strip()
    owner_user_id = str(current_auth().owner_user_id)
    artifacts = PaperArtifactRepository(int(owner_user_id))
    if not phash:
        return api_bad_request("No paper_hash provided")

    owned_clients = get_connected_clients(owner_user_id=owner_user_id)
    owned_by_id = {
        str(row.get('client_id') or ''): row for row in owned_clients
    }
    if requested_client_id and requested_client_id not in owned_by_id:
        return api_error(
            'Browser client is not connected for this user.',
            status=409,
        )
    client_id = requested_client_id or (
        str(max(
            owned_clients,
            key=lambda row: row.get('last_poll', 0),
        ).get('client_id') or '')
        if owned_clients else ''
    )
    # Require an owned connected extension up front — a clear failure, not a hang.
    if not is_extension_connected(
            client_id, owner_user_id=owner_user_id):
        logger.info(
            "[OpenReview] Autofill requested but no extension connected (hash=%s)",
            phash,
        )
        return api_error(
            "Browser extension is not connected. Install/enable the Tofu "
            "Browser Bridge extension, open the OpenReview page, and retry.",
            status=409,
        )

    # Fetch the finished review for this paper+venue+lang.
    review_key = make_review_lang(venue, ui_lang)
    review_body = ""
    try:
        row = await asyncio.to_thread(artifacts.get_report, phash, review_key)
        if row and row.report:
            review_body = row.report
    except Exception as e:
        logger.warning("[OpenReview] Review lookup failed hash=%s: %s", phash, e)
    if not review_body.strip():
        return api_error(
            "No review found for this paper yet. Generate the review first, "
            "then auto-fill.",
            status=409,
        )

    # Try to carry the paper title into the review-title field.
    title = ""
    try:
        title = lookup_paper_title(phash, user_id=int(owner_user_id)) or ""
    except Exception as e:
        logger.debug("[OpenReview] title lookup failed (non-fatal): %s", e)
    values = extract_review_values(review_body, title=title)

    logger.info(
        "[OpenReview] Autofill start hash=%s venue=%s ui=%s oa=%s conf=%s client=%s",
        phash,
        venue,
        ui_lang,
        values.get("overall"),
        values.get("confidence"),
        (client_id or "active")[:12],
    )

    # send_browser_command is synchronous (blocks on the extension round-trip);
    # run the whole orchestration off the event loop so Hypercorn isn't blocked.
    import lib.browser.queue as browser_queue

    def _run():
        return autofill_openreview_review(
            browser_queue,
            values,
            client_id=client_id,
            owner_user_id=owner_user_id,
            timeout=20,
        )

    try:
        report = await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(
            "[OpenReview] Autofill orchestration failed hash=%s: %s",
            phash,
            e,
            exc_info=True,
        )
        return api_internal_error(
            "Auto-fill failed unexpectedly — the form was not submitted."
        )

    logger.info(
        "[OpenReview] Autofill done hash=%s ok=%s stage=%s filled=%d avoided_submit=%d",
        phash,
        report.get("ok"),
        report.get("stage"),
        len(report.get("filled", [])),
        report.get("submit_controls_detected", 0),
    )
    status = 200 if report.get("ok") else 409
    return api_payload(report, status)


@api_v1_paper_bp.route("/api/v1/paper/extract-images", methods=["POST"])
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
    filename = os.path.basename((data.get("filename") or "").strip())
    if not filename:
        logger.warning("[Paper:Images] Request with no filename")
        return api_bad_request("No filename")

    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.isfile(filepath):
        logger.warning("[Paper:Images] PDF not found: %s", filename)
        return api_not_found("PDF not found")

    try:
        max_images = int(data.get("max_images", 30))
        max_image_width = int(data.get("max_image_width", 900))
    except (ValueError, TypeError) as e:
        logger.warning("[Paper:Images] Invalid numeric parameter: %s", e)
        return api_bad_request(f"Invalid parameter: {e}")

    # Cache key — prefer client-provided hash (matches the report cache key),
    # fall back to filename-based hash.
    phash = _safe_hash_dir(data.get("paper_hash", "").strip()) or _paper_hash(filename)
    # Figure extraction is CPU/IO-heavy (pymupdf) — offload off the loop.
    images_out = await asyncio.to_thread(
        extract_paper_figures,
        filepath,
        phash,
        max_images=max_images,
        max_image_width=max_image_width,
    )
    return api_ok({"paper_hash": phash, "images": images_out})


@paper_bp.route("/api/paper/images/<phash>/<filename>")
def serve_paper_image(phash, filename):
    """Serve an extracted paper figure image.

    This stays synchronous so filesystem checks and the explicit
    ``lib.quart_sync`` file-response boundary run in Quart's executor rather
    than on the event loop.
    """
    phash_safe = _safe_hash_dir(phash)
    if not phash_safe:
        logger.debug("[Paper:Images] Invalid hash: %.40s", phash)
        return api_bad_request("Invalid hash")
    filename = os.path.basename(filename)
    # Only allow our known filename pattern
    if not re.fullmatch(r"fig_\d+_p\d+\.(jpg|jpeg|png)", filename, re.IGNORECASE):
        logger.debug("[Paper:Images] Invalid filename: %s", filename)
        return api_bad_request("Invalid filename")
    filepath = os.path.join(PAPER_IMG_DIR, phash_safe, filename)
    if not os.path.isfile(filepath):
        return api_not_found("Image not found")
    mt = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
    from lib.file_serving import send_file_conditional

    return send_file_conditional(filepath, mimetype=mt)
