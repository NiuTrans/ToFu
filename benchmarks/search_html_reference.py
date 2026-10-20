"""Offline word-overlap diagnostic for frozen HTML extraction outputs.

CLI: --corpus DIR --outputs DIR --out NEW_JSON. Corpus layout follows Mozilla
Readability's test-pages/<case>/{source,expected}.html; outputs are <case>.md.
No retrieval or model runs occur here. BS4 and MarkdownIt provide DOM/Markdown
parsing. This measures lexical overlap, not entailment or agent/search quality.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt


_BLOCK_BOUNDARIES = frozenset({
    'address', 'article', 'aside', 'blockquote', 'body', 'br', 'caption', 'dd',
    'details', 'div', 'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer',
    'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'li',
    'main', 'menu', 'nav', 'ol', 'p', 'pre', 'section', 'summary', 'table',
    'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul',
})


def html_text(markup):
    """Keep inline continuations; separate DOM blocks without inventing words.

    This is not browser layout: CSS, visibility attributes, generated content,
    image alt text and JavaScript are outside this lexical diagnostic.
    """
    soup = BeautifulSoup(markup, 'html.parser')
    for element in soup.find_all(['script', 'style']):
        element.decompose()
    for element in soup.find_all(_BLOCK_BOUNDARIES):
        element.insert_before(' ')
        element.insert_after(' ')
    return soup.get_text(separator='', strip=False)


def overlap(reference, markdown, renderer=None):
    """Score both sides through the same HTML text boundary."""
    renderer = renderer or MarkdownIt('commonmark', {'html': True})
    expected = Counter(re.findall(r'\w+', html_text(reference).casefold()))
    actual = Counter(re.findall(r'\w+', html_text(renderer.render(markdown)).casefold()))
    matched = sum((expected & actual).values())
    recall = matched / sum(expected.values()) if expected else float(not actual)
    precision = matched / sum(actual.values()) if actual else float(not expected)
    return {'reference_words': sum(expected.values()), 'output_words': sum(actual.values()),
            'overlap': matched, 'recall': recall, 'precision': precision,
            'f1': 2 * recall * precision / (recall + precision) if recall + precision else 0.0}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def measure(corpus, outputs):
    """Keep every reference case, including missing/unreadable output failures."""
    references = sorted((corpus / 'test-pages').glob('*/expected.html'))
    if not references:
        raise ValueError('No test-pages/*/expected.html references found')
    renderer, rows = MarkdownIt('commonmark', {'html': True}), []
    for reference_path in references:
        case = reference_path.parent.name
        reference = reference_path.read_bytes()
        source = (reference_path.parent / 'source.html').read_bytes()
        output_path = outputs / (case + '.md')
        raw, error = None, None
        try:
            raw = output_path.read_bytes()
            body = raw.decode('utf-8').split('--- Page Links ---')[0]
        except (OSError, UnicodeError) as exc:
            body, error = '', type(exc).__name__
        metrics = overlap(reference, body, renderer)
        if error:
            metrics.update(recall=0.0, precision=0.0, f1=0.0)
        rows.append({'case': case, 'source_sha256': sha256(source),
                     'expected_sha256': sha256(reference),
                     'output_sha256': sha256(raw) if raw is not None else None,
                     'error': error, **metrics})
    inputs = {}
    for name, path in [('corpus_manifest', corpus / 'manifest.json'),
                       ('extraction_report', outputs / 'report.json')]:
        inputs[name + '_sha256'] = sha256(path.read_bytes()) if path.exists() else None
    return {
        'metric_version': 2, 'benchmark_sha256': sha256(Path(__file__).read_bytes()),
        'corpus': str(corpus.resolve()), 'outputs': str(outputs.resolve()), **inputs,
        'versions': {name: version(name) for name in ('beautifulsoup4', 'markdown-it-py')},
        'definition': 'Multiset Unicode word overlap. DOM block boundaries separate text; '
                      'inline tags do not insert spaces. Markdown and reference HTML share '
                      'that rule. Excludes the Page Links inventory. No CSS/JavaScript '
                      'layout, CJK word segmentation, entailment or search-quality claim.',
        'cases': rows,
        'summary': {'cases': len(rows), 'errors': sum(r['error'] is not None for r in rows),
                    **{'macro_' + key: sum(r[key] for r in rows) / len(rows)
                       for key in ('f1', 'recall', 'precision')}},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--outputs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    report = measure(args.corpus, args.outputs)
    # Keep earlier measurements intact, including superseded metric versions.
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(report['summary']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
