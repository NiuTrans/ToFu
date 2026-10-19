"""Validate and summarize real-application live-pipeline diagnostic receipts.

Entry: summarize(receipt), or python -m benchmarks.live_pipeline receipt.json.
Producer: tests/visual/test_visual_live_pipeline.py. Clocks include observation
overhead; bytes are logical command/SSE bytes, never physical WAL/storage I/O.
"""
from __future__ import annotations

import json
import math
import statistics
import sys


def _distribution(values):
    ordered = sorted(values)
    if not ordered or any(not math.isfinite(value) for value in ordered):
        raise ValueError('Missing or invalid measured population')
    return {'count': len(ordered), 'median': statistics.median(ordered),
            'p95': ordered[math.ceil(len(ordered) * .95) - 1], 'max': ordered[-1]}


def _wal_observation(receipt):
    before = receipt.get('storageMetricsBefore', {}).get('walWriteBuffer')
    after = receipt.get('storageMetricsAfter', {}).get('walWriteBuffer')
    if before is None and after is None:
        return None
    if before is None or after is None:
        raise ValueError('Incomplete WAL counter window')
    if any(row['failed'] or row['retainedFrameBytes'] or row['queuedFrames'] != row['writtenFrames']
           for row in (before, after)):
        return {'qualified': False, 'reason': 'WAL window has failed or in-flight frames'}
    if any(after[key] < before[key] for key in ('writtenBytes', 'writtenFrames')):
        raise ValueError('WAL counters reset across the window')
    return {'qualified': True, 'writtenBytesDelta': after['writtenBytes'] - before['writtenBytes'],
            'writtenFramesDelta': after['writtenFrames'] - before['writtenFrames'],
            'scope': 'Observed process WAL-buffer writes including background work; not per-command attribution or stable disk footprint.'}


def _process_resource_observation(receipt):
    samples = receipt.get('processResourceSamples')
    if samples is None:
        return None
    if (len(samples) < 3 or samples[0]['boundary'] != 'start'
            or samples[-1]['boundary'] != 'settled'
            or not any(row['boundary'] == 'stream' for row in samples)):
        raise ValueError('Incomplete process resource samples')
    previous = receipt['measuredStart']
    for row in samples:
        if (row['boundary'] not in {'start', 'stream', 'settled'}
                or not math.isfinite(row['at']) or not previous <= row['at'] <= receipt['measuredEnd']
                or any(type(row[key]) is not int or row[key] <= 0 for key in ('rssBytes', 'threads'))):
            raise ValueError('Invalid process resource sample')
        previous = row['at']
    return {'samples': len(samples), 'startRssBytes': samples[0]['rssBytes'],
        'peakObservedRssBytes': max(row['rssBytes'] for row in samples),
        'settledRssBytes': samples[-1]['rssBytes'], 'startThreads': samples[0]['threads'],
        'peakObservedThreads': max(row['threads'] for row in samples),
        'settledThreads': samples[-1]['threads'],
        'scope': 'Sampled whole-application process RSS and threads; not allocation attribution, OS peak RSS or device-wide memory.'}


def _native_ingress_observation(receipt):
    """Qualify actual native callbacks independently of provider chunk boundaries."""
    kind = receipt.get('producerKind', 'python')
    if kind == 'python':
        return None
    pin = receipt.get('nativeArchiveSha256')
    child_cpu = receipt.get('joinedChildrenCpuMs')
    if (kind != 'native' or not isinstance(pin, str) or len(pin) != 64
            or any(char not in '0123456789abcdef' for char in pin)
            or type(child_cpu) not in (int, float) or not math.isfinite(child_cpu) or child_cpu < 0):
        raise ValueError('Invalid native ingress identity or child CPU observation')
    partial = receipt.get('markersAtTypingStart')
    if type(partial) is not int or not 0 < partial < len(receipt['produced']):
        raise ValueError('Native text was not observed before completion')
    frames = 0
    for participant in receipt['participants']:
        witness = participant.get('nativeIngress')
        if (not witness or type(witness.get('providerRequests')) is not int or witness['providerRequests'] != 1
                or witness.get('clientJoined') is not True or not witness.get('frames')):
            raise ValueError('Incomplete native ingress or client cleanup')
        previous, total = receipt['measuredStart'], 0
        for sequence, row in enumerate(witness['frames'], 1):
            if (type(row['sequence']) is not int or row['sequence'] != sequence or row['channel'] != 'content'
                    or type(row['bytes']) is not int or row['bytes'] <= 0
                    or not math.isfinite(row['at']) or not previous <= row['at'] <= participant['finishedAt']):
                raise ValueError('Invalid native text order, bytes or clock')
            total += row['bytes']
            previous = row['at']
        if total != receipt['contentBytes'] or witness.get('textSha256') != receipt['contentSha256']:
            raise ValueError('Native text bytes differ from accepted content')
        frames += len(witness['frames'])
    return {'participants': len(receipt['participants']), 'textFrames': frames,
        'archiveSha256': pin, 'observedJoinedChildrenCpuMs': child_cpu,
        'scope': 'Real native process through run_native_task and the common writer; paced loopback provider. Child CPU is process-wide waited-child usage, not per-frame attribution, real inference or public ROOT admission.'}


def _concurrent_workload(receipt):
    """Qualify the whole producer cohort before interpreting shared CPU/WAL."""
    keys = ('concurrentConversations', 'participants', 'participantWrites', 'measuredEnd')
    if not any(key in receipt for key in keys):
        return None  # Older single-conversation receipts have no cohort evidence.
    if not all(key in receipt for key in keys):
        raise ValueError('Incomplete concurrent workload evidence')
    count, participants = receipt['concurrentConversations'], receipt['participants']
    if type(count) is not int or count < 1 or [row['index'] for row in participants] != list(range(count)):
        raise ValueError('Incomplete concurrent participant population')
    begin, end = receipt['measuredStart'], receipt['measuredEnd']
    if not all(math.isfinite(value) for value in (begin, end)) or end <= begin:
        raise ValueError('Invalid concurrent measurement window')
    first, finished = [], []
    for row in participants:
        produced = row['produced']
        if [item['index'] for item in produced] != list(range(len(receipt['produced']))) or not produced:
            raise ValueError('Concurrent producer evidence is incomplete')
        previous = begin
        for item in produced:
            if not all(math.isfinite(item[key]) for key in ('start', 'end')) or not previous <= item['start'] <= item['end'] <= end:
                raise ValueError('Concurrent producer escaped its measurement window')
            previous = item['end']
        if not math.isfinite(row['finishedAt']) or not previous <= row['finishedAt'] <= end:
            raise ValueError('Concurrent completion escaped its measurement window')
        delivery = row['deliveryReceipt']
        if delivery['accepted'] <= 0 or delivery['accepted'] != delivery['completed']:
            raise ValueError('Concurrent producer has unsettled delivery')
        if row['contentSha256'] != receipt['contentSha256']:
            raise ValueError('Concurrent producer content differs')
        first.append(produced[0]['start']); finished.append(row['finishedAt'])
    overlap = min(finished) - max(first)
    if overlap <= 0 or participants[0]['produced'] != receipt['produced']:
        raise ValueError('Concurrent cohort did not overlap or primary evidence differs')
    writes = receipt['participantWrites']
    if {row['participant'] for row in writes} != set(range(count)):
        raise ValueError('Concurrent command population is incomplete')
    if [row for row in writes if row['participant'] == 0] != receipt['writes']:
        raise ValueError('Primary command evidence differs')
    for row in writes:
        if not all(math.isfinite(row[key]) for key in ('start', 'end', 'bytes')) or not begin <= row['start'] <= row['end'] <= end or row['bytes'] <= 0:
            raise ValueError('Concurrent command escaped its measurement window')
    return {'conversations': count, 'overlapMs': overlap, 'completeWindowMs': end - begin,
            'commandCount': len(writes), 'commandLogicalBytes': sum(row['bytes'] for row in writes),
            'scope': 'All producer completions and process CPU/WAL; one foreground SSE/DOM subscriber, no peer browser visibility claim.'}



def _interaction_observation(receipt, begin, end):
    """Summarize thresholded browser timings without imputing missing entries."""
    observation = receipt['browserObservations']
    timing = observation.get('nativeTiming')
    if timing is None:
        return None  # Historical receipts never acquire measurements retroactively.
    if not timing['supported']:
        return {'qualified': False, 'reason': 'Event Timing is unsupported'}
    if timing.get('error') or timing['droppedEntries']:
        raise ValueError('Native interaction observation failed or dropped entries')
    if (timing['thresholdMs'] != 16 or timing['durationQuantumMs'] != 8
            or type(timing['calibrationDurationMs']) not in (int, float)
            or not math.isfinite(timing['calibrationDurationMs'])
            or timing['calibrationDurationMs'] < 64):
        raise ValueError('Missing native interaction calibration or measurement policy')
    start, finish = timing['startedAt'], timing['endedAt']
    if not all(math.isfinite(value) for value in [start, finish]) or start >= finish:
        raise ValueError('Invalid native interaction observation window')
    before, after = timing['keyCountBefore'], timing['keyCountAfter']
    if any(type(value) is not int or value < 0 for value in [before, after]) or after <= before:
        raise ValueError('Missing browser-native keyboard census')
    rows = observation['rows']
    keys = [row for row in rows['keydowns'] if start <= row['at'] <= finish]
    if len(keys) != after - before or any(not row['trusted'] for row in keys):
        raise ValueError('Incomplete or untrusted keyboard population')
    stream_keys = [row for row in keys if begin <= row['at'] <= end]
    if not stream_keys:
        raise ValueError('Native keyboard interaction did not overlap streaming')
    events = [row for row in rows['eventTimings'] if row['composer'] and begin <= row['at'] <= end]
    groups = {}
    for row in events:
        if (any(not math.isfinite(row[key]) for key in ['at', 'duration', 'processingStart', 'processingEnd'])
                or row['duration'] < timing['thresholdMs'] or row['duration'] % 8
                or not row['at'] <= row['processingStart'] <= row['processingEnd']
                or type(row['interactionId']) is not int or row['interactionId'] < 0):
            raise ValueError('Invalid native Event Timing entry')
        if row['interactionId']:
            groups[row['interactionId']] = max(groups.get(row['interactionId'], 0), row['duration'])
    scroll = receipt.get('scrollProbe')
    scroll_observation = None
    if scroll is not None:
        wheels = [row for row in rows['wheels'] if start <= row['at'] <= finish]
        if (len(wheels) != 2 or not all(row['trusted'] for row in wheels)
                or not wheels[0]['deltaY'] < 0 < wheels[1]['deltaY']
                or not scroll['before'] - scroll['afterUp'] > 50
                or not scroll['afterDown'] > scroll['afterUp']
                or not 0 < scroll['markersAfterUp'] < len(receipt['produced'])):
            raise ValueError('Missing trusted scroll progress during streaming')
        frames = [row['frame'] - row['at'] for row in rows['scrolls']
                  if 'frame' in row and begin <= row['at'] <= end]
        if not frames or min(frames) < 0:
            raise ValueError('Missing scroll animation observations')
        scroll_observation = {'trustedWheelCount': len(wheels),
                              'scrollHandlerToFrameMs': _distribution(frames)}
    return {'qualified': True, 'thresholdMs': timing['thresholdMs'], 'durationQuantumMs': 8,
        'calibrationDurationMs': timing['calibrationDurationMs'],
        'trustedKeyDownCount': len(keys), 'streamTrustedKeyDownCount': len(stream_keys),
        'reportedEventCount': len(events), 'reportedInteractionCount': len(groups),
        'reportedInteractionDurationMs': _distribution(list(groups.values())) if groups else None,
        'reportedEventInputDelayMs': _distribution([row['processingStart'] - row['at'] for row in events]) if events else None,
        'reportedEventProcessingMs': _distribution([row['processingEnd'] - row['processingStart'] for row in events]) if events else None,
        'scroll': scroll_observation,
        'interpretation': 'Thresholded browser Event Timing, not full-page INP or physical-pixel latency. '
            'Unreported fast interactions have no imputed duration; scroll frame clocks are separate.'}


def summarize(receipt):
    """Refuse partial evidence; never turn missing stages into zero latency."""
    if 'fixtureCleanup' in receipt and receipt['fixtureCleanup'].get('complete') is not True:
        raise ValueError('Owned fixture cleanup did not complete')
    storage = receipt['storage']
    if not storage['ready'] or storage['backend'] not in {'tofudb', 'sqlite', 'postgres'}:
        raise ValueError('Selected storage was not ready')
    before, after = receipt['alignmentBefore'], receipt['alignmentAfter']
    low = min(row['offset'] - row['uncertainty'] for row in (before, after))
    high = max(row['offset'] + row['uncertainty'] for row in (before, after))
    if abs(before['offset'] - after['offset']) > before['uncertainty'] + after['uncertainty']:
        raise ValueError('Browser/server clock alignment drifted outside measured bounds')
    offset, uncertainty = (low + high) / 2, (high - low) / 2
    rows = receipt['browserObservations']['rows']
    produced = receipt['produced']
    if not produced or [row['index'] for row in produced] != list(range(len(produced))):
        raise ValueError('Producer indices are incomplete or duplicated')
    if [row['index'] for row in rows['dom']] != list(range(len(produced))):
        raise ValueError('DOM evidence is incomplete or duplicated')
    if any('/sync?' in url for url in receipt['requests']):
        raise ValueError('Hot stream recovered through a snapshot')
    commit = {row['sequence']: row['at'] for row in receipt['commits']}
    first_wire = {}
    wire_sequences = []
    for row in rows['wire']:
        if row['kind'] == 'sync.reset_required':
            raise ValueError('Hot stream reset')
        if row.get('sequence') is not None:
            wire_sequences.append(row['sequence'])
        for index in row.get('markers', []):
            if row['sequence'] not in commit:
                raise ValueError('Missing post-ACK notification for a visible frame')
            first_wire.setdefault(index, row)
    if wire_sequences != sorted(set(wire_sequences)):
        raise ValueError('Duplicate or out-of-order sync delivery')
    if set(first_wire) != set(range(len(produced))):
        raise ValueError('Missing wire evidence for produced text')
    if not receipt['writes'] or not receipt['frames']:
        raise ValueError('Missing storage or transport observations')
    stages = {key: [] for key in ('producerToDomMs', 'commitNoticeToWireMs', 'wireToDomMs', 'commitNoticeToDomMs')}
    for dom in rows['dom']:
        index = dom['index']; wire = first_wire[index]; committed = commit[wire['sequence']]
        stages['producerToDomMs'].append(dom['at'] + offset - produced[index]['start'])
        stages['commitNoticeToWireMs'].append(wire['at'] + offset - committed)
        stages['wireToDomMs'].append(dom['at'] - wire['at'])
        if dom['at'] < wire['at']:
            raise ValueError('DOM evidence preceded its wire frame')
        stages['commitNoticeToDomMs'].append(dom['at'] + offset - committed)
    # The notification callback can race the wake subscriber. Keep signed raw
    # clocks and report that limitation; it is not the database commit instant.
    begin = receipt['measuredStart'] - offset
    end = rows['dom'][-1]['at']
    timing = receipt['browserObservations'].get('nativeTiming')
    workload_inputs = [row for row in rows['inputs'] if 'frame' in row
        and (not timing or timing['startedAt'] <= row['at'] <= timing['endedAt'])]
    stream_inputs = [row['frame'] - row['at'] for row in rows['inputs']
        if 'frame' in row and produced[0]['start'] <= row['at'] + offset - uncertainty
        and row['at'] <= end]
    if not stream_inputs:
        raise ValueError('No input response observed during text streaming')
    return {**{key: _distribution(values) for key, values in stages.items()},
        'observedWalWindow': _wal_observation(receipt), 'concurrentWorkload': _concurrent_workload(receipt),
        'observedProcessResources': _process_resource_observation(receipt),
        'nativeIngress': _native_ingress_observation(receipt),
        'nativeInteractions': _interaction_observation(receipt, produced[0]['start'] - offset + uncertainty, end),
        'firstProducedToDomMs': stages['producerToDomMs'][0],
        'clockUncertaintyMs': uncertainty,
        'commandAckMs': _distribution([row['end'] - row['start'] for row in receipt['writes']]),
        'commandCount': len(receipt['writes']),
        'commandLogicalBytes': sum(row['bytes'] for row in receipt['writes']),
        'sseFrameCount': len(receipt['frames']), 'sseLogicalBytes': sum(row['bytes'] for row in receipt['frames']),
        'contentBytes': receipt['contentBytes'], 'changeReads': len(receipt['reads']),
        'inputHandlerToFrameMs': _distribution([row['frame'] - row['at'] for row in workload_inputs]),
        'streamInputHandlerToFrameMs': _distribution(stream_inputs),
        'hotLongTasks': [row for row in rows['longTasks'] if begin <= row['at'] <= end]
            if receipt['browserObservations']['longTaskSupported'] else None,
        'interpretation': 'Instrumented local diagnostic; no physical I/O, paint, full inference, or production latency admission.'}


if __name__ == '__main__':
    for filename in sys.argv[1:]:
        with open(filename) as handle:
            print(json.dumps({'file': filename, 'summary': summarize(json.load(handle))}, indent=2))
