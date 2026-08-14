"""Asset-first image generation for slide storyboards.

The old assets stage only downloaded URLs that a caller had already supplied;
with the normal topic entry point it therefore did nothing.  This preflight
materialises the storyboard's explicit ``asset_prompt`` obligations before
page authoring, persists a resume manifest and returns deck-relative paths the
page author can actually use.
"""

from __future__ import annotations

import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.json_store import write_bytes_atomic
from lib.log import get_logger
from lib.production.contracts import normalise_asset_briefs

logger = get_logger(__name__)

__all__ = ['prepare_deck_assets', 'MANIFEST_NAME']

MANIFEST_NAME = '.tofu-slide-assets.json'


def _load(deck_dir: str) -> list[dict]:
    path = os.path.join(deck_dir, MANIFEST_NAME)
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[SlideAssetPreflight] manifest unavailable %s: %s',
                     path, e)
        return []
    return raw if isinstance(raw, list) else []


def _usable(deck_dir: str, rec: dict) -> bool:
    rel = str(rec.get('path') or '')
    return bool(rel and not os.path.isabs(rel) and '..' not in rel.split('/')
                and os.path.isfile(os.path.join(deck_dir, rel)))


def _write_bytes(path: str, data: bytes) -> None:
    write_bytes_atomic(path, data)


def _generate(index: int, prompt: str, semantic_target: str,
              deck_dir: str) -> dict:
    from lib.image_gen import generate_image

    result = generate_image(prompt, aspect_ratio='16:9', resolution='2K')
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or 'image generation failed')
    raw = base64.b64decode(result['image_b64'])
    mime = str(result.get('mime_type') or 'image/png').lower()
    ext = {'image/png': '.png', 'image/jpeg': '.jpg',
           'image/webp': '.webp'}.get(mime, '.png')
    rel = f'media/generated_{index + 1:02d}{ext}'
    _write_bytes(os.path.join(deck_dir, rel), raw)
    record = {'page_index': index, 'prompt': prompt, 'path': rel,
              'bytes': len(raw)}
    if semantic_target:
        record['semantic_target'] = semantic_target
    return record


def prepare_deck_assets(outline: dict, deck_dir: str, *, max_assets: int = 6,
                        parallel: int = 2) -> dict:
    """Generate storyboard assets concurrently and attach them to pages.

    Returns ``{'by_page': {index: [path]}, 'records': [...], 'findings': [...]}``.
    Provider failures are findings, never exceptions that kill the deck.
    """
    pages = outline.get('pages') or []
    wanted: list[tuple[int, str, str]] = []
    for index, page in enumerate(pages):
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
        if len(wanted) >= max_assets:
            break
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
    if missing:
        with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(missing)))) as pool:
            futures = {
                pool.submit(_generate, i, prompt, semantic_target, deck_dir):
                (i, prompt)
                for i, prompt, semantic_target in missing
            }
            for future in as_completed(futures):
                index, _prompt = futures[future]
                try:
                    records.append(future.result())
                except Exception as exc:
                    logger.warning('[Slides:AssetPreflight] page %d failed: %s',
                                   index + 1, exc)
                    findings.append(
                        f'page {index + 1} required an editorial image but '
                        f'preflight generation failed: {exc}')

    records.sort(key=lambda r: int(r.get('page_index') or 0))
    if records:
        from lib.json_store import write_json_atomic
        write_json_atomic(os.path.join(deck_dir, MANIFEST_NAME), records)
    by_page: dict[int, list[str]] = {}
    for rec in records:
        index = int(rec['page_index'])
        by_page.setdefault(index, []).append(str(rec['path']))
    for index, page in enumerate(pages):
        if isinstance(page, dict):
            page['resolved_assets'] = by_page.get(index, [])
    return {'by_page': by_page, 'records': records, 'findings': findings}
