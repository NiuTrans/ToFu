"""Outbound channel attachments (docs/CHANNELS.md C10): admission and verified bytes.

Responsibility: turn requested attachments into durable outbox attachment
records — name, MIME type, size, sha256, kind and where the bytes live — after
checking that each one belongs to the sending conversation (a regular file in
its verified workspace roots, read without following symlinks, one of its own
published artifacts, or a slide deck it produced) and fits 大象's documented
upload limits
(``x-dxAttachments``); find the workspace files a final answer explicitly
links; and, at send time, re-read an original from its source and refuse bytes
that changed after the item was queued. Refusals are ``ChannelError`` codes
whose messages say what to do instead.

Entry points: ``from_tool`` (``channel_send``), ``for_final_answer`` (the
command-chat auto-reply), ``admit``, ``classify``, ``linked_references``,
``identities``, ``read_original``; ``OutboxItem.view`` projects the records.
Dependencies: the task's root policy (``lib.tools.rich_response.task_roots``),
the shared no-follow reader (``lib.artifacts.file_snapshot``) and the artifact
loaders; no network and no channel storage of its own.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lib.channels.model import DX_ATTACHMENTS, ChannelError
from lib.log import get_logger

logger = get_logger(__name__)

TYPES: dict[str, list[str]] = DX_ATTACHMENTS['types']
SIZE_BELOW_BYTES: int = DX_ATTACHMENTS['sizeBelowBytes']
_MIB = 1024 * 1024
_ARTIFACT_FILE_URL = re.compile(r'^/api/artifacts/([A-Za-z0-9_-]{1,64})/(?:download|raw|view)(?:\?[^#]*)?$')
_ARTIFACT_IMAGE_URL = re.compile(r'^/api/response-assets/([A-Za-z0-9_-]{1,64})(?:\?[^#]*)?$')
# The download URL the slide tools return for a deck (lib/tasks_pkg/handlers/motion_video.py).
_SLIDES_FILE_URL = re.compile(r'^/api/(?:v[14]/)?slides/([A-Za-z0-9_-]{1,64})/file(?:\?[^#]*)?$')
_IMAGE_SUFFIX = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/bmp': 'bmp'}
# Markdown links and images ``[text](target "title")`` outside code; the target
# may be wrapped in angle brackets.
_FENCED = re.compile(r'^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1[ \t]*$', re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r'(`+)[^`].*?\1', re.DOTALL)
_LINK = re.compile(r'!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^<>\n]+>|[^\s()<>]+)'
                   r'(?:\s+(?:"[^"\n]*"|\'[^\'\n]*\'|\([^()\n]*\)))?\s*\)')
_SCHEME = re.compile(r'^([A-Za-z][A-Za-z0-9+.-]*):')


def limits_text() -> str:
    images = ', '.join(sorted(s for s, (kind, _) in TYPES.items() if kind == 'image'))
    files = ', '.join(sorted(s for s, (kind, _) in TYPES.items() if kind == 'file'))
    return (f'大象 accepts images ({images}) and files ({files}) under '
            f'{SIZE_BELOW_BYTES // _MIB} MiB each')


def classify(name: str) -> tuple[str, str]:
    """``(kind, MIME type)`` of a file name by its suffix, or a refusal."""
    suffix = Path(name).suffix.lower().lstrip('.')
    if suffix not in TYPES:
        raise ChannelError('attachment_type_unsupported',
                           f'{name}: {limits_text()} (documented upload types); '
                           'zip other files or convert them to PDF')
    kind, mime = TYPES[suffix]
    return kind, mime


def _record(name: str, data_size: int, sha256: str, source: dict[str, Any]) -> dict[str, Any]:
    kind, mime = classify(name)
    if data_size >= SIZE_BELOW_BYTES:
        raise ChannelError('attachment_too_large',
                           f'{name} is {data_size / _MIB:.1f} MiB; {limits_text()}')
    return {'name': name, 'type': mime, 'size': int(data_size), 'sha256': sha256, 'kind': kind,
            'renditionOf': None, 'source': source, 'state': 'pending', 'error': None}


def _snapshot(path: str, roots: list[str], *, include_bytes: bool):
    """The shared descriptor-pinned reader, its failures as actionable refusals."""
    from lib.artifacts.file_snapshot import read_project_snapshot
    shown = path
    try:
        return read_project_snapshot(path, roots, include_bytes=include_bytes, fingerprint=True)
    except PermissionError as error:
        raise ChannelError('attachment_outside_workspace',
                           f'{shown} is outside this conversation\'s workspace; attach a file inside '
                           f'one of its roots ({", ".join(roots)})') from error
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ChannelError('attachment_symlink',
                               f'{shown} is, or passes through, a symbolic link; attach the real file '
                               'inside the workspace') from error
        if error.errno == errno.ENOENT:
            raise ChannelError('attachment_not_found', f'{shown} does not exist in this conversation\'s '
                                                       'workspace') from error
        raise ChannelError('attachment_unreadable', f'{shown} cannot be read ({error.strerror})') from error
    except ValueError as error:
        if str(error) == 'Invalid project path':
            raise ChannelError('attachment_path_invalid',
                               f'{shown}: a path may not contain ".." or NUL; give the file\'s path '
                               'inside the workspace') from error
        if 'regular file' in str(error):
            raise ChannelError('attachment_not_a_file',
                               f'{shown} is not a non-empty regular file') from error
        raise ChannelError('attachment_changed', f'{shown} changed while it was read; try again') from error


def _admit_path(path: str, roots: list[str]) -> dict[str, Any]:
    classify(os.path.basename(path))
    if '..' not in Path(path).parts and '\x00' not in path:
        # A file that is too large is refused before it is read; the reader
        # below stays the only authority on what is inside the workspace. A
        # path through a symlink is left to the reader, which refuses it
        # without telling anything about the link's target.
        candidate = os.path.abspath(path if os.path.isabs(path) else os.path.join(roots[0], path))
        if os.path.realpath(candidate) == candidate and any(
                os.path.commonpath([candidate, os.path.abspath(root)]) == os.path.abspath(root)
                for root in roots):
            try:
                size = os.lstat(candidate).st_size
            except OSError:
                size = 0
            if size >= SIZE_BELOW_BYTES:
                raise ChannelError('attachment_too_large',
                                   f'{os.path.basename(path)} is {size / _MIB:.1f} MiB; {limits_text()}')
    snapshot = _snapshot(path, roots, include_bytes=False)
    return _record(os.path.basename(snapshot.path), snapshot.size, snapshot.sha256,
                   {'path': snapshot.path, 'roots': list(roots)})


def _load_artifact(source: dict[str, Any], owner: int) -> tuple[bytes, str]:
    from lib.artifacts.core import ArtifactNotFoundError
    try:
        if source['store'] == 'file':
            from lib.artifacts.response_files import load_file
            data, info = load_file(source['artifact'], user_id=owner, conv_id=source['conversationId'])
            return data, str(info['filename'])
        from lib.response_assets import load_asset
        data, info = load_asset(source['artifact'], user_id=owner, conv_id=source['conversationId'])
        suffix = _IMAGE_SUFFIX.get(str(info.get('mime')), str(info.get('mime', '')).split('/')[-1])
        return data, f'image-{source["artifact"][:8]}.{suffix}'
    except ArtifactNotFoundError as error:
        raise ChannelError('attachment_not_found',
                           f'Artifact {source["artifact"]} is not a published artifact of this '
                           'conversation') from error


def _slides_deck(task_id: str, owner: int, conversation_id: str) -> tuple[str, str]:
    """``(pptx path, deck directory)`` of a slide deck this conversation produced, or a refusal.

    The job's disk manifest (``lib.production.jobs``) names its owner and
    conversation; the deck directory holds the exported pptx beside its
    ``deck.pptd`` source, the newest export being the current one.
    """
    from lib.production.jobs import read_manifest
    from lib.slides.engine import slides_root
    workdir = os.path.join(slides_root(), 'jobs', task_id)
    manifest = read_manifest(workdir) or {}
    if str(manifest.get('user_id') or '') != str(owner) or str(manifest.get('conv_id') or '') != conversation_id:
        raise ChannelError('attachment_not_found', f'Slide deck {task_id} is not a deck of this conversation')
    deck_dir = os.path.join(workdir, 'deck')
    try:
        with os.scandir(deck_dir) as entries:
            exports = [(entry.stat(follow_symlinks=False).st_mtime_ns, entry.path) for entry in entries
                       if entry.name.lower().endswith('.pptx') and entry.is_file(follow_symlinks=False)]
    except OSError:
        exports = []
    if not exports:
        raise ChannelError('attachment_not_found', f'Slide deck {task_id} has no exported pptx yet')
    return max(exports)[1], deck_dir


def _artifact_source(reference: str, conversation_id: str) -> dict[str, Any] | None:
    for store, pattern in (('file', _ARTIFACT_FILE_URL), ('asset', _ARTIFACT_IMAGE_URL)):
        match = pattern.fullmatch(reference)
        if match:
            return {'artifact': match.group(1), 'store': store, 'conversationId': conversation_id}
    return None


def admit(reference: str, *, roots: list[str], owner: int, conversation_id: str) -> dict[str, Any]:
    """One attachment record for a workspace path or an own-artifact delivery URL."""
    if not isinstance(reference, str) or not reference.strip():
        raise ChannelError('invalid_attachments', 'Each attachment needs a non-empty path')
    source = _artifact_source(reference.strip(), conversation_id)
    if source is not None:
        data, name = _load_artifact(source, owner)
        return _record(name, len(data), hashlib.sha256(data).hexdigest(), source)
    slides = _SLIDES_FILE_URL.fullmatch(reference.strip())
    if slides is not None:
        path, deck_dir = _slides_deck(slides.group(1), owner, conversation_id)
        return _admit_path(path, [deck_dir])
    if not roots:
        raise ChannelError('attachment_outside_workspace',
                           'This conversation has no workspace, so it has no files to attach')
    return _admit_path(reference, roots)


def _scope(task: dict[str, Any]) -> tuple[list[str], Callable[[str], str], set[str]]:
    """The task's verified roots, its path routing (the read_files authority) and root labels."""
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots, route_project_tool_args
    from lib.tools.rich_response import task_roots
    try:
        roots = task_roots(task, (task.get('config') or {}).get('projectPath'))
        labels = {name: state['path']
                  for name, state in ((ensure_execution_roots(task) or {}).get('roots') or {}).items()}
    except Exception as error:
        logger.debug('[Channels] attachment roots unavailable: %s', type(error).__name__)
        return [], lambda value: value, set()

    def route(value: str) -> str:
        if value.startswith('file://'):
            value = value[len('file://'):]
        routed = route_project_tool_args(task, 'read_files', {'path': value})['path']
        if routed != value:
            return routed
        label, colon, rest = value.partition(':')
        if colon and label and '/' not in label:
            for name, path in labels.items():
                if name.lower() == label.lower():
                    return os.path.join(path, rest.lstrip('/'))
        return value
    return roots, route, {name.lower() for name in labels}


def from_tool(task: dict[str, Any], owner: int, conversation_id: str, entries: Any) -> list[dict[str, Any]]:
    """``channel_send`` attachments: every entry admitted, or the first refusal."""
    if entries is None:
        return []
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        raise ChannelError('invalid_attachments', 'attachments must be a non-empty list of {path} objects')
    roots, route, _ = _scope(task)
    records, seen = [], set()
    for entry in entries:
        if set(entry) != {'path'}:
            raise ChannelError('invalid_attachments', 'Each attachment is {path}')
        reference = entry['path']
        record = admit(route(reference) if isinstance(reference, str) else reference, roots=roots,
                       owner=owner, conversation_id=conversation_id)
        identity = _identity(record)
        if identity not in seen:
            seen.add(identity)
            records.append(record)
    return records


def linked_references(markdown: str) -> list[str]:
    """Targets of the Markdown links and images in ``markdown``, outside code, in order."""
    text = _INLINE_CODE.sub(' ', _FENCED.sub('\n', markdown or ''))
    targets = []
    for match in _LINK.finditer(text):
        target = match.group(1)
        if target.startswith('<') and target.endswith('>'):
            target = target[1:-1].strip()
        if target and not target.startswith('#') and target not in targets:
            targets.append(target)
    return targets


def for_final_answer(task: dict[str, Any], owner: int, conversation_id: str,
                     text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The files a final answer explicitly links, and a note per linked file refused.

    Only Markdown links and images whose target is a file of this
    conversation's own workspace, or the download URL of a slide deck it
    produced, are attached; web URLs, artifact links and anything else are
    left alone. It runs on the task's terminal seam, so it reads files only
    (no artifact decoding workers).
    """
    references = linked_references(text)
    if not references:
        return [], []
    roots, route, labels = _scope(task)
    records, notes, seen = [], [], set()
    for reference in references:
        scheme = _SCHEME.match(reference)
        if scheme and scheme.group(1).lower() not in labels and scheme.group(1).lower() != 'file':
            continue  # a web or app URL, never fetched
        candidates = [reference] if unquote(reference) == reference else [reference, unquote(reference)]
        for candidate in candidates:
            if _SLIDES_FILE_URL.fullmatch(candidate):
                target = candidate
            elif _artifact_source(candidate, conversation_id) is None and _workspace_file(route(candidate), roots):
                target = route(candidate)
            else:
                continue  # not a file of this conversation: nothing to attach or to explain
            try:
                record = admit(target, roots=roots, owner=owner, conversation_id=conversation_id)
            except ChannelError as error:
                if error.code in ('attachment_type_unsupported', 'attachment_too_large'):
                    notes.append(str(error))
                    break
                continue
            identity = _identity(record)
            if identity not in seen:
                seen.add(identity)
                records.append(record)
            break
    return records, notes


def _workspace_file(path: str, roots: list[str]) -> bool:
    """Whether ``path`` names a regular file lexically inside ``roots`` (the reader decides the rest)."""
    if not roots or '..' in Path(path).parts or '\x00' in path:
        return False
    candidate = os.path.abspath(path if os.path.isabs(path) else os.path.join(roots[0], path))
    if not any(os.path.commonpath([candidate, os.path.abspath(root)]) == os.path.abspath(root)
               for root in roots):
        return False
    try:
        return stat.S_ISREG(os.lstat(candidate).st_mode)
    except OSError:
        return False


def _identity(record: dict[str, Any]) -> tuple[str, str]:
    source = record.get('source') or {}
    return (str(source.get('path') or source.get('artifact') or ''), record['sha256'])


def identities(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """What makes two requests carry the same attachments (outbox idempotency)."""
    return [(r['name'], r['sha256']) for r in records if r.get('renditionOf') is None]


def read_original(record: dict[str, Any], *, owner: int) -> bytes:
    """The original's bytes from its source, refused when they no longer match the record."""
    source = record.get('source') or {}
    if 'artifact' in source:
        data, _ = _load_artifact(source, owner)
    elif 'path' in source:
        data = _snapshot(source['path'], list(source.get('roots') or []), include_bytes=True).content or b''
    else:
        raise ChannelError('attachment_unavailable', f'{record.get("name")} has no readable source')
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ChannelError('attachment_changed',
                           f'{record["name"]} changed after it was queued; send it again')
    return data



__all__ = ['SIZE_BELOW_BYTES', 'TYPES', 'admit', 'classify', 'for_final_answer', 'from_tool', 'identities',
           'limits_text', 'linked_references', 'read_original']
