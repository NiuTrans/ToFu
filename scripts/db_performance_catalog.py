#!/usr/bin/env python3
"""Offline evidence catalog: import/list/report; no database or network access.

Authority: contracts/db_performance_catalog_v1.json (jsonschema dependency).
Content-addressed owner namespaces retain source bytes and normalized runs.
Problem: mutable receipts and unlike workloads invite false rankings. Decision:
verify immutable snapshots on every read, compare explicit identities, retain
all gates and raw windows. Alternative rejected: a universal speedup score.
"""
import argparse
import hashlib
import heapq
import html
import json
import math
import os
from pathlib import Path
import re
import statistics
import stat
import sys
import tempfile
import textwrap

from jsonschema import Draft202012Validator

PROJECT = Path(__file__).resolve().parents[1]
FORMAT = "db-performance-catalog/v1"
MAX_SOURCE_BYTES = 16 * 1024 * 1024

MAX_OUTPUT_BYTES = MAX_SOURCE_BYTES
SCHEMA = json.loads((PROJECT / "contracts/db_performance_catalog_v1.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


class CatalogError(ValueError):
    """Invalid evidence, unsafe paths, or an invalid comparison."""


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise CatalogError("unsafe name: " + value)
    return value


def identifier(value):
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CatalogError("ID must be a lowercase SHA256 digest")
    return value


def safe_path(path, create=False):
    """Refuse lexical traversal and symlinks, including ancestor symlinks.

    Catalog directories are operator-owned, not writable by untrusted users;
    this is not an authorization boundary against hostile concurrent renames.
    """
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise CatalogError("path traversal refused")
    path = path.absolute()
    for component in reversed([path, *path.parents]):
        if component.is_symlink():
            raise CatalogError("symlink refused: " + str(component))
    if create:
        path.mkdir(parents=True, exist_ok=True)
        # Persist directory creation, including newly created ancestors.
        for component in [path, *path.parents]:
            if component.parent == component:
                break
            fsync_directory(component.parent)
    return path


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def immutable_write(path, data):
    """Publish fully synced bytes without replacing an existing inode."""
    path = safe_path(path)
    safe_path(path.parent, create=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_bytes(path) != data:
                raise CatalogError("immutable object conflict: " + str(path))
        fsync_directory(path.parent)
    finally:
        os.unlink(temporary)
        fsync_directory(path.parent)


def read_bytes(path):
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise CatalogError("only regular files are accepted")
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise CatalogError("evidence/object exceeds 16 MiB")
    return data


def parse(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CatalogError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def invalid(value):
        raise CatalogError("non-finite JSON number: " + value)

    return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)


def validate(payload):
    error = next(VALIDATOR.iter_errors(payload), None)
    if error is not None:
        raise CatalogError("invalid evidence: " + error.message)
    def check_strings(value):
        if isinstance(value, str):
            if any(not (char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF) for char in value):
                raise CatalogError("XML-forbidden codepoint in evidence")
        elif isinstance(value, dict):
            for key, item in value.items():
                check_strings(key)
                check_strings(item)
        elif isinstance(value, list):
            for item in value:
                check_strings(item)
    check_strings(payload)
    if sum(len(result["windows"]) for result in payload["results"]) > 10000:
        raise CatalogError("too many total windows (maximum 10000)")
    # Reject overflow numbers and duplicate dimensions/windows, not just NaN.
    canonical(payload)
    keys = set()
    for result in payload["results"]:
        key = (result["backend"], result["metric"])
        if key in keys:
            raise CatalogError("duplicate backend/metric")
        keys.add(key)
        labels = set()
        for window in result["windows"]:
            if not math.isfinite(window["value"]):
                raise CatalogError("non-finite measurement")
            if window["label"] in labels:
                raise CatalogError("duplicate window label")
            labels.add(window["label"])
    return payload


def normalize(source):
    try:
        return normalize_payload(source)
    except CatalogError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        raise CatalogError("malformed evidence: " + str(exc)) from exc


def budget_gate(acceptance, metric, value):
    recorded = acceptance.get("metrics", {}).get(metric, {})
    passed = recorded.get("passed")
    if passed is False:
        return False
    if passed is not None and passed is not True:
        raise CatalogError("invalid budget gate")
    if any(field not in recorded for field in ("p99_ms", "budget_ms", "samples", "expected_samples")):
        return None
    budget, samples, expected = recorded["budget_ms"], recorded["samples"], recorded["expected_samples"]
    if (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0 or
            type(samples) is not int or type(expected) is not int or expected <= 0 or samples != expected or
            recorded["p99_ms"] != value or value > budget):
        return False
    return passed


def normalize_payload(source):
    if not isinstance(source, dict):
        raise CatalogError("source must be a JSON object")
    if source.get("format") == FORMAT:
        return validate(source)
    results = []
    if "report" in source and isinstance(source["report"], dict) and "timings" in source["report"]:
        report = source["report"]
        scope = report["scope"]

        timings = report["timings"]
        if not timings or {row["backend"] for row in timings} != {"native", "sqlite"}:
            raise CatalogError("hydration requires native/sqlite backend identities")
        for row in timings:
            if type(row["round"]) is not int or row["round"] < 0 or type(row["calls"]) is not int or row["calls"] <= 0:
                raise CatalogError("invalid hydration window")
        if {r["round"] for r in timings if r["backend"] == "native"} != {r["round"] for r in timings if r["backend"] == "sqlite"}:
            raise CatalogError("unmatched hydration windows")

        native_calls = {r["round"]: r["calls"] for r in timings if r["backend"] == "native"}
        sqlite_calls = {r["round"]: r["calls"] for r in timings if r["backend"] == "sqlite"}
        if native_calls != sqlite_calls:
            raise CatalogError("hydration call-count mismatch")
        correctness = report.get("exact_response_equal")
        if report.get("difference_paths") or len(set(report.get("bytes", {}).values())) > 1:
            correctness = False
        elif "difference_paths" not in report or set(report.get("bytes", {})) != {"native", "sqlite"}:
            correctness = None
        for backend in sorted({row["backend"] for row in report["timings"]}):
            results.append({"backend": backend,
                            "backend_version": source.get("native_source", "unknown") if backend == "native" else "unknown",
                            "metric": "hydration_mean", "unit": "ms", "direction": "lower",
                            "aggregation": "median_windows", "windows": [
                                {"label": str(row["round"]), "value": row["mean_ms"],
                                 "gates": {"correctness": correctness,
                                           "coverage": None, "budget": None}, "raw": row}
                                for row in report["timings"] if row["backend"] == backend]})
        scenario = {"kind": "exact-semantic-hydration", "conversations": report["conversations"],
                    "turns": report["turns"], "scope": scope}
        limits = source.get("limits", []) + [source.get("correction", "")]
    elif "rows" in source and "scope" in source:
        scope = source["scope"]
        grouped = {}

        rows = source["rows"]
        backends = {row["authority"]["backend"] for row in rows}
        if "sqlite" not in backends or not backends.intersection({"tofu", "tofudb"}) or not backends.issubset({"sqlite", "tofu", "tofudb"}):
            raise CatalogError("direct pair requires tofu/tofudb and sqlite backend identities")
        metric_names = set(rows[0]["p99_ms"])
        if not metric_names or not metric_names.issubset({"submit_to_running", "stream_scheduled", "settle"}):
            raise CatalogError("unsupported direct pair metrics")
        for row in source["rows"]:
            backend = row["authority"]["backend"]

            if set(row["p99_ms"]) != metric_names:
                raise CatalogError("direct pair metric mismatch")
            backend = "native" if backend in ("tofu", "tofudb") else backend
            version = row["authority"].get("build_identity", {}).get("version", "unknown")
            version += ":" + row.get("source_scope", {}).get("native_source", "unknown") if backend == "native" else ""
            for metric, value in row["p99_ms"].items():
                result = grouped.setdefault((backend, metric), {
                    "backend": backend, "backend_version": version, "metric": metric + "_p99",
                    "unit": "ms", "direction": "lower", "aggregation": "median_windows", "windows": []})
                if result["backend_version"] != version:
                    raise CatalogError("mixed backend versions in evidence")
                acceptance = row.get("acceptance", {})
                correctness = None
                if row.get("correctness") is False or acceptance.get("correctness_passed") is False or row.get("errors", 0) != 0:
                    correctness = False
                elif row.get("correctness") is True and acceptance.get("correctness_passed") is True and type(row.get("errors")) is int and row["errors"] == 0:
                    correctness = True
                coverage = acceptance.get("coverage_passed")
                if coverage is True and (acceptance.get("coverage_gaps") or acceptance.get("uncovered")):
                    coverage = False
                result["windows"].append({"label": row["label"], "value": value,
                    "gates": {"correctness": correctness, "coverage": coverage,
                              "budget": budget_gate(acceptance, metric, value)}, "raw": row})
        results = list(grouped.values())
        scenario = {"kind": "direct-semantic-concurrency", "scope": scope}
        limits = source.get("limitations", []) + [source.get("decision", "")]
    else:
        raise CatalogError("unsupported source format")
    return validate({"format": FORMAT, "scenario": scenario,
                     "environment": {"recorded_scope": scope, "independent_environment_identity": "unknown"},
                     "scope": scope, "limits": limits + ["Environment identity is limited to the recorded scope; hardware equivalence is not independently certified."],
                     "source_metadata": {key: value for key, value in source.items() if key != "rows" and key != "report"},
                     "results": results})


class Catalog:
    def __init__(self, root, owner, version):
        self.owner, self.version = name(owner), name(version)
        self.root = safe_path(root)
        self.namespace = self.root / self.owner
        self.runs = self.namespace / self.version / "runs"

    def import_source(self, source_path):
        raw = read_bytes(source_path)
        payload = normalize(parse(raw))
        source_sha = digest(raw)
        run = {"owner": self.owner, "version": self.version, "source_sha256": source_sha,
               "evidence": payload}
        encoded = canonical(run)

        if len(encoded) > MAX_SOURCE_BYTES:
            raise CatalogError("normalized run exceeds 16 MiB")
        run_id = digest(encoded)
        immutable_write(self.namespace / "sources" / (source_sha + ".json"), raw)
        immutable_write(self.runs / (run_id + ".json"), encoded)
        self.load(run_id)
        return run_id

    def load(self, run_id, version=None):
        run_id = identifier(run_id)
        version = name(version or self.version)
        raw = read_bytes(self.namespace / version / "runs" / (run_id + ".json"))
        if digest(raw) != run_id:
            raise CatalogError("normalized run SHA256 integrity failure")
        run = parse(raw)
        if run["owner"] != self.owner or run["version"] != version:
            raise CatalogError("owner/version mismatch")
        source_sha = identifier(run["source_sha256"])
        source = read_bytes(self.namespace / "sources" / (source_sha + ".json"))
        if digest(source) != source_sha:
            raise CatalogError("source SHA256 integrity failure")
        if normalize(parse(source)) != run["evidence"]:
            raise CatalogError("normalization provenance mismatch")
        return run

    def list_runs(self, limit=100, offset=0):
        if not 1 <= limit <= 1000 or not 0 <= offset <= 10000:
            raise CatalogError("list limit must be 1..1000; offset must be 0..10000")
        safe_path(self.runs)
        if not self.runs.exists():
            return []
        # Streaming enumeration with bounded selection, not a full run load.
        with os.scandir(self.runs) as entries:
            names = heapq.nsmallest(offset + limit, (entry.name for entry in entries if entry.name.endswith(".json")))
        summaries = []
        for filename in names[offset:]:
            run_id = Path(filename).stem
            run = self.load(run_id)
            summaries.append({"id": run_id, "owner": run["owner"], "version": run["version"],
                              "source_sha256": run["source_sha256"], "scope": run["evidence"]["scope"],
                              "dimensions": [{key: result[key] for key in ("backend", "backend_version", "metric", "unit", "direction")} for result in run["evidence"]["results"]]})
        return summaries

    def report(self, run_id, baseline, candidate, baseline_id=None, baseline_version=None, output_dir=None):
        candidate_run = self.load(run_id)
        baseline_run = self.load(baseline_id or run_id, baseline_version)
        report = compare(baseline_run, candidate_run, name(baseline), name(candidate))
        report["provenance"] = {"baseline": {"id": baseline_id or run_id, **baseline_run},
                                "candidate": {"id": run_id, **candidate_run}}
        encoded = canonical(report)
        report_id = digest(encoded)
        output = safe_path(output_dir or self.namespace / self.version / "reports", create=True)
        outputs = (("svg", render_svg(report).encode()), ("md", render_markdown(report).encode()),
                   ("json", encoded), ("rich.json", canonical(render_rich_response(report))))
        if any(len(data) > MAX_OUTPUT_BYTES for _, data in outputs):
            raise CatalogError("report output exceeds 16 MiB")
        for suffix, data in outputs:
            immutable_write(output / (report_id + "." + suffix), data)
        return {"report_id": report_id, "files": {suffix: str(output / (report_id + "." + suffix)) for suffix in ("svg", "md", "json", "rich.json")}, "comparison": report}


def compare(baseline_run, candidate_run, baseline, candidate):
    left, right = baseline_run["evidence"], candidate_run["evidence"]

    if (left["environment"].get("independent_environment_identity") == "unknown" or
            right["environment"].get("independent_environment_identity") == "unknown") and baseline_run["source_sha256"] != candidate_run["source_sha256"]:
        raise CatalogError("unknown environment: historical comparisons require the same source pair")
    for identity in ("scenario", "environment", "scope"):
        if left[identity] != right[identity]:
            raise CatalogError(identity + " mismatch; comparison refused")
    a = {r["metric"]: r for r in left["results"] if r["backend"] == baseline}
    b = {r["metric"]: r for r in right["results"] if r["backend"] == candidate}
    if not a or set(a) != set(b):
        raise CatalogError("missing backend or metric mismatch")
    rows = []
    for metric in sorted(a):
        for field in ("unit", "direction", "aggregation"):
            if a[metric][field] != b[metric][field]:
                raise CatalogError(field + " mismatch")
        for result in (a[metric], b[metric]):
            if any(w["gates"]["correctness"] is not True for w in result["windows"]):
                raise CatalogError("failed/unknown correctness; invalid comparison denominator")
        av = statistics.median(w["value"] for w in a[metric]["windows"])
        bv = statistics.median(w["value"] for w in b[metric]["windows"])

        if not math.isfinite(av) or not math.isfinite(bv):
            raise CatalogError("non-finite aggregate; comparison refused")
        if av <= 0:
            raise CatalogError("zero baseline; normalized ratio undefined")

        if not math.isfinite(bv / av):
            raise CatalogError("normalized ratio is non-finite")
        rows.append({"metric": metric, "unit": a[metric]["unit"], "direction": a[metric]["direction"],
                     "aggregation": "median_windows", "baseline_value": av, "candidate_value": bv,
                     "baseline_normalized": 1, "candidate_normalized": bv / av,
                     "baseline": a[metric], "candidate": b[metric]})
    return {"format": "db-performance-report/v1", "baseline": baseline, "candidate": candidate,
            "scenario": left["scenario"], "environment": left["environment"], "scope": left["scope"],
            "limits": list(dict.fromkeys(left["limits"] + right["limits"] + [
                "Bars show candidate/baseline in original units; baseline = 1. Direction determines which is better.",
                "Median of recorded window statistics, not a pooled request statistic or confidence interval. Raw windows and gates remain binding.",
                "Unknown coverage is not a pass; faster measurements do not erase failed budgets. No overall aggregate."])),

            "metrics": rows}


def render_rich_response(report):
    """Export presentation data, not model-generated markup or asset URLs.

    Values/gates come from the measured report. Source identity and full raw
    evidence stay in its adjacent JSON receipt; no missing gate becomes pass.
    """
    rows = []
    for row in report["metrics"]:
        windows = row["candidate"]["windows"]
        gates = [value for window in windows for key, value in window["gates"].items()
                 if key in ("correctness", "coverage", "budget")]
        status = "failed" if any(value is False for value in gates) else (
            "passed" if gates and all(value is True for value in gates) else "unknown")
        metric = {"label": row["metric"], "baseline": row["baseline_value"],
                  "candidate": row["candidate_value"], "unit": row["unit"],
                  "direction": row["direction"], "status": status,
                  "note": "Median of window statistics; not pooled P99. Candidate gates: " + status}
        metric_name = row["metric"][:-4] if row["metric"].endswith("_p99") else row["metric"]
        budgets = {window["raw"].get("budget_ms", window["raw"].get("acceptance", {}).get("metrics", {}).get(metric_name, {}).get("budget_ms"))
                   for window in windows}
        if len(budgets) == 1 and None not in budgets:
            metric["budget"] = next(iter(budgets))

            if (row["direction"] == "lower" and metric["candidate"] > metric["budget"]) or (
                    row["direction"] == "higher" and metric["candidate"] < metric["budget"]):
                metric["status"] = "failed"
        rows.append(metric)
    blocks = []
    for offset in range(0, len(rows), 32):
        blocks.append({"type": "comparison", "id": "metrics-" + str(offset),
                       "title": "Measured performance comparison",
                       "baselineLabel": report["baseline"], "candidateLabel": report["candidate"],
                       "metrics": rows[offset:offset + 32]})
    if len(blocks) > 22:
        raise CatalogError("rich response exceeds block limit; select a smaller measured cohort")
    blocks.append({"type": "evidence", "id": "measurement-scope", "title": "Scope and limitations",
                   "items": [{"label": "Scope", "value": report["scope"][:1000]}] +
                   [{"label": "Limit", "value": limit[:1000]} for limit in report["limits"][:31]]})
    if report.get("provenance"):
        blocks.append({"type": "evidence", "id": "measurement-provenance", "title": "Recorded builds and sources",
                       "items": [{"label": side, "value": "Run " + value["id"] + "; source SHA256 " +
                                  value["source_sha256"] + "; version " + value["version"]}
                                 for side, value in report["provenance"].items()]})
    payload = {"version": 1, "title": "Database performance comparison",
               "summary": "Historical measured cohort. Relative improvement does not imply release acceptance.",
               "blocks": blocks}
    schema = parse((PROJECT / "contracts/rich_response_v1.schema.json").read_bytes())
    error = next(Draft202012Validator(schema).iter_errors(payload), None)
    if error:
        raise CatalogError("rich response cannot represent this report: " + error.message[:300])
    return payload


def render_markdown(report):
    # Escape all untrusted prose, including Markdown delimiters.
    def text(value):
        escaped = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
        return re.sub(r"[\\\\`|\[\]()!*_#+~.-]", lambda match: "&#" + str(ord(match.group())) + ";", escaped)
    lines = ["# Offline database performance comparison", "", text(report["scope"]), "",
             "| Metric | Unit | Direction | Baseline median | Candidate median | Candidate / baseline |",
             "|---|---|---|---:|---:|---:|"]
    for row in report["metrics"]:
        lines.append("| " + " | ".join(text(row[k]) for k in ("metric", "unit", "direction", "baseline_value", "candidate_value", "candidate_normalized")) + " |")
    lines += ["", "## Limits", *["- " + text(limit) for limit in report["limits"]], "", "## Raw windows, gates and provenance", "", "```json", json.dumps(report, indent=2, ensure_ascii=True).replace("`", "\\u0060"), "```", ""]
    return "\n".join(lines)


def render_svg(report):
    elements = ['<title>Offline performance comparison; baseline = 1; no overall aggregate</title>',
                '<rect width="100%" height="100%" fill="white"/>',
                '<desc>' + html.escape(json.dumps(report, ensure_ascii=True)) + '</desc>']
    y = 25

    def label(value):
        nonlocal y
        for line in textwrap.wrap(str(value), width=120) or [""]:
            elements.append(f'<text x="20" y="{y}" font-family="monospace" font-size="12">{html.escape(line)}</text>')
            y += 18

    label(f'{report["baseline"]} baseline = 1; {report["candidate"]} candidate/baseline; median windows')
    label(report["scope"])
    scale = 800 / max(1, *(r["candidate_normalized"] for r in report["metrics"]))
    for row in report["metrics"]:
        y += 15
        label(f'{row["metric"]} ({row["unit"]}, {row["direction"]} is better)')
        for key, color in (("baseline", "#627d98"), ("candidate", "#267b73")):
            value, ratio = row[key + "_value"], row[key + "_normalized"]
            label(f'{key}: {value:.12g} {row["unit"]}; normalized = {ratio:.6g}; backend version = {row[key]["backend_version"]}')
            elements.append(f'<rect x="20" y="{y}" width="{ratio * scale:.4f}" height="18" fill="{color}"/>')
            y += 38
            for window in row[key]["windows"]:
                label(f'window {window["label"]}: {window["value"]} {row["unit"]}; gates = ' + json.dumps(window["gates"], sort_keys=True))
    y += 15
    label("Limits (complete raw records are also in SVG description, Markdown and JSON):")
    for limit in report["limits"]:
        label("- " + limit)
    if "provenance" in report:
        for key, run in report["provenance"].items():
            label(f'{key}: owner={run["owner"]}; version={run["version"]}; run ID={run["id"]}')
            label("source SHA256=" + run["source_sha256"])
    height = y + 10
    elements.insert(0, f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}" role="img">')
    elements.append('</svg>')
    return "\n".join(elements)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "list", "report"):
        sub = commands.add_parser(command)
        sub.add_argument("--owner", required=True)
        sub.add_argument("--version", required=True)
        sub.add_argument("--catalog", default=str(PROJECT / "data/benchmarks/performance"),
                         help="Catalog root; pass the same canonical path in every checkout for a shared centralized catalog")
        if command == "list":
            sub.add_argument("--limit", type=int, default=100)
            sub.add_argument("--offset", type=int, default=0)
        if command == "import":
            sub.add_argument("--source", required=True)
        if command == "report":
            sub.add_argument("--id", required=True)
            sub.add_argument("--baseline", required=True)
            sub.add_argument("--candidate", required=True)
            sub.add_argument("--baseline-id")
            sub.add_argument("--baseline-version")
            sub.add_argument("--output-dir")
    args = parser.parse_args(argv)
    try:
        catalog = Catalog(args.catalog, args.owner, args.version)
        if args.command == "import":
            result = {"id": catalog.import_source(args.source)}
        elif args.command == "list":
            result = catalog.list_runs(args.limit, args.offset)
        else:
            if args.baseline_version and not args.baseline_id:
                raise CatalogError("--baseline-version requires --baseline-id")
            result = catalog.report(args.id, args.baseline, args.candidate, args.baseline_id,
                                    args.baseline_version, args.output_dir)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (CatalogError, OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        print("catalog error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
