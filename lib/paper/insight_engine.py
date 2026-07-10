"""Insight second-pass engine for Paper Reading Mode.

Runs AFTER the fidelity report is written. Where the report engine optimises for
completeness at ``temperature=0``, this pass optimises for the *uncovered* axis —
synthesis, taste, and TRANSFER — at a higher temperature, because insight is a
divergent act that temp=0 crushes.

Design contract (enforced, not aspirational — mirrors the recommend engine):

* The synthesis step is **agentic**: the model is given ``web_search`` /
  ``fetch_url`` (via the report engine's ``_execute_report_tool``) plus a
  current-date anchor and told to research what the subfield is stuck on TODAY
  before writing — a frozen-memory "future directions" list is exactly the
  regression the recommend engine already fixed.
* **The moat is transfer.** A "reader context" block (the reader's paper library
  + relevant stored memories) is injected so the pass can build concrete
  "this connects to «a paper you already read»" / "this transfers to «problem X»"
  bridges — the one thing a generic summariser cannot do.
* **Every paper it name-drops is GROUNDED** through the SAME
  ``search_arxiv`` / ``fetch_arxiv_title`` path the recommend engine uses. An
  ungrounded ref is stripped to ``null`` (the prose survives, the fake link does
  not) — an "insight" citing a hallucinated follow-up is worse than none.

Also here: :func:`score_report_rubric` — the measurement instrument shipped in
the same increment. It scores any report on four INSIGHT axes and returns strict
JSON so one-pass vs two-pass reports are a numeric diff, not a vibe.

Every failure path leaves a trace per CLAUDE.md §2.
"""

import json
import os
import re
import time

from lib.agent_loop import AbortSignal, run_agent_loop
from lib.llm_dispatch.api import dispatch_stream
from lib.llm_errors import AbortedError
from lib.log import get_logger

# Grounding: reuse the recommend engine's pure helpers (no patched deps) but do
# the actual search/verify through THIS module's ``search_arxiv`` /
# ``fetch_arxiv_title`` so tests can monkeypatch the network the same way.
from .arxiv import _extract_arxiv_id, fetch_arxiv_title, search_arxiv
from .insight_prompts import (
    RUBRIC_AXES,
    insight_system_prompt,
    rubric_prompt,
)
from .prompts import _REPORT_TOOLS, date_anchor_clause
from .recommend_engine import _norm_id, _title_grounded
from .tools import (
    _execute_report_tool,
    display_query_for,
    parse_and_repair_tool_args,
)

logger = get_logger(__name__)

# How many tool-eligible research rounds the insight agent gets before it MUST
# produce its final JSON. Enough to scan the current frontier + open a couple of
# hits, but bounded so the pass stays a cheap add-on to the report.
_MAX_INSIGHT_TOOL_ROUNDS = 6

# Above the report's temp=0 (insight is a divergent act) but NOT so high that
# strict-JSON extraction breaks: temp=0.7 failed to emit parseable JSON ~1/3 of
# real runs, which then silently returned nothing. 0.45 keeps some divergence
# while restoring reliability; the one-shot repair re-ask below (temp=0) is the
# safety net for the residual failures.
_INSIGHT_TEMPERATURE = 0.45

# Repair re-ask: cap tokens generously — this is pure JSON, no research.
_REPAIR_MAX_TOKENS = 4000

# Rubric scoring is a judgement call, not creative — keep it deterministic.
# max_tokens must comfortably clear the JSON + four passage-citing
# justifications; 1500 sat right at the ceiling and truncated → unparseable
# JSON → a spurious None. Give it ample headroom.
_RUBRIC_TEMPERATURE = 0.0
_RUBRIC_MAX_TOKENS = 3000

# Reader-context caps (kept small — this is a hint, not a corpus dump).
_CTX_LIBRARY_MAX = 8
_CTX_MEMORY_MAX = 6
_INSIGHT_LANG_PREFIX = 'insight'

# Headroom gate: the n=9 A/B showed the section-vs-report win is CONDITIONAL —
# it wins where the report's own insight rubric is LOW (all wins at overall
# baseline <= 3.9) and ties/loses where the report is already insight-saturated
# (all losses at baseline >= 4.5). The split is clean at ~4.0, fixed a-priori
# here so the gated eval isn't tuned post-hoc. The pass only fires when the
# report's OWN insight baseline is at or below this.
INSIGHT_GATE_THRESHOLD = 4.0


def insight_gate_fires(baseline_overall) -> bool:
    """Should the insight pass fire, given the report's own insight-rubric score?

    ``baseline_overall`` is the mean 4-axis rubric score of the plain report
    (arm A). Fires iff there is headroom (baseline <= INSIGHT_GATE_THRESHOLD).
    A ``None`` baseline (scoring failed) fails OPEN → fire (never silently
    withhold on an instrument error; the pass is non-destructive anyway).
    """
    if baseline_overall is None:
        return True
    return baseline_overall <= INSIGHT_GATE_THRESHOLD + 1e-9


def insight_enabled() -> bool:
    """Is the insight second-pass turned on?

    Flag-gated OFF by default so the prototype is fully non-destructive — the
    ordinary report path is byte-identical unless an operator opts in via
    ``TOFU_PAPER_INSIGHT=1``.
    """
    return (os.environ.get('TOFU_PAPER_INSIGHT', '') or '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def insight_lang_key(ui_lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted insight pass.

    ``insight:<ui_lang>`` — a separate row from the plain ``'en'`` / ``'zh'``
    report, so persisting an insight NEVER overwrites the fidelity report and
    the two can be diffed. Mirrors Review Mode's ``review:<venue>:<uilang>``.
    """
    return f'{_INSIGHT_LANG_PREFIX}:{ui_lang or "en"}'


# ═══════════════════════════════════════════════════════
#  Reader context (the transfer moat)
# ═══════════════════════════════════════════════════════

def _context_query(report_md: str, paper_text: str) -> str:
    """Derive a short relevance query from the report title + TL;DR / paper head."""
    head = (report_md or '')[:1200]
    if not head:
        head = (paper_text or '')[:800]
    return head


def _library_context(phash: str, query: str):
    """Recent OTHER papers the reader has in their library, ranked by relevance.

    Returns a list of ``{'title', 'arxiv_id'}`` dicts (best-effort; empty on any
    failure — the pass still runs without a library).
    """
    try:
        from lib.database import get_db, get_thread_db
        try:
            db = get_db()
        except RuntimeError as e:
            logger.debug('[Paper:Insight] no request-context DB, using thread DB: %s', e)
            db = get_thread_db()
        rows = db.execute(
            "SELECT title, arxiv_id, paper_hash FROM paper_library "
            "WHERE paper_hash != ? AND title != '' "
            "ORDER BY updated_at DESC LIMIT 40",
            (phash or '',)).fetchall()
    except Exception as e:
        logger.debug('[Paper:Insight] Library context unavailable: %s', e)
        return []

    items = []
    for r in rows or []:
        try:
            items.append({'title': r['title'] or '', 'arxiv_id': r['arxiv_id'] or ''})
        except Exception as e:
            logger.debug('[Paper:Insight] skipping malformed library row: %s', e)
            continue
    items = [it for it in items if it['title']]
    if not items:
        return []

    # Relevance-rank titles against the current paper so the bridge candidates
    # are topical, not just recent.
    try:
        from lib.memory.relevance import score_items
        scored = score_items(query, [it['title'] for it in items])
        if scored:
            ranked = [items[i] for i, _ in scored]
            # Keep any positive-scoring hits; if none scored, fall back to recency.
            items = ranked or items
    except Exception as e:
        logger.debug('[Paper:Insight] Library relevance rank failed: %s', e)
    return items[:_CTX_LIBRARY_MAX]


def _memory_context(query: str, project_path=None):
    """Relevant stored memories (the reader's problems/notes), ranked by BM25.

    Returns a list of ``{'name', 'description'}`` dicts (best-effort; empty on
    any failure).
    """
    try:
        from lib.memory.relevance import filter_relevant_memories
        from lib.memory.storage import get_eligible_memories
        mems = get_eligible_memories(project_path)
        if not mems:
            return []
        top = filter_relevant_memories(mems, query, top_k=_CTX_MEMORY_MAX)
    except Exception as e:
        logger.debug('[Paper:Insight] Memory context unavailable: %s', e)
        return []
    out = []
    for m in top[:_CTX_MEMORY_MAX]:
        name = (m.get('name') or '').strip()
        desc = (m.get('description') or '').strip()
        if name or desc:
            out.append({'name': name, 'description': desc})
    return out


def _build_reader_context(phash, report_md, paper_text, ui_lang, project_path=None):
    """Assemble the "reader context" block injected into the insight prompt.

    Empty string when the reader has no library / memories — the prompt tells
    the model to be honest about that rather than manufacture a link.
    """
    query = _context_query(report_md, paper_text)
    library = _library_context(phash, query)
    memories = _memory_context(query, project_path)
    if not library and not memories:
        return ''

    zh = ui_lang == 'zh'
    lines = ['## READER CONTEXT (for transfer — do NOT restate; use to build bridges)'
             if not zh else
             '## 读者背景（用于迁移——不要复述；用来搭桥）']
    if library:
        lines.append('\n### Papers the reader has already read (their library):'
                     if not zh else '\n### 读者已经读过的论文（他的文库）：')
        for it in library:
            aid = f" (arXiv:{it['arxiv_id']})" if it.get('arxiv_id') else ''
            lines.append(f"- {it['title']}{aid}")
    if memories:
        lines.append('\n### Problems / notes the reader cares about (their memory store):'
                     if not zh else '\n### 读者关心的问题/笔记（他的记忆库）：')
        for m in memories:
            desc = f" — {m['description']}" if m.get('description') else ''
            lines.append(f"- {m['name']}{desc}")
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════
#  Grounding (the anti-hallucination gate — same guarantee as recommend)
# ═══════════════════════════════════════════════════════

def _self_identity(phash, report_md, self_arxiv_id=None, self_title=None):
    """Resolve the identity (arxiv_id, title) of the paper UNDER ANALYSIS.

    Needed by the self-reference guard: a "connection" whose target IS this
    paper is vacuous (the bd79f6/Transformer failure — a foundational paper's
    library descendants make the model bridge the paper back to itself). Tries
    explicit args → the paper_library row for ``phash`` → the report's own head
    (Paper Card title + any arXiv id). Best-effort; either field may be ''.
    """
    aid = _extract_arxiv_id(str(self_arxiv_id)) if self_arxiv_id else None
    title = (self_title or '').strip()
    if aid and title:
        return aid, title

    if phash and (not aid or not title):
        try:
            from lib.database import get_db, get_thread_db
            try:
                db = get_db()
            except RuntimeError as e:
                logger.debug('[Paper:Insight] no request-context DB, using thread DB: %s', e)
                db = get_thread_db()
            row = db.execute(
                "SELECT title, arxiv_id FROM paper_library WHERE paper_hash = ? LIMIT 1",
                (phash,)).fetchone()
            if row:
                if not title:
                    title = (row['title'] or '').strip()
                if not aid and row['arxiv_id']:
                    aid = _extract_arxiv_id(str(row['arxiv_id']))
        except Exception as e:
            logger.debug('[Paper:Insight] self-identity DB lookup failed: %s', e)

    head = (report_md or '')[:2500]
    if not title:
        m = re.search(r'^#\s+(.+?)\s*$', head, re.MULTILINE)
        if m:
            title = m.group(1).strip()
    if not aid:
        aid = _extract_arxiv_id(head)
    return (aid or None), (title or '')


def _is_self_reference(ref, conn_text, self_aid, self_title):
    """Does this connection's target refer to the paper UNDER ANALYSIS itself?

    Checked on the CLAIMED ref (before grounding), because grounding a
    self-titled ref can fuzzy-match a spurious paper (bd79f6 grounded
    "Attention Is All You Need" to a title-meme paper) and thereby hide the
    vacuity. Three signals:
      1. claimed arXiv id == the paper's own id;
      2. claimed title strongly matches the paper's own title;
      3. circular prose — the paper's own title named 2+ times in the bridge
         text (the "X is a generalized form of X itself" degenerate form).
    """
    if isinstance(ref, dict):
        if self_aid:
            claimed = _extract_arxiv_id(str(ref.get('arxiv_id'))) if ref.get('arxiv_id') else None
            if claimed and _norm_id(claimed) == _norm_id(self_aid):
                return True
        if self_title:
            rt = (ref.get('title') or '').strip()
            if rt and _title_grounded(rt, self_title):
                return True
    if self_title and conn_text:
        st = self_title.lower()
        if len(st) >= 8 and conn_text.lower().count(st) >= 2:
            return True
    return False


# ═══════════════════════════════════════════════════════
#  Grounding (the anti-hallucination gate — same guarantee as recommend)
# ═══════════════════════════════════════════════════════

def _ground_ref(ref):
    """Verify a ``{title, arxiv_id}`` ref against arXiv; return a card or None.

    Reuses the recommend engine's pure matching helpers but drives the search
    through THIS module's ``search_arxiv`` / ``fetch_arxiv_title`` (so tests
    patch one namespace). A ref that cannot be grounded is dropped (logged) —
    the prose that mentioned it survives, but the clickable/verifiable link does
    not, so a hallucinated paper never reaches the reader as fact.
    """
    if not isinstance(ref, dict):
        return None
    title = (ref.get('title') or '').strip()
    raw_id = ref.get('arxiv_id')
    claimed_id = _extract_arxiv_id(str(raw_id)) if raw_id else None
    if not title and not claimed_id:
        return None

    results = search_arxiv(title, max_results=5) if title else []
    if claimed_id:
        for r in results:
            if _norm_id(r.get('arxiv_id')) == _norm_id(claimed_id):
                return {'title': r.get('title') or title, 'arxiv_id': r.get('arxiv_id'),
                        'abs_url': r.get('abs_url') or f'https://arxiv.org/abs/{r.get("arxiv_id")}'}
    if results and _title_grounded(title, results[0].get('title', '')):
        r = results[0]
        return {'title': r.get('title') or title, 'arxiv_id': r.get('arxiv_id'),
                'abs_url': r.get('abs_url') or f'https://arxiv.org/abs/{r.get("arxiv_id")}'}
    if claimed_id:
        real_title = fetch_arxiv_title(claimed_id)
        if real_title:
            return {'title': real_title, 'arxiv_id': claimed_id,
                    'abs_url': f'https://arxiv.org/abs/{claimed_id}'}
    logger.debug('[Paper:Insight] Dropped ungrounded ref: %.120s (claimed id=%s)',
                 title or '(no title)', claimed_id)
    return None


def _ground_insight(insight, self_aid=None, self_title=''):
    """Ground every paper ref in a parsed insight dict, in place.

    Mutates ``connections[].paper`` and ``open_problems[].grounded_by`` to either
    a grounded card (real title + arxiv_id + abs_url) or ``None``. Returns
    ``(grounded_count, dropped_count, selfref_count)``.

    SELF-REFERENCE GUARD (fix for the foundational-paper backfire): a connection
    whose target IS the paper under analysis is VACUOUS — grounding proves a ref
    exists, not that the bridge is non-vacuous. Such connections are removed
    ENTIRELY (not just their link nulled) because the surrounding prose is the
    circular "X is a generalized form of X itself" text, which is worse than no
    connection. Detection runs on the CLAIMED ref BEFORE grounding, since
    grounding a self-titled ref can fuzzy-match a spurious paper and hide it.
    """
    grounded = 0
    dropped = 0
    selfref = 0
    if not isinstance(insight, dict):
        return (0, 0, 0)

    conns = insight.get('connections') or []
    kept_conns = []
    for conn in conns:
        if not isinstance(conn, dict):
            continue
        ref = conn.get('paper')
        text = conn.get('text') or ''
        if (self_aid or self_title) and _is_self_reference(ref, text, self_aid, self_title):
            selfref += 1
            logger.info('[Paper:Insight] Dropped self-referential connection: %.100s', text)
            continue
        if isinstance(ref, dict) and (ref.get('title') or ref.get('arxiv_id')):
            card = _ground_ref(ref)
            conn['paper'] = card
            if card:
                grounded += 1
            else:
                dropped += 1
        kept_conns.append(conn)
    if isinstance(insight.get('connections'), list):
        insight['connections'] = kept_conns

    for op in insight.get('open_problems') or []:
        if not isinstance(op, dict):
            continue
        ref = op.get('grounded_by')
        if isinstance(ref, dict) and (ref.get('title') or ref.get('arxiv_id')):
            if (self_aid or self_title) and _is_self_reference(ref, op.get('text') or '',
                                                               self_aid, self_title):
                op['grounded_by'] = None
                selfref += 1
                continue
            card = _ground_ref(ref)
            op['grounded_by'] = card
            if card:
                grounded += 1
            else:
                dropped += 1

    return (grounded, dropped, selfref)


# ═══════════════════════════════════════════════════════
#  JSON extraction (shared shape with recommend)
# ═══════════════════════════════════════════════════════

def _parse_llm_json(content):
    """Extract the first JSON object from an LLM reply (tolerates code fences)."""
    if not content:
        return None
    text = content.strip()
    fence = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = text.find('{')
        if brace > 0:
            text = text[brace:]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Paper:Insight] LLM reply was not parseable JSON: %s', e)
        return None


_REPAIR_INSTRUCTION = (
    'Your previous reply could not be parsed as JSON. Do NOT research further and '
    'do NOT add any prose, explanation, apology, or code fences. Reply with ONLY '
    'the single JSON object described earlier — starting with { and ending with } '
    'and nothing else.'
)


def _repair_json_reask(messages, bad_content, *, model, abort_signal):
    """One-shot recovery when the final synthesis content isn't parseable JSON.

    Re-asks the SAME conversation (so the model still has all its research +
    reader context in-context) with a strict "return ONLY the JSON object"
    instruction at temperature 0 and NO tools — a deterministic reformat of what
    it already produced, not a fresh generation. Returns the parsed dict or None.

    This is the safety net for the residual JSON failures that survive the
    lowered generation temperature; without it a prose-wrapped or truncated
    reply makes the whole feature silently no-op.
    """
    reask = list(messages)
    # Feed back what it actually said so the model reformats THAT, not re-invents.
    reask.append({'role': 'assistant', 'content': (bad_content or '')[:6000]})
    reask.append({'role': 'user', 'content': _REPAIR_INSTRUCTION})
    buf = {'content': ''}

    def _on_content(text):
        buf['content'] += text

    try:
        msg, _finish, _usage = dispatch_stream(
            reask,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            prefer_model=model or None,
            strict_model=bool(model),
            capability='text',
            max_tokens=_REPAIR_MAX_TOKENS,
            temperature=0.0,
            thinking_enabled=False,
            log_prefix='[Paper:Insight:Repair]',
        )
    except AbortedError:
        raise
    except Exception as e:
        logger.warning('[Paper:Insight:Repair] Re-ask dispatch failed: %s', e)
        return None

    content = buf['content'] or (msg.get('content') if isinstance(msg, dict) else '') or ''
    parsed = _parse_llm_json(content)
    if isinstance(parsed, dict):
        logger.info('[Paper:Insight:Repair] Recovered JSON via one-shot re-ask (%d chars)',
                    len(content))
        return parsed
    logger.warning('[Paper:Insight:Repair] Re-ask still did not yield parseable JSON')
    return None


# ═══════════════════════════════════════════════════════
#  The agentic synthesis pass
# ═══════════════════════════════════════════════════════

def _research_and_synthesize(paper_text, report_md, reader_context, ui_lang, *,
                             model=None, abort=None, on_tool_event=None):
    """Agentic insight synthesis: research the frontier, then return the model's
    structured insight JSON (ungrounded — the caller grounds it).

    Runs the shared tool-calling loop (``web_search`` / ``fetch_url`` via the
    report engine's ``_execute_report_tool``) at higher temperature with a
    date-anchored system prompt. This is the single seam tests monkeypatch.

    Raises:
        AbortedError: loop aborted mid-dispatch (caller treats as clean empty).
        Exception: hard LLM dispatch failure (caller flags an error).
    """
    system = date_anchor_clause(ui_lang) + insight_system_prompt(ui_lang)
    # The paper text is truncated by the caller; the report is the primary
    # material for synthesis (it already distilled the paper).
    user_parts = []
    if reader_context:
        user_parts.append(reader_context)
    user_parts.append('## THE EXPLAINER REPORT (already written — synthesize on top, do not restate)\n\n'
                      + (report_md or ''))
    if paper_text:
        user_parts.append('## PAPER TEXT (reference)\n\n' + paper_text)
    user_content = '\n\n---\n\n'.join(user_parts)

    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user_content},
    ]
    abort_signal = AbortSignal.from_callback(abort)
    user_question = (report_md or paper_text or '')[:300]

    _round = {'content': ''}
    _last = {'msg': None}
    _round_counter = {'n': 0}
    model_name = model or None

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            _round['content'] += text

        logger.info('[Paper:Insight] Synthesis round %d — msgs=%d tools=%s',
                    rnd + 1, len(messages), 'yes' if tools else 'no')
        return dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            prefer_model=model_name if model else None,
            strict_model=bool(model),
            capability='text',
            tools=tools,
            max_tokens=8000,
            temperature=_INSIGHT_TEMPERATURE,
            thinking_enabled=False,
            log_prefix='[Paper:Insight]',
        )

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg

    def _begin_tool_round(rnd, msg):
        # This round issued tool calls → its prose is interim scaffolding, not
        # the final JSON. Drop it and append the assistant turn.
        _round['content'] = ''
        messages.append(msg)

    def _execute_tool(rnd, tc):
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')
        fn_args, _ = parse_and_repair_tool_args(fn_name, fn_args_raw)
        _round_counter['n'] += 1
        rn = _round_counter['n']
        display_query = display_query_for(fn_name, fn_args)

        if on_tool_event:
            on_tool_event({
                'type': 'tool_start', 'roundNum': rn, 'toolName': fn_name,
                'query': display_query, 'toolCallId': tc_id,
            })

        tool_t0 = time.time()
        result, display_results, search_diag, engine_breakdown, verticals = _execute_report_tool(
            fn_name, fn_args_raw, user_question=user_question, abort=abort_signal.is_set)
        tool_elapsed = time.time() - tool_t0
        logger.info('[Paper:Insight:Tool] %s → %d chars in %.1fs',
                    fn_name, len(result), tool_elapsed)

        if on_tool_event:
            done_ev = {
                'type': 'tool_done', 'roundNum': rn, 'toolName': fn_name,
                'toolCallId': tc_id, 'elapsed': round(tool_elapsed, 1),
                'results': display_results,
            }
            if engine_breakdown:
                done_ev['engineBreakdown'] = engine_breakdown
            if verticals:
                done_ev['verticals'] = verticals
            on_tool_event(done_ev)

        messages.append({
            'role': 'tool', 'tool_call_id': tc_id, 'content': result[:30000],
        })

    run_agent_loop(
        abort=abort_signal,
        max_tool_rounds=_MAX_INSIGHT_TOOL_ROUNDS,
        round_tools=_REPORT_TOOLS,
        dispatch=_dispatch,
        execute_tool=_execute_tool,
        on_round_result=_on_round_result,
        on_tool_round=_begin_tool_round,
    )

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''

    parsed = _parse_llm_json(content)
    if isinstance(parsed, dict):
        return parsed

    # The final content wasn't parseable JSON (prose-wrapped / truncated / fenced
    # in a way the extractor missed). Recover with one deterministic re-ask
    # rather than silently returning nothing.
    logger.info('[Paper:Insight] Final content unparseable — attempting one-shot JSON repair')
    return _repair_json_reask(messages, content, model=model_name if model else None,
                              abort_signal=abort_signal)


# ═══════════════════════════════════════════════════════
#  Rendering
# ═══════════════════════════════════════════════════════

_HEADINGS = {
    'en': {
        'section': '## 💡 Insight & Ideas',
        'thesis': '### The Bet',
        'connections': '### Connections to Your Reading',
        'opinion': '### A Take',
        'open': '### Open Problems Worth Your Monday',
        'prov': '### Provocations',
    },
    'zh': {
        'section': '## 💡 洞见与灵感',
        'thesis': '### 这篇论文的赌注',
        'connections': '### 与你读过的工作的联系',
        'opinion': '### 一个观点',
        'open': '### 值得你周一动手的开放问题',
        'prov': '### 挑衅式追问',
    },
}


def _ref_md(card):
    """Render a grounded ref as a Markdown link, or '' when ungrounded/absent."""
    if not isinstance(card, dict) or not card.get('arxiv_id'):
        return ''
    url = card.get('abs_url') or f'https://arxiv.org/abs/{card["arxiv_id"]}'
    title = card.get('title') or card['arxiv_id']
    return f' ([{title}]({url}))'


def render_insight_markdown(insight, ui_lang='en'):
    """Render a grounded insight dict to a Markdown section.

    Blockquote callouts (``> Key takeaway:`` / ``> 关键结论：``) reuse the
    report renderer's styled-callout convention. Grounded refs render as inline
    arXiv links; ungrounded refs render as prose only.
    """
    if not isinstance(insight, dict):
        return ''
    h = _HEADINGS.get(ui_lang, _HEADINGS['en'])
    zh = ui_lang == 'zh'
    out = [h['section'], '']

    thesis = (insight.get('thesis') or '').strip()
    if thesis:
        kw = '关键结论：' if zh else 'Key takeaway:'
        out += [h['thesis'], '', f'> {kw} {thesis}', '']

    conns = [c for c in (insight.get('connections') or []) if isinstance(c, dict) and (c.get('text') or '').strip()]
    if conns:
        out += [h['connections'], '']
        for c in conns:
            out.append(f"- {c['text'].strip()}{_ref_md(c.get('paper'))}")
        out.append('')

    opinion = (insight.get('opinion') or '').strip()
    if opinion:
        out += [h['opinion'], '', opinion, '']

    ops = [o for o in (insight.get('open_problems') or []) if isinstance(o, dict) and (o.get('text') or '').strip()]
    if ops:
        out += [h['open'], '']
        for o in ops:
            out.append(f"- {o['text'].strip()}{_ref_md(o.get('grounded_by'))}")
        out.append('')

    provs = [p for p in (insight.get('provocations') or []) if isinstance(p, str) and p.strip()]
    if provs:
        out += [h['prov'], '']
        for p in provs:
            out.append(f"- {p.strip()}")
        out.append('')

    return '\n'.join(out).rstrip() + '\n'


# ═══════════════════════════════════════════════════════
#  Public entry — generate insight
# ═══════════════════════════════════════════════════════

def generate_insight(paper_text, report_md, ui_lang='en', *, phash='',
                     model=None, project_path=None, abort=None, on_tool_event=None,
                     self_arxiv_id=None, self_title=None, allow_personal_context=True):
    """Produce a grounded insight section for a paper, given its report.

    Args:
        paper_text: full (or truncated) paper text — reference material.
        report_md: the already-written fidelity report — the primary material.
        ui_lang: 'en' | 'zh'.
        phash: paper hash (used to exclude the current paper from the library
            context and — by the caller — to persist).
        model: optional model override.
        project_path: optional project path for memory scoping.
        abort: optional zero-arg predicate (trips the loop's abort checks).
        on_tool_event: optional callback for research tool_start/tool_done.
        self_arxiv_id, self_title: identity of the paper under analysis, used to
            suppress self-referential connections (a foundational paper's own
            descendants tempt the model to bridge it back to itself). Resolved
            from phash/report when not given.
        allow_personal_context: when False (fail-closed default on every
            headless/BYO surface — see lib/agent_core/personal_scope), the
            operator-personal "reader context" (their paper library + memory
            store) is NOT injected, so one operator's reading history never
            leaks into an unrelated caller's paper analysis. The pass still
            runs; it just loses its personal transfer bridges.

    Returns:
        {
          'insight': <grounded insight dict> | None,
          'markdown': <rendered section str>,
          'grounded': int, 'dropped': int, 'selfref': int,
          'llmError': bool,
        }
    """
    out = {'insight': None, 'markdown': '', 'grounded': 0, 'dropped': 0,
           'selfref': 0, 'llmError': False}
    if not (report_md or '').strip() and not (paper_text or '').strip():
        logger.warning('[Paper:Insight] Nothing to synthesize (empty report + paper)')
        return out

    if allow_personal_context:
        reader_context = _build_reader_context(phash, report_md, paper_text, ui_lang, project_path)
    else:
        # Headless / BYO surface: never splice the operator's library+memories
        # into an unrelated caller's analysis (personal_scope fail-closed).
        reader_context = ''
        logger.info('[Paper:Insight] hash=%s — personal reader-context SUPPRESSED '
                    '(headless / opt-out)', phash)

    try:
        insight = _research_and_synthesize(
            paper_text, report_md, reader_context, ui_lang,
            model=model, abort=abort, on_tool_event=on_tool_event)
    except AbortedError:
        logger.info('[Paper:Insight] Synthesis aborted for hash=%s', phash)
        return out
    except Exception as e:
        logger.error('[Paper:Insight] Synthesis failed for hash=%s: %s', phash, e, exc_info=True)
        out['llmError'] = True
        return out

    if not isinstance(insight, dict):
        logger.warning('[Paper:Insight] No usable insight JSON for hash=%s', phash)
        return out

    self_aid, self_ttl = _self_identity(phash, report_md, self_arxiv_id, self_title)
    grounded, dropped, selfref = _ground_insight(insight, self_aid=self_aid, self_title=self_ttl)
    out['insight'] = insight
    out['grounded'] = grounded
    out['dropped'] = dropped
    out['selfref'] = selfref
    out['markdown'] = render_insight_markdown(insight, ui_lang)
    logger.info('[Paper:Insight] hash=%s — %d grounded / %d dropped / %d self-ref dropped, %d chars',
                phash, grounded, dropped, selfref, len(out['markdown']))
    return out


# ═══════════════════════════════════════════════════════
#  Rubric critic — the measurement instrument
# ═══════════════════════════════════════════════════════

def _coerce_score(v):
    """Clamp a rubric score to an int in [1, 5]; None on garbage."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError) as e:
        logger.debug('[Paper:Insight] non-numeric rubric score %r (->None): %s', v, e)
        return None
    return max(1, min(5, n))


def score_report_rubric(report_md, *, model=None, abort=None):
    """Score a report on the four INSIGHT axes. Returns a parseable verdict.

    A single no-tool dispatch at temp=0 — a judgement, not a creative act. This
    is the A/B instrument: run it on the plain report AND on report+insight and
    diff ``overall`` to see whether pass 2 moved the needle.

    Returns:
        {
          'scores': {axis: 1-5, ...},   # all four axes, coerced
          'overall': float,             # mean of the four (recomputed, trusted)
          'justifications': {axis: str},
          'one_line_verdict': str,
          'raw': <parsed model json>,   # for debugging
        }
        or None on dispatch/parse failure (logged).
    """
    report_md = (report_md or '').strip()
    if not report_md:
        logger.warning('[Paper:Insight:Rubric] Empty report — nothing to score')
        return None

    messages = [{'role': 'user', 'content': rubric_prompt(report_md)}]
    abort_signal = AbortSignal.from_callback(abort)
    buf = {'content': ''}

    def _on_content(text):
        buf['content'] += text

    try:
        msg, finish, usage = dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            prefer_model=model or None,
            strict_model=bool(model),
            capability='text',
            max_tokens=_RUBRIC_MAX_TOKENS,
            temperature=_RUBRIC_TEMPERATURE,
            thinking_enabled=False,
            log_prefix='[Paper:Insight:Rubric]',
        )
    except AbortedError:
        logger.info('[Paper:Insight:Rubric] Scoring aborted')
        return None
    except Exception as e:
        logger.error('[Paper:Insight:Rubric] Scoring dispatch failed: %s', e, exc_info=True)
        return None

    content = buf['content'] or (msg.get('content') if isinstance(msg, dict) else '') or ''
    parsed = _parse_llm_json(content)
    if not isinstance(parsed, dict):
        logger.warning('[Paper:Insight:Rubric] Unparseable rubric reply')
        return None

    raw_scores = parsed.get('scores') if isinstance(parsed.get('scores'), dict) else {}
    scores = {}
    for axis in RUBRIC_AXES:
        s = _coerce_score(raw_scores.get(axis))
        if s is not None:
            scores[axis] = s
    if not scores:
        logger.warning('[Paper:Insight:Rubric] No valid axis scores in reply')
        return None

    # Recompute the mean ourselves — never trust the model's arithmetic.
    overall = round(sum(scores.values()) / len(scores), 2)
    justifs = parsed.get('justifications') if isinstance(parsed.get('justifications'), dict) else {}
    verdict = (parsed.get('one_line_verdict') or '').strip()
    logger.info('[Paper:Insight:Rubric] scores=%s overall=%.2f', scores, overall)
    return {
        'scores': scores,
        'overall': overall,
        'justifications': {k: str(v) for k, v in justifs.items()},
        'one_line_verdict': verdict,
        'raw': parsed,
    }


# ═══════════════════════════════════════════════════════
#  Orchestration — the gated, persisted report-path entry
# ═══════════════════════════════════════════════════════

def _persist_insight(phash, ui_lang, markdown, model):
    """Persist an insight section under the ``insight:<ui_lang>`` key.

    Reuses the report engine's EXACT write-path (``upsert(db, PAPER_REPORTS,
    …)``) — no hand-rolled second writer — so the insight row obeys the same
    schema, upsert semantics, and PG/SQLite bridge as every other paper_reports
    row. The composite lang key keeps it a SEPARATE row from the plain report,
    so it never overwrites the fidelity report.
    """
    try:
        from lib.database import get_thread_db
        from lib.database._core_schema import PAPER_REPORTS, upsert
        db = get_thread_db()
        upsert(db, PAPER_REPORTS, {
            'paper_hash': phash,
            'lang': insight_lang_key(ui_lang),
            'report': markdown,
            'model': model or '',
            'meta': json.dumps({'kind': 'insight'}, ensure_ascii=False),
            'created_at': int(time.time()),
        }, retry=True)
        logger.info('[Paper:Insight] Persisted insight — hash=%s key=%s %d chars',
                    phash, insight_lang_key(ui_lang), len(markdown))
        return True
    except Exception as e:
        logger.warning('[Paper:Insight] Failed to persist insight hash=%s: %s', phash, e)
        return False


def run_report_insight(paper_text, report_md, ui_lang='en', *, phash='',
                       model=None, project_path=None, abort=None, on_tool_event=None,
                       self_arxiv_id=None, self_title=None, allow_personal_context=True,
                       persist=True):
    """Gated, persisted insight pass — the report-path entry point.

    Pipeline (the production shape validated by the gated A/B):
      1. Score the just-written report on the 4 insight axes (ONE no-tool rubric
         call — the gate input).
      2. HEADROOM GATE: only proceed when the report's own insight baseline is
         at/below ``INSIGHT_GATE_THRESHOLD`` (the pass helps low-baseline
         explainers, no-ops/hurts insight-saturated ones — proven at n=7 fired,
         CI lower 1.00). A ``None`` baseline fails OPEN.
      3. Generate the grounded insight (self-ref-suppressed, personal-context
         gated by ``allow_personal_context``).
      4. Persist under ``insight:<ui_lang>`` reusing the report write-path.

    Returns:
        {
          'fired': bool,           # did the gate let it run?
          'baseline': float|None,  # the report's own insight-rubric overall
          'insight': dict|None, 'markdown': str,
          'grounded': int, 'dropped': int, 'selfref': int,
          'persisted': bool, 'llmError': bool,
        }
    """
    out = {'fired': False, 'baseline': None, 'insight': None, 'markdown': '',
           'grounded': 0, 'dropped': 0, 'selfref': 0, 'persisted': False,
           'llmError': False}

    baseline = None
    verdict = score_report_rubric(report_md, model=model, abort=abort)
    if verdict:
        baseline = verdict['overall']
    out['baseline'] = baseline

    if not insight_gate_fires(baseline):
        logger.info('[Paper:Insight] Gate WITHHELD — hash=%s baseline=%.2f > %.1f '
                    '(report already insight-saturated)', phash, baseline or -1,
                    INSIGHT_GATE_THRESHOLD)
        return out

    out['fired'] = True
    logger.info('[Paper:Insight] Gate FIRED — hash=%s baseline=%s (<= %.1f)',
                phash, f'{baseline:.2f}' if baseline is not None else 'None',
                INSIGHT_GATE_THRESHOLD)

    gen = generate_insight(
        paper_text, report_md, ui_lang, phash=phash, model=model,
        project_path=project_path, abort=abort, on_tool_event=on_tool_event,
        self_arxiv_id=self_arxiv_id, self_title=self_title,
        allow_personal_context=allow_personal_context)

    out.update({k: gen[k] for k in ('insight', 'markdown', 'grounded', 'dropped',
                                    'selfref', 'llmError')})

    if persist and gen.get('markdown') and gen.get('insight'):
        out['persisted'] = _persist_insight(phash, ui_lang, gen['markdown'], model)
    return out
