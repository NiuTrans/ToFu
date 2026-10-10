"""Release metadata verification precedes installer publication."""
import hashlib

import pytest

from lib.desktop_dist import mirror, platforms, store

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('bad_digest', ['sha256:bad', 'md5:' + 'a' * 32, '', 12])
def test_malformed_release_digest_is_not_optional(bad_digest):
    with pytest.raises(ValueError):
        mirror._release_sha256({'digest': bad_digest})


def test_release_parser_keeps_integrity_metadata():
    digest = 'sha256:' + 'a' * 64
    rows = platforms._assets_from_release_payload({'tag_name': 'v1', 'assets': [
        {'name': 'installer.exe', 'browser_download_url': 'https://example.test/a',
         'size': 4, 'digest': digest}]}, 'owner/repo')
    assert rows[0]['digest'] == digest
    assert mirror._release_sha256(rows[0]) == 'a' * 64


@pytest.mark.parametrize('failure', ['digest', 'size', 'malformed'])
def test_failed_verification_keeps_previously_published_installer(
        tmp_path, monkeypatch, failure):
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path))
    name = 'Tofu-Setup-1.0.0-win64.exe'
    old = b'old'
    (tmp_path / name).write_bytes(old)
    store.record_artifact({'filename': name, 'os': 'windows', 'arch': 'x86_64',
                           'kind': 'full', 'source': 'mirrored', 'size': len(old),
                           'sha256': hashlib.sha256(old).hexdigest()})
    content = b'new-download'
    asset = {'name': name, 'url': 'https://example.test/installer',
             'size': len(content), 'digest': 'sha256:' + hashlib.sha256(content).hexdigest()}
    if failure == 'digest':
        asset['digest'] = 'sha256:' + '0' * 64
    elif failure == 'size':
        asset['size'] += 1
    else:
        asset['digest'] = 'broken'
    monkeypatch.setattr(platforms, 'fetch_latest_release',
                        lambda: {'tag': 'v1.0.0', 'assets': [asset]})

    def download(_url, path):
        with open(path, 'wb') as handle:
            handle.write(content)
        return len(content), hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(mirror, '_download', download)
    assert mirror.refresh_now() is False
    assert (tmp_path / name).read_bytes() == old
    assert not (tmp_path / (name + '.part')).exists()
    assert store.artifacts()[name]['sha256'] == hashlib.sha256(old).hexdigest()


def test_same_size_replacement_digest_forces_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path))
    name = 'Tofu-Setup-1.0.0-win64.exe'
    old, new = b'old', b'new'
    (tmp_path / name).write_bytes(old)
    store.record_artifact({'filename': name, 'os': 'windows', 'arch': 'x86_64',
                           'kind': 'full', 'source': 'mirrored', 'size': 3,
                           'sha256': hashlib.sha256(old).hexdigest()})
    sha = hashlib.sha256(new).hexdigest()
    monkeypatch.setattr(platforms, 'fetch_latest_release', lambda: {
        'tag': 'v1.0.0', 'assets': [{'name': name, 'size': 3,
        'digest': 'sha256:' + sha, 'url': 'https://example.test/installer'}]})

    def download(_url, path):
        with open(path, 'wb') as handle:
            handle.write(new)
        return 3, sha

    monkeypatch.setattr(mirror, '_download', download)
    assert mirror.refresh_now()
    assert (tmp_path / name).read_bytes() == new
    assert store.artifacts()[name]['sha256'] == sha


@pytest.mark.parametrize('budget', ['bytes', 'deadline'])
def test_download_is_bounded(tmp_path, monkeypatch, budget):
    from contextlib import contextmanager

    class Response:
        status_code = 200

        def iter_content(self, chunk_size):
            yield b'1234'
            yield b'5678'

    @contextmanager
    def stream(*args, **kwargs):
        yield Response()

    monkeypatch.setattr('lib.http_client.http_stream', stream)
    if budget == 'bytes':
        monkeypatch.setattr(mirror, '_DOWNLOAD_MAX_BYTES', 6)
    else:
        monkeypatch.setattr(mirror, '_DOWNLOAD_MAX_SECONDS', 0)
    with pytest.raises(RuntimeError, match='limit|deadline'):
        mirror._download('https://example.test/file', str(tmp_path / 'file.part'))
    assert (tmp_path / 'file.part').stat().st_size <= 4


def test_cached_local_corruption_is_repaired(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_DESKTOP_DIST_DIR', str(tmp_path))
    name = 'Tofu-Setup-1.0.0-win64.exe'
    sha = hashlib.sha256(b'new').hexdigest()
    (tmp_path / name).write_bytes(b'bad')
    store.record_artifact({'filename': name, 'os': 'windows', 'arch': 'x86_64',
                           'kind': 'full', 'source': 'mirrored', 'size': 3, 'sha256': sha})
    monkeypatch.setattr(platforms, 'fetch_latest_release', lambda: {
        'tag': 'v1.0.0', 'assets': [{'name': name, 'size': 3,
        'digest': 'sha256:' + sha, 'url': 'https://example.test/installer'}]})

    def download(_url, path):
        with open(path, 'wb') as handle:
            handle.write(b'new')
        return 3, sha

    monkeypatch.setattr(mirror, '_download', download)
    assert mirror.refresh_now()
    assert (tmp_path / name).read_bytes() == b'new'
    assert store.artifacts()[name]['integrity_source'] == 'github-api-digest'
    assert store.artifacts()[name]['release_sha256'] == sha


@pytest.mark.parametrize('size', [-1, '12', True])
def test_parser_does_not_erase_invalid_size(size):
    rows = platforms._assets_from_release_payload({'tag_name': 'v1', 'assets': [
        {'name': 'installer.exe', 'browser_download_url': 'https://example.test/a',
         'size': size}]}, 'owner/repo')
    assert rows[0]['size'] == size
