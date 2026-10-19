"""Caller windows must exclude warmup, losses and mismatched populations."""
import pytest
from scripts.tofudb_profile_report import analyze_window
from tests.tofudb.test_request_profile_report import fixture

pytestmark = pytest.mark.unit


def matched_window_fixture():
    from copy import deepcopy
    before = fixture()["storage_metrics"]
    before["requestProfile"]["seen"] = 2
    after = deepcopy(before)
    profile = after["requestProfile"]
    profile.update(seen=8, selected=8)
    row = profile["operations"][0]
    for key in ("requests", "dispatchNanos", "unattributedNanos", "embeddedQueueNanos", "embeddedQueueSamples"):
        row[key] *= 4
    row["dispatchLog2NanosHistogram"] = [0, 8]
    for stage in row["stages"]:
        stage["inclusiveNanos"] *= 4
        stage["exclusiveNanos"] *= 4
    return {"operation": "record.get", "count": 6, "samples_ns": [2000] * 6,
            "metrics_before": before, "metrics_after": after}


def test_matched_window_excludes_warmup_and_reconciles_exact_outer_population():
    document = matched_window_fixture()
    result = analyze_window(document)
    assert result["requests"] == 6
    assert result["operations"][0]["dispatch_ms"] == .003
    assert result["operations"][0]["embedded_queue_ms"] == .006
    assert result["matched_window"]["mean_caller_ns"] == 2000
    assert result["matched_window"]["mean_outside_queue_and_dispatch_ns"] == 500
    assert document["metrics_before"]["requestProfile"]["operations"][0]["requests"] == 2


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(count=5),
    lambda d: d.update(samples_ns=[2000] * 5),
    lambda d: d.update(samples_ns=[-1] * 6),
    lambda d: d.update(samples_ns=[True] * 6),
    lambda d: d.update(samples_ns=[1000] * 6),
    lambda d: d["metrics_after"]["requestProfile"].update(selected=7),
    lambda d: d["metrics_after"]["requestProfile"].update(seen=9),
    lambda d: d["metrics_after"]["requestProfile"].update(dropped=1),
    lambda d: d["metrics_after"]["requestProfile"].update(sampleEvery=2),
    lambda d: d["metrics_after"]["requestProfile"]["operations"][0].update(errors=1),
    lambda d: d["metrics_after"]["requestProfile"]["operations"][0].update(embeddedQueueSamples=7),
    lambda d: d["metrics_after"]["requestProfile"]["operations"][0].update(work={"reads": -1}),
    lambda d: d["metrics_before"]["requestProfile"]["operations"][0].update(work={"reads": 1}),
    lambda d: d["metrics_after"]["requestProfile"]["operations"].append(d["metrics_after"]["requestProfile"]["operations"][0]),
    lambda d: d["metrics_after"]["requestProfile"]["operations"][0]["stages"].append({"stage": "authority_hold", "calls": 0, "inclusiveNanos": 0, "exclusiveNanos": 0}),
])
def test_matched_window_refuses_partial_contaminated_or_reset_evidence(mutation):
    document = matched_window_fixture()
    mutation(document)
    with pytest.raises(ValueError):
        analyze_window(document)


def test_matched_window_allows_new_operation_but_never_adds_lifetime_extremes():
    document = matched_window_fixture()
    before = document["metrics_before"]["requestProfile"]
    before.update(operations=[], seen=0, selected=0)
    document.update(count=8, samples_ns=[2000] * 8)
    document["metrics_after"]["requestProfile"]["operations"][0]["maxDispatchNanos"] = 999999
    result = analyze_window(document)
    assert result["requests"] == 8
    assert result["matched_window"]["mean_outside_queue_and_dispatch_ns"] == 500


def test_matched_window_cli_uses_window_attribution(tmp_path, monkeypatch, capsys):
    import json
    from scripts.tofudb_profile_report import main
    path = tmp_path / "window.json"
    path.write_text(json.dumps(matched_window_fixture()))
    monkeypatch.setattr("sys.argv", ["tofudb_profile_report", str(path), "--window", "--json"])
    main()
    assert json.loads(capsys.readouterr().out)["matched_window"]["requests"] == 6
