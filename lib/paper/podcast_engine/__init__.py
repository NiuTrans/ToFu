"""lib/paper/podcast_engine — report → spoken-script → audio pipeline.

The paper-podcast worker package (docs/PAPER_PODCAST_DESIGN.md). Layout:

  * ``_validate`` — the deterministic quality gates (LaTeX residue, Unicode
    math symbols, zh abbreviation watchlist, number provenance incl. derived
    channels, structure, duration). A script must pass these before TTS.
  * ``_script``   — prompt assembly, JSON parse/repair, validator-feedback
    revision, critic round, server-side duration estimates.
  * ``_audio``    — per-segment TTS synthesis + WAV concat + MP3 transcode.
  * this file     — the facade + the task worker ``_run_podcast_task``
    (source resolution → script → TTS → file → DB row → events).
"""

from __future__ import annotations

import json
import os
import time

from lib.agent_core.events import Phase, build_phase
from lib.log import audit_log, get_logger

from lib.paper.podcast_engine._audio import (  # noqa: F401
    AudioSynthesisAborted,
    synthesize_script_audio,
)
from lib.paper.podcast_engine._script import (  # noqa: F401
    ScriptParseError,
    build_critic_prompt,
    critic_enabled,
    generate_script,
    normalize_script,
    parse_script_json,
    render_figure_list,
    script_plain_text,
    stamp_estimates,
)
from lib.paper.podcast_engine._validate import (  # noqa: F401
    MATH_SYMBOLS,
    check_abbreviations,
    check_duration,
    check_latex_residue,
    check_number_provenance,
    check_structure,
    check_unicode_math,
    estimate_seconds,
    extract_data_numbers,
    validate_script,
)

logger = get_logger(__name__)


class PodcastSourceError(Exception):
    """No usable source material for this paper (report gate must stop it)."""


def _load_source_text(paper_hash: str, lang: str) -> tuple[str, str]:
    """Resolve the script's grounding material; return (text, kind).

    Order: report in the script's language → report in the other language →
    translation → parsed paper text. The start route gates on report
    presence (report-first UX); the deeper fallbacks exist for headless
    callers and for papers whose report lives in the other language.
    """
    from lib.database import get_thread_db

    db = get_thread_db()
    langs = [lang] + (['en'] if lang == 'zh' else ['zh'])
    for lg in langs:
        row = db.execute(
            'SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?',
            (paper_hash, lg)).fetchone()
        if row and (row['report'] or '').strip():
            return row['report'], f'report_{lg}'
    for lg in ('zh', 'en'):
        row = db.execute(
            'SELECT text FROM paper_translations WHERE paper_hash = ? AND lang = ?',
            (paper_hash, lg)).fetchone()
        if row and (row['text'] or '').strip():
            return row['text'], f'translation_{lg}'
    # Parsed full text lives in the paper_library row (not on disk).
    row = db.execute(
        'SELECT parsed_text FROM paper_library WHERE paper_hash = ? LIMIT 1',
        (paper_hash,)).fetchone()
    if row and (row['parsed_text'] or '').strip():
        return row['parsed_text'], 'parsed_text'
    return '', 'none'


def has_source_material(paper_hash: str) -> bool:
    """True when ANY report/translation/parsed text exists (route gate)."""
    text, kind = _load_source_text(paper_hash, 'zh')
    return kind != 'none' and bool(text.strip())


def has_report(paper_hash: str) -> bool:
    """True when a report exists in EITHER language (report-first gate)."""
    from lib.database import get_thread_db
    db = get_thread_db()
    for lg in ('zh', 'en'):
        row = db.execute(
            'SELECT 1 FROM paper_reports WHERE paper_hash = ? AND lang = ? LIMIT 1',
            (paper_hash, lg)).fetchone()
        if row:
            return True
    return False


def _persist_podcast_row(paper_hash: str, mode: str, lang: str, voice: str,
                         *, status: str, script: dict, meta: dict,
                         file_path: str = '', duration_sec: float = 0.0,
                         model: str = '', tts_model: str = '') -> None:
    """Upsert the paper_podcasts cache row (script + audio metadata)."""
    from lib.database import get_thread_db
    from lib.database._core_schema import PAPER_PODCASTS, upsert

    now = int(time.time())
    db = get_thread_db()
    upsert(db, PAPER_PODCASTS, {
        'paper_hash': paper_hash, 'mode': mode, 'lang': lang, 'voice': voice,
        'status': status,
        'script_json': json.dumps(script or {}, ensure_ascii=False),
        'file_path': file_path,
        'duration_sec': float(duration_sec or 0),
        'model': model or '', 'tts_model': tts_model or '',
        'meta': json.dumps(meta or {}, ensure_ascii=False),
        'created_at': now, 'updated_at': now,
    })
    db.commit()


def load_interrupted_podcast(paper_hash: str, mode: str, lang: str,
                             voice: str) -> bool:
    """True when the cache row says a previous run was cut by a restart.

    P-UX4 (docs/PAPER_MEDIA_UX_DESIGN.md §3.3): the worker persists a
    ``generating`` row at start; startup flips every lingering
    ``generating`` row to ``interrupted`` (a live process would have
    overwritten it). The lookup route surfaces this so the tab can say
    "被服务器重启打断" + offer a one-click regenerate, instead of
    pretending nothing ever happened.
    """
    from lib.database import get_thread_db

    db = get_thread_db()
    row = db.execute(
        'SELECT status FROM paper_podcasts WHERE paper_hash = ? AND mode = ?'
        ' AND lang = ? AND voice = ?',
        (paper_hash, mode, lang, voice)).fetchone()
    return bool(row and (row['status'] or '') == 'interrupted')


def mark_interrupted_podcasts() -> int:
    """Startup sweep: every ``generating`` row belongs to a dead process.

    Called once at server boot (next to motion's resume_interrupted_jobs).
    Returns the number of rows flipped. Best-effort, never raises.
    """
    try:
        from lib.database import get_thread_db
        db = get_thread_db()
        cur = db.execute(
            "UPDATE paper_podcasts SET status = 'interrupted',"
            ' updated_at = ? WHERE status = ?', (int(time.time()), 'generating'))
        db.commit()
        n = cur.rowcount if cur is not None else 0
        if n:
            logger.info('[Paper:Podcast] marked %d generating row(s) '
                        'interrupted on startup', n)
        return n or 0
    except Exception as e:
        logger.warning('[Paper:Podcast] interrupted sweep failed: %s', e)
        return 0


def load_cached_podcast(paper_hash: str, mode: str, lang: str,
                        voice: str) -> dict | None:
    """Fetch the cached row for the dedup key; parsed script/meta included."""
    from lib.database import get_thread_db

    db = get_thread_db()
    row = db.execute(
        'SELECT * FROM paper_podcasts WHERE paper_hash = ? AND mode = ?'
        ' AND lang = ? AND voice = ?',
        (paper_hash, mode, lang, voice)).fetchone()
    if not row or (row['status'] or '') not in ('done', 'script_only'):
        return None
    out = dict(row)
    for col in ('script_json', 'meta'):
        try:
            out[col] = json.loads(out.get(col) or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Paper:Podcast] cached %s unparsable for %s/%s/%s: %s',
                           col, paper_hash[:8], mode, lang, e)
            out[col] = {}
    return out


def _voice_slug(voice: str) -> str:
    slug = ''.join(c if (c.isalnum() or c in '-_') else '_' for c in (voice or ''))
    return slug[:40] or 'v'


def podcast_audio_url(paper_hash: str, mode: str, lang: str, voice: str) -> str:
    from urllib.parse import quote
    return (f'/api/v1/paper/podcast/audio/{paper_hash}/{mode}/{lang}/'
            f'{quote(voice or "-", safe="")}')


def _run_podcast_task(task):
    """Background worker: resolve source → script → TTS → file → DB → events.

    Event vocabulary (mirrors the report worker + two podcast-specific):
    status / phase / script / segment_done / audio_ready / done / error /
    aborted. The poll route flattens the task fields into the response.
    """
    from lib import tts as _tts
    from lib.paper import _load_image_manifest, _lookup_paper_title
    from lib.paper.podcast_runtime import _append_podcast_event
    from lib.production.heartbeat import heartbeat

    task_id = task['task_id']
    paper_hash, mode, lang = task['paper_hash'], task['mode'], task['lang']
    voice, model = task['voice'], task.get('model')
    task['status'] = 'running'

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
        _persist_podcast_row(paper_hash, mode, lang, voice,
                             status='generating', script={},
                             meta={'task_id': task_id},
                             model=model or '')
    except Exception as e:
        logger.warning('[Paper:Podcast] generating-row persist failed '
                       '(continuing): %s', e)

    def _aborted() -> bool:
        return bool(task['abort_event'].is_set())

    try:
        # ── Stage 0: source material (report gate runs in the route) ──
        _phase_started('source')
        source_text, source_kind = _load_source_text(paper_hash, lang)
        if not source_text:
            raise PodcastSourceError(f'no source material for {paper_hash[:8]}')
        images = _load_image_manifest(paper_hash)
        title = _lookup_paper_title(paper_hash)

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
            _persist_podcast_row(paper_hash, mode, lang, voice,
                                 status='script_only', script=script,
                                 meta=script_meta, model=task.get('model') or '')
            task['script_only'] = True
            task['status'] = 'done'
            _append_podcast_event(task, {
                'type': 'done', 'script': script, 'meta': script_meta,
                'scriptOnly': True, 'reason': 'no_tts_slot',
                'audioUrl': '', 'durationSec': 0})
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
        from lib.paper.hashing import PAPER_DIR
        out_dir = os.path.join(PAPER_DIR, 'podcast', paper_hash)
        os.makedirs(out_dir, exist_ok=True)
        fname = f'{mode}_{lang}_{_voice_slug(voice)}.{audio["ext"]}'
        fpath = os.path.join(out_dir, fname)
        tmp = fpath + f'.tmp.{os.getpid()}'
        with open(tmp, 'wb') as f:
            f.write(audio['audio_bytes'])
        os.replace(tmp, fpath)

        script_meta = {**script_meta,
                       'duration_estimated': audio['duration_estimated'],
                       'container': audio['container']}
        _persist_podcast_row(
            paper_hash, mode, lang, voice, status='done', script=script,
            meta=script_meta, file_path=fpath,
            duration_sec=audio['duration_sec'],
            model=task.get('model') or '', tts_model=audio['tts_model'])

        audio_url = podcast_audio_url(paper_hash, mode, lang, voice)
        task['audio_url'] = audio_url
        task['duration_sec'] = audio['duration_sec']
        _append_podcast_event(task, {'type': 'audio_ready', 'url': audio_url,
                                     'durationSec': audio['duration_sec'],
                                     'ext': audio['ext']})
        task['status'] = 'done'
        _append_podcast_event(task, {
            'type': 'done', 'script': script, 'meta': script_meta,
            'scriptOnly': False, 'audioUrl': audio_url,
            'durationSec': audio['duration_sec']})
        logger.info('[Paper:Podcast] task %s done: %s (%.1fs, %d KB)',
                    task_id, fname, audio['duration_sec'],
                    len(audio['audio_bytes']) // 1024)
        audit_log('paper_podcast_done', task_id=task_id, script_only=False,
                  duration_sec=round(audio['duration_sec'], 1),
                  tts_model=audio['tts_model'])

    except AudioSynthesisAborted:
        task['status'] = 'aborted'
        _append_podcast_event(task, {'type': 'aborted'})
        logger.info('[Paper:Podcast] task %s aborted', task_id)
        audit_log('paper_podcast_abort', task_id=task_id)
        _final_status = 'aborted'
    except PodcastSourceError as e:
        task['status'] = 'error'
        _append_podcast_event(task, {'type': 'error', 'error': str(e),
                                     'reason': 'report_required'})
        logger.warning('[Paper:Podcast] task %s source error: %s', task_id, e)
        _final_status = 'error'
    except Exception as e:
        task['status'] = 'error'
        err_env = {'type': 'error', 'error': f'podcast generation failed: {e}'}
        try:
            from lib import tts as _t
            if isinstance(e, _t.TTSError):
                err_env = {'type': 'error', 'error': e.detail,
                           'reason': 'tts_unavailable' if e.status == 503
                           else 'tts_failed'}
        except Exception as inner:
            logger.debug('[Paper:Podcast] error-envelope classify failed: %s', inner)
        _append_podcast_event(task, err_env)
        logger.error('[Paper:Podcast] task %s failed: %s', task_id, e,
                     exc_info=True)
        _final_status = 'error'
    finally:
        task['updated_at'] = time.time()
        # P-UX4/§3.4F: the generating row must never linger (it would be
        # misread as "interrupted by a restart" on the next boot). A completed
        # script survives an abort as a script_only row — the partial product
        # is kept, per the abort-semantics contract.
        if task.get('status') in ('aborted', 'error'):
            try:
                if task['status'] == 'aborted' and task.get('script'):
                    _persist_podcast_row(
                        paper_hash, mode, lang, voice, status='script_only',
                        script=task['script'],
                        meta={**(task.get('script_meta') or {}),
                              'degrade_reason': 'aborted_before_audio'},
                        model=model or '')
                else:
                    _persist_podcast_row(
                        paper_hash, mode, lang, voice,
                        status=task['status'], script=task.get('script') or {},
                        meta={'task_id': task_id}, model=model or '')
            except Exception as e:
                logger.warning('[Paper:Podcast] terminal-row persist failed: %s', e)


__all__ = [
    'AudioSynthesisAborted',
    'PodcastSourceError',
    'synthesize_script_audio',
    'has_report',
    'has_source_material',
    'load_cached_podcast',
    'load_interrupted_podcast',
    'mark_interrupted_podcasts',
    'podcast_audio_url',
    '_load_source_text',
    '_persist_podcast_row',
    '_run_podcast_task',
    'ScriptParseError',
    'critic_enabled',
    'generate_script',
    'normalize_script',
    'parse_script_json',
    'render_figure_list',
    'script_plain_text',
    'stamp_estimates',
    'MATH_SYMBOLS',
    'check_abbreviations',
    'check_duration',
    'check_latex_residue',
    'check_number_provenance',
    'check_structure',
    'check_unicode_math',
    'estimate_seconds',
    'extract_data_numbers',
    'validate_script',
]
