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


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("matched window counters and samples must be nonnegative integers")
    return value


def _delta(left, right):
    value = _integer(right) - _integer(left)
    if value < 0:
        raise ValueError("matched window counter decreased; reset or different process")
    return value


def _indexed(rows, key):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("matched window contains duplicate labels")
    return result


def _profile_interval(before, after):
    """One subtraction owner for exact single- and multi-operation windows."""
    for profile in (before, after):
        analyze(profile)
        if type(profile["sampleEvery"]) is not int or profile["sampleEvery"] != 1:
            raise ValueError("matched window requires every request to be sampled")
        completed = sum(_integer(row["requests"]) for row in profile["operations"])
        if _integer(profile["selected"]) != completed + _integer(profile["dropped"]):
            raise ValueError("matched window endpoint has unsettled or inconsistent selected requests")
    if _delta(before["dropped"], after["dropped"]):
        raise ValueError("matched window has dropped request profiles")
    count = _delta(before["selected"], after["selected"])
    if not count or _delta(before["seen"], after["seen"]) != count:
        raise ValueError("profile population does not match completed window")
    old = _indexed(before["operations"], "operation")
    new = _indexed(after["operations"], "operation")
    rows = []
    for operation in sorted(old.keys() | new.keys()):
        left, right = old.get(operation, {}), new.get(operation, {})
        requests = _delta(left.get("requests", 0), right.get("requests", 0))
        row = {"operation": operation}
        for key in ("requests", "errors", "dispatchNanos", "embeddedQueueNanos",
                    "embeddedQueueSamples", "unattributedNanos", "invalidSpans"):
            before_value, after_value = left.get(key, 0), right.get(key, 0)
            if key == "embeddedQueueNanos":
                before_value = 0 if before_value is None else before_value
                after_value = 0 if after_value is None else after_value
            row[key] = _delta(before_value, after_value)
        previous_histogram = left.get("dispatchLog2NanosHistogram", [0] * len(right["dispatchLog2NanosHistogram"]))
        if len(previous_histogram) != len(right["dispatchLog2NanosHistogram"]):
            raise ValueError("matched window histogram layouts differ")
        row["dispatchLog2NanosHistogram"] = [_delta(a, b) for a, b in zip(previous_histogram, right["dispatchLog2NanosHistogram"])]
        old_stages = _indexed(left.get("stages", []), "stage")
        new_stages = _indexed(right["stages"], "stage")
        row["stages"] = [{"stage": name, **{key: _delta(old_stages.get(name, {}).get(key, 0), new_stages.get(name, {}).get(key, 0))
            for key in ("calls", "inclusiveNanos", "exclusiveNanos")}} for name in sorted(old_stages.keys() | new_stages.keys())]
        old_work, new_work = left.get("work", {}), right["work"]
        row["work"] = {name: _delta(old_work.get(name, 0), new_work.get(name, 0)) for name in old_work.keys() | new_work.keys()}
        if not requests:
            changed = (any(row[key] for key in ("errors", "dispatchNanos", "embeddedQueueNanos",
                "embeddedQueueSamples", "unattributedNanos", "invalidSpans"))
                or any(row["dispatchLog2NanosHistogram"])
                or any(stage[key] for stage in row["stages"]
                       for key in ("calls", "inclusiveNanos", "exclusiveNanos"))
                or any(row["work"].values()))
            if changed:
                raise ValueError("profile counters changed without a completed request")
            continue
        rows.append(row)
    if sum(row["requests"] for row in rows) != count:
        raise ValueError("profile population does not match completed window")
    interval = {**after, "selected": count, "dropped": 0, "operations": rows, "slowSamples": []}
    analyze(interval)  # The differences must reconcile independently too.
    return interval


def analyze_window(document: dict) -> dict:
    """Attribute the caller-owned, quiescent single-operation population."""
    operation = document["operation"]
    samples = document["samples_ns"]
    if not isinstance(operation, str) or not operation or not isinstance(samples, list) or not samples:
        raise ValueError("matched window requires an operation and nonempty caller samples")
    count = len(samples)
    if _integer(document["count"]) != count:
        raise ValueError("caller sample count does not match the declared window")
    outer = sum(_integer(value) for value in samples)
    interval = _profile_interval(document["metrics_before"]["requestProfile"], document["metrics_after"]["requestProfile"])
    if interval["selected"] != count or len(interval["operations"]) != 1 or interval["operations"][0]["operation"] != operation:
        raise ValueError("operation population does not match caller samples")
    row = interval["operations"][0]
    if row["embeddedQueueSamples"] != count or row["errors"]:
        raise ValueError("matched window requires complete embedded queue coverage and successful requests")
    result = analyze(interval)
    outside = outer - row["embeddedQueueNanos"] - row["dispatchNanos"]
    if outside < 0:
        raise ValueError("native time exceeds caller time; boundaries or populations do not match")
    result["matched_window"] = {"requests": count, "mean_caller_ns": outer / count,
        "mean_outside_queue_and_dispatch_ns": outside / count,
        "scope": "same completed profile-on request population; wall time, not CPU; outside includes caller conversion, delivery, scheduling and profiling overhead; global stages and lifetime maxima are excluded"}
    return result


def analyze_pipeline_window(document: dict) -> dict:
    """Refuse cross-restart, in-flight or lossy pipeline attribution."""
    import math

    keys = ("productive_start_storage_metrics", "storage_metrics")
    expected = ("recovered-before-provider-release", "completed-before-terminal-restart")
    stamps = []
    provenance = document["storage_metrics_provenance"]
    for key, boundary in zip(keys, expected):
        item = provenance[key]
        pid, birth, incarnation = item["process_id"], item["process_started_unix_seconds"], item["authority_incarnation"]
        if (type(pid) is not int or pid <= 0 or type(birth) not in (int, float)
                or not math.isfinite(birth) or birth <= 0 or type(incarnation) is not int or incarnation < 0):
            raise ValueError("pipeline window lacks valid process and authority identity")
        if item["boundary"] != boundary or item["benchmark_calls_quiescent"] is not True:
            raise ValueError("pipeline window boundaries are not quiescent event-work fences")
        stamps.append((pid, birth, incarnation))
    if stamps[0] != stamps[1]:
        raise ValueError("pipeline window crosses a process or authority incarnation")
    interval = _profile_interval(*(document[key]["requestProfile"] for key in keys))
    result = analyze(interval)
    result["pipeline_window"] = {"metrics_keys": list(keys), "process_id": stamps[0][0],
        "process_started_unix_seconds": stamps[0][1], "authority_incarnation": stamps[0][2],
        "scope": "same authority/process and completed sampled operations, including retries/errors; summed elapsed spans are not CPU or critical-path time; instrumentation does not prove equivalence to default dispatch scheduling"}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--json", action="store_true")
    windows = parser.add_mutually_exclusive_group()
    windows.add_argument("--window", action="store_true", help="validate and subtract a matched caller window")
    windows.add_argument("--pipeline-window", action="store_true", help="require quiescent same-incarnation event-work snapshots")
    args = parser.parse_args()
    if args.report.stat().st_size > 64 * 1024 * 1024:
        parser.error("profile input exceeds 64 MiB")
    try:
        reader = analyze_pipeline_window if args.pipeline_window else analyze_window if args.window else analyze
        result = reader(json.loads(args.report.read_text()))
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
    if "matched_window" in result:
        window = result["matched_window"]
        print(f"matched caller mean {window['mean_caller_ns'] / 1000:.3f} us; "
              f"outside queue/dispatch {window['mean_outside_queue_and_dispatch_ns'] / 1000:.3f} us")
        print(window["scope"])
    if "pipeline_window" in result:
        window = result["pipeline_window"]
        print(f"pipeline process {window['process_id']}; authority incarnation {window['authority_incarnation']}; "
              f"metrics: {' -> '.join(window['metrics_keys'])}")
        print(window["scope"])


if __name__ == "__main__":
    main()
