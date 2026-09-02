"""Run and persist the report-to-script-to-audio podcast workflow.

Responsibility: resolve owner-scoped source material, persist interruption-safe
rows, execute script/TTS stages, and publish task events.  ``_script`` owns
script generation, ``_audio`` owns synthesis, and ``_validate`` owns quality
gates.
"""

from __future__ import annotations

import os
import time
import uuid

from lib.agent_core.events import Phase, build_phase
from lib.json_store import write_bytes_atomic
from lib.log import audit_log, get_logger

from lib.paper.podcast_engine._errors import AudioSynthesisAborted

logger = get_logger(__name__)


def generate_script(**kwargs):
    """Load the LLM/script stack only when a generation task reaches it.

    This module-level seam intentionally remains patchable by focused worker
    tests and deployments that provide a custom deterministic script stage.
    """
    from lib.paper.podcast_engine._script import generate_script as implementation
    return implementation(**kwargs)


def synthesize_script_audio(*args, **kwargs):
    """Load TTS/audio assembly only after a generated script needs audio."""
    from lib.paper.podcast_engine._audio import (
        synthesize_script_audio as implementation,
    )
    return implementation(*args, **kwargs)


class PodcastSourceError(Exception):
    """No usable source material for this paper (report gate must stop it)."""


def load_source_text(
    paper_hash: str,
    lang: str,
    *,
    user_id: int,
) -> tuple[str, str]:
    """Resolve the script's grounding material; return (text, kind).

    Order: report in the script's language → report in the other language →
    translation → parsed paper text. The start route gates on report
    presence (report-first UX); the deeper fallbacks exist for headless
    callers and for papers whose report lives in the other language.
    """
    from lib.paper.artifact_repository import PaperArtifactRepository
    artifacts = PaperArtifactRepository(user_id)
    langs = [lang] + (['en'] if lang == 'zh' else ['zh'])
    for lg in langs:
        row = artifacts.get_report(paper_hash, lg)
        if row and row.report.strip():
            return row.report, f'report_{lg}'
    for lg in ('zh', 'en'):
        row = artifacts.get_translation(paper_hash, lg)
        if row and row.text.strip():
            return row.text, f'translation_{lg}'
    # Parsed full text is private bookshelf data, so it crosses the typed,
    # owner-scoped repository rather than the global report cache boundary.
    from lib.paper.library_repository import PaperLibraryRepository
    identity = PaperLibraryRepository(user_id).identity(paper_hash)
    if identity and identity.parsed_text.strip():
        return identity.parsed_text, 'parsed_text'
    return '', 'none'


def has_source_material(paper_hash: str, *, user_id: int) -> bool:
    """True when ANY report/translation/parsed text exists (route gate)."""
    text, kind = load_source_text(paper_hash, 'zh', user_id=user_id)
    return kind != 'none' and bool(text.strip())


def has_report(paper_hash: str, *, user_id: int) -> bool:
    """True when a report exists in EITHER language (report-first gate)."""
    from lib.paper.artifact_repository import PaperArtifactRepository
    artifacts = PaperArtifactRepository(user_id)
    for lg in ('zh', 'en'):
        row = artifacts.get_report(paper_hash, lg)
        if row:
            return True
    return False


def persist_podcast_row(paper_hash: str, mode: str, lang: str, voice: str,
                         *, status: str, script: dict, meta: dict,
                         file_path: str = '', duration_sec: float = 0.0,
                         model: str = '', tts_model: str = '',
                         user_id: int) -> None:
    """Upsert the paper_podcasts cache row (script + audio metadata)."""
    now = int(time.time())
    from lib.paper.artifact_repository import (
        PaperArtifactRepository,
        PaperPodcast,
    )
    PaperArtifactRepository(user_id).put_podcast(
        PaperPodcast(
            paper_hash=paper_hash,
            mode=mode,
            lang=lang,
            voice=voice,
            status=status,
            script=script or {},
            file_path=file_path,
            duration_sec=float(duration_sec or 0),
            model=model or '',
            tts_model=tts_model or '',
            meta=meta or {},
            created_at=now,
            updated_at=now,
        ),
        command_id=f'paper.podcast.upsert:{uuid.uuid4().hex}',
    )


def load_interrupted_podcast(paper_hash: str, mode: str, lang: str,
                             voice: str, *, user_id: int) -> bool:
    """True when the cache row says a previous run was cut by a restart.

    P-UX4 (docs/modules/ingest_media.md §3.3): the worker persists a
    ``generating`` row at start; startup flips every lingering
    ``generating`` row to ``interrupted`` (a live process would have
    overwritten it). The lookup route surfaces this so the tab can say
    "被服务器重启打断" + offer a one-click regenerate, instead of
    pretending nothing ever happened.
    """
    from lib.paper.artifact_repository import PaperArtifactRepository
    row = PaperArtifactRepository(user_id).get_podcast(
        paper_hash, mode, lang, voice)
    return bool(row and row.status == 'interrupted')


def mark_interrupted_podcasts() -> int:
    """Startup sweep: every ``generating`` row belongs to a dead process.

    Called once at server boot (next to motion's resume_interrupted_jobs).
    Returns the number of rows flipped. Best-effort, never raises.
    """
    try:
        from lib.paper.artifact_repository import (
            mark_all_generating_podcasts_interrupted,
        )
        n = mark_all_generating_podcasts_interrupted(
            updated_at=int(time.time()),
            command_id=f'paper.podcast.interrupt:{uuid.uuid4().hex}',
        )
        if n:
            logger.info('[Paper:Podcast] marked %d generating row(s) '
                        'interrupted on startup', n)
        return n or 0
    except Exception as e:
        logger.warning('[Paper:Podcast] interrupted sweep failed: %s', e)
        return 0


def load_cached_podcast(paper_hash: str, mode: str, lang: str,
                        voice: str, *, user_id: int) -> dict | None:
    """Fetch the cached row for the dedup key; parsed script/meta included."""
    from lib.paper.artifact_repository import PaperArtifactRepository
    row = PaperArtifactRepository(user_id).get_podcast(
        paper_hash, mode, lang, voice)
    if not row or row.status not in ('done', 'script_only'):
        return None
    return row.to_projection()


def _voice_slug(voice: str) -> str:
    slug = ''.join(c if (c.isalnum() or c in '-_') else '_' for c in (voice or ''))
    return slug[:40] or 'v'


def podcast_audio_url(paper_hash: str, mode: str, lang: str, voice: str) -> str:
    from urllib.parse import quote
    return (f'/api/v1/paper/podcast/audio/{paper_hash}/{mode}/{lang}/'
            f'{quote(voice or "-", safe="")}')


def run_podcast_task(task):
    """Background worker: resolve source → script → TTS → file → DB → events.

    Event vocabulary (mirrors the report worker + two podcast-specific):
    status / phase / script / segment_done / audio_ready / done / error /
    aborted. The poll route flattens the task fields into the response.
    """
    from lib import tts as _tts
    from lib.paper.images.figures import load_image_manifest
    from lib.paper.images.titles import lookup_paper_title
    from lib.paper.podcast_runtime import _append_podcast_event, _podcast_runtime
    from lib.production.heartbeat import heartbeat

    task_id = task['task_id']
    paper_hash, mode, lang = task['paper_hash'], task['mode'], task['lang']
    voice, model = task['voice'], task.get('model')
    owner_user_id = int(task['_userId'])
    _podcast_runtime.mark_running(task_id)

    # P-UX2: the phase vocabulary the frontend stepper renders.
    _PHASES = ['source', 'script', 'audio']

    def _phase_started(phase: str) -> None:
        _append_podcast_event(task, {
            'type': 'phase_started', 'phase': phase,
            'phase_index': _PHASES.index(phase) + 1,
            'phase_total': len(_PHASES), 'phases': list(_PHASES),
            'started_at': time.time()})

    _append_podcast_event(task, {'type': 'status', 'status': 'running'})
    logger.info('[Paper:Podcast] task %s started phash=%s mode=%s lang=%s '
                'voice=%s model=%s', task_id, paper_hash[:8], mode, lang,
                voice or '(default)', model or '(auto)')
    audit_log('paper_podcast_start', task_id=task_id,
              paper_hash=paper_hash[:8], mode=mode, lang=lang)

    # P-UX4: anchor the run in the DB so a server restart can honestly say
    # "interrupted" instead of losing the run entirely.
    try:
        persist_podcast_row(paper_hash, mode, lang, voice,
                             status='generating', script={},
                             meta={'task_id': task_id},
                             model=model or '', user_id=owner_user_id)
    except Exception as e:
        logger.warning('[Paper:Podcast] generating-row persist failed '
                       '(continuing): %s', e)

    def _aborted() -> bool:
        return bool(task['abort_event'].is_set())

    try:
        # ── Stage 0: source material (report gate runs in the route) ──
        _phase_started('source')
        source_text, source_kind = load_source_text(
            paper_hash, lang, user_id=owner_user_id)
        if not source_text:
            raise PodcastSourceError(f'no source material for {paper_hash[:8]}')
        images = load_image_manifest(paper_hash)
        title = lookup_paper_title(paper_hash, user_id=owner_user_id)

        # ── Stage 1: script (1–3 min of LLM rounds — heartbeat + sub-steps) ──
        _phase_started('script')
        _append_podcast_event(task, build_phase(Phase.SCRIPT))
        with heartbeat(task, _append_podcast_event, 'script'):
            script, script_meta = generate_script(
                source_text=source_text, lang=lang, mode=mode, title=title,
                images=images, model=model, source_kind=source_kind,
                on_event=lambda ev: _append_podcast_event(task, ev))
        task['script'] = script
        task['script_meta'] = script_meta
        _append_podcast_event(task, {'type': 'script', 'script': script,
                                     'meta': script_meta})
        if _aborted():
            raise AudioSynthesisAborted()

        # ── Stage 2: TTS (degrade to script-only without a slot) ──
        if not _tts.tts_available():
            script_meta = {**script_meta, 'degrade_reason': 'no_tts_slot'}
            persist_podcast_row(paper_hash, mode, lang, voice,
                                 status='script_only', script=script,
                                 meta=script_meta, model=task.get('model') or '',
                                 user_id=owner_user_id)
            task['script_only'] = True
            _podcast_runtime.finish(
                task_id,
                result={'script': script, 'meta': script_meta,
                        'scriptOnly': True, 'audioUrl': '', 'durationSec': 0},
                terminal_event_fields={
                    'type': 'done', 'script': script, 'meta': script_meta,
                    'scriptOnly': True, 'reason': 'no_tts_slot',
                    'audioUrl': '', 'durationSec': 0,
                },
            )
            logger.info('[Paper:Podcast] task %s done SCRIPT-ONLY (no tts slot)',
                        task_id)
            audit_log('paper_podcast_done', task_id=task_id, script_only=True)
            return

        _phase_started('audio')
        _append_podcast_event(
            task, build_phase(Phase.AUDIO,
                              total=len(script.get('segments') or [])))
        audio = synthesize_script_audio(
            script, voice=voice or _tts.default_voice(),
            abort_check=_aborted,
            on_segment_done=lambda d, t: (
                task['progress'].update(done=d, total=t),
                _append_podcast_event(task, {'type': 'segment_done',
                                             'done': d, 'total': t})))

        # ── Stage 3: atomic file write + cache row ──
        from lib.paper_identity import PAPER_DIR
        out_dir = os.path.join(
            PAPER_DIR, 'podcast', str(owner_user_id), paper_hash)
        os.makedirs(out_dir, exist_ok=True)
        fname = f'{mode}_{lang}_{_voice_slug(voice)}.{audio["ext"]}'
        fpath = os.path.join(out_dir, fname)
        write_bytes_atomic(fpath, audio['audio_bytes'])

        script_meta = {**script_meta,
                       'duration_estimated': audio['duration_estimated'],
                       'container': audio['container']}
        persist_podcast_row(
            paper_hash, mode, lang, voice, status='done', script=script,
            meta=script_meta, file_path=fpath,
            duration_sec=audio['duration_sec'],
            model=task.get('model') or '', tts_model=audio['tts_model'],
            user_id=owner_user_id)

        audio_url = podcast_audio_url(paper_hash, mode, lang, voice)
        task['audio_url'] = audio_url
        task['duration_sec'] = audio['duration_sec']
        _append_podcast_event(task, {'type': 'audio_ready', 'url': audio_url,
                                     'durationSec': audio['duration_sec'],
                                     'ext': audio['ext']})
        _podcast_runtime.finish(
            task_id,
            result={
                'script': script, 'meta': script_meta,
                'scriptOnly': False, 'audioUrl': audio_url,
                'durationSec': audio['duration_sec'],
            },
            terminal_event_fields={
                'type': 'done', 'script': script, 'meta': script_meta,
                'scriptOnly': False, 'audioUrl': audio_url,
                'durationSec': audio['duration_sec'],
            },
        )
        logger.info('[Paper:Podcast] task %s done: %s (%.1fs, %d KB)',
                    task_id, fname, audio['duration_sec'],
                    len(audio['audio_bytes']) // 1024)
        audit_log('paper_podcast_done', task_id=task_id, script_only=False,
                  duration_sec=round(audio['duration_sec'], 1),
                  tts_model=audio['tts_model'])

    except AudioSynthesisAborted:
        _podcast_runtime.abort(task_id)
        _podcast_runtime.finish(
            task_id, terminal_event_fields={'type': 'aborted'})
        logger.info('[Paper:Podcast] task %s aborted', task_id)
        audit_log('paper_podcast_abort', task_id=task_id)
    except PodcastSourceError as e:
        _podcast_runtime.finish(
            task_id,
            error=e,
            error_context='paper-podcast',
            terminal_event_fields={'reason': 'report_required'},
        )
        logger.warning('[Paper:Podcast] task %s source error: %s', task_id, e)
    except Exception as e:
        error_detail = f'podcast generation failed: {e}'
        error_reason = ''
        try:
            from lib import tts as _t
            if isinstance(e, _t.TTSError):
                error_detail = e.detail
                error_reason = (
                    'tts_unavailable' if e.status == 503 else 'tts_failed')
        except Exception as inner:
            logger.debug('[Paper:Podcast] error-envelope classify failed: %s', inner)
        _podcast_runtime.finish(
            task_id,
            error=error_detail,
            error_context='paper-podcast',
            terminal_event_fields=(
                {'reason': error_reason} if error_reason else None),
        )
        logger.error('[Paper:Podcast] task %s failed: %s', task_id, e,
                     exc_info=True)
    finally:
        # P-UX4/§3.4F: the generating row must never linger (it would be
        # misread as "interrupted by a restart" on the next boot). A completed
        # script survives an abort as a script_only row — the partial product
        # is kept, per the abort-semantics contract.
        if task.get('status') in ('aborted', 'error'):
            try:
                if task['status'] == 'aborted' and task.get('script'):
                    persist_podcast_row(
                        paper_hash, mode, lang, voice, status='script_only',
                        script=task['script'],
                        meta={**(task.get('script_meta') or {}),
                              'degrade_reason': 'aborted_before_audio'},
                        model=model or '', user_id=owner_user_id)
                else:
                    persist_podcast_row(
                        paper_hash, mode, lang, voice,
                        status=task['status'], script=task.get('script') or {},
                        meta={'task_id': task_id}, model=model or '',
                        user_id=owner_user_id)
            except Exception as e:
                logger.warning('[Paper:Podcast] terminal-row persist failed: %s', e)


__all__ = [
    'PodcastSourceError',
    'has_report',
    'has_source_material',
    'load_cached_podcast',
    'load_interrupted_podcast',
    'mark_interrupted_podcasts',
    'podcast_audio_url',
    'load_source_text',
    'persist_podcast_row',
    'run_podcast_task',
]
