"""Built-in inference facts keep the running server's schema/source revision."""
import importlib
import json
from pathlib import Path
import sys
import uuid

import pytest

from lib.model_routing import model_defaults
from lib.model_routing.domain import ModelRef
from lib.runtime.pinned_import_source import PinnedImportSource

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('warm', [False, True], ids=['first-use-after-merge', 'already-used'])
@pytest.mark.parametrize('change', ['new-enum', 'deleted'])
def test_defaults_keep_pinned_revision_when_checkout_changes(tmp_path, monkeypatch, warm, change):
    package_name = f'defaults_snapshot_{uuid.uuid4().hex}'
    package = tmp_path / package_name
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'domain.py').write_text('from lib.model_routing.domain import ModelRef\n')
    (package / 'inference.py').write_text(
        'from lib.model_routing.inference import '
        'profile_from_metadata, resolve_inference_profile, validate_inference_profile\n')
    (package / 'model_defaults.py').write_text(Path(model_defaults.__file__).read_text())
    data = {'contract_version': 'tofu.model-inference-defaults/v1', 'groups': [{
        'creator_id': 'example', 'model_ids': ['example-model'],
        'inference_profile': {'reasoning': {'support': 'unsupported'}},
    }]}
    data_path = tmp_path / 'model_info' / 'model_inference_defaults.json'
    data_path.parent.mkdir()
    generated_path = package / 'generated_inference_defaults.py'

    def write_defaults():
        source = json.dumps(data)
        data_path.write_text(source)
        generated_path.write_text(f'DEFAULTS_JSON = {source!r}\n')

    write_defaults()
    monkeypatch.syspath_prepend(str(tmp_path))
    source = PinnedImportSource([tmp_path])
    source.install()
    try:
        importlib.import_module(package_name)
        source.join()
        ref = ModelRef('example', 'example-model')
        expected = ({'reasoning': {'support': 'unsupported'}}, ())
        if warm:
            owner = importlib.import_module(f'{package_name}.model_defaults')
            assert owner.current_model_defaults().resolve(ref) == expected
        if change == 'deleted':
            data_path.unlink()
            generated_path.unlink()
        else:
            # A newer checkout can declare a vocabulary the boot schema lacks.
            data['groups'][0]['inference_profile']['reasoning']['preserve_history'] = 'future_replay'
            write_defaults()
        importlib.invalidate_caches()
        owner = importlib.import_module(f'{package_name}.model_defaults')
        assert owner.current_model_defaults().resolve(ref) == expected
    finally:
        source.join()
        source.uninstall()
        for name in list(sys.modules):
            if name.partition('.')[0] == package_name:
                del sys.modules[name]
