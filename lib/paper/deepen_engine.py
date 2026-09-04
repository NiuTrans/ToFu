"""On-demand section deepening for Paper Reading Mode.

The fidelity report deliberately cannot be infinitely deep — real depth is
served ON DEMAND: a reader clicks "再深一层" on a section (or "逐步推导" on a
formula) and a bounded agentic task expands exactly that section, one level
deeper. Depth cost is paid only when a reader actually asks (对齐「低成本」),
the main report prompt is untouched (零漂移风险), and results are cached per
(mode, section, lang) so the same section deepens once.

Design commitments:

  * **Clone the proven QA machinery** — same ``run_agent_loop`` chassis
    (charter IRON RULE), same chat-compatible event schema (tool_start /
    tool_done / delta / done / error), same TaskRuntime + push pattern. No
    new orchestration mechanics.
  * **The stored report is authoritative** — the section body is extracted
    from the persisted ``paper_reports`` row server-side (never client-
    supplied), so the cache validator and the prompt see the same bytes.
  * **Cache with staleness validation** — results persist under the composite
    key ``deep:<mode>:<sec>:<ui_lang>`` with the section's content hash in
    meta; a regenerated report shifts section bodies → hash mismatch → the
    stale row is ignored and overwritten. A cache hit never re-bills.
  * **Cost visibility (design §3.3)** — a finished deepen ACCUMULATES its
    usage into the report meta's ``secondPasses.deepen`` (multiple sections
    sum), so the finish tag's total stays honest; the live drawer shows the
    per-call usage from the task's done event.
  * **Prompt-injection hardened** — context assembly reuses
    ``qa_context.build_qa_messages`` (sanitize + untrusted fence + date
    anchor) unchanged.

Every failure path leaves a trace per CLAUDE.md §2.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid

import lib as _lib
from lib.agent_loop import AbortSignal
from lib.llm_dispatch.api import dispatch_stream
from lib.llm_errors import AbortedError
from lib.log import get_logger
from lib.tasks_pkg.tool_display import tool_round_label as _display_query_for
from lib.tool_input_repair import parse_and_repair_tool_args

from .deepen_runtime import _deepen_runtime
from .agent_loop_policy import run_guarded_paper_agent_loop
from .agent_usage import PaperAgentUsageMeter
from .qa_context import build_qa_messages
from .request_policy import paper_request_policy_telemetry
from .tools import (
    PaperToolResultBudgetV2,
    execute_paper_tool,
    apply_paper_tool_epoch_guidance,
    build_paper_full_tool_epoch,
    make_paper_exec_shim,
    paper_effective_tool_name,
)

logger = get_logger(__name__)

__all__ = [
    'DEEPEN_MODES',
    'deepen_lang_key',
    'extract_report_section',
    'read_deepen_cache',
    'start_deepen',
    '_deepen_runtime',
    '_new_deepen_task',
    '_append_deepen_event',
    '_run_deepen_task',
]

DEEPEN_MODES = ('deeper', 'derive', 'eli5')

_LANG_PREFIX = 'deep'


def deepen_lang_key(mode: str, section_idx: int, ui_lang: str) -> str:
    """Composite ``paper_reports.lang`` cache key for one deepened section."""
    return f'{_LANG_PREFIX}:{mode}:{int(section_idx)}:{ui_lang or "en"}'


# ── Section extraction (authoritative: from the stored report body) ──────

_HEADING_RE = re.compile(r'^(#{2,3})\s+(.+?)\s*$', re.MULTILINE)


def extract_report_section(report_md, section_idx):
    """Extract one h2/h3 section (heading + body span) from a report body.

    ``section_idx`` enumerates the report's h2/h3 headings in document order —
    the SAME enumeration ``insight_engine._anchors.extract_report_headings``
    produces and the frontend uses when tagging buttons. A section spans from
    its heading to the next heading of equal-or-higher level (an h3 section
    ends at the next h2 OR h3; an h2 section ends at the next h2). Code fences
    are tolerated: headings inside ``` blocks are ignored.

    Returns ``{'heading': str, 'body': str, 'text': str, 'level': int,
    'hash': str}`` or None when the index is out of range. ``hash`` is a
    short content fingerprint used for cache staleness validation.
    """
    body = re.sub(r'```.*?```', lambda m: ' ' * (m.end() - m.start()),
                  report_md or '', flags=re.DOTALL)
    matches = list(_HEADING_RE.finditer(body))
    if section_idx is None or section_idx < 0 or section_idx >= len(matches):
        return None
    m = matches[section_idx]
    level = len(m.group(1))
    end = len(body)
    for nxt in matches[section_idx + 1:]:
        if len(nxt.group(1)) <= level:
            end = nxt.start()
            break
    text = (report_md or '')[m.start():end].strip()
    heading = m.group(2).strip()
    sec_body = (report_md or '')[m.end():end].strip()
    fp = hashlib.sha256(text.encode('utf-8', 'ignore')).hexdigest()[:16]
    return {'heading': heading, 'body': sec_body, 'text': text,
            'level': level, 'hash': fp}


# ── Cache (paper_reports composite key) ──────────────────────────────────

def read_deepen_cache(
    phash, mode, section_idx, ui_lang, section_hash, *, user_id: int,
):
    """Return the cached deepen row ``{'content', 'usage'}`` when fresh, else None.

    Fresh = the row exists AND its meta's section_hash matches the CURRENT
    section body (a regenerated report invalidates silently — never serve
    stale depth).
    """
    try:
        from lib.paper.artifact_repository import PaperArtifactRepository
        row = PaperArtifactRepository(user_id).get_report(
            phash, deepen_lang_key(mode, section_idx, ui_lang))
    except Exception as e:
        logger.warning('[Paper:Deepen] cache read failed hash=%s: %s', phash, e)
        return None
    if not row or not row.report:
        return None
    meta = row.meta
    if not isinstance(meta, dict):
        logger.debug('[Paper:Deepen] bad cache meta (treated as miss)')
        return None
    if meta.get('section_hash') != section_hash:
        logger.info('[Paper:Deepen] cache stale (report regenerated) — hash=%s '
                    'mode=%s sec=%d', phash, mode, section_idx)
        return None
    return {'content': row.report, 'usage': meta.get('usage'),
            'model': row.model}


def _write_deepen_cache(phash, mode, section_idx, ui_lang, section_hash,
                        content, usage, model, *, user_id: int):
    try:
        from lib.paper.artifact_repository import (
            PaperArtifactRepository,
            PaperReport,
        )
        meta = {'kind': 'deep', 'v': 1, 'mode': mode, 'section_idx': section_idx,
                'section_hash': section_hash, 'usage': usage}
        PaperArtifactRepository(user_id).put_report(
            PaperReport(
                paper_hash=phash,
                lang=deepen_lang_key(mode, section_idx, ui_lang),
                report=content,
                model=model or '',
                meta=meta,
                created_at=int(time.time()),
            ),
            command_id=f'paper.deepen.upsert:{uuid.uuid4().hex}',
        )
        logger.info('[Paper:Deepen] Cached — hash=%s key=%s %d chars',
                    phash, deepen_lang_key(mode, section_idx, ui_lang), len(content))
        return True
    except Exception as e:
        logger.warning('[Paper:Deepen] cache write failed hash=%s: %s', phash, e)
        return False


def _accumulate_deepen_cost(phash, lang, usage, model, *, user_id: int):
    """Fold a finished deepen's usage into the REPORT row's secondPasses.deepen.

    Accumulates across calls (each deepened section adds its own cost) and
    re-computes the pass + total cost, so the finish tag's TOTAL stays the
    honest sum of everything this paper ever billed (design §3.3). Meta-only
    UPDATE — the report body is never touched.
    """
    if not usage:
        return
    try:
        from lib.cost import compute_cost
        from lib.paper.artifact_repository import PaperArtifactRepository
        cost = compute_cost(usage, model_id=model or '') or {}
        updated = PaperArtifactRepository(
            user_id).accumulate_report_second_pass(
                phash,
                lang,
                'deepen',
                usage,
                cost_cny=cost.get('costCny'),
                cost_usd=cost.get('costUsd'),
                command_id=f'paper.deepen.cost:{uuid.uuid4().hex}',
            )
        if not isinstance(updated, dict):
            return
        calls = int(
            ((updated.get('secondPasses') or {}).get('deepen') or {})
            .get('calls', 0))
        logger.info('[Paper:Deepen] cost accumulated — hash=%s calls=%d',
                    phash, calls)
    except Exception as e:
        logger.warning('[Paper:Deepen] cost accumulation failed hash=%s: %s', phash, e)


# ── Task store (mirrors qa_runtime) ──────────────────────────────────────

_deepen_dedup: dict[tuple, str] = {}
_deepen_dedup_lock = threading.Lock()


def _new_deepen_task(task_id, phash, lang, model, *, section_idx, mode,
                     section_heading, user_id: int, config=None):
    detached_config = dict(config or {})
    request_policy = paper_request_policy_telemetry(
        model=model, config=detached_config)
    task = _deepen_runtime.create(
        user_id=user_id,
        task_id=task_id,
        meta={'paper_hash': phash, 'lang': lang, 'model': model,
              'section_idx': section_idx, 'mode': mode,
              'execution_fingerprint': request_policy[
                  'executionFingerprint']},
    )
    _deepen_runtime.update_fields(task_id, fields={
        'task_id': task_id,
        'paper_hash': phash,
        'lang': lang,
        'model': model,
        'section_idx': section_idx,
        'mode': mode,
        'section_heading': section_heading,
        'config': detached_config,
        'execution_fingerprint': request_policy['executionFingerprint'],
        'requestPolicyV1': request_policy,
        'full_text': '',
        'tool_rounds': [],
        'round_counter': 0,
    })
    return task


def _append_deepen_event(task, event):
    _deepen_runtime.append_event(task['task_id'], event)


# ── Mode instructions ────────────────────────────────────────────────────

_INSTR = {
    'deeper': {
        'en': ('Expand the report section below ONE LEVEL DEEPER for a reader who '
               'already understood the surface version. Add the layer the report '
               'compressed: the step-by-step reasoning, the edge cases, the exact '
               'numbers, the implementation details a reproducer needs, and the '
               '"why this and not the obvious alternative" for each non-obvious '
               'choice. Stay strictly grounded in the section + the paper context — '
               'no new claims beyond them (use web_search only to verify or fetch a '
               'detail, never to wander). Write Markdown.'),
        'zh': ('把下面这一节报告**往深扩一层**，读者已经看懂了表层版本。补上报告压缩掉的那一层：'
               '逐步推理、边界情况、精确数字、复现者需要的实现细节，以及每个不显然抉择的'
               '「为什么选它而不是显然的备选」。严格以本节与论文上下文为据，不引入超出它们的新断言'
               '（web_search 只用于核实或补齐细节，绝不漫游）。用 Markdown 写。'),
    },
    'derive': {
        'en': ('Derive the mathematics of the report section below STEP BY STEP. '
               'For every equation: start from the previous step or a stated '
               'assumption, show EVERY algebraic move (no "it follows that"), define '
               'every symbol, and give a one-line plain-language gloss per step. If '
               'the paper skips steps, fill them explicitly and flag that the paper '
               'elided them. Use KaTeX ($...$ / $$...$$) for all math. Write Markdown.'),
        'zh': ('把下面这一节报告里的数学**逐步推导**出来。对每个公式：从上一步或明说的假设出发，'
               '展示**每一个**代数动作（禁止「由此可得」），定义每个符号，每步配一行大白话注解。'
               '论文跳步的地方明确补上并标注「论文此处跳步」。所有数学用 KaTeX（$...$ / $$...$$）。'
               '用 Markdown 写。'),
    },
    'eli5': {
        'en': ('Rewrite the report section below for a SMART BEGINNER who knows '
               'nothing of the field. Plain language, one honest everyday analogy '
               'per core concept, every technical term defined inline in three words '
               'the moment it appears, short sentences. Keep every NUMBER and its '
               'meaning accurate — simplicity must never become wrongness. Write Markdown.'),
        'zh': ('把下面这一节报告改写给**完全不懂这个领域但聪明的初学者**。大白话、每个核心概念配一个'
               '诚实的生活化类比、每个术语出现的当下用三五个字就地解释、句子要短。每个**数字**及其含义'
               '必须保持准确——通俗绝不能变成错误。用 Markdown 写。'),
    },
}


def _build_deepen_messages(section, mode, paper_text, report_md, ui_lang):
    """Assemble the deepen message list on top of build_qa_messages.

    The "question" the QA assembler frames is the deepening instruction
    carrying the authoritative section body; the report (full) + the relevant
    paper sections ride the system message with the SAME sanitize/fence/date
    discipline every paper surface uses.
    """
    instr = _INSTR[mode]['zh' if ui_lang == 'zh' else 'en']
    if ui_lang == 'zh':
        question = (f'{instr}\n\n===== 要深挖的报告小节(标题:「{section["heading"]}」) =====\n'
                    + section['text'])
    else:
        question = (f'{instr}\n\n===== THE REPORT SECTION TO DEEPEN (heading: '
                    f'"{section["heading"]}") =====\n' + section['text'])
    messages, _diag = build_qa_messages(question, paper_text, report_md,
                                        history=[], lang=ui_lang)
    return messages


# ── The worker (clone of qa_engine._run_qa_task) ─────────────────────────

def _run_deepen_task(task, messages, *, paper_hash, section, ui_lang):
    """Background worker: run the deepen tool loop and populate task events."""
    task_id = task['task_id']
    _deepen_runtime.mark_running(task_id)
    _append_deepen_event(task, {'type': 'status', 'status': 'running'})

    model = task['model']
    abort_event = task['abort_event']

    def _abort_check():
        return abort_event.is_set()

    model_name = model or _lib.LLM_MODEL
    _agent_usage = PaperAgentUsageMeter.for_stage(
        'deepen', fallback_model=model_name)
    task['agentUsageV1'] = _agent_usage.snapshot()
    t0 = time.time()
    full_content = ''
    mode = task['mode']
    section_idx = task['section_idx']

    abort_signal = AbortSignal.from_event(abort_event)
    paper_epoch = build_paper_full_tool_epoch(
        owner_user_id=task.get('_userId'), model=model_name,
        cfg=task.get('config'))
    task['toolEpochV2'] = paper_epoch.telemetry()
    paper_tools = list(paper_epoch.wire_schemas)
    apply_paper_tool_epoch_guidance(
        messages, paper_epoch, lang=task.get('lang') or 'en')
    _exec_shim = make_paper_exec_shim(task_id=task['task_id'],
                                      abort=abort_signal.is_set,
                                      owner_user_id=task.get('_userId'),
                                      cfg=task.get('config'),
                                      tool_epoch=paper_epoch,
                                      model=model_name)
    _result_budget = PaperToolResultBudgetV2(
        owner_user_id=task.get('_userId'), model=model_name,
        result_envelope=paper_epoch.result_envelope,
        conv_id=task['task_id'])
    task['toolResultPolicyV1'] = _result_budget.telemetry()
    _round = {'content': ''}
    _usage_total = {'prompt_tokens': 0, 'completion_tokens': 0,
                    'cache_read_tokens': 0, 'cache_write_tokens': 0,
                    'reasoning_tokens': 0}
    _resolved_model = ''

    def _acc_usage(usage):
        nonlocal _resolved_model
        if not isinstance(usage, dict):
            return
        from lib.cost import normalize_usage as _nu
        _n = _nu(usage)
        _usage_total['prompt_tokens'] += _n['input']
        _usage_total['completion_tokens'] += _n['output']
        _usage_total['cache_read_tokens'] += _n['cache_read']
        _usage_total['cache_write_tokens'] += _n['cache_write']
        _usage_total['reasoning_tokens'] += _n['thinking']
        _disp = usage.get('_dispatch') or {}
        if _disp.get('model'):
            _resolved_model = _disp['model']

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            nonlocal full_content
            _round['content'] += text
            full_content += text
            task['full_text'] = full_content
            _append_deepen_event(task, {'type': 'delta', 'delta': text})

        logger.info('[Paper:Deepen] Task %s round %d — model=%s msgs=%d',
                    task['task_id'], rnd + 1, model_name, len(messages))
        from lib.llm.stream_result import ensure_provider_stream_result
        return ensure_provider_stream_result(dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=_abort_check,
            prefer_model=model_name if model else None,
            strict_model=bool(model),
            tools=tools,
            max_tokens=16000,
            temperature=0,
            thinking_enabled=False,
            log_prefix='[Paper:Deepen]',
        ))

    def _on_round_result(rnd, msg, finish, usage):
        _acc_usage(usage)
        task['agentUsageV1'] = _agent_usage.snapshot()

    def _begin_tool_round(rnd, msg):
        nonlocal full_content
        round_content = _round['content']
        if round_content:
            full_content = full_content[:-len(round_content)]
            task['full_text'] = full_content
            _append_deepen_event(task, {'type': 'delta_reset'})
        messages.append(msg)

    def _execute_tool(rnd, tc):
        fn_name = tc['function']['name']
        fn_args_raw = tc['function']['arguments']
        tc_id = tc.get('id', '')
        fn_args, _ = parse_and_repair_tool_args(fn_name, fn_args_raw)
        task['round_counter'] += 1
        rn = task['round_counter']
        display_query = _display_query_for(fn_name, fn_args)
        effective_name = paper_effective_tool_name(fn_name)
        round_entry = {
            'roundNum': rn, 'llmRound': rnd,
            'toolName': effective_name, 'query': display_query,
            'toolCallId': tc_id,
            'toolArgs': (fn_args_raw if isinstance(fn_args_raw, str)
                         else json.dumps(fn_args, ensure_ascii=False)),
            'status': 'searching', 'results': None,
        }
        task['tool_rounds'].append(round_entry)
        _append_deepen_event(task, {
            'type': 'tool_start', 'roundNum': rn, 'toolName': effective_name,
            'query': display_query, 'toolCallId': tc_id,
            'toolArgs': round_entry['toolArgs'],
        })
        tool_t0 = time.time()
        result, display_results, search_diag, engine_breakdown, verticals = execute_paper_tool(
            fn_name, fn_args_raw, user_question=(section.get('heading') or '')[:300],
            abort=abort_signal.is_set,
            exec_shim=_exec_shim, round_entry=round_entry)
        tool_elapsed = time.time() - tool_t0
        logger.info('[Paper:Deepen:Tool] %s → %d chars in %.1fs',
                    fn_name, len(result), tool_elapsed)
        tool_status = ('rejected' if round_entry.get('status') == 'rejected'
                       else 'done')
        round_entry['status'] = tool_status
        round_entry['_elapsed'] = f'{tool_elapsed:.1f}s'
        round_entry['results'] = display_results
        round_entry['toolContent'] = result[:4000]
        done_ev = {
            'type': 'tool_done', 'roundNum': rn, 'toolName': effective_name,
            'toolCallId': tc_id, 'elapsed': round(tool_elapsed, 1),
            'toolContent': result[:4000], 'results': display_results,
            'status': tool_status,
        }
        if round_entry.get('contractError'):
            done_ev['contractError'] = round_entry['contractError']
        if search_diag:
            done_ev['searchDiag'] = search_diag
        if engine_breakdown:
            done_ev['engineBreakdown'] = engine_breakdown
        if verticals:
            done_ev['verticals'] = verticals
        _append_deepen_event(task, done_ev)
        _result_budget.append(
            messages, round_index=rnd, tool_name=fn_name,
            tool_call_id=tc_id, content=result, round_entry=round_entry,
            world_version=str(task.get('_worldVersion') or ''),
            tool_arguments=fn_args)

    try:
        _outcome = run_guarded_paper_agent_loop(
            context='Paper Deepen agent',
            allow_aborted_outcome=True,
            usage_meter=_agent_usage,
            abort=abort_signal,
            round_tools=paper_tools,
            dispatch=_dispatch,
            execute_tool=_execute_tool,
            on_round_result=_on_round_result,
            on_tool_round=_begin_tool_round,
            on_round_end=_result_budget.finish_round,
        )
        aborted = _outcome.aborted
        if aborted:
            _deepen_runtime.abort(task_id)
            _deepen_runtime.finish(
                task_id,
                terminal_event_fields={
                    'type': 'aborted', 'partial': full_content,
                    'agentUsageV1': _agent_usage.snapshot(),
                },
            )
            return

        elapsed = time.time() - t0
        logger.info('[Paper:Deepen] Task %s complete — %d chars, %.1fs',
                    task['task_id'], len(full_content), elapsed)

        report_model = _resolved_model or model or _lib.LLM_MODEL
        # Persist the cache row + accumulate cost into the report's
        # secondPasses.deepen (design §3.3).
        cache_isolated = (
            (task.get('requestPolicyV1') or {}).get('cacheMode')
            == 'request_local')
        if not cache_isolated:
            _write_deepen_cache(
                paper_hash, mode, section_idx, ui_lang,
                section.get('hash') or '', full_content,
                dict(_usage_total), report_model,
                user_id=int(task['_userId']))
            _accumulate_deepen_cost(
                paper_hash, task['lang'], dict(_usage_total), report_model,
                user_id=int(task['_userId']))
        else:
            logger.info(
                '[Paper:Deepen] Request-local policy — cache/cost mutation '
                'suppressed hash=%s policy=%s',
                paper_hash, str(task.get('execution_fingerprint') or '')[:12])

        _deepen_runtime.finish(
            task_id,
            result=full_content,
            terminal_event_fields={
                'type': 'done', 'content': full_content,
                'paperHash': paper_hash, 'sectionIdx': section_idx, 'mode': mode,
                'usage': dict(_usage_total), 'model': report_model,
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )

    except AbortedError:
        _deepen_runtime.abort(task_id)
        _deepen_runtime.finish(
            task_id,
            terminal_event_fields={
                'type': 'aborted', 'partial': full_content,
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )
    except Exception as e:
        logger.error('[Paper:Deepen] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-deepen',
            source='routes.paper:deepen',
        )
        _deepen_runtime.finish(
            task_id,
            error=envelope,
            error_context='paper-deepen',
            terminal_event_fields={
                'agentUsageV1': _agent_usage.snapshot(),
            },
        )
    finally:
        task['agentUsageV1'] = _agent_usage.snapshot()
        with _deepen_dedup_lock:
            dedup_key = task.get('_dedupKey')
            if isinstance(dedup_key, tuple):
                _deepen_dedup.pop(dedup_key, None)


def start_deepen(paper_hash, lang, mode, section_idx, paper_text, *,
                 model=None, ui_lang=None, user_id: int, config=None):
    """Validate → cache-check → spawn a deepen task (or return the cache hit).

    Returns one of:
      {'cached': True, 'content': ..., 'usage': ..., 'section': heading}
      {'task': task_dict}                      — freshly spawned
      {'joined': task_dict}                    — already in flight
      {'error': (message, http_status)}        — validation failure
    """
    if mode not in DEEPEN_MODES:
        return {'error': (f'unknown deepen mode: {mode}', 400)}
    ui_lang = ui_lang or ('zh' if lang == 'zh' else 'en')

    # The stored report is authoritative — never a client-supplied body.
    try:
        from lib.paper.artifact_repository import PaperArtifactRepository
        row = PaperArtifactRepository(user_id).get_report(paper_hash, lang)
    except Exception as e:
        logger.warning('[Paper:Deepen] report lookup failed hash=%s: %s', paper_hash, e)
        row = None
    if not row or not row.report:
        return {'error': ('no generated report for this paper+language yet', 409)}
    report_md = row.report

    section = extract_report_section(report_md, section_idx)
    if not section:
        return {'error': (f'section index {section_idx} out of range', 400)}

    request_policy = paper_request_policy_telemetry(
        model=model, config=config or {})
    cache_isolated = request_policy['cacheMode'] == 'request_local'
    cached = None if cache_isolated else read_deepen_cache(
        paper_hash, mode, section_idx, ui_lang,
        section['hash'], user_id=user_id)
    if cached:
        return {'cached': True, 'content': cached['content'],
                'usage': cached.get('usage'), 'section': section['heading'],
                'mode': mode, 'sectionIdx': section_idx}

    dedup_key = (
        user_id, paper_hash, lang, mode, section_idx,
        request_policy['executionFingerprint'])
    with _deepen_dedup_lock:
        existing_id = _deepen_dedup.get(dedup_key)
        existing = _deepen_runtime.get(existing_id) if existing_id else None
        if existing and existing['status'] in ('pending', 'running'):
            return {'joined': existing}
        task_id = f'deepen_{uuid.uuid4().hex[:16]}'
        task = _new_deepen_task(task_id, paper_hash, lang, model,
                                section_idx=section_idx, mode=mode,
                                section_heading=section['heading'],
                                user_id=user_id, config=config)
        task['_dedupKey'] = dedup_key
        _deepen_dedup[dedup_key] = task_id

    messages = _build_deepen_messages(section, mode, paper_text or '',
                                      report_md, ui_lang)
    import threading as _th
    worker = _th.Thread(target=_run_deepen_task,
                        args=(task, messages),
                        kwargs={'paper_hash': paper_hash, 'section': section,
                                'ui_lang': ui_lang},
                        daemon=True)
    worker.start()
    logger.info('[Paper:Deepen] Started — task=%s hash=%s mode=%s sec=%d (%s)',
                task_id, paper_hash, mode, section_idx, section['heading'])
    return {'task': task}
