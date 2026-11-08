"""Media Model facts: maintained catalog, domain projection and admission.

Specifies lib/model_info/media_facts.py together with the two model-routing
boundaries that consume it: ``normalize_document`` / ``public_projection``
(shape validation, read-only official projections stripped on write) and
``prepare_registration_document`` (media kinds may be priced in media units;
units must belong to the Model's kinds).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib.model_info.capability_taxonomy import (
    MODEL_KINDS,
    is_media_kind,
    media_price_units_for,
)
from lib.model_info.media_facts import (
    MediaFactError,
    admit_media_fact_catalog,
    effective_media_profile,
    maintained_media_facts,
)
from lib.model_routing.domain import (
    ModelRoutingError,
    empty_document,
    normalize_document,
    public_projection,
)
from lib.model_routing.registration import prepare_registration_document

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / 'lib/model_info/media_model_facts.json'
_KIND_CAPS = {kind['id']: list(kind['capabilities']) for kind in MODEL_KINDS}
_UNSUPPORTED = {'reasoning': {'support': 'unsupported'}}
_EVIDENCE = {'release_date': '2026-01-01', 'checked_at': '2026-11-06',
             'source_urls': ['https://example.invalid/media-model']}


def _document(*models):
    document = empty_document()
    document['creators'] = [
        {'creator_id': 'openai', 'name': 'OpenAI'},
        {'creator_id': 'google', 'name': 'Google'},
        {'creator_id': 'acme', 'name': 'Acme'},
    ]
    document['models'] = [copy.deepcopy(model) for model in models]
    return document


def _model(model_id, capabilities, **facts):
    return {'creator_id': facts.pop('creator_id', 'acme'), 'model_id': model_id,
            'display_name': model_id, 'capabilities': capabilities,
            'context_window': 4096, 'quality_rank': 0,
            'inference_profile': _UNSUPPORTED, **facts}


def _video_model(**facts):
    facts.setdefault('media_pricing', {'currency': 'USD', 'rates': [
        {'unit': 'per_second', 'price': 0.1, 'tier': '720p'}]})
    return _model('acme-video-1', ['video_gen'], registration_metadata=_EVIDENCE, **facts)


# ── Maintained catalog ──────────────────────────────────────────────────

def test_maintained_catalog_admits_and_every_entry_is_a_coherent_media_fact():
    raw = json.loads(CATALOG.read_text(encoding='utf-8'))
    catalog = admit_media_fact_catalog(raw)
    assert len(catalog) == len(raw['models'])
    kinds = {entry['kind'] for entry in catalog.values()}
    # Every media kind has at least one maintained official Model.
    assert kinds == {kind['id'] for kind in MODEL_KINDS if is_media_kind(kind['id'])}
    for (creator_id, model_id), entry in catalog.items():
        pricing = entry.get('media_pricing')
        if pricing:
            units = {rate['unit'] for rate in pricing['rates']}
            assert units <= media_price_units_for(_KIND_CAPS[entry['kind']]), model_id
        profile = entry.get('media_profile', {})
        if entry['kind'] in {'image_generation', 'video_generation', 'audio_generation',
                             'speech_synthesis'} and 'output_modalities' in profile:
            produced = {'image_generation': 'image', 'video_generation': 'video',
                        'audio_generation': 'audio', 'speech_synthesis': 'audio'}
            assert produced[entry['kind']] in profile['output_modalities'], model_id


def test_maintained_lookup_is_exact_and_detached():
    tts = maintained_media_facts('openai', 'tts-1')
    assert tts['media_pricing']['rates'] == [
        {'unit': 'per_million_characters', 'price': 15.0, 'tier': 'input text characters'}]
    assert tts['media_profile']['max_input_chars'] == 4096
    tts['media_profile']['voices'].clear()
    assert maintained_media_facts('openai', 'tts-1')['media_profile']['voices']
    assert maintained_media_facts('google', 'tts-1') is None
    assert maintained_media_facts('openai', 'TTS-1') is None
    assert maintained_media_facts('openai', 'tts-1-2025-01-01') is None


@pytest.mark.parametrize('mutate, field', [
    (lambda row: row['media_pricing']['rates'][0].update(unit='per_image'), 'media_pricing'),
    (lambda row: row.update(kind='chat'), 'kind'),
    (lambda row: row.update(source_urls=['http://insecure.example/x']), 'models[0]'),
    (lambda row: row['media_profile'].update(aspect_ratios=['wide']), 'media_profile'),
    (lambda row: row.pop('media_pricing') and row.pop('media_profile'), 'models[0]'),
])
def test_catalog_rejects_incoherent_entries(mutate, field):
    raw = json.loads(CATALOG.read_text(encoding='utf-8'))
    row = next(row for row in raw['models'] if row['model_id'] == 'tts-1')
    raw['models'] = [copy.deepcopy(row)]
    mutate(raw['models'][0])
    with pytest.raises(MediaFactError) as caught:
        admit_media_fact_catalog(raw)
    assert field in caught.value.field


def test_catalog_rejects_duplicate_identity():
    raw = json.loads(CATALOG.read_text(encoding='utf-8'))
    raw['models'] = raw['models'][:1] * 2
    with pytest.raises(MediaFactError, match='duplicate'):
        admit_media_fact_catalog(raw)


# ── Domain shape, projection and stripping ──────────────────────────────

@pytest.mark.parametrize('pricing', [
    {'currency': 'USD', 'rates': []},
    {'currency': 'EUR', 'rates': [{'unit': 'per_second', 'price': 1}]},
    {'currency': 'USD', 'rates': [{'unit': 'per_token', 'price': 1}]},
    {'currency': 'USD', 'rates': [{'unit': 'per_second', 'price': -1}]},
    {'currency': 'USD', 'rates': [{'unit': 'per_second', 'price': float('nan')}]},
    {'currency': 'USD', 'rates': [{'unit': 'per_second', 'price': True}]},
    {'currency': 'USD', 'rates': [{'unit': 'per_second', 'price': 1, 'extra': 1}]},
])
def test_malformed_media_pricing_fails_closed_with_its_field(pricing):
    with pytest.raises(ModelRoutingError) as caught:
        normalize_document(_document(_video_model(media_pricing=pricing)))
    assert caught.value.field == 'models[0].media_pricing'


@pytest.mark.parametrize('profile', [
    {'aspect_ratios': ['16x9']},
    {'sizes': ['huge']},
    {'output_modalities': ['smell']},
    {'durations_s': [0]},
    {'durations_s': [4, 4]},
    {'max_outputs': 0},
    {'audio_formats': ['MP3']},
    {'unknown': True},
])
def test_malformed_media_profile_fails_closed_with_its_field(profile):
    with pytest.raises(ModelRoutingError) as caught:
        normalize_document(_document(_video_model(media_profile=profile)))
    assert caught.value.field == 'models[0].media_profile'


def test_official_media_facts_project_on_read_and_never_persist():
    document = _document(_model('tts-1', ['tts'], creator_id='openai'))
    projected = public_projection(document)['models'][0]
    assert projected['official_media_pricing']['rates'][0]['unit'] == 'per_million_characters'
    assert projected['official_media_profile']['voices'][0] == 'alloy'
    echoed = normalize_document(public_projection(document))['models'][0]
    assert 'official_media_pricing' not in echoed
    assert 'official_media_profile' not in echoed


def test_registered_media_facts_win_over_maintained_ones():
    registered = {'currency': 'CNY', 'rates': [
        {'unit': 'per_million_characters', 'price': 99.0}]}
    profile = {'voices': ['house-voice']}
    document = _document(_model('tts-1', ['tts'], creator_id='openai',
                                media_pricing=registered, media_profile=profile))
    projected = public_projection(document)['models'][0]
    assert projected['media_pricing'] == registered
    assert 'official_media_pricing' not in projected
    assert 'official_media_profile' not in projected
    assert effective_media_profile(projected) == profile


def test_maintained_facts_never_describe_a_row_of_another_kind():
    """A chat registration sharing a media model's id is not that model."""
    document = _document(_model('tts-1', ['text'], creator_id='openai'))
    projected = public_projection(document)['models'][0]
    assert 'official_media_pricing' not in projected
    assert 'official_media_profile' not in projected


def test_effective_profile_falls_back_to_maintained_facts():
    row = _model('gpt-image-2', ['image_gen'], creator_id='openai')
    assert effective_media_profile(row)['max_outputs'] == 10
    assert effective_media_profile(_model('unknown-image', ['image_gen'])) is None


# ── Admission ───────────────────────────────────────────────────────────

def test_media_kind_priced_in_media_units_is_admitted():
    profile = {'output_modalities': ['video'], 'durations_s': [4, 8], 'native_audio': True}
    candidate = prepare_registration_document(
        _document(_video_model(media_profile=profile)), previous=empty_document())
    model = candidate['models'][0]
    assert model['registration_state'] == 'ready'
    assert model['registration_issues'] == []
    assert model['media_profile'] == profile


def test_media_units_of_another_kind_keep_the_model_a_draft():
    model = _video_model()
    model['media_pricing']['rates'][0]['unit'] = 'per_image'
    candidate = prepare_registration_document(_document(model), previous=empty_document())
    row = candidate['models'][0]
    assert row['registration_state'] == 'pending_metadata'
    assert 'media_pricing units do not apply to this model kind: per_image' in row['registration_issues']
    assert any(issue.startswith('list_pricing.input/output/currency/unit or media_pricing')
               for issue in row['registration_issues'])


def test_chat_models_still_require_token_prices():
    model = _video_model()
    model['capabilities'] = ['text']
    candidate = prepare_registration_document(_document(model), previous=empty_document())
    issues = candidate['models'][0]['registration_issues']
    assert 'list_pricing.input/output/currency/unit' in issues
    assert 'media_pricing units do not apply to this model kind: per_second' in issues


def test_activation_of_an_unpriced_media_model_is_refused():
    model = _video_model(registration_state='ready')
    del model['media_pricing']
    with pytest.raises(ModelRoutingError) as caught:
        prepare_registration_document(_document(model), previous=empty_document())
    assert 'media_pricing (per_request/per_second)' in str(caught.value)


def test_registration_evidence_may_cite_media_facts():
    evidence = dict(_EVIDENCE, field_sources={
        'media_pricing': ['https://example.invalid/media-model'],
        'media_profile': ['https://example.invalid/media-model']})
    model = _video_model()
    model['registration_metadata'] = evidence
    candidate = prepare_registration_document(_document(model), previous=empty_document())
    assert candidate['models'][0]['registration_metadata']['field_sources'] == evidence['field_sources']
