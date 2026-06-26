"""Background worker for agentic paper Q&A.

Runs the SAME tool-calling loop the report engine proves (web_search /
fetch_url via ``_execute_report_tool``), but for a single user question. The
message context is built by ``qa_context.build_qa_messages`` — full generated
report + question-relevant paper sections — so the model can answer both
"what did you mean in the Limitations section?" (from the report) and "find
the follow-up paper" (via tools), without the legacy 100k blind truncation.

Emits chat-compatible events (tool_start / tool_done / delta / done / error)
so the frontend reuses ``renderToolRoundsHTML`` and the report poll renderer.
"""

import json
import time
from urllib.parse import urlparse

import lib as _lib
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .qa_runtime import _append_qa_event, _cleanup_stale_qa_tasks
from .tools import _execute_report_tool

logger = get_logger(__name__)

# Q&A is interactive — fewer tool rounds than a report (which does a deep
# literature scan). A handful is plenty for "look this up and answer".
_MAX_QA_TOOL_ROUNDS = 4


def _run_qa_task(task, messages):
    """Background worker: run the Q&A tool loop and populate task events.

    Args:
        task: the Q&A task dict (from ``_new_qa_task``).
        messages: the assembled message list (from ``build_qa_messages``).
    """
    task['status'] = 'running'
    _append_qa_event(task, {'type': 'status', 'status': 'running'})

    model = task['model']
    abort_event = task['abort_event']

    def _abort_check():
        return abort_event.is_set()

    model_name = model or _lib.LLM_MODEL
    t0 = time.time()
    full_content = ''
    question = task.get('question', '')
    user_question = question[:300]

    try:
        for rnd in range(_MAX_QA_TOOL_ROUNDS + 1):
            if _abort_check():
                logger.info('[Paper:QA] Task %s aborted', task['task_id'])
                break

            _round_tools = _QA_TOOLS if rnd < _MAX_QA_TOOL_ROUNDS else None
            logger.info('[Paper:QA] Task %s round %d — model=%s msgs=%d',
                        task['task_id'], rnd + 1, model_name, len(messages))

            # Per-round buffer so an interim draft emitted alongside a tool
            # call (the model "thinks out loud" then rewrites after the tool
            # result) is discarded — same fix as the report engine.
            round_content = ''

            def _on_content(text):
                nonlocal round_content, full_content
                round_content += text
                full_content += text
                task['full_text'] = full_content
                _append_qa_event(task, {'type': 'delta', 'delta': text})

            msg, finish, usage = dispatch_stream(
                messages,
                on_content=_on_content,
                abort_check=_abort_check,
                prefer_model=model_name if model else None,
                strict_model=bool(model),
                tools=_round_tools,
                max_tokens=8000,
                temperature=0,
                thinking_enabled=False,
                log_prefix='[Paper:QA]',
            )

            tool_calls = msg.get('tool_calls')
            if not tool_calls:
                logger.info('[Paper:QA] Task %s — answer complete (%d chars, %.1fs)',
                            task['task_id'], len(full_content), time.time() - t0)
                break

            # Discard any interim draft prose this round emitted (it will be
            # rewritten after the tool results land).
            if round_content:
                full_content = full_content[:-len(round_content)]
                task['full_text'] = full_content
                _append_qa_event(task, {'type': 'delta_reset'})

            messages.append(msg)

            for tc in tool_calls:
                fn_name = tc['function']['name']
                fn_args_raw = tc['function']['arguments']
                tc_id = tc.get('id', '')

                try:
                    fn_args = (json.loads(fn_args_raw)
                               if isinstance(fn_args_raw, str) else (fn_args_raw or {}))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[Paper:QA] bad tool args JSON: %s', e)
                    fn_args = {}

                task['round_counter'] += 1
                rn = task['round_counter']
                display_query = _display_query_for(fn_name, fn_args)

                round_entry = {
                    'roundNum': rn, 'toolName': fn_name, 'query': display_query,
                    'toolCallId': tc_id,
                    'toolArgs': (fn_args_raw if isinstance(fn_args_raw, str)
                                 else json.dumps(fn_args, ensure_ascii=False)),
                    'status': 'searching', 'results': None,
                }
                task['tool_rounds'].append(round_entry)
                _append_qa_event(task, {
                    'type': 'tool_start', 'roundNum': rn, 'toolName': fn_name,
                    'query': display_query, 'toolCallId': tc_id,
                    'toolArgs': round_entry['toolArgs'],
                })

                tool_t0 = time.time()
                result, display_results, search_diag = _execute_report_tool(
                    fn_name, fn_args_raw, user_question=user_question)
                tool_elapsed = time.time() - tool_t0
                logger.info('[Paper:QA:Tool] %s → %d chars in %.1fs',
                            fn_name, len(result), tool_elapsed)

                round_entry['status'] = 'done'
                round_entry['_elapsed'] = f'{tool_elapsed:.1f}s'
                round_entry['results'] = display_results
                round_entry['toolContent'] = result[:4000]

                done_ev = {
                    'type': 'tool_done', 'roundNum': rn, 'toolName': fn_name,
                    'toolCallId': tc_id, 'elapsed': round(tool_elapsed, 1),
                    'toolContent': result[:4000], 'results': display_results,
                }
                if search_diag:
                    done_ev['searchDiag'] = search_diag
                _append_qa_event(task, done_ev)

                messages.append({
                    'role': 'tool', 'tool_call_id': tc_id,
                    'content': result[:30000],
                })

        elapsed = time.time() - t0
        logger.info('[Paper:QA] Task %s complete — %d chars, %.1fs',
                    task['task_id'], len(full_content), elapsed)
        task['status'] = 'done'
        task['finished_at'] = time.time()
        _append_qa_event(task, {'type': 'done', 'answer': full_content,
                                'paperHash': task['paper_hash']})

    except Exception as e:
        logger.error('[Paper:QA] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-qa', source='routes.paper:qa')
        task['status'] = 'error'
        task['error'] = envelope
        task['finished_at'] = time.time()
        _append_qa_event(task, {'type': 'error', 'error': envelope})
    finally:
        _cleanup_stale_qa_tasks()


def _display_query_for(fn_name, fn_args):
    """Build the short human-readable query label for a tool round."""
    if fn_name == 'web_search':
        queries = fn_args.get('queries') or []
        if not queries and fn_args.get('query'):
            queries = [{'query': fn_args['query']}]
        if len(queries) > 1:
            previews = [q.get('query', '?')[:30] for q in queries[:3] if isinstance(q, dict)]
            suffix = f' +{len(queries) - 3} more' if len(queries) > 3 else ''
            return f'{len(queries)} searches: {"; ".join(previews)}{suffix}'
        return queries[0].get('query', '') if queries and isinstance(queries[0], dict) else ''
    if fn_name == 'fetch_url':
        urls = fn_args.get('urls') or []
        if not urls and fn_args.get('url'):
            urls = [{'url': fn_args['url']}]
        if len(urls) > 1:
            previews = []
            for u in urls[:3]:
                if isinstance(u, dict):
                    try:
                        p = urlparse(u.get('url', '?'))
                        previews.append((p.netloc or '') + (p.path or '')[:30])
                    except ValueError:
                        previews.append((u.get('url', '?'))[:40])
            suffix = f' +{len(urls) - 3} more' if len(urls) > 3 else ''
            return f'{len(urls)} URLs: {", ".join(previews)}{suffix}'
        target = urls[0].get('url', '') if urls and isinstance(urls[0], dict) else ''
        return target
    return fn_name


# Tool list — reuse the report engine's batch search/fetch tools.
from .prompts import _REPORT_TOOLS as _QA_TOOLS  # noqa: E402
