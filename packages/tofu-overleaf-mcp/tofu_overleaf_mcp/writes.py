"""Acknowledged Git file writes for the pinned upstream MCP implementation.

The upstream project lock encloses commit and this adapter's exact-object push.
Receipts describe an acknowledged Git revision, never a cloud compile revision.
One request owns the active context; no output text establishes commit success.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time
import uuid

from git import Repo
from jsonschema import Draft202012Validator

from .git_push import push_commit

@lru_cache(maxsize=1)
def contract():
    return json.loads(Path(__file__).with_name('file_receipts.generated.json').read_text())


CONTROL = contract()['x-mcp-write-protocol']['control_field']
META = contract()['x-mcp-write-protocol']['metadata_key']


def definition(name):
    return {**contract()['$defs'][name],
            **({'$defs': contract()['$defs']} if name == 'OverleafWriteReceiptV1' else {})}


class WriteError(ValueError):
    pass


def _path(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or re.search(r'[\\\x00-\x1f]', value):
        raise WriteError('write_path_invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) == '.' or '..' in path.parts or any(
            part.lower() == '.git' for part in path.parts) or re.match(r'^[a-z]:', value, re.I):
        raise WriteError('write_path_invalid')
    return str(path)


@dataclass
class _Write:
    project_id: str
    path: str
    action: str
    request_id: str
    owner_user_id: int | None
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    proof: dict | None = None
    process_group_owned: bool = False

    def check(self):
        if self.cancel.is_set() or time.monotonic() >= self.deadline:
            raise WriteError('write_cancelled')


class WriteReceipts:
    def __init__(self, upstream, *, process_group_owned=False):
        if getattr(upstream, '_tofu_write_receipts', None) is not None:
            raise WriteError('write_adapter_already_installed')
        self.upstream = upstream
        self.process_group_owned = process_group_owned
        self.client = upstream.git_client
        self._retry = self.client._retry_push
        self._current = ContextVar('overleaf_write', default=None)
        self._active = None
        # Install once before serving. The pinned private seam is confined here.
        self.client._retry_push = self._push
        upstream._tofu_write_receipts = self

    def tools(self, tools):
        result = []
        for tool in tools:
            if tool.name in contract()['x-mcp-write-tools']:
                inputs = dict(tool.input_schema)
                inputs['properties'] = {**inputs.get('properties', {}),
                    CONTROL: definition('OverleafWriteControlV1')}
                tool = tool.model_copy(update={'input_schema': inputs,
                    'output_schema': definition('OverleafWriteReceiptV1'),
                    'meta': {**(tool.meta or {}), META: {'schema_version': 1, 'source': 'overleaf'}}})
            result.append(tool)
        return result

    def _prepare(self, state):
        state.check()
        project = self.upstream.get_project(state.project_id)
        with self.client._with_project_lock(state.project_id):
            with self.client.ensure_repo(project) as repo:
                branch = repo.head.reference
                tracking = branch.tracking_branch()
                if tracking is None or tracking.commit != branch.commit or repo.is_dirty(untracked_files=True):
                    raise WriteError('write_cache_unpublished')
                candidate = Path(repo.working_tree_dir)
                for part in PurePosixPath(state.path).parts:
                    candidate = candidate / part
                    if candidate.is_symlink():
                        raise WriteError('write_path_invalid')
                target = self.client.validate_path(Path(repo.working_tree_dir), state.path)
                if target.exists() and (not target.is_file() or target.stat().st_size >
                        contract()['x-resource-limits']['overleaf_file_bytes']):
                    raise WriteError('write_file_limit')

    def _push(self, _operation, _description):
        state = self._current.get()
        if state is None:
            raise WriteError('write_context_missing')
        state.check()
        with Repo(self.client._repo_path(state.project_id)) as repo:
            branch = repo.head.reference
            commit = branch.commit
            tracking = branch.tracking_branch()
            # Refuse to publish older unacknowledged local work with this write.
            if tracking is None or len(commit.parents) != 1 or commit.parents[0] != tracking.commit:
                raise WriteError('write_cache_unpublished')
            entry = {'changeId': hashlib.sha256((state.request_id + state.path + commit.hexsha).encode()).hexdigest()[:32],
                     'path': state.path, 'action': state.action}
            if state.action != 'deleted':
                blob = commit.tree / state.path
                if blob.type != 'blob' or blob.mode == 0o120000 or blob.size > contract()['x-resource-limits']['overleaf_file_bytes']:
                    raise WriteError('write_file_limit')
                digest = hashlib.sha256()
                stream = blob.data_stream
                for _ in range((blob.size + 65535) // 65536):
                    state.check()
                    digest.update(stream.read(65536))
                entry['sourceHash'] = digest.hexdigest()
            target = branch.path
            def push_exact_commit():
                state.check()
                push_commit(repo, commit.hexsha, target, state, limits=contract()['x-resource-limits'])
            result = self._retry(push_exact_commit, 'push')
            state.proof = {'file_receipts_version': 1, 'source': 'overleaf',
                'project_id': state.project_id, 'request_id': state.request_id,
                'owner_user_id': state.owner_user_id, 'source_revision': commit.hexsha,
                'file_changes': [entry]}
            return result

    async def invoke(self, name, arguments):
        if self._active is not None:
            raise WriteError('write_busy')
        args = dict(arguments)
        control = args.pop(CONTROL, None)
        if control is not None:
            Draft202012Validator(definition('OverleafWriteControlV1')).validate(control)
        project_id = args.get('project_id')
        if not isinstance(project_id, str) or not re.fullmatch('[0-9a-f]{24}', project_id):
            raise WriteError('write_project_invalid')
        path = _path(args.get('file_path'))
        maximum = contract()['x-resource-limits']['overleaf_file_bytes']
        for key in ('content', 'old_string', 'new_string', 'new_content'):
            if key in args and (not isinstance(args[key], str) or len(args[key].encode()) > maximum):
                raise WriteError('write_file_limit')
        if name == 'upload_file':
            source = Path(args.get('source_path', '')).expanduser()
            if not source.is_file() or source.stat().st_size > maximum:
                raise WriteError('write_file_limit')
        seconds = contract()['x-resource-limits']['overleaf_write_seconds']
        state = _Write(project_id, path, contract()['x-mcp-write-tools'][name],
                       control['request_id'] if control else uuid.uuid4().hex,
                       control['owner_user_id'] if control else None, time.monotonic() + seconds,
                       process_group_owned=self.process_group_owned)
        async def run():
            token = self._current.set(state)
            try:
                await asyncio.to_thread(self._prepare, state)
                state.check()
                try:
                    output = await self.upstream._dispatch(name, args)
                except Exception:
                    if state.proof is None:
                        raise
                    output = 'File committed; supplementary result formatting failed.'
                if state.proof is None:
                    raise WriteError('write_push_unconfirmed')
                result = {**state.proof, 'output': str(output)[:4096]}
                Draft202012Validator(definition('OverleafWriteReceiptV1')).validate(result)
                return result
            finally:
                self._current.reset(token)
        pending = asyncio.create_task(run())
        self._active = pending
        def retired(done):
            self._active = None
            if not done.cancelled():
                done.exception()
        pending.add_done_callback(retired)
        try:
            return await asyncio.wait_for(asyncio.shield(pending), seconds)
        except (asyncio.CancelledError, TimeoutError):
            state.cancel.set()
            # The slot remains owned until the upstream thread actually retires.
            raise


def upstream_writes(upstream):
    """All transports sharing an upstream module share its one write admission."""
    return getattr(upstream, '_tofu_write_receipts', None) or WriteReceipts(upstream)
