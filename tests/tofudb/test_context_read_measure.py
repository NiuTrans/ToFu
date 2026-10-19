"""Performance evidence must reject wrong content, coverage and proof."""
import copy

import pytest

from benchmarks.tofudb_context_read import validate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("replay", [False, True])
def test_context_measure_rejects_incomplete_or_changed_work(replay):
    expected = [(f"turn-{i}", {"content": f"evidence-{i}"}) for i in range(4)]
    continuation = {"messages": [{"role": "assistant", "content": "prefix"}]}
    proof = {"version": 2, "count": 3, "digest": "a" * 64}
    result = {"supported": True, "covered": 2 if replay else 0,
        "nextTurnId": None, "proofEligible": True,
        "continuationStatus": "available" if replay else "replay_disabled",
        "continuation": continuation if replay else None,
        "turns": [{"turnId": key, "projection": value}
                  for key, value in expected[2 if replay else 0:]],
        "historyProof": proof}
    validate(result, expected, continuation, replay, proof)
    mutations = [
        ("covered", 1), ("nextTurnId", "unread-page"), ("turns", []),
        ("continuation", {"unexpected": "content"}),
        ("historyProof", proof | {"digest": "b" * 64}),
        ("continuationStatus", "history_changed"), ("proofEligible", False),
    ]
    for field, value in mutations:
        changed = copy.deepcopy(result)
        changed[field] = value
        with pytest.raises(ValueError):
            validate(changed, expected, continuation, replay, proof)
    changed = copy.deepcopy(result)
    changed["turns"][-1]["projection"]["content"] = "truncated"
    with pytest.raises(ValueError):
        validate(changed, expected, continuation, replay, proof)
    appended = [{"turnId": key, "projection": value} for key, value in expected]
    validate(result, expected, continuation, replay, proof, appended)
    changed = copy.deepcopy(result)
    changed["turns"][-1]["status"] = "running"
    with pytest.raises(ValueError):
        validate(changed, expected, continuation, replay, proof, appended)
