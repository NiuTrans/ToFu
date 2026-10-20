"""Metric labels must remain independent of the implementation being measured."""

import pytest

from benchmarks.search_relevance import cases, metrics

pytestmark = pytest.mark.unit


def test_rank_and_fact_metrics_use_labels_and_delivered_text():
    grades = {"answer": 3, "partial": 1}
    failed = metrics(["noise"], grades, ["irrelevant"], ["verified fact"], 1)
    assert failed == {"hit_at_1": 0.0, "reciprocal_rank": 0.0, "ndcg": 0.0,
                      "fact_recall": 0.0}
    perfect = metrics(["answer", "partial"], grades, ["verified fact"], ["verified fact"], 2)
    assert all(value == 1.0 for value in perfect.values())
    partial = metrics(["partial", "answer"], grades, ["verified fact"], ["verified fact"], 2)
    assert partial["hit_at_1"] == 0.0
    assert partial["reciprocal_rank"] == 0.5
    assert partial["ndcg"] == pytest.approx(0.7098097414)
    assert partial["fact_recall"] == 1.0


def test_fixture_labels_resolve_and_facts_are_in_sources():
    fixtures = cases()
    assert len({item["id"] for item in fixtures}) == len(fixtures)
    for item in fixtures:
        rows = {row["url"]: row for row in item["rows"]}
        assert set(item["grades"]) <= rows.keys()
        assert any(grade >= 2 for grade in item["grades"].values())
        assert all(any(fact in row["full_content"] for row in rows.values())
                   for fact in item["facts"])
