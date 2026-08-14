"""lib/paper/ideate.py — breakthrough-idea discovery + anti-"A+B" gate (R3).

The auto-research recipe's stage 4 (docs/AUTO_RESEARCH_SYSTEM_DESIGN.md §3 阶段 4)
and its intellectual core: turn R2's library-verified open-gap map into
**genuinely novel** research ideas, and — the hard part — KILL the "A+B stitching"
ideas that a plain LLM produces by the dozen.

The gate is what makes this not a toy. It runs three passes on every idea, in
cost order (free structural checks first, LLM judging last):

  Gate ① — zero-LLM structural gate (:func:`_structural_gate`)
    Required fields non-empty (linked_gap_id / why_not_AB / core_mechanism /
    falsifiable_prediction / kind), and — the load-bearing rule — the idea's
    ``linked_gap_id`` MUST hit a real ``open_gaps[].id`` from R2. An idea that
    solves no library-verified gap is invalid: it invented its own problem, and
    a problem nobody has is exactly where A+B stitching hides. (Owner pin #2.)

  Gate ② — forced-neighbor-retrieval novelty (:func:`_novelty_prior_set`)
    Owner pin #1: **novelty is measured against a RETRIEVED neighbor set, not
    against what the model chose to cite.** Before judging, we ALWAYS run
    ``search_arxiv`` on a SANITIZED term query (never the mechanism prose —
    that returned 0/25 on-topic neighbours in production and one HTTP 500) and
    pull the top-K into the basis. The retrieved set and the model's
    self-reported prior_art are kept in SEPARATE fields and never merged: they
    have different provenance and different trustworthiness. The judge compares
    the mechanism against the CLOSEST retrieved paper and must label the delta
    mechanism-level vs parameter-level; a parameter-level delta (new dataset /
    new backbone / two modules glued) caps the novelty axis. When retrieval
    yields nothing the basis is ``none`` and the idea CANNOT be accepted — pin
    #1 does not hold, so a rubric score is not evidence of novelty.

  Gate ③ — four-axis rubric (:func:`_score_idea`, mirrors insight `_rubric`)
    novelty / falsifiability / mechanism_depth / value, each 1-5, mean must
    clear ``IDEATE_GATE_THRESHOLD`` (a single tunable constant, initial 4.0 to
    match insight — 宁缺毋滥). Grounding (recommend engine) strips ideas whose
    novelty rests on a hallucinated paper.

Every rejected idea — with its four-axis scores and the reason from EACH gate —
is preserved in the result's ``rejected`` list (and persisted under the
``ideate:<lang>`` report key) so the threshold can be calibrated from real
rejection distributions rather than guessed. (Owner requirement.)

Seams (facade discipline, same as survey/insight): ``dispatch_stream``,
``search_arxiv``, ``fetch_arxiv_title`` are resolved THROUGH this module so a
test patches ``ideate.search_arxiv`` etc. and it bites every gate — the whole
thing is testable with zero network and zero real LLM.
"""

from __future__ import annotations

from typing import Callable, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'generate_ideas', 'ideate_lang_key', 'IDEATE_GATE_THRESHOLD',
    'IDEATE_NOVELTY_RETRIEVAL_K', 'RUBRIC_AXES',
    '_structural_gate', '_novelty_prior_set', '_ground_idea_prior_art',
]

# ── Tunables (all single named constants — calibrate, never hardcode in logic) ──

# Four-axis mean an idea must clear to survive. 4.0 matches insight's headroom
# gate (宁缺毋滥). A SINGLE constant so the owner can calibrate from the real
# rejection distribution without touching gate logic; tests must NOT hardcode
# this value (they drive scores relative to it).
IDEATE_GATE_THRESHOLD = 4.0

# How many neighbors search_arxiv MUST contribute to the novelty prior set,
# unconditionally, regardless of what the model self-reported. Owner pin #1:
# novelty = f(retrieved set). K>=5.
IDEATE_NOVELTY_RETRIEVAL_K = 5

# Generation divergence — mirror insight's 0.45 (divergent but JSON-safe).
_IDEATE_TEMPERATURE = 0.45
_IDEATE_MAX_TOKENS = 8000
# The open-ended generation/research loop gets a token envelope. Per-idea
# rubric calls are finite and are accounted in the same usage snapshot, but do
# not need a loop breaker.
_IDEATE_AGENT_TOKEN_BUDGET = 160_000
# Rubric judging is a judgement, not creative — deterministic.
_RUBRIC_TEMPERATURE = 0.0
_RUBRIC_MAX_TOKENS = 3000

_IDEATE_LANG_PREFIX = 'ideate'

RUBRIC_AXES = ('novelty', 'falsifiability', 'mechanism_depth', 'value')

# Required non-empty fields on every idea (gate ①).
_REQUIRED_FIELDS = ('title', 'kind', 'linked_gap_id', 'corpus_anchor_id',
                    'corpus_delta', 'core_mechanism', 'novelty_claim',
                    'falsifiable_prediction', 'why_not_AB')
_VALID_KINDS = ('methodology', 'analysis')

#: Synonyms a real model emits for the two frozen kinds. Production evidence
#: (research_7a444f96c65d42b5): one run produced 'Methodology', 'Algorithm',
#: 'Architecture' and 'Novelty' — none of which matched the enum, and ALL SIX
#: ideas died at the structural gate before the novelty retrieval or the rubric
#: ever ran. `kind` picks the R5/R6 template; it is metadata, so it is coerced
#: here, never used to reject. Unmapped values fall back to 'methodology'.
_KIND_SYNONYMS = {
    'methodology': 'methodology', 'method': 'methodology', 'algorithm': 'methodology',
    'architecture': 'methodology', 'model': 'methodology', 'system': 'methodology',
    'technique': 'methodology', 'approach': 'methodology', 'framework': 'methodology',
    'novelty': 'methodology', 'theory': 'methodology',
    'analysis': 'analysis', 'empirical': 'analysis', 'empirical study': 'analysis',
    'study': 'analysis', 'evaluation': 'analysis', 'benchmark': 'analysis',
    'measurement': 'analysis', 'survey': 'analysis',
}
#: What an unmappable kind becomes (the more common template).
_KIND_FALLBACK = 'methodology'

# ── Retrieval-query construction (gate ②'s wire contract) ──────────────────
#
# Production evidence (research_7a444f96c65d42b5): sending `title + the whole
# core_mechanism` — 473-558 chars of prose with `*asterisks*`, commas and
# parentheses — as arXiv's `all:` query returned 0/25 on-topic neighbours
# (gravitational-wave catalogues for KV-cache ideas), the SAME papers in 5 of 6
# ideas, and one HTTP 500 from an unescaped `*`. A long prose query degrades
# `all:` into near-unconstrained matching that just returns the most-cited
# collaboration papers, so the novelty axis became a CONSTANT.

#: Hard ceiling on a search query. Longer than this is prose, not search terms.
_MAX_RETRIEVAL_QUERY_CHARS = 160
#: How many terms the zero-LLM fallback extracts from the title.
_FALLBACK_QUERY_TERMS = 8
#: Stopwords the fallback drops — they carry no retrieval signal.
_QUERY_STOPWORDS = frozenset((
    'a', 'an', 'the', 'of', 'for', 'and', 'or', 'to', 'in', 'on', 'with', 'by',
    'via', 'using', 'from', 'into', 'at', 'as', 'is', 'are', 'be', 'that',
    'this', 'these', 'those', 'it', 'its', 'their', 'our', 'we', 'can', 'may',
    'instead', 'based', 'towards', 'toward', 'novel', 'new', 'approach',
))


def sanitize_retrieval_query(raw, *, max_chars: int = _MAX_RETRIEVAL_QUERY_CHARS) -> str:
    """Make any candidate query safe and useful to send to a search API.

    Strips the characters that break arXiv's parser (an unescaped ``*`` in one
    real mechanism caused HTTP 500 → zero neighbours → gate ② silently off),
    collapses whitespace and truncates on a WORD boundary. Every path into
    retrieval funnels through here — model-supplied and fallback alike — so
    prose can never reach the wire regardless of which path produced it.
    """
    import re
    txt = raw if isinstance(raw, str) else ''
    # Drop everything that is punctuation/markup rather than a search term.
    txt = re.sub(r'[*()\[\]{}"\'`,;:!?/\\|<>+=~^$#@&—–“”‘’]', ' ', txt)
    txt = re.sub(r'[.](?!\d)', ' ', txt)          # keep 2502.09992, drop sentence dots
    txt = re.sub(r'\s+', ' ', txt).strip()
    if len(txt) <= max_chars:
        return txt
    cut = txt[:max_chars]
    sp = cut.rfind(' ')
    return (cut[:sp] if sp > 0 else cut).strip()


def _fallback_retrieval_query(idea: dict) -> str:
    """Zero-LLM query builder — the path that MUST work without model help.

    The model is asked for a ``retrieval_query``, but 'the contract is in the
    prompt' is not the same as 'the model obeyed it' (the `kind` bug proved
    that), and ideas generated before the field existed have none at all. So
    this derives terms from the TITLE (a noun phrase naming the mechanism —
    exactly what a search needs) and never from the mechanism prose.
    """
    title = idea.get('title') if isinstance(idea.get('title'), str) else ''
    words = sanitize_retrieval_query(title, max_chars=_MAX_RETRIEVAL_QUERY_CHARS).split()
    terms = [w for w in words if w.lower() not in _QUERY_STOPWORDS]
    return ' '.join((terms or words)[:_FALLBACK_QUERY_TERMS])


def build_retrieval_query(idea: dict) -> tuple:
    """Return ``(query, source)`` for one idea's neighbour retrieval.

    ``source`` is ``'model'`` when the idea's own ``retrieval_query`` survived
    sanitization, else ``'fallback'``. Both paths go through the SAME sanitizer,
    so a model that ignores the contract and pastes a whole sentence is trimmed
    rather than trusted.
    """
    supplied = sanitize_retrieval_query(idea.get('retrieval_query'))
    # A model-supplied query must be terms, not a restated sentence; if it is
    # implausible (empty or a single word) fall back to the title terms.
    if supplied and len(supplied.split()) >= 2:
        return supplied, 'model'
    fb = _fallback_retrieval_query(idea)
    return fb, 'fallback'


# ── Fielded query: identity vs domain (owner ruling C) ─────────────────────
#
# Sanitizing the query fixed relevance (0/25 → 25/25 on-topic) but NOT
# discrimination: two ideas whose titles share the domain words still got
# byte-identical neighbour sets — `Predictive Delta Compression KV Cache` and
# `Quantum-Inspired Entanglement KV Cache Compression` shared 5/5 papers,
# because arXiv's `all:` is an unordered bag of words with no notion of which
# terms name THIS idea and which merely place it in a field.
#
# The fix says what we actually mean, using syntax the API already has:
#
#     ti:"<identity terms>" AND all:"<domain terms>"
#
# Identity terms constrain the TITLE (so two different mechanisms diverge);
# domain terms constrain the full text (so the search stays in the field and
# does not drift to real quantum physics for a 'Quantum-Inspired' idea).
#
# The split is COMPUTED, not hand-tuned: within one batch of ideas, a term
# carried by several ideas is domain background; a term unique to this idea is
# its identity. That is a recomputable structural criterion over the batch, not
# an invented weight.

#: Share of a batch's ideas a term must appear in to count as domain
#: background rather than one idea's identity.
_DOMAIN_TERM_MIN_SHARE = 0.5


def split_identity_domain(queries) -> list:
    """Split each query's terms into identity vs domain across a batch.

    Args:
        queries: the sanitized term-string of every idea in the batch.

    Returns:
        One ``{'identity': [...], 'domain': [...]}`` per input query. A term
        carried by >= ``_DOMAIN_TERM_MIN_SHARE`` of the batch is domain; the
        rest is that idea's identity. With a single idea there is nothing to
        compare against, so everything is identity and the domain leg is empty
        (the caller then degrades to a plain ``all:`` query).
    """
    from collections import Counter
    toks = [[t for t in (q or '').split() if t] for q in queries]
    n = len([t for t in toks if t])
    if n <= 1:
        return [{'identity': list(t), 'domain': []} for t in toks]
    seen = Counter()
    for t in toks:
        seen.update({w.lower() for w in t})       # once per idea, not per occurrence
    cut = max(2, int(n * _DOMAIN_TERM_MIN_SHARE))
    out = []
    for t in toks:
        out.append({'identity': [w for w in t if seen[w.lower()] < cut],
                    'domain': [w for w in t if seen[w.lower()] >= cut]})
    return out


def assemble_arxiv_query(identity, domain, *, tier: int = 1) -> tuple:
    """Build the arXiv query string; return ``(query, mode)``.

    ★ CONSTRUCTION LIVES IN tofu-search. This is a thin delegation to
    ``tofu_search.search.vertical.arxiv.build_query`` — the three measured
    syntax facts (quoted = exact phrase so novel terms recall zero; identity
    terms must be OR-ed not AND-ed; the domain leg stays a quoted phrase) are
    pinned by executable assertions in that package's
    ``tests/test_arxiv_query_syntax.py``, not by this comment. A comment cannot
    go red; that is exactly why the knowledge moved.

    Kept as a named function (rather than inlined at the call site) because
    three tests drive it directly as the ideate-side contract. It maps our
    ``tier`` (1 = title, 2 = abstract) onto the shared helper's ``field``, and
    re-labels the mode as ``fielded_t1``/``fielded_t2`` because the ladder in
    :func:`_novelty_prior_set` reports WHICH rung produced the basis.

    ``mode``: ``'fielded_t1'`` | ``'fielded_t2'`` | ``'domain'`` | ``'all'``.
    """
    from tofu_search.search.vertical import arxiv as _ts

    field = 'ti' if tier <= 1 else 'abs'
    query, mode = _ts.build_query(identity, domain, field=field)
    if mode == 'fielded':
        return query, f'fielded_t{1 if tier <= 1 else 2}'
    if mode == 'domain':
        return query, 'domain'
    # 'terms' (no domain constraint) / 'empty' both read as the flat last rung.
    return query, 'all'


def _normalize_kind(raw) -> tuple:
    """Coerce a free-form ``kind`` into the frozen enum.

    Returns ``(kind, was_normalized)``. NEVER raises and never signals
    invalidity — an unrecognized kind degrades to ``_KIND_FALLBACK`` so a
    labelling slip can never kill an otherwise-valid idea (owner ruling: the
    structural gate judges evidence, not formatting).
    """
    txt = (raw or '').strip().lower() if isinstance(raw, str) else ''
    if txt in _KIND_SYNONYMS:
        out = _KIND_SYNONYMS[txt]
    else:
        # tolerate decorated values like 'Methodology (novel mechanism)'
        out = next((v for k, v in _KIND_SYNONYMS.items() if k in txt), _KIND_FALLBACK)
    return out, out != raw


def ideate_lang_key(lang: str) -> str:
    """Composite ``paper_reports.lang`` key for a persisted ideate pass.

    ``ideate:<lang>`` — a separate row from survey/report/insight, mirroring the
    established composite-key convention."""
    return f'{_IDEATE_LANG_PREFIX}:{lang or "en"}'


# ── id normalization (shared discipline) ───────────────────────────────────

def _norm_id(arxiv_id) -> str:
    """Strip a version suffix so ``2502.09992v3`` and ``2502.09992`` compare equal."""
    from lib.paper.arxiv import normalize_arxiv_id
    return normalize_arxiv_id(arxiv_id)


# ── Facade seams (monkeypatchable) ─────────────────────────────────────────

def dispatch_stream(*args, **kwargs):
    """Facade → real dispatcher. Patched by tests as ``ideate.dispatch_stream``."""
    from lib.llm_dispatch import dispatch_stream as _ds
    return _ds(*args, **kwargs)


def search_arxiv(query, max_results=10):
    """Facade → FREE-TEXT arXiv search. Patched by tests as ``ideate.search_arxiv``.

    NOT the gate ② seam — that is :func:`ts_search_by_query`. Gate ② has
    identity/domain legs to pass and must never flatten them into a string
    here; see that function's docstring for the regression this split prevents.
    """
    from lib.paper.arxiv import search_arxiv as _sa
    return _sa(query, max_results=max_results)


def ts_search_by_query(identity_terms, domain_terms=None, *, field='ti',
                       max_results=5):
    """Facade → STRUCTURED neighbour retrieval in tofu-search. Patched by tests.

    Gate ② uses this rather than :func:`search_arxiv` because the shared
    vertical is the SINGLE place that turns legs into arXiv syntax. Passing a
    pre-built query through the free-text entry point instead is what made a
    KV-cache idea come back with titanium-alloy papers: the free-text path
    sanitizes its input (correct for terms, destructive for built syntax).

    Returns the shared envelope, whose ``outcome`` separates ``no_matches``
    (asked properly, nothing there → widen the rung) from ``request_failed`` /
    ``unusable_query`` (not evidence about the literature at all).
    """
    from tofu_search.search.vertical.arxiv import search_by_query as _sbq
    return _sbq(identity_terms, domain_terms, field=field,
                max_results=max_results)


def fetch_arxiv_title(arxiv_id):
    """Facade → arXiv title verify. Patched by tests as ``ideate.fetch_arxiv_title``."""
    from lib.paper.arxiv import fetch_arxiv_title as _ft
    return _ft(arxiv_id)


# ── Gate ①: zero-LLM structural gate ───────────────────────────────────────

def _structural_gate(idea: dict, valid_gap_ids: set) -> Optional[str]:
    """Return a rejection reason string if the idea fails the structural gate,
    else None (passes).

    Owner pin #2 — the load-bearing rule is ``linked_gap_id ∈ valid_gap_ids``:
    an idea must attach to a real, library-verified open gap from R2. An idea
    that invents its own problem is invalid (that is where A+B stitching hides).
    All checks are pure — no LLM, no network — so this runs first and free.

    This gate judges EVIDENCE, never formatting: it asks "does this idea solve a
    verified gap, is it falsifiable, does it name concrete prior art". ``kind``
    is deliberately NOT a rejection reason — it selects the R5/R6 template and
    is normalized by :func:`_normalize_kind` instead (a real run once lost 6/6
    ideas to `invalid kind: 'Methodology'` before the expensive gates ran).
    """
    if not isinstance(idea, dict):
        return 'not a dict'
    for f in _REQUIRED_FIELDS:
        v = idea.get(f)
        if not (isinstance(v, str) and v.strip()):
            return f'missing/empty required field: {f}'
    gid = (idea.get('linked_gap_id') or '').strip()
    if gid not in valid_gap_ids:
        return (f'linked_gap_id {gid!r} does not match any library-verified '
                f'open_gap (invented problem — no evidence it solves a real gap)')
    # Substantive novelty is judged against the forced retrieved-neighbour set
    # below. Requiring the generator to hand-copy an arXiv id here was a
    # formatting gate: a potentially useful analysis idea died before evidence
    # retrieval and the LLM judge ran. The claim remains required/non-empty;
    # positive prior-art evidence decides whether it is actually novel.
    return None


# ── Gate ②: forced-neighbor-retrieval novelty prior set ────────────────────

def _novelty_prior_set(idea: dict, *, k: int = IDEATE_NOVELTY_RETRIEVAL_K,
                       batch_terms=None) -> dict:
    """Build the neighbor set the novelty judge MUST use (owner pin #1).

    ALWAYS runs ``search_arxiv`` on a SANITIZED term query and takes the top-k
    hits — this is the retrieved evidence the judge compares against, and it is
    kept STRICTLY SEPARATE from the model's self-reported prior_art.

    The two are never merged. Production shipped a ``merged_ids`` union in which
    the model's self-report (up to 257 ids) outnumbered the retrieved set (5) by
    50:1, and that union was what landed in the audit record — so the audit
    claimed a 41-paper basis while the judge actually saw 5 papers. Different
    provenance, different trustworthiness: they stay in different fields.

    When ``batch_terms`` (every idea's sanitized query in this pass) is given,
    the query is FIELDED so ideas that share a domain still retrieve different
    neighbours. Two ideas whose titles both ended in "KV Cache Compression"
    previously got byte-identical neighbour sets, which made the novelty axis a
    constant.

    Degradation chain, each step LOUD (never a silent empty basis):
      title-identity → abstract-identity → domain phrase → flat ``all:`` →
      ``novelty_basis='none'``. Widening the identity leg (rather than dropping
      straight to the flat bag of words) is what keeps a genuinely-new idea from
      reporting a false "no prior art".

    Returns::

        {
          'retrieved': [ {arxiv_id, title, summary}, ... ],  # from search_arxiv
          'retrieved_ids': ['id', ...],                       # the ACTUAL basis
          'self_reported_ids': ['id', ...],                   # idea.prior_art
          'retrieval_query': str,                             # what hit the wire
          'query_source': 'model'|'fallback',
          'query_mode': 'fielded_t1'|'fielded_t2'|'domain'|'all',
          'novelty_basis': 'retrieved'|'none',
        }

    ``novelty_basis='none'`` means the retrieval produced nothing, so pin #1
    cannot hold: the caller MUST NOT accept that idea on a rubric score alone.
    """
    import lib.paper.ideate as _self
    title = (idea.get('title') or '').strip()
    terms, query_source = _self.build_retrieval_query(idea)

    # ★ The ladder carries STRUCTURED LEGS, not pre-built query strings.
    #
    # It used to carry strings and hand them to `search_arxiv`, whose contract
    # is "free text" — so the adapter sanitized them a SECOND time, stripping
    # `(`, `)` and `:` out of a perfectly good fielded query. Measured damage:
    # `(ti:predictive OR ti:delta) AND all:"KV cache compression"` became
    # `all:ti predictive OR ti delta AND all KV cache compression`, and arXiv
    # answered a KV-cache idea with TITANIUM ALLOY papers (bare `ti` matched
    # "Ti"). Nothing raised; on-topic fell 96% → 57%.
    #
    # Sanitizing is correct for TERMS and destructive for an ALREADY-BUILT
    # query. One function cannot take both input shapes, so the legs stay
    # structured all the way down to the single constructor in tofu-search.
    #
    # Rungs: tier 1 title → tier 2 abstract → domain phrase only → flat terms.
    # Measured: tier 1 alone left 2/6 real ideas with an empty basis (their
    # identity terms are genuinely too new for ANY title); the abstract rung
    # recovers both while keeping 0% pairwise overlap. Widening — never
    # collapsing back to a bag of words — is what stops a genuinely-new idea
    # from falsely reporting "no prior art".
    #
    # Each rung is (identity_terms, domain_terms, field, mode_label).
    _flat = (sanitize_retrieval_query(terms).split(), None, 'ti', 'all')
    attempts = [_flat]
    if terms and batch_terms:
        try:
            legs = _self.split_identity_domain(list(batch_terms) + [terms])[-1]
            ladder = []
            if legs['identity'] and legs['domain']:
                ladder.append((legs['identity'], legs['domain'], 'ti', 'fielded_t1'))
                ladder.append((legs['identity'], legs['domain'], 'abs', 'fielded_t2'))
            if legs['domain']:
                ladder.append(([], legs['domain'], 'ti', 'domain'))
            if ladder:
                attempts = ladder + [_flat]
        except Exception as e:
            logger.warning('[Paper:Ideate] identity/domain split failed for %.60s: %s '
                           '— falling back to flat terms', title, e)
            attempts = [_flat]

    def _search(ident, dom, field):
        """Retrieve via the shared vertical. Returns (papers, outcome)."""
        try:
            res = _self.ts_search_by_query(
                ident, dom, field=field,
                max_results=max(k, IDEATE_NOVELTY_RETRIEVAL_K)) or {}
        except Exception as e:
            logger.warning('[Paper:Ideate] novelty retrieval failed for %.60s: %s', title, e)
            return [], 'request_failed', ''
        out = []
        for h in (res.get('papers') or [])[:k]:
            aid = _norm_id(h.get('arxiv_id'))
            if aid:
                out.append({'arxiv_id': aid, 'title': h.get('title') or '',
                            'summary': (h.get('summary') or '')[:400]})
        return out, res.get('outcome') or '', res.get('query') or ''

    query, query_mode, retrieved = '', 'all', []
    for _ident, _dom, _field, _mode in attempts:
        if not (_ident or _dom):
            continue
        retrieved, _outcome, _wire = _search(_ident, _dom, _field)
        query, query_mode = _wire, _mode
        if retrieved:
            break
        # `no_matches` = asked properly, nothing there → widen the rung.
        # `request_failed` / `unusable_query` are NOT evidence about the
        # literature, so they must not be read as "no prior art" either.
        logger.warning('[Paper:Ideate] %s rung retrieved nothing for %.60s '
                       '(outcome=%s query=%r) — widening',
                       _mode, title, _outcome, _wire)
    if not query:
        logger.warning('[Paper:Ideate] no usable retrieval query for %.60s', title)

    retrieved_ids = list(dict.fromkeys(r['arxiv_id'] for r in retrieved))
    self_reported = list(dict.fromkeys(
        _norm_id(x) for x in (idea.get('prior_art') or []) if _norm_id(x)))
    basis = 'retrieved' if retrieved_ids else 'none'
    if basis == 'none':
        logger.error('[Paper:Ideate] EMPTY novelty basis for %.60s (query=%r source=%s '
                     'mode=%s) — pin #1 cannot hold, this idea is unjudgeable',
                     title, query, query_source, query_mode)
    logger.info('[Paper:Ideate] novelty prior set for %.50s — retrieved=%d self=%d '
                'basis=%s mode=%s query=%r (%s)', title, len(retrieved_ids),
                len(self_reported), basis, query_mode, query, query_source)
    return {'retrieved': retrieved, 'retrieved_ids': retrieved_ids,
            'self_reported_ids': self_reported, 'retrieval_query': query,
            'query_source': query_source, 'query_mode': query_mode,
            'novelty_basis': basis}


# ── Gate ②/③: the LLM judge (novelty + four-axis rubric in one dispatch) ────

def _parse_llm_json(content):
    from lib.llm.json_extract import extract_first_json_object
    return extract_first_json_object(content, log_prefix='[Paper:Ideate]', log=logger)


def _coerce_score(v):
    """Clamp a rubric score to an int in [1,5]; None on garbage (mirrors insight)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError) as _e:
        logger.debug('coerce score: unexpected type/unparseable (%s)', _e)
        return None
    return max(1, min(5, n))


def _judge_prompt(idea: dict, prior_set: dict, gap: dict, lang: str) -> str:
    """Prompt for the combined novelty + four-axis judgement.

    Feeds the RETRIEVED neighbor set (not the model's self-report) as the
    comparison basis, and demands an explicit mechanism-level vs
    parameter-level verdict against the closest retrieved paper."""
    zh = (lang or 'en').startswith('zh')
    neigh_lines = []
    for r in prior_set.get('retrieved', []):
        neigh_lines.append(f"- arXiv:{r['arxiv_id']}  {r['title']}\n    {r['summary']}")
    neighbors = '\n'.join(neigh_lines) or '(retrieval returned nothing)'
    gap_txt = f"{gap.get('id')}: {gap.get('gap','')}" if gap else '(no linked gap)'
    idea_txt = (f"title: {idea.get('title')}\nkind: {idea.get('kind')}\n"
                f"corpus_anchor_id: {idea.get('corpus_anchor_id')}\n"
                f"corpus_delta: {idea.get('corpus_delta')}\n"
                f"failure_cause: {idea.get('failure_cause')}\n"
                f"new_invariant: {idea.get('new_invariant')}\n"
                f"intervention_level: {idea.get('intervention_level')}\n"
                f"core_mechanism: {idea.get('core_mechanism')}\n"
                f"novelty_claim: {idea.get('novelty_claim')}\n"
                f"falsifiable_prediction: {idea.get('falsifiable_prediction')}\n"
                f"why_not_AB: {idea.get('why_not_AB')}")
    if zh:
        head = (
            '你是一位严格的审稿人,正在判定一个研究 idea 是否真正新颖,还是「A+B 缝合」。\n'
            '**新颖性只能相对下面【检索到的近邻集合】判定,不许用 idea 自报的 prior_art 糊弄。**\n'
            '尤其要对比 idea 的 core_mechanism 与近邻里【最接近的那一篇】:差异是\n'
            '**机理级(mechanism-level,新的原理/机制)还是参数级(parameter-level,换数据集/\n'
            '换 backbone/两个已有模块拼接)**?parameter-level 增量的 novelty 轴最高只能给 2。\n')
        axes = ('四轴打分(1-5 整数):novelty(相对检索近邻的新颖度)、falsifiability(预测可否证伪)、'
                'mechanism_depth(是否解释了为什么而非只描述做什么)、value(若成立是否真解决 linked gap)。')
        fmt = ('只返回一个 JSON:{"mechanism_delta":"mechanism-level|parameter-level",'
               '"closest_neighbor":"arxiv_id","scores":{"novelty":n,"falsifiability":n,'
               '"mechanism_depth":n,"value":n},"justifications":{轴:一句话},"verdict":"一句话"}')
    else:
        head = (
            'You are a strict reviewer judging whether a research idea is genuinely novel '
            'or an "A+B stitch".\n'
            '**Judge novelty ONLY against the RETRIEVED NEIGHBORS below — do NOT let the '
            "idea's self-reported prior_art off the hook.**\n"
            "Compare the idea's core_mechanism against the CLOSEST retrieved paper: is the "
            'delta **mechanism-level (a new principle/mechanism) or parameter-level (new '
            'dataset / new backbone / two existing modules glued together)**? A '
            'parameter-level delta caps the novelty axis at 2.\n')
        axes = ('Score four axes (integers 1-5): novelty (vs retrieved neighbors), '
                'falsifiability, mechanism_depth (explains WHY not just WHAT), value '
                '(does it truly solve the linked gap).')
        fmt = ('Return ONLY one JSON: {"mechanism_delta":"mechanism-level|parameter-level",'
               '"closest_neighbor":"arxiv_id","scores":{"novelty":n,"falsifiability":n,'
               '"mechanism_depth":n,"value":n},"justifications":{axis:one-line},"verdict":"one-line"}')
    return (f'{head}\n{axes}\n\n## LINKED GAP\n{gap_txt}\n\n## RETRIEVED NEIGHBORS '
            f'(the ONLY novelty basis)\n{neighbors}\n\n## IDEA\n{idea_txt}\n\n{fmt}')


def _score_idea(idea: dict, prior_set: dict, gap: dict, lang: str, *,
                model=None, abort=None, usage_meter=None) -> Optional[dict]:
    """Judge one idea: novelty-vs-retrieved + four-axis rubric, one dispatch.

    Returns::
        {'scores': {axis: 1-5}, 'overall': float, 'mechanism_delta': str,
         'closest_neighbor': str, 'justifications': {...}, 'verdict': str,
         'novelty_capped': bool}
    or None on dispatch/parse failure. ``overall`` is recomputed here — never
    trust the model's arithmetic. A parameter-level mechanism_delta forces the
    novelty axis to min(score, 2) (the cap is applied HERE, deterministically,
    not left to the model).
    """
    import lib.paper.ideate as _self
    from lib.agent_loop import AbortSignal
    from lib.llm_errors import AbortedError
    _dispatch_stream = _self.dispatch_stream

    abort_signal = AbortSignal.from_callback(abort)
    buf = {'content': ''}

    def _on_content(t):
        buf['content'] += t

    try:
        msg, _finish, _usage = _dispatch_stream(
            [{'role': 'user', 'content': _judge_prompt(idea, prior_set, gap, lang)}],
            on_content=_on_content, abort_check=abort_signal.is_set,
            prefer_model=model or None, strict_model=bool(model), capability='text',
            max_tokens=_RUBRIC_MAX_TOKENS, temperature=_RUBRIC_TEMPERATURE,
            thinking_enabled=False, log_prefix='[Paper:Ideate:Judge]')
    except AbortedError:
        raise
    except Exception as e:
        logger.error('[Paper:Ideate] judge dispatch failed: %s', e, exc_info=True)
        return None
    if usage_meter:
        usage_meter.record(_usage)

    content = buf['content'] or (msg.get('content') if isinstance(msg, dict) else '') or ''
    parsed = _parse_llm_json(content)
    if not isinstance(parsed, dict):
        logger.warning('[Paper:Ideate] unparseable judge reply for %.50s', idea.get('title'))
        return None

    raw = parsed.get('scores') if isinstance(parsed.get('scores'), dict) else {}
    scores = {}
    for axis in RUBRIC_AXES:
        s = _coerce_score(raw.get(axis))
        if s is not None:
            scores[axis] = s
    if not scores:
        logger.warning('[Paper:Ideate] no valid axis scores for %.50s', idea.get('title'))
        return None

    delta = (parsed.get('mechanism_delta') or '').strip().lower()
    novelty_capped = False
    if delta == 'parameter-level' and scores.get('novelty', 0) > 2:
        # Deterministic cap — do not let a parameter-level stitch score high novelty.
        scores['novelty'] = 2
        novelty_capped = True
        logger.info('[Paper:Ideate] novelty capped to 2 (parameter-level delta) for %.50s',
                    idea.get('title'))

    overall = round(sum(scores.values()) / len(scores), 2)
    justifs = parsed.get('justifications') if isinstance(parsed.get('justifications'), dict) else {}
    return {
        'scores': scores, 'overall': overall, 'mechanism_delta': delta,
        'closest_neighbor': _norm_id(parsed.get('closest_neighbor')),
        'justifications': {k: str(v) for k, v in justifs.items()},
        'verdict': (parsed.get('verdict') or '').strip(),
        'novelty_capped': novelty_capped,
    }


# ── Grounding (recommend engine, facade-resolved) ──────────────────────────

def _ground_idea_prior_art(idea: dict) -> tuple:
    """Ground every arXiv id in the idea's prior_art; return (grounded, dropped).

    Reuses the arXiv seam. An id that cannot be grounded is removed from
    prior_art and counted — an idea whose novelty rests on hallucinated papers
    should not survive on their strength. Mutates ``idea['prior_art']`` in place.
    """
    import lib.paper.ideate as _self
    grounded, dropped = [], 0
    for raw in (idea.get('prior_art') or []):
        aid = _norm_id(raw)
        if not aid:
            continue
        try:
            title = _self.fetch_arxiv_title(aid)
        except Exception as e:
            logger.debug('[Paper:Ideate] grounding lookup failed for %s: %s', aid, e)
            title = ''
        if title:
            grounded.append(aid)
        else:
            dropped += 1
            logger.debug('[Paper:Ideate] dropped ungrounded prior_art id %s', aid)
    idea['prior_art'] = grounded
    return grounded, dropped


# ── Generation (facade-resolved agentic loop, mirrors survey) ──────────────

def _generate_raw_ideas(direction, open_gaps, reader_context, lang, *,
                        n_ideas=6, model=None, abort=None, on_tool_event=None,
                        usage_meter=None) -> list:
    """Generate raw (un-gated) ideas anchored to R2's open_gaps.

    Single seam tests monkeypatch (``ideate._generate_raw_ideas``) to drive the
    gate pipeline offline. Mirrors insight/survey synthesis: run_agent_loop +
    _REPORT_TOOLS, facade-resolved dispatch."""
    import lib.paper.ideate as _self
    from lib.agent_loop import AbortSignal, run_agent_loop
    from lib.paper.prompts import _REPORT_TOOLS, date_anchor_clause
    from lib.paper.tools import make_research_tool_executor

    system = date_anchor_clause(lang) + _ideate_system_prompt(lang, n_ideas)
    gaps_json = _compact_gaps(open_gaps)
    parts = [f'## RESEARCH DIRECTION\n{direction}']
    if reader_context:
        parts.append(reader_context)
    parts.append('## OPEN GAPS (from the survey — every idea MUST link to one of these ids)\n'
                 + gaps_json)
    messages = [{'role': 'system', 'content': system},
                {'role': 'user', 'content': '\n\n---\n\n'.join(parts)}]
    abort_signal = AbortSignal.from_callback(abort)
    _round = {'content': ''}
    _last = {'msg': None}

    def _dispatch(rnd, tools):
        _round['content'] = ''

        def _on_content(t):
            _round['content'] += t
        effective_tools = usage_meter.allowed_tools(tools) if usage_meter else tools
        return _self.dispatch_stream(
            messages, on_content=_on_content, abort_check=abort_signal.is_set,
            prefer_model=model or None, strict_model=bool(model), capability='text',
            tools=effective_tools, max_tokens=_IDEATE_MAX_TOKENS,
            temperature=_IDEATE_TEMPERATURE,
            thinking_enabled=False, log_prefix='[Paper:Ideate]')

    def _on_round_result(rnd, msg, finish, usage):
        _last['msg'] = msg
        if usage_meter:
            usage_meter.observe_agent_round(usage, msg)

    def _begin_tool_round(rnd, msg):
        _round['content'] = ''
        messages.append(msg)

    from lib.paper.report_engine import _execute_report_tool
    _execute_tool = make_research_tool_executor(
        messages, user_question=direction[:300], abort_signal=abort_signal,
        execute_report_tool=_execute_report_tool, on_tool_event=on_tool_event,
        log_prefix='[Paper:Ideate]')

    run_agent_loop(
        abort=abort_signal,
        round_tools=_REPORT_TOOLS, dispatch=_dispatch, execute_tool=_execute_tool,
        on_round_result=_on_round_result, on_tool_round=_begin_tool_round)

    content = _round['content']
    if not content and isinstance(_last['msg'], dict):
        content = _last['msg'].get('content') or ''
    parsed = _parse_llm_json(content)
    if isinstance(parsed, dict) and isinstance(parsed.get('ideas'), list):
        return parsed['ideas']
    if isinstance(parsed, list):
        return parsed
    logger.warning('[Paper:Ideate] generation produced no idea list')
    return []


def _compact_gaps(open_gaps: dict) -> str:
    """Compact the open_gaps map to just the fields the generator needs."""
    import json
    gaps = []
    for g in (open_gaps or {}).get('open_gaps', []):
        if isinstance(g, dict) and g.get('id'):
            gaps.append({'id': g['id'], 'gap': g.get('gap', ''),
                         'why_open': g.get('why_open', ''),
                         'kind_hint': g.get('kind_hint', ''),
                         'evidence': list(g.get('evidence') or [])})
    return json.dumps({'open_gaps': gaps}, ensure_ascii=False)


def _valid_gap_ids(open_gaps: dict) -> set:
    """The set of real open_gap ids an idea may link to (gate ① basis)."""
    ids = set()
    for g in (open_gaps or {}).get('open_gaps', []):
        if isinstance(g, dict) and (g.get('id') or '').strip():
            ids.add(g['id'].strip())
    return ids


def _gap_by_id(open_gaps: dict, gid: str) -> Optional[dict]:
    for g in (open_gaps or {}).get('open_gaps', []):
        if isinstance(g, dict) and g.get('id') == gid:
            return g
    return None


def _ideate_system_prompt(lang: str, n_ideas: int) -> str:
    zh = (lang or 'en').startswith('zh')
    if zh:
        return (
            f'你是一位有品位的资深研究员,正在为一个方向提出 {n_ideas} 个**真正新颖**的研究 idea。\n'
            '铁律:每个 idea 必须 `linked_gap_id` 指向给定 open_gaps 里的一个真实 id —— '
            '**不许凭空发明问题**。core_mechanism 要讲清「为什么 work」的机理,不是操作步骤;'
            'why_not_AB 要正面回答「为什么这不是两个已有件的拼接」;novelty_claim 有证据时引用具体 arxiv_id。'
            '先做一轮静默淘汰：凡是可概括成「已有指标/监测器/损失/模块 + 新场景」的候选都替换掉。'
            '方法类 idea 必须从 gap 的 failure_cause 出发，提出一个能主动改变系统因果动力学的'
            'new_invariant；优先改变 representation/objective/architecture/algorithm/protocol，'
            '不能只在失败后检测、切换阈值或包一层防御。若含反应式组件，必须说明它如何改变状态转移，'
            '而不是只报警。多个 idea 要来自不同的因果机制族，不是同一指标的改写。'
            '每个 idea 还必须从 linked gap 的 evidence 中选一个 `corpus_anchor_id`，并用 '
            '`corpus_delta` 说明对该论文真实机制改了哪一处；库外近邻只能辅助查新，不能替代语料锚点。'
            f'只返回 JSON:{{"ideas":[{{...}} × {n_ideas}]}},每个 idea 含 title/kind/linked_gap_id/'
            'corpus_anchor_id/corpus_delta/failure_cause/new_invariant/intervention_level/'
            'core_mechanism/novelty_claim/'
            'prior_art/falsifiable_prediction/why_not_AB。'
            f'**kind 只能是 {" 或 ".join(_VALID_KINDS)} 两个值之一**'
            '(提出新方法/新机制写 methodology；做度量/实证分析写 analysis)，'
            '可参考对应 open_gap 的 kind_hint。'
            '另必须给 `retrieval_query`：3-8 个用于检索相似论文的**术语**（空格分隔，'
            '不要标点/星号/整句）—— 它会被直接送进论文搜索 API，整段描述会使检索失效。')
    return (
        f'You are a tasteful senior researcher proposing {n_ideas} GENUINELY NOVEL ideas '
        'for a direction.\n'
        'Iron rule: every idea MUST set linked_gap_id to a real id from the given open_gaps '
        '— do NOT invent your own problem. core_mechanism must explain WHY it works (a '
        'mechanism, not steps); why_not_AB must directly answer why this is not two existing '
        'pieces glued; novelty_claim should cite a concrete arxiv_id where evidence supports it. '
        'First run a silent rejection pass: replace any candidate that can be summarized as '
        '"an existing metric, monitor, loss, or module applied to a new setting". A methodology '
        'idea must start from the gap\'s failure_cause and introduce a new_invariant that '
        'proactively changes the system\'s causal dynamics. Prefer interventions at the '
        'representation, objective, architecture, algorithm, or protocol level; a detector, '
        'threshold switch, or defensive wrapper is insufficient unless you explain how it '
        'changes the state transition rather than merely raising an alarm. Ideas must span '
        'different causal mechanism families, not paraphrases of one diagnostic signal. '
        'Every idea must also choose a `corpus_anchor_id` from its linked gap\'s evidence '
        'and use `corpus_delta` to state exactly what changes in that paper\'s actual '
        'mechanism. External neighbours help novelty checking but cannot replace this '
        'in-corpus mechanism anchor. '
        f'Return ONLY JSON: {{"ideas":[{{...}} × {n_ideas}]}}, each idea with title/kind/'
        'linked_gap_id/corpus_anchor_id/corpus_delta/failure_cause/new_invariant/'
        'intervention_level/core_mechanism/'
        'novelty_claim/prior_art/falsifiable_prediction/why_not_AB. '
        f'**kind MUST be exactly one of: {" | ".join(_VALID_KINDS)}** '
        '(a new method/mechanism is "methodology"; a measurement or empirical study is '
        '"analysis") — follow the linked open_gap\'s kind_hint when unsure. '
        'Also give `retrieval_query`: 3-8 space-separated TERMS for finding similar '
        'papers (no punctuation, no asterisks, no full sentences) — it goes straight '
        'into a paper-search API, and a prose paragraph makes the search useless.')


# ── Public entry ───────────────────────────────────────────────────────────

def generate_ideas(direction: str, open_gaps: dict, *, lang: str = 'en',
                   reader_context: str = '', n_ideas: int = 6,
                   threshold: Optional[float] = None, model: Optional[str] = None,
                   abort: Optional[Callable[[], bool]] = None,
                   on_tool_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Generate ideas from R2's open-gap map and run the anti-A+B gate.

    Args:
        direction: the research direction.
        open_gaps: the library-verified gap map from R2 (survey.build_survey's
            ``open_gaps`` — schema_version 1). Its ``open_gaps[].id`` set is the
            basis for the structural gate.
        lang / reader_context / n_ideas / model / abort / on_tool_event: as usual.
        threshold: override IDEATE_GATE_THRESHOLD (calibration only; defaults to
            the module constant).

    Returns:
        {
          'ok': bool,
          'direction': str, 'lang': str,
          'accepted': [ {idea..., 'scores', 'overall', 'mechanism_delta',
                         'closest_neighbor', 'prior_set_ids', ...}, ... ],
          'rejected': [ {idea..., 'reject_stage': 'structural'|'grounding'|'rubric',
                         'reject_reason', 'scores'?, 'overall'?}, ... ],
          'threshold': float,
          'error': str,
        }
    Every rejected idea keeps its scores + the gate that killed it, so the
    threshold can be calibrated from real data.
    """
    import lib.paper.ideate as _self
    from lib.research.telemetry import ResearchUsageMeter, research_token_budget

    direction = (direction or '').strip()
    thr = IDEATE_GATE_THRESHOLD if threshold is None else float(threshold)
    valid_gaps = _valid_gap_ids(open_gaps)
    usage_meter = ResearchUsageMeter(
        'ideate', fallback_model=model or '',
        token_budget=research_token_budget(
            'TOFU_RESEARCH_IDEATE_TOKEN_BUDGET', _IDEATE_AGENT_TOKEN_BUDGET))
    if not direction:
        return {'ok': False, 'error': 'empty direction', 'direction': '', 'lang': lang,
                'accepted': [], 'rejected': [], 'threshold': thr,
                'usage': usage_meter.snapshot()}
    if not valid_gaps:
        return {'ok': False, 'error': 'open_gaps has no verified gap ids (run survey first)',
                'direction': direction, 'lang': lang, 'accepted': [], 'rejected': [],
                'threshold': thr, 'usage': usage_meter.snapshot()}

    try:
        raw = _self._generate_raw_ideas(direction, open_gaps, reader_context, lang,
                                        n_ideas=n_ideas, model=model, abort=abort,
                                        on_tool_event=on_tool_event,
                                        usage_meter=usage_meter)
    except Exception as e:
        from lib.llm_errors import AbortedError
        if isinstance(e, AbortedError):
            return {'ok': False, 'error': 'aborted', 'direction': direction, 'lang': lang,
                    'accepted': [], 'rejected': [], 'threshold': thr,
                    'usage': usage_meter.snapshot()}
        logger.error('[Paper:Ideate] generation failed: %s', e, exc_info=True)
        return {'ok': False, 'error': f'generation failed: {e}', 'direction': direction,
                'lang': lang, 'accepted': [], 'rejected': [], 'threshold': thr,
                'usage': usage_meter.snapshot()}

    accepted, rejected = [], []
    # Batch-wide term census for the fielded query: a term several ideas share is
    # domain background, a term unique to one idea is its identity. Computed ONCE
    # over the whole batch so the identity/domain split is a recomputable
    # structural fact rather than a per-idea guess.
    _batch_terms = []
    for _i in raw:
        if isinstance(_i, dict):
            try:
                _t, _ = _self.build_retrieval_query(_i)
            except Exception as _e:
                # Names the call that actually failed (build_retrieval_query)
                # rather than the enclosing function: a census that silently
                # drops one idea's terms skews the identity/domain split for
                # the WHOLE batch, and 'generate ideas failed' would send the
                # reader looking in the wrong place.
                logger.debug('retrieval-query build for the batch term '
                             'census failed: %s', _e)
                _t = ''
            if _t:
                _batch_terms.append(_t)
    for idea in raw:
        if not isinstance(idea, dict):
            continue
        # `kind` is template metadata, not a validity verdict — coerce it into
        # the frozen enum BEFORE any gate sees it, and record the coercion so
        # the normalization is auditable rather than silent.
        _kind, _kind_changed = _self._normalize_kind(idea.get('kind'))
        idea['kind'] = _kind
        if _kind_changed:
            idea['kind_normalized'] = True
        # Gate ① — structural (free, first)
        reason = _structural_gate(idea, valid_gaps)
        if reason:
            rejected.append({**idea, 'reject_stage': 'structural', 'reject_reason': reason})
            logger.info('[Paper:Ideate] REJECT(structural) %.50s — %s', idea.get('title'), reason)
            continue
        gap = _gap_by_id(open_gaps, idea.get('linked_gap_id'))
        evidence_ids = {_norm_id(raw) for raw in
                        ((gap or {}).get('evidence') or [])}
        evidence_ids.discard('')
        anchor_id = _norm_id(idea.get('corpus_anchor_id'))
        if evidence_ids and anchor_id not in evidence_ids:
            reason = (f'corpus_anchor_id {anchor_id!r} is not evidence for linked gap '
                      f'{idea.get("linked_gap_id")!r}')
            rejected.append({**idea, 'reject_stage': 'structural',
                             'reject_reason': reason})
            logger.info('[Paper:Ideate] REJECT(structural) %.50s — %s',
                        idea.get('title'), reason)
            continue
        idea['corpus_anchor_id'] = anchor_id
        # Grounding — strip hallucinated prior_art
        _grounded, dropped = _ground_idea_prior_art(idea)
        # Gate ② — forced-neighbor retrieval prior set
        prior_set = _novelty_prior_set(idea, batch_terms=_batch_terms)
        # Gate ③ — four-axis rubric judged against the RETRIEVED set
        verdict = _score_idea(idea, prior_set, gap, lang, model=model, abort=abort,
                              usage_meter=usage_meter)
        if verdict is None:
            rejected.append({**idea, 'reject_stage': 'rubric',
                             'reject_reason': 'judge failed/unparseable',
                             'retrieved_ids': prior_set['retrieved_ids'],
                             'self_reported_ids': prior_set['self_reported_ids'],
                             'novelty_basis': prior_set['novelty_basis'],
                             'query_mode': prior_set['query_mode']})
            continue
        # pin #1 hard floor: novelty is f(RETRIEVED set). With an empty basis
        # there is nothing to measure novelty against, so a high rubric score is
        # not evidence of novelty — it is an unjudged idea wearing a score.
        # 宁可判不了,不许假装判过.
        if prior_set['novelty_basis'] == 'none':
            rejected.append({**idea, **verdict, 'reject_stage': 'novelty_basis',
                             'reject_reason': (
                                 'retrieval produced no neighbours (query='
                                 f'{prior_set["retrieval_query"]!r}, source='
                                 f'{prior_set["query_source"]}) — novelty could not be '
                                 'judged against any prior art'),
                             'retrieved_ids': [],
                             'self_reported_ids': prior_set['self_reported_ids'],
                             'novelty_basis': 'none',
                             'retrieval_query': prior_set['retrieval_query']})
            logger.error('[Paper:Ideate] REJECT(novelty_basis) %.50s — empty basis',
                         idea.get('title'))
            continue
        # R2/R3 seam v2 — a gap resting ONLY on grounded-but-unharvested papers
        # (low_confidence) is a weaker foundation: R3 must not fully trust that
        # the idea "solves a real gap". Deterministically dock the value axis by
        # one and recompute overall (mirrors the parameter-level novelty cap —
        # applied HERE, not left to the judge). Flagged + a follow-up-harvest
        # hint so the owner can re-judge after harvesting the missing papers.
        linked_low_conf = bool(isinstance(gap, dict) and gap.get('low_confidence'))
        if linked_low_conf and verdict['scores'].get('value', 0) > 1:
            verdict['scores']['value'] -= 1
            verdict['overall'] = round(
                sum(verdict['scores'].values()) / len(verdict['scores']), 2)
            logger.info('[Paper:Ideate] value axis docked (linked gap %s low_confidence) '
                        'for %.50s → overall %.2f', idea.get('linked_gap_id'),
                        idea.get('title'), verdict['overall'])
        record = {**idea, **verdict,
                  'retrieved_ids': prior_set['retrieved_ids'],
                  'self_reported_ids': prior_set['self_reported_ids'],
                  'novelty_basis': prior_set['novelty_basis'],
                  'retrieval_query': prior_set['retrieval_query'],
                  'query_source': prior_set['query_source'],
                  'query_mode': prior_set['query_mode'],
                  'prior_art_dropped': dropped,
                  'linked_gap_low_confidence': linked_low_conf}
        if verdict['overall'] >= thr:
            accepted.append(record)
            logger.info('[Paper:Ideate] ACCEPT %.50s overall=%.2f', idea.get('title'),
                        verdict['overall'])
        else:
            rejected.append({**record, 'reject_stage': 'rubric',
                             'reject_reason': f'overall {verdict["overall"]:.2f} < threshold {thr}'})
            logger.info('[Paper:Ideate] REJECT(rubric) %.50s overall=%.2f < %.2f',
                        idea.get('title'), verdict['overall'], thr)

    logger.info('[Paper:Ideate] done — direction=%.60s generated=%d accepted=%d rejected=%d thr=%.2f',
                direction, len(raw), len(accepted), len(rejected), thr)
    out = {'ok': True, 'direction': direction, 'lang': lang, 'accepted': accepted,
           'rejected': rejected, 'threshold': thr,
           'usage': usage_meter.snapshot(), 'error': ''}

    # Pipeline-pathology invariant: the zero-LLM structural gate wiping EVERY
    # generated idea is a DEFECT, not 宁缺毋滥 — it means the expensive gates
    # (novelty retrieval + rubric) never ran, so the run proved nothing. A
    # rubric-based zero is honest and is NOT flagged: there the judging happened
    # and the ideas genuinely lost. Without this, 'accepted 0' from a broken
    # gate is indistinguishable from 'accepted 0' from a working one — exactly
    # how the kind bug hid behind 40 green tests.
    structural = [r for r in rejected if r.get('reject_stage') == 'structural']
    # gate_reached — the DEPTH the pipeline actually got to, three states:
    #   'accepted'   at least one idea cleared the whole chain;
    #   'rubric'     the expensive gates ran (retrieval + judging) and the ideas
    #                genuinely lost — an HONEST zero;
    #   'structural' the free gate killed everything; nothing was ever judged.
    # A bare `accepted: 0` cannot distinguish these, which is exactly how the
    # kind bug stayed invisible. Callers (and the frontend) get the depth, not
    # just the count.
    if accepted:
        out['gate_reached'] = 'accepted'
    elif raw and len(structural) == len(raw):
        out['gate_reached'] = 'structural'
    elif rejected:
        out['gate_reached'] = 'rubric'
    else:
        out['gate_reached'] = 'none'

    if raw and not accepted and len(structural) == len(raw):
        from collections import Counter
        reasons = Counter((r.get('reject_reason') or '').split('(')[0].strip()
                          for r in structural)
        dominant, n = reasons.most_common(1)[0]
        out['degraded'] = True
        out['degraded_reason'] = (
            f'structural gate rejected ALL {len(raw)} generated idea(s) — the novelty '
            f'retrieval and rubric never ran; dominant reason ({n}/{len(structural)}): '
            f'{dominant}')
        logger.error('[Paper:Ideate] DEGRADED — %s', out['degraded_reason'])

    return out
