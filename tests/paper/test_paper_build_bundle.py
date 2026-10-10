"""Remote build bytes cannot publish under another identity or after corruption."""
import base64
import json

import pytest

from lib.paper.builds.bundle import BundleReceiver, iter_bundle
from lib.paper.builds.compiler import CompiledBuild, artifact_metadata
from lib.paper.builds.errors import BuildError
from tests.support.paper_pdf import paper_pdf

pytestmark = pytest.mark.unit


@pytest.fixture
def build(tmp_path):
    directory = tmp_path / 'sender'
    directory.mkdir()
    (directory / 'document.pdf').write_bytes(paper_pdf(padding=0))
    (directory / 'build.log').write_text('A bounded compile log.')
    receipt = {'schema_version': 1, 'owner_user_id': 7, 'document_id': 'a' * 64,
               'build_id': 'b' * 32, 'target': 'desktop', 'source_digest': 'c' * 64,
               'version_verified': True, 'main_file': 'main.tex', 'engine': 'pdflatex',
               'toolchain': 'fixture', 'status': 'success', 'started_at': 1, 'completed_at': 2,
               'artifacts': [artifact_metadata(directory / name) for name in ('build.log', 'document.pdf')],
               'diagnostics': []}
    return CompiledBuild(receipt, directory)


def receiver(tmp_path, **identity):
    return BundleReceiver(tmp_path / 'received', **{
        'owner_user_id': 7, 'document_id': 'a' * 64, 'build_id': 'b' * 32,
        'target': 'desktop', **identity})


def test_bundle_round_trip_preserves_exact_artifacts_and_receipt(build, tmp_path):
    target = receiver(tmp_path)
    for frame in iter_bundle(build):
        target.accept(*frame)
    result, location = target.finish()
    assert result.receipt == build.receipt and location is None
    for row in build.receipt['artifacts']:
        assert (result.directory / row['name']).read_bytes() == (build.directory / row['name']).read_bytes()
    with pytest.raises(BuildError):
        target.accept(*next(iter_bundle(build)))


@pytest.mark.parametrize('identity', [{'owner_user_id': 8}, {'build_id': 'd' * 32},
                                     {'document_id': 'd' * 64}, {'target': 'overleaf'}])
def test_bundle_rejects_foreign_identity_before_writing(build, tmp_path, identity):
    with pytest.raises(BuildError, match='identity'):
        receiver(tmp_path, **identity).accept(*next(iter_bundle(build)))
    assert not (tmp_path / 'received').exists()


def test_bundle_rejects_missing_corrupt_undeclared_and_oversized_artifacts(build, tmp_path):
    target = receiver(tmp_path)
    target.accept(*next(iter_bundle(build)))
    with pytest.raises(BuildError, match='integrity'):
        target.finish()
    with pytest.raises(BuildError, match='Unexpected'):
        target.accept('paper-artifact/../escaped', 'AAAA')
    with pytest.raises(BuildError, match='declared size'):
        size = next(row['size_bytes'] for row in build.receipt['artifacts'] if row['name'] == 'document.pdf')
        target.accept('paper-artifact/document.pdf', base64.b64encode(b'x' * (size + 1)).decode())
    for stream, data in list(iter_bundle(build))[1:]:
        if stream.endswith('document.pdf'):
            data = base64.b64encode(b'x' * len(base64.b64decode(data))).decode()
        target.accept(stream, data)
    with pytest.raises(BuildError, match='integrity'):
        target.finish()


@pytest.mark.parametrize('verified,location_build', [(False, 'b' * 32), (True, 'd' * 32)])
def test_bundle_rejects_unproven_locations(build, tmp_path, verified, location_build):
    manifest = json.loads(next(iter_bundle(build))[1])
    manifest['receipt']['version_verified'] = verified
    manifest['location'] = {'build_id': location_build, 'file': 'main.tex', 'line': 1, 'page': 1, 'rects': []}
    with pytest.raises(BuildError, match='location'):
        receiver(tmp_path).accept('paper-manifest', json.dumps(manifest))


def test_invalid_transfer_unicode_is_a_typed_failure(tmp_path):
    with pytest.raises(BuildError, match='Invalid paper transfer text'):
        receiver(tmp_path).accept('paper-manifest', '\ud800')
