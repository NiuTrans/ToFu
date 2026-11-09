"""Cold verification follows transitive Python dependencies without running imports."""
from pathlib import Path

import pytest

from lib.project_mod import impact_selection, landing_verification
from tests.project_mod.test_landing_verification import (
    git, write, commit, shared as shared, private_local_root as private_local_root,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/test_calc.py', 'tests/test_far.py', 'tests/test_user.py',
    'lib/core.py', 'lib/core/helper.py', 'lib/feature/owner.py',
}


def test_cold_selection_follows_relative_imports_and_package_initializers(shared, tmp_path):
    from lib.project_mod.verification_dependencies import expand_changes
    selector = landing_verification._load_selector(ROOT)
    write(shared, 'pkg/calc.py', 'from . import helper\ndef scale(x):\n    return helper.twice(x)\n')
    write(shared, 'pkg/helper.py', 'from .calc import scale\ndef twice(x):\n    return x + x\n')
    write(shared, 'tests/test_far.py', 'def test_far():\n    assert True\n')
    commit(shared, 'transitive dependency cycle')
    tree = impact_selection.Tree.of(shared, 'HEAD')
    cache = tmp_path / 'dependencies.json'
    paths = expand_changes(shared, tree, ['pkg/helper.py'], selector, cache)
    assert {'pkg/helper.py', 'pkg/calc.py', 'tests/test_calc.py'} <= set(paths)
    assert 'tests/test_far.py' not in paths
    assert expand_changes(shared, tree, ['pkg/helper.py'], selector, cache) == paths
    assert 'tests/test_calc.py' in expand_changes(shared, tree, ['pkg/__init__.py'], selector, cache)

    # A removed module remains a referenced missing input in the candidate.
    (shared / 'pkg/helper.py').unlink()
    commit(shared, 'removed helper')
    removed = impact_selection.Tree.of(shared, 'HEAD')
    assert 'tests/test_calc.py' in expand_changes(shared, removed, ['pkg/helper.py'], selector, cache)


def test_corrupt_optional_graph_cache_rebuilds_the_same_scope(shared, tmp_path):
    from lib.project_mod.verification_dependencies import expand_changes
    selector = landing_verification._load_selector(ROOT)
    cache = tmp_path / 'dependencies.json'
    tree = impact_selection.Tree.of(shared, 'HEAD')
    before = expand_changes(shared, tree, ['pkg/calc.py'], selector, cache)
    cache.write_text('{broken')
    assert expand_changes(shared, tree, ['pkg/calc.py'], selector, cache) == before


def test_source_reference_policy_covers_relative_and_from_import_modules():
    selector = landing_verification._load_selector(ROOT)
    refs = selector.source_references('from ..core import helper\nimport serverctl_pkg.lifecycle\n',
                                      'lib/feature/owner.py')
    assert {'lib/core.py', 'lib/core/helper.py', 'lib/__init__.py',
            'serverctl_pkg/lifecycle.py', 'serverctl_pkg/__init__.py'} <= refs


def test_documentation_literals_are_not_transitive_module_execution_edges():
    selector = landing_verification._load_selector(ROOT)
    source = '"""See `docs/TESTING_STRATEGY.md`."""\nPATH = "tests/test_calc.py"\n'
    assert selector.source_references(source, 'tests/conftest.py') == set()
    assert 'docs/TESTING_STRATEGY.md' in selector.refs_of_test_file(source)


def test_affected_importer_does_not_become_a_fictitious_global_input(shared, tmp_path):
    selector = landing_verification._load_selector(ROOT)
    write(shared, 'tests/conftest.py',
          'import pytest\n@pytest.fixture\ndef application():\n    from pkg.calc import scale\n    return scale\n')
    write(shared, 'tests/test_far.py', 'def test_far():\n    assert True\n')
    write(shared, 'tests/test_user.py', 'def test_application(application):\n    assert application(2) == 4\n')
    commit(shared, 'a lazy fixture and unrelated unit test')
    tree = impact_selection.Tree.of(shared, 'HEAD')
    guards = {'tests/meta/' + name: set() for name in selector.GUARD_CORE_BASENAMES}
    index = {**guards, 'tests/test_calc.py': {'pkg/calc.py'}, 'tests/test_far.py': set(),
             'tests/test_user.py': set()}
    reasons = landing_verification._static_selection(selector, index, ['pkg/calc.py'],
        landing_verification._global_inputs(selector), tree, tree, None, shared)
    assert 'tests/test_calc.py' in reasons
    assert 'tests/test_user.py' in reasons
    assert 'tests/test_far.py' not in reasons
    direct = landing_verification._static_selection(selector, index, ['tests/conftest.py'],
        landing_verification._global_inputs(selector), impact_selection.Tree({}), tree, None, shared)
    assert set(direct) == set(index)


@pytest.mark.parametrize('kind', ['autouse', 'hook', 'fixture_chain', 'dynamic_fixture'])
def test_fixture_helpers_hooks_and_dynamic_requests_select_their_consumers(shared, tmp_path, kind):
    from lib.project_mod.verification_dependencies import expand_changes
    selector = landing_verification._load_selector(ROOT)
    source = 'import pytest\ndef build():\n    from pkg.calc import scale\n    return scale\n'
    if kind == 'hook':
        source += 'def pytest_configure(config):\n    build()\n'
    else:
        source += ('@pytest.fixture(autouse=' + str(kind == 'autouse') + ', name="application")\n'
                   'def app():\n    return build()\n'
                   '@pytest.fixture\ndef client(application):\n    return application\n')
    write(shared, 'tests/conftest.py', source)
    write(shared, 'tests/test_far.py', 'def test_far():\n    assert True\n')
    body = ('def test_user(client):\n    assert client(2) == 4\n' if kind == 'fixture_chain' else
            'def test_user(request):\n    name = "application"\n    assert request.getfixturevalue(name)(2) == 4\n')
    write(shared, 'tests/test_user.py', body)
    commit(shared, 'fixture dependency contracts')
    paths = expand_changes(shared, impact_selection.Tree.of(shared, 'HEAD'), ['pkg/calc.py'],
                           selector, tmp_path / 'graph.json')
    assert 'tests/test_user.py' in paths
    assert ('tests/test_far.py' in paths) == (kind in {'autouse', 'hook'})


@pytest.mark.parametrize('declaration', [
    'OPTIONS = {"autouse": True}\n@pytest.fixture(**OPTIONS)\ndef lazy():\n    return build()\n',
    '@pytest.fixture(params=build())\ndef lazy(request):\n    return request.param\n',
])
def test_unknown_registration_and_import_time_fixture_inputs_cover_the_scope(shared, tmp_path, declaration):
    from lib.project_mod.verification_dependencies import expand_changes
    selector = landing_verification._load_selector(ROOT)
    write(shared, 'tests/conftest.py', 'import pytest\ndef build():\n'
          '    from pkg.calc import scale\n    return [scale]\n' + declaration)
    write(shared, 'tests/test_far.py', 'def test_far():\n    assert True\n')
    commit(shared, 'fixture registration inputs')
    selected = expand_changes(shared, impact_selection.Tree.of(shared, 'HEAD'), ['pkg/calc.py'],
                              selector, tmp_path / 'graph.json')
    assert 'tests/test_far.py' in selected


@pytest.mark.parametrize('declaration', [
    'pytest_plugins = "tests.support.semantic_authorities"',
    'pytest_plugins = ["tests.support.semantic_authorities"]',
    'pytest_plugins: tuple = ("tests.support.semantic_authorities",)',
])
def test_declared_plugins_follow_nested_dependencies_without_selecting_unrelated_tests(
        shared, tmp_path, declaration):
    from lib.project_mod.verification_dependencies import expand_changes
    selector = landing_verification._load_selector(ROOT)
    write(shared, 'tests/support/semantic_authorities.py',
          'pytest_plugins = ("tests.support.postgres_cluster",)\n')
    write(shared, 'tests/support/postgres_cluster.py', 'from pkg.calc import scale\n')
    write(shared, 'tests/test_user.py', declaration + '\ndef test_user():\n    assert True\n')
    write(shared, 'tests/test_far.py', 'def test_far():\n    assert True\n')
    commit(shared, 'declared plugin dependencies')
    tree = impact_selection.Tree.of(shared, 'HEAD')
    for changed in ['pkg/calc.py', 'tests/support/postgres_cluster.py']:
        selected = expand_changes(shared, tree, [changed], selector, tmp_path / 'graph.json')
        assert 'tests/test_user.py' in selected
        assert 'tests/test_far.py' not in selected


@pytest.mark.parametrize('owner', ['tests/test_user.py', 'tests/conftest.py'])
def test_unknown_plugin_declarations_conservatively_follow_python_changes(shared, tmp_path, owner):
    from lib.project_mod.verification_dependencies import expand_changes
    selector = landing_verification._load_selector(ROOT)
    write(shared, owner, 'pytest_plugins = discover_plugins()\n')
    write(shared, 'tests/test_far.py', 'def test_far():\n    assert True\n')
    commit(shared, 'unknown plugin dependency')
    selected = expand_changes(shared, impact_selection.Tree.of(shared, 'HEAD'),
                              ['pkg/calc.py'], selector, tmp_path / 'graph.json')
    assert owner in selected
    assert ('tests/test_far.py' in selected) == (owner == 'tests/conftest.py')
