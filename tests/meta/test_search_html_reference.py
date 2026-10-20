"""Reference scoring preserves inline words while separating block content."""

import json

import pytest

from benchmarks.search_html_reference import html_text, main, measure, overlap

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('markup', 'expected'), [
    ('<p>up<span>date</span> completed.</p>', 'update completed.'),
    ('<p>容量は<code>四</code>件です。</p>', '容量は四件です。'),
    ('<p>Read <a href="/x">it<code>ems[0]</code></a>.</p>', 'Read items[0].'),
    ('<div>First</div><div>second<br>third</div>', 'First second third'),
    ('<p>A</p><script>hidden()</script><style>.x{}</style><p>B</p>', 'A B'),
])
def test_reference_dom_boundaries_do_not_split_inline_words(markup, expected):
    assert ' '.join(html_text(markup).split()) == expected


def test_markup_changes_do_not_lose_words_or_add_link_destinations():
    result = overlap('<p>up<span>date</span>: <a href="/x">items[0]</a>.</p>',
                     'up*date*: [items[0]](https://example.test/x).')
    assert result['f1'] == result['recall'] == result['precision'] == 1.0
    assert overlap('<p>update now</p>', 'update')['recall'] == 0.5


def test_raw_html_and_code_use_the_same_text_rules():
    assert overlap('<pre>a&lt;b</pre>', '```\na<b\n```')['f1'] == 1.0
    assert overlap('<p>A<br>B</p>', 'A<br>B')['f1'] == 1.0


def fixture(corpus, case, expected):
    path = corpus / 'test-pages' / case
    path.mkdir(parents=True)
    (path / 'expected.html').write_text(expected)
    (path / 'source.html').write_text('<html>' + expected + '</html>')


def test_missing_outputs_stay_in_denominator_and_reports_are_not_overwritten(tmp_path):
    corpus, outputs = tmp_path / 'corpus', tmp_path / 'outputs'
    fixture(corpus, 'present', '<p>up<span>date</span> done</p>')
    fixture(corpus, 'missing', '<p>Second case</p>')
    outputs.mkdir()
    (outputs / 'present.md').write_text('update done\n--- Page Links ---\nUnrelated navigation')
    out = tmp_path / 'score.json'
    args = ['--corpus', str(corpus), '--outputs', str(outputs), '--out', str(out)]
    assert main(args) == 0
    report = json.loads(out.read_text())
    assert report['summary'] == {'cases': 2, 'errors': 1, 'macro_f1': 0.5,
                                 'macro_recall': 0.5, 'macro_precision': 0.5}
    missing, present = report['cases']
    assert missing['error'] == 'FileNotFoundError'
    assert present['error'] is None and len(present['output_sha256']) == 64
    before = out.read_bytes()
    with pytest.raises(FileExistsError):
        main(args)
    assert out.read_bytes() == before


def test_bad_reference_corpus_cannot_silently_drop_cases(tmp_path):
    with pytest.raises(ValueError, match='No test-pages'):
        measure(tmp_path, tmp_path)
    fixture(tmp_path, 'incomplete', '<p>Fact</p>')
    (tmp_path / 'test-pages/incomplete/source.html').unlink()
    with pytest.raises(FileNotFoundError):
        measure(tmp_path, tmp_path)
