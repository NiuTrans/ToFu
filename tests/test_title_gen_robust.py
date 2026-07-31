"""tests/test_title_gen_robust — pt_title_gen_robust P1 guard.

Root cause pinned by ``debug/title_gen_repro.py`` (5 forced deepseek-v4-pro
calls, 1/5 returned ``finish=length + cleaned=''``): cheap-pool models
carrying the ``thinking`` capability burn the entire max_tokens=512 budget
on reasoning, leaving the visible title empty or a single stray character
(the mryjczi2v9ck9k "跨" incident).

Fix (P1): ``generate_conversation_title`` passes ``exclude_models=`` to
``dispatch_chat`` so the cheap-tier thinking models are not eligible for
routing. The excluded set is a frozenset constant maintained inside
``title_gen`` — this test guards two invariants:

1. The constant exactly equals the set derived from
   ``bootstrap._BUILTIN_PROVIDER_TEMPLATES`` (i.e. every model with BOTH
   ``cheap`` AND ``thinking`` capabilities). If a sibling adds a new
   cheap+thinking model to bootstrap without updating the constant,
   this fails — forcing the update.
2. ``generate_conversation_title`` forwards that exact set as
   ``exclude_models=`` to the dispatcher. NEUTER check disproves a
   trivial no-op guard.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


pytestmark = pytest.mark.unit


def _derive_expected_from_bootstrap() -> frozenset[str]:
    """Compute the ground-truth set of cheap+thinking model ids from bootstrap."""
    bootstrap = importlib.import_module('bootstrap')
    result: set[str] = set()
    for provider in bootstrap._BUILTIN_PROVIDER_TEMPLATES:
        for m in provider.get('models', []):
            caps = set(m.get('capabilities', []))
            if {'cheap', 'thinking'}.issubset(caps):
                result.add(m['model_id'])
    return frozenset(result)


def test_constant_matches_bootstrap_derivation():
    """title_gen's excluded set ≡ every cheap+thinking model in bootstrap."""
    tg = importlib.import_module('lib.conversations.title_gen')
    assert hasattr(tg, '_THINKING_MODELS_TO_EXCLUDE'), \
        'title_gen must expose _THINKING_MODELS_TO_EXCLUDE constant'
    got = tg._THINKING_MODELS_TO_EXCLUDE
    assert isinstance(got, frozenset), \
        f'must be frozenset, got {type(got).__name__}'
    expected = _derive_expected_from_bootstrap()
    assert got == expected, (
        f'title_gen._THINKING_MODELS_TO_EXCLUDE out of sync with bootstrap.\n'
        f'  missing (in bootstrap, not in constant): {expected - got}\n'
        f'  extra (in constant, not in bootstrap): {got - expected}\n'
        f'Update _THINKING_MODELS_TO_EXCLUDE in lib/conversations/title_gen.py '
        f'when adding/removing a cheap+thinking model in bootstrap.py.')


def test_constant_contains_known_offenders():
    """Spot-check: the 4 models we sampled must be in the excluded set.

    Belt-and-suspenders — if bootstrap ever drops one of these while
    they remain live in a corp gateway template, we want to notice.
    """
    tg = importlib.import_module('lib.conversations.title_gen')
    got = tg._THINKING_MODELS_TO_EXCLUDE
    for name in ('deepseek-v4-pro', 'kimi-k2-thinking',
                 'qwen3-max', 'glm-4.7'):
        assert name in got, (
            f'{name} sampled as producing finish=length on title tasks '
            f'must stay in _THINKING_MODELS_TO_EXCLUDE')


def test_generate_forwards_exclude_models_to_dispatch():
    """generate_conversation_title MUST forward the constant as exclude_models."""
    tg = importlib.import_module('lib.conversations.title_gen')
    captured: dict = {}

    def _fake_dispatch(msgs, **kwargs):
        captured['kwargs'] = kwargs
        return '正常标题', {
            'finish_reason': 'stop',
            'completion_tokens': 8,
            '_dispatch': {'model': 'gpt-4.1-mini'},
        }

    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = _fake_dispatch
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': 'hi'},
             {'role': 'assistant', 'content': 'hey'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    assert title == '正常标题'
    assert 'exclude_models' in captured.get('kwargs', {}), (
        'generate_conversation_title must pass exclude_models=')
    fwd = set(captured['kwargs']['exclude_models'])
    assert fwd == set(tg._THINKING_MODELS_TO_EXCLUDE), (
        f'forwarded exclude_models {fwd} != constant '
        f'{set(tg._THINKING_MODELS_TO_EXCLUDE)}')


def test_neuter_forwarding_check_would_fail_on_empty_exclude():
    """NEUTER: if generate_conversation_title stopped forwarding the constant
    (or forwarded an empty list), test_generate_forwards_exclude_models_to_dispatch
    MUST turn red. Prove non-triviality by hand-constructing the negative case."""
    tg = importlib.import_module('lib.conversations.title_gen')
    # The forwarding-check compares to a NON-EMPTY constant; if a would-be
    # regression forwarded exclude_models=[] we should be able to detect
    # inequality against the real constant. Sanity: the constant is non-empty.
    assert len(tg._THINKING_MODELS_TO_EXCLUDE) >= 4, (
        'constant is expected to hold at least the 4 known offenders — '
        'if it drops below that, the NEUTER guard is weakened')


# ─────────────────────────────────────────────────────────────
# P2: _clean_title changed from "first non-empty line" to
# "strip label + merge all non-empty lines with single-space
# separator". Rationale: hypothetical "跨\n设备协同状态同步" would
# have been silently truncated to "跨" under the old rule.
# ─────────────────────────────────────────────────────────────


def test_clean_title_merges_multiline_output():
    """The reported "跨" failure mode: if a model splits topic across
    two lines, the cleaner must MERGE them, not drop the second."""
    tg = importlib.import_module('lib.conversations.title_gen')
    got = tg._clean_title('跨\n设备协同状态同步')
    # After merge: single string containing both parts.
    assert '跨' in got, f'first line dropped: {got!r}'
    assert '设备协同状态同步' in got, (
        f'second line silently dropped (bug the P2 change fixes): {got!r}')


def test_clean_title_strips_label_prefix():
    """`Title: xxx` / `标题：xxx` label must still be stripped —
    the merging change must not lose this existing invariant."""
    tg = importlib.import_module('lib.conversations.title_gen')
    for raw, expect in [
        ('Title: 主题内容', '主题内容'),
        ('标题：主题内容', '主题内容'),
        ('标题: 主题内容', '主题内容'),
        ('TITLE: X', 'X'),
    ]:
        got = tg._clean_title(raw)
        assert got == expect, (
            f'label strip failed for {raw!r}: got {got!r}, want {expect!r}')


def test_clean_title_strips_wrapping_quotes():
    """Quote wrapping is a separate invariant — merging must not
    break the strip of leading/trailing quotes."""
    tg = importlib.import_module('lib.conversations.title_gen')
    for raw, expect in [
        ('"quoted title"', 'quoted title'),
        ("'single quoted'", 'single quoted'),
        ('「书名号」', '书名号'),
        ('《书名号》', '书名号'),
    ]:
        got = tg._clean_title(raw)
        assert got == expect, (
            f'quote strip failed for {raw!r}: got {got!r}, want {expect!r}')


def test_clean_title_truncates_over_max_chars():
    """Hard cap TITLE_MAX_CHARS still enforced after merge."""
    tg = importlib.import_module('lib.conversations.title_gen')
    long_raw = '主' * 100
    got = tg._clean_title(long_raw)
    assert len(got) <= tg.TITLE_MAX_CHARS + 1, (
        f'no truncation: len={len(got)}, cap={tg.TITLE_MAX_CHARS}')
    assert got.endswith('…') or len(got) == tg.TITLE_MAX_CHARS, (
        f'truncation should end with ellipsis: {got!r}')


def test_clean_title_label_before_merge():
    """Label strip must happen BEFORE line merge, else `Title:\nX` merges
    to `Title: X` and the strip regex fails on the merged form."""
    tg = importlib.import_module('lib.conversations.title_gen')
    # Label on line 1 alone, real title on line 2 — must both strip AND merge.
    got = tg._clean_title('Title:\n主题内容')
    assert got == '主题内容', (
        f'label-before-merge order broken: got {got!r}')


# ─────────────────────────────────────────────────────────────
# P3: on a suspicious result (finish=length OR <=3 chars OR empty)
# do exactly ONE automatic retry with the failing model added to
# exclude_models. Two failures → _fallback_title.
# ─────────────────────────────────────────────────────────────


def _make_dispatch_stub(scripted_returns):
    """Build a fake dispatch_chat that returns items from a list in order.

    Each item is ``(content, usage)`` — usage MUST carry _dispatch.model
    so the retry logic can add it to exclude_models on the next attempt.
    The stub also RECORDS every call's ``exclude_models`` kwarg into
    ``captured_calls`` for assertions.
    """
    calls: list[dict] = []
    idx = [0]

    def _stub(msgs, **kwargs):
        i = idx[0]
        idx[0] += 1
        calls.append({
            'attempt': i,
            'exclude_models': list(kwargs.get('exclude_models') or []),
            'kwargs': kwargs,
        })
        if i >= len(scripted_returns):
            raise RuntimeError(
                f'stub called {i+1} times, only scripted {len(scripted_returns)}')
        return scripted_returns[i]

    return _stub, calls


def test_retry_on_finish_length_then_success():
    """1st call: deepseek burnout (finish=length, empty content).
    2nd call: gpt-4.1-mini stop, real title.
    Must return 2nd call's title, and 2nd call's exclude_models must
    contain the 1st call's actual model."""
    tg = importlib.import_module('lib.conversations.title_gen')
    scripted = [
        # Attempt 1: A-class burnout on a non-thinking model that leaked
        # through (imagine a corp gateway slot with unclamped budget).
        ('', {
            'finish_reason': 'length',
            'completion_tokens': 512,
            'reasoning_tokens': 500,
            '_dispatch': {'model': 'flaky-flash-3'},
        }),
        # Attempt 2: healthy stop.
        ('跨设备同步机制现状解析', {
            'finish_reason': 'stop',
            'completion_tokens': 20,
            '_dispatch': {'model': 'gpt-4.1-mini'},
        }),
    ]
    stub, calls = _make_dispatch_stub(scripted)
    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = stub
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': 'test'},
             {'role': 'assistant', 'content': 'ok'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    assert title == '跨设备同步机制现状解析', (
        f'retry should have returned attempt-2 title, got {title!r}')
    assert len(calls) == 2, (
        f'expected exactly 2 dispatch calls, got {len(calls)}')
    # Attempt 2's exclude_models must contain attempt 1's model.
    a2_ex = calls[1]['exclude_models']
    assert 'flaky-flash-3' in a2_ex, (
        f'retry exclude_models {a2_ex} missing failing model flaky-flash-3')
    # Attempt 2 must ALSO still carry the base thinking-model exclusion set.
    a1_ex = set(calls[0]['exclude_models'])
    assert a1_ex.issubset(set(a2_ex)), (
        'retry lost the base thinking-exclusion set')


def test_retry_on_short_title_then_success():
    """1st call: single-char 「跨」 (C-class flake).
    2nd call: real title. Same contract — retry must happen."""
    tg = importlib.import_module('lib.conversations.title_gen')
    scripted = [
        ('跨', {
            'finish_reason': 'stop',
            'completion_tokens': 4,
            '_dispatch': {'model': 'flaky-mini'},
        }),
        ('跨设备同步机制现状', {
            'finish_reason': 'stop',
            'completion_tokens': 15,
            '_dispatch': {'model': 'reliable-mini'},
        }),
    ]
    stub, calls = _make_dispatch_stub(scripted)
    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = stub
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': 'test'},
             {'role': 'assistant', 'content': 'ok'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    assert title == '跨设备同步机制现状', (
        f'retry should return attempt-2 title, got {title!r}')
    assert len(calls) == 2
    assert 'flaky-mini' in calls[1]['exclude_models']


def test_both_attempts_bad_falls_back():
    """1st call: length burnout empty. 2nd call: still empty (worst case).
    Must fall back to _fallback_title (truncated first user message)."""
    tg = importlib.import_module('lib.conversations.title_gen')
    scripted = [
        ('', {
            'finish_reason': 'length',
            '_dispatch': {'model': 'flaky-1'},
        }),
        ('', {
            'finish_reason': 'length',
            '_dispatch': {'model': 'flaky-2'},
        }),
    ]
    stub, calls = _make_dispatch_stub(scripted)
    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = stub
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': '这是用户第一条消息内容用来做 fallback'},
             {'role': 'assistant', 'content': 'ok'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    # After both attempts fail, _fallback_title returns the truncated first
    # user message (not a model-generated title).
    assert '这是用户第一条消息内容' in title, (
        f'expected fallback to first-user-message text, got {title!r}')
    assert len(calls) == 2, (
        f'expected exactly 2 attempts before falling back, got {len(calls)}')


def test_no_retry_on_first_success():
    """First call clean → NO retry. Dispatcher must be called exactly once."""
    tg = importlib.import_module('lib.conversations.title_gen')
    scripted = [
        ('正常的六字标题', {
            'finish_reason': 'stop',
            '_dispatch': {'model': 'gpt-4.1-mini'},
        }),
    ]
    stub, calls = _make_dispatch_stub(scripted)
    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = stub
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': 'x'},
             {'role': 'assistant', 'content': 'y'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    assert title == '正常的六字标题'
    assert len(calls) == 1, (
        f'first attempt was clean, dispatcher must NOT be re-called; '
        f'got {len(calls)} calls')


def test_retry_neuter_would_regress_without_retry_logic():
    """NEUTER contract: if the retry loop were removed and a single bad
    call directly returned _clean_title(''), test_retry_on_finish_length
    would return '' (or _fallback_title on empty), which is NOT the
    happy-path attempt-2 title. This test asserts the retry-produced
    title differs from both the bad first attempt AND the fallback —
    proving the retry logic is actually the reason we get the good title."""
    tg = importlib.import_module('lib.conversations.title_gen')
    scripted = [
        ('', {'finish_reason': 'length',
              '_dispatch': {'model': 'bad-model'}}),
        ('好标题内容', {'finish_reason': 'stop',
              '_dispatch': {'model': 'good-model'}}),
    ]
    stub, calls = _make_dispatch_stub(scripted)
    fake_mod = types.ModuleType('lib.llm_dispatch')
    fake_mod.dispatch_chat = stub
    sys.modules['lib.llm_dispatch'] = fake_mod
    try:
        title = tg.generate_conversation_title(
            [{'role': 'user', 'content': 'msg body for fallback'},
             {'role': 'assistant', 'content': 'a'}],
            lang='zh')
    finally:
        sys.modules.pop('lib.llm_dispatch', None)

    # Must NOT be the first-attempt empty result.
    assert title != '', 'retry did not run — returned first-attempt empty'
    # Must NOT be the fallback (first-user-message truncation).
    assert 'msg body' not in title, (
        f'retry did not run — fell back to first-user-message: {title!r}')
    # MUST be the second attempt's clean title.
    assert title == '好标题内容', (
        f'expected retry to produce attempt-2 title, got {title!r}')
