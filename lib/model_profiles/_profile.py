"""Pure model-profile inference with explicit evidence provenance."""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)


# Quality is ordinal only within this selection subsystem. It is not a
# benchmark score and must never be presented as a measured percentage.
_QUALITY = {
    'unknown': 0,
    'light': 100,
    'standard': 200,
    'heavy': 300,
    'frontier': 400,
}

# These relations come from provider catalogue descriptions or project
# reference rows that name the product position explicitly. Unknown/new names
# intentionally receive no inferred rank: weak evidence may create a profile,
# but it may not auto-promote a model into a stronger swarm role.
_DECLARED: tuple[tuple[re.Pattern, dict], ...] = (
    (re.compile(r'gpt[-_.]?5[.-]?6[-_.]?sol(?:$|[-_.])', re.I), {
        'family': 'gpt-5.6', 'quality': 'frontier',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic'),
        'evidence': 'provider_catalog', 'confidence': 1.0,
    }),
    (re.compile(r'gpt[-_.]?5[.-]?6[-_.]?terra(?:$|[-_.])', re.I), {
        'family': 'gpt-5.6', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'provider_catalog', 'confidence': 1.0,
    }),
    (re.compile(r'gpt[-_.]?5[.-]?6[-_.]?luna(?:$|[-_.])', re.I), {
        'family': 'gpt-5.6', 'quality': 'light',
        'roles': ('writer', 'researcher', 'analyst', 'browser', 'general'),
        'evidence': 'provider_catalog', 'confidence': 1.0,
    }),
    (re.compile(r'deepseek[-_.]?v4[-_.]?pro(?:$|[-_.])', re.I), {
        'family': 'deepseek-v4', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.95,
    }),
    (re.compile(r'deepseek[-_.]?v4[-_.]?flash(?:$|[-_.])', re.I), {
        'family': 'deepseek-v4', 'quality': 'light',
        'roles': ('writer', 'researcher', 'analyst', 'browser', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.95,
    }),
    (re.compile(r'claude[-_.]?opus[-_.]?5(?:$|[-_.])', re.I), {
        'family': 'claude-5', 'quality': 'frontier',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic'),
        'evidence': 'vendor_declaration', 'confidence': 0.95,
    }),
    (re.compile(r'(?:claude[-_.]?)?fable[-_.]?5(?:$|[-_.])', re.I), {
        'family': 'claude-5', 'quality': 'heavy',
        'roles': ('writer', 'researcher', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.95,
    }),
    (re.compile(r'claude[-_.]?sonnet[-_.]?5(?:$|[-_.])', re.I), {
        'family': 'claude-5', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.95,
    }),
    (re.compile(r'claude[-_.]?haiku[-_.]?(?:4|5)', re.I), {
        'family': 'claude', 'quality': 'light',
        'roles': ('writer', 'researcher', 'analyst', 'browser', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.95,
    }),
    (re.compile(r'gemini[-_.]?3(?:[.-]\d+)?[-_.]?pro', re.I), {
        'family': 'gemini-3', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.9,
    }),
    (re.compile(r'gemini[-_.]?3(?:[.-]\d+)?[-_.]?flash[-_.]?lite', re.I), {
        'family': 'gemini-3', 'quality': 'light',
        'roles': ('writer', 'researcher', 'analyst', 'browser', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.9,
    }),
    (re.compile(r'gemini[-_.]?3(?:[.-]\d+)?[-_.]?flash', re.I), {
        'family': 'gemini-3', 'quality': 'standard',
        'roles': ('writer', 'researcher', 'analyst', 'browser', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.9,
    }),
    (re.compile(r'minimax[-_.]?m3(?:$|[-_.])', re.I), {
        'family': 'minimax-m3', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.9,
    }),
    (re.compile(r'kimi[-_.]?k3(?:$|[-_.])', re.I), {
        'family': 'kimi-k3', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.9,
    }),
    (re.compile(r'glm[-_.]?5(?:[.-]\d+)?(?:$|[-_.])', re.I), {
        'family': 'glm-5', 'quality': 'heavy',
        'roles': ('planner', 'coder', 'reviewer', 'worker', 'critic',
                  'researcher', 'analyst', 'general'),
        'evidence': 'vendor_declaration', 'confidence': 0.85,
    }),
)


def infer_model_family(model_id: str) -> str:
    """Return a stable family name without treating it as quality evidence."""
    mid = (model_id or '').lower()
    for pattern, declared in _DECLARED:
        if pattern.search(mid):
            return declared['family']
    for family, token in (
        ('claude', 'claude'), ('claude', 'fable'), ('gpt', 'gpt'),
        ('deepseek', 'deepseek'), ('gemini', 'gemini'), ('qwen', 'qwen'),
        ('kimi', 'kimi'), ('minimax', 'minimax'), ('glm', 'glm'),
        ('doubao', 'doubao'), ('longcat', 'longcat'),
    ):
        if token in mid:
            return family
    return ''


def _explicit_profile(model_entry: dict | None) -> dict | None:
    raw = (model_entry or {}).get('capability_profile')
    if not isinstance(raw, dict):
        return None
    quality = str(raw.get('quality') or 'unknown').lower()
    if quality not in _QUALITY:
        logger.warning('[ModelProfile] invalid explicit quality=%r for %s',
                       quality, (model_entry or {}).get('model_id', ''))
        quality = 'unknown'
    try:
        confidence = max(0.0, min(1.0, float(raw.get('confidence') or 0)))
    except (TypeError, ValueError) as exc:
        logger.debug('[ModelProfile] invalid explicit confidence: %s', exc)
        confidence = 0.0
    return {
        'family': str(raw.get('family') or ''),
        'quality': quality,
        'qualityScore': _QUALITY[quality],
        'roles': sorted({str(x) for x in (raw.get('roles') or ()) if x}),
        'evidence': str(raw.get('evidence') or 'operator'),
        'confidence': confidence,
        'autoSelectable': quality != 'unknown' and confidence >= 0.8,
    }


def build_model_profile(model_id: str, *, provider_id: str = '',
                        model_entry: dict | None = None) -> dict:
    """Build a model profile; weak evidence never grants auto-selection."""
    explicit = _explicit_profile(model_entry)
    if explicit is not None:
        profile = explicit
    else:
        declared = next((row for pattern, row in _DECLARED
                         if pattern.search(model_id or '')), None)
        if declared:
            profile = {
                'family': declared['family'],
                'quality': declared['quality'],
                'qualityScore': _QUALITY[declared['quality']],
                'roles': sorted(declared['roles']),
                'evidence': declared['evidence'],
                'confidence': declared['confidence'],
                'autoSelectable': declared['confidence'] >= 0.8,
            }
        else:
            profile = {
                'family': infer_model_family(model_id),
                'quality': 'unknown',
                'qualityScore': _QUALITY['unknown'],
                'roles': [],
                'evidence': 'name_only' if model_id else 'unknown',
                'confidence': 0.0,
                'autoSelectable': False,
            }
    profile = dict(profile)
    profile.update(modelId=model_id or '', providerId=provider_id or '')
    return profile


__all__ = ['build_model_profile', 'infer_model_family']
