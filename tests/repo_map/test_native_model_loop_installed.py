"""Opt-in full model/tool smoke from a built wheel, without checkout imports."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.api


@pytest.mark.parametrize('repository_kind', ['git', 'plain', 'byte_paths', 'invalid_configuration', 'held_history', 'installed_grammars'])
def test_installed_native_model_and_repository_cycle(tmp_path, repository_kind):
    installed = os.environ.get('TOFU_NATIVE_INSTALLED_AGENT')
    dependencies = os.environ.get('TOFU_NATIVE_INSTALLED_DEPENDENCY_PATH')
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not installed or not dependencies or not archive:
        pytest.skip('explicit installed agent, dependency path and native archive required')
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tests.tasks_pkg.test_native_application import profile

    configuration = profile()
    configuration['model_control']['pricing'] = False
    configuration['model_counter'].update(input_bytes=1<<20, text_bytes=1<<20, tool_json_bytes=1<<20)
    routing = standalone_model_routing_envelope(base_url='http://127.0.0.1:1/v1')
    routing['model_routing']['models'][0]['context_window'] = 131072
    routing['model_routing']['deployments'][0]['max_output_tokens'] = 1024
    if repository_kind == 'invalid_configuration':
        routing['model_routing']['models'][0]['inference_profile'] = 'invalid profile'
    fixture = tmp_path/'provider.json'
    fixture.write_text(json.dumps(dict(profile=configuration, routing=routing)))
    source = Path(__file__).parents[1]/'support/native_repository_installed_smoke.py'
    script = tmp_path/'installed_smoke.py'
    script.write_bytes(source.read_bytes())
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([installed, dependencies]))
    # Prove SDK request admission does not need the CLI's global isolation knob.
    environment.pop('TOFU_DISABLE_CONFIGURED_SLOTS', None)
    result = subprocess.run([
        os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), '-S', str(script),
        installed, str(fixture), 'git' if repository_kind in {'invalid_configuration', 'installed_grammars'} else repository_kind,
        'installed' if repository_kind == 'installed_grammars' else 'external'],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        timeout=20 if repository_kind == 'invalid_configuration' else 90)
    if repository_kind == 'invalid_configuration':
        assert result.returncode == 1 and 'inference_profile' in result.stderr, result.stdout + result.stderr
        return  # Returning within the deadline proves the non-daemon provider joined.
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['model_requests'] == 4 and report['sdk_runs'] == 2
    assert report['joined_scan_children'] == (2 if repository_kind in {'git', 'installed_grammars'} else 4)
    assert report['grammar_selection'] == ('installed' if repository_kind == 'installed_grammars' else 'external')
    assert report['scan_images'] == 1
    assert report['retained_code_after_close'] == 0
    assert report['source_or_benchmark_imports'] is False
    if repository_kind == 'held_history':
        assert report['history_jobs_joined'] == 2
        assert report['close_withheld_while_history'] is True
