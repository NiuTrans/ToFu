"""Tests for the vertical _fetch_json helper, the npm repo-URL .git-strip fix,
and the LLM content-filter worker cap.
"""

import pytest

from tofu_search.search.vertical import base
from tofu_search.search.vertical import npm as npm_mod
from tofu_search.search.vertical.base import _FETCH_FAILED, _fetch_json


class FakeResp:
    def __init__(self, status_code=200, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json = json_data
        self._raise_json = raise_json

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._json


# ── _fetch_json ──

def test_fetch_json_success(monkeypatch):
    monkeypatch.setattr(base, "http_get",
                        lambda url, **kw: FakeResp(200, {"ok": True}))
    assert _fetch_json("https://api/x") == {"ok": True}


def test_fetch_json_http_error_returns_sentinel(monkeypatch):
    monkeypatch.setattr(base, "http_get", lambda url, **kw: FakeResp(404))
    assert _fetch_json("https://api/x") is _FETCH_FAILED


def test_fetch_json_request_exception_returns_sentinel(monkeypatch):
    def boom(url, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(base, "http_get", boom)
    assert _fetch_json("https://api/x") is _FETCH_FAILED


def test_fetch_json_parse_error_returns_sentinel(monkeypatch):
    monkeypatch.setattr(base, "http_get",
                        lambda url, **kw: FakeResp(200, raise_json=True))
    assert _fetch_json("https://api/x") is _FETCH_FAILED


def test_fetch_json_retries_once_on_429(monkeypatch):
    calls = []
    seq = [FakeResp(429), FakeResp(200, {"recovered": True})]

    def fake_get(url, **kw):
        calls.append(1)
        return seq[len(calls) - 1]

    monkeypatch.setattr(base, "http_get", fake_get)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)  # no real wait
    assert _fetch_json("https://api/x") == {"recovered": True}
    assert len(calls) == 2


def test_fetch_json_429_gives_up_after_one_retry(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(1)
        return FakeResp(429)

    monkeypatch.setattr(base, "http_get", fake_get)
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    assert _fetch_json("https://api/x") is _FETCH_FAILED
    assert len(calls) == 2  # initial + one retry, then stop


# ── npm repo-URL .git strip (regression for the .rstrip('.git') char-set bug) ──

@pytest.mark.parametrize("repo_in,expected", [
    ("git+https://github.com/user/digit.git", "https://github.com/user/digit"),
    ("git://github.com/user/repo.git", "https://github.com/user/repo"),
    # The old .rstrip('.git') would have mangled a trailing 'digit' → 'di'.
    ("https://github.com/user/digit", "https://github.com/user/digit"),
])
def test_npm_repo_url_git_strip(monkeypatch, repo_in, expected):
    payload = {
        "name": "pkg", "description": "d",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {}},
        "repository": {"url": repo_in},
        "homepage": "", "readme": "",
        "maintainers": [],
    }
    monkeypatch.setattr(base, "_fetch_json", lambda *a, **k: payload)
    out = npm_mod.search("pkg", {})
    assert out is not None
    assert f"**Repository**: {expected}" in out["content"]


# ── LLM content-filter worker cap (#9) ──

def test_filter_batch_worker_cap(monkeypatch):
    import tofu_search.fetch.content_filter as cf
    from tofu_search import configure

    configure(llm_api_key="sk-test", filter_min_chars=0)

    captured = {}

    class FakePool:
        def __init__(self, max_workers):
            captured["max_workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *a, **kw):
            class _F:
                def result(self_inner):
                    return "cleaned"
            return _F()

    monkeypatch.setattr(cf, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(cf, "as_completed", lambda futs: list(futs))

    items = [(f"https://e{i}.com", "x" * 5000) for i in range(25)]
    cf.filter_web_contents_batch(items, query="q")
    assert captured["max_workers"] == 8  # capped, not 25
