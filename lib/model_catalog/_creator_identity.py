"""Creator identity for provider-decorated model ids.

API service providers (Amazon Bedrock, Azure, Google Vertex, NVIDIA NIM,
Alibaba DashScope, …) re-publish other creators' models under their own id
decoration: regional routing prefixes (``au.``/``eu.``/``global.``…), creator
namespaces (``anthropic.``, ``deepseek-ai/``), Bedrock version suffixes
(``-v1:0``) and Vertex snapshot markers (``@default``/``@20250805``). The
catalog and the discovery directory group models by *creator* family, never
by the API provider that relays them, so this module is the single place
that strips relay decoration and attributes an id to its creator.

Attribution is deterministic and offline: decoration is stripped first, then
the bare id is matched against the migration-owned family taxonomy. Anything
that still does not resolve stays unattributed — a wrong family is worse than
none.
"""

from __future__ import annotations

import re

from lib.model_catalog._creator_families import (
    CREATOR_PROVIDERS,
    FAMILY_BRAND_KEYS,
    FAMILY_ID_PREFIXES,
)


def _normalize_name(value: object) -> str:
    if not isinstance(value, str):
        return ''
    return re.sub(r'[^a-z0-9]', '', value.lower())

# Bedrock-style regional routing prefixes. Only stripped when immediately
# followed by a creator namespace, so a hypothetical first-party id starting
# with ``us.``/``global.`` is never mangled.
_REGIONAL_PREFIX = re.compile(
    r'^(?:au|ap|ca|eu|jp|sa|us|global)\.(?=[a-z][a-z0-9-]*[./])')

# Creator namespaces that relay providers prepend (``anthropic.`` Bedrock
# style) or use as publisher path segments (``deepseek-ai/`` Hugging-Face
# style on NVIDIA NIM / DashScope).
_NAMESPACE_PREFIX = re.compile(
    r'^(?:anthropic|openai|xai|meta|google|deepseek(?:-ai)?|mistral(?:ai)?'
    r'|cohere|ai21|amazon|qwen|minimax(?:ai)?|moonshot(?:ai)?|nvidia'
    r'|microsoft|bytedance|tencent|zhipu|zai(?:-org)?|alibaba|baai|ibm'
    r'|snowflake|liquid|upstage|stepfun(?:-ai)?|allenai'
    r'|nous(?:research)?|teknium|stability(?:ai)?)[./]')


# A creator namespace is attribution evidence even when removing it leaves a
# generic product id (``meta/esmfold``, ``nvidia/usdcode``,
# ``deepseek.r1-v1:0``). Keep aliases explicit so relay/provider namespaces
# that are not creators never silently become brands.
_NAMESPACE_FAMILIES: dict[str, str] = {
    'anthropic': 'anthropic', 'openai': 'openai', 'xai': 'xai',
    'meta': 'meta', 'google': 'google', 'deepseek': 'deepseek',
    'deepseek-ai': 'deepseek', 'mistral': 'mistral',
    'mistralai': 'mistral', 'cohere': 'cohere', 'amazon': 'amazon',
    'qwen': 'alibaba', 'minimax': 'minimax', 'minimaxai': 'minimax',
    'moonshot': 'moonshot', 'moonshotai': 'moonshot',
    'nvidia': 'nvidia', 'microsoft': 'microsoft',
    'bytedance': 'bytedance', 'tencent': 'tencent', 'zhipu': 'zhipu',
    'zai': 'zhipu', 'zai-org': 'zhipu', 'alibaba': 'alibaba',
    'upstage': 'upstage', 'stepfun': 'stepfun', 'stepfun-ai': 'stepfun',
}
_NAMESPACE_TOKEN = re.compile(r'^([a-z][a-z0-9-]*)[./]')
# Bedrock revision markers: ``-v1:0`` is unambiguous and always stripped.
_VERSION_REVISION_SUFFIX = re.compile(r'[-.]v\d+:\d+$')

# Bare ``-v1``/``.v2`` is also a Bedrock marker, but first-party ids legitimately
# end this way (``deepseek-v3``), so it is stripped only once regional/namespace
# decoration proved the id is a relay SKU.
_VERSION_SUFFIX = re.compile(r'[-.]v\d+$')

# Vertex snapshot markers: ``@default``, ``@20250805``.
_VERTEX_SUFFIX = re.compile(r'@(?:default|\d{8})$')


# Aggregator channel suffixes name the access product, not a distinct model.
# Keep the set explicit: broad suffix stripping would collapse real variants
# such as ``-turbo``, ``-preview`` or ``-instruct``.
_RELAY_CHANNEL_SUFFIX = re.compile(r'-(?:maas)$', re.IGNORECASE)
# Trailing YYYYMMDD snapshot date on an already-normalized key
# (``claudeopus4520251101`` → ``claudeopus45``). Six-digit YYMMDD and
# four-digit version months (``-2507``) are deliberately kept: providers use
# them to distinguish model versions.
_SNAPSHOT_DATE_SUFFIX = re.compile(r'20\d{6}$')

# Bedrock-style regional display names: ``Claude Opus 5 (Global)``.
_REGION_NAME_TAG = re.compile(
    r'\s*\((?:Global|AU|AP|CA|EU|JP|SA|US)\)\s*$', re.IGNORECASE)


def strip_region_display_tag(name: object) -> str:
    """Drop a trailing regional routing tag from a relay display name."""
    if not isinstance(name, str):
        return ''
    return _REGION_NAME_TAG.sub('', name).strip()


def strip_routing_decoration(model_id: object) -> str:
    """Return *model_id* minus relay routing decoration.

    Regional prefixes, creator namespaces, Bedrock version markers and Vertex
    snapshot markers are stripped iteratively (``au.anthropic.claude-x-v1:0``
    → ``claude-x``). Snapshot *dates* inside the bare id are kept — they are
    part of the creator's own API id (``claude-haiku-4-5-20251001``).
    """
    if not isinstance(model_id, str):
        return ''
    text = model_id.strip()
    decorated = False
    while True:
        stripped = _REGIONAL_PREFIX.sub('', text, count=1)
        stripped = _NAMESPACE_PREFIX.sub('', stripped, count=1)
        decorated = decorated or stripped != text
        stripped = _VERSION_REVISION_SUFFIX.sub('', stripped)
        if decorated:
            stripped = _VERSION_SUFFIX.sub('', stripped)
        stripped = _VERTEX_SUFFIX.sub('', stripped)
        stripped = _RELAY_CHANNEL_SUFFIX.sub('', stripped)
        if stripped == text:
            return text
        text = stripped


def has_regional_prefix(model_id: object) -> bool:
    """Whether *model_id* carries a Bedrock-style regional routing prefix."""
    return isinstance(model_id, str) \
        and bool(_REGIONAL_PREFIX.match(model_id.strip()))


def canonical_key(model_id: object) -> str:
    """Creator-identity dedupe key for one model id.

    Normalized (lowercase alnum) bare id with any trailing YYYYMMDD snapshot
    date removed, so the creator's dated id, the relay's undated alias and a
    regional variant all share one key
    (``claude-opus-4-5-20251101`` ≡ ``claude-opus-4-5`` ≡
    ``global.anthropic.claude-opus-4-5``).
    """
    key = _normalize_name(strip_routing_decoration(model_id))
    return _SNAPSHOT_DATE_SUFFIX.sub('', key)


def creator_family(model_id: object) -> str | None:
    """Attribute one (possibly decorated) model id to its creator family.

    Detection uses the bare id only — never ``brand``, which for relay-sourced
    rows carries the *provider's* family and is exactly what this function
    exists to correct.
    """
    if not isinstance(model_id, str):
        return None
    raw = model_id.strip().lower()
    if not raw:
        return None
    raw = _REGIONAL_PREFIX.sub('', raw, count=1)
    namespace = _NAMESPACE_TOKEN.match(raw)
    if namespace and namespace.group(1) in _NAMESPACE_FAMILIES:
        return _NAMESPACE_FAMILIES[namespace.group(1)]
    text = strip_routing_decoration(model_id).lower()
    if not text:
        return None
    for family, prefixes in FAMILY_ID_PREFIXES.items():
        if any(text.startswith(prefix) for prefix in prefixes):
            return family
    for family, keys in FAMILY_BRAND_KEYS.items():
        if any(key in text for key in keys):
            return family
    return None


def brand_family(brand: object) -> str | None:
    """Resolve an explicit model ``brand`` string to a creator family."""
    if not isinstance(brand, str):
        return None
    text = brand.strip().lower()
    if not text:
        return None
    for family, keys in FAMILY_BRAND_KEYS.items():
        if any(key in text for key in keys):
            return family
    return None


def is_creator_row(provider_id: object, family: str | None) -> bool:
    """Whether a models.dev row from *provider_id* is a creator's own row.

    Creator providers publish their own models under the provider section
    listed in the migration taxonomy (``anthropic`` for Claude,
    ``azure`` for Phi/GPT, ``nova``+``amazon-bedrock`` for Nova). A Bedrock
    row for ``anthropic.claude-*`` is a relay row; a Bedrock row for
    ``amazon.nova-*`` is a creator row.
    """
    if not family:
        return False
    providers = CREATOR_PROVIDERS.get(family) or ()
    return str(provider_id or '') in providers


__all__ = [
    'brand_family',
    'canonical_key',
    'creator_family',
    'has_regional_prefix',
    'is_creator_row',
    'strip_region_display_tag',
    'strip_routing_decoration',
]
