"""Checkpoint second pass — per-section self-test flip cards (design P2,
docs/PAPER_READING_EXPERIENCE_DESIGN.md).

The fidelity report is a PASSIVE read; the cheapest proven way to make
understanding stick is active recall. After a report completes, this pass asks
the model — ONE bounded, no-tool call — for a handful of self-test questions,
one per important section, each with a concise answer that carries the
specific number / mechanism from the text. The model NOMINATES the section by
heading text; the deterministic resolver from the insight pass
(``insight_engine._anchors.resolve_anchor``) decides where each card actually
lands — a nomination that does not resolve is DROPPED, never guessed.

Design commitments (mirrors the insight pass deliberately):

  * **One no-tool call**, temp 0.45, with the one-shot repair re-ask at temp 0
    on strict-JSON parse failure (the reliability recipe the insight and
    termfill passes already proved). No agentic search — every answer must
    come from the report itself.
  * **Anchors are resolved, not trusted** — ``anchor_idx`` is computed HERE
    against the report's real h2/h3 sequence and persisted with the row; the
    read path never re-resolves.
  * **Separate-key persistence** (``checkpoints:<ui_lang>``, v2 meta with the
    structured items + usage) — the primary report body is byte-identical
    whether this runs or not, and the frontend distributes flip cards from
    the structured payload (no body merge).
  * **Three-level enable chain** (design §3.4, Settings toggle retired
    2026-08-06): per-request cfg stamp (``paperCheckpointsEnabled``, headless
    fail-closed) > env ``TOFU_PAPER_CHECKPOINTS`` > interactive default ON.
  * **Cost visibility** — the call's usage is returned so
    ``report_engine._hooks._merge_second_pass`` can fold it into the report
    meta's secondPasses breakdown.

Every failure path leaves a trace per CLAUDE.md §2; this module is best-effort
and must NEVER taint the already-finished report.
"""

from __future__ import annotations

import json
import os
import time

from lib.agent_loop import AbortSignal
from lib.llm_errors import AbortedError
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .insight_engine._anchors import resolve_anchor, extract_report_headings

logger = get_logger(__name__)

__all__ = [
    'checkpoints_enabled',
    'checkpoints_lang_key',
    'run_report_checkpoints',
]

_LANG_PREFIX = 'checkpoints'
_TEMPERATURE = 0.45          # mirrors insight: some divergence, JSON still reliable
_REPAIR_MAX_TOKENS = 4000
_MAX_TOKENS = 4000
_MAX_CARDS = 8               # never bury the reader in pop-quiz cards


def checkpoints_lang_key(ui_lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted checkpoint set."""
    return f'{_LANG_PREFIX}:{ui_lang or "en"}'


def checkpoints_enabled(cfg=None) -> bool:
    """Three-level enable chain (mirrors ``insight_engine._config.insight_enabled``):

      1. explicit per-request cfg ``paperCheckpointsEnabled`` (headless stamp /
         opt-in) — always wins;
      2. env ``TOFU_PAPER_CHECKPOINTS`` (fleet kill switch);
      3. interactive default ON (Settings toggle retired 2026-08-06).
    """
    if isinstance(cfg, dict) and 'paperCheckpointsEnabled' in cfg:
        return bool(cfg['paperCheckpointsEnabled'])
    env = (os.environ.get('TOFU_PAPER_CHECKPOINTS', '') or '').strip().lower()
    if env:
        return env in ('1', 'true', 'yes', 'on')
    return True


_SYSTEM_EN = """\
You are a great reading-group leader writing SELF-TEST questions for a paper explainer report (given below). Your ONE job: help the reader verify they actually understood each important section — active recall, not trivia.

Rules:
- Pick the {n} sections that matter MOST for understanding the paper (typically Method, Experiments, Design Choices, Limitations). Skip TL;DR, the Paper Card, the Terminology table, and the Technical Reference appendix.
- For each picked section write ONE self-test question: the kind a strong discussant asks to check real understanding (a "why", a "how does X produce Y", a "which design choice forces Z", a "what does the number mean"), NOT a factoid lookup.
- Write ONE concise answer (1-3 sentences) per question, carrying the SPECIFIC number / mechanism / name from the report — an answer that only says "see the section" is a failure.
- Copy the section heading EXACTLY as it appears in the report (without the leading ##) into ``section``.

Respond with STRICT JSON ONLY (no prose, no code fences) as your FINAL message:
{
  "checkpoints": [
    {"section": "<exact heading text>",
     "question": "<the self-test question>",
     "answer": "<1-3 sentence answer with the specific detail>"}
  ]
}"""

_SYSTEM_ZH = """\
你是一位出色的读书会领读人，正在为一篇论文讲解报告（见下）写**自测题**。你唯一的任务：帮读者验证自己是否真正读懂了每个重要小节——是主动回忆，不是 trivia 问答。

规则：
- 挑出对理解这篇论文**最重要**的 {n} 个小节（通常是方法、实验、设计抉择、局限）。跳过摘要、论文卡片、术语表和技术附录。
- 每个挑中的小节出**一道**自测题：一个好的领读者用来检验真理解的那种（一个“为什么”、一个“X 如何产生 Y”、一个“哪个设计抉择迫使了 Z”、一个“这个数字意味着什么”），**不要**事实背诵题。
- 每题配**一个**简洁答案（1-3 句），必须带上报告里**具体**的数字/机制/名称——只说“见该节”的答案是失败品。
- 把小节标题**逐字照抄**（不带前导 ##）填进 ``section``。

只返回 STRICT JSON（无散文、无代码围栏），作为你的**最后一条消息**：
{
  "checkpoints": [
    {"section": "<逐字的小节标题>",
     "question": "<自测题>",
     "answer": "<1-3 句带具体细节的答案>"}
  ]
}"""


def _parse_json_obj(text):
    from lib.llm.json_extract import extract_first_json_object
    obj = extract_first_json_object(text or '', log_prefix='[Paper:Checkpoints]',
                                    log=logger)
    if isinstance(obj, dict) and isinstance(obj.get('checkpoints'), list):
        return obj['checkpoints']
    if isinstance(obj, list):
        return obj
    return []


def run_report_checkpoints(report_md, ui_lang='en', *, phash='', model=None,
                           abort=None, persist=True) -> dict:
    """Generate → anchor → persist per-section checkpoint cards. Best-effort.

    Returns ``{'items': [{section, anchor_idx, question, answer}], 'usage':
    dict|None, 'persisted': bool, 'llmError': bool}`` — ``items`` empty when
    nothing usable was produced (never an exception into the report path).
    """
    out = {'items': [], 'usage': None, 'persisted': False, 'llmError': False}
    report_md = (report_md or '').strip()
    if not report_md:
        return out

    headings = extract_report_headings(report_md)
    if len(headings) < 3:
        logger.info('[Paper:Checkpoints] report too short for section cards — hash=%s', phash)
        return out

    zh = ui_lang == 'zh'
    n = min(_MAX_CARDS, max(3, len(headings) - 2))
    system = (_SYSTEM_ZH if zh else _SYSTEM_EN).replace('{n}', str(n))
    # Give the model the heading list so its nominations copy real headings.
    heading_list = '\n'.join(f'- {h["text"]}' for h in headings)
    user = ('## 报告的小节标题（从这里挑并逐字照抄）\n' if zh
            else '## The report section headings (pick from these; copy exactly)\n') \
        + heading_list + '\n\n---\n\n' + report_md[:80000]
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user}]

    abort_signal = AbortSignal.from_callback(abort)
    usage_acc = {'prompt_tokens': 0, 'completion_tokens': 0,
                 'cache_read_tokens': 0, 'cache_write_tokens': 0,
                 'reasoning_tokens': 0}

    def _acc(usage):
        if not isinstance(usage, dict):
            return
        try:
            from lib.cost import normalize_usage as _nu
            _n = _nu(usage)
            usage_acc['prompt_tokens'] += _n['input']
            usage_acc['completion_tokens'] += _n['output']
            usage_acc['cache_read_tokens'] += _n['cache_read']
            usage_acc['cache_write_tokens'] += _n['cache_write']
            usage_acc['reasoning_tokens'] += _n['thinking']
        except Exception as e:
            logger.debug('[Paper:Checkpoints] usage accumulate failed: %s', e)

    def _dispatch(msgs, *, temperature, max_tokens, prefix):
        buf = {'content': ''}

        def _on_content(text):
            buf['content'] += text

        _msg, _finish, usage = dispatch_stream(
            msgs,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            prefer_model=model or None,
            strict_model=bool(model),
            capability='text',
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=False,
            log_prefix=prefix,
        )
        _acc(usage)
        content = buf['content'] or (_msg.get('content') if isinstance(_msg, dict) else '')
        return content

    try:
        content = _dispatch(messages, temperature=_TEMPERATURE,
                            max_tokens=_MAX_TOKENS, prefix='[Paper:Checkpoints]')
        cards = _parse_json_obj(content)
        if not cards:
            logger.info('[Paper:Checkpoints] unparseable reply — one-shot repair re-ask')
            repair = list(messages)
            repair.append({'role': 'assistant', 'content': (content or '')[:6000]})
            repair.append({'role': 'user', 'content':
                           'Your previous reply could not be parsed as JSON. Reply with ONLY '
                           'the single JSON object described earlier — starting with { and '
                           'ending with } and nothing else.'})
            content = _dispatch(repair, temperature=0.0,
                                max_tokens=_REPAIR_MAX_TOKENS,
                                prefix='[Paper:Checkpoints:Repair]')
            cards = _parse_json_obj(content)
    except AbortedError:
        logger.info('[Paper:Checkpoints] aborted — hash=%s', phash)
        return out
    except Exception as e:
        logger.error('[Paper:Checkpoints] dispatch failed hash=%s: %s', phash, e, exc_info=True)
        out['llmError'] = True
        return out

    out['usage'] = dict(usage_acc)

    # Deterministic anchor resolution: keep cards whose section nomination
    # resolves to a real heading; drop the rest (log) — never guess.
    items = []
    for c in cards[: _MAX_CARDS * 2]:   # bound the loop even on a runaway list
        if not isinstance(c, dict):
            continue
        question = (c.get('question') or '').strip()
        answer = (c.get('answer') or '').strip()
        section = (c.get('section') or '').strip()
        if not question or not answer:
            continue
        idx = resolve_anchor(section, headings) if section else None
        if idx is None:
            logger.info('[Paper:Checkpoints] dropping card with unresolved section: %.80s',
                        section or '(none)')
            continue
        items.append({'section': headings[idx]['text'], 'anchor_idx': idx,
                      'question': question, 'answer': answer})
        if len(items) >= _MAX_CARDS:
            break
    out['items'] = items
    logger.info('[Paper:Checkpoints] hash=%s — %d/%d cards anchored', phash,
                len(items), len(cards) if isinstance(cards, list) else 0)

    if persist and items:
        try:
            from lib.database import get_thread_db
            from lib.database._core_schema import PAPER_REPORTS, upsert
            meta = {'kind': 'checkpoints', 'v': 2, 'items': items,
                    'usage': out['usage']}
            db = get_thread_db()
            upsert(db, PAPER_REPORTS, {
                'paper_hash': phash,
                'lang': checkpoints_lang_key(ui_lang),
                # The report column carries a compact markdown rendering so the
                # row is human-inspectable; the structured items ride meta.
                'report': '\n'.join(
                    ['## 🧠 Checkpoints' if not zh else '## 🧠 随堂检查', ''] +
                    [f"- **{i['section']}** — {i['question']} → {i['answer']}"
                     for i in items]) + '\n',
                'model': model or '',
                'meta': json.dumps(meta, ensure_ascii=False),
                'created_at': int(time.time()),
            }, retry=True)
            out['persisted'] = True
            logger.info('[Paper:Checkpoints] Persisted — hash=%s key=%s %d cards',
                        phash, checkpoints_lang_key(ui_lang), len(items))
        except Exception as e:
            logger.warning('[Paper:Checkpoints] persist failed hash=%s: %s', phash, e)
    return out
