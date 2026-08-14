"""Pre-request MCP catalog search with task-sticky native schemas.

The MCP server's ``tools/list`` result is the allowed upper bound.  This
module derives a smaller active set for one task; it never manufactures tools
and never replaces their native schemas with a generic invoke envelope.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from lib.mcp.types import parse_namespaced_name


DEFAULT_ACTIVE_LIMIT = 8
MAX_DEPENDENCY_DEPTH = 8
_STATE_TTL_SECONDS = 24 * 60 * 60
_MAX_TASK_STATES = 1024

_LOCK = threading.RLock()
_INDEX_CACHE: dict[str, 'CatalogIndex'] = {}
_TASK_STATE: dict[str, dict[str, Any]] = {}


def canonical_schema_hash(schema: Any) -> str:
    payload = json.dumps(schema if isinstance(schema, dict) else {},
                         ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _terms(value: Any) -> list[str]:
    text = str(value or '').casefold()
    # Keep Latin identifiers whole, but segment CJK text into characters so
    # ``请编辑学城文档`` matches an intent metadata value ``编辑学城文档``.
    # Python ``\w`` includes CJK and would otherwise swallow each phrase as
    # one unrelated token.
    words = re.findall(r'[a-z0-9_.-]+|[\u3400-\u9fff]', text,
                       flags=re.IGNORECASE)
    out: list[str] = []
    for word in words:
        out.append(word)
        out.extend(part for part in re.split(r'[_./:-]+', word)
                   if part and part != word)
    return out


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


@dataclass(frozen=True)
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
    terms: tuple[str, ...]


@dataclass(frozen=True)
class CatalogIndex:
    fingerprint: str
    tools: tuple[CatalogTool, ...]
    by_name: dict[str, CatalogTool]
    by_server_short: dict[tuple[str, str], str]
    built_at: float


def _catalog_fingerprint(snapshot: list[dict[str, Any]]) -> str:
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


def build_catalog_index(snapshot: list[dict[str, Any]]) -> CatalogIndex:
    """Return a content-addressed index; unchanged catalogs reuse the object."""
    fingerprint = _catalog_fingerprint(snapshot)
    with _LOCK:
        cached = _INDEX_CACHE.get(fingerprint)
        if cached is not None:
            return cached

    tools: list[CatalogTool] = []
    by_server_short: dict[tuple[str, str], str] = {}
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
        item = CatalogTool(
            name=name, server_id=server_id, short_name=short_name,
            definition=definition, description=description,
            schema_hash=schema_hash, catalog_version=version,
            bundle=bundle, requires=requires, profiles=profiles,
            intents=intents, aliases=aliases, risk=risk,
            terms=tuple(_terms(search_text)),
        )
        tools.append(item)
        by_server_short[(server_id, short_name)] = name
    tools.sort(key=lambda tool: tool.name)
    index = CatalogIndex(
        fingerprint=fingerprint, tools=tuple(tools),
        by_name={tool.name: tool for tool in tools},
        by_server_short=by_server_short, built_at=time.time())
    with _LOCK:
        _INDEX_CACHE[fingerprint] = index
    return index


def invalidate_server_catalog(server_id: str) -> None:
    """Invalidate indexes containing *server_id* after list_changed."""
    wanted = str(server_id or '')
    with _LOCK:
        doomed = [key for key, index in _INDEX_CACHE.items()
                  if any(tool.server_id == wanted for tool in index.tools)]
        for key in doomed:
            _INDEX_CACHE.pop(key, None)
        # Task selections are tied to fingerprints.  Leave their called-tool
        # history intact; the next selection intersects it with the new upper
        # bound and recomputes dependencies.


def _prune_states(now: float) -> None:
    expired = [task_id for task_id, state in _TASK_STATE.items()
               if now - float(state.get('touched') or 0) > _STATE_TTL_SECONDS]
    for task_id in expired:
        _TASK_STATE.pop(task_id, None)
    if len(_TASK_STATE) > _MAX_TASK_STATES:
        oldest = sorted(_TASK_STATE,
                        key=lambda key: _TASK_STATE[key].get('touched', 0))
        for task_id in oldest[:len(_TASK_STATE) - _MAX_TASK_STATES]:
            _TASK_STATE.pop(task_id, None)


def record_mcp_tool_used(task_id: str, namespaced_name: str) -> None:
    if not task_id or parse_namespaced_name(str(namespaced_name or '')) is None:
        return
    now = time.time()
    with _LOCK:
        state = _TASK_STATE.setdefault(task_id, {
            'active': [], 'used': [], 'fingerprint': '', 'query': '',
            'touched': now})
        if namespaced_name not in state['used']:
            state['used'].append(namespaced_name)
        if namespaced_name not in state['active']:
            state['active'].append(namespaced_name)
        state['touched'] = now


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


def select_active_mcp_tools(
    snapshot: list[dict[str, Any]],
    *,
    task_id: str,
    query: str,
    used_names: list[str] | None = None,
    limit: int = DEFAULT_ACTIVE_LIMIT,
) -> list[dict[str, Any]]:
    """Select stable native schemas, then expand same-server dependencies."""
    index = build_catalog_index(snapshot)
    if len(index.tools) <= max(1, int(limit or DEFAULT_ACTIVE_LIMIT)):
        return [tool.definition for tool in index.tools]
    wanted = max(1, min(int(limit or DEFAULT_ACTIVE_LIMIT), 8))
    now = time.time()
    with _LOCK:
        _prune_states(now)
        state = _TASK_STATE.setdefault(task_id or index.fingerprint, {
            'active': [], 'used': [], 'fingerprint': index.fingerprint,
            'query': '', 'touched': now})
        for name in used_names or ():
            if name in index.by_name and name not in state['used']:
                state['used'].append(name)
        previous = [name for name in state.get('active', [])
                    if name in index.by_name]
        retained_used = [name for name in state.get('used', [])
                         if name in index.by_name]

    qterms = _terms(query)
    query_key = ' '.join(qterms)
    # Identical task/query/catalog returns the exact previous ordering.
    if (state.get('fingerprint') == index.fingerprint
            and state.get('query') == query_key and previous):
        # Calling a tool must not move it to the front of the next request's
        # schema array. Preserve the exact active order and append only a used
        # tool that was somehow not active (e.g. a direct hidden-name call).
        names = _expand_dependencies(index, list(dict.fromkeys(
            [*previous, *retained_used])))
        return [index.by_name[name].definition for name in names]

    doc_freq = Counter()
    for tool in index.tools:
        doc_freq.update(set(tool.terms))
    ranked: list[tuple[float, str]] = []
    score_by_name: dict[str, float] = {}
    qtext = str(query or '').casefold().strip()
    for tool in index.tools:
        counts = Counter(tool.terms)
        score = 0.0
        for term in qterms:
            freq = counts.get(term, 0)
            if freq:
                score += freq * (1.0 + len(index.tools) /
                                 max(1, doc_freq.get(term, 1)))
        if qtext and (qtext == tool.short_name.casefold()
                      or qtext == tool.name.casefold()):
            score += 1000
        elif qtext and any(qtext == alias.casefold()
                           for alias in tool.aliases):
            score += 500
        elif qtext and any(qtext in intent.casefold()
                           for intent in tool.intents):
            score += 100
        if score > 0:
            ranked.append((-score, tool.name))
            score_by_name[tool.name] = score
    ranked.sort()

    # Sticky order: used first, then still-relevant previous tools, then new
    # ranking. Scores never reorder an already-active tool within the task.
    # On a material intent change, retain called tools unconditionally and
    # only the prior tools that are still relevant to the new query.  If the
    # query yields no signal at all, keep the old stable set rather than
    # making tools disappear on an ambiguous turn.
    retained_set = set(retained_used)
    relevant_previous = [name for name in previous
                         if name in score_by_name or name in retained_set]
    selected = list(dict.fromkeys([*relevant_previous, *retained_used]))
    for _negative, name in ranked:
        if name not in selected:
            selected.append(name)
        if len(selected) >= wanted:
            break
    if not selected and previous:
        selected = list(previous)
    if not selected:
        # An underspecified prompt still gets a small deterministic starter
        # surface instead of zero MCP capability. Prefer non-write tools when
        # the server supplied risk metadata.
        fallback = sorted(
            index.tools,
            key=lambda tool: (tool.risk.casefold() == 'write', tool.name))
        selected = [tool.name for tool in fallback[:min(4, wanted)]]
    selected = selected[:max(wanted, len(retained_used))]
    selected = _expand_bundles(
        index, selected, max(wanted, len(retained_used)))
    selected = _expand_dependencies(index, selected)

    with _LOCK:
        state['active'] = list(selected)
        state['fingerprint'] = index.fingerprint
        state['query'] = query_key
        state['touched'] = now
    return [index.by_name[name].definition for name in selected]


def catalog_cache_stats() -> dict[str, int]:
    with _LOCK:
        return {'indexes': len(_INDEX_CACHE), 'tasks': len(_TASK_STATE)}


__all__ = [
    'build_catalog_index', 'canonical_schema_hash', 'catalog_cache_stats',
    'invalidate_server_catalog', 'record_mcp_tool_used',
    'select_active_mcp_tools',
]
