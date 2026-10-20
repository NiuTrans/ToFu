"""ChatGPT share pages are exported from the flight payload, not the DOM.

The page chrome is the only server-rendered text: a link that is not public, or
a page whose payload moved, must fail loudly instead of exporting "Log in".
"""

import json

import pytest

from scripts import fetch_chatgpt_share as share

pytestmark = pytest.mark.unit

BODY = (
    '## 一、结论\n\n'
    '| 方法 | 作用 |\n|---|---|\n'
    '| JIT | 编译成程序 |\n\n'
    '来源见 [arxiv](https://arxiv.org/html/2605.01293v1?utm_source=chatgpt.com)。\n\n'
    '这一段足够长，用来确认表格、引号 "quoted" 和换行在导出后仍然完整保留。'
)

LONG_UNANCHORED = '这是一段没有任何 role 锚点的长正文。\n' * 12


def _page(*chunks):
    """Wrap flight chunks the way React Router emits them into the HTML."""
    scripts = []
    for chunk in chunks:
        literal = json.dumps(chunk, ensure_ascii=False)
        scripts.append(
            '<script>window.__reactRouterContext.streamController.enqueue('
            f'{literal})</script>')
    return ('<html><body>Log in<meta charset="utf-8">'
            + ''.join(scripts) + '</body></html>')


def _message(body, role='assistant', ref=1912):
    literal = json.dumps(body, ensure_ascii=False)
    return f'"content_type","parts",[{ref}],{literal},"role","{role}","name"'


def test_flight_payload_is_concatenated_in_page_order():
    html = _page('first-chunk', 'second-chunk')
    assert share.decode_flight(html) == 'first-chunksecond-chunk'


def test_message_body_and_role_come_from_the_parts_anchor():
    html = _page(_message('第一段回答。\n继续。', role='user', ref=7))
    assert share.extract_messages(share.decode_flight(html)) == [
        ('user', '第一段回答。\n继续。')]


def test_message_body_survives_table_pipes_quotes_and_newlines():
    html = _page(_message(BODY))
    [(role, body)] = share.extract_messages(share.decode_flight(html))
    assert role == 'assistant'
    assert body == BODY
    assert '| JIT | 编译成程序 |' in body
    assert '"quoted"' in body


def test_citation_widgets_and_tracking_params_are_stripped_from_export():
    body = _message(BODY.replace('来源见', '来源见\ue200genui\ue202{"refs":[]}\ue201'))
    markdown = share.render(share.extract_messages(body), 'https://chatgpt.com/s/abc')
    assert '\ue200' not in markdown
    assert 'utm_source' not in markdown
    assert 'https://arxiv.org/html/2605.01293v1' in markdown


def test_repeated_references_are_emitted_once():
    html = _page(_message(BODY), _message(BODY))
    assert share.extract_messages(share.decode_flight(html)) == [
        ('assistant', BODY)]


def test_unanchored_payload_falls_back_to_long_multiline_strings():
    html = _page('{"_1":"x"}',
                 json.dumps(LONG_UNANCHORED, ensure_ascii=False),
                 '"Log in"', '"Skip to content"')
    messages = share.extract_messages(share.decode_flight(html))
    assert messages == [('unknown', LONG_UNANCHORED)]


def test_page_chrome_alone_never_exports_a_transcript():
    html = _page('{"authStatus":"logged_out"}', 'Log in', 'Skip to content')
    assert share.extract_messages(share.decode_flight(html)) == []


def test_bare_share_id_is_expanded_to_a_public_url():
    assert share.normalize_url('t_6ad718e') == 'https://chatgpt.com/s/t_6ad718e'
    url = 'https://chatgpt.com/s/t_6ad718e'
    assert share.normalize_url(url) == url


def test_fetch_failure_exits_nonzero_without_writing(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise share.requests.RequestException('proxy timeout')

    monkeypatch.setattr(share.requests, 'get', boom)
    out = tmp_path / 'missing.md'
    assert share.main(['t_abc', '--out', str(out)]) == 1
    assert not out.exists()
    assert 'proxy timeout' in capsys.readouterr().err


def test_missing_payload_exits_with_decode_failure(monkeypatch, capsys):
    monkeypatch.setattr(share.requests, 'get', lambda *a, **k: _StubResponse(
        '<html><body>Log in</body></html>'))
    assert share.main(['t_abc']) == 1
    assert 'no React Router flight payload' in capsys.readouterr().err


class _StubResponse:
    text = ''

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None
