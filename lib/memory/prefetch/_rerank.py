"""lib/memory/prefetch/_rerank.py — Cheap-LLM precision stage.

Given the recent turns + candidate summaries, the cheap model returns the
JSON list of memory indices that are directly relevant — preferring
precision over recall.

Design note (no-fallback policy): the reranker runs under a hard wall-clock
deadline but with NO exception handling of dispatch failures. If the cheap
call fails, the exception propagates to run_memory_prefetch's outer handler
and we inject NOTHING. On deadline timeout we also inject nothing. We
deliberately do NOT fall back to BM25 top-K.

Accounting note: the deadline stops us WAITING, it does not stop the gateway
BILLING. An abandoned worker's request is still processed and still charged
for, so the optional ``usage_sink`` is invoked from INSIDE the worker thread
— it fires whether or not the caller was still waiting. Without that, the
rounds that cost money and returned nothing were exactly the ones that
recorded nothing.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from lib.log import get_logger

from lib.memory.prefetch._config import (
    PREFETCH_BODY_PREVIEW_LEN,
    PREFETCH_DEADLINE_MS,
    PREFETCH_MAX_INJECTED,
    PREFETCH_MIN_CANDIDATES,
)

logger = get_logger(__name__)


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
    import re

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
    import re

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


# Sentinel distinguishing a deadline timeout from a legitimate falsy result.
_DEADLINE_SENTINEL = object()


def _run_with_deadline(fn, deadline_ms: int):
    """Run ``fn()`` with a hard wall-clock deadline.

    Returns ``fn()``'s result if it completes within ``deadline_ms``. On
    timeout returns ``_DEADLINE_SENTINEL`` and ABANDONS the worker — the
    thread is a daemon so it can never block process exit, and its eventual
    result is discarded. ``deadline_ms <= 0`` disables the bound (runs
    ``fn()`` inline, blocking indefinitely — the legacy behaviour).

    The no-exception-swallowing contract is preserved: if ``fn()`` raises,
    the exception is re-raised on THIS thread so the caller's outer handler
    sees it exactly as an inline call would. Only a timeout is special-cased.
    """
    if deadline_ms <= 0:
        return fn()

    box: dict[str, Any] = {}

    def _worker():
        try:
            box['result'] = fn()
        except BaseException as e:  # noqa: BLE001 — re-raised on caller thread
            box['error'] = e

    t = threading.Thread(target=_worker, name='mem-prefetch-rerank',
                         daemon=True)
    t.start()
    t.join(deadline_ms / 1000.0)

    if t.is_alive():
        # Worker still running past the deadline — abandon it (daemon).
        return _DEADLINE_SENTINEL
    if 'error' in box:
        raise box['error']
    return box.get('result')


def _call_cheap_reranker(memories: list[dict],
                         candidate_indices: list[int],
                         recent_turns: str,
                         current_request: str = '',
                         project_path: str | None = None,
                         active_tools: list[str] | None = None,
                         usage_sink=None,
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
        # Trivially too few candidates — skip LLM, take all of them. This is
        # a FAST-SKIP, not a timeout: no dispatch happens, so the deadline
        # does not apply.
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

    def _do_dispatch():
        # No internal timeout kwarg — dispatch_chat's per-attempt timeout does
        # NOT bound total wall-clock (429 cycling runs to its full budget), so
        # the hard bound is enforced by the caller's join() below, not here.
        _out = dispatch_chat(
            [
                {'role': 'system', 'content': _RERANK_SYSTEM_PROMPT},
                {'role': 'user', 'content': user_content},
            ],
            max_tokens=5120,
            temperature=0,
            capability='cheap',
            log_prefix='[MemPrefetch]',
        )
        # ★ Report the bill HERE, on the worker thread, BEFORE returning.
        #   On a deadline timeout the caller has already stopped waiting and
        #   will discard this result — but the gateway processed and charged
        #   for the request all the same. Reporting from inside the worker is
        #   what makes the abandoned call visible to the cost report instead
        #   of being silently spent.
        if usage_sink is not None:
            try:
                _u = _out[1] if isinstance(_out, tuple) and len(_out) > 1 else None
                if _u:
                    usage_sink(dict(_u))
            except Exception as _se:
                # Accounting is advisory — never take the turn down with it.
                logger.debug('[MemPrefetch] usage_sink failed: %s', _se)
        return _out

    # Resolve the deadline THROUGH the package facade at call time so a test's
    # ``monkeypatch.setattr(lib.memory.prefetch, 'PREFETCH_DEADLINE_MS', …)``
    # steers this stage exactly as it did on the pre-split single module.
    import lib.memory.prefetch as _facade
    _deadline_ms = getattr(_facade, 'PREFETCH_DEADLINE_MS', PREFETCH_DEADLINE_MS)
    outcome = _run_with_deadline(_do_dispatch, _deadline_ms)

    diag['elapsed_ms'] = int((time.time() - t0) * 1000)

    if outcome is _DEADLINE_SENTINEL:
        # Wall-clock deadline hit. Per the no-fallback policy, inject NOTHING
        # — do not splice a BM25 top-K. The abandoned worker is a daemon that
        # cannot block process exit; its eventual result is simply discarded.
        diag['timed_out'] = True
        diag['skipped'] = 'rerank_timeout'
        logger.info('[MemPrefetch] rerank exceeded %dms deadline — '
                    'injecting nothing (no fallback)', PREFETCH_DEADLINE_MS)
        return [], diag

    content, usage = outcome
    diag['usage'] = usage or {}

    selected_ranks = _parse_rerank_response(content or '', len(candidate_indices))
    selected = [candidate_indices[r] for r in selected_ranks]
    return selected, diag
