"""First-round narration stays single throughout pending parallel tool calls.

Use the real app, stream/reducer and Chromium DOM. Model tokens and the search
primitive are stubbed; a delegating gate holds the first projection event so
the real create-turn command response lands before it. Tool gates keep the faulty pending phase
observable instead of merely asserting the settled transcript.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

pytestmark = pytest.mark.visual

_NARRATION = 'I will inspect the existing dropdown before changing its behavior.'
_FINAL = 'The dropdown inspection is complete.'


@pytest.fixture(autouse=True)
def narrated_tools(monkeypatch, page):
    from lib.agent_core.events import EventType, build_event
    from lib.rate_limit_store import reset_for_test
    from lib.tasks_pkg.manager._events import append_event
    import lib.tasks_pkg.llm_fallback._call as llm_call
    import lib.tasks_pkg.handlers.search._core as search_core
    import tofu_search
    import lib.turn_lifecycle as lifecycle
    import lib.tasks_pkg.orchestrator.api as orchestrator
    import lib.tasks_pkg.manager._stream as provider_stream

    def reject_provider(*args, **kwargs):
        pytest.fail('the pending-narration browser test attempted real provider I/O')

    monkeypatch.setattr(provider_stream, 'dispatch_stream', reject_provider)

    worker_finished = threading.Event()
    original_runner = orchestrator.run_task
    owned_tasks = []

    def run_and_drain(task):
        try:
            return original_runner(task)
        finally:
            worker_finished.set()

    monkeypatch.setattr(orchestrator, 'run_task', run_and_drain)
    first_projection = threading.Event()
    allow_projection = threading.Event()
    original_projection = lifecycle.record_task_event

    def gated_projection(task, *args, **kwargs):
        if task.get('_attemptId') and not first_projection.is_set():
            owned_tasks.append(task)
            first_projection.set()
            if not allow_projection.wait(timeout=60):
                raise TimeoutError('Test did not release the first structural write')
        return original_projection(task, *args, **kwargs)

    monkeypatch.setattr(lifecycle, 'record_task_event', gated_projection)
    release = threading.Event()
    model_return = threading.Event()
    searches = []
    stream_calls = []
    lock = threading.Lock()

    def stream(task, body, tag='', on_tool_call_ready=None):
        stream_calls.append(task['id'] if 'id' in task else 'round')
        if not task.get('_pending_narration_test'):
            task['_pending_narration_test'] = True
            for chunk in (_NARRATION[:4], _NARRATION[4:25], _NARRATION[25:]):
                with task['content_lock']:
                    task['content'] += chunk
                append_event(task, build_event(EventType.DELTA, content=chunk))
                time.sleep(0.3)
            calls = []
            for index in range(2):
                call = {
                    'id': f'pending_narration_{index}', 'index': index,
                    'type': 'function', 'function': {
                        'name': 'web_search',
                        'arguments': json.dumps({'query': f'pending fixture {index}',
                                                 'vertical': 'off'}),
                    },
                }
                calls.append(call)
                if on_tool_call_ready:
                    on_tool_call_ready(call)
            if not model_return.wait(timeout=60):
                raise TimeoutError('Test did not finish the streaming tool-call prelude')
            return ({'role': 'assistant', 'content': _NARRATION,
                     'tool_calls': calls}, 'tool_calls',
                    {'prompt_tokens': 10, 'completion_tokens': 12,
                     'total_tokens': 22})
        with task['content_lock']:
            task['content'] += _FINAL
        append_event(task, build_event(EventType.DELTA, content=_FINAL))
        return ({'role': 'assistant', 'content': _FINAL, 'tool_calls': []},
                'stop', {'prompt_tokens': 22, 'completion_tokens': 8,
                         'total_tokens': 30})

    def search(query, *args, **kwargs):
        with lock:
            searches.append(query)
        if not release.wait(timeout=60):
            raise TimeoutError('Test did not release pending tool calls')
        return [{'title': 'Offline fixture', 'snippet': 'Controlled tool result',
                 'url': 'https://example.invalid/pending-narration', 'source': 'stub'}]

    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '0')
    reset_for_test()
    monkeypatch.setattr(llm_call, 'stream_llm_response', stream)
    monkeypatch.setattr(search_core, 'perform_web_search', search)
    monkeypatch.setattr(tofu_search, 'perform_web_search', search)
    try:
        yield {'release': release, 'model_return': model_return,
               'first_projection': first_projection, 'allow_projection': allow_projection,
               'searches': searches, 'streams': stream_calls, 'tasks': owned_tasks}
    finally:
        # Keep provider stubs and Sidecar alive until this test's real worker
        # drains, including assertion failures before the model can start.
        from lib.tasks_pkg.manager.cancellation import cancel_task
        from lib.tasks_pkg.manager._registry import task_user_id
        for task in owned_tasks:
            if task.get('status') in {'pending', 'running'}:
                cancel_task(task['id'], user_id=task_user_id(task),
                            source='pending-narration-test-cleanup')
        allow_projection.set()
        model_return.set()
        release.set()
        if owned_tasks:
            assert worker_finished.wait(timeout=30), 'test worker did not drain'
        reset_for_test()


_OBSERVE = r"""
(marker) => {
  const root = document.querySelector('#chatInner');
  const observations = [];
  let maxCopies = 0;
  const sample = () => {
    const blocks = [...root.querySelectorAll('.conversation-block--text')]
      .map(node => ({
        id: node.dataset.blockId,
        text: node.innerText,
        copies: node.innerText.split(marker).length - 1,
      })).filter(block => block.copies);
    const state = {
      copies: blocks.reduce((count, block) => count + block.copies, 0),
      tools: root.querySelectorAll('.conversation-block--tool').length,
      blocks,
    };
    maxCopies = Math.max(maxCopies, state.copies);
    if (observations.length < 200 &&
        JSON.stringify(observations.at(-1)) !== JSON.stringify(state)) {
      observations.push(state);
    }
    return state;
  };
  const observer = new MutationObserver(sample);
  observer.observe(root, {subtree: true, childList: true, characterData: true});
  window.__pendingNarrationTest = {
    sample, observations, maxCopies: () => maxCopies,
    stop: () => { observer.disconnect(); return observations; },
  };
  sample();
}
"""


def _open_persisted_conversation(page, conversation_id):
    # A sidebar row may be locally cached before its server turn count arrives.
    # Wait for the same catalog boundary as the normal reload browser journey.
    page.wait_for_function(
        """(id) => window.conversations?.some(conversation =>
          conversation.id === id && Number(conversation._serverTurnCount) > 0)""",
        arg=conversation_id, timeout=15000)
    page.locator(f'.conv-item[data-conv-id="{conversation_id}"]').click(
        position={'x': 12, 'y': 12})


def test_first_round_narration_is_single_while_tools_pending(
        narrated_tools, page, tmp_path, assert_no_js_errors):
    from tests.conftest import _dismiss_onboarding_modals

    page.wait_for_selector('#userInput', state='visible', timeout=15000)
    page.wait_for_function('window.TofuModules?.version === 3', timeout=15000)
    _dismiss_onboarding_modals(page)
    page.locator('.new-chat-btn').click()
    _dismiss_onboarding_modals(page)
    page.evaluate(_OBSERVE, _NARRATION)
    page.locator('#userInput').fill('Inspect the dropdown using two offline searches.')
    with page.expect_response(
        lambda response: response.request.method == 'POST'
        and response.url.endswith('/turns')
    ) as create_response:
        page.locator('#sendBtn').click()
    assert create_response.value.ok
    try:
        assert narrated_tools['first_projection'].wait(timeout=30)

        conversation_id = narrated_tools['tasks'][0]['convId']
        # The worker may reach its gate before the browser has consumed the
        # create-turn ACK. Do not navigate away and cancel that initial send.
        page.wait_for_selector('.conversation-turn[data-turn-role=assistant]',
                               timeout=15000)
        # The public command response is an early replay base, just like a
        # snapshot. Hold all structural events until that base is installed;
        # tool completion must not be allowed to mask a duplicate afterward.
        created = create_response.value.json()
        assert created['turn']['conversationId'] == conversation_id
        narrated_tools['allow_projection'].set()
        page.wait_for_function(
            'window.__pendingNarrationTest.sample().tools === 2', timeout=45000)

        page.wait_for_function(
            'window.__pendingNarrationTest.sample().copies >= 1', timeout=10000)

        page.wait_for_timeout(400)
        assert page.evaluate('window.__pendingNarrationTest.sample().copies') == 1
        narrated_tools['model_return'].set()
        # A browser-owned delay lets paints/stream batches land while tool
        # completion is impossible (the gate is released only below).
        page.wait_for_timeout(1200)
        pending = page.evaluate('window.__pendingNarrationTest.sample()')
        observations = page.evaluate('window.__pendingNarrationTest.observations')
        (tmp_path / 'pending-narration.json').write_text(
            json.dumps(observations, ensure_ascii=False, indent=2))
        page.screenshot(path=str(tmp_path / 'pending-narration.png'), full_page=True)
        assert len(narrated_tools['searches']) == 2, 'both stub tools must be running'
        assert pending['copies'] == 1, pending

        assert _FINAL not in page.inner_text('#chatInner')
        assert all(item['copies'] <= 1 for item in observations), observations
    except Exception:
        evidence = page.evaluate("""() => ({
          conversations: (window.conversations || []).map(c => ({
            id: c.id, count: c._serverTurnCount, rev: c._serverRev,
            snapshotRequired: c._turnSnapshotRequired,
          })),
          rows: [...document.querySelectorAll('.conv-item')].map(n => ({
            id: n.dataset.convId, active: n.classList.contains('active'),
          })),
          turns: [...document.querySelectorAll('.conversation-turn')]
            .map(n => ({id: n.dataset.turnId, status: n.dataset.turnStatus})),
          text: document.querySelector('#chatInner')?.innerText,
        })""")
        if narrated_tools['tasks']:
            task = narrated_tools['tasks'][0]
            response = page.request.get(
                f"{page.url.rstrip('/')}/api/v4/conversations/{task['convId']}/sync")
            evidence['snapshotStatus'] = response.status
            evidence['snapshot'] = response.json()
        (tmp_path / 'failure.json').write_text(json.dumps(evidence, indent=2))
        page.screenshot(path=str(tmp_path / 'failure.png'), full_page=True)
        raise
    finally:
        narrated_tools['allow_projection'].set()
        narrated_tools['release'].set()
        narrated_tools['model_return'].set()
    page.wait_for_function(
        '(text) => document.querySelector("#chatInner").innerText.includes(text)',
        arg=_FINAL, timeout=45000)
    page.wait_for_function(
        'document.querySelector(".conversation-turn[data-turn-status=completed]")',
        timeout=15000)
    observations = page.evaluate('window.__pendingNarrationTest.stop()')
    assert all(item['copies'] <= 1 for item in observations), observations
    assert len(narrated_tools['streams']) == 2

    assert page.evaluate('window.__pendingNarrationTest.maxCopies()') == 1
    assert page.evaluate('window.__pendingNarrationTest.sample().copies') == 1
    # The cold snapshot must agree with the live stream, not merely hide the
    # duplicate at settlement. Page fixture owns and cleans this conversation.
    page.reload()
    page.wait_for_selector('#userInput', state='visible', timeout=15000)
    _dismiss_onboarding_modals(page)
    _open_persisted_conversation(page, conversation_id)
    page.wait_for_function(
        '(text) => document.querySelector("#chatInner").innerText.includes(text)',
        arg=_FINAL, timeout=30000)
    page.evaluate(_OBSERVE, _NARRATION)
    assert page.evaluate('window.__pendingNarrationTest.sample().copies') == 1
    page.evaluate('window.__pendingNarrationTest.stop()')
