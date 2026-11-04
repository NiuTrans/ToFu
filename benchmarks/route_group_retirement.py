"""Offline ordered route-pool lifecycle; no storage, transport or model calls.

Compare alternating fresh processes with --pool-source pointing to frozen
slot_pool.py files. The owner depends only on Slot attributes and the standard
library; all other code/fixtures stay identical. Complete membership, scopes,
credential counts and retirement receipts gate the clocks. Allocation samples
are separate from timing and exclude Slot construction. This is not an agent
throughput or application capacity certificate.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import statistics
import sys
import time
import tracemalloc


def load_pool(path):
    spec = importlib.util.spec_from_file_location('measured_slot_pool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SlotPool


def measure(pool_type, *, groups, candidates, samples, allocations=False):
    from lib.llm_dispatch.slot import Slot

    if min(groups, candidates, samples) < 1:
        raise ValueError('groups, candidates and samples must be positive')
    configured = [Slot(key_name='configured', api_key='', model='synthetic',
                       capabilities={'text'}, provider_id='configured')]
    waves = [[Slot(key_name=f'group-{g}-candidate-{k}', api_key='',
                   model='synthetic', capabilities={'text'},
                   provider_id=f'request-{g}', request_scoped=True,
                   routing_provider_id='provider', routing_owner_user_id=23,
                   route_credential_id=f'credential-{k % 2}')
              for k in range(candidates)] for g in range(groups)]
    ordered = configured + [slot for wave in waves for slot in wave]
    expected = [id(slot) for slot in ordered]
    order = list(range(groups))
    random.Random(23).shuffle(order)
    expected_removed = [{id(slot) for slot in wave} for wave in waves]
    fixture = [[slot.provider_id, slot.key_name] for slot in ordered]
    fixture_digest = hashlib.sha256(json.dumps(fixture).encode()).hexdigest()
    namespace = waves[0][0].key_stats_provider_id()
    credentials = tuple(sorted({s.key_stats_key_name() for s in waves[0]}))
    clocks = {'publish_ms': [], 'retire_ms': [], 'lifecycle_ms': [], 'read_ms': []}

    def prepare():
        pool = pool_type(configured)
        started = time.perf_counter()
        for wave in waves:
            pool.publish(wave)
        elapsed = (time.perf_counter() - started) * 1000
        assert [id(slot) for slot in pool] == expected
        assert pool.key_stats_key_names(namespace) == credentials
        return pool, elapsed

    def retire(pool):
        receipts = []
        started = time.perf_counter()
        for i in order:
            receipts.append(pool.withdraw(waves[i]))
        elapsed = (time.perf_counter() - started) * 1000
        assert receipts == [expected_removed[i] for i in order]
        assert [id(slot) for slot in pool] == [id(slot) for slot in configured]
        assert pool.scope(None) == configured
        assert all(pool.scope(wave[0].provider_id) == [] for wave in waves)
        assert pool.key_stats_key_names(namespace) == ()
        assert pool.key_stats_key_names('configured') == ('configured',)
        return elapsed

    # One unmeasured complete lifecycle warms the same path in each arm.
    warm, _ = prepare()
    retire(warm)
    del warm
    for _ in range(samples):
        pool, publish = prepare()
        started = time.perf_counter()
        snapshot = list(pool)
        read = (time.perf_counter() - started) * 1000
        assert [id(slot) for slot in snapshot] == expected
        retirement = retire(pool)
        clocks['publish_ms'].append(publish)
        clocks['retire_ms'].append(retirement)
        clocks['lifecycle_ms'].append(publish + retirement)
        clocks['read_ms'].append(read)
        del snapshot, pool

    live_bytes = idle_bytes = released_bytes = None
    if allocations:
        gc.collect()
        tracemalloc.start()
        try:
            pool, _ = prepare()
            live_bytes = tracemalloc.get_traced_memory()[0]
            retire(pool)
            gc.collect()
            idle_bytes = tracemalloc.get_traced_memory()[0]
            del pool
            gc.collect()
            released_bytes = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
    return dict(groups=groups, candidates=candidates, slots=len(ordered),
                samples=samples, fixture_sha256=fixture_digest,
                retired_slots=groups * candidates, remaining_slots=1,
                live_index_bytes=live_bytes, idle_index_bytes=idle_bytes,
                released_index_bytes=released_bytes, raw=clocks,
                medians={name: statistics.median(values) for name, values in clocks.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool-source', type=Path)
    parser.add_argument('--groups', type=int, nargs='+', default=[1, 32, 256, 1024])
    parser.add_argument('--candidates', type=int, default=52)
    parser.add_argument('--samples', type=int, default=7)
    parser.add_argument('--allocations', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    source = (args.pool_source or root / 'lib/llm_dispatch/slot_pool.py').resolve()
    pool_type = load_pool(source)
    result = dict(source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  cases=[measure(pool_type, groups=g, candidates=args.candidates,
                                 samples=args.samples, allocations=args.allocations)
                         for g in args.groups])
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n')
    print(rendered)


if __name__ == '__main__':
    main()
