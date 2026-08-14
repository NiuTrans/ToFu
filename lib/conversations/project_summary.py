"""lib.conversations.project_summary — cross-conversation project awareness.

Layer 2 of the cross-conversation work (see ``docs/CROSS_CONV_AWARENESS.md``).
Where Layer 1 made ``list_conversations`` project-aware, this module gives the
model *ambient* awareness of sibling conversations of the same project, without
the user attaching anything.

Two pieces:

  • **Lazy summary generation** — ``ensure_summary(conv_id)`` produces a 1-3
    sentence "outcome + key decisions" description of a conversation via the
    cheap model (reusing the ``title_gen`` dispatch pattern) and caches it in
    ``settings.projectSummary = {text, generated_at, msg_count_at_gen, lang}``.
    It regenerates only when the conversation has grown *materially* past the
    msg_count it was last summarized at — so a static conversation is never
    re-summarized. Triggered lazily (post-first-reply, and on first
    ``get_conversation`` of a sibling), never eagerly for every conversation.

  • **Bounded project digest** — ``build_project_digest(project_path, ...)``
    returns a compact, bounded list (top ``DIGEST_MAX_SIBLINGS``) of the most
    recently-updated *other* conversations of the same project, each rendered
    as ``title — summary [id]``. Injected always-on in project mode by
    ``lib/tasks_pkg/system_context.py`` so the model knows the siblings exist.
    Its header only instructs the model to ``get_conversation(id)`` /
    ``list_conversations()`` when those tools are actually registered for the
    turn (``conv_tools_available``); otherwise the siblings are surfaced for
    ambient awareness only, naming no tool the model cannot call.

Design notes / tradeoffs are documented in ``docs/CROSS_CONV_AWARENESS.md``.
"""

from __future__ import annotations

import threading
import time

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger
from lib.utils import safe_json

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # mirrors lib.conv_ref / routes.common

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


def ensure_summary(conv_id: str, *, force: bool = False,
                   blocking: bool = True) -> str | None:
    """Ensure ``conv_id`` has a fresh ``settings.projectSummary``; return it.

    Reads the conversation, and if its stored summary is missing or stale
    (msg_count grew >= ``SUMMARY_STALE_GROWTH`` since last generation),
    regenerates and persists it into the ``settings`` JSON.

    Args:
        conv_id: conversation to summarize.
        force: regenerate even if the cached summary looks fresh.
        blocking: when True (default) generate inline and return the text;
            when False, spawn a daemon thread and return the cached text (or
            None) immediately — used by hot paths that must not wait on an LLM
            call (post-first-reply, get_conversation).

    Returns:
        The summary text, or None when unavailable (too short, generation
        failed, or — in non-blocking mode — not yet generated).
    """
    if not conv_id:
        return None

    if not blocking:
        # Fire-and-forget: return whatever is cached now, generate in the bg.
        cached = _read_cached_summary(conv_id)
        threading.Thread(
            target=_ensure_summary_blocking,
            args=(conv_id,), kwargs={'force': force},
            name=f'projsummary-{conv_id[:8]}', daemon=True,
        ).start()
        return cached

    return _ensure_summary_blocking(conv_id, force=force)


def _read_cached_summary(conv_id: str) -> str | None:
    """Return the stored summary text without generating, or None."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)).fetchone()
        if not row:
            return None
        settings = safe_json(row['settings'], default={}, label='projsummary-settings')
        ps = settings.get('projectSummary') if isinstance(settings, dict) else None
        if isinstance(ps, dict) and ps.get('text'):
            return ps['text']
    except Exception as e:
        logger.debug('[ProjSummary] cached read failed conv=%s: %s',
                     conv_id[:8], e)
    return None


def _ensure_summary_blocking(conv_id: str, *, force: bool = False) -> str | None:
    """Inline generate-if-stale + persist. Returns the (possibly new) text."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        from lib.database.conversation_repository import load_conversation
        row = load_conversation(
            db, conv_id, user_id=DEFAULT_USER_ID,
            metadata_columns=('title', 'settings'))
    except Exception as e:
        logger.warning('[ProjSummary] load failed conv=%s: %s', conv_id[:8], e)
        return None
    if not row:
        return None

    messages = row.messages
    settings = safe_json(row['settings'], default={}, label='projsummary-settings')
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

    _persist_summary(conv_id, summary, msg_count)
    return summary


def _persist_summary(conv_id: str, summary: str, msg_count: int) -> None:
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
        from lib.conversations import set_conversation_settings
        # notify=False: projectSummary is pure prompt-assembly state (never
        # rendered in the UI), so invalidate the local sidebar cache (its blob
        # now carries the new summary) but do NOT push a cross-device frame.
        set_conversation_settings(conv_id, {'projectSummary': record},
                                  user_id=DEFAULT_USER_ID, notify=False)
        logger.debug('[ProjSummary] persisted summary conv=%s (msg_count=%d)',
                     conv_id[:8], msg_count)
    except Exception as e:
        logger.warning('[ProjSummary] persist failed conv=%s: %s',
                       conv_id[:8], e)


def project_digest_entries(project_path: str,
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
        db = get_thread_db(DOMAIN_CHAT)
        rows = db.execute(
            "SELECT id, title, settings FROM conversations "
            "WHERE user_id=? AND json_extract(settings, '$.projectPath') = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (DEFAULT_USER_ID, project_path, _DIGEST_SCAN_LIMIT)).fetchall()
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
        settings = safe_json(r['settings'], default={}, label='projsummary-digest')
        ps = settings.get('projectSummary') if isinstance(settings, dict) else None
        summary = (ps.get('text') if isinstance(ps, dict) else '') or ''
        candidates.append({'id': cid, 'title': title, 'summary': summary})

    if not candidates:
        return []

    # No query → pure recency (unchanged legacy behaviour).
    if not query or not query.strip():
        return candidates[:limit]

    # Relevance-gate: BM25 over "title + summary" per candidate.
    try:
        from lib.memory.relevance import score_items
        docs = [f'{c["title"]} {c["summary"]}'.strip() for c in candidates]
        scored = score_items(query, docs)  # [(idx, score)], score>0, desc
    except Exception as e:
        logger.debug('[ProjSummary] digest relevance scoring failed: %s', e)
        return candidates[:limit]

    ordered: list[dict] = []
    seen: set[str] = set()
    # 1) Relevance-ranked positive matches first.
    for idx, _score in scored:
        c = candidates[idx]
        if c['id'] in seen:
            continue
        seen.add(c['id'])
        ordered.append(c)
        if len(ordered) >= limit:
            return ordered
    # 2) Recency floor: keep the most-recent few unconditionally so a fresh /
    #    off-topic turn is never empty (candidates is already recency-ordered).
    for c in candidates[:_DIGEST_RECENCY_FLOOR]:
        if c['id'] in seen:
            continue
        seen.add(c['id'])
        ordered.append(c)
        if len(ordered) >= limit:
            break
    return ordered[:limit]


def build_project_digest(project_path: str, current_conv_id: str | None = None,
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
            @-attached a conversation; see ``lib/tools/registry.py``
            ``_build_conv_ref``) the header is tool-free: the siblings are
            surfaced for ambient awareness ONLY, naming no tool the model can't
            actually call. Defaults to True for back-compat with direct callers.
            Both header variants share the substring ``related conversation(s)``
            so the injection-side idempotency probe (``_DIGEST_MARKER`` in
            ``lib/tasks_pkg/system_context.py``) matches either one.
    """
    structured = project_digest_entries(project_path, current_conv_id, limit,
                                        query=query)
    if not structured:
        return ''
    entries = []
    for e in structured:
        if e.get('summary'):
            entries.append(f'• "{e["title"]}" — {e["summary"]} [{e["id"]}]')
        else:
            # No summary yet (not referenced/summarized) — still surface the
            # title so the model knows the sibling exists.
            entries.append(f'• "{e["title"]}" [{e["id"]}]')

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


__all__ = [
    'ensure_summary', 'generate_summary', 'build_project_digest',
    'project_digest_entries',
    'SUMMARY_MAX_CHARS', 'SUMMARY_STALE_GROWTH', 'SUMMARY_MIN_MESSAGES',
    'DIGEST_MAX_SIBLINGS',
]
