"""Expose fixed delivery anchors as Prometheus histograms, without task labels.

Entry: emit_delivery_profile; consumes an existing snapshot and emits text only.
Collection never creates a coordinator or opens a storage authority.
"""
from lib.agent_core.generated_task_event_delivery_profile import DELIVERY_PROFILE_POLICY


def emit_delivery_profile(out, profile, emit_counter, emit_gauge):
    prefix = 'tofu_task_event_delivery_profile'
    emit_gauge(out, prefix + '_enabled', 'Opt-in delivery wall-clock sampling enabled',
               [({}, int(bool(profile.get('enabled'))))])
    emit_gauge(out, prefix + '_available', 'Delivery profile snapshot available',
               [({}, int(profile.get('available') is not False))])
    if not profile.get('enabled') or profile.get('available') is False:
        return
    for key in ('seenBatches', 'selectedBatches', 'completedSamples', 'invalidClocks',
                'observerErrors', 'failedPersistenceRounds', 'barrierErrors', 'acceptedFrameAnchors',
                'acknowledgedFrameAnchors', 'failedAttempts', 'publishErrors'):
        emit_counter(out, prefix + '_' + key + '_total', 'Delivery profile ' + key,
                     [({}, profile.get(key, 0))])
    for key in ('sampleEvery', 'activeSamples', 'activeFrameAnchors', 'peakFrameAnchors', 'metadataBytes'):
        emit_gauge(out, prefix + '_' + key, 'Delivery profile ' + key,
                   [({}, profile.get(key, 0))])
    for metric, field in (('attempts_total', 'count'), ('attempt_seconds_total', 'totalNanos')):
        divisor = 1_000_000_000 if field == 'totalNanos' else 1
        emit_counter(out, prefix + '_' + metric, 'Sampled persistence attempts including failures',
            [({'outcome': name}, profile.get('attemptOutcomes', {}).get(name, {}).get(field, 0) / divisor)
             for name in DELIVERY_PROFILE_POLICY['attemptOutcomes']])
    emit_gauge(out, prefix + '_attempt_max_seconds', 'Maximum sampled attempt duration by fixed outcome',
        [({'outcome': name}, profile.get('attemptOutcomes', {}).get(name, {}).get('maxNanos', 0) / 1_000_000_000)
         for name in DELIVERY_PROFILE_POLICY['attemptOutcomes']])
    name = prefix + '_stage_seconds'
    out.extend([f'# HELP {name} Sampled callback wall time; nested stages must not be summed',
                f'# TYPE {name} histogram'])
    for stage in DELIVERY_PROFILE_POLICY['anchors']:
        row = profile.get('stages', {}).get(stage)
        if row is None:
            continue
        count = 0
        for bucket, amount in enumerate(row['log2NanosHistogram']):
            count += amount
            ceiling = '+Inf' if bucket == 63 else str((1 << bucket) / 1_000_000_000)
            out.append(f'{name}_bucket{{stage="{stage}",le="{ceiling}"}} {count}')
        out.append(f'{name}_count{{stage="{stage}"}} {row["count"]}')
        out.append(f'{name}_sum{{stage="{stage}"}} {row["totalNanos"] / 1_000_000_000}')
    emit_gauge(out, prefix + '_stage_max_seconds', 'Maximum observed sampled wall time',
               [({'stage': name}, profile['stages'][name]['maxNanos'] / 1_000_000_000)
                for name in DELIVERY_PROFILE_POLICY['anchors'] if name in profile.get('stages', {})])
