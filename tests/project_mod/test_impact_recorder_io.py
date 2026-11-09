"""Ignored importer I/O costs no canonicalization; application reads stay observed."""
import os

import pytest

from lib.project_mod import impact_recorder as recorder

pytestmark = pytest.mark.unit


@pytest.fixture
def recording(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, '_STATE_OK', True)
    monkeypatch.setattr(recorder, '_ROOT', str(tmp_path.resolve()))
    monkeypatch.setattr(recorder, '_code_paths', {})
    monkeypatch.setattr(recorder, '_SNAPSHOT_PATHS', frozenset({'input.txt'}))
    monkeypatch.setattr(recorder, '_SNAPSHOT_DIRS', {'': {'input.txt'}})
    return recorder._Bucket()


@pytest.mark.parametrize('event', ['open', 'os.listdir', 'os.scandir'])
def test_import_system_io_never_resolves_an_excluded_path(recording, tmp_path, monkeypatch, event):
    namespace = {}
    exec(compile('def observe(recorder, bucket, event, args):\n'
                 '    recorder._observe(bucket, event, args)\n',
                 '<frozen importlib._bootstrap_external>', 'exec'), namespace)
    resolutions = []
    original = recorder._relative

    def observed(path):
        resolutions.append(path)
        return original(path)

    monkeypatch.setattr(recorder, '_relative', observed)
    namespace['observe'](recorder, recording, event, (str(tmp_path / 'bytecode.pyc'), 'rb', os.O_RDONLY))
    assert not recording.data and not recording.dirs and not recording.opaque
    assert resolutions == []


def test_application_reads_and_listings_preserve_dependency_evidence(recording, tmp_path):
    source = tmp_path / 'input.txt'
    source.write_text('application data')
    recorder._observe(recording, 'open', (os.fsencode(source), 'r', os.O_RDONLY))
    recorder._observe(recording, 'os.listdir', (str(tmp_path),))
    recorder._observe(recording, 'os.scandir', (os.fsencode(tmp_path),))
    assert recording.data == {'input.txt'}
    assert recording.dirs == {''}
    assert not recording.opaque
    recorder._observe(recording, 'open', (str(tmp_path.parent / 'external.txt'), 'r', os.O_RDONLY))
    assert recording.opaque


@pytest.mark.parametrize('operation', ['read', 'list', 'scan'])
def test_present_inputs_outside_the_git_snapshot_are_opaque(recording, tmp_path, operation):
    source = tmp_path / 'generated.txt'
    source.write_text('not captured by Git')
    if operation == 'read':
        recorder._observe(recording, 'open', (str(source), 'r', os.O_RDONLY))
    else:
        recorder._observe(recording, 'os.listdir' if operation == 'list' else 'os.scandir',
                          (str(tmp_path),))
    assert recording.opaque


def test_missing_optional_read_keeps_its_negative_dependency(recording, tmp_path):
    recorder._observe(recording, 'open', (str(tmp_path / 'missing.txt'), 'r', os.O_RDONLY))
    assert recording.data == {'missing.txt'}
    assert recording.opaque, 'an ignored file can appear without changing the Git snapshot'


@pytest.mark.parametrize('exists', [False, True])
def test_a_directory_outside_the_snapshot_is_opaque_even_when_empty(recording, tmp_path, exists):
    directory = tmp_path / 'not-in-snapshot'
    if exists:
        directory.mkdir()
    recorder._observe(recording, 'os.listdir', (str(directory),))
    assert recording.dirs == {'not-in-snapshot'}
    assert recording.opaque
