"""Read content-free TofuDB request profiles; reconcile before attributing costs.

Accepts a system.metrics payload or conversation_concurrency JSON. Never adds
nested inclusive timers or interprets elapsed time as CPU/device time. The
profile contract and operating instructions live in STORAGE_PERFORMANCE_CHARTER.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

WAIT_STAGES = frozenset({"fifo_wait", "mutex_wait", "parallel_snapshot_wait",
                         "wal_barrier_wait", "rotation_park",
                         "entity_page_cache_read_wait", "entity_page_cache_write_wait",
                         "block_pending_wait", "block_cache_wait", "block_history_source_wait", "rotation_prepare_wait", "rotation_install_wait"})


def analyze(document: dict) -> dict:
    metrics = document.get("storage_metrics", document)
    profile = metrics.get("requestProfile", metrics)
    if not profile.get("enabled") or not profile.get("available"):
        raise ValueError("request profile is disabled or unavailable; enable TOFU_DB_REQUEST_PROFILE_EVERY")
    if profile.get("contract") != "tofudb.request-profile/v1":
        raise ValueError("unsupported request profile contract")
    rows = []
    for operation in profile["operations"]:
        stages = operation["stages"]
        dispatch = operation["dispatchNanos"]
        attributed = sum(stage["exclusiveNanos"] for stage in stages)
        if operation["invalidSpans"] or attributed + operation["unattributedNanos"] != dispatch:
            raise ValueError(f"unreconciled span accounting: {operation['operation']}")
        requests = operation["requests"]
        if requests <= 0 or sum(operation["dispatchLog2NanosHistogram"]) != requests:
            raise ValueError("request histogram does not reconcile")
        queue_samples = operation["embeddedQueueSamples"]
        queue_nanos = operation["embeddedQueueNanos"]
        if (not isinstance(queue_samples, int) or not 0 <= queue_samples <= requests
                or (queue_samples > 0 and queue_nanos is None)
                or (queue_nanos is not None and (not isinstance(queue_nanos, int) or queue_nanos < 0))
                or (queue_samples == 0 and queue_nanos not in (None, 0))):
            raise ValueError(f"invalid embedded queue accounting: {operation['operation']}")
        # Dispatch aggregates cover all requests. Combining a partial queue
        # population with them would manufacture a mean for unmatched samples.
        queue_mean = queue_nanos / queue_samples / 1e6 if queue_samples else None
        native_mean = ((queue_nanos + dispatch) / requests / 1e6
                       if queue_samples == requests else None)
        waiting = sum(stage["exclusiveNanos"] for stage in stages if stage["stage"] in WAIT_STAGES)
        hold = sum(stage["inclusiveNanos"] for stage in stages if stage["stage"] == "authority_hold")
        rows.append({
            "operation": operation["operation"], "requests": requests, "errors": operation["errors"],
            "dispatch_ms": dispatch / 1e6, "mean_dispatch_ms": dispatch / requests / 1e6,
            "wait_ms": waiting / 1e6, "wait_percent": 100 * waiting / max(1, dispatch),
            "authority_hold_ms": hold / 1e6,
            "embedded_queue_ms": (queue_nanos or 0) / 1e6,
            "embedded_queue_samples": queue_samples,
            "mean_embedded_queue_ms": queue_mean,
            "mean_embedded_queue_plus_dispatch_ms": native_mean,
            "unattributed_ms": operation["unattributedNanos"] / 1e6,
            "largest_exclusive_stages": sorted(stages, key=lambda s: s["exclusiveNanos"], reverse=True)[:8],
            "work": operation["work"],
        })
    rows.sort(key=lambda row: row["dispatch_ms"], reverse=True)
    dispatch = sum(row["dispatch_ms"] for row in rows)
    wait = sum(row["wait_ms"] for row in rows)
    return {"format": "tofudb.profile-analysis/v1", "sample_every": profile["sampleEvery"],
            "selected": profile["selected"], "dropped": profile["dropped"],
            "requests": sum(row["requests"] for row in rows),
            "summed_dispatch_ms": dispatch, "summed_wait_ms": wait,
            "wait_percent": 100 * wait / max(dispatch, 1e-9),
            "scope": "sampled completed dispatch wall time; native mean adds embedded queue only with full matching coverage; excludes client scheduling, conversion and delivery; not CPU time or end-to-end latency",
            "operations": rows, "slow_samples": profile["slowSamples"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.report.stat().st_size > 64 * 1024 * 1024:
        parser.error("profile input exceeds 64 MiB")
    try:
        result = analyze(json.loads(args.report.read_text()))
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"{result['requests']} sampled completed requests; {result['dropped']} dropped; "
          f"{result['wait_percent']:.1f}% of summed dispatch time in identified waits")
    print("operation                              calls  dispatch ms  queue ms  native ms  queue coverage  wait %  hold ms/call")
    for row in result["operations"]:
        queue = row["mean_embedded_queue_ms"]
        native = row["mean_embedded_queue_plus_dispatch_ms"]
        queue_text = "--" if queue is None else f"{queue:.3f}"
        native_text = "--" if native is None else f"{native:.3f}"
        coverage = f"{row['embedded_queue_samples']}/{row['requests']}"
        print(f"{row['operation']:<38} {row['requests']:>5} {row['mean_dispatch_ms']:>12.3f} "
              f"{queue_text:>9} {native_text:>10} {coverage:>15} "
              f"{row['wait_percent']:>7.1f} {row['authority_hold_ms']/row['requests']:>13.3f}")
    print("dispatch/queue/native columns are means; native = embedded queue + dispatch, not end-to-end latency")
    print(result["scope"])


if __name__ == "__main__":
    main()
