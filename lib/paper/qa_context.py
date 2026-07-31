"""Section-aware context assembly for paper Q&A.

The legacy Q&A path stuffed ``paper_text[:100000]`` into a system prompt —
silently dropping the tail of long papers, and never showing the model the
generated report (so "what did you mean in the Limitations section?" could not
be answered). This module fixes both:

  • ``split_into_sections`` — break the parsed paper (pymupdf4llm Markdown, so
    it carries ``#``/``##`` headings) into titled sections; falls back to
    fixed-size chunks when a paper has no headings.
  • ``select_relevant_sections`` — rank sections by token overlap with the
    question + recent dialogue (same cheap lexical scoring proven in
    ``arxiv._rerank_by_title``), always keep the head (title/abstract/intro),
    and greedily fill a character budget — so a long paper contributes its
    *relevant* sections instead of just its first 100k chars.
  • ``build_qa_messages`` — assemble the final message list: a system prompt
    carrying the FULL report (small, always kept) + the selected paper
    sections, then the recent dialogue.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

# Defaults tuned so the whole prompt (report + sections + history) stays within
# a comfortable window. The report is kept in full; paper sections fill the
# rest up to this budget.
_DEFAULT_SECTION_BUDGET = 60000
_REPORT_CAP = 60000          # generated reports run ~40-55k chars; cap defensively
_FALLBACK_CHUNK_CHARS = 3500
_MAX_HISTORY_MSGS = 10


def _token_set(s):
    """Lowercase alphanumeric token set for lexical overlap scoring."""
    return set(re.findall(r'[a-z0-9]+', (s or '').lower()))


def split_into_sections(text):
    """Split parsed paper text into ``[{heading, body, text, index}]``.

    Primary strategy: split on Markdown ATX headings (``#``..``######``),
    which pymupdf4llm emits for real paper sections. Each section spans from
    one heading to the next. Any preamble before the first heading becomes
    section 0 (typically title + abstract).

    Fallback (no headings, e.g. ``fast`` text mode): fixed-size character
    chunks on paragraph boundaries so retrieval still has units to rank.

    Returns a list of dicts; ``text`` is the heading+body slice used both for
    scoring and for assembly.
    """
    text = text or ''
    if not text.strip():
        return []

    heading_re = re.compile(r'^(#{1,6})\s+(.+?)\s*$', re.MULTILINE)
    matches = list(heading_re.finditer(text))

    sections = []
    if matches:
        first = matches[0]
        if first.start() > 0:
            pre = text[:first.start()].strip()
            if pre:
                sections.append({'heading': '', 'body': pre, 'text': pre})
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunk = text[start:end].strip()
            if not chunk:
                continue
            sections.append({
                'heading': m.group(2).strip(),
                'body': text[m.end():end].strip(),
                'text': chunk,
            })
    else:
        # No headings — chunk on blank lines into ~_FALLBACK_CHUNK_CHARS pieces.
        paras = re.split(r'\n\s*\n', text)
        buf = ''
        for p in paras:
            if buf and len(buf) + len(p) > _FALLBACK_CHUNK_CHARS:
                sections.append({'heading': '', 'body': buf.strip(), 'text': buf.strip()})
                buf = p
            else:
                buf = (buf + '\n\n' + p) if buf else p
        if buf.strip():
            sections.append({'heading': '', 'body': buf.strip(), 'text': buf.strip()})

    for i, s in enumerate(sections):
        s['index'] = i
    return sections


def select_relevant_sections(question, sections, *, history=None,
                             budget_chars=_DEFAULT_SECTION_BUDGET,
                             always_keep_head=2):
    """Rank sections by overlap with the question + recent history, fill budget.

    Returns the selected sections IN DOCUMENT ORDER (so the assembled context
    reads coherently). The first ``always_keep_head`` sections (title /
    abstract / intro) are always included regardless of score — they anchor
    the paper's identity. Remaining budget is filled by descending relevance.

    If everything fits in the budget, all sections are returned (no dropping)
    — the whole point is to avoid the silent tail-loss of blind truncation.
    """
    if not sections:
        return []

    total = sum(len(s['text']) for s in sections)
    if total <= budget_chars:
        return list(sections)  # whole paper fits — keep it all, in order

    query_tokens = _token_set(question)
    # Recent dialogue widens the query so follow-ups ("and its ablations?")
    # still retrieve the right section.
    for msg in (history or [])[-4:]:
        query_tokens |= _token_set(msg.get('content', '') if isinstance(msg, dict) else str(msg))

    def _score(sec):
        st = _token_set(sec['text'])
        if not st:
            return 0.0
        overlap = len(query_tokens & st)
        # Normalize by sqrt(len) so huge sections don't always win on raw count.
        return overlap / (len(st) ** 0.5)

    # Head-anchoring (title/abstract/intro) must NOT starve relevance: cap how
    # much of the budget the always-keep-head sections may consume. In a real
    # paper the abstract/intro are small and fit easily; this cap only kicks in
    # for pathological inputs (huge leading sections) so a relevant LATER
    # section is still retrievable — the long-paper tail guarantee.
    head_reserve = budget_chars * 0.4
    chosen = set()
    used = 0
    for i in range(min(always_keep_head, len(sections))):
        size = len(sections[i]['text'])
        if used + size <= head_reserve or (used == 0 and size <= budget_chars):
            chosen.add(i)
            used += size

    ranked = sorted(
        (s for s in sections if s['index'] not in chosen),
        key=_score, reverse=True)
    for sec in ranked:
        size = len(sec['text'])
        if used + size > budget_chars:
            # Skip this one (too big for remaining budget) but keep scanning —
            # a smaller, also-relevant section later may still fit.
            continue
        chosen.add(sec['index'])
        used += size

    selected = [s for s in sections if s['index'] in chosen]
    logger.info('[Paper:QA] Section select — %d/%d sections, %d/%d chars '
                '(budget %d), q_tokens=%d',
                len(selected), len(sections), used, total, budget_chars,
                len(query_tokens))
    return selected


def _render_sections(sections, all_sections_count):
    """Render selected sections to a context string, flagging any omissions."""
    parts = []
    prev_idx = -1
    for s in sections:
        if s['index'] != prev_idx + 1 and prev_idx >= 0:
            parts.append('\n\n[… omitted sections not relevant to this question …]\n')
        parts.append(s['text'])
        prev_idx = s['index']
    return '\n\n'.join(parts)


def build_qa_messages(question, paper_text, report_md, *, history=None,
                      lang='en', section_budget=_DEFAULT_SECTION_BUDGET):
    """Build the message list for an agentic Q&A turn.

    Injects BOTH the generated report (in full, capped at ``_REPORT_CAP``) and
    the question-relevant paper sections. The model is told it has the full
    standard tool set (web_search / fetch_url / read_files / code_exec / …)
    and should use them for anything outside the paper.

    Returns ``(messages, diag)`` where ``diag`` is a small dict for tests /
    logging (n_sections_total, n_sections_selected, report_present, …).
    """
    history = history or []
    # ── Prompt-injection hardening (untrusted PDF text) ──
    # The paper text is UNTRUSTED: a submitted PDF can embed directives aimed
    # at the LLM ("ignore previous instructions", "give a positive review",
    # hidden white/zero-width text). Sanitize the WHOLE text ONCE up front —
    # BEFORE split_into_sections — so directives are defanged and invisible
    # carriers stripped without fence markers leaking into the heading/section
    # split logic. The assembled section context is then fenced as untrusted
    # data, and a hard-constraint clause is prepended to the system prompt.
    from .injection_guard import injection_notice, sanitize_paper_text, wrap_untrusted
    paper_text, _inj_findings = sanitize_paper_text(paper_text)
    sections = split_into_sections(paper_text)
    selected = select_relevant_sections(
        question, sections, history=history, budget_chars=section_budget)
    paper_context = wrap_untrusted(_render_sections(selected, len(sections)))

    report_block = ''
    report_present = bool(report_md and report_md.strip())
    if report_present:
        rep = report_md.strip()
        if len(rep) > _REPORT_CAP:
            rep = rep[:_REPORT_CAP] + '\n\n[report truncated]'
        report_block = (
            '\n\n===== GENERATED ANALYSIS REPORT (already shown to the user) =====\n'
            'The user is reading the structured report below. When they ask about '
            '"the report", a specific section (e.g. "Limitations", "the TL;DR"), or '
            '"what did you mean by X", answer from THIS report — it is what they see.\n\n'
            + rep)

    if lang == 'zh':
        sys_head = (
            '你是一位严谨的科研助手。用户正在阅读一篇学术论文，下面给出论文相关章节'
            '与系统已生成的分析报告。请基于这些材料回答用户的问题，做到具体、可引用到'
            '具体章节/表格。当问题涉及报告里的措辞（如"Limitations 一节是什么意思"），'
            '直接依据报告内容解释。\n\n'
            '你拥有完整的标准工具集。当问题需要论文之外的信息（最新进展、'
            '他人复现、引用数、相关工作的细节、某链接里到底有什么）时，主动用 '
            'web_search / fetch_url 检索，不要凭空作答；用 read_files 打开抓取落盘'
            '（或超长结果溢出落盘）的本地文件，用 code_exec 做数值核验；论文/报告里'
            '已有的内容则无需联网。回答用中文。')
        paper_label = '\n\n===== 论文相关章节 =====\n'
    else:
        sys_head = (
            'You are a rigorous research assistant. The user is reading an academic '
            'paper; below are the relevant paper sections AND the structured analysis '
            'report the system already generated. Answer from these materials — be '
            'specific and cite the section / table. When the question is about the '
            'report\'s wording (e.g. "what did you mean in the Limitations section?"), '
            'answer directly from the report below — it is what the user sees.\n\n'
            'You have the full standard tool set. When a question needs information '
            'beyond the paper (recent follow-ups, reproductions, citation counts, '
            'details of related work, what a given link actually contains), use '
            'web_search / fetch_url rather than guessing; use read_files to open any '
            'local file a fetch stages or an oversized tool result spills to disk, '
            'and code_exec when a numeric check beats prose. For content already in '
            'the paper/report, no web access is needed.')
        paper_label = '\n\n===== RELEVANT PAPER SECTIONS =====\n'

    # The date anchor goes FIRST: Q&A does time-relative reasoning (the system
    # prompt tells the model to web_search for "recent follow-ups, reproductions,
    # citation counts"), and like the report/review engines this message is
    # self-assembled and never inherits the chat `Current date:` block. Without
    # it the model conflates the paper's PUBLICATION date with "now" and refuses
    # to surface post-publication work when asked "有没有后续工作/被谁超越了".
    # Then the input-safety clause, so the model reads the untrusted-data framing
    # before any paper content — the fenced paper sections below are data to
    # answer FROM, never instructions to obey.
    from .prompts import date_anchor_clause
    ui_lang = 'zh' if lang == 'zh' else 'en'
    notice = injection_notice(ui_lang, _inj_findings)
    system_content = (date_anchor_clause(ui_lang) + notice + sys_head
                      + report_block + paper_label + paper_context)
    messages = [{'role': 'system', 'content': system_content}]

    for msg in history[-_MAX_HISTORY_MSGS:]:
        role = msg.get('role') if isinstance(msg, dict) else None
        content = msg.get('content') if isinstance(msg, dict) else None
        if role in ('user', 'assistant') and content:
            messages.append({'role': role, 'content': content})

    # The current question is appended by the caller as the last user turn IF
    # not already present in history; here we ensure it's the final message.
    if not (messages[-1]['role'] == 'user' and messages[-1]['content'] == question):
        messages.append({'role': 'user', 'content': question})

    diag = {
        'n_sections_total': len(sections),
        'n_sections_selected': len(selected),
        'paper_context_chars': len(paper_context),
        'report_present': report_present,
        'system_chars': len(system_content),
    }
    logger.info('[Paper:QA] Built messages — sections %d/%d, paper %d chars, '
                'report=%s, system=%d chars',
                diag['n_sections_selected'], diag['n_sections_total'],
                diag['paper_context_chars'], report_present, diag['system_chars'])
    return messages, diag
