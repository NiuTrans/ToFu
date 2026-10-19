"""The independent graph oracle refuses forged history and unequal-work claims."""
import copy
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.harness_graph_controls import HistoryModel, Rejected, Verifier, run

pytestmark = pytest.mark.unit
SCOPE = {"tenant": 7, "owner": 11}


def command(model, op, branch="main", **fields):
    return {"op": op, "scope": SCOPE, "branch": branch,
            "command_id": str(len(model.receipts)), **fields}


def history():
    model = HistoryModel()
    model.apply(command(model, "create"))
    for delta in ["first", "第二\\\"\n😀"]:
        model.apply(command(model, "append", expected=model.token(SCOPE, "main")["head"], delta=delta))
    return model


def test_independent_history_rejects_forged_witness_and_same_hash_ABA():
    model = history()
    token = model.token(SCOPE, "main")
    model.apply(command(model, "fork", "branch", source=token))
    model.apply(command(model, "append", "branch", expected=model.token(SCOPE, "branch")["head"], delta="private"))
    full = {"op": "context", "scope": SCOPE, "branch": "main", "after": None}
    assert model.apply(full)["fragments"] == ["first", "第二\\\"\n😀"]
    forged = model.token(SCOPE, "branch")
    forged["branch"] = "main"
    with pytest.raises(Rejected):
        model.apply(dict(full, after=forged))
    for _ in range(2):
        old = model.token(SCOPE, "main")
        model.apply(command(model, "reset", expected=old["head"], delta="same", reason="edit"))
        with pytest.raises(Rejected, match="epoch"):
            model.apply(dict(full, after=old))
    assert old["head"]["node"] == model.token(SCOPE, "main")["head"]["node"]
    forged = model.token(SCOPE, "main")
    forged["head"]["depth"] = 0
    with pytest.raises(Rejected, match="witness"):
        model.apply(dict(full, after=forged))


@pytest.mark.parametrize("corruption", ["truncated", "wrong_hash", "unaccounted_prefix"])
def test_verifier_refuses_wrong_content_hash_or_logical_work(corruption):
    model = history()
    request = {"op": "context", "scope": SCOPE, "branch": "main", "after": None}
    expected = model.apply(request)
    response = {"ok": True, "value": {"result": copy.deepcopy(expected),
        "work": {"reads": 3, "writes": 0, "read_bytes": 100, "write_bytes": 0,
                 "node_reads": 2, "header_reads": 0}}}
    if corruption == "truncated":
        response["value"]["result"]["fragments"].pop(0)
    elif corruption == "wrong_hash":
        response["value"]["result"]["token"]["head"]["node"] = "f" * 64
    else:
        response["value"]["work"]["header_reads"] = 2
    client = SimpleNamespace(exchange=lambda _: response, process=SimpleNamespace(pid=123))
    verifier = Verifier(client, io.StringIO())
    verifier.model = model
    with pytest.raises(AssertionError):
        verifier.check(request, corruption)


def test_verifier_refuses_backend_accepting_forged_depth():
    model = history()
    token = model.token(SCOPE, "main")
    token["head"]["depth"] = 0
    request = {"op": "context", "scope": SCOPE, "branch": "main", "after": token}
    client = SimpleNamespace(exchange=lambda _: {"ok": True, "value": {}}, process=SimpleNamespace(pid=123))
    verifier = Verifier(client, io.StringIO())
    verifier.model = model
    with pytest.raises(AssertionError):
        verifier.check(request, "forged depth accepted")


@pytest.mark.skipif(not os.environ.get("TOFU_HARNESS_GRAPH_BINARY"), reason="opt-in graph prototype binary")
def test_real_native_and_sqlite_graph_contract_and_process_crash(tmp_path):
    report = run(Path(os.environ["TOFU_HARNESS_GRAPH_BINARY"]).resolve(), tmp_path / "run",
                 ["native", "sqlite"], seed=20261019, steps=64)
    for result in report["backends"].values():
        assert result["accepted"] > 64 and result["rejected"] > 20
        assert len(set(result["pids"])) == 3
        assert len(result["crashes"]) == 1 and result["crashes"][0]["returncode"] == -9
    assert report["backends"]["native"]["work"] == report["backends"]["sqlite"]["work"]
