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
import os

from lib.log import get_logger
from lib.production.contracts import normalise_asset_briefs

logger = get_logger(__name__)

__all__ = ['prepare_scene_assets', 'PREFLIGHT_MANIFEST']

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
                         max_assets: int = 2) -> dict:
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
