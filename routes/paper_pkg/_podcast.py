"""Paper routes — podcast and video-abstract task endpoints plus task-factory registrations."""

import asyncio
import os
from urllib.parse import unquote

from quart import request


from lib.api_response import (
    api_bad_request,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.log import get_logger
from lib.paper.deepen_runtime import _deepen_runtime
from lib.paper.qa_runtime import _qa_runtime
from lib.paper.report_runtime import _report_runtime
from lib.paper.translate_runtime import _translate_runtime
from lib.paper_identity import (
    _paper_hash,
    _safe_hash_dir,
)
from lib.request_parser import async_parse_body
from routes._task_routes import register_task_routes
from routes.task_http import task_replay_cursor, task_replay_response

logger = get_logger(__name__)

from routes.paper_pkg._common import (
    api_v1_paper_bp,
)
from routes.api_v1.auth import request_user_id
from routes.paper_pkg._pdf import (
    _stream_file_response,
)


# ═══ Podcast (paper podcast: report → spoken script → TTS audio) ═══
#
# The paper-podcast surface (docs/modules/ingest_media.md, epic
# ). Report-first UX: the start route GATES on a report
# existing in either language (report_required → the frontend chains the
# report flow first, then retries). Without any configured TTS slot the
# worker degrades to script_only (script + transcript, honest reason) —
# owner directive 2026-07-25: no hard failure, no hardcoded model/voice.

from lib.paper.podcast_prompts import PODCAST_MODES


from lib.paper.podcast_runtime import (
    _podcast_index_get,
    _podcast_index_register,
    _podcast_runtime,
    _cleanup_stale_podcast_tasks,
    _new_podcast_task,
    _podcast_task_id,
)


def has_report(paper_hash, *, user_id: int):
    """Cross the podcast worker boundary only for an actual report lookup."""
    from lib.paper.podcast_engine.worker import has_report as implementation
    return implementation(paper_hash, user_id=user_id)


def load_cached_podcast(paper_hash, mode, lang, voice, *, user_id: int):
    """Load the owner-scoped podcast cache without expanding server boot."""
    from lib.paper.podcast_engine.worker import (
        load_cached_podcast as implementation,
    )
    return implementation(paper_hash, mode, lang, voice, user_id=user_id)


def podcast_audio_url(paper_hash, mode, lang, voice):
    """Resolve a published audio URL through the on-demand worker module."""
    from lib.paper.podcast_engine.worker import podcast_audio_url as implementation
    return implementation(paper_hash, mode, lang, voice)


def run_podcast_task(task):
    """Activate the report-to-script worker in the background task thread."""
    from lib.paper.podcast_engine.worker import run_podcast_task as implementation
    return implementation(task)


@api_v1_paper_bp.route("/api/v1/paper/podcast/status", methods=["GET"])
async def podcast_status():
    """Feature status: is a TTS slot configured, which models, mode bands."""
    from lib import tts as _tts

    available = _tts.tts_available()
    return api_ok(
        {
            "tts_available": available,
            "models": _tts.list_tts_models() if available else [],
            "default_voice": _tts.default_voice() if available else "",
            "modes": {
                m: {"target": band[0], "min": band[1], "max": band[2]}
                for m, band in PODCAST_MODES.items()
            },
        }
    )


def _resolve_podcast_request(data):
    """Shared request parsing for start/lookup; returns (phash, mode, lang,
    voice, model, force, error_response)."""
    phash = (data.get("paper_hash") or "").strip()
    paper_text = (data.get("paper_text") or "").strip()
    if phash and not _safe_hash_dir(phash):
        phash = ""
    if not phash and paper_text:
        phash = _paper_hash(paper_text)
    if not phash:
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            (api_bad_request("paper_hash or paper_text required")),
        )
    mode = (data.get("mode") or "short").strip() or "short"
    lang = (data.get("lang") or "zh").strip() or "zh"
    if mode not in PODCAST_MODES:
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            (api_bad_request(f"unknown mode: {mode}")),
        )
    if lang not in ("zh", "en"):
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            (api_bad_request(f"unsupported lang: {lang}")),
        )
    voice = (data.get("voice") or "").strip()
    model = (data.get("model") or "").strip() or None
    force = bool(data.get("force"))
    return phash, mode, lang, voice, model, force, None


@api_v1_paper_bp.route("/api/v1/paper/podcast/start", methods=["POST"])
async def start_podcast_task():
    """Start (or join) a podcast task; report-gated; cache-aware.

    Request: {paper_hash?, paper_text?, mode?, lang?, voice?, force?, model?}
    Responses:
      - {ok, task_id, reused?}           — live task (new or joined)
      - {ok, cached: true, ...}          — finished/script_only cache hit
      - {ok: false, report_required}     — no report yet; chain report first
    """
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    _cleanup_stale_podcast_tasks()
    phash, mode, lang, voice, model, force, err = _resolve_podcast_request(data)
    if err:
        return err
    if not has_report(phash, user_id=owner_user_id):
        return api_payload(
            {
                "ok": False,
                "report_required": True,
                "report_lang": lang,
                "error": "a report is required before a podcast can be generated",
            },
            200,
        )
    from lib import tts as _tts

    eff_voice = voice or _tts.default_voice()

    tid = _podcast_index_get(
        phash, mode, lang, eff_voice, model, user_id=owner_user_id)
    if tid:
        return api_ok({"task_id": tid, "reused": True})

    cached = load_cached_podcast(
        phash, mode, lang, eff_voice, user_id=owner_user_id)
    # The cache row is ONE slot per (paper_hash, mode, lang, voice) — and
    # the model that MADE it is part of its honest identity: asking for a
    # different model and being served the previous model's audio would be
    # the cache-key-skew family (label says X, key is generic). A requested
    # model that differs from the row's model is a cache MISS (regenerate +
    # overwrite the slot); a legacy caller sending no model accepts the
    # row as-is, preserving pre-picker behaviour for API clients.
    if cached and not force and not (model and (cached.get("model") or "") != model):
        status = cached.get("status") or ""
        return api_ok(
            {
                "cached": True,
                "status": status,
                "script": cached.get("script_json") or {},
                "meta": cached.get("meta") or {},
                "scriptOnly": status == "script_only",
                "model": cached.get("model") or "",
                "audioUrl": (
                    podcast_audio_url(phash, mode, lang, eff_voice)
                    if status == "done"
                    else ""
                ),
                "durationSec": cached.get("duration_sec") or 0,
            }
        )

    task_id = _podcast_task_id()
    _podcast_index_register(
        phash, mode, lang, eff_voice, model, task_id,
        user_id=owner_user_id)
    task = _new_podcast_task(
        task_id, phash, mode, lang, eff_voice, model,
        user_id=owner_user_id)
    _podcast_runtime.spawn(task_id, run_podcast_task, task)
    return api_ok({"task_id": task_id})


@api_v1_paper_bp.route("/api/v1/paper/video/start", methods=["POST"])
async def start_video_abstract_task():
    """Start a paper video abstract (report → narrated MG video).

    Request: {paper_hash, lang?, voice?, speed?, alignment?, narration?,
              burn_in?, quality?, parallel?, max_scenes?, model?}
    Responses:
      - {ok, task_id, scenes, source_kind}  — motion task started; poll via
        GET /api/v1/motion/videos/poll/<task_id>, download via
        /api/v1/motion/videos/<task_id>/file
      - {ok: false, report_required}        — chain a report first
    """
    from lib.paper.video_abstract import start_video_abstract

    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    phash = (data.get("paper_hash") or "").strip()
    if not phash:
        return api_bad_request("paper_hash is required", field="paper_hash")
    lang = (data.get("lang") or "zh").strip()
    if lang not in ("zh", "en"):
        return api_bad_request("lang must be zh|en", field="lang")
    quality = (data.get("quality") or "standard").strip()
    if quality not in ("draft", "standard", "high"):
        return api_bad_request("quality must be draft|standard|high", field="quality")
    alignment = (data.get("alignment") or "loose").strip()
    if alignment not in ("loose", "strict"):
        return api_bad_request("alignment must be loose|strict", field="alignment")
    try:
        parallel = max(1, min(int(data.get("parallel") or 2), 4))
        max_scenes = max(1, min(int(data.get("max_scenes") or 8), 16))
    except (TypeError, ValueError):
        return api_bad_request("parallel/max_scenes must be ints", field="parallel")
    # Composition tier. None = follow the fleet default (authored), which is
    # resolved in ONE place (lib/motion_video/_scene_author.scene_author_enabled)
    # rather than re-stated here. This is deliberately NOT the draft/standard/
    # high knob above: that one is the RENDER preset (bitrate/scale) and says
    # nothing about whether a scene gets a bespoke composition.
    scene_author = data.get("scene_author")
    if scene_author is not None:
        scene_author = bool(scene_author)
    # Off-loop: start_video_abstract is a fully sync pipeline (PG report
    # probe → FUSE source-file reads → a blocking LLM beat-writing call,
    # tens of seconds). Run inline it froze the event loop for the whole
    # request (2026-08-01: 39.6s stall → LoopWatch trip at
    # lib/http_client.py — every SSE/WS connection dropped).
    res = await asyncio.to_thread(
        start_video_abstract,
        phash,
        lang=lang,
        voice=(data.get("voice") or "").strip(),
        speed=data.get("speed"),
        alignment=alignment,
        narration=bool(data.get("narration", True)),
        burn_in=bool(data.get("burn_in", False)),
        quality=quality,
        parallel=parallel,
        max_scenes=max_scenes,
        scene_author=scene_author,
        model=(data.get("model") or "").strip() or None,
        user_id=owner_user_id,
        force=bool(data.get("force", False)),
    )
    if not res.get("ok"):
        return api_payload(
            {
                "ok": False,
                "report_required": res.get("reason") == "report_required",
                "error": res.get("reason"),
            },
            200,
        )
    return api_payload(res, 200)


@api_v1_paper_bp.route("/api/v1/paper/video/lookup", methods=["GET"])
async def lookup_video_abstract():
    """Re-attach a paper's video-abstract task on tab open.

    Scans the motion runtime for the newest task tagged with this
    paper_hash (tasks live for the runtime TTL, 1h). Returns
    {ok, found, running, task_id, result, report_available}.
    """
    from lib.motion_video.runtime import _motion_runtime
    from lib.paper.podcast_engine.worker import has_report

    phash = (request.args.get("paper_hash") or "").strip()
    if not phash:
        return api_bad_request("paper_hash is required", field="paper_hash")
    owner_user_id = int(request_user_id())
    best = None
    for task in _motion_runtime.snapshot_owned(user_id=owner_user_id):
        if task.get("paper_hash") == phash:
            if best is None or task.get("created_at", 0) > best.get("created_at", 0):
                best = task
    resp = {
        "ok": True,
        "report_available": await asyncio.to_thread(
            has_report, phash, user_id=owner_user_id),
    }
    if best:
        from lib.agent_core.task_runtime import _epoch_ms

        resp.update(
            {
                "found": True,
                "task_id": best["task_id"],
                "running": best["status"] in ("pending", "running"),
                "status": best["status"],
                "model": best.get("model") or "",
                # Start clock on the re-attach frame (epoch ms) — this lands
                #   before the first poll, so without it a refreshed tab paints
                #   0:00 for a frame. `created_at` was already read above to pick
                #   the newest task; it just was never surfaced.
                "createdAt": _epoch_ms(best.get("created_at")),
                "updatedAt": _epoch_ms(
                    best.get("updated_at") or best.get("created_at")
                ),
            }
        )
        if best["status"] == "done" and best.get("result"):
            resp["result"] = best["result"]
        # Product-quality axis (lib/agent_core/task_runtime.py). A degraded
        # film keeps status='done' BY DESIGN, so this field is the only thing
        # separating "all 8 scenes fell back to the plain template card" from
        # a clean success. Dropping it here is what let the panel render both
        # identically.
        if best.get("artifact_quality"):
            resp["artifact_quality"] = best["artifact_quality"]
        return api_payload(resp, 200)

    # P-UX4: memory missed — fall back to the on-disk job manifests so a
    # finished video survives a server restart (and an interrupted one is
    # honestly reported instead of vanishing).
    disk = _lookup_paper_video_on_disk(phash, user_id=owner_user_id)
    if disk:
        resp.update(disk)
    else:
        resp["found"] = False
    return api_payload(resp, 200)


def _lookup_paper_video_on_disk(phash: str, *, user_id: int) -> dict | None:
    """Newest owner-visible on-disk motion job for this paper.

    Scans ``<motion_root>/jobs/*/job.json`` for manifests tagged with this
    paper_hash. A ``done`` manifest with its final.mp4 still on disk is a
    playable result; a ``running`` manifest whose task is NOT live in the
    runtime means the resume scanner already declined it (or it died
    again) — report interrupted. Returns a lookup-response fragment or
    None when nothing on disk matches.
    """
    from lib.agent_core.task_runtime import _epoch_ms
    from lib.motion_video._env import motion_root
    from lib.motion_video.runtime import _motion_runtime
    from lib.production.jobs import read_manifest

    jobs_dir = os.path.join(motion_root(), "jobs")
    try:
        names = sorted(os.listdir(jobs_dir))
    except OSError as e:
        logger.debug("[Paper:Video] disk lookup cannot list %s: %s", jobs_dir, e)
        return None
    best = None  # (mtime, task_id, manifest)
    for name in names:
        workdir = os.path.join(jobs_dir, name)
        m = read_manifest(workdir)
        if (not m or m.get("paper_hash") != phash
                or int(m.get("user_id") or 0) != user_id):
            continue
        try:
            mt = os.path.getmtime(os.path.join(workdir, "job.json"))
        except OSError as _e:
            logger.debug("lookup paper video on disk: unreadable (%s)", _e)
            mt = 0.0
        if best is None or mt > best[0]:
            best = (mt, m.get("task_id") or name, m)
    if not best:
        return None
    _mt, task_id, m = best
    state = m.get("state")

    # Server-authoritative clocks on the DISK path too.
    #
    # This branch IS the post-restart re-attach: the task is gone from memory,
    # so unlike the in-memory branch there is no first poll to correct a
    # locally-minted stopwatch afterwards — runtime.poll() 404s on a task it
    # does not hold. Whatever this response says is what the panel shows for
    # the rest of the run, so omitting the clocks here made a resumed job
    # restart its elapsed at 0:00 permanently.
    #
    # `created_at` is persisted in the manifest precisely because
    # resume_running_jobs() mints a fresh task (see motion_video.engine's
    # _MANIFEST_FIELDS note); job.json's mtime is the last time the worker
    # actually wrote its state, which is the honest liveness signal available
    # from disk. Both are OMITTED rather than guessed when unavailable: a
    # missing field lets the client fall back to its local clock, whereas a
    # fabricated one renders as 1970 or year 58000 — silently wrong beats
    # nothing here only if it is TRUE.
    def _disk_clocks() -> dict:
        out = {}
        created = _epoch_ms(m.get("created_at"))
        if created is not None:
            out["createdAt"] = created
        seen = _epoch_ms(_mt) if _mt else None
        # Liveness may never claim to predate the start.
        if seen is not None and (created is None or seen >= created):
            out["updatedAt"] = seen
        elif created is not None:
            out["updatedAt"] = created
        return out

    if (state == "running"
            and _motion_runtime.get_owned(task_id, user_id=user_id) is None):
        return {
            "found": True,
            "interrupted": True,
            "task_id": task_id,
            "model": m.get("model") or "",
            **_disk_clocks(),
        }
    if state == "done":
        workdir = os.path.join(jobs_dir, task_id)
        final = os.path.join(workdir, "final.mp4")
        if os.path.isfile(final):
            duration = 0.0
            try:
                from lib import motion_video as mv

                probe = mv.probe_video(final)
                duration = round(float((probe or {}).get("duration") or 0), 3)
            except Exception as e:
                logger.debug("[Paper:Video] disk lookup probe failed: %s", e)
            return {
                "found": True,
                "running": False,
                "status": "done",
                "task_id": task_id,
                "model": m.get("model") or "",
                "result": {
                    "final_path": final,
                    "duration": duration,
                    "workdir": workdir,
                    "narrated": bool(m.get("narration")),
                },
                # Read from the manifest, which the engine writes AFTER
                # computing the verdict. This branch has no later poll to
                # correct it (runtime.poll 404s on a task it no longer
                # holds), so whatever is omitted here is lost for good.
                **(
                    {"artifact_quality": m["artifact_quality"]}
                    if m.get("artifact_quality")
                    else {}
                ),
                **_disk_clocks(),
            }
    return None


@api_v1_paper_bp.route("/api/v1/paper/podcast/poll", methods=["GET"])
async def poll_podcast_task():
    """Poll podcast events. Same cursor protocol as the report poll; on done
    the response flattens script / audioUrl / durationSec / scriptOnly."""
    task_id = request.args.get("task_id", "")
    cursor = task_replay_cursor(request.args)
    owner_user_id = int(request_user_id())
    t = _podcast_runtime.get_owned(task_id, user_id=owner_user_id)
    if not t:
        return api_not_found("Task not found")
    # Go through the shared throat rather than re-deriving the reply here.
    #   runtime.poll() owns the stall reap AND the server-authoritative clocks
    #   (createdAt / updatedAt, epoch ms) that let a refreshed client continue
    #   its elapsed timer instead of restarting at 0:00. A hand-rolled reply
    #   silently misses every field the throat gains later — which is exactly
    #   how this endpoint came to be the one production surface with no clocks.
    base = _podcast_runtime.poll(task_id, cursor=cursor)
    status = t.get("status")
    resp = {
        "format": base.get("format"),
        "ok": True,
        "status": base.get("status", status),
        "done": base.get("done", status in ("done", "error", "aborted")),
        "events": base.get("events", []),
        # This endpoint's wire name for the cursor is `cursor`, not
        # `next_cursor` — keep it (the podcast client reads `cursor`).
        "cursor": base.get("next_cursor", 0),
        # Add the standard producer-owned replay metadata without changing the
        # legacy numeric ``cursor`` above. New clients can detect a trimmed-log
        # reset; existing podcast clients keep advancing the integer field.
        "next_cursor": base.get("next_cursor", 0),
        "cursorInfo": base.get("cursor"),
        "taskId": base.get("taskId", task_id),
        "progress": t.get("progress") or {"done": 0, "total": 0},
        "createdAt": base.get("createdAt"),
        "updatedAt": base.get("updatedAt"),
    }
    if base.get("requestId"):
        resp["requestId"] = base["requestId"]
    if base.get("finishedAt") is not None:
        resp["finishedAt"] = base["finishedAt"]
    # The reap may have just flipped the task terminal.
    status = resp["status"]
    events = t["events"]
    if status == "done":
        resp["script"] = t.get("script")
        resp["meta"] = t.get("script_meta") or {}
        resp["scriptOnly"] = bool(t.get("script_only"))
        resp["audioUrl"] = t.get("audio_url") or ""
        resp["durationSec"] = t.get("duration_sec") or 0
        resp["model"] = t.get("model") or ""
    elif status == "error":
        for ev in reversed(events):
            if ev.get("type") == "error":
                resp["error"] = ev.get("error", "unknown error")
                if ev.get("reason"):
                    resp["reason"] = ev["reason"]
                break
    return task_replay_response(resp)


@api_v1_paper_bp.route("/api/v1/paper/podcast/lookup", methods=["POST"])
async def lookup_podcast():
    """Find a live task or cached podcast for (paper_hash, mode, lang, voice)."""
    owner_user_id = int(request_user_id())
    data = await async_parse_body()
    phash, mode, lang, voice, _model, _force, err = _resolve_podcast_request(data)
    if err:
        return err
    from lib import tts as _tts

    eff_voice = voice or _tts.default_voice()
    tid = _podcast_index_get(
        phash, mode, lang, eff_voice, _model, user_id=owner_user_id)
    if not tid:
        # Re-attach fallback: a LOOKUP caller cannot name the model/voice the
        # RUNNING task was started with — discovering them is the lookup's
        # job (the panel adopts the returned model). START dedup stays
        # exact-key (a model-B start must never join model-A's task), but on
        # an exact-key miss the lookup scans live tasks for (paper_hash,
        # mode, lang), newest wins — the video-abstract lookup's semantics.
        # Without it a run started with any concrete model was invisible to
        # the re-attach and the tab regressed to the idle card mid-run.
        best = None
        for task in _podcast_runtime.snapshot_owned(user_id=owner_user_id):
            if (
                task.get("status") in ("pending", "running")
                and task.get("paper_hash") == phash
                and task.get("mode") == mode
                and task.get("lang") == lang
                and (
                    best is None
                    or task.get("created_at", 0) > best.get("created_at", 0)
                )
            ):
                best = task
        if best is not None:
            tid = best["task_id"]
    if tid:
        # The re-attach frame lands BEFORE the first poll, so it must carry
        #   the start clock too — otherwise a refreshed panel paints 0:00 for
        #   one frame before the first poll corrects it (a visible flash).
        from lib.agent_core.task_runtime import _epoch_ms

        _lt = _podcast_runtime.get(tid) or {}
        return api_ok(
            {
                "found": True,
                "running": True,
                "task_id": tid,
                "model": _lt.get("model") or "",
                "createdAt": _epoch_ms(_lt.get("created_at")),
                "updatedAt": _epoch_ms(_lt.get("updated_at") or _lt.get("created_at")),
            }
        )
    cached = load_cached_podcast(
        phash, mode, lang, eff_voice, user_id=owner_user_id)
    if cached:
        status = cached.get("status") or ""
        return api_ok(
            {
                "found": True,
                "cached": True,
                "status": status,
                "script": cached.get("script_json") or {},
                "meta": cached.get("meta") or {},
                "scriptOnly": status == "script_only",
                "model": cached.get("model") or "",
                "audioUrl": (
                    podcast_audio_url(phash, mode, lang, eff_voice)
                    if status == "done"
                    else ""
                ),
                "durationSec": cached.get("duration_sec") or 0,
            }
        )
    # P-UX4: a generating row flipped to interrupted at boot = the last run
    # was cut by a server restart. Surface it honestly (regenerate button).
    from lib.paper.podcast_engine.worker import load_interrupted_podcast

    if load_interrupted_podcast(
        phash, mode, lang, eff_voice, user_id=owner_user_id):
        return api_ok(
            {"found": True, "interrupted": True,
             "report_available": has_report(phash, user_id=owner_user_id)}
        )
    return api_ok(
        {
            "found": False,
            "tts_available": _tts.tts_available(),
            "report_available": has_report(phash, user_id=owner_user_id),
        }
    )


@api_v1_paper_bp.route("/api/v1/paper/podcast/script", methods=["GET"])
async def get_podcast_script():
    """Return the cached spoken script + meta (transcript tab, md export)."""
    owner_user_id = int(request_user_id())
    phash = (request.args.get("paper_hash") or "").strip()
    mode = (request.args.get("mode") or "short").strip() or "short"
    lang = (request.args.get("lang") or "zh").strip() or "zh"
    voice = (request.args.get("voice") or "").strip()
    if not phash or not _safe_hash_dir(phash):
        return api_bad_request("paper_hash required")
    from lib import tts as _tts

    eff_voice = voice or _tts.default_voice()
    # Off-loop: load_cached_podcast is a sync PG read (get_thread_db).
    cached = await asyncio.to_thread(
        load_cached_podcast, phash, mode, lang, eff_voice,
        user_id=owner_user_id)
    if not cached:
        return api_not_found("Podcast not found")
    return api_ok(
        {
            "script": cached.get("script_json") or {},
            "meta": cached.get("meta") or {},
            "scriptOnly": (cached.get("status") or "") == "script_only",
            "audioUrl": (
                podcast_audio_url(phash, mode, lang, eff_voice)
                if (cached.get("status") or "") == "done"
                else ""
            ),
            "durationSec": cached.get("duration_sec") or 0,
        }
    )


@api_v1_paper_bp.route(
    "/api/v1/paper/podcast/audio/<paper_hash>/<mode>/<lang>/<voice>", methods=["GET"]
)
async def serve_podcast_audio(paper_hash, mode, lang, voice):
    """Stream the podcast audio with HTTP Range support (seekable player).

    Path containment mirrors _safe_paper_file: the persisted file_path must
    resolve under PAPER_DIR/podcast/<paper_hash>/ — a row pointing anywhere
    else is treated as tampered and 404s (logged).
    """
    import os as _os

    from lib.paper_identity import PAPER_DIR as _PAPER_DIR

    owner_user_id = int(request_user_id())
    if not _safe_hash_dir(paper_hash):
        return api_bad_request("invalid paper_hash")
    voice = unquote(voice or "")
    if voice == "-":
        voice = ""
    # Off-loop: sync PG read; the streaming body itself is a sync generator,
    # which Quart iterates via run_sync_iterable (executor) either way.
    cached = await asyncio.to_thread(
        load_cached_podcast, paper_hash, mode, lang, voice,
        user_id=owner_user_id)
    fpath = (cached or {}).get("file_path") or ""
    if not cached or not fpath:
        return api_not_found("Podcast audio not found")
    root = _os.path.abspath(_os.path.join(
        _PAPER_DIR, "podcast", str(owner_user_id), paper_hash))
    real = _os.path.abspath(fpath)
    if not real.startswith(root + _os.sep):
        logger.warning("[Paper:Podcast] audio path escapes podcast dir: %s", fpath)
        return api_not_found("Podcast audio not found")
    if not _os.path.exists(real):
        logger.warning(
            "[Paper:Podcast] audio file missing on disk (stale row): %s", real
        )
        return api_not_found("Podcast audio file missing")
    ext = real.rsplit(".", 1)[-1].lower() if "." in real else ""
    mime = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "bin": "application/octet-stream",
    }.get(ext, "application/octet-stream")
    return _stream_file_response(real, mime)


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
register_task_routes(
    api_v1_paper_bp,
    _report_runtime,
    url_prefix="/api/v1/paper/report",
    enable_poll=False,
)


register_task_routes(
    api_v1_paper_bp, _qa_runtime, url_prefix="/api/v1/paper/qa", enable_poll=False
)


register_task_routes(
    api_v1_paper_bp,
    _translate_runtime,
    url_prefix="/api/v1/paper/translate",
    enable_poll=False,
)


register_task_routes(
    api_v1_paper_bp,
    _podcast_runtime,
    url_prefix="/api/v1/paper/podcast",
    enable_poll=False,
)


# ── Deepen (on-demand section depth, reading-xp P3) ──
# POLL + ABORT both ride the generic factory — a first for the paper family:
# the deepen drawer replays the event log and takes the content from the
# `done` event, so no engine-specific poll keys are needed (the report/QA
# polls stay custom for their legacy keys).
register_task_routes(
    api_v1_paper_bp,
    _deepen_runtime,
    url_prefix="/api/v1/paper/deepen",
    enable_poll=True,
)
