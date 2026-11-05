"""Meta-guard: the test suite's own health must not silently degrade.

WHY THIS EXISTS
---------------
This project's JOURNAL records SEVEN separate incidents of one family
("守卫过期家族"): a guard test stopped guarding anything and nobody noticed
until it was found by accident. The charter's verdict is blunt — "守卫死了没人
知道,比没有守卫更危险——因为它制造了『有保护』的错觉."

Every one of those incidents was individually fixed. None of them was
*prevented from recurring*, because the suite had no way to observe its own
health: with ~1160 test files and ~320k lines, "read them one by one" is not a
review anyone can actually perform, so the failure modes accumulated unseen.

``scripts/audit_tests.py`` performs that review mechanically (AST only — no
imports, no execution, ~6s over the whole suite). THIS test is what makes it
binding: a one-way ratchet. Debt can be paid down; it cannot silently
re-accumulate. When a category grows, CI names the category, the file and the
line.

WHAT IS GATED (and why these are the failure modes that matter)
    A0  a test file that does not parse
    A1  a test whose only exit is pytest.skip() — structurally cannot fail, so
        it reports nothing while looking like a check
    A2  the call under test wrapped in bare ``except Exception: pass`` — "it
        worked" and "it exploded" produce the same green result
    A   no assertion anywhere in the test's 1-2 level call closure
    B   ``assert ... or True`` — green by construction
    C   assertions swallowed by a try/except that cannot fail
    D   unconditional skip/xfail — a dead test on life support
    E   a source-anchor guard whose needle is absent from the file it reads:
        THE detector for the 守卫过期家族
    F   a reference to a repo path that no longer exists — how a scanning guard
        degrades into scanning nothing while staying green

    G   a read of shipped source text. Narrow architecture and
        generated-artifact ratchets may remain, but every existing test/source
        identity is grandfathered explicitly and the set may only shrink

HOW TO RESPOND TO A FAILURE HERE
    Fix the finding. Do NOT raise the baseline to make it green — that is the
    exact move the charter forbids ("禁止:为了让数字变绿而上调 BASELINE"),
    and it converts a real signal back into the illusion of protection.
    A baseline may only be regenerated DOWNWARD, deliberately:
        python3 scripts/audit_tests.py --write-baseline
    and the commit must say which findings were fixed to earn the new number.
"""

from __future__ import annotations

import ast
import hashlib
import json
import importlib.util
import os
import subprocess
import sys
from functools import cache
from types import SimpleNamespace

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = pytest.mark.unit

# The audit baseline (tests/audit_baseline.json) is a census of the SOURCE
# tree. The opensource export is structurally different — it deliberately
# strips files the suite legitimately references (scripts/cache_waste_report.py
# et al, see tests/meta/test_gitignore_covers_export_excludes.py), which shows up
# as phantom F-category growth against a baseline those files were counted in.
# The two baseline-comparison guards therefore self-skip on a public build.
_OPENSOURCE = is_opensource_build()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
AUDIT = os.path.join(ROOT, 'scripts', 'audit_tests.py')
BASELINE_PATH = os.path.join(HERE, '..', 'audit_baseline.json')


def _fixture_test_path(name: str) -> str:
    """Build a synthetic test path without declaring a repository anchor."""
    return 'tests/' + name


def _load_audit_module():
    spec = importlib.util.spec_from_file_location('audit_tests_under_test', AUDIT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _census() -> dict:
    """Run the audit tool and return its JSON census."""
    out = os.path.join(
        os.environ.get('TMPDIR', '/tmp'), f'tofu_census_{os.getpid()}.json')
    r = subprocess.run([sys.executable, AUDIT, '--json', out],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, (
        f'scripts/audit_tests.py failed (rc={r.returncode}):\n'
        f'{(r.stderr or r.stdout)[-2000:]}')
    try:
        with open(out, encoding='utf-8') as f:
            return json.load(f)
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


@pytest.fixture(scope='module')
def read_suite_census():
    """Read lazily once per module invocation; later invocations collect afresh."""
    return cache(_census)


def test_audit_tool_is_runnable(read_suite_census):
    """The census tool itself must work — a broken tool would make every other
    assertion here vacuously green."""
    assert os.path.isfile(AUDIT), 'scripts/audit_tests.py is missing'
    census = read_suite_census()
    assert census['files'] > 500, (
        f"census only saw {census['files']} test files — it is not reaching the "
        f'suite (expected >1000). A collapsed scan makes this whole guard inert.')
    assert census['tests'] > 5000, (
        f"census only found {census['tests']} test functions — scan collapsed")


def test_tracked_scan_excludes_worktree_deletions(tmp_path, monkeypatch):
    """An unstaged deletion is absent from the tree pytest will execute.

    ``git ls-files`` still reports it until the deletion is staged/committed;
    the audit must therefore filter by current filesystem state rather than
    manufacture an A0 parse failure that disappears after commit.
    """
    audit = _load_audit_module()
    live = tmp_path / 'tests' / 'test_live.py'
    live.parent.mkdir()
    live.write_text('def test_live():\n    assert True\n', encoding='utf-8')
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    monkeypatch.setattr(
        audit.subprocess,
        'run',
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stderr='', stdout=(f'{_fixture_test_path("test_live.py")}\n'
                    f'{_fixture_test_path("test_deleted.py")}\n'),
        ),
    )

    assert audit._tracked('tests/test_*.py') == [
        _fixture_test_path('test_live.py')]


def test_tracked_scan_refuses_failed_git(monkeypatch):
    """A failed census must never report a clean, empty suite."""
    audit = _load_audit_module()
    monkeypatch.setattr(
        audit.subprocess, 'run',
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout='', stderr='git cannot launch'))
    with pytest.raises(RuntimeError, match='git cannot launch'):
        audit._tracked('tests/test_*.py')


def test_census_refuses_empty_suite(monkeypatch):
    audit = _load_audit_module()
    monkeypatch.setattr(audit, '_tracked', lambda pattern: [])
    monkeypatch.setattr(audit.sys, 'argv', ['audit_tests'])
    with pytest.raises(RuntimeError, match='refusing an empty census'):
        audit.main()


def test_assertion_closure_follows_explicit_neighbor_test_helper(
        tmp_path, monkeypatch):
    """An imported behavioral driver can own the consumer's assertions."""
    audit = _load_audit_module()
    tests_dir = tmp_path / 'tests'
    tests_dir.mkdir()
    (tests_dir / 'test_driver.py').write_text(
        'def _run_subject():\n'
        '    observed = 2 + 2\n'
        '    assert observed == 4\n',
        encoding='utf-8',
    )
    (tests_dir / 'test_consumer.py').write_text(
        'from tests.test_driver import _run_subject\n'
        'def test_delegates_to_driver():\n'
        '    _run_subject()\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(
        _fixture_test_path('test_consumer.py'))

    assert not [item for item in report.findings if item[0] == 'A']


@pytest.mark.parametrize(('source', 'unchecked'), [
    ('from tests import test_driver as driver\n'
     'def test_subject():\n    driver.exercise()\n', False),
    ('import tests.test_driver as driver\n'
     'def test_subject():\n    driver.exercise()\n', False),
    ('def test_subject():\n'
     '    from tests.test_driver import exercise as run\n    run()\n', False),
    ('from tests import test_driver as driver\n'
     '@pytest.mark.parametrize("scenario", [driver.exercise, driver.exercise])\n'
     'def test_subject(scenario):\n    scenario()\n', False),
    ('from tests import test_driver as driver\n'
     'SCENARIOS = (driver.exercise,)\n'
     '@pytest.mark.parametrize("scenario", SCENARIOS)\n'
     'def test_subject(scenario):\n    scenario()\n', False),
    ('from tests import test_driver as driver\n'
     '@pytest.mark.parametrize("scenario", [driver.exercise, driver.unchecked])\n'
     'def test_subject(scenario):\n    scenario()\n', True),
    ('from tests import test_driver as driver\n'
     '@pytest.mark.parametrize("scenario", [driver.exercise, unknown])\n'
     'def test_subject(scenario):\n    scenario()\n', True),
    ('from tests import test_driver as driver\n'
     'def test_subject():\n    driver.unchecked()\n', True),
    ('def test_subject():\n'
     '    from tests.test_driver import unchecked as run\n    run()\n'
     'def unrelated():\n'
     '    from tests.test_driver import exercise as run\n    run()\n', True),
    ('from tests import test_driver as driver\n'
     'def _run():\n    assert 1 == 1\n'
     'def test_subject():\n    driver.delegates_without_assertion()\n', True),
    ('from tests import test_driver as driver\n'
     'def test_subject(driver):\n    driver.exercise()\n', True),
    ('def test_subject():\n'
     '    from tests.test_driver import exercise as run\n'
     '    run = unknown\n    run()\n', True),
    ('class TestOwner:\n'
     '    def _run(self):\n        assert 2 + 2 == 4\n'
     '    def test_subject(self):\n        self._run()\n', False),
    ('class OtherOwner:\n'
     '    def _run(self):\n        assert 2 + 2 == 4\n'
     'class TestOwner:\n'
     '    def _run(self):\n        return 4\n'
     '    def test_subject(self):\n        self._run()\n', True),
])
def test_assertion_closure_resolves_only_owned_explicit_drivers(
        tmp_path, monkeypatch, source, unchecked):
    audit = _load_audit_module()
    tests_dir = tmp_path / 'tests'
    tests_dir.mkdir()
    (tests_dir / 'test_driver.py').write_text(
        'def exercise():\n    assert 2 + 2 == 4\n'
        'def unchecked():\n    return 4\n'
        'def _run():\n    return 4\n'
        'def delegates_without_assertion():\n    _run()\n', encoding='utf-8')
    (tests_dir / 'test_consumer.py').write_text(source, encoding='utf-8')
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_consumer.py'))

    findings = [item for item in report.findings if item[0] == 'A']
    assert bool(findings) is unchecked
    if unchecked:
        assert len(findings) == 1 and 'test_subject()' in findings[0][2]


def test_dead_path_scan_ignores_prose_and_negative_absence_guards(
        tmp_path, monkeypatch):
    """F describes a dead *lookup*, not documentation or an absence pin."""
    audit = _load_audit_module()
    test_path = tmp_path / 'tests' / 'test_retired_owner.py'
    test_path.parent.mkdir()
    test_path.write_text(
        '"""Migrated from lib/retired_owner.py."""\n'
        'from pathlib import Path\n'
        'def test_absent():\n'
        '    assert not Path("lib/retired_owner.py").exists()\n'
        '    source = "current graph"\n'
        '    assert "static/js/retired.js" not in source\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_retired_owner.py'))

    assert not [item for item in report.findings if item[0] == 'F']


def test_dead_path_scan_still_reports_positive_source_lookup(
        tmp_path, monkeypatch):
    audit = _load_audit_module()
    test_path = tmp_path / 'tests' / 'test_missing_owner.py'
    test_path.parent.mkdir()
    test_path.write_text(
        'from pathlib import Path\n'
        'def test_read():\n'
        '    source = Path("lib/missing_owner.py").read_text()\n'
        '    assert source\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_missing_owner.py'))

    assert [item for item in report.findings if item[0] == 'F'] == [
        ('F', 0, 'references missing path lib/missing_owner.py')]


def test_dead_path_scan_accepts_only_live_runtime_adapter_identities(
        tmp_path, monkeypatch):
    audit = _load_audit_module()
    runtime = tmp_path / 'frontend/src/runtime/app-runtime.js'
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        '/* ===== migrated source: core/live.js ===== */\nconst live = 1;\n',
        encoding='utf-8',
    )
    test_path = tmp_path / _fixture_test_path('test_runtime_owner.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from tests._runtime_sections import runtime_section\n'
        'def test_owner():\n'
        '    assert runtime_section("core/live.js")\n'
        '    assert runtime_section("core/missing.js")\n'
        '    labels = ["static/js/core/live.js", '
        '"static/js/core/missing.js"]\n'
        '    assert labels\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_runtime_owner.py'))

    assert [item for item in report.findings if item[0] == 'F'] == [
        ('F', 0, 'references missing path static/js/core/missing.js')]


def test_dead_path_scan_accepts_only_live_native_adapter_identities(
        tmp_path, monkeypatch):
    audit = _load_audit_module()
    owner = tmp_path / 'frontend/src/features/orchestration/task-mode-list.ts'
    owner.parent.mkdir(parents=True)
    owner.write_text('export const live = true;\n', encoding='utf-8')
    test_path = tmp_path / _fixture_test_path('test_native_owner.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from tests._runtime_sections import orchestration_legacy_test_root\n'
        'def test_owner():\n'
        '    paths = ["static/js/task-mode-list.js", '
        '"static/js/task-mode-missing.js"]\n'
        '    assert paths\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_native_owner.py'))

    assert [item for item in report.findings if item[0] == 'F'] == [
        ('F', 0, 'references missing path static/js/task-mode-missing.js')]


@pytest.mark.parametrize('built', [False, True])
def test_gitignored_build_outputs_are_neither_dead_anchors_nor_sources(
        tmp_path, monkeypatch, built):
    """CI audits a clean checkout; a local build must not change the census.

    A gitignored output such as a Vite bundle exists only after a local
    build. Classifying it by on-disk existence made the same test a dead
    anchor (F) in CI and a source-text read (G) in every built tree, so no
    baseline could hold in both.
    """
    audit = _load_audit_module()
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('static/vite/\n', encoding='utf-8')
    test_path = tmp_path / _fixture_test_path('test_build_output.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from pathlib import Path\n'
        'ROOT = Path(__file__).resolve().parents[1]\n'
        'def test_bundle():\n'
        '    bundle = (ROOT / "static/vite/assets/app.js").read_text()\n'
        '    assert "render(" in bundle\n',
        encoding='utf-8',
    )
    if built:
        bundle = tmp_path / 'static' / 'vite' / 'assets' / 'app.js'
        bundle.parent.mkdir(parents=True)
        bundle.write_text('render();\n', encoding='utf-8')
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_build_output.py'))

    assert not [item for item in report.findings if item[0] in {'E', 'F', 'G'}]


def test_shipped_source_read_is_reported_per_source_identity(
        tmp_path, monkeypatch):
    audit = _load_audit_module()
    owner = tmp_path / 'lib' / 'live_owner.py'
    owner.parent.mkdir()
    owner.write_text('def public_contract():\n    return 1\n', encoding='utf-8')
    test_path = tmp_path / _fixture_test_path('test_source_pin.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from pathlib import Path\n'
        'def test_source_pin():\n'
        '    source = (Path(__file__).parents[2] / "lib" / '
        '"live_owner.py").read_text()\n'
        '    assert "def public_contract(" in source\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_source_pin.py'))

    assert [item for item in report.findings if item[0] == 'G'] == [
        ('G', 0, 'source-text read of lib/live_owner.py')]
    assert 'G' in audit.BLOCKING


def test_path_shaped_business_data_is_not_a_source_read(
        tmp_path, monkeypatch):
    audit = _load_audit_module()
    owner = tmp_path / 'lib' / 'live_owner.py'
    owner.parent.mkdir()
    owner.write_text('def public_contract():\n    return 1\n', encoding='utf-8')
    test_path = tmp_path / _fixture_test_path('test_path_data.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_path_label():\n'
        '    expected_path = "lib/live_owner.py"\n'
        '    rendered_label = "Changed lib/live_owner.py"\n'
        '    assert expected_path in rendered_label\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_path_data.py'))

    assert not [item for item in report.findings if item[0] == 'G']


@pytest.mark.parametrize('reader,missing', [
    ('    assert (root / "tests/fixture_created.py").read_text() == "ok"\n', False),
    ('    assert Path("tests/fixture_created.py").read_text() == "ok"\n', True),
    ('def test_other(root):\n'
     '    assert (root / "tests/fixture_created.py").read_text() == "ok"\n', True),
])
def test_created_fixture_paths_do_not_hide_missing_source_anchors(
        tmp_path, monkeypatch, reader, missing):
    audit = _load_audit_module()
    test_path = tmp_path / _fixture_test_path('test_created_paths.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from pathlib import Path\n'
        'def test_fixture(root):\n'
        '    (root / "tests/fixture_created.py").write_text("ok")\n'
        + reader, encoding='utf-8')
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_created_paths.py'))

    findings = [item for item in report.findings if item[0] == 'F']
    assert findings == ([('F', 0, 'references missing path tests/fixture_created.py')]
                        if missing else [])


def test_generated_fixture_source_is_not_a_shipped_source_anchor(tmp_path, monkeypatch):
    audit = _load_audit_module()
    owner = tmp_path / 'frontend/src/i18n/languages.generated.ts'
    owner.parent.mkdir(parents=True)
    owner.write_text('export type UiLanguage = "zh" | "en" | "ja";\n')
    test_path = tmp_path / _fixture_test_path('test_fixture_owner.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_generated_language_union(tmp_path):\n'
        '    source = (tmp_path / "frontend/src/i18n/languages.generated.ts").read_text()\n'
        '    assert \'export type UiLanguage = "zh" | "en" | "fr";\' in source\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_fixture_owner.py'))

    assert not [item for item in report.findings if item[0] in {'E', 'F', 'G'}]


def test_source_provenance_stays_scoped_to_each_test_function(
        tmp_path, monkeypatch):
    audit = _load_audit_module()
    owner_dir = tmp_path / 'lib'
    owner_dir.mkdir()
    (owner_dir / 'alpha.py').write_text('ALPHA_CONTRACT = 1\n')
    (owner_dir / 'beta.py').write_text('BETA_CONTRACT = 2\n')
    test_path = tmp_path / _fixture_test_path('test_scoped_sources.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from pathlib import Path\n'
        'def test_alpha():\n'
        '    source = Path("lib/alpha.py").read_text()\n'
        '    assert "ALPHA_CONTRACT =" in source\n'
        'def test_beta():\n'
        '    source = Path("lib/beta.py").read_text()\n'
        '    assert "BETA_CONTRACT =" in source\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})

    report = audit.analyze_file(_fixture_test_path('test_scoped_sources.py'))

    assert not [item for item in report.findings if item[0] == 'E']
    assert [item for item in report.findings if item[0] == 'G'] == [
        ('G', 0, 'source-text read of lib/alpha.py'),
        ('G', 0, 'source-text read of lib/beta.py'),
    ]


def test_audit_uses_pytest_function_collection_rule():
    audit = _load_audit_module()
    assert audit._is_test_func(ast.parse('def test_collected():\n    assert True').body[0])
    assert not audit._is_test_func(ast.parse('def testnot_collected():\n    assert True').body[0])


def test_uncalled_nested_helper_is_not_an_executed_assertion(tmp_path, monkeypatch):
    audit = _load_audit_module()
    test_path = tmp_path / _fixture_test_path('test_uncalled_helper.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_empty():\n'
        '    def helper():\n'
        '        assert subject() == expected\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    report = audit.analyze_file(_fixture_test_path('test_uncalled_helper.py'))
    assert [item[0] for item in report.findings] == ['A']


@pytest.mark.parametrize('callback', [
    'def helper():\n    raise RuntimeError("unexpected")\n',
    'helper = lambda: pytest.fail("unexpected")\n',
    'class helper:\n    def put_nowait(self, item):\n        raise RuntimeError("unexpected")\n',
])
def test_must_not_call_injected_callback_is_an_executed_contract(
        tmp_path, monkeypatch, callback):
    audit = _load_audit_module()
    test_path = tmp_path / _fixture_test_path('test_no_call_contract.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_skips_forbidden_path(monkeypatch):\n'
        + ''.join('    ' + line + '\n' for line in callback.splitlines())
        + '    monkeypatch.setattr(subject, "forbidden", helper' + ('()' if callback.startswith('class ') else '') + ')\n'
        '    subject.skip_forbidden_path()\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    report = audit.analyze_file(_fixture_test_path('test_no_call_contract.py'))
    assert not [item for item in report.findings if item[0] == 'A']


def test_uncalled_nested_exceptions_are_not_executed_assertions(tmp_path, monkeypatch):
    audit = _load_audit_module()
    test_path = tmp_path / _fixture_test_path('test_uncalled_exceptions.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_uncalled_raise():\n'
        '    def helper():\n'
        '        raise RuntimeError("unused")\n'
        'def test_uncalled_fail():\n'
        '    def helper():\n'
        '        pytest.fail("unused")\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    report = audit.analyze_file(_fixture_test_path('test_uncalled_exceptions.py'))
    assert [item[0] for item in report.findings] == ['A', 'A']


def test_executor_callback_assertions_are_recognized(tmp_path, monkeypatch):
    audit = _load_audit_module()
    test_path = tmp_path / _fixture_test_path('test_executor_callback.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_parallel():\n'
        '    with ThreadPoolExecutor(max_workers=4) as pool:\n'
        '        def worker():\n'
        '            assert subject() == expected\n'
        '        list(pool.map(worker, range(4)))\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    report = audit.analyze_file(_fixture_test_path('test_executor_callback.py'))
    assert not [item for item in report.findings if item[0] == 'A']


def test_approx_without_comparison_is_not_an_assertion(tmp_path, monkeypatch):
    audit = _load_audit_module()
    test_path = tmp_path / _fixture_test_path('test_uncounted_approx.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'def test_empty():\n'
        '    pytest.approx(subject())\n'
        'def test_compared():\n'
        '    assert subject() == pytest.approx(expected)\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    report = audit.analyze_file(_fixture_test_path('test_uncounted_approx.py'))
    assert [item[0] for item in report.findings] == ['A']


def test_local_root_from_file_keeps_real_source_provenance(tmp_path, monkeypatch):
    audit = _load_audit_module()
    owner = tmp_path / 'lib' / 'live_owner.py'
    owner.parent.mkdir()
    owner.write_text('CURRENT_CONTRACT = 1\n')
    test_path = tmp_path / _fixture_test_path('test_local_repo_root.py')
    test_path.parent.mkdir()
    test_path.write_text(
        'from pathlib import Path\n'
        'def test_real_source():\n'
        '    root = Path(__file__).resolve().parents[1]\n'
        '    source = (root / "lib/live_owner.py").read_text()\n'
        '    assert "CURRENT_CONTRACT" in source\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(audit, 'REPO', str(tmp_path))
    monkeypatch.setattr(audit, '_shared_helper_defs', lambda: {})
    report = audit.analyze_file(_fixture_test_path('test_local_repo_root.py'))
    assert not [item for item in report.findings if item[0] == 'E']
    assert [item[0] for item in report.findings] == ['G']


def _finding_digest(census: dict, category: str) -> str:
    finding_keys = sorted({
        f'{relative_path}:{detail}'
        for relative_path, _line, detail
        in census.get('findings', {}).get(category, ())
    })
    return hashlib.sha256(
        '\n'.join(finding_keys).encode('utf-8')).hexdigest()


@pytest.mark.skipif(_OPENSOURCE, reason='audit baseline is source-tree-specific; the export deliberately strips referenced files')
def test_no_category_regressed(read_suite_census):
    """One-way ratchet over every gated failure mode."""
    with open(BASELINE_PATH, encoding='utf-8') as f:
        baseline_document = json.load(f)
    baseline = baseline_document['counts']
    census = read_suite_census()
    counts = census['counts']

    grew = []
    for cat, base in sorted(baseline.items()):
        now = counts.get(cat, 0)
        if now > base:
            grew.append((cat, base, now))

    baseline_g_digest = baseline_document.get(
        'finding_digests', {}).get('G')
    actual_g_digest = _finding_digest(census, 'G')
    g_identity_changed = baseline_g_digest != actual_g_digest

    if grew or g_identity_changed:
        detail = []
        for cat, base, now in grew:
            detail.append(f'  [{cat}] {base} → {now}  (+{now - base})')
        if g_identity_changed:
            detail.append(
                '  [G] source-text read identity set changed; inspect '
                '`python3 scripts/audit_tests.py --category G`')
        raise AssertionError(
            'Test-suite health regressed — new findings in these categories:\n'
            + '\n'.join(detail)
            + '\n\nSee the offending file:line with:\n'
            + '\n'.join(f'  python3 scripts/audit_tests.py --category {c}'
                        for c, _, _ in grew)
            + '\n\nFIX THE FINDING. Do not raise tests/audit_baseline.json to '
              'silence it (charter: 禁止为了让数字变绿而上调 BASELINE) — a '
              'baseline may only move DOWN, and the commit must say which '
              'findings were fixed.')


@pytest.mark.skipif(_OPENSOURCE, reason='audit baseline is source-tree-specific; the export deliberately strips referenced files')
def test_baseline_is_not_loose(read_suite_census):
    """The baseline must track reality DOWNWARD too.

    Without this, a category could be cleaned up and then quietly re-dirtied
    back to the stale (higher) baseline with CI never objecting — the ratchet
    would only be one-way on paper. Deliberately mirrors the tightness check on
    the frontend ratchets (which had to be fixed for exactly this reason: they
    ``skip``ped instead of failing, so a loose baseline was unobservable).
    """
    with open(BASELINE_PATH, encoding='utf-8') as f:
        baseline_document = json.load(f)
    baseline = baseline_document['counts']
    census = read_suite_census()
    counts = census['counts']
    loose = [(c, b, counts.get(c, 0)) for c, b in sorted(baseline.items())
             if counts.get(c, 0) < b]
    assert not loose, (
        'tests/audit_baseline.json is LOOSE — these categories are now cleaner '
        'than the recorded baseline, so regressions would go unnoticed up to '
        'the stale bound:\n'
        + '\n'.join(f'  [{c}] baseline={b}, actual={n}' for c, b, n in loose)
        + '\n\nRegenerate: python3 scripts/audit_tests.py --write-baseline')

    assert baseline_document.get('finding_digests', {}).get('G') == \
        _finding_digest(census, 'G'), (
            'G source-text read identities changed; inspect category G '
            'and regenerate the baseline only after the debt shrank')


def test_census_reader_observes_one_snapshot_then_refreshes_for_a_new_fixture(monkeypatch):
    state = {'revision': 1}
    calls = []

    def census():
        calls.append(state['revision'])
        return dict(state)

    monkeypatch.setitem(globals(), '_census', census)
    first = read_suite_census.__wrapped__()
    assert first() == {'revision': 1}
    state['revision'] = 2
    assert first() == {'revision': 1}
    second = read_suite_census.__wrapped__()
    assert second() == {'revision': 2}
    assert calls == [1, 2]


def test_census_reader_retries_failed_reads_without_retaining_the_error(monkeypatch):
    calls = []
    failure = RuntimeError('census unavailable')

    def census():
        calls.append(True)
        if len(calls) == 1:
            raise failure
        return {'revision': 2}

    monkeypatch.setitem(globals(), '_census', census)
    read = read_suite_census.__wrapped__()
    with pytest.raises(RuntimeError) as caught:
        read()
    assert caught.value is failure
    assert read() == {'revision': 2}
    assert read() == {'revision': 2}
    assert len(calls) == 2


def test_event_contract_scan_targets_resolve():
    """Cross-check on the specific failure mode that motivated this file.

    ``tests/events/test_event_registry.py`` scans a list of emit sites for SSE event
    types. Measured 2026-07-27: 10 of its 21 listed paths had ceased to exist
    (each split into a package), the scanner skipped missing paths silently, and
    HALF the emit surface went unverified while the test stayed green. Its own
    header comment documented an EARLIER instance of the same accident.

    That test now fails on an unresolvable target. This assertion is the
    independent tripwire: if someone reintroduces the silent-skip pattern there,
    the F-category ratchet above catches the dead path, and this catches the
    behaviour directly.
    """
    sys.path.insert(0, ROOT)
    from tests.events.test_event_registry import (
        _BACKEND_FILES, _FRONTEND_FILES, _resolve_scan_targets)

    back_ok, back_bad = _resolve_scan_targets(_BACKEND_FILES)
    front_ok, front_bad = _resolve_scan_targets(_FRONTEND_FILES)
    assert not back_bad and not front_bad, (
        f'event-contract scan targets do not resolve: {back_bad + front_bad}')
    assert len(back_ok) >= 20, (
        f'event-contract backend scan reaches only {len(back_ok)} file(s) — '
        'it has drifted off the real emit surface')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))


@pytest.mark.parametrize('checked', [False, True])
def test_checked_subprocess_is_an_executable_assertion(checked):
    audit = _load_audit_module()
    source = f'def test_child():\n    subprocess.run(["child"], check={checked!r})\n'
    assert audit._body_asserts(ast.parse(source).body[0]) is checked


@pytest.mark.parametrize('observed', [False, True])
def test_worker_exception_collection_requires_observation(observed):
    audit = _load_audit_module()
    source = ('def test_worker():\n    errors = []\n    try:\n        assert subject()\n'
              '    except Exception as error:\n        errors.append(error)\n')
    if observed:
        source += '    assert not errors\n'
    assert bool(audit._swallows(ast.parse(source).body[0])) is not observed


@pytest.mark.parametrize('binding', [
    '    root = _generator_tree(tmp_path)\n',
    '    with tempfile.TemporaryDirectory() as directory:\n'
    '        root = Path(directory)\n',
])
def test_temporary_tree_named_root_is_not_a_repository_read(binding):
    """``root``/``*_ROOT`` name the repository by convention only.

    A generator test may call its temporary tree ``root`` and read what it
    wrote there under a shipped-looking path: that is a fixture. Any binding
    to the repository keeps the same read a source read.
    """
    audit = _load_audit_module()
    read = '    source = (root / "lib/vite_assets.py").read_text()\n'
    temporary = ast.parse('def test_generated(tmp_path):\n' + binding + read)
    repository = ast.parse(
        'def test_shipped():\n'
        '    root = Path(__file__).resolve().parents[2]\n' + read)
    # A ``root`` fixture parameter is not a binding: it still names the
    # repository, as in test_created_fixture_paths_do_not_hide_missing_source_anchors.
    parameter = ast.parse('def test_shipped(root):\n' + read)
    assert audit._shipped_source_reads(temporary) == {}
    assert len(audit._shipped_source_reads(repository)) == 1
    assert len(audit._shipped_source_reads(parameter)) == 1


def test_parsed_json_is_data_but_raw_json_remains_source():
    audit = _load_audit_module()
    expression = 'Path("frontend/src/i18n/locales/en.json").read_text()'
    parsed = ast.parse('data = json.loads(' + expression + ')')
    raw = ast.parse('source = ' + expression)
    assert audit._shipped_source_reads(parsed) == {}
    assert len(audit._shipped_source_reads(raw)) == 1


def test_parsed_json_stays_data_behind_a_reader_helper():
    """Moving ``json.loads(path.read_text())`` into a helper is the same read."""
    audit = _load_audit_module()
    expression = 'Path("frontend/src/i18n/locales/en.json").read_text()'
    parsed = ast.parse(
        'def _table():\n    return json.loads(' + expression + ')\n'
        'def test_table():\n    data = _table()\n')
    raw = ast.parse(
        'def _text():\n    return ' + expression + '\n'
        'def test_text():\n    source = _text()\n')
    assert audit._shipped_source_reads(parsed) == {}
    assert len(audit._shipped_source_reads(raw)) == 1


def test_reader_helper_is_data_only_when_it_parses_whole_files():
    """A helper follows the inline rule: whole-file JSON parses are data.

    A helper that reads shipped source, cuts a literal out of it and parses
    that slice inspects implementation text; its final ``json.loads`` must
    not hide the read. A helper that parses the whole file and returns a
    value derived from the data is no source read.
    """
    audit = _load_audit_module()
    sliced = ast.parse(
        'RUNTIME = Path("frontend/src/runtime/app-runtime.js")\n'
        'def _table():\n'
        '    src = RUNTIME.read_text()\n'
        '    body = src[src.index("{"):]\n'
        '    return json.loads(body)\n'
        'def test_table():\n    data = _table()\n')
    whole_file = ast.parse(
        'MANIFEST = Path("frontend/src/runtime/sections/manifest.json")\n'
        'def _manifest():\n'
        '    return json.loads(MANIFEST.read_text())\n'
        'def test_manifest():\n    data = _manifest()\n')
    derived = ast.parse(
        'PACKAGE = Path("package.json")\n'
        'def _scripts():\n'
        '    package = json.loads(PACKAGE.read_text())\n'
        '    return set(package["scripts"])\n'
        'def test_scripts():\n    names = _scripts()\n')
    assert len(audit._shipped_source_reads(sliced)) == 1
    assert audit._shipped_source_reads(whole_file) == {}
    assert audit._shipped_source_reads(derived) == {}
