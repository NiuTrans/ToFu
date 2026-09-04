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

def test_core_checks_the_wall_before_returning_auth_text():
    """Source-level: the auth-replay result is content-judged, not truth-tested.

    Guarding at source level because driving the whole authenticated path
    offline would require faking Playwright; what must not regress is the
    ORDER — the wall check has to sit between the replay and its `return`.
    """
    import inspect

    from tofu_search.fetch import core
    src = inspect.getsource(core.fetch_page_content)

    replay = src.find('_try_authenticated_fetch(')
    assert replay > 0, 'the authenticated replay call vanished'
    wall_check = src.find('_looks_like_login_wall', replay)
    assert wall_check > replay, (
        'the replay result must be checked for a login wall')
    clean_return = src.find('return text', wall_check)
    assert clean_return > wall_check, (
        'the wall check must PRECEDE the clean-text return inside the _replay '
        'closure, otherwise the wall is returned as content and the browser '
        'escalation is unreachable')
    consumed = src.find('_replay()', clean_return)
    assert consumed > clean_return, (
        'the success path must consume the wall-judging _replay() closure, '
        'never the raw replay call')


def test_browser_is_tried_before_the_cookie_replay():
    """0.7.0: the live browser session is the FIRST identity for auth-source
    domains (native signing, same IP/fingerprint); the stored-cookie replay —
    the classic account risk-control trigger — is only the fallback. Pin the
    ORDER at source level."""
    import inspect

    from tofu_search.fetch import core
    src = inspect.getsource(core.fetch_page_content)
    browser_pos = src.find('auth_source_browser_first')
    replay_pos = src.find('_try_authenticated_fetch(')
    assert browser_pos > 0, 'the browser-first escalation for auth domains vanished'
    assert replay_pos > 0, 'the authenticated replay call vanished'
    assert browser_pos < replay_pos, (
        'the live browser session must be tried BEFORE the stored-cookie replay')


def test_unrescued_wall_returns_a_typed_diagnosis():
    """When the browser cannot take over, report WHY — never the wall itself."""
    import inspect

    from tofu_search.fetch import core
    src = inspect.getsource(core.fetch_page_content)
    assert 'auth_replay_rejected' in src
    # The diagnosis must name the actionable cause, not just "failed".
    assert 'extension' in src.lower(), (
        'the diagnosis should point at the browser extension / API path')
