"""Reuse verified lane results and require real execution when attributing failures."""
from __future__ import annotations

import pytest

from tests.project_mod.test_landing_verification import (
    SELECTOR, commit, feature as feature, git,
    private_local_root as private_local_root, shared as shared, verify, write,
)

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_calc.py', 'tests/test_report.py', 'tests/test_absent.py'}


def test_verification_announces_its_checkout_and_log_before_starting_tests(
        shared, tmp_path, monkeypatch, capsys):
    from lib.project_mod import landing_verification as verification
    log = tmp_path / 'this-run.log'
    selector = verification._load_selector(shared)
    original = verification.subprocess.Popen
    started = []

    def start(*args, **kwargs):
        if getattr(kwargs.get('stdout'), 'name', None) not in (log, str(log)):
            return original(*args, **kwargs)
        receipt = capsys.readouterr().err
        assert str(shared) in receipt
        assert str(log) in receipt
        assert '1 selection' in receipt
        started.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(verification.subprocess, 'Popen', start)
    run = verification._run(verification._project_interpreter(shared), shared,
                            selector, ['tests/test_calc.py'], log)
    assert run.usable and not run.failed
    assert started, 'the real pytest subprocess must exercise the receipt assertion'


@pytest.mark.parametrize('summary', ['interleaved', 'missing'])
def test_failure_receipts_survive_corrupted_console_summary(
        shared, tmp_path, monkeypatch, summary):
    from lib.project_mod import landing_verification as verification
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    log = tmp_path / 'noisy-run.log'
    original = verification.subprocess.Popen
    corrupted = []

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        if getattr(kwargs.get('stdout'), 'name', None) not in (log, str(log)):
            return process
        wait = process.wait

        def complete(*args, **kwargs):
            result = wait(*args, **kwargs)
            lines = log.read_text().splitlines(keepends=True)
            changed = []
            for line in lines:
                if line.startswith('FAILED tests/test_calc.py::test_scale'):
                    corrupted.append(True)
                    if summary == 'missing':
                        continue
                    line = line.replace('test_scale',
                        'test_scale2026-11-07 INFO background writer', 1)
                changed.append(line)
            log.write_text(''.join(changed))
            return result

        process.wait = complete
        return process

    monkeypatch.setattr(verification.subprocess, 'Popen', start)
    run = verification._run(verification._project_interpreter(shared), shared,
        verification._load_selector(shared), ['tests/test_calc.py'], log)
    assert run.usable
    assert run.failed == ('tests/test_calc.py::test_scale',)
    assert corrupted, 'the real failure summary must be damaged by this fault injection'


@pytest.mark.parametrize('observations', [(), ({
    'kind': 'selection', 'collected': ['tests/test_calc.py::test_scale'],
    'executed': [],
},)])
def test_console_failure_survives_missing_worker_completion(observations):
    from lib.project_mod import landing_verification as verification
    output = ('==== short test summary info ====\n'
              'FAILED tests/test_calc.py::test_scale - worker crashed\n')
    assert verification._failed_ids(output, observations) == (
        'tests/test_calc.py::test_scale',)


def test_recorded_lane_result_is_reused_by_merged_verification(shared, feature, monkeypatch):
    from lib.project_mod import landing_verification as verification
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(feature, 'equivalent calculation')
    assert verification.main(['record', 'tests/test_calc.py', '--into', str(feature)]) == 0

    def repeated(*args, **kwargs):
        pytest.fail('the committed lane already verified these unchanged inputs')

    monkeypatch.setattr(verification, '_run', repeated)
    verdict = verify(shared, 'feature')
    assert (verdict.status, verdict.selected, verdict.skipped_files) == ('passed', (), 1)


@pytest.mark.parametrize('previous', ['skipped', 'passed'])
def test_cached_no_failure_does_not_prove_current_baseline_passes(
        shared, feature, monkeypatch, previous):
    from lib.project_mod import landing_verification as verification
    monkeypatch.delenv('EXERCISE_CASE', raising=False)
    inactive = "pytest.skip('optional case')" if previous == 'skipped' else 'return'
    source = ('import os\nimport pytest\nfrom pkg.calc import scale\n'
              'def test_scale():\n'
              "    if not os.environ.get('EXERCISE_CASE'):\n"
              f'        {inactive}\n'
              '    assert scale(0) == 1\n')
    write(shared, 'tests/test_calc.py', source)
    commit(shared, 'record a case without an observed failure')
    git(feature, 'merge', '--ff-only', 'trunk')
    recorded = verification.record_tests(shared, ['tests/test_calc.py'])
    assert (recorded['recorded'], recorded['failed']) == (0 if previous == 'skipped' else 1, 0)
    write(feature, 'tests/test_calc.py', source + '# unrelated comment\n')
    commit(feature, 'the assertion itself is unchanged')
    monkeypatch.setenv('EXERCISE_CASE', '1')

    verdict = verify(shared, 'feature')

    assert verdict.status == 'passed'
    assert verdict.preexisting == ('tests/test_calc.py::test_scale',)
    assert verdict.regressions == ()


def test_concurrent_source_invalidates_a_recorded_lane_result(shared, feature):
    from lib.project_mod import landing_verification as verification
    write(feature, 'pkg/report.py',
          'from pkg.calc import scale\n\ndef report():\n    return scale(2)\n')
    write(feature, 'tests/test_report.py',
          'from pkg.report import report\n\ndef test_report():\n    assert report() == 4\n')
    commit(feature, 'new report')
    assert verification.record_tests(feature, ['tests/test_report.py'])['recorded'] == 1
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(shared, 'changed calculation')

    verdict = verify(shared, 'feature')
    assert verdict.status == 'regressed'
    assert 'tests/test_report.py::test_report' in verdict.regressions


@pytest.mark.parametrize('changed_identity', ['function', 'parameter'])
def test_missing_baseline_case_falls_back_and_still_reports_the_regression(
        shared, feature, monkeypatch, changed_identity):
    from lib.project_mod import landing_verification as verification
    if changed_identity == 'function':
        source = (feature / 'tests/test_calc.py').read_text()
        source += '\ndef test_new_case():\n    assert scale(0) == 1\n'
        nodeid = 'tests/test_calc.py::test_new_case'
    else:
        write(shared, 'tests/test_calc.py',
              'import pytest\nfrom pkg.calc import scale\n'
              '@pytest.mark.parametrize("value", [0], ids=["old"])\n'
              'def test_scale(value):\n    assert scale(value) == 0\n')
        commit(shared, 'old parametrization')
        git(feature, 'merge', '--ff-only', 'trunk')
        source = ((feature / 'tests/test_calc.py').read_text()
                  .replace('ids=["old"]', 'ids=["new"]')
                  .replace('== 0', '== 1'))
        nodeid = 'tests/test_calc.py::test_scale[new]'
    write(feature, 'tests/test_calc.py', source)
    commit(feature, 'new failing case identity')
    calls = []
    original = verification._run

    def observed(python, root, selector, tests, log):
        if log.name in ('tip.log', 'tip-files.log'):
            calls.append(list(tests))
        return original(python, root, selector, tests, log)

    monkeypatch.setattr(verification, '_run', observed)
    verdict = verify(shared, 'feature')
    assert verdict.status == 'regressed'
    assert verdict.regressions == (nodeid,)
    assert calls == [[nodeid], ['tests/test_calc.py']]


def test_a_deselected_confirmation_is_not_reported_as_flaky(shared, feature, tmp_path):
    counter = tmp_path / 'collections'
    selector = SELECTOR.replace("*existing, '-q'", "*existing, '-m', 'not slow', '-q'")
    write(feature, 'scripts/test_select.py', selector)
    write(feature, 'tests/test_calc.py',
          'from pathlib import Path\nimport pytest\nfrom pkg.calc import scale\n'
          f'counter = Path({str(counter)!r})\n'
          'previous = int(counter.read_text()) if counter.exists() else 0\n'
          'counter.write_text(str(previous + 1))\n'
          '@(pytest.mark.slow if previous else pytest.mark.unit)\n'
          'def test_changes_marker():\n    assert scale(0) == 1\n')
    commit(feature, 'a failing case becomes deselected during confirmation')
    verdict = verify(shared, 'feature')
    assert verdict.status == 'unavailable'
    assert verdict.flaky == ()
    assert 'receipt' in verdict.evidence


def test_partial_confirmation_preserves_a_proven_regression_without_inventing_flakiness(
        tmp_path, monkeypatch):
    from lib.project_mod import landing_verification as verification
    cases = ('tests/test_calc.py::first', 'tests/test_calc.py::unexecuted')
    monkeypatch.setattr(verification, '_known_at_tip',
                        lambda *args: ({case: 'passed' for case in cases}, []))
    monkeypatch.setattr(verification, '_run', lambda *args:
                        verification._Run(False, (cases[0],), 'second case has no receipt'))
    verdict = verification._attribute(tmp_path, 'head', 'python', None,
        verification._Run(True, cases, ''), tmp_path, tmp_path, {}, {}, None, None)
    assert verdict.status == 'regressed'
    assert verdict.regressions == (cases[0],)
    assert verdict.flaky == ()


def test_record_cli_reports_failing_tests(shared, capsys):
    from lib.project_mod import landing_verification as verification
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(shared, 'broken calculation')
    assert verification.main(['record', 'tests/test_calc.py', '--into', str(shared)]) == 1
    output = capsys.readouterr()
    assert '1 failing' in output.out
    assert 'tests/test_calc.py::test_scale' in output.err


@pytest.mark.parametrize('subdirectory', ['', 'pkg'])
def test_record_cli_checks_the_requested_lane_revision(shared, feature, subdirectory):
    from lib.project_mod import landing_verification as verification
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'only the lane is broken')
    assert verification.main(['record', 'tests/test_calc.py', '--into',
                              str(feature / subdirectory)]) == 1


def test_record_cli_rejects_a_partially_unmatched_selection(shared, monkeypatch):
    from lib.project_mod import landing_verification as verification

    def unexpected_run(*args, **kwargs):
        pytest.fail('a partially unmatched selection must be rejected before execution')

    monkeypatch.setattr(verification, '_run', unexpected_run)
    assert verification.main(['record', 'tests/test_calc.py', 'tests/test_absent.py',
                              '--into', str(shared)]) == 1


@pytest.mark.parametrize('selection', ['tests/test_absent.py', 'tests/test_calc.py'])
def test_record_cli_refuses_an_empty_execution(shared, selection):
    from lib.project_mod import landing_verification as verification
    write(shared, 'tests/test_calc.py', 'VALUE = 1\n')
    commit(shared, 'no executable tests')
    assert verification.main(['record', selection, '--into', str(shared)]) == 1


def test_record_cli_reports_an_unusable_runner(shared):
    from lib.project_mod import landing_verification as verification
    write(shared, 'scripts/test_select.py',
          SELECTOR.replace("*existing, '-q'", "*existing, '--invalid-for-pytest', '-q'"))
    commit(shared, 'invalid runner arguments')
    assert verification.main(['record', 'tests/test_calc.py', '--into', str(shared)]) == 1


@pytest.mark.parametrize('variable,value', [
    ('PYTEST_ADDOPTS', '-k never_selected'),
    ('PYTEST_PLUGINS', 'nonexistent_ambient_pytest_plugin'),
])
def test_ambient_pytest_configuration_cannot_hide_a_regression(
        shared, feature, monkeypatch, variable, value):
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'broken calculation')
    monkeypatch.setenv(variable, value)
    verdict = verify(shared, 'feature')
    assert verdict.status == 'regressed'
    assert verdict.regressions == ('tests/test_calc.py::test_scale',)
