"""lib/memory/prefetch.py — Per-turn proactive memory surfacing.

Pipeline (round 0 only, once per user turn):

    1. Build a query from the recent conversational surface (last K
       user+assistant text turns, stripping all tool_calls / tool_results
       / thinking blocks).
    2. BM25 coarse ranking over name+description+tags+body → top-N candidates.
    3. Cheap-LLM precision filter:  given the recent turns + candidate
       summaries, the cheap model returns the JSON list of memory indices
       that are directly relevant — preferring precision over recall.
    4. Inject the selected memories (full body) into the last user message
       as a ``<relevant_memories>`` block wrapped in ``<system-reminder>``.

Every stage emits an SSE ``memory_prefetch`` event so the frontend can
show the user that a cheap model is filtering memories in the background
(otherwise the latency would feel unexplained).

The mechanism is a PROACTIVE companion to the model's explicit
``search_memories`` tool — it fixes the class of failures where the model
doesn't realise a relevant memory exists and therefore never searches.

Design note (no-fallback policy): the cheap-LLM reranker runs with NO
wall-clock timeout and NO exception handling. If the cheap call fails
or hangs, the exception propagates up to ``run_memory_prefetch``'s
outer handler and we inject NOTHING. We deliberately do NOT fall back
to BM25 top-K, because a noisy BM25 injection tends to waste tokens
and distract the main model more than it helps.

Feature-flagged via ``features.json → memory_prefetch`` (default ``True``).
Environment-variable override: ``MEMORY_PREFETCH=0`` disables.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from lib.log import audit_log, get_logger
from lib.memory.relevance import _tokenize, _build_memory_doc

logger = get_logger(__name__)

__all__ = [
    'run_memory_prefetch',
    'inject_relevant_memories',
    'PREFETCH_ENABLED',
    'PREFETCH_BM25_TOP_N',
    'PREFETCH_MAX_INJECTED',
]


# ═══════════════════════════════════════════════════════════════════════
#  Tunables (all change requires user approval per CLAUDE.md §10 if
#  adjusted at runtime — the defaults below were agreed in the planning
#  discussion before implementation).
# ═══════════════════════════════════════════════════════════════════════

PREFETCH_BM25_TOP_N       = 40     # coarse-stage candidate pool — BM25's top-40 reliably
                                   # contains the true positives; 80 just doubled the
                                   # reranker payload (~50KB) for a step that drops most of it.
PREFETCH_MAX_INJECTED     = 5      # hard cap on memories injected
PREFETCH_MAX_BYTES        = 12_000 # hard cap on injected body bytes — sized so
                                   # PREFETCH_MAX_INJECTED median-length bodies (~2KB) fit
                                   # as FULL bodies instead of being truncated to titles.
PREFETCH_RECENT_TURNS_K   = 3      # number of user+assistant pairs used for query
PREFETCH_MIN_CANDIDATES   = 2      # below this, skip cheap-LLM step
PREFETCH_BODY_PREVIEW_LEN = 500    # chars of body shown to cheap model

# Bytes cap per-memory when building the context for the cheap model
_SUMMARY_BODY_CAP = 800

# Respect feature flag in the normal way (env > features.json > default).
try:
    from lib import _resolve_feature_flag  # type: ignore
    PREFETCH_ENABLED = _resolve_feature_flag('MEMORY_PREFETCH',
                                             'memory_prefetch', True)
except Exception as _e:  # pragma: no cover — defensive
    logger.warning('[MemPrefetch] Could not resolve feature flag: %s', _e)
    PREFETCH_ENABLED = True


# ═══════════════════════════════════════════════════════════════════════
#  Query construction — strip tools, thinking, system, keep last K turns
# ═══════════════════════════════════════════════════════════════════════

_MAX_QUERY_CHARS = 4_000   # total bytes of recent conversational surface


def _msg_plain_text(msg: dict) -> str:
    """Return a message's user-visible text, stripping tool/image blocks.

    Also strips any ``<system-reminder>...</system-reminder>`` blocks —
    those are out-of-band injections (CLAUDE.md context, prior prefetch,
    cache hints) that shouldn't drive the relevance query.
    """
    content = msg.get('content', '')
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get('type') or ''
            if btype in ('text', 'output_text'):
                parts.append(block.get('text', '') or '')
            # Skip image / tool_use / tool_result / thinking / input_json / …
        text = '\n'.join(p for p in parts if p)
    else:
        return ''
    # Strip <system-reminder>...</system-reminder> blocks (DOTALL).
    text = re.sub(r'<system-reminder>.*?</system-reminder>', '',
                  text, flags=re.DOTALL)
    return text


def _extract_current_user_request(messages: list,
                                  cap: int = _MAX_QUERY_CHARS // 2) -> str:
    """Return ONLY the last user message's plain text (no role prefix).

    Used to anchor the cheap-LLM filter on "what the user just asked",
    distinct from the prior conversational background.
    """
    for msg in reversed(messages):
        if msg.get('role') != 'user':
            continue
        text = _msg_plain_text(msg).strip()
        if not text:
            continue
        if len(text) > cap:
            return text[:cap] + '…'
        return text
    return ''


def _build_recent_turns_text(messages: list, k: int = PREFETCH_RECENT_TURNS_K,
                             exclude_last_user: bool = False) -> str:
    """Collect up to K most recent user+assistant turns as plain text.

    Excludes system messages, tool messages, tool calls, thinking blocks,
    and image attachments.  Produces a compact ``[role] text`` transcript
    capped at _MAX_QUERY_CHARS total.

    Args:
        exclude_last_user: When True, the most recent user message is
            skipped — used by the cheap-LLM rerank step where the last
            user request is shown in its own dedicated section so the
            model can anchor on it rather than blend it into history.
    """
    pairs: list[tuple[str, str]] = []
    skipped_last_user = not exclude_last_user
    # Walk newest-first; collect text for 'user' and 'assistant' roles.
    for msg in reversed(messages):
        role = msg.get('role', '')
        if role not in ('user', 'assistant'):
            continue
        text = _msg_plain_text(msg).strip()
        if not text:
            continue
        if not skipped_last_user and role == 'user':
            skipped_last_user = True
            continue
        pairs.append((role, text))
        # Roughly: 2 messages = 1 "round", so stop after 2*K entries.
        if len(pairs) >= 2 * k:
            break
    pairs.reverse()

    buf: list[str] = []
    total = 0
    for role, text in pairs:
        line = f'[{role}] {text}'
        if total + len(line) > _MAX_QUERY_CHARS:
            remain = _MAX_QUERY_CHARS - total
            if remain > 100:
                buf.append(line[:remain] + '…')
            break
        buf.append(line)
        total += len(line) + 1
    return '\n\n'.join(buf)


# ═══════════════════════════════════════════════════════════════════════
#  BM25 coarse stage (reuses lib/memory/relevance tokenizer/doc builder)
# ═══════════════════════════════════════════════════════════════════════

def _bm25_top_n(memories: list[dict], query: str,
                top_n: int = PREFETCH_BM25_TOP_N) -> list[tuple[int, float]]:
    """Return [(memory_index, score), ...] sorted by BM25 score descending.

    Only memories with score > 0 are returned.  Uses the same tokenizer
    and document construction as relevance.search_memories for consistency.
    """
    import math

    from lib.memory.relevance import BM25_K1, BM25_B

    q_tokens = _tokenize(query)
    if not q_tokens or not memories:
        return []

    docs = [_build_memory_doc(m, include_body=True) for m in memories]
    doc_lens = [len(d) for d in docs]
    n = len(memories)
    avg_dl = (sum(doc_lens) / n) if n > 0 else 1.0

    q_terms = set(q_tokens)
    df: dict[str, int] = {}
    for term in q_terms:
        df[term] = sum(1 for d in docs if term in d)

    scored: list[tuple[int, float]] = []
    for i, (doc, dl) in enumerate(zip(docs, doc_lens)):
        tf_map: dict[str, int] = {}
        for t in doc:
            if t in q_terms:
                tf_map[t] = tf_map.get(t, 0) + 1
        score = 0.0
        for term in q_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            d = df.get(term, 0)
            idf = math.log((n - d + 0.5) / (d + 0.5) + 1.0)
            numerator = tf * (BM25_K1 + 1)
            denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_dl)
            score += idf * numerator / denominator
        if score > 0:
            scored.append((i, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]


# ═══════════════════════════════════════════════════════════════════════
#  Cheap-LLM precision stage
# ═══════════════════════════════════════════════════════════════════════

_RERANK_SYSTEM_PROMPT = """\
You are a memory-relevance filter.

You will be given:
  - ## Current user request — the message the user just sent. ANCHOR on this.
  - ## Recent context — earlier turns for background only.
  - ## Active environment — the project and tools available this turn.
  - ## Candidate memories — past lessons / conventions / bug patterns
    pre-selected by keyword search. MOST ARE FALSE POSITIVES.

Your job: pick the small subset that DIRECTLY help with the Current user \
request. The keyword filter is noisy — surface overlap is not enough.

Decision rules (apply in order):
  1. If you cannot name the concrete step of the Current user request that \
     a memory affects, DROP it.
  2. If a memory is about a tool / subsystem not listed in Active \
     environment, DROP it (e.g. browser memory when no browser tool is on, \
     trading memory when the project isn't trading).
  3. If a memory is for a different project than the one in Active \
     environment, DROP it unless its content is explicitly project-agnostic.
  4. If a memory only shares surface keywords (same words, different topic), \
     DROP it.
  5. KEEP if the memory describes a trap the user is about to walk into, \
     or a project convention the next action must follow.

Default to FEWER. Prefer 0–2 highly-relevant memories over 5 loosely-related \
ones. Returning an empty list is the correct answer when nothing fits.

Return ONLY a JSON object of the form:
  {"ids": [3, 7], "reason": "brief justification"}
where ids are the 1-based indices from the candidate list. Return \
{"ids": [], "reason": "none relevant"} if nothing fits."""


def _format_candidates_for_rerank(memories: list[dict],
                                  indices: list[int]) -> str:
    """Format candidate memories as a numbered list for the cheap model."""
    lines = []
    for rank, idx in enumerate(indices, 1):
        m = memories[idx]
        name = m.get('name', '')
        desc = m.get('description', '')
        tags = m.get('tags', [])
        tag_str = f' [tags: {", ".join(tags)}]' if tags else ''
        body = (m.get('body') or '')[:PREFETCH_BODY_PREVIEW_LEN]
        body = body.replace('\n', ' ').strip()
        if len(m.get('body') or '') > PREFETCH_BODY_PREVIEW_LEN:
            body += '…'
        lines.append(
            f'{rank}. {name}{tag_str}\n'
            f'   description: {desc}\n'
            f'   body preview: {body}'
        )
    return '\n\n'.join(lines)


def _extract_first_balanced_object(text: str) -> str | None:
    """Scan *text* and return the first balanced ``{...}`` substring.

    Respects strings (including escaped quotes) so `{"k":"}"}` parses
    correctly. Returns None if no balanced object exists.
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start:i + 1]
    return None


def _salvage_ids_from_truncated(text: str) -> list[int] | None:
    """Salvage the `ids` array from a response truncated mid-JSON.

    Common cause: the cheap model hits ``max_tokens`` before closing its
    outer ``}`` (e.g. ``{"ids": [1, 2, 10, 1``). Both direct-parse and
    balanced-object-scan require matched braces, so we fall through here.

    Strategy: locate ``"ids"`` → its opening ``[`` → take everything up
    to the matching ``]`` OR end-of-string, then grab integer literals.
    A trailing partial number (no comma/bracket after it) is discarded.

    Returns the list of ints, or None if ``ids`` isn't findable.
    """
    m = re.search(r'["\']ids["\']\s*:\s*\[', text)
    if not m:
        return None
    body = text[m.end():]
    # Take up to the first ']' if present; otherwise everything left.
    close = body.find(']')
    if close >= 0:
        body = body[:close]
    else:
        # Truncated mid-array: drop everything after the last comma,
        # since the final token may be a partial number ("1" from "12").
        last_comma = body.rfind(',')
        if last_comma >= 0:
            body = body[:last_comma]
    # Extract whole integer literals (handles negatives defensively).
    return [int(x) for x in re.findall(r'-?\d+', body)]


def _parse_rerank_response(content: str, max_idx: int) -> list[int]:
    """Parse the cheap model's JSON response into a list of 0-based indices.

    Tolerant of:
      - leading/trailing prose (e.g. "Here is the answer:\\n```json\\n{...}\\n```")
      - markdown code fences anywhere
      - the model emitting multiple `{}` blocks (uses the first balanced one)
      - responses truncated mid-JSON by max_tokens (salvage the `ids` array)
    Only warns when ALL THREE paths fail — so a plausible JSON body buried
    in prose or cut off mid-array no longer trips a warning.
    """
    if not content:
        return []
    text = content.strip()

    # Pre-clean: drop any leading/trailing code-fence markers anywhere in the
    # string so ```json ... ``` with preamble text still parses. We do this
    # with a regex replace (not anchored) so text like "Here is:\n```json\n"
    # still yields a parseable body.
    cleaned = re.sub(r'```(?:json)?\s*', '', text)
    cleaned = re.sub(r'\s*```', '', cleaned).strip()

    # Path 1: direct JSON parse on the cleaned body
    obj = None
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as _e_audit:
        logger.debug('[prefetch] _parse_rerank_response caught %s: %s', type(_e_audit).__name__, _e_audit)
        obj = None

    # Path 2: scan for the first BALANCED {...} substring (brace counter
    # with string-awareness) — handles preamble like "Here is the answer:"
    if obj is None:
        candidate = _extract_first_balanced_object(cleaned)
        if candidate is None:
            candidate = _extract_first_balanced_object(text)
        if candidate is not None:
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError as _e_audit:
                logger.debug('[prefetch] _parse_rerank_response caught %s: %s', type(_e_audit).__name__, _e_audit)
                obj = None

    ids: Any = None
    if isinstance(obj, dict):
        ids = obj.get('ids')

    # Path 3: truncated-JSON salvage — recovers when the cheap model hits
    # max_tokens mid-response (e.g. `{"ids": [1, 2, 10, 1`).
    if not isinstance(ids, list):
        salvaged = _salvage_ids_from_truncated(cleaned)
        if salvaged is None:
            salvaged = _salvage_ids_from_truncated(text)
        if salvaged is not None:
            ids = salvaged
            logger.info('[MemPrefetch] rerank response truncated; '
                        'salvaged %d ids from partial JSON', len(salvaged))

    if not isinstance(ids, list):
        # Truly unparseable — demote to INFO since the prefetch pipeline
        # gracefully degrades (no memories injected) and error.log would
        # otherwise see routine cheap-model hiccups. Business log at INFO
        # still preserves the preview for diagnosis (CLAUDE.md §2 routing).
        logger.info('[MemPrefetch] rerank response not parseable as JSON, '
                    'preview: %.200s', content)
        return []

    result: list[int] = []
    for raw in ids:
        try:
            n = int(raw)
        except (TypeError, ValueError) as _e_audit:
            logger.debug('[prefetch] _parse_rerank_response caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue
        # Model uses 1-based ranks → convert to 0-based
        n -= 1
        if 0 <= n < max_idx:
            result.append(n)
    return result[:PREFETCH_MAX_INJECTED]


def _format_active_environment(project_path: str | None,
                               active_tools: list[str] | None) -> str:
    """Format the project + active-tools block shown to the cheap model.

    Project is reduced to its basename so memories don't get matched on
    incidental path components (e.g. shared `/mnt/.../user/...` prefixes).
    Tools are listed verbatim — `[]` is meaningful (means "no tools this
    turn", which is itself a strong filter signal).
    """
    import os as _os
    proj_name = ''
    if project_path:
        try:
            proj_name = _os.path.basename(_os.path.normpath(project_path))
        except Exception as e:
            logger.debug('[MemPrefetch] basename(%s) failed: %s',
                         project_path, e)
            proj_name = project_path
    tools = list(active_tools or [])
    return (f'project: {proj_name or "(none)"}\n'
            f'tools: {tools}')


def _call_cheap_reranker(memories: list[dict],
                         candidate_indices: list[int],
                         recent_turns: str,
                         current_request: str = '',
                         project_path: str | None = None,
                         active_tools: list[str] | None = None,
                         ) -> tuple[list[int], dict[str, Any]]:
    """Run the cheap-model filter.  Returns (selected_indices, diagnostics).

    selected_indices are 0-based indices into ``memories``.  The diagnostics
    dict always contains 'elapsed_ms'.  NO timeout and NO exception
    handling here — if dispatch_chat raises, we let it propagate so the
    caller/orchestrator sees it rather than silently injecting a noisy
    BM25 top-K fallback.

    The cheap model is given four sections:
      - ## Current user request — anchor for relevance
      - ## Recent context       — prior turns (may be empty on round 0)
      - ## Active environment   — project basename + active_tools list
      - ## Candidate memories   — numbered list of name/desc/tags/preview
    """
    t0 = time.time()
    diag: dict[str, Any] = {'elapsed_ms': 0}

    if len(candidate_indices) < PREFETCH_MIN_CANDIDATES:
        # Trivially too few candidates — skip LLM, take all of them.
        diag['elapsed_ms'] = int((time.time() - t0) * 1000)
        diag['skipped'] = 'too_few_candidates'
        return list(candidate_indices[:PREFETCH_MAX_INJECTED]), diag

    from lib.llm_dispatch import dispatch_chat

    cand_text = _format_candidates_for_rerank(memories, candidate_indices)
    env_text = _format_active_environment(project_path, active_tools)
    sections: list[str] = []
    if current_request:
        sections.append(f'## Current user request\n\n{current_request}')
    if recent_turns:
        sections.append(f'## Recent context (background only)\n\n{recent_turns}')
    sections.append(f'## Active environment\n\n{env_text}')
    sections.append(
        f'## Candidate memories ({len(candidate_indices)} items)\n\n{cand_text}'
    )
    user_content = '\n\n'.join(sections)

    # No timeout, no try/except — exceptions propagate to the caller.
    content, usage = dispatch_chat(
        [
            {'role': 'system', 'content': _RERANK_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        max_tokens=5120,
        temperature=0,
        capability='cheap',
        log_prefix='[MemPrefetch]',
    )

    diag['elapsed_ms'] = int((time.time() - t0) * 1000)
    diag['usage'] = usage or {}

    selected_ranks = _parse_rerank_response(content or '', len(candidate_indices))
    selected = [candidate_indices[r] for r in selected_ranks]
    return selected, diag


# ═══════════════════════════════════════════════════════════════════════
#  Injection
# ═══════════════════════════════════════════════════════════════════════

_RELEVANT_MEMORIES_TAG = '<relevant_memories>'


def _render_relevant_memories_block(selected_memories: list[dict]) -> str:
    """Render the injection block, enforcing PREFETCH_MAX_BYTES."""
    header = (
        'The following memories were pre-selected as likely relevant to '
        "what you're doing in this turn. Read them BEFORE taking action — "
        'they may warn you about traps you previously hit or remind you of '
        'project conventions you previously established. If a memory turns '
        'out not to apply, just ignore it.'
    )
    chunks: list[str] = []
    total = len(header) + len(_RELEVANT_MEMORIES_TAG) * 2 + 200
    for m in selected_memories:
        name = m.get('name', '')
        desc = m.get('description', '')
        body = (m.get('body') or '').strip()
        scope = m.get('scope', 'project')
        fp = m.get('filepath', '')
        chunk = (
            f'### memory: {name}\n'
            f'- scope: {scope}\n'
            f'- description: {desc}\n'
            f'- path: {fp}\n\n'
            f'{body}'
        )
        if total + len(chunk) > PREFETCH_MAX_BYTES:
            # Budget exhausted — truncate remaining bodies to titles + descs
            chunk_short = (
                f'### memory: {name}\n- description: {desc}\n'
                f'- path: {fp}  (body omitted — read with read_files if needed)'
            )
            if total + len(chunk_short) > PREFETCH_MAX_BYTES:
                break
            chunks.append(chunk_short)
            total += len(chunk_short)
            continue
        chunks.append(chunk)
        total += len(chunk)

    body = '\n\n'.join(chunks)
    return (
        f'{_RELEVANT_MEMORIES_TAG}\n'
        f'{header}\n\n{body}\n'
        f'</relevant_memories>'
    )


def inject_relevant_memories(messages: list,
                             selected_memories: list[dict],
                             conv_id: str | None = None) -> None:
    """Inject a <relevant_memories> block into the last user message.

    Wrapped in <system-reminder> so the model knows it's an authoritative
    out-of-band hint, not something the user said.  If the message content
    is already a list-of-blocks, we append a new text block; otherwise we
    convert the string content to a 2-element list for clean cache
    segmentation.

    Args:
        messages: Message list; last user message is mutated in place.
        selected_memories: Memory dicts to inject.
        conv_id: If provided, notify cache_tracking that we legitimately
            mutated the last user message. Without this, the next
            detect_cache_break() call treats the mutation as a
            'PREFIX MUTATION DETECTED' false positive.
    """
    if not selected_memories:
        return
    block = _render_relevant_memories_block(selected_memories)
    reminder = f'<system-reminder>\n{block}\n</system-reminder>'

    injected = False
    # Find last user message
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') != 'user':
            continue
        content = messages[i].get('content', '')
        if isinstance(content, str):
            messages[i]['content'] = [
                {'type': 'text', 'text': content},
                {'type': 'text', 'text': reminder},
            ]
        elif isinstance(content, list):
            messages[i]['content'] = list(content) + [
                {'type': 'text', 'text': reminder},
            ]
        else:
            messages[i]['content'] = [{'type': 'text', 'text': reminder}]
        injected = True
        break

    if injected and conv_id:
        # Tell cache_tracking this mutation is expected so it does NOT
        # false-positive as 'PREFIX MUTATION DETECTED' on the next call.
        try:
            from lib.tasks_pkg.cache_tracking import notify_compaction
            notify_compaction(conv_id)
        except Exception as e:
            logger.debug('[MemPrefetch] notify_compaction unavailable: %s', e)


# ═══════════════════════════════════════════════════════════════════════
#  Orchestration entry point
# ═══════════════════════════════════════════════════════════════════════

def run_memory_prefetch(messages: list,
                        project_path: str | None,
                        task: dict | None = None,
                        emit_event=None,
                        active_tools: list[str] | None = None,
                        extra_paths: list[str] | None = None) -> list[dict]:
    """Run the full BM25 → cheap-LLM → inject pipeline.

    Args:
        messages:     The message list for the current task; its last user
                      message will receive the <relevant_memories> block.
                      Mutated in-place.
        project_path: Project path for scoping project memories. Also passed
                      to the cheap-LLM filter (as a basename) so memories
                      tagged for unrelated projects are dropped.
        task:         The current task dict (for logging/audit).
        emit_event:   Callable(event_dict).  Used to emit SSE events.
                      Pass None to suppress frontend notifications.
        active_tools: Names of tools available this turn (e.g. ``['read_files',
                      'web_search']``).  Lets the cheap-LLM filter drop
                      memories about subsystems the user can't currently use.
                      Pass None or [] when unknown.
        extra_paths:  Additional workspace roots (multi-root session) whose
                      memories are unioned in alongside the primary root's.

    Returns:
        The list of memory dicts that were injected (empty list if none).
        Always returns a list — errors are logged + swallowed (advisory path).
    """
    if not PREFETCH_ENABLED:
        return []

    # Terminal phases whose payload should also be stashed on the task so
    # it survives into the DB-persisted message and the poll fallback.
    _TERMINAL_PHASES = {'done', 'skipped', 'failed'}

    def _emit(phase: str, **kw):
        """Emit a `memory_prefetch` SSE event + stash on task for persistence."""
        payload = {'phase': phase, **kw}
        if emit_event:
            try:
                from lib.agent_core.events import EventType, build_event
                emit_event(build_event(EventType.MEMORY_PREFETCH, **payload))
            except Exception as e:  # pragma: no cover
                logger.debug('[MemPrefetch] emit_event failed: %s', e)
        if task is not None:
            try:
                task['_memoryPrefetch'] = dict(payload)
            except (TypeError, AttributeError) as e:
                logger.debug('[MemPrefetch] could not stash payload on task: %s', e)

    t_start = time.time()
    tid = (task or {}).get('id', '?')[:8]

    try:
        from lib.memory.storage import get_eligible_memories
        memories = get_eligible_memories(project_path, extra_paths=extra_paths)
    except Exception as e:
        logger.warning('[MemPrefetch] get_eligible_memories failed: %s', e)
        _emit('failed', reason=f'load_error: {e}')
        return []

    if not memories:
        _emit('skipped', reason='no_memories')
        return []

    # ── Query construction
    #   For BM25 we keep the full last-K transcript (current request + prior
    #   turns) — wider lexical surface helps recall on the coarse stage.
    #   For the cheap-LLM rerank we split current_request out into its own
    #   section so the model can anchor on it.
    recent_turns = _build_recent_turns_text(messages)
    if not recent_turns.strip():
        _emit('skipped', reason='empty_query')
        return []
    current_request = _extract_current_user_request(messages)
    rerank_recent_turns = _build_recent_turns_text(messages,
                                                   exclude_last_user=True)

    _emit('started',
          total_memories=len(memories),
          candidate_target=PREFETCH_BM25_TOP_N)

    # ── Stage 1: BM25 coarse
    t_bm25 = time.time()
    scored = _bm25_top_n(memories, recent_turns, top_n=PREFETCH_BM25_TOP_N)
    bm25_ms = int((time.time() - t_bm25) * 1000)

    if not scored:
        logger.debug('[MemPrefetch][%s] BM25 found zero scored candidates '
                     '(memories=%d) — skipping cheap-LLM stage', tid, len(memories))
        _emit('skipped', reason='bm25_empty', bm25_ms=bm25_ms)
        return []

    candidate_indices = [i for i, _ in scored]
    _emit('bm25_done',
          candidates=len(candidate_indices),
          bm25_ms=bm25_ms,
          top_score=round(scored[0][1], 2))

    # ── Stage 2: Cheap-LLM precision filter
    _emit('rerank_started', candidates=len(candidate_indices))

    # NOTE: no timeout, no exception swallowing. If the cheap reranker
    # raises, we let it propagate — better to surface the failure than
    # silently fall back to a noisy BM25 top-K injection.
    selected_idx, diag = _call_cheap_reranker(
        memories, candidate_indices, rerank_recent_turns,
        current_request=current_request,
        project_path=project_path,
        active_tools=active_tools,
    )
    rerank_ms = diag.get('elapsed_ms', 0)

    # ── Stage 3: Inject
    selected_memories: list[dict] = [memories[i] for i in selected_idx]

    if not selected_memories:
        _emit('done',
              selected=0,
              bm25_ms=bm25_ms,
              rerank_ms=rerank_ms,
              total_ms=int((time.time() - t_start) * 1000),
              reason=diag.get('skipped') or 'none_relevant')
        return []

    try:
        _conv_id = (task or {}).get('convId') or None
        inject_relevant_memories(messages, selected_memories,
                                 conv_id=_conv_id)
    except Exception as e:
        logger.error('[MemPrefetch] inject failed: %s', e, exc_info=True)
        _emit('failed', reason=f'inject_error: {e}')
        return []

    total_ms = int((time.time() - t_start) * 1000)
    # Send the FULL description to the frontend so the expanded provenance
    # strip can show it in full (the chip used to hard-cap at 120 chars,
    # which clipped the very text the user wants to read on expand). The
    # description is already bounded at write time (~120 chars by convention)
    # but may legitimately run longer; the chip wraps it, so no UI cap here.
    selection_summary = [
        {'name': m.get('name', ''),
         'scope': m.get('scope', ''),
         'description': m.get('description', '')}
        for m in selected_memories
    ]

    logger.info(
        '[MemPrefetch][%s] injected %d memories (from %d BM25 candidates) '
        'in %dms (bm25=%dms rerank=%dms)',
        tid, len(selected_memories), len(candidate_indices),
        total_ms, bm25_ms, rerank_ms,
    )
    audit_log('memory_prefetch',
              task_id=(task or {}).get('id', ''),
              conv_id=(task or {}).get('convId', ''),
              injected=len(selected_memories),
              bm25_candidates=len(candidate_indices),
              bm25_ms=bm25_ms,
              rerank_ms=rerank_ms,
              memory_names=[m.get('name', '') for m in selected_memories])

    _emit('done',
          selected=len(selected_memories),
          candidates=len(candidate_indices),
          bm25_ms=bm25_ms,
          rerank_ms=rerank_ms,
          total_ms=total_ms,
          memories=selection_summary)

    return selected_memories
