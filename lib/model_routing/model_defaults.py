"""Install declared Model defaults; provider facts never become defaults.

Entry points: apply_model_defaults and official_model_directory. These run at
catalog/install and authority normalization boundaries. Dispatch consumes the
normalized owner Model aggregate; there is no request-time name guessing.
Package data is bounded and cached by file revision, with one cached revision.

Maintained lookups resolve one Creator/Model identity: the exact spelling
first, else the single entry of the same creator sharing its spelling key —
case and dot/dash/underscore/space separators only (``claude-opus-5.5`` and
``claude-opus-5-5``). Snapshot dates and quantization suffixes stay
significant: ``canonical_key`` also strips them to merge registry aliases, but
a moving alias's price, profile or context is no fact of a dated snapshot or a
quantized variant. Entries sharing a key whose borrowed facts are identical
(the declared dot and dash spellings of one Model) are one entry; entries
whose facts differ, another creator or another version never match.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import copy
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re

from .domain import ModelRef
from .inference import profile_from_metadata, resolve_inference_profile, validate_inference_profile

_PATH = Path(__file__).parents[1] / 'model_info' / 'model_inference_defaults.json'
_SPELLING_SEPARATORS = re.compile(r'[\s._-]+')
# Developer facts a respelling borrows from its one catalog entry.
OFFICIAL_MODEL_FACTS = (
    'capabilities', 'context_window', 'quality_rank', 'inference_profile', 'list_pricing')


@lru_cache(maxsize=4096)
def _spelling_key(model_id: str) -> str:
    return _SPELLING_SEPARATORS.sub('', model_id.casefold())


def spelling_identity(creator_id: str, model_id: str) -> tuple[str, str]:
    """Same-creator key of spellings differing only in case and separators."""
    return creator_id, _spelling_key(model_id)


def index_spelling_identities(rows: Iterable[Mapping]) -> dict[tuple[str, str], list[Mapping]]:
    """Group catalog rows by spelling identity, one row per exact identity."""
    index: dict[tuple[str, str], dict[tuple[str, str], Mapping]] = {}
    for row in rows:
        exact = (row['creator_id'], row['model_id'])
        index.setdefault(spelling_identity(*exact), {}).setdefault(exact, row)
    return {key: list(group.values()) for key, group in index.items()}


def _sole_entry(candidates: Sequence, facts: Callable = lambda candidate: candidate):
    """The first candidate when all lend the same facts, else None (ambiguous)."""
    if candidates and all(facts(other) == facts(candidates[0]) for other in candidates[1:]):
        return candidates[0]
    return None


def _borrowed_facts(row: Mapping) -> dict:
    return {fact: row[fact] for fact in OFFICIAL_MODEL_FACTS if fact in row}


def unique_spelling_row(index: Mapping, creator_id: str | None, model_id: str) -> Mapping | None:
    """The one indexed entry of *creator_id* sharing *model_id*'s spelling key, else None."""
    if not creator_id:
        return None
    return _sole_entry(index.get(spelling_identity(creator_id, model_id), ()), _borrowed_facts)


@dataclass(frozen=True, slots=True)
class ModelDefaults:
    """Validated defaults of one file revision.

    ``exact`` maps each declared identity to its (profile, superseded
    profiles); ``spelling`` indexes those identities by spelling identity.
    """

    exact: Mapping[ModelRef, tuple[dict, tuple[dict, ...]]]
    spelling: Mapping[tuple[str, str], tuple[ModelRef, ...]]

    def resolve(self, ref: ModelRef) -> tuple[dict, tuple[dict, ...]] | None:
        found = self.exact.get(ref)
        if found is None:
            refs = self.spelling.get(spelling_identity(ref.creator_id, ref.model_id), ())
            found = _sole_entry([self.exact[twin] for twin in refs])
        return found


@lru_cache(maxsize=1)
def _profiles(mtime_ns: int, size: int) -> ModelDefaults:
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
        if profile in previous:
            # Applying such a seed would replace itself forever: every
            # authority normalization would report a change.
            raise ValueError('current model inference default is also listed as superseded')
        for model_id in group['model_ids']:
            ref = ModelRef.from_value({'creator_id': group['creator_id'], 'model_id': model_id})
            if ref in result or len(result) >= 2048:
                raise ValueError('duplicate or excessive model inference default identities')
            result[ref] = (profile, previous)
    spelling: dict[tuple[str, str], list[ModelRef]] = {}
    for ref in result:
        spelling.setdefault(spelling_identity(ref.creator_id, ref.model_id), []).append(ref)
    return ModelDefaults(result, {key: tuple(refs) for key, refs in spelling.items()})


def current_model_defaults() -> ModelDefaults:
    """Return the validated defaults for the file revision on disk now.

    One stat per call. A caller applying defaults to many Models passes this
    snapshot as ``profiles`` so a whole-aggregate normalization stats once,
    not once per Model: each stat is a GIL-releasing syscall, and under a busy
    interpreter every one of them waits a full switch interval to reacquire.
    """
    stat = _PATH.stat()
    return _profiles(stat.st_mtime_ns, stat.st_size)


def has_exact_model_defaults(model: dict) -> bool:
    """Whether maintained knowledge names this Creator/Model identity.

    "Exact" excludes family guesses: the declared spelling or the creator's
    one entry spelled differently only in case and separators, never a
    similar name (another snapshot date or suffix).
    """
    return current_model_defaults().resolve(ModelRef.from_value(model)) is not None


def apply_model_defaults(
    model: dict, *, baseline: dict | None = None, profiles: ModelDefaults | None = None,
) -> bool:
    """Fill missing declared fields without overriding owner edits."""
    if profiles is None:
        profiles = current_model_defaults()
    defaults, previous = profiles.resolve(ModelRef.from_value(model)) or ({}, ())
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
    from lib.provider.provider_template_recipes import provider_from_template
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
