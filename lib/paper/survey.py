"""lib/paper/survey.py — multi-paper fan-in survey + open-gap map (R2).

The auto-research recipe's stage 3 (docs/AUTO_RESEARCH_SYSTEM_DESIGN.md §3 阶段 3):
take the local library that ``harvest`` (R1) built and synthesize it into

  1. a human-readable **survey markdown**, and
  2. a machine-readable **``open_gaps.json``** — the FROZEN contract that R3's
     anti-"A+B" novelty gate consumes (schema_version 1, defined in the design
     doc). ``open_gaps[].id`` is R3's handle on "does this idea solve a REAL
     gap", so the shape is locked here and evolves only by version bump.

Three owner-pinned invariants this module enforces
--------------------------------------------------
**Pin #1 — the open-gap map is machine-checked against the local library.**
A zero-LLM structural gate (:func:`_verify_against_library`) walks every arXiv
id the survey cites in ``clusters[].papers`` / ``method_matrix[].paper`` /
``open_gaps[].evidence`` and confirms each resolves to a row in
``paper_library`` (the shelf R1 built). An id that does NOT resolve is STRIPPED
from its entry — this is the recommend grounding gate run in reverse: not
grounding a *new* citation, but verifying that a paper the survey *claims to
cover* is actually in the shelf. If an ``open_gap``'s evidence is stripped
empty, the whole gap is dropped (default posture: a citation to a paper we
never harvested is treated as a model fabrication, not a crawl miss — a real
crawl miss is surfaced separately as ``missing_ids`` for a follow-up harvest,
never silently folded into the gap map R3 trusts).

**Pin #2 — inputs come from the library/reports, never a re-parse.** The survey
feeds the model each paper's ALREADY-GENERATED report (``paper_reports``, zero
cost when present) or, failing that, a truncated slice of ``paper_library``'s
stored ``parsed_text``. It NEVER calls ``parse_pdf`` and NEVER regenerates a
per-paper report. Each paper is capped at ``_SURVEY_PER_PAPER_CHARS`` so N
papers can't blow the context.

**Pin #3 — citations in the prose are audited by the existing tool.** The
survey markdown's inline ``arXiv:<id>`` identifiers go through
``lib.paper.citation_audit.build_citation_audit`` verbatim; a suspicious
(unresolvable) identifier surfaces a citation-integrity card in the result meta.

Monkeypatch seams (same discipline as insight/recommend): ``dispatch_stream``
and ``_execute_report_tool`` are resolved THROUGH this module at call time, so a
test patches ``survey.dispatch_stream`` / ``survey._execute_report_tool`` and it
bites the whole chain — no network, no real LLM needed to test the gates.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'build_survey', 'survey_lang_key', 'OPEN_GAPS_SCHEMA_VERSION',
    '_verify_against_library', '_load_paper_inputs', '_extract_survey_ids',
]

# The frozen open_gaps.json schema version. R3 reads this; bump (never silently
# reshape) when the contract changes.
OPEN_GAPS_SCHEMA_VERSION = 1

# Per-paper input cap (chars) — keeps N papers inside the context window. A
# report is already distilled, so this is generous; raw parsed_text is the
# fallback and gets the same ceiling.
_SURVEY_PER_PAPER_CHARS = 6000

# How many library papers to feed the synthesis at most (a survey of hundreds
# is summarized from the most relevant slice, not the entire shelf).
_SURVEY_MAX_PAPERS = 40

_MAX_SURVEY_TOOL_ROUNDS = 6
_SURVEY_TEMPERATURE = 0.3      # below insight's 0.45 — survey is descriptive, not divergent
_SURVEY_MAX_TOKENS = 8000
_SURVEY_LANG_PREFIX = 'survey'


def survey_lang_key(lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted survey.

    ``survey:<lang>`` — a separate row from plain reports / insights, mirroring
    Review Mode's ``review:<venue>:<uilang>`` and insight's ``insight:<lang>``.
    Lets a survey persist without ever overwriting a per-paper report.
    """
    return f'{_SURVEY_LANG_PREFIX}:{lang or "en"}'


# ── id normalization (shared discipline with recommend/_ground) ────────────

def _norm_id(arxiv_id) -> str:
    """Strip a version suffix so ``2502.09992v3`` and ``2502.09992`` compare equal.

    Mirrors ``recommend_engine._ground._norm_id`` exactly (kept local to avoid a
    cross-engine import on the survey cold path).

    TOTAL function (e2e fix, research_f12ab5e8): a real model emits dict-shaped
    entries in gap-map id lists (``{'id': …}`` / ``{'arxiv_id': …}`` /
    ``{'paper': …}``) — salvage the id from those keys and return '' for
    anything unparseable instead of crashing on ``.split``.
    """
    if isinstance(arxiv_id, dict):
        for _k in ('arxiv_id', 'id', 'paper'):
            _v = arxiv_id.get(_k)
            if isinstance(_v, str) and _v.strip():
                arxiv_id = _v
                break
        else:
            return ''
    if not isinstance(arxiv_id, str):
        return ''
    return arxiv_id.split('v')[0].strip()


# ── Input loading (pin #2: reports/library only, never a re-parse) ─────────

def _load_paper_inputs(arxiv_ids, *, lang: str = 'en', user_id: int = 1,
                       per_paper_chars: int = _SURVEY_PER_PAPER_CHARS,
                       max_papers: int = _SURVEY_MAX_PAPERS) -> list:
    """Load survey inputs for a set of library papers WITHOUT re-parsing.

    For each arXiv id, in priority order:
      1. an already-generated report from ``paper_reports`` (keyed on the
         paper's ``paper_hash`` + the plain ``lang`` — zero cost);
      2. the stored ``parsed_text`` slice from ``paper_library``.
    A paper with neither (not in the shelf, or empty text) is skipped and
    reported back to the caller as a ``missing`` id — it is NEVER parsed here.

    Returns a list of dicts::

        {'arxiv_id', 'paper_hash', 'title', 'source': 'report'|'parsed_text',
         'content': <capped str>}

    Best-effort: a DB hiccup on one paper is logged and that paper is skipped.
    """
    out = []
    seen = set()
    try:
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
    except Exception as e:
        logger.error('[Paper:Survey] cannot open DB for input load: %s', e, exc_info=True)
        return out
    try:
        for raw in (arxiv_ids or []):
            aid = _norm_id(raw)
            if not aid or aid in seen:
                continue
            seen.add(aid)
            if len(out) >= max_papers:
                logger.info('[Paper:Survey] input cap %d reached — %d id(s) not loaded',
                            max_papers, len(arxiv_ids) - len(out))
                break
            try:
                row = db.execute(
                    'SELECT paper_hash, title, parsed_text FROM paper_library '
                    'WHERE arxiv_id=? AND user_id=? AND parsed_text != \'\' '
                    'ORDER BY updated_at DESC LIMIT 1', (raw, user_id)).fetchone()
                if not row:
                    # try the version-normalized id too
                    row = db.execute(
                        'SELECT paper_hash, title, parsed_text FROM paper_library '
                        'WHERE arxiv_id=? AND user_id=? AND parsed_text != \'\' '
                        'ORDER BY updated_at DESC LIMIT 1', (aid, user_id)).fetchone()
            except Exception as e:
                logger.warning('[Paper:Survey] library lookup failed for %s: %s', aid, e)
                continue
            if not row:
                logger.debug('[Paper:Survey] %s not in library — skipped (no reparse)', aid)
                continue
            phash = row['paper_hash'] or ''
            title = row['title'] or f'arXiv:{aid}'
            # Prefer an already-generated report (zero cost).
            content, source = '', ''
            if phash:
                try:
                    rep = db.execute(
                        'SELECT report FROM paper_reports WHERE paper_hash=? AND lang=?',
                        (phash, lang)).fetchone()
                    if rep and (rep['report'] or '').strip():
                        content = rep['report']
                        source = 'report'
                except Exception as e:
                    logger.debug('[Paper:Survey] report lookup failed for %s: %s', aid, e)
            if not content:
                content = row['parsed_text'] or ''
                source = 'parsed_text'
            if not content.strip():
                continue
            out.append({
                'arxiv_id': aid, 'paper_hash': phash, 'title': title,
                'source': source, 'content': content[:per_paper_chars],
            })
    finally:
        try:
            _pool_put(db)
        except Exception as _e:
            logger.debug('load paper inputs: failed (%s)', _e)
            pass
    logger.info('[Paper:Survey] loaded %d paper input(s) — %d from reports, %d from parsed_text',
                len(out), sum(1 for p in out if p['source'] == 'report'),
                sum(1 for p in out if p['source'] == 'parsed_text'))
    return out


# ── The library-verifiable structural gate (pin #1) ────────────────────────

def _library_id_set(user_id: int = 1, folder_id: str = '') -> set:
    """Return the set of version-normalized arXiv ids present in paper_library.

    Optionally scoped to a ``folder_id`` (the research task's shelf). Any id in
    this set is a paper we have actually harvested; anything the survey cites
    outside it is unverifiable and gets stripped."""
    ids = set()
    try:
        from lib.database._core import _pool_get, _pool_put
        db = _pool_get()
        try:
            if folder_id:
                rows = db.execute(
                    'SELECT arxiv_id FROM paper_library WHERE user_id=? AND folder_id=? '
                    'AND arxiv_id != \'\'', (user_id, folder_id)).fetchall()
            else:
                rows = db.execute(
                    'SELECT arxiv_id FROM paper_library WHERE user_id=? AND arxiv_id != \'\'',
                    (user_id,)).fetchall()
        finally:
            _pool_put(db)
    except Exception as e:
        logger.error('[Paper:Survey] library id-set query failed: %s', e, exc_info=True)
        return ids
    for r in rows:
        nid = _norm_id(r['arxiv_id'])
        if nid:
            ids.add(nid)
    return ids


def _tier_ids(id_list, lib_ids: set, stripped: list, tiers: dict,
              ground_fn, ground_cache: dict) -> list:
    """Classify each id into library / grounded / hallucinated (R2/R3 seam v2).

    Returns the kept ids (library + grounded). Records:
      * ``stripped`` (list): ids that are hallucinations (not in library, and
        ``ground_fn`` could not confirm them) — removed from the entry.
      * ``tiers`` (dict): id → 'library' | 'grounded' for every KEPT id.
      * ``ground_cache`` (dict): id → bool memo so a repeated id isn't
        re-grounded (one network probe per distinct id per verify pass).

    ``library`` (a pure DB-set hit) is checked first and is free; ``grounded``
    (``ground_fn`` confirms the paper exists) costs one title lookup and is the
    fallback that keeps a real-but-not-yet-harvested citation alive; anything
    else is a fabrication.
    """
    kept = []
    for raw in (id_list or []):
        nid = _norm_id(raw)
        if not nid:
            # A dict entry with no salvageable id is an unverifiable CLAIM —
            # record it as stripped rather than silently skipping it.
            if isinstance(raw, dict):
                stripped.append(str(raw)[:80])
            continue
        if nid in lib_ids:
            kept.append(nid)
            tiers[nid] = 'library'
            continue
        # Not in the shelf — is it a real paper (grounded) or a hallucination?
        if nid not in ground_cache:
            ground_cache[nid] = bool(ground_fn(nid)) if ground_fn else False
        if ground_cache[nid]:
            kept.append(nid)
            tiers[nid] = 'grounded'
        else:
            stripped.append(nid)
    return kept


def _verify_against_library(gap_map: dict, *, user_id: int = 1,
                            folder_id: str = '', lib_ids: Optional[set] = None,
                            ground_fn=None) -> dict:
    """Grade every arXiv id in an open-gap map into three tiers (R2/R3 seam v2).

    Walks ``clusters[].papers``, ``method_matrix[].paper``, and
    ``open_gaps[].evidence`` and classifies each id:
      * **library** — in ``paper_library`` (scoped to ``folder_id`` when given):
        the strongest evidence, a paper we actually harvested and read;
      * **grounded** — not in the shelf but ``ground_fn`` (arXiv title lookup)
        confirms it exists: kept, but the id is added to ``missing_ids`` as a
        follow-up-harvest signal;
      * **hallucination** — neither: stripped from the entry.

    Rules:
      * a ``cluster``'s ``papers`` keeps library+grounded ids;
      * a ``method_matrix`` row is dropped only if its paper is a hallucination
        (a real-but-unharvested paper row is kept);
      * an ``open_gap`` is DROPPED only when ALL its evidence is hallucination —
        a gap with any library OR grounded evidence survives. A gap whose kept
        evidence has ZERO ``library`` ids (grounded-only) is flagged
        ``low_confidence=True`` — it rests on papers we have not actually read,
        so R3 must discount it (see ideate).

    Returns a NEW dict with added meta:
      * ``stripped_ids`` — hallucinations removed (replay/debug);
      * ``missing_ids`` — grounded-but-unharvested ids to harvest next;
      * per-gap: ``evidence_tiers`` / ``library_evidence_count`` /
        ``grounded_evidence_count`` / ``low_confidence``.

    ``ground_fn`` defaults to the module's ``_fetch_arxiv_title`` seam; pass a
    stub in tests to avoid the network.
    """
    if not isinstance(gap_map, dict):
        return {'schema_version': OPEN_GAPS_SCHEMA_VERSION, 'clusters': [],
                'method_matrix': [], 'open_gaps': [], 'stripped_ids': [],
                'missing_ids': []}
    if lib_ids is None:
        lib_ids = _library_id_set(user_id=user_id, folder_id=folder_id)
    if ground_fn is None:
        import lib.paper.survey as _self
        ground_fn = _self._fetch_arxiv_title

    out = dict(gap_map)
    out['schema_version'] = OPEN_GAPS_SCHEMA_VERSION
    stripped: list = []
    all_tiers: dict = {}      # id → 'library'|'grounded' across the whole map
    ground_cache: dict = {}   # id → bool, one probe per distinct id

    # clusters
    clusters = []
    for c in (gap_map.get('clusters') or []):
        if not isinstance(c, dict):
            continue
        c2 = dict(c)
        c2['papers'] = _tier_ids(c.get('papers'), lib_ids, stripped, all_tiers,
                                 ground_fn, ground_cache)
        clusters.append(c2)
    out['clusters'] = clusters

    # method_matrix — a row IS a paper; drop only a hallucinated-paper row
    matrix = []
    for m in (gap_map.get('method_matrix') or []):
        if not isinstance(m, dict):
            continue
        kept = _tier_ids([m.get('paper')], lib_ids, stripped, all_tiers,
                         ground_fn, ground_cache)
        if kept:
            m2 = dict(m)
            m2['paper'] = kept[0]   # normalized bare id (salvaged if dict-shaped)
            matrix.append(m2)
    out['method_matrix'] = matrix

    # open_gaps — drop only when ALL evidence is hallucination; flag low_confidence
    gaps = []
    dropped_gaps = 0
    for g in (gap_map.get('open_gaps') or []):
        if not isinstance(g, dict):
            continue
        g2 = dict(g)
        gap_tiers: dict = {}
        g2['evidence'] = _tier_ids(g.get('evidence'), lib_ids, stripped, gap_tiers,
                                   ground_fn, ground_cache)
        if not g2['evidence']:
            dropped_gaps += 1
            logger.info('[Paper:Survey] dropped hallucinated open_gap %s (all evidence '
                        'unresolvable): %.80s', g.get('id', '?'), g.get('gap', ''))
            continue
        lib_n = sum(1 for t in gap_tiers.values() if t == 'library')
        gnd_n = sum(1 for t in gap_tiers.values() if t == 'grounded')
        g2['evidence_tiers'] = {k: gap_tiers[k] for k in
                                {_norm_id(e) for e in g2['evidence']} if k in gap_tiers}
        g2['library_evidence_count'] = lib_n
        g2['grounded_evidence_count'] = gnd_n
        g2['low_confidence'] = (lib_n == 0)
        all_tiers.update(gap_tiers)
        if g2['low_confidence']:
            logger.info('[Paper:Survey] open_gap %s is low_confidence (grounded-only, '
                        '%d grounded / 0 library): %.60s', g.get('id', '?'), gnd_n,
                        g.get('gap', ''))
        gaps.append(g2)
    out['open_gaps'] = gaps

    # missing_ids = grounded (real but not yet in the shelf) → next harvest;
    # stripped_ids = hallucinations removed.
    missing = sorted({nid for nid, t in all_tiers.items() if t == 'grounded'})
    uniq_stripped = sorted(set(stripped))
    out['stripped_ids'] = uniq_stripped
    out['missing_ids'] = missing
    if uniq_stripped or missing:
        logger.warning('[Paper:Survey] library gate — %d hallucination(s) stripped, '
                       '%d grounded-not-harvested → missing_ids, %d gap(s) dropped',
                       len(uniq_stripped), len(missing), dropped_gaps)
    return out


# ── Citation audit passthrough (pin #3) ────────────────────────────────────

def _audit_citations(survey_md: str) -> Optional[dict]:
    """Run the existing citation-hallucination audit on the survey prose.

    Verbatim reuse of ``lib.paper.citation_audit.build_citation_audit`` — returns
    a card payload only when a cited identifier is suspicious, else None."""
    try:
        from lib.paper.citation_audit import build_citation_audit
        return build_citation_audit(survey_md)
    except Exception as e:
        logger.warning('[Paper:Survey] citation audit failed: %s', e)
        return None


# ── LLM synthesis (facade-resolved seams, mirrors insight) ─────────────────

def _parse_llm_json(content):
    from lib.llm.json_extract import extract_first_json_object
    return extract_first_json_object(content, log_prefix='[Paper:Survey]', log=logger)


def dispatch_stream(*args, **kwargs):
    """Facade seam → the real dispatcher. Patched by tests as ``survey.dispatch_stream``."""
    from lib.llm_dispatch import dispatch_stream as _ds
    return _ds(*args, **kwargs)


def _execute_report_tool(*args, **kwargs):
    """Facade seam → the report engine's research-tool executor.
    Patched by tests as ``survey._execute_report_tool``."""
    from lib.paper.report_engine import _execute_report_tool as _ert
    return _ert(*args, **kwargs)


def _fetch_arxiv_title(arxiv_id):
    """Facade seam → arXiv title verify (the 'grounded' evidence tier probe).

    Patched by tests as ``survey._fetch_arxiv_title``. Returns '' when the id
    cannot be confirmed to exist (→ that id is a hallucination, stripped)."""
    try:
        from lib.paper.arxiv import fetch_arxiv_title as _ft
        return _ft(arxiv_id) or ''
    except Exception as e:
        logger.debug('[Paper:Survey] grounding title lookup failed for %s: %s', arxiv_id, e)
        return ''


def _synthesize_survey(paper_inputs, direction, lang, *, model=None, abort=None,
                       on_tool_event=None):
    """Agentic fan-in synthesis: research the frontier, then emit the survey.

    Returns ``(survey_md, gap_map)`` where ``gap_map`` is the RAW (un-gated)
    open_gaps dict the model produced; the caller runs the library gate on it.

    Mirrors ``insight_engine._synthesize._research_and_synthesize``: shared
    ``run_agent_loop`` with ``_REPORT_TOOLS`` at a low temperature, all LLM/tool
    seams resolved through this module for monkeypatching. The model is asked to
    emit the markdown survey followed by a fenced ```json``` block carrying the
    open_gaps map; we split on the last JSON object.
    """
    import lib.paper.survey as _self
    from lib.agent_loop import AbortSignal, run_agent_loop
    from lib.paper.prompts import _REPORT_TOOLS, date_anchor_clause
    from lib.paper.tools import make_research_tool_executor

    _dispatch_stream = _self.dispatch_stream
    _exec_report_tool = _self._execute_report_tool

    system = date_anchor_clause(lang) + _survey_system_prompt(lang)
    parts = [f'## RESEARCH DIRECTION\n\n{direction}\n',
             '## LIBRARY PAPERS (already parsed — synthesize from these, do NOT re-read)\n']
    for i, p in enumerate(paper_inputs, 1):
        parts.append(f'### [{i}] {p["title"]}  (arXiv:{p["arxiv_id"]}, source={p["source"]})\n\n'
                     + p['content'])
    user_content = '\n\n---\n\n'.join(parts)

    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': user_content}]
    abort_signal = AbortSignal.from_callback(abort)
    _round = {'content': ''}
    _last = {'msg': None}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(text):
            _round['content'] += text

        logger.info('[Paper:Survey] round %d — msgs=%d tools=%s papers=%d',
                    rnd + 1, len(messages), 'yes' if tools else 'no', len(paper_inputs))
        return _dispatch_stream(
            messages, on_content=_on_content, abort_check=abort_signal.is_set,
            prefer_model=model or None, strict_model=bool(model), capability='text',
            tools=tools, max_tokens=_SURVEY_MAX_TOKENS, temperature=_SURVEY_TEMPERATURE,
            thinking_enabled=False, log_prefix='[Paper:Survey]')

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg

    def _begin_tool_round(rnd, msg):
        _round['content'] = ''
        messages.append(msg)

    _execute_tool = make_research_tool_executor(
        messages, user_question=direction[:300], abort_signal=abort_signal,
        execute_report_tool=_exec_report_tool, on_tool_event=on_tool_event,
        log_prefix='[Paper:Survey]')

    run_agent_loop(
        abort=abort_signal, max_tool_rounds=_MAX_SURVEY_TOOL_ROUNDS,
        round_tools=_REPORT_TOOLS, dispatch=_dispatch, execute_tool=_execute_tool,
        on_round_result=_on_round_result, on_tool_round=_begin_tool_round)

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''

    gap_map = _parse_llm_json(content) or {}
    survey_md = _strip_trailing_json(content)
    return survey_md, gap_map


def _strip_trailing_json(content: str) -> str:
    """Return the markdown prefix, dropping a trailing fenced/bare JSON block.

    The model emits ``<survey markdown>`` then the open_gaps JSON; the human
    survey is everything before that final object. Falls back to the whole
    content if no JSON tail is found."""
    if not content:
        return ''
    # Prefer a fenced ```json ... ``` block boundary.
    fence = content.rfind('```json')
    if fence > 0:
        return content[:fence].rstrip()
    # else cut at the last top-level '{' that starts the trailing object
    brace = content.rfind('\n{')
    if brace > 0:
        return content[:brace].rstrip()
    return content.strip()


def _survey_system_prompt(lang: str) -> str:
    """System prompt for the fan-in survey (kept inline; R7 may move to a pack)."""
    zh = (lang or 'en').startswith('zh')
    if zh:
        return (
            '你是一位资深研究员,正在为一个方向撰写**相关工作综述 + 空白地图**。\n'
            '基于给定的库内论文(已解析,勿重读),完成两件事:\n'
            '1) 一份结构化的中文综述 markdown:按主题聚类描述已有工作、它们的共同假设与局限;'
            '引用具体论文时用 `arXiv:<id>` 内联标注(id 必须来自给定库内论文);\n'
            '2) 综述之后,追加一个 ```json 代码块,严格符合 open_gaps schema(schema_version=1):'
            'clusters / method_matrix / open_gaps 三部分。**所有 papers/paper/evidence 里的 arxiv_id '
            '必须是上面给定库内论文的 id,不得编造库外论文。**\n'
            'open_gaps 是核心:标出真正没人做的空白,每条附 evidence(证明这确实是空白的库内论文 id)。'
            '可用 web 工具交叉核对,但空白地图的 id 只能来自库内。')
    return (
        'You are a senior researcher writing a **related-work survey + open-gap map** '
        'for a direction. From the given library papers (already parsed — do NOT re-read), produce:\n'
        '1) a structured survey markdown: cluster prior work by theme, describe shared '
        'assumptions and limitations; cite specific papers inline as `arXiv:<id>` '
        '(ids MUST come from the given library papers);\n'
        '2) AFTER the survey, append one ```json code block strictly matching the open_gaps '
        'schema (schema_version=1): clusters / method_matrix / open_gaps. **Every arxiv_id in '
        'papers/paper/evidence MUST be an id of a given library paper — never invent papers '
        'outside the library.**\n'
        'open_gaps is the core: mark genuinely unexplored gaps, each with evidence (library '
        'paper ids that prove it is a gap). You may cross-check with web tools, but the gap '
        "map's ids may only come from the library.")


# ── Public entry ───────────────────────────────────────────────────────────

def build_survey(direction: str, arxiv_ids, *, lang: str = 'en', user_id: int = 1,
                 folder_id: str = '', model: Optional[str] = None,
                 abort: Optional[Callable[[], bool]] = None,
                 on_tool_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Produce a fan-in survey + library-verified open-gap map for a direction.

    Args:
        direction: the research direction being surveyed (free text).
        arxiv_ids: the library papers to synthesize (ids harvested in R1).
        lang: 'en' | 'zh' — survey language + the ``paper_reports`` lang used to
            find already-generated per-paper reports.
        user_id: owner scope.
        folder_id: the research task's library folder; scopes the verifiable-id
            set to this shelf when given (else the whole library).
        model / abort / on_tool_event: forwarded to the synthesis loop.

    Returns:
        {
          'ok': bool,
          'direction': str,
          'lang': str,
          'survey_md': str,                 # human-readable survey
          'open_gaps': { ...schema v1... }, # library-gated gap map (R3 input)
          'citation_audit': dict | None,    # suspicious-citation card, if any
          'inputs_used': int,               # papers actually fed to synthesis
          'error': str,                     # set when ok is False
        }
    """
    direction = (direction or '').strip()
    if not direction:
        return {'ok': False, 'error': 'empty direction', 'direction': '',
                'lang': lang, 'survey_md': '', 'open_gaps': {}, 'citation_audit': None,
                'inputs_used': 0}

    inputs = _load_paper_inputs(arxiv_ids, lang=lang, user_id=user_id)
    if not inputs:
        logger.warning('[Paper:Survey] no library inputs for direction=%.80s (harvest first?)',
                       direction)
        return {'ok': False, 'error': 'no library papers to survey (run harvest first)',
                'direction': direction, 'lang': lang, 'survey_md': '',
                'open_gaps': {}, 'citation_audit': None, 'inputs_used': 0}

    try:
        survey_md, raw_gap_map = _synthesize_survey(
            inputs, direction, lang, model=model, abort=abort, on_tool_event=on_tool_event)
    except Exception as e:
        from lib.llm_errors import AbortedError
        if isinstance(e, AbortedError):
            logger.info('[Paper:Survey] aborted during synthesis')
            return {'ok': False, 'error': 'aborted', 'direction': direction, 'lang': lang,
                    'survey_md': '', 'open_gaps': {}, 'citation_audit': None,
                    'inputs_used': len(inputs)}
        logger.error('[Paper:Survey] synthesis failed: %s', e, exc_info=True)
        return {'ok': False, 'error': f'synthesis failed: {e}', 'direction': direction,
                'lang': lang, 'survey_md': '', 'open_gaps': {}, 'citation_audit': None,
                'inputs_used': len(inputs)}

    # Pin #1 — library-verifiable structural gate (zero LLM).
    gap_map = _verify_against_library(raw_gap_map, user_id=user_id, folder_id=folder_id)
    gap_map.setdefault('schema_version', OPEN_GAPS_SCHEMA_VERSION)
    gap_map['direction'] = direction
    gap_map['lang'] = lang
    gap_map['library_folder_id'] = folder_id
    gap_map['surveyed_count'] = len(inputs)

    # Pin #3 — citation audit on the prose.
    audit = _audit_citations(survey_md)

    logger.info('[Paper:Survey] done — direction=%.60s inputs=%d clusters=%d gaps=%d '
                'stripped=%d citation_suspicious=%s', direction, len(inputs),
                len(gap_map.get('clusters', [])), len(gap_map.get('open_gaps', [])),
                len(gap_map.get('stripped_ids', [])),
                bool(audit and audit.get('suspicious')))

    return {'ok': True, 'direction': direction, 'lang': lang, 'survey_md': survey_md,
            'open_gaps': gap_map, 'citation_audit': audit, 'inputs_used': len(inputs),
            'error': ''}


def _extract_survey_ids(gap_map: dict) -> set:
    """All version-normalized arXiv ids referenced anywhere in a gap map.

    Convenience for callers/tests that want to assert every surfaced id is
    library-verifiable."""
    ids = set()
    if not isinstance(gap_map, dict):
        return ids
    for c in gap_map.get('clusters') or []:
        for p in (c.get('papers') or []) if isinstance(c, dict) else []:
            ids.add(_norm_id(p))
    for m in gap_map.get('method_matrix') or []:
        if isinstance(m, dict):
            ids.add(_norm_id(m.get('paper')))
    for g in gap_map.get('open_gaps') or []:
        for e in (g.get('evidence') or []) if isinstance(g, dict) else []:
            ids.add(_norm_id(e))
    ids.discard('')
    return ids
