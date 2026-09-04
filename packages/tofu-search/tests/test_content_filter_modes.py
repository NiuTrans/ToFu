"""Tests for the 0.6.0 content-filter rework (fetch/content_filter.py):

* ``filter_mode='gate'`` (new default) — verdict-only LLM call on the CAPPED
  head of the page; useful pages keep their ORIGINAL text.
* ``filter_mode='rewrite'`` — the pre-0.6 full-page cleaning/regeneration.
* The ``filter_min_chars`` short-circuit is honoured again (the orchestrator
  used to force ``min_chars=0``, dragging every short page through the LLM).
* LLM errors/timeouts fall back to the raw text (filtering is an
  enhancement, never a blocker).
* The result cache (mode,url,query,user_question,raw_text) makes repeated
  searches of the same pages cost zero LLM calls.

All offline: the LLM is a fake callable injected via ``SearchConfig``.
"""

import inspect

import pytest

import tofu_search.fetch.content_filter as cf
from tofu_search.config import SearchConfig
from tofu_search.fetch.content_filter import (
    IRRELEVANT_SENTINEL,
    filter_web_content,
    filter_web_contents_batch,
)
from tofu_search.search import orchestrator


@pytest.fixture(autouse=True)
def _clear_filter_cache():
    cf._reset_filter_cache()
    yield
    cf._reset_filter_cache()


def _fake_llm(calls, response=None, handler=None):
    def fn(messages, **kwargs):
        calls.append({'messages': messages, 'kwargs': kwargs})
        if handler is not None:
            return handler(messages, **kwargs)
        return response
    return fn


def _cfg(fn, **overrides):
    base = dict(filter_enabled=True, llm_function=fn, filter_mode='gate',
                filter_min_chars=3000, filter_timeout=45,
                gate_input_max_chars=12_000,
                filter_cache_ttl=600, filter_cache_max_size=500)
    base.update(overrides)
    return SearchConfig(**base)


# ── Gate mode ──

def test_gate_useful_returns_original_text():
    calls = []
    raw = 'Real article body. ' * 400          # ~7,600 chars > 3,000 floor
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'))
    out = filter_web_content(raw, url='https://example.com/a', query='q', config=cfg)
    assert out == raw                            # original text, NOT an LLM rewrite
    assert len(calls) == 1
    # the irrelevance stop token is always passed so generation halts early
    assert '§§IRRELEVANT§§' in (calls[0]['kwargs'].get('stop') or [])


def test_gate_selects_within_gate_input_max_chars():
    calls = []
    raw = 'x' * 20_000
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'))
    filter_web_content(raw, url='https://example.com/long', query='q', config=cfg)
    user_msg = calls[0]['messages'][1]['content']
    assert 'selected ' in user_msg
    payload = user_msg.split('---\n', 1)[-1]
    assert 0 < len(payload) <= 12_000


def test_gate_can_see_relevant_section_beyond_old_head():
    calls = []
    raw = ('navigation filler words\n\n' * 900
           + 'The decisive quasar latency benchmark is 17 milliseconds.\n\n'
           + 'footer filler\n\n' * 100)
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'), gate_input_max_chars=4_000)
    filter_web_content(raw, url='https://example.com/late',
                       query='quasar latency benchmark', config=cfg)
    user_msg = calls[0]['messages'][1]['content']
    assert '17 milliseconds' in user_msg
    assert len(user_msg) < len(raw)


def test_gate_irrelevant_stop_token_returns_sentinel():
    calls = []
    cfg = _cfg(_fake_llm(calls, '§§IRRELEVANT§§'))
    out = filter_web_content('y' * 5000, url='https://example.com/wall',
                             query='q', config=cfg)
    assert out == IRRELEVANT_SENTINEL


def test_gate_irrelevant_sentinel_text_returns_sentinel():
    calls = []
    cfg = _cfg(_fake_llm(calls, '[IRRELEVANT]'))
    out = filter_web_content('y' * 5000, url='https://example.com/wall2',
                             query='q', config=cfg)
    assert out == IRRELEVANT_SENTINEL


def test_gate_prompt_is_verdict_only():
    calls = []
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'))
    filter_web_content('z' * 5000, url='https://example.com/p', query='q', config=cfg)
    system_msg = calls[0]['messages'][0]['content']
    assert 'relevance judge' in system_msg
    assert 'content cleaner' not in system_msg   # no rewrite instructions in gate mode


def test_unknown_filter_mode_falls_back_to_gate():
    calls = []
    raw = 'w' * 5000
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'), filter_mode='bogus')
    out = filter_web_content(raw, url='https://example.com/bogus', query='q', config=cfg)
    assert out == raw                            # gate semantics: original text


# ── min_chars short-circuit (regression: orchestrator forced min_chars=0) ──

def test_short_page_skips_llm_via_config_min_chars():
    calls = []
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'))
    out = filter_web_content('short' * 100, url='https://example.com/s',
                             query='q', config=cfg)   # 500 chars < 3000
    assert out == 'short' * 100
    assert calls == []                           # LLM never invoked


def test_batch_respects_min_chars_short_pages_pass_through():
    calls = []
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'))
    items = [('https://example.com/short', 'tiny' * 100),       # 400 chars — skip
             ('https://example.com/long', 'v' * 5000)]          # through the LLM
    out = filter_web_contents_batch(items, query='q', config=cfg)
    assert out['https://example.com/short'] == 'tiny' * 100
    assert out['https://example.com/long'] == 'v' * 5000
    assert len(calls) == 1                       # only the long page was judged


def test_orchestrator_no_longer_forces_min_chars_zero():
    # Source pin: step 5 must not re-introduce the min_chars=0 override.
    src = inspect.getsource(orchestrator)
    assert 'min_chars=0' not in src


# ── Rewrite mode (pre-0.6 behaviour, preserved as opt-in) ──

def test_rewrite_mode_returns_cleaned_text_and_sends_full_page():
    calls = []
    cleaned = ('Cleaned article. ' * 20).strip()  # >100 floor; response is strip()ed
    raw = 'r' * 20_000
    cfg = _cfg(_fake_llm(calls, '[USEFUL]\n' + cleaned), filter_mode='rewrite')
    out = filter_web_content(raw, url='https://example.com/rw', query='q', config=cfg)
    assert out == cleaned                        # the LLM rewrite is served
    user_msg = calls[0]['messages'][1]['content']
    assert 'r' * 20_000 in user_msg              # full page sent, no gate cap
    assert 'content cleaner' in calls[0]['messages'][0]['content']


# ── Failure fallback ──

def test_llm_error_returns_raw_text():
    def _boom(messages, **kwargs):
        raise RuntimeError('simulated timeout')
    calls = []
    raw = 'f' * 5000
    cfg = _cfg(_fake_llm(calls, handler=_boom))
    out = filter_web_content(raw, url='https://example.com/err', query='q', config=cfg)
    assert out == raw                            # filtering never blocks the page


# ── Empty-response anomaly (fail-open, never cached) ──

def test_empty_response_fails_open_and_is_not_cached():
    calls = []
    raw = 'e' * 5000
    cfg = _cfg(_fake_llm(calls, '   '))            # whitespace-only completion
    kwargs = dict(url='https://example.com/empty', query='q', config=cfg)
    assert filter_web_content(raw, **kwargs) == raw   # fail-open, NOT sentinel
    assert filter_web_content(raw, **kwargs) == raw
    assert len(calls) == 2                            # anomaly was NOT cached


def test_empty_string_response_fails_open():
    calls = []
    raw = 'n' * 5000
    cfg = _cfg(_fake_llm(calls, ''))               # empty completion
    out = filter_web_content(raw, url='https://example.com/emptystr', query='q', config=cfg)
    assert out == raw


# ── Result cache ──
# ── Result cache ──

def test_cache_hit_skips_second_llm_call():
    calls = []
    raw = 'c' * 5000
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'))
    kwargs = dict(url='https://example.com/cached', query='q', config=cfg)
    assert filter_web_content(raw, **kwargs) == raw
    assert filter_web_content(raw, **kwargs) == raw
    assert len(calls) == 1                       # second call served from cache


def test_cache_stores_irrelevant_verdict():
    calls = []
    cfg = _cfg(_fake_llm(calls, '§§IRRELEVANT§§'))
    kwargs = dict(url='https://example.com/irr', query='q', config=cfg)
    assert filter_web_content('i' * 5000, **kwargs) == IRRELEVANT_SENTINEL
    assert filter_web_content('i' * 5000, **kwargs) == IRRELEVANT_SENTINEL
    assert len(calls) == 1


def test_cache_key_includes_mode():
    calls = []
    cleaned = ('Mode-specific clean. ' * 20).strip()
    raw = 'm' * 5000
    common = dict(url='https://example.com/modes', query='q')
    gate_cfg = _cfg(_fake_llm(calls, '[USEFUL]'), **{})
    rewrite_cfg = _cfg(_fake_llm(calls, '[USEFUL]\n' + cleaned),
                       filter_mode='rewrite')
    assert filter_web_content(raw, config=gate_cfg, **common) == raw
    assert filter_web_content(raw, config=rewrite_cfg, **common) == cleaned
    assert len(calls) == 2                       # mode switch bypasses the cache


def test_cache_disabled_with_zero_ttl():
    calls = []
    raw = 'd' * 5000
    cfg = _cfg(_fake_llm(calls, '[USEFUL]'), filter_cache_ttl=0)
    kwargs = dict(url='https://example.com/nocache', query='q', config=cfg)
    filter_web_content(raw, **kwargs)
    filter_web_content(raw, **kwargs)
    assert len(calls) == 2
