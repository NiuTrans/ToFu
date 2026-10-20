"""Search must deliver actual evidence, including late and multilingual facts."""

from copy import deepcopy

import pytest

from tofu_search.passages import select_relevant_passages
from tofu_search.search import orchestrator
from tofu_search.search.rerank import rerank_by_bm25

pytestmark = pytest.mark.unit


def row(name, content, title="Reference", snippet=""):
    return {"url": f"https://{name}.example/article", "title": title,
            "snippet": snippet, "full_content": content}


def test_late_answer_beats_partial_match_without_mutating_sources():
    answer = row("answer", "General introduction and navigation.\n\n" * 800
                 + "Quasar latency is 17 milliseconds with caching enabled.")
    partial = row("partial", "Latency measures how long an operation takes.")
    candidates = [partial, answer, row("noise", "Gardening and cooking.")]
    original = deepcopy(candidates)
    ranked = rerank_by_bm25("quasar latency caching", candidates, top_k=1)
    assert ranked[0]["url"] == answer["url"]
    assert candidates == original


def test_rank_small_candidate_sets_too():
    candidates = [row("noise", "Gardening and cooking."),
                  row("answer", "Quasar latency is 17 milliseconds.")]
    assert rerank_by_bm25("quasar latency", candidates, top_k=5)[0] is candidates[1]


@pytest.mark.parametrize("max_results", [1, 2, 5])
@pytest.mark.parametrize("fetch_pages", [False, True])
@pytest.mark.parametrize("rerank", [False, True])
def test_public_search_ranks_relevant_candidates_independent_of_pool_size(
    monkeypatch, max_results, fetch_pages, rerank,
):
    candidates = [
        {"url": "https://sampling.example/article", "title": "Lunar isotope sampling notes",
         "snippet": "Material measurements and calibration methods for lunar isotope samples."},
        {"url": "https://atlas.example/reference", "title": "Lunar isotope atlas reference",
         "snippet": "The atlas maps lunar isotope abundance across mapped regions."},
    ]
    original = deepcopy(candidates)
    monkeypatch.setattr(orchestrator, "search_bing", lambda *args, **kwargs: candidates)
    bodies = {candidate["url"]: candidate["snippet"] for candidate in candidates}
    monkeypatch.setattr(orchestrator, "fetch_page_content", lambda url, **kwargs: bodies[url])

    results = orchestrator.perform_web_search(
        "lunar isotope atlas", max_results=max_results, engines=["Bing"],
        fetch_pages=fetch_pages, filter_pages=False, rerank=rerank, deepen=False,
    )

    expected = list(reversed(candidates)) if rerank else candidates
    assert [result["url"] for result in results] == [
        candidate["url"] for candidate in expected[:max_results]
    ]
    assert all(result["relevance"] == "lexical_match" for result in results)
    assert candidates == original


@pytest.mark.parametrize("top_k", [0, -1])
def test_nonpositive_result_count_is_empty(top_k):
    assert rerank_by_bm25("quasar", [row("answer", "Quasar")], top_k) == []


def test_constraints_do_not_become_ranking_evidence():
    candidates = [
        row("noise", "example org obsolete html source site", title="example org"),
        row("answer", "Quasar"),
        row("other", "Gardening"),
    ]
    ranked = rerank_by_bm25(
        "quasar site:example.org filetype:html -obsolete", candidates, 1)
    assert ranked[0] is candidates[1]


def test_link_inventory_cannot_promote_irrelevant_page():
    candidates = [
        row("noise", "Unrelated prose.\n--- Page Links ---\n"
            + "quasar latency caching\n" * 80),
        row("answer", "Quasar latency is 17 milliseconds with caching enabled."),
        row("other", "Gardening"),
    ]
    assert rerank_by_bm25("quasar latency caching", candidates, 1)[0] is candidates[1]


@pytest.mark.parametrize(("query", "answer"), [
    ("кэширование задержка", "Кэширование уменьшает задержка до 17 мс."),
    ("التخزين المؤقت", "يقلل التخزين المؤقت زمن الاستجابة."),
    ("mémoire fidélité", "La mémoire conserve la fidélité des observations."),
    ("缓存延迟", "缓存延迟为17毫秒。"),
])
def test_multilingual_rank_and_excerpt_share_evidence(query, answer):
    candidates = [row("noise", "Navigation and unrelated material."),
                  row("answer", "unrelated background\n\n" * 600 + answer)]
    ranked = rerank_by_bm25(query, candidates, 1)
    assert ranked[0] is candidates[1]
    assert answer in select_relevant_passages(ranked[0]["full_content"], query, 300)


def test_passages_cover_complementary_facts_before_repeating_one_aspect():
    repeated = "Quasar latency is 17 milliseconds. " + "Background material. " * 8
    throughput = "Throughput reaches 900 requests per second."
    text = "\n\n".join([repeated] * 20 + [throughput])
    excerpt = select_relevant_passages(text, "quasar latency throughput", 600,
                                       chunk_chars=240)
    assert "17 milliseconds" in excerpt
    assert throughput in excerpt
    assert excerpt.count("17 milliseconds") == 1
    assert len(excerpt) <= 600


def test_lead_for_page_identity_leaves_room_for_late_evidence():
    lead = "Login required. " + "Account information. " * 20
    fact = "Quasar latency is exactly 17 milliseconds."
    text = lead + "\n\n" + "Unrelated material.\n\n" * 100 + fact
    excerpt = select_relevant_passages(text, "quasar latency", 400,
                                       chunk_chars=300, include_lead=True)
    assert "Login required." in excerpt
    assert fact in excerpt
    assert len(excerpt) <= 400


def test_cjk_terms_do_not_join_across_unrelated_words():
    candidates = [row("noise", "北京 unrelated 上海"),
                  row("answer", "京上")]
    assert rerank_by_bm25("京上", candidates, 1)[0] is candidates[1]


def test_excerpt_ignores_operator_operands():
    text = ("obsolete example org html\n\n" * 200
            + "Quasar latency is 17 milliseconds.")
    excerpt = select_relevant_passages(
        text, "quasar site:example.org filetype:html -obsolete", 150)
    assert "17 milliseconds" in excerpt


def test_packed_passages_preserve_original_text_and_mark_gaps():
    first = "Quasar latency is 17 ms. Do not interpret this as throughput."
    last = "Throughput is 900 req/s. This is measured separately."
    text = first + "\n\n" + "Unrelated material.\n\n" * 100 + last
    excerpt = select_relevant_passages(text, "quasar latency throughput", 350,
                                       chunk_chars=150)
    assert first in excerpt and last in excerpt
    assert "[... omitted ...]" in excerpt
    for part in excerpt.split("\n\n[... omitted ...]\n\n"):
        assert part in text


def test_similar_passages_with_different_values_remain_available():
    first = "Quasar latency is 17 milliseconds in the first run."
    second = "Quasar latency is 70 milliseconds in the second run."
    text = first + "\n\n" + "Unrelated background.\n\n" * 200 + second
    excerpt = select_relevant_passages(text, "quasar latency", 400)
    assert first in excerpt and second in excerpt


def test_public_pipeline_delivers_late_evidence_through_search_and_format(monkeypatch):
    from tofu_search import perform_web_search
    from tofu_search.config import SearchConfig
    from tofu_search.search import orchestrator
    from tofu_search.search.format import format_search_for_tool_response

    fact = "Quasar latency is 17 milliseconds with caching enabled."
    rows = [
        row("partial", "Latency is the elapsed time.", title="Latency concepts"),
        row("answer", "Unrelated navigation.\n\n" * 800 + fact, title="Technical reference"),
        row("noise", "Growing vegetables.", title="Garden cultivation"),
    ]
    bodies = {item["url"]: item["full_content"] for item in rows}
    serp = [{**{key: value for key, value in item.items() if key != "full_content"},
             "source": "DDG-HTML"}
            for item in rows]
    monkeypatch.setattr(orchestrator, "search_ddg_html", lambda *a, **kw: serp)
    monkeypatch.setattr(orchestrator, "fetch_page_content",
                        lambda url, **kw: bodies[url])
    results = perform_web_search(
        "quasar latency caching", max_results=2, engines=["DDG-HTML"],
        filter_pages=False, config=SearchConfig(
            prefetch_gate_enabled=False, search_deadline_secs=5))
    assert results[0]["url"] == rows[1]["url"]
    assert results[0]["full_content"] == bodies[rows[1]["url"]]
    assert fact in format_search_for_tool_response(
        results, query="quasar latency caching", max_total_content_chars=600)
