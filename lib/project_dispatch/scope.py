"""Live file-grant admission and descriptor confinement for project dispatch.

Exports: profile_for, validate_profile, admit_tool and tool_scope. Admission owns
identity/workspace lookup; this module owns relative-prefix policy and anchored
regular-file I/O. Ordinary tasks have no scope. No shell, ref, remote, plugin or
workspace-registration authority can be borrowed from a file grant.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import os
import stat

from lib.llm.llm_errors import AbortedError

FILE_TOOLS = frozenset({'read_files', 'write_file', 'edit_file'})
_CONSULT = {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}
_current = ContextVar('project_dispatch_file_scope', default=None)


def _relative(path):
    if (not isinstance(path, str) or not path or len(path) > 1024
            or path != path.strip() or any(ord(char) < 32 for char in path)
            or '\\' in path or ':' in path or '~' in path or '%' in path
            or path.startswith('/') or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ValueError('File scope requires an unaliased canonical project-relative path')
    parts = path.split('/')
    # Runtime authority, checkout control and undo evidence are never model files.
    if any(part.lower() in {'.git', '.worktrees', 'data', 'logs', 'uploads',
                           'write-intents-v1', 'modifications.json', 'project.json',
                           '.env', '.venv', 'node_modules', '__pycache__', 'target', 'dist', 'build'}
           or part.lower().startswith(('.tofu', '.env.')) or part.lower() == '.native' for part in parts):
        raise ValueError('File scope cannot access runtime or checkout metadata')
    return path


def relative_path_allowed(path, prefixes):
    """Segment-prefix membership shared with checkpoint publication admission."""
    try:
        path = _relative(path)
        return any(prefix == '.' or path == _relative(prefix) or path.startswith(_relative(prefix) + '/')
                   for prefix in prefixes)
    except (TypeError, ValueError):
        return False


def validate_profile(request):
    from .repository import _validate_schema
    profile = request.get('profile', _CONSULT)
    _validate_schema(profile, 'profile')
    for path in profile['readPaths'] + profile['writePaths']:
        if path != '.':
            _relative(path)
    if profile['mode'] == 'files':
        if any(name not in FILE_TOOLS for name in profile['tools']):
            raise ValueError('Only first-party explicit file tools have a file fence')
        if any(name in profile['tools'] for name in ('write_file', 'edit_file')) and not profile['writePaths']:
            raise ValueError('Write tools require an explicit write prefix')
        if any(not ('.' in profile['readPaths'] if path == '.'
                    else relative_path_allowed(path, profile['readPaths'])) for path in profile['writePaths']):
            raise ValueError('Write prefixes must also permit their pre-image reads')
    return deepcopy(profile)


def profile_for(assignment):
    return validate_profile(assignment['request'])


def _exact_fields(value, allowed):
    if not isinstance(value, dict) or any(key not in allowed for key in value):
        raise ValueError('File scope refuses unknown/internal control fields and alternate transports')


def _paths(name, args):
    if name == 'read_files':
        _exact_fields(args, {'path', 'start_line', 'end_line', 'reads', 'bind_sources'})
        if 'bind_sources' in args and type(args['bind_sources']) is not bool:
            raise ValueError('bind_sources must be boolean')
        if 'reads' in args:
            reads = args['reads']
            if not isinstance(reads, list) or not reads:
                raise ValueError('Scoped reads must be a nonempty explicit file list')
            # Do not hide a second path behind the batch shape.
            if any(key in args for key in ('path', 'start_line', 'end_line')):
                raise ValueError('Choose a single read or an explicit batch, not both')
        else:
            reads = [{key: value for key, value in args.items() if key != 'bind_sources'}]
        for read in reads:
            _exact_fields(read, {'path', 'start_line', 'end_line'})
            for key in ('start_line', 'end_line'):
                if key in read and (type(read[key]) is not int or read[key] < 1):
                    raise ValueError('Read ranges must be positive line numbers')
            if read.get('end_line', read.get('start_line', 1)) < read.get('start_line', 1):
                raise ValueError('Read range is reversed')
        return [read.get('path') for read in reads]
    if name == 'write_file':
        _exact_fields(args, {'path', 'content', 'description', 'append'})
        if (not isinstance(args.get('content'), str)
                or 'append' in args and type(args['append']) is not bool):
            raise ValueError('Scoped writes require literal text and a boolean append flag')
        return [args.get('path')]
    _exact_fields(args, {'description', 'edits', 'atomic'})
    if 'atomic' in args and type(args['atomic']) is not bool:
        raise ValueError('Scoped edits require a boolean atomic flag')
    edits = args.get('edits')
    if not isinstance(edits, list) or not edits:
        raise ValueError('Scoped edits require a nonempty explicit edit list')
    for edit in edits:
        _exact_fields(edit, {'path', 'operation', 'anchor', 'content', 'replace_all', 'description'})
        if (edit.get('operation') not in {'replace', 'insert_before', 'insert_after'}
                or not isinstance(edit.get('anchor'), str) or not edit['anchor']
                or not isinstance(edit.get('content'), str)
                or 'replace_all' in edit and type(edit['replace_all']) is not bool):
            raise ValueError('Scoped edits require an explicit text operation')
    return [edit.get('path') for edit in edits]


@dataclass(frozen=True)
class FileScope:
    task: dict
    root: str
    root_identity: tuple
    tool: str
    paths: tuple
    read_prefixes: tuple
    write_prefixes: tuple

    def check(self, path, *, write=False):
        from lib.project_mod.tool_admission import cancellation
        control = cancellation()
        if control is not None and control.is_set():
            raise AbortedError('File dispatch was cancelled before I/O')
        from .admission import owned_workspace, validate_task
        binding = validate_task(self.task)
        row = owned_workspace(self.task, binding)
        if row['workspace_path'] != self.root:
            raise AbortedError('File execution workspace ownership changed')
        path = _relative(path)
        if (path not in self.paths or not relative_path_allowed(path, self.read_prefixes)
                or write and (self.tool == 'read_files' or not relative_path_allowed(path, self.write_prefixes))):
            raise ValueError('Target is outside the exact admitted tool/path prefixes')
        return path

    @contextmanager
    def parent(self, path, *, write=False, create=False):
        from lib.project_mod.snapshot_content import anchored_parent
        self.check(path, write=write)
        if not create:
            with anchored_parent(self.root, path, self.root_identity) as pair:
                yield pair
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        descriptor = os.open(self.root, flags)
        try:
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != self.root_identity:
                raise ValueError('Owned scope root changed')
            components = path.split('/')
            for index, component in enumerate(components[:-1], 1):
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    ancestor = '/'.join(components[:index])
                    if not relative_path_allowed(ancestor, self.write_prefixes):
                        raise ValueError('Creating a parent would expand the accepted write prefix')
                    self.check(path, write=True)
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, components[-1]
        finally:
            os.close(descriptor)

    def read(self, path, limit=None, *, missing=False, write=False):
        """No-follow, nonblocking descriptor read; no converter/attachment fallback."""
        from lib.project_mod.snapshot_content import file_fingerprint
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError('Explicit read limit must be a nonnegative integer')
        try:
            with self.parent(path, write=write) as (parent, name):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                     | getattr(os, 'O_CLOEXEC', 0), dir_fd=parent)
                try:
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode) or write and before.st_nlink != 1:
                        raise ValueError('Scoped targets must be regular files; hard-linked writes are refused')
                    data = bytearray()
                    while limit is None or len(data) <= limit:
                        chunk = os.read(descriptor, 65536 if limit is None else min(65536, limit + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                    if limit is not None and len(data) > limit:
                        raise ValueError('Scoped file exceeds the bounded content budget')
                    fingerprint = file_fingerprint(before)
                    if file_fingerprint(os.fstat(descriptor)) != fingerprint:
                        raise ValueError('File changed during scoped read')
                finally:
                    os.close(descriptor)
            with self.parent(path, write=write) as (parent, name):
                if file_fingerprint(os.stat(name, dir_fd=parent, follow_symlinks=False)) != fingerprint:
                    raise ValueError('File path changed during scoped read')
            return bytes(data), fingerprint
        except FileNotFoundError:
            if missing:
                return None, None
            raise

    def publish(self, path, data, expected, *, preimage, before_replace=None, after_replace=None):
        """Atomic dirfd replacement, rechecking the pre-image after journal preparation."""
        import secrets
        from lib.project_mod.snapshot_content import file_fingerprint
        from lib.project_mod.write_tools._ops import _WriteDurabilityFailed
        with self.parent(path, write=True, create=True) as (parent, name):
            def unchanged():
                self.check(path, write=True)
                # Re-walk the current root chain, not just the pinned directory.
                with self.parent(path, write=True) as (current, _):
                    if (os.fstat(current).st_dev, os.fstat(current).st_ino) != (os.fstat(parent).st_dev, os.fstat(parent).st_ino):
                        raise ValueError('Target parent changed before scoped publication')
                try:
                    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if expected is not None:
                        raise ValueError('Pre-image disappeared before scoped publication')
                    return None
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or expected is None or file_fingerprint(metadata) != expected):
                    raise ValueError('Pre-image changed or unsafe link appeared before scoped publication')
                # Network filesystems can keep the same inode/timestamps for
                # same-size rewrites. Undo admission must compare exact bytes,
                # not mistake an unchanged stat tuple for an unchanged image.
                current, current_fingerprint = self.read(path, len(preimage), write=True)
                if current != preimage or current_fingerprint != expected:
                    raise ValueError('Pre-image content changed before scoped publication')
                return stat.S_IMODE(metadata.st_mode)
            mode = unchanged()
            stage = '.tofu_atomic_' + secrets.token_hex(16)
            descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o666 if mode is None else 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    if mode is not None:
                        os.fchmod(handle.fileno(), mode)
                    os.fsync(handle.fileno())

                    staged = os.fstat(handle.fileno())
                def stage_unchanged():
                    current = os.stat(stage, dir_fd=parent, follow_symlinks=False)
                    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                            or file_fingerprint(current) != file_fingerprint(staged)):
                        raise ValueError('Scoped stage was replaced or changed before publication')
                stage_unchanged()
                if before_replace is not None:
                    before_replace(parent, stage)
                unchanged()
                stage_unchanged()
                os.replace(stage, name, src_dir_fd=parent, dst_dir_fd=parent)
                stage = None
                if after_replace is not None:
                    after_replace()
                try:
                    os.fsync(parent)
                except OSError as error:
                    raise _WriteDurabilityFailed() from error
            finally:
                if stage is not None:
                    os.unlink(stage, dir_fd=parent)


def admit_tool(task, name, args, project_path=None):
    """Fresh durable authority before refs, remote routing, preimages or I/O."""
    from .admission import validate_task, repository_for, owned_workspace
    task = task or {}
    binding = validate_task(task)
    if binding is None:
        return None
    profile = profile_for(repository_for(binding).validate(binding))
    if profile['mode'] == 'coordinator':
        if (name not in profile['tools'] or task.get('_tool_env') is not None
                or project_path is not None and project_path != binding['projectPath']):
            raise AbortedError('Coordinator call exceeds its original continuation policy')
        return None
    if profile['mode'] == 'project':
        if name not in profile['tools']:
            raise AbortedError('Worker tool is outside its original Coordinator capability ceiling')
        if task.get('_tool_env') is not None:
            raise AbortedError('Delegated workers cannot replace their tool environment')
        row = owned_workspace(task, binding)
        if project_path is not None and project_path != row['workspace_path']:
            raise AbortedError('Worker call root differs from its registered isolated workspace')
        # Ordinary project, command, browser and MCP owners keep their own
        # admission/approval/teardown. This profile is not a file sandbox claim.
        return None
    if profile['mode'] != 'files' or name not in profile['tools'] or name not in FILE_TOOLS:
        raise AbortedError('Project dispatch has no authority for this exact tool')
    cfg = task.get('config') or {}
    if any(cfg.get(key) for key in ('remoteWorktree', 'remoteWorktreeBinding', 'remoteWorkspace',
                                   'workspaceRef', 'projectPaths', 'allowOutsideWorkspace')):
        raise AbortedError('File dispatch cannot use remote or expanded workspace authority')
    from lib.desktop.remote import remote_worktree_binding
    if remote_worktree_binding(cfg):
        raise AbortedError('File dispatch cannot use a remote transport')
    paths = tuple(_relative(path) for path in _paths(name, args))
    for path in paths:
        if (not relative_path_allowed(path, profile['readPaths'])
                or name != 'read_files' and not relative_path_allowed(path, profile['writePaths'])):
            raise AbortedError('File target is outside its accepted read/write prefixes')
    row = owned_workspace(task, binding)
    root = row['workspace_path']
    if project_path is not None and project_path != root:
        raise AbortedError('File call root differs from its durably owned workspace')
    if os.path.realpath(root) != root:
        raise AbortedError('Owned workspace root must not be a symlink alias')
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd or os.replace not in os.supports_dir_fd and os.rename not in os.supports_dir_fd:
        raise AbortedError('Descriptor-confined file I/O is unavailable on this platform')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
    finally:
        os.close(descriptor)
    return FileScope(task, root, identity, name, paths, tuple(profile['readPaths']), tuple(profile['writePaths']))


def current_scope():
    return _current.get()


@contextmanager
def tool_scope(task, name, args, project_path=None):
    from lib.project_mod.tool_admission import tool_admission_scope, cancellation
    inherited = current_scope()
    if inherited is not None:
        if task is None:
            task = inherited.task
        elif task is not inherited.task:
            raise AbortedError('File scope cannot be replaced by another task/child context')
    control = cancellation()
    runtime = control.runtime_context if control is not None else None
    scope = admit_tool(task, name, args, project_path)
    token = _current.set(scope)
    try:
        with tool_admission_scope(task, runtime, tool_name=name):
            yield scope
    finally:
        _current.reset(token)


def assert_firstparty_file_handler(name, handler):
    """Main gateway calls this before any custom handler/transport can shadow a name."""
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    if name not in FILE_TOOLS or handler is not _handle_project_tool:
        raise AbortedError('File grants require the exact first-party project handler')


def checkpoint_paths_allowed(request, paths):
    profile = validate_profile(request)
    return profile['mode'] == 'files' and all(relative_path_allowed(path, profile['writePaths']) for path in paths)
