"""The packaged manifest is inert, detached data, usable without Tofu or ML deps."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tofu_search.generated_local_models import CATALOG_SHA256, local_model_artifacts

pytestmark = pytest.mark.unit


def test_packaged_artifact_identity_and_digest_are_stable():
    catalog = local_model_artifacts()
    encoded = (json.dumps(catalog, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()
    assert hashlib.sha256(encoded).hexdigest() == CATALOG_SHA256
    artifact = catalog['artifacts'][0]
    assert artifact['model']['registration_state'] == 'ready'
    assert artifact['model']['capabilities'] == ['reranking']
    assert artifact['source']['revision'] == 'e61197ed45024b0ed8a2d74b80b4d909f1255473'
    assert not any(key in catalog for key in ('providers', 'credentials', 'offerings'))
    catalog['artifacts'].clear()
    assert local_model_artifacts()['artifacts']


def test_manifest_executes_without_host_or_optional_dependencies(tmp_path):
    # Copy the artifact outside both repositories; -I -S removes installed
    # packages and environment import paths. Only the stdlib remains available.
    from tofu_search import generated_local_models

    target = tmp_path / 'generated_local_models.py'
    target.write_bytes(Path(generated_local_models.__file__).read_bytes())
    script = (
        'import runpy, sys\n'
        'module = runpy.run_path(sys.argv[1])\n'
        "assert module['local_model_artifacts']()['artifacts']\n"
        "assert not {'lib', 'torch', 'transformers', 'jsonschema'} & set(sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, '-I', '-S', '-c', script, str(target)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
