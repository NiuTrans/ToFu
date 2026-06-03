"""Background worker for paper report generation.

Drives the LLM tool-calling loop (web_search / fetch_url for landscape
research), emits chat-compatible events (tool_start / tool_done / delta /
thinking / done / enriched / error), and persists the enriched report to
``paper_reports`` on completion.
"""

import json
import re
import time

import lib as _lib
from lib.database import db_execute_with_retry, get_thread_db
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .images import _inject_images_into_report, _lookup_paper_title
from .prompts import _MAX_REPORT_TOOL_ROUNDS, _REPORT_TOOLS
from .report_runtime import _append_report_event, _cleanup_stale_report_tasks
from .tools import _execute_report_tool

logger = get_logger(__name__)


def _run_report_task(task, messages, images):
    """Background worker: runs the tool loop and populates task events.

    Event schema (mirrors chat stream for frontend reuse):
      - {type: 'tool_start', roundNum, toolName, query, toolCallId, toolArgs}
      - {type: 'tool_done',  roundNum, toolName, toolContent (truncated preview), elapsed}
      - {type: 'thinking',   delta}
      - {type: 'delta',      delta}
      - {type: 'enriched',   text}             — post-stream image injection
      - {type: 'done',       report, paperHash}
      - {type: 'error',      error}
    """
    task['status'] = 'running'
    _append_report_event(task, {'type': 'status', 'status': 'running'})

    phash = task['paper_hash']
    lang = task['lang']
    model = task['model']
    abort_event = task['abort_event']

    def _abort_check():
        return abort_event.is_set()

    model_name = model or _lib.LLM_MODEL
    t0 = time.time()
    full_content = ''
    # Extract a short context string for search relevance filtering
    _user_msg = messages[1]['content'] if len(messages) > 1 else ''
    _report_user_question = _user_msg[:300] if _user_msg else ''

    try:
        for rnd in range(_MAX_REPORT_TOOL_ROUNDS + 1):
            if _abort_check():
                logger.info('[Paper:Report] Task %s aborted', task['task_id'])
                break

            _round_tools = _REPORT_TOOLS if rnd < _MAX_REPORT_TOOL_ROUNDS else None
            logger.info('[Paper:Report] Task %s round %d — model=%s msgs=%d',
                        task['task_id'], rnd + 1, model_name, len(messages))

            def _on_content(text):
                nonlocal full_content
                full_content += text
                task['full_text'] = full_content
                _append_report_event(task, {'type': 'delta', 'delta': text})

            def _on_thinking(text):
                _append_report_event(task, {'type': 'thinking', 'delta': text})

            # ★ max_tokens: pass a very large ceiling so the report can run
            #   to completion without artificial truncation.  dispatch_stream
            #   → build_body → _clamp_max_tokens() automatically reduces this
            #   to each model's native API limit (GPT=32k, Claude=128k,
            #   Qwen per-model 16–64k, etc.), so we get "as much as the model
            #   allows" without hardcoding a small cap.
            #   Prior behavior: fell back to dispatch_stream's default 4096,
            #   which truncated long reports mid-section.
            msg, finish, usage = dispatch_stream(
                messages,
                on_content=_on_content,
                on_thinking=_on_thinking,
                abort_check=_abort_check,
                prefer_model=model_name if model else None,
                strict_model=bool(model),
                tools=_round_tools,
                max_tokens=128000,
                temperature=0,
                thinking_enabled=False,
                log_prefix='[Paper:Report]',
            )

            tool_calls = msg.get('tool_calls')
            if not tool_calls:
                logger.info('[Paper:Report] Task %s — no tool calls, report complete '
                            '(%d chars, %.1fs)', task['task_id'], len(full_content), time.time() - t0)
                break

            messages.append(msg)

            # Execute tool calls — emit chat-compatible tool_start / tool_done events
            for tc in tool_calls:
                fn_name = tc['function']['name']
                fn_args_raw = tc['function']['arguments']
                tc_id = tc.get('id', '')

                # Parse args for display
                try:
                    fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else (fn_args_raw or {})
                except (json.JSONDecodeError, TypeError) as _e_audit:
                    logger.debug('[paper] _run_report_task caught %s: %s', type(_e_audit).__name__, _e_audit)
                    fn_args = {}

                # Build chat-style round entry (subset of what
                # lib.tasks_pkg.tool_display produces — for paper report we
                # only have web_search / fetch_url).
                task['round_counter'] += 1
                rn = task['round_counter']

                if fn_name == 'web_search':
                    queries = fn_args.get('queries') or []
                    if not queries and fn_args.get('query'):
                        queries = [{'query': fn_args['query']}]
                    if len(queries) > 1:
                        previews = [q.get('query', '?')[:30] for q in queries[:3] if isinstance(q, dict)]
                        suffix = f' +{len(queries) - 3} more' if len(queries) > 3 else ''
                        display_query = f'{len(queries)} searches: {"; ".join(previews)}{suffix}'
                    else:
                        display_query = queries[0].get('query', '') if queries and isinstance(queries[0], dict) else ''
                elif fn_name == 'fetch_url':
                    urls = fn_args.get('urls') or []
                    if not urls and fn_args.get('url'):
                        urls = [{'url': fn_args['url']}]
                    if len(urls) > 1:
                        previews = []
                        for u in urls[:3]:
                            if isinstance(u, dict):
                                url = u.get('url', '?')
                                # short host+path
                                try:
                                    from urllib.parse import urlparse
                                    p = urlparse(url)
                                    previews.append((p.netloc or '') + (p.path or '')[:30])
                                except ValueError as e:
                                    logger.debug('[Paper] urlparse failed for %r: %s', url[:80], e)
                                    previews.append(url[:40])
                        suffix = f' +{len(urls) - 3} more' if len(urls) > 3 else ''
                        display_query = f'📄 {len(urls)} URLs: {", ".join(previews)}{suffix}'
                    else:
                        target_url = urls[0].get('url', '') if urls and isinstance(urls[0], dict) else ''
                        display_query = f'🌐 {target_url}'
                else:
                    display_query = fn_name

                round_entry = {
                    'roundNum': rn,
                    'toolName': fn_name,
                    'query': display_query,
                    'toolCallId': tc_id,
                    'toolArgs': fn_args_raw if isinstance(fn_args_raw, str) else json.dumps(fn_args, ensure_ascii=False),
                    'status': 'searching',
                    'results': None,
                }
                task['tool_rounds'].append(round_entry)

                _append_report_event(task, {
                    'type': 'tool_start',
                    'roundNum': rn,
                    'toolName': fn_name,
                    'query': display_query,
                    'toolCallId': tc_id,
                    'toolArgs': round_entry['toolArgs'],
                })

                tool_t0 = time.time()
                result, display_results, search_diag = _execute_report_tool(
                    fn_name, fn_args_raw, user_question=_report_user_question)
                tool_elapsed = time.time() - tool_t0
                logger.info('[Paper:Report:Tool] %s → %d chars in %.1fs', fn_name, len(result), tool_elapsed)

                # Update round entry → done
                round_entry['status'] = 'done'
                round_entry['_elapsed'] = f'{tool_elapsed:.1f}s'
                round_entry['results'] = display_results
                # Preview of the tool content (capped, so polling responses stay small)
                tool_preview = result[:4000]
                round_entry['toolContent'] = tool_preview

                tool_done_event = {
                    'type': 'tool_done',
                    'roundNum': rn,
                    'toolName': fn_name,
                    'toolCallId': tc_id,
                    'elapsed': round(tool_elapsed, 1),
                    'toolContent': tool_preview,
                    'results': display_results,
                }
                if search_diag:
                    tool_done_event['searchDiag'] = search_diag
                _append_report_event(task, tool_done_event)

                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc_id,
                    'content': result[:30000],
                })

        elapsed = time.time() - t0
        logger.info('[Paper:Report] Task %s content stream complete — %d chars in %.1fs',
                    task['task_id'], len(full_content), elapsed)

        # Strip LLM preamble before the first heading. Models often emit
        # "Now I have enough information..." / "Let me compile..." / multi-
        # paragraph "I'll research..." before the actual Markdown report
        # starts at the first `## ` or `# `. Strip aggressively — any text
        # before the first heading is non-report chatter regardless of length.
        _heading_start = re.search(r'^#{1,6}\s', full_content, re.MULTILINE)
        if _heading_start and _heading_start.start() > 0:
            preamble = full_content[:_heading_start.start()].strip()
            if preamble:
                logger.info('[Paper:Report] Stripping %d-char preamble: %.120s',
                            len(preamble), preamble.replace('\n', ' '))
                full_content = full_content[_heading_start.start():]

        # Prepend a top-level `# Title` heading so the rendered report has a
        # title bar instead of starting cold at "## TL;DR". Title is sourced
        # from paper_library keyed by paper_hash; falls back to the
        # client-supplied title (sent on the report start request) which
        # avoids a race when paper_library hasn't been upserted yet.
        title = _lookup_paper_title(phash) or task.get('client_title') or ''
        if title and full_content:
            already_titled = re.match(r'^\s*#\s+\S', full_content)
            if not already_titled:
                full_content = f'# {title}\n\n' + full_content.lstrip()
                logger.info('[Paper:Report] Prepended title: %.120s', title)
            else:
                logger.info('[Paper:Report] Title prepend skipped — content already starts with H1')
        else:
            logger.warning('[Paper:Report] No title available for hash=%s — report will lack header', phash)

        # Inject figures/tables into the report
        enriched = _inject_images_into_report(full_content, images, lang=lang)
        task['enriched_text'] = enriched

        # Persist to DB
        if enriched:
            try:
                db2 = get_thread_db()
                db_execute_with_retry(
                    db2,
                    "INSERT OR REPLACE INTO paper_reports (paper_hash, lang, report, model, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (phash, lang, enriched, model or _lib.LLM_MODEL, int(time.time())),
                )
                logger.info('[Paper:Report] Persisted — hash=%s lang=%s %d chars (%d imgs)',
                            phash, lang, len(enriched), len(images))
            except Exception as e:
                logger.warning('[Paper:Report] Failed to persist: %s', e)

        # If enrichment changed the text, emit an enriched event so pollers
        # replay the image-embedded version as the canonical body.
        if enriched and enriched != full_content:
            _append_report_event(task, {'type': 'enriched', 'text': enriched, 'paperHash': phash})

        task['status'] = 'done'
        task['finished_at'] = time.time()
        _append_report_event(task, {'type': 'done', 'report': enriched or full_content, 'paperHash': phash})

    except Exception as e:
        logger.error('[Paper:Report] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-report',
            source='routes.paper:report',
        )
        task['status'] = 'error'
        task['error'] = envelope
        task['finished_at'] = time.time()
        _append_report_event(task, {'type': 'error', 'error': envelope})
    finally:
        _cleanup_stale_report_tasks()
