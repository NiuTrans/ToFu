"""Hermetic end-to-end smoke test — real app + real browser + STUB LLM.

WHY
---
Every other frontend test runs the JS under jsdom (no real bundle load, no
CSS, no real event loop, no Hypercorn). The recurring bug class in this
project's JOURNAL — "click does nothing", "panel done but tool spinning", the
welcome-page sidebar collapse — were all INTEGRATION failures that unit
harnesses structurally cannot see. This test closes that gap: it boots the
REAL Quart/Hypercorn app on an ephemeral port (``live_server`` fixture),
drives a REAL headless Chromium to it (``page`` fixture), sends a message, and
asserts the production render pipeline end-to-end.

HERMETIC (no API key / no network)
----------------------------------
The chat streaming path is ``stream_llm_response → dispatch_stream →
stream_chat`` (the orchestrator's ``StreamingToolAccumulator`` only supplies
the ``on_tool_call_ready`` callback — the LLM call still funnels through
``stream_llm_response``). We replace ``stream_llm_response`` with a
deterministic stub that emits DELTA events through the task's OWN event system
and returns ``(content, finish_reason, usage)`` — so the genuine SSE→frontend
render path runs UNCHANGED; only the upstream token source is faked. The
``web_search`` execution seam (``_web_search_one``) is likewise stubbed so the
tool-round test needs no network.

The patch is installed at SESSION scope (autouse) BEFORE the session-scoped
``live_server`` ever handles a request, and a module-level SENTINEL counts stub
invocations. Each chat test asserts the sentinel advanced — so if the patch
ever misses and a REAL model streams, the test FAILS LOUDLY instead of
silently passing on real output.

WHAT IT PROVES THAT NOTHING ELSE DOES
-------------------------------------
1. ``window.renderToolRoundsHTML`` is DEFINED in the browser → the js_bundler
   allowlist (§3.2.1) wired tool_rounds.js into the served bundle end-to-end.
2. A sent message streams and finalizes into a real assistant message.
3. A web_search tool round renders a ``.ptool-panel`` in the live DOM — the
   SSE tool_start/tool_result → tool_rounds.js render path.

Requires Playwright + a launchable Chromium (the ``browser`` fixture
self-bootstraps LD_LIBRARY_PATH for rootless conda libs and skips with a
concrete reason only when launch genuinely can't happen on the host).
"""
from __future__ import annotations

import json
import time

import pytest

# visual ONLY: the slow leg has no chromium, so these tests always skip
# there — but the session-scoped autouse _install_llm_stubs fixture still
# installs its LLM stubs for the WHOLE session, and worksteal then lands
# suites that need their OWN LLM mock (test_endpoint_messages) on the
# polluted worker: their calls hit this stub and the recorder sees 0
# (d0d473d slow leg). The e2e leg selects -m visual, unchanged.
pytestmark = [pytest.mark.visual]

_STREAM_TEXT = "Hello from the stubbed model. This is a deterministic reply."

# Module-level invocation counters — the loud guard against a patch-miss.
# If a chat runs and these stayed at 0, a REAL model streamed → test fails.
_SENTINEL = {'stream_calls': 0, 'search_calls': 0}


@pytest.fixture(autouse=True)
def _disable_open_mode_rate_limit(monkeypatch):
    """Keep browser journeys isolated from the process-wide RPM bucket.

    A real page boot performs several legitimate API requests.  Reusing the
    session-scoped live server across the visual suite must not make later
    journeys depend on how many earlier pages were opened.
    """
    from lib.rate_limit_store import reset_for_test

    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '0')
    reset_for_test()
    yield
    reset_for_test()


def _stub_stream_llm_response_factory():
    """Build the stub bound to the concrete event owner."""
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager._events import append_event

    def _stub(task, body, tag='', on_tool_call_ready=None):
        # NOTE: the real stream_llm_response returns ``msg`` as a DICT
        # (msg['content'] / msg['reasoning_content'] / msg['tool_calls']),
        # NOT a string — _llm_call_with_fallback unpacks it as a dict. Match
        # that exactly or the orchestrator mis-handles the turn.
        _SENTINEL['stream_calls'] += 1
        msgs = body.get('messages', []) if isinstance(body, dict) else []
        last = ''
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get('role') == 'user':
                c = m.get('content')
                last = c if isinstance(c, str) else json.dumps(c)
                break

        # Tool scenario: emit ONE web_search tool call on the first call (both
        # in the returned msg dict AND via on_tool_call_ready for the
        # streaming pre-exec); the orchestrator executes it (rendering the
        # panel) and re-calls this stub, which then streams the closing text
        # (guarded by a task flag).
        if '__e2e_tool__' in last and not task.get('_e2e_tool_done'):
            task['_e2e_tool_done'] = True
            tc = {
                'id': 'call_e2e_1', 'index': 0, 'type': 'function',
                'function': {'name': 'web_search',
                             'arguments': json.dumps({'query': 'e2e query'})},
            }
            if on_tool_call_ready:
                try:
                    on_tool_call_ready(tc)
                except Exception:
                    pass
            return ({'role': 'assistant', 'content': '', 'tool_calls': [tc]},
                    'tool_calls',
                    {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})

        # Slow-stream scenario for the ABORT journey: 60 words at 50ms each,
        # checking task['aborted'] every word — a mid-stream abort click lands
        # deterministically and the loop honours the same abort flag the real
        # stream checks.
        if '__e2e_slow__' in last and not task.get('_e2e_slow_done'):
            task['_e2e_slow_done'] = True
            words = [f'slow{i:02d}' for i in range(60)]
            for w in words:
                if task.get('aborted'):
                    break
                cd = w + ' '
                with task['content_lock']:
                    task['content'] += cd
                append_event(task, build_event(EventType.DELTA, content=cd))
                time.sleep(0.05)
            return ({'role': 'assistant', 'content': task['content'], 'tool_calls': []},
                    'stop',
                    {'prompt_tokens': 10, 'completion_tokens': len(words),
                     'total_tokens': 10 + len(words)})

        # Plain text: stream word-by-word as real DELTA events, then return
        # the full content in the msg dict.
        words = _STREAM_TEXT.split()
        for i, w in enumerate(words):
            if task.get('aborted'):
                break
            cd = w + (' ' if i < len(words) - 1 else '')
            with task['content_lock']:
                task['content'] += cd
            append_event(task, build_event(EventType.DELTA, content=cd))
        return ({'role': 'assistant', 'content': _STREAM_TEXT, 'tool_calls': []},
                'stop',
                {'prompt_tokens': 20, 'completion_tokens': len(words),
                 'total_tokens': 20 + len(words)})

    return _stub


def _stub_perform_web_search(query, user_question='', freshness='', **kwargs):
    """Deterministic offline web search — replaces tofu_search.perform_web_search
    at the search primitive owner's dependency binding. Returns a plain list
    of result dicts (the real function returns a list-like with optional
    _search_diag / _engine_breakdown attrs, which the handler reads via
    getattr → None here is fine)."""
    _SENTINEL['search_calls'] += 1
    return [{
        'title': 'E2E stub result',
        'snippet': 'A deterministic search result for the E2E smoke test.',
        'url': 'https://example.invalid/e2e',
        'source': 'stub',
    }]


@pytest.fixture(scope='module', autouse=True)
def _install_llm_stubs():
    """Module-scoped stubs shared by this file's live-server task threads.

    Uses plain setattr because the server task threads share ``sys.modules``.
    Module scope is the authority boundary: session scope leaked these stubs
    into unrelated tests later scheduled on the same xdist worker.
    """
    import tofu_search
    import lib.tasks_pkg.llm_fallback._call as llm_fb
    import lib.tasks_pkg.handlers.search._core as search_core

    # The root loop delegates every primary round to _call, which owns the
    # streaming dependency binding. Patch that single explicit seam.
    #
    # Every tool execution path delegates to search_core._web_search_one. The
    # production-research API resolves tofu_search at call time, so retain the
    # source patch as a second, distinct dependency seam.
    saved = {
        'llm_fb': llm_fb.stream_llm_response,
        'search_core': search_core.perform_web_search,
        'tofu': getattr(tofu_search, 'perform_web_search', None),
    }
    stub = _stub_stream_llm_response_factory()
    llm_fb.stream_llm_response = stub
    tofu_search.perform_web_search = _stub_perform_web_search
    search_core.perform_web_search = _stub_perform_web_search
    try:
        yield
    finally:
        llm_fb.stream_llm_response = saved['llm_fb']
        search_core.perform_web_search = saved['search_core']
        if saved['tofu'] is not None:
            tofu_search.perform_web_search = saved['tofu']


def _wait_app_ready(page, timeout=15000):
    page.wait_for_selector('#userInput', state='visible', timeout=timeout)
    page.wait_for_function(
        "window.TofuModules && window.TofuModules.version === 3",
        timeout=timeout)


def test_bundle_loaded_in_browser(page, assert_no_js_errors):
    """The served Vite entry reaches app-ready with the two allowed bridges."""
    _wait_app_ready(page)
    public = page.evaluate("""() => ({
        version: window.TofuModules?.version,
        api: typeof window.Api?.request,
        newChat: typeof window.TofuModules?.resolveAction('newChat'),
        settings: window.TofuModules?.canInvokeFeature('openSettings'),
        oldScripts: performance.getEntriesByType('resource')
          .some(entry => entry.name.includes('/static/js/')),
    })""")
    assert public == {
        'version': 3, 'api': 'function', 'newChat': 'function',
        'settings': True, 'oldScripts': False,
    }


def test_send_message_streams_and_renders(page, assert_no_js_errors):
    """Send a message against the stubbed LLM and assert a real assistant
    message renders in the live DOM. Fresh conversation (newChat) for
    isolation — without it the send collides with the open conv and the task
    manager auto-aborts it as superseded."""
    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    time.sleep(0.4)

    calls_before = _SENTINEL['stream_calls']
    page.locator('#userInput').fill('Hello E2E')
    page.locator('#sendBtn').click()

    page.wait_for_function(
        "document.querySelector('#chatInner').innerText.includes('stubbed model')",
        timeout=30000)

    body_text = page.inner_text('#chatInner')
    assert 'Hello E2E' in body_text, 'user message not rendered'
    assert 'stubbed model' in body_text, (
        f'assistant reply not rendered; chat body was:\n{body_text[:500]}'
    )
    # LOUD GUARD: the stub MUST have run. If it didn't, a real model produced
    # the output (or the patch missed) — fail rather than pass on real data.
    assert _SENTINEL['stream_calls'] > calls_before, (
        'stream_llm_response stub never ran — a real model may have streamed; '
        'the E2E is no longer hermetic.'
    )


def test_tool_round_renders(page, assert_no_js_errors):
    """A web_search tool round renders through the native Turn block owner —
    exercises SSE tool_start/tool_result → projection → rich tool renderer.
    Fresh conversation for isolation (see above)."""
    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    time.sleep(0.4)

    stream_before = _SENTINEL['stream_calls']
    search_before = _SENTINEL['search_calls']
    page.locator('#userInput').fill('__e2e_tool__ please search')
    page.locator('#sendBtn').click()

    try:
        page.wait_for_selector(
            '.conversation-block--tool', state='attached', timeout=30000)
    except Exception:
        body = page.inner_text('#chatInner')
        raise AssertionError(
            'native tool block never rendered for a tool round; '
            f'body:\n{body[:600]}')
    assert page.locator(
        '.conversation-block--tool .ptool-line, '
        '.conversation-block--tool .conversation-tool, '
        '.conversation-block--tool .ptool-results-block',
    ).count() >= 1, (
        'native tool block rendered but contains no rich or fallback tool row'
    )
    # LOUD GUARD: both the LLM stub and the search stub must have run.
    assert _SENTINEL['stream_calls'] > stream_before, (
        'stream_llm_response stub never ran for the tool scenario'
    )
    assert _SENTINEL['search_calls'] > search_before, (
        'perform_web_search stub never ran — a real web search may have executed'
    )


def test_admin_entry_loads_its_own_vite_graph(
        page, live_server, assert_no_js_errors):
    """The standalone admin page boots its ESM entry and visible auth gate."""
    response = page.goto(f'{live_server}/admin', wait_until='domcontentloaded')
    assert response and response.status == 200
    page.wait_for_function(
        "window.TofuModules && window.TofuModules.version === 3",
        timeout=15000)
    page.wait_for_selector('#adminGate', state='visible', timeout=15000)
    evidence = page.evaluate(r"""() => ({
      entry: performance.getEntriesByType('resource')
        .some(item => /\/static\/vite\/assets\/admin-[^/]+\.js$/.test(item.name)),
      classic: performance.getEntriesByType('resource')
        .some(item => item.name.includes('/static/js/')),
      api: typeof window.Api?.request,
    })""")
    assert evidence == {'entry': True, 'classic': False, 'api': 'function'}
