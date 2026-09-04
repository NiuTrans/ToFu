"""Pre-request MCP catalog search with conversation-sticky native schemas.

The MCP server's ``tools/list`` result is the allowed upper bound. This module
derives a smaller active set for one owner-scoped conversation (falling back
to a task when no conversation exists); it never manufactures tools and never
replaces their native schemas with a generic invoke envelope.

The first selection for one scope FREEZES the wire: the tools array opens
every provider request, so a later catalog/query change must not rewrite it
(the whole prefix cache sits behind it). Servers that connect, drop, or
reconnect after the freeze are surfaced through the context composer's
per-turn tail delta block instead and stay callable via ``execute_tools``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any

from lib.mcp.types import parse_namespaced_name


DEFAULT_ACTIVE_LIMIT = 8
MAX_DEPENDENCY_DEPTH = 8
_STATE_TTL_SECONDS = 24 * 60 * 60
_MAX_STICKY_USED_TOOLS = 32
_MAX_INLINE_STATE_KEY_CHARS = 256

_LOCK = threading.RLock()
_INDEX_CACHE: OrderedDict[str, 'CatalogIndex'] = OrderedDict()
_SELECTION_STATE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_CACHE_METRICS = {
    'indexEvictions': 0,
    'stateEvictions': 0,
    'stateExpirations': 0,
}


def _catalog_index_capacity() -> int:
    from lib.tools.resource_policy import tool_search_catalog_index_capacity
    return tool_search_catalog_index_capacity()


def _selection_state_capacity() -> int:
    from lib.tools.resource_policy import tool_search_selection_state_capacity
    return tool_search_selection_state_capacity()


def canonical_schema_hash(schema: Any) -> str:
    payload = json.dumps(schema if isinstance(schema, dict) else {},
                         ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


_TERM_PATTERN = re.compile(
    r'[a-z0-9_.-]+|[\u3400-\u9fff]',
    flags=re.IGNORECASE,
)


def _iter_terms(value: Any):
    # Keep Latin identifiers whole, but segment CJK text into characters so
    # ``请编辑学城文档`` matches an intent metadata value ``编辑学城文档``.
    # Python ``\w`` includes CJK and would otherwise swallow each phrase as
    # one unrelated token.
    for match in _TERM_PATTERN.finditer(str(value or '')):
        word = match.group(0).casefold()
        yield word
        yield from (
            part for part in re.split(r'[_./:-]+', word)
            if part and part != word
        )


def _query_profile(value: Any) -> tuple[Counter[str], bytes]:
    """Return exact term multiplicities plus a content-free sequence digest."""
    counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    for term in _iter_terms(value):
        counts[term] += 1
        encoded = term.encode('utf-8')
        digest.update(len(encoded).to_bytes(4, 'big'))
        digest.update(encoded)
    return counts, digest.digest()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item or '').strip()]


def _normalized_meta(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = ('bundle', 'requires', 'profiles', 'intents', 'aliases',
               'risk', 'schemaHash', 'catalogVersion')
    return {key: value[key] for key in allowed if key in value}


def mcp_selection_scope_id(
    *, task_id: str, conv_id: str = '', owner_user_id: int = 0,
) -> str:
    """Return the bounded-state key for one owner's conversation or task.

    Owner identity remains explicit so two users can never share a selector
    state merely because their clients supplied the same conversation ID.
    Headless callers without a conversation retain the historical task scope.
    """
    conversation = str(conv_id or '').strip()
    if conversation:
        return f'conversation:{int(owner_user_id or 0)}:{conversation}'
    return f'task:{str(task_id or "anonymous")}'


def _mcp_name_from_call(value: Any) -> str:
    if not isinstance(value, dict):
        return ''
    function = value.get('function')
    if isinstance(function, dict):
        candidate = function.get('name')
    else:
        candidate = value.get('toolName') or value.get('tool_name') \
            or value.get('name')
    name = str(candidate or '')
    return name if parse_namespaced_name(name) is not None else ''


def recent_conversation_mcp_tool_names(
    messages: list[dict[str, Any]] | None,
    *, limit: int = DEFAULT_ACTIVE_LIMIT,
) -> list[str]:
    """Read MCP calls from the completed interaction before the latest user.

    Raw Turn projections retain ``toolRounds`` while provider-style histories
    may carry ``tool_calls`` or ``tool_use`` content blocks. Only actual call
    identities are read; search-result candidates and tool arguments are not.
    """
    rows = messages if isinstance(messages, list) else []
    user_indexes = [
        index for index, message in enumerate(rows)
        if isinstance(message, dict) and message.get('role') == 'user'
    ]
    if len(user_indexes) < 2:
        return []
    start, stop = user_indexes[-2] + 1, user_indexes[-1]
    wanted = max(1, min(int(limit or DEFAULT_ACTIVE_LIMIT), 8))
    names: list[str] = []

    def add(call: Any) -> None:
        name = _mcp_name_from_call(call)
        if name and name not in names and len(names) < wanted:
            names.append(name)

    for message in rows[start:stop]:
        if not isinstance(message, dict):
            continue
        for call in message.get('toolRounds') or ():
            add(call)
        for call in message.get('tool_calls') or ():
            add(call)
        for api_round in message.get('apiRounds') or ():
            if isinstance(api_round, dict):
                for call in api_round.get('toolCalls') or ():
                    add(call)
        content = message.get('content')
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict)
                        and block.get('type') in ('tool_use', 'function_call')):
                    add(block)
    return names


_READ_ONLY_RISKS = frozenset({'none', 'read', 'read-only', 'readonly'})


def _risk_fallback_priority(risk: str) -> int:
    """Normalize cross-server risk vocabularies for ambiguous-intent fallback.

    Servers currently use both ``read|write|destructive`` and
    ``none|auth|mutating|destructive``. Explicitly read-only tools come first,
    tools without metadata remain usable, and every declared non-read risk is
    kept out of the starter set when safer choices exist. This is retrieval
    ordering only; execution authorization remains a separate boundary.
    """
    normalized = str(risk or '').strip().casefold()
    if normalized in _READ_ONLY_RISKS:
        return 0
    if not normalized:
        return 1
    return 2

@dataclass(frozen=True, slots=True)
class CatalogTool:
    name: str
    server_id: str
    short_name: str
    definition: dict[str, Any]
    description: str
    schema_hash: str
    catalog_version: str
    bundle: str
    requires: tuple[str, ...]
    profiles: tuple[str, ...]
    intents: tuple[str, ...]
    aliases: tuple[str, ...]
    risk: str


@dataclass(frozen=True, slots=True)
class CatalogIndex:
    fingerprint: str
    tools: tuple[CatalogTool, ...]
    by_name: dict[str, CatalogTool]
    by_server_short: dict[tuple[str, str], str]
    term_postings: dict[str, tuple[tuple[str, int], ...]]
    fallback_names: tuple[str, ...]
    max_query_boost_chars: int
    built_at: float


def catalog_snapshot_fingerprint(
    snapshot: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> str:
    """Return the content identity of an allowed MCP catalog snapshot."""
    rows = []
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        definition = row.get('openai_def') or {}
        fn = definition.get('function') or {}
        meta = _normalized_meta(row.get('meta'))
        schema_hash = str(meta.get('schemaHash')
                          or row.get('schema_hash')
                          or canonical_schema_hash(fn))
        rows.append({
            'server': str(row.get('server_id') or row.get('server_name') or ''),
            'version': str(row.get('catalog_version')
                           or meta.get('catalogVersion') or ''),
            'name': str(fn.get('name') or row.get('namespaced_name') or ''),
            'schemaHash': schema_hash,
            # Metadata changes affect retrieval even when a server supplied a
            # schema-only hash, so include the normalized search metadata too.
            'meta': meta,
            'description': str(fn.get('description') or ''),
        })
    rows.sort(key=lambda row: (row['server'], row['name']))
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def catalog_search_text_by_name(
    snapshot: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, str]:
    """Project private MCP retrieval metadata into deterministic plain text."""
    search_text_by_name: dict[str, str] = {}
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        definition = row.get('openai_def') or {}
        function = definition.get('function') or {}
        name = str(function.get('name') or row.get('namespaced_name') or '')
        if not name:
            continue
        meta = row.get('meta') or {}
        meta = meta if isinstance(meta, dict) else {}
        # Internal bridge rows omit the redundant server_id field. Substitute
        # only when absent so explicit third-party snapshot values retain their
        # historical search-text bytes.
        server_id = (
            row.get('server_id')
            if 'server_id' in row else row.get('server_name'))
        values = [
            server_id, row.get('server_name'), row.get('tool_name'),
            row.get('description'), meta.get('bundle'), meta.get('profiles'),
            meta.get('intents'), meta.get('aliases'),
        ]
        search_text_by_name[name] = ' '.join(
            ' '.join(str(item) for item in value)
            if isinstance(value, (list, tuple, set))
            else str(value or '')
            for value in values)
    return search_text_by_name


def build_catalog_index(
    snapshot: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    fingerprint_hint: str = '',
) -> CatalogIndex:
    """Return a content-addressed index; unchanged catalogs reuse the object."""
    hinted = str(fingerprint_hint or '').strip().lower()
    fingerprint = (
        hinted
        if re.fullmatch(r'[0-9a-f]{64}', hinted)
        else catalog_snapshot_fingerprint(snapshot)
    )
    with _LOCK:
        cached = _INDEX_CACHE.get(fingerprint)
        if cached is not None:
            _INDEX_CACHE.move_to_end(fingerprint)
            return cached

    tools: list[CatalogTool] = []
    by_server_short: dict[tuple[str, str], str] = {}
    posting_rows: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        definition = row.get('openai_def')
        if not isinstance(definition, dict):
            continue
        fn = definition.get('function') or {}
        name = str(fn.get('name') or row.get('namespaced_name') or '')
        parsed = parse_namespaced_name(name)
        if not name or parsed is None:
            continue
        server_id, short_name = parsed
        meta = _normalized_meta(row.get('meta'))
        description = str(fn.get('description') or row.get('description') or '')
        schema_hash = str(meta.get('schemaHash')
                          or row.get('schema_hash')
                          or canonical_schema_hash(fn))
        version = str(row.get('catalog_version')
                      or meta.get('catalogVersion') or '')
        bundle = str(meta.get('bundle') or '')
        requires = tuple(_string_list(meta.get('requires')))
        profiles = tuple(_string_list(meta.get('profiles')))
        intents = tuple(_string_list(meta.get('intents')))
        aliases = tuple(_string_list(meta.get('aliases')))
        risk = str(meta.get('risk') or '')
        search_text = ' '.join((name, short_name, server_id, description,
                                bundle, risk, *profiles, *intents, *aliases))
        term_counts = Counter(_iter_terms(search_text))
        item = CatalogTool(
            name=name, server_id=server_id, short_name=short_name,
            definition=definition, description=description,
            schema_hash=schema_hash, catalog_version=version,
            bundle=bundle, requires=requires, profiles=profiles,
            intents=intents, aliases=aliases, risk=risk,
        )
        tools.append(item)
        by_server_short[(server_id, short_name)] = name
        for term, frequency in term_counts.items():
            posting_rows[term].append((name, frequency))
    tools.sort(key=lambda tool: tool.name)
    fallback_names = tuple(tool.name for tool in sorted(
        tools,
        key=lambda tool: (_risk_fallback_priority(tool.risk), tool.name),
    ))
    max_query_boost_chars = max((
        len(value.casefold())
        for tool in tools
        for value in (
            tool.name, tool.short_name, *tool.aliases, *tool.intents)
    ), default=0)
    index = CatalogIndex(
        fingerprint=fingerprint, tools=tuple(tools),
        by_name={tool.name: tool for tool in tools},
        by_server_short=by_server_short,
        term_postings={
            term: tuple(sorted(rows)) for term, rows in posting_rows.items()
        },
        fallback_names=fallback_names,
        max_query_boost_chars=max_query_boost_chars,
        built_at=time.time(),
    )
    with _LOCK:
        # Another request may have built the same immutable index while this
        # request was outside the lock. Preserve one shared object identity.
        cached = _INDEX_CACHE.get(fingerprint)
        if cached is not None:
            _INDEX_CACHE.move_to_end(fingerprint)
            return cached
        _INDEX_CACHE[fingerprint] = index
        capacity = max(1, int(_catalog_index_capacity()))
        while len(_INDEX_CACHE) > capacity:
            _INDEX_CACHE.popitem(last=False)
            _CACHE_METRICS['indexEvictions'] += 1
    return index


def invalidate_server_catalog(server_id: str) -> None:
    """Invalidate indexes containing *server_id* after list_changed."""
    wanted = str(server_id or '')
    with _LOCK:
        doomed = [key for key, index in _INDEX_CACHE.items()
                  if any(tool.server_id == wanted for tool in index.tools)]
        for key in doomed:
            _INDEX_CACHE.pop(key, None)
        # Frozen wires survive invalidation by design (prefix-cache stability);
        # the composer tail delta block surfaces whatever the rebuilt catalog
        # adds. Called-tool history stays intact for post-eviction reselection.


def _prune_states(now: float) -> None:
    # Every state update moves its entry to the OrderedDict tail.  Monotonic
    # timestamps therefore make expiration an ordered-prefix operation: once
    # the oldest state is live, every newer state is live too.  Avoid scanning
    # every active conversation on each request.
    while _SELECTION_STATE:
        oldest_state = next(iter(_SELECTION_STATE.values()))
        if now - float(oldest_state.get('touched') or 0) <= _STATE_TTL_SECONDS:
            break
        _SELECTION_STATE.popitem(last=False)
        _CACHE_METRICS['stateExpirations'] += 1
    capacity = max(1, int(_selection_state_capacity()))
    while len(_SELECTION_STATE) > capacity:
        _SELECTION_STATE.popitem(last=False)
        _CACHE_METRICS['stateEvictions'] += 1


def _bounded_state_key(value: Any) -> str:
    state_key = str(value or '')
    if len(state_key) <= _MAX_INLINE_STATE_KEY_CHARS:
        return state_key
    digest = hashlib.sha256(state_key.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def _state_for_update_locked(
    state_key: str,
    *,
    now: float,
) -> dict[str, Any]:
    _prune_states(now)
    state = _SELECTION_STATE.pop(state_key, None)
    if state is None:
        capacity = max(1, int(_selection_state_capacity()))
        while len(_SELECTION_STATE) >= capacity:
            _SELECTION_STATE.popitem(last=False)
            _CACHE_METRICS['stateEvictions'] += 1
        state = {
            'used': [],
            'touched': now,
        }
    state['touched'] = now
    _SELECTION_STATE[state_key] = state
    return state


def _append_bounded_unique(
    values: list[str],
    value: str,
    *,
    limit: int,
) -> None:
    if value in values:
        return
    values.append(value)
    if len(values) > limit:
        del values[:len(values) - limit]


def record_mcp_tool_used(
    task_id: str,
    namespaced_name: str,
    *,
    selection_scope_id: str = '',
) -> None:
    state_key = _bounded_state_key(selection_scope_id or task_id or '')
    namespaced_name = str(namespaced_name or '')
    if (not state_key
            or parse_namespaced_name(namespaced_name) is None):
        return
    now = time.monotonic()
    with _LOCK:
        state = _state_for_update_locked(state_key, now=now)
        _append_bounded_unique(
            state['used'], namespaced_name, limit=_MAX_STICKY_USED_TOOLS)


def _resolve_dependency(index: CatalogIndex, owner: CatalogTool,
                        raw: str) -> str | None:
    value = str(raw or '').strip()
    if not value:
        return None
    if value in index.by_name:
        return value
    # Server-local short names are the ergonomic _meta spelling.  They cannot
    # cross namespaces, preventing one server's ``login`` dependency from
    # binding another server's tool with the same short name.
    return index.by_server_short.get((owner.server_id, value))


def _expand_dependencies(index: CatalogIndex, names: list[str]) -> list[str]:
    out = list(dict.fromkeys(name for name in names if name in index.by_name))
    visiting: set[str] = set()

    def visit(name: str, depth: int) -> None:
        if depth > MAX_DEPENDENCY_DEPTH or name in visiting:
            return
        visiting.add(name)
        tool = index.by_name[name]
        for raw in tool.requires:
            dep = _resolve_dependency(index, tool, raw)
            if not dep or dep in visiting:
                continue
            # Dependencies precede the operation that needs them even when
            # retrieval/bundle expansion already selected both in the reverse
            # order.  The old code only inserted a missing dependency, leaving
            # ``update_doc, prepare_doc_edit`` untouched when both ranked.
            owner_position = out.index(name) if name in out else len(out)
            if dep in out:
                dependency_position = out.index(dep)
                if dependency_position > owner_position:
                    out.pop(dependency_position)
                    out.insert(owner_position, dep)
            else:
                out.insert(owner_position, dep)
            visit(dep, depth + 1)
        visiting.discard(name)

    for current in list(out):
        visit(current, 0)
    return out


def _expand_bundles(index: CatalogIndex, names: list[str], limit: int) -> list[str]:
    """Add same-workflow companions without exceeding the base selection cap.

    ``requires`` is the hard execution dependency and may exceed the cap;
    ``bundle`` is a retrieval hint, so companions consume ordinary active
    slots.  This keeps the default visible selection at eight while still
    returning a coherent edit/read workflow when room is available.
    """
    out = list(dict.fromkeys(name for name in names if name in index.by_name))
    bundles = []
    for name in list(out):
        bundle = index.by_name[name].bundle
        if bundle and bundle not in bundles:
            bundles.append(bundle)
    for bundle in bundles:
        for tool in index.tools:
            if len(out) >= limit:
                return out
            if tool.bundle == bundle and tool.name not in out:
                out.append(tool.name)
    return out


def _apply_query_phrase_boosts(
    index: CatalogIndex,
    score_by_name: dict[str, float],
    qtext: str,
    *,
    query_has_terms: bool,
) -> None:
    # A name/alias equality or intent substring containing at least one term
    # must already have produced a posting hit because those same fields built
    # the index. Pure-punctuation queries have no such candidate proof and keep
    # the complete scan for compatibility (for example, an intent containing
    # ``++``).
    # Sparse name lookups win until postings cover most of the catalog; above
    # seven eighths, contiguous tuple iteration avoids a dense run of mapping
    # lookups. The threshold is deliberately conservative around the measured
    # crossover so broad generic terms do not regress.
    use_sparse_candidates = (
        query_has_terms
        and len(score_by_name) * 8 < len(index.tools) * 7
    )
    candidate_tools = (
        (index.by_name[name] for name in score_by_name)
        if use_sparse_candidates else iter(index.tools)
    )
    for tool in candidate_tools:
        score = score_by_name.get(tool.name, 0.0)
        if qtext == tool.short_name.casefold() or qtext == tool.name.casefold():
            score += 1000
        elif any(qtext == alias.casefold() for alias in tool.aliases):
            score += 500
        elif any(qtext in intent.casefold() for intent in tool.intents):
            score += 100
        if score > 0:
            score_by_name[tool.name] = score


def _freeze_and_return(state_key: str, *, now: float,
                       definitions: list[dict[str, Any]]
                       ) -> list[dict[str, Any]]:
    """Freeze *definitions* as the scope's wire unless already frozen."""
    with _LOCK:
        state = _state_for_update_locked(state_key, now=now)
        frozen = state.get('wire')
        if frozen is None:
            frozen = list(definitions)
            state['wire'] = frozen
    return list(frozen)


def freeze_wire_definitions(
    selection_scope_id: str,
    definitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Idempotently freeze *definitions* as the scope's wire; return the wire.

    Used by the registry to freeze an EMPTY wire when no MCP server is
    connected at the conversation's first tool assembly, so a server that
    appears mid-conversation enters the tail delta block, not the tools array.
    """
    state_key = _bounded_state_key(selection_scope_id)
    if not state_key:
        return list(definitions)
    return _freeze_and_return(
        state_key, now=time.monotonic(), definitions=definitions)


def frozen_wire_tool_names(
    selection_scope_id: str,
) -> tuple[tuple[str, ...], bool]:
    """Return ``(names, True)`` for a frozen scope, ``((), False)`` otherwise.

    Read-only: never creates or touches selection state.
    """
    state_key = _bounded_state_key(selection_scope_id)
    if not state_key:
        return (), False
    with _LOCK:
        state = _SELECTION_STATE.get(state_key)
        wire = (state or {}).get('wire')
    if wire is None:
        return (), False
    names = tuple(
        str((tool.get('function') or {}).get('name') or '') for tool in wire)
    return tuple(name for name in names if name), True

def select_active_mcp_tools(
    snapshot: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    task_id: str,
    query: str,
    used_names: list[str] | None = None,
    limit: int = DEFAULT_ACTIVE_LIMIT,
    selection_scope_id: str = '',
    catalog_fingerprint: str = '',
) -> list[dict[str, Any]]:
    """Select stable native schemas, then expand same-server dependencies."""
    index = build_catalog_index(
        snapshot, fingerprint_hint=catalog_fingerprint)
    now = time.monotonic()
    state_key = _bounded_state_key(
        selection_scope_id or task_id or index.fingerprint)
    with _LOCK:
        state = _state_for_update_locked(state_key, now=now)
        for name in used_names or ():
            if name in index.by_name:
                _append_bounded_unique(
                    state['used'], name, limit=_MAX_STICKY_USED_TOOLS)
        frozen = state.get('wire')
        if frozen is None and len(index.tools) <= max(
                1, int(limit or DEFAULT_ACTIVE_LIMIT)):
            # Small catalogs skip retrieval scoring but still freeze: a later
            # disconnect must not shrink the wire mid-conversation either.
            frozen = [tool.definition for tool in index.tools]
            state['wire'] = list(frozen)
        retained_used = [name for name in state.get('used', [])
                         if name in index.by_name]
    if frozen is not None:
        return list(frozen)
    wanted = max(1, min(int(limit or DEFAULT_ACTIVE_LIMIT), 8))

    query_term_counts, _query_digest = _query_profile(query)

    # Inverted postings preserve the old TF/DF formula exactly while changing
    # work from every-tool × every-query-term to query terms + actual matches.
    score_by_name: dict[str, float] = defaultdict(float)
    for term, query_frequency in query_term_counts.items():
        postings = index.term_postings.get(term)
        if not postings:
            continue
        weight = 1.0 + len(index.tools) / len(postings)
        for name, tool_frequency in postings:
            score_by_name[name] += (
                query_frequency * tool_frequency * weight)

    qtext = str(query or '').casefold().strip()
    # Equality and substring boosts are impossible once the complete query is
    # longer than every indexed name, alias, and intent. Keep longform prompts
    # on postings only instead of scanning the complete catalog again.
    if qtext and len(qtext) <= index.max_query_boost_chars:
        _apply_query_phrase_boosts(
            index, score_by_name, qtext,
            query_has_terms=bool(query_term_counts),
        )
    ranked = sorted(
        (-score, name) for name, score in score_by_name.items()
        if score > 0
    )

    # Tools already called in this conversation (seeded from history at
    # freeze time) lead the surface; new retrieval ranking fills the rest.
    # This scoring runs exactly once per scope — its result becomes the
    # frozen wire — so there is no cross-turn retention to preserve here.
    selected = list(dict.fromkeys(retained_used))
    for _negative, name in ranked:
        if name not in selected:
            selected.append(name)
        if len(selected) >= wanted:
            break
    if not selected:
        # An underspecified prompt still gets a small deterministic starter
        # surface instead of zero MCP capability. Prefer non-write tools when
        # the server supplied risk metadata.
        selected = list(index.fallback_names[:min(4, wanted)])
    selected = selected[:max(wanted, len(retained_used))]
    selected = _expand_bundles(
        index, selected, max(wanted, len(retained_used)))
    selected = _expand_dependencies(index, selected)

    definitions = [index.by_name[name].definition for name in selected]
    return _freeze_and_return(
        state_key, now=now, definitions=definitions)


def catalog_cache_stats() -> dict[str, int]:
    with _LOCK:
        return {
            'indexes': len(_INDEX_CACHE),
            'indexCapacity': max(1, int(_catalog_index_capacity())),
            'tasks': len(_SELECTION_STATE),
            'taskCapacity': max(1, int(_selection_state_capacity())),
            **_CACHE_METRICS,
        }


def clear_tool_search_caches() -> int:
    """Release reconstructible indexes/sticky state; return entries removed."""
    with _LOCK:
        dropped = len(_INDEX_CACHE) + len(_SELECTION_STATE)
        _INDEX_CACHE.clear()
        _SELECTION_STATE.clear()
        return dropped


__all__ = [
    'build_catalog_index', 'canonical_schema_hash', 'catalog_cache_stats',
    'catalog_search_text_by_name', 'catalog_snapshot_fingerprint',
    'clear_tool_search_caches', 'freeze_wire_definitions',
    'frozen_wire_tool_names', 'invalidate_server_catalog',
    'mcp_selection_scope_id',
    'recent_conversation_mcp_tool_names', 'record_mcp_tool_used',
    'select_active_mcp_tools',
]
