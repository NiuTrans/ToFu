"""Opensource export must not ship tests that import modules it dropped.

The public CI collects the shipped tests against the shipped tree. When the
export strips a first-party module (the root ``scripts/`` tree minus the keep
list, ``tofu_db/``, ``bundles/`` …) but keeps a test importing it, the public
lane fails at collection with ModuleNotFoundError — the dominant red class on
rangehow/ToFu through 2026-09. The gate in ``export_pkg._verify`` makes the
invariant mechanical: a shipped test must be collectable against the shipped
tree.
"""

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = (
    'scripts/helper.py', 'scripts/private.py', 'tests/test_retained.py',
)

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


def _make_src(root):
    """Minimal first-party layout the verifier treats as importable roots."""
    (root / 'pkg_core' / 'pricing').mkdir(parents=True)
    (root / 'pkg_core' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'pkg_core' / 'pricing' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'scripts').mkdir()
    (root / 'scripts' / 'gen_x.py').write_text('X = 1\n', encoding='utf-8')
    (root / 'tofu_db').mkdir()
    (root / 'tofu_db' / '__init__.py').write_text('', encoding='utf-8')


def _make_dest(dest, test_sources, with_lib=True, with_gen_x=True):
    tests = dest / 'tests'
    tests.mkdir(parents=True)
    for name, source in test_sources.items():
        (tests / name).write_text(source, encoding='utf-8')
    if with_lib:
        (dest / 'pkg_core' / 'pricing').mkdir(parents=True)
        (dest / 'pkg_core' / '__init__.py').write_text('', encoding='utf-8')
        (dest / 'pkg_core' / 'pricing' / '__init__.py').write_text('', encoding='utf-8')
    if with_gen_x:
        (dest / 'scripts').mkdir(exist_ok=True)
        (dest / 'scripts' / 'gen_x.py').write_text('X = 1\n', encoding='utf-8')


def test_clean_tree_passes(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_ok.py': 'import pkg_core.pricing\nfrom scripts import gen_x\n\n'
                      'def test_x():\n    assert True\n',
    })
    _verify_exported_test_surface(dest, src)


def test_missing_root_module_is_blocked(tmp_path):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {'test_db.py': 'import tofu_db\n'})

    with pytest.raises(ExportTestSurfaceError, match=r'test_db\.py -> tofu_db'):
        _verify_exported_test_surface(dest, src)


def test_missing_second_level_submodule_is_blocked(tmp_path):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'scripts' / 'gen_wire_fixtures.py').write_text('', encoding='utf-8')
    dest = tmp_path / 'dest'
    # scripts/ survives the export, but this one generator does not.
    _make_dest(dest, {'test_wire.py': 'from scripts import gen_wire_fixtures\n'})

    with pytest.raises(ExportTestSurfaceError,
                       match=r'test_wire\.py -> scripts\.gen_wire_fixtures'):
        _verify_exported_test_surface(dest, src)


def test_importorskip_is_tolerated(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_gate.py': "import pytest\n"
                        "pytest.importorskip('tofu_db', reason='optional')\n",
    })
    _verify_exported_test_surface(dest, src)


def test_importorskip_guard_suppresses_later_first_party_imports(tmp_path):
    """The suite's `export` gate: importorskip('export') skips the whole module
    in public trees, so imports BEHIND it (e.g. export_pkg internals) can never
    execute there and must not be flagged."""
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'export.py').write_text('', encoding='utf-8')
    (src / 'export_pkg').mkdir()
    (src / 'export_pkg' / '__init__.py').write_text('', encoding='utf-8')
    (src / 'export_pkg' / '_handoff.py').write_text('', encoding='utf-8')
    dest = tmp_path / 'dest'
    # Neither export.py nor export_pkg/ ships in the public tree.
    _make_dest(dest, {
        'test_handoff.py': (
            "import pytest\n"
            "pytest.importorskip('export', reason='not shipped')\n"
            "import export_pkg._handoff as handoff  # noqa: E402\n"
        ),
    })
    _verify_exported_test_surface(dest, src)


def test_import_before_the_skip_guard_is_still_flagged(tmp_path):
    """Ordering matters: an import placed BEFORE the importorskip line would
    crash collection before the skip can fire."""
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'export.py').write_text('', encoding='utf-8')
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_order.py': (
            "import tofu_db\n"
            "import pytest\n"
            "pytest.importorskip('export', reason='not shipped')\n"
        ),
    })
    with pytest.raises(ExportTestSurfaceError, match=r'test_order\.py -> tofu_db'):
        _verify_exported_test_surface(dest, src)


def test_try_guarded_and_function_level_imports_are_tolerated(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_opt.py': (
            'try:\n'
            '    import tofu_db\n'
            'except ImportError:\n'
            '    tofu_db = None\n'
            'def test_lazy():\n'
            '    import scripts.missing_helper  # deferred, not collection-time\n'
        ),
    })
    _verify_exported_test_surface(dest, src)


def test_type_checking_imports_are_tolerated(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_typing.py': 'from typing import TYPE_CHECKING\n'
                          'if TYPE_CHECKING:\n'
                          '    import tofu_db\n',
    })
    _verify_exported_test_surface(dest, src)


def test_from_import_of_module_attribute_is_not_flagged(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    # `pricing_for_model` is a NAME inside pkg_core/pricing/__init__.py, not a
    # submodule; only the module part must resolve.
    _make_dest(dest, {
        'test_pricing.py': 'from pkg_core.pricing import pricing_for_model\n',
    })
    _verify_exported_test_surface(dest, src)


def test_package_getattr_tolerates_lazy_submodules(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    (src / 'pkg_core' / 'lazy').mkdir()
    (src / 'pkg_core' / 'lazy' / '__init__.py').write_text(
        'def __getattr__(name):\n    raise AttributeError(name)\n', encoding='utf-8')
    dest = tmp_path / 'dest'
    _make_dest(dest, {'test_lazy.py': 'import pkg_core.lazy.deferred\n'})
    (dest / 'pkg_core' / 'lazy').mkdir()
    (dest / 'pkg_core' / 'lazy' / '__init__.py').write_text(
        'def __getattr__(name):\n    raise AttributeError(name)\n', encoding='utf-8')
    _verify_exported_test_surface(dest, src)


def test_third_party_imports_are_ignored(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_deps.py': 'import pytest\nimport numpy\nfrom flask import Flask\n',
    })
    _verify_exported_test_surface(dest, src)


def test_findings_report_every_offending_pair(tmp_path):
    from export import _test_surface_findings

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    _make_dest(dest, {
        'test_a.py': 'import tofu_db\n',
        'test_b.py': 'import tofu_db\nimport scripts.missing_helper\n',
        'conftest.py': 'import tofu_db\n',
    })
    findings = _test_surface_findings(dest, src)
    # Path fragments stay split so the suite-health audit's dead-path scan
    # (category F) does not read these expectations as repo-path references.
    t = 'tests'
    assert sorted(findings) == [
        (t + '/conftest.py', 'tofu_db'),
        (t + '/test_a.py', 'tofu_db'),
        (t + '/test_b.py', 'scripts.missing_helper'),
        (t + '/test_b.py', 'tofu_db'),
    ]


def test_missing_tests_tree_is_a_noop(tmp_path):
    from export import _verify_exported_test_surface

    src = tmp_path / 'src'
    src.mkdir()
    _make_src(src)
    dest = tmp_path / 'dest'
    dest.mkdir()
    _verify_exported_test_surface(dest, src)


@pytest.mark.parametrize('mode', ('internal', 'opensource'))
def test_retained_generator_is_not_gitignored(tmp_path, monkeypatch, mode):
    import subprocess
    import export_pkg._export_core as core
    from export_pkg import _state

    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    source = tmp_path / 'source'
    target = tmp_path / mode
    (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/helper.py').write_text("VALUE = 'RETAINED_SAMPLE'\n")
    target.mkdir()
    (target / '.gitignore').write_text('/scripts/*\n')
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES', {'scripts/helper.py'})
    monkeypatch.setattr(core, '_sanitize_source_opensource',
                        lambda text, rel: text.replace('RETAINED_SAMPLE', 'PUBLIC_SAMPLE'))
    core._restore_export_kept_files(target, mode)
    expected = 'PUBLIC_SAMPLE' if mode == 'opensource' else 'RETAINED_SAMPLE'
    assert (target / 'scripts/helper.py').read_text() == f"VALUE = '{expected}'\n"
    subprocess.run(['git', 'init', '-q'], cwd=target, check=True)
    retained = subprocess.run(['git', 'check-ignore', 'scripts/helper.py'], cwd=target)
    private = subprocess.run(['git', 'check-ignore', 'scripts/private.py'], cwd=target, capture_output=True)
    assert retained.returncode == 1
    assert private.returncode == 0


@pytest.mark.parametrize('keep_helper', (True, False))
def test_internal_export_restores_and_checks_test_dependencies(tmp_path, monkeypatch, keep_helper):
    import export_pkg._export_core as core
    import export_pkg._orchestrate as orchestrate
    from export_pkg import _state
    from export_pkg._verify import ExportTestSurfaceError, _verify_exported_test_surface

    source = tmp_path / 'source'
    (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/helper.py').write_text('VALUE = 1\n')
    target = tmp_path / 'internal'
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(orchestrate, 'ROOT', source)
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES',
                        {'scripts/helper.py'} if keep_helper else set())
    monkeypatch.setattr(_state, '_EXPORT_VERSION', None)
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    monkeypatch.setattr(orchestrate, '_read_version', lambda: '0.18.2')

    def copy_source(mode, dest, **kwargs):
        assert mode == 'internal'
        (dest / 'tests').mkdir(parents=True)
        (dest / 'tests/test_retained.py').write_text('import scripts.helper\n')
        (dest / '.gitignore').write_text('/scripts/*\n')
        return {'copied': 2, 'sanitized': 0}

    checked = []

    def verify(dest):
        _verify_exported_test_surface(dest, source)
        checked.append(dest)

    monkeypatch.setattr(orchestrate, '_export_via_tar_with_sanitize', copy_source)
    monkeypatch.setattr(orchestrate, '_verify_exported_test_surface', verify)
    for name in ('_create_skeleton', '_print_export_summary', '_verify_exported_js_syntax',
                 '_verify_exported_py_syntax', '_verify_exported_py_integrity'):
        monkeypatch.setattr(orchestrate, name, lambda *args: None)

    if keep_helper:
        orchestrate.export_project('internal', target)
        assert (target / 'scripts/helper.py').read_text() == 'VALUE = 1\n'
        assert checked == [target]
    else:
        with pytest.raises(ExportTestSurfaceError, match=r'scripts\.helper'):
            orchestrate.export_project('internal', target)
        assert not checked


@pytest.mark.parametrize('optional_import', (
    'def later():\n    import scripts.gen_x\n',
    'try:\n    import scripts.gen_x\nexcept ImportError:\n    pass\n',
))
def test_optional_sibling_never_waives_mandatory_import(tmp_path, optional_import):
    from export import ExportTestSurfaceError, _verify_exported_test_surface

    source = tmp_path / 'source'
    source.mkdir()
    _make_src(source)
    target = tmp_path / 'target'
    _make_dest(target, {
        'test_required.py': 'import scripts.missing_helper\n' + optional_import,
    })
    with pytest.raises(ExportTestSurfaceError, match=r'scripts\.missing_helper'):
        _verify_exported_test_surface(target, source)


@pytest.mark.parametrize('helper', (
    'scripts/fetch_chatgpt_share.py',
    'scripts/inspect_native_crash.py',
    'scripts/trace_experiment_reads.py',
    'scripts/tofudb_profile_report.py',
    'benchmarks/search_html_reference.py',
    'benchmarks/search_relevance.py',
    'benchmarks/tofudb_context_progression.py',
))
@pytest.mark.parametrize('mode', ('internal', 'opensource'))
def test_portable_diagnostic_test_dependencies_follow_retained_source_policy(
        tmp_path, monkeypatch, helper, mode):
    import export_pkg._export_core as core
    from export_pkg import _lists, _state
    from export_pkg._verify import _verify_exported_test_surface

    source = tmp_path / 'source'
    path = source / helper
    path.parent.mkdir(parents=True)
    path.write_text('VALUE = 1\n')
    target = tmp_path / mode
    (target / 'tests').mkdir(parents=True)
    module = helper.removesuffix('.py').replace('/', '.')
    (target / 'tests/test_portable.py').write_text(f'import {module}\n')
    (target / '.gitignore').write_text('/scripts/*\n/benchmarks/*\n')
    # Select only this diagnostic from the real authority; an absent entry
    # must remain a collection failure, never become a fixture exemption.
    monkeypatch.setattr(core, '_OPENSOURCE_KEEP_FILES',
                        {helper} & _lists._OPENSOURCE_KEEP_FILES)
    monkeypatch.setattr(core, 'ROOT', source)
    monkeypatch.setattr(_state, '_EXPORT_SOURCE_SHA', None)
    core._restore_export_kept_files(target, mode)
    _verify_exported_test_surface(target, source)
    assert (target / helper).read_text() == 'VALUE = 1\n'
    assert not (target / 'export_pkg').exists()


def test_public_installer_contract_does_not_require_private_export_toolchain(tmp_path):
    from pathlib import Path
    from export_pkg._lists import ROOT
    from export_pkg._verify import _verify_exported_test_surface

    target = tmp_path / 'public'
    test = Path('tests/installer/test_install_carried_user_state.py')
    (target / test).parent.mkdir(parents=True)
    (target / test).write_bytes((ROOT / test).read_bytes())
    _verify_exported_test_surface(target, ROOT)
    assert not (target / 'export_pkg').exists()
