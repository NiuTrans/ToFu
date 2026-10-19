"""Overleaf execution adapter: owned source binding, bounded MCP bytes, shared cache.

The existing paper TaskRuntime owns admission, coalescing and publication.
Artifact transfer starts independent reads on demand and never interprets producer
filesystem paths. Failed/unverified builds cannot advance the verified pointer.
"""
from __future__ import annotations

import base64
from concurrent.futures import CancelledError, Future
from lib.observability import InstrumentedThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import tempfile
import time
import uuid

from lib.log import get_logger
from .bundle import BundleReceiver
from .contract import limit, validate
from .errors import BuildError, check_cancelled
from .overleaf_capabilities import bound_source, enabled_capabilities

logger = get_logger(__name__)


@dataclass(frozen=True)
class OverleafDocument:
    owner_user_id: int
    conversation_id: str
    document_id: str
    source: dict

    @property
    def main_file(self):
        return self.source['main_file'] or ''


class _Deadline:
    def __init__(self, *, deadline, cancel=None, stopped=None):
        self.deadline, self.cancel, self.stopped = deadline, cancel, stopped

    def is_set(self):
        return ((self.deadline is not None and time.monotonic() >= self.deadline) or (self.cancel is not None and self.cancel.is_set())
                or (self.stopped is not None and self.stopped.is_set()))


def _bridge(owner_user_id):
    from lib.mcp import get_bridge
    return get_bridge(owner_user_id)


def bind_overleaf_document(payload, *, owner_user_id, settings, bridge=None):
    if payload.get('engine', 'auto') != 'auto' or payload.get('project_root') or payload.get('project_path'):
        raise BuildError('overleaf_capability', 'Use the selected Overleaf project and its compiler settings')
    bridge = bridge or _bridge(owner_user_id)
    source = bound_source(payload, owner_user_id=owner_user_id, settings=settings, bridge=bridge)
    identity = {'owner': owner_user_id, 'target': 'overleaf', 'server': source['mcp_server'],
                'project': source['project_id'], 'main': source['main_file']}
    document_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return OverleafDocument(owner_user_id, payload['conv_id'], document_id, source)


def _authorized(document, bridge):
    from .service import owned_settings
    current = bound_source(document.source, owner_user_id=document.owner_user_id,
                           settings=owned_settings(document.conversation_id, document.owner_user_id), bridge=bridge)
    if current != document.source:
        raise BuildError('overleaf_binding', 'Overleaf project selection changed during the build')
    return current['capabilities']


def _call(bridge, tool, payload, deadline):
    if deadline.is_set():
        raise CancelledError()
    remaining = None if deadline.deadline is None else max(1, math.ceil(deadline.deadline - time.monotonic()))
    try:
        return bridge.call_tool_document(tool, payload, timeout=remaining, cancel=deadline)
    except CancelledError:
        raise
    except TimeoutError as exc:
        raise BuildError('overleaf_timeout', 'Overleaf request timed out') from exc
    except Exception as exc:
        raise BuildError('overleaf_unavailable', 'Overleaf export is unavailable; check the MCP connection') from exc


def _download(bridge, tool, lease, artifacts, receiver, *, cancel, workers):
    """Demand reads; one anonymous spool owns completed out-of-order bytes.

    Workers never write cache files. The caller feeds the receiver in artifact/
    offset order, then joins every accepted reader before releasing the spool.
    """
    stop = threading.Event()
    deadline = _Deadline(deadline=time.monotonic() + min(
        limit('transfer_timeout_seconds'), lease['expires_in_seconds']), cancel=cancel, stopped=stop)
    identity = {key: lease[key] for key in ('schema_version', 'owner_user_id', 'project_id', 'request_id')}
    auth = {**identity, 'lease_token': lease['lease_token']}
    spool_lock = threading.Lock()
    with tempfile.TemporaryFile(mode='w+b') as spool:
        def read_chunk(artifact, offset, spool_offset):
            request = {**auth, 'artifact': artifact['name'], 'offset': offset}
            chunk = validate(_call(bridge, tool, request, deadline), 'PaperMcpChunkV1')
            expected_size = min(limit('transfer_chunk_bytes'), artifact['size_bytes'] - offset)
            if (any(chunk[key] != value for key, value in identity.items())
                    or chunk['artifact'] != artifact['name'] or chunk['offset'] != offset
                    or chunk['eof'] != (offset + expected_size == artifact['size_bytes'])):
                raise BuildError('build_identity', 'MCP artifact chunk does not match its leased build')
            try:
                data = base64.b64decode(chunk['data'], validate=True)
                if len(data) != expected_size:
                    raise ValueError('chunk length')
            except (ValueError, TypeError) as exc:
                raise BuildError('transfer_data', 'MCP artifact chunk is incomplete or corrupt') from exc
            with spool_lock:
                spool.seek(spool_offset)
                spool.write(data)
            # Future results carry no response/data bodies.

        pool = InstrumentedThreadPoolExecutor(max_workers=max(1, workers),
            metric_pool='paper-mcp-read', thread_name_prefix='paper-mcp-read')
        pending = []
        spool_offset = 0
        try:
            for artifact in artifacts:
                artifact = dict(artifact)  # Freeze this leased chunk plan for workers.
                for offset in range(0, artifact['size_bytes'], limit('transfer_chunk_bytes')):
                    check_cancelled(cancel)
                    size = min(limit('transfer_chunk_bytes'), artifact['size_bytes'] - offset)
                    try:
                        future = pool.submit(read_chunk, artifact, offset, spool_offset)
                    except Exception as error:
                        future = Future()
                        failure = BuildError('overleaf_unavailable', 'Overleaf artifact worker could not start')
                        failure.__cause__ = error
                        future.set_exception(failure)
                    pending.append((artifact['name'], spool_offset, size, future))
                    spool_offset += size
            for name, offset, size, future in pending:
                check_cancelled(cancel)
                future.result()
                with spool_lock:
                    spool.seek(offset)
                    data = spool.read(size)
                if len(data) != size:
                    raise BuildError('transfer_data', 'Overleaf artifact spool is incomplete')
                receiver.accept('paper-artifact/' + name, base64.b64encode(data).decode('ascii'))
        except CancelledError as exc:
            check_cancelled(cancel)
            raise BuildError('overleaf_timeout', 'Overleaf artifact transfer timed out') from exc
        finally:
            stop.set()
            for _name, _offset, _size, future in pending:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)


def overleaf_execution(document, payload, scheduler, *, bridge=None):
    def execute(task, publish):
        from .service import artifact_url
        client = bridge or _bridge(document.owner_user_id)
        capabilities = _authorized(document, client)
        cancel = task['abort_event']
        check_cancelled(cancel)
        build_id = uuid.uuid4().hex
        identity = {'schema_version': 1, 'owner_user_id': document.owner_user_id,
                    'project_id': document.source['project_id'], 'request_id': build_id}
        body = validate({**identity, **({'main_file': document.main_file} if document.main_file else {})},
                        'PaperMcpBuildRequestV1')
        manifest = None
        started_at = int(time.time() * 1000)
        try:
            with scheduler.cache.stage(document.owner_user_id, document.document_id, build_id) as stage:
                try:
                    received = _call(client, capabilities['build']['name'], body, _Deadline(
                        deadline=None, cancel=cancel))
                except CancelledError as exc:
                    check_cancelled(cancel)
                    raise BuildError('overleaf_timeout', 'Overleaf compilation timed out') from exc
                manifest = validate(received, 'PaperMcpManifestV1')
                if any(manifest[key] != value for key, value in identity.items()):
                    # Never release a token belonging to an unrelated export.
                    manifest = None
                    raise BuildError('build_identity', 'MCP paper build identity does not match the request')
                if document.main_file and manifest['main_file'] is not None and manifest['main_file'] != document.main_file:
                    raise BuildError('overleaf_main_file', 'Overleaf compiled a different main document')
                status = manifest['compile_status']
                receipt = validate({'schema_version': 1, 'owner_user_id': document.owner_user_id,
                    'document_id': document.document_id, 'build_id': build_id, 'target': 'overleaf',
                    'source_digest': None, 'version_verified': False,
                    'main_file': manifest['main_file'] or document.main_file,
                    'engine': 'remote', 'toolchain': 'remote (unverified)',
                    'remote_build_id': manifest['remote_build_id'], 'status': status,
                    'started_at': started_at, 'completed_at': int(time.time() * 1000),
                    'artifacts': manifest['artifacts'], 'diagnostics': [] if status == 'success' else [{
                        'severity': 'error', 'code': 'overleaf_compile_failed',
                        'message': 'Overleaf compilation failed; inspect the build log',
                        'file': manifest['main_file'] or '', 'line': None}]})
                receiver = BundleReceiver(stage / 'build', owner_user_id=document.owner_user_id,
                    document_id=document.document_id, build_id=build_id, target='overleaf')
                receiver.accept('paper-manifest', json.dumps({'schema_version': 1,
                    'receipt': receipt, 'location': None}, separators=(',', ':')))
                _download(client, capabilities['read']['name'], manifest, receipt['artifacts'], receiver,
                          cancel=cancel, workers=scheduler.budget.workers)
                build, _ = receiver.finish(cancel=cancel)
                check_cancelled(cancel)
                _authorized(document, client)
                return publish(build, location=None, pdf_url=artifact_url(document.document_id, build_id, 'document.pdf')
                               if status == 'success' else None)
        finally:
            if manifest is not None:
                try:
                    # Cleanup authority lasts for this already-acquired lease;
                    # a changed tool contract cannot receive its token.
                    current = enabled_capabilities(client, document.source['mcp_server'],
                        owner_user_id=document.owner_user_id, settings={'mcpEnabled': True})
                    if current['release'] == capabilities['release']:
                        release = validate({**identity, 'lease_token': manifest['lease_token']}, 'PaperMcpLeaseV1')
                        _call(client, capabilities['release']['name'], release,
                              _Deadline(deadline=time.monotonic() + 5))
                except Exception:
                    logger.warning('Paper MCP lease release unavailable; bounded producer expiry applies')
    return execute
