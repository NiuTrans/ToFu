"""Grounding (the anti-hallucination gate — same guarantee as recommend).

Every paper the insight name-drops is verified against arXiv through THIS
package's ``search_arxiv`` / ``fetch_arxiv_title`` (re-exported on the facade so
tests monkeypatch ONE namespace). An ungrounded ref is stripped to ``None`` (the
prose survives, the fake link dies). A connection that bridges the paper back to
ITSELF is dropped entirely (the foundational-paper backfire).

The arXiv seams (``search_arxiv`` / ``fetch_arxiv_title``) are resolved THROUGH
the package facade at call time (``import lib.paper.insight_engine as _pkg``) so
a test patching ``ie.search_arxiv`` bites here exactly as it did in the original
single-module layout.
"""

import re

from lib.log import get_logger

from ..arxiv import _extract_arxiv_id
from ..recommend_engine import _norm_id, _title_grounded

logger = get_logger(__name__)


def _self_identity(phash, report_md, self_arxiv_id=None, self_title=None):
    """Resolve the identity (arxiv_id, title) of the paper UNDER ANALYSIS.

    Needed by the self-reference guard: a "connection" whose target IS this
    paper is vacuous (the bd79f6/Transformer failure — a foundational paper's
    library descendants make the model bridge the paper back to itself). Tries
    explicit args → the paper_library row for ``phash`` → the report's own head
    (Paper Card title + any arXiv id). Best-effort; either field may be ''.
    """
    aid = _extract_arxiv_id(str(self_arxiv_id)) if self_arxiv_id else None
    title = (self_title or '').strip()
    if aid and title:
        return aid, title

    if phash and (not aid or not title):
        try:
            from lib.database import get_db, get_thread_db
            try:
                db = get_db()
            except RuntimeError as e:
                logger.debug('[Paper:Insight] no request-context DB, using thread DB: %s', e)
                db = get_thread_db()
            row = db.execute(
                "SELECT title, arxiv_id FROM paper_library WHERE paper_hash = ? LIMIT 1",
                (phash,)).fetchone()
            if row:
                if not title:
                    title = (row['title'] or '').strip()
                if not aid and row['arxiv_id']:
                    aid = _extract_arxiv_id(str(row['arxiv_id']))
        except Exception as e:
            logger.debug('[Paper:Insight] self-identity DB lookup failed: %s', e)

    head = (report_md or '')[:2500]
    if not title:
        m = re.search(r'^#\s+(.+?)\s*$', head, re.MULTILINE)
        if m:
            title = m.group(1).strip()
    if not aid:
        aid = _extract_arxiv_id(head)
    return (aid or None), (title or '')


def _is_self_reference(ref, conn_text, self_aid, self_title):
    """Does this connection's target refer to the paper UNDER ANALYSIS itself?

    Checked on the CLAIMED ref (before grounding), because grounding a
    self-titled ref can fuzzy-match a spurious paper (bd79f6 grounded
    "Attention Is All You Need" to a title-meme paper) and thereby hide the
    vacuity. Three signals:
      1. claimed arXiv id == the paper's own id;
      2. claimed title strongly matches the paper's own title;
      3. circular prose — the paper's own title named 2+ times in the bridge
         text (the "X is a generalized form of X itself" degenerate form).
    """
    if isinstance(ref, dict):
        if self_aid:
            claimed = _extract_arxiv_id(str(ref.get('arxiv_id'))) if ref.get('arxiv_id') else None
            if claimed and _norm_id(claimed) == _norm_id(self_aid):
                return True
        if self_title:
            rt = (ref.get('title') or '').strip()
            if rt and _title_grounded(rt, self_title):
                return True
    if self_title and conn_text:
        st = self_title.lower()
        if len(st) >= 8 and conn_text.lower().count(st) >= 2:
            return True
    return False


def _ground_ref(ref):
    """Verify a ``{title, arxiv_id}`` ref against arXiv; return a card or None.

    Reuses the recommend engine's pure matching helpers but drives the search
    through THIS package's ``search_arxiv`` / ``fetch_arxiv_title`` (so tests
    patch one namespace). A ref that cannot be grounded is dropped (logged) —
    the prose that mentioned it survives, but the clickable/verifiable link does
    not, so a hallucinated paper never reaches the reader as fact.
    """
    if not isinstance(ref, dict):
        return None
    title = (ref.get('title') or '').strip()
    raw_id = ref.get('arxiv_id')
    claimed_id = _extract_arxiv_id(str(raw_id)) if raw_id else None
    if not title and not claimed_id:
        return None

    # Resolve the arXiv seams through the facade so ``ie.search_arxiv`` /
    # ``ie.fetch_arxiv_title`` monkeypatches bite exactly as in the flat module.
    import lib.paper.insight_engine as _pkg
    search_arxiv = _pkg.search_arxiv
    fetch_arxiv_title = _pkg.fetch_arxiv_title

    results = search_arxiv(title, max_results=5) if title else []
    if claimed_id:
        for r in results:
            if _norm_id(r.get('arxiv_id')) == _norm_id(claimed_id):
                return {'title': r.get('title') or title, 'arxiv_id': r.get('arxiv_id'),
                        'abs_url': r.get('abs_url') or f'https://arxiv.org/abs/{r.get("arxiv_id")}'}
    if results and _title_grounded(title, results[0].get('title', '')):
        r = results[0]
        return {'title': r.get('title') or title, 'arxiv_id': r.get('arxiv_id'),
                'abs_url': r.get('abs_url') or f'https://arxiv.org/abs/{r.get("arxiv_id")}'}
    if claimed_id:
        real_title = fetch_arxiv_title(claimed_id)
        if real_title:
            return {'title': real_title, 'arxiv_id': claimed_id,
                    'abs_url': f'https://arxiv.org/abs/{claimed_id}'}
    logger.debug('[Paper:Insight] Dropped ungrounded ref: %.120s (claimed id=%s)',
                 title or '(no title)', claimed_id)
    return None


def _ground_insight(insight, self_aid=None, self_title=''):
    """Ground every paper ref in a parsed insight dict, in place.

    Mutates ``connections[].paper`` and ``open_problems[].grounded_by`` to either
    a grounded card (real title + arxiv_id + abs_url) or ``None``. Returns
    ``(grounded_count, dropped_count, selfref_count)``.

    SELF-REFERENCE GUARD (fix for the foundational-paper backfire): a connection
    whose target IS the paper under analysis is VACUOUS — grounding proves a ref
    exists, not that the bridge is non-vacuous. Such connections are removed
    ENTIRELY (not just their link nulled) because the surrounding prose is the
    circular "X is a generalized form of X itself" text, which is worse than no
    connection. Detection runs on the CLAIMED ref BEFORE grounding, since
    grounding a self-titled ref can fuzzy-match a spurious paper and hide it.
    """
    grounded = 0
    dropped = 0
    selfref = 0
    if not isinstance(insight, dict):
        return (0, 0, 0)

    conns = insight.get('connections') or []
    kept_conns = []
    for conn in conns:
        if not isinstance(conn, dict):
            continue
        ref = conn.get('paper')
        text = conn.get('text') or ''
        if (self_aid or self_title) and _is_self_reference(ref, text, self_aid, self_title):
            selfref += 1
            logger.info('[Paper:Insight] Dropped self-referential connection: %.100s', text)
            continue
        if isinstance(ref, dict) and (ref.get('title') or ref.get('arxiv_id')):
            card = _ground_ref(ref)
            conn['paper'] = card
            if card:
                grounded += 1
            else:
                dropped += 1
        kept_conns.append(conn)
    if isinstance(insight.get('connections'), list):
        insight['connections'] = kept_conns

    for op in insight.get('open_problems') or []:
        if not isinstance(op, dict):
            continue
        ref = op.get('grounded_by')
        if isinstance(ref, dict) and (ref.get('title') or ref.get('arxiv_id')):
            if (self_aid or self_title) and _is_self_reference(ref, op.get('text') or '',
                                                               self_aid, self_title):
                op['grounded_by'] = None
                selfref += 1
                continue
            card = _ground_ref(ref)
            op['grounded_by'] = card
            if card:
                grounded += 1
            else:
                dropped += 1

    return (grounded, dropped, selfref)
