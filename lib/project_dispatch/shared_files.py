"""Declared project-file metadata and exact untrusted DATA reads.

Entry points: ProjectSharedFiles.list/register/read/refresh. Dependencies: existing
owner-scoped conversation/source, file-grant/workspace and origin.delivery owners,
portable record CAS and snapshot_content no-follow descriptors. No document
writes, scanner, transcript fetch, prompt injection or model-tool admission.
"""
from __future__ import annotations
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
import errno
import hashlib
import json
import os
import stat
import time
import uuid
from jsonschema import Draft202012Validator
from lib.identity import PrincipalContext, require_user_id
from lib.storage.errors import StorageError
from .repository import ProjectDispatchRepository, evidence_owner_matches
from .scope import _relative, relative_path_allowed

CONTRACT = json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/project_shared_files_v1.schema.json').read_text())
LIMITS = CONTRACT['limits']
NAMESPACE = CONTRACT['namespace']


class ProjectSharedFilesError(ValueError):
    """Invalid, stale, unsafe, unavailable or over-budget reference; no bytes."""


@lru_cache(maxsize=16)
def _validator(name):
    return Draft202012Validator({**CONTRACT, '$ref': '#/$defs/' + name})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def validate(value, name):
    """Schema validation seam for application/route consumers."""
    try:
        if not _validator(name).is_valid(value):
            raise ProjectSharedFilesError('Invalid shared-files ' + name)
        _json(value)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ProjectSharedFilesError('Invalid shared-files ' + name) from exc


def _path(path):
    _relative(path)  # existing runtime/checkout/traversal exclusions
    if len(path.encode('utf-8')) > LIMITS['maxPathBytes']:
        raise ProjectSharedFilesError('Shared-file path byte limit exceeded')
    return path


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _root_identity(root):
    if not os.path.isabs(root) or str(Path(root).resolve()) != root:
        raise ProjectSharedFilesError('Shared-file root must be canonical and not symlinked')
    metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProjectSharedFilesError('Shared-file root must be a real directory')
    return [metadata.st_dev, metadata.st_ino]


def _capture(root, path, identity, limit):
    """Return (status, bytes); never follow links or return a partial pre-image."""
    from lib.project_mod.snapshot_content import anchored_parent, file_fingerprint, read_snapshot_content
    try:
        if _root_identity(root) != list(identity):
            return 'unsafe', None
        with anchored_parent(root, path, tuple(identity)) as (parent, name):
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return 'unsafe', None
            if metadata.st_size > limit:
                return 'oversized', None
            snapshot = SimpleNamespace(root_identity=tuple(identity),
                                       fingerprints={path: file_fingerprint(metadata)})
        content = read_snapshot_content(root, path, snapshot, limit)
        if content is None:
            return 'unavailable', None
        content.decode('utf-8', errors='strict')
        return 'current', content
    except FileNotFoundError:
        return 'missing', None
    except OSError as exc:
        return ('unsafe' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 'unavailable'), None
    except (ValueError, UnicodeError, NotImplementedError):
        return 'unsafe', None


class ProjectSharedFiles:
    """Bounded owner/project/tenant record; no process-global identity defaults.

    dispatch_repository injects existing conversation/turn/attempt/workspace
    ports. Request/file actor labels cannot substitute for current owner metadata.
    """
    def __init__(self, owner, project, *, client=None, dispatch_repository=None, clock=None):
        self.owner = require_user_id(owner, context='shared project files')
        if (not isinstance(project, str) or not project or len(project.encode()) > 4096
                or project != os.path.normpath(project) or not os.path.isabs(project)):
            raise ProjectSharedFilesError('Canonical absolute project path required')
        self.project = project
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.dispatch = dispatch_repository or ProjectDispatchRepository(owner, project, client=client)
        if self.dispatch.owner != self.owner or self.dispatch.project != project:
            raise PermissionError('Shared-file project owner mismatch')
        self.clock = clock or time.time
        self.key = str(self.owner) + ':' + _digest(project.encode())

    def _principal(self, principal, *, task=None):
        if not isinstance(principal, PrincipalContext) or principal.owner_user_id != self.owner:
            raise PermissionError('Shared-file owning principal required')
        if principal.kind != 'user' and task is None:
            raise PermissionError('Shared-file reads do not grant system/consult tools')
        principal.require_scope('chat')

    def _load(self, principal):
        row = self.client(write=False).query('record.get', {'namespace': NAMESPACE, 'key': self.key})
        if row is None:
            return {'owner': self.owner, 'projectPath': self.project,
                    'tenant': principal.tenant_id, 'entries': []}, 0
        if type(row.get('version')) is not int or row['version'] < 1:
            raise ProjectSharedFilesError('Invalid shared-file record version')
        self._check(row.get('value'), principal)
        return deepcopy(row['value']), row['version']

    def _check(self, value, principal):
        validate(value, 'record')
        if (value['owner'] != self.owner or value['projectPath'] != self.project
                or value['tenant'] != principal.tenant_id):
            raise PermissionError('Shared-file owner/project/tenant mismatch')
        if len(_json(value)) > LIMITS['maxRecordBytes']:
            raise ProjectSharedFilesError('Shared-file record capacity exceeded; no evidence evicted')
        ids, commands = set(), set()
        for stored in value['entries']:
            entry = stored['entry']
            _path(entry['path'])
            if entry['replacementPath'] is not None:
                _path(entry['replacementPath'])
            if stored['tenant'] != principal.tenant_id:
                raise PermissionError('Shared-file tenant mismatch')
            if entry['id'] in ids or stored['commandId'] in commands:
                raise ProjectSharedFilesError('Duplicate shared-file reference or command')
            ids.add(entry['id'])
            commands.add(stored['commandId'])

    def _change(self, principal, update):
        for _ in range(LIMITS['casAttempts']):
            original, version = self._load(principal)
            value = deepcopy(original)
            result = update(value)
            self._check(value, principal)
            if value == original:
                return deepcopy(result)
            try:
                self.client(write=True).command('record.put', {
                    'namespace': NAMESPACE, 'key': self.key, 'value': value,
                    'expected_version': version}, 'project-shared-files:' + uuid.uuid4().hex)
                return deepcopy(result)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise ProjectSharedFilesError('Concurrent/uncertain shared-file update; read before retry')

    def _human(self, source):
        validate(source, 'authorizationSource')
        self.dispatch._conversation(source['conversationId'])  # metadata only, never transcript
        self.dispatch.validate_human_source(source)

    def _grant(self, task, principal, path):
        from .admission import owned_workspace, principal_for, validate_task
        binding = validate_task(task)
        if (binding is None or binding['owner'] != self.owner
                or binding['projectPath'] != self.project or principal != principal_for(binding)):
            raise PermissionError('Current matching files grant required')
        assignment = self.dispatch.validate(binding)
        profile = assignment['request'].get('profile') or {}
        if (profile.get('mode') != 'files' or 'read_files' not in profile.get('tools', [])
                or not relative_path_allowed(path, profile.get('readPaths', []))):
            raise PermissionError('Current files read grant does not allow reference')
        return binding, owned_workspace(task, binding)

    def _access(self, stored, principal, task=None):
        source = stored['entry']['source']
        if task is not None:
            _, workspace = self._grant(task, principal, stored['entry']['path'])
            if stored['root'] not in (self.project, workspace['workspace_path']):
                raise PermissionError('Files grant cannot read another candidate workspace')
        elif source['kind'] == 'human':

            if stored['root'] != self.project:
                raise PermissionError('Human shared-file reference escaped canonical project')
            self._human(source['authorizationSource'])
        else:
            assignment = self.dispatch.read(source['assignmentId'])
            self.dispatch._conversation(assignment['request']['recipientId'])
            if assignment['grant']['tenant'] != principal.tenant_id:
                raise PermissionError('Candidate grant tenant mismatch')
            row = self.dispatch.get_workspace(self.project, source['taskId'], user_id=self.owner)
            if (not isinstance(row, dict) or row.get('user_id') != self.owner
                    or row.get('project_root') != self.project or row.get('task_id') != source['taskId']
                    or row.get('workspace_path') != stored['root'] or row.get('state') == 'discarded'):
                raise PermissionError('Candidate workspace ownership changed')

    def _delivered(self, evidence, principal, path, content):
        from .publication import delivery_view
        assignment = self.dispatch.read(evidence['assignmentId'])
        if (assignment['grant']['owner'] != self.owner
                or assignment['grant']['tenant'] != principal.tenant_id
                or assignment['request']['projectPath'] != self.project):
            raise PermissionError('Delivery owner/project/tenant mismatch')
        execution = assignment.get('execution') or {}
        attempt = self.dispatch.get_attempt(execution.get('attemptId'))
        observed = []
        def workspace_get(*args, **kwargs):
            workspace = self.dispatch.get_workspace(*args, **kwargs)
            observed.append(workspace)
            return workspace
        delivery = delivery_view(assignment, attempt, workspace_get)
        from .admission import KEY
        workspace = observed[0] if observed and isinstance(observed[0], dict) else {}
        origin = workspace.get('origin') or {}
        receipt = origin.get('delivery') or {}
        if (not isinstance(attempt, dict) or not evidence_owner_matches(attempt, self.owner)
                or origin.get(KEY) != self.dispatch._admission(assignment)
                or workspace.get('state') == 'discarded'
                or workspace.get('checkpoint_sha') != evidence['checkpointSha']
                or receipt.get('checkpointSha') != evidence['checkpointSha']
                or (assignment.get('publication') or {}).get('grantRevision') != assignment['grant']['revision']
                or not delivery or delivery['status'] != 'delivered'
                or delivery['taskId'] != evidence['taskId']
                or delivery['checkpointSha'] != evidence['checkpointSha']
                or (assignment.get('publication') or {}).get('checkpointSha') != evidence['checkpointSha']):
            raise PermissionError('Actual canonical delivery evidence required')
        validate(delivery['targetSha'], 'sha')
        # Delivery is project-level. THIS file must match the immutable delivered
        # tree, not a current arbitrary docs path or a model's adoption label.
        from lib import integration_control as ic
        spec = delivery['targetSha'] + ':' + path
        size = ic._git(Path(self.project), ['cat-file', '-s', spec], check=True).stdout.strip()
        if not size.isdigit() or int(size) > LIMITS['maxContentBytes']:
            raise ProjectSharedFilesError('Delivered file exceeds content budget')
        object_id = ic._git(Path(self.project), ['rev-parse', '--verify', spec], check=True).stdout.strip()
        algorithm = 'sha1' if len(object_id) == 40 else 'sha256'
        blob_id = hashlib.new(algorithm, b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if len(content) != int(size) or object_id != blob_id:
            raise PermissionError('Current file does not match actual delivered version')
        return assignment

    def _request(self, request, name):
        validate(request, name)
        if request['projectPath'] != self.project:
            raise PermissionError('Shared-file request project mismatch')

    def register(self, request, principal, command_id, *, task=None):
        self._request(request, 'register')
        self._principal(principal, task=task)
        validate(command_id, 'identifier')
        if len(command_id.encode()) > LIMITS['maxCommandBytes'] or not command_id.strip():
            raise ProjectSharedFilesError('Bounded command identity required')
        path = _path(request['path'])
        if len(request['purpose'].encode()) > LIMITS['maxPurposeBytes']:
            raise ProjectSharedFilesError('Shared-file purpose byte budget exceeded')
        request_digest = _digest(_json({'request': request, 'principal': principal.to_payload(),
                                      'taskId': task.get('id') if task else None}))
        def add(value):
            replay = next((s for s in value['entries'] if s['commandId'] == command_id), None)
            if replay:
                if replay['requestDigest'] != request_digest:
                    raise ProjectSharedFilesError('Command reused with different shared-file request')
                self._access(replay, principal, task)
                return replay['entry']
            source = {'kind': 'human', 'subject': principal.subject_id,
                      'authorizationSource': request.get('authorizationSource'),
                      'assignmentId': None, 'grantRevision': None, 'taskId': None}
            root, checkpoint, state = self.project, None, 'candidate'
            if task is None:
                self._human(request.get('authorizationSource'))
            else:
                if request.get('authorizationSource') or request.get('delivery'):
                    raise PermissionError('Files candidate cannot spoof human/delivery source')
                binding, workspace = self._grant(task, principal, path)
                root, checkpoint = workspace['workspace_path'], workspace.get('checkpoint_sha')
                source.update(kind='files_grant', assignmentId=binding['assignmentId'],
                              grantRevision=binding['revision'], taskId=task['id'])
            identity = _root_identity(root)
            status, content = _capture(root, path, identity, LIMITS['maxContentBytes'])
            if content is None:
                raise ProjectSharedFilesError('Shared-file capture failed: ' + status)
            if request.get('delivery'):
                evidence = request['delivery']
                assignment = self._delivered(evidence, principal, path, content)
                source.update(assignmentId=assignment['id'], grantRevision=assignment['grant']['revision'],
                              taskId=evidence['taskId'])
                checkpoint, state = evidence['checkpointSha'], 'adopted'
            identity_key = _digest(_json([self.key, principal.tenant_id, root, path]))
            old = next((s for s in value['entries'] if s['entry']['id'] == identity_key), None)
            expected = old['entry']['revision'] if old else 0
            if request['expectedRevision'] != expected:
                raise ProjectSharedFilesError('Stale shared-file expected revision')
            if old is None and len(value['entries']) >= LIMITS['entriesPerProject']:
                raise ProjectSharedFilesError('Shared-file capacity reached; no evidence evicted')
            entry = {'id': identity_key, 'revision': expected + 1, 'path': path,
                     'purpose': request['purpose'], 'digest': _digest(content), 'byteLength': len(content),
                     'source': source, 'checkpointSha': checkpoint, 'knowledgeState': state,
                     'status': 'current', 'observedDigest': _digest(content), 'replacementPath': None,
                     'updatedAt': self.clock()}
            stored = {'entry': entry, 'root': root, 'rootIdentity': identity,
                      'commandId': command_id, 'requestDigest': request_digest, 'tenant': principal.tenant_id}
            if old:
                value['entries'][value['entries'].index(old)] = stored
            else:
                value['entries'].append(stored)
            return entry
        return self._change(principal, add)

    @staticmethod
    def _find(value, reference):
        validate(reference, 'reference')
        stored = next((s for s in value['entries'] if s['entry']['id'] == reference['id']), None)
        if stored is None:
            raise ProjectSharedFilesError('Shared-file reference not found in owned project')
        if any(stored['entry'][k] != reference[k] for k in ('revision', 'digest')):
            raise ProjectSharedFilesError('Stale shared-file version reference')
        return stored

    def _probe(self, stored):
        entry = deepcopy(stored['entry'])
        status, content = _capture(stored['root'], entry['path'], stored['rootIdentity'], LIMITS['maxContentBytes'])
        entry['observedDigest'] = _digest(content) if content is not None else None
        entry['status'] = 'changed' if content is not None and entry['observedDigest'] != entry['digest'] else status
        return entry

    def list(self, principal, *, cursor=None, limit=None):
        self._principal(principal)
        limit = LIMITS['pageEntries'] if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= LIMITS['pageEntries']:
            raise ProjectSharedFilesError('Invalid shared-file page size')
        value, version = self._load(principal)
        start = 0
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 256:
                raise ProjectSharedFilesError('Invalid shared-file cursor')
            parts = cursor.split(':')
            if (len(parts) != 3 or parts[0] != _digest(_json([self.key, principal.tenant_id]))[:24]
                    or not all(p.isdigit() for p in parts[1:]) or int(parts[1]) != version):
                raise ProjectSharedFilesError('Stale/foreign shared-file cursor; restart page')
            start = int(parts[2])
            if not 0 < start < len(value['entries']):
                raise ProjectSharedFilesError('Invalid shared-file cursor offset')
        entries = []
        for stored in value['entries'][start:start + limit]:
            self._access(stored, principal)
            entries.append(self._probe(stored))
        end = start + len(entries)
        next_cursor = (f'{_digest(_json([self.key, principal.tenant_id]))[:24]}:{version}:{end}'
                       if end < len(value['entries']) else None)
        page = {'projectPath': self.project, 'revision': version, 'entries': entries, 'nextCursor': next_cursor}
        validate(page, 'page')
        if len(_json(page)) > LIMITS['maxPageBytes']:
            raise ProjectSharedFilesError('Shared-file page byte capacity exceeded')
        return page

    def read(self, request, principal, *, task=None):
        self._request(request, 'read')
        self._principal(principal, task=task)
        value, _ = self._load(principal)
        stored = self._find(value, request['reference'])
        self._access(stored, principal, task)
        entry = stored['entry']
        status, content = _capture(stored['root'], entry['path'], stored['rootIdentity'],
                                   request.get('maxBytes', LIMITS['maxContentBytes']))
        if content is None or _digest(content) != entry['digest']:
            raise ProjectSharedFilesError('Shared-file exact version unavailable: ' +
                                          ('changed' if content is not None else status))
        current, _ = self._load(principal)
        self._access(self._find(current, request['reference']), principal, task)
        return {'reference': deepcopy(request['reference']), 'path': entry['path'],
                'digest': entry['digest'], 'byteLength': len(content),
                'trust': 'untrusted_data', 'content': content.decode('utf-8')}

    def refresh(self, request, principal):
        self._request(request, 'refresh')
        self._principal(principal)
        def update(value):
            stored = self._find(value, request['reference'])
            self._access(stored, principal)
            entry = self._probe(stored)
            if request.get('replacementPath') is not None:
                self._human(request.get('authorizationSource'))
                entry['replacementPath'] = _path(request['replacementPath'])
            # Observations keep the old version pin; never silently adopt bytes.
            if entry != stored['entry']:
                entry['updatedAt'] = self.clock()
                stored['entry'] = entry
            return entry
        return self._change(principal, update)
