"""Owner-scoped retained builds, bounded reads and optional cache hydration.

Entry: prepare uploads immutable bytes and yields a workspace-commit witness;
ensure_cached restores an already retained build without invoking a compiler.
Storage owns durable bytes, pins and GC. The compiler only supplies its leased
CompiledBuild. HTTP and location reads do not require cache hydration.
"""
from __future__ import annotations

import base64
import logging
import time
import uuid
from contextlib import ExitStack, contextmanager

from lib.identity import require_user_id
from lib.agent_core.storage_errors import StorageError
from .artifact_reader import FileBuildArtifacts, RetainedBuildArtifacts
from .compiler import CompiledBuild
from .contract import limit, validate
from .errors import BuildError, check_cancelled

_log = logging.getLogger(__name__)


def _storage(*, write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


class PaperBuildArtifactRepository:
    def __init__(self, owner_user_id: int, *, client_factory=None):
        self.owner_user_id = require_user_id(owner_user_id, context='retained paper artifact owner')
        self._client = client_factory or _storage

    def _command(self, operation, payload):
        return self._client(write=True).command('paper.artifact.' + operation,
            {**payload, 'user_id': self.owner_user_id}, f'paper.artifact.{operation}:{uuid.uuid4().hex}')

    @contextmanager
    def prepare(self, build: CompiledBuild, *, cancel=None):
        receipt = validate(build.receipt)
        if receipt['owner_user_id'] != self.owner_user_id:
            raise BuildError('build_identity', 'Retained build belongs to a different owner')
        proof = {key: receipt[key] for key in ('document_id', 'build_id')}
        proof['upload_id'] = uuid.uuid4().hex
        admitted = False
        try:
            check_cancelled(cancel)
            # Reclamation work scales with this accepted build, not a source quota.
            # Each GC transaction remains bounded and cancellation-aware.
            chunks = sum((row['size_bytes'] + limit('retained_chunk_bytes') - 1)
                         // limit('retained_chunk_bytes') for row in receipt['artifacts'])
            steps = (chunks + limit('retained_gc_chunks') - 1) // limit('retained_gc_chunks')
            for _ in range(steps):
                check_cancelled(cancel)
                if not self._command('prune', {})['progress']:
                    break
            self._command('prepare', {**proof, 'receipt': receipt})
            admitted = True
            sequence = 0
            for artifact in receipt['artifacts']:
                remaining = artifact['size_bytes']
                with (build.directory / artifact['name']).open('rb') as stream:
                    while remaining:
                        check_cancelled(cancel)
                        data = stream.read(min(remaining, limit('retained_chunk_bytes')))
                        if not data:
                            raise BuildError('artifact_changed', 'Build artifact ended during retention')
                        self._command('append', {**proof, 'sequence': sequence,
                                                'data': base64.b64encode(data).decode()})
                        remaining -= len(data)
                        sequence += 1
                    if stream.read(1):
                        raise BuildError('artifact_changed', 'Build artifact grew during retention')
            check_cancelled(cancel)
            self._command('seal', proof)
            yield proof
        finally:
            if admitted:
                try:
                    self._command('discard', proof)
                except StorageError:
                    # The finite upload deadline reclaims an uncommitted upload.
                    # Committed document pins survive ambiguous acknowledgements.
                    _log.warning('Retained paper upload cleanup deferred', exc_info=True)

    def latest(self, document_id):
        return self._client().query('paper.artifact.latest',
                                   {'user_id': self.owner_user_id, 'document_id': document_id})

    @contextmanager
    def open_artifact(self, cache, document_id, build_id, name):
        with self.open_build(cache, document_id, build_id) as build:
            with build.open_artifact(name) as artifact:
                yield artifact

    @contextmanager
    def open_build(self, cache, document_id, build_id):
        with ExitStack() as reads:
            retained = None

            def fallback():
                nonlocal retained
                if retained is None:
                    retained = reads.enter_context(self._open_retained(document_id, build_id))
                return retained

            try:
                cached = reads.enter_context(cache.lease(self.owner_user_id, document_id, build_id))
            except (BuildError, OSError) as exc:
                if isinstance(exc, BuildError) and exc.code not in {
                        'build_not_found', 'cache_busy', 'cache_limit', 'build_contract'}:
                    raise
                try:
                    durable = fallback()
                except BuildError as missing:
                    if missing.code == 'build_not_found':
                        raise exc from missing
                    raise
                yield durable
            else:
                yield FileBuildArtifacts(cached, fallback=fallback)

    @contextmanager
    def _open_retained(self, document_id, build_id):
        lease = {'document_id': document_id, 'build_id': build_id, 'lease_id': uuid.uuid4().hex}
        held = False
        try:
            held = self._command('lease', {**lease, 'operation': 'retain'})
            receipt = validate(held['receipt'])
            if (receipt['owner_user_id'], receipt['document_id'], receipt['build_id']) != (
                    self.owner_user_id, document_id, build_id):
                raise BuildError('build_identity', 'Retained receipt belongs to a different build')
            deadline = time.monotonic() + limit('viewer_lease_seconds') - 5

            def read_chunk(sequence):
                if time.monotonic() >= deadline:
                    raise BuildError('cache_busy', 'Retained paper read deadline expired; retry')
                response = self._client().query('paper.artifact.read',
                    {**lease, 'user_id': self.owner_user_id, 'sequence': sequence})
                try:
                    return base64.b64decode(response['data'], validate=True)
                except (ValueError, KeyError, TypeError) as exc:
                    raise BuildError('artifact_changed', 'Retained paper chunk is invalid') from exc

            yield RetainedBuildArtifacts(receipt, read_chunk)
        except StorageError as exc:
            if exc.code == 'database_not_found':
                raise BuildError('build_not_found', 'Retained paper build is unavailable') from exc
            raise
        finally:
            try:
                if held:
                    self._command('lease', {**lease, 'operation': 'release'})
            except StorageError:
                _log.warning('Retained paper read lease cleanup deferred', exc_info=True)

    def viewer(self, cache, document_id, build_id, payload):
        request = validate(payload, 'PaperBuildViewerRequestV1')
        try:
            result = self._command('lease', {'document_id': document_id, 'build_id': build_id,
                'lease_id': request['viewer_id'], 'operation': request['operation']})
            if 'receipt' in result:
                return validate({'retained': result['retained'], 'expires_in_seconds':
                    limit('viewer_lease_seconds') if result['retained'] else 0}, 'PaperBuildViewerResultV1')
        except StorageError as exc:
            if exc.code != 'database_not_found':
                raise
        try:
            return cache.viewer(self.owner_user_id, document_id, build_id, request)
        except BuildError as exc:
            if exc.code != 'build_not_found' or request['operation'] == 'retain':
                raise
            return validate({'retained': False, 'expires_in_seconds': 0}, 'PaperBuildViewerResultV1')

    def ensure_cached(self, cache, document_id, build_id):
        def present():
            try:
                with cache.lease(self.owner_user_id, document_id, build_id):
                    return True
            except BuildError as exc:
                if exc.code != 'build_not_found':
                    raise
                return False

        if present():
            return
        try:
            if present():
                return
            self._restore(cache, document_id, build_id)
        except StorageError as exc:
            if exc.code == 'database_not_found':
                raise BuildError('build_not_found', 'Retained paper build is unavailable') from exc
            raise

    def _restore(self, cache, document_id, build_id):
        lease = {'document_id': document_id, 'build_id': build_id, 'lease_id': uuid.uuid4().hex}
        held = self._command('lease', {**lease, 'operation': 'retain'})
        try:
            receipt = validate(held['receipt'])
            if (receipt['owner_user_id'], receipt['document_id'], receipt['build_id']) != (
                    self.owner_user_id, document_id, build_id):
                raise BuildError('build_identity', 'Retained receipt belongs to a different build')
            deadline = time.monotonic() + limit('viewer_lease_seconds') - 5
            # A distinct private stage permits simultaneous consumers to share
            # the immutable final cache entry without colliding on a work path.
            with cache.stage(self.owner_user_id, document_id, uuid.uuid4().hex) as stage:
                sequence = 0
                for artifact in receipt['artifacts']:
                    remaining = artifact['size_bytes']
                    with (stage / artifact['name']).open('xb') as stream:
                        while remaining:
                            if time.monotonic() >= deadline:
                                raise BuildError('cache_busy', 'Retained paper restore deadline expired')
                            response = self._client().query('paper.artifact.read',
                                {**lease, 'user_id': self.owner_user_id, 'sequence': sequence})
                            data = base64.b64decode(response['data'], validate=True)
                            if len(data) != min(remaining, limit('retained_chunk_bytes')):
                                raise BuildError('artifact_changed', 'Retained paper chunk has an invalid length')
                            stream.write(data)
                            remaining -= len(data)
                            sequence += 1
                cache.publish(CompiledBuild(receipt, stage), make_current=False)
        finally:
            try:
                self._command('lease', {**lease, 'operation': 'release'})
            except StorageError:
                _log.warning('Retained paper read lease cleanup deferred', exc_info=True)
