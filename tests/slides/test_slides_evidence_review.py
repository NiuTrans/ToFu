"""Evidence follows owner-approved plans through review, caching and repair."""

from __future__ import annotations

import copy
import json

import pytest

from lib.design_sys import visual_qa as vqa
from lib.slides import recipe
from lib.slides.review_context import build_review_context
from tests.slides.test_slides_visual_repair import repair_case as repair_case

pytestmark = pytest.mark.unit


def _outline(point='Accuracy 82% on held-out data; no causal claim.'):
    return {'title': 'Research results', 'scenario': 'academic-research',
            'pages': [{'purpose': 'Assess the evidence',
                       'key_message': 'The method improves prediction.',
                       'content_notes': 'Compare baseline and method [S1].',
                       'sources': [{'id': 'S1', 'title': 'Study, Table 2',
                                    'url': 'https://example.org/study',
                                    'point': point}]}]}


def test_evidence_context_matches_dispatched_digest(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    image.write_bytes(b'page-pixels')
    prompts = []
    monkeypatch.setattr('lib.model_info._capabilities.model_supports_vision',
                        lambda _model: True)

    def dispatch(messages, **kwargs):
        prompts.append(messages[0]['content'][0]['text'])
        return ('{"findings":[{"check":"claim-scope","severity":"major",'
                '"issue":"Prediction is presented as causation",'
                '"fix":"Use the supported predictive claim"}]}', {})

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', dispatch)
    context = build_review_context(_outline(), page_index=0)
    identity = vqa.qa_frame_input_sha256(
        str(image), model='vlm-test', review_context=context)
    result = vqa.qa_frame(str(image), model='vlm-test', review_context=context)

    assert result['ok'] and result['input_sha256'] == identity
    assert result['findings'][0]['check'] == 'claim-scope'
    assert 'held-out data' in prompts[0] and 'https://example.org/study' in prompts[0]
    assert 'not independent evidence' in prompts[0]
    changed = build_review_context(_outline('Accuracy 79%.'), page_index=0)
    assert vqa.qa_frame_input_sha256(
        str(image), model='vlm-test', review_context=changed) != identity


def test_page_cache_reuses_only_the_same_effective_evidence(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    image.write_bytes(b'unchanged-pixels')
    calls = []
    approved = _outline('Owner-corrected result: 79%.')
    ctx = {'topic': 'Present the supplied study', 'artifacts': {
        'outline': _outline(), 'outline_gate': {'outline': approved}}}

    def review(path, **kwargs):
        calls.append(kwargs['review_context'])
        identity = vqa.qa_frame_input_sha256(
            path, theme=kwargs['theme'], subject=kwargs['subject'],
            model=kwargs['model'], review_context=kwargs['review_context'])
        return {'ok': True, 'findings': [], 'input_sha256': identity}

    monkeypatch.setattr(vqa, 'qa_frame', review)
    cache = {'version': 'slide-visual-qa-v1', 'entries': {}}
    kwargs = {'theme': None, 'vqa': vqa, 'qa_model': 'vlm-test',
              'cache': cache, 'cache_path': str(tmp_path / 'qa.json')}
    recipe._run_page_visual_reviews(ctx, [str(image)], **kwargs)
    recipe._run_page_visual_reviews(ctx, [str(image)], **kwargs)
    assert len(calls) == 1
    assert 'Owner-corrected result' in calls[0] and 'Accuracy 82%' not in calls[0]

    approved['pages'][0]['sources'][0]['point'] = 'Owner-corrected result: 77%.'
    recipe._run_page_visual_reviews(ctx, [str(image)], **kwargs)
    assert len(calls) == 2 and '77%' in calls[-1]


def test_repaired_page_keeps_the_effective_source_context(repair_case, monkeypatch):
    ctx, state, source, preview, seed, candidate = repair_case
    ctx['artifacts']['outline_gate'] = {'outline': _outline()}
    previous = vqa.qa_frame
    contexts = []

    def review(path, **kwargs):
        contexts.append((kwargs['label'], kwargs['review_context']))
        return previous(path, **kwargs)

    monkeypatch.setattr(vqa, 'qa_frame', review)
    result = recipe._run_visual_qa(ctx)
    assert result['repairedPages'] == [1] and source.read_text() == candidate
    pages = [context for label, context in contexts if label == 'page-01']
    assert len(pages) == 2 and pages[0] == pages[1]
    assert 'held-out data' in pages[0]
    decks = [json.loads(context) for label, context in contexts
             if label == 'deck-coherence']
    assert len(decks) == 2 and decks[0] == decks[1]
    assert decks[0]['model_authored_story_plan'][0]['key_message'].startswith('The method')


def test_context_projection_is_bounded_and_does_not_mutate_sources():
    outline = _outline('x' * 100_000)
    original = copy.deepcopy(outline)
    context = build_review_context(outline, page_index=0, request='y' * 100_000)
    assert len(context) < 12000
    assert json.loads(context)['cited_source_excerpts'][0]['point'].endswith('…')
    assert outline == original


def test_context_truncation_is_shared_by_cache_and_dispatch(tmp_path, monkeypatch):
    image = tmp_path / 'page.png'
    image.write_bytes(b'page-pixels')
    prompts = []
    monkeypatch.setattr('lib.model_info._capabilities.model_supports_vision',
                        lambda _model: True)

    def dispatch(messages, **kwargs):
        prompts.append(messages[0]['content'][0]['text'])
        return '{"findings": []}', {}

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', dispatch)
    context = 'source ' * 100_000
    identity = vqa.qa_frame_input_sha256(
        str(image), model='vlm-test', review_context=context)
    result = vqa.qa_frame(str(image), model='vlm-test', review_context=context)
    assert identity == result['input_sha256']
    assert len(prompts[0]) < 20000 and '已截断' in prompts[0]
