"""Scroll-up history paging in the real app: trigger distance, waits, jumps.

Real local app and Chromium with synthetic owner-scoped histories shaped like
the two production cases: agent rounds whose assistant Turn carries megabytes
of folded process evidence (byte-bounded pages hold one Turn) and long chats
with hundreds of small Turns. The history service is wrapped with the latency
profile measured read-only on the loaded production host, so a reader who
reaches the loaded top waits visibly. A constant-speed reader scrolls to the
first Turn; one JSON evidence object per scenario is written and printed.
Assertions are geometric and work budgets, never host wall-clock limits.
"""
from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import pytest

pytestmark = pytest.mark.visual

# Read-only probes of the production loopback API on 2026-10-28 under agent
# load: a 2 KiB page answered in 139 ms, 1.4 MiB pages in 211-1275 ms, a
# 2.2 MiB page in 555 ms and 5.2 MiB in 1.9 s (GET /metrics: mean 210 ms).
HISTORY_BASE_LATENCY_S = float(os.environ.get('TOFU_HISTORY_BASE_MS') or 150) / 1000
HISTORY_LATENCY_PER_MIB_S = float(os.environ.get('TOFU_HISTORY_PER_MIB_MS') or 150) / 1000
READER_SPEED_PX_PER_S = int(os.environ.get('TOFU_HISTORY_READER_PX_PER_S') or 2400)
READER_DEADLINE_MS = 90_000

_OBSERVERS = """(() => {
  const perf = window.__historyPerf = {fetches: [], longTasks: [], frames: [], firstSeen: {}};
  const fetchImpl = window.fetch;
  window.fetch = function(input, init) {
    const raw = typeof input === 'string' ? input : input?.url || '';
    if (!raw.includes('/turns/history?')) return fetchImpl.apply(this, arguments);
    const url = new URL(raw, location.href).href;
    const viewport = document.querySelector('#chatContainer');
    const surface = document.querySelector('#chatInner .conversation-surface');
    const before = Number(new URL(url).searchParams.get('beforeOrdinal'));
    const turns = [...(surface?.querySelectorAll(
      '[data-conversation-part="main-lane"] article[data-turn-id]') || [])];
    const span = turns.length ? turns[turns.length - 1].getBoundingClientRect().bottom
      - turns[0].getBoundingClientRect().top : 0;
    const record = {url, beforeOrdinal: before, start: performance.now(),
      scrollTop: viewport?.scrollTop ?? null, clientHeight: viewport?.clientHeight ?? null,
      windowStart: Number(surface?.dataset.windowStart || 0),
      averageTurnPx: turns.length ? span / turns.length : 0};
    perf.fetches.push(record);
    const response = fetchImpl.apply(this, arguments);
    response.then(value => { record.status = value.status; record.headersAt = performance.now(); },
      () => { record.failed = true; });
    return response;
  };
  for (const type of ['longtask', 'long-animation-frame']) {
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) {
          (type === 'longtask' ? perf.longTasks : perf.frames).push(
            {start: entry.startTime, duration: entry.duration});
        }
      }).observe({type, buffered: true});
    } catch (_error) { /* unsupported entry type */ }
  }
  new MutationObserver(mutations => {
    const now = performance.now();
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType === 1 && node.tagName === 'ARTICLE' && node.dataset.turnId
            && !(node.dataset.turnId in perf.firstSeen)) perf.firstSeen[node.dataset.turnId] = now;
      }
    }
  }).observe(document, {childList: true, subtree: true});
})();"""

# Constant-speed upward reader. A frame first measures how far its anchor moved
# without reader input (an unanchored prepend or layout shift), then scrolls.
_READER = """async ({speed, deadline, firstTurnId}) => {
  const viewport = document.querySelector('#chatContainer');
  const surface = () => document.querySelector('#chatInner .conversation-surface');
  const t0 = performance.now();
  const stalls = [], jumps = [];
  let last = t0, stallMs = 0, stall = null, anchor = null, anchorTop = 0;
  const pickAnchor = () => {
    const rect = viewport.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return hit?.closest('[data-block-id], [data-turn-id]') || null;
  };
  return await new Promise(resolve => {
    const step = now => {
      const dt = Math.max(0, Math.min(64, now - last));
      last = now;
      if (anchor?.isConnected) {
        const moved = anchor.getBoundingClientRect().top - anchorTop;
        if (Math.abs(moved) > 1) jumps.push({at: now - t0, px: moved});
      }
      const first = surface()?.querySelector('[data-turn-id]');
      const reached = first?.dataset.turnId === firstTurnId && viewport.scrollTop <= 1;
      if (reached || now - t0 > deadline) {
        resolve({t0, elapsedMs: now - t0, reached, stallMs, stalls, jumps});
        return;
      }
      const before = viewport.scrollTop;
      viewport.scrollBy({top: -speed * dt / 1000, behavior: 'instant'});
      if (before - viewport.scrollTop < 0.5) {
        stallMs += dt;
        if (!stall) { stall = {at: now - t0, ms: 0}; stalls.push(stall); }
        stall.ms += dt;
      } else stall = null;
      anchor = pickAnchor();
      anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}"""


def _opaque(size: int) -> str:
    """Incompressible provider-signature-like text."""
    return base64.b64encode(os.urandom(size))[:size].decode()


def _agent_reply(prefix: str, index: int, rounds: int = 34) -> dict:
    identity = {'attemptId': f'{prefix}-attempt-{index}', 'taskId': f'{prefix}-task-{index}'}
    segments, tool_rounds = [], []
    for round_index in range(rounds):
        call_id = f'{prefix}-{index}-call-{round_index}'
        args = {'command': f'pytest tests/unit -k case_{round_index} ' + '-q ' * 200}
        output = f'Synthetic command output {round_index}\n' + 'evidence line\n' * 300
        tool_rounds.append({**identity, 'llmRound': round_index + 1, 'roundNum': round_index + 1,
                            'toolCallId': call_id, 'toolName': 'run_command', 'status': 'done',
                            'toolArgs': args, 'toolContent': output,
                            'results': [{'title': f'Result {round_index}', 'fetched': 'Synthetic'}],
                            'thinkingSignature': _opaque(24_000)})
        segments += [
            {'type': 'thinking', 'blockId': f'{prefix}-{index}-thinking-{round_index}',
             'text': 'Synthetic reasoning about the next step. ' * 8,
             'signature': _opaque(2_400), 'llmRound': round_index + 1, **identity},
            {'type': 'tool_use', 'blockId': f'{prefix}-{index}-tool-{round_index}', 'id': call_id,
             'name': 'run_command', 'input': args, 'result': {'status': 'done', 'content': output},
             'llmRound': round_index + 1, **identity},
        ]
    answer = f'Agent answer {index}. ' + 'Settled findings in readable prose. ' * 40
    segments.append({'type': 'text', 'blockId': f'{prefix}-{index}-answer', 'text': answer,
                     'terminal': True, 'deliverable': True})
    return {'content': answer, 'segments': segments, 'toolRounds': tool_rounds}


def _chat_projection(prefix: str, index: int, actor: str) -> dict:
    text = (f'Chat item {index}. ' + 'A short question about the history. ' * 4 if actor == 'human'
            else f'Chat item {index}. ' + '\n\n'.join(['Readable answer paragraph. ' * 14] * 3))
    return {'content': text, 'segments': [{'type': 'text', 'blockId': f'{prefix}-{index}:answer',
                                           'text': text, 'terminal': True, 'deliverable': True}]}


def _seed(context, live_server: str, cid: str, scenario: str) -> list[str]:
    turn_ids = []
    count = 28 if scenario == 'agent' else 240
    for index in range(count):
        actor = 'human' if index % 2 == 0 else 'assistant'
        projection = (_agent_reply(cid, index) if scenario == 'agent' and actor == 'assistant'
                      else _chat_projection(cid, index, actor))
        body = {'commandId': f'{cid}-{index}', 'actor': actor, 'kind': 'text', 'projection': projection}
        if index == 0:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(
            f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body, timeout=120_000)
        assert response.status == 200, response.text()
        turn_ids.append(response.json()['turn']['turnId'])
    return turn_ids


def _evidence(perf: dict, reader: dict, resources: list[dict], turn_ids: list[str]) -> dict:
    t0 = reader['t0']
    by_url = {}
    for entry in resources:
        by_url.setdefault(entry['name'], []).append(entry)
    requests = []
    cursors = [fetch['beforeOrdinal'] for fetch in perf['fetches']] + [0]
    for position, fetch in enumerate(perf['fetches']):
        timing = next((entry for entry in by_url.get(fetch['url'], [])
                       if abs(entry['startTime'] - fetch['start']) < 50), None)
        before = fetch['beforeOrdinal']
        newest = turn_ids[before - 1] if 0 < before <= len(turn_ids) else ''
        landed = perf['firstSeen'].get(newest)
        response_end = timing['responseEnd'] if timing else None
        height = fetch.get('clientHeight') or 1
        query = parse_qs(urlsplit(fetch['url']).query)
        requests.append({
            'beforeOrdinal': before, 'status': fetch.get('status'),
            'limit': int(query.get('limit', ['0'])[0]),
            'windowKiB': int(query.get('windowBytes', ['0'])[0]) // 1024,
            'turns': before - cursors[position + 1],
            'startMs': round(fetch['start'] - t0),
            'viewportsAboveAtStart': round((fetch.get('scrollTop') or 0) / height, 2),
            'loadedViewportsAboveAtStart': round(((fetch.get('scrollTop') or 0) + (fetch.get(
                'windowStart') or 0) * (fetch.get('averageTurnPx') or 0)) / height, 2),
            'unmountedLoadedTurnsAtStart': fetch.get('windowStart'),
            'networkMs': round(response_end - fetch['start']) if response_end else None,
            'decodedKiB': round(timing['decodedBodySize'] / 1024) if timing else None,
            'landingMs': round(landed - response_end) if landed and response_end else None,
        })
    window = [task for task in perf['longTasks']
              if t0 <= task['start'] <= t0 + reader['elapsedMs']]
    frames = [frame for frame in perf['frames']
              if t0 <= frame['start'] <= t0 + reader['elapsedMs']]
    reads = [request for request in requests if request['startMs'] >= 0]

    def median(key):
        values = sorted(request[key] for request in requests if request[key] is not None)
        return values[len(values) // 2] if values else None

    counts = {}
    for request in requests:
        counts[request['beforeOrdinal']] = counts.get(request['beforeOrdinal'], 0) + 1
    return {
        'reached': reader['reached'],
        'elapsedMs': round(reader['elapsedMs']),
        'stallMs': round(reader['stallMs']),
        'stalls': len(reader['stalls']),
        'longestStallMs': round(max((stall['ms'] for stall in reader['stalls']), default=0)),
        'historyRequests': len(requests),
        'duplicateRequests': sum(count - 1 for count in counts.values()),
        'historyKiB': sum(request['decodedKiB'] or 0 for request in requests),
        'medianTurnsPerRequest': median('turns'),
        'medianRequestKiB': median('decodedKiB'),
        'medianNetworkMs': median('networkMs'),
        'medianLandingMs': median('landingMs'),
        'medianViewportsAboveAtStart': sorted(
            request['viewportsAboveAtStart'] for request in reads)[len(reads) // 2] if reads else None,
        'medianLoadedViewportsAboveAtStart': sorted(
            request['loadedViewportsAboveAtStart'] for request in reads)[len(reads) // 2] if reads else None,
        'longTaskMs': round(sum(task['duration'] for task in window)),
        'longestTaskMs': round(max((task['duration'] for task in window), default=0)),
        'longFrames': len(frames),
        'jumps': len(reader['jumps']),
        'largestJumpPx': round(max((abs(jump['px']) for jump in reader['jumps']), default=0), 1),
        'requests': requests,
    }


@pytest.mark.parametrize('scenario', ['agent', 'chat'])
def test_scroll_up_history_is_ready_before_the_reader_arrives(
        browser, live_server, monkeypatch, tmp_path, scenario):
    from playwright.sync_api import expect
    from routes.conversation_sync_v3 import _service
    from tests.conftest import _dismiss_onboarding_modals
    import orjson

    cid = f'history-latency-{scenario}-' + uuid.uuid4().hex[:8]
    original_turn_page = _service.turn_page

    def production_latency_turn_page(conversation_id, *args, **kwargs):
        page = original_turn_page(conversation_id, *args, **kwargs)
        if conversation_id == cid:
            size = len(orjson.dumps(page)) / (1024 * 1024)
            time.sleep(HISTORY_BASE_LATENCY_S + HISTORY_LATENCY_PER_MIB_S * size)
        return page

    monkeypatch.setattr(_service, 'turn_page', production_latency_turn_page)
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, service_workers='block')
    context.add_init_script(f"localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            f"sessionStorage.setItem('tofu_activeConvId', '{cid}');")
    context.add_init_script(_OBSERVERS)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        turn_ids = _seed(context, live_server, cid, scenario)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        surface = page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]')
        expect(surface).to_be_visible(timeout=30_000)
        expect(surface.locator(f'[data-turn-id="{turn_ids[-1]}"]')).to_have_count(1, timeout=30_000)
        page.wait_for_timeout(1500)
        reader = page.evaluate(_READER, {'speed': READER_SPEED_PX_PER_S,
                                         'deadline': READER_DEADLINE_MS, 'firstTurnId': turn_ids[0]})
        page.wait_for_timeout(300)
        perf = page.evaluate('window.__historyPerf')
        resources = page.evaluate("""() => performance.getEntriesByType('resource')
          .filter(entry => entry.name.includes('/turns/history?'))
          .map(entry => ({name: entry.name, startTime: entry.startTime,
            responseEnd: entry.responseEnd, decodedBodySize: entry.decodedBodySize}))""")
        evidence = {'scenario': scenario, 'turns': len(turn_ids),
                    'readerPxPerSecond': READER_SPEED_PX_PER_S,
                    **_evidence(perf, reader, resources, turn_ids)}
        path = tmp_path / f'history-paging-{scenario}.json'
        path.write_text(json.dumps(evidence, indent=2) + '\n')
        print(f'History paging evidence ({scenario}): {path}\n' + json.dumps(
            {key: value for key, value in evidence.items() if key != 'requests'}))
        assert evidence['reached'], evidence
        assert not errors, errors
        # Work and anchoring budgets, not wall clock: one read per cursor, no
        # unexplained movement of the reader's anchor, and pages that bytes cut
        # short grow to carry several Turns instead of one round trip per Turn.
        assert evidence['duplicateRequests'] == 0, evidence
        assert evidence['largestJumpPx'] <= 2, evidence
        if scenario == 'agent':
            assert evidence['historyRequests'] <= len(turn_ids) // 3, evidence
        else:
            assert evidence['historyRequests'] <= -(-len(turn_ids) // 64), evidence
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
