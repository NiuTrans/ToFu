"""The authored defaults and boot-pinned artifact share the routing generator."""
import json
from pathlib import Path
import sys

from jsonschema import ValidationError
import pytest

from lib.model_routing import model_defaults
from lib.model_routing.domain import ModelRoutingError
from scripts import gen_api_v1_model_routing_contract as generator

pytestmark = pytest.mark.unit


def test_generated_defaults_match_the_authored_catalogue():
    source = Path(generator.INFERENCE_DEFAULTS_SOURCE).read_text()
    assert json.loads(model_defaults.DEFAULTS_JSON) == json.loads(source)
    assert Path(generator.INFERENCE_DEFAULTS_OUTPUT).read_text() == (
        generator.render_inference_defaults(generator._load_contract()))


def test_generator_check_detects_default_catalogue_drift(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'defaults.json'
    source.write_text(Path(generator.INFERENCE_DEFAULTS_SOURCE).read_text())
    monkeypatch.setattr(generator, 'INFERENCE_DEFAULTS_SOURCE', str(source))
    for name in ('PYTHON_OUTPUT', 'TYPESCRIPT_OUTPUT', 'INFERENCE_DEFAULTS_OUTPUT'):
        monkeypatch.setattr(generator, name, str(tmp_path / Path(getattr(generator, name)).name))
    monkeypatch.setattr(sys, 'argv', ['generator'])
    assert generator.main() == 0
    monkeypatch.setattr(sys, 'argv', ['generator', '--check'])
    assert generator.main() == 0
    data = json.loads(source.read_text())
    data['groups'][0]['inference_profile']['source'] = 'official-revised'
    source.write_text(json.dumps(data))
    assert generator.main() == 1
    assert 'generated_inference_defaults.py' in capsys.readouterr().err
    monkeypatch.setattr(sys, 'argv', ['generator'])
    assert generator.main() == 0
    monkeypatch.setattr(sys, 'argv', ['generator', '--check'])
    assert generator.main() == 0


@pytest.mark.parametrize('previous', [False, True], ids=['current', 'superseded'])
def test_generator_rejects_defaults_outside_its_schema(tmp_path, monkeypatch, previous):
    group = {'creator_id': 'example', 'model_ids': ['example-model'],
             'inference_profile': {'reasoning': {'support': 'unsupported'}}}
    invalid = {'reasoning': {'preserve_history': 'future_replay'}}
    if previous:
        group['previous_profiles'] = [invalid]
    else:
        group['inference_profile'] = invalid
    source = json.dumps({'contract_version': 'tofu.model-inference-defaults/v1', 'groups': [group]})
    path = tmp_path / 'defaults.json'
    path.write_text(source)
    monkeypatch.setattr(generator, 'INFERENCE_DEFAULTS_SOURCE', str(path))
    with pytest.raises(ValidationError):
        generator.render_inference_defaults(generator._load_contract())
    # The runtime boundary also remains strict if an invalid artifact is loaded.
    monkeypatch.setattr(model_defaults, 'DEFAULTS_JSON', source)
    with pytest.raises(ModelRoutingError, match='invalid inference profile'):
        model_defaults.current_model_defaults()
