"""Real-browser guard: Reading-mode report/review generation rides the CHAT
inline tool timeline (``renderSegmentTimelineHTML``) — reasoning sits adjacent
to the most recent tool calls, exactly like a chat agent bubble.

WHY THIS GUARD (owner acceptance criteria, 2026-07-28)
──────────────────────────────────────────────────────
The report progress UI used to be a fixed three-zone layout (tool cards / one
aggregated thinking block / body). The objective: reuse the agent-bubble
inline tool timeline so thinking + narration appear next to the tool calls
that produced them. This suite pins the RESULT in a real browser against the
real bundle, with ``/api/v1/paper/report/start`` + ``/api/v1/paper/report/poll``
stubbed to replay a recorded event sequence (no LLM cost, deterministic
timing), for BOTH views that share the pipeline (owner criterion 3 — a
shared-code-path claim is not a verification):

  1. PRE-TOOL PHASE — before the first tool call the thinking content is
     still visible (lightweight strip); the area never goes blank.
  2. INLINE TIMELINE — once a tool starts, thinking renders inside the
     ``.ptool-panel.seg-timeline`` panel ADJACENT to the tool rows, and a
     later round's thinking lands right after the most recent tool group.
  3. delta_reset — a tool round's discarded draft never lingers in the
     timeline (its narration segment is removed; thinking segments are not).
  4. DONE STATE — the timeline stays visible ABOVE the final report body
     (chat parity: settled bubbles keep the tool panel over the deliverable).
     For the review view this must ALSO hold across the EN/中 reading-language
     toggle, which re-renders via ``_renderFinalReport``.

This file extends the reading-mode browser coverage started in
``test_visual_surfaces.py`` (which opens the shell only) to the report
generation render chain itself.

NEUTER DISCIPLINE (charter: a guard that never bites is worse than none)
────────────────────────────────────────────────────────────────────────
Two negative controls mutate the recorded transport input before it reaches
the live reducer (no filesystem mutation, nothing to restore):

  * drop the ``thinking`` frames while retaining the tool frames: the panel
    must keep its tool rows but lose inline thinking;
  * drop the ``delta_reset`` frame: the discarded draft's narration segment
    must survive and render.

This boundary-level mutation remains valid after Paper state became
module-owned: it avoids adding a public mutable runtime hook solely for tests,
while still proving that both positive assertions fail when their causal input
is removed.

Skips cleanly when Playwright/Chromium is unavailable (standard visual mark).
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

_THINK_R0 = 'I should verify the follow-up work first. '
_DRAFT_R0 = 'This interim draft must vanish. '
_THINK_R1 = 'Now I can write the final report. '
_FINAL_BODY = '# Demo Report\n\nFinal report body with the verified facts.'
_REVIEW_ZH = '# 中文评审\n\n这是翻译后的评审正文。'
_SEARCH_QUERY = 'LLaDA2.2 Levenshtein editing'

#: Per-view DOM handles. Both views share _applyReportEvent / the skeleton /
#: _paintReportFromState, parameterized ONLY by these ids (paper-reader.js
#: `_reportView(kind)`) — exactly the fork point this suite pins.
_VIEWS = {
    'report': {
        'container': 'paperReportContent',
        'thinking_block': 'reportThinkingBlock',
        'entry': '_generatePaperReport',
    },
    'review': {
        'container': 'paperReviewContent',
        'thinking_block': 'reviewThinkingBlock',
        'entry': '_generatePaperReview',
    },
}


def _poll_payload(
        cursor: int, *, include_thinking: bool = True,
        include_delta_reset: bool = True) -> dict:
    """The recorded event sequence a real report task emits, paged by cursor.

    Round 0: thinking → draft prose (discarded via delta_reset) → web_search.
    Round 1 (terminal): thinking → final report body (no tools) → done.
    """
    if cursor == 0:
        events = [{'type': 'status', 'status': 'running'}]
        if include_thinking:
            events.append(
                {'type': 'thinking', 'delta': _THINK_R0, 'llmRound': 0})
        return {
            'events': events,
            'next_cursor': 1, 'status': 'running',
        }
    if cursor == 1:
        events = [
            {'type': 'delta', 'delta': _DRAFT_R0, 'llmRound': 0},
        ]
        if include_delta_reset:
            events.append({'type': 'delta_reset', 'llmRound': 0})
        events.append(
            {'type': 'tool_start', 'roundNum': 1, 'llmRound': 0,
             'toolName': 'web_search', 'query': _SEARCH_QUERY,
             'toolCallId': 'tc-1',
             'toolArgs': '{"queries": ["LLaDA2.2"]}'})
        return {
            'events': events,
            'next_cursor': 2, 'status': 'running',
        }
    if cursor == 2:
        events = [
            {'type': 'tool_done', 'roundNum': 1, 'llmRound': 0,
             'toolName': 'web_search', 'toolCallId': 'tc-1', 'elapsed': 1.2,
             'toolContent': 'search result bytes the model saw',
             'results': [{'title': 'LLaDA2.2', 'url': 'https://example.org/x',
                          'snippet': 'follow-up work'}]},
        ]
        if include_thinking:
            events.append(
                {'type': 'thinking', 'delta': _THINK_R1, 'llmRound': 1})
        events.append(
            {'type': 'delta', 'delta': _FINAL_BODY, 'llmRound': 1})
        return {
            'events': events,
            'next_cursor': 3, 'status': 'running',
        }
    return {
        'events': [
            {'type': 'done', 'report': _FINAL_BODY, 'paperHash': 'h-tl',
             'meta': {'model': 'guard-model'}},
        ],
        'next_cursor': cursor + 1, 'status': 'done',
        'report': _FINAL_BODY, 'meta': {'model': 'guard-model'},
    }


def _install_report_task_routes(
        page, *, include_thinking: bool = True,
        include_delta_reset: bool = True) -> None:
    """Stub the report-task endpoints (shared by report AND review — the review
    reuses the whole report pipeline with a composite cache key), plus the two
    review-only pre/post chains (venue registry, translation cache).

    Returns a dict of captured request evidence (the start body) so tests can
    pin WHICH cache key the generation ran under."""
    captured: dict = {'start_bodies': []}

    def _fulfill(route, payload: dict) -> None:
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps(payload))

    def on_start(route) -> None:
        try:
            captured['start_bodies'].append(
                json.loads(route.request.post_data or '{}'))
        except Exception:
            captured['start_bodies'].append({})
        _fulfill(route, {'ok': True, 'task_id': 'task-tl-1', 'paper_hash': 'h-tl'})

    def on_poll(route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        cursor = int(query.get('cursor', ['0'])[0])
        payload = _poll_payload(
            cursor,
            include_thinking=include_thinking,
            include_delta_reset=include_delta_reset,
        )
        payload['ok'] = True
        _fulfill(route, payload)

    page.route('**/api/v1/paper/report/start', on_start)
    # Trailing '**' swallows the ?task_id=…&cursor=N query string — without it
    # the glob must match the WHOLE url and the real (404ing) server answers.
    page.route('**/api/v1/paper/report/poll**', on_poll)
    # Review generation resolves the venue BEFORE starting (composite cache
    # key review:<venue>:<lang>) — one venue so the resolve picks it.
    page.route(
        '**/api/v1/paper/review/venues**',
        lambda route: _fulfill(route, {
            'ok': True,
            'venues': [{'key': 'neurips2026', 'name': 'NeurIPS 2026'}],
        }))
    # The EN→中 reading toggle asks the translate cache first; a hit renders
    # instantly through _renderFinalReport (the path that must keep the
    # timeline), so no translate task is ever started.
    page.route(
        '**/api/v1/paper/translate/cache',
        lambda route: _fulfill(route, {'ok': True, 'text': _REVIEW_ZH}))
    return captured


def _enter_paper_mode(page) -> None:
    """Load the lazy Vite Paper domain before inspecting its global seams."""
    page.evaluate("""async () => {
        await window.TofuModules.invokeFeature('togglePaperMode', [], () => {});
    }""")
    page.wait_for_function(
        "window.paperMode && typeof window.TofuModules.resolveAction("
        "'_reportSegmentsForRender') === 'function'",
        timeout=15000)
    # Paper library hydration is intentionally async and may replace the
    # active entry. Let it settle before the fixture stages its synthetic id.
    page.wait_for_timeout(800)


def _start_generation(page, view: str = 'report', *, enter: bool = True) -> None:
    """Stage a paper through its owner and drive the real generation flow.

    Paper state is module-owned after the ESM migration.  Assigning similarly
    named ``window`` properties only mutates stale compatibility snapshots and
    leaves the report owner with no parsed text.  Build and open the fixture
    through the Paper library surface so this browser guard exercises the same
    authority boundary as a user opening a stored paper.
    """
    if enter:
        _enter_paper_mode(page)
    page.evaluate(
        "(entryAction) => {"
        " const resolve = window.TofuModules.resolveAction;"
        " const create = resolve('_createPaperEntry');"
        " const setActive = resolve('_setActivePaperId');"
        " const open = resolve('_openPaperEntry');"
        " const paper = create('Timeline fixture', '',"
        "   'parsed paper text', '', 'p-tl');"
        " paper.paperHash = 'h-tl';"
        # ``create`` selects the new entry.  Clear that selection before
        # ``open`` because open first saves the prior active entry; otherwise
        # the empty reader state would overwrite this fixture's parsed text.
        " setActive('');"
        " open(paper);"
        " resolve(entryAction)();"
        " }",
        _VIEWS[view]['entry'])


def _panel_order(page, view: str = 'report'):
    """Classify the timeline panel's top-level children in DOM order."""
    return page.evaluate(
        "() => {"
        " const body = document.querySelector("
        f"   '#{_VIEWS[view]['container']} .ptool-panel.seg-timeline .ptool-panel-body');"
        " if (!body) return null;"
        " return [...body.children].map(el =>"
        "   el.classList.contains('thinking-block') ? 'thinking'"
        "   : el.classList.contains('seg-narration') ? 'narration'"
        "   : 'tools');"
        "}")


def _wait_ready(page, timeout=20000):
    page.wait_for_selector('#userInput', state='visible', timeout=timeout)
    page.wait_for_function(
        "window.TofuModules?.version === 3"
        " && typeof window.TofuModules.invokeFeature === 'function'",
        timeout=timeout)


def _assert_timeline_flow(page, view: str) -> None:
    """Owner criteria 1–4, asserted against the given view's container. The
    generation must already have been started and its routes stubbed."""
    container = _VIEWS[view]['container']
    strip_id = _VIEWS[view]['thinking_block']

    # ── Criterion 1: pre-tool phase — thinking visible before any tool call.
    page.wait_for_function(
        "() => { const b = document.getElementById(" + json.dumps(strip_id) + ");"
        f" return b && b.style.display !== 'none'"
        f"   && b.textContent.includes({json.dumps(_THINK_R0.strip())}); }}",
        timeout=15000)

    # ── Criterion 2: once the tool starts, thinking lives INSIDE the chat
    #    timeline panel, adjacent to the tool rows; the standalone strip
    #    yields (no double-showing the same reasoning).
    page.wait_for_function(
        "() => !!document.querySelector("
        f"  '#{container} .ptool-panel.seg-timeline')",
        timeout=15000)
    page.wait_for_function(
        "() => { const p = document.querySelector("
        f"   '#{container} .ptool-panel.seg-timeline');"
        f" return p && p.textContent.includes({json.dumps(_THINK_R0.strip())}); }}",
        timeout=15000)

    order = _panel_order(page, view)
    assert order is not None, f'{view}: timeline panel body never appeared'
    assert order == ['thinking', 'tools'], (
        f'{view}: expected round-0 thinking batch adjacent BEFORE its tool '
        f'rows, got child order: {order}')

    # The standalone strip must be hidden now (the panel carries the thinking).
    strip_display = page.evaluate(
        "() => document.getElementById(" + json.dumps(strip_id) + ").style.display")
    assert strip_display == 'none', (
        f'{view}: standalone thinking strip still visible alongside the '
        f'timeline — the same reasoning renders twice')

    # ── Criterion 3: the discarded round-0 draft NEVER renders in the panel.
    panel_text = page.evaluate(
        "() => document.querySelector("
        f"  '#{container} .ptool-panel.seg-timeline').textContent")
    assert _DRAFT_R0.strip() not in panel_text, (
        f'{view}: delta_reset draft leaked into the timeline — the segment '
        f'removal did not bite')

    # ── Round-1 thinking lands right AFTER the most recent tool group.
    page.wait_for_function(
        "() => { const p = document.querySelector("
        f"   '#{container} .ptool-panel.seg-timeline');"
        f" return p && p.textContent.includes({json.dumps(_THINK_R1.strip())}); }}",
        timeout=15000)
    order = _panel_order(page, view)
    assert order == ['thinking', 'tools', 'thinking'], (
        f'{view}: round-1 thinking must sit adjacent to the MOST RECENT tool '
        f'rows, got child order: {order}')

    # ── Criterion 4: done — timeline stays ABOVE the final body.
    page.wait_for_function(
        "() => !!document.querySelector("
        f"  '#{container} .paper-report-article')",
        timeout=15000)
    _assert_timeline_above_body(page, view, 'Final report body')

    # The discarded draft is nowhere in the whole container either.
    container_text = page.evaluate(
        "() => document.getElementById(" + json.dumps(container) + ").textContent")
    assert _DRAFT_R0.strip() not in container_text


def _assert_timeline_above_body(page, view: str, body_marker: str) -> None:
    """Done-state layout: the timeline panel exists, precedes the article,
    still carries the thinking, and the article carries the expected body."""
    container = _VIEWS[view]['container']
    layout = page.evaluate(
        "() => {"
        f" const p = document.querySelector('#{container} .ptool-panel');"
        " const a = document.querySelector("
        f"   '#{container} .paper-report-article');"
        " if (!p || !a) return null;"
        " return { before: !!(p.compareDocumentPosition(a)"
        "           & Node.DOCUMENT_POSITION_FOLLOWING),"
        f"  thinking: p.textContent.includes({json.dumps(_THINK_R0.strip())}),"
        f"  body: a.textContent.includes({json.dumps(body_marker)}),"
        " };"
        "}")
    assert layout is not None, (
        f'{view}: done state lost the timeline — the chat-parity rule is '
        f'"tool panel above the deliverable"')
    assert layout['before'], f'{view}: timeline does not precede the article'
    assert layout['thinking'], f'{view}: done-state timeline dropped the thinking'
    assert layout['body'], f'{view}: final body ({body_marker!r}) did not render'


def test_report_stream_uses_chat_inline_tool_timeline(page, assert_no_js_errors):
    _wait_ready(page)
    _install_report_task_routes(page)
    _enter_paper_mode(page)
    _start_generation(page, 'report', enter=False)
    _assert_timeline_flow(page, 'report')


def test_review_stream_uses_chat_inline_tool_timeline(page, assert_no_js_errors):
    """The review view shares _applyReportEvent / the skeleton / the paint via
    idPrefix — a shared-path claim is NOT a verification, so the same four
    criteria run against #paperReviewContent through the REAL review entry
    (_generatePaperReview → venue resolve → composite cache key)."""
    _wait_ready(page)
    captured = _install_report_task_routes(page)
    _start_generation(page, 'review')
    _assert_timeline_flow(page, 'review')

    # The venue-resolve pre-chain must have resolved the stubbed venue into
    # the composite cache key — otherwise the generation silently ran under
    # review:generic:… (the historical cache-key-skew bug class).
    langs = [b.get('lang') for b in captured['start_bodies']]
    assert langs == ['review:neurips2026:en'], (
        f'review generation ran under unexpected cache key(s): {langs} — the '
        f'venue resolve chain did not pick up the stubbed venue')

    # ── EN/中 reading-language toggle re-renders through _renderFinalReport —
    #    the timeline must survive BOTH directions (a regression here would
    #    eat the panel the first time a zh UI user toggles the view).
    page.evaluate(
        "() => window.TofuModules.resolveAction('_setReviewLang')('zh')")
    page.wait_for_function(
        "() => { const a = document.querySelector("
        "   '#paperReviewContent .paper-report-article');"
        f" return a && a.textContent.includes({json.dumps('翻译后的评审正文')}); }}",
        timeout=15000)
    _assert_timeline_above_body(page, 'review', '翻译后的评审正文')

    page.evaluate(
        "() => window.TofuModules.resolveAction('_setReviewLang')('en')")
    page.wait_for_function(
        "() => { const a = document.querySelector("
        "   '#paperReviewContent .paper-report-article');"
        "  return a && a.textContent.includes('Final report body'); }",
        timeout=15000)
    _assert_timeline_above_body(page, 'review', 'Final report body')


def test_neuter_drop_thinking_events_removes_inline_thinking(page, assert_no_js_errors):
    """NEUTER 1: without thinking frames, tools remain but thinking vanishes."""
    _wait_ready(page)
    _install_report_task_routes(page, include_thinking=False)
    _enter_paper_mode(page)
    _start_generation(page, 'report', enter=False)

    page.wait_for_function(
        "() => !!document.querySelector('#paperReportContent .ptool-panel')",
        timeout=15000)
    state = page.evaluate(
        "() => { const p = document.querySelector("
        "  '#paperReportContent .ptool-panel');"
        f" return {{ hasThinking: p.textContent.includes({json.dumps(_THINK_R0.strip())}),"
        f"  hasQuery: p.textContent.includes({json.dumps(_SEARCH_QUERY)}) }}; }}")
    assert state['hasQuery'], (
        'neutered input lost the tool rows too — the control says nothing')
    assert not state['hasThinking'], (
        'thinking rendered without any thinking frame — the positive guard '
        'does not depend on its claimed transport input')


def test_neuter_drop_delta_reset_event_leaks_draft(page, assert_no_js_errors):
    """NEUTER 2: without delta_reset, the discarded draft must render."""
    _wait_ready(page)
    _install_report_task_routes(page, include_delta_reset=False)
    _enter_paper_mode(page)
    _start_generation(page, 'report', enter=False)

    page.wait_for_function(
        "() => !!document.querySelector("
        "  '#paperReportContent .ptool-panel.seg-timeline')",
        timeout=15000)
    page.wait_for_function(
        "() => { const p = document.querySelector("
        "   '#paperReportContent .ptool-panel.seg-timeline');"
        f" return p && p.textContent.includes({json.dumps(_DRAFT_R0.strip())}); }}",
        timeout=15000)
    panel_text = page.evaluate(
        "() => document.querySelector("
        "  '#paperReportContent .ptool-panel.seg-timeline').textContent")
    assert _DRAFT_R0.strip() in panel_text, (
        'delta_reset-negative control did not preserve the discarded draft')
