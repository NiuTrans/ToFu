"""Bounded immutable build transfer shared by remote execution adapters.

iter_bundle emits a manifest followed by bounded base64 artifact chunks.
BundleReceiver writes only declared names into a fresh staging directory and
verifies identity, byte counts and digests before returning a publishable build.
Transport framing, authority and cancellation remain with the caller.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path

from .compiler import CompiledBuild, artifact_metadata
from .pdf_validation import validate_pdf
from .contract import limit, validate
from .errors import BuildError, check_cancelled


def iter_bundle(build: CompiledBuild, *, location=None, cancel=None):
    manifest = validate({'schema_version': 1, 'receipt': build.receipt,
                         'location': location}, 'PaperBuildBundleV1')
    text = json.dumps(manifest, separators=(',', ':'))
    if len(text.encode()) > limit('transfer_frame_bytes'):
        raise BuildError('transfer_limit', 'Paper build manifest exceeds its transfer limit')
    yield 'paper-manifest', text
    for artifact in build.receipt['artifacts']:
        path = build.directory / artifact['name']
        if artifact_metadata(path) != artifact:
            raise BuildError('artifact_changed', 'Paper artifact changed before transfer')
        with path.open('rb') as stream:
            while chunk := stream.read(limit('transfer_chunk_bytes')):
                check_cancelled(cancel)
                yield 'paper-artifact/' + artifact['name'], base64.b64encode(chunk).decode('ascii')


class BundleReceiver:
    def __init__(self, directory: Path, *, owner_user_id: int, document_id: str,
                 build_id: str, target: str):
        self.directory = directory
        self.identity = {'owner_user_id': owner_user_id, 'document_id': document_id,
                         'build_id': build_id, 'target': target}
        self.manifest = None
        self.artifacts = {}
        self.sizes = {}
        self.hashes = {}
        self.finished = False

    def accept(self, stream: str, data: str) -> None:
        try:
            size = len(data.encode('utf-8')) if isinstance(data, str) else -1
        except UnicodeError as exc:
            raise BuildError('transfer_data', 'Invalid paper transfer text') from exc
        if (self.finished or not isinstance(stream, str) or size < 0
                or size > limit('transfer_frame_bytes')):
            raise BuildError('transfer_limit', 'Invalid or oversized paper transfer frame')
        if stream == 'paper-manifest':
            if self.manifest is not None:
                raise BuildError('transfer_order', 'Paper build manifest was repeated')
            try:
                manifest = validate(json.loads(data), 'PaperBuildBundleV1')
                receipt = validate(manifest['receipt'])
            except (ValueError, TypeError) as exc:
                raise BuildError('build_contract', 'Invalid paper build manifest') from exc
            if any(receipt[key] != value for key, value in self.identity.items()):
                raise BuildError('build_identity', 'Remote paper build identity does not match the request')
            if manifest['location'] is not None and (
                    not receipt['version_verified'] or receipt['status'] != 'success'
                    or manifest['location']['build_id'] != receipt['build_id']):
                raise BuildError('build_identity', 'Remote paper location does not match the verified build')
            self.directory.mkdir(parents=True, exist_ok=False)
            self.manifest = manifest
            self.artifacts = {row['name']: row for row in receipt['artifacts']}
            return
        name = stream.removeprefix('paper-artifact/')
        if not stream.startswith('paper-artifact/') or name not in self.artifacts:
            raise BuildError('transfer_order', 'Unexpected paper build artifact')
        if len(data) > 4 * ((limit('transfer_chunk_bytes') + 2) // 3):
            raise BuildError('transfer_limit', 'Paper artifact chunk exceeds its byte limit')
        try:
            chunk = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BuildError('transfer_data', 'Invalid paper artifact encoding') from exc
        size = self.sizes.get(name, 0) + len(chunk)
        if not chunk or size > self.artifacts[name]['size_bytes']:
            raise BuildError('transfer_limit', 'Paper artifact exceeds its declared size')
        # Reopening one bounded append avoids a file-descriptor lifetime beyond
        # this call. The directory is private and the names are schema enums.
        with (self.directory / name).open('ab') as output:
            output.write(chunk)
        self.sizes[name] = size
        self.hashes.setdefault(name, hashlib.sha256()).update(chunk)

    def finish(self, *, cancel=None) -> tuple[CompiledBuild, dict | None]:
        check_cancelled(cancel)
        if self.finished or self.manifest is None:
            raise BuildError('transfer_incomplete', 'Paper build transfer has no complete manifest')
        for name, row in self.artifacts.items():
            if self.sizes.get(name) != row['size_bytes'] or self.hashes[name].hexdigest() != row['sha256']:
                raise BuildError('artifact_changed', 'Paper artifact transfer failed integrity verification')
        receipt = self.manifest['receipt']
        if receipt['status'] == 'success':
            validate_pdf(self.directory / 'document.pdf', cancel=cancel)
        self.finished = True
        (self.directory / 'receipt.json').write_text(json.dumps(receipt, separators=(',', ':')))
        return CompiledBuild(receipt, self.directory), self.manifest['location']
