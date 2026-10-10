"""Owned document binding and preview application commands.

The conversation repository supplies authorized roots. Execution adapters own
source access, while the scheduler owns admission and publication. HTTP callers
receive logical identities and immutable artifact URLs, never host paths.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .compiler import compile_snapshot
from .contract import validate
from .errors import BuildError, check_cancelled
from .location import build_location, needle_to_line  # noqa: F401 — public pure helper
from .runtime import PreviewBuildScheduler, get_scheduler
from .sources import contained_path, resolve_main, snapshot_sources


@dataclass(frozen=True)
class HostDocument:
    owner_user_id: int
    document_id: str
    project_root: Path
    main_file: str
    edited_file: str
    source: dict


def _identity(parts: dict) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def owned_settings(conversation_id: str, owner_user_id: int) -> dict:
    from lib.conversations.repository import get_conversation
    conversation = get_conversation(conversation_id, user_id=owner_user_id, include_messages=False)
    if conversation is None:
        raise BuildError('document_not_found', 'Paper conversation is unavailable')
    return dict(conversation.get('settings') or {})


def bind_host_document(payload: dict, *, owner_user_id: int, settings: dict) -> HostDocument:
    """Resolve only roots from the caller's owned conversation projection."""
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id < 1:
        raise BuildError('owner', 'Paper document requires an explicit owner')
    roots = []
    references = {}
    for value in [settings.get('projectPath'), *(settings.get('projectPaths') or [])]:
        if not isinstance(value, str) or not value.strip():
            continue
        if value.startswith('remote:'):
            continue
        if payload.get('project_root') and value != payload['project_root']:
            continue
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except OSError:
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
            references[path] = value
    if not roots:
        raise BuildError('project_root', 'Choose a project directory for this conversation')
    raw = payload['path']
    selected_root = roots[0]
    path = Path(raw).expanduser()
    if not path.is_absolute() and ':' in raw:
        name, relative = raw.split(':', 1)
        matches = [root for root in roots if root.name == name]
        if len(matches) != 1:
            raise BuildError('project_root', 'Choose an unambiguous project root')
        selected_root, path = matches[0], Path(relative)
    candidate = path if path.is_absolute() else selected_root / path
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise BuildError('source_path', 'Paper source is unavailable') from exc
    authorized = [root for root in roots if candidate.is_relative_to(root)]
    if not authorized:
        raise BuildError('source_path', 'Paper source is outside this conversation project')
    authorized_root = max(authorized, key=lambda root: len(root.parts))
    main = resolve_main(authorized_root, candidate, main_file=payload.get('main_file', ''))
    project_root = (contained_path(authorized_root, payload['project_path'])
                    if payload.get('project_path') else main.parent)
    if not project_root.is_dir():
        raise BuildError('project_root', 'Choose a paper source directory')
    edited = contained_path(project_root, candidate)
    main = contained_path(project_root, main)
    main_file = main.relative_to(project_root).as_posix()
    identity = _identity({'owner': owner_user_id, 'target': 'host',
                          'project': str(project_root), 'main': main_file})
    relative_main = main.relative_to(authorized_root).as_posix()
    source = {'source': 'local', 'project_root': references[authorized_root],
              'path': relative_main, 'main_file': relative_main, 'label': relative_main}
    return HostDocument(owner_user_id, identity, project_root, main_file,
                        edited.relative_to(project_root).as_posix(), source)


def host_execution(document: HostDocument, payload: dict, scheduler: PreviewBuildScheduler):
    """Build from leased frozen inputs; return only build-bound geometry."""
    def execute(task, publish):
        cancel = task['abort_event']
        check_cancelled(cancel)
        build_id = uuid.uuid4().hex
        with scheduler.cache.stage(document.owner_user_id, document.document_id, build_id) as stage:
            snapshot = snapshot_sources(document.project_root,
                                        document.project_root / document.main_file,
                                        stage / 'sources', cancel=cancel)
            with scheduler.cache.warm_lease(document.owner_user_id, document.document_id) as warm:
                build = compile_snapshot(snapshot, stage / 'build',
                                         owner_user_id=document.owner_user_id,
                                         document_id=document.document_id, build_id=build_id,
                                         engine=payload.get('engine', 'auto'), cancel=cancel,
                                         warm_dir=warm, toolchain_state=scheduler.cache.toolchain_state)
            location = build_location(build, snapshot, document.edited_file,
                                      line=payload.get('line'), needle=payload.get('needle', ''))
            return publish(build, location=location,
                           pdf_url=artifact_url(document.document_id, build_id, 'document.pdf')
                           if build.receipt['status'] == 'success' else None)
    return execute


def start_record_build(records, *, owner_user_id: int, document_id: str,
                       subscriber_id: str, request_key: str,
                       scheduler: PreviewBuildScheduler | None = None) -> dict:
    """Admit an application-owned source tree through the shared build lane.

    Callers authorize their repository owner and create validated SourceRecords
    before entry. This is not a filesystem or conversation authority bypass.
    """
    scheduler = scheduler or get_scheduler()

    def execute(task, publish):
        build_id = uuid.uuid4().hex
        cancel = task['abort_event']
        with scheduler.cache.stage(owner_user_id, document_id, build_id) as stage:
            snapshot = records.freeze(stage / 'sources', cancel=cancel)
            with scheduler.cache.warm_lease(owner_user_id, document_id) as warm:
                build = compile_snapshot(snapshot, stage / 'build', owner_user_id=owner_user_id,
                                         document_id=document_id, build_id=build_id, cancel=cancel,
                                         warm_dir=warm, toolchain_state=scheduler.cache.toolchain_state)
            return publish(build, pdf_url=artifact_url(document_id, build_id, 'document.pdf')
                           if build.receipt['status'] == 'success' else None)

    return scheduler.start(owner_user_id=owner_user_id, document_id=document_id,
                           subscriber_id=subscriber_id, request_key=request_key, execute=execute,
                           meta={'main_file': records.main_file, 'source': 'records'})


def artifact_url(document_id: str, build_id: str, name: str) -> str:
    return f'/api/v1/paper/builds/{document_id}/{build_id}/artifacts/{name}'


def start_preview(payload: dict, *, owner_user_id: int,
                  scheduler: PreviewBuildScheduler | None = None) -> dict:
    validate(payload, 'PaperBuildRequestV1')
    settings = owned_settings(payload['conv_id'], owner_user_id)
    scheduler = scheduler or get_scheduler()
    if payload['source'] == 'overleaf':
        from .overleaf import bind_overleaf_document, overleaf_execution
        document = bind_overleaf_document(payload, owner_user_id=owner_user_id, settings=settings)
        return _start_bound(document, payload, overleaf_execution,
                            scheduler, owner_user_id)
    references = [value for value in [settings.get('projectPath'), *(settings.get('projectPaths') or [])]
                  if isinstance(value, str) and value.strip()]
    selected = payload.get('project_root')
    if selected and selected not in references:
        raise BuildError('project_root', 'Paper project is outside this conversation')
    selected = selected or (references[0] if references else '')
    if not payload.get('project_root'):
        path = payload['path']
        if Path(path).is_absolute():
            selected = ''  # host binding checks every owned local root
        elif ':' in path:
            label = path.split(':', 1)[0]
            matches = [reference for reference in references if
                       (reference.split(':', 2)[-1] if reference.startswith('remote:')
                        else Path(reference).name) == label]
            if len(matches) != 1:
                raise BuildError('project_root', 'Choose an unambiguous project root')
            selected = matches[0]
    if selected.startswith('remote:'):
        from .desktop import bind_desktop_document, desktop_execution
        document = bind_desktop_document(payload, owner_user_id=owner_user_id, root_reference=selected)
        execution = desktop_execution
    else:
        document = bind_host_document(payload, owner_user_id=owner_user_id, settings=settings)
        execution = host_execution
    return _start_bound(document, payload, execution, scheduler, owner_user_id)


def _start_bound(document, payload, execution_factory, scheduler, owner_user_id):
    from .artifact_repository import PaperBuildArtifactRepository
    from .document_repository import PaperDocumentRepository
    from .overleaf_capabilities import source_document
    papers = PaperDocumentRepository(owner_user_id)
    source = (source_document(document.source) if payload['source'] == 'overleaf' else document.source)
    if payload.get('document_id'):
        source = {**source, 'document_id': payload['document_id']}
    registered = papers.register(source)
    document = replace(document, document_id=registered['document_id'])
    payload = {**payload, 'document_id': document.document_id}
    # An id reused with different options cannot accidentally join different work.
    request_key = _identity({key: value for key, value in payload.items() if key != 'subscriber_id'})
    admission_key = _identity({'request': request_key, 'admission': uuid.uuid4().hex})
    generation = 0

    def admit():
        nonlocal generation
        generation = papers.request(document.document_id, payload['conv_id'], admission_key)['generation']

    def retire():
        if generation:
            papers.abandon(document.document_id, generation)

    adapter = execution_factory(document, payload, scheduler)

    def execution(task, publish):
        def retain(build, **extra):
            if build.receipt['status'] != 'success':
                return publish(build, commit=lambda: papers.finish(
                    document.document_id, generation, build.receipt, None), **extra)
            with PaperBuildArtifactRepository(owner_user_id).prepare(build, cancel=task['abort_event']) as proof:
                return publish(build, commit=lambda: papers.finish(
                    document.document_id, generation, build.receipt, proof), **extra)
        return adapter(task, retain)

    task = scheduler.start(owner_user_id=owner_user_id, document_id=document.document_id,
                           request_key=request_key, subscriber_id=payload['subscriber_id'],
                           execute=execution, on_admit=admit, on_retire=retire,
                           meta={'conversationId': payload['conv_id'], 'main_file': document.main_file,
                                 'source': payload['source']})
    return validate({'task_id': task['id'], 'document_id': document.document_id,
                     'subscriber_id': payload['subscriber_id']}, 'PaperBuildAcceptedV1')


def locate_build(document_id: str, build_id: str, payload: dict, *, owner_user_id: int,
                 scheduler: PreviewBuildScheduler | None = None) -> dict:
    validate(payload, 'PaperBuildLocationRequestV1')
    scheduler = scheduler or get_scheduler()
    from .artifact_repository import PaperBuildArtifactRepository
    from .synctex import locate_stream
    with PaperBuildArtifactRepository(owner_user_id).open_build(scheduler.cache, document_id, build_id) as build:
        if build.receipt['status'] != 'success' or not build.receipt['version_verified']:
            return validate({'build_id': build_id, **payload, 'page': None, 'rects': []}, 'PaperBuildLocationV1')
        declared = {row['name'] for row in build.receipt['artifacts']}
        if 'document.synctex.gz' not in declared or 'sources.json' not in declared:
            return validate({'build_id': build_id, **payload, 'page': None, 'rects': []}, 'PaperBuildLocationV1')
        with build.open_artifact('sources.json') as (stream, _):
            files = json.load(stream)
        if payload['file'] not in {row['path'] for row in files}:
            raise BuildError('source_path', 'Source file is not part of this paper build')
        with build.open_artifact('document.synctex.gz') as (stream, _):
            return locate_stream(stream, build_id=build_id, **payload)


def locate_inverse_build(document_id: str, build_id: str, payload: dict, *,
                         owner_user_id: int,
                         scheduler: PreviewBuildScheduler | None = None) -> dict:
    validate(payload, 'PaperBuildInverseLocationRequestV1')
    scheduler = scheduler or get_scheduler()
    from .artifact_repository import PaperBuildArtifactRepository
    from .synctex import inverse_stream
    empty = {'build_id': build_id, 'page': payload['page'], 'file': '', 'line': None}
    with PaperBuildArtifactRepository(owner_user_id).open_build(scheduler.cache, document_id, build_id) as build:
        if build.receipt['status'] != 'success' or not build.receipt['version_verified']:
            return validate(empty, 'PaperBuildInverseLocationV1')
        declared = {row['name'] for row in build.receipt['artifacts']}
        if 'document.synctex.gz' not in declared:
            return validate(empty, 'PaperBuildInverseLocationV1')
        with build.open_artifact('document.synctex.gz') as (stream, _):
            return inverse_stream(stream, build_id=build_id, **payload)
