"""H2-section splitting + heading regexes + section-scoped suppressors.

Holds the header regexes (``_H2_RE`` / ``_GLOSSARY_HDR_RE`` / ``_CARD_HDR_RE`` /
``_RELATED_HDR_RE``) and the functions that reason about *where* a term appears:
``_split_sections`` (document-order H2 split), ``_is_inline_defined`` (the body
expands the term at its point of use), and ``_only_in_related_work`` (a term
seen ONLY inside the Research-Landscape survey is a citation, not jargon). All
stateless (regex consts + pure functions).
"""

from __future__ import annotations

import re

from lib.log import get_logger

from ._acronyms import _strip_code

logger = get_logger(__name__)

# A markdown H2 section header, e.g. ``## 💡 Method``.
_H2_RE = re.compile(r'^##\s+(.*)$', re.MULTILINE)
# The glossary heading — matched loosely (emoji optional, EN or ZH).
_GLOSSARY_HDR_RE = re.compile(
    r'core\s+terminology|核心术语|术语表|glossary', re.IGNORECASE)
# The Paper Card heading — excluded from "body usage" (it is metadata, and
# citing an id like DOI there is not term usage).
_CARD_HDR_RE = re.compile(r'paper\s+card|论文信息卡|信息卡', re.IGNORECASE)
# The Research-Landscape / related-work heading. A capitalised method/model name
# that appears ONLY inside this section is a CITATION (a named prior/concurrent/
# follow-up system), not undefined jargon the reader must grasp to follow the
# report — so it is suppressed. EN: "🗺️ Research Landscape & Impact";
# ZH: "🗺️ 研究全景与影响".
_RELATED_HDR_RE = re.compile(
    r'research\s+landscape|related\s+work|研究全景|相关工作|研究图景', re.IGNORECASE)


def _is_inline_defined(term: str, body: str) -> bool:
    """True if ``body`` expands ``term`` inline, giving the reader its meaning.

    Recognises the two conventional expansion forms:
      * ``TERM (Expanded Words)``  — acronym first, gloss in parens;
      * ``Expanded Words (TERM)``  — gloss first, acronym in parens.
    Either way the reader was handed the meaning at the point of use, so the term
    is NOT a self-containment gap even without a glossary row.
    """
    esc = re.escape(term)
    # TERM (something wordy) — require letters + a little length in the parens so
    # a bare "(n)" / "(1)" citation-count doesn't count as a definition.
    if re.search(esc + r'\s*\(\s*[A-Za-z][^)]{3,}\)', body):
        return True
    # Expanded Words (TERM) — a multi-word capitalised phrase immediately before
    # "(TERM)". Requires >=2 words so a single adjacent word isn't over-matched.
    if re.search(r'(?:[A-Z][A-Za-z\-]+\s+){1,}[A-Za-z\-]+\s*\(\s*' + esc + r'\s*\)', body):
        return True
    return False


def _only_in_related_work(term: str, sections) -> bool:
    """True if ``term`` occurs ONLY inside the Research-Landscape section.

    Such a token is a named prior/concurrent/follow-up system cited in the
    survey — not a concept the reader needs defined to follow THIS paper's
    method. If it also appears in any non-related-work body section it is
    load-bearing there and is NOT suppressed.
    """
    esc = re.compile(r'\b' + re.escape(term) + r'\b')
    seen_related = seen_other = False
    for header, body in sections:
        if _GLOSSARY_HDR_RE.search(header) or _CARD_HDR_RE.search(header):
            continue
        if not esc.search(_strip_code(body)):
            continue
        if _RELATED_HDR_RE.search(header):
            seen_related = True
        else:
            seen_other = True
    return seen_related and not seen_other


def _split_sections(report_text: str) -> list[tuple[str, str]]:
    """Split the report into ``(header, body)`` H2 sections in document order.

    Text before the first H2 (title / H1) is returned under an empty header.
    """
    sections: list[tuple[str, str]] = []
    matches = list(_H2_RE.finditer(report_text))
    if not matches:
        return [('', report_text)]
    pre = report_text[:matches[0].start()].strip()
    if pre:
        sections.append(('', pre))
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(report_text)
        sections.append((header, report_text[start:end]))
    return sections
