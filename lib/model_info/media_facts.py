"""lib/model_info/media_facts.py — media Model facts: shape checks and maintained catalog.

Responsibility: validate the two media fact objects a Model may carry —
``media_pricing`` (official prices in the media units of
:data:`capability_taxonomy.MEDIA_PRICE_UNITS`) and ``media_profile``
(documented generation/recognition constraints) — and read the
developer-maintained official facts in ``media_model_facts.json``.

Entry points: validate_media_pricing, validate_media_profile,
media_pricing_unit_issues, maintained_media_facts, effective_media_profile.
Dependencies: the generated model-routing contract for field shapes (the
authority is contracts/model_routing_v2.schema.json, ``mediaPricing`` and
``mediaProfile``) and capability_taxonomy for kinds and units. No network,
credentials or persistence; the catalog is read once per process.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from lib.log import get_logger
from lib.model_info.capability_taxonomy import (
    MODEL_KINDS,
    is_media_kind,
    media_price_units_for,
)

logger = get_logger(__name__)

CATALOG_CONTRACT = 'tofu.media-model-facts/v1'
_CATALOG = Path(__file__).with_name('media_model_facts.json')
_ENTRY_FIELDS = frozenset({
    'creator_id', 'model_id', 'kind', 'media_pricing', 'media_profile',
    'checked_at', 'source_urls', 'notes'})
_KIND_CAPABILITIES = {
    str(kind['id']): tuple(kind['capabilities'])  # type: ignore[arg-type]
    for kind in MODEL_KINDS}


class MediaFactError(ValueError):
    """A media fact object violates its contract; ``field`` names it."""

    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


@lru_cache(maxsize=2)
def _validator(definition: str):
    from jsonschema import Draft202012Validator
    from lib.generated_api_v1_model_routing import EXTERNAL_DEFS
    return Draft202012Validator({'$ref': f'#/$defs/{definition}', '$defs': EXTERNAL_DEFS})


def _all_finite(node: Any) -> bool:
    if isinstance(node, float):
        return math.isfinite(node)
    if isinstance(node, Mapping):
        return all(_all_finite(child) for child in node.values())
    if isinstance(node, list):
        return all(_all_finite(child) for child in node)
    return True


def _validated(value: Any, definition: str, field: str) -> dict:
    # jsonschema messages echo the invalid value; facts are inspectable
    # routing metadata, so the error names only the field.
    if (not _all_finite(value)
            or next(_validator(definition).iter_errors(value), None) is not None):
        raise MediaFactError(f'{field}: invalid {definition}', field=field)
    return copy.deepcopy(value)


def validate_media_pricing(value: Any, *, field: str = 'media_pricing') -> dict:
    """Return a detached copy of a contract-valid ``mediaPricing`` object."""
    return _validated(value, 'mediaPricing', field)


def validate_media_profile(value: Any, *, field: str = 'media_profile') -> dict:
    """Return a detached copy of a contract-valid ``mediaProfile`` object."""
    return _validated(value, 'mediaProfile', field)


def media_pricing_unit_issues(pricing: Mapping | None,
                              capabilities: Iterable[str] | None) -> list[str]:
    """Units of *pricing* that no kind of a model with *capabilities* bills in.

    An image model may price per image, a recognizer per minute; a chat
    model declares no media units at all, so every rate it carries is stray.
    """
    if not pricing:
        return []
    allowed = media_price_units_for(capabilities)
    return sorted({str(rate['unit']) for rate in pricing['rates']} - allowed)


def _admit_entry(row: Any, index: int) -> tuple[tuple[str, str], dict]:
    field = f'models[{index}]'
    if not isinstance(row, dict) or set(row) - _ENTRY_FIELDS:
        raise MediaFactError(f'{field}: unknown or malformed entry', field=field)
    creator_id, model_id, kind = row.get('creator_id'), row.get('model_id'), row.get('kind')
    if not all(isinstance(value, str) and value.strip() == value and value
               for value in (creator_id, model_id, kind)):
        raise MediaFactError(f'{field}: creator_id, model_id and kind are required', field=field)
    if not is_media_kind(kind):
        raise MediaFactError(f'{field}.kind must be a media kind', field=f'{field}.kind')
    entry: dict[str, Any] = {'kind': kind}
    if 'media_pricing' in row:
        pricing = validate_media_pricing(row['media_pricing'], field=f'{field}.media_pricing')
        stray = media_pricing_unit_issues(pricing, _KIND_CAPABILITIES[kind])
        if stray:
            raise MediaFactError(
                f'{field}.media_pricing units {stray} are not {kind} units',
                field=f'{field}.media_pricing')
        entry['media_pricing'] = pricing
    if 'media_profile' in row:
        entry['media_profile'] = validate_media_profile(
            row['media_profile'], field=f'{field}.media_profile')
    if len(entry) == 1:
        raise MediaFactError(f'{field}: declares no media fact', field=field)
    checked = row.get('checked_at')
    urls = row.get('source_urls')
    try:
        if not isinstance(checked, str) or len(checked) != 10:
            raise ValueError
        date.fromisoformat(checked)
        if (not isinstance(urls, list) or not 1 <= len(urls) <= 8
                or len(set(urls)) != len(urls)):
            raise ValueError
        for url in urls:
            parsed = urlsplit(url) if isinstance(url, str) else None
            if not parsed or parsed.scheme != 'https' or not parsed.hostname:
                raise ValueError
    except ValueError:
        raise MediaFactError(
            f'{field}: requires checked_at and 1-8 unique HTTPS source_urls',
            field=field) from None
    if 'notes' in row and (not isinstance(row['notes'], str) or len(row['notes']) > 512):
        raise MediaFactError(f'{field}.notes must be a short string', field=f'{field}.notes')
    return (creator_id, model_id), entry


def admit_media_fact_catalog(raw: Any) -> dict[tuple[str, str], dict]:
    """Validate a maintained catalog document; index facts by Model identity."""
    if not isinstance(raw, dict) or raw.get('contract_version') != CATALOG_CONTRACT:
        raise MediaFactError(f'contract_version must be {CATALOG_CONTRACT!r}',
                             field='contract_version')
    rows = raw.get('models')
    if not isinstance(rows, list):
        raise MediaFactError('models must be a list', field='models')
    catalog: dict[tuple[str, str], dict] = {}
    for index, row in enumerate(rows):
        key, entry = _admit_entry(row, index)
        if key in catalog:
            raise MediaFactError(f'duplicate media facts for {key[0]}/{key[1]}',
                                 field=f'models[{index}]')
        catalog[key] = entry
    return catalog


@lru_cache(maxsize=1)
def _catalog() -> dict[tuple[str, str], dict]:
    try:
        return admit_media_fact_catalog(json.loads(_CATALOG.read_text(encoding='utf-8')))
    except (OSError, ValueError) as exc:
        # The catalog is developer-maintained and pinned by tests; a broken
        # checkout loses maintained facts, never the routing authority.
        logger.error('[MediaFacts] maintained catalog rejected: %s', exc)
        return {}


def effective_media_profile(model: Mapping) -> dict | None:
    """A Model row's registered ``media_profile``, else its maintained one.

    Registered facts win exactly as ``media_pricing`` wins over the
    maintained price; None when neither documents the Model.
    """
    profile = model.get('media_profile')
    if isinstance(profile, Mapping):
        return copy.deepcopy(dict(profile))
    facts = maintained_media_facts(str(model.get('creator_id') or ''),
                                   str(model.get('model_id') or ''))
    return facts.get('media_profile') if facts else None


def maintained_media_facts(creator_id: str, model_id: str) -> dict | None:
    """Official media facts for one exact Creator/Model identity, else None.

    Returns a detached ``{'kind', 'media_pricing'?, 'media_profile'?}``.
    Identity is exact: a dated snapshot or another spelling has no borrowed
    media facts.
    """
    entry = _catalog().get((creator_id, model_id))
    return copy.deepcopy(entry) if entry is not None else None


__all__ = [
    'CATALOG_CONTRACT',
    'MediaFactError',
    'admit_media_fact_catalog',
    'effective_media_profile',
    'maintained_media_facts',
    'media_pricing_unit_issues',
    'validate_media_pricing',
    'validate_media_profile',
]
