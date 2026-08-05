"""JSON extraction + the agentic synthesis pass.

Holds :func:`_parse_llm_json`, the one-shot :func:`_repair_json_reask` recovery,
and :func:`_research_and_synthesize` — the single seam tests monkeypatch to
drive the agentic loop offline.

The two patchable dependencies — ``dispatch_stream`` (the LLM) and
``_execute_report_tool`` (the research tool executor) — are resolved THROUGH the
package facade at call time (``import lib.paper.insight_engine as _pkg``) so a
test patching ``ie.dispatch_stream`` / ``ie._execute_report_tool`` bites exactly
as it did in the original flat module.
"""

from lib.agent_loop import AbortSignal, run_agent_loop
from lib.llm_errors import AbortedError
from lib.log import get_logger
from lib.llm.json_extract import extract_first_json_object

from ._config import (
    _INSIGHT_TEMPERATURE,
    _MAX_INSIGHT_TOOL_ROUNDS,
    _REPAIR_MAX_TOKENS,
)
from ..insight_prompts import insight_system_prompt
from ..prompts import _REPORT_TOOLS, date_anchor_clause
from ..tools import make_research_tool_executor  # noqa: F401

logger = get_logger(__name__)


def _parse_llm_json(content):
    """Extract the first JSON object from an LLM reply (tolerates code fences)."""
    return extract_first_json_object(content, log_prefix='[Paper:Insight]', log=logger)


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
    import lib.paper.insight_engine as _pkg
    dispatch_stream = _pkg.dispatch_stream

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
    import lib.paper.insight_engine as _pkg
    dispatch_stream = _pkg.dispatch_stream
    _execute_report_tool = _pkg._execute_report_tool

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
    # Cost visibility (design §3.3): accumulate EVERY dispatch round's usage
    # (tool rounds + the terminal JSON round) so the caller can fold the
    # synthesis cost into the report meta's secondPasses breakdown.
    _usage_acc = {'prompt_tokens': 0, 'completion_tokens': 0,
                  'cache_read_tokens': 0, 'cache_write_tokens': 0,
                  'reasoning_tokens': 0}

    def _acc_usage(usage):
        if not isinstance(usage, dict):
            return
        try:
            from lib.cost import normalize_usage as _nu
            _n = _nu(usage)
            _usage_acc['prompt_tokens'] += _n['input']
            _usage_acc['completion_tokens'] += _n['output']
            _usage_acc['cache_read_tokens'] += _n['cache_read']
            _usage_acc['cache_write_tokens'] += _n['cache_write']
            _usage_acc['reasoning_tokens'] += _n['thinking']
        except Exception as e:
            logger.debug('[Paper:Insight] usage accumulate failed (non-fatal): %s', e)

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
        _acc_usage(usage)

    def _begin_tool_round(rnd, msg):
        # This round issued tool calls → its prose is interim scaffolding, not
        # the final JSON. Drop it and append the assistant turn.
        _round['content'] = ''
        messages.append(msg)

    # Shared research tool-round executor (see lib/paper/tools.make_research_tool_executor):
    # insight uses the model's own vertical choice (no force_vertical). Pass the
    # facade-resolved _execute_report_tool so ie._execute_report_tool patches bite.
    _execute_tool = make_research_tool_executor(
        messages, user_question=user_question, abort_signal=abort_signal,
        execute_report_tool=_execute_report_tool,
        on_tool_event=on_tool_event, log_prefix='[Paper:Insight]')

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
        # Private key (popped by generate_insight before grounding/render) —
        # tests monkeypatching this seam return plain dicts without it, which
        # the caller treats as unknown usage.
        parsed['_usage'] = dict(_usage_acc)
        return parsed

    # The final content wasn't parseable JSON (prose-wrapped / truncated / fenced
    # in a way the extractor missed). Recover with one deterministic re-ask
    # rather than silently returning nothing.
    logger.info('[Paper:Insight] Final content unparseable — attempting one-shot JSON repair')
    repaired = _repair_json_reask(messages, content, model=model_name if model else None,
                                  abort_signal=abort_signal)
    if isinstance(repaired, dict):
        repaired['_usage'] = dict(_usage_acc)
    return repaired
