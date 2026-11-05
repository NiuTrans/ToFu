"""Local artifact publication requires shared Model admission and immutable bytes."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from lib.model_info.local_artifacts import admit_local_model_artifacts, load_local_model_artifacts
from lib.model_routing import ModelRoutingError

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog():
    return json.loads((ROOT / 'lib/model_info/local_model_artifacts.json').read_text())


def test_catalog_publishes_ready_ranking_model_without_mutating_input(catalog):
    before = deepcopy(catalog)
    admitted = admit_local_model_artifacts(catalog)
    model = admitted['artifacts'][0]['model']
    assert catalog == before
    assert model['registration_state'] == 'ready'
    assert model['release_date'] == '2025-06-05'
    assert model['capabilities'] == ['reranking']
    assert model['context_window'] == 32768
    assert model['inference_profile']['reasoning']['support'] == 'unsupported'
    assert model['list_pricing'] == {
        'input': .01, 'output': .01, 'currency': 'USD', 'unit': 'per_million_tokens',
    }
    assert load_local_model_artifacts() == admitted
    assert admit_local_model_artifacts(admitted) == admitted
    model['capabilities'].append('text')
    assert load_local_model_artifacts()['artifacts'][0]['model']['capabilities'] == ['reranking']


@pytest.mark.parametrize('field', ['registration_metadata', 'list_pricing', 'inference_profile'])
def test_incomplete_model_cannot_be_exported_as_a_local_artifact(catalog, field):
    del catalog['artifacts'][0]['model'][field]
    before = deepcopy(catalog)
    with pytest.raises(ModelRoutingError) as error:
        admit_local_model_artifacts(catalog)
    assert error.value.kind == 'model_registration_incomplete'
    assert catalog == before


@pytest.mark.parametrize('state', ['pending_metadata', 'pending_validation', 'disabled'])
def test_local_export_never_promotes_explicit_non_callable_state(catalog, state):
    catalog['artifacts'][0]['model']['registration_state'] = state
    with pytest.raises(ValueError, match='cannot promote'):
        admit_local_model_artifacts(catalog)


@pytest.mark.parametrize('name', [
    '../model.safetensors', '/tmp/model.safetensors', 'a/b', 'a\\b', '.hidden', 'weights\n',
])
def test_artifact_file_names_cannot_escape_the_snapshot(catalog, name):
    catalog['artifacts'][0]['source']['files'][0]['name'] = name
    with pytest.raises(ValidationError):
        admit_local_model_artifacts(catalog)


@pytest.mark.parametrize('field,value', [
    ('revision', 'main'), ('revision', 'v1'), ('revision', 'a' * 40 + '\n'),
])
def test_artifact_requires_an_immutable_repository_revision(catalog, field, value):
    catalog['artifacts'][0]['source'][field] = value
    with pytest.raises(ValidationError):
        admit_local_model_artifacts(catalog)


def test_duplicate_file_names_are_rejected_even_with_different_hashes(catalog):
    files = catalog['artifacts'][0]['source']['files']
    other = dict(files[0], sha256='0' * 64)
    files.append(other)
    with pytest.raises(ValueError, match='duplicate.*filename'):
        admit_local_model_artifacts(catalog)


def test_artifacts_cannot_disagree_about_a_shared_model_identity(catalog):
    other = deepcopy(catalog['artifacts'][0])
    other['artifact_id'] += '-variant'
    other['model']['context_window'] = 4096
    catalog['artifacts'].append(other)
    with pytest.raises(ValueError, match='disagree'):
        admit_local_model_artifacts(catalog)


def test_ranking_execution_cannot_publish_a_chat_only_model(catalog):
    catalog['artifacts'][0]['model']['capabilities'] = ['text']
    with pytest.raises(ValueError, match='reranking capability'):
        admit_local_model_artifacts(catalog)
