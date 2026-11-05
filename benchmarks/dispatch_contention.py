"""Dispatcher contention: slot selection and route-group minting under GIL load.

Reproduces the production shape behind slow ``registration`` and
``route_selection``: about 1,600 live request-scoped slots (route groups of
~52 candidates minted through the real model-routing adapter), a configured
pool, real ``lib.key_stats`` admission reads against a store file on the
selected directory, and CPU-bound threads that keep the GIL contended.

Picker threads run pinned and unpinned ``pick_and_reserve`` calls; one thread
mints and disposes a further route group. Every latency is wall clock around
the public call, so dispatcher-lock waits are included. The authority and
audit sink are in-memory; no storage RPC, network or inference is measured.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from pathlib import Path
import random
import statistics
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def _summary(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)

    def rank(fraction):
        return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]

    return {'count': len(ordered),
            'p50_ms': round(statistics.median(ordered) * 1000, 3),
            'p90_ms': round(rank(0.90) * 1000, 3),
            'p99_ms': round(rank(0.99) * 1000, 3),
            'max_ms': round(ordered[-1] * 1000, 3)}


def run(*, groups, models, credentials, configured, cpu_threads, pickers,
        seconds, stats_dir):
    from benchmarks.model_routing_request import synthetic_catalog
    import lib.key_stats as key_stats
    from lib.llm_dispatch import ephemeral, factory
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.llm_dispatch.slot import Slot
    from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
    from lib.model_routing import dispatch_adapter as adapter

    document = synthetic_catalog(models, credentials)
    boundary = OwnerBoundary.create(23)
    selection = NativeModelSelection(ModelRef('benchmark', 'model-0000'), None, 'a')
    repository = SimpleNamespace(
        get=lambda owner: SimpleNamespace(document=copy.deepcopy(document), revision=7),
        resolve_secret=lambda owner, reference: 'synthetic-' + reference)
    dispatcher = LLMDispatcher()
    dispatcher.slots = [
        Slot(key_name=f'configured-{index}', api_key='synthetic',
             model=f'configured-model-{index % 8}', capabilities={'text'},
             provider_id=f'configured-provider-{index % 4}')
        for index in range(configured)]
    dispatcher._initialized = True

    stats_path = Path(stats_dir) / 'key_stats.json'
    stats_path.write_text(json.dumps({'day': time.strftime('%Y-%m-%d'),
                                      'stats': {}, 'overrides': {}}))
    results = {}
    with patch.object(factory, 'get_dispatcher', lambda: dispatcher), \
            patch.object(ephemeral, '_handles', {}), \
            patch.object(ephemeral, 'audit_log', lambda *a, **k: None), \
            patch.object(ephemeral, '_preflight_enabled', lambda: False), \
            patch.object(key_stats, '_STATS_PATH', str(stats_path)):
        live = [adapter.mint_routed_slot_group(repository, boundary, selection)
                for _ in range(groups)]
        pins = [group.pin_id for group in live]
        results['live_slots'] = len(dispatcher.slots)
        results['group_size'] = len(live[0].handles)

        stop = threading.Event()

        def burn():
            value = 0
            while not stop.is_set():
                for index in range(2000):
                    value ^= index * index

        pinned_latency, unpinned_latency = [], []

        def pick(pinned, sink):
            rng = random.Random(len(sink) + (1 if pinned else 0))
            while not stop.is_set():
                pin = rng.choice(pins) if pinned else None
                started = time.monotonic()
                with provider_pin(pin):
                    slot = dispatcher.pick_and_reserve(
                        capability='text',
                        prefer_model=('model-0000' if pinned else
                                      'configured-model-1'),
                        strict_model=True)
                sink.append(time.monotonic() - started)
                if slot is not None:
                    slot.release()

        mint_latency, dispose_latency = [], []

        def mint():
            while not stop.is_set():
                started = time.monotonic()
                group = adapter.mint_routed_slot_group(
                    repository, boundary, selection)
                minted = time.monotonic()
                adapter.dispose_routed_slot_group(group)
                mint_latency.append(minted - started)
                dispose_latency.append(time.monotonic() - minted)

        threads = [threading.Thread(target=burn, daemon=True)
                   for _ in range(cpu_threads)]
        threads += [threading.Thread(target=pick, args=(True, pinned_latency),
                                     daemon=True) for _ in range(pickers)]
        threads.append(threading.Thread(target=pick, args=(False, unpinned_latency),
                                        daemon=True))
        threads.append(threading.Thread(target=mint, daemon=True))
        for thread in threads:
            thread.start()
        time.sleep(seconds)
        stop.set()
        for thread in threads:
            thread.join(30)
        for group in live:
            adapter.dispose_routed_slot_group(group)
        # Joined cleanup is part of the receipt: no request route may survive.
        results['leftover_request_slots'] = sum(
            1 for slot in dispatcher.slots if slot.request_scoped)
        results['leftover_registered_handles'] = ephemeral.count_ephemeral_slots()
        results.update(pinned_pick=_summary(pinned_latency),
                       unpinned_pick=_summary(unpinned_latency),
                       group_mint=_summary(mint_latency),
                       group_dispose=_summary(dispose_latency))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--groups', type=int, default=30)
    parser.add_argument('--models', type=int, default=13)
    parser.add_argument('--credentials', type=int, default=2)
    parser.add_argument('--configured', type=int, default=100)
    parser.add_argument('--cpu-threads', type=int, default=8)
    parser.add_argument('--pickers', type=int, default=4)
    parser.add_argument('--seconds', type=float, default=20.0)
    parser.add_argument('--stats-dir', required=True,
                        help='directory for the key-stats store file')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    logging.disable(logging.CRITICAL)
    import lib.llm_dispatch.dispatcher as dispatcher_module
    result = dict(source=dispatcher_module.__file__, switch_interval_s=sys.getswitchinterval(),
                  cpu_count=os.cpu_count(), **run(
                      groups=args.groups, models=args.models,
                      credentials=args.credentials, configured=args.configured,
                      cpu_threads=args.cpu_threads, pickers=args.pickers,
                      seconds=args.seconds, stats_dir=args.stats_dir))
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
