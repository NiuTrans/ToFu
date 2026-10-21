"""Pytest's declared first-party sources cannot come from a private editable.

Run the real bootstrap in disposable synthetic checkouts. Only the package
fixtures and a hostile editable finder are synthetic; no shipped source is
mutated and no installed first-party implementation can supply a green result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_MEMBERS = (
    ('tofu_search', ('packages', 'tofu-search')),
    ('tofu_trading', ('plugins', 'tofu-trading')),
    ('tofu_environments', ('packages', 'tofu-environments')),
)

_PROBE = r'''
import importlib
from importlib.abc import MetaPathFinder
from importlib.machinery import PathFinder
import importlib.util
import json
from pathlib import Path
import sys

checkout, private = map(Path, sys.argv[1:3])
preload = sys.argv[3]
names = ('tofu_search', 'tofu_trading', 'tofu_environments')
sys.path.insert(0, str(checkout))

class PoisonedEditableFinder(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split('.')
        if parts[0] not in (*names, 'tofu_unregistered'):
            return None
        parent = private if len(parts) == 1 else private.joinpath(*parts[:-1])
        return PathFinder.find_spec(fullname, [str(parent)])

# A local source authority must work even before a hostile editable finder,
# not just before the ordinary PathFinder in the default host ordering.
sys.meta_path.insert(0, PoisonedEditableFinder())
if preload:
    importlib.import_module(preload + '.probe')

spec = importlib.util.spec_from_file_location(
    'synthetic_test_bootstrap', checkout / 'tests' / 'conftest.py')
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)

observed = {}
for name in names:
    package = importlib.import_module(name)
    probe = importlib.import_module(name + '.probe')
    observed[name] = {'package': package.__file__, 'probe': probe.__file__}
    try:
        importlib.import_module(name + '.private_only')
    except ModuleNotFoundError as error:
        observed[name]['missing_name'] = error.name
    else:
        observed[name]['missing_name'] = None
observed['tofu_search']['config'] = importlib.import_module('tofu_search.config').__file__
observed['unregistered'] = importlib.import_module('tofu_unregistered.probe').__file__
print(json.dumps(observed))
'''


def _write_package(root: Path, name: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('"""Synthetic member."""\n', encoding='utf-8')
    (package / 'probe.py').write_text('VALUE = "synthetic"\n', encoding='utf-8')
    if name == 'tofu_search':
        (package / 'config.py').write_text(
            'class SearchConfig:\n    pass\n'
            '_global_config = SearchConfig()\n', encoding='utf-8')


@pytest.fixture
def synthetic_checkout(tmp_path):
    checkout = tmp_path / 'checkout'
    private = tmp_path / 'private-editable'
    tests = checkout / 'tests'
    tests.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / 'conftest.py', tests / 'conftest.py')
    (tests / '__init__.py').write_text('', encoding='utf-8')
    # Bootstrap's unrelated PDF policy must not import any real host source.
    (checkout / 'runtime_guards.py').write_text(
        'def install_pymupdf_classic_policy():\n    pass\n', encoding='utf-8')
    for name, parts in (*_MEMBERS, ('tofu_unregistered', ('packages', 'tofu-unregistered'))):
        member = checkout.joinpath(*parts)
        _write_package(member, name)
        _write_package(private, name)
        (private / name / 'private_only.py').write_text('PRIVATE = True\n', encoding='utf-8')
    return checkout, private


def _bootstrap(synthetic_checkout, *, preload=''):
    checkout, private = synthetic_checkout
    # -I ignores ambient PYTHONPATH/user-site; only the synthetic checkout is
    # added by the probe. Keep real pytest/Quart dependencies, but no inherited
    # production credentials, project paths, or lifecycle authority.
    return subprocess.run(
        [sys.executable, '-I', '-B', '-c', _PROBE, str(checkout), str(private), preload],
        cwd=checkout,
        env={
            'PATH': os.environ.get('PATH', ''),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        },
        capture_output=True, text=True, timeout=30,
    )


def _observed(synthetic_checkout):
    completed = _bootstrap(synthetic_checkout)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


def test_shipped_member_origins_override_poisoned_editable(synthetic_checkout):
    checkout, _ = synthetic_checkout
    observed = _observed(synthetic_checkout)
    expected = {
        name: {
            'package': str(checkout.joinpath(*parts, name, '__init__.py')),
            'probe': str(checkout.joinpath(*parts, name, 'probe.py')),
        }
        for name, parts in _MEMBERS
    }
    actual = {
        name: {key: observed[name][key] for key in ('package', 'probe')}
        for name, _ in _MEMBERS
    }
    assert actual == expected
    assert observed['tofu_search']['config'] == str(
        checkout / 'packages' / 'tofu-search' / 'tofu_search' / 'config.py')


def test_private_editable_cannot_supply_missing_member_children(synthetic_checkout):
    observed = _observed(synthetic_checkout)

    assert {name: observed[name]['missing_name'] for name, _ in _MEMBERS} == {
        name: name + '.private_only' for name, _ in _MEMBERS
    }


def test_unregistered_member_is_not_discovered_by_a_source_walk(synthetic_checkout):
    _, private = synthetic_checkout
    observed = _observed(synthetic_checkout)

    assert observed['unregistered'] == str(private / 'tofu_unregistered' / 'probe.py')


@pytest.mark.parametrize('name,parts', _MEMBERS)
def test_preimported_private_member_is_refused(synthetic_checkout, name, parts):
    checkout, private = synthetic_checkout

    completed = _bootstrap(synthetic_checkout, preload=name)

    assert completed.returncode != 0, completed.stdout
    assert 'first-party test source' in completed.stderr.lower()
    assert 'already imported' in completed.stderr.lower()
    assert name in completed.stderr and str(private / name) in completed.stderr
    assert str(checkout.joinpath(*parts, name)) in completed.stderr


@pytest.mark.parametrize('name,parts', _MEMBERS)
@pytest.mark.parametrize('malformation', [
    'missing-member', 'member-file', 'missing-package', 'missing-initializer',
    'escaping-member', 'escaping-module',
])
def test_malformed_declared_sources_fail_closed(
        synthetic_checkout, name, parts, malformation):
    checkout, private = synthetic_checkout
    member = checkout.joinpath(*parts)
    package = member / name
    if malformation == 'missing-member':
        shutil.rmtree(member)
    elif malformation == 'member-file':
        shutil.rmtree(member)
        member.write_text('not a directory', encoding='utf-8')
    elif malformation == 'missing-package':
        shutil.rmtree(package)
    elif malformation == 'missing-initializer':
        (package / '__init__.py').unlink()
    elif malformation == 'escaping-member':
        shutil.rmtree(member)
        member.symlink_to(private, target_is_directory=True)
    else:
        (package / 'probe.py').unlink()
        (package / 'probe.py').symlink_to(private / name / 'probe.py')

    completed = _bootstrap(synthetic_checkout)

    assert completed.returncode != 0, completed.stdout
    assert 'first-party test source' in completed.stderr.lower()
    assert name in completed.stderr and str(member) in completed.stderr
