"""Optional scores reuse search evidence, priors, disable policy and fallback."""

from concurrent.futures import CancelledError
from copy import deepcopy

import pytest

from tofu_search import SearchConfig, perform_web_search
from tofu_search.local_rerank import LocalRerankCleanupError, LocalRerankError
from tofu_search.search import orchestrator
from tofu_search.search.rerank import rerank_by_scores

pytestmark = pytest.mark.unit


class Scorer:
    def __init__(self, scores=None, error=None):
        self.scores, self.error, self.calls = scores, error, []

    def score(self, query, documents, **kwargs):
        self.calls.append((query, documents, kwargs))
        if self.error:
            raise self.error
        return self.scores or [0.5] * len(documents)


def search(monkeypatch, scorer, *, rerank=True, body=None):
    rows = [
        {
            "url": "https://report.example/a",
            "title": "Quasar calibration sensor",
            "snippet": "A table of measured wavelengths.",
            "source": "Bing",
        },
        {
            "url": "https://manual.example/b",
            "title": "Quasar calibration sensor",
            "snippet": "Instrument operating instructions.",
            "source": "Bing",
        },
        {
            "url": "https://garden.example/c",
            "title": "Tomato cultivation",
            "snippet": "Garden planting notes.",
            "source": "Bing",
        },
    ]
    monkeypatch.setattr(orchestrator, "search_bing", lambda *a, **kw: rows)
    if body:
        monkeypatch.setattr(orchestrator, "fetch_page_content", lambda *a, **kw: body)
    return perform_web_search(
        "quasar calibration sensor",
        engines=["Bing"],
        fetch_pages=bool(body),
        filter_pages=False,
        deepen=False,
        rerank=rerank,
        config=SearchConfig(
            local_reranker=scorer,
            prefetch_gate_enabled=False,
            local_rerank_timeout=4,
            search_deadline_secs=5,
        ),
    )


def test_one_scoring_call_keeps_uncertainty_separate_and_bounds_time(monkeypatch):
    scorer = Scorer([0.1, 0.9, 1.0])
    result = search(monkeypatch, scorer)
    assert len(scorer.calls) == 1
    query, documents, kwargs = scorer.calls[0]
    assert kwargs["group_sizes"] == [2, 1]
    assert 0 < kwargs["timeout"] <= 4
    assert query == "quasar calibration sensor"
    assert len(documents) == 3
    assert all("garden.example" not in row["url"] for row in result)
    assert result._unverified_candidates[0]["relevance"] == "unverified"
    assert result._local_rerank["status"] == "applied"


def test_rerank_false_never_invokes_the_local_model(monkeypatch):
    scorer = Scorer(error=AssertionError("must not run"))
    search(monkeypatch, scorer, rerank=False)
    assert not scorer.calls


@pytest.mark.parametrize("error", [LocalRerankError("snapshot_invalid", "missing"), TimeoutError()])
def test_unavailable_optional_scores_preserve_the_lexical_result(monkeypatch, error):
    baseline = search(monkeypatch, None)
    result = search(monkeypatch, Scorer(error=error))
    assert result == baseline
    assert result._unverified_candidates == baseline._unverified_candidates
    assert result._local_rerank["status"] in ("snapshot_invalid", "timeout")


@pytest.mark.parametrize("error", [CancelledError(), LocalRerankCleanupError("still owned")])
def test_cancellation_or_unjoined_cleanup_cannot_be_hidden_as_lexical_fallback(monkeypatch, error):
    with pytest.raises(type(error)):
        search(monkeypatch, Scorer(error=error))


def test_fetched_source_text_and_evidence_fields_survive_neural_order(monkeypatch):
    body = "The quasar calibration sensor measures a wavelength of 520 nanometers. " * 30
    scorer = Scorer()
    result = search(monkeypatch, scorer, body=body)
    assert result
    assert all(row["full_content"] == body for row in result)
    assert all(row["relevance_evidence"] for row in result)
    assert any("520 nanometers" in text for text in scorer.calls[0][1])


def test_positive_neural_probability_does_not_admit_unrelated_native_votes():
    rows = [
        {
            "url": "https://popular.example/x",
            "title": "Growing tomatoes",
            "snippet": "Garden tools",
            "rrf_score": 100,
        },
        {
            "url": "https://report.example/x",
            "title": "Lunar isotope atlas",
            "snippet": "Measurement reference",
            "rrf_score": 1 / 70,
        },
    ]
    original = deepcopy(rows)
    ranked = rerank_by_scores("lunar isotope atlas", rows, 2, [0.9, 0.8])
    assert ranked[0] is rows[1]
    assert rows == original


@pytest.mark.parametrize("scores", [[0.5], [float("nan"), 0.2], [True, 0.2]])
def test_incomplete_or_nonfinite_scores_are_rejected(scores):
    with pytest.raises(ValueError):
        rerank_by_scores("x", [{"title": "x"}, {"title": "x"}], 2, scores)


def test_config_copies_borrow_the_same_ranking_owner():
    scorer = Scorer()
    original = SearchConfig(local_reranker=scorer)
    assert original.copy(fetch_top_n=2).local_reranker is scorer
