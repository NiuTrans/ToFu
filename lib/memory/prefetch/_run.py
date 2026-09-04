"""High-confidence local memory prefetch.

Automatic surfacing is intentionally cheaper and stricter than explicit
``search_memories``: metadata-only BM25, then deterministic confidence gates.
Bodies remain searchable on demand and are never allowed to trigger themselves.
"""

from __future__ import annotations

import re
import time

from lib.log import audit_log, get_logger
from lib.memory.prefetch._config import (
    PREFETCH_BM25_TOP_N,
    PREFETCH_ENABLED,
    PREFETCH_MAX_BYTES,
    PREFETCH_MAX_INJECTED,
)
from lib.memory.prefetch._query import _extract_current_user_request
from lib.memory.prefetch._shortlist import _bm25_top_n
from lib.memory.relevance import _tokenize

logger = get_logger(__name__)

_SPECIFIC_RE = re.compile(
    r'(?:[\w.-]+/)+[\w.-]+|'
    r'\b[\w-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|cpp|h|md|json|ya?ml|toml)\b|'
    r'\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9_]+)+\b|'
    r'\b[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*\b|'
    r'[A-Z]{2,}[-_]?[0-9]{2,}|'
    r'(?i:(?:HTTP|ERR(?:OR)?)[-_ ]?[0-9]{3,})',
)


def _metadata(memory: dict) -> str:
    tags = memory.get('tags') or []
    return ' '.join([
        str(memory.get('name') or ''),
        str(memory.get('description') or ''),
        ' '.join(str(tag) for tag in tags),
    ]).strip()


def _todo_identifiers(task: dict | None) -> list[str]:
    values: list[str] = []
    for row in (task or {}).get('_todos') or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get('content') or row.get('text') or '')
        values.extend(match.group(0) for match in _SPECIFIC_RE.finditer(text))
    return values[:12]


def _confidence(query: str, memory: dict) -> tuple[bool, str, int]:
    meta = _metadata(memory)
    q_tokens = set(_tokenize(query))
    m_tokens = set(_tokenize(meta))
    overlap = len(q_tokens & m_tokens)
    meta_lower = meta.lower()
    exact = [match.group(0) for match in _SPECIFIC_RE.finditer(query)
             if match.group(0).lower() in meta_lower]
    if exact:
        return True, 'exact_identifier:' + exact[0], overlap
    if overlap >= 2:
        return True, f'distinct_token_overlap:{overlap}', overlap
    return False, f'low_confidence_overlap:{overlap}', overlap


def run_memory_prefetch(messages: list, project_path: str | None,
                        task: dict | None = None, emit_event=None,
                        active_tools: list[str] | None = None,
                        extra_paths: list[str] | None = None,
                        usage_sink=None) -> list[dict]:
    """Select at most two local memories and stash them for the Composer."""
    del active_tools, usage_sink  # no auxiliary model call, hence no billing
    if not PREFETCH_ENABLED:
        return []

    def emit(phase: str, **payload):
        data = {'phase': phase, **payload}
        if task is not None:
            task['_memoryPrefetch'] = dict(data)
        if emit_event:
            try:
                from lib.agent_core.events import EventType, build_event
                emit_event(build_event(EventType.MEMORY_PREFETCH, **data))
            except Exception as exc:
                logger.debug('[MemPrefetch] event emit failed: %s', exc)

    started = time.monotonic()
    current = _extract_current_user_request(messages)
    identifiers = _todo_identifiers(task)
    query = current
    if identifiers:
        query += '\n' + ' '.join(identifiers)
    if not query.strip():
        emit('skipped', reason='empty_query')
        return []

    try:
        from lib.memory.storage import get_eligible_memories
        memories = get_eligible_memories(
            project_path,
            extra_paths=extra_paths,
            include_body=False,
            record_view='retrieval',
        )
    except Exception as exc:
        emit('failed', reason=f'load_error:{exc}')
        return []
    if not memories:
        emit('skipped', reason='no_memories')
        return []

    emit('started', total_memories=len(memories),
         candidate_target=PREFETCH_BM25_TOP_N, strategy='local_high_confidence')
    scored = _bm25_top_n(memories, query, top_n=PREFETCH_BM25_TOP_N,
                         include_body=False)
    selected: list[dict] = []
    rejected = 0
    for idx, score in scored:
        keep, reason, overlap = _confidence(query, memories[idx])
        if not keep:
            rejected += 1
            continue
        row = dict(memories[idx])
        row['_prefetch_score'] = round(float(score), 4)
        row['_prefetch_reason'] = reason
        row['_prefetch_overlap'] = overlap
        selected.append(row)
        if len(selected) >= PREFETCH_MAX_INJECTED:
            break

    if selected:
        try:
            from lib.memory.storage import load_eligible_memories
            hydrated = load_eligible_memories(
                [row.get('id', '') for row in selected if row.get('id')],
                project_path,
                extra_paths=extra_paths,
                body_char_limit=PREFETCH_MAX_BYTES,
            )
            hydrated_by_id = {
                memory['id']: memory for memory in hydrated
            }
            refreshed = []
            for row in selected:
                memory_id = row.get('id')
                if not memory_id:
                    # Synthetic/custom sources without repository identity
                    # cannot be reloaded; retain their already-frozen row.
                    refreshed.append(row)
                    continue
                loaded = hydrated_by_id.get(memory_id)
                if loaded is None:
                    continue
                keep, reason, overlap = _confidence(query, loaded)
                if not keep:
                    continue
                loaded = dict(loaded)
                loaded['_prefetch_score'] = row['_prefetch_score']
                loaded['_prefetch_reason'] = reason
                loaded['_prefetch_overlap'] = overlap
                refreshed.append(loaded)
            selected = refreshed
        except Exception as exc:
            # Metadata evidence and an explicit read_files recovery path remain
            # useful when a selected file disappears or cannot be hydrated.
            logger.debug('[MemPrefetch] selected body hydration failed: %s', exc)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if task is not None:
        task['_prefetchedMemories'] = selected
    emit('done', selected=len(selected), candidates=len(scored),
         rejectedLowConfidence=rejected, total_ms=elapsed_ms,
         strategy='local_high_confidence', auxiliaryLlmCalls=0,
         memories=[{'name': row.get('name', ''),
                    'description': row.get('description', ''),
                    'reason': row.get('_prefetch_reason', '')}
                   for row in selected])
    audit_log('memory_prefetch',
              task_id=(task or {}).get('id', ''),
              conv_id=(task or {}).get('convId', ''),
              injected=len(selected), candidates=len(scored),
              rejected_low_confidence=rejected, total_ms=elapsed_ms,
              strategy='local_high_confidence', auxiliary_llm_calls=0)
    return selected


__all__ = ['run_memory_prefetch']
