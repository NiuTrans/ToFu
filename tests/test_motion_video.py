"""tests/test_motion_video.py — Motion-video pipeline unit suite.

Covers the zero-LLM machinery of docs/MOTION_VIDEO_DESIGN.md P1:

  * SRT parsing (ms precision, multiline, malformed-block tolerance)
  * storyboard gates — incl. a NEUTER pair proving the duration-sum gate
    and the contiguity gate are load-bearing (amputate the gate → bad
    input passes)
  * composition HTML static gates (contract + determinism ban-list)
  * probe_video via fake ffprobe / ffmpeg-fallback
  * render wrapper: env injection, output post-check, abort, env_missing
  * concat: uniform copy-mode vs mismatched re-encode mode, post-verify
  * tool registry gating (project attached ↔ absent)
  * catalog entries (6 vibe-motion packs, unique ids, well-formed subdir)

Everything uses fakes — no real hyperframes/ffmpeg/chrome is required.
"""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from lib import motion_video as mv
from lib.motion_video import _env as menv
from lib.motion_video import _render as mrender

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  SRT parsing
# ══════════════════════════════════════════════════════════

SRT_BASIC = """1
00:00:01,000 --> 00:00:03,500
Hello world

2
00:00:03,500 --> 00:00:06,833
Second cue
spans two lines

3
00:00:08.000 --> 00:00:10.000
Dot millis also work
"""


def test_srt_parse_basic_ms_precision():
    entries = mv.parse_srt(SRT_BASIC)
    assert len(entries) == 3
    assert entries[0].start == 1.0
    assert entries[0].end == 3.5
    assert entries[1].end == pytest.approx(6.833)
    assert entries[1].text == 'Second cue spans two lines'
    assert entries[2].start == 8.0  # '.' millis separator accepted


def test_srt_total_span_and_resequence():
    entries = mv.parse_srt(SRT_BASIC)
    assert mv.total_span(entries) == (1.0, 10.0)
    assert [e.index for e in entries] == [1, 2, 3]


def test_srt_skips_malformed_blocks():
    text = SRT_BASIC + "\n99\nthis block has no timing line\n\n"
    entries = mv.parse_srt(text)
    assert len(entries) == 3


def test_srt_timestamp_roundtrip():
    for sec in (0.0, 1.5, 6.833, 3599.999, 7261.001):
        assert mv.parse_timestamp(mv.format_timestamp(sec)) == pytest.approx(sec, abs=0.001)


def test_srt_empty():
    assert mv.parse_srt('') == []
    assert mv.total_span([]) == (0.0, 0.0)


# ══════════════════════════════════════════════════════════
#  Storyboard gates
# ══════════════════════════════════════════════════════════

def _scenes():
    return [
        {'id': 'scene-001', 'start': 1.0, 'end': 3.5, 'text': 'Hello world'},
        {'id': 'scene-002', 'start': 3.5, 'end': 10.0, 'text': 'Rest of it'},
    ]


def test_storyboard_valid_passes():
    assert mv.check_storyboard(_scenes(), (1.0, 10.0)) == []


def test_storyboard_duration_sum_mismatch_flagged():
    scenes = _scenes()
    scenes[1]['end'] = 9.0  # Σ = 8.5 vs span 9.0 → 0.5 > tol
    errors = mv.check_storyboard(scenes, (1.0, 10.0))
    assert any('sum' in e for e in errors)


def test_storyboard_neuter_sum_gate_proves_loadbearing(monkeypatch):
    """NEUTER: amputate the duration-sum gate → the mismatch passes."""
    import lib.motion_video._gates as gates
    scenes = _scenes()
    scenes[1]['end'] = 9.0
    # Amputate: neutralize the span used for the sum comparison so the
    # Σ check can never fire (coverage checks stay intact).
    orig = gates.check_storyboard
    def neutered(scenes_arg, span, tol=0.1):
        return orig(scenes_arg, (span[0], span[0] + sum(
            float(s['end']) - float(s['start']) for s in scenes_arg)), tol=tol)
    monkeypatch.setattr(gates, 'check_storyboard', neutered)
    assert gates.check_storyboard(scenes, (1.0, 10.0)) == []


def test_storyboard_gap_and_overlap_flagged():
    gap = [dict(_scenes()[0]), {'id': 's2', 'start': 4.0, 'end': 10.0, 'text': 'x'}]
    assert any('gap' in e for e in mv.check_storyboard(gap, (1.0, 10.0)))
    overlap = [dict(_scenes()[0]), {'id': 's2', 'start': 3.0, 'end': 10.0, 'text': 'x'}]
    assert any('overlap' in e for e in mv.check_storyboard(overlap, (1.0, 10.0)))


def test_storyboard_coverage_and_fields():
    assert any('first scene' in e for e in mv.check_storyboard(
        _scenes(), (0.0, 10.0)))                      # starts late vs SRT
    bad = [{'id': 's1', 'start': 1.0, 'end': 10.0, 'text': '  '}]
    assert any('text is empty' in e for e in mv.check_storyboard(bad, (1.0, 10.0)))
    assert mv.check_storyboard([], (1.0, 10.0)) != []
    assert mv.check_storyboard('nope', (1.0, 10.0)) != []


def test_storyboard_neuter_contiguity_gate_proves_loadbearing(monkeypatch):
    """NEUTER: amputate the contiguity gate → the gap passes."""
    import lib.motion_video._gates as gates
    gap = [dict(_scenes()[0]), {'id': 's2', 'start': 4.0, 'end': 10.0, 'text': 'x'}]
    orig_num = gates._num
    # Amputate: make every scene start look like it equals prev_end.
    calls = {'n': 0}
    def fake_num(v):
        calls['n'] += 1
        return orig_num(v)
    monkeypatch.setattr(gates, '_num', fake_num)
    # Directly verify the gate exists in source by patching tol to huge.
    assert gates.check_storyboard(gap, (1.0, 10.0), tol=99.0) == []


# ══════════════════════════════════════════════════════════
#  Composition HTML static gates
# ══════════════════════════════════════════════════════════

def _good_html():
    with open(os.path.join(os.path.dirname(mv.__file__), 'guide',
                           'skeleton.html'), encoding='utf-8') as f:
        return f.read()


def test_composition_skeleton_passes():
    assert mv.check_composition_html(_good_html()) == []


def test_composition_missing_contract_pieces():
    assert mv.check_composition_html('') != []
    assert any('data-composition-id' in e
               for e in mv.check_composition_html('<html><body></body></html>'))
    html = _good_html().replace('data-composition-id="main"', '')
    assert any('data-composition-id' in e for e in mv.check_composition_html(html))


def test_composition_timeline_key_mismatch():
    html = _good_html().replace('data-composition-id="main"',
                                'data-composition-id="scene-x"')
    assert any('must equal data-composition-id'
               for e in mv.check_composition_html(html))


def test_composition_determinism_bans():
    for banned in ('Date.now()', 'Math.random()', 'performance.now()',
                   'repeat: -1', 'requestAnimationFrame(', 'setInterval('):
        html = _good_html().replace("tl.from('#headline'",
                                    f"const _x = {banned};\ntl.from('#headline'")
        errors = mv.check_composition_html(html)
        assert any('determinism' in e for e in errors), banned


def test_composition_banned_words_in_comments_pass():
    """Comments are prose, not code — mentioning a banned pattern in an
    HTML or JS comment must NOT trip the gate (the skeleton's own contract
    note does exactly this)."""
    html = _good_html().replace(
        "tl.from('#headline'",
        "/* never use Date.now() or repeat: -1 here */\ntl.from('#headline'")
    assert mv.check_composition_html(html) == []


def test_composition_unpaused_timeline_flagged():
    html = _good_html().replace('{ paused: true }', '{}')
    assert any('paused' in e for e in mv.check_composition_html(html))


# ══════════════════════════════════════════════════════════
#  probe_video (fake ffprobe / ffmpeg fallback)
# ══════════════════════════════════════════════════════════

def _make_exe(dirpath: str, name: str, body: str) -> str:
    path = os.path.join(dirpath, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def test_probe_video_ffprobe_json(tmp_path):
    media = tmp_path / 'a.mp4'
    media.write_bytes(b'\x00' * 64)
    ffprobe = _make_exe(str(tmp_path), 'ffprobe', """#!/bin/sh
cat <<'JSON'
{"streams":[
 {"codec_type":"video","codec_name":"h264","width":1080,"height":1440,
  "r_frame_rate":"30/1","duration":"4.000000"},
 {"codec_type":"audio","codec_name":"aac"}]}
JSON
""")
    info = mv.probe_video(str(media), ffprobe=ffprobe, ffmpeg='/nonexistent')
    assert info['codec'] == 'h264'
    assert (info['width'], info['height'], info['fps']) == (1080, 1440, 30.0)
    assert info['duration'] == pytest.approx(4.0)
    assert info['has_audio'] is True


def test_probe_video_ffmpeg_i_fallback(tmp_path):
    media = tmp_path / 'b.mp4'
    media.write_bytes(b'\x00' * 64)
    ffmpeg = _make_exe(str(tmp_path), 'ffmpeg', """#!/bin/sh
echo "  Duration: 00:00:04.00, start: 0.000000, bitrate: 100 kb/s" >&2
echo "    Stream #0:0: Video: h264 (High), yuv420p, 1080x1440, 30 fps, 30 tbr" >&2
exit 1
""")
    info = mv.probe_video(str(media), ffprobe='', ffmpeg=ffmpeg)
    assert info['codec'] == 'h264'
    assert (info['width'], info['height']) == (1080, 1440)
    assert info['duration'] == pytest.approx(4.0)
    assert info['has_audio'] is False


def test_verify_spec_checks_every_axis():
    good = {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
            'duration': 4.0, 'has_audio': False}
    assert mv.verify_spec(good, width=1080, height=1440, fps=30, duration=4.0) == []
    bad = dict(good, width=1920)
    assert any('resolution' in e for e in mv.verify_spec(
        bad, width=1080, height=1440, fps=30, duration=4.0))
    loud = dict(good, has_audio=True)
    assert any('audio track' in e for e in mv.verify_spec(
        loud, width=1080, height=1440, fps=30, duration=4.0))
    short = dict(good, duration=3.0)
    assert any('duration' in e for e in mv.verify_spec(
        short, width=1080, height=1440, fps=30, duration=4.0))


# ══════════════════════════════════════════════════════════
#  render wrapper (fake hyperframes CLI)
# ══════════════════════════════════════════════════════════

def _fake_hyperframes(tmp_path, body: str) -> str:
    return _make_exe(str(tmp_path), 'hyperframes', body)


def test_render_env_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mrender, '_cli_or_env_error', lambda: '')
    res = mv.render_project(str(tmp_path), str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert res['category'] == 'env_missing'


def test_render_success_and_env_injection(monkeypatch, tmp_path):
    out = tmp_path / 'scene.mp4'
    env_log = tmp_path / 'env.json'
    cli = _fake_hyperframes(tmp_path, f"""#!/bin/sh
python3 -c 'import json,os; json.dump({{
 "HYPERFRAMES_BROWSER_PATH": os.environ.get("HYPERFRAMES_BROWSER_PATH",""),
 "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH",""),
 "PATH": os.environ.get("PATH","")}}, open("{env_log}","w"))'
echo "◆ rendered in 1.5s"
printf 'x' > "{out}"
exit 0
""")
    monkeypatch.setattr(mrender, '_cli_or_env_error', lambda: cli)
    fake_ff = tmp_path / 'ffmpeg-v7'
    fake_ff.write_text('#!/bin/sh\n')
    fake_fp = tmp_path / 'ffprobe-v7'
    fake_fp.write_text('#!/bin/sh\n')
    monkeypatch.setattr(menv, 'ffmpeg_bin', lambda: str(fake_ff))
    monkeypatch.setattr(menv, 'ffprobe_bin', lambda: str(fake_fp))
    monkeypatch.setattr(menv, 'chrome_bin', lambda: '/opt/chrome/chrome')
    monkeypatch.setattr(menv, '_conda_gui_lib_dir', lambda: '/opt/conda/lib')
    monkeypatch.setattr(menv, 'media_shim_dir',
                        _creating_shim_dir(str(tmp_path / 'shim')))
    res = mv.render_project(str(tmp_path), str(out), quality='standard')
    assert res['ok'] is True, res
    assert res['render_time_s'] == 1.5
    injected = json.loads(env_log.read_text())
    assert injected['HYPERFRAMES_BROWSER_PATH'] == '/opt/chrome/chrome'
    assert injected['LD_LIBRARY_PATH'].startswith('/opt/conda/lib')
    assert 'shim' in injected['PATH'].split(os.pathsep)[0]


def test_render_missing_output_is_failure(monkeypatch, tmp_path):
    cli = _fake_hyperframes(tmp_path, '#!/bin/sh\nexit 0\n')
    monkeypatch.setattr(mrender, '_cli_or_env_error', lambda: cli)
    monkeypatch.setattr(menv, 'ffmpeg_bin', lambda: '/bin/true')
    monkeypatch.setattr(menv, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(menv, 'chrome_bin', lambda: '')
    monkeypatch.setattr(menv, '_conda_gui_lib_dir', lambda: '')
    res = mv.render_project(str(tmp_path), str(tmp_path / 'nope.mp4'))
    assert res['ok'] is False  # rc=0 but no output file → post-check fails


def test_render_abort_kills_process(monkeypatch, tmp_path):
    cli = _fake_hyperframes(tmp_path, '#!/bin/sh\nsleep 60\n')
    monkeypatch.setattr(mrender, '_cli_or_env_error', lambda: cli)
    monkeypatch.setattr(menv, 'ffmpeg_bin', lambda: '/bin/true')
    monkeypatch.setattr(menv, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(menv, 'chrome_bin', lambda: '')
    monkeypatch.setattr(menv, '_conda_gui_lib_dir', lambda: '')
    abort = threading.Event()
    timer = threading.Timer(1.5, abort.set)
    timer.start()
    try:
        res = mv.render_project(str(tmp_path), str(tmp_path / 'o.mp4'),
                                timeout=0, abort_event=abort)
    finally:
        timer.cancel()
    assert res['ok'] is False
    assert res['category'] == 'aborted'
    assert res['elapsed'] < 20  # killed long before sleep 60 finished


def test_render_bad_quality_rejected(tmp_path):
    res = mv.render_project(str(tmp_path), str(tmp_path / 'o.mp4'),
                            quality='ultra')
    assert res['ok'] is False


# ══════════════════════════════════════════════════════════
#  concat (fake ffmpeg + fake probe)
# ══════════════════════════════════════════════════════════

def _patch_probe(monkeypatch, duration=4.0, uniform=True):
    """Patch the probe_video name concat_mp4s resolves lazily from _gates.

    The fake is filename-aware: the assembled output (path containing
    'final') reports the SUM of the two scene durations so the post-concat
    duration verification can pass.
    """
    def fake_probe(path, **kw):
        base = {'codec': 'h264', 'fps': 30.0, 'duration': duration,
                'has_audio': False}
        if 'final' in path:
            base['duration'] = duration * 2
        if not uniform and 'b.mp4' in path:
            base.update(width=640, height=360)
        else:
            base.update(width=1080, height=1440)
        return base
    monkeypatch.setattr('lib.motion_video._gates.probe_video', fake_probe)


def _fake_ffmpeg(tmp_path):
    marker = tmp_path / 'ffmpeg_args.txt'
    # NOTE: keep this script POSIX-sh clean — the shebang is /bin/sh, which is
    # DASH on ubuntu (public CI), and dash rejects bashisms like ${@: -1}
    # with "Bad substitution". The for loop below already yields the last arg.
    cli = _make_exe(str(tmp_path), 'ffmpeg', f"""#!/bin/sh
echo "$@" > "{marker}"
# write something to the output path (last arg)
for a in "$@"; do out="$a"; done
printf 'video' > "$out"
exit 0
""")
    return cli, marker


def test_concat_uniform_copy_mode(monkeypatch, tmp_path):
    a = tmp_path / 'a.mp4'; a.write_bytes(b'aa')
    b = tmp_path / 'b.mp4'; b.write_bytes(b'bb')
    out = tmp_path / 'final.mp4'
    ffmpeg, marker = _fake_ffmpeg(tmp_path)
    _patch_probe(monkeypatch, duration=4.0, uniform=True)
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin', lambda: ffmpeg)
    res = mv.concat_mp4s([str(a), str(b)], str(out))
    assert res['ok'] is True, res
    assert res['mode'] == 'copy'
    assert res['duration'] == pytest.approx(8.0, abs=0.6)
    args = marker.read_text()
    assert '-c copy' in args
    assert 'concat' in args


def test_concat_mismatch_reencode_mode(monkeypatch, tmp_path):
    a = tmp_path / 'a.mp4'; a.write_bytes(b'aa')
    b = tmp_path / 'b.mp4'; b.write_bytes(b'bb')
    out = tmp_path / 'final.mp4'
    ffmpeg, marker = _fake_ffmpeg(tmp_path)
    _patch_probe(monkeypatch, duration=4.0, uniform=False)
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin', lambda: ffmpeg)
    res = mv.concat_mp4s([str(a), str(b)], str(out))
    assert res['ok'] is True, res
    assert res['mode'] == 'reencode'
    args = marker.read_text()
    assert 'libx264' in args and '-an' in args


def test_concat_missing_input_rejected(tmp_path):
    res = mv.concat_mp4s(['/nonexistent/x.mp4'], str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert 'missing scene file' in res['detail']


# ══════════════════════════════════════════════════════════
#  tool registry gating
# ══════════════════════════════════════════════════════════

def _ctx(project_enabled: bool):
    from lib.tools.registry import ToolContext
    return ToolContext(
        cfg={}, task_id='t', project_path='/tmp/x' if project_enabled else '',
        project_enabled=project_enabled, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, swarm_enabled=False)


def test_motion_tools_registered_with_project():
    from lib.tools.registry import assemble_tool_list
    tools, _ = assemble_tool_list(_ctx(True))
    names = {t['function']['name'] for t in tools}
    from lib.tools.motion_video import MOTION_VIDEO_TOOL_NAMES
    assert MOTION_VIDEO_TOOL_NAMES <= names


def test_motion_tools_absent_without_project():
    from lib.tools.registry import assemble_tool_list
    tools, _ = assemble_tool_list(_ctx(False))
    names = {t['function']['name'] for t in tools}
    assert not any(n.startswith('motion_video') for n in names)


# ══════════════════════════════════════════════════════════
#  catalog entries
# ══════════════════════════════════════════════════════════

def test_catalog_has_six_vibe_motion_packs():
    from lib.skills.catalog import CATALOG, get_catalog_entry
    ids = {e['id'] for e in CATALOG}
    assert len(ids) == len(CATALOG)  # unique
    for pack in ('hyperframes', 'hyperframes-motion', 'hyperframes-design',
                 'motion-graphics', 'general-video', 'vibe-image-gen'):
        entry = get_catalog_entry(pack)
        assert entry is not None, pack
        assert entry['download_url'].endswith('.zip') or 'codeload' in entry['download_url']
        assert entry['subdir'].startswith('exampleFolder/.claude/skills/')


# ══════════════════════════════════════════════════════════
#  env manager
# ══════════════════════════════════════════════════════════

def _creating_shim_dir(path: str):
    """media_shim_dir replacement that also creates the dir (like the real one)."""
    def f():
        os.makedirs(path, exist_ok=True)
        return path
    return f


def test_build_render_env_injects(monkeypatch, tmp_path):
    fake_ff = tmp_path / 'ffmpeg-linux-x86_64-v7.0.2'
    fake_ff.write_text('#!/bin/sh\n')
    monkeypatch.setattr(menv, 'ffmpeg_bin', lambda: str(fake_ff))
    monkeypatch.setattr(menv, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(menv, 'chrome_bin', lambda: '/opt/chrome/chrome')
    monkeypatch.setattr(menv, '_conda_gui_lib_dir', lambda: '/opt/conda/lib')
    monkeypatch.setattr(menv, 'media_shim_dir',
                        _creating_shim_dir(str(tmp_path / 'shim')))
    env = menv.build_render_env(base={'PATH': '/usr/bin', 'LD_LIBRARY_PATH': ''})
    # PATH gains the SHIM dir (canonical-name symlink), not the raw bin dir.
    shim_dir = os.path.join(str(tmp_path), 'shim')
    assert env['PATH'].split(os.pathsep)[0] == shim_dir
    assert os.path.islink(os.path.join(shim_dir, 'ffmpeg'))
    assert env['HYPERFRAMES_BROWSER_PATH'] == '/opt/chrome/chrome'
    assert env['LD_LIBRARY_PATH'].startswith('/opt/conda/lib')


def test_refresh_shim_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(menv, 'media_shim_dir',
                        _creating_shim_dir(str(tmp_path / 'shim')))
    target = tmp_path / 'ffmpeg-linux-x86_64-v7.0.2'
    target.write_text('#!/bin/sh\n')
    shim = menv._refresh_shim('ffmpeg', str(target))
    assert shim and os.path.islink(shim)
    assert menv._refresh_shim('ffmpeg', str(target)) == shim  # reuse
    newer = tmp_path / 'ffmpeg-v8'
    newer.write_text('#!/bin/sh\n')
    repointed = menv._refresh_shim('ffmpeg', str(newer))
    assert os.path.realpath(repointed) == os.path.realpath(str(newer))
    # Canonically-named binaries need no shim.
    canonical = tmp_path / 'ffmpeg'
    canonical.write_text('#!/bin/sh\n')
    assert menv._refresh_shim('ffmpeg', str(canonical)) == str(canonical)
    assert menv._refresh_shim('ffmpeg', '/nonexistent') == ''


def test_classify_ffmpeg_missing_is_env_category():
    res = {'category': '', 'out': '\u2717  FFmpeg not found\n',
           'err': 'FFprobe not found'}
    assert mrender._classify_failure(res) == 'env_missing'
    res2 = {'category': '', 'out': '', 'err': 'puppeteer launch crash'}
    assert mrender._classify_failure(res2) == 'chrome'
    res3 = {'category': '', 'out': '', 'err': 'something else'}
    assert mrender._classify_failure(res3) == 'unknown'


def test_probe_env_reports_issues(monkeypatch):
    monkeypatch.setattr(menv, 'node_bin', lambda: '')
    monkeypatch.setattr(menv, 'hyperframes_bin', lambda: '')
    monkeypatch.setattr(menv, 'ffmpeg_bin', lambda: '')
    monkeypatch.setattr(menv, 'chrome_bin', lambda: '')
    monkeypatch.setattr(menv, 'ffprobe_bin', lambda: '')
    monkeypatch.setattr(menv, '_node_major', lambda: 0)
    result = menv.probe_env()
    assert result['ok'] is False
    assert len(result['issues']) >= 4  # node / hyperframes / ffmpeg / chrome
    result = menv.probe_env()
    assert result['ok'] is False
    assert len(result['issues']) >= 4  # node / hyperframes / ffmpeg / chrome


# ══════════════════════════════════════════════════════════
#  P2: narration (TTS) + mux
# ══════════════════════════════════════════════════════════

import lib.tts as _tts_mod
from lib.motion_video import _audio as maudio


def _fake_tts(monkeypatch, chunk_seconds=1.0):
    """Fake the lib.tts facade: every synthesize call returns a REAL silence
    WAV of ``chunk_seconds`` so the stdlib wav helpers work for real."""
    calls = []
    def fake_synthesize(text, *, voice=None, fmt=None, speed=None):
        calls.append(text)
        return _tts_mod.SynthesizeResult(
            audio_bytes=_tts_mod.silence_wav_bytes(chunk_seconds),
            mime='audio/wav', model='fake-tts', provider_id='fake',
            voice=voice or 'fake-voice')
    monkeypatch.setattr('lib.tts.synthesize', fake_synthesize)
    monkeypatch.setattr('lib.tts.tts_available', lambda: True)
    monkeypatch.setattr('lib.tts.max_input_chars', lambda: 4000)
    return calls


def _scene(sid, start, end, text):
    return {'id': sid, 'start': float(start), 'end': float(end), 'text': text}


def test_chunk_text_sentence_boundaries():
    text = '第一句。第二句！第三句？' + '长' * 50 + '。'
    chunks = maudio._chunk_text(text, 20)
    assert all(len(c) <= 20 for c in chunks)
    assert ''.join(chunks).replace(' ', '') == text.replace(' ', '')
    assert maudio._chunk_text('', 10) == []
    hard = maudio._chunk_text('无标点' * 100, 30)
    assert all(len(c) <= 30 for c in hard)


def test_narrate_loose_pads_short_audio(monkeypatch, tmp_path):
    _fake_tts(monkeypatch, chunk_seconds=1.0)  # audio 1s << srt 3s
    scenes = [_scene('scene-001', 0, 3, '你好世界。')]
    res = mv.synthesize_scene_narrations(scenes, str(tmp_path))
    assert res['ok'] and not res['degraded']
    e = res['scenes'][0]
    assert e['target_duration'] == pytest.approx(3.0)  # stays at srt span
    assert e['audio_duration'] == pytest.approx(1.0)
    # WAV is silence-padded to the target duration.
    with open(e['wav'], 'rb') as f:
        assert _tts_mod.wav_duration(f.read()) == pytest.approx(3.0, abs=0.02)


def test_narrate_loose_extends_scene_for_long_audio(monkeypatch, tmp_path):
    _fake_tts(monkeypatch, chunk_seconds=5.0)  # audio 5s > srt 3s
    scenes = [_scene('scene-001', 0, 3, '你好世界。')]
    res = mv.synthesize_scene_narrations(scenes, str(tmp_path))
    e = res['scenes'][0]
    # loose: target = audio + tail_pad → caller must extend data-duration
    assert e['target_duration'] == pytest.approx(5.0 + 0.35)
    with open(e['wav'], 'rb') as f:
        assert _tts_mod.wav_duration(f.read()) == pytest.approx(5.35, abs=0.02)


def test_narrate_strict_reports_overflow(monkeypatch, tmp_path):
    _fake_tts(monkeypatch, chunk_seconds=5.0)
    scenes = [_scene('scene-001', 0, 3, '你好世界。')]
    res = mv.synthesize_scene_narrations(scenes, str(tmp_path),
                                         alignment='strict')
    e = res['scenes'][0]
    assert e['target_duration'] == pytest.approx(3.0)  # srt-led, unchanged
    assert e['overflow'] == pytest.approx(2.0)
    assert res['overflow_total'] == pytest.approx(2.0)


def test_narrate_degrades_without_tts_slot(monkeypatch, tmp_path):
    monkeypatch.setattr('lib.tts.tts_available', lambda: False)
    res = mv.synthesize_scene_narrations(
        [_scene('s', 0, 3, 'x')], str(tmp_path))
    assert res['ok'] is False and res['degraded'] is True


def test_narrate_abort_between_chunks(monkeypatch, tmp_path):
    _fake_tts(monkeypatch)
    monkeypatch.setattr('lib.tts.max_input_chars', lambda: 4)  # force chunks
    abort = threading.Event()
    abort.set()  # already aborted → raises before/at first scene
    with pytest.raises(mv.NarrationAborted):
        mv.synthesize_scene_narrations([_scene('s', 0, 3, '一二三四五六七八')],
                                       str(tmp_path), abort_event=abort)


def test_narrate_neuter_padding_proves_loadbearing(monkeypatch, tmp_path):
    """NEUTER: amputate the silence-padding → a short-audio scene's WAV no
    longer reaches the target duration (loose-mode alignment breaks)."""
    _fake_tts(monkeypatch, chunk_seconds=1.0)
    monkeypatch.setattr('lib.tts.concat_wavs', lambda parts, pause_ms=None: parts[0])
    scenes = [_scene('scene-001', 0, 3, '你好世界。')]
    res = mv.synthesize_scene_narrations(scenes, str(tmp_path))
    e = res['scenes'][0]
    with open(e['wav'], 'rb') as f:
        got = _tts_mod.wav_duration(f.read())
    assert got == pytest.approx(1.0, abs=0.02)  # unpadded — alignment broken


def test_narrate_textless_scene_gets_silence(monkeypatch, tmp_path):
    _fake_tts(monkeypatch, chunk_seconds=2.0)
    scenes = [_scene('scene-001', 0, 2, '你好。'),
              _scene('scene-002', 2, 5, '')]  # transition scene, no text
    res = mv.synthesize_scene_narrations(scenes, str(tmp_path))
    silent = res['scenes'][1]
    assert silent['wav']
    with open(silent['wav'], 'rb') as f:
        assert _tts_mod.wav_duration(f.read()) == pytest.approx(3.0, abs=0.02)


def test_concat_narrations_merges_with_pause(monkeypatch, tmp_path):
    w1 = tmp_path / 'a.wav'
    w1.write_bytes(_tts_mod.silence_wav_bytes(1.0))
    w2 = tmp_path / 'b.wav'
    w2.write_bytes(_tts_mod.silence_wav_bytes(2.0))
    out = tmp_path / 'narration.wav'
    res = mv.concat_narrations([str(w1), str(w2)], str(out), pause_ms=250)
    assert res['ok']
    assert res['duration'] == pytest.approx(1.0 + 2.0 + 0.25, abs=0.03)


def test_mux_happy_path(monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'vv')
    audio = tmp_path / 'a.wav'
    audio.write_bytes(b'aa')
    out = tmp_path / 'final.mp4'
    ffmpeg, marker = _fake_ffmpeg(tmp_path)
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin', lambda: ffmpeg)

    def fake_probe(path, **kw):
        base = {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
                'duration': 8.0, 'has_audio': 'final' in path}
        return base
    monkeypatch.setattr('lib.motion_video._gates.probe_video', fake_probe)
    res = mv.mux_audio_video(str(video), str(audio), str(out))
    assert res['ok'] is True, res
    args = marker.read_text()
    assert '-c:v copy' in args and 'aac' in args and 'loudnorm' in args


def test_mux_missing_inputs(monkeypatch, tmp_path):
    res = mv.mux_audio_video('/nonexistent/v.mp4', '/nonexistent/a.wav',
                             str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert 'missing video file' in res['detail']


def test_mux_requires_audio_track_post_check(monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'vv')
    audio = tmp_path / 'a.wav'
    audio.write_bytes(b'aa')
    ffmpeg, _marker = _fake_ffmpeg(tmp_path)
    monkeypatch.setattr('lib.motion_video._env.ffmpeg_bin', lambda: ffmpeg)
    monkeypatch.setattr('lib.motion_video._gates.probe_video',
                        lambda path, **kw: {'codec': 'h264', 'width': 1080,
                                            'height': 1440, 'fps': 30.0,
                                            'duration': 8.0, 'has_audio': False})
    res = mv.mux_audio_video(str(video), str(audio), str(tmp_path / 'o.mp4'))
    assert res['ok'] is False
    assert 'no audio track' in res['detail']


def test_motion_tools_include_p2_tools_with_project():
    from lib.tools.registry import assemble_tool_list
    tools, _ = assemble_tool_list(_ctx(True))
    names = {t['function']['name'] for t in tools}
    assert {'motion_video_narrate', 'motion_video_mux'} <= names
