"""jsdom test for the Paper Reading-Mode glossary hover card.

Loads the REAL shipped ``static/js/paper/report.js`` under jsdom and drives the
glossary pipeline (``_extractGlossary`` + ``_decorateGlossaryTerms``) against a
report article whose "Core Terminology" table holds an ALREADY-RENDERED KaTeX
definition — the exact situation in production, where the report body has been
markdown+KaTeX rendered before the glossary is parsed.

The bug this guards against:
  • ``_extractGlossary`` used ``cell.textContent`` for the definition. A
    KaTeX-rendered cell carries BOTH the visual .katex-html tree AND a hidden
    .katex-mathml annotation with the raw LaTeX, so textContent produced the
    garbled "T/F=生成长度/NFE\\text{T/F}=…" from the screenshot.
  • the tooltip was a pure-CSS ``::after{content:attr(data-def)}`` — it could
    only ever show PLAIN TEXT, so no Markdown/KaTeX could render.

The fix: capture the cell's rendered ``innerHTML`` into a real DOM hover card
(``.paper-term-card``) and derive a CLEAN plain-text (mathml stripped) form for
aria-label.

Checks:
  • the extracted plain def does NOT contain the raw ``\\text{`` LaTeX leak;
  • a later mention of the term becomes a ``.paper-term`` span with tabindex;
  • that span holds a ``.paper-term-card`` child whose innerHTML preserves the
    rendered KaTeX (``.katex`` element), i.e. it renders, not shows raw text;
  • the visible term text stays exactly the matched word (card is a child,
    not inline in the flow);
  • reading-word count EXCLUDES the duplicated card text.

Negative control (in-harness): re-deriving the def via naive textContent (the
old behaviour) makes the "no LaTeX leak" check FAIL.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A "Core Terminology" table whose definition cell has been KaTeX-rendered.
// This mirrors what katex.renderToString() emits: a .katex wrapper containing
// BOTH a hidden .katex-mathml <annotation> with the raw LaTeX AND the visual
// .katex-html tree. A naive textContent concatenates the annotation source
// (the leak) with the glyphs.
const katexRendered =
  '<span class="katex">' +
    '<span class="katex-mathml">' +
      '<math><semantics><annotation encoding="application/x-tex">' +
        '\\text{T/F} = \\text{生成长度}/\\text{NFE}' +
      '</annotation></semantics></math>' +
    '</span>' +
    '<span class="katex-html" aria-hidden="true">T/F = 生成长度/NFE</span>' +
  '</span>';

const article = document.createElement('article');
article.className = 'paper-report-article';
article.innerHTML =
  '<h2>Glossary</h2>' +
  '<table><thead><tr><th>Term</th><th>Definition</th></tr></thead><tbody>' +
    '<tr><td>NFE</td><td>' + katexRendered + '</td></tr>' +
  '</tbody></table>' +
  '<h2>Method</h2>' +
  '<p>Later the paper discusses NFE budget in detail.</p>';

const glossary = _extractGlossary(article);
check('glossary_extracted_one_row', glossary.length === 1);
const def = glossary.length ? glossary[0].def : '';
// The CLEAN plain-text def must NOT carry the raw LaTeX annotation source.
check('def_no_latex_leak', def.indexOf('\\text{') === -1);
check('def_has_visual_text', def.indexOf('T/F') !== -1 && def.indexOf('NFE') !== -1);
// The rendered HTML must be preserved for the card.
check('glossary_kept_html', /katex/.test(glossary.length ? (glossary[0].defHtml || '') : ''));

// Negative control: the OLD behaviour (naive textContent) leaks the LaTeX.
const naive = (article.querySelector('tbody td:nth-child(2)').textContent || '')
  .replace(/\s+/g, ' ').trim();
check('NC_naive_textContent_leaks', naive.indexOf('\\text{') !== -1);

// Decorate later mentions and inspect the produced span + card.
_decorateGlossaryTerms(article, glossary);
const term = article.querySelector('.paper-term');
check('later_mention_decorated', !!term);
check('term_is_focusable', !!term && term.getAttribute('tabindex') === '0');
// Visible label is just the matched word — the card is a hidden child.
check('term_visible_text', !!term && term.firstChild && term.firstChild.nodeType === 3 &&
      term.firstChild.nodeValue === 'NFE');
const card = term && term.querySelector('.paper-term-card');
check('card_present', !!card);
// The card renders the KaTeX (has a .katex element) rather than raw source.
check('card_renders_katex', !!card && !!card.querySelector('.katex'));
check('card_no_raw_latex_visible',
      !!card && (card.querySelector('.katex-html') ? card.querySelector('.katex-html').textContent : '')
        .indexOf('\\text{') === -1);
check('card_aria_hidden', !!card && card.getAttribute('aria-hidden') === 'true');

// Reading-word count must EXCLUDE the duplicated card text.
const wordsWithCard = _countReadingWords(article);
// Remove the card, recount — the count must be identical (card text ignored).
const clone = article.cloneNode(true);
const cardsInClone = clone.querySelectorAll('.paper-term-card');
for (let i = 0; i < cardsInClone.length; i++) cardsInClone[i].remove();
const wordsNoCard = _countReadingWords(clone);
check('reading_count_ignores_card', wordsWithCard === wordsNoCard && wordsWithCard > 0);

console.log(out.join('\n'));
process.exit(0);
"""


def _run():
    harness = os.path.join(HERE, '_paper_glossary_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'paper', 'report.js'), ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper glossary-card failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_glossary_card_renders_markdown():
    _run()


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP: node + jsdom not available')
    else:
        _run()
        print('PASS: paper glossary card renders Markdown/KaTeX')
