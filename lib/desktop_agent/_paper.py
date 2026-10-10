"""Device paper builds over the existing desktop command stream.

The polling runtime owns this worker instance and its execution threads.
Commands resolve declared share roots again on the device. Compilation uses
the shared frozen-source compiler; only manifest/artifact bytes cross the
bridge. No server path or persistent device artifact registry is accepted.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from lib.paper.builds.bundle import iter_bundle
from lib.paper.builds.compiler import compile_snapshot
from lib.paper.builds.contract import limit, validate
from lib.paper.builds.errors import BuildError, check_cancelled
from lib.paper.builds.location import build_location
from lib.paper.builds.sources import contained_path, resolve_main, snapshot_sources
from ._project import _resolve


def resolve_document(params):
    validate(params, 'PaperDesktopResolveV1')
    root, candidate = _resolve(params['root'], params['path'])
    root, candidate = Path(root), Path(candidate)
    main = resolve_main(root, candidate, main_file=params.get('main_file', ''))
    project = contained_path(root, params['project_path']) if params.get('project_path') else main.parent
    if not project.is_dir():
        raise BuildError('project_root', 'Choose a paper source directory')
    main = contained_path(project, main)
    edited = contained_path(project, candidate)
    return validate({'schema_version': 1, 'project_path': project.relative_to(root).as_posix(),
                     'main_file': main.relative_to(project).as_posix(),
                     'edited_file': edited.relative_to(project).as_posix()}, 'PaperDesktopDocumentV1')


class _Cancellation:
    def __init__(self, stopped, connected):
        self.local = threading.Event()
        self.stopped, self.connected = stopped, connected
        self.deadline = time.monotonic() + limit('compile_timeout_seconds') + limit('transfer_timeout_seconds')

    def is_set(self):
        return self.local.is_set() or self.stopped.is_set() or not self.connected() or time.monotonic() >= self.deadline


class PaperBuildWorker:
    def __init__(self, stopped, connected, *, capacity=None, temporary_parent=None):
        self.stopped, self.connected = stopped, connected
        self.temporary_parent = temporary_parent
        self.pending = {}
        self._closing = False
        self.condition = threading.Condition()

    def admit(self, command_id):
        with self.condition:
            if command_id in self.pending or self._closing or self.stopped.is_set():
                return False
            self.pending[command_id] = _Cancellation(self.stopped, self.connected)
            return True

    def cancel(self, command_id):
        with self.condition:
            control = self.pending.get(command_id)
            if control is not None:
                control.local.set()

    def release(self, command_id):
        with self.condition:
            self.pending.pop(command_id, None)
            self.condition.notify_all()

    def execute(self, command_id, params, emit):
        try:
            validate(params, 'PaperDesktopBuildV1')
            if params['build_id'] != command_id:
                raise BuildError('build_identity', 'Paper command and build identities differ')
            with self.condition:
                cancel = self.pending[command_id]
            check_cancelled(cancel)
            _, project = _resolve(params['root'], params['project_path'])
            project = Path(project)
            main = contained_path(project, params['main_file'])
            edited = contained_path(project, params['edited_file']).relative_to(project).as_posix()
            with TemporaryDirectory(prefix='tofu-paper-', dir=self.temporary_parent) as temporary:
                directory = Path(temporary)
                snapshot = snapshot_sources(project, main, directory / 'sources', cancel=cancel)
                build = compile_snapshot(snapshot, directory / 'build',
                                         owner_user_id=params['owner_user_id'],
                                         document_id=params['document_id'], build_id=params['build_id'],
                                         target='desktop', engine=params.get('engine', 'auto'), cancel=cancel)
                check_cancelled(cancel)
                location = build_location(build, snapshot, edited,
                                          line=params.get('line'), needle=params.get('needle', ''))
                for stream, data in iter_bundle(build, location=location, cancel=cancel):
                    emit(stream, data, cancel)
                emit('paper-end', '', cancel)
                return {'schema_version': 1, 'build_id': params['build_id']}
        finally:
            self.release(command_id)

    def close(self):
        with self.condition:
            self._closing = True
            for control in self.pending.values():
                control.local.set()
            self.condition.wait_for(lambda: not self.pending)
