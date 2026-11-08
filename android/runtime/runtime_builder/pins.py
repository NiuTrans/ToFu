"""``pins.json``: every downloaded input, and ``resolve``, which refreshes it.

Entry points: ``load_pins``, ``write_pins``, ``resolve_pins`` (network; the
only code that reads "latest" publisher indexes). Each pinned SHA-256 is
copied from the publisher's own checksum file, recorded next to it.
Dependencies: stdlib, ``gpgv``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile

from . import debs
from .fetch import fetch_bytes, fetch_pinned

PINS_FORMAT = 1
UBUNTU_BASE_RELEASES = 'https://cdimage.ubuntu.com/ubuntu-base/releases'
PBS_RELEASES = 'https://github.com/astral-sh/python-build-standalone/releases/download'
UV_RELEASES = 'https://github.com/astral-sh/uv/releases/download'
PORTS_ARCHIVE = 'http://ports.ubuntu.com/ubuntu-ports'
APT_SUITES = ('noble', 'noble-updates', 'noble-security')
APT_COMPONENT = 'main'
APT_ARCHITECTURE = 'arm64'
TRIPLES = ('aarch64-unknown-linux-gnu', 'x86_64-unknown-linux-gnu')
BASE_KEYRING = 'usr/share/keyrings/ubuntu-archive-keyring.gpg'


class PinsError(RuntimeError):
    """pins.json is malformed or a publisher checksum does not match."""


def load_pins(path: Path) -> dict:
    pins = json.loads(path.read_text(encoding='utf-8'))
    if pins.get('format') != PINS_FORMAT:
        raise PinsError(f'{path}: unsupported pins format {pins.get("format")!r}')
    return pins


def write_pins(path: Path, pins: dict) -> None:
    path.write_text(json.dumps(pins, indent=2, sort_keys=True, ensure_ascii=False) + '\n',
                    encoding='utf-8')


def _checksum_line(text: str, filename: str) -> str:
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip('*') == filename:
            return fields[0].lower()
    raise PinsError(f'{filename} is not listed in its checksum file')


def _verify_detached(data: bytes, signature: bytes, keyring: str) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        data_path, signature_path = Path(scratch, 'data'), Path(scratch, 'data.gpg')
        data_path.write_bytes(data)
        signature_path.write_bytes(signature)
        result = subprocess.run(['gpgv', '--keyring', keyring, str(signature_path), str(data_path)],
                                capture_output=True, check=False)
    if result.returncode != 0:
        raise PinsError('SHA256SUMS signature verification failed: '
                        + result.stderr.decode('utf-8', 'replace').strip())


def resolve_ubuntu_base(version: str, keyring: str) -> dict:
    series = '.'.join(version.split('.')[:2])
    directory = f'{UBUNTU_BASE_RELEASES}/{series}/release'
    filename = f'ubuntu-base-{version}-base-{APT_ARCHITECTURE}.tar.gz'
    sums = fetch_bytes(f'{directory}/SHA256SUMS')
    _verify_detached(sums, fetch_bytes(f'{directory}/SHA256SUMS.gpg'), keyring)
    return {
        'version': version,
        'url': f'{directory}/{filename}',
        'sha256': _checksum_line(sums.decode('utf-8'), filename),
        'checksums': f'{directory}/SHA256SUMS',
        'signature': f'{directory}/SHA256SUMS.gpg',
    }


def resolve_pbs(release: str, version: str) -> dict:
    sums_url = f'{PBS_RELEASES}/{release}/SHA256SUMS'
    sums = fetch_bytes(sums_url).decode('utf-8')
    assets = {}
    for triple in TRIPLES:
        filename = f'cpython-{version}+{release}-{triple}-install_only.tar.gz'
        assets[triple] = {
            'url': f'{PBS_RELEASES}/{release}/{filename.replace("+", "%2B")}',
            'sha256': _checksum_line(sums, filename),
        }
    return {'release': release, 'version': version, 'checksums': sums_url, 'assets': assets}


def resolve_uv(version: str) -> dict:
    assets = {}
    for triple in TRIPLES:
        filename = f'uv-{triple}.tar.gz'
        sums_url = f'{UV_RELEASES}/{version}/{filename}.sha256'
        assets[triple] = {
            'url': f'{UV_RELEASES}/{version}/{filename}',
            'sha256': _checksum_line(fetch_bytes(sums_url).decode('utf-8'), filename),
            'checksums': sums_url,
        }
    return {'version': version, 'assets': assets}


def load_indexes(keyring: str) -> tuple[dict[str, list[dict[str, str]]], dict]:
    """Verified Packages indexes of every suite, fetched by hash."""
    indexes, record = {}, {}
    for suite in APT_SUITES:
        inrelease = fetch_bytes(f'{PORTS_ARCHIVE}/dists/{suite}/InRelease')
        debs.verify_inrelease(inrelease, keyring)
        path = f'{APT_COMPONENT}/binary-{APT_ARCHITECTURE}/Packages.xz'
        sha256, size = debs.inrelease_sha256(inrelease)[path]
        data = fetch_bytes(f'{PORTS_ARCHIVE}/dists/{suite}/{APT_COMPONENT}/binary-{APT_ARCHITECTURE}/'
                           f'by-hash/SHA256/{sha256}')
        if hashlib.sha256(data).hexdigest() != sha256 or len(data) != size:
            raise PinsError(f'{suite} {path} does not match its InRelease entry')
        indexes[suite] = debs.load_packages_index(data)
        (release,) = debs.parse_deb822(inrelease.decode('utf-8').split('\n\n', 1)[1]
                                       .split('-----BEGIN PGP SIGNATURE-----', 1)[0])
        record[suite] = {'date': release.get('Date', ''), 'packages_sha256': sha256}
    return indexes, record


def resolve_apt(requested: list[str], base_tarball: Path) -> dict:
    with tarfile.open(base_tarball) as base:
        status = base.extractfile('var/lib/dpkg/status').read().decode('utf-8')
        keyring_bytes = base.extractfile(BASE_KEYRING).read()
    installed = debs.installed_from_status(status)
    with tempfile.NamedTemporaryFile(suffix='.gpg') as keyring:
        keyring.write(keyring_bytes)
        keyring.flush()
        indexes, suites = load_indexes(keyring.name)
    candidates = debs.best_candidates(indexes, APT_ARCHITECTURE)
    closure = debs.resolve_closure(requested, installed, candidates)
    newer = sorted(name for name, version in installed.versions.items()
                   if name in candidates and name not in {c.name for c in closure}
                   and debs.compare_versions(candidates[name].version, version) > 0)
    return {
        'archive': PORTS_ARCHIVE,
        'architecture': APT_ARCHITECTURE,
        'component': APT_COMPONENT,
        'suites': suites,
        'requested': requested,
        'packages': [{
            'name': candidate.name,
            'version': candidate.version,
            'suite': candidate.suite,
            'filename': candidate.stanza['Filename'],
            'sha256': candidate.stanza['SHA256'],
            'size': int(candidate.stanza['Size']),
        } for candidate in closure],
        'base_packages_with_updates': newer,
    }


def resolve_pins(current: dict, cache: Path, keyring: str) -> dict:
    """Refresh every SHA-256 for the versions ``current`` names."""
    ubuntu = resolve_ubuntu_base(current['ubuntu_base']['version'], keyring)
    base_tarball = fetch_pinned(ubuntu['url'], ubuntu['sha256'], None, cache)
    pbs = current['python_build_standalone']
    return {
        'format': PINS_FORMAT,
        'ubuntu_base': ubuntu,
        'apt': resolve_apt(current['apt']['requested'], base_tarball),
        'python_build_standalone': resolve_pbs(pbs['release'], pbs['version']),
        'uv': resolve_uv(current['uv']['version']),
    }
