"""CI checks the frozen candidate and attributes failures without publishing Git refs."""
import json
import subprocess
import sys

import pytest

from lib.project_mod import landing_verification
from tests.project_mod.test_landing_verification import (
    git, write, commit, shared as shared, private_local_root as private_local_root,
)

pytestmark = pytest.mark.unit
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_calc.py'}


def test_candidate_check_reports_a_regression_and_preserves_the_callers_tree(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return 3 * x\n')
    commit(shared, 'regression')
    head = git(shared, 'rev-parse', 'HEAD').strip()
    result = landing_verification.check_revision(shared, base)
    assert result['revision'] == head and result['scope'] == 'incremental'
    assert result['head'] == base
    assert result['status'] == 'regressed'
    assert result['regressions'] == ('tests/test_calc.py::test_scale',)
    assert git(shared, 'rev-parse', 'HEAD').strip() == head
    assert git(shared, 'status', '--porcelain').strip() == ''


def test_candidate_check_reuses_matching_whole_file_evidence(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(shared, 'equivalent implementation')
    first = landing_verification.check_revision(shared, base)
    second = landing_verification.check_revision(shared, base)
    assert first['status'] == second['status'] == 'passed'
    assert first['selected'] == ('tests/test_calc.py',)
    assert second['selected'] == () and second['skipped_files'] == 1


def test_check_cli_emits_revision_bound_evidence(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    result = subprocess.run([sys.executable, '-m', 'lib.project_mod.landing_verification',
                             'check', '--base', base, '--into', str(shared), '--json'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['revision'] == base and receipt['status'] == 'skipped'
    assert receipt['scope'] == 'incremental'


def test_unknown_base_cannot_authorize_a_check(shared):
    with pytest.raises(ValueError, match='base'):
        landing_verification.check_revision(shared, 'missing-base')


def test_ci_uses_its_verified_generated_inputs(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, '.gitignore', 'generated-input.txt\n')
    write(shared, 'tests/test_calc.py',
          'from pathlib import Path\ndef test_artifact():\n'
          '    assert Path("generated-input.txt").read_text() == "verified artifact"\n')
    commit(shared, 'artifact consumer')
    write(shared, 'generated-input.txt', 'verified artifact')
    result = landing_verification.check_revision(shared, base)
    assert result['status'] == 'passed'


def test_ci_refuses_dirty_or_unexpected_candidate_sources(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    with pytest.raises(ValueError, match='revision'):
        landing_verification.check_revision(shared, base, expected_revision='0' * 40)
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return 5 * x\n')
    with pytest.raises(ValueError, match='clean'):
        landing_verification.check_revision(shared, base)


def test_changed_ignored_input_cannot_reuse_a_passing_receipt(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, '.gitignore', 'generated-input.txt\n')
    write(shared, 'tests/test_calc.py',
          'from pathlib import Path\ndef test_artifact():\n'
          '    assert Path("generated-input.txt").read_text() == "good"\n')
    commit(shared, 'artifact consumer')
    write(shared, 'generated-input.txt', 'good')
    assert landing_verification.check_revision(shared, base)['status'] == 'passed'
    write(shared, 'generated-input.txt', 'bad')
    second = landing_verification.check_revision(shared, base)
    assert second['status'] == 'regressed', second
    assert second['selected'] == ('tests/test_calc.py',)


def test_creating_a_previously_missing_ignored_input_invalidates_evidence(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, '.gitignore', 'generated-input.txt\n')
    write(shared, 'tests/test_calc.py',
          'from pathlib import Path\ndef test_artifact():\n'
          '    try:\n        value = Path("generated-input.txt").read_text()\n'
          '    except FileNotFoundError:\n        value = "good"\n'
          '    assert value == "good"\n')
    commit(shared, 'optional artifact consumer')
    assert landing_verification.check_revision(shared, base)['status'] == 'passed'
    write(shared, 'generated-input.txt', 'bad')
    assert landing_verification.check_revision(shared, base)['status'] == 'regressed'


def test_ci_directory_receipts_notice_new_ignored_entries(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, '.gitignore', 'inputs/generated.txt\n')
    write(shared, 'inputs/tracked.txt', 'tracked')
    write(shared, 'tests/test_calc.py', 'import os\ndef test_listing():\n'
          '    assert os.listdir("inputs") == ["tracked.txt"]\n')
    commit(shared, 'directory consumer')
    assert landing_verification.check_revision(shared, base)['status'] == 'passed'
    write(shared, 'inputs/generated.txt', 'outside the captured Git tree')
    assert landing_verification.check_revision(shared, base)['status'] == 'regressed'
