"""Actual selected storage -> v4 SSE -> shipped application -> visible DOM.

Only the provider is a paced synthetic producer. Observations are diagnostic,
not latency admission: post-ACK notifications are not native fsync timestamps;
DOM mutations and animation frames are not proof of displayed pixels.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def _clock_alignment(page):
    """Keep all round-trip bounds; choose the narrowest interval, not one-way RPC."""
    samples = []
    for _ in range(9):
        start = time.perf_counter() * 1000
        browser = page.evaluate('performance.now()')
        end = time.perf_counter() * 1000
        samples.append({'lower': start - browser, 'upper': end - browser})
    best = min(samples, key=lambda row: row['upper'] - row['lower'])
    return {'samples': samples, 'offset': (best['lower'] + best['upper']) / 2,
            'uncertainty': (best['upper'] - best['lower']) / 2}


@pytest.mark.parametrize('history_count', [int(value) for value in
    os.environ.get('TOFU_LIVE_PIPELINE_HISTORY', '0,256').split(',')])
def test_storage_commit_to_browser_and_responsive_composer(browser, live_server, monkeypatch, tmp_path, history_count):
    import psutil
    from lib import turn_lifecycle as lifecycle
    from lib.storage import get_storage_client, storage_status
    from lib.storage.commit_events import subscribe_committed_events
    from lib.tasks_pkg.event_delivery import flush_task_events, detach_task_event_delivery
    from lib.tasks_pkg.manager import append_event, create_task, discard_task
    from lib.tasks_pkg.manager._delta_coalescer import TaskTextDeltaCoalescer
    import routes.conversation_sync_v3 as routes

    assert 0 <= history_count <= 65536, 'Explicit diagnostic history must be bounded'
    producer_kind = os.environ.get('TOFU_LIVE_PIPELINE_PRODUCER', 'python')
    assert producer_kind in {'python', 'native'}
    if producer_kind == 'native' and not os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE'):
        pytest.skip('explicit verified native archive required')
    cid = 'visual-pipeline-' + uuid4().hex[:12]
    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 900})
    context.add_init_script(path=str(ROOT / 'tests/support/live_pipeline_browser.js'))
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{cid}')")
    page = context.new_page()
    output = Path(os.environ.get('TOFU_LIVE_PIPELINE_OUTPUT', tmp_path)); output.mkdir(parents=True, exist_ok=True)
    errors, requests, commits, frames, reads, writes, produced = [], [], [], [], [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append(request.url) if f'/conversations/{cid}/' in request.url else None)
    client = get_storage_client(write=True)
    defaults = {'allowCreate': True, 'title': 'Live pipeline fixture', 'createdAt': 1, 'settings': {}}
    concurrency = int(os.environ.get('TOFU_LIVE_PIPELINE_CONVERSATIONS', '1'))
    assert 1 <= concurrency <= 64, 'Diagnostic workload must have 1–64 conversations'
    participants = []
    task = None
    producers = []
    start_gate = threading.Event()
    stop = threading.Event()
    failures = []
    native = None
    model_config = {'model': 'alpha' if producer_kind == 'native' else 'fixture-model',
        'maxTokens': 64, 'memoryEnabled': False, 'projectEnabled': False, 'searchMode': 'off'}

    def committed(carriers):
        at = time.perf_counter() * 1000
        for carrier in carriers:
            event = carrier.get('event', {})
            if event.get('conversationId') == cid:
                commits.append({'at': at, 'sequence': event['syncSeq']})

    unsubscribe = subscribe_committed_events(committed)
    original_frame, original_changes, original_command = routes._sse_frame, routes._service.changes, client.command

    def frame(event, **kwargs):
        result = original_frame(event, **kwargs)
        if event.get('conversationId') == cid:
            frames.append({'at': time.perf_counter() * 1000, 'sequence': event.get('syncSeq'),
                'kind': event['type'], 'bytes': len(result.encode())})
        return result

    def changes(conversation_id, *args, **kwargs):
        start = time.perf_counter() * 1000
        result = original_changes(conversation_id, *args, **kwargs)
        if conversation_id == cid:
            reads.append({'start': start, 'end': time.perf_counter() * 1000,
                          'events': len(result.get('events', []))})
        return result

    def command(operation, payload, *args, **kwargs):
        participant = next((index for index, row in enumerate(participants) if
            (operation == 'turn.event.record' and payload.get('attempt_id') == row['task']['_attemptId']) or
            (operation == 'event.append_batch' and any(event.get('task_id') == row['task']['id']
                for event in payload.get('events', [])))), None)
        if participant is None:
            return original_command(operation, payload, *args, **kwargs)
        # Serialization is instrumentation only, outside this client-ACK clock.
        size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())
        start = time.perf_counter() * 1000
        result = original_command(operation, payload, *args, **kwargs)
        writes.append({'participant': participant, 'operation': operation, 'start': start, 'end': time.perf_counter() * 1000, 'bytes': size,
                       'applied': result.get('applied'), 'events': result.get('task_event_count'),
                       'fieldBytes': {key: len(json.dumps(value, ensure_ascii=False).encode()) for key, value in payload.items()}})
        return result

    try:
        for index in range(history_count):
            lifecycle.append_settled_turn(cid, command_id=f'{cid}:history:{index}', actor='human' if index % 2 == 0 else 'assistant',
                projection={'content': f'Historical fixture {index}\n' + 'Retained evidence. ' * 32}, user_id=1,
                conversation_defaults=defaults)
        created = lifecycle.create_turn_pair(cid, command_id=cid, user_id=1,
            input_projection={'content': 'Measure actual delivery and preserve the draft'}, config={},
            conversation_defaults=defaults)
        task = create_task(cid, [], {**model_config, '_turnId': created['turn']['turnId'],
            '_attemptId': created['attempt']['attemptId']}, user_id=1, supersede=False)
        client.command('turn.attempt.bind', {'attempt_id': task['_attemptId'], 'user_id': 1, 'task_id': task['id']}, cid + ':bind')
        task['_workerStartedAt'] = time.time()
        participants.append({'task': task, 'conversation': cid, 'produced': produced})
        for index in range(1, concurrency):
            peer_cid = f'{cid}-peer-{index}'
            created = lifecycle.create_turn_pair(peer_cid, command_id=peer_cid, user_id=1,
                input_projection={'content': 'Concurrent pipeline fixture'}, config={}, conversation_defaults=defaults)
            peer = create_task(peer_cid, [], {**model_config, '_turnId': created['turn']['turnId'],
                '_attemptId': created['attempt']['attemptId']}, user_id=1, supersede=False)
            participants.append({'task': peer, 'conversation': peer_cid, 'produced': []})
            client.command('turn.attempt.bind', {'attempt_id': peer['_attemptId'], 'user_id': 1, 'task_id': peer['id']}, peer_cid + ':bind')
            peer['_workerStartedAt'] = time.time()
        monkeypatch.setattr(routes, '_sse_frame', frame)
        monkeypatch.setattr(routes._service, 'changes', changes)
        monkeypatch.setattr(client, 'command', command)
        if producer_kind == 'native':
            from tests.support.native_live_pipeline import NativePipelineProducer
            native = NativePipelineProducer(archive=os.environ['TOFU_NATIVE_TERMINAL_TEST_ARCHIVE'],
                participants=participants, stop=stop, directory=tmp_path,
                sample_resources=lambda boundary: sample_resources(boundary))
        # Explicit setup makes the asynchronous first-run dialog deterministic.
        # Dismiss through its real control before measuring trusted interactions.
        page.goto(live_server.rstrip('/') + '/?setup=1', wait_until='domcontentloaded')
        page.locator('#obCloseX').click(timeout=30000)
        page.locator('#onboardingModal').wait_for(state='detached')
        page.wait_for_function('cid => document.querySelector(".conversation-surface")?.dataset.conversationId === cid', arg=cid)
        page.wait_for_function('cid => livePipeline.rows.opened.some(row=>row.url.includes(cid))', arg=cid)
        page.evaluate('([cid,tid]) => livePipeline.start(cid,tid)', [cid, task['_turnId']])
        draft = page.locator('#userInput')
        draft.focus()
        # Separate calibration proves browser-native timing is working. The
        # deliberately delayed trusted key is outside the measured workload.
        page.evaluate("""() => {
          const input = document.querySelector('#userInput');
          input.addEventListener('keydown', () => {
            const until = performance.now() + 80;
            while (performance.now() < until) { /* bounded calibration */ }
          }, {once: true, capture: true});
        }""")
        page.keyboard.press('x')
        page.wait_for_function("livePipeline.rows.eventTimings.some(e=>e.composer && e.name==='keydown' && e.duration>=64)")
        page.evaluate("livePipeline.nativeTiming.calibrationDurationMs = Math.max(...livePipeline.rows.eventTimings.filter(e=>e.composer).map(e=>e.duration))")
        page.keyboard.press('Backspace')
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert draft.input_value() == ''
        alignment = _clock_alignment(page)
        storage_before = client.metrics(deadline=5)
        measured_start = time.perf_counter() * 1000
        cpu_start = time.process_time()
        child_start = native.joined_child_cpu_ms() if native is not None else None
        process = psutil.Process(os.getpid())
        resource_samples = []
        def sample_resources(boundary):
            resource_samples.append({'boundary': boundary, 'rssBytes': process.memory_info().rss,
                'threads': process.num_threads(), 'at': time.perf_counter()*1000})
        sample_resources('start')
        commits.clear(); frames.clear(); reads.clear(); requests.clear()
        expected = ''.join(f'PIPELINE_{index:05d} ' + 'Exact synthetic live evidence. ' * 16 + '\n\n' for index in range(60))

        def produce(participant):
            target = participant['task']
            deltas = None
            try:
                assert start_gate.wait(timeout=10), 'Concurrent producer gate did not open'
                if native is not None:
                    native.produce(participant)
                    flush_task_events(target, timeout=20)
                    participant['finishedAt'] = time.perf_counter() * 1000
                    return
                deltas = TaskTextDeltaCoalescer(target, append_event,
                    on_first_delta=lambda: None, on_after_delta=lambda: None)
                for index in range(60):
                    if stop.is_set():
                        return
                    content = f'PIPELINE_{index:05d} ' + 'Exact synthetic live evidence. ' * 16 + '\n\n'
                    start = time.perf_counter() * 1000
                    deltas.on_content(content)
                    if participant is participants[0] and index % 4 == 0:
                        sample_resources('stream')
                    participant['produced'].append({'index': index, 'start': start, 'end': time.perf_counter() * 1000})
                    if index < 40 and stop.wait(.04):
                        return
                deltas.close()
                participant['textCoalescing'] = deltas.stats
                target.update(status='done', finishReason='stop', streamState='provider_finished')
                append_event(target, {'type': 'done', 'finishReason': 'stop', 'usage': {}})
                flush_task_events(target, timeout=20)
                participant['finishedAt'] = time.perf_counter() * 1000
            except BaseException as exc:
                import traceback
                failures.append(''.join(traceback.format_exception(exc)))
            finally:
                if deltas is not None:
                    deltas.close()

        draft = page.locator('#userInput')
        draft.focus()
        for index, participant in enumerate(participants):
            producer = threading.Thread(target=produce, args=(participant,), name=f'live-pipeline-provider-{index}')
            producers.append(producer)
            producer.start()
        page.evaluate("livePipeline.beginInteractions()")
        start_gate.set()
        # Trusted keyboard events overlap real delivery and use the actual composer.
        page.wait_for_function('livePipeline.rows.dom.length > 0', timeout=30000)
        markers_at_typing_start = page.evaluate('livePipeline.rows.dom.length')
        assert 0 < markers_at_typing_start < 60, 'Text appeared only after the whole response'
        page.keyboard.type('Keep this draft ', delay=35)
        scroll_probe = None
        if history_count:
            scroll = page.locator('#chatContainer')
            scroll.hover()
            assert scroll.evaluate('(node)=>node.scrollTop') > 100, 'History did not provide a scrollable reader'
            page.mouse.wheel(0, -400)
            # The compositor may scroll before the passive wheel callback.
            # Observe actual successive positions after the input timestamp;
            # neither a pre-hover read nor the wheel-handler position is a
            # reliable pre-scroll baseline during concurrent text growth.
            page.wait_for_function('livePipeline.rows.wheels.length === 1')
            progress = page.wait_for_function('''() => {
              const rows = livePipeline.rows.scrolls, wheel = livePipeline.rows.wheels[0];
              const index = rows.findIndex((row, i) => i > 0 && row.at >= wheel.at && rows[i-1].top - row.top > 50);
              return index > 0 && {before: rows[index-1].top, afterUp: rows[index].top,
                beforeAt: rows[index-1].at, afterAt: rows[index].at};
            }''', timeout=5000)
            scroll_probe = {**progress.json_value(), 'markersAfterUp': page.evaluate('livePipeline.rows.dom.length')}
        page.keyboard.type('while evidence arrives', delay=35)
        if scroll_probe is not None:
            page.mouse.wheel(0, page.locator('#chatContainer').evaluate('(node)=>node.scrollHeight'))
            page.wait_for_function('() => { const node=document.querySelector("#chatContainer"); return node.scrollHeight-node.clientHeight-node.scrollTop < 8; }')
            scroll_probe['afterDown'] = page.locator('#chatContainer').evaluate('(node)=>node.scrollTop')
        assert not failures, '\n'.join(failures)
        page.wait_for_function('livePipeline.rows.dom.length === 60', timeout=30000)
        for producer in producers:
            producer.join(timeout=25)
        sample_resources('settled')
        producer_cpu_ms = (time.process_time() - cpu_start) * 1000
        child_end = native.joined_child_cpu_ms() if native is not None else None
        measured_end = time.perf_counter() * 1000
        assert not any(producer.is_alive() for producer in producers) and not failures, failures
        for participant in participants:
            receipt = participant['task']['_eventDeliveryReceipt']
            assert receipt['accepted'] == receipt['completed']
        storage_after = client.metrics(deadline=5)
        final_alignment = _clock_alignment(page)
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        page.evaluate('livePipeline.endInteractions()')
        observations = page.evaluate('({rows: livePipeline.rows, longTaskSupported: livePipeline.longTaskSupported, nativeTiming: livePipeline.nativeTiming})')
        mounted_turns = page.locator('[data-turn-id]').count()
        assert observations['rows']['inputs'] and all(row['trusted'] for row in observations['rows']['inputs'])
        assert draft.input_value() == 'Keep this draft while evidence arrives'
        assert draft.evaluate('(node)=>node===document.activeElement')
        turn = page.locator(f'[data-turn-id="{task["_turnId"]}"]')
        text = turn.inner_text()
        assert re.findall(r'PIPELINE_\d{5}', text) == re.findall(r'PIPELINE_\d{5}', expected)
        for participant in participants:
            durable = lifecycle.get_turn(participant['conversation'], participant['task']['_turnId'], user_id=1)
            assert durable['projection']['content'] == expected
            assert durable['status'] == 'completed'
        assert not any('/sync?' in url for url in requests), requests
        assert not errors, errors
        matched = {row['sequence'] for row in commits} & {row.get('sequence') for row in observations['rows']['wire']}
        assert matched and writes and frames
        before_reload = hashlib.sha256(expected.encode()).hexdigest()
        # Freeze the measured population before cold-read verification adds I/O.
        measured = {key: list(value) for key, value in {'commits': commits, 'frames': frames,
            'reads': reads, 'writes': writes, 'requests': requests}.items()}
        measured['participantWrites'] = measured['writes']
        measured['writes'] = [row for row in writes if row['participant'] == 0]
        page.screenshot(path=str(output / f'pipeline-{history_count}.png'))
        page.reload(wait_until='domcontentloaded')
        turn.get_by_text(re.compile('PIPELINE_00059')).wait_for()
        assert re.findall(r'PIPELINE_\d{5}', turn.inner_text()) == re.findall(r'PIPELINE_\d{5}', expected)
        status = storage_status()
        result = {'scope': __doc__, 'historyTurns': history_count, 'browser': browser.version,
            'fixtureCleanup': {'complete': False},
            'producerKind': producer_kind,
            'markersAtTypingStart': markers_at_typing_start, 'scrollProbe': scroll_probe,
            'nativeArchiveSha256': native.sha256 if native is not None else None,
            'joinedChildrenCpuMs': child_end - child_start if native is not None else None,
            'storage': {key: status.get(key) for key in ('backend', 'transport', 'pid', 'ready', 'state')},
            'projectionThrottleOverride': os.environ.get('TOFU_TURN_DELTA_RECORD_MS'),
            'processId': os.getpid(), 'applicationProcessCpuMs': producer_cpu_ms,
            'processResourceSamples': resource_samples,
            'concurrentConversations': concurrency, 'measuredEnd': measured_end,
            'participants': [{'index': index, 'produced': row['produced'], 'finishedAt': row['finishedAt'],
                'contentSha256': before_reload, 'deliveryReceipt': row['task']['_eventDeliveryReceipt'],
                'textCoalescing': row.get('textCoalescing'), 'nativeIngress': row.get('nativeIngress')}
                for index, row in enumerate(participants)],
            'mountedTurns': mounted_turns,
            'storageMetricsBefore': storage_before, 'storageMetricsAfter': storage_after,
            'alignmentBefore': alignment, 'alignmentAfter': final_alignment,
            'measuredStart': measured_start, 'produced': produced, **measured, 'browserObservations': observations,
            'matchedSequences': sorted(matched), 'contentSha256': before_reload, 'contentBytes': len(expected.encode()),
            'deliveryReceipt': task['_eventDeliveryReceipt'],
            'sourceSha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [Path(__file__), ROOT / 'tests/support/live_pipeline_browser.js',
                    ROOT / 'lib/turn_lifecycle.py', ROOT / 'lib/tasks_pkg/manager/_events.py',
                    ROOT / 'lib/tasks_pkg/manager/_delta_coalescer.py', ROOT / 'lib/tasks_pkg/event_delivery.py',
                    ROOT / 'tests/support/native_live_pipeline.py', ROOT / 'lib/tasks_pkg/native_execution.py',
                    ROOT / 'lib/agent_core/native_process.py',
                    ROOT / 'contracts/task_event_delivery_v1.json', ROOT / 'benchmarks/live_pipeline.py',
                    ROOT / 'routes/conversation_sync_v3.py', ROOT / 'static/vite/manifest.json', ROOT / 'static/styles.css']}}
        manifest = json.loads((ROOT / 'static/vite/manifest.json').read_text())
        assets = {name for entry in manifest.values() for name in [entry['file'], *entry.get('css', [])]}
        result['builtAssetsSha256'] = {name: hashlib.sha256((ROOT / 'static/vite' / name).read_bytes()).hexdigest() for name in sorted(assets)}
        # Preserve the raw population even when qualification rejects it.
        receipt_path = output / f'pipeline-{history_count}.json'
        receipt_path.write_text(json.dumps(result, indent=2, default=str) + '\n')
    except Exception as original_error:
        # Failure diagnostics are outside a qualified measurement and must
        # never replace its original exception or bypass owned cleanup.
        try:
            failure = page.evaluate('({rows: livePipeline.rows, nativeTiming: livePipeline.nativeTiming, scrollTop: document.querySelector("#chatContainer")?.scrollTop})')
            (output / f'pipeline-{history_count}-failure.json').write_text(json.dumps(failure, indent=2) + '\n')
            page.screenshot(path=str(output / f'pipeline-{history_count}-failure.png'))
        except Exception as diagnostic_error:
            original_error.add_note(f'Failure diagnostics unavailable: {diagnostic_error}')
        raise
    finally:
        stop.set()
        start_gate.set()
        for producer in producers:
            producer.join(timeout=25)
            assert not producer.is_alive(), 'Synthetic provider failed to stop'
        if native is not None:
            native.close()
        unsubscribe()
        page.close(); context.close()
        for participant in participants:
            target = participant['task']
            detach_task_event_delivery(target)
            lifecycle.abort_attempt(target['_attemptId'], user_id=1)
            discard_task(target['id'])
            if participant['conversation'] != cid:
                client.command('conversation.delete', {'conv_id': participant['conversation'], 'user_id': 1},
                    participant['conversation'] + ':delete')
        try:
            client.command('conversation.delete', {'conv_id': cid, 'user_id': 1}, cid + ':cleanup')
        except Exception as exc:
            detail = exc.to_payload() if callable(getattr(exc, 'to_payload', None)) else repr(exc)
            raise AssertionError(f'Owned fixture deletion failed: {detail}') from exc
    result['fixtureCleanup']['complete'] = True
    from benchmarks.live_pipeline import summarize
    result['summary'] = summarize(result)
    receipt_path.write_text(json.dumps(result, indent=2, default=str) + '\n')
