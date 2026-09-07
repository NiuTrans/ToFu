"""Owner-scoped repository for reconstructible large tool results.

Application code uses semantic Sidecar operations only. Filesystem paths and
SQL never cross this boundary, keeping SQLite/PostgreSQL swappable.
"""

from __future__ import annotations

import hashlib
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
        ttl = max(1, min(int(ttl_ms), MAX_TTL_MS))
        payload = {
            "user_id": owner,
            "content": str(content),
            "media_type": str(media_type or "text/plain"),
            "created_at_ms": now,
            "expires_at_ms": now + ttl,
        }
        digest = hashlib.sha256(
            str(content).encode("utf-8", errors="replace")).hexdigest()
        command_id = f"tool-result:{owner}:{digest}:{now + ttl}"
        return get_storage_client(write=True).command(
            "tool_result_artifact.put", payload, command_id,
            deadline=self.deadline_seconds,
        )

    def read_range(self, *, user_id: int, artifact_ref: str,
                   offset: int = 0, limit: int = 8192,
                   now_ms: int | None = None) -> dict[str, Any] | None:
        return get_storage_client().query(
            "tool_result_artifact.read",
            {
                "user_id": _owner(user_id),
                "artifact_ref": str(artifact_ref),
                "offset": max(0, int(offset)),
                "limit": max(1, min(int(limit), 64 * 1024)),
                "now_ms": int(now_ms or time.time() * 1000),
            },
            deadline=self.deadline_seconds,
        )

    def search(self, *, user_id: int, artifact_ref: str, query: str,
               cursor: int = 0, limit: int = 8,
               now_ms: int | None = None) -> dict[str, Any] | None:
        return get_storage_client().query(
            "tool_result_artifact.search",
            {
                "user_id": _owner(user_id),
                "artifact_ref": str(artifact_ref),
                "query": str(query),
                "cursor": max(0, int(cursor)),
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


def artifact_provenance(task, artifact_ref):
    """Return the registered origin of ``artifact_ref`` on this task, if any."""
    if not isinstance(task, dict) or not artifact_ref:
        return None
    provenance = task.get('_artifactProvenance')
    if not isinstance(provenance, dict):
        return None
    entry = provenance.get(artifact_ref)
    return entry if isinstance(entry, dict) else None


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
    "DEFAULT_TTL_MS", "MAX_TTL_MS", "ToolResultArtifactRepository",
    "ToolResultArtifactWriter",
    "artifact_provenance", "continuation_display_label",
    "continuation_origin_meta", "register_artifact_provenance",
]
