"""Tests for primary-source domain authority (authority.py) and its two
integration points: the BM25 rerank boost and the format.py Authority tag.

The motivating bug: an SEO aggregator (latencycost.com) outranked the vendor's
own pricing page (aws.amazon.com), so the model synthesized a wrong number.
These tests pin the fix: the vendor domain NAMED IN THE QUERY is 'official',
structural docs/registries are 'primary', SEO price-farms are 'aggregator',
and the rerank boost + format tag reflect that.
"""

from tofu_search.search.authority import (
    AUTHORITY_BOOST,
    authority_label,
    classify_authority,
)
from tofu_search.search.format import format_search_for_tool_response as fmt
from tofu_search.search.rerank import rerank_by_bm25

# ── classifier: OFFICIAL (brand named in query) ──

def test_official_when_vendor_named_in_query():
    q = "对比 Cloudflare Fastly AWS CloudFront 免费额度"
    assert classify_authority("https://aws.amazon.com/cloudfront/pricing/", q) == "official"
    assert classify_authority("https://www.cloudflare.com/plans/free/", q) == "official"
    assert classify_authority("https://www.fastly.com/pricing", q) == "official"


def test_official_needs_the_brand_in_query():
    # Same vendor domain, but the query does NOT name it → not 'official'.
    assert classify_authority("https://www.cloudflare.com/plans/free/",
                              "how do I bake sourdough bread") != "official"


def test_official_ignores_generic_host_labels():
    # amazonaws / com / www must never make a random host 'official' just
    # because the query happens to contain the word 'aws'.
    assert classify_authority("https://random-blog.s3.amazonaws.com/post",
                              "aws cloudfront pricing") != "official"


# ── classifier: PRIMARY (structural, query-independent) ──

def test_primary_docs_and_registries():
    assert classify_authority("https://docs.astral.sh/ruff/faq/", "ruff vs flake8") == "primary"
    assert classify_authority("https://pypi.org/project/numpy/", "numpy version") == "primary"
    assert classify_authority("https://nvd.nist.gov/vuln/detail/CVE-2021-44228", "log4shell") == "primary"
    assert classify_authority("https://developer.mozilla.org/en-US/docs/Web/HTTP", "http caching") == "primary"


# ── classifier: AGGREGATOR (SEO/price farms) ──

def test_aggregator_seo_price_farms():
    q = "AWS CloudFront pricing"
    assert classify_authority("https://latencycost.com/cdn-comparison", q) == "aggregator"
    assert classify_authority("https://costbench.com/software/cdn-edge/aws-cloudfront/", q) == "aggregator"
    assert classify_authority("https://comparetiers.com/tools/fastly", "fastly") == "aggregator"


def test_neutral_news_and_wiki():
    q = "OpenAI flagship model GPT"
    assert classify_authority("https://en.wikipedia.org/wiki/GPT-5", q) == "neutral"
    assert classify_authority("https://venturebeat.com/ai/openai-gpt", q) == "neutral"


def test_classify_handles_garbage_url():
    assert classify_authority("", "q") == "neutral"
    assert classify_authority("not-a-url", "q") == "neutral"


# ── rerank: authority boost lifts the vendor page over the aggregator ──

def _r(url, title, snippet):
    return {"url": url, "title": title, "snippet": snippet, "source": "Test"}


# ── host_brand_labels: entity attribution ──

def test_host_brand_labels_excludes_generic():
    from tofu_search.search.authority import host_brand_labels
    assert host_brand_labels("https://www.cloudflare.com/plans/") == {"cloudflare"}
    # aws.amazon.com carries BOTH 'aws' and 'amazon' (both non-generic) — good
    # for entity attribution when a query names either.
    assert host_brand_labels("https://aws.amazon.com/cloudfront/") == {"aws", "amazon"}
    # Generic host labels (com/amazonaws/github/www) never count as a brand.
    assert host_brand_labels("https://random.s3.amazonaws.com/x") == {"random"}
    assert host_brand_labels("") == set()


# ── rerank: entity-diversified top-K for multi-entity comparisons ──

# Rich text that mentions ALL vendors → high, near-equal BM25; used so the
# dominant vendor's pages would monopolise a plain top-K.
_RICH = ("cloudflare fastly cloudfront aws free tier pricing limits bandwidth "
         "requests comparison cdn")


def test_diversify_covers_each_named_entity():
    # Query names 3 vendors. The two blog.cloudflare pages have the strongest
    # lexical hits, so a plain top-3 is ALL Cloudflare (verified: it drops
    # Fastly + CloudFront). The diversified selection must instead include at
    # least one page per named vendor.
    q = "对比 Cloudflare Fastly AWS CloudFront 免费额度 pricing free tier"
    results = [
        _r("https://blog.cloudflare.com/vs-1", "CF vs all A", _RICH + " " + _RICH),
        _r("https://blog.cloudflare.com/vs-2", "CF vs all B", _RICH + " " + _RICH),
        _r("https://www.cloudflare.com/compare", "CF compare", _RICH),
        _r("https://www.fastly.com/pricing", "Fastly pricing",
           "fastly pricing free tier"),
        _r("https://aws.amazon.com/cloudfront/pricing/", "AWS CloudFront pricing",
           "cloudfront aws pricing free tier egress requests"),
    ]
    # Guard: plain top-3 (diversification disabled) really IS all-Cloudflare,
    # so this fixture genuinely exercises the branch.
    _g = rerank_by_bm25.__globals__
    _orig = _g["_diversify_by_entity"]
    _orig_domain = _g["_diversify_by_domain"]
    _g["_diversify_by_entity"] = lambda *a, **k: None
    _g["_diversify_by_domain"] = lambda q, scored, top_k: [
        item[2] for item in scored[:top_k]]
    try:
        plain = rerank_by_bm25(q, results, top_k=3)
    finally:
        _g["_diversify_by_entity"] = _orig
        _g["_diversify_by_domain"] = _orig_domain
    assert {r["url"].split("/")[2] for r in plain} == {"blog.cloudflare.com",
                                                        "www.cloudflare.com"}

    out = rerank_by_bm25(q, results, top_k=3)
    hosts = {r["url"].split("/")[2] for r in out}
    assert any("cloudflare.com" in h for h in hosts)
    assert any("fastly.com" in h for h in hosts)
    assert any("amazon.com" in h for h in hosts)


def test_diversify_picks_authoritative_winner_within_entity():
    # Cloudflare dominates lexically AND appears as both an aggregator and its
    # official page. Diversified top-2 must (a) free a slot for Fastly and
    # (b) fill the Cloudflare slot with the OFFICIAL page — the authority boost
    # decides the within-entity winner, not raw BM25 (the aggregator is denser).
    q = "compare Cloudflare Fastly free tier pricing limits"
    rich = "cloudflare fastly free tier pricing limits bandwidth requests comparison"
    results = [
        _r("https://cloudflarecost.com/free", "CF cost", rich + " " + rich),
        _r("https://www.cloudflare.com/plans/free/", "CF official", rich),
        _r("https://blog.cloudflare.com/x", "CF blog", rich),
        _r("https://www.fastly.com/pricing", "Fastly", "fastly free tier pricing"),
    ]
    out = rerank_by_bm25(q, results, top_k=2)
    hosts = {r["url"].split("/")[2] for r in out}
    assert "www.cloudflare.com" in hosts       # official Cloudflare page kept
    assert "cloudflarecost.com" not in hosts   # aggregator loses within-entity
    assert "www.fastly.com" in hosts           # coverage freed a slot for Fastly


def test_diversify_noop_for_single_entity_query():
    # Only ONE named entity present → no diversification; plain global top-K
    # (the single most relevant page) is returned unchanged.
    q = "kubernetes pod security admission controller yaml"
    results = [
        _r("https://kubernetes.io/docs/psa", "Pod Security Admission",
           "kubernetes pod security admission controller configuration yaml example"),
        _r("https://someblog.example/k8s", "k8s psa guide",
           "kubernetes pod security admission controller yaml"),
        _r("https://another.example/x", "unrelated", "gardening tomatoes"),
    ]
    out = rerank_by_bm25(q, results, top_k=1)
    assert out[0]["url"] == "https://kubernetes.io/docs/psa"


def test_rerank_lifts_official_over_aggregator_on_tie():
    q = "AWS CloudFront free tier egress"
    # Aggregator listed FIRST and with equally-strong lexical content; the
    # official aws.amazon.com page must still come out on top after the boost.
    results = [
        _r("https://latencycost.com/cdn", "AWS CloudFront free tier egress",
           "cloudfront free tier egress pricing comparison"),
        _r("https://aws.amazon.com/cloudfront/pricing/", "AWS CloudFront free tier egress",
           "cloudfront free tier egress pricing official"),
        _r("https://someblog.example/cdn", "unrelated gardening", "tomatoes"),
    ]
    out = rerank_by_bm25(q, results, top_k=2)
    assert out[0]["url"] == "https://aws.amazon.com/cloudfront/pricing/"


def test_rerank_boost_does_not_beat_much_stronger_relevance():
    # A neutral page that is FAR more relevant should still beat a barely
    # on-topic official page — the boost is a nudge, not an override.
    q = "kubernetes pod security admission controller configuration example yaml"
    results = [
        _r("https://aws.amazon.com/", "AWS", "cloud"),  # official-ish but irrelevant
        _r("https://someblog.example/k8s",
           "kubernetes pod security admission controller configuration example yaml",
           "kubernetes pod security admission controller configuration example yaml full guide"),
    ]
    out = rerank_by_bm25(q, results, top_k=1)
    assert out[0]["url"] == "https://someblog.example/k8s"


def test_boost_values_ordered():
    assert AUTHORITY_BOOST["official"] > AUTHORITY_BOOST["primary"] > 0
    assert AUTHORITY_BOOST["aggregator"] < 0
    assert AUTHORITY_BOOST["neutral"] == 0.0


# ── format: Authority tag surfaces for non-neutral tiers only ──

def test_format_emits_authority_tag_for_official():
    q = "AWS CloudFront pricing"
    out = fmt([_r("https://aws.amazon.com/cloudfront/pricing/", "Pricing", "…")], query=q)
    assert "Authority: OFFICIAL SOURCE" in out
    assert "URL: https://aws.amazon.com/cloudfront/pricing/" in out


def test_format_flags_aggregator():
    q = "AWS CloudFront pricing"
    out = fmt([_r("https://latencycost.com/cdn", "CDN compare", "…")], query=q)
    assert "third-party aggregator" in out


def test_format_no_tag_when_neutral_or_no_query():
    # Neutral domain → no Authority line at all.
    out = fmt([_r("https://en.wikipedia.org/wiki/CDN", "CDN", "…")], query="CDN")
    assert "Authority:" not in out
    # No query passed → backward-compatible, no tags.
    out2 = fmt([_r("https://aws.amazon.com/cloudfront/", "Pricing", "…")])
    assert "Authority:" not in out2


def test_authority_label_empty_for_neutral():
    assert authority_label("https://en.wikipedia.org/wiki/X", "x") == ""
