"""Asset-first image generation for slide storyboards.

The old assets stage only downloaded URLs that a caller had already supplied;
with the normal topic entry point it therefore did nothing.  This preflight
materialises the storyboard's explicit ``asset_prompt`` obligations before
page authoring, persists an exact bounded resume manifest, and returns
deck-relative paths the page author can actually use. Provider calls use the
shared production image fan-out/retry policy; invalid cache bytes regenerate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from lib.json_store import write_bytes_atomic
from lib.log import get_logger
from lib.production.contracts import normalise_asset_briefs

logger = get_logger(__name__)

__all__ = ['prepare_deck_assets', 'MANIFEST_NAME']

MANIFEST_NAME = '.tofu-slide-assets.json'
_MAX_ASSETS = 6
_MAX_MANIFEST_BYTES = 128 * 1024
_GENERATED_PATH_RE = re.compile(
    r'^media/generated_[0-9]{2}\.(?:png|jpg|webp)$')


def _load(deck_dir: str) -> list[dict]:
    path = os.path.join(deck_dir, MANIFEST_NAME)
    try:
        with open(path, 'rb') as fh:
            data = fh.read(_MAX_MANIFEST_BYTES + 1)
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ValueError('asset manifest exceeds byte limit')
        raw = json.loads(data.decode('utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
        logger.debug('[SlideAssetPreflight] manifest unavailable %s: %s',
                     path, e)
        return []
    return raw if isinstance(raw, list) else []


def _usable(deck_dir: str, rec: dict) -> bool:
    rel = str(rec.get('path') or '')
    declared_bytes = rec.get('bytes')
    expected_sha256 = rec.get('sha256')
    if (not _GENERATED_PATH_RE.fullmatch(rel)
            or isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)):
        return False
    from lib.slides._media_io import (MAX_SLIDE_IMAGE_BYTES,
                                      hash_file_bounded)
    if not 0 < declared_bytes <= MAX_SLIDE_IMAGE_BYTES:
        return False
    path = os.path.realpath(os.path.join(deck_dir, *rel.split('/')))
    root = os.path.realpath(deck_dir)
    if not path.startswith(root + os.sep):
        return False
    try:
        actual_sha256, actual_bytes = hash_file_bounded(path)
    except (OSError, ValueError):
        return False
    return (actual_bytes == declared_bytes
            and actual_sha256 == expected_sha256)


def _write_bytes(path: str, data: bytes) -> None:
    write_bytes_atomic(path, data)


def _generate(index: int, prompt: str, semantic_target: str,
              deck_dir: str, *, abort_check=None,
              max_429_attempts: int | None = None,
              owner_user_id: int | None = None,
              tenant_id: str | None = None) -> dict:
    from lib.image_gen import generate_image
    from lib.production.image_policy import production_image_dispatch_kwargs
    from lib.slides._media_io import decode_image_base64_bounded

    if abort_check is not None and abort_check():
        raise InterruptedError('slide asset generation aborted')
    result = generate_image(
        prompt, aspect_ratio='16:9', resolution='2K',
        **production_image_dispatch_kwargs(
            abort_check=abort_check,
            max_429_attempts=max_429_attempts),
        owner_user_id=owner_user_id,
        tenant_id=tenant_id)
    if result.get('aborted') or (abort_check is not None and abort_check()):
        raise InterruptedError('slide asset generation aborted')
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or 'image generation failed')
    raw = decode_image_base64_bounded(result.get('image_b64'))
    mime = str(result.get('mime_type') or 'image/png').lower()
    ext = {'image/png': '.png', 'image/jpeg': '.jpg',
           'image/webp': '.webp'}.get(mime, '.png')
    rel = f'media/generated_{index + 1:02d}{ext}'
    _write_bytes(os.path.join(deck_dir, rel), raw)
    record = {'page_index': index, 'prompt': prompt, 'path': rel,
              'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if semantic_target:
        record['semantic_target'] = semantic_target
    return record


def prepare_deck_assets(outline: dict, deck_dir: str, *, max_assets: int = 6,
                        parallel: int | None = None, abort_check=None,
                        max_429_attempts: int | None = None,
                        owner_user_id: int | None = None,
                        tenant_id: str | None = None) -> dict:
    """Generate storyboard assets concurrently and attach them to pages.

    Returns ``{'by_page': {index: [path]}, 'records': [...], 'findings': [...]}``.
    Provider failures are findings, never exceptions that kill the deck.
    """
    if (isinstance(max_assets, bool) or not isinstance(max_assets, int)
            or max_assets < 0):
        raise ValueError('max_assets must be a non-negative integer')
    max_assets = min(_MAX_ASSETS, max_assets)
    if parallel is None:
        from lib.production.image_policy import production_image_fanout
        worker_budget = production_image_fanout()
    else:
        from lib.production.image_policy import production_image_fanout
        worker_budget = production_image_fanout(parallel)
    from lib.production.image_policy import production_image_max_429_attempts
    resolved_max_429_attempts = production_image_max_429_attempts(
        max_429_attempts)

    pages = outline.get('pages') or []
    wanted: list[tuple[int, str, str]] = []
    for index, page in enumerate(pages):
        if len(wanted) >= max_assets:
            break
        if not isinstance(page, dict) or page.get('asset_mode') != 'generate':
            continue
        briefs = normalise_asset_briefs(
            [{'role': 'subject', 'prompt': page.get('asset_prompt'),
              'semantic_target': page.get('asset_semantic_target')}],
            allowed_roles=('subject',), fallback_role='subject', max_items=1,
            log_prefix='[SlideAssetPreflight]')
        if briefs:
            wanted.append((index, briefs[0]['prompt'],
                           str(briefs[0].get('semantic_target') or '')))
    cached = _load(deck_dir)
    records: list[dict] = []
    missing: list[tuple[int, str, str]] = []
    for index, prompt, semantic_target in wanted:
        hit = next((r for r in cached
                    if r.get('page_index') == index and r.get('prompt') == prompt
                    and _usable(deck_dir, r)), None)
        if hit:
            record = dict(hit)
            if semantic_target:
                record['semantic_target'] = semantic_target
            records.append(record)
        else:
            missing.append((index, prompt, semantic_target))

    findings: list[str] = []
    aborted = bool(abort_check is not None and abort_check())
    if missing and not aborted:
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        worker_limit = min(worker_budget, len(missing))
        pending = iter(missing)
        in_flight: dict = {}

        def _submit_one(pool) -> bool:
            try:
                index, prompt, semantic_target = next(pending)
            except StopIteration:
                return False
            future = pool.submit(
                _generate, index, prompt, semantic_target, deck_dir,
                abort_check=abort_check,
                max_429_attempts=resolved_max_429_attempts,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id)
            in_flight[future] = (index, prompt)
            return True

        with ThreadPoolExecutor(
                max_workers=worker_limit,
                thread_name_prefix='slides-image') as pool:
            for _ in range(worker_limit):
                _submit_one(pool)
            while in_flight:
                completed, _not_done = wait(
                    in_flight, return_when=FIRST_COMPLETED)
                for future in sorted(
                        completed, key=lambda item: in_flight[item][0]):
                    index, _prompt = in_flight.pop(future)
                    try:
                        records.append(future.result())
                    except InterruptedError:
                        aborted = True
                    except Exception as exc:
                        logger.warning(
                            '[Slides:AssetPreflight] page %d failed: %s',
                            index + 1, exc)
                        findings.append(
                            f'page {index + 1} required an editorial image but '
                            f'preflight generation failed: {exc}')
                if abort_check is not None and abort_check():
                    aborted = True
                while (not aborted and len(in_flight) < worker_limit
                       and _submit_one(pool)):
                    pass

    records.sort(key=lambda r: int(r.get('page_index') or 0))
    from lib.json_store import write_json_atomic
    write_json_atomic(os.path.join(deck_dir, MANIFEST_NAME), records)
    retained_paths = {str(record.get('path') or '') for record in records}
    for record in cached:
        stale = str(record.get('path') or '')
        if (stale not in retained_paths and _GENERATED_PATH_RE.fullmatch(stale)):
            try:
                os.unlink(os.path.join(deck_dir, *stale.split('/')))
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug('[SlideAssetPreflight] stale cleanup failed: %s',
                             exc)
    by_page: dict[int, list[str]] = {}
    for rec in records:
        index = int(rec['page_index'])
        by_page.setdefault(index, []).append(str(rec['path']))
    for index, page in enumerate(pages):
        if isinstance(page, dict):
            page['resolved_assets'] = by_page.get(index, [])
    return {'by_page': by_page, 'records': records, 'findings': findings,
            'aborted': aborted}
