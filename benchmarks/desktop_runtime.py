"""Offline desktop admission/reclamation measurement; no tools or providers run.

Run `python benchmarks/desktop_runtime.py --commands 100000`. A deterministic
monotonic clock advances .01s per admitted/completed command, whose pickup TTL
is 1s. Report admission throughput and tracemalloc separately; neither measures
execution latency, process RSS nor concurrent client capacity.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import sys
import threading
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.desktop_agent._runtime import RuntimeState, execution_budget  # noqa: E402


def sample(commands, budget, *, memory=False):
    clock = [0.0]
    state = RuntimeState(threading.Event(), budget=budget, clock=lambda: clock[0])
    state.observe_server_time(1000)
    if memory:
        tracemalloc.start()
    start = time.perf_counter()
    peak_ids = 0
    for index in range(commands):
        clock[0] = index * .01
        command_id = str(index)
        if not state.admit(command_id, expires_at=1000 + clock[0] + 1):
            raise RuntimeError('Unexpected admission refusal at command %s' % index)
        state.finish(command_id)
        peak_ids = max(peak_ids, len(state.seen))
    duration = time.perf_counter() - start
    result = {'commands': commands, 'seconds': duration,
              'peak_replay_ids': peak_ids, 'final_replay_ids': len(state.seen)}
    if memory:
        result['traced_current_bytes'], result['traced_peak_bytes'] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    else:
        result['admissions_per_second'] = commands / duration
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commands', type=int, default=100000)
    args = parser.parse_args()
    if not 1 <= args.commands <= 1000000:
        parser.error('--commands must be in 1..1000000')
    budget = execution_budget()
    runs = [sample(args.commands, budget) for _ in range(3)]
    print(json.dumps({'schema': 1, 'scope': 'offline admission and expiry reclamation',
        'budget': asdict(budget), 'clock_tick_seconds': .01, 'pickup_ttl_seconds': 1,
        'timing_runs': runs, 'median_admissions_per_second': statistics.median(
            run['admissions_per_second'] for run in runs),
        'traced_memory_run': sample(args.commands, budget, memory=True)}, indent=2))


if __name__ == '__main__':
    main()
