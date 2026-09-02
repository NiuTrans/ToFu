"""Owner-scoped conversation summaries and bounded project digests.

Settled project turns schedule summary refreshes on a fixed worker lane. Jobs
coalesce by ``(user_id, conversation_id)`` so bursts cannot create unbounded
threads or duplicate LLM work. Prompt assembly only reads cached summaries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lib.conversations._bounded_lane import BoundedCoalescingLane
from lib.log import get_logger
from runtime_guards import resolve_resource_budget
logger = get_logger(__name__)

# Hard cap on a stored summary (chars). A digest of 10 of these must stay
# small enough to be cache-friendly in the system prompt.
SUMMARY_MAX_CHARS = 320

# Regenerate the summary when the conversation has grown by at least this many
# NEW messages since it was last summarized. A conversation that hasn't grown
# materially keeps its cached summary (no redundant LLM call, stable digest).
SUMMARY_STALE_GROWTH = 6

# A conversation must have at least this many messages before it's worth
# summarizing (a 1-turn conv is adequately described by its title).
SUMMARY_MIN_MESSAGES = 3

# Digest bounds (the "always-on in project mode" injection).
DIGEST_MAX_SIBLINGS = 10
# Only consider this many recent project conversations as digest candidates
# (a generous superset of DIGEST_MAX_SIBLINGS so we can skip ones lacking a
# usable summary without a second query).
_DIGEST_SCAN_LIMIT = 24

# When the digest is relevance-gated (a query is supplied), keep at least this
# many of the MOST-RECENT siblings unconditionally, unioned with the BM25
# matches — so an off-topic or brand-new turn still surfaces *something* rather
# than an empty digest.
_DIGEST_RECENCY_FLOOR = 3


@dataclass(frozen=True, slots=True)
class ProjectDigestProjection:
    """One sibling snapshot projected into prompt text and UI metadata."""

    text: str
    entries: tuple[dict[str, str], ...]

_SYSTEM_PROMPT = (
    'You write a ONE to THREE sentence summary of a chat conversation, used so '
    'an AI assistant working on the same project can tell at a glance what this '
    'conversation accomplished — without opening it.\n'
    'Rules:\n'
    '- Lead with the concrete OUTCOME or DECISION: what was built, fixed, '
    'decided, or concluded. Name the actual thing (the file, feature, bug, '
    'technology, or design choice).\n'
    '- Include key decisions or constraints that a future conversation would '
    'need to know, if any.\n'
    '- Be specific and dense. Skip greetings, the fact that it was a chat, and '
    'filler ("the user asked", "we discussed").\n'
    '- Use the SAME language as the conversation (Chinese summary for a Chinese '
    'conversation, English for English).\n'
    '- No markdown, no bullet points, no trailing label. Output ONLY the '
    'summary sentences.'
)


_SUMMARY_WORKERS = 2
_SUMMARY_CAPACITY = resolve_resource_budget(
    'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY', maximum=4096)


def _merge_summary_request(current: bool, newest: bool) -> bool:
    return bool(current or newest)


def _consume_summary_request(scope: tuple[int, str], force: bool) -> None:
    user_id, conv_id = scope
    _ensure_summary_blocking(conv_id, user_id=user_id, force=bool(force))


def _report_summary_failure(
    scope: tuple[int, str], error: Exception
) -> None:
    logger.warning(
        '[ProjSummary] background refresh failed conv=%s: %s',
        scope[1][:8], error, exc_info=True)


_summary_lane = BoundedCoalescingLane[tuple[int, str], bool](
    name='project-summary',
    workers=_SUMMARY_WORKERS,
    capacity=_SUMMARY_CAPACITY,
    merge=_merge_summary_request,
    consume=_consume_summary_request,
    on_error=_report_summary_failure,
)


def _schedule_summary(conv_id: str, *, user_id: int, force: bool) -> bool:
    scope = (int(user_id), conv_id)
    return _summary_lane.submit(scope, bool(force))


def _wait_for_background_summaries(timeout: float = 5.0) -> bool:
    """Wait for the summary lane to become idle; test/lifecycle diagnostic."""
    return _summary_lane.wait_idle(timeout)


def background_summary_lane_snapshot() -> dict[str, float | int | str]:
    """Operational counters for capacity, saturation, and coalescing."""
    return _summary_lane.snapshot()


def _msg_text(msg: dict) -> str:
    """Plain user-visible text of a message (no tool/image blocks)."""
    content = msg.get('content', '')
    original = msg.get('originalContent')
    if isinstance(original, str) and original.strip():
        content = original
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get('type') in (
                    'text', 'output_text', None):
                parts.append(block.get('text', '') or '')
        return '\n'.join(p for p in parts if p).strip()
    return ''


def _build_digest_source(messages: list, *, max_chars: int = 4000) -> str:
    """Condense a conversation into a compact transcript for summarization.

    Takes the user+assistant text turns (skips tool noise) and caps the total
    so the cheap call stays fast. Keeps the opening turn (sets the topic) and
    the most recent turns (the outcome).
    """
    turns = []
    for m in messages:
        role = m.get('role')
        if role not in ('user', 'assistant'):
            continue
        text = _msg_text(m)
        if text:
            turns.append((role, text))
    if not turns:
        return ''
    # Keep the first 2 turns + the last 6 turns (outcome-weighted).
    if len(turns) > 8:
        kept = turns[:2] + turns[-6:]
    else:
        kept = turns
    lines = []
    budget = max_chars
    for role, text in kept:
        snippet = text[:1200]
        line = f'{role.capitalize()}: {snippet}'
        if budget - len(line) < 0:
            break
        lines.append(line)
        budget -= len(line)
    return '\n\n'.join(lines)


def generate_summary(messages: list) -> str:
    """Produce a 1-3 sentence project-aware summary of a conversation.

    Returns the cleaned, length-capped summary, or '' on failure / empty
    conversation (callers treat '' as "no summary available").
    """
    if not isinstance(messages, list) or len(messages) < SUMMARY_MIN_MESSAGES:
        return ''
    source = _build_digest_source(messages)
    if not source:
        return ''

    started = time.time()
    try:
        from lib.llm_dispatch import dispatch_chat
        content, _usage = dispatch_chat(
            [
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user',
                 'content': f'Conversation:\n\n{source}\n\nSummary:'},
            ],
            max_tokens=512,
            temperature=0.2,
            capability='cheap',
            log_prefix='[ProjSummary]',
        )
    except Exception as e:
        logger.warning('[ProjSummary] dispatch_chat failed after %.1fs: %s',
                       time.time() - started, e)
        return ''

    summary = _clean_summary(content or '')
    if summary:
        logger.info('[ProjSummary] generated summary=%.80r in %.1fs',
                    summary, time.time() - started)
    else:
        logger.info('[ProjSummary] empty/unusable model output (%.80r)', content)
    return summary


def _clean_summary(raw: str) -> str:
    """Normalize model output into a single-paragraph, length-capped summary."""
    text = (raw or '').strip()
    # Drop a leading "Summary:" / "总结：" label if the model added one.
    import re
    text = re.sub(r'^\s*(?:summary|摘要|总结|概要)\s*[:：]\s*', '', text,
                  flags=re.IGNORECASE)
    # Collapse internal newlines/bullets to a single paragraph.
    text = re.sub(r'\s*\n+\s*', ' ', text)
    text = re.sub(r'^\s*[-*•]\s*', '', text).strip()
    text = text.strip().strip('"\u201c\u201d\'`').strip()
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rstrip() + '…'
    return text


def _is_stale(stored: dict | None, msg_count: int) -> bool:
    """Whether a stored summary needs (re)generation for the given msg_count."""
    if not stored or not stored.get('text'):
        return True
    prev_count = stored.get('msg_count_at_gen')
    if not isinstance(prev_count, int):
        return True
    return (msg_count - prev_count) >= SUMMARY_STALE_GROWTH


def ensure_summary(conv_id: str, *, user_id: int, force: bool = False,
                   blocking: bool = True) -> str | None:
    """Ensure ``conv_id`` has a fresh ``settings.projectSummary``; return it.

    Reads the conversation, and if its stored summary is missing or stale
    (msg_count grew >= ``SUMMARY_STALE_GROWTH`` since last generation),
    regenerates and persists it into the ``settings`` JSON.

    Args:
        conv_id: conversation to summarize.
        force: regenerate even if the cached summary looks fresh.
        blocking: when True generate inline; when False, coalesce a refresh on
            the bounded worker lane and immediately return the cached text.

    Returns:
        The summary text, or None when unavailable (too short, generation
        failed, or — in non-blocking mode — not yet generated).
    """
    if not conv_id:
        return None

    if not blocking:
        cached = _read_cached_summary(conv_id, user_id=user_id)
        _schedule_summary(conv_id, user_id=user_id, force=force)
        return cached

    return _ensure_summary_blocking(conv_id, user_id=user_id, force=force)


def _read_cached_summary(conv_id: str, *, user_id: int) -> str | None:
    """Return the stored summary text without generating, or None."""
    try:
        from lib.conversations.repository import get_conversation
        snapshot = get_conversation(
            conv_id, user_id=int(user_id), include_messages=False)
        if snapshot is None:
            return None
        settings = snapshot.get('settings') or {}
        ps = settings.get('projectSummary') if isinstance(settings, dict) else None
        if isinstance(ps, dict) and ps.get('text'):
            return ps['text']
    except Exception as e:
        logger.debug('[ProjSummary] cached read failed conv=%s: %s',
                     conv_id[:8], e)
    return None


def _ensure_summary_blocking(
    conv_id: str, *, user_id: int, force: bool = False
) -> str | None:
    """Inline generate-if-stale + persist. Returns the (possibly new) text."""
    try:
        from lib.conversations.repository import get_conversation
        row = get_conversation(conv_id, user_id=int(user_id))
    except Exception as e:
        logger.warning('[ProjSummary] load failed conv=%s: %s', conv_id[:8], e)
        return None
    if not row:
        return None

    messages = row.messages
    settings = row.get('settings') or {}
    if not isinstance(settings, dict):
        settings = {}
    stored = settings.get('projectSummary')
    msg_count = len(messages) if isinstance(messages, list) else 0

    if msg_count < SUMMARY_MIN_MESSAGES:
        return stored.get('text') if isinstance(stored, dict) else None

    if not force and not _is_stale(stored, msg_count):
        return stored.get('text') if isinstance(stored, dict) else None

    summary = generate_summary(messages)
    if not summary:
        # Keep any previous text rather than wiping it on a transient failure.
        return stored.get('text') if isinstance(stored, dict) else None

    _persist_summary(
        conv_id,
        summary,
        msg_count,
        user_id=user_id,
        expected_rev=row.get('rev'),
    )
    return summary


def _persist_summary(conv_id: str, summary: str, msg_count: int,
                     *, user_id: int,
                     expected_rev: int | None = None) -> None:
    """Read-modify-write ``settings.projectSummary`` for one conversation.

    Only the ``settings`` column is touched (not ``messages`` / ``updated_at``),
    so this never reorders the sidebar or races the message-persist path on
    other columns. Routes through the shared ``settings_store`` helper, which
    serializes the read-merge-write per conv across ALL settings writers — so
    this no longer clobbers (or is clobbered by) an unrelated settings write
    (autopilot / tool-state / activeTaskId), closing the "rare lost update"
    the module lock could not prevent.
    """
    record = {
        'text': summary,
        'generated_at': int(time.time() * 1000),
        'msg_count_at_gen': msg_count,
    }
    try:
        from lib.storage import StorageError, get_storage_client
        payload = {'conv_id': conv_id, 'user_id': int(user_id),
                   'updates': {'projectSummary': record}}
        if expected_rev is not None:
            payload['expected_rev'] = int(expected_rev)
        try:
            get_storage_client(write=True).command(
                'conversation.settings.update', payload,
                f'project-summary:{conv_id}:{expected_rev}:{record["generated_at"]}')
        except StorageError as exc:
            if exc.code == 'database_conflict':
                logger.debug('[ProjSummary] settings CAS lost conv=%s', conv_id[:8])
                return
            raise
        logger.debug('[ProjSummary] persisted summary conv=%s (msg_count=%d)',
                     conv_id[:8], msg_count)
    except Exception as e:
        logger.warning('[ProjSummary] persist failed conv=%s: %s',
                       conv_id[:8], e)


def _rank_digest_candidates(candidates: list[dict], limit: int,
                            query: str | None) -> list[dict]:
    if not candidates:
        return []
    if not query or not query.strip():
        return candidates[:limit]
    try:
        from lib.memory.relevance import score_items
        docs = [f'{c["title"]} {c["summary"]}'.strip() for c in candidates]
        scored = score_items(query, docs)
    except Exception as e:
        logger.debug('[ProjSummary] digest relevance scoring failed: %s', e)
        return candidates[:limit]
    ordered: list[dict] = []
    seen: set[str] = set()
    for idx, _score in scored:
        c = candidates[idx]
        if c['id'] in seen:
            continue
        seen.add(c['id'])
        ordered.append(c)
        if len(ordered) >= limit:
            return ordered
    for c in candidates[:_DIGEST_RECENCY_FLOOR]:
        if c['id'] in seen:
            continue
        seen.add(c['id'])
        ordered.append(c)
        if len(ordered) >= limit:
            break
    return ordered[:limit]


def project_digest_entries(project_path: str, *, user_id: int,
                           current_conv_id: str | None = None,
                           limit: int = DIGEST_MAX_SIBLINGS,
                           query: str | None = None) -> list[dict]:
    """Return the bounded sibling-conversation list as structured dicts.

    The structured backbone of :func:`build_project_digest`: up to ``limit`` of
    the OTHER conversations of ``project_path`` that have a title (and, when
    available, a cached summary), each as ``{'id', 'title', 'summary'}``.
    ``summary`` is '' when none is cached.

    Selection strategy:
      • Always scan the ``_DIGEST_SCAN_LIMIT`` most-recently-updated siblings.
      • When ``query`` is falsy → return the top ``limit`` by pure recency
        (back-compat: this is what every prior caller got).
      • When ``query`` is present → BM25-rank the candidates by ``title +
        summary`` relevance (reusing :func:`lib.memory.relevance.score_items`,
        the same CJK-aware scorer the preference-detail tier uses) and return
        the relevant matches UNIONED with a small recency floor
        (``_DIGEST_RECENCY_FLOOR`` most-recent kept unconditionally), so an
        off-topic or fresh turn is never empty. Result order: relevance-first,
        then the recency-floor remainder; total capped at ``limit``.

    Read-only and side-effect-free (never generates a summary). Returns ``[]``
    on no project / no siblings / DB error. Used both to render the prompt
    digest text and to stash the same data for the frontend provenance chip,
    so the two can never disagree about which siblings were surfaced.
    """
    if not project_path:
        return []
    limit = max(1, min(int(limit or DIGEST_MAX_SIBLINGS), DIGEST_MAX_SIBLINGS))
    try:
        from lib.conversations.repository import list_conversations
        rows = list_conversations(
            user_id=int(user_id),
            project_path=project_path,
            order_by='updated_at_desc',
            limit=_DIGEST_SCAN_LIMIT,
            include_messages=False,
            settings_keys=['projectPath', 'projectSummary'],
        )
        # Keep a fail-closed witness during mixed-version rollouts: a prior
        # Sidecar ignores an unknown optional filter instead of applying it.
        rows = [
            row for row in rows
            if (row.get('settings') or {}).get('projectPath') == project_path
        ]
    except Exception as e:
        logger.warning('[ProjSummary] digest query failed: %s', e)
        return []

    # Candidate list, recency-ordered (the SQL already sorts updated_at DESC),
    # excluding the current conversation.
    candidates: list[dict] = []
    for r in rows:
        cid = r['id']
        if current_conv_id and cid == current_conv_id:
            continue
        title = (r['title'] or '(untitled)').strip()
        settings = r.get('settings') or {}
        ps = settings.get('projectSummary') if isinstance(settings, dict) else None
        summary = (ps.get('text') if isinstance(ps, dict) else '') or ''
        candidates.append({'id': cid, 'title': title, 'summary': summary})

    return _rank_digest_candidates(candidates, limit, query)


def _render_project_digest(
    structured: tuple[dict[str, str], ...], *, conv_tools_available: bool
) -> str:
    if not structured:
        return ''
    entries = []
    for entry in structured:
        if entry.get('summary'):
            entries.append(
                f'• "{entry["title"]}" — {entry["summary"]} [{entry["id"]}]'
            )
        else:
            # No summary yet (not referenced/summarized) — still surface the
            # title so the model knows the sibling exists.
            entries.append(f'• "{entry["title"]}" [{entry["id"]}]')

    if conv_tools_available:
        header = (
            f'This project has {len(entries)} related conversation(s) you can '
            f'consult. Use list_conversations(scope="project") to search them and '
            f'get_conversation(conversation_id="<id>") to read one in full when '
            f'relevant to the user\'s request:')
    else:
        # Tool-free variant: the conv-ref tools (list_conversations /
        # get_conversation) are NOT registered this turn, so the model cannot
        # call them — never instruct it to. Surface the siblings for ambient
        # awareness only. Shares the substring "related conversation(s)" with
        # the tool-enabled header so the idempotency probe matches either.
        header = (
            f'For ambient awareness: this project has {len(entries)} related '
            f'conversation(s). You cannot open them this turn, but knowing they '
            f"exist may inform your answer:")
    return header + '\n' + '\n'.join(entries)


def build_project_digest_projection(
    project_path: str,
    *,
    user_id: int,
    current_conv_id: str | None = None,
    limit: int = DIGEST_MAX_SIBLINGS,
    conv_tools_available: bool = True,
    query: str | None = None,
) -> ProjectDigestProjection:
    """Read siblings once and derive the prompt and UI views from that snapshot."""
    structured = tuple(
        dict(entry)
        for entry in project_digest_entries(
            project_path,
            user_id=user_id,
            current_conv_id=current_conv_id,
            limit=limit,
            query=query,
        )
    )
    return ProjectDigestProjection(
        text=_render_project_digest(
            structured, conv_tools_available=conv_tools_available
        ),
        entries=structured,
    )


def build_project_digest(project_path: str, *, user_id: int,
                         current_conv_id: str | None = None,
                         limit: int = DIGEST_MAX_SIBLINGS,
                         conv_tools_available: bool = True,
                         query: str | None = None) -> str:
    """Build a bounded digest of sibling conversations of the same project.

    Returns a compact block listing up to ``limit`` of the most recently
    updated OTHER conversations of ``project_path`` that have a summary (or at
    least a title), each as ``• "title" — summary [id]``. Returns '' when there
    are no usable siblings (so the caller can skip injection entirely).

    Does NOT generate summaries (that's ``ensure_summary``'s job, run lazily on
    the trigger paths) — it only reads what's already cached, so it stays fast
    and side-effect-free on the hot prompt-assembly path.

    Args:
        conv_tools_available: Whether the ``list_conversations`` /
            ``get_conversation`` tools are registered for THIS turn. When True
            the header instructs the model to call them to drill in. When False
            (the common case — the conv-ref tools only register once the user
            @-attached a conversation; see ``lib/tools/registry/``
            ``_build_conv_ref``) the header is tool-free: the siblings are
            surfaced for ambient awareness ONLY, naming no tool the model can't
            actually call. Defaults to True for back-compat with direct callers.
            Both header variants share the substring ``related conversation(s)``
            so the injection-side idempotency probe (``_DIGEST_MARKER`` in
            ``lib/tasks_pkg/context_composer``) matches either one.
    """
    return build_project_digest_projection(
        project_path,
        user_id=user_id,
        current_conv_id=current_conv_id,
        limit=limit,
        conv_tools_available=conv_tools_available,
        query=query,
    ).text


__all__ = [
    'ensure_summary', 'generate_summary', 'build_project_digest',
    'build_project_digest_projection', 'project_digest_entries',
    'ProjectDigestProjection',
    'SUMMARY_MAX_CHARS', 'SUMMARY_STALE_GROWTH', 'SUMMARY_MIN_MESSAGES',
    'DIGEST_MAX_SIBLINGS',
]
