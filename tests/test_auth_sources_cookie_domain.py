"""tests/test_auth_sources_cookie_domain.py — cookie scope must survive the store.

``cookies_from_fields`` / ``parse_cookie_header`` used to stamp EVERY cookie
with ``'.' + domain``. That silently broke host-only cookies: a session cookie
issued for ``your-llm-gateway.example.com`` (no leading dot) was rewritten to
``.internal.example.com``, the browser treated it as a DIFFERENT cookie, the site's auth
probe (``/sso/web/auth``) never received it and answered 401, and the
authenticated fetch landed on the SSO login wall — while the store still
reported the source as "connected".

The measured contrast that pins this: injecting the same 9 cookies with their
devtools-reported scopes gives ``/sso/web/auth`` → 200 and no redirect; the same
9 cookies flattened to ``.internal.example.com`` give 401 + redirect to the login page.

Invariant: THE SCOPE THE CALLER SUPPLIED IS THE SCOPE THAT GETS STORED. Nothing
in the store may widen (or narrow) a cookie's domain behind the caller's back.
"""

import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


# Referenced ONLY through this constant: export.py rewrites the literal
# 'https://api.openai.com/...' endpoint form and the bare 'your-llm-gateway.example.com'
# host form DIFFERENTLY, so a URL built from the constant stays consistent
# with the registered source domain in both the source and the exported tree.
_HOST = 'your-llm-gateway.example.com'


@pytest.fixture(autouse=True)
def _isolated_store():
    import lib.auth_sources as A
    prev = A._STORE_PATH
    A._STORE_PATH = os.path.join(tempfile.mkdtemp(), 'auth_sources.json')
    A.invalidate_cache()
    yield A
    A._STORE_PATH = prev
    A.invalidate_cache()


# ── The regression: host-only must stay host-only ──

def test_bare_value_defaults_to_host_only(_isolated_store):
    """No leading dot. Host-only is the narrower, safer default."""
    A = _isolated_store
    cookies = A.cookies_from_fields({'sid': 'V'}, 'your-llm-gateway.example.com')
    assert len(cookies) == 1
    assert cookies[0]['domain'] == 'your-llm-gateway.example.com', (
        'a bare value must NOT be widened to a parent domain')
    assert not cookies[0]['domain'].startswith('.')


def test_explicit_host_only_domain_is_preserved_verbatim(_isolated_store):
    A = _isolated_store
    cookies = A.cookies_from_fields(
        {'12d702aa62_ssoid': {'value': 'V', 'domain': 'your-llm-gateway.example.com'}},
        'example-corp.com')
    assert cookies[0]['domain'] == 'your-llm-gateway.example.com'


def test_explicit_parent_domain_is_preserved_verbatim(_isolated_store):
    """The reverse case: an explicitly parent-scoped cookie keeps its dot."""
    A = _isolated_store
    cookies = A.cookies_from_fields(
        {'venusToken': {'value': 'V', 'domain': '.internal.example.com'}},
        'example-corp.com')
    assert cookies[0]['domain'] == '.internal.example.com'


def test_mixed_scopes_each_keep_their_own(_isolated_store):
    """The real payload shape: host-only session + parent-domain token."""
    A = _isolated_store
    cookies = A.cookies_from_fields({
        '12d702aa62_ssoid': {'value': 'A', 'domain': 'your-llm-gateway.example.com'},
        'venusToken': {'value': 'B', 'domain': '.internal.example.com'},
    }, 'example-corp.com')
    got = {c['name']: c['domain'] for c in cookies}
    assert got == {'12d702aa62_ssoid': 'your-llm-gateway.example.com',
                   'venusToken': '.internal.example.com'}


# ── Round-trip through the store ──

def test_stored_domain_survives_match_source_verbatim(_isolated_store):
    """THE end-to-end invariant: what goes in is what comes back out."""
    A = _isolated_store
    A.upsert_source(_HOST, enabled=True, cookie_fields={
        '12d702aa62_ssoid': {'value': 'A', 'domain': 'your-llm-gateway.example.com'},
        'venusToken': {'value': 'B', 'domain': '.internal.example.com'},
    })
    A.invalidate_cache()
    src = A.match_source(f'https://{_HOST}/ml/modelPlaza/modelInfo')
    assert src is not None
    got = {c['name']: c['domain'] for c in src['cookies']}
    assert got['12d702aa62_ssoid'] == 'your-llm-gateway.example.com', (
        'store rewrote a host-only cookie to a parent domain')
    assert got['venusToken'] == '.internal.example.com'


def test_explicit_cookie_list_is_not_rewritten(_isolated_store):
    """Passing `cookies=` directly must also be left alone."""
    A = _isolated_store
    A.upsert_source(_HOST, enabled=True, cookies=[
        {'name': 'sid', 'value': 'A', 'domain': 'your-llm-gateway.example.com', 'path': '/'},
    ])
    A.invalidate_cache()
    src = A.match_source(f'https://{_HOST}/x')
    assert src['cookies'][0]['domain'] == 'your-llm-gateway.example.com'


# ── parse_cookie_header gets the same treatment ──

def test_cookie_header_is_host_only(_isolated_store):
    """A `Cookie:` header carries NO scope, so inventing a parent dot is wrong."""
    A = _isolated_store
    cookies = A.parse_cookie_header('a=1; b=2', 'your-llm-gateway.example.com')
    assert {c['domain'] for c in cookies} == {'your-llm-gateway.example.com'}
    assert all(not c['domain'].startswith('.') for c in cookies)


def test_cookie_header_values_still_parsed(_isolated_store):
    A = _isolated_store
    cookies = A.parse_cookie_header('a=1; b=2', 'x.example.com')
    assert {c['name']: c['value'] for c in cookies} == {'a': '1', 'b': '2'}


# ── Per-cookie extras ──

def test_path_and_secure_overrides_are_honoured(_isolated_store):
    A = _isolated_store
    cookies = A.cookies_from_fields(
        {'sid': {'value': 'V', 'domain': 'h.example.com',
                 'path': '/app', 'secure': True}},
        'example.com')
    c = cookies[0]
    assert c['path'] == '/app'
    assert c['secure'] is True


def test_blank_values_are_still_dropped(_isolated_store):
    """Dict form must not resurrect the empty-value entry."""
    A = _isolated_store
    cookies = A.cookies_from_fields(
        {'a': {'value': '   ', 'domain': 'h.example.com'}, 'b': ''},
        'example.com')
    assert cookies == []


def test_match_source_still_matches_a_host_only_cookie(_isolated_store):
    """Scope fidelity must not cost us the match: the source still resolves."""
    A = _isolated_store
    A.upsert_source(_HOST, enabled=True, cookie_fields={
        'sid': {'value': 'A', 'domain': 'your-llm-gateway.example.com'}})
    A.invalidate_cache()
    assert A.match_source(f'https://{_HOST}/x') is not None
    assert A.match_source('https://other.example.com/x') is None
