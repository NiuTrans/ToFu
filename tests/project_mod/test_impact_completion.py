"""Only completed, non-skipped test files may authorize verification reuse."""
import pytest

_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_a.py', 'tests/test_empty.py'}

from lib.project_mod import impact_selection as impact
from tests.project_mod.test_impact_selection import _project, _write, _observe, _tree, TESTS, REFS

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('body', [
    'pytest.skip("not exercised")',
    'pytest.xfail("not established")',
    'pytest.exit("interrupted")',
])
def test_nonverifying_runs_never_install_pass_records(tmp_path, body):
    project = tmp_path / 'project'
    _project(project)
    _write(project, 'tests/test_a.py', 'import pytest\n\ndef test_not_verified():\n    ' + body + '\n')
    observations = _observe(project, tmp_path)
    records = impact.records_from_observations(observations, TESTS, _tree(project), 'tree', REFS)
    assert 'tests/test_a.py' not in {record.test for record in records}


def test_empty_collection_cannot_install_a_pass_record():
    lines = [{'kind': 'file', 'test': 'tests/test_empty.py', 'collected': [], 'executed': []}]
    assert impact.records_from_observations(
        lines, ['tests/test_empty.py'], impact.Tree({'tests/test_empty.py': 'blob'}), 'tree', {}) == []


def test_absent_input_manifest_preserves_receipts_but_never_dependency_evidence(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    _project(project)
    source = impact.plugin_source()
    monkeypatch.setattr(impact, 'plugin_source', lambda paths=None: source)
    lines = _observe(project, tmp_path)
    assert {line['kind'] for line in lines} == {'selection'}
    assert lines[0]['collected'] == lines[0]['executed']
    assert impact.records_from_observations(lines, TESTS, _tree(project), 'tree', REFS) == []


@pytest.mark.parametrize('entry', ['def _relative(path)', 'def _observe(bucket: _Bucket, event, args)'])
def test_observation_errors_disable_dependency_reuse_but_preserve_receipts(tmp_path, monkeypatch, entry):
    project = tmp_path / 'project'
    _project(project)
    source = impact.plugin_source(_tree(project).blobs)
    start = source.index('\n', source.index(entry)) + 1
    broken = source[:start] + '    raise RuntimeError("observation fault")\n' + source[start:]
    monkeypatch.setattr(impact, 'plugin_source', lambda paths=None: broken)
    lines = _observe(project, tmp_path)
    assert {line['kind'] for line in lines} == {'selection'}
    assert lines[0]['observation_error'] == 'RuntimeError'
    assert lines[0]['collected'] == lines[0]['executed']
    assert impact.records_from_observations(lines, TESTS, _tree(project), 'tree', REFS) == []


@pytest.mark.parametrize('operation', ['read', 'list'])
@pytest.mark.parametrize('relocation', ['chdir', 'symlink'])
def test_filesystem_observations_follow_current_path_identity(tmp_path, operation, relocation):
    project = tmp_path / 'project'
    _project(project)
    outside = tmp_path / 'outside'
    outside.mkdir()
    for root in (project, outside):
        if operation == 'read':
            _write(root, 'payload', 'expected')
        else:
            _write(root, 'payload/expected', '')
    path = 'payload' if relocation == 'chdir' else 'alias'
    expression = f'pathlib.Path({path!r}).read_text()' if operation == 'read' else f'os.listdir({path!r})'
    expected = repr('expected' if operation == 'read' else ['expected'])
    prepare = '' if relocation == 'chdir' else '    pathlib.Path("alias").symlink_to("payload")\n'
    relocate = (f'        os.chdir({str(outside)!r})\n' if relocation == 'chdir' else
                '        pathlib.Path("alias").unlink()\n'
                f'        pathlib.Path("alias").symlink_to({str(outside / "payload")!r})\n')
    cleanup = '        os.chdir(original)\n' if relocation == 'chdir' else '        pathlib.Path("alias").unlink()\n'
    _write(project, 'tests/test_a.py',
           'import os, pathlib\n\ndef test_current_path():\n'
           '    original = os.getcwd()\n' + prepare + '    try:\n'
           f'        assert {expression} == {expected}\n' + relocate +
           f'        assert {expression} == {expected}\n' + '    finally:\n' + cleanup)
    lines = _observe(project, tmp_path)
    records = {record.test: record for record in impact.records_from_observations(
        lines, TESTS, _tree(project), 'tree', REFS)}
    record = records['tests/test_a.py']
    assert record.outcome == 'passed'
    assert record.opaque, 'a repeated spelling may now refer to unobserved external input'
