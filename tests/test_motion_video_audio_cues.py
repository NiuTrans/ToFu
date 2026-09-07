"""Licensed audio-cue IR and deterministic mix graph."""

from __future__ import annotations

import json
import os

import pytest

from lib.motion_video._audio_cues import (
    AUDIO_CONTRACT_VERSION,
    _mix_args,
    audio_plan_errors,
    audio_plan_summary,
    audio_plan_template,
    normalise_audio_plan,
    write_audio_attribution,
)
from lib.motion_video._timeline import normalise_timeline_contract

pytestmark = pytest.mark.unit


def _asset(tmp_path, name='sound.wav'):
    path = tmp_path / name
    path.write_bytes(b'RIFF' + b'0' * 128)
    return path


def _scenes():
    scenes = [
        {'id': 's1', 'start': 0, 'end': 4},
        {'id': 's2', 'start': 4, 'end': 10},
    ]
    normalise_timeline_contract(scenes)
    return scenes


def _verified_grid(*, offset=0.2):
    return {
        'bpm': 120, 'offset_s': offset,
        'beat_observations_s': [offset + index * 0.5
                                for index in range(20)],
        'first_beat_valid': True, 'verified': True,
    }


def test_plan_stages_deduplicates_and_resolves_scene_progress(tmp_path):
    asset = _asset(tmp_path)
    plan = normalise_audio_plan({
        'beat_grid': _verified_grid(),
        'bgm': {'asset': asset.name, 'license': 'user-provided'},
        'cues': [
            {'id': 'enter', 'kind': 'whoosh', 'asset': asset.name,
             'license': 'user-provided', 'scene_id': 's2', 'progress': 0.5,
             'duration_s': 1.0, 'peak_offset_s': 0.2},
            {'id': 'slam', 'kind': 'impact', 'asset': asset.name,
             'license': 'user-provided', 'beat': 8, 'duration_s': 0.5},
        ],
    }, _scenes(), base_dir=str(tmp_path),
        stage_dir=str(tmp_path / 'job' / 'audio' / 'assets'),
        program_duration=10)
    assert plan['version'] == AUDIO_CONTRACT_VERSION
    assert plan['cues'][0]['target_s'] == pytest.approx(7.0)
    assert plan['cues'][0]['start_s'] == pytest.approx(6.8)
    assert plan['cues'][1]['target_s'] == pytest.approx(4.2)
    assert plan['beat_grid']['max_residual_ms'] == pytest.approx(0)
    assert plan['beat_grid']['match_ratio'] == pytest.approx(1)
    paths = {plan['bgm']['asset_path'],
             *(cue['asset_path'] for cue in plan['cues'])}
    assert len(paths) == 1, 'same bytes should be staged only once'
    assert os.path.isfile(next(iter(paths)))
    assert audio_plan_errors(plan) == []
    assert audio_plan_summary(plan)['beat_synced'] is True


def test_non_original_audio_requires_source_and_cc_by_attribution(tmp_path):
    asset = _asset(tmp_path)
    with pytest.raises(ValueError, match='source_url'):
        normalise_audio_plan({
            'bgm': {'asset': asset.name, 'license': 'Mixkit'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)
    with pytest.raises(ValueError, match='attribution'):
        normalise_audio_plan({
            'bgm': {'asset': asset.name, 'license': 'CC-BY-4.0',
                    'source_url': 'https://example.com/music'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_runtime_download_and_path_escape_are_rejected(tmp_path):
    _asset(tmp_path)
    with pytest.raises(ValueError, match='must be local'):
        normalise_audio_plan({
            'bgm': {'asset': 'https://example.com/a.mp3',
                    'license': 'user-provided'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)
    outside = tmp_path.parent / 'outside.wav'
    outside.write_bytes(b'x')
    with pytest.raises(ValueError, match='escapes'):
        normalise_audio_plan({
            'bgm': {'asset': '../outside.wav', 'license': 'user-provided'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_verified_beat_grid_cannot_claim_bad_residual(tmp_path):
    asset = _asset(tmp_path)
    with pytest.raises(ValueError, match='residual'):
        normalise_audio_plan({
            'beat_grid': {
                'bpm': 120, 'offset_s': 0.2, 'verified': True,
                'beat_observations_s': [0.2, 0.7, 1.2, 1.723],
                'first_beat_valid': True,
            },
            'bgm': {'asset': asset.name, 'license': 'original'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_verified_grid_requires_recomputable_observations(tmp_path):
    asset = _asset(tmp_path)
    with pytest.raises(ValueError, match='beat observations'):
        normalise_audio_plan({
            'beat_grid': {'bpm': 120, 'offset_s': 0,
                          'max_residual_ms': 0, 'match_ratio': 1,
                          'mean_abs_ms': 0, 'drift_ms': 0,
                          'first_beat_valid': True, 'verified': True},
            'bgm': {'asset': asset.name, 'license': 'original'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_verified_grid_recomputes_match_ratio_from_missing_beats(tmp_path):
    asset = _asset(tmp_path)
    observations = [index * 0.5 for index in range(100)]
    observations[50] = None
    observations[75] = None
    observations[90] = None
    with pytest.raises(ValueError, match='match>=98%'):
        normalise_audio_plan({
            'beat_grid': {'bpm': 120, 'offset_s': 0,
                          'beat_observations_s': observations,
                          'first_beat_valid': True, 'verified': True},
            'bgm': {'asset': asset.name, 'license': 'original'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_unknown_audio_contract_version_is_rejected(tmp_path):
    asset = _asset(tmp_path)
    with pytest.raises(ValueError, match='unsupported audio plan version'):
        normalise_audio_plan({
            'version': 'motion-audio-v999',
            'bgm': {'asset': asset.name, 'license': 'original'},
        }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_staged_asset_hash_drift_is_a_contract_error(tmp_path):
    asset = _asset(tmp_path)
    plan = normalise_audio_plan({
        'bgm': {'asset': asset.name, 'license': 'original'},
    }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
        program_duration=10)
    with open(plan['bgm']['asset_path'], 'ab') as handle:
        handle.write(b'tampered')
    assert any('hash drift' in error for error in audio_plan_errors(plan))


def test_public_template_and_human_attribution_are_deliverable(tmp_path):
    template = audio_plan_template()
    assert template['version'] == AUDIO_CONTRACT_VERSION
    assert template['bgm']['license']
    asset = _asset(tmp_path)
    plan = normalise_audio_plan({
        'bgm': {'asset': asset.name, 'title': 'Original score',
                'license': 'original'},
    }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
        program_duration=10)
    dest = tmp_path / 'ATTRIBUTION.txt'
    write_audio_attribution(plan, str(dest))
    text = dest.read_text(encoding='utf-8')
    assert 'Original score' in text
    assert 'License: original' in text
    assert plan['bgm']['sha256'] in text


def test_mix_graph_ducks_bgm_and_pins_sfx_by_delay(tmp_path):
    bgm = _asset(tmp_path, 'bgm.wav')
    sfx = _asset(tmp_path, 'hit.wav')
    plan = normalise_audio_plan({
        'bgm': {'asset': bgm.name, 'license': 'original',
                'fade_in_s': 1, 'fade_out_s': 2},
        'cues': [{'id': 'hit', 'kind': 'impact', 'asset': sfx.name,
                  'license': 'original', 'at_s': 4.2,
                  'peak_offset_s': 0.2, 'duration_s': 0.8}],
    }, _scenes(), base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
        program_duration=10)
    args, graph = _mix_args(
        '/bin/ffmpeg', '/x/video.mp4', '/x/voice.wav', plan, '/x/out.mp4')
    assert 'sidechaincompress=' in graph
    assert 'adelay=4000:all=1[sfx0]' in graph
    assert 'afade=t=out:st=8.0000:d=2.0000' in graph
    assert 'loudnorm=I=-14.00' in graph
    assert args.count('-i') == 4
    assert '-c:v' in args and 'copy' in args and '192k' in args


def test_verified_grid_audits_visual_transition_cut_error(tmp_path):
    asset = _asset(tmp_path)
    scenes = _scenes()
    scenes[1]['transition_in'] = 'dissolve'
    scenes[1]['transition_in_duration_s'] = 0.4
    plan = normalise_audio_plan({
        'beat_grid': _verified_grid(offset=0),
        'beat_sync_mode': 'audit',
        'bgm': {'asset': asset.name, 'license': 'original'},
    }, scenes, base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
        program_duration=10)
    # Scene 2 begins at 4.0s: exactly beat 8 at 120 BPM.
    assert plan['beat_alignment']['status'] == 'passed'
    assert plan['beat_alignment']['items'][0]['error_frames'] == 0


def test_required_beat_sync_rejects_off_grid_transition(tmp_path):
    asset = _asset(tmp_path)
    scenes = _scenes()
    scenes[1]['timeline_start_s'] = 4.2
    scenes[1]['transition_in_duration_s'] = 0.4
    with pytest.raises(ValueError, match='required beat sync failed'):
        normalise_audio_plan({
            'beat_grid': _verified_grid(offset=0),
            'beat_sync_mode': 'required',
            'bgm': {'asset': asset.name, 'license': 'original'},
        }, scenes, base_dir=str(tmp_path), stage_dir=str(tmp_path / 'stage'),
            program_duration=10)


def test_direct_mux_tool_upgrades_legacy_scenes_before_relative_cues(
        monkeypatch, tmp_path):
    """The chat tool and automatic engine must resolve the same cue clock."""
    from lib.tasks_pkg.handlers import motion_video as handler

    asset = _asset(tmp_path)
    scenes_path = tmp_path / 'scenes.json'
    scenes_path.write_text(json.dumps([
        {'id': 's1', 'start': 0, 'end': 2},
        {'id': 's2', 'start': 2, 'end': 4},
    ]), encoding='utf-8')
    plan_path = tmp_path / 'plan.json'
    plan_path.write_text(json.dumps({
        'cues': [{'id': 'hit', 'kind': 'impact', 'asset': asset.name,
                  'license': 'original', 'scene_id': 's2', 'progress': 0.5,
                  'duration_s': 0.4}],
    }), encoding='utf-8')
    video = tmp_path / 'silent.mp4'
    video.write_bytes(b'video')
    captured = {}

    monkeypatch.setattr('lib.motion_video.probe_video',
                        lambda _path: {'duration': 4.0})

    def fake_mix(_video, _narration, plan, output, **_kwargs):
        captured['plan'] = plan
        return {'ok': True, 'output': output}

    monkeypatch.setattr('lib.motion_video.mix_audio_timeline', fake_mix)
    monkeypatch.setattr(handler, '_finalize_tool_round',
                        lambda *_args, **_kwargs: None)
    _, content, _ = handler._handle_motion_video_tool(
        {}, None, 'motion_video_mux', 'call-1', {
            'video': str(video), 'output': str(tmp_path / 'final.mp4'),
            'audio_plan_path': str(plan_path),
            'scenes_path': str(scenes_path),
        }, 0, {}, {}, str(tmp_path), True)

    assert json.loads(content)['ok'] is True
    assert captured['plan']['cues'][0]['target_s'] == pytest.approx(3.0)
    assert captured['plan']['cues'][0]['start_s'] == pytest.approx(3.0)
