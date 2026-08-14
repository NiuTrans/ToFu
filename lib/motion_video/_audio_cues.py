"""Licensed, deterministic BGM/SFX timeline and FFmpeg mix contract.

Audio is a film-level timeline, never ad-hoc code inside a scene.  Every asset
is local, hashed and staged into the job; every non-original asset retains its
license/source metadata; every SFX resolves to a concrete program-time target.
The mixer can therefore be re-run byte-for-byte after a crash or scene regen.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'AUDIO_CONTRACT_VERSION', 'normalise_audio_plan', 'load_audio_plan',
    'audio_plan_errors', 'mix_audio_timeline', 'audio_plan_summary',
    'audio_plan_template', 'write_audio_attribution',
]

AUDIO_CONTRACT_VERSION = 'motion-audio-v1'
_MAX_CUES = 128
_SAFE_LOCAL_LICENSES = frozenset({'original', 'user-provided', 'internal'})
_CUE_KINDS = frozenset({
    'whoosh', 'impact', 'riser', 'sparkle', 'transition', 'foley',
    'ambience', 'click', 'typing', 'custom',
})


def _number(value, fallback: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        logger.debug('[MotionVideo] invalid numeric cue value %r: %s', value, exc)
        parsed = float(fallback)
    return max(low, min(high, parsed))


def _safe_name(path: str) -> str:
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', os.path.basename(path)).strip('-')
    return name or 'audio.bin'


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_metadata(raw: dict, *, label: str) -> dict:
    license_name = str(raw.get('license') or '').strip()
    source_url = str(raw.get('source_url') or '').strip()
    attribution = str(raw.get('attribution') or '').strip()
    if not license_name:
        raise ValueError(f'{label}: license is required')
    local_license = license_name.lower() in _SAFE_LOCAL_LICENSES
    if not local_license:
        parsed = urlparse(source_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError(f'{label}: non-original audio requires source_url')
    if 'cc-by' in license_name.lower() and not attribution:
        raise ValueError(f'{label}: CC-BY audio requires attribution text')
    return {
        'title': str(raw.get('title') or '').strip(),
        'license': license_name,
        'source_url': source_url,
        'attribution': attribution,
    }


def _resolve_local_asset(path: str, base_dir: str) -> str:
    raw = str(path or '').strip()
    if not raw:
        raise ValueError('audio asset path is required')
    parsed = urlparse(raw)
    if parsed.scheme in ('http', 'https'):
        raise ValueError('audio assets must be local; runtime downloads are forbidden')
    resolved = os.path.realpath(raw if os.path.isabs(raw)
                                else os.path.join(base_dir, raw))
    root = os.path.realpath(base_dir)
    try:
        inside = os.path.commonpath([resolved, root]) == root
    except ValueError as exc:
        logger.debug('[MotionVideo] audio asset path comparison failed: %s', exc)
        inside = False
    if not inside:
        raise ValueError(f'audio asset escapes its plan directory: {raw!r}')
    if not os.path.isfile(resolved):
        raise ValueError(f'audio asset does not exist: {raw!r}')
    return resolved


def _stage_asset(path: str, stage_dir: str, staged: dict[str, dict]) -> dict:
    sha = _sha256_file(path)
    if sha in staged:
        return dict(staged[sha])
    os.makedirs(stage_dir, exist_ok=True)
    dest = os.path.join(stage_dir, f'{sha[:12]}-{_safe_name(path)}')
    if not os.path.isfile(dest):
        from lib.json_store import atomic_output_path
        with atomic_output_path(dest) as tmp:
            shutil.copyfile(path, tmp)
    record = {'asset_path': dest, 'sha256': sha,
              'bytes': os.path.getsize(dest)}
    staged[sha] = record
    return dict(record)


def _beat_grid(raw) -> dict:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError('beat_grid must be an object')
    bpm = _number(raw.get('bpm'), 0, 0, 400)
    if not 20 <= bpm <= 300:
        raise ValueError('beat_grid bpm must be between 20 and 300')
    verified = bool(raw.get('verified'))
    offset = _number(raw.get('offset_s'), 0, -30, 30)
    observations = raw.get('beat_observations_s')
    if observations is not None and not isinstance(observations, list):
        raise ValueError('beat_observations_s must be a list of seconds/nulls')
    if verified and (not observations or len(observations) < 4):
        raise ValueError(
            'verified beat_grid requires at least 4 beat observations')

    residuals: list[tuple[int, float]] = []
    clean_observations: list[float | None] = []
    if observations:
        period = 60.0 / bpm
        for index, value in enumerate(observations):
            if value is None:
                clean_observations.append(None)
                continue
            try:
                observed = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'beat observation #{index} is not a second/null') from exc
            if not math.isfinite(observed) or observed < 0:
                raise ValueError(
                    f'beat observation #{index} must be a finite second')
            clean_observations.append(round(observed, 6))
            residuals.append((index, observed - (offset + index * period)))

    if residuals:
        residual = max(abs(value) for _, value in residuals) * 1000
        match_ratio = len(residuals) / len(clean_observations)
        mean_abs_ms = (sum(abs(value) for _, value in residuals)
                       / len(residuals) * 1000)
        if len(residuals) > 1:
            mean_i = sum(index for index, _ in residuals) / len(residuals)
            mean_r = sum(value for _, value in residuals) / len(residuals)
            denom = sum((index - mean_i) ** 2 for index, _ in residuals)
            slope = (sum((index - mean_i) * (value - mean_r)
                         for index, value in residuals) / denom
                     if denom else 0)
            drift_ms = abs(slope) * max(0, len(clean_observations) - 1) * 1000
        else:
            drift_ms = 0.0
    else:
        residual = _number(raw.get('max_residual_ms'), 9999, 0, 9999)
        match_ratio = _number(raw.get('match_ratio'), 0, 0, 1)
        mean_abs_ms = _number(raw.get('mean_abs_ms'), 9999, 0, 9999)
        drift_ms = _number(raw.get('drift_ms'), 9999, 0, 9999)

    claimed_first_valid = bool(raw.get('first_beat_valid'))
    first_observed = bool(clean_observations
                          and clean_observations[0] is not None
                          and residuals
                          and residuals[0][0] == 0
                          and abs(residuals[0][1]) <= 0.015)
    first_beat_valid = claimed_first_valid and first_observed
    if verified and not (residual <= 15 and match_ratio >= 0.98
                         and mean_abs_ms < 10 and drift_ms < 5
                         and first_beat_valid):
        raise ValueError(
            'verified beat_grid requires residual<=15ms, match>=98%, '
            'mean_abs<10ms, drift<5ms and a valid first beat')
    return {
        'bpm': round(bpm, 5),
        'offset_s': round(offset, 6),
        'beat_observations_s': clean_observations,
        'max_residual_ms': round(residual, 3),
        'match_ratio': round(match_ratio, 5),
        'mean_abs_ms': round(mean_abs_ms, 3),
        'drift_ms': round(drift_ms, 3),
        'first_beat_valid': first_beat_valid,
        'verified': verified,
        'analysis_source': str(raw.get('analysis_source') or 'manual'),
    }


def _cue_target(raw: dict, scenes_by_id: dict[str, dict], beat: dict,
                duration: float, label: str) -> float:
    if raw.get('beat') is not None:
        if not beat:
            raise ValueError(f'{label}: beat target requires beat_grid')
        if not beat.get('verified'):
            raise ValueError(f'{label}: beat target requires a verified beat_grid')
        target = (float(beat['offset_s'])
                  + float(raw['beat']) * 60.0 / float(beat['bpm']))
    elif raw.get('at_s') is not None:
        target = float(raw['at_s'])
    else:
        scene_id = str(raw.get('scene_id') or '')
        scene = scenes_by_id.get(scene_id)
        if scene is None:
            raise ValueError(f'{label}: unknown scene_id {scene_id!r}')
        start = float(scene.get('timeline_start_s') or 0)
        content = float(scene.get('content_duration_s') or
                        (float(scene.get('end') or 0)
                         - float(scene.get('start') or 0)))
        if raw.get('progress') is not None:
            progress = _number(raw.get('progress'), 0, 0, 1)
            target = start + progress * content
        else:
            target = start + _number(raw.get('offset_s'), 0, 0, content)
    if not 0 <= target <= duration:
        raise ValueError(f'{label}: target {target:.3f}s is outside the film')
    return target


def normalise_audio_plan(plan: dict, scenes: list[dict], *, base_dir: str,
                         stage_dir: str, program_duration: float) -> dict:
    """Validate, resolve and stage one audio plan."""
    if not isinstance(plan, dict):
        raise ValueError('audio_plan must be an object')
    supplied_version = str(plan.get('version') or '').strip()
    if supplied_version and supplied_version != AUDIO_CONTRACT_VERSION:
        raise ValueError(f'unsupported audio plan version {supplied_version!r}')
    duration = max(0.001, float(program_duration))
    beat = _beat_grid(plan.get('beat_grid'))
    scenes_by_id = {str(scene.get('id') or ''): scene for scene in scenes}
    staged: dict[str, dict] = {}
    out = {
        'version': AUDIO_CONTRACT_VERSION,
        'program_duration_s': round(duration, 4),
        'beat_grid': beat,
        'beat_sync_mode': str(plan.get('beat_sync_mode') or 'audit').lower(),
        'beat_alignment': {},
        'bgm': {},
        'cues': [],
        'mix': {
            'narration_gain_db': _number(
                (plan.get('mix') or {}).get('narration_gain_db'), 0, -24, 12),
            'ducking_db': _number(
                (plan.get('mix') or {}).get('ducking_db'), 10, 0, 30),
            'loudness_lufs': _number(
                (plan.get('mix') or {}).get('loudness_lufs'), -14, -24, -9),
        },
    }
    if out['beat_sync_mode'] not in ('off', 'audit', 'required'):
        raise ValueError('beat_sync_mode must be off|audit|required')
    bgm = plan.get('bgm') or {}
    if bgm:
        if not isinstance(bgm, dict):
            raise ValueError('bgm must be an object')
        source = _resolve_local_asset(bgm.get('asset'), base_dir)
        staged_asset = _stage_asset(source, stage_dir, staged)
        out['bgm'] = {
            **staged_asset,
            **_asset_metadata(bgm, label='bgm'),
            'gain_db': _number(bgm.get('gain_db'), -12, -60, 6),
            'trim_start_s': _number(bgm.get('trim_start_s'), 0, 0, 86400),
            'fade_in_s': _number(bgm.get('fade_in_s'), 1.0, 0, duration),
            'fade_out_s': _number(bgm.get('fade_out_s'), 1.5, 0, duration),
            'loop': bool(bgm.get('loop', True)),
        }
    cues = plan.get('cues') or []
    if not isinstance(cues, list):
        raise ValueError('audio cues must be a list')
    if len(cues) > _MAX_CUES:
        raise ValueError(f'audio cues exceed the {_MAX_CUES}-cue limit')
    seen_ids = set()
    for index, cue in enumerate(cues, 1):
        if not isinstance(cue, dict):
            raise ValueError(f'audio cue #{index} must be an object')
        cue_id = str(cue.get('id') or f'cue-{index:03d}').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', cue_id):
            raise ValueError(f'audio cue #{index} has invalid id')
        if cue_id in seen_ids:
            raise ValueError(f'duplicate audio cue id {cue_id!r}')
        seen_ids.add(cue_id)
        kind = str(cue.get('kind') or 'custom').strip().lower()
        if kind not in _CUE_KINDS:
            raise ValueError(f'audio cue {cue_id}: unsupported kind {kind!r}')
        source = _resolve_local_asset(cue.get('asset'), base_dir)
        staged_asset = _stage_asset(source, stage_dir, staged)
        target = _cue_target(cue, scenes_by_id, beat, duration,
                             f'audio cue {cue_id}')
        peak = _number(cue.get('peak_offset_s'), 0, 0, 30)
        unclamped_start = target - peak
        start = max(0.0, unclamped_start)
        trim_start = _number(cue.get('trim_start_s'), 0, 0, 86400)
        # If a file's internal peak would require a negative timeline start,
        # trim the unavailable lead-in so the peak still lands on its target.
        if unclamped_start < 0:
            trim_start += -unclamped_start
        cue_duration = _number(cue.get('duration_s'), duration - start,
                               0.01, duration - start)
        out['cues'].append({
            'id': cue_id,
            'kind': kind,
            **staged_asset,
            **_asset_metadata(cue, label=f'audio cue {cue_id}'),
            'target_s': round(target, 6),
            'start_s': round(start, 6),
            'peak_offset_s': round(peak, 6),
            'trim_start_s': round(trim_start, 6),
            'duration_s': round(cue_duration, 6),
            'gain_db': _number(cue.get('gain_db'), -7, -60, 18),
            'fade_in_s': _number(cue.get('fade_in_s'), 0, 0, cue_duration),
            'fade_out_s': _number(cue.get('fade_out_s'), 0, 0, cue_duration),
            'scene_id': str(cue.get('scene_id') or ''),
            'action': str(cue.get('action') or '').strip(),
        })
    if not out['bgm'] and not out['cues']:
        raise ValueError('audio_plan contains no BGM or SFX cues')
    out['beat_alignment'] = _beat_alignment(
        beat, scenes, mode=out['beat_sync_mode'])
    if (out['beat_sync_mode'] == 'required'
            and out['beat_alignment'].get('failed')):
        raise ValueError(
            'required beat sync failed: transition cuts exceed 3 frames')
    return out


def _beat_alignment(beat: dict, scenes: list[dict], *, mode: str,
                    fps: float = 30.0) -> dict:
    if mode == 'off' or not beat or not beat.get('verified'):
        return {'checked': 0, 'failed': 0, 'max_error_frames': 0.0,
                'status': 'off' if mode == 'off' else 'unverified'}
    period = 60.0 / float(beat['bpm'])
    offset = float(beat['offset_s'])
    items = []
    for scene in (scenes or [])[1:]:
        if not float(scene.get('transition_in_duration_s') or 0):
            continue
        time_s = float(scene.get('timeline_start_s') or 0)
        beat_no = round((time_s - offset) / period)
        beat_time = offset + beat_no * period
        error_s = abs(time_s - beat_time)
        error_frames = error_s * fps
        items.append({
            'scene_id': str(scene.get('id') or ''),
            'time_s': round(time_s, 6),
            'nearest_beat': int(beat_no),
            'error_ms': round(error_s * 1000, 3),
            'error_frames': round(error_frames, 3),
            'ok': error_frames <= 3.0,
        })
    failed = sum(1 for item in items if not item['ok'])
    return {
        'checked': len(items),
        'failed': failed,
        'max_error_frames': max(
            (item['error_frames'] for item in items), default=0.0),
        'status': 'passed' if not failed else 'failed',
        'items': items,
    }


def load_audio_plan(task: dict, scenes: list[dict], workdir: str,
                    program_duration: float) -> dict | None:
    """Load inline/path input and stage a normalized plan into the job."""
    inline = task.get('audio_plan')
    plan_path = str(task.get('audio_plan_path') or '').strip()
    if not inline and not plan_path:
        return None
    if plan_path:
        with open(plan_path, encoding='utf-8') as handle:
            raw = json.load(handle)
        base_dir = os.path.dirname(os.path.abspath(plan_path))
    else:
        raw = inline
        base_dir = str(task.get('audio_base_dir') or '').strip()
        if not base_dir:
            scenes_path = str(task.get('scenes_path') or '').strip()
            base_dir = (os.path.dirname(os.path.abspath(scenes_path))
                        if scenes_path else workdir)
    return normalise_audio_plan(
        raw, scenes, base_dir=base_dir,
        stage_dir=os.path.join(workdir, 'audio', 'assets'),
        program_duration=program_duration)


def audio_plan_errors(plan: dict | None) -> list[str]:
    if plan is None:
        return []
    if not isinstance(plan, dict):
        return ['audio plan is not an object']
    errors = []
    if plan.get('version') != AUDIO_CONTRACT_VERSION:
        errors.append('audio plan has an unknown contract version')
    hashes: dict[str, str] = {}
    for label, item in ([('bgm', plan.get('bgm'))]
                        + [(f'cue {cue.get("id")}', cue)
                           for cue in (plan.get('cues') or [])]):
        if not item:
            continue
        path = str(item.get('asset_path') or '')
        if not path or not os.path.isfile(path):
            errors.append(f'{label}: staged asset is missing')
        elif item.get('sha256'):
            actual = hashes.get(path)
            if actual is None:
                try:
                    actual = _sha256_file(path)
                except OSError as exc:
                    errors.append(f'{label}: cannot hash staged asset: {exc}')
                    actual = ''
                hashes[path] = actual
            if actual and actual != item.get('sha256'):
                errors.append(f'{label}: staged asset hash drift')
        if not item.get('sha256') or not item.get('license'):
            errors.append(f'{label}: provenance is incomplete')
    return errors


def audio_plan_summary(plan: dict | None) -> dict:
    if not plan:
        return {'enabled': False, 'bgm': False, 'cues': 0,
                'beat_synced': False}
    licenses = sorted({str(item.get('license')) for item in
                       ([plan.get('bgm')] + list(plan.get('cues') or []))
                       if item and item.get('license')})
    return {
        'enabled': True,
        'contract_version': plan.get('version'),
        'bgm': bool(plan.get('bgm')),
        'cues': len(plan.get('cues') or []),
        'beat_synced': bool((plan.get('beat_grid') or {}).get('verified')),
        'beat_alignment': dict(plan.get('beat_alignment') or {}),
        'licenses': licenses,
    }


def audio_plan_template() -> dict:
    """Return a copyable public example without pretending assets exist."""
    return {
        'version': AUDIO_CONTRACT_VERSION,
        'beat_grid': {
            'bpm': 128.0,
            'offset_s': 0.2244,
            'beat_observations_s': [],
            'verified': False,
            'max_residual_ms': 0,
            'match_ratio': 0,
            'mean_abs_ms': 0,
            'drift_ms': 0,
            'first_beat_valid': False,
            'analysis_source': 'manual-or-librosa-grid-fit',
        },
        'beat_sync_mode': 'audit',
        'bgm': {
            'asset': 'audio/bgm.mp3',
            'title': 'Track title',
            'license': 'user-provided',
            'source_url': '',
            'attribution': '',
            'gain_db': -12,
            'fade_in_s': 1.0,
            'fade_out_s': 1.5,
            'loop': True,
        },
        'cues': [{
            'id': 'hero-impact',
            'kind': 'impact',
            'asset': 'audio/impact.wav',
            'license': 'user-provided',
            'scene_id': 'scene-001',
            'progress': 0.5,
            'peak_offset_s': 0.08,
            'duration_s': 1.2,
            'gain_db': -6,
            'action': 'hero subject lands',
        }],
        'mix': {
            'narration_gain_db': 0,
            'ducking_db': 10,
            'loudness_lufs': -14,
        },
    }


def write_audio_attribution(plan: dict, path: str) -> str:
    """Write the human-deliverable license/source manifest."""
    from lib.json_store import write_text_atomic

    lines = [
        f'Audio attribution — {AUDIO_CONTRACT_VERSION}',
        'Assets are content-addressed in audio_plan.json.',
        '',
    ]
    seen = set()
    items = ([('BGM', plan.get('bgm'))]
             + [('SFX', cue) for cue in (plan.get('cues') or [])])
    for kind, item in items:
        if not item or item.get('sha256') in seen:
            continue
        seen.add(item.get('sha256'))
        title = item.get('title') or os.path.basename(item.get('asset_path') or '')
        lines.append(f'- {kind}: {title}')
        lines.append(f'  License: {item.get("license") or "unknown"}')
        if item.get('source_url'):
            lines.append(f'  Source: {item["source_url"]}')
        if item.get('attribution'):
            lines.append(f'  Attribution: {item["attribution"]}')
        lines.append(f'  SHA-256: {item.get("sha256") or "unknown"}')
    write_text_atomic(path, '\n'.join(lines) + '\n')
    return path


def _mix_args(ffmpeg: str, video_path: str, narration_path: str,
              plan: dict, output: str) -> tuple[list[str], str]:
    """Build the deterministic filter graph; split for contract tests."""
    duration = float(plan['program_duration_s'])
    args = [ffmpeg, '-y', '-i', video_path]
    input_index = 1
    narration_index = None
    if narration_path:
        narration_index = input_index
        args += ['-i', narration_path]
        input_index += 1
    bgm_index = None
    bgm = plan.get('bgm') or {}
    if bgm:
        bgm_index = input_index
        if bgm.get('loop'):
            args += ['-stream_loop', '-1']
        args += ['-i', bgm['asset_path']]
        input_index += 1
    cue_indexes = []
    for cue in plan.get('cues') or []:
        cue_indexes.append(input_index)
        args += ['-i', cue['asset_path']]
        input_index += 1

    filters = []
    mix_labels = []
    voice_side = ''
    if narration_index is not None:
        gain = float((plan.get('mix') or {}).get('narration_gain_db') or 0)
        filters.append(
            f'[{narration_index}:a]aresample=48000,apad,'
            f'atrim=duration={duration:.4f},volume={gain:.2f}dB[voice0]')
        if bgm:
            filters.append('[voice0]asplit=2[voice][voice_sc]')
            mix_labels.append('[voice]')
            voice_side = '[voice_sc]'
        else:
            mix_labels.append('[voice0]')
    if bgm_index is not None:
        trim = float(bgm.get('trim_start_s') or 0)
        gain = float(bgm.get('gain_db') or -12)
        fade_in = float(bgm.get('fade_in_s') or 0)
        fade_out = float(bgm.get('fade_out_s') or 0)
        chain = (f'[{bgm_index}:a]aresample=48000,'
                 f'atrim=start={trim:.4f}:duration={duration:.4f},'
                 f'asetpts=PTS-STARTPTS,volume={gain:.2f}dB')
        if fade_in:
            chain += f',afade=t=in:st=0:d={fade_in:.4f}'
        if fade_out:
            chain += (f',afade=t=out:st={max(0, duration-fade_out):.4f}:'
                      f'd={fade_out:.4f}')
        chain += '[bgm0]'
        filters.append(chain)
        if voice_side:
            duck = float((plan.get('mix') or {}).get('ducking_db') or 10)
            ratio = max(2.0, min(20.0, 1.0 + duck / 2.0))
            filters.append(
                f'[bgm0]{voice_side}sidechaincompress=threshold=0.06:'
                f'ratio={ratio:.2f}:attack=20:release=450[bgm]')
            mix_labels.append('[bgm]')
        else:
            mix_labels.append('[bgm0]')
    for index, (cue_index, cue) in enumerate(
            zip(cue_indexes, plan.get('cues') or [])):
        trim = float(cue.get('trim_start_s') or 0)
        cue_duration = float(cue['duration_s'])
        start = float(cue['start_s'])
        gain = float(cue.get('gain_db') or -7)
        chain = (f'[{cue_index}:a]aresample=48000,'
                 f'atrim=start={trim:.4f}:duration={cue_duration:.4f},'
                 f'asetpts=PTS-STARTPTS,volume={gain:.2f}dB')
        fade_in = float(cue.get('fade_in_s') or 0)
        fade_out = float(cue.get('fade_out_s') or 0)
        if fade_in:
            chain += f',afade=t=in:st=0:d={fade_in:.4f}'
        if fade_out:
            chain += (f',afade=t=out:st={max(0, cue_duration-fade_out):.4f}:'
                      f'd={fade_out:.4f}')
        chain += f',adelay={round(start * 1000)}:all=1[sfx{index}]'
        filters.append(chain)
        mix_labels.append(f'[sfx{index}]')
    if not mix_labels:
        raise ValueError('audio mix has no tracks')
    lufs = float((plan.get('mix') or {}).get('loudness_lufs') or -14)
    filters.append(
        ''.join(mix_labels)
        + f'amix=inputs={len(mix_labels)}:duration=longest:normalize=0,'
          f'atrim=duration={duration:.4f},alimiter=limit=0.95,'
          f'loudnorm=I={lufs:.2f}:TP=-1.0:LRA=11[outa]')
    graph = ';'.join(filters)
    args += [
        '-filter_complex', graph, '-map', '0:v:0', '-map', '[outa]',
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', f'{duration:.4f}',
        '-movflags', '+faststart', output,
    ]
    return args, graph


def mix_audio_timeline(video_path: str, narration_path: str, plan: dict,
                       output: str, *, timeout: int = 900,
                       abort_event=None) -> dict:
    """Mix narration/BGM/SFX against program time and mux atomically."""
    from lib.motion_video._env import ffmpeg_bin
    from lib.motion_video._gates import probe_video
    from lib.motion_video._render import _run_cli

    if not os.path.isfile(video_path):
        return {'ok': False, 'category': 'io',
                'detail': f'missing video file: {video_path}'}
    if narration_path and not os.path.isfile(narration_path):
        return {'ok': False, 'category': 'io',
                'detail': f'missing narration file: {narration_path}'}
    errors = audio_plan_errors(plan)
    if errors:
        return {'ok': False, 'category': 'contract',
                'detail': ' | '.join(errors)}
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return {'ok': False, 'category': 'env_missing',
                'detail': 'ffmpeg not found (run motion_video_env_check)'}
    from lib.json_store import temporary_output_path
    with temporary_output_path(output, suffix='.tmp.mp4') as tmp_out:
        try:
            args, _graph = _mix_args(
                ffmpeg, video_path, narration_path, plan, tmp_out)
        except ValueError as exc:
            logger.debug('[MotionVideo] invalid audio mix contract: %s', exc)
            return {'ok': False, 'category': 'contract', 'detail': str(exc)}
        res = _run_cli(args,
                       cwd=os.path.dirname(os.path.abspath(output)) or '.',
                       timeout=timeout, abort_event=abort_event)
        if res['category'] or res['rc'] != 0:
            return {'ok': False, 'category': res['category'] or 'unknown',
                    'detail': res['err'][-1500:]}
        if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
            return {'ok': False, 'category': 'io',
                    'detail': 'ffmpeg produced no mixed output'}
        probe = probe_video(tmp_out)
        if not probe or not probe.get('has_audio'):
            return {'ok': False, 'category': 'io',
                    'detail': 'mixed output has no audio track'}
        drift = abs(float(probe.get('duration') or 0)
                    - float(plan['program_duration_s']))
        if drift > 0.5:
            return {'ok': False, 'category': 'io',
                    'detail': f'mixed duration drifted {drift:.3f}s'}
        os.replace(tmp_out, output)
    return {
        'ok': True, 'output': output,
        'duration': round(float(probe.get('duration') or 0), 3),
        'elapsed': round(res['elapsed'], 2),
        'audio': audio_plan_summary(plan),
    }
