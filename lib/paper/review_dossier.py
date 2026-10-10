"""Deterministic peer-review dossier + paper skeleton scaffolds.

``build_review_dossier`` assembles the packet that lets the CALLING model
act as a rigorous reviewer: a NeurIPS-style rubric, the paper's key content
sliced to a character budget, a claims-audit protocol, and a mandated
prioritized revision list. It deliberately makes NO model call itself — the
review authority stays with the model that requested the dossier, and the
builder stays pure, testable and reproducible (docs/PAPER_QUALITY.md).

``build_scaffold`` renders a main.tex skeleton plus per-section stub
contents as pure text for the user to confirm before any file is written.
"""
from __future__ import annotations

import re

from lib.paper.bib import parse_entries
from lib.paper.checks import (_ABSTRACT, _CAPTION, _SECTION, TexSource,
                              _balanced_braces, read_tex_sources)

_MAX_SECTIONS = 32
_MAX_TITLE = 200
_MAX_CAPTIONS = 32
_MAX_BIB_ENTRIES = 64
_FULL_SECTION_CHARS = 4096        # abstract/intro/conclusion each
_FIRST_PARAGRAPH_CHARS = 700      # per remaining section
_SOURCE_BUDGET_BYTES = 8 * 1024 * 1024

_SLUG = re.compile(r'[^a-z0-9]+')
_BLANK_LINE = re.compile(r'\n\s*\n')

_RUBRIC = """\
## Review rubric (score each 1-10, one-line justification per score)

1. **Soundness** — are claims supported by the experiments/proofs actually
   presented? Any methodological gaps, missing baselines, unfair comparisons?
2. **Presentation** — structure, clarity, notation consistency, figure/table
   quality, writing-level issues a reviewer would flag.
3. **Contribution** — significance and novelty relative to the cited prior
   art; is the delta over the closest work made explicit?
4. **Related work** — coverage and accuracy; are key citations missing,
   mischaracterized, or only name-dropped?
5. **Reproducibility** — could a competent reader reproduce the results from
   what is stated (data, code, hyperparameters, seeds, compute)?

Then give an **Overall recommendation** (1-10) with a one-paragraph
justification, followed by:
- **Strengths** — bullet list, most important first.
- **Weaknesses** — bullet list, most important first.
- **Questions to the authors** — what must be answered at rebuttal."""

_CLAIMS_PROTOCOL = """\
## Claims audit (mandatory)

Extract every load-bearing claim — abstract/introduction promises, stated
improvements over baselines, "we show/demonstrate/prove" sentences, and any
quantitative assertion. Classify each as exactly one of:

- **supported** — backed by the paper's own experiments, proofs, or an
  adequate citation of prior work;
- **unsupported** — no evidence presented anywhere in the paper;
- **overstated** — evidence exists but covers a narrower claim (e.g. one
  dataset, one metric, small deltas stated as "significant");
- **contradicted** — the paper's own tables/figures/related work argue
  against it.

Output as a table, one row per claim, most consequential first:

| # | Claim (short quote) | Location | Verdict | Evidence / why |
|---|---------------------|----------|---------|----------------|"""

_REVISION_FOOTER = """\
## Required ending

Conclude the review with a **prioritized revision plan**: P0 items block
submission (must fix), P1 items materially strengthen the paper, P2 items
are polish. Each item names the section it applies to and the concrete edit.
"""


def _slug(title: str, used: set[str]) -> str:
    slug = _SLUG.sub('-', title.lower()).strip('-')[:48] or 'section'
    candidate, suffix = slug, 2
    while candidate in used:
        candidate = '%s-%d' % (slug, suffix)
        suffix += 1
    used.add(candidate)
    return candidate


def _first_paragraph(body: str) -> str:
    """First prose paragraph of a section body, comment noise removed."""
    chunks = _BLANK_LINE.split(body.strip(), maxsplit=1)
    paragraph = chunks[0].strip() if chunks else ''
    return paragraph[:_FIRST_PARAGRAPH_CHARS]


def _captions(source: TexSource) -> list[str]:
    captions = []
    for match in _CAPTION.finditer(source.text):
        content, _end = _balanced_braces(source.text, match.end() - 1)
        captions.append(' '.join(content.split())[:240])
    return captions


def _named_section(sources: list[TexSource], pattern: re.Pattern) -> str:
    """Full body of the first section whose title matches, else ''."""
    for source in sources:
        sections = [m for m in _SECTION.finditer(source.text)
                    if m.group(1) == 'section']
        for index, match in enumerate(sections):
            title, _end = _balanced_braces(source.text, match.end() - 1)
            if not pattern.search(title):
                continue
            stop = (sections[index + 1].start()
                    if index + 1 < len(sections) else len(source.text))
            return source.text[match.end():stop].strip()
    return ''


def _slice_paper(sources: list[TexSource], bibs: list[str],
                 budget: int) -> str:
    """Priority-ordered key content of the paper within ``budget`` chars."""
    parts: list[str] = []

    def add(label: str, text: str, cap: int) -> None:
        if not text:
            return
        snippet = text[:cap]
        if len(text) > cap:
            snippet += '\n… [truncated]'
        parts.append('### %s\n%s' % (label, snippet))

    abstract = ''
    for source in sources:
        match = _ABSTRACT.search(source.text)
        if match:
            abstract = ' '.join(match.group(1).split())
            break
    add('Abstract (full)', abstract, _FULL_SECTION_CHARS)
    add('Introduction (full)',
        _named_section(sources, re.compile(r'introduction', re.I)),
        _FULL_SECTION_CHARS)
    add('Conclusion (full)',
        _named_section(sources, re.compile(r'conclusion', re.I)),
        _FULL_SECTION_CHARS)

    outline = []
    for source in sources:
        sections = [m for m in _SECTION.finditer(source.text)
                    if m.group(1) == 'section']
        for index, match in enumerate(sections):
            title, _end = _balanced_braces(source.text, match.end() - 1)
            stop = (sections[index + 1].start()
                    if index + 1 < len(sections) else len(source.text))
            outline.append('- %s:%d `%s`\n  %s' % (
                source.path, source.text.count('\n', 0, match.start()) + 1,
                ' '.join(title.split()),
                _first_paragraph(source.text[match.end():stop])))
    add('Section outline (heads + first paragraphs)', '\n'.join(outline),
        budget // 3)

    captions = [caption for source in sources
                for caption in _captions(source)][: _MAX_CAPTIONS]
    add('All figure/table captions', '\n'.join('- ' + c for c in captions),
        budget // 4)

    entries = []
    for bib in bibs:
        parsed, _duplicates = parse_entries(bib.raw)
        entries.extend('- `%s` (%s, %s:%d)'
                       % (entry.key, entry.entry_type, bib.path, entry.line)
                       for entry in parsed)
    add('Bibliography entries (%d)' % len(entries),
        '\n'.join(entries[:_MAX_BIB_ENTRIES]) or '(no .bib file found)',
        budget // 4)

    text = '\n\n'.join(parts)
    if len(text) > budget:
        text = text[:budget] + '\n… [paper content truncated to budget]'
    return text


def build_review_dossier(root, main_file: str, *,
                         max_chars: int = 24000) -> str:
    """Assemble the review packet for the calling model. Deterministic.

    ``root`` is the already-authorized paper tree (typically the main
    file's directory); ``main_file`` is informational (relative to root).
    The result never exceeds ``max_chars`` by more than one truncation
    marker line.
    """
    sources, bibs = read_tex_sources(root, budget_bytes=_SOURCE_BUDGET_BYTES)
    overhead = len(_RUBRIC) + len(_CLAIMS_PROTOCOL) + len(_REVISION_FOOTER) + 512
    paper_budget = max(2000, max_chars - overhead)
    paper = _slice_paper(sources, bibs, paper_budget)
    header = ('# Peer-review dossier: `%s`\n\n'
              'You are now an expert reviewer for a top venue. Review the '
              'paper content below rigorously and honestly; do not inflate '
              'scores. Fill every section of the rubric, run the claims '
              'audit, and end with the revision plan.' % main_file)
    dossier = '\n\n'.join([header, _RUBRIC, '## Paper content\n\n' + paper,
                           _CLAIMS_PROTOCOL, _REVISION_FOOTER])
    if len(dossier) > max_chars:
        dossier = dossier[:max_chars - 40] + '\n… [dossier cut at budget]'
    return dossier


def build_scaffold(title: str, sections: list[str], *,
                   bibliography: str = 'refs') -> dict:
    """Render a paper skeleton as pure text (no writes).

    Returns {'main_tex': str, 'files': [{'path', 'content'}]} where files
    are ``sections/<slug>.tex`` stubs (``\\section`` + a TODO comment).
    """
    clean_title = ' '.join(str(title or '').split())[:_MAX_TITLE] or 'Untitled'
    titles = []
    for raw in (sections or [])[:_MAX_SECTIONS]:
        name = ' '.join(str(raw or '').split())[:120]
        if name:
            titles.append(name)
    if not titles:
        titles = ['Introduction', 'Method', 'Experiments', 'Conclusion']
    used: set[str] = set()
    stubs = [{'title': name, 'slug': _slug(name, used)} for name in titles]

    inputs = '\n'.join('\\input{sections/%s}' % stub['slug'] for stub in stubs)
    main_tex = (
        '\\documentclass{article}\n'
        '\\usepackage{amsmath,amssymb}\n'
        '\\usepackage{graphicx}\n'
        '\\usepackage[numbers]{natbib}\n'
        '\\usepackage{hyperref}\n\n'
        '\\title{%s}\n'
        '\\author{Anonymous}\n\n'
        '\\begin{document}\n'
        '\\maketitle\n\n'
        '\\begin{abstract}\n'
        '%% TODO: abstract (problem, approach, key result, one number)\n'
        '\\end{abstract}\n\n'
        '%s\n\n'
        '\\bibliographystyle{plainnat}\n'
        '\\bibliography{%s}\n'
        '\\end{document}\n' % (clean_title, inputs, bibliography))
    files = [{'path': 'sections/%s.tex' % stub['slug'],
              'content': '\\section{%s}\n%% TODO: %s\n' % (stub['title'],
                                                           stub['title'].lower())}
             for stub in stubs]
    return {'title': clean_title, 'main_tex': main_tex, 'files': files}
