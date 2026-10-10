"""Live `tools/call` tests: do the registered tools actually WORK?

★ Distinct from test_mcp_server_smoke.py, which proves the tools are correctly
DECLARED (tools/list). Declaration passing says nothing about invocation: a
handler can advertise a perfect schema and still call its underlying function
with the wrong arguments. That exact bug shipped here -- ``search_vertical``'s
auto path passed the query where the dispatcher expects a registered TYPE name,
so every auto lookup silently matched no handler and fell through to the
"nothing matched" reply. tools/list was green throughout.

These run IN-PROCESS against the MCPServer object rather than over a subprocess,
because a tool result has to be inspected as structured data, and because the
HTTP seam must be patched -- which is impossible across a process boundary.
Network-touching tools (web_search, fetch_page) are exercised with their
underlying pipeline patched, so nothing here reaches the open internet.
"""

from __future__ import annotations

import json

import anyio
import pytest

pytest.importorskip('mcp', reason='MCP SDK not installed (pip install "tofu-search[mcp]")')

from tofu_search.mcp_server.server import build_server  # noqa: E402
from tofu_search.search.vertical import base as vertical_base  # noqa: E402


@pytest.fixture(scope='module')
def server():
    return build_server()


def _sync_call(server, name: str, args: dict):
    """Invoke a tool the way a client does, returning (text, structured).

    Driven with anyio.run() rather than an async test plugin: the suite runs
    under PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, so pytest-asyncio / anyio's plugin
    are not loaded and an `async def test_` would be silently skipped -- a
    passing-but-useless test, which is worse than no test.
    """
    return anyio.run(_acall, server, name, args)


async def _acall(server, name: str, args: dict):
    result = await server.call_tool(name, args)
    # MCP SDK v2 returns CallToolResult.  Keep the older shapes in this tiny
    # normaliser so assertion failures stay readable when bisecting across the
    # SDK migration.
    if hasattr(result, 'content'):
        blocks = result.content
        structured = getattr(result, 'structured_content', None)
        text = '\n'.join(getattr(b, 'text', '') or '' for b in blocks)
        return text, structured
    if isinstance(result, tuple):
        blocks, structured = result
    else:
        blocks, structured = result, None
    text = '\n'.join(getattr(b, 'text', '') or '' for b in blocks)
    return text, structured


# ── search_vertical: the auto path must actually dispatch ────────────────────

def test_auto_path_dispatches_a_recognised_identifier(server, monkeypatch):
    """A CVE ID must reach the cve handler.

    ★ THE REGRESSION TEST for the bug described in this module's docstring. It
    fails loudly if the auto path ever again passes the raw query where a TYPE
    name belongs, because then no handler matches and the tool returns its
    "nothing matched" text instead of the record.
    """
    captured = {}

    def fake_get(url, **kw):
        captured['url'] = url

        class _Resp:
            ok = True
            status_code = 200

            @staticmethod
            def json():
                return {'vulnerabilities': [{'cve': {
                    'id': 'CVE-2021-44228',
                    'descriptions': [{'lang': 'en', 'value': 'Log4Shell.'}],
                    'metrics': {}, 'references': [],
                }}]}

            text = ''
        return _Resp()

    monkeypatch.setattr(vertical_base, 'http_get', fake_get)

    text, _ = _sync_call(server, 'search_vertical',
                          {'query': 'CVE-2021-44228', 'domain': 'auto'})

    assert 'No authoritative source matched' not in text, (
        'the auto path failed to dispatch a valid CVE ID -- it is passing the '
        f'query where a registered type name belongs. Got: {text[:300]!r}')
    assert 'CVE-2021-44228' in text
    assert captured.get('url'), 'no HTTP call was made, so no handler ran'


def test_unrecognised_query_returns_guidance_not_an_error(server):
    """An unmatched query must teach the model what the result means.

    "Nothing matched" and "this subject does not exist" are different claims,
    and conflating them is how a model ends up telling a user something is not
    real when the tool simply had no identifier to work with.
    """
    text, _ = _sync_call(
        server, 'search_vertical',
        {'query': 'some entirely unstructured musing about nothing', 'domain': 'auto'})

    assert 'does NOT mean' in text, (
        f'the miss reply must disclaim non-existence; got {text[:300]!r}')
    assert 'web_search' in text, 'the miss reply should redirect to web_search'


# ── verify_citations: structured output must survive serialisation ───────────

_FAKE_BIB = """\
@article{ghost2021,
  title   = {A Paper That Does Not Exist},
  author  = {Nobody, A.},
  year    = {2021},
  doi     = {10.9999/definitely-not-a-real-doi},
}
"""


def test_fabricated_doi_is_reported_suspicious(server, monkeypatch):
    """A DOI that 404s on CrossRef is the one case that may be called out.

    Also pins the response SHAPE, because the tool returns a dict and the SDK
    serialises it: nested evidence/citation dicts have to survive that round
    trip, and `summary` must stay a tally (summarize() also returns the full
    suspicious records, which would duplicate every finding already in
    `results`).
    """
    def fake_get(url, **kw):
        class _Resp:
            ok = False
            status_code = 404
            text = ''

            @staticmethod
            def json():
                return {}
        return _Resp()

    monkeypatch.setattr(vertical_base, 'http_get', fake_get)

    text, structured = _sync_call(server, 'verify_citations',
                                   {'text': _FAKE_BIB, 'format': 'bibtex'})

    payload = structured if isinstance(structured, dict) else json.loads(text)
    if 'result' in payload and 'summary' not in payload:
        payload = payload['result']

    summary = payload['summary']
    assert set(summary['counts']) == {'verified', 'suspicious', 'unverifiable'}
    assert summary['counts']['suspicious'] == 1, payload
    assert summary['has_suspicious'] is True
    assert 'suspicious' not in summary, (
        'summary must stay a tally -- the full records already live in results')
    assert payload['parsed_format'] == 'bibtex'

    entry = payload['results'][0]
    assert entry['state'] == 'suspicious'
    assert entry['evidence'].get('checked'), 'evidence must name the URL checked'
    assert entry['citation']['doi'] == '10.9999/definitely-not-a-real-doi'


def test_unresolvable_title_is_unverifiable_not_suspicious(server, monkeypatch):
    """The anti-false-positive rule the tool description promises the model.

    A title-only claim that no catalogue confirms is a COVERAGE GAP. If this
    ever returns `suspicious`, the description becomes a lie and the model will
    accuse a user of fabricating a real paper.
    """
    def fake_get(url, **kw):
        class _Resp:
            ok = True
            status_code = 200
            text = '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

            @staticmethod
            def json():
                return {'message': {'items': []}, 'data': []}
        return _Resp()

    monkeypatch.setattr(vertical_base, 'http_get', fake_get)

    text, structured = _sync_call(
        server, 'verify_citations',
        {'text': 'Nobody, A. (2021). A Paper With No Identifier At All.',
         'format': 'references'})

    payload = structured if isinstance(structured, dict) else json.loads(text)
    if 'result' in payload and 'summary' not in payload:
        payload = payload['result']

    assert payload['summary']['counts']['suspicious'] == 0, (
        'a title-only miss was reported as suspicious; the tool description '
        f'promises it degrades to unverifiable. Payload: {payload}')


# ── web_search / fetch_page: no network, just the wiring ─────────────────────

def test_web_search_passes_diagnostics_through_on_zero_results(server, monkeypatch):
    """The zero-result path must forward the pipeline's diagnostics.

    perform_web_search attaches `_search_diag` ONLY when it found nothing --
    exactly when the model most needs to know whether the cause was "no
    matches" or "every engine had a network error". Dropping it leaves the
    model with a bare "No search results found."
    """
    from tofu_search.mcp_server import server as server_mod
    from tofu_search.search.orchestrator import SearchResultList

    empty = SearchResultList()
    empty._search_diag = {'reason': 'network_error', 'reason_detail': 'all engines down'}
    monkeypatch.setattr(server_mod, 'perform_web_search', lambda *a, **kw: empty)

    text, _ = _sync_call(server, 'web_search', {'query': 'anything'})

    assert 'network error' in text.lower(), (
        f'_search_diag was not forwarded to the formatter; got {text[:300]!r}')


def test_web_search_applies_shared_context_budget(server, monkeypatch):
    from tofu_search.mcp_server import server as server_mod
    from tofu_search.search.orchestrator import SearchResultList

    rows = SearchResultList([
        {'title': f'Source {i}', 'url': f'https://s{i}.example/a',
         'source': 'test', 'snippet': 'quasar benchmark',
         'full_content': 'quasar benchmark fact ' * 4_000}
        for i in range(3)
    ])
    monkeypatch.setattr(server_mod, 'perform_web_search', lambda *a, **kw: rows)

    text, _ = _sync_call(server, 'web_search', {
        'query': 'quasar benchmark', 'content_budget_chars': 4_000})

    assert text.count('URL:') == 3
    assert len(text) < 7_000
    assert 'Query-Focused Excerpts' in text


def test_fetch_page_failure_is_explained_not_silent(server, monkeypatch):
    """An unreadable page must not look like an empty page."""
    from tofu_search.mcp_server import server as server_mod

    monkeypatch.setattr(server_mod, 'fetch_page_content', lambda *a, **kw: None)

    text, _ = _sync_call(server, 'fetch_page', {'url': 'https://example.com/gone'})

    assert 'Could not read' in text
    assert 'says nothing about whether the information exists' in text
