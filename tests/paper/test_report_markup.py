"""Report math survives Markdown without creating markup or altering code."""

from html.parser import HTMLParser

import pytest

from lib.paper.report_markup import render_report_markdown


pytestmark = pytest.mark.unit


class ParsedReport(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.tags: list[str] = []
        self.text: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize('formula', [
    '$a_i^2 + f^*$', r'\(a_i + f^*\)',
    '$$\na_i^2 + f^*\n$$', '\\[\na_i + f^*\n\\]',
])
def test_math_delimiters_and_content_survive_emphasis_parsing(formula):
    parsed = ParsedReport(render_report_markdown(f'**Result**\n\n{formula}'))
    assert formula in ''.join(parsed.text)
    assert 'strong' in parsed.tags
    assert 'em' not in parsed.tags


def test_formula_markup_is_text_and_not_an_html_element():
    formula = '$x < y + <script>alert("x")</script> & z$'
    parsed = ParsedReport(render_report_markdown(formula))
    assert ''.join(parsed.text) == formula
    assert 'script' not in parsed.tags


def test_fenced_and_inline_code_preserve_literal_math_and_html():
    literal = '$a_i < b_j$'
    rendered = render_report_markdown(f'`{literal}`\n\n```tex\n{literal}\n```')
    parsed = ParsedReport(rendered)
    assert parsed.tags.count('code') == 2
    assert ''.join(parsed.text).count(literal) == 2
    assert 'em' not in parsed.tags


def test_table_currency_does_not_merge_cells_and_math_survives():
    rendered = render_report_markdown(
        '| Cost | Math |\n| --- | --- |\n| $5 | $a_i$ |',
    )
    parsed = ParsedReport(rendered)
    assert parsed.tags.count('td') == 2
    assert '$5' in parsed.text
    assert '$a_i$' in parsed.text


def test_user_placeholder_text_and_report_attributes_are_preserved():
    rendered = render_report_markdown(
        'MATH0 \x02MATH1\x03 $a_i$\n\n### Heading {#report-heading}\n\n'
        '<aside>Existing report HTML</aside>',
    )
    assert 'MATH0 MATH1 $a_i$' in rendered
    assert '<h3 id="report-heading">Heading</h3>' in rendered
    assert '<aside>Existing report HTML</aside>' in rendered
