"""lib/branch_meta.py — Branch title → icon classification.

Pure-function port of ``static/js/branch.js:_branchAutoIcon``. Decides
which emoji to assign a branch based on keywords in its title.

Why server-side?
----------------
The (currently UI-only) branch creation flow is on the migration path
to ``POST /api/v1/conversations/{id}/branches``. Once that exists,
the title→icon mapping becomes a per-request policy decision the
caller shouldn't have to know about. Centralising it here also means:

* SDK callers get the same icons the UI shows — no policy
  duplication in CI scripts that auto-create branches.
* The keyword tables can be tuned in one place.
* Tests verify the contract instead of relying on JS regex review.

Also exposes ``classify_branch_title`` returning a richer dict
(``{icon, kind}``) so future callers (search, filtering, statistics)
have a structured handle instead of just a glyph.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Each entry: (regex, icon, kind). Order matters — first match wins.
# Patterns mirror the JS ``_branchAutoIcon`` exactly (case-insensitive).
# Icon is the empty string in the original JS (it returns "" for
# everything because the actual icons were stripped at some point);
# we keep the same behaviour to preserve render parity, but expose
# ``kind`` for callers that want a semantic label.
_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r'paper|论文|arxiv', re.IGNORECASE), '', 'paper'),
    (re.compile(r'code|代码|实现|implement', re.IGNORECASE), '', 'code'),
    (re.compile(r'data|数据|dataset', re.IGNORECASE), '', 'data'),
    (re.compile(r'math|公式|proof|证明', re.IGNORECASE), '', 'math'),
    (re.compile(r'image|图|visual|vision', re.IGNORECASE), '', 'image'),
    (re.compile(r'compare|对比|vs\.?', re.IGNORECASE), '', 'compare'),
    (re.compile(r'bug|error|issue|问题', re.IGNORECASE), '', 'bug'),
    (re.compile(r'todo|plan|计划', re.IGNORECASE), '', 'todo'),
    (re.compile(r'idea|想法|thought', re.IGNORECASE), '', 'idea'),
    (re.compile(r'summary|总结|概述', re.IGNORECASE), '', 'summary'),
]


def branch_auto_icon(title: Optional[str]) -> str:
    """Return the icon for a branch title. Mirrors JS ``_branchAutoIcon``.

    Always returns a string (possibly empty). Never raises.
    """
    if not title or not isinstance(title, str):
        return ''
    for pat, icon, _kind in _PATTERNS:
        if pat.search(title):
            return icon
    return ''


def classify_branch_title(title: Optional[str]) -> dict:
    """Classify a branch title. Returns ``{'icon': str, 'kind': str}``.

    ``kind`` is one of ``paper / code / data / math / image / compare /
    bug / todo / idea / summary / generic`` — useful for filtering or
    aggregating branch lists in the API.
    """
    if not title or not isinstance(title, str):
        return {'icon': '', 'kind': 'generic'}
    for pat, icon, kind in _PATTERNS:
        if pat.search(title):
            return {'icon': icon, 'kind': kind}
    return {'icon': '', 'kind': 'generic'}


__all__ = ['branch_auto_icon', 'classify_branch_title']
