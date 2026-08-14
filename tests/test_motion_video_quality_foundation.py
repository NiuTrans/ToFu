"""Asset-first scene preparation and temporal-QA foundation."""

from __future__ import annotations

import os

import pytest

from lib.design_sys.visual_qa import QA_CHECKLIST
from lib.motion_video import _asset_preflight as preflight
from lib.motion_video._quality import asset_floor_findings, scene_telemetry

pytestmark = pytest.mark.unit


def _scene():
    return {
        'id': 'scene-001',
        'text': 'Explain it',
        'assets': [
            {'role': 'subject', 'prompt': 'editorial robot, no text'},
            {'role': 'background', 'prompt': 'paper texture'},
        ],
    }


def test_preflight_materialises_required_roles_and_reuses_manifest(
        tmp_path, monkeypatch):
    calls = []

    def _generate(prompt, scene_dir, **kwargs):
        calls.append((prompt, kwargs))
        assets = tmp_path / 'assets'
        assets.mkdir(exist_ok=True)
        path = assets / 'hero.png'
        path.write_bytes(b'png')
        return 'assets/hero.png'

    monkeypatch.setattr('lib.motion_video._scene_author._generate_scene_asset',
                        _generate)
    scene = _scene()
    first = preflight.prepare_scene_assets(scene, str(tmp_path))
    assert first['resolved'][0]['role'] == 'subject'
    assert scene['resolved_assets'] == first['resolved']
    assert len(calls) == 1, 'optional background must not spend generation'
    second_scene = _scene()
    second = preflight.prepare_scene_assets(second_scene, str(tmp_path))
    assert second['resolved'] == first['resolved']
    assert len(calls) == 1, 'resume regenerated an existing required asset'


def test_prepared_asset_must_be_referenced_by_the_composition(tmp_path):
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'hero.png').write_bytes(b'png')
    scene = _scene()
    scene['resolved_assets'] = [
        {'role': 'subject', 'prompt': 'x', 'path': 'assets/hero.png'}]
    findings = asset_floor_findings(
        scene, '<svg><rect width="10" height="10"/></svg>', str(tmp_path),
        mode='authored')
    assert any('not used' in finding for finding in findings)
    html = ('<img src="assets/hero.png">'
            '<svg><rect width="10" height="10"/></svg>')
    assert not any('not used' in finding for finding in asset_floor_findings(
        scene, html, str(tmp_path), mode='authored'))


def test_scene_telemetry_exposes_the_creative_contract(tmp_path):
    scene = _scene() | {
        'narrative_role': 'hook', 'blueprint': 'hook-counter-burst',
        'transition_in': 'cold-open', 'resolved_assets': [{'path': 'x'}],
    }
    rec = scene_telemetry(scene, '<html/>', str(tmp_path),
                          mode='authored', fill=None,
                          craft_reads=['hook-counter-burst'])
    assert rec['narrative_role'] == 'hook'
    assert rec['blueprint'] == 'hook-counter-burst'
    assert rec['resolved_assets'] == 1
    assert rec['craft_reads'] == ['hook-counter-burst']


def test_visual_qa_has_temporal_and_semantic_axes():
    ids = {item[0] for item in QA_CHECKLIST}
    assert {'temporal-staging', 'semantic-consistency'} <= ids
