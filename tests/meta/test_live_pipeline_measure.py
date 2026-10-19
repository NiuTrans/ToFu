"""Reject partial application timing evidence before computing diagnostics."""
import copy

import pytest

from benchmarks.live_pipeline import summarize

pytestmark = pytest.mark.unit


@pytest.fixture
def receipt():
    return {'storage': {'ready': True, 'backend': 'tofudb'},
        'alignmentBefore': {'offset': 1000, 'uncertainty': 1},
        'alignmentAfter': {'offset': 1000.2, 'uncertainty': 1},
        'measuredStart': 1000, 'produced': [{'index': 0, 'start': 1000}, {'index': 1, 'start': 1010}],
        'requests': [], 'commits': [{'sequence': 4, 'at': 1005}, {'sequence': 5, 'at': 1020}],
        'writes': [{'start': 1000, 'end': 1002, 'bytes': 30}, {'start': 1010, 'end': 1013, 'bytes': 40}],
        'frames': [{'bytes': 40}, {'bytes': 40}], 'reads': [{}, {}], 'contentBytes': 20,
        'browserObservations': {'longTaskSupported': True, 'rows': {
            'wire': [{'kind': 'attempt.event', 'sequence': 4, 'at': 10, 'markers': [0]},
                     {'kind': 'attempt.event', 'sequence': 5, 'at': 25, 'markers': [1]}],
            'dom': [{'index': 0, 'at': 20}, {'index': 1, 'at': 35}],
            'inputs': [{'at': 15, 'frame': 30}], 'longTasks': [{'at': -100, 'duration': 70}]}}}


def test_complete_receipt_reports_separate_populations_and_clock_uncertainty(receipt):
    original = copy.deepcopy(receipt)
    result = summarize(receipt)
    assert result['firstProducedToDomMs'] == pytest.approx(20.1)
    assert result['producerToDomMs']['count'] == 2
    assert result['wireToDomMs']['median'] == 10
    assert result['commandAckMs']['median'] == 2.5
    assert result['commandLogicalBytes'] == 70
    assert result['sseLogicalBytes'] == 80
    assert result['clockUncertaintyMs'] == pytest.approx(1.1)
    assert result['hotLongTasks'] == []
    assert receipt == original


def test_failed_fixture_cleanup_cannot_qualify_a_measurement(receipt):
    receipt['fixtureCleanup'] = {'complete': False}
    with pytest.raises(ValueError, match='cleanup'):
        summarize(receipt)
    receipt['fixtureCleanup']['complete'] = True
    assert summarize(receipt)['producerToDomMs']['count'] == 2


@pytest.mark.parametrize('fault', ['commit', 'wire', 'dom', 'reset', 'clock', 'writes', 'snapshot', 'causality'])
def test_incomplete_receipts_never_get_zero_latency_or_a_speed_verdict(receipt, fault):
    rows = receipt['browserObservations']['rows']
    if fault == 'commit': receipt['commits'].pop()
    elif fault == 'wire': rows['wire'].append(copy.deepcopy(rows['wire'][-1]))
    elif fault == 'dom': rows['dom'].pop()
    elif fault == 'reset': rows['wire'].append({'kind': 'sync.reset_required'})
    elif fault == 'clock': receipt['alignmentAfter']['offset'] += 50
    elif fault == 'writes': receipt['writes'].clear()
    elif fault == 'snapshot': receipt['requests'].append('/conversations/example/sync?after=x')
    elif fault == 'causality': rows['dom'][0]['at'] = 5
    with pytest.raises(ValueError):
        summarize(receipt)


def test_physical_write_window_distinguishes_counters_from_current_residency(receipt):
    before = {'failed': False, 'retainedFrameBytes': 0, 'queuedFrames': 7, 'writtenFrames': 7, 'writtenBytes': 100}
    after = {**before, 'queuedFrames': 9, 'writtenFrames': 9, 'writtenBytes': 400}
    receipt.update(storageMetricsBefore={'walWriteBuffer': before}, storageMetricsAfter={'walWriteBuffer': after})
    assert summarize(receipt)['observedWalWindow']['writtenBytesDelta'] == 300
    after['retainedFrameBytes'] = 200
    assert summarize(receipt)['observedWalWindow']['qualified'] is False
    after['retainedFrameBytes'] = 0
    after['writtenBytes'] = 99
    with pytest.raises(ValueError, match='reset'):
        summarize(receipt)


@pytest.fixture
def concurrent_receipt(receipt):
    for row in receipt['produced']:
        row['end'] = row['start'] + 1
    receipt.update(concurrentConversations=2, measuredEnd=1040, contentSha256='a' * 64)
    for row in receipt['writes']:
        row['participant'] = 0
    receipt['participantWrites'] = copy.deepcopy(receipt['writes'])
    receipt['participantWrites'] += [{**row, 'participant': 1} for row in receipt['writes']]
    receipt['participants'] = [{'index': index, 'produced': copy.deepcopy(receipt['produced']),
        'finishedAt': 1035, 'contentSha256': receipt['contentSha256'],
        'deliveryReceipt': {'accepted': 2, 'completed': 2}} for index in range(2)]
    return receipt


def test_loaded_receipt_keeps_foreground_and_total_work_separate(concurrent_receipt):
    result = summarize(concurrent_receipt)
    assert result['commandCount'] == 2
    assert result['concurrentWorkload']['commandCount'] == 4
    assert result['concurrentWorkload']['commandLogicalBytes'] == 140
    assert result['concurrentWorkload']['overlapMs'] == 35
    assert result['concurrentWorkload']['completeWindowMs'] == 40


@pytest.mark.parametrize('fault', ['missing', 'participant', 'produced', 'completion', 'debt',
    'content', 'serial', 'commands', 'primary', 'clock'])
def test_loaded_evidence_refuses_incomplete_or_unmeasured_peers(concurrent_receipt, fault):
    peer = concurrent_receipt['participants'][1]
    if fault == 'missing': del concurrent_receipt['measuredEnd']
    elif fault == 'participant': concurrent_receipt['participants'].pop()
    elif fault == 'produced': peer['produced'].pop()
    elif fault == 'completion': peer['finishedAt'] = 1050
    elif fault == 'debt': peer['deliveryReceipt']['completed'] = 1
    elif fault == 'content': peer['contentSha256'] = 'b' * 64
    elif fault == 'serial':
        for row in peer['produced']:
            row.update(start=1036, end=1037)
        peer['produced'][1].update(start=1037, end=1038)
        peer['finishedAt'] = 1039
    elif fault == 'commands': concurrent_receipt['participantWrites'] = concurrent_receipt['writes']
    elif fault == 'primary': concurrent_receipt['participants'][0]['produced'][0]['end'] += .1
    elif fault == 'clock': peer['produced'][0]['end'] = float('nan')
    with pytest.raises(ValueError):
        summarize(concurrent_receipt)


@pytest.mark.parametrize('fault', [None, 'missing_end', 'missing_stream', 'negative', 'outside', 'unordered'])
def test_resource_observations_require_an_ordered_complete_window(concurrent_receipt, fault):
    rows = [{'boundary': boundary, 'at': at, 'rssBytes': rss, 'threads': threads}
            for boundary, at, rss, threads in [('start', 1000, 100, 2), ('stream', 1010, 150, 4), ('settled', 1040, 120, 2)]]
    concurrent_receipt['processResourceSamples'] = rows
    if fault == 'missing_end': rows.pop()
    elif fault == 'missing_stream': rows.pop(1)
    elif fault == 'negative': rows[1]['rssBytes'] = -1
    elif fault == 'outside': rows[-1]['at'] = 1041
    elif fault == 'unordered': rows[1]['at'] = 999
    if fault:
        with pytest.raises(ValueError): summarize(concurrent_receipt)
    else:
        result = summarize(concurrent_receipt)['observedProcessResources']
        assert result['peakObservedRssBytes'] == 150 and result['peakObservedThreads'] == 4
        assert result['settledRssBytes'] == 120 and result['settledThreads'] == 2


@pytest.mark.parametrize('fault', [None, 'archive', 'kind', 'cpu', 'missing', 'requests',
    'unjoined', 'sequence', 'channel', 'bytes', 'clock', 'content', 'late_dom'])
def test_native_ingress_requires_actual_ordered_complete_text_and_cleanup(concurrent_receipt, fault):
    concurrent_receipt.update(producerKind='native', nativeArchiveSha256='b'*64,
        joinedChildrenCpuMs=12.5, markersAtTypingStart=1)
    for participant in concurrent_receipt['participants']:
        participant['nativeIngress'] = {'providerRequests': 1, 'clientJoined': True,
            'textSha256': concurrent_receipt['contentSha256'],
            'frames': [{'sequence': i+1, 'channel': 'content', 'bytes': 10, 'at': at}
                for i, at in enumerate((1003, 1013))]}
    witness = concurrent_receipt['participants'][1]['nativeIngress']
    if fault == 'archive': concurrent_receipt['nativeArchiveSha256'] = 'unverified'
    elif fault == 'kind': concurrent_receipt['producerKind'] = 'unknown'
    elif fault == 'cpu': concurrent_receipt['joinedChildrenCpuMs'] = float('nan')
    elif fault == 'missing': del concurrent_receipt['participants'][1]['nativeIngress']
    elif fault == 'requests': witness['providerRequests'] = 2
    elif fault == 'unjoined': witness['clientJoined'] = False
    elif fault == 'sequence': witness['frames'][1]['sequence'] = 3
    elif fault == 'channel': witness['frames'][0]['channel'] = 'reasoning'
    elif fault == 'bytes': witness['frames'].pop()
    elif fault == 'clock': witness['frames'][1]['at'] = 1041
    elif fault == 'content': witness['textSha256'] = 'c'*64
    elif fault == 'late_dom': concurrent_receipt['markersAtTypingStart'] = 2
    if fault:
        with pytest.raises(ValueError): summarize(concurrent_receipt)
    else:
        result = summarize(concurrent_receipt)['nativeIngress']
        assert result['participants'] == 2 and result['textFrames'] == 4
        assert result['observedJoinedChildrenCpuMs'] == 12.5


@pytest.mark.parametrize('at', [-10, 40])
def test_input_responses_outside_text_stream_cannot_qualify_responsiveness(receipt, at):
    receipt['browserObservations']['rows']['inputs'] = [{'at': at, 'frame': at+1}]
    with pytest.raises(ValueError, match='during text streaming'):
        summarize(receipt)


@pytest.fixture
def interaction_receipt(receipt):
    observation = receipt['browserObservations']
    observation['nativeTiming'] = {'supported': True, 'error': None, 'droppedEntries': 0,
        'thresholdMs': 16, 'durationQuantumMs': 8, 'calibrationDurationMs': 80,
        'startedAt': 0, 'endedAt': 40, 'keyCountBefore': 2, 'keyCountAfter': 4}
    observation['rows'].update(keydowns=[{'at': 15, 'handledAt': 17, 'trusted': True},
        {'at': 26, 'handledAt': 26.5, 'trusted': True}], eventTimings=[
        {'name': 'keydown', 'at': 15, 'duration': 24, 'processingStart': 17,
         'processingEnd': 19, 'interactionId': 123, 'composer': True},
        {'name': 'keyup', 'at': 20, 'duration': 16, 'processingStart': 21,
         'processingEnd': 22, 'interactionId': 123, 'composer': True}], wheels=[], scrolls=[])
    return receipt


def test_native_interactions_group_events_without_filling_fast_censored_samples(interaction_receipt):
    interaction_receipt['browserObservations']['rows']['inputs'].append({'at': -100, 'frame': 100})
    result = summarize(interaction_receipt)['nativeInteractions']
    assert result['qualified'] and result['trustedKeyDownCount'] == 2
    assert result['reportedEventCount'] == 2 and result['reportedInteractionCount'] == 1
    assert result['reportedInteractionDurationMs'] == {'count': 1, 'median': 24, 'p95': 24, 'max': 24}
    assert result['reportedEventInputDelayMs']['max'] == 2
    assert summarize(interaction_receipt)['inputHandlerToFrameMs']['max'] < 200
    interaction_receipt['browserObservations']['rows']['eventTimings'].clear()
    empty = summarize(interaction_receipt)['nativeInteractions']
    assert empty['qualified'] and empty['reportedInteractionDurationMs'] is None
    assert empty['trustedKeyDownCount'] == 2 and empty['reportedInteractionCount'] == 0


@pytest.mark.parametrize('fault', ['calibration', 'missing_calibration', 'dropped', 'observer', 'census', 'trusted',
                                 'window', 'duration', 'processing', 'interaction'])
def test_native_interaction_gaps_refuse_qualification(interaction_receipt, fault):
    observation = interaction_receipt['browserObservations']; timing = observation['nativeTiming']
    rows = observation['rows']
    if fault == 'calibration': timing['calibrationDurationMs'] = 0
    elif fault == 'missing_calibration': timing['calibrationDurationMs'] = None
    elif fault == 'dropped': timing['droppedEntries'] = 1
    elif fault == 'observer': timing['error'] = 'observer failed'
    elif fault == 'census': timing['keyCountAfter'] += 1
    elif fault == 'trusted': rows['keydowns'][0]['trusted'] = False
    elif fault == 'window': timing['endedAt'] = -1
    elif fault == 'duration': rows['eventTimings'][0]['duration'] = float('nan')
    elif fault == 'processing': rows['eventTimings'][0]['processingStart'] = 14
    elif fault == 'interaction': rows['eventTimings'][0]['interactionId'] = -1
    with pytest.raises(ValueError): summarize(interaction_receipt)


def test_unsupported_event_timing_and_legacy_receipts_never_claim_fast_input(receipt, interaction_receipt):
    interaction_receipt['browserObservations']['nativeTiming']['supported'] = False
    assert summarize(interaction_receipt)['nativeInteractions']['qualified'] is False
    del receipt['browserObservations']['nativeTiming']
    assert summarize(receipt)['nativeInteractions'] is None


def test_scroll_evidence_requires_trusted_bidirectional_progress_during_stream(interaction_receipt):
    rows = interaction_receipt['browserObservations']['rows']
    interaction_receipt['scrollProbe'] = {'before': 500, 'afterUp': 100, 'afterDown': 600, 'markersAfterUp': 1}
    rows['wheels'] = [{'at': 15, 'deltaY': -400, 'trusted': True}, {'at': 25, 'deltaY': 600, 'trusted': True}]
    rows['scrolls'] = [{'at': 20, 'top': 100, 'frame': 25}]
    assert summarize(interaction_receipt)['nativeInteractions']['scroll']['trustedWheelCount'] == 2
    interaction_receipt['scrollProbe']['markersAfterUp'] = 2
    with pytest.raises(ValueError, match='scroll progress'): summarize(interaction_receipt)
