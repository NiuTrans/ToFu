"""Discover TeX documents without execution; persist explicit owner-scoped selection.

Conversation roots are the only grants. Every query revalidates durable selection;
partial scans never imply a sole document. Settings CAS rechecks changed grants.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .contract import limit, validate
from .errors import BuildError
from .service import owned_settings
from .sources import read_source

_SKIP = frozenset({'node_modules', '__pycache__', 'venv', 'vendor', 'vendors',
                   'build', 'dist', 'out', 'output', 'outputs', 'target', 'texmf'})
_DOCUMENT = re.compile(r'\\documentclass\s*(?:\[[^\]]*\])?\s*\{')


def _references(settings):
    values = [settings.get('projectPath'), *(settings.get('projectPaths') or [])]
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v.strip()))


def _safe(root, raw):
    path = Path(raw)
    if path.is_absolute() or '..' in path.parts:
        raise BuildError('source_path', 'Choose a relative path inside the owned root')
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise BuildError('source_path', 'Paper discovery does not follow symlinks')
    if not candidate.resolve(strict=True).is_relative_to(root):
        raise BuildError('source_path', 'Paper source is outside the owned root')
    return candidate


def _root(reference):
    if reference.startswith('remote:'):
        raise BuildError('desktop_unavailable', 'Remote document discovery is unavailable; no host fallback')
    path = Path(reference).expanduser().absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise BuildError('source_path', 'Paper discovery requires a regular project root')
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise BuildError('project_root', 'Paper project directory is unavailable')
    return root


def _document(root, reference, path):
    relative = path.relative_to(root).as_posix()
    return validate({'source': 'local', 'path': relative, 'project_root': reference,
                     'main_file': relative, 'label': relative}, 'PaperDocumentV1')


def _scan(settings, payload):
    references = _references(settings)
    requested = payload.get('project_root')
    if requested and requested not in references:
        raise BuildError('project_root', 'Paper project is outside this conversation')
    references = [requested] if requested else references
    if len(references) > limit('discovery_roots'):
        raise BuildError('discovery_limit', 'Too many project roots for document discovery')
    documents, diagnostics = [], []
    entries = size = 0

    def diagnostic(code, message):
        diagnostics.append({'severity': 'warning', 'code': code, 'message': message,
                            'file': '', 'line': None})

    def visit(root, reference, path, depth=0):
        nonlocal entries, size
        if depth > limit('discovery_depth'):
            raise BuildError('discovery_limit', 'Document discovery depth limit reached')
        path = _safe(root, path.relative_to(root))
        if path.is_dir():
            # scandir is consumed incrementally: a huge directory cannot allocate an unbounded list.
            with os.scandir(path) as children:
                for child in children:
                    entries += 1
                    if entries > limit('discovery_entries'):
                        raise BuildError('discovery_limit', 'Document discovery entry limit reached')
                    if child.name.startswith('.') or child.name in _SKIP:
                        continue
                    if child.is_symlink():
                        raise BuildError('source_path', 'Document scan encountered a symlink; scan is incomplete')
                    visit(root, reference, Path(child.path), depth + 1)
        elif path.suffix.lower() in {'.tex', '.ltx'}:
            data = read_source(root, path, limit=min(limit('discovery_file_bytes'),
                                                   limit('discovery_bytes') - size))
            size += len(data)
            text = re.sub(r'(?<!\\)%[^\n]*', '', data.decode('utf-8', errors='replace'))
            if _DOCUMENT.search(text):
                if len(documents) >= limit('discovery_candidates'):
                    raise BuildError('discovery_limit', 'Document candidate limit reached')
                documents.append(_document(root, reference, path))

    for reference in references:
        try:
            root = _root(reference)
            visit(root, reference, _safe(root, payload.get('path', '.')))
        except BuildError as exc:
            diagnostic(exc.code, str(exc))
            if exc.code in {'discovery_limit', 'source_limit'}:
                break
        except (OSError, RuntimeError, ValueError):
            diagnostic('discovery_scan', 'Paper source scan failed; results are incomplete')
    if not references:
        diagnostic('project_root', 'Choose a project directory for this conversation')
    return sorted(documents, key=lambda d: (d['project_root'], d['path'])), diagnostics


def _validated_selection(settings, payload):
    reference = payload['project_root']
    if reference not in _references(settings):
        raise BuildError('project_root', 'Paper project is outside this conversation')
    try:
        root = _root(reference)
        path = _safe(root, payload['path'])
        main = payload.get('main_file')
        if main:
            candidate = _safe(root, main)
            if candidate != path and (not path.is_dir() or not candidate.is_relative_to(path)):
                raise BuildError('main_file', 'Main file must belong to the selected path')
            path = candidate
        documents, diagnostics = _scan(settings, {'project_root': reference,
                                                  'path': path.relative_to(root).as_posix()})
        if diagnostics:
            raise BuildError(diagnostics[0]['code'], diagnostics[0]['message'])
        if len(documents) != 1:
            raise BuildError('main_ambiguous' if documents else 'main_file',
                             'Choose one verified main document')
        return documents[0]
    except (OSError, RuntimeError, ValueError) as exc:
        raise BuildError('source_path', 'Selected paper source is unavailable') from exc


def discover_documents(payload: dict, *, owner_user_id: int) -> dict:
    from .overleaf_capabilities import discover_sources
    from .document_identity import source_binding_key
    from .document_repository import PaperDocumentRepository
    validate(payload, 'PaperDocumentDiscoveryRequestV1')
    settings = owned_settings(payload['conv_id'], owner_user_id)
    remote, servers = discover_sources(settings, owner_user_id=owner_user_id)
    documents, diagnostics = (_scan(settings, payload) if _references(settings)
        or payload.get('project_root') or not (remote or servers) else ([], []))
    documents.extend(remote)
    selected = None
    if 'paperDocument' in settings:
        try:
            saved = validate(settings['paperDocument'], 'PaperDocumentV1')
            if saved['source'] == 'overleaf':
                selected = next((value for value in remote
                                 if source_binding_key(value) == source_binding_key(saved)), None)
                if selected is None:
                    raise BuildError('selection_stale', 'Saved Overleaf project is unavailable')
            else:
                selected = _validated_selection(settings, saved)
        except BuildError:
            diagnostics.append({'severity': 'warning', 'code': 'selection_stale',
                                'message': 'Saved paper selection is unavailable; explicitly select a document',
                                'file': '', 'line': None})
    elif len(documents) == 1 and not diagnostics:
        selected = documents[0]
    maximum = limit('discovery_candidates')
    if len(documents) > maximum:
        documents = ([selected] if selected else []) + [value for value in documents if value != selected]
        documents = documents[:maximum]
        diagnostics.append({'severity': 'warning', 'code': 'discovery_limit',
                            'message': 'Document list is incomplete', 'file': '', 'line': None})
    identities = PaperDocumentRepository(owner_user_id).resolve(documents)
    documents = [{**value, **({'document_id': identity} if identity else {})}
                 for value, identity in zip(documents, identities, strict=True)]
    if selected is not None:
        selected = next((value for value in documents
                         if source_binding_key(value) == source_binding_key(selected)), selected)
    return validate({'documents': documents, 'selected': selected, 'diagnostics': diagnostics,
                     'overleaf_servers': servers},
                    'PaperDocumentDiscoveryResultV1')


def select_document(payload: dict, *, owner_user_id: int) -> dict:
    from lib.conversations.settings_store import update_conversation_settings
    from .document_repository import PaperDocumentRepository
    validate(payload, 'PaperDocumentSelectionRequestV1')
    if 'project_id' in payload:
        from .overleaf_capabilities import select_source, source_document
        result = select_source(payload, owner_user_id=owner_user_id)
        selected = PaperDocumentRepository(owner_user_id).register(source_document(result['source']))['source']
        return validate({'selected': selected}, 'PaperDocumentSelectionResultV1')
    settings = owned_settings(payload['conv_id'], owner_user_id)
    selected = PaperDocumentRepository(owner_user_id).register(_validated_selection(settings, payload))['source']

    def mutate(current):
        value = _validated_selection(current, payload)
        current['paperDocument'] = {**value, 'document_id': selected['document_id']}

    updated = update_conversation_settings(payload['conv_id'], mutate, user_id=owner_user_id)
    if updated is None:
        raise BuildError('document_not_found', 'Paper conversation is unavailable or settings changed; retry')
    return validate({'selected': updated['paperDocument']}, 'PaperDocumentSelectionResultV1')
