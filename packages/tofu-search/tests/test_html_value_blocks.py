"""Short HTML value blocks survive article extraction in source order."""

import re

import pytest

from tofu_search.fetch.html_extract import extract_html_text

pytestmark = pytest.mark.unit


def document(block):
    return (
        '<html><body><main><h1>Visitor information</h1>'
        '<p>The main building has two connected galleries. Visitors can see '
        'the permanent collection and temporary exhibitions with an ordinary '
        'admission ticket. Use the entrance on the east side if you have '
        'already booked a ticket online.</p>'
        + block
        + '<p>Visit notices: the following information applies to individual '
        'visitors. Groups should contact the admissions office before arriving '
        'and confirm the availability of guided visits for their party.</p>'
        '<p>Tickets include the permanent collection. Temporary exhibition '
        'availability may vary during installation periods. Please consult '
        'the exhibition calendar when planning your visit.</p>'
        '</main></body></html>'
    )


def values(*cells, advice=True):
    note = (
        '<div><p>This schedule applies to individual visitors. Group visits '
        'should be arranged with the admissions office before arrival.</p>'
        '<p>The entry ticket includes the permanent collections and current '
        'temporary exhibitions.</p></div>'
    )
    return ('<div class="value-grid">' + ''.join(f'<div>{x}</div>' for x in cells)
            + (note if advice else '') + '</div>')


def plain(text):
    return re.sub(r'\s+', ' ', text.split('--- Page Links ---')[0]).strip()


def test_div_values_stay_with_their_context_in_the_original_position():
    html = document('<h2>Ordinary admission</h2>' + values(
        'Free hours', 'Tuesday', 'Closed', 'Sunday', '10:00', '12:30', '14:30'))
    text = extract_html_text(html, 0, 'https://example.test/visit')
    assert 'Free hours Tuesday Closed Sunday 10:00 12:30 14:30' in plain(text)
    assert text.index('Ordinary admission') < text.index('Tuesday') < text.index('Visit notices')
    assert text.count('Tuesday') == 1


def test_short_values_do_not_need_english_words_or_a_site_class():
    html = document(values('通常開館', '月曜日', '休館', '日曜日', '09:30', '17:45'))
    text = extract_html_text(html, 0, 'https://example.test/visit')
    assert '通常開館 月曜日 休館 日曜日 09:30 17:45' in plain(text)


@pytest.mark.parametrize('advice', [False, True])
def test_nested_inline_values_keep_punctuation_and_relative_links(advice):
    html = document(values(
        'Capacity: <strong>42</strong> units.',
        'Version: <a href="../release"><code>items[0]</code></a>.',
        'Units are available individually.', advice=advice))
    text = extract_html_text(html, 0, 'https://example.test/guide/visit')
    assert 'Capacity: **42** units.' in text
    assert 'https://example.test/release' in text
    assert 'items[0]' in text and r'items\[0\]' not in text


@pytest.mark.parametrize('attribute', [
    'hidden', 'hidden="false"', 'aria-hidden="true"', 'style="display: none!important"',
])
def test_hidden_value_groups_are_excluded_without_losing_the_visible_tail(attribute):
    html = document(
        f'<div {attribute}>' + values('HIDDEN SENTINEL', '99:99') + '</div>'
        '<p>Visible qualification remains after the hidden group.</p>'
        + values('Open', '08:15', '18:45'))
    text = extract_html_text(html, 0, 'https://example.test/visit')
    assert 'HIDDEN SENTINEL' not in text and '99:99' not in text
    assert 'Visible qualification remains after the hidden group.' in text
    assert 'Open 08:15 18:45' in plain(text)


def test_hidden_inline_content_does_not_remove_visible_following_text():
    html = document('<p>Read <span hidden>HIDDEN SENTINEL</span>the public instructions.</p>')
    text = extract_html_text(html, 0, 'https://example.test/visit')
    assert 'HIDDEN SENTINEL' not in text
    assert 'Read the public instructions.' in plain(text)


def test_navigation_does_not_become_article_evidence():
    html = document(values('Desk A', '08:15', '18:45'))
    html = html.replace('<main>', '<nav>' + values('NAV SENTINEL', '99:99') + '</nav><main>')
    text = extract_html_text(html, 0, 'https://example.test/visit')
    assert 'NAV SENTINEL' not in text and '99:99' not in text
    assert 'Desk A 08:15 18:45' in plain(text)


def test_related_link_cards_do_not_become_article_evidence():
    cards = ''.join(
        f'<div><div><a href="/related/{index}">RELATED SENTINEL {index}</a></div>'
        f'<div>Separate recommended reading description {index}</div></div>'
        for index in range(3))
    html = document(values('Desk A', '08:15', '18:45') + '<div>' + cards + '</div>')
    text = extract_html_text(html, 0, 'https://example.test/visit')
    body = text.split('--- Page Links ---')[0]
    assert 'RELATED SENTINEL' not in body
    assert 'Separate recommended reading' not in body
    assert 'Desk A 08:15 18:45' in plain(text)
    assert 'https://example.test/related/0' in text


def test_block_content_inside_inline_wrappers_keeps_code_and_paragraph_order():
    code = 'if count < 3:\n    await next_item()'
    html = document(
        '<div><div><span><p>Read the procedure before starting this operation.</p>'
        '<pre>' + code.replace('<', '&lt;') + '</pre></span></div>'
        '<div><span><p>After the procedure finishes, inspect all collected '
        'items before leaving the room.</p></span></div></div>')
    text = extract_html_text(html, 0, 'https://example.test/visit')
    assert code in text
    assert text.index('Read the procedure') < text.index('After the procedure')
