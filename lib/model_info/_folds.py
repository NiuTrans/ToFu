"""lib/model_info/_folds.py — Display-fold knowledge for model pickers (SSOT).

A gateway endpoint can expose dozens of near-identical rows. Two display-only
folds keep the picker readable:

ALIAS fold
    Entries that are interchangeable on the wire — one logical entry whose
    ``routing`` pool covers another entry's ``model_id``, or ids sharing a
    static ``MODEL_ALIAS_GROUPS`` group — collapse into ONE picker row (the
    canonical entry). Selecting any member routes to the same pool, so the
    fold loses nothing.

FAMILY fold
    Same model line, different versions (``glm-5.1`` / ``glm-5.2`` /
    ``glm-5.3``) collapse under the family's primary row + a
    "N more versions" expander. The family key strips version-like tokens
    (``v3.2``, ``4o``, ``k3``, ``m2.5``, ``2601``) but keeps letter-bearing
    SKU tokens (``qwen3``), so generations never merge (``deepseek-v3.2`` →
    ``deepseek`` vs ``deepseek-v4-pro`` → ``deepseek-pro`` stay apart).
    A trailing STAGE marker (``preview``/``beta``/``alpha``) is a release
    stage, not a line discriminator — stripping it folds
    ``gemini-3-flash-preview`` with ``gemini-3.5-flash``.

Both folds are DISPLAY-ONLY: every fold is one click away from expanded, the
current model is never hidden by the frontend, and routing/failover reads the
unfolded config. This module is pure — callers precompute ``routing`` /
``explicit_pool`` so nothing here imports the dispatcher (cycle-proof).
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['family_key', 'version_tuple', 'build_fold_index']


# A token is version-like when it is digits/dots with at most a two-letter
# prefix and one-letter suffix: v3.2 / 4o / k3 / m2.5 / 2601 all qualify;
# 'flash', 'pro', 'qwen3' (4-letter prefix) never do.
_VERSION_TOKEN = re.compile(r'^[a-z]{0,2}\d+(?:\.\d+)*[a-z]?$')

# Gateway prefixes that spell the same underlying model (wire detail, not
# identity) — stripped before tokenizing so 'aws.claude-opus-4.8' and
# 'claude-opus-4.8' land in one family.
_GATEWAY_PREFIX = re.compile(r'^(?:aws|vertex|azure|us\.anthropic)\.')

# Trailing release-stage markers. A '-preview' tail made gemini-3-flash-preview
# its own family apart from gemini-3.5-flash (owner report 2026-08-23); a stage
# says HOW released, not WHICH line. Only TRAILING tokens strip — a mid-id
# marker may still discriminate (e.g. a hypothetical 'gemini-preview-2' SKU).
_STAGE_SUFFIX = frozenset({'preview', 'beta', 'alpha'})


def family_key(model_id: str) -> str:
    """Return the version-stripped line key for *model_id*.

    Models sharing a key are the same line in different versions and are
    foldable under one family row. Deterministic and conservative: when every
    token looks version-like (pathological id), the original id is the key.

    >>> family_key('glm-5.3')
    'glm'
    >>> family_key('deepseek-v4-pro')
    'deepseek-pro'
    >>> family_key('qwen3.5-plus')
    'qwen3-plus'
    >>> family_key('gemini-3-flash-preview')
    'gemini-flash'
    """
    m = (model_id or '').strip().lower()
    if not m:
        return ''
    m = _GATEWAY_PREFIX.sub('', m)
    # ':' splits too — Bedrock build tags ('claude-opus-4-8-v1:0') carry one.
    tokens = [t for t in re.split(r'[-_.:]+', m) if t]
    kept = [t for t in tokens if not _VERSION_TOKEN.match(t)]
    while len(kept) > 1 and kept[-1] in _STAGE_SUFFIX:
        kept.pop()
    if not kept:
        return m
    return '-'.join(kept)


def version_tuple(model_id: str) -> tuple:
    """All digit runs of *model_id* as an int tuple — the family "newest"
    ordering key (``glm-5.10`` → ``(5, 10)`` beats ``glm-5.9`` → ``(5, 9)``)."""
    return tuple(int(x) for x in re.findall(r'\d+', model_id or ''))


def _signature(entry: dict) -> tuple:
    """Alias-fold safety signature: only entries interchangeable in EVERY
    user-visible respect may share one row. A mirror entry deliberately
    configured with different capabilities stays its own row."""
    caps = entry.get('capabilities') or ['text']
    return (tuple(sorted(str(c) for c in caps)),
            bool(entry.get('thinking_default')))


def _pick_canonical(members: list, sets: dict) -> str:
    """Choose the face of an alias group.

    Priority: the template-managed logical entry (explicit ``request_ids``
    pool) > the entry whose pool ABSORBS the most siblings' ids > shortest
    id > alphabetical. Deterministic for any input order.
    """
    def rank(mid: str):
        entry = next(e for e in members if e['model_id'] == mid)
        absorbs = sum(1 for other in members
                      if other['model_id'] != mid
                      and other['model_id'] in sets.get(mid, set()))
        return (1 if entry.get('explicit_pool') else 0,
                absorbs, -len(mid))
    # Iterate alphabetically so max() returns the FIRST maximal element on
    # a rank tie — deterministic and the most readable spelling wins.
    return max(sorted((m['model_id'] for m in members), key=str.lower),
               key=rank)


def build_fold_index(models, alias_map=None) -> dict:
    """Compute display-fold metadata for one payload's worth of models.

    Args:
        models: iterable of dicts, one per (provider, model) pair::

            {'scope': str,            # provider id — folds never cross it
             'model_id': str,
             'capabilities': list,    # alias-fold signature guard
             'thinking_default': bool,
             'routing': set,          # every wire id this entry serves
             'explicit_pool': bool,   # declares request_ids (logical entry)
             'recommended': bool}     # explicit family-face override

        alias_map: ``{model_id: {interchangeable ids}}`` — the dispatcher's
            ``MODEL_ALIASES``. Optional; pass ``None``/``{}`` to disable the
            static-group leg (unit tests).

    Returns:
        ``{'scope::model_id': {...}}`` — ONLY for entries participating in a
        fold. Value keys:

        ``fold_group`` / ``fold_canonical``
            alias fold — group id and the face's model_id.
        ``family`` / ``family_primary``
            family fold — group id and the face's model_id. Emitted only on
            entries that are NOT a folded-away alias mirror (the alias unit
            folds as a whole via its canonical entry).
    """
    alias_map = alias_map or {}
    by_scope: dict[str, list] = {}
    for entry in models or []:
        mid = (entry.get('model_id') or '').strip()
        scope = (entry.get('scope') or '').strip()
        if not mid or not scope:
            continue
        by_scope.setdefault(scope, []).append(entry)

    out: dict[str, dict] = {}

    for scope, entries in by_scope.items():
        # ── ALIAS fold: union entries whose interchangeable id-sets
        # intersect, restricted to equal user-visible signatures ──
        id_sets = {}
        for e in entries:
            mid = e['model_id']
            ids = set(e.get('routing') or ()) | {mid}
            ids |= set(alias_map.get(mid) or ())
            id_sets[mid] = ids

        # union-find over entries sharing a signature
        parents = {e['model_id']: e['model_id'] for e in entries}

        def find(x):
            while parents[x] != x:
                parents[x] = parents[parents[x]]
                x = parents[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parents[ra] = rb

        by_sig: dict[tuple, list] = {}
        for e in entries:
            by_sig.setdefault(_signature(e), []).append(e['model_id'])
        for mids in by_sig.values():
            for i in range(len(mids)):
                for j in range(i + 1, len(mids)):
                    if id_sets[mids[i]] & id_sets[mids[j]]:
                        union(mids[i], mids[j])

        groups: dict[str, list] = {}
        for e in entries:
            groups.setdefault(find(e['model_id']), []).append(e)

        alias_mirror: set[str] = set()
        for members in groups.values():
            if len(members) < 2:
                continue
            canonical = _pick_canonical(members, id_sets)
            group_id = f'{scope}:{canonical}'
            for e in members:
                mid = e['model_id']
                out[f'{scope}::{mid}'] = {
                    'fold_group': group_id,
                    'fold_canonical': canonical,
                }
                if mid != canonical:
                    alias_mirror.add(mid)

        # ── FAMILY fold: version lines among the entries that remain
        # display-visible after the alias pass ──
        by_family: dict[str, list] = {}
        for e in entries:
            if e['model_id'] in alias_mirror:
                continue
            by_family.setdefault(family_key(e['model_id']), []).append(e)
        for fam, members in by_family.items():
            if not fam or len(members) < 2:
                continue
            explicit = [m for m in members if m.get('recommended')]
            pool = explicit or members
            # Same tie rule as _pick_canonical: alphabetical-first on rank ties.
            primary = max(
                sorted((m['model_id'] for m in pool), key=str.lower),
                key=lambda mid: (version_tuple(mid), -len(mid)))
            fam_id = f'{scope}:{fam}'
            for e in members:
                slot = out.setdefault(f'{scope}::{e["model_id"]}', {})
                slot['family'] = fam_id
                slot['family_primary'] = primary

    logger.debug('[model_folds] %d scopes, %d entries folded',
                 len(by_scope), len(out))
    return out
