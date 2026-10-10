"""Offline observer-stall measurement for the committed conversation wake lane.

Run from a checkout: ``python benchmarks/conversation_wake.py --commits 10000``.
The observer loop is paused during the burst. This measures hint publication,
scheduled callbacks and Python allocation peak, not conversation throughput or
durable storage latency. Disposable state prevents borrowing the live app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import tracemalloc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commits", type=int, default=10_000)
    args = parser.parse_args()
    if not 1 <= args.commits <= 1_000_000:
        parser.error("--commits must be between 1 and 1000000")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="tofu-wake-benchmark-") as data_root:
        os.environ["TOFU_DATA_DIR"] = data_root
        os.environ["TOFU_CODEX_SHARED_LOGIN"] = "0"
        from lib.conversation_sync.broker import ConversationWakeBroker

        async def measure() -> dict:
            broker = ConversationWakeBroker(owner_history_capacity=16)
            subscription = broker.subscribe(1, "synthetic-wake-benchmark")
            loop = asyncio.get_running_loop()
            schedule = loop.call_soon_threadsafe
            callbacks = 0

            def counted_schedule(callback, *callback_args, **kwargs):
                nonlocal callbacks
                callbacks += 1
                return schedule(callback, *callback_args, **kwargs)

            loop.call_soon_threadsafe = counted_schedule
            tracemalloc.start()
            started = time.perf_counter_ns()
            for sequence in range(args.commits):
                broker.wake(1, subscription.conversation_id, sequence)
            duration_ns = time.perf_counter_ns() - started
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            scheduled_callbacks = callbacks
            delivered = await subscription.wait(1.0)
            loop.call_soon_threadsafe = schedule
            subscription.close()
            return {
                "commits": args.commits,
                "scheduledWakeCallbacks": scheduled_callbacks,
                "publicationMs": round(duration_ns / 1_000_000, 3),
                "publicationUsPerCommit": round(duration_ns / args.commits / 1_000, 3),
                "peakPythonAllocationBytes": peak_bytes,
                "delivered": delivered,
                "activeSubscriptionsAfterClose": broker.snapshot()["active"],
                "python": platform.python_version(),
                "tracemallocEnabledDuringPublication": True,
            }

        print(json.dumps(asyncio.run(measure()), sort_keys=True))


if __name__ == "__main__":
    main()
