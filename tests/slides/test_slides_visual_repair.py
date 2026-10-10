"""Measured visual repair must preserve retained pages until it can improve them."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml

from lib.slides import recipe
from lib.production.stages import StageAborted, StageFailed, run_stages

pytestmark = pytest.mark.unit


@pytest.fixture
def repair_case(tmp_path, monkeypatch):
    import lib.design_sys.visual_qa as vqa

    root = tmp_path / 'deck'
    (root / 'pages').mkdir(parents=True)
    (root / 'preview').mkdir()
    page = {'pageType': 'content', 'elements': [{
        'elementId': 'title', 'elementType': 'text',
        'bounds': [80, 80, 900, 160], 'content': {'text': 'Original'}}]}
    seed = yaml.safe_dump(page)
    page['elements'][0]['content']['text'] = 'Improved'
    candidate = yaml.safe_dump(page)
    source = root / 'pages' / '01.page'
    source.write_text(seed)
    manifest = {'version': 'v2', 'title': 'Visual repair', 'size': [1280, 720],
                'theme': {}, 'pages': ['pages/01.page']}
    (root / 'deck.pptd').write_text(yaml.safe_dump(manifest))
    preview = root / 'preview' / '01.png'
    preview.write_bytes(b'original')
    ctx = {'deck_dir': str(root), 'owner_user_id': 17, 'provider_pin_id': 'owner-route',
           'qa_model': 'test-vlm', 'abort_event': threading.Event(),
           'artifacts': {'outline': {'pages': [{'key_message': 'Original'}]},
                         'design': {'theme_id': 'paper-engineer'}, 'author': {},
                         'render': {'previews': [str(preview)]}}}
    state = {'author_calls': 0, 'reviews': [], 'layout_ok': True,
             'repair_review_ok': True, 'render_ok': True, 'worsen_deck': False,
             'abort_during_recheck': False, 'remaining': False}
    finding = {'check': 'hierarchy', 'element': 'title', 'issue': 'Unreadable title',
               'severity': 'major', 'fix': 'Increase type size'}
    monkeypatch.setenv('TOFU_PRODUCTION_LLM_FANOUT', '1')
    monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))
    monkeypatch.setattr(vqa, 'resolve_visual_qa_model', lambda value: value)

    def contact(paths, output, **kwargs):
        Path(output).write_bytes(b'|'.join(Path(path).read_bytes() for path in paths))
        return output

    monkeypatch.setattr('lib.design_sys.contact_sheet.build_contact_sheet', contact)

    def author(deck, brief, index, total, **kwargs):
        assert kwargs['seed_yaml'] == seed
        assert kwargs['owner_user_id'] == 17
        assert kwargs['provider_pin_id'] == 'owner-route'
        state['author_calls'] += 1
        return {'mode': 'authored', 'yaml': candidate}

    monkeypatch.setattr('lib.slides.author.author_page', author)

    def layout(deck, **kwargs):
        assert kwargs['page_indexes'] == [0]
        assert 'Improved' in deck.pages[0].raw['elements'][0]['content']['text']
        assert source.read_text() == seed
        return {'ran': True, 'ok': state['layout_ok'],
                'findings': [] if state['layout_ok'] else [{'message': 'overflow'}]}

    monkeypatch.setattr('lib.slides.layout_qa.inspect_deck_layout', layout)

    def render(deck, index, output, **kwargs):
        assert source.read_text() == seed
        if not state['render_ok']:
            raise RuntimeError('browser died')
        Path(output).write_bytes(b'improved')

    monkeypatch.setattr('lib.slides.render_png.render_page_png', render)

    def review(path, **kwargs):
        assert kwargs['owner_user_id'] == 17
        assert kwargs['provider_pin_id'] == 'owner-route'
        improved = Path(path).read_bytes() == b'improved'
        state['reviews'].append((kwargs['label'], improved))
        if kwargs['label'] == 'deck-coherence':
            if state.get('only_global'):
                return {'ok': True, 'findings': [] if improved else [
                    {**finding, 'element': 'whole deck', 'issue': 'Repeated layout'}]}
            return {'ok': True, 'findings': [finding] if improved and state['worsen_deck'] else []}
        if improved and state['abort_during_recheck']:
            ctx['abort_event'].set()
        return {'ok': not improved or state['repair_review_ok'],
                'findings': [finding] if (not improved and not state.get('only_global'))
                or state['remaining'] else []}

    monkeypatch.setattr(vqa, 'qa_frame', review)
    return ctx, state, source, preview, seed, candidate


def test_verified_repair_rechecks_page_and_deck_and_preserves_owner(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    result = recipe._run_visual_qa(ctx)
    assert result['repairedPages'] == [1]
    assert result['remaining'] == []
    assert source.read_text() == candidate
    assert preview.read_bytes() == b'improved'
    assert ('page-01', True) in state['reviews']
    assert ('deck-coherence', True) in state['reviews']
    assert recipe._gate_visual_qa(ctx, result) == []


@pytest.mark.parametrize('failure', ['layout_ok', 'repair_review_ok', 'render_ok'])
def test_unverified_candidate_cannot_replace_page_or_preview(repair_case, failure):
    ctx, state, source, preview, seed, candidate = repair_case
    state[failure] = False
    result = recipe._run_visual_qa(ctx)
    assert result['repaired'] == 0
    assert result['rejectedPages'] == [1]
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'
    assert recipe._gate_visual_qa(ctx, result)


def test_unchanged_visual_defect_is_rejected_and_blocks_export(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['remaining'] = True
    stages = [stage for stage in recipe.slides_recipe_stages() if stage.name == 'visual_qa']
    with pytest.raises(StageFailed, match='visual_qa'):
        run_stages(stages, ctx, state_path=str(source.parent.parent / 'state.json'))
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'
    assert state['author_calls'] == 1


def test_whole_deck_regression_rolls_back_verified_local_repair(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['worsen_deck'] = True
    result = recipe._run_visual_qa(ctx)
    assert result['repaired'] == 0
    assert result['rejectedPages'] == [1]
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'


def test_abort_during_visual_recheck_preserves_original(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['abort_during_recheck'] = True
    with pytest.raises(StageAborted):
        recipe._run_visual_qa(ctx)
    assert source.read_text() == seed
    assert preview.read_bytes() == b'original'


def test_global_coherence_findings_enter_repair_even_without_page_defects(repair_case):
    ctx, state, source, preview, seed, candidate = repair_case
    state['only_global'] = True
    result = recipe._run_visual_qa(ctx)
    assert state['author_calls'] == 1
    assert result['repairedPages'] == [1]
    assert result['coherence'] == result['remaining'] == []
