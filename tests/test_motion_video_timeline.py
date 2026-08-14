"""Program-time / transition-handle contract."""

from __future__ import annotations

import pytest

from lib.motion_video._timeline import (
    TIMELINE_CONTRACT_VERSION,
    normalise_timeline_contract,
    timeline_contract_errors,
)
from lib.motion_video._concat import _normalise_transitions, _transition_args

pytestmark = pytest.mark.unit


def _scenes():
    return [
        {'id': 's1', 'start': 0, 'end': 4, 'transition_in': 'cold-open'},
        {'id': 's2', 'start': 4, 'end': 9, 'transition_in': 'dissolve'},
        {'id': 's3', 'start': 9, 'end': 12, 'transition_in': 'cut'},
    ]


def test_overlap_uses_visual_handle_without_shortening_program():
    scenes = _scenes()
    plan = normalise_timeline_contract(scenes)
    assert plan['version'] == TIMELINE_CONTRACT_VERSION
    assert plan['duration_s'] == pytest.approx(12.0)
    assert scenes[0]['content_duration_s'] == pytest.approx(4.0)
    assert scenes[0]['outgoing_handle_s'] == pytest.approx(0.4)
    assert scenes[0]['render_duration_s'] == pytest.approx(4.4)
    assert scenes[1]['timeline_start_s'] == pytest.approx(4.0)
    assert scenes[1]['transition_window_s'] == pytest.approx([4.0, 4.4])
    assert scenes[1]['transition_ffmpeg'] == 'fade'
    assert scenes[-1]['render_duration_s'] == pytest.approx(3.0)
    assert timeline_contract_errors(scenes) == []


def test_tts_durations_are_the_program_clock_and_handles_are_extra():
    scenes = _scenes()
    normalise_timeline_contract(
        scenes, durations={'s1': 5.2, 's2': 6.3, 's3': 2.5})
    assert scenes[0]['content_duration_s'] == pytest.approx(5.2)
    assert scenes[0]['render_duration_s'] == pytest.approx(5.6)
    assert scenes[1]['timeline_start_s'] == pytest.approx(5.2)
    assert scenes[-1]['timeline_end_s'] == pytest.approx(14.0)


def test_overlap_is_clamped_to_a_quarter_of_short_incoming_shot():
    scenes = [
        {'id': 'a', 'start': 0, 'end': 3},
        {'id': 'b', 'start': 3, 'end': 4,
         'transition_in': 'dissolve', 'transition_duration_s': 2.0},
    ]
    normalise_timeline_contract(scenes)
    assert scenes[1]['transition_in_duration_s'] == pytest.approx(0.25)
    assert scenes[0]['outgoing_handle_s'] == pytest.approx(0.25)


def test_match_cut_is_authored_choreography_not_fake_overlap():
    scenes = [
        {'id': 'a', 'start': 0, 'end': 3},
        {'id': 'b', 'start': 3, 'end': 6,
         'transition_in': 'match-cut', 'transition_duration_s': 0.5},
    ]
    plan = normalise_timeline_contract(scenes)
    assert plan['transitions'][0]['duration_s'] == 0
    assert plan['transitions'][0]['ffmpeg'] == ''
    assert scenes[0]['render_duration_s'] == pytest.approx(3.0)


def test_gate_catches_handle_drift():
    scenes = _scenes()
    normalise_timeline_contract(scenes)
    scenes[0]['render_duration_s'] = 4.0
    assert any('does not include handle' in error
               for error in timeline_contract_errors(scenes))


def test_ffmpeg_graph_chains_overlap_and_hard_cut():
    inputs = ['/x/a.mp4', '/x/b.mp4', '/x/c.mp4']
    probes = [
        {'width': 1080, 'height': 1440, 'fps': 30, 'duration': 4.4},
        {'width': 1080, 'height': 1440, 'fps': 30, 'duration': 5.0},
        {'width': 1080, 'height': 1440, 'fps': 30, 'duration': 3.0},
    ]
    transitions = _normalise_transitions([
        {'duration_s': 0.4, 'ffmpeg': 'fade'},
        {'duration_s': 0, 'ffmpeg': ''},
    ], 3)
    args, duration = _transition_args(
        '/bin/ffmpeg', inputs, probes, transitions, '/x/out.mp4')
    graph = args[args.index('-filter_complex') + 1]
    assert 'xfade=transition=fade:duration=0.4000:offset=4.0000' in graph
    assert '[vx1][v2]concat=n=2:v=1:a=0[vx2]' in graph
    assert duration == pytest.approx(12.0)


def test_transition_contract_rejects_filter_injection():
    with pytest.raises(ValueError, match='unsupported xfade'):
        _normalise_transitions([
            {'duration_s': 0.4, 'ffmpeg': 'fade;movie=/etc/passwd'},
        ], 2)


def test_transition_contract_enforces_overlap_ceiling():
    with pytest.raises(ValueError, match='0.8s overlap ceiling'):
        _normalise_transitions([
            {'duration_s': 0.81, 'ffmpeg': 'fade'},
        ], 2)
