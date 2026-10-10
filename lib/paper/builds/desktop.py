"""Owner-addressed Desktop Agent adapter for the paper-build service.

The existing desktop bridge carries a versioned document resolution command
and one bounded build stream. No shared filesystem is assumed. The receiving
cache stage is publishable only after bundle identity and integrity validation.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

from lib.desktop import (consume_desktop_command, enqueue_desktop_command,
                         online_agents, release_desktop_command, send_desktop_command)
from lib.desktop.remote import parse_remote_path, validate_remote_binding
from .bundle import BundleReceiver
from .contract import limit, validate
from .errors import BuildError, check_cancelled


@dataclass(frozen=True)
class DesktopDocument:
    owner_user_id: int
    document_id: str
    agent_id: str
    root: str
    project_path: str
    main_file: str
    edited_file: str

    @property
    def source(self):
        from pathlib import PurePosixPath
        main = (PurePosixPath(self.project_path) / self.main_file).as_posix()
        return {'source': 'local', 'project_root': f'remote:{self.agent_id}:{self.root}',
                'path': main, 'main_file': main, 'label': main}


def bind_desktop_document(payload, *, owner_user_id, root_reference):
    remote = parse_remote_path(root_reference)
    if remote is None:
        raise BuildError('project_root', 'Select a connected desktop project')
    agent_id, root = remote
    binding, error = validate_remote_binding(agent_id, root, user_id=str(owner_user_id))
    if error or binding is None:
        raise BuildError('desktop_unavailable', error or 'Desktop project is unavailable')
    agent = next((row for row in online_agents() if row['agent_id'] == agent_id
                  and row.get('user_id') == str(owner_user_id)), None)
    capability = (agent or {}).get('capabilities', {}).get('paper_build')
    if type(capability) is not int or capability != 1:
        raise BuildError('desktop_capability', 'Update the Desktop Agent and enable command execution for paper builds')
    body = {'schema_version': 1, 'root': root, 'path': payload['path'],
            **{key: payload[key] for key in ('project_path', 'main_file') if key in payload}}
    if body['path'].startswith(root + ':'):
        body['path'] = body['path'][len(root) + 1:]
    validate(body, 'PaperDesktopResolveV1')
    resolved, error = send_desktop_command('paper_resolve', body, timeout=10,
                                         target_agent_id=agent_id, user_id=str(owner_user_id))
    if error or not isinstance(resolved, dict) or resolved.get('error'):
        raise BuildError('desktop_source', str(error or (resolved.get('error') if isinstance(resolved, dict) else '')
                                             or 'Cannot resolve desktop paper source')[:1000])
    validate(resolved, 'PaperDesktopDocumentV1')
    identity = {'owner': owner_user_id, 'target': 'desktop', 'agent': agent_id, 'root': root,
                'project': resolved['project_path'], 'main': resolved['main_file']}
    document_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return DesktopDocument(owner_user_id, document_id, agent_id, root,
                           resolved['project_path'], resolved['main_file'], resolved['edited_file'])


def desktop_execution(document, payload, scheduler):
    def execute(task, publish):
        cancel = task['abort_event']
        check_cancelled(cancel)
        build_id = uuid.uuid4().hex
        body = validate({'schema_version': 1, 'root': document.root,
                         'project_path': document.project_path, 'main_file': document.main_file,
                         'edited_file': document.edited_file, 'owner_user_id': document.owner_user_id,
                         'document_id': document.document_id, 'build_id': build_id,
                         **{key: payload[key] for key in ('engine', 'line', 'needle') if key in payload}},
                        'PaperDesktopBuildV1')
        address = {'target_agent_id': document.agent_id, 'user_id': str(document.owner_user_id)}
        timeout = limit('compile_timeout_seconds') + limit('transfer_timeout_seconds')
        with scheduler.cache.stage(document.owner_user_id, document.document_id, build_id) as stage:
            receiver = BundleReceiver(stage / 'build', owner_user_id=document.owner_user_id,
                                      document_id=document.document_id, build_id=build_id, target='desktop')
            command_id, error = enqueue_desktop_command('paper_build', body, **address,
                cmd_id=build_id, ttl=timeout, stream_limits={
                    'bytes': limit('transfer_window_bytes'), 'frames': limit('transfer_window_frames'),
                    'frame_bytes': limit('transfer_frame_bytes')})
            if error:
                raise BuildError('desktop_unavailable', str(error)[:1000])
            completed = False
            try:
                deadline = time.monotonic() + timeout
                while True:
                    check_cancelled(cancel)
                    if time.monotonic() >= deadline:
                        raise BuildError('desktop_timeout', 'Desktop paper build or transfer timed out')
                    state = consume_desktop_command(command_id, **address)
                    if state is None:
                        raise BuildError('desktop_unavailable', 'Desktop paper command expired or was disconnected')
                    if state['error']:
                        raise BuildError('desktop_build', str(state['error'])[:1000])
                    for _, stream, data in state['frames']:
                        if stream == 'paper-end' and data == '':
                            continue
                        receiver.accept(stream, data)
                    if state['settled'] and state['stream_done']:
                        result = state['result']
                        if (not isinstance(result, dict) or type(result.get('schema_version')) is not int
                                or result['schema_version'] != 1 or result.get('build_id') != build_id):
                            raise BuildError('build_identity', 'Desktop build settlement does not match its command')
                        build, location = receiver.finish(cancel=cancel)
                        completed = True
                        check_cancelled(cancel)
                        from .service import artifact_url
                        return publish(build, location=location,
                            pdf_url=artifact_url(document.document_id, build_id, 'document.pdf')
                            if build.receipt['status'] == 'success' else None)
                    time.sleep(0.1)
            finally:
                claimed = release_desktop_command(command_id, **address)
                if claimed and not completed:
                    enqueue_desktop_command('paper_build_cancel', {'cmd_id': command_id},
                                            **address, ttl=5, fire_and_forget=True)
    return execute
