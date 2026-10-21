"""Browser recovery uses the common source extractor, never raw DOM text."""

import asyncio
from types import SimpleNamespace

import pytest

from tofu_search.fetch import playwright_pool, rendered_source

pytestmark = pytest.mark.unit

FACT = 'The eastern observatory opened to visitors on 17 May 1986.'
HTML = ('<html><body><main><h1>Observatory archive</h1><p>' + FACT + '</p>'
        '<p>Read the <a href="../records">original records</a> before starting.</p>'
        '<pre>if count &lt; 3:\n    await next_item()\n\n    finish()</pre>'
        '<table><tr><th>Year</th><th>Visitors</th></tr>'
        '<tr><td>1986</td><td>42</td></tr></table>'
        '<p hidden>HIDDEN SENTINEL</p>'
        '<script>window.privateData = "SCRIPT SENTINEL";</script>'
        '</main></body></html>')


@pytest.fixture
def page_harness(monkeypatch):
    contexts = []

    class Locator:
        async def text_content(self, **_kwargs):
            return FACT + ' HIDDEN SENTINEL SCRIPT SENTINEL ' + 'opaque script data ' * 20

    class Page:
        url = 'https://example.test/archive/page'
        main_frame = object()
        response_status = 200
        later_status = None
        during_snapshot_status = None
        during_snapshot_navigation = False
        pending_with_late_response = False
        snapshot = HTML
        snapshot_error = False
        snapshot_truncated = False

        async def route(self, *_args):
            pass

        def __init__(self):
            self.listeners = {'request': [], 'response': []}

        def on(self, event, callback):
            self.listeners[event].append(callback)

        def remove_listener(self, event, callback):
            self.listeners[event].remove(callback)

        def request(self, *, main=True, navigation=True):
            request = SimpleNamespace(frame=self.main_frame if main else object(),
                is_navigation_request=lambda: navigation)
            for listener in self.listeners['request']:
                listener(request)
            return request

        def respond(self, status, *, main=True, navigation=True):
            request = self.request(main=main, navigation=navigation)
            response = SimpleNamespace(status=status, frame=request.frame, request=request)
            for listener in self.listeners['response']:
                listener(response)
            return response

        async def goto(self, *_args, **_kwargs):
            if self.response_status is not None:
                self.initial_response = self.respond(self.response_status)

        async def wait_for_function(self, *_args, **_kwargs):
            if self.pending_with_late_response:
                self.request()
                for listener in self.listeners['response']:
                    listener(self.initial_response)

        async def wait_for_timeout(self, *_args):
            if self.later_status is not None:
                self.respond(self.later_status)
                self.later_status = None
            self.respond(503, main=False)
            self.respond(404, navigation=False)

        def locator(self, *_args):
            return Locator()

        async def evaluate(self, expression, *_args):
            if 'outerHTML' in expression:
                if self.during_snapshot_navigation:
                    self.request()
                if self.during_snapshot_status is not None:
                    self.respond(self.during_snapshot_status)
                if self.snapshot_error:
                    raise ValueError('Snapshot unavailable')
                return {'html': self.snapshot, 'truncated': self.snapshot_truncated}
            return 400

        async def content(self):
            return HTML

    page = Page()

    class Context:
        closed = False

        async def new_page(self):
            return page

        async def close(self):
            self.closed = True

    async def new_context(*_args, **_kwargs):
        context = Context()
        contexts.append(context)
        return context

    async def blocking(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    pool = playwright_pool.PlaywrightPool()
    monkeypatch.setattr(pool, '_new_context', new_context)
    monkeypatch.setattr(rendered_source, 'run_browser_blocking', blocking)
    yield pool, page
    assert contexts and all(c.closed for c in contexts)
    assert not any(page.listeners.values())


def fetch(harness, lane, max_chars=0):
    pool, page = harness
    if lane == 'anonymous':
        action = pool._do_fetch(object(), page.url, 10, max_chars)
    else:
        action = pool._do_fetch_authenticated(object(), {
            'url': page.url, 'timeout': 10, 'max_chars': max_chars, 'cookies': []})
    return asyncio.run(action)


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_rendered_html_keeps_source_structure_and_excludes_noncontent(page_harness, lane):
    text = fetch(page_harness, lane)
    assert FACT in text
    assert 'HIDDEN SENTINEL' not in text and 'SCRIPT SENTINEL' not in text
    assert 'https://example.test/records' in text
    assert 'if count < 3:\n    await next_item()\n\n    finish()' in text
    assert '| 1986 | 42 |' in text


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_rendered_source_bounds_are_applied_after_extraction(page_harness, lane):
    full = fetch(page_harness, lane)
    assert fetch(page_harness, lane, 80) == full[:80] + '\n[…truncated]'


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_hidden_only_rendered_page_is_not_readable_evidence(page_harness, lane):
    page_harness[1].snapshot = '<html><body><p hidden>' + FACT + '</p></body></html>'
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_failed_rendered_snapshot_does_not_fall_back_to_script_or_hidden_text(page_harness, lane):
    page_harness[1].snapshot_error = True
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_short_readable_rendered_source_is_retained(page_harness, lane):
    page_harness[1].snapshot = '<main><p>The archive opens at nine each morning.</p></main>'
    assert 'opens at nine' in fetch(page_harness, lane)


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
@pytest.mark.parametrize('status', [None, 302, 401, 403, 404, 429, 503])
def test_main_document_without_success_is_not_source_even_with_plausible_body(page_harness, lane, status):
    page_harness[1].response_status = status
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_successful_later_document_can_recover_during_existing_render_wait(page_harness, lane):
    page_harness[1].response_status = 403
    page_harness[1].later_status = 200
    assert FACT in fetch(page_harness, lane)


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_later_error_document_does_not_inherit_initial_success(page_harness, lane):
    page_harness[1].later_status = 503
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
@pytest.mark.parametrize('status', [200, 403])
def test_navigation_during_snapshot_invalidates_source_receipt(page_harness, lane, status):
    page_harness[1].during_snapshot_status = status
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_new_document_request_invalidates_snapshot_before_any_response(page_harness, lane):
    page_harness[1].during_snapshot_navigation = True
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_old_response_cannot_authorize_a_new_pending_navigation(page_harness, lane):
    page_harness[1].pending_with_late_response = True
    assert fetch(page_harness, lane) is None


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_partial_rendered_snapshot_marks_source_loss(page_harness, lane):
    page_harness[1].snapshot_truncated = True
    text = fetch(page_harness, lane)
    assert FACT in text and text.endswith('\n[…truncated]')


@pytest.mark.parametrize('lane', ['anonymous', 'authenticated'])
def test_rendered_navigation_with_an_empty_application_main_is_unavailable(page_harness, lane):
    page_harness[1].snapshot = ('<body><div id="app"><header>' + FACT * 10 + '</header>'
                               '<main></main><div>Corporate store navigation and links</div>'
                               '</div></body>')
    assert fetch(page_harness, lane) is None


def test_readiness_does_not_restart_the_budget_after_navigation(monkeypatch):
    clock = [8.0]
    calls = []

    class Page:
        async def wait_for_function(self, _expression, *, timeout):
            calls.append(('ready', timeout))
            clock[0] = 10.0
            raise TimeoutError('Existing deadline exhausted')

        async def evaluate(self, *_args):
            pytest.fail('No stability probe starts after the deadline')

    monkeypatch.setattr(rendered_source.time, 'monotonic', lambda: clock[0])
    asyncio.run(rendered_source.wait_for_rendered_source(Page(), deadline=10.0))
    assert calls == [('ready', 2000.0)]


def test_already_expired_readiness_starts_no_browser_wait(monkeypatch):
    monkeypatch.setattr(rendered_source.time, 'monotonic', lambda: 10.0)
    asyncio.run(rendered_source.wait_for_rendered_source(object(), deadline=9.0))
