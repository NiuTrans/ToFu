"""Native EventSource recovery replaces the rendered authoritative projection.

The local browser origin serves the real coordinator, TurnStore and Surface.
Only HTTP responses are fault-injected; no live storage or model is required.
"""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def _stream_cursor(route) -> str:
    """The single subscription's cursor on a multiplexed events request."""
    from urllib.parse import parse_qs, urlsplit
    [stream] = json.loads(parse_qs(urlsplit(route.request.url).query)['streams'][0])
    return stream['after']


def _multiplexed_frame(event: dict) -> str:
    return f"event: {event['type']}\ndata: 0\ndata: {json.dumps(event)}\n\n"


@pytest.mark.parametrize('startup', ['pending', 'tools'])
def test_http_204_recovery_retires_stale_startup_and_reaches_terminal(frontend_visual_page, startup):
    """A real CLOSED EventSource must not pin accepted/preparation UI forever."""
    from pathlib import Path
    from playwright.sync_api import expect

    page = frontend_visual_page
    for name, source in [
        ('sync', 'frontend/src/core/conversation-sync.ts'),
        ('streams', 'frontend/src/core/conversation-event-multiplexer.ts'),
        ('state', 'frontend/src/core/turn-state.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('session', 'frontend/src/conversation/application/conversation-session.ts'),
        ('renderers', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'closed-{name}.js', source))
    locale = json.loads((Path(__file__).resolve().parents[2]
                         / 'frontend/src/i18n/locales/zh.json').read_text())
    heads, event_cursors = [], []

    def serve_snapshot(route):
        head = 1 + len(heads) * 8
        heads.append(head)
        status = ('pending' if startup == 'pending' else 'running') if head == 1 else 'completed'
        body = {
            'ok': True, 'contract': 'tofu.conversation-sync.snapshot/v1',
            'scope': {'kind': 'conversation', 'ownerId': 7, 'threadId': 'closed'},
            'conversationId': 'closed', 'conversationRevision': head, 'syncSeq': head,
            'cursor': f'cursor-{head}', 'serverBootId': 'test', 'heartbeatIntervalMs': 15000,
            'settings': {}, 'queueItems': [],
            'attempts': [{'attemptId': 'attempt', 'turnId': 'answer', 'status': status}],
            'turns': [{
                'turnId': 'answer', 'presentationId': 'answer', 'conversationId': 'closed',
                'laneId': 'main', 'ordinal': 1, 'actor': 'assistant', 'kind': 'reply', 'runId': '',
                'status': status, 'currentAttemptId': 'attempt', 'projectionRevision': head,
                'createdAt': 1, 'updatedAt': head, 'settlement': {},
                'projection': {'content': '' if head == 1 else 'Committed completed answer'},
            }],
        }
        if head == 1 and startup == 'tools':
            body['activeAttemptPhase'] = {'attemptId': 'attempt', 'turnId': 'answer',
                'projectionRevision': head, 'phase': {'phase': 'working',
                    'detailKey': 'stream.phase.startupTools'}}
        route.fulfill(content_type='application/json', body=json.dumps(body))

    def serve_events(route):
        event_cursors.append(_stream_cursor(route))
        if len(event_cursors) <= 2:
            route.fulfill(status=204, headers={'X-Tofu-Stream-Admission': 'capacity'})
        else:
            event = {'contract': 'tofu.conversation-sync.event/v1', 'conversationId': 'closed',
                     'type': 'sync.heartbeat', 'cursor': 'cursor-17', 'serverBootId': 'test',
                     'degraded': False}
            route.fulfill(content_type='text/event-stream', body=_multiplexed_frame(event))

    page.route('**/closed-fixture/snapshot', serve_snapshot)
    page.route('**/closed-fixture/events?*', serve_events)
    page.evaluate("""locale => {
      document.body.innerHTML = '<main id="surface"></main>';
      const store = createTurnStore('closed');
      const surface = createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({renderSafeMarkdownHtml: value => value,
          localizedText: (key, fallback) => locale[key] || fallback}));
      const session = bindConversationSession(store, surface);
      window.closedStore = store;
      const streams = createConversationEventMultiplexer({streamClientId:'closed-page', api:{
        multiplexedEventsUrl:(_client, subscriptions) =>
          '/closed-fixture/events?streams=' + encodeURIComponent(JSON.stringify(subscriptions)),
      }});
      window.coordinator = new ConversationSyncCoordinator({conversationId:'closed', api:{
        async snapshot(_id, options) {
          return (await fetch('/closed-fixture/snapshot', {signal:options.signal})).json();
        },
      }, eventSourceFactory: streams.open,
      onSnapshot(snapshot) { store.dispatch({type:'snapshot', snapshot}); },
      onTurnDelta() {return true;}, onAttemptEvent() {return true;}});
      window.disposeClosed = () => {coordinator.close(); session.dispose();};
      void coordinator.resume();
    }""", locale)
    key = 'stream.phase.executorPreparing' if startup == 'pending' else 'stream.phase.startupTools'
    try:
        expect(page.locator('#surface')).to_contain_text(locale[key])
        expect(page.locator('#surface')).to_contain_text('Committed completed answer', timeout=10000)
        page.wait_for_function('coordinator.cursor === "cursor-17"')
        expect(page.locator('#surface')).not_to_contain_text(locale[key])
        assert page.evaluate('closedStore.getState().turnsById.answer.status') == 'completed'
        assert heads == [1, 9, 17]
        # A finished fixture body is a dropped response; it resumes from 17.
        assert event_cursors[:3] == ['cursor-1', 'cursor-9', 'cursor-17']
        assert set(event_cursors[3:]) <= {'cursor-17'}
    finally:
        page.evaluate('disposeClosed()')


def test_failed_reset_snapshot_recovers_rendering_before_reopening_sse(frontend_visual_page):
    page = frontend_visual_page
    for name, source in [
        ('sync', 'frontend/src/core/conversation-sync.ts'),
        ('streams', 'frontend/src/core/conversation-event-multiplexer.ts'),
        ('state', 'frontend/src/core/turn-state.ts'),
        ('surface', 'frontend/src/conversation/ui/conversation-surface.ts'),
        ('session', 'frontend/src/conversation/application/conversation-session.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'recovery-{name}.js', source))

    snapshot_requests = []
    event_cursors = []

    def serve_snapshot(route):
        snapshot_requests.append(route.request.url)
        if len(snapshot_requests) == 2:
            route.fulfill(status=503, content_type='application/json',
                          body=json.dumps({'error': 'temporary storage failure'}))
            return
        head = 1 if len(snapshot_requests) == 1 else 9
        content = 'Old answer' if head == 1 else 'Recovered committed answer'
        route.fulfill(content_type='application/json', body=json.dumps({
            'ok': True, 'contract': 'tofu.conversation-sync.snapshot/v1',
            'scope': {'kind': 'conversation', 'ownerId': 7, 'threadId': 'recovery'},
            'conversationId': 'recovery', 'conversationRevision': head,
            'syncSeq': head, 'cursor': f'cursor-{head}', 'serverBootId': 'test',
            'heartbeatIntervalMs': 15000, 'settings': {}, 'attempts': [], 'queueItems': [],
            'turns': [{
                'turnId': 'answer', 'presentationId': 'answer', 'conversationId': 'recovery',
                'laneId': 'main', 'ordinal': 1, 'actor': 'assistant', 'kind': 'reply',
                'runId': '', 'status': 'completed', 'currentAttemptId': '',
                'projectionRevision': head, 'createdAt': 1, 'updatedAt': head,
                'settlement': {}, 'projection': {'content': content, 'segments': [{
                    'type': 'text', 'blockId': 'answer-text', 'text': content,
                    'terminal': True, 'deliverable': True,
                }]},
            }],
        }))

    def serve_events(route):
        event_cursors.append(_stream_cursor(route))
        event = {'contract': 'tofu.conversation-sync.event/v1', 'conversationId': 'recovery'}
        if len(event_cursors) == 1:
            event.update(type='sync.reset_required', cursor='reset-head-9', reason='server_restarted')
        else:
            event.update(type='sync.heartbeat', cursor='cursor-9', serverBootId='test', degraded=False)
        route.fulfill(content_type='text/event-stream', body=_multiplexed_frame(event))

    page.route('**/recovery-fixture/snapshot', serve_snapshot)
    page.route('**/recovery-fixture/events?*', serve_events)
    page.evaluate("""() => {
      document.body.innerHTML = '<main id="surface"></main>';
      const store = createTurnStore('recovery');
      const surface = createConversationSurface(document.getElementById('surface'));
      const session = bindConversationSession(store, surface);
      window.acceptedHeads = []; window.syncFailures = [];
      const streams = createConversationEventMultiplexer({streamClientId:'recovery-page', api:{
        multiplexedEventsUrl:(_client, subscriptions) =>
          '/recovery-fixture/events?streams=' + encodeURIComponent(JSON.stringify(subscriptions)),
      }});
      window.coordinator = new ConversationSyncCoordinator({
        conversationId: 'recovery',
        api: {
          async snapshot(_id, options) {
            const response = await fetch('/recovery-fixture/snapshot', { signal: options.signal });
            if (!response.ok) throw new Error('temporary storage failure');
            return response.json();
          },
        },
        eventSourceFactory: streams.open,
        onSnapshot(snapshot) { store.dispatch({type:'snapshot', snapshot}); acceptedHeads.push(snapshot.syncSeq); },
        onTurnDelta() { return true; }, onAttemptEvent() { return true; },
        onProtocolError(error) { syncFailures.push(error.message); },
      });
      window.disposeRecovery = () => { coordinator.close(); session.dispose(); surface.dispose(); };
      void coordinator.resume();
    }""")
    try:
        from playwright.sync_api import expect
        expect(page.locator('#surface')).to_contain_text('Recovered committed answer', timeout=10000)
        page.wait_for_function('coordinator.connected && coordinator.cursor === "cursor-9"')
        assert page.evaluate('acceptedHeads') == [1, 9]
        assert page.evaluate('syncFailures') == ['temporary storage failure']
        assert len(snapshot_requests) == 3
        assert event_cursors[:2] == ['cursor-1', 'cursor-9']
        assert set(event_cursors[2:]) <= {'cursor-9'}
        expect(page.locator('#surface')).not_to_contain_text('Old answer')
    finally:
        page.evaluate('disposeRecovery()')
