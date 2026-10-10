"""Owner-scoped repository for retained tool observations; never reexecute.

Application code uses semantic Sidecar operations only. Filesystem paths and
SQL never cross this boundary, keeping SQLite/PostgreSQL swappable.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

from lib.storage import get_storage_client


from lib.tool_result_artifact_writer import ToolResultArtifactWriter

DEFAULT_TTL_MS = 24 * 60 * 60 * 1000
MAX_TTL_MS = 7 * 24 * 60 * 60 * 1000


def _owner(user_id: int) -> int:
    value = int(user_id)
    if value <= 0:
        raise ValueError("user_id must be a positive repository owner")
    return value


@dataclass(frozen=True)
class ToolResultArtifactRepository:
    """Stateless semantic repository; ownership is explicit on every call."""

    deadline_seconds: float = 5.0

    def put(self, *, user_id: int, content: str,
            media_type: str = "text/plain", ttl_ms: int = DEFAULT_TTL_MS,
            now_ms: int | None = None) -> dict[str, Any]:
        owner = _owner(user_id)
        now = int(now_ms or time.time() * 1000)
        if not isinstance(content, str):
            raise TypeError('Tool result text retention requires a string')
        encoded = content.encode('utf-8')  # Invalid text must never become replacement bytes.
        ttl = max(1, min(int(ttl_ms), MAX_TTL_MS))
        payload = {
            "user_id": owner,
            "content": str(content),
            "media_type": str(media_type or "text/plain"),
            "created_at_ms": now,
            "expires_at_ms": now + ttl,
        }
        # The command_id is an idempotency key for ONE logical put, so it must
        # stay unique per capture — keyed on the RAW content digest (including
        # the manifest's random resultId), NOT the canonical content address.
        # Canonicalizing here would collapse two distinct captures of identical
        # content onto one command_id with different payloads, which the writer
        # correctly rejects as a reused command_id. The canonical (resultId-
        # excluded) address is computed by the backend at store time; it is not
        # this layer's concern.
        digest = hashlib.sha256(encoded).hexdigest()
        command_id = f"tool-result:{owner}:{digest}:{now + ttl}"
        return get_storage_client(write=True).command(
            "tool_result_artifact.put", payload, command_id,
            deadline=self.deadline_seconds,
        )

    def read_range(self, *, user_id: int, artifact_ref: str,
                   offset: int | str = 0, limit: int = 8192,
                   selector: str | None = None,
                   now_ms: int | None = None) -> dict[str, Any] | None:
        return get_storage_client().query(
            "tool_result_artifact.read",
            {
                "user_id": _owner(user_id),
                "artifact_ref": str(artifact_ref),
                "offset": offset,
                "selector": selector or '',
                "limit": max(1, min(int(limit), 64 * 1024)),
                "now_ms": int(now_ms or time.time() * 1000),
            },
            deadline=self.deadline_seconds,
        )

    def search(self, *, user_id: int, artifact_ref: str, query: str,
               cursor: int | str = 0, limit: int = 8,
               now_ms: int | None = None) -> dict[str, Any] | None:
        return get_storage_client().query(
            "tool_result_artifact.search",
            {
                "user_id": _owner(user_id),
                "artifact_ref": str(artifact_ref),
                "query": str(query),
                "cursor": cursor,
                "limit": max(1, min(int(limit), 20)),
                "now_ms": int(now_ms or time.time() * 1000),
            },
            deadline=self.deadline_seconds,
        )


#: Bound on per-task origin entries. A turn rarely spills more than a
#: handful of results; the FIFO cap keeps a pathological loop from growing
#: task state without bound. Entries are tiny and advisory — eviction just
#: means the legacy digest label is kept.
_PROVENANCE_MAX_ENTRIES = 256

#: Durable origin store namespace. The in-task ``_artifactProvenance`` map
#: dies with the turn, so a cross-turn ``read_tool_artifact`` read-back used
#: to lose the source round and fall back to the bare digest label. The
#: origin is therefore ALSO written through the generic ``record.*`` store —
#: an already-certified dual-backend (TofuDB / SQL sidecar) semantic
#: operation — keyed by content digest, so a later turn can re-resolve it.
#:
#: WHY record and not a dedicated artifact-origin table: provenance is a
#: reconstructible read-model (advisory, never re-executes), and ``record.*``
#: already owns the "store a small JSON document by key" contract on both
#: backends. A parallel table would re-implement that subset beside the
#: certified mechanism for zero behavioural gain. The artifact body itself
#: stays in the dedup'd ``tool_result_artifacts`` table; only the tiny
#: origin pointer rides record.
#:
#: TODO(enterprise): ``storage_records`` has no ``user_id`` column, so owner
#: isolation is carried INSIDE the value (``user_id`` field) and enforced at
#: the read boundary here — the same pattern ``task_results`` uses. If a
#: second real user ever lands, provenance reads must stay owner-filtered
#: (they are) and a dedicated owner-keyed store should replace this.
_ORIGIN_NAMESPACE = 'tool_result_artifact_origin'


def _origin_key(artifact_ref) -> str:
    """Content-digest record key for a ``tool-result:<sha256>`` pointer."""
    ref = str(artifact_ref or '').strip()
    prefix = 'tool-result:'
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def persist_artifact_origin(user_id, artifact_ref, *, tool_name, display,
                            llm_round, tool_call_id='', ttl_ms=DEFAULT_TTL_MS,
                            now_ms=None):
    """Durably record the origin of a spilled artifact, keyed by digest.

    Advisory and overwrite-on-respill: two turns that spill byte-identical
    content share one digest, and the LATEST origin wins — acceptable because
    the label is a hint, not a fact used for execution. The value carries
    ``user_id`` (owner filter at read time) and ``expiresAtMs`` (lazy
    expiry: a stale origin past its artifact's TTL reads as absent, since the
    artifact body is gone by then anyway). Any storage failure degrades to
    the in-memory map only — provenance must never crash a tool settle.
    """
    try:
        owner = _owner(user_id)
    except (TypeError, ValueError):
        return
    key = _origin_key(artifact_ref)
    if not key:
        return
    now = int(now_ms or time.time() * 1000)
    ttl = max(1, min(int(ttl_ms), MAX_TTL_MS))
    value = {
        'v': 1,
        'user_id': owner,
        'toolName': str(tool_name or ''),
        'display': ' '.join(str(display or '').split()),
        'llmRound': llm_round if isinstance(llm_round, int) else None,
        'expiresAtMs': now + ttl,
    }
    if tool_call_id:
        value['toolCallId'] = str(tool_call_id)
    try:
        get_storage_client(write=True).command(
            'record.put',
            {'namespace': _ORIGIN_NAMESPACE, 'key': key, 'value': value},
            f'tool-result-origin:{owner}:{key}:{now}',
        )
    except Exception as exc:  # advisory: never let a store hiccup break settle
        _origin_logger().debug(
            '[artifact_provenance] persist failed for %s: %s', key, exc)


def load_artifact_origin(user_id, artifact_ref, *, now_ms=None):
    """Re-resolve a spilled artifact's origin from the durable record store.

    Returns the same dict shape the in-task map holds, or None when the
    record is absent, belongs to another owner, or is past its lazy expiry.
    """
    try:
        owner = _owner(user_id)
    except (TypeError, ValueError):
        return None
    key = _origin_key(artifact_ref)
    if not key:
        return None
    try:
        row = get_storage_client().query(
            'record.get',
            {'namespace': _ORIGIN_NAMESPACE, 'key': key},
        )
    except Exception as exc:
        _origin_logger().debug(
            '[artifact_provenance] load failed for %s: %s', key, exc)
        return None
    value = row.get('value') if isinstance(row, dict) else None
    if not isinstance(value, dict):
        return None
    # Owner filter precedes any use: a foreign record is indistinguishable
    # from an absent one at this boundary (see TODO(enterprise) above).
    if value.get('user_id') != owner:
        return None
    expires = value.get('expiresAtMs')
    now = int(now_ms or time.time() * 1000)
    if isinstance(expires, int) and expires <= now:
        return None
    origin = {
        'toolName': str(value.get('toolName') or ''),
        'display': str(value.get('display') or ''),
        'llmRound': value.get('llmRound'),
    }
    if value.get('toolCallId'):
        origin['toolCallId'] = str(value['toolCallId'])
    return origin


def _origin_logger():
    from lib.log import get_logger
    return get_logger(__name__)


# ── Model-facing semantic aliases ────────────────────────────────────────────
#: Canonical storage reference: a bare content digest. This remains the ONLY
#: key the storage boundary accepts (CAS dedup, manifest children, signed
#: cursor binding, compaction anchors all depend on the exact shape).
DIGEST_REF_RE = re.compile(r'tool-result:[0-9a-f]{64}')

#: Model-facing alias: ``tr:<semantic-slug>:<digest-prefix>``. The slug makes
#: the handle memorable and BPE-friendly; the hex prefix disambiguates. An
#: alias is a PROJECTION, never a key: it resolves to the full digest through
#: a durable ``record.*`` row before any storage call, so a wrong alias fails
#: loud instead of silently reading the wrong evidence.
ALIAS_REF_RE = re.compile(r'tr:[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?:[0-9a-f]{8,64}')

#: Durable alias store namespace. Same ``record.*`` contract (and the same
#: TODO(enterprise) owner-in-value caveat) as ``_ORIGIN_NAMESPACE`` above.
_ALIAS_NAMESPACE = 'tool_result_artifact_alias'

#: Digest-prefix lengths tried in order. A same-slug same-prefix collision
#: with DIFFERENT content lengthens the suffix; the full 64 hex is unique by
#: construction, so the loop always terminates.
_ALIAS_SUFFIX_LENGTHS = (8, 16, 32, 64)

#: In-task alias map cap; same FIFO story as ``_PROVENANCE_MAX_ENTRIES``.
_ALIAS_MAX_ENTRIES = 256

#: Slug source cap. Keeps the whole alias <= 92 chars even at full-length
#: suffix, inside the 96-char ``artifact_ref`` schema ceiling.
_ALIAS_SLUG_MAX = 24


def _alias_slug(tool_name, display) -> str:
    """ASCII handle for an alias, preferring the richer display label."""
    for raw in (display, tool_name):
        slug = re.sub(r'[^a-z0-9]+', '-', str(raw or '').lower()).strip('-')
        slug = slug[:_ALIAS_SLUG_MAX].strip('-')
        if slug:
            return slug
    return 'result'


def _register_task_alias(task, alias, digest_ref) -> None:
    """Same-turn fast path for resolve; loss degrades to the durable store."""
    if not isinstance(task, dict):
        return
    aliases = task.setdefault('_artifactAliases', {})
    if alias not in aliases and len(aliases) >= _ALIAS_MAX_ENTRIES:
        aliases.pop(next(iter(aliases)))
    aliases[alias] = digest_ref


def _claim_alias_record(owner, alias, digest_ref, *, now_ms, ttl_ms) -> bool:
    """Persist ``alias -> digest_ref`` unless a live row maps it elsewhere.

    Advisory: store hiccups return False (caller keeps the digest). A live
    row for different content — or another owner's handle — is never
    overwritten; the caller lengthens the suffix instead. The read-after-write
    verify closes the concurrent-mint race: a lane that lost the write race
    sees the foreign digest here and lengthens rather than silently aliasing
    to the wrong evidence.
    """
    try:
        client = get_storage_client(write=True)
        row = client.query(
            'record.get', {'namespace': _ALIAS_NAMESPACE, 'key': alias})
        value = row.get('value') if isinstance(row, dict) else None
        if isinstance(value, dict):
            expires = value.get('expiresAtMs')
            live = not (isinstance(expires, int) and expires <= now_ms)
            if live and (value.get('user_id') != owner
                         or value.get('digest') != digest_ref):
                return False
        client.command(
            'record.put',
            {'namespace': _ALIAS_NAMESPACE, 'key': alias, 'value': {
                'v': 1, 'user_id': owner, 'digest': digest_ref,
                'expiresAtMs': now_ms + ttl_ms}},
            f'tool-result-alias:{owner}:{alias}:{now_ms}')
        row = client.query(
            'record.get', {'namespace': _ALIAS_NAMESPACE, 'key': alias})
        value = row.get('value') if isinstance(row, dict) else None
        return (isinstance(value, dict) and value.get('user_id') == owner
                and value.get('digest') == digest_ref)
    except Exception as exc:  # advisory: never let a store hiccup break settle
        _origin_logger().debug('[artifact_alias] claim failed for %s: %s',
                               alias, exc)
        return False


def mint_artifact_alias(task, artifact_ref, *, tool_name, display,
                        user_id=None, ttl_ms=DEFAULT_TTL_MS, now_ms=None):
    """Mint a deterministic semantic alias for a freshly spilled artifact.

    Deterministic: the same (slug, content) always yields the same alias, so
    a re-spill reuses the existing row (refreshing its expiry) and identical
    model projections stay identical for replay/dedup guards. Returns None —
    caller keeps the canonical digest — when the ref is not a digest, no
    owner is resolvable (durable rows require one), or every candidate is
    claimed by different content.
    """
    ref = str(artifact_ref or '')
    if not DIGEST_REF_RE.fullmatch(ref):
        return None
    if user_id is None:
        return None
    try:
        owner = _owner(user_id)
    except (TypeError, ValueError):
        return None
    now = int(now_ms or time.time() * 1000)
    ttl = max(1, min(int(ttl_ms), MAX_TTL_MS))
    slug = _alias_slug(tool_name, display)
    digest_hex = ref[len('tool-result:'):]
    for length in _ALIAS_SUFFIX_LENGTHS:
        alias = f'tr:{slug}:{digest_hex[:length]}'
        if _claim_alias_record(owner, alias, ref, now_ms=now, ttl_ms=ttl):
            _register_task_alias(task, alias, ref)
            return alias
    return None


def resolve_artifact_ref(task, artifact_ref, *, user_id=None, now_ms=None):
    """Resolve a model-facing reference to the canonical storage digest.

    Full digests and unrecognized strings pass through unchanged (the storage
    boundary validates them). An alias resolves via the in-task map first,
    then the durable record — owner-filtered and lazily expired, exactly like
    the origin record. Returns None only for a well-formed alias that does
    not resolve (unknown, foreign, or expired): callers must fail loud with
    ``artifact_unavailable`` rather than guess. Store ERRORS propagate so the
    handler maps them to a retryable unavailability instead of a verdict.
    """
    ref = str(artifact_ref or '').strip()
    if DIGEST_REF_RE.fullmatch(ref):
        return ref
    if not ALIAS_REF_RE.fullmatch(ref):
        return ref
    if isinstance(task, dict):
        aliases = task.get('_artifactAliases')
        if isinstance(aliases, dict):
            hit = aliases.get(ref)
            if isinstance(hit, str) and DIGEST_REF_RE.fullmatch(hit):
                return hit
    if user_id is None:
        return None
    try:
        owner = _owner(user_id)
    except (TypeError, ValueError):
        return None
    row = get_storage_client().query(
        'record.get', {'namespace': _ALIAS_NAMESPACE, 'key': ref})
    value = row.get('value') if isinstance(row, dict) else None
    if not isinstance(value, dict):
        return None
    if value.get('user_id') != owner:
        return None
    expires = value.get('expiresAtMs')
    now = int(now_ms or time.time() * 1000)
    if isinstance(expires, int) and expires <= now:
        return None
    digest = str(value.get('digest') or '')
    return digest if DIGEST_REF_RE.fullmatch(digest) else None


def register_artifact_provenance(task, artifact_ref, *, tool_name, display,
                                 llm_round, tool_call_id=''):
    """Remember which tool call produced a spilled tool-result artifact.

    The dispatch pipeline replaces an oversized result with a v2 envelope
    whose ``artifactRef`` is a bare content hash — by construction it carries
    NO provenance, so the origin is knowable only at spill time, where the
    source round and the pointer coexist. A later ``read_tool_artifact`` /
    ``search_tool_artifact`` row consults this map to label itself with the
    source round + tool instead of the digest.

    The map lives on the task dict (one task == one assistant turn), so a
    cross-turn read simply misses and keeps the legacy label.
    ``task.setdefault``/item assignment are atomic under the GIL, which is
    the whole thread-safety story this needs: parallel tool lanes register
    concurrently, and a lost entry degrades to the legacy label, never to a
    crash.
    """
    if not isinstance(task, dict) or not artifact_ref:
        return
    provenance = task.setdefault('_artifactProvenance', {})
    if (artifact_ref not in provenance
            and len(provenance) >= _PROVENANCE_MAX_ENTRIES):
        provenance.pop(next(iter(provenance)))
    entry = {
        'toolName': str(tool_name or ''),
        'display': ' '.join(str(display or '').split()),
        'llmRound': llm_round,
    }
    if tool_call_id:
        entry['toolCallId'] = str(tool_call_id)
    provenance[artifact_ref] = entry


def artifact_provenance(task, artifact_ref, *, user_id=None):
    """Return the registered origin of ``artifact_ref``, in-task then durable.

    The in-task map is the fast path (same turn). On a miss — a cross-turn
    read-back, where the map died with its turn — fall back to the durable
    record store so the continuation row still names its source round. The
    fallback needs the owner to filter the shared record namespace, so it
    only fires when ``user_id`` is supplied.
    """
    if not isinstance(task, dict) or not artifact_ref:
        return None
    provenance = task.get('_artifactProvenance')
    if isinstance(provenance, dict):
        entry = provenance.get(artifact_ref)
        if isinstance(entry, dict):
            return entry
    if user_id is None:
        return None
    return load_artifact_origin(user_id, artifact_ref)


def _search_queries(fn_args) -> list[str]:
    """Normalized search patterns from the single or batch form, in order.

    The v2 contract accepts either a top-level ``query`` or a ``searches``
    batch (max 16 items, each with its own ``query``); both are bounded by
    schema, so the collected list is bounded too. Duplicates collapse so a
    repeated pattern is rendered once.
    """
    args = fn_args if isinstance(fn_args, dict) else {}
    raw: list[str] = []
    top = args.get('query')
    if isinstance(top, str):
        raw.append(top)
    searches = args.get('searches')
    if isinstance(searches, list):
        raw.extend(
            item['query'] for item in searches
            if isinstance(item, dict) and isinstance(item.get('query'), str))
    queries: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalized = ' '.join(value.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            queries.append(normalized)
    return queries


def continuation_origin_meta(fn_name, fn_args, provenance):
    """Structured origin of a continuation row, for chip-style rendering.

    The flat label stacks two verbs (``Read compacted result of R54 · Read 1
    file: panel.ts``); a frontend that renders this meta instead shows an
    origin chip (``R54 compacted``) in front of the source call's own label,
    so the read-back action and the original call stay visually distinct.
    ``sourceRound`` is 1-based, matching the ``R`` anchor on every tool row.
    """
    if not isinstance(provenance, dict):
        return None
    source = str(provenance.get('display') or '').strip()
    if not source:
        source = str(provenance.get('toolName') or '').strip()
    if len(source) > 96:
        source = source[:95].rstrip() + '…'
    llm_round = provenance.get('llmRound')
    meta = {
        'kind': 'read' if fn_name == 'read_tool_artifact' else 'search',
        'sourceRound': llm_round + 1 if isinstance(llm_round, int) else None,
        'source': source,
    }
    if provenance.get('toolCallId'):
        meta['sourceToolCallId'] = str(provenance['toolCallId'])
    if fn_name == 'search_tool_artifact':
        # The row must show WHAT is being searched, not just where: without
        # the patterns the chip answers "R10 compacted" but the model's
        # actual query stays invisible (batch form especially — it has no
        # top-level ``query`` at all).
        queries = _search_queries(fn_args)
        if queries:
            meta['queries'] = queries
            meta['query'] = queries[0]
    return meta


def continuation_display_label(fn_name, fn_args, provenance):
    """Human label for a continuation row: name the SOURCE, not the digest.

    ``Read compacted result of R10 · web_search: citadel`` answers the two
    questions the bare ``Read tool result: tool-result:657e`` could not —
    WHICH round (``R10`` matches the round anchor rendered on every tool
    row) and WHICH call's spilled result is being read. Built from
    ``continuation_origin_meta`` so the flat string and the structured chip
    meta never drift.
    """
    meta = continuation_origin_meta(fn_name, fn_args, provenance) or {}
    label = ('Read compacted result' if fn_name == 'read_tool_artifact'
             else 'Search compacted result')
    if isinstance(meta.get('sourceRound'), int):
        label += f" of R{meta['sourceRound']}"
    if meta.get('source'):
        label += f" · {meta['source']}"
    if fn_name == 'search_tool_artifact':
        queries = meta.get('queries') or []
        if queries:
            shown = [q if len(q) <= 48 else q[:47].rstrip() + '…'
                     for q in queries[:3]]
            label += ': ' + ' · '.join(shown)
            if len(queries) > 3:
                label += f" · +{len(queries) - 3}"
    return label


__all__ = [
    "ALIAS_REF_RE", "DEFAULT_TTL_MS", "DIGEST_REF_RE", "MAX_TTL_MS",
    "ToolResultArtifactRepository", "ToolResultArtifactWriter",
    "artifact_provenance", "continuation_display_label",
    "continuation_origin_meta", "load_artifact_origin",
    "mint_artifact_alias", "persist_artifact_origin",
    "register_artifact_provenance", "resolve_artifact_ref",
]
