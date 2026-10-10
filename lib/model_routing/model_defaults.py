"""Install exact, declared Model defaults; provider facts never become defaults.

Entry points: apply_model_defaults and official_model_directory. These run at
catalog/install and authority normalization boundaries. Dispatch consumes the
normalized owner Model aggregate; there is no request-time name guessing.
Package data is bounded and cached by file revision, with one cached revision.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import json
from pathlib import Path

from .domain import ModelRef
from .inference import profile_from_metadata, resolve_inference_profile, validate_inference_profile

_PATH = Path(__file__).parents[1] / 'model_info' / 'model_inference_defaults.json'


@lru_cache(maxsize=1)
def _profiles(mtime_ns: int, size: int) -> dict:
    if size > 262144:
        raise ValueError('model inference defaults exceed 256 KiB')
    data = json.loads(_PATH.read_text(encoding='utf-8'))
    if data.get('contract_version') != 'tofu.model-inference-defaults/v1':
        raise ValueError('invalid model inference defaults version')
    groups = data.get('groups', [])
    if len(groups) > 128:
        raise ValueError('too many model inference default groups')
    result = {}
    for group in groups:
        profile = validate_inference_profile(group['inference_profile'])
        previous = group.get('previous_profiles', [])
        if not isinstance(previous, list) or len(previous) > 8:
            raise ValueError('too many superseded model inference profiles')
        previous = tuple(validate_inference_profile(p) for p in previous)
        for model_id in group['model_ids']:
            ref = ModelRef.from_value({'creator_id': group['creator_id'], 'model_id': model_id})
            if ref in result or len(result) >= 2048:
                raise ValueError('duplicate or excessive model inference default identities')
            result[ref] = (profile, previous)
    return result


def has_exact_model_defaults(model: dict) -> bool:
    """Whether maintained knowledge names this exact Creator/Model identity."""
    stat = _PATH.stat()
    return ModelRef.from_value(model) in _profiles(stat.st_mtime_ns, stat.st_size)


def apply_model_defaults(model: dict, *, baseline: dict | None = None) -> bool:
    """Fill missing declared fields without overriding owner edits."""
    stat = _PATH.stat()
    defaults, previous = _profiles(stat.st_mtime_ns, stat.st_size).get(
        ModelRef.from_value(model), ({}, ()))
    if baseline is not None:
        defaults = resolve_inference_profile(
            model={'inference_profile': defaults}, offering=baseline)
    if not defaults:
        return False
    current = model.get('inference_profile') or {}
    # Only exact prior shipped defaults may be replaced. An owner edit,
    # including source=configured or any different field, always wins.
    if current and current in previous:
        current = {}
    resolved = resolve_inference_profile(
        model={'inference_profile': defaults}, offering={'inference_profile': current})
    if resolved == current:
        return False
    model['inference_profile'] = resolved
    return True


def official_model_directory(templates: list[dict]) -> list[dict]:
    """Compile developer-owned catalog seeds once for all provider templates.

    Relay recipes cannot replace developer Model facts. Their declarations
    remain Offering/Connection/Deployment overrides in the importing bundle.
    """
    from lib.model_catalog._creator_identity import brand_family
    from lib.provider_template_recipes import provider_from_template
    from .migration import plan_legacy_migration

    rows = {}
    for template in templates:
        if template.get('category') != 'official':
            continue
        creator = brand_family(template.get('brand') or template['key'])
        provider = provider_from_template(template, template['key'])
        plan = plan_legacy_migration({'providers': [provider]})
        recipes = {r['model_id']: r for r in template['offering_recipes']}
        for source in plan.document['models']:
            if source['creator_id'] != creator:
                continue
            model = copy.deepcopy(source)
            recipe = recipes.get(model['model_id'], {})
            if not recipe.get('context_window'):
                from lib.model_info._context import context_profile
                window = context_profile(model['model_id']).get('window')
                if isinstance(window, int) and window > 0:
                    model['context_window'] = window
            declared = profile_from_metadata(recipe, source='official')
            if declared:
                model['inference_profile'] = declared
            apply_model_defaults(model)
            rows.setdefault((model['creator_id'], model['model_id']), model)
    return list(rows.values())
