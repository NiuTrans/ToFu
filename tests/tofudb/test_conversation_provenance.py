"""Synthetic source/artifact receipts; never launch a server or open storage."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from benchmarks import conversation_provenance as provenance
from benchmarks.conversation_acceptance import POLICY
from benchmarks.conversation_authority import ConversationAuthority

pytestmark = pytest.mark.unit


def sources():
    return {'native_source_sha256': 'a' * 64,
            'files': {name: 'b' * 64 for name in provenance.REQUIRED_SOURCES}}


def native_receipt(*, embedded=False):
    build = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64}
    declared = {'binary_sha256': 'c' * 64}
    artifact = {**declared, 'build_identity': build}
    authority = {'backend': 'tofudb', 'client_mode': 'embedded' if embedded else 'application',
                 'build_identity': deepcopy(build), 'checkout_source_sha256': 'a' * 64,
                 'source_matches_checkout': True}
    if embedded:
        declared['shared_library_sha256'] = 'd' * 64
        artifact.update(shared_library_sha256='d' * 64,
                        embedded_build_identity={'source_sha256': 'a' * 64})
        authority.update(shared_library_sha256='d' * 64,
                         embedded_build_identity={'source_sha256': 'a' * 64})
    return {'format': provenance.FORMAT, 'sources_before': sources(), 'sources_after': sources(),
            'declared_artifact': declared, 'artifact_before': deepcopy(artifact),
            'artifact_after': deepcopy(artifact)}, authority


def qualify(receipt, authority):
    return provenance.qualify_provenance(receipt, backend='tofudb', binary_sha256='c' * 64,
                                        authority=authority, current_sources=sources())


def test_source_capture_reuses_certification_inventory_and_native_fingerprint(tmp_path, monkeypatch):
    for name in provenance.REQUIRED_SOURCES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic source\n')
    sdk = tmp_path / 'tofu_db/wire.py'
    sdk.write_text('synthetic sdk v1\n')
    monkeypatch.setattr(provenance, 'ROOT', tmp_path)
    monkeypatch.setattr(provenance, 'verification_sources',
                        lambda: {'tofu_db/wire.py': provenance.fingerprint(sdk)})
    seen = []
    def native_fingerprint(root):
        seen.append(root)
        return 'e' * 64
    monkeypatch.setattr(provenance, 'source_fingerprint', native_fingerprint)
    first = provenance.capture_sources()
    assert seen == [tmp_path]
    assert first['native_source_sha256'] == 'e' * 64
    assert first['files']['tofu_db/wire.py'] == provenance.fingerprint(sdk)
    assert first['files']['benchmarks/conversation_concurrency_contract.json']

    # Independent dependency knowledge, not an assertion derived from the list
    # being tested: the application executor is only an adapter to this kernel.
    assert {'tofu_workers/__init__.py', 'tofu_workers/executor.py', 'runtime_guards.py',
            'bootstrap_pkg/resource_policy_generated.py'} <= first['files'].keys()
    sdk.write_text('synthetic sdk v2\n')
    assert provenance.capture_sources()['files']['tofu_db/wire.py'] != first['files']['tofu_db/wire.py']
    sdk.unlink()
    with pytest.raises(OSError):
        provenance.capture_sources()


def test_artifact_capture_uses_owned_native_identity_and_loaded_library(tmp_path, monkeypatch):
    binary = tmp_path / 'tofu-db'
    library = tmp_path / 'libtofu_db.so'
    binary.write_bytes(b'synthetic executable, never run')
    library.write_bytes(b'synthetic library, never loaded')
    identity = {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64}
    loaded = {'source_sha256': 'a' * 64}
    seen = []
    def identify(path):
        seen.append(path)
        return identity
    monkeypatch.setattr(provenance, 'build_identity', identify)
    captured = provenance.capture_artifact(binary, embedded_build_identity=loaded)
    assert seen == [binary]
    assert captured['binary_sha256'] == provenance.fingerprint(binary)
    assert captured['shared_library_sha256'] == provenance.fingerprint(library)
    assert captured['build_identity'] == identity
    loaded['source_sha256'] = 'f' * 64
    assert captured['embedded_build_identity']['source_sha256'] == 'a' * 64


@pytest.mark.parametrize('change_pinned_binary', [False, True])
def test_authority_observes_actual_pinned_bytes_before_cleanup(tmp_path, monkeypatch, change_pinned_binary):
    binary = tmp_path / 'declared-tofu-db'
    binary.write_bytes(b'synthetic declared build')
    pinned = tmp_path / 'pinned-tofu-db'
    authority = ConversationAuthority('tofudb', binary, tmp_path)
    def start():
        pinned.write_bytes(binary.read_bytes())
    engine = SimpleNamespace(_binary=pinned, _client=SimpleNamespace(_timeout=POLICY['rpc_deadline_seconds']),
                             start=start, stop=pinned.unlink)
    authority.engine = engine
    monkeypatch.setattr(provenance, 'build_identity',
                        lambda _: {'format': 'tofu-db-build.v1', 'source_sha256': 'a' * 64})
    authority.start()
    expected = provenance.fingerprint(binary)
    assert engine._expected_sha256 == expected
    assert authority.declared_artifact == {'binary_sha256': expected}
    assert authority.artifact_before['binary_sha256'] == expected
    if change_pinned_binary:
        pinned.write_bytes(b'changed artifact')
    else:
        # A rebuild of the original path cannot change the bytes actually used.
        binary.write_bytes(b'different unpinned build')
    authority.stop()
    assert not pinned.exists()
    assert (authority.artifact_before == authority.artifact_after) is not change_pinned_binary


def test_artifact_observation_failure_still_joins_cleanup(tmp_path, monkeypatch):
    binary = tmp_path / 'never-created'
    authority = ConversationAuthority('tofudb', binary, tmp_path)
    stopped = []
    authority.engine = SimpleNamespace(_binary=binary, stop=lambda: stopped.append(True))
    authority.stop()
    assert stopped == [True]
    assert 'capture_error' in authority.artifact_after


@pytest.mark.parametrize('embedded', [False, True])
def test_valid_current_native_receipts_qualify_without_executing_anything(embedded, monkeypatch):
    receipt, authority = native_receipt(embedded=embedded)
    def forbidden(*args, **kwargs):
        raise AssertionError('pure qualification must never execute a report artifact')
    monkeypatch.setattr(provenance, 'capture_artifact', forbidden)
    monkeypatch.setattr(provenance, 'capture_sources', forbidden)
    result = qualify(receipt, authority)
    assert result == {'current_source_qualified': True, 'frozen_build_comparable': True,
                      'failures': [], 'frozen_failures': []}


@pytest.mark.parametrize('mutation', ['missing-declaration', 'different-declaration', 'changed-library',
                                       'missing-loaded-identity', 'foreign-loaded-build', 'wrong-mode'])
def test_embedded_library_and_loaded_build_must_match_the_declared_pair(mutation):
    receipt, authority = native_receipt(embedded=True)
    if mutation == 'missing-declaration':
        del receipt['declared_artifact']['shared_library_sha256']
    elif mutation == 'different-declaration':
        receipt['declared_artifact']['shared_library_sha256'] = 'f' * 64
    elif mutation == 'changed-library':
        receipt['artifact_after']['shared_library_sha256'] = 'f' * 64
    elif mutation == 'missing-loaded-identity':
        receipt['artifact_before']['embedded_build_identity'] = None
    elif mutation == 'foreign-loaded-build':
        for name in ('artifact_before', 'artifact_after'):
            receipt[name]['embedded_build_identity']['source_sha256'] = 'f' * 64
        authority['embedded_build_identity']['source_sha256'] = 'f' * 64
    else:
        authority['client_mode'] = 'application'
    result = qualify(receipt, authority)
    assert not result['current_source_qualified'] and not result['frozen_build_comparable']


def test_policy_and_receipts_use_the_same_provenance_format():
    assert POLICY['provenance_format'] == provenance.FORMAT
