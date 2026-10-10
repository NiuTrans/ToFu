"""Request attribution must reject bad ledgers and never add inclusive spans."""
import pytest
from scripts.tofudb_profile_report import analyze

pytestmark = pytest.mark.unit


def fixture():
    return {"storage_metrics": {"requestProfile": {
        "enabled": True, "available": True, "contract": "tofudb.request-profile/v1",
        "sampleEvery": 1, "selected": 2, "dropped": 0, "slowSamples": [],
        "operations": [{"operation": "record.get", "requests": 2, "errors": 0,
            "dispatchNanos": 1000, "unattributedNanos": 100, "invalidSpans": 0,
            "embeddedQueueNanos": 2000, "embeddedQueueSamples": 2,
            "dispatchLog2NanosHistogram": [0, 2], "work": {}, "stages": [
                {"stage": "authority_wait", "inclusiveNanos": 800, "exclusiveNanos": 100},
                {"stage": "fifo_wait", "inclusiveNanos": 700, "exclusiveNanos": 700},
                {"stage": "authority_hold", "inclusiveNanos": 100, "exclusiveNanos": 100},
            ]}],
    }}}


def test_nested_waits_reconcile_and_queue_is_not_in_dispatch_denominator():
    result = analyze(fixture())
    assert result["wait_percent"] == pytest.approx(70)
    assert result["summed_dispatch_ms"] == .001
    assert result["operations"][0]["embedded_queue_ms"] == .002
    assert result["operations"][0]["mean_embedded_queue_ms"] == .001
    assert result["operations"][0]["mean_embedded_queue_plus_dispatch_ms"] == .0015
    assert result["operations"][0]["authority_hold_ms"] == .0001


@pytest.mark.parametrize("cache_wait", ["entity_page_cache_read_wait", "entity_page_cache_write_wait",
    "block_pending_wait", "block_cache_wait", "block_history_source_wait",
    "rotation_prepare_wait", "rotation_install_wait"])
def test_read_lock_wait_is_waiting_inside_authority_not_execution(cache_wait):
    document = fixture()
    operation = document["storage_metrics"]["requestProfile"]["operations"][0]
    operation["stages"] = [
        {"stage": "load_node", "inclusiveNanos": 900, "exclusiveNanos": 100},
        {"stage": cache_wait, "inclusiveNanos": 700, "exclusiveNanos": 700},
        {"stage": "entity_page_decode", "inclusiveNanos": 100, "exclusiveNanos": 100},
    ]
    result = analyze(document)
    assert result["wait_percent"] == pytest.approx(70)
    assert result["summed_dispatch_ms"] == .001


@pytest.mark.parametrize("samples,nanos,mean", [(0, None, None), (0, 0, None), (1, 2000, .002)])
def test_unknown_or_partial_queue_cannot_invent_combined_latency(samples, nanos, mean):
    document = fixture()
    operation = document["storage_metrics"]["requestProfile"]["operations"][0]
    operation.update(embeddedQueueSamples=samples, embeddedQueueNanos=nanos)
    row = analyze(document)["operations"][0]
    assert row["mean_embedded_queue_ms"] == mean
    assert row["mean_embedded_queue_plus_dispatch_ms"] is None


@pytest.mark.parametrize("samples,nanos", [(-1, 1), (3, 2000), (1, None), (0, 2000), (2, -1)])
def test_invalid_queue_coverage_is_rejected(samples, nanos):
    document = fixture()
    document["storage_metrics"]["requestProfile"]["operations"][0].update(
        embeddedQueueSamples=samples, embeddedQueueNanos=nanos)
    with pytest.raises(ValueError, match="embedded queue accounting"):
        analyze(document)


def test_shorter_dispatch_does_not_hide_longer_worker_queue():
    before = analyze(fixture())["operations"][0]
    document = fixture()
    operation = document["storage_metrics"]["requestProfile"]["operations"][0]
    operation.update(dispatchNanos=500, unattributedNanos=50, embeddedQueueNanos=4000)
    for stage in operation["stages"]:
        stage["exclusiveNanos"] //= 2
        stage["inclusiveNanos"] //= 2
    after = analyze(document)["operations"][0]
    assert after["mean_dispatch_ms"] < before["mean_dispatch_ms"]
    assert after["mean_embedded_queue_plus_dispatch_ms"] > before["mean_embedded_queue_plus_dispatch_ms"]


def test_cli_exposes_queue_coverage_and_unknown_latency(tmp_path, monkeypatch, capsys):
    import json
    from scripts.tofudb_profile_report import main
    document = fixture()
    document["storage_metrics"]["requestProfile"]["operations"][0].update(
        embeddedQueueSamples=0, embeddedQueueNanos=None)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr("sys.argv", ["tofudb_profile_report", str(path)])
    main()
    output = capsys.readouterr().out
    assert "queue coverage" in output and "0/2" in output
    assert "--" in output and "not end-to-end latency" in output


@pytest.mark.parametrize("field,value", [("invalidSpans", 1), ("dispatchNanos", 999),
    ("unattributedNanos", 90), ("requests", 3)])
def test_invalid_accounting_is_rejected(field, value):
    document = fixture()
    document["storage_metrics"]["requestProfile"]["operations"][0][field] = value
    with pytest.raises(ValueError):
        analyze(document)


@pytest.mark.parametrize("field,value", [("enabled", False), ("available", False), ("contract", "unknown")])
def test_missing_or_unknown_profile_cannot_produce_an_attribution(field, value):
    document = fixture()
    document["storage_metrics"]["requestProfile"][field] = value
    with pytest.raises(ValueError):
        analyze(document)
