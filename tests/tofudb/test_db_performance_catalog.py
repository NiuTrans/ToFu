"""Executable specification for the offline catalog; synthetic evidence only."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/db_performance_catalog.py"
spec = importlib.util.spec_from_file_location("db_performance_catalog", SCRIPT)
catalog_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog_module)
pytestmark = pytest.mark.unit


def generic():
    def result(backend, values):
        return {"backend": backend, "backend_version": "engine-1", "metric": "latency_p99",
                "unit": "ms", "direction": "lower", "aggregation": "median_windows",
                "windows": [{"label": str(i), "value": value,
                             "gates": {"correctness": True, "coverage": None, "budget": False},
                             "raw": {"budget_ms": 1, "samples": 10}}
                            for i, value in enumerate(values)]}
    return {"format": catalog_module.FORMAT, "scenario": {"fixture": "fixed"},
            "environment": {"machine": "synthetic", "durability": "strict"},
            "scope": "synthetic window statistics only", "limits": ["No crash coverage"],
            "results": [result("sqlite", [8, 12]), result("native", [2, 4])]}


def hydration():
    return {"report": {"scope": "exact synthetic warm reads; no crash/concurrent claim",
                       "conversations": 2, "turns": 4, "exact_response_equal": True,
                       "difference_paths": [], "bytes": {"native": 100, "sqlite": 100},
                       "timings": [{"round": i, "backend": backend, "mean_ms": value, "calls": 30}
                                   for backend, values in (("native", [2, 4]), ("sqlite", [8, 12]))
                                   for i, value in enumerate(values)]},
            "native_source": "a" * 64, "probe_sha256": "b" * 64, "limits": ["warm only"]}


def direct():
    return {"scope": "S-N-N-S synthetic full application workload; graceful reopen only",
            "limitations": ["No process crash"], "rows": [
                {"label": f"{backend}-{i}", "authority": {"backend": backend},
                 "correctness": True, "errors": 0, "p99_ms": {"stream_scheduled": value},
                 "acceptance": {"correctness_passed": True, "coverage_passed": False,
                                "metrics": {"stream_scheduled": {"passed": False, "budget_ms": 1}},
                                "uncovered": ["process-crash"], "passed": False}}
                for backend, values in (("tofu", [2, 4]), ("sqlite", [8, 12]))
                for i, value in enumerate(values)]}


@pytest.fixture
def catalog(tmp_path):
    return catalog_module.Catalog(tmp_path / "catalog", "operator", "release-1")


def put(catalog, tmp_path, payload, filename="source.json"):
    source = tmp_path / filename
    source.write_text(json.dumps(payload))
    return catalog.import_source(source)


@pytest.mark.parametrize("fixture", [generic, hydration, direct])
def test_import_idempotency_and_snapshot(catalog, tmp_path, fixture):
    run_id = put(catalog, tmp_path, fixture())
    assert put(catalog, tmp_path, fixture()) == run_id
    (tmp_path / "source.json").unlink()
    assert catalog.load(run_id)["owner"] == "operator"
    assert [r["id"] for r in catalog.list_runs()] == [run_id]
    assert len(list(catalog.runs.glob("*.json"))) == 1
    assert not list(catalog.runs.glob(".pending-*"))


@pytest.mark.parametrize("fixture", [generic, hydration, direct])
def test_report_values_gates_provenance_and_exports(catalog, tmp_path, fixture):
    run_id = put(catalog, tmp_path, fixture())
    result = catalog.report(run_id, "sqlite", "native")
    report = result["comparison"]
    metric = report["metrics"][0]
    assert metric["baseline_value"] == 10
    assert metric["candidate_value"] == 3
    assert metric["candidate_normalized"] == 0.3
    assert metric["baseline_normalized"] == 1
    assert metric["unit"] == "ms"
    assert metric["candidate"]["windows"][0]["gates"]["budget"] is (None if fixture is hydration else False)
    assert report["provenance"]["candidate"]["id"] == run_id
    assert catalog.report(run_id, "sqlite", "native")["report_id"] == result["report_id"]
    svg = Path(result["files"]["svg"]).read_text()
    assert ET.fromstring(svg).tag.endswith("svg")
    assert "baseline = 1" in svg
    markdown = Path(result["files"]["md"]).read_text()
    assert "Raw windows, gates and provenance" in markdown
    assert "No overall aggregate" in markdown
    assert json.loads(Path(result["files"]["json"]).read_text()) == report


def test_cross_version_report(catalog, tmp_path):
    baseline_id = put(catalog, tmp_path, generic())
    next_catalog = catalog_module.Catalog(catalog.root, "operator", "release-2")
    payload = generic()
    payload["results"][1]["backend_version"] = "engine-2"
    payload["results"][1]["windows"][0]["value"] = 1
    candidate_id = put(next_catalog, tmp_path, payload, "next.json")
    report = next_catalog.report(candidate_id, "native", "native", baseline_id, "release-1")["comparison"]
    assert report["metrics"][0]["baseline_value"] == 3
    assert report["metrics"][0]["candidate_value"] == 2.5
    assert report["provenance"]["baseline"]["version"] == "release-1"
    with pytest.raises(FileNotFoundError):
        next_catalog.load(baseline_id)
    assert next_catalog.list_runs()[0]["id"] == candidate_id


@pytest.mark.parametrize("field,value", [("scenario", {"fixture": "different"}),
                                          ("environment", {"machine": "different"}),
                                          ("scope", "different workload")])
def test_identity_mismatch_refused(catalog, tmp_path, field, value):
    baseline_id = put(catalog, tmp_path, generic())
    payload = generic()
    payload[field] = value
    candidate_id = put(catalog, tmp_path, payload, "other.json")
    with pytest.raises(catalog_module.CatalogError, match="mismatch"):
        catalog.report(candidate_id, "sqlite", "native", baseline_id)


@pytest.mark.parametrize("field,value", [("unit", "seconds"), ("direction", "higher"),
                                          ("metric", "other")])
def test_metric_mismatch_refused(catalog, tmp_path, field, value):
    payload = generic()
    payload["results"][1][field] = value
    run_id = put(catalog, tmp_path, payload)
    with pytest.raises(catalog_module.CatalogError, match="mismatch"):
        catalog.report(run_id, "sqlite", "native")


@pytest.mark.parametrize("backend_index", [0, 1])
@pytest.mark.parametrize("correctness", [False, None])
def test_correctness_denominators_refused(catalog, tmp_path, backend_index, correctness):
    payload = generic()
    payload["results"][backend_index]["windows"][0]["gates"]["correctness"] = correctness
    run_id = put(catalog, tmp_path, payload)
    assert catalog.list_runs()  # failed runs are retained, not discarded
    with pytest.raises(catalog_module.CatalogError, match="correctness"):
        catalog.report(run_id, "sqlite", "native")


@pytest.mark.parametrize("mutation", ["errors", "acceptance", "differences", "bytes"])
def test_adapter_correctness_conflicts(mutation):
    payload = hydration() if mutation in ("differences", "bytes") else direct()
    if mutation == "errors":
        payload["rows"][0]["errors"] = 1
    elif mutation == "acceptance":
        payload["rows"][0]["acceptance"]["correctness_passed"] = False
    elif mutation == "differences":
        payload["report"]["difference_paths"] = ["turns"]
    else:
        payload["report"]["bytes"]["native"] = 101
    normalized = catalog_module.normalize(payload)
    assert any(w["gates"]["correctness"] is False for r in normalized["results"] for w in r["windows"])


@pytest.mark.parametrize("object_kind", ["source", "run"])
def test_integrity_failure(catalog, tmp_path, object_kind):
    run_id = put(catalog, tmp_path, generic())
    run = catalog.load(run_id)
    path = catalog.runs / (run_id + ".json") if object_kind == "run" else catalog.namespace / "sources" / (run["source_sha256"] + ".json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(catalog_module.CatalogError, match="integrity"):
        catalog.load(run_id)
    with pytest.raises(catalog_module.CatalogError, match="integrity"):
        catalog.list_runs()
    with pytest.raises(catalog_module.CatalogError):
        catalog.report(run_id, "sqlite", "native")


def test_resealed_provenance_failure(catalog, tmp_path):
    run_id = put(catalog, tmp_path, generic())
    run = catalog.load(run_id)
    run["evidence"]["scope"] = "invented"
    raw = catalog_module.canonical(run)
    resealed = catalog_module.digest(raw)
    catalog_module.immutable_write(catalog.runs / (resealed + ".json"), raw)
    with pytest.raises(catalog_module.CatalogError, match="provenance"):
        catalog.load(resealed)


@pytest.mark.parametrize("bad", ["../escape", "/absolute", ".", "..", "a/b", "a\\b", "", "a\n", "a" * 129])
def test_safe_owner_version_names(tmp_path, bad):
    for owner, version in ((bad, "v1"), ("owner", bad)):
        with pytest.raises(catalog_module.CatalogError, match="unsafe"):
            catalog_module.Catalog(tmp_path, owner, version)


@pytest.mark.parametrize("bad", ["../escape", "a" * 63, "A" * 64, "/absolute"])
def test_safe_ids(catalog, bad):
    with pytest.raises(catalog_module.CatalogError, match="SHA256"):
        catalog.load(bad)


def test_path_traversal_and_symlinks(catalog, tmp_path):
    with pytest.raises(catalog_module.CatalogError, match="traversal"):
        catalog_module.Catalog(tmp_path / "../escape", "owner", "v1")
    outside = tmp_path / "outside"
    outside.mkdir()
    catalog.root.mkdir()
    (catalog.root / "operator").symlink_to(outside, target_is_directory=True)
    with pytest.raises(catalog_module.CatalogError, match="symlink"):
        put(catalog, tmp_path, generic())
    assert not list(outside.iterdir())
    source = tmp_path / "source-link.json"
    source.symlink_to(tmp_path / "source.json")
    with pytest.raises(catalog_module.CatalogError, match="symlink"):
        catalog.import_source(source)


def test_symlink_objects_and_output(catalog, tmp_path):
    run_id = put(catalog, tmp_path, generic())
    outside = tmp_path / "outside.json"
    run_file = catalog.runs / (run_id + ".json")
    outside.write_bytes(run_file.read_bytes())
    run_file.unlink()
    run_file.symlink_to(outside)
    with pytest.raises(catalog_module.CatalogError, match="symlink"):
        catalog.load(run_id)
    run_file.unlink()
    run_file.write_bytes(outside.read_bytes())
    output = tmp_path / "output"
    output.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(catalog_module.CatalogError, match="symlink"):
        catalog.report(run_id, "sqlite", "native", output_dir=output)


@pytest.mark.parametrize("mutation", ["empty", "duplicate", "duplicate_window", "negative", "nonfinite", "aggregation", "missing_gate", "unknown_field"])
def test_invalid_generic(catalog, tmp_path, mutation):
    payload = generic()
    result = payload["results"][0]
    if mutation == "empty":
        result["windows"] = []
    elif mutation == "duplicate":
        payload["results"].append(copy.deepcopy(result))
    elif mutation == "duplicate_window":
        result["windows"].append(copy.deepcopy(result["windows"][0]))
    elif mutation == "negative":
        result["windows"][0]["value"] = -1
    elif mutation == "nonfinite":
        result["windows"][0]["value"] = float("inf")
    elif mutation == "aggregation":
        result["aggregation"] = "pooled_p99"
    elif mutation == "missing_gate":
        del result["windows"][0]["gates"]["correctness"]
    else:
        payload["extra"] = "not v1"
    with pytest.raises((catalog_module.CatalogError, ValueError)):
        put(catalog, tmp_path, payload)
    assert not catalog.runs.exists()


@pytest.mark.parametrize("data", [b'{}', b'{"format":1,"format":2}', b'{', b'{"format":"v2"}', b'[]', b'{"x":1e999}'])
def test_bad_json_cli(catalog, tmp_path, data, capsys):
    source = tmp_path / "bad.json"
    source.write_bytes(data)
    assert catalog_module.main(["import", "--owner", "operator", "--version", "v1", "--catalog", str(catalog.root), "--source", str(source)]) == 2
    assert "catalog error" in capsys.readouterr().err


def test_zero_baseline_missing_backend_and_overflow(catalog, tmp_path):
    payload = generic()
    for window in payload["results"][0]["windows"]:
        window["value"] = 0
    run_id = put(catalog, tmp_path, payload)
    with pytest.raises(catalog_module.CatalogError, match="zero"):
        catalog.report(run_id, "sqlite", "native")
    with pytest.raises(catalog_module.CatalogError, match="missing"):
        catalog.report(run_id, "missing", "native")
    for window in payload["results"][0]["windows"]:
        window["value"] = 1e-300
    for window in payload["results"][1]["windows"]:
        window["value"] = 1e300
    run_id = put(catalog, tmp_path, payload)
    with pytest.raises(catalog_module.CatalogError, match="non-finite"):
        catalog.report(run_id, "sqlite", "native")


def test_atomic_failure_and_immutable_conflict(catalog, tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(generic()))
    original = catalog_module.os.link
    def fail_publish(*args):
        raise OSError("simulated publication failure")
    monkeypatch.setattr(catalog_module.os, "link", fail_publish)
    with pytest.raises(OSError, match="publication"):
        catalog.import_source(source)
    assert not list(catalog.root.rglob("*.json"))
    assert not list(catalog.root.rglob(".pending-*"))
    monkeypatch.setattr(catalog_module.os, "link", original)
    run_id = catalog.import_source(source)
    path = catalog.runs / (run_id + ".json")
    with pytest.raises(catalog_module.CatalogError, match="conflict"):
        catalog_module.immutable_write(path, b"other")
    assert catalog.load(run_id)


def test_fsync_failure_not_acknowledged(catalog, tmp_path, monkeypatch):
    def fail_sync(fd):
        raise OSError("simulated durability failure")
    monkeypatch.setattr(catalog_module.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="durability"):
        put(catalog, tmp_path, generic())
    assert not list(catalog.root.rglob("*.json"))


def test_concurrent_import(catalog, tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(generic()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: catalog.import_source(source), range(8)))
    assert len(set(ids)) == 1
    assert len(catalog.list_runs()) == 1
    assert not list(catalog.root.rglob(".pending-*"))


def test_payload_limit(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_module, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(catalog_module.CatalogError, match="exceeds"):
        put(catalog, tmp_path, generic())


def test_output_escaping(catalog, tmp_path):
    payload = generic()
    payload["scope"] = '<script>alert("bad")</script> | `'
    payload["results"][0]["metric"] = payload["results"][1]["metric"] = "<script>evil</script>"
    run_id = put(catalog, tmp_path, payload)
    result = catalog.report(run_id, "sqlite", "native")
    assert "<script>" not in Path(result["files"]["svg"]).read_text()
    assert "<script>" not in Path(result["files"]["md"]).read_text().split("```json")[0]


def test_cli_full_cycle(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(generic()))
    common = ["--owner", "cli-owner", "--version", "v1", "--catalog", str(tmp_path / "catalog")]
    def cli(command, *args):
        return subprocess.run([sys.executable, str(SCRIPT), command, *common, *args], text=True, capture_output=True, check=True)
    run_id = json.loads(cli("import", "--source", str(source)).stdout)["id"]
    assert json.loads(cli("list").stdout)[0]["id"] == run_id
    result = json.loads(cli("report", "--id", run_id, "--baseline", "sqlite", "--candidate", "native").stdout)
    assert all(Path(path).exists() for path in result["files"].values())
    missing = subprocess.run([sys.executable, str(SCRIPT), "list", "--catalog", str(tmp_path)], capture_output=True, text=True)
    assert missing.returncode == 2
    assert "--owner" in missing.stderr and "--version" in missing.stderr


@pytest.mark.parametrize("fixture", [hydration, direct])
def test_historical_unknown_environment_cross_source_refused(catalog, tmp_path, fixture):
    payload = fixture()
    baseline_id = put(catalog, tmp_path, payload)
    if fixture is hydration:
        payload["report"]["timings"][0]["mean_ms"] = 1
    else:
        payload["rows"][0]["p99_ms"]["stream_scheduled"] = 1
    candidate_id = put(catalog, tmp_path, payload, "other.json")
    with pytest.raises(catalog_module.CatalogError, match="unknown environment"):
        catalog.report(candidate_id, "sqlite", "native", baseline_id)


@pytest.mark.parametrize("fixture", [hydration, direct])
def test_adapter_backend_identity_refusal(fixture):
    payload = fixture()
    if fixture is hydration:
        payload["report"]["timings"][0]["backend"] = "raw-sqlite"
    else:
        payload["rows"][0]["authority"]["backend"] = "raw-sqlite"
    with pytest.raises(catalog_module.CatalogError, match="backend identities"):
        catalog_module.normalize(payload)


@pytest.mark.parametrize("mutation,expected", [("valid", True), ("missing", None), ("samples", False),
                                               ("budget", False), ("value", False), ("zero_budget", False)])
def test_direct_budget_consistency(mutation, expected):
    payload = direct()
    metric = {"p99_ms": 2, "budget_ms": 10, "samples": 100, "expected_samples": 100, "passed": True}
    if mutation == "missing":
        del metric["samples"]
    elif mutation == "samples":
        metric["samples"] = 99
    elif mutation == "budget":
        metric["budget_ms"] = 1
    elif mutation == "value":
        metric["p99_ms"] = 3
    elif mutation == "zero_budget":
        metric["budget_ms"] = 0
    payload["rows"][0]["acceptance"]["metrics"]["stream_scheduled"] = metric
    normalized = catalog_module.normalize(payload)
    row = next(r for r in normalized["results"] if r["backend"] == "native")
    assert row["windows"][0]["gates"]["budget"] is expected
    assert row["windows"][0]["raw"]["acceptance"]["metrics"]["stream_scheduled"] == metric


def test_direct_unknown_correctness_and_coverage_conflict():
    payload = direct()
    del payload["rows"][0]["errors"]
    payload["rows"][0]["acceptance"]["coverage_passed"] = True
    normalized = catalog_module.normalize(payload)
    row = next(r for r in normalized["results"] if r["backend"] == "native")
    assert row["windows"][0]["gates"]["correctness"] is None
    assert row["windows"][0]["gates"]["coverage"] is False


@pytest.mark.parametrize("fixture", [hydration, direct])
def test_malformed_adapter_clean_error(fixture):
    payload = fixture()
    if fixture is hydration:
        payload["report"]["timings"] = [None]
    else:
        payload["rows"] = "not rows"
    with pytest.raises(catalog_module.CatalogError):
        catalog_module.normalize(payload)


def test_markdown_external_content_inert(catalog, tmp_path):
    payload = generic()
    payload["scope"] = "![image](https://example.invalid/pixel) [link](https://example.invalid/) # heading *bold*"
    run_id = put(catalog, tmp_path, payload)
    result = catalog.report(run_id, "sqlite", "native")
    prose = Path(result["files"]["md"]).read_text().split("```json")[0]
    assert "![image]" not in prose
    assert "[link]" not in prose
    assert "&#91;image&#93;" in prose


@pytest.mark.parametrize("value", [10 ** 400, 1e308])
def test_numeric_import_or_aggregate_overflow(catalog, tmp_path, capsys, value):
    payload = generic()
    for window in payload["results"][0]["windows"]:
        window["value"] = value
    if type(value) is int:
        source = tmp_path / "huge.json"
        source.write_text(json.dumps(payload))
        assert catalog_module.main(["import", "--source", str(source), "--owner", "owner", "--version", "v1", "--catalog", str(catalog.root)]) == 2
        assert "catalog error" in capsys.readouterr().err
    else:
        run_id = put(catalog, tmp_path, payload)
        with pytest.raises(catalog_module.CatalogError, match="non-finite aggregate"):
            catalog.report(run_id, "sqlite", "native")


def test_zero_candidate_and_higher_direction(catalog, tmp_path):
    payload = generic()
    for result in payload["results"]:
        result["direction"] = "higher"
        result["unit"] = "operations/s"
    for window in payload["results"][1]["windows"]:
        window["value"] = 0
    run_id = put(catalog, tmp_path, payload)
    result = catalog.report(run_id, "sqlite", "native")
    assert result["comparison"]["metrics"][0]["candidate_normalized"] == 0
    assert result["comparison"]["metrics"][0]["direction"] == "higher"
    assert "operations/s, higher is better" in Path(result["files"]["svg"]).read_text()


def test_fifo_refused_without_blocking(catalog, tmp_path):
    fifo = tmp_path / "input.fifo"
    catalog_module.os.mkfifo(fifo)
    with pytest.raises(catalog_module.CatalogError, match="regular files"):
        catalog.import_source(fifo)


@pytest.mark.parametrize("value", ["bad\u0000text", "bad\ud800text", "bad\ufffftext"])
def test_xml_forbidden_codepoints_refused(catalog, tmp_path, value):
    payload = generic()
    payload["scope"] = value
    with pytest.raises(catalog_module.CatalogError, match="XML-forbidden"):
        put(catalog, tmp_path, payload)


def test_list_bounded_summary_and_pagination(catalog, tmp_path):
    first = put(catalog, tmp_path, generic())
    payload = generic()
    payload["limits"].append("another receipt")
    second = put(catalog, tmp_path, payload, "second.json")
    page = catalog.list_runs(limit=1)
    assert [r["id"] for r in page] == [min(first, second)]
    assert catalog.list_runs(limit=1, offset=1)[0]["id"] == max(first, second)
    assert "evidence" not in page[0]
    assert "dimensions" in page[0]
    with pytest.raises(catalog_module.CatalogError, match="limit"):
        catalog.list_runs(limit=1001)


def test_output_limit_and_prose_limit(catalog, tmp_path, monkeypatch):
    payload = generic()
    payload["scope"] = "a" * 4097
    with pytest.raises(catalog_module.CatalogError, match="invalid evidence"):
        put(catalog, tmp_path, payload)
    run_id = put(catalog, tmp_path, generic())
    monkeypatch.setattr(catalog_module, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(catalog_module.CatalogError, match="output exceeds"):
        catalog.report(run_id, "sqlite", "native")
    assert not list((catalog.namespace / catalog.version / "reports").glob("*.*"))


def test_hydration_call_count_mismatch_refused():
    payload = hydration()
    payload["report"]["timings"][0]["calls"] = 31
    with pytest.raises(catalog_module.CatalogError, match="call-count mismatch"):
        catalog_module.normalize(payload)
