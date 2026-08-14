"""Definition-backfill second pass — CURE the glossary gaps the audit flags.

``terminology_audit`` makes the gap VISIBLE (step 1): it labels the failure with
a warning card. This module CURES it (step 2). When the audit returns gaps, a
bounded, PURE-BODY-CONTEXT LLM call generates definitions for the missing /
dangling terms, and an addendum glossary table is appended to the report — but
ONLY the subset of definitions that PROVABLY close a gap.

"Provably" is the whole point, and it is enforced by a RE-AUDIT GATE, not by
trusting the LLM: after building a candidate addendum we re-run
``build_terminology_audit`` on ``body + addendum`` and keep a proposed row iff

  (a) it removes a real gap (its term was missing, or it was the dangling
      referent of a glossary definition), AND
  (b) it does NOT itself introduce a NEW undefined term (a definition that
      leans on a fresh acronym would trade one gap for another — rejected).

The north star is the report being SELF-CONTAINED, not merely annotated, so a
backfill that ran but did not shrink the gap set is NOT a success — it returns
``''`` and the warning card is retained.

Design (mirrors ``insight_engine`` deliberately — reuse, don't hand-roll):
  * PURE BODY CONTEXT: the terms are used *in the report*, so their definitions
    should be derivable from the report. No agentic search, no ``run_agent_loop``,
    no arXiv grounding, no pymupdf/thread exposure — one ``dispatch_chat`` call.
  * temp=0.45 + one repair-reask at temp=0 on strict-JSON parse failure (the
    same reliability recipe the insight pass uses).
  * SEPARATE-KEY persistence (``termfill:<ui>``) + cache-reopen merge, so the
    primary persisted report body is byte-identical whether or not this runs.
  * Flag-gated OFF by default (``TOFU_PAPER_TERMFILL``), skipped in Review Mode,
    fully wrapped so a failure never taints the emitted report.
"""

from __future__ import annotations

import os
import time
import uuid

from lib.llm_json import extract_json
from lib.log import get_logger

from .terminology_audit import (
    _extract_acronyms,
    _strip_code,
    build_terminology_audit,
)

logger = get_logger(__name__)

__all__ = [
    'termfill_globally_disabled',
    'termfill_lang_key',
    'build_backfill_addendum',
    'run_report_termfill',
]


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)

_TERMFILL_LANG_PREFIX = 'termfill'
# The addendum header intentionally contains "glossary" so the audit's
# _GLOSSARY_HDR_RE treats it as a glossary section: its rows then count as
# DEFINED (enabling the re-audit to see closed gaps) AND its own cells are NOT
# scanned as body usage (a fresh undefined term in a kept row is caught only via
# the explicit _definition_adds_new_gap check, by design).
_ADDENDUM_HEADER_EN = '## 📖 Added definitions (glossary backfill)'
_ADDENDUM_HEADER_ZH = '## 📖 补充术语定义（术语表补全）'

# Max terms per definition-generation call. Real reports carry 10-50 genuine
# gaps AFTER precision suppression; asking for all of them in ONE strict-JSON
# reply blows the token budget and the reply truncates to unparseable (measured:
# 70-term request → 0 parseable defs, 15-term request → 15 defs). Chunking keeps
# every reply small enough to close cleanly; the re-audit gate then runs ONCE
# over the merged result.
_MAX_TERMS_PER_CALL = 10


def termfill_globally_disabled() -> bool:
    """Fleet-wide KILL SWITCH for the backfill pass (default: NOT disabled).

    The per-request decision lives in
    ``lib.agent_core.personal_scope.resolve_paper_termfill_enabled`` (interactive
    ON, headless opt-in). This env var is a separate master override so an
    operator can force-DISABLE the feature fleet-wide regardless of that
    resolution — set ``TOFU_PAPER_TERMFILL=0`` (or ``off``/``false``/``no``).
    Absent or any truthy value → NOT disabled (the measured default-on stands).
    """
    return os.environ.get('TOFU_PAPER_TERMFILL', '').strip().lower() in (
        '0', 'off', 'false', 'no')


def termfill_lang_key(ui_lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted backfill addendum.

    ``termfill:<ui_lang>`` — a separate row from the plain report, mirroring
    ``insight:<ui>`` / ``review:<venue>:<uilang>``, so persisting the addendum
    NEVER overwrites the fidelity report.
    """
    return f'{_TERMFILL_LANG_PREFIX}:{ui_lang or "en"}'


def _gap_terms(audit: dict) -> set[str]:
    """The set of term keys (upper-cased) the audit wants defined."""
    terms = {(m.get('term') or '').upper() for m in (audit.get('missing') or [])}
    terms |= {(d.get('referencedTerm') or '').upper() for d in (audit.get('dangling') or [])}
    terms.discard('')
    return terms


def _backfill_prompt(report_text: str, gap_terms: list[str], ui_lang: str) -> list[dict]:
    """Build the pure-body-context strict-JSON backfill request.

    The model is given the FULL report and the exact list of terms to define,
    and is told to define each SOLELY from how the report uses it, in prose that
    introduces no further undefined jargon. Output is strict JSON
    ``{term: definition}``.
    """
    zh = ui_lang == 'zh'
    terms_line = ', '.join(gap_terms)
    if zh:
        sys = (
            '你在校对一篇论文讲解报告的术语自洽性。报告正文用到了下列术语，但术语表里没有'
            '（或其定义又引用了未定义的术语）。请**仅依据报告正文如何使用这些术语**，为每个'
            '术语写一句自洽的定义：定义本身**不得引入新的未定义缩写/术语**，要让完全没读过'
            '原文的读者也能看懂。无法从正文可靠推断的术语就省略，不要臆造。\n\n'
            f'需要定义的术语：{terms_line}\n\n'
            '只返回 STRICT JSON（无散文、无代码围栏），形如 '
            '{"术语": "一句定义", ...}，作为你的最后一条消息。'
        )
    else:
        sys = (
            'You are checking the terminology self-containment of a paper '
            'explainer report. The body uses the terms below, but they are '
            'missing from the glossary (or a glossary definition leans on them). '
            'Define each term using ONLY how the report itself uses it, in ONE '
            'self-contained sentence that introduces NO further undefined '
            'acronym/jargon, understandable to a reader who never saw the '
            'original paper. Omit any term you cannot reliably infer from the '
            'body — never invent.\n\n'
            f'Terms to define: {terms_line}\n\n'
            'Respond with STRICT JSON ONLY (no prose, no code fences), shaped '
            '{"TERM": "one-sentence definition", ...}, as your final message.'
        )
    user = ('Report:\n\n' + (report_text or ''))[:60000]
    return [{'role': 'system', 'content': sys},
            {'role': 'user', 'content': user}]


def _parse_json_obj(text: str) -> dict:
    """Best-effort strict-JSON object extraction (tolerates code fences / prose)."""
    obj = extract_json(text)
    return obj if isinstance(obj, dict) else {}


def _collect_usage(usage_sink, usage):
    """Append a normalized usage dict to the optional sink (design §3.3)."""
    if usage_sink is None or not isinstance(usage, dict):
        return
    try:
        from lib.cost import normalize_usage as _nu
        _n = _nu(usage)
        usage_sink.append({'prompt_tokens': _n['input'],
                           'completion_tokens': _n['output'],
                           'cache_read_tokens': _n['cache_read'],
                           'cache_write_tokens': _n['cache_write'],
                           'reasoning_tokens': _n['thinking']})
    except Exception as e:
        logger.debug('[Paper:TermFill] usage collect failed (non-fatal): %s', e)


def _generate_definitions(report_text, gap_terms, ui_lang, model, dispatch,
                          usage_sink=None) -> dict:
    """Call the LLM once (temp 0.45) + one repair-reask (temp 0) → {term: def}."""
    messages = _backfill_prompt(report_text, gap_terms, ui_lang)
    try:
        content, _usage = dispatch(messages, max_tokens=1500, temperature=0.45,
                                   prefer_model=model, strict_model=bool(model),
                                   log_prefix='[Paper:TermFill]')
        _collect_usage(usage_sink, _usage)
    except Exception as e:
        logger.warning('[Paper:TermFill] dispatch failed: %s', e)
        return {}
    obj = _parse_json_obj(content)
    if not obj:
        # One repair re-ask at temp 0 (mirrors the insight pass).
        try:
            content2, _usage2 = dispatch(
                messages + [{'role': 'assistant', 'content': content or ''},
                            {'role': 'user', 'content':
                             'Your reply was not valid JSON. Reply with ONLY the '
                             'JSON object {"TERM": "definition", ...}.'}],
                max_tokens=1500, temperature=0, prefer_model=model,
                strict_model=bool(model), log_prefix='[Paper:TermFill:repair]')
            _collect_usage(usage_sink, _usage2)
            obj = _parse_json_obj(content2)
        except Exception as e:
            logger.warning('[Paper:TermFill] repair re-ask failed: %s', e)
            return {}
    # Normalise: keep str→str, non-empty.
    return {str(k).strip(): str(v).strip()
            for k, v in obj.items() if str(k).strip() and str(v).strip()}


def _definition_adds_new_gap(definition: str, known_terms_ci: set[str]) -> bool:
    """True if ``definition`` leans on an acronym not in ``known_terms_ci``.

    ``known_terms_ci`` = all currently-glossaried terms (upper-cased) PLUS the
    other terms being defined in this same addendum (so cross-references among
    backfilled rows are allowed). A definition that introduces a genuinely fresh
    undefined acronym would trade one gap for another → its row is rejected.
    """
    for tok in _extract_acronyms(_strip_code(definition)):
        if tok.upper() not in known_terms_ci:
            return True
    return False


def _keep_gap_closing_rows(report_text: str, offered: dict, audit: dict) -> dict:
    """RE-AUDIT GATE: keep only offered rows that PROVABLY close a gap.

    Strategy: build the addendum from ALL offered rows, re-audit
    ``body + addendum``, and keep a row iff (a) its term was an actual gap and
    (b) it does not itself introduce a new undefined term. Returns the accepted
    ``{term: definition}`` subset (possibly empty).
    """
    if not offered:
        return {}
    gap_ci = _gap_terms(audit)
    if not gap_ci:
        return {}

    # (a) restrict to offered terms that actually name a flagged gap.
    candidates = {t: d for t, d in offered.items() if t.upper() in gap_ci}
    if not candidates:
        return {}

    # (b) drop any candidate whose definition introduces a fresh undefined term.
    # "known" = the candidate terms themselves (intra-addendum cross-refs are
    # allowed); anything else a definition leans on is a fresh gap. This is a
    # cheap pre-filter — the authoritative check is the full re-audit below,
    # which catches any residual dangle a kept definition still creates.
    known_ci = {t.upper() for t in candidates}
    accepted = {t: d for t, d in candidates.items()
                if not _definition_adds_new_gap(d, known_ci)}
    if not accepted:
        return {}

    # Final proof: re-audit body + addendum-of-accepted-rows and require that the
    # gap set STRICTLY shrank and no NEW gap term appeared.
    before = _gap_terms(audit)
    addendum = _render_addendum(accepted, 'en')  # header lang irrelevant to audit
    after_audit = build_terminology_audit(report_text.rstrip() + '\n\n' + addendum + '\n')
    after = _gap_terms(after_audit) if after_audit else set()
    if not after.issubset(before) or not (before - after):
        # Either a new gap appeared, or nothing actually closed → reject wholesale.
        logger.info('[Paper:TermFill] re-audit did not cleanly shrink gaps '
                    '(before=%s after=%s) — dropping addendum', before, after)
        return {}
    return accepted


def _render_addendum(rows: dict, ui_lang: str) -> str:
    """Render accepted ``{term: definition}`` rows as a glossary-table addendum."""
    if not rows:
        return ''
    zh = ui_lang == 'zh'
    header = _ADDENDUM_HEADER_ZH if zh else _ADDENDUM_HEADER_EN
    col_term = '术语' if zh else 'Term'
    col_def = '定义' if zh else 'Definition'
    lines = [header, '', f'| {col_term} | {col_def} |', '|------|-----------|']
    for term, definition in rows.items():
        safe_def = definition.replace('|', '\\|').replace('\n', ' ').strip()
        lines.append(f'| {term} | {safe_def} |')
    return '\n'.join(lines)


def build_backfill_addendum(report_text, audit, ui_lang='en', *,
                            model=None, dispatch=None, usage_sink=None) -> str:
    """Generate a gap-closing glossary addendum, or ``''`` if none provably closes.

    Args:
        report_text: the finalized report body.
        audit: the ``terminologyAudit`` payload (missing/dangling gaps).
        ui_lang: 'en' or 'zh' — controls the addendum header/columns + prompt.
        model: preferred generation model (optional).
        dispatch: injectable non-streaming dispatcher ``(messages, **kw) ->
            (content, usage)``. Defaults to ``dispatch_chat``; overridden in
            tests. Kept as a parameter so this function is unit-testable offline.

    Returns:
        The addendum Markdown (a ``## 📖 … (glossary backfill)`` section) whose
        rows PROVABLY shrink the audit's gap set, or ``''`` when the LLM produced
        nothing that closes a gap (→ the warning card is retained).
    """
    if not audit:
        return ''
    gap_ci = _gap_terms(audit)
    if not gap_ci:
        return ''
    if dispatch is None:
        from lib.llm_dispatch.api import dispatch_chat as dispatch

    # The exact term strings to define (prefer the original casing from the audit).
    want = []
    seen = set()
    for m in (audit.get('missing') or []):
        t = m.get('term')
        if t and t.upper() not in seen:
            want.append(t); seen.add(t.upper())
    for d in (audit.get('dangling') or []):
        t = d.get('referencedTerm')
        if t and t.upper() not in seen:
            want.append(t); seen.add(t.upper())
    if not want:
        return ''

    # Generate in bounded chunks so each strict-JSON reply stays parseable
    # (a single all-terms request truncates to 0 defs on gappy reports), then
    # merge before the single re-audit gate.
    offered = {}
    for i in range(0, len(want), _MAX_TERMS_PER_CALL):
        chunk = want[i:i + _MAX_TERMS_PER_CALL]
        got = _generate_definitions(report_text, chunk, ui_lang, model, dispatch,
                                    usage_sink=usage_sink)
        if got:
            offered.update(got)
    if not offered:
        logger.info('[Paper:TermFill] LLM offered no parseable definitions '
                    '(%d terms across %d chunk(s))', len(want),
                    (len(want) + _MAX_TERMS_PER_CALL - 1) // _MAX_TERMS_PER_CALL)
        return ''

    accepted = _keep_gap_closing_rows(report_text, offered, audit)
    if not accepted:
        logger.info('[Paper:TermFill] no offered definition provably closed a gap — '
                    'dropping addendum (warning card retained)')
        return ''
    logger.info('[Paper:TermFill] accepted %d/%d definitions that close gaps: %s',
                len(accepted), len(offered), ', '.join(accepted))
    return _render_addendum(accepted, ui_lang)


def run_report_termfill(report_md, ui_lang='en', *, phash='', model=None,
                        audit=None, persist=True, dispatch=None) -> dict:
    """Generate → gate → persist the backfill addendum. Best-effort.

    Mirrors ``run_report_insight``: computes the addendum, and on success
    persists it under the SEPARATE ``termfill:<ui>`` key via the production
    write-path ``upsert(db, PAPER_REPORTS, …)`` — never overwriting the report.

    Returns ``{'markdown': str|'' , 'closed': bool, 'usage': dict|None}``.
    """
    result = {'markdown': '', 'closed': False, 'usage': None}
    try:
        if audit is None:
            audit = build_terminology_audit(report_md)
        if not audit:
            return result
        usage_sink = []
        addendum = build_backfill_addendum(report_md, audit, ui_lang,
                                           model=model, dispatch=dispatch,
                                           usage_sink=usage_sink)
        if usage_sink:
            keys = ('prompt_tokens', 'completion_tokens', 'cache_read_tokens',
                    'cache_write_tokens', 'reasoning_tokens')
            result['usage'] = {k: sum(int(u.get(k, 0)) for u in usage_sink)
                               for k in keys}
        if not addendum:
            return result
        result['markdown'] = addendum
        result['closed'] = True
    except Exception as e:
        logger.warning('[Paper:TermFill] run failed (non-fatal) hash=%s: %s', phash, e,
                       exc_info=True)
        return result

    if persist and addendum:
        try:
            _storage(write=True).command('paper.report.upsert', {
                'paper_hash': phash,
                'lang': termfill_lang_key(ui_lang),
                'report': addendum,
                'model': model or '',
                'meta': {'kind': 'termfill'},
                'created_at': int(time.time()),
            }, f'paper-termfill:{uuid.uuid4().hex}')
            logger.info('[Paper:TermFill] Persisted addendum — hash=%s key=%s %d chars',
                        phash, termfill_lang_key(ui_lang), len(addendum))
        except Exception as e:
            logger.warning('[Paper:TermFill] persist failed hash=%s: %s', phash, e)
    return result
