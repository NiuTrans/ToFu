"""One resource defaults authority, generated bootstrap and acyclic owners."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS
from lib.resource_control.contract import catalog_document

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_generated_bootstrap_matches_complete_catalog():
    subprocess.run([sys.executable, str(ROOT / 'scripts/gen_resource_policy.py'), '--check'], check=True)
    settings = catalog_document()['launch_settings']
    assert settings
    assert 'TOFU_PRODUCTION_LLM_FANOUT' not in settings
    for mode, defaults in RESOURCE_FALLBACKS.items():
        assert defaults == {key: spec['defaults'][mode] for key, spec in settings.items()}
    resources = {spec['id']: spec for spec in catalog_document()['resources']}
    def visit(key, path):
        assert key not in path, 'cyclic resource dependency'
        assert key in resources, 'undeclared dependency'
        for dependency in resources[key]['dependencies']:
            visit(dependency, path | {key})
    for key in resources:
        visit(key, set())


def test_importing_policy_does_not_load_optional_domains():
    script = '''import sys
from lib.resource_control.runtime import launch_policy
launch_policy({})
assert 'lib.llm_dispatch.dispatcher' not in sys.modules
assert 'lib.storage.v2_client' not in sys.modules
assert 'lib.swarm.execution_gate' not in sys.modules
'''
    subprocess.run([sys.executable, '-c', script], cwd=ROOT, check=True)


def test_explicit_operator_capacity_is_preserved():
    from lib.resource_control.runtime import launch_policy
    policy = launch_policy({'TOFU_AGENT_WORKERS': '2', 'TOFU_MAX_INFLIGHT_TASKS': '2',
                            'TOFU_PROCESS_RSS_RECYCLE_MB': '4096',
                            'TOFU_EPHEMERAL_SLOT_CAPACITY': '128'})
    assert policy['root_maximum'] == 2
    assert 'route_capacity' not in policy
    assert launch_policy({'TOFU_DEPLOYMENT_MODE': 'distributed'})['automatic'] is False


def test_compile_memory_limit_requires_an_explicit_operator_choice():
    settings = catalog_document()['launch_settings']
    assert 'TOFU_PAPER_BUILD_COMPILE_MAX_MIB' not in settings
    for defaults in RESOURCE_FALLBACKS.values():
        assert 'TOFU_PAPER_BUILD_COMPILE_MAX_MIB' not in defaults
