"""Extracted evidence preserves DOM text order and executable code whitespace."""

import re

import pytest

from tofu_search.fetch.html_extract import extract_html_text

pytestmark = pytest.mark.unit


def document(body):
    return (
        '<html><body><main><h1>Worker group reference</h1>'
        '<p>This guide describes how a worker group collects failures. '
        'All running workers settle before the group reports the result.</p>'
        + body + '</main></body></html>'
    )


def extract(body):
    # Test the real dependency through the package's extraction entry point;
    # no mock can certify that its tree transformations preserve source order.
    return extract_html_text(document(body), 0, 'https://docs.example.test/groups')


def body_without_markdown(text):
    text = text.split('--- Page Links ---')[0]
    text = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    return ' '.join(text.replace('`', '').split())


@pytest.mark.parametrize('tag', ['code', 'em', 'strong'])
def test_nested_link_text_stays_between_its_original_neighbors(tag):
    text = extract(
        '<p>Failures are combined in a '
        f'<a href="/errors#Group"><{tag}><span>FailureGroup</span></{tag}></a>'
        ' or '
        f'<a href="/errors#BaseGroup"><{tag}><span>BaseFailureGroup</span></{tag}></a>'
        ' before the caller resumes.</p>'
    )
    plain = body_without_markdown(text).replace('*', '')
    assert 'Failures are combined in a FailureGroup or BaseFailureGroup before the caller resumes.' in plain
    assert '[FailureGroup](https://docs.example.test/errors#Group)' in text
    assert '[BaseFailureGroup](https://docs.example.test/errors#BaseGroup)' in text


def test_code_block_keeps_indentation_and_decoded_operators():
    code = (
        'async def main():\n'
        '    async with WorkerGroup() as group:\n'
        '        if capacity < 4:\n'
        '            group.create_task(work())'
    )
    text = extract('<pre><code>' + code.replace('<', '&lt;') + '</code></pre>')
    assert code in text.split('--- Page Links ---')[0]


def test_brackets_in_linked_code_remain_the_original_identifier():
    text = extract(
        '<p>Read <a href="/items"><code>items[0]</code></a> to obtain the first '
        'value, then continue with the next worker in the group.</p>'
    )
    body = text.split('--- Page Links ---')[0]
    assert '`items[0]`' in body
    assert r'items\[0\]' not in body
    assert body.count('to obtain the first value') == 1


def test_multilingual_inline_text_retains_order_and_punctuation():
    text = extract(
        '<p>容量は<code>四</code>件です。容量为<code>四</code>项。'
        'El límite es <a href="/limits"><em>cuatro</em></a> tareas.</p>'
    )
    plain = body_without_markdown(text).replace('*', '')
    assert '容量は四件です。容量为四项。El límite es cuatro tareas.' in plain


def test_repeated_extractions_do_not_consume_each_others_evidence():
    body = (
        '<p>The controller waits for all workers to settle, even when one '
        'worker raises a failure. It then returns the collected failures.</p>'
    )
    first = extract(body)
    assert 'It then returns the collected failures.' in first
    for _ in range(8):
        assert extract(body) == first


def test_repeated_pages_keep_short_structured_values_and_formatting():
    body = (
        '<div><div>Service</div><div>Standard</div>'
        '<div>Delivery days</div><div>3–5</div></div>'
    )
    first = extract(body)
    assert 'Standard' in first and '3–5' in first
    for _ in range(8):
        assert extract(body) == first


def test_highlighted_code_inside_a_list_preserves_blank_lines_and_indents():
    code = (
        'async def main():\n'
        '    await step_one()\n'
        '\n'
        '    if capacity < 4:\n'
        '        await step_two()\n'
    )
    text = extract(
        '<ul><li><p>Await the first step before checking the remaining capacity '
        'and possibly scheduling the next operation.</p>'
        '<div class="highlight"><pre><span class="k">async</span>'
        + code[len('async'):].replace('<', '&lt;') + '</pre></div></li></ul>'
    )
    assert code.strip('\n') in text.split('--- Page Links ---')[0]


def test_code_recovery_preserves_literal_fences_and_does_not_repeat(monkeypatch):
    from tofu_search.fetch import html_extract

    code = 'sample = """```example\n    source\n```"""\nprint(sample)'
    collapsed = ' '.join(code.split())
    monkeypatch.setattr(html_extract.trafilatura, 'extract', lambda *a, **k: collapsed)
    text = extract('<pre>' + code + '</pre><pre>' + code + '</pre>'
                   '<pre hidden>private hidden example</pre>')
    assert text.count(code) == 1
    assert '````\n' + code + '\n````' in text
    assert 'private hidden example' not in text
