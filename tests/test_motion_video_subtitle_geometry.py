#!/usr/bin/env python3
"""tests/test_motion_video_subtitle_geometry.py — burn-in subtitle geometry.

Root-cause guards for the shipped defect (measured 2026-07-28): the pipeline
burned its sidecar SRT with NO style at all — every call site passed only
``fontsdir``, so ``force_style`` was never populated by anybody. A bare SRT
carries no ``[Script Info]``, so libass falls back to a 384x288 reference
frame and scales its default style by ``height/288``; combined with libass
refusing to wrap CJK (our cues are unspaced whole sentences), a real shipped
cue rendered with an ink bounding box of ``x[0..1079]`` on a 1080px frame —
clipped at BOTH edges.

Two facts this suite pins, each measured rather than assumed:

  * ``force_style`` cannot repair it. It only reaches ``[V4+ Styles]``, never
    ``PlayResX``/``PlayResY``. Measured: ``FontSize=10`` alone still produced
    ``x[0..1079]``, as did the filter's ``original_size`` option. Only a real
    ASS document with an explicit PlayRes header binds libass to the frame.
  * The old gate was blind. :func:`_font_burn_failed` detects "no glyph was
    drawn at all" and says nothing about "drawn, but off both edges" — which
    is why this reached a user. The pixel-diff safe-box verifier is the gate
    that closes it.

The real-render tests skip cleanly where ffmpeg / a CJK font / numpy+PIL are
unavailable; the geometry and wrap tests are pure and always run.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from lib import motion_video as mv
from lib.motion_video._subtitle import (MAX_LINES_PER_CUE, build_ass,
                                        safe_box, style_for_frame, wrap_line)

pytestmark = pytest.mark.unit

#: The exact cue that shipped clipped (job motion_a9b6b528279d42f6, cue 5).
SHIPPED_CUE = ('效果如何?只开编辑这一招,SWE-bench分数从35.8涨到44.4;'
               '整体吞吐是自回归模型的1.64倍。')


def _has_pixel_tools() -> bool:
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
#  geometry contract — derived from the frame, not hardcoded
# ══════════════════════════════════════════════════════════

def test_style_scales_with_frame():
    """Type size / margins are proportional to the frame, so a 1080x1920 or
    1920x1080 job gets proportionate subtitles instead of the 384x288 blow-up."""
    v = style_for_frame(1080, 1440)
    tall = style_for_frame(1080, 1920)
    wide = style_for_frame(1920, 1080)
    # font follows HEIGHT
    assert tall.font_px > v.font_px > wide.font_px
    # side margin follows WIDTH
    assert wide.margin_x > v.margin_x == tall.margin_x
    for st in (v, tall, wide):
        assert st.usable_px == st.width - 2 * st.margin_x
        assert st.usable_px > 0
        assert st.outline >= 2


def test_safe_box_agrees_with_style():
    """The verifier and the wrapper must read the SAME numbers — a safe box
    computed independently of the wrap budget is how they drift apart."""
    for w, h in ((1080, 1440), (1080, 1920), (1920, 1080), (1080, 1080)):
        left, right = safe_box(w, h)
        st = style_for_frame(w, h)
        assert left == st.margin_x
        assert right == w - st.margin_x
        assert right - left == st.usable_px


# ══════════════════════════════════════════════════════════
#  wrapping — the half libass will not do for CJK
# ══════════════════════════════════════════════════════════

def test_unspaced_cjk_cue_is_wrapped():
    """The shipped cue has ZERO spaces, so libass had no break opportunity
    and ran it off both edges. We must break it ourselves."""
    st = style_for_frame(1080, 1440)
    assert ' ' not in SHIPPED_CUE.replace('\u3000', '')
    lines = wrap_line(SHIPPED_CUE, st)
    assert len(lines) > 1, 'an unspaced 53-char cue must be split'
    # nothing may be lost or invented by wrapping
    assert ''.join(lines).replace(' ', '') == SHIPPED_CUE.replace(' ', '')


def test_every_wrapped_line_fits_the_budget():
    """Each produced line must measure within the usable width — this is the
    property the real-render guard then confirms in pixels."""
    from lib.motion_video._subtitle import measure_advance
    st = style_for_frame(1080, 1440)
    for cue in (SHIPPED_CUE,
                'AI写代码出错改不了怎么办?蚂蚁的新模型LLaDA2.2,让扩散语言模型学会了边生成边编辑。',
                'Hello world this is a fairly long English caption line for testing',
                '混合CJK与Latin: SWE-bench 44.4 分,throughput 1.64x,全部要能放下。'):
        for line in wrap_line(cue, st):
            adv = measure_advance(line, st)
            if adv is not None:
                assert adv <= st.usable_px, (line, adv, st.usable_px)


def test_latin_words_are_not_broken_midword():
    """Breaking inside a Latin word is a legibility defect; CJK may break
    anywhere (correct Chinese typesetting)."""
    st = style_for_frame(1080, 1440)
    lines = wrap_line('benchmark throughput autoregressive diffusion '
                      'transformer evaluation', st)
    assert len(lines) > 1
    for line in lines:
        for word in line.split():
            assert word in ('benchmark', 'throughput', 'autoregressive',
                            'diffusion', 'transformer', 'evaluation'), line


def test_single_token_longer_than_budget_still_breaks():
    """A pathological unbroken token (long URL) must still be split, or it
    becomes exactly the overflow we are fixing."""
    st = style_for_frame(1080, 1440)
    lines = wrap_line('x' * 400, st)
    assert len(lines) > 1
    from lib.motion_video._subtitle import measure_advance
    for line in lines:
        adv = measure_advance(line, st)
        if adv is not None:
            assert adv <= st.usable_px


def test_overlong_cue_is_reported_not_silently_stacked():
    """A cue needing more lines than the budget covers the composition it is
    supposed to caption — that is reported, not silently stacked."""
    _ass, warns = build_ass([(0.0, 3.0, '很长的中文句子' * 40)], 1080, 1440)
    assert warns, 'an over-long cue must produce a warning'
    assert str(MAX_LINES_PER_CUE) in warns[0]


# ══════════════════════════════════════════════════════════
#  the ASS document — the only mechanism that binds geometry
# ══════════════════════════════════════════════════════════

def test_ass_carries_playres_matching_the_frame():
    """PlayResX/Y is THE fix: without it libass assumes 384x288 and scales
    the default style by height/288 (a 5x blow-up at 1440px)."""
    ass, _w = build_ass([(0.0, 3.0, SHIPPED_CUE)], 1080, 1440)
    assert 'PlayResX: 1080' in ass
    assert 'PlayResY: 1440' in ass
    assert '[Script Info]' in ass and '[V4+ Styles]' in ass
    st = style_for_frame(1080, 1440)
    # margins + size come from the contract, not from libass defaults
    assert f',{st.margin_x},{st.margin_x},{st.margin_v},' in ass
    assert f'Default,{st.font_name},{st.font_px},' in ass


def test_ass_prewraps_with_hard_breaks():
    """We pre-wrap and set WrapStyle 2 so libass does not re-flow our lines."""
    ass, _w = build_ass([(0.0, 3.0, SHIPPED_CUE)], 1080, 1440)
    assert 'WrapStyle: 2' in ass
    dialogue = [ln for ln in ass.splitlines() if ln.startswith('Dialogue:')]
    assert len(dialogue) == 1
    assert '\\N' in dialogue[0], 'the cue must carry explicit line breaks'


def test_ass_timestamps_are_centiseconds():
    ass, _w = build_ass([(65.5, 68.25, '测试')], 1080, 1440)
    assert '0:01:05.50' in ass
    assert '0:01:08.25' in ass


# ══════════════════════════════════════════════════════════
#  REAL render: ink must land inside the safe box
# ══════════════════════════════════════════════════════════

def _black_clip(tmp_path, ffmpeg, w=1080, h=1440, dur=2):
    from lib.motion_video._env import build_render_env
    video = tmp_path / f'black_{w}x{h}.mp4'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    f'color=c=black:s={w}x{h}:r=30:d={dur}',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
                   check=True, capture_output=True, timeout=180,
                   env=build_render_env())
    return video


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
@pytest.mark.skipif(not _has_pixel_tools(), reason='numpy+PIL unavailable')
def test_shipped_cue_burns_inside_safe_box(tmp_path):
    """THE regression: burn the exact cue that shipped clipped and assert the
    ink now lands inside the safe box. Pre-fix this measured x[0..1079]."""
    import numpy as np
    from PIL import Image
    from lib.motion_video._env import build_render_env

    ffmpeg = mv.ffmpeg_bin()
    video = _black_clip(tmp_path, ffmpeg)
    srt = tmp_path / 'c.srt'
    srt.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{SHIPPED_CUE}\n',
                   encoding='utf-8')
    out = tmp_path / 'burned.mp4'
    res = mv.burn_in_subtitles(str(video), str(srt), str(out))
    if res.get('category') == 'font_missing':
        pytest.skip('no CJK-capable font on this box')
    assert res['ok'] is True, res
    assert res.get('safe_box_checked', 0) >= 1, (
        'the safe-box verifier must actually have run on a real burn')

    png = tmp_path / 'f.png'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-ss', '1.0', '-i', str(out),
                    '-frames:v', '1', str(png)], check=True,
                   capture_output=True, timeout=120, env=build_render_env())
    a = np.asarray(Image.open(png).convert('L'))
    ys, xs = np.where(a > 40)
    assert len(xs), 'subtitles rendered nothing'
    left, right = safe_box(1080, 1440)
    assert int(xs.min()) >= left, f'ink starts at {xs.min()}, safe left {left}'
    assert int(xs.max()) <= right, f'ink ends at {xs.max()}, safe right {right}'


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
@pytest.mark.skipif(not _has_pixel_tools(), reason='numpy+PIL unavailable')
def test_safe_box_verifier_rejects_raw_srt_burn_NEUTER(tmp_path):
    """NEUTER: burn the RAW SRT exactly the way the pipeline used to, and the
    verifier must REJECT it as subtitle_overflow.

    This is the guard's teeth. If this test ever passes the raw burn, the
    verifier has stopped detecting the defect it exists for.
    """
    import lib.motion_video._concat as MC
    from lib.motion_video._env import build_render_env

    ffmpeg = mv.ffmpeg_bin()
    env = build_render_env()
    video = _black_clip(tmp_path, ffmpeg)
    srt = tmp_path / 'c.srt'
    srt.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{SHIPPED_CUE}\n',
                   encoding='utf-8')
    raw = tmp_path / 'rawsrt.mp4'
    filt = f"subtitles='{MC._escape_filter_path(str(srt))}'"
    proc = subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-i', str(video), '-vf', filt,
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'ultrafast',
         '-an', str(raw)], capture_output=True, text=True, timeout=300,
        env=env)
    assert proc.returncode == 0, proc.stderr[-500:]
    if MC._font_burn_failed(proc.stderr):
        pytest.skip('no CJK-capable font on this box')

    verdict = MC._verify_safe_box(
        str(video), str(raw), [(0.0, 2.0, SHIPPED_CUE)], 1080, 1440,
        ffmpeg=ffmpeg, env=env, workdir=str(tmp_path))
    assert verdict['ok'] is False, (
        'the pre-fix raw-SRT burn must be rejected — this guard is what '
        'stops the clipped subtitle from shipping again')
    assert verdict['category'] == 'subtitle_overflow', verdict


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
@pytest.mark.skipif(not _has_pixel_tools(), reason='numpy+PIL unavailable')
def test_verifier_detects_a_no_op_burn(tmp_path):
    """A burn that changed NO pixels is reported as subtitle_missing rather
    than passing — the complement of the overflow case."""
    import lib.motion_video._concat as MC
    from lib.motion_video._env import build_render_env

    ffmpeg = mv.ffmpeg_bin()
    video = _black_clip(tmp_path, ffmpeg)
    verdict = MC._verify_safe_box(
        str(video), str(video),          # identical → nothing changed
        [(0.0, 2.0, SHIPPED_CUE)], 1080, 1440,
        ffmpeg=ffmpeg, env=build_render_env(), workdir=str(tmp_path))
    assert verdict['ok'] is False
    assert verdict['category'] == 'subtitle_missing', verdict


@pytest.mark.skipif(not mv.ffmpeg_bin(), reason='ffmpeg unavailable')
@pytest.mark.skipif(not _has_pixel_tools(), reason='numpy+PIL unavailable')
@pytest.mark.parametrize('w,h', [(1080, 1920), (1920, 1080)])
def test_other_frame_sizes_also_stay_inside(tmp_path, w, h):
    """Geometry is derived, so non-default frames must hold too — a hardcoded
    1080x1440 constant would pass the test above and fail here."""
    import numpy as np
    from PIL import Image
    from lib.motion_video._env import build_render_env

    ffmpeg = mv.ffmpeg_bin()
    video = _black_clip(tmp_path, ffmpeg, w=w, h=h)
    srt = tmp_path / f'c{w}x{h}.srt'
    srt.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{SHIPPED_CUE}\n',
                   encoding='utf-8')
    out = tmp_path / f'b{w}x{h}.mp4'
    res = mv.burn_in_subtitles(str(video), str(srt), str(out))
    if res.get('category') == 'font_missing':
        pytest.skip('no CJK-capable font on this box')
    assert res['ok'] is True, res
    png = tmp_path / f'f{w}x{h}.png'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-ss', '1.0', '-i', str(out),
                    '-frames:v', '1', str(png)], check=True,
                   capture_output=True, timeout=120, env=build_render_env())
    a = np.asarray(Image.open(png).convert('L'))
    ys, xs = np.where(a > 40)
    assert len(xs), 'subtitles rendered nothing'
    left, right = safe_box(w, h)
    assert int(xs.min()) >= left
    assert int(xs.max()) <= right


# ══════════════════════════════════════════════════════════
#  wiring: the pipeline must not be able to skip the contract
# ══════════════════════════════════════════════════════════

def test_burn_stages_an_ass_document(monkeypatch, tmp_path):
    """burn_in_subtitles must hand libass an .ass file, never the bare SRT —
    the SRT stays the user-facing sidecar."""
    import lib.motion_video._concat as MC

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'mp4')
    srt = tmp_path / 'final.srt'
    srt.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{SHIPPED_CUE}\n',
                   encoding='utf-8')
    seen = {}

    def fake_run(args, **kw):
        seen['args'] = list(args)
        with open(args[-1], 'wb') as f:
            f.write(b'mp4')
        return {'rc': 0, 'category': '', 'elapsed': 0.1, 'err': ''}

    # Pure wiring test: _run_ffmpeg is faked, so any non-empty ffmpeg path
    # satisfies burn_in_subtitles' env gate — no real binary needed (public
    # CI has none).
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin',
                        lambda: '/fake/ffmpeg')
    monkeypatch.setattr(MC, '_run_ffmpeg', fake_run)
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda p, **kw: {'width': 1080, 'height': 1440,
                                         'duration': 2.0, 'has_audio': False})
    res = mv.burn_in_subtitles(str(video), str(srt), str(tmp_path / 'o.mp4'),
                               verify_safe_box=False)
    assert res['ok'] is True, res
    joined = ' '.join(seen['args'])
    assert '.ass' in joined, 'the burn must consume a geometry-bound ASS doc'
    assert 'final.srt' not in joined, 'the raw SRT must not reach libass'


def test_geometry_comes_from_the_video_not_the_caller(monkeypatch, tmp_path):
    """Frame size is read from the probed video, so a new call site cannot
    forget to pass it and silently reinstate the 384x288 default."""
    import lib.motion_video._concat as MC

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'mp4')
    srt = tmp_path / 'final.srt'
    srt.write_text(f'1\n00:00:00,000 --> 00:00:02,000\n{SHIPPED_CUE}\n',
                   encoding='utf-8')
    captured = {}

    def fake_run(args, **kw):
        for a in args:
            if a.endswith('.ass') or '.ass' in a:
                for tok in a.split("'"):
                    if tok.endswith('.ass') and os.path.isfile(tok):
                        captured['ass'] = open(tok, encoding='utf-8').read()
        with open(args[-1], 'wb') as f:
            f.write(b'mp4')
        return {'rc': 0, 'category': '', 'elapsed': 0.1, 'err': ''}

    # Same as above: fake runner ⇒ a stand-in ffmpeg path is enough.
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin',
                        lambda: '/fake/ffmpeg')
    monkeypatch.setattr(MC, '_run_ffmpeg', fake_run)
    # A DELIBERATELY unusual frame — if geometry were hardcoded this stays 1080
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda p, **kw: {'width': 720, 'height': 1280,
                                         'duration': 2.0, 'has_audio': False})
    mv.burn_in_subtitles(str(video), str(srt), str(tmp_path / 'o.mp4'),
                         verify_safe_box=False)
    assert 'ass' in captured, 'no ASS document was staged'
    assert 'PlayResX: 720' in captured['ass']
    assert 'PlayResY: 1280' in captured['ass']
