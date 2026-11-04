"""The lean portability job must install every explicitly loaded pytest plugin."""
from importlib.metadata import entry_points
from pathlib import Path
import shlex
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_lean_portability_tools_cover_project_pytest_plugins():
    workflow = yaml.safe_load((ROOT / '.github/workflows/installer-portability.yml').read_text())
    job = workflow['jobs']['wheel-admission']
    assert job['env']['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'
    installed = set()
    for step in job['steps']:
        command = shlex.split(step.get('run', ''))
        if command[:4] == ['python', '-m', 'pip', 'install']:
            installed.update(canonicalize_name(Requirement(value).name) for value in command[4:])
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    arguments = shlex.split(config['tool']['pytest']['ini_options']['addopts'])
    plugins = {arguments[index + 1] for index, value in enumerate(arguments) if value == '-p'}
    registered = {entry.module: canonicalize_name(entry.dist.name)
                  for entry in entry_points(group='pytest11')}
    missing = {module: registered.get(module, 'undeclared distribution')
               for module in plugins if not module.startswith('no:')
               and registered.get(module) not in installed}
    assert not missing, f'Lean installer CI cannot collect tests: missing plugin dependencies {missing}'
