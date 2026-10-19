"""lib/production/t2v_media.py — text-to-video clip resolution (asset fallback).

「实拍素材找不到」的兜底: an OpenAI-compatible async video-generations
endpoint (MiniMax H3, Seedance/Ark, or any gateway speaking the same
shape) produces ONE bounded MP4 clip for a scene that stock could not
cover — or for an explicit swap_asset kind='t2v' request.

Provider discipline mirrors ``lib/production/stock_media.py``: env-gated
configuration, degraded ``{'ok': False, 'reason': ...}`` instead of
escaping exceptions, hard bounds on every axis (duration, poll wall-clock,
download bytes), and attribution fields the media ledger can carry.

Config (ALL required; any absence degrades cleanly):
    ``TOFU_T2V_BASE_URL``  e.g. https://api.minimaxi.com  (no trailing path)
    ``TOFU_T2V_API_KEY``
    ``TOFU_T2V_MODEL``     e.g. MiniMax-H3
Optional: ``TOFU_T2V_DURATION_S`` (default 5, clamped 2..12),
          ``TOFU_T2V_TIMEOUT_S`` (default 300, clamped 60..900),
          ``TOFU_T2V_POLL_S`` (default 2.0, clamped 0.5..10).
"""

from __future__ import annotations

import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['t2v_configured', 'resolve_t2v_clip', 'T2V_MAX_CLIP_BYTES']

T2V_MAX_CLIP_BYTES = 48 * 1024 * 1024
_MAX_TASK_RESPONSE_BYTES = 2 * 1024 * 1024
_TERMINAL_FAILURES = ('failed', 'fail', 'error', 'cancelled', 'canceled')
_TERMINAL_SUCCESS = ('success', 'succeeded', 'completed', 'done', 'finished')


def t2v_configured() -> bool:
    return all(os.environ.get(k) for k in
               ('TOFU_T2V_BASE_URL', 'TOFU_T2V_API_KEY', 'TOFU_T2V_MODEL'))


def _config() -> dict:
    missing = [k for k in ('TOFU_T2V_BASE_URL', 'TOFU_T2V_API_KEY',
                           'TOFU_T2V_MODEL')
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError('text-to-video is not configured: set '
                           + ', '.join(missing))
    def _float(name: str, default: float, lo: float, hi: float) -> float:
        try:
            value = float(os.environ.get(name) or default)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, value))
    return {
        'base': os.environ['TOFU_T2V_BASE_URL'].rstrip('/'),
        'key': os.environ['TOFU_T2V_API_KEY'],
        'model': os.environ['TOFU_T2V_MODEL'],
        'duration_s': int(_float('TOFU_T2V_DURATION_S', 5, 2, 12)),
        'timeout_s': _float('TOFU_T2V_TIMEOUT_S', 300, 60, 900),
        'poll_s': _float('TOFU_T2V_POLL_S', 2.0, 0.5, 10),
    }


def _bounded_json(response, what: str) -> dict:
    if len(response.content or b'') > _MAX_TASK_RESPONSE_BYTES:
        raise ValueError(f'{what} response exceeds 2 MiB')
    import json
    try:
        payload = json.loads(response.content)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f'{what} returned non-JSON: {exc}') from exc
    if not isinstance(payload, dict):
        raise ValueError(f'{what} returned a non-object payload')
    return payload


def _extract_task_id(payload: dict) -> str:
    for key in ('id', 'task_id', 'taskId', 'generation_id'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    data = payload.get('data')
    if isinstance(data, dict):
        return _extract_task_id(data)
    return ''


def _extract_status(payload: dict) -> tuple:
    """(status, video_url, error_message) across the common wire shapes."""
    body = payload.get('data') if isinstance(payload.get('data'), dict) \
        else payload
    status = str(body.get('status') or body.get('state') or '').lower()
    url = ''
    for key in ('video_url', 'videoUrl', 'url', 'download_url', 'file_url'):
        value = body.get(key)
        if isinstance(value, str) and value.startswith('http'):
            url = value
            break
    if not url and isinstance(body.get('videos'), list) and body['videos']:
        first = body['videos'][0]
        if isinstance(first, dict):
            url = str(first.get('video_url') or first.get('url') or '')
    error = str(body.get('error') or body.get('message') or '')[:300]
    return status, url, error


def resolve_t2v_clip(query: str, *, kind: str = 'video',
                     abort_check=None) -> dict:
    """Generate ONE bounded MP4 clip for ``query``.

    Returns ``{'ok', 'data', 'suffix', 'provider', 'query', 'duration_s'}``;
    every failure mode (unconfigured, provider error, timeout, oversize) is
    a degraded ``{'ok': False, 'reason': ...}``, never an exception.
    """
    query = str(query or '').strip()
    if not query:
        return {'ok': False, 'reason': 'text-to-video query is empty'}
    if len(query) > 1200:
        return {'ok': False, 'reason': 'text-to-video query exceeds 1200 '
                                       'chars'}
    if kind != 'video':
        return {'ok': False, 'reason': "only kind='video' is supported "
                                       '(image generation already exists)'}
    try:
        cfg = _config()
    except RuntimeError as exc:
        return {'ok': False, 'reason': str(exc)}
    try:
        from lib.http_transport.client import http_get, http_post
        headers = {'Authorization': f'Bearer {cfg["key"]}',
                   'Content-Type': 'application/json'}
        submit = http_post(
            f"{cfg['base']}/videos/generations", headers=headers, timeout=60,
            json={'model': cfg['model'], 'prompt': query,
                  'duration': cfg['duration_s']})
        submit.raise_for_status()
        task_id = _extract_task_id(_bounded_json(submit, 't2v submit'))
        if not task_id:
            raise ValueError('t2v submit returned no task id')
        logger.info('[T2VMedia] clip task %s (%ds) for %.60s',
                    task_id, cfg['duration_s'], query)

        deadline = time.monotonic() + cfg['timeout_s']
        video_url = ''
        while time.monotonic() < deadline:
            if abort_check is not None and abort_check():
                return {'ok': False, 'reason': 'aborted while polling'}
            time.sleep(cfg['poll_s'])
            poll = http_get(
                f"{cfg['base']}/videos/generations/{task_id}",
                headers=headers, timeout=30)
            poll.raise_for_status()
            status, video_url, error = _extract_status(
                _bounded_json(poll, 't2v poll'))
            if status in _TERMINAL_FAILURES:
                raise ValueError(f't2v task failed: {error or status}')
            if video_url and (status in _TERMINAL_SUCCESS or status == ''):
                break
        else:
            return {'ok': False,
                    'reason': f't2v task did not finish in '
                              f"{int(cfg['timeout_s'])}s"}
        if not video_url:
            raise ValueError('t2v task finished without a video url')

        download = http_get(video_url, headers={}, timeout=120, stream=True)
        download.raise_for_status()
        content_type = str(
            download.headers.get('content-type') or '').split(';')[0].strip()
        if content_type and not content_type.startswith('video/'):
            raise ValueError(f't2v clip is {content_type!r}, not video/*')
        data = download.content
        if not 0 < len(data) <= T2V_MAX_CLIP_BYTES:
            raise ValueError(f't2v clip is {len(data)} bytes; limit is '
                             f'{T2V_MAX_CLIP_BYTES}')
        logger.info('[T2VMedia] clip ready: %d bytes for %.60s',
                    len(data), query)
        return {'ok': True, 'data': data, 'suffix': '.mp4',
                'provider': f"t2v:{cfg['model']}", 'query': query,
                'duration_s': cfg['duration_s']}
    except Exception as exc:
        logger.warning('[T2VMedia] clip generation failed: %s', exc)
        return {'ok': False, 'reason': str(exc)[:300]}
