"""Sidebar conversation switch: trusted click to first transcript paint.

Real local app and Chromium. By default the conversations are synthetic
owner-scoped history seeded through the public API. When
``TOFU_SWITCH_PAINT_CAPTURED`` names a local directory of captured
``snap_<id>.json``/``hist_<id>.json`` documents, their Turns replace the
seeded Turns in the real /sync and history responses (route interception);
cursor, revision and owner scope still come from the isolated server.
Timings are diagnostics written to ``TOFU_SWITCH_PAINT_OUTPUT``; there are no
host-dependent thresholds. ``TOFU_SWITCH_PAINT_PROFILE=1`` adds CDP CPU
profiles of every switch.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual

_OBSERVER = r"""
(() => {
  const state = { longTasks: [], frames: [], clicks: [], elements: [], watch: null };
  window.__switchPaint = state;
  const observe = (type, map) => {
    try {
      new PerformanceObserver((list) => { for (const entry of list.getEntries()) map(entry); })
        .observe({ type, buffered: true });
    } catch (_error) { /* entry type unsupported by this engine */ }
  };
  observe('longtask', (entry) => state.longTasks.push({
    start: entry.startTime, duration: entry.duration }));
  observe('long-animation-frame', (entry) => state.frames.push({
    start: entry.startTime, duration: entry.duration, renderStart: entry.renderStart,
    styleAndLayoutStart: entry.styleAndLayoutStart, blockingDuration: entry.blockingDuration,
    scripts: Array.from(entry.scripts || [], (script) => ({
      start: script.startTime, duration: script.duration, invoker: script.invoker,
      invokerType: script.invokerType, sourceURL: script.sourceURL,
      sourceFunctionName: script.sourceFunctionName,
      sourceCharPosition: script.sourceCharPosition,
      forcedStyleAndLayoutDuration: script.forcedStyleAndLayoutDuration,
    })),
  }));
  observe('element', (entry) => state.elements.push({
    identifier: entry.identifier, renderTime: entry.renderTime }));
  document.addEventListener('pointerdown', (event) => {
    const item = event.target && event.target.closest ? event.target.closest('.conv-item') : null;
    state.clicks.push({ at: event.timeStamp, id: item ? item.dataset.convId || '' : '' });
  }, true);
  /* The Surface stamps data-dom-turn-count at the end of every commit, so an
   * attribute-filtered observer sees each commit without recording the build. */
  state.arm = (conversationId, label) => {
    const watch = { conversationId, label, armedAt: performance.now(), surfaceAt: null,
      domAt: null, paintAt: null, commits: 0, mountedTurns: 0, blocks: 0,
      commitsAfterPaint: 0, turnCounts: [] };
    state.watch = watch;
    const host = document.getElementById('chatInner');
    const check = () => {
      if (state.watch !== watch) return;
      const surface = host.querySelector(':scope > .conversation-surface');
      if (!surface || surface.dataset.conversationId !== conversationId) return;
      if (watch.domAt !== null) {
        watch.commitsAfterPaint += 1;
        watch.turnCounts.push(Number(surface.dataset.domTurnCount || 0));
        return;
      }
      if (watch.surfaceAt === null) watch.surfaceAt = performance.now();
      watch.commits += 1;
      if (!(Number(surface.dataset.domTurnCount || 0) > 0)) return;
      watch.domAt = performance.now();
      watch.mountedTurns = Number(surface.dataset.domTurnCount || 0);
      watch.blocks = surface.querySelectorAll('[data-block-id]').length;
      const turns = surface.querySelectorAll('[data-turn-id]');
      const last = turns[turns.length - 1];
      const texts = last ? last.querySelectorAll('p, li, summary, .conversation-turn-header') : [];
      for (let index = 0; index < texts.length && index < 64; index += 1) {
        texts[index].setAttribute('elementtiming', `switch:${label}`);
      }
      const channel = new MessageChannel();
      channel.port1.onmessage = () => { watch.paintAt = performance.now(); };
      channel.port2.postMessage(0);
    };
    const observer = new MutationObserver(check);
    observer.observe(host, { childList: true, subtree: true, attributes: true,
      attributeFilter: ['data-dom-turn-count'] });
    watch.disconnect = () => observer.disconnect();
    check();
  };
  state.collect = (settleUntil) => {
    const watch = state.watch;
    const click = [...state.clicks].reverse().find((item) => item.id === watch.conversationId);
    const from = click ? click.at : watch.armedAt;
    const until = settleUntil;
    const sync = performance.getEntriesByType('resource')
      .filter((entry) => entry.name.includes(`/conversations/${encodeURIComponent(watch.conversationId)}/sync?`)
        && entry.startTime >= from - 1)
      .map((entry) => ({ start: entry.startTime, responseStart: entry.responseStart,
        responseEnd: entry.responseEnd, encoded: entry.encodedBodySize, decoded: entry.decodedBodySize }));
    const within = (start, end) => (item) => item.start + (item.duration || 0) > start && item.start < end;
    const paint = watch.paintAt ?? until;
    const elementPaint = state.elements.filter((item) => item.identifier === `switch:${watch.label}`)
      .map((item) => item.renderTime).filter((time) => time > 0);
    watch.disconnect?.();
    return {
      conversationId: watch.conversationId, label: watch.label, clickAt: from,
      surfaceMs: watch.surfaceAt === null ? null : watch.surfaceAt - from,
      domMs: watch.domAt === null ? null : watch.domAt - from,
      paintMs: watch.paintAt === null ? null : watch.paintAt - from,
      elementPaintMs: elementPaint.length ? Math.min(...elementPaint) - from : null,
      surfaceCommits: watch.commits, mountedTurns: watch.mountedTurns, mountedBlocks: watch.blocks,
      commitsAfterPaint: watch.commitsAfterPaint, turnCountsAfterPaint: watch.turnCounts,
      sync,
      longTasksToPaint: state.longTasks.filter(within(from, paint)),
      longTasksAfterPaint: state.longTasks.filter(within(paint, until)),
      framesToPaint: state.frames.filter(within(from, paint)),
      framesAfterPaint: state.frames.filter(within(paint, until)),
    };
  };
})();
"""


_WORDS = ('storage', 'snapshot', 'window', 'render', 'cursor', 'replay', 'turn', 'layout', 'budget',
          'projection', 'reducer', 'surface', 'markdown', 'stream', 'commit', 'paint', 'latency',
          'catalog', 'sidebar', 'history', 'overlay', 'residency', 'anchor', 'viewport')
_HAN = '存储快照窗口渲染游标重放回合布局预算投影归约界面流式提交绘制延迟目录侧栏历史覆盖锚点视口'


def _prose(seed, chars):
    """Deterministic Markdown prose with lists, emphasis and a code fence."""
    words, index, lines = [], seed, []
    while sum(len(line) + 1 for line in lines) < chars:
        index = (index * 1103515245 + 12345) % 2147483648
        words.append(_WORDS[index % len(_WORDS)])
        if len(words) == 14:
            kind = index % 9
            sentence = ' '.join(words)
            words = []
            if kind == 0:
                lines.append(f'- **{sentence[:18]}** {sentence}')
            elif kind == 1:
                lines.append(f'```python\nvalue_{index % 97} = "{sentence}"\nprint(value_{index % 97})\n```')
            elif kind == 2:
                lines.append(f'| {sentence[:20]} | `{sentence[20:40]}` |\n|---|---|\n| a | b |')
            else:
                lines.append(f'{sentence.capitalize()} with `{sentence[:12]}`.')
            lines.append('')
    return '\n'.join(lines)


def _han(seed, chars):
    return ''.join(_HAN[(seed * 7 + offset * 13) % len(_HAN)] for offset in range(chars))


def _blob(seed, chars):
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    return ''.join(alphabet[(seed * 31 + offset * 17) % 64] for offset in range(chars))


def _agentic_reply(seed, rounds, thinking_chars, tool_chars, answer):
    """One settled agentic reply shaped like long production tool loops."""
    identity = {'attemptId': f'attempt-{seed}', 'taskId': f'task-{seed}'}
    segments, tool_rounds, api_rounds, activity = [], [], [], []
    tools = ('read_files', 'run_command', 'write_file', 'search_files')
    for index in range(rounds):
        call_id = f'call-{seed}-{index}'
        name = tools[index % len(tools)]
        args = json.dumps({'path': f'src/module_{index}.py', 'content': _prose(seed + index, tool_chars // 3)})
        output = _prose(seed * 3 + index, tool_chars)
        tool_rounds.append({
            **identity, 'llmRound': index, 'roundNum': index + 1, 'toolCallId': call_id,
            'toolName': name, 'status': 'done', 'toolArgs': args, 'toolContent': output,
            'query': f'{name} src/module_{index}.py', 'thinkingSignature': _blob(seed + index, 4600),
            'results': [{'title': name, 'snippet': output[:600], 'source': 'Project', 'fetched': True}],
            'toolResultEvidence': {'contractVersion': 'tofu.tool-result-evidence/v1', 'status': 'ok',
                                   'resultContractVersion': 'tofu.tool-result/v2', 'projectionKind': 'text',
                                   'envelopeBytes': len(output) + 400, 'evidenceId': f'ev_{seed}_{index}',
                                   'executionStatus': 'ok', 'rawBytes': len(output),
                                   'visibleBytes': len(output), 'truncated': False},
        })
        segments.append({**identity, 'type': 'thinking', 'blockId': f'thinking-{seed}-{index}',
                         'llmRound': index, 'text': _prose(seed * 5 + index, thinking_chars),
                         'translatedText': _han(seed + index, thinking_chars // 3),
                         'signature': _blob(seed * 11 + index, 7000)})
        if index % 5 == 0:
            segments.append({**identity, 'type': 'text', 'blockId': f'text-{seed}-{index}', 'llmRound': index,
                             'text': _prose(seed * 9 + index, 600), 'translatedText': _han(seed * 2 + index, 200)})
        segments.append({**identity, 'type': 'tool_use', 'blockId': f'tool-{seed}-{index}', 'id': call_id,
                         'name': name, 'llmRound': index, 'roundNum': index + 1,
                         'input': json.loads(args), 'result': {'status': 'done', 'content': output}})
        api_rounds.append({'round': index + 1, 'tag': f'R{index + 1}', 'model': 'synthetic-model',
                           'usage': {'prompt_tokens': 600000 + index, 'completion_tokens': 900 + index},
                           'promptAdmission': {'action': 'admit', 'messageCount': 200 + index,
                                               'totalTokens': 640000 + index, 'detail': _prose(index, 500)}})
        activity.append({**identity, 'id': f'activity:{seed}:{index}', 'kind': 'tool', 'toolCallId': call_id,
                         'spanId': f'tool:{seed}:{index}', 'occurredAt': 1000 + index, 'severity': 'info',
                         'count': 1, 'llmRound': index, 'anchorToolRoundNum': index + 1, 'durationMs': 500,
                         'toolName': name, 'roundNum': index + 1, 'status': 'succeeded', 'seq': index,
                         'summary': f'{name} succeeded', 'summaryKey': 'activity.tool.succeeded',
                         'summaryArgs': {'tool': name}, 'startedAt': 1000 + index, 'endedAt': 1500 + index})
    segments.append({**identity, 'type': 'text', 'blockId': f'answer-{seed}', 'text': answer,
                     'translatedText': _han(seed, len(answer) // 3), 'terminal': True, 'deliverable': True})
    return {'content': answer, 'segments': segments, 'toolRounds': tool_rounds, 'apiRounds': api_rounds,
            'activityTimeline': {'blockId': 'activity-timeline', 'version': 1, 'entries': activity},
            'model': 'synthetic-model'}


def _synthetic_conversations():
    """Three histories: one 2 MB tool loop, one 0.7 MB reply, 24 mixed Turns."""
    long_answer = 'Long tool loop answer. ' + _prose(7, 2400)
    mid_answer = 'Mid reply answer. ' + _prose(8, 1800)
    many = []
    for index in range(12):
        many.append(('human', {'content': f'Mixed question {index}. ' + _prose(20 + index, 400)}))
        # The newest answer spans several viewports, so residency retires the
        # Turns above it, as in long coordinator conversations.
        answer = f'Mixed answer {index}. ' + _prose(40 + index, 16000 if index == 11 else 900)
        many.append(('assistant', _agentic_reply(30 + index, 6, 1200, 900, answer)))
    return [
        ('synthetic-long', [('human', {'content': 'Long tool loop question'}),
                            ('assistant', _agentic_reply(1, 86, 3000, 1600, long_answer))], 'Long tool loop answer'),
        ('synthetic-mid', [('human', {'content': 'Mid reply question'}),
                           ('assistant', _agentic_reply(2, 18, 3500, 2400, mid_answer))], 'Mid reply answer'),
        ('synthetic-many', many, 'Mixed answer 11'),
    ]


def _captured_documents():
    directory = os.environ.get('TOFU_SWITCH_PAINT_CAPTURED', '').strip()
    if not directory:
        return []
    documents = []
    for path in sorted(Path(directory).glob('snap_*.json')):
        source_id = path.name[len('snap_'):-len('.json')]
        history = path.with_name(f'hist_{source_id}.json')
        documents.append((source_id, path.read_text(), history.read_text() if history.exists() else None))
    return documents


def _install_captured_routes(context, seeded, captured):
    """Serve captured Turns inside the isolated server's own sync envelope."""
    isolated_heads = {}
    owned_keys = ('ok', 'contract', 'scope', 'conversationId', 'conversationRevision',
                  'syncSeq', 'cursor', 'serverBootId', 'heartbeatIntervalMs', 'settings')

    def handlers(conversation_id, source_id, snapshot_text, history_text):
        def serve_snapshot(route, _request=None):
            response = route.fetch()
            isolated = response.json()
            merged = json.loads(snapshot_text.replace(source_id, conversation_id))
            for key in owned_keys:
                if key in isolated:
                    merged[key] = isolated[key]
            isolated_heads[conversation_id] = isolated
            headers = {name: value for name, value in response.headers.items()
                       if name.lower() not in ('content-length', 'content-encoding', 'transfer-encoding')}
            route.fulfill(status=response.status, headers=headers, body=json.dumps(merged))

        def serve_history(route, _request=None):
            if not history_text:
                route.continue_()
                return
            query = dict(item.split('=', 1) for item in route.request.url.split('?', 1)[1].split('&'))
            page = json.loads(history_text.replace(source_id, conversation_id))
            head = isolated_heads.get(conversation_id, {})
            page['syncSeq'] = int(query['syncSeq'])
            page['conversationRevision'] = head.get('conversationRevision', page['conversationRevision'])
            page['cursor'] = head.get('cursor', page['cursor'])
            route.fulfill(status=200, content_type='application/json', body=json.dumps(page))

        return serve_snapshot, serve_history

    totals = {}
    for conversation_id, (source_id, snapshot_text, history_text) in zip(seeded, captured):
        serve_snapshot, serve_history = handlers(conversation_id, source_id, snapshot_text, history_text)
        escaped = re.escape(conversation_id)
        context.route(re.compile(rf'.*/api/v4/conversations/{escaped}/sync\?.*'), serve_snapshot)
        context.route(re.compile(rf'.*/api/v4/conversations/{escaped}/turns/history\?.*'), serve_history)
        totals[conversation_id] = int(json.loads(snapshot_text)['turnWindow']['totalTurns'])

    def serve_catalog(route, _request=None):
        """The catalog counts every captured Turn, as production rows do."""
        headers = {name: value for name, value in route.request.headers.items()
                   if name.lower() not in ('if-none-match', 'if-modified-since')}
        response = route.fetch(headers=headers)
        if response.status != 200:
            route.fulfill(response=response)
            return
        body = response.json()

        def rewrite(node):
            if isinstance(node, list):
                for item in node:
                    rewrite(item)
            elif isinstance(node, dict):
                if node.get('id') in totals:
                    for key in ('messageCount', 'msgCount', 'msg_count'):
                        if key in node:
                            node[key] = totals[node['id']]
                for value in node.values():
                    rewrite(value)

        rewrite(body)
        headers = {name: value for name, value in response.headers.items()
                   if name.lower() not in ('content-length', 'content-encoding', 'transfer-encoding')}
        route.fulfill(status=response.status, headers=headers, body=json.dumps(body))

    context.route(re.compile(r'.*/api/v4/conversations(\?.*)?$'), serve_catalog)


def _measure(page, conversation_id, label, profiler=None, output=None, settle_ms=1500, writes=None):
    from tests.conftest import _dismiss_onboarding_modals

    _dismiss_onboarding_modals(page)
    written = len(writes) if writes is not None else 0
    page.evaluate('([id, label]) => window.__switchPaint.arm(id, label)', [conversation_id, label])
    trace = output is not None and os.environ.get('TOFU_SWITCH_PAINT_TRACE') == '1'
    if trace:
        page.context.browser.start_tracing(page=page, path=str(output / f'{label}.trace.json'), categories=[
            'devtools.timeline', 'disabled-by-default-devtools.timeline',
            'disabled-by-default-devtools.timeline.stack', 'blink.user_timing'])
    if profiler is not None:
        profiler.send('Profiler.start')
    page.locator(f'.conv-item[data-conv-id="{conversation_id}"]').click(position={'x': 12, 'y': 12})
    page.wait_for_function('() => window.__switchPaint.watch.paintAt !== null', timeout=60000)
    page.wait_for_timeout(settle_ms)
    result = page.evaluate('() => window.__switchPaint.collect(performance.now())')
    result['writes'] = list(writes[written:]) if writes is not None else []
    if trace:
        page.context.browser.stop_tracing()
    if profiler is not None:
        profile = profiler.send('Profiler.stop')['profile']
        if output is not None:
            (output / f'{label}.cpuprofile').write_text(json.dumps(profile))
    return result


def test_conversation_switch_first_paint(browser, live_server):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    captured = _captured_documents()
    output_dir = os.environ.get('TOFU_SWITCH_PAINT_OUTPUT', '').strip()
    output = Path(output_dir) if output_dir else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    iterations = int(os.environ.get('TOFU_SWITCH_PAINT_ITERATIONS', '1'))
    profile = os.environ.get('TOFU_SWITCH_PAINT_PROFILE') == '1'
    prefix = 'switch-paint-' + uuid.uuid4().hex[:10]
    startup = prefix + '-start'
    synthetic = [] if captured else _synthetic_conversations()
    sources = [source for source, _text, _history in captured] or [name for name, _turns, _marker in synthetic]
    targets = [f'{prefix}-c{index}' for index in range(len(sources))]
    request_context = browser.new_context()

    def append(cid, command, actor, projection, create=False):
        body = {'commandId': f'{prefix}-{command}', 'actor': actor, 'kind': 'text', 'projection': projection}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = request_context.request.post(
            f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    samples = []
    try:
        append(startup, 'start', 'human', {'content': 'Switch paint start'}, create=True)
        if captured:
            for cid in targets:
                append(cid, cid + '-input', 'human', {'content': 'Switch paint question'}, create=True)
        for cid, (_name, turns, _marker) in zip(targets, synthetic):
            for index, (actor, projection) in enumerate(turns):
                append(cid, f'{cid}-{index}', actor, projection, create=index == 0)
        for iteration in range(iterations):
            context = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 900})
            context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{startup}');")
            context.add_init_script(_OBSERVER)
            if captured:
                _install_captured_routes(context, targets, captured)
            page = context.new_page()
            errors, writes = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: writes.append(
                f"{request.method} {request.url.split('/api/', 1)[-1][:160]}")
                if '/api/' in request.url and (request.method != 'GET' or (
                    'conversations' in request.url and not any(part in request.url for part in (
                        '/sync?', '/events?', '/turns/history', '/tool-results/')))) else None)
            profiler = None
            if profile:
                profiler = context.new_cdp_session(page)
                profiler.send('Profiler.enable')
                profiler.send('Profiler.setSamplingInterval', {'interval': 200})
            try:
                page.goto(live_server, wait_until='domcontentloaded')
                _dismiss_onboarding_modals(page)
                for cid in targets:
                    expect(page.locator(f'.conv-item[data-conv-id="{cid}"]')).to_have_count(1, timeout=20000)
                expect(page.locator('#chatInner')).to_contain_text('Switch paint start', timeout=20000)
                page.wait_for_timeout(1000)
                for phase in ('cold', 'warm'):
                    if phase == 'warm':
                        # Another conversation changes, as busy agents do, and
                        # the catalog revalidates (tab return, reconnect or any
                        # conversation's invalidation all reload it).
                        append(startup, f'start-{iteration}', 'assistant', {'content': 'Elsewhere'})
                        with page.expect_response(re.compile(r'.*/api/v4/conversations(\?.*)?$')):
                            page.evaluate("() => window.dispatchEvent(new Event('online'))")
                        page.wait_for_timeout(500)
                    for index, cid in enumerate(targets):
                        label = f'i{iteration}-{phase}-c{index}'
                        result = _measure(page, cid, label, profiler, output, writes=writes)
                        result.update(iteration=iteration, phase=phase, target=index, source=sources[index])
                        samples.append(result)
                assert not errors, errors
            finally:
                context.close()
    finally:
        for cid in (startup, *targets):
            request_context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        request_context.close()
    if output is not None:
        # Written before the budget assertions so failed runs keep evidence.
        (output / 'samples.json').write_text(json.dumps({
            'browser': browser.version, 'scope': __doc__, 'samples': samples}, indent=2) + '\n')
    for sample in samples:
        assert sample['paintMs'] is not None, sample
        assert sample['mountedTurns'] > 0, sample
        # Residency may retire Turns once; it never remounts and re-retires
        # them on later renders without reader demand.
        counts = [sample['mountedTurns'], *sample['turnCountsAfterPaint']]
        assert not any(counts[i] > counts[j] < counts[k] for i in range(len(counts))
                       for j in range(i + 1, len(counts)) for k in range(j + 1, len(counts))), sample
        if sample['phase'] == 'warm':
            # A warm TurnStore resumes its ordered stream; an unchanged
            # conversation is never downloaded again after a catalog refresh.
            assert sample['sync'] == [], sample
