"""Deterministic anchor resolution — the model NOMINATES, code DECIDES.

The insight prompt lets the model attach an optional ``anchor`` (the heading
text of the report section a connection / provocation relates to) to each item.
Model output is untrusted: the heading it names may not exist, may be
paraphrased, or may drift across languages. This module resolves each
nomination against the report's ACTUAL h2/h3 headings with a deterministic
chain (the ``finalize_review_body`` discipline — prompt asks, code belts):

  1. normalized exact match (case / whitespace / emoji stripped);
  2. fuzzy match — Jaccard token overlap >= ``_FUZZY_THRESHOLD`` (mixed token
     set: Latin words + CJK characters, so it works for both report languages);
     ties go to the EARLIER heading;
  3. otherwise ``None`` — the item falls back to the end-of-report section
     (today's behaviour). Never guess past the threshold: a card anchored to
     the WRONG section is worse than no anchor.

The resolved ``anchor_idx`` (0-based index into the report's h2/h3 sequence)
is persisted with the insight row, so the read path never re-resolves against
a possibly-edited body.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

_HEADING_RE = re.compile(r'^(#{2,3})\s+(.+?)\s*$', re.MULTILINE)

# Minimum Jaccard overlap for a fuzzy anchor match. Chosen a-priori: high
# enough that "Method — How It Works" never fuzzy-matches "Experimental
# Setup", low enough to tolerate the model dropping an emoji or a dash suffix.
_FUZZY_THRESHOLD = 0.6

_CJK_RE = re.compile(r'[぀-ヿ㐀-䶿一-鿿豈-﫿]')


def extract_report_headings(report_md):
    """Return the report's h2/h3 headings in document order.

    Returns a list of ``{'index', 'level', 'text'}`` — ``index`` is the 0-based
    position in the h2/h3 sequence (the SAME enumeration the frontend uses when
    it queries ``article h2, h3``), ``level`` is 2 or 3, ``text`` the raw
    heading text. Code fences are stripped first so a ```## example``` inside a
    code block is not mistaken for a heading.
    """
    body = re.sub(r'```.*?```', '', report_md or '', flags=re.DOTALL)
    out = []
    for m in _HEADING_RE.finditer(body):
        out.append({'index': len(out), 'level': len(m.group(1)),
                    'text': m.group(2).strip()})
    return out


def _norm_text(s):
    """Normalize for exact matching: lowercase, strip emoji/decoration, collapse
    whitespace. Keeps CJK + alphanumerics only — punctuation/dashes/colons go."""
    if not s:
        return ''
    s = s.lower()
    # Drop anything that is not a letter/number/CJK — emoji, #, *, :, —, …
    s = re.sub(r'[^\w一-鿿㐀-䶿豈-﫿぀-ヿ]+', ' ', s, flags=re.UNICODE)
    return ' '.join(s.split())


def _token_set(s):
    """Mixed token set: Latin word runs + individual CJK characters.

    Chinese headings have no whitespace, so word tokens alone would make every
    zh heading a single giant token; CJK chars give the overlap metric
    resolution, Latin words keep english headings cheap.
    """
    s = _norm_text(s)
    toks = set()
    for part in s.split():
        cjk = _CJK_RE.findall(part)
        if cjk:
            toks.update(cjk)
        latin = re.sub(r'[぀-ヿ㐀-䶿一-鿿豈-﫿]', ' ', part)
        toks.update(t for t in latin.split() if t)
    return toks


def resolve_anchor(anchor_text, headings):
    """Resolve one nominated anchor to a heading index, or ``None``.

    Args:
        anchor_text: the model's nomination (heading text it wrote).
        headings: ``extract_report_headings`` output.

    Returns:
        The 0-based heading index, or None when nothing meets the bar.
    """
    if not anchor_text or not headings:
        return None
    want = _norm_text(anchor_text)
    if not want:
        return None

    # 1. exact (normalized)
    for h in headings:
        if _norm_text(h['text']) == want:
            return h['index']

    # 2. fuzzy — best Jaccard overlap at/above the threshold; ties → earlier.
    want_toks = _token_set(anchor_text)
    if not want_toks:
        return None
    best_idx = None
    best_score = 0.0
    for h in headings:
        have = _token_set(h['text'])
        if not have:
            continue
        inter = len(want_toks & have)
        if not inter:
            continue
        score = inter / len(want_toks | have)
        if score > best_score:
            best_score = score
            best_idx = h['index']
    if best_idx is not None and best_score >= _FUZZY_THRESHOLD:
        return best_idx
    return None


def _iter_anchor_items(insight):
    """Yield the mutable items that may carry an anchor nomination.

    Connections are dicts; provocations may be plain strings (legacy schema)
    or ``{'text', 'anchor'}`` dicts (v2 schema) — strings are upgraded in
    place to dicts so the anchor survives, while a string WITHOUT an anchor is
    left as-is (byte-identical legacy shape).
    """
    conns = insight.get('connections')
    if isinstance(conns, list):
        for c in conns:
            if isinstance(c, dict):
                yield c
    provs = insight.get('provocations')
    if isinstance(provs, list):
        for i, p in enumerate(provs):
            if isinstance(p, dict):
                yield p
            elif isinstance(p, str) and p.strip():
                # Legacy string provocation — no anchor nomination possible.
                continue


def resolve_insight_anchors(insight, report_md):
    """Resolve every nominated anchor in an insight dict, in place.

    Each item that nominates ``anchor`` gets ``anchor_idx`` set to the resolved
    heading index, or ``None`` when the nomination does not resolve (fall back
    to the end-of-report section). Items without a nomination are untouched
    (no ``anchor_idx`` key). Returns ``{'nominated': n, 'resolved': m}``.
    """
    stats = {'nominated': 0, 'resolved': 0}
    if not isinstance(insight, dict):
        return stats
    headings = extract_report_headings(report_md)
    for item in _iter_anchor_items(insight):
        nomination = item.get('anchor')
        if not nomination:
            continue
        stats['nominated'] += 1
        idx = resolve_anchor(str(nomination), headings)
        item['anchor_idx'] = idx
        if idx is not None:
            stats['resolved'] += 1
        else:
            logger.info('[Paper:Insight] anchor nomination unresolved (→ end section): %.80s',
                        nomination)
    if stats['nominated']:
        logger.info('[Paper:Insight] anchor resolution — %d/%d nominated resolved',
                    stats['resolved'], stats['nominated'])
    return stats
