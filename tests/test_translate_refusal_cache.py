"""tests/test_translate_refusal_cache.py — persistent refusal markers.

Production evidence (2026-07-27): one 488-char chunk was refused 36× in a
single day — every page load re-ran the full guard roster (5 model dispatches,
35-58 s) for a chunk whose content shape makes refusal deterministic, then
surfaced another 502. The retry budget exists to find a HEALTHY model; once
the budget is exhausted the verdict is a property of the CONTENT, not of the
model roster, so re-burning dispatches on every load buys nothing.

Fix: when a guard refuses after the full budget (``TranslationContentRefused``),
the engine records a small marker keyed by sha256(target | source | text)
under ``data/translate_refusal/``. A later call for the same chunk replays the
refusal INSTANTLY (zero dispatches, same typed envelope → same 502 shape).
The marker expires after ``TOFU_TRANSLATE_REFUSAL_TTL_DAYS`` (default 7) so a
healthier future model roster gets one fresh attempt; ``use_cache=False``
(the repair-script path) bypasses the replay and re-drives fresh.

failing-first: on pre-fix HEAD ``lib/translate_refusal`` does not exist, so
this suite errors at collection; after implementation every test passes.
NEUTER: monkeypatch-removing the lookup OR the record each turns a test RED.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_translate_refusal_cache.py -v
"""

from __future__ import annotations

import os
import time

import pytest

import lib.translate.engine as engine
import lib.translate_refusal as refusal
from lib.translate import TranslationContentRefused

pytestmark = pytest.mark.unit


_MIXED_SOURCE = (
    'Good question. The relevant code is in tool_display.py.\n\n'
    '## 为什么没有前缀\n'
    '直接原因很明确：模型这次用的是绝对路径，不是带前缀的命名空间路径。'
    '整套逻辑只能从两个来源推断出名字，而这条调用两个都命中不了。'
    '所以结论是：一个落在非主目录下的绝对路径，既不能从前缀解析出来，'
    '回退也只会指向主目录，两条路都到不了，于是前端就没有标签显示出来。'
)

_FLIPPED_EN = (
    'Good question. The relevant code is in tool_display.py.\n\n'
    '## Why there is no prefix\n'
    'The direct reason is clear: the model used an absolute path this time, '
    'not a namespace path with a prefix. The entire logic can only infer the '
    'name from two sources, and this call misses both. So the conclusion is: '
    'an absolute path under a non-primary directory cannot be parsed from the '
    'prefix, and the fallback only points to the primary directory, so neither '
    'path works and the frontend shows no label.'
)


def _isolate_store(monkeypatch, tmp_path):
    """Point the refusal store at a fresh tmp dir."""
    monkeypatch.setattr(refusal, '_REFUSAL_DIR', str(tmp_path / 'refusal'))


def test_default_pytest_refusal_store_uses_session_owned_root(
        monkeypatch, tmp_path):
    monkeypatch.setattr(refusal, '_REFUSAL_DIR', refusal._DEFAULT_DIR)
    monkeypatch.setenv('TOFU_PYTEST_RUN_ROOT', str(tmp_path))

    effective = refusal._effective_dir()

    assert os.path.commonpath((effective, str(tmp_path))) == str(tmp_path)


def _patch_models(monkeypatch, replies):
    """Disable MT + result-cache; the LLM seams return successive `replies`.
    state['i'] counts REAL LLM dispatches.

    BOTH dispatch paths are stubbed: the engine picks dispatch_stream when a
    progress_cb is present and smart_chat otherwise, and on the CI runner the
    pick differed from the dev box (smart_chat alone was stubbed → a REAL
    401-burning dispatch ran for 31s — 4dcea38 3.12 leg). An offline suite
    must make a live call IMPOSSIBLE, not merely unlikely."""
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(engine.translate_cache, 'get', lambda *a, **k: None)
    monkeypatch.setattr(engine.translate_cache, 'put', lambda *a, **k: None)
    state = {'i': 0}

    def _next():
        i = min(state['i'], len(replies) - 1)
        state['i'] += 1
        return replies[i]

    def _fake_smart_chat(messages=None, **kw):
        return _next(), {'finish_reason': 'stop',
                         '_dispatch': {'model': 'm0', 'key': 'k1'}}

    def _fake_dispatch_stream(messages, on_content=None, **kw):
        reply = _next()
        if on_content:
            on_content(reply)
        return ({'role': 'assistant', 'content': reply}, 'stop',
                {'finish_reason': 'stop',
                 '_dispatch': {'model': 'm0', 'key': 'k1'}})

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', _fake_smart_chat)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', _fake_dispatch_stream)
    return state


def _flip_call(**kw):
    return engine._translate_one_chunk(
        _MIXED_SOURCE, system_prompt='translate', source='English',
        target='Chinese', overall_deadline=30, **kw)


# ── 0. Store-level roundtrip / TTL / corruption ───────────────────────────

def test_store_roundtrip_and_corruption(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    assert refusal.get('text', 'English', 'Chinese') is None
    refusal.put('text', 'English', 'Chinese', verdict='wrong_language',
                reason='out latin-dominant', model='m0', content_fails=5)
    hit = refusal.get('text', 'English', 'Chinese')
    assert hit and hit['verdict'] == 'wrong_language'
    assert hit['reason'] == 'out latin-dominant' and hit['content_fails'] == 5
    # Corrupt payload → treated as absent, never raises.
    path = refusal._path_for(refusal._key('text', 'English', 'Chinese'))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert refusal.get('text', 'English', 'Chinese') is None


def test_store_ttl_expiry(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    refusal.put('text', 'English', 'Chinese', verdict='noop',
                reason='echo', model='m0', content_fails=5)
    path = refusal._path_for(refusal._key('text', 'English', 'Chinese'))
    import json as _json
    with open(path, encoding='utf-8') as f:
        payload = _json.load(f)
    payload['ts'] = int(time.time()) - (refusal._TTL_SECONDS + 60)
    with open(path, 'w', encoding='utf-8') as f:
        _json.dump(payload, f)
    assert refusal.get('text', 'English', 'Chinese') is None, (
        'expired marker was honoured — TTL check missing')


# ── 1. The replay: second call for a refused chunk costs ZERO dispatches ──

def test_second_call_replays_cached_refusal(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    state = _patch_models(monkeypatch, [_FLIPPED_EN])  # always flips
    with pytest.raises(TranslationContentRefused) as first:
        _flip_call()
    assert first.value.verdict == 'wrong_language'
    burned = state['i']
    assert burned >= 5, f'first refusal should burn the full budget, got {burned}'

    with pytest.raises(TranslationContentRefused) as second:
        _flip_call()
    assert state['i'] == burned, (
        f'refused chunk re-dispatched {state["i"] - burned}× — the refusal '
        'marker was not consulted')
    assert second.value.verdict == 'wrong_language'
    assert second.value.attempts == 0
    assert 'cached refusal' in second.value.reason


def test_replay_not_recorded_for_eventual_success(monkeypatch, tmp_path):
    """A chunk that fails once then SUCCEEDS inside the budget must leave no
    marker — only full-budget refusals are content-shaped verdicts."""
    _isolate_store(monkeypatch, tmp_path)
    good_zh = (
        '好问题。相关代码在 tool_display.py 里。\n\n'
        '## 为什么没有前缀\n'
        '直接原因很明确：模型这次用的是绝对路径，不是带前缀的命名空间路径。'
        '整套逻辑只能从两个来源推断出名字，而这条调用两个都命中不了。'
        '所以结论是：一个落在非主目录下的绝对路径，既不能从前缀解析出来，'
        '回退也只会指向主目录，两条路都到不了，于是前端就没有标签显示出来。'
    )
    _patch_models(monkeypatch, [_FLIPPED_EN, good_zh])
    out, _usage = _flip_call()
    assert out == good_zh.strip()
    assert refusal.get(_MIXED_SOURCE, 'English', 'Chinese') is None


# ── 2. Bypass paths: TTL expiry / use_cache=False / kill-switch ───────────

def test_expired_marker_retries_fresh(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    refusal.put(_MIXED_SOURCE, 'English', 'Chinese', verdict='wrong_language',
                reason='old verdict', model='m0', content_fails=5)
    path = refusal._path_for(refusal._key(_MIXED_SOURCE, 'English', 'Chinese'))
    import json as _json
    with open(path, encoding='utf-8') as f:
        payload = _json.load(f)
    payload['ts'] = int(time.time()) - (refusal._TTL_SECONDS + 60)
    with open(path, 'w', encoding='utf-8') as f:
        _json.dump(payload, f)

    state = _patch_models(monkeypatch, [_FLIPPED_EN])
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    assert state['i'] >= 1, 'expired marker was NOT re-tried — still replaying'


def test_use_cache_false_bypasses_replay(monkeypatch, tmp_path):
    """The repair-script path (use_cache=False) must re-drive fresh even when
    a marker exists."""
    _isolate_store(monkeypatch, tmp_path)
    refusal.put(_MIXED_SOURCE, 'English', 'Chinese', verdict='wrong_language',
                reason='stale verdict', model='m0', content_fails=5)
    state = _patch_models(monkeypatch, [_FLIPPED_EN])
    with pytest.raises(TranslationContentRefused):
        _flip_call(use_cache=False)
    assert state['i'] >= 1, 'use_cache=False still replayed the marker'


def test_kill_switch_disables_store(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    monkeypatch.setattr(refusal, '_ENABLED', False)
    state = _patch_models(monkeypatch, [_FLIPPED_EN])
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    assert state['i'] >= 10, 'kill-switch on: second call must re-dispatch'
    assert refusal.get(_MIXED_SOURCE, 'English', 'Chinese') is None


# ── 3. NEUTER: lookup and record are each load-bearing ────────────────────

def test_NEUTER_without_lookup_second_call_redispatches(monkeypatch, tmp_path):
    """Simulates deleting the engine's marker consult: the replay MUST
    disappear and the second call burns dispatches again."""
    _isolate_store(monkeypatch, tmp_path)
    state = _patch_models(monkeypatch, [_FLIPPED_EN])
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    burned = state['i']
    monkeypatch.setattr(engine.translate_refusal, 'get', lambda *a, **k: None)
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    assert state['i'] > burned, (
        'NEUTER FAILED: removing the lookup did not restore re-dispatch')


def test_NEUTER_without_record_second_call_redispatches(monkeypatch, tmp_path):
    """Simulates deleting the marker write on refusal: nothing is stored, so
    the second call burns dispatches again."""
    _isolate_store(monkeypatch, tmp_path)
    monkeypatch.setattr(engine.translate_refusal, 'put', lambda *a, **k: None)
    state = _patch_models(monkeypatch, [_FLIPPED_EN])
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    burned = state['i']
    with pytest.raises(TranslationContentRefused):
        _flip_call()
    assert state['i'] > burned, (
        'NEUTER FAILED: removing the record did not restore re-dispatch')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
