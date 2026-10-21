"""Bing search must preserve destination URLs and only unwrap its own redirects."""

import base64
from html import escape
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from tofu_search.search import orchestrator
from tofu_search.search.engines import bing


def wrapped(target, origin="https://www.bing.com", *, padded=False):
    payload = base64.urlsafe_b64encode(target.encode()).decode()
    if not padded:
        payload = payload.rstrip("=")
    return origin + "/ck/a?" + urlencode({"u": "a1" + payload})


@pytest.mark.parametrize("target", [
    "https://example.org/a?x=~~~&y=%2F#part",
    "https://example.org/a?x=???&y=%252F#part",
    "https://example.org/日本語?q=%E6%97%A5&next=https%3A%2F%2Fother.example%2Fx",
    "HTTPS://EXAMPLE.org/Case?next=%2F#Part",
])
@pytest.mark.parametrize("origin", ["https://www.bing.com", "//www.bing.com", ""])
@pytest.mark.parametrize("padded", [False, True])
def test_urlsafe_redirect_preserves_exact_destination(target, origin, padded):
    assert bing._bing_decode_url(wrapped(target, origin, padded=padded)) == target


@pytest.mark.parametrize("url", [
    "https://example.org/ck/archive?u=original",
    "https://example.org/ck/a?u=original",
    wrapped("https://other.example/new", "https://example.org"),
    wrapped("https://other.example/new", "https://www.bing.com.example.org"),
    wrapped("https://other.example/new", "https://example.org/www.bing.com"),
    "https://www.bing.com/ck/archive?u=original",
])
def test_non_tracking_urls_are_not_reinterpreted(url):
    assert bing._bing_decode_url(url) == url


@pytest.mark.parametrize("raw", [
    "https://www.bing.com/ck/a?u=a1@@@",
    "https://www.bing.com/ck/a?u=a1aHR0!!!!cHM6Ly9leGFtcGxlLm9yZy8",
    wrapped("https://example.org/") + "&u=",
    wrapped("https://example.org/") + "&u=a1aHR0cHM6Ly9vdGhlci5leGFtcGxlLw",
    "https://www.bing.com/ck/a?" + urlencode({"u": "a1" + base64.urlsafe_b64encode(b"https://example.org/\x80").decode()}),
    wrapped("http-not-a-url"),
    wrapped("https:///missing-host"),
    wrapped("javascript:alert(1)"),
    wrapped("https://example.org/with\nnewline"),
    wrapped("https://example.org/with space"),
])
def test_malformed_tracking_destinations_are_dropped_without_repair(raw):
    assert bing._bing_decode_url(raw) is None


def test_tracking_destinations_are_unwrapped_once():
    inner = wrapped("https://example.org/exact?next=%252F")
    assert bing._bing_decode_url(wrapped(inner)) == inner


def test_public_search_keeps_recovered_and_direct_links(monkeypatch):
    targets = ["https://example.org/a?x=~~~&y=%2F", "https://example.org/ck/archive",
               "HTTPS://EXAMPLE.org/Case?next=%2F#Part"]
    links = [wrapped(targets[0]), targets[1], targets[2], wrapped("https:///missing-host"),
             "http-not-a-url", "https:///missing-host"]
    html = "<ol id='b_results'>" + "".join(
        f"<li class='b_algo'><h2><a href='{escape(url, quote=True)}'>Kittens {i}</a></h2>"
        "<p>Evidence about kittens and their development.</p></li>"
        for i, url in enumerate(links)) + "</ol>"
    monkeypatch.setattr(bing, "http_search_get", lambda **kw: kw["parser"](SimpleNamespace(text=html)))
    monkeypatch.setattr(orchestrator, "search_bing", bing.search_bing)
    rows = orchestrator.perform_web_search(
        "kittens", max_results=6, engines=["Bing"], fetch_pages=False,
        filter_pages=False, rerank=False)
    assert [row["url"] for row in rows] == targets
