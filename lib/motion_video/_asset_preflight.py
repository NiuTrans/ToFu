"""Asset-first preflight for motion scenes.

Required subject/diagram briefs used to be left as optional model tool calls.
Real jobs then spent ~90k tokens per scene while generating zero files.  This
module resolves those briefs before composition authoring and hands the author
verified scene-local paths.  A small manifest makes retries and crash resumes
zero-spend.

Generation is best-effort: provider outages are recorded as findings and the
existing author/fallback ladder remains available.  Invented paths are never
returned.
"""

from __future__ import annotations

import json
import hashlib
import os

from lib.log import get_logger
from lib.production.contracts import (
    normalise_asset_briefs,
    normalise_media_queries,
)

logger = get_logger(__name__)

__all__ = [
    'collect_media_attribution', 'prepare_scene_assets', 'PREFLIGHT_MANIFEST',
]

PREFLIGHT_MANIFEST = '.tofu-assets.json'
_REQUIRED_ROLES = ('subject', 'diagram')


def _load(scene_dir: str) -> list[dict]:
    path = os.path.join(scene_dir, PREFLIGHT_MANIFEST)
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug('[MotionAssetPreflight] manifest unavailable %s: %s',
                     path, e)
        return []
    return raw if isinstance(raw, list) else []


def _save(scene_dir: str, records: list[dict]) -> None:
    from lib.json_store import write_json_atomic

    write_json_atomic(os.path.join(scene_dir, PREFLIGHT_MANIFEST), records)


def _usable(scene_dir: str, record: dict) -> bool:
    rel = str(record.get('path') or '')
    return bool(rel and not os.path.isabs(rel) and '..' not in rel.split('/')
                and os.path.isfile(os.path.join(scene_dir, rel)))


def prepare_scene_assets(scene: dict, scene_dir: str, *,
                         max_assets: int = 2, max_media: int = 1) -> dict:
    """Resolve required briefs and attach ``scene['resolved_assets']``.

    Returns ``{'resolved': [...], 'findings': [...]}``.  One record is
    ``{'role', 'prompt', 'path'}``; every returned path exists on disk.
    """
    all_briefs = normalise_asset_briefs(
        scene.get('assets'),
        allowed_roles=('subject', 'diagram', 'background'),
        fallback_role='background', max_items=32,
        log_prefix='[MotionAssetPreflight]')
    briefs = [brief for brief in all_briefs
              if brief['role'] in _REQUIRED_ROLES][:max(0, max_assets)]
    cached = _load(scene_dir)
    resolved: list[dict] = []
    findings: list[str] = []
    for brief in briefs:
        role = str(brief.get('role') or '').strip().lower()
        prompt = str(brief.get('prompt') or '').strip()
        semantic_target = str(brief.get('semantic_target') or '').strip()
        hit = next((r for r in cached
                    if r.get('role') == role and r.get('prompt') == prompt
                    and _usable(scene_dir, r)), None)
        if hit is not None:
            record = dict(hit)
            if semantic_target:
                record['semantic_target'] = semantic_target
            resolved.append(record)
            continue
        try:
            # Reuse the author's existing provider/cache/materialisation path;
            # this is orchestration, not a second image-generation chassis.
            from lib.motion_video._scene_author import _generate_scene_asset
            if role == 'diagram':
                rel = _generate_scene_asset(prompt, scene_dir,
                                            width=1280, height=720)
            else:
                rel = _generate_scene_asset(prompt, scene_dir,
                                            width=1024, height=1024)
            record = {'role': role, 'prompt': prompt, 'path': rel}
            if semantic_target:
                record['semantic_target'] = semantic_target
            if not _usable(scene_dir, record):
                raise RuntimeError(f'generated path is not usable: {rel!r}')
            resolved.append(record)
        except Exception as exc:
            logger.warning('[AssetPreflight] %s %s generation failed: %s',
                           scene.get('id'), role, exc)
            findings.append(
                f'required {role} asset could not be prepared before '
                f'authoring: {exc}')
    media_requests = normalise_media_queries(
        scene.get('media_queries'), max_items=max(0, max_media))
    for request in media_requests:
        query = str(request.get('query') or '')
        requested_kind = str(request.get('kind') or 'image')
        semantic_target = str(request.get('semantic_target') or '')
        hit = next((record for record in cached
                    if record.get('requested_kind') == requested_kind
                    and record.get('query') == query
                    and _usable(scene_dir, record)), None)
        if hit is not None:
            record = dict(hit)
            record['semantic_target'] = semantic_target
            resolved.append(record)
            continue
        try:
            from lib.production.stock_media import resolve_stock_media
            result = resolve_stock_media(request)
            if not result.get('ok'):
                raise RuntimeError(result.get('reason') or
                                   'stock provider returned no media')
            data = result.pop('data', b'')
            suffix = str(result.pop('suffix', '') or '')
            from lib.motion_video._assets import scene_asset_dir
            name = f'stock_{hashlib.sha256(data).hexdigest()[:20]}{suffix}'
            asset_dir = scene_asset_dir(scene_dir)
            absolute_path = os.path.join(asset_dir, name)
            from lib.json_store import write_bytes_atomic
            if not os.path.isfile(absolute_path):
                write_bytes_atomic(absolute_path, data)
            record = {
                **result,
                'path': f'assets/{name}',
                'role': f'stock-{result.get("media_kind") or requested_kind}',
                'prompt': query,
                'semantic_target': semantic_target,
            }
            record.pop('ok', None)
            if not _usable(scene_dir, record):
                raise RuntimeError('stock provider returned an unusable path')
            resolved.append(record)
        except Exception as exc:
            logger.warning('[AssetPreflight] %s stock %s failed: %s',
                           scene.get('id'), requested_kind, exc)
            findings.append(
                f'required {requested_kind} media could not be prepared '
                f'before authoring: {exc}')
    if resolved:
        # Keep only unique current records.  The manifest is the resume cache,
        # not an append-only history of abandoned prompts.
        unique = {(r['role'], r['prompt']): r for r in resolved}
        resolved = list(unique.values())
        try:
            _save(scene_dir, resolved)
        except Exception as exc:
            logger.warning('[AssetPreflight] cannot persist %s manifest: %s',
                           scene.get('id'), exc)
    scene['resolved_assets'] = resolved
    return {'resolved': resolved, 'findings': findings}


def collect_media_attribution(workdir: str, *, max_records: int = 64) -> dict:
    """Write the bounded public attribution ledger for stock assets in a film."""
    scenes_dir = os.path.join(workdir, 'scenes')
    records: list[dict] = []
    try:
        scene_names = sorted(os.listdir(scenes_dir))[:max(0, max_records)]
    except OSError:
        scene_names = []
    for scene_name in scene_names:
        scene_dir = os.path.join(scenes_dir, scene_name)
        for record in _load(scene_dir):
            if not isinstance(record, dict) or not record.get('provider'):
                continue
            records.append({
                'scene_id': scene_name,
                'provider': str(record.get('provider') or ''),
                'provider_url': str(record.get('provider_url') or ''),
                'creator': str(record.get('creator') or ''),
                'creator_url': str(record.get('creator_url') or ''),
                'media_url': str(record.get('page_url') or ''),
                'query': str(record.get('query') or ''),
                'license_hint': str(record.get('license_hint') or ''),
            })
            if len(records) >= max_records:
                break
        if len(records) >= max_records:
            break
    if not records:
        return {'records': 0, 'json_path': '', 'text_path': ''}
    from lib.json_store import write_json_atomic, write_text_atomic
    json_path = os.path.join(workdir, 'media_attribution.json')
    text_path = os.path.join(workdir, 'media_attribution.txt')
    write_json_atomic(json_path, records)
    lines = ['Media provided by Pexels — https://www.pexels.com']
    for record in records:
        creator = record['creator'] or 'Pexels contributor'
        lines.append(
            f'{record["scene_id"]}: {creator} — '
            f'{record["media_url"] or record["provider_url"]}')
    write_text_atomic(text_path, '\n'.join(lines) + '\n')
    return {'records': len(records), 'json_path': json_path,
            'text_path': text_path}
