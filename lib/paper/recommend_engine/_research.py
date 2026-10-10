"""Research current literature and return structured recommendation candidates.

Instead of guessing candidate titles from the model's frozen training memory
(which cannot know about a conference happening *today* or papers posted last
week), the model is given the project's own ``web_search`` / ``fetch_url`` tools
and told to actually RESEARCH the current literature BEFORE it proposes
candidates. A "current date" anchor is injected so it never treats an
in-progress year as the future. The final turn returns strict JSON.

"""

import hashlib

from lib.agent_loop import AbortSignal
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger
from lib.llm.json_extract import extract_first_json_object
from lib.paper.agent_loop_policy import run_guarded_paper_agent_loop
from lib.paper.agent_usage import PaperAgentUsageMeter

from ..prompts import date_anchor_clause
from ..tools import (
    PaperToolResultBudgetV2,
    build_research_tool_schemas,
    execute_paper_tool,
    freeze_paper_tool_epoch,
    make_paper_exec_shim,
    make_research_tool_executor,
)
from ._ground import _detect_lang

logger = get_logger(__name__)

# The interpretation agent researches KNOWN-TITLE papers, so its web_search
# calls are forced onto the academic vertical (arXiv + Semantic Scholar JSON
# APIs). Those APIs have their OWN uptime, independent of the Brave/Bing/DDG/
# SearXNG HTML fleet and its per-engine circuit breakers — so a title lookup
# still resolves during a window where every HTML engine is down/breaker-open.
# The vertical runs CONCURRENTLY WITH and ADDITIVE TO the HTML pipeline (see
# handlers/search.py::_web_search_one), so this widens coverage without losing
# the web engines. We force it in code rather than trusting the model to pick
# it — the ACL-26 "no results" trace was exactly the model NOT choosing it.
_RESEARCH_VERTICAL = 'academic'

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
    "user describes; the app already routes these to arXiv/Semantic Scholar for "
    "you, so just pass plain search terms. Pass "
    "``freshness='month'``/``'year'`` when the user implies recency. Run a few "
    "targeted queries (e.g. the topic + the venue+year, and the specific "
    "award/track if one is claimed). When searching for a KNOWN or suspected "
    "title, type the title words UNQUOTED — do NOT wrap it in exact-phrase "
    "quotes (\"...\"). A quoted full-title phrase is brittle and often returns "
    "zero when a search engine is busy; unquoted title tokens have far higher "
    "recall, and the app grounds the real paper by title anyway.\n"
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


def _parse_llm_json(content):
    """Extract the first JSON object from an LLM reply (tolerates code fences)."""
    return extract_first_json_object(content, log_prefix='[Paper:Recommend]', log=logger)


def _research_and_interpret(description, max_results, *, abort=None,
                            on_tool_event=None, user_id=None):
    """Agentic interpretation: research the current literature, then return the
    model's structured candidate/correction JSON.

    Runs the shared tool-calling loop (``web_search`` / ``fetch_url`` via the
    report engine's ``execute_paper_tool``) with a date-anchored system
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
    _agent_usage = PaperAgentUsageMeter.for_stage('recommend')
    user_question = description[:300]
    paper_tools, paper_contracts = freeze_paper_tool_epoch(
        build_research_tool_schemas(), owner_user_id=user_id)
    _exec_shim = make_paper_exec_shim(
        task_id=('paper-recommend-' + hashlib.sha256(
            description.encode('utf-8')).hexdigest()[:16]),
        abort=abort_signal.is_set, owner_user_id=user_id,
        tool_contract_documents_by_name=paper_contracts)
    _result_budget = PaperToolResultBudgetV2(owner_user_id=user_id)
    contracts_by_round = {}

    # Per-round content buffer (reset each dispatch). The FINAL (no-tool) round's
    # content is the JSON answer; interim prose emitted alongside a tool call is
    # discarded so it never pollutes the JSON parse (same pattern as the report
    # engine). ``_last['msg']`` is the belt for models that put content on the
    # message rather than streaming it.
    _round = {'content': ''}
    _last = {'msg': None}

    def _dispatch(rnd, tools):
        _round['content'] = ''
        effective_tools, contracts_by_round[rnd] = freeze_paper_tool_epoch(
            tools, owner_user_id=user_id)

        def _on_content(text):
            _round['content'] += text

        logger.info('[Paper:Recommend] Research round %d — msgs=%d tools=%s',
                    rnd + 1, len(messages),
                    'yes' if effective_tools else 'no')
        from lib.llm.stream_result import ensure_provider_stream_result
        return ensure_provider_stream_result(dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            capability='text',
            tools=effective_tools,
            max_tokens=4000,
            temperature=0,
            thinking_enabled=False,
            log_prefix='[Paper:Recommend]',
        ))

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg

    def _begin_tool_round(rnd, msg):
        # This round issued tool calls, so any prose it emitted is interim
        # scaffolding, not the final JSON — drop it and append the assistant
        # turn so the tool results attach to it.
        _round['content'] = ''
        messages.append(msg)

    # Shared research tool-round executor. Recommendation forces the academic
    # vertical so known-title lookup does not depend on general web availability.
    _execute_tool = make_research_tool_executor(
        messages, user_question=user_question, abort_signal=abort_signal,
        result_budget=_result_budget, exec_shim=_exec_shim,
        paper_tool_executor=execute_paper_tool,
        on_tool_event=on_tool_event, log_prefix='[Paper:Recommend]',
        force_vertical=_RESEARCH_VERTICAL,
        contract_documents_for_round=contracts_by_round.get)

    run_guarded_paper_agent_loop(
        context='Paper Recommend agent',
        usage_meter=_agent_usage,
        abort=abort_signal,
        round_tools=paper_tools,
        dispatch=_dispatch,
        execute_tool=_execute_tool,
        on_round_result=_on_round_result,
        on_tool_round=_begin_tool_round,
        on_round_end=_result_budget.finish_round,
    )

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''
    parsed = _parse_llm_json(content)
    if isinstance(parsed, dict):
        parsed['_agentUsageV1'] = _agent_usage.snapshot()
    return parsed
