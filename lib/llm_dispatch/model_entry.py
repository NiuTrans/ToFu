# HOT_PATH — resolve_request_ids runs once per (provider, model, key) at slot build.
"""lib/llm_dispatch/model_entry.py — the model-identity contract.

ONE entry in ``provider['models']`` describes ONE LOGICAL model:

``model_id``
    The LOGICAL id. This is the name presets target, the frontend picker
    shows and sorts, conversations persist, and pricing / display look up.
    It is an *identity*, not necessarily a value that goes on the wire.

``request_ids``
    The ordered POOL of ids actually sent as the ``"model"`` field on the
    wire for that logical model. Several ids in one pool are interchangeable
    gateway deployments of the same underlying model, so the dispatcher
    builds one slot per (id × key) and rotates across them.

Provider-tainted spellings — ``aws.``, ``vertex.``, ``yuju-…-evaDaily`` — are
wire details and belong in ``request_ids``. ``model_id`` stays the clean
logical name, which is why the picker and the presets can stay stable while a
gateway renames its deployments underneath.

Compatibility — do NOT "simplify" this away
-------------------------------------------
An entry written before this contract has no ``request_ids`` and carries its
extra wire ids under ``aliases``. For such an entry the pool is
``[model_id] + aliases``: the root id MUST stay in the pool. Reading ``aliases``
as *the* pool (dropping the root) is the dangerous half-migration — a config
that used to reach three gateway spellings would quietly reach two, with no
error raised anywhere, because each remaining id still works.

Because an entry may name a logical ``model_id`` that is NOT itself a wire id,
``routing_group()`` returns ``{model_id} ∪ request_ids``. The dispatcher feeds
those groups to its alias index so requesting the logical id — or any single
wire id a stored conversation still references — resolves to the same pool.
"""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['resolve_request_ids', 'routing_group', 'has_explicit_pool']


def _clean(values) -> list:
    """Return ``values`` as a de-duplicated list of non-empty strings, in order."""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def has_explicit_pool(entry: dict) -> bool:
    """True when *entry* declares its wire pool under the new contract.

    Used to decide whether the root ``model_id`` is folded into the pool
    (legacy ``aliases``-only entry) or omitted (explicit ``request_ids``,
    where a logical-only id is the whole point).
    """
    return bool(isinstance(entry, dict) and _clean(entry.get('request_ids')))


def resolve_request_ids(entry: dict, cell: dict | None = None) -> list:
    """Return the ordered wire-id pool for one model entry.

    Args:
        entry: a ``provider['models'][i]`` dict.
        cell: optional ``entry['key_access'][key_idx]`` override dict. A cell
            may narrow or replace the pool for one API key (a gateway often
            grants different deployments to different keys).

    Returns:
        Ordered list of ids to send as ``"model"`` on the wire. Never contains
        duplicates. Empty only when the entry names no id at all.

    Resolution:
        * ``request_ids`` present  → that list verbatim (authoritative; the
          logical ``model_id`` is included only if listed).
        * otherwise                → ``[model_id] + aliases`` (legacy shape,
          root preserved — see the module docstring).

        A ``cell`` may override either field; an absent cell field inherits
        from the entry. ``cell['disabled_ids']`` then subtracts concrete ids
        this key must not serve.
    """
    if not isinstance(entry, dict):
        return []
    model_id = (entry.get('model_id') or '').strip()
    cell = cell if isinstance(cell, dict) else {}

    # An explicit pool wins, and a cell-level pool wins over the entry's.
    explicit = _clean(cell.get('request_ids')) or _clean(entry.get('request_ids'))
    if explicit:
        pool = explicit
    else:
        # Legacy entry: aliases ADD to the root, they do not replace it.
        aliases = (_clean(cell.get('aliases'))
                   if 'aliases' in cell else _clean(entry.get('aliases')))
        pool = _clean([model_id] + aliases)

    disabled = set(_clean(cell.get('disabled_ids')))
    if disabled:
        pool = [mid for mid in pool if mid not in disabled]
    return pool


def routing_group(entry: dict) -> set:
    """Return every id that must route to *entry*'s pool.

    ``{model_id} ∪ request_ids`` across the entry and all of its ``key_access``
    cells. The logical id is always a member so ``prefer_model=<logical>``
    resolves even when the logical id is never sent on the wire; every wire id
    is a member so a conversation persisted against one gateway spelling keeps
    routing after the pool is re-shaped.
    """
    if not isinstance(entry, dict):
        return set()
    model_id = (entry.get('model_id') or '').strip()
    group = {model_id} if model_id else set()
    # Union over the entry itself plus every per-key cell, ignoring
    # disabled_ids: a group member disabled on ONE key is still the same
    # logical model and must stay routable through the other keys.
    cells = entry.get('key_access') or {}
    sources = [None] + [c for c in cells.values() if isinstance(c, dict)]
    for cell in sources:
        _c = dict(cell) if cell else None
        if _c:
            _c.pop('disabled_ids', None)
        group.update(resolve_request_ids(entry, _c))
    return {g for g in group if g}
