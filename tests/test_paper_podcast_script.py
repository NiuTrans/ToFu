#!/usr/bin/env python3
"""Unit tests for the paper-podcast script layer (Layer 1).

Covers the deterministic quality gates (lib/paper/podcast_engine/_validate.py)
and the generation pipeline (lib/paper/podcast_engine/_script.py) with a mocked
dispatch_chat. The four owner-mandated gates are all here:

  * Unicode math symbols (α β ² × ≤ →) — hole #1;
  * zh abbreviation watchlist (LLM / KV cache) — hole #2;
  * number provenance with DERIVED channels (diff / relative-change / ratio)
    — hole #4, incl. a NEUTER proving the derived channel is load-bearing
    (literal-only matching must flag "高 3.2 个百分点" from 86.3−83.1);
  * gate load-bearing NEUTERs: with a gate amputated the dirty script passes,
    with it intact the same script fails.

Run standalone (python tests/test_paper_podcast_script.py) or via pytest.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
    pytestmark = pytest.mark.unit
except ImportError:
    pass


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


SRC = ('实验结果显示,本文方法在三个数据集上分别达到 86.3、85.0 和 84.2,'
       '上一代最强基线为 83.1。模型规模为 130 亿参数,训练耗时 512 小时。')


def _pad_zh(s: str, n: int) -> str:
    """Pad a zh sentence to length with gate-clean filler prose."""
    filler = '这里继续展开方法的直觉与动机,解释为什么这样设计是合理的。'
    while len(s) < n:
        s += filler
    return s


def _good_script(mode_chars: int = 1200) -> dict:
    """A gate-clean zh script: 5 segments ≈ mode_chars total (~288s spoken)."""
    per = mode_chars // 5
    segs = [
        {'section': 'cold_open',
         'text': _pad_zh('一篇论文把准确率推到了 86.3,比上一代最强基线高了 3.2 个百分点,'
                         '而代价只是训练 512 小时。', per)},
        {'section': 'problem',
         'text': _pad_zh('要解决的问题很具体:长上下文下注意力开销随长度平方增长。', per)},
        {'section': 'method',
         'text': _pad_zh('方法的核心是一个稀疏路由,每个词只和少数键做交互。', per)},
        {'section': 'experiments',
         'text': _pad_zh('三个数据集上分别达到 86.3、85.0 和 84.2,基线是 83.1。', per)},
        {'section': 'recap',
         'text': _pad_zh('三条带走:第一,稀疏路由把平方增长压了下来;第二,86.3 的成绩'
                         '来自 512 小时的训练;第三,办法简单,容易复现。', per)},
    ]
    return {'title': '稀疏注意力论文精读', 'segments': segs}


def _dirty_script() -> dict:
    s = _good_script()
    segs = s['segments']
    segs[1]['text'] = _pad_zh('LLM 的 KV cache 在这里是瓶颈,平方项是 $O(n^2)$,'
                              '也就是 α 乘以 n²,提升 ≤ 2 倍,趋势 → 下降。', 240)
    return s


# ═══ Validators ═══

def test_latex_residue():
    from lib.paper.podcast_engine._validate import check_latex_residue
    dirty = '损失是 $L = \\frac{1}{2} x^{2}$,另有 \\sum_i 和 \\alpha。'
    issues = check_latex_residue(dirty)
    assert issues, 'LaTeX residue not detected'
    assert not check_latex_residue('这是一段干净的口播文字,没有任何公式记号。')
    _ok('gate 1a: LaTeX residue caught / clean prose passes')


def test_unicode_math_caught():
    from lib.paper.podcast_engine._validate import check_unicode_math
    issues = check_unicode_math('参数 α 的平方项 n² 让增长 ≤ 2 倍,趋势 → 下降,误差不等于 0。')
    assert issues, 'Unicode math symbols sailed past the gate (owner hole #1)'
    joined = issues[0]
    for ch in ('α', '²', '≤', '→'):
        assert ch in joined, f'{ch} not reported'
    _ok('gate 1b: raw Unicode math symbols caught (owner hole #1)')


def test_unicode_math_spoken_forms_pass():
    from lib.paper.podcast_engine._validate import check_unicode_math
    clean = '参数阿尔法的平方项让增长不超过两倍,约等于基线的水平,最后推出下降的结论。'
    assert not check_unicode_math(clean)
    _ok('gate 1b: spoken names (阿尔法/平方/不超过) pass')


def test_abbreviations_zh():
    from lib.paper.podcast_engine._validate import check_abbreviations
    issues = check_abbreviations('这个 LLM 的 KV cache 是瓶颈,GPU 跑不动。', 'zh')
    toks = ' '.join(issues)
    assert 'LLM' in toks and 'KV cache' in toks and 'GPU' in toks, toks
    # 'KV cache' must NOT double-fire the shorter 'KV' token
    assert toks.count('KV') == 1 or '"KV "' not in toks, toks
    # spoken forms pass
    assert not check_abbreviations('这个大语言模型的键值缓存是瓶颈,显卡跑不动。', 'zh')
    # English scripts exempt (English voices read acronyms fine)
    assert not check_abbreviations('The LLM uses a KV cache on the GPU.', 'en')
    _ok('gate 1c: zh watchlist caught / spoken forms pass / en exempt (owner hole #2)')


def test_extract_data_numbers_skips_structural():
    from lib.paper.podcast_engine._validate import extract_data_numbers
    vals = [d['value'] for d in extract_data_numbers(
        '三条带走:第一,2024 年的工作做到 86.3,提升 2 倍,训练 512 小时,共 5 个数据集。')]
    assert 86.3 in vals and 512 in vals
    assert 2 in vals, 'suffixed 倍 number must count as data'
    assert 2024 not in vals, 'bare year must be exempt'
    assert 3 not in vals and 5 not in vals, 'structural small ints must be exempt'
    _ok('number extraction: data kept / structural + years exempt')


def test_number_provenance_literal():
    from lib.paper.podcast_engine._validate import check_number_provenance
    assert not check_number_provenance('成绩是 86.3。', SRC)
    # rounding tolerance: source has 86.34, script rounds to 86.3
    assert not check_number_provenance('成绩是 86.3。', SRC + ' 精确值 86.34。')
    _ok('provenance: literal hit + rounding tolerance pass')


def test_number_provenance_derived_diff():
    from lib.paper.podcast_engine._validate import check_number_provenance
    # 3.2 does NOT appear in SRC; it is 86.3 − 83.1 (owner hole #4 case)
    assert '3.2' not in SRC
    issues = check_number_provenance('比上一代高了 3.2 个百分点。', SRC)
    assert not issues, f'legitimate derived number killed: {issues}'
    _ok('provenance: derived percentage-point diff (86.3−83.1=3.2) passes (owner hole #4)')


def test_number_provenance_derived_percent_and_ratio():
    from lib.paper.podcast_engine._validate import check_number_provenance
    src = '基线吞吐是 100,我们的方法是 120,延迟从 50 降到 25。'
    assert not check_number_provenance('相对提升了 20%。', src)          # (120−100)/100
    assert not check_number_provenance('吞吐是原来的 1.2 倍。', src)       # 120/100
    assert not check_number_provenance('延迟减半,只有原来的 0.5 倍。', src)  # 25/50
    _ok('provenance: derived relative-change and ratios pass')


def test_number_provenance_flags_fabricated():
    from lib.paper.podcast_engine._validate import check_number_provenance
    issues = check_number_provenance('准确率达到了 99.9。', SRC)
    assert issues and '99.9' in issues[0]
    _ok('provenance: fabricated number flagged')


def test_number_provenance_NEUTER_derived_is_loadbearing():
    """NEUTER: with the derived channels amputated (literal-only matching),
    "高 3.2 个百分点" MUST be flagged — proving the derived channel is what
    carries the owner hole #4 case, not an accidental pass."""
    import lib.paper.podcast_engine._validate as V
    orig = V._number_traces

    def _literal_only(n, decimals, src):
        tol = max(0.5 * (10 ** (-decimals)), 1e-9)
        return any(abs(s - n) <= tol for s in src)

    V._number_traces = _literal_only
    try:
        issues = V.check_number_provenance('比上一代高了 3.2 个百分点。', SRC)
        assert issues, 'NEUTER failed: literal-only matching did NOT flag 3.2'
    finally:
        V._number_traces = orig
    # Control: with the real function the same case passes (tested above)
    assert not V.check_number_provenance('比上一代高了 3.2 个百分点。', SRC)
    _ok('NEUTER: amputating derived channels flips 3.2pp to flagged — channel load-bearing')


def test_structure_gates():
    from lib.paper.podcast_engine._validate import check_structure
    good = _good_script()
    assert not check_structure(good, []), check_structure(good, [])

    bad1 = dict(good); bad1['segments'] = list(good['segments'])
    bad1['segments'][0] = dict(good['segments'][0], section='problem')
    assert any('cold_open' in i for i in check_structure(bad1, []))

    bad2 = dict(good); bad2['segments'] = list(good['segments'])
    bad2['segments'][0] = dict(good['segments'][0],
                               text=_pad_zh('开场白没有任何数字钩子。', 240).replace('86.3', ''))
    bad2['segments'][0]['text'] = ''.join(c for c in bad2['segments'][0]['text']
                                          if not c.isdigit())
    assert any('数字' in i for i in check_structure(bad2, []))

    bad3 = dict(good); bad3['segments'] = list(good['segments'])
    bad3['segments'][-1] = dict(good['segments'][-1], section='landscape')
    assert any('recap' in i for i in check_structure(bad3, []))

    bad4 = dict(good); bad4['segments'] = list(good['segments'])
    bad4['segments'][2] = dict(good['segments'][2], figure_ref='fig_99_p1.png')
    assert any('figure_ref' in i for i in check_structure(bad4, ['fig_01_p2.png']))

    good_fig = _good_script()
    good_fig['segments'][2]['figure_ref'] = 'fig_01_p2.png'
    assert not check_structure(good_fig, ['fig_01_p2.png'])
    _ok('gate 3: cold_open/digit-hook/recap/figure_ref whitelist all enforced')


def test_duration_band():
    from lib.paper.podcast_engine._validate import check_duration, estimate_seconds
    assert not check_duration(_good_script(1200), 'short', 'zh')
    tiny = {'segments': [{'section': 'cold_open', 'text': '太短了 1。'},
                          {'section': 'problem', 'text': '也短。'},
                          {'section': 'method', 'text': '短。'},
                          {'section': 'recap', 'text': _pad_zh('收。', 90)}]}
    assert check_duration(tiny, 'short', 'zh')
    assert estimate_seconds('一' * 250) == 60.0
    _ok('gate 4: ±20% duration band enforced (250 字/分)')


def test_NEUTER_unicode_gate_loadbearing():
    """NEUTER: a script with a raw α must FAIL validate_script; with the
    unicode-math gate amputated it must PASS — the gate carries the case."""
    import lib.paper.podcast_engine._validate as V
    script = _good_script()
    script['segments'][1]['text'] = _pad_zh('这里的系数记作 α,含义是步长。', 240)
    full = lambda: V.validate_script(script, mode='short', lang='zh',
                                     source_text=SRC, manifest_files=[])
    assert any('数学符号' in i for i in full()), 'raw α must fail the full gate set'
    orig = V.check_unicode_math
    V.check_unicode_math = lambda text: []
    try:
        assert not full(), 'NEUTER: with gate amputated the α-script must pass'
    finally:
        V.check_unicode_math = orig
    _ok('NEUTER: amputating unicode-math gate flips α-script pass↔fail')


# ═══ Script pipeline (mocked dispatch_chat) ═══

def test_parse_script_json():
    from lib.paper.podcast_engine._script import ScriptParseError, parse_script_json
    good = json.dumps(_good_script(), ensure_ascii=False)
    assert parse_script_json(good)['segments']
    fenced = '```json\n' + good + '\n```'
    assert parse_script_json(fenced)['segments']
    try:
        parse_script_json('这不是 JSON')
        raise AssertionError('garbage must raise ScriptParseError')
    except ScriptParseError:
        pass
    _ok('parse: plain/fenced JSON accepted, garbage rejected')


def test_normalize_script():
    from lib.paper.podcast_engine._script import normalize_script
    raw = {'title': ' t ', 'segments': [
        {'section': 'cold_open', 'text': '  有数字 1 的开场。  '},
        {'text': ''},
        'garbage',
        {'section': 'recap', 'text': '总结。', 'est_seconds': 999},
    ]}
    out = normalize_script(raw, mode='short', lang='zh')
    assert len(out['segments']) == 2
    assert out['segments'][0]['id'] == 0 and out['segments'][1]['id'] == 1
    assert out['segments'][1]['speaker'] == 'host'
    assert out['segments'][1]['est_seconds'] == 0.0, 'LLM est must be discarded'
    assert out['segments'][0]['text'].startswith('有数字')
    assert out['segments_dropped'] == 0


def test_normalize_caps_segments_and_the_quality_gate_reports_the_drop():
    from lib.paper.podcast_engine._script import normalize_script
    from lib.paper.podcast_engine._validate import check_structure

    raw = {
        'title': 'too many',
        'segments': [
            {'section': 'method', 'text': f'第 {index} 段' + ('内容' * 30)}
            for index in range(30)
        ],
    }
    script = normalize_script(raw, mode='short', lang='zh')

    assert len(script['segments']) == 24
    assert script['segments_dropped'] == 6
    assert any('段落超过上限 24' in issue for issue in
               check_structure(script, [], mode='short'))
    _ok('normalize: ids assigned, defaults filled, junk dropped, LLM est discarded')


def test_render_figure_list():
    from lib.paper.podcast_engine._script import render_figure_list
    text, files = render_figure_list([
        {'url': '/api/paper/images/abc/fig_01_p2.png', 'caption': 'Framework', 'page': 2},
        {'url': '/api/paper/images/abc/fig_02_p5.jpg', 'caption': '', 'page': 5}])
    assert files == ['fig_01_p2.png', 'fig_02_p5.jpg']
    assert 'Framework' in text
    empty_text, empty_files = render_figure_list([])
    assert not empty_files and '不得' in empty_text
    _ok('figure list: basenames extracted, empty manifest renders the no-figure note')


def _install_dispatch_mock(replies):
    """Patch _script.dispatch_chat + dispatch_stream with reply sequences.

    The critic still goes through dispatch_chat; every script pass goes
    through dispatch_stream (3-tuple return). The stream fake replays the
    reply through on_content in two chunks so the accumulation path in
    _stream_call is exercised exactly as in production."""
    import lib.paper.podcast_engine._script as S
    calls = []

    def _next_script():
        script = replies['script'][min(replies['script_idx'][0], len(replies['script']) - 1)]
        replies['script_idx'][0] += 1
        return script

    def _fake_chat(messages, **kwargs):
        calls.append(messages)
        return replies['critic'], {'prompt_tokens': 1, 'completion_tokens': 1}

    def _fake_stream(messages, **kwargs):
        calls.append(messages)
        script = _next_script()
        on_content = kwargs.get('on_content')
        if on_content:
            half = len(script) // 2
            on_content(script[:half])
            on_content(script[half:])
        return {}, 'stop', {'prompt_tokens': 10, 'completion_tokens': 100}

    orig_chat, orig_stream = S.dispatch_chat, S.dispatch_stream
    S.dispatch_chat = _fake_chat
    S.dispatch_stream = _fake_stream

    def _restore():
        S.dispatch_chat = orig_chat
        S.dispatch_stream = orig_stream
    return calls, _restore


def test_generate_script_happy_path():
    from lib.paper.podcast_engine._script import generate_script
    good = json.dumps(_good_script(), ensure_ascii=False)
    replies = {'script': [good], 'script_idx': [0], 'critic': '{"issues": []}'}
    calls, restore = _install_dispatch_mock(replies)
    try:
        script, meta = generate_script(
            source_text=SRC, lang='zh', mode='short', title='测试论文',
            images=[], model=None, source_kind='report')
        assert not meta['low_confidence'], meta
        assert meta['issues'] == []
        assert len(calls) == 2, f'script + critic = 2 calls, got {len(calls)}'
        assert all(seg['est_seconds'] > 0 for seg in script['segments'])
        assert script['lang'] == 'zh' and script['mode'] == 'short'
    finally:
        restore()
    _ok('pipeline: clean one-shot generation (script + critic), est stamped')


def test_generate_script_gate_revision():
    from lib.paper.podcast_engine._script import generate_script
    dirty = json.dumps(_dirty_script(), ensure_ascii=False)
    good = json.dumps(_good_script(), ensure_ascii=False)
    replies = {'script': [dirty, good], 'script_idx': [0], 'critic': '{"issues": []}'}
    calls, restore = _install_dispatch_mock(replies)
    try:
        script, meta = generate_script(
            source_text=SRC, lang='zh', mode='short', title='t', images=[], model=None)
        assert meta['revisions'] == 1, meta
        assert meta['issues'] == [], meta
        assert not meta['low_confidence']
        # the revision request must carry the gate issue list
        assert any('缩写' in (c[-1]['content'] or '') or 'LaTeX' in (c[-1]['content'] or '')
                   or '数学符号' in (c[-1]['content'] or '') for c in calls[1:2])
    finally:
        restore()
    _ok('pipeline: dirty draft → gate-feedback revision → clean (issues fed back)')


def test_generate_script_low_confidence():
    from lib.paper.podcast_engine._script import generate_script
    dirty = json.dumps(_dirty_script(), ensure_ascii=False)
    replies = {'script': [dirty, dirty], 'script_idx': [0], 'critic': '{"issues": []}'}
    calls, restore = _install_dispatch_mock(replies)
    try:
        script, meta = generate_script(
            source_text=SRC, lang='zh', mode='short', title='t', images=[], model=None)
        assert meta['low_confidence'] is True
        assert meta['issues'], 'remaining gate issues must be recorded'
        # critic must be SKIPPED when hard gates still fail
        assert not any(len(c) == 1 and '审听编辑' in c[-1]['content'] for c in calls)
    finally:
        restore()
    _ok('pipeline: unfixable script → low_confidence + issues surfaced, critic skipped')


def test_generate_script_critic_revision():
    from lib.paper.podcast_engine._script import generate_script
    good = json.dumps(_good_script(), ensure_ascii=False)
    replies = {'script': [good, good], 'script_idx': [0],
               'critic': '{"issues": ["第二段数字与报告不符"]}'}
    calls, restore = _install_dispatch_mock(replies)
    try:
        script, meta = generate_script(
            source_text=SRC, lang='zh', mode='short', title='t', images=[], model=None)
        assert meta['critic_issues'] == ['第二段数字与报告不符'], meta
        assert meta['revisions'] == 1
        assert len(calls) == 3, f'script + critic + revision = 3, got {len(calls)}'
        assert not meta['low_confidence']
    finally:
        restore()
    _ok('pipeline: critic flags → one revision → clean, critic_issues recorded')


def test_generate_script_streams_progress():
    """The draft pass must emit MEASURED progress events as content streams
    in: chars monotonically increasing, segments counted from "section" keys,
    and a restart resetting both to 0. Throttle is zeroed for the test."""
    import lib.paper.podcast_engine._script as S
    from lib.paper.podcast_engine._script import generate_script
    good = json.dumps(_good_script(), ensure_ascii=False)
    events = []

    def _fake_stream(messages, **kwargs):
        on_content = kwargs.get('on_content')
        on_restart = kwargs.get('on_attempt_restart')
        third = len(good) // 3
        on_content(good[:third])
        on_content(good[third:2 * third])
        if on_restart:
            on_restart(reason='test_restart')
        on_content(good[:third])
        on_content(good[third:])
        return {}, 'stop', {'prompt_tokens': 10, 'completion_tokens': 100}

    def _fake_chat(messages, **kwargs):
        return '{"issues": []}', {'prompt_tokens': 1, 'completion_tokens': 1}

    orig_chat, orig_stream = S.dispatch_chat, S.dispatch_stream
    orig_interval = S._STREAM_EVENT_MIN_INTERVAL
    S.dispatch_chat = _fake_chat
    S.dispatch_stream = _fake_stream
    S._STREAM_EVENT_MIN_INTERVAL = 0.0
    try:
        generate_script(source_text=SRC, lang='zh', mode='short', title='t',
                        images=[], model=None, on_event=events.append)
    finally:
        S.dispatch_chat = orig_chat
        S.dispatch_stream = orig_stream
        S._STREAM_EVENT_MIN_INTERVAL = orig_interval

    beats = [e for e in events if e.get('step') == 'draft' and 'chars' in e]
    assert beats, f'no streamed draft beats in {events}'
    seq = [e['chars'] for e in beats]
    assert seq[0] > 0 and seq[1] > seq[0], f'chars not increasing: {seq}'
    assert 0 in seq[2:], f'restart must reset chars to 0: {seq}'
    assert seq[-1] == len(good), f'final chars {seq[-1]} != full length {len(good)}'
    assert all(e['char_target'] == 1500 for e in beats), 'zh/short target is 1500'
    assert beats[-1]['segments'] == good.count('"section"'), beats[-1]
    _ok('pipeline: streamed draft emits measured chars/segments, restart resets')


def test_critic_disabled_by_env():
    from lib.paper.podcast_engine._script import generate_script
    good = json.dumps(_good_script(), ensure_ascii=False)
    replies = {'script': [good], 'script_idx': [0], 'critic': '{"issues": ["x"]}'}
    calls, restore = _install_dispatch_mock(replies)
    old = os.environ.get('TOFU_PAPER_PODCAST_CRITIC')
    os.environ['TOFU_PAPER_PODCAST_CRITIC'] = '0'
    try:
        generate_script(source_text=SRC, lang='zh', mode='short', title='t',
                        images=[], model=None)
        assert len(calls) == 1, 'critic must not run when env-disabled'
    finally:
        restore()
        if old is None:
            os.environ.pop('TOFU_PAPER_PODCAST_CRITIC', None)
        else:
            os.environ['TOFU_PAPER_PODCAST_CRITIC'] = old
    _ok('pipeline: TOFU_PAPER_PODCAST_CRITIC=0 skips the critic round')


def test_script_dispatch_is_abortable_and_retry_bounded():
    import lib.paper.podcast_engine._script as script_module
    from lib.paper.podcast_engine._errors import PodcastGenerationAborted

    signal = {'aborted': False}
    calls = []
    original = script_module.dispatch_stream
    old_budget = os.environ.get('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS')

    def interrupted(_messages, **kwargs):
        calls.append(kwargs)
        signal['aborted'] = True
        raise RuntimeError('transport stopped')

    script_module.dispatch_stream = interrupted
    os.environ['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] = '3'
    try:
        with pytest.raises(PodcastGenerationAborted):
            script_module.generate_script(
                source_text=SRC, lang='zh', mode='short', title='t',
                images=[], model=None,
                abort_check=lambda: signal['aborted'])
    finally:
        script_module.dispatch_stream = original
        if old_budget is None:
            os.environ.pop('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', None)
        else:
            os.environ['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] = old_budget

    assert len(calls) == 1
    assert calls[0]['max_retries'] == 2
    assert calls[0]['max_429_attempts'] == 3
    assert callable(calls[0]['abort_check'])


def main():
    print()
    print(_color('═══ Paper Podcast Script Layer Tests ═══', '36'))
    print()
    tests = [
        test_latex_residue,
        test_unicode_math_caught,
        test_unicode_math_spoken_forms_pass,
        test_abbreviations_zh,
        test_extract_data_numbers_skips_structural,
        test_number_provenance_literal,
        test_number_provenance_derived_diff,
        test_number_provenance_derived_percent_and_ratio,
        test_number_provenance_flags_fabricated,
        test_number_provenance_NEUTER_derived_is_loadbearing,
        test_structure_gates,
        test_duration_band,
        test_NEUTER_unicode_gate_loadbearing,
        test_parse_script_json,
        test_normalize_script,
        test_normalize_caps_segments_and_the_quality_gate_reports_the_drop,
        test_render_figure_list,
        test_generate_script_happy_path,
        test_generate_script_gate_revision,
        test_generate_script_low_confidence,
        test_generate_script_critic_revision,
        test_generate_script_streams_progress,
        test_critic_disabled_by_env,
        test_script_dispatch_is_abortable_and_retry_bounded,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} SCRIPT-LAYER TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
