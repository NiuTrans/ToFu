"""tests/test_auth_replay_login_wall.py — a login wall is not a successful fetch.

``fetch_page_content`` used to accept ANY non-empty text from the authenticated
replay:

    auth_text = _try_authenticated_fetch(...)
    if auth_text:
        return auth_text        # ← an SSO login wall lands here

An SSO login wall is a COMPLETE page (the observed one extracts to ~1900 chars,
mostly inlined bootstrap JS), so "non-empty" never distinguished it from real
content. Two consequences, both measured:

  * the caller received the wall AS IF it were the article, and
  * the browser-escalation block right below that ``return`` was UNREACHABLE —
    the very fallback meant to rescue this case could never run.

Root cause shape: judging success by an easy proxy (non-empty) instead of the
real condition (is this the content we asked for). The fix judges the CONTENT
and, when it is a wall, escalates to the host browser; if the browser cannot
take over, it returns a typed diagnosis instead of the wall.

Why cookie re-pasting cannot fix such a site (measured on your-llm-gateway.example.com):
its auth probe echoes back the ssoid it received, and it echoed EMPTY for every
transport we tried — four cookie namings, host-only and parent scope,
sameSite=None, and a hand-built ``Cookie:`` header. The devtools-visible stored
value is not the value the page transmits (its JS re-assembles the ticket), so
the browser extension (or the site's API) is the only viable path.
"""

from types import SimpleNamespace

import pytest

from tofu_search.fetch.utils import _looks_like_login_wall

pytestmark = pytest.mark.unit

# The real extraction of the observed SSO wall (head of it, padded to length).
REAL_WALL = (
    "QR Code LoginEnglishLog inNextForgot PasswordFeedback!(function (win, doc) {"
    "  if(win.URLSearchParams && win.location.search){"
    "    var sInfo = new URLSearchParams(location.search);"
    "    win.__client_id = sInfo.get('client_id') || '';"
    "    win.__hmis = sInfo.get('hmis') || localStorage.getItem('_username') || '';"
    "  } win.owl && win.owl('start', { project: 'com.example-corp.sso.auth.fe',"
) + 'x' * 1200

SHORT_WALL = '简体中文 登录您的账号 下一步 忘记密码 问题反馈'


# ── The wall detector ──

def test_detects_the_real_long_login_wall():
    """REGRESSION: the observed wall is ~1900 chars, NOT short.

    A length-only heuristic (`<= 600 chars`) missed it, which is why the first
    version of this gate passed the wall through.
    """
    assert _looks_like_login_wall(REAL_WALL) is True


def test_detects_a_short_login_page():
    assert _looks_like_login_wall(SHORT_WALL) is True


def test_real_content_is_not_a_wall():
    text = '模型广场 文本生成 LongCat DeepSeek Qwen ' + '真实模型描述条目 ' * 400
    assert _looks_like_login_wall(text) is False


def test_long_article_mentioning_a_login_phrase_is_not_a_wall():
    """A single incidental phrase must not condemn a long page."""
    text = '本文讨论了忘记密码时的账号恢复流程设计与权衡。' + '正文段落 ' * 300
    assert _looks_like_login_wall(text) is False


def test_very_long_text_is_never_a_wall():
    text = ('忘记密码 二维码登录 登录您的账号 ' * 50) + 'x' * 5000
    assert _looks_like_login_wall(text) is False, (
        'past 4000 chars the page is real content whatever words it contains')


def test_empty_is_not_a_wall():
    assert _looks_like_login_wall('') is False
    assert _looks_like_login_wall(None) is False


# ── The seam: the wall must not be returned as success ──

@pytest.fixture
def auth_path(monkeypatch):
    """Exercise the public pipeline with only external identities stubbed."""
    from tofu_search.fetch import core, utils

    calls = []
    source = {'domain': 'example.com', 'cookies': [{'name': 'session', 'value': 'test'}]}
    responses = {'browser': None, 'replay': REAL_WALL}

    def browser(*_args, **kwargs):
        calls.append(('browser', kwargs['reason']))
        return responses['browser']

    def replay(*_args, **_kwargs):
        calls.append(('replay', None))
        return responses['replay']

    def anonymous(*_args, **_kwargs):
        calls.append(('anonymous', None))
        raise AssertionError('An unrescued login wall must stop anonymous fetching')

    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_get_reader', lambda _url: None)
    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: SimpleNamespace(
        match_source=lambda _url: source))
    monkeypatch.setattr(core, '_try_browser_fetch', browser)
    monkeypatch.setattr(core, '_try_authenticated_fetch', replay)
    monkeypatch.setattr(core, '_do_request', anonymous)
    return core.fetch_page_content, source, responses, calls


@pytest.mark.parametrize('query', [None, 'quasar throughput'])
def test_core_checks_the_wall_before_returning_auth_text(auth_path, query):
    fetch, source, responses, calls = auth_path
    source['access_strategy'] = 'cookies_replay'
    responses['browser'] = 'Quasar throughput is 9137 records per second.'
    assert fetch('https://example.com/manual', max_chars=500, query=query) == responses['browser']
    assert calls == [('replay', None), ('browser', 'auth_source_browser_fallback')]


@pytest.mark.parametrize('query', [None, 'quasar throughput'])
def test_browser_is_tried_before_the_cookie_replay(auth_path, query):
    fetch, _source, responses, calls = auth_path
    responses['browser'] = 'Quasar throughput is 9137 records per second.'
    assert fetch('https://example.com/manual', max_chars=500, query=query) == responses['browser']
    assert calls == [('browser', 'auth_source_browser_first')]


@pytest.mark.parametrize('query', [None, 'quasar throughput'])
def test_unrescued_wall_returns_a_typed_diagnosis(auth_path, query):
    fetch, _source, _responses, calls = auth_path
    diagnostics = {}
    assert fetch('https://example.com/manual', max_chars=500,
                 query=query, diag=diagnostics) is None
    assert diagnostics['reason'] == 'auth_replay_rejected'
    assert 'extension' in diagnostics['detail'].lower()
    assert calls == [('browser', 'auth_source_browser_first'), ('replay', None)]
