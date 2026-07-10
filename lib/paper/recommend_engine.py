"""Describe-to-recommend engine for Paper Reading Mode.

The user often remembers *what a paper was about* but not its title (or even
mis-remembers a premise — "a diffusion LM won a NeurIPS award"). This engine
turns that fuzzy free-text description into a ranked list of **real, addable**
arXiv papers.

Design contract (enforced, not aspirational):

* The interpretation step is now **agentic**: instead of guessing candidate
  titles from the model's frozen training memory (which cannot know about a
  conference happening *today* or papers posted last week), the model is given
  the project's own ``web_search`` / ``fetch_url`` tools and told to actually
  RESEARCH the current literature — search arXiv / the web, open the promising
  hits, verify any venue/award claim against a real source — BEFORE it proposes
  candidates. A "current date" anchor is injected so it never treats an
  in-progress year as the future. The final turn returns strict JSON
  (candidates + optional correction), exactly the schema the grounding stage
  below consumes. This is what lets genuinely recent work surface.
* **No card is ever surfaced unless its arXiv ID resolves through the existing
  ``search_arxiv`` / ``fetch_arxiv_title`` path to a real paper.** A title the
  model produced but that cannot be grounded is dropped, logged at debug, and
  never rendered. This gate is unchanged by the agentic upgrade — it is what
  prevents surfacing a hallucinated paper.
* The interpretive prose (``why`` / correction ``note``) is model text by
  design; the *papers* it points at are always grounded.

Every failure path leaves a trace per CLAUDE.md §2 — LLM call failure, JSON
parse failure, grounding miss, and empty result are all logged.
"""

import json
import re
import time

from lib.agent_loop import AbortSignal, run_agent_loop
from lib.llm_dispatch.api import dispatch_stream
from lib.llm_errors import AbortedError
from lib.log import get_logger

from .arxiv import _extract_arxiv_id, fetch_arxiv_title, search_arxiv
from .prompts import _REPORT_TOOLS, date_anchor_clause
from .tools import (
    _execute_report_tool,
    display_query_for,
    parse_and_repair_tool_args,
)

logger = get_logger(__name__)

# The model may over-propose; we ground up to this many before stopping so a
# few hallucinated titles don't starve the real ones.
_GROUND_ATTEMPT_MULTIPLIER = 2
_GROUND_SEARCH_DEPTH = 5

# How many tool-eligible research rounds the interpretation agent gets before
# it MUST produce its final JSON. Enough to search, open a couple of hits, and
# verify a venue/award claim — but bounded so the describe box stays snappy.
_MAX_RECOMMEND_TOOL_ROUNDS = 5

# Tokens too generic to count toward a title-match (a lone "models" / "learning"
# overlap must not ground an unrelated paper).
_STOPWORDS = frozenset({
    'a', 'an', 'the', 'of', 'for', 'and', 'or', 'to', 'in', 'on', 'via', 'with',
    'is', 'are', 'be', 'model', 'models', 'learning', 'network', 'networks',
    'neural', 'deep', 'using', 'towards', 'toward', 'new', 'from',
})

_RECOMMEND_SYSTEM = (
    "You are a research-librarian assistant for a paper-reading app. The user "
    "describes a paper (or a few papers) from memory — often vaguely, sometimes "
    "with a MISTAKEN PREMISE (e.g. claiming a certain kind of paper won an award "
    "when it did not, or getting the year/venue wrong). Your job is to identify "
    "the REAL arXiv papers they most likely mean.\n\n"
    "**You MUST research before answering — do NOT rely on memory alone.** Your "
    "training data is stale: a conference the user mentions may be happening "
    "right now or already past, and the papers they mean may have been posted "
    "very recently. Use the provided tools to find the ACTUAL current papers:\n"
    "  1. web_search — search arXiv and the web for the topic/venue/award the "
    "user describes. Prefer ``vertical='academic'`` for paper topics, and pass "
    "``freshness='month'``/``'year'`` when the user implies recency. Run a few "
    "targeted queries (e.g. the topic + the venue+year, and the specific "
    "award/track if one is claimed).\n"
    "  2. fetch_url — open the most promising results (an arXiv listing, an "
    "awards page, a paper's abs page) to confirm titles, arXiv IDs, and any "
    "venue/award claim BEFORE you commit to it. Never assert an award/venue you "
    "did not see on a real page.\n"
    "Do this research first; only then produce your final answer. If the user's "
    "premise is contradicted by what you find, that goes in ``correction``.\n\n"
    "When you are done researching, respond with STRICT JSON ONLY (no prose, no "
    "code fences) as your FINAL message, with this schema:\n"
    "{\n"
    '  "candidates": [\n'
    '    {"title": "<exact paper title as found>",\n'
    '     "arxiv_id": "<arXiv id like 2502.09992 if known, else null>",\n'
    '     "venue": "<short label like \\"ICML 2025 Oral\\" if you VERIFIED it, '
    'else null>",\n'
    '     "why": "<ONE sentence, <=140 chars, tying this paper to the user\'s '
    'description>"}\n'
    "  ],\n"
    '  "correction": null OR {\n'
    '     "note": "<if the description contains a factual mistake, one or two '
    'sentences stating the correction and what is actually true>",\n'
    '     "paper": {"title": "...", "arxiv_id": "..."} OR null\n'
    "  }\n"
    "}\n\n"
    "Rules:\n"
    "- Only include papers you confirmed are REAL arXiv papers via the tools. "
    "Give the arxiv_id whenever you saw it.\n"
    "- Order candidates most-likely / most-relevant first.\n"
    "- Set correction ONLY when the user's premise is actually wrong (verified), "
    "otherwise null. Put the real award/venue winner in correction.paper when "
    "relevant.\n"
    "- Write \"why\" and \"note\" in the SAME language as the user's description.\n"
    "- Do NOT invent arXiv IDs. If unsure of the id, give the title and leave "
    "arxiv_id null — the app will resolve it.\n"
    "- Your FINAL message must be the JSON object and nothing else."
)


def _detect_lang(text):
    """Rough UI-language guess for the date-anchor clause: zh if any CJK char."""
    for ch in (text or ''):
        if '\u4e00' <= ch <= '\u9fff':
            return 'zh'
    return 'en'


def _norm_id(arxiv_id):
    """Strip a version suffix so ``2502.09992v3`` and ``2502.09992`` compare equal."""
    return (arxiv_id or '').split('v')[0].strip()


def _title_tokens(s):
    """Significant lowercase token set of a title (stopwords removed)."""
    toks = set(re.findall(r'[a-z0-9]+', (s or '').lower()))
    return {t for t in toks if t not in _STOPWORDS and len(t) > 1}


def _title_grounded(claimed_title, real_title):
    """Is ``real_title`` a plausible match for the model's ``claimed_title``?

    Guards against ``search_arxiv`` returning a tangential top hit for a title
    the model half-remembered. Requires a meaningful significant-token overlap
    (not a single generic word).
    """
    ct = _title_tokens(claimed_title)
    rt = _title_tokens(real_title)
    if not ct or not rt:
        return False
    shared = ct & rt
    # Need at least 2 shared significant tokens AND >=40% of the (shorter) claim.
    need = max(2, round(0.4 * len(ct)))
    return len(shared) >= min(need, len(ct)) and len(shared) >= 2


def _card_from_result(result, why='', venue=''):
    """Build a recommend card (search_arxiv result + why/venue annotations)."""
    card = dict(result)
    card['why'] = (why or '').strip()
    card['venue'] = (venue or '').strip()
    return card


def _ground_candidate(cand):
    """Resolve one model-proposed candidate to a REAL arXiv paper, or None.

    Grounding path (reuses the existing arXiv seam only):
      1. Search arXiv by the proposed title → full metadata. If the model gave
         an arxiv_id, prefer the search hit whose id matches; otherwise accept
         the top hit only when its title plausibly matches (``_title_grounded``).
      2. Fallback: if the model gave an arxiv_id that search didn't surface,
         verify the id directly via ``fetch_arxiv_title`` and build a
         title-only card.
      3. Otherwise drop (logged at debug) — an ungrounded card is NEVER shown.
    """
    title = (cand.get('title') or '').strip()
    raw_id = cand.get('arxiv_id')
    claimed_id = _extract_arxiv_id(raw_id) if raw_id else None
    why = cand.get('why') or ''
    venue = cand.get('venue') or ''

    results = search_arxiv(title, max_results=_GROUND_SEARCH_DEPTH) if title else []

    # 1a. Exact id match among search hits (strongest signal).
    if claimed_id:
        for r in results:
            if _norm_id(r.get('arxiv_id')) == _norm_id(claimed_id):
                return _card_from_result(r, why, venue)

    # 1b. Title-plausible top hit.
    if results and _title_grounded(title, results[0].get('title', '')):
        return _card_from_result(results[0], why, venue)

    # 2. Verify a claimed id directly (title-only card) when search missed it.
    if claimed_id:
        real_title = fetch_arxiv_title(claimed_id)
        if real_title:
            logger.debug('[Paper:Recommend] Grounded %s via direct id verify (search missed)',
                         claimed_id)
            return _card_from_result({
                'arxiv_id': claimed_id,
                'title': real_title,
                'authors': [],
                'summary': '',
                'published': '',
                'primary_category': '',
                'pdf_url': f'https://arxiv.org/pdf/{claimed_id}.pdf',
                'abs_url': f'https://arxiv.org/abs/{claimed_id}',
            }, why, venue)

    logger.debug('[Paper:Recommend] Dropped ungrounded candidate: %.120s (claimed id=%s)',
                 title or '(no title)', claimed_id)
    return None


def _ground_correction(correction, seen_ids):
    """Ground the optional correction block.

    The ``note`` (interpretive prose) is kept as-is; the offered ``paper``, if
    any, must ground through the same path or it is dropped (logged) — the
    banner then shows the note without a bogus paper offer.
    """
    if not isinstance(correction, dict):
        return None
    note = (correction.get('note') or '').strip()
    if not note:
        return None
    out = {'note': note, 'paper': None}
    paper = correction.get('paper')
    if isinstance(paper, dict) and (paper.get('title') or paper.get('arxiv_id')):
        grounded = _ground_candidate(paper)
        if grounded and _norm_id(grounded['arxiv_id']) not in seen_ids:
            out['paper'] = grounded
        elif not grounded:
            logger.debug('[Paper:Recommend] Correction paper failed grounding: %.120s',
                         paper.get('title') or paper.get('arxiv_id') or '(unknown)')
    return out


def _parse_llm_json(content):
    """Extract the first JSON object from an LLM reply (tolerates code fences)."""
    if not content:
        return None
    text = content.strip()
    # Strip a leading ```json / ``` fence if present.
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
        logger.warning('[Paper:Recommend] LLM reply was not parseable JSON: %s', e)
        return None


def _research_and_interpret(description, max_results, *, abort=None, on_tool_event=None):
    """Agentic interpretation: research the current literature, then return the
    model's structured candidate/correction JSON.

    Runs the shared tool-calling loop (``web_search`` / ``fetch_url`` via the
    report engine's ``_execute_report_tool``) with a date-anchored system
    prompt, so the model surfaces genuinely current papers instead of guessing
    from stale training memory. This is the single seam the streaming pipeline
    and the blocking wrapper both go through, and the one tests monkeypatch to
    run offline.

    Args:
        description: the user's fuzzy free-text description.
        max_results: max grounded cards eventually wanted (only used to bound
            how many candidates are worth proposing — grounding enforces it).
        abort: optional zero-arg predicate; trips the loop's triple abort check.
        on_tool_event: optional ``(event_dict) -> None`` callback fired with a
            ``tool_start`` / ``tool_done`` event for each research tool call, so
            the caller can stream research activity to the UI. The blocking
            wrapper leaves this ``None``.

    Returns:
        The parsed JSON dict (``{"candidates": [...], "correction": ...}``), or
        ``None`` when the model's final message was not parseable JSON.

    Raises:
        AbortedError: the loop was aborted mid-dispatch (caller treats as a
            clean empty finish, not an error).
        Exception: any hard LLM dispatch failure (caller flags ``llmError``).
    """
    ui_lang = _detect_lang(description)
    system = date_anchor_clause(ui_lang) + _RECOMMEND_SYSTEM
    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': description},
    ]
    abort_signal = AbortSignal.from_callback(abort)
    user_question = description[:300]

    # Per-round content buffer (reset each dispatch). The FINAL (no-tool) round's
    # content is the JSON answer; interim prose emitted alongside a tool call is
    # discarded so it never pollutes the JSON parse (same pattern as the report
    # engine). ``_last['msg']`` is the belt for models that put content on the
    # message rather than streaming it.
    _round = {'content': ''}
    _last = {'msg': None}
    _round_counter = {'n': 0}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            _round['content'] += text

        logger.info('[Paper:Recommend] Research round %d — msgs=%d tools=%s',
                    rnd + 1, len(messages), 'yes' if tools else 'no')
        return dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            capability='text',
            tools=tools,
            max_tokens=4000,
            temperature=0,
            thinking_enabled=False,
            log_prefix='[Paper:Recommend]',
        )

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg

    def _begin_tool_round(rnd, msg):
        # This round issued tool calls, so any prose it emitted is interim
        # scaffolding, not the final JSON — drop it and append the assistant
        # turn so the tool results attach to it.
        _round['content'] = ''
        messages.append(msg)

    def _execute_tool(rnd, tc):
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')
        # Parse + schema-repair once so the display label and the executor see
        # the same normalized shape (a bare-string queries/urls → array).
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
        logger.info('[Paper:Recommend:Tool] %s → %d chars in %.1fs',
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
        max_tool_rounds=_MAX_RECOMMEND_TOOL_ROUNDS,
        round_tools=_REPORT_TOOLS,
        dispatch=_dispatch,
        execute_tool=_execute_tool,
        on_round_result=_on_round_result,
        on_tool_round=_begin_tool_round,
    )

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''
    return _parse_llm_json(content)


def iter_recommend_events(description, max_results=6, *, abort=None, on_tool_event=None):
    """Stream the describe-to-recommend pipeline as an ordered event sequence.

    This is the single source of truth for the recommend flow: the agentic
    interpretation call (research → structured JSON) and the *per-candidate*
    arXiv grounding are surfaced as discrete events so the frontend can reveal
    each grounded card the instant it resolves. The blocking ``recommend_papers``
    below is a thin consumer of this generator, so both the streaming task and
    the legacy one-shot route share identical ordering + gating.

    Args:
        description: Free-text description of the paper(s) the user recalls.
        max_results: Max grounded cards to yield (capped 1..12).
        abort: Optional zero-arg predicate; when it returns True the generator
            stops (interpretation loop + grounding) and finishes early.
        on_tool_event: Optional ``(event_dict) -> None`` callback fired with the
            interpretation agent's ``tool_start`` / ``tool_done`` research
            events (BEFORE ``interpret_done``), so the caller can stream the
            "researching…" activity. The blocking wrapper leaves this ``None``.

    Yields (each a dict with a ``type`` key):
        {'type': 'interpret_done', 'query': str, 'candidateCount': int,
         'correctionPending': bool}
            — research + interpretation complete: the model's proposals are
              parsed. ``candidateCount`` is how many grounding *attempts* will
              run (so the UI can paint that many skeleton cards).
        {'type': 'candidate', 'index': int, 'card': <card>}
            — one grounded, real arXiv card (search_arxiv shape + why/venue).
        {'type': 'correction', 'correction': {'note': str, 'paper': <card>|None}}
            — the optional false-premise correction (only when present).
        {'type': 'done', 'query': str, 'resultCount': int,
         'correctionPresent': bool}
            — terminal success.
        {'type': 'error', 'llmError': bool, 'query': str}
            — terminal failure of the interpretation call (distinct from
              "grounded nothing", which is a normal ``done`` with resultCount 0).
    """
    description = (description or '').strip()
    if not description:
        logger.warning('[Paper:Recommend] Empty description')
        yield {'type': 'done', 'query': description, 'resultCount': 0,
               'correctionPresent': False}
        return

    max_results = max(1, min(int(max_results or 6), 12))

    try:
        parsed = _research_and_interpret(
            description, max_results, abort=abort, on_tool_event=on_tool_event)
    except AbortedError:
        logger.info('[Paper:Recommend] Interpretation aborted for %.80s', description)
        yield {'type': 'done', 'query': description, 'resultCount': 0,
               'correctionPresent': False}
        return
    except Exception as e:
        logger.error('[Paper:Recommend] Interpretation research failed for %.120s: %s',
                     description, e, exc_info=True)
        yield {'type': 'error', 'llmError': True, 'query': description}
        return

    if parsed is None:
        logger.warning('[Paper:Recommend] No usable interpretation for %.120s', description)
        yield {'type': 'done', 'query': description, 'resultCount': 0,
               'correctionPresent': False}
        return

    candidates = parsed.get('candidates') if isinstance(parsed, dict) else None
    if not isinstance(candidates, list):
        candidates = []
    attempts = [c for c in candidates[:max_results * _GROUND_ATTEMPT_MULTIPLIER]
                if isinstance(c, dict)]
    raw_correction = parsed.get('correction') if isinstance(parsed, dict) else None
    correction_pending = bool(isinstance(raw_correction, dict)
                              and (raw_correction.get('note') or '').strip())

    yield {'type': 'interpret_done', 'query': description,
           'candidateCount': len(attempts), 'correctionPending': correction_pending}

    grounded_count = 0
    seen_ids = set()
    for cand in attempts:
        if abort is not None and abort():
            logger.info('[Paper:Recommend] Grounding aborted after %d cards for %.80s',
                        grounded_count, description)
            break
        card = _ground_candidate(cand)
        if not card:
            continue
        base = _norm_id(card['arxiv_id'])
        if base in seen_ids:
            continue
        seen_ids.add(base)
        yield {'type': 'candidate', 'index': grounded_count, 'card': card}
        grounded_count += 1
        if grounded_count >= max_results:
            break

    correction = _ground_correction(raw_correction, seen_ids)
    if correction and correction.get('note'):
        yield {'type': 'correction', 'correction': correction}

    if not grounded_count and not (correction and correction.get('note')):
        logger.info('[Paper:Recommend] Nothing grounded for %.120s (%d proposed)',
                    description, len(candidates))
    else:
        logger.info('[Paper:Recommend] %d grounded / %d proposed%s for %.120s',
                    grounded_count, len(candidates),
                    ' (+correction)' if correction else '', description)

    yield {'type': 'done', 'query': description, 'resultCount': grounded_count,
           'correctionPresent': bool(correction and correction.get('note'))}


def recommend_papers(description, max_results=6):
    """Interpret a fuzzy description into grounded, addable arXiv paper cards.

    Blocking convenience wrapper over :func:`iter_recommend_events` — it drains
    the generator and assembles the same aggregate shape the legacy one-shot
    ``/api/v1/paper/recommend`` route has always returned. Prefer the generator
    (via the streaming task) for the interactive UI.

    Args:
        description: Free-text description of the paper(s) the user recalls.
        max_results: Max grounded cards to return (capped 1..12).

    Returns:
        {
          'query': str,
          'correction': {'note': str, 'paper': <card>|None} | None,
          'results': [ <card>, ... ],   # each card = search_arxiv shape + why/venue
          'llmError': bool,             # true only when the interpretation call failed
        }
        A card is guaranteed to carry a real ``arxiv_id`` (grounded), so the
        frontend can click straight into the existing ``_fetchArxivPaper``.
    """
    query = (description or '').strip()
    out = {'query': query, 'correction': None, 'results': [], 'llmError': False}
    for ev in iter_recommend_events(description, max_results):
        etype = ev.get('type')
        if etype == 'candidate':
            out['results'].append(ev['card'])
        elif etype == 'correction':
            out['correction'] = ev['correction']
        elif etype == 'error':
            out['llmError'] = bool(ev.get('llmError'))
    return out
