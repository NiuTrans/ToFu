"""Installed grammar authority validates pins before any native code is loaded."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def helper():
    path = ROOT / 'packages/tofu-repo-kernel/tofu_repo_kernel/_grammars.py'
    spec = importlib.util.spec_from_file_location('_grammar_test_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(tmp_path):
    source = tmp_path / 'source';source.mkdir()
    data = b'fixture-code' * 20000
    (source / 'libtree_sitter_fixture.so').write_bytes(data)
    record = dict(format='tofu.repo-grammar-bundle/v1', registry_version='1.20.0',
                  libraries=[dict(file='libtree_sitter_fixture.so', bytes=len(data), sha256=hashlib.sha256(data).hexdigest())])
    content = json.dumps(record).encode()
    (source / 'grammars.json').write_bytes(content)
    (source / 'NOTICES.txt').write_text('Project-owned non-executable test fixture.\n')
    return source, hashlib.sha256(content).hexdigest(), record


def test_packaged_bytes_survive_source_replacement_and_tampering_refuses(helper, bundle, tmp_path):
    source, pin, record = bundle
    destination = tmp_path / 'installed'
    descriptor = helper.package_bundle(source, destination, pin)
    (source / 'libtree_sitter_fixture.so').write_bytes(b'replaced')
    assert helper.check_bundle(destination, descriptor) == dict(libraries=1, bytes=record['libraries'][0]['bytes'])
    (destination / 'grammars/libtree_sitter_fixture.so').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='size or type'):
        helper.check_bundle(destination, descriptor)


@pytest.mark.parametrize('field,value', [('sha256', '0'*64), ('bytes', True), ('file', '../outside'),
    ('file', 'libtree_sitter_bad/name.so'), ('sha256', 'G'*64), ('bytes', 0)])
def test_manifest_or_library_failure_leaves_no_installed_profile(helper, bundle, tmp_path, field, value):
    source, _, record = bundle
    record['libraries'][0][field] = value
    content = json.dumps(record).encode();(source/'grammars.json').write_bytes(content)
    destination = tmp_path/'installed'
    with pytest.raises(ValueError):helper.package_bundle(source, destination, hashlib.sha256(content).hexdigest())
    assert not (destination/'grammars').exists()
    assert not destination.exists() or not list(destination.iterdir())


def test_symlinked_code_and_repeated_output_refuse(helper, bundle, tmp_path):
    source, pin, _ = bundle
    code = source/'libtree_sitter_fixture.so';actual = source/'outside';code.rename(actual);code.symlink_to(actual)
    with pytest.raises(OSError):helper.package_bundle(source, tmp_path/'refused', pin)
    code.unlink();actual.rename(code)
    destination = tmp_path/'installed';helper.package_bundle(source, destination, pin)
    (destination/'grammars/marker').write_bytes(b'preserve')
    with pytest.raises(ValueError, match='fresh build'):
        helper.package_bundle(source, destination, pin)
    assert (destination/'grammars/marker').read_bytes() == b'preserve'


def test_descriptor_validation_is_inert_and_rejects_redirects(helper, bundle, tmp_path, monkeypatch):
    source, pin, _ = bundle
    descriptor = helper.package_bundle(source, tmp_path/'installed', pin)
    def refuse(*args, **kwargs):raise AssertionError('descriptor resolution read a file')
    monkeypatch.setattr(helper.os, 'open', refuse)
    assert helper.installed_bundle(tmp_path/'installed', descriptor)[0] == tmp_path/'installed/grammars'
    for value in ['../outside', '/outside', 'grammars/..']:
        bad = dict(descriptor, directory=value)
        with pytest.raises(ValueError, match='invalid installed'):helper.installed_bundle(tmp_path, bad)


def test_duplicate_library_and_json_keys_refuse(helper, bundle, tmp_path):
    source, _, record = bundle
    record['libraries'].append(record['libraries'][0])
    content = json.dumps(record).encode();(source/'grammars.json').write_bytes(content)
    with pytest.raises(ValueError):helper.package_bundle(source, tmp_path/'duplicates', hashlib.sha256(content).hexdigest())
    content = b'{"format":"one","format":"two"}';(source/'grammars.json').write_bytes(content)
    with pytest.raises(ValueError, match='duplicate'):
        helper.package_bundle(source, tmp_path/'duplicate-keys', hashlib.sha256(content).hexdigest())


def test_installed_selection_consumes_policy_without_opening_native_images():
    from tofu_agent import NativeRepositoryConfiguration
    configuration = NativeRepositoryConfiguration.from_launch_policy(launch_policy={'frame_maximum':1<<20})
    assert configuration.grammar_directory is None
    runtime = configuration.create_runtime()
    try:
        assert runtime.grammar_directory is None and runtime.retained_image_bytes() == 0
    finally:runtime.close()


def test_nonregular_notice_input_refuses_without_waiting(helper, bundle, tmp_path):
    import os
    source, pin, _ = bundle
    notice = source/'NOTICES.txt';notice.unlink();os.mkfifo(notice)
    with pytest.raises(ValueError, match='regular nonempty notices'):
        helper.package_bundle(source, tmp_path/'installed', pin)
    assert not (tmp_path/'installed').exists()
