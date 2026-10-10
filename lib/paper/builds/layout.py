"""Page-layout report from one verified build's artifacts.

Consumes the build's OWN normalized SyncTeX (and its PDF MediaBox + build
log geometry block) — never recompiles and never trusts the live tree:
section titles are read from a live source file only when its sha256 matches
the frozen manifest row, otherwise the section reports page+line without a
title. Supports "how many pages / where does each section start / how full
is the last page" answers for both the browser and the agent's page-budget
work ("compress to N pages").
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from lib.paper.latex_layout import (_body_bottom_on_page, _page_for_line,
                                    _parse_geometry_fields,
                                    _parse_mediabox_height,
                                    _synctex_tags_for_label, parse_synctex)

from .contract import validate
from .errors import BuildError
from .synctex import _decode

_SECTION = re.compile(
    r'\\(part|chapter|section|subsection|subsubsection|paragraph)\*?'
    r'\s*(?:\[[^\]]{0,300}\])?\s*\{([^{}]{1,300})\}')
_STRIP_COMMENTS = re.compile(r'(?<!\\)%[^\n]*')
_MAX_SECTIONS = 128


def _section_rows(sources: dict[str, str], parsed: dict) -> list[dict]:
    rows = []
    for path, text in sorted(sources.items()):
        stripped = _STRIP_COMMENTS.sub('', text)
        tags = _synctex_tags_for_label(parsed, path)
        if not tags:
            continue
        for match in _SECTION.finditer(stripped):
            line = stripped.count('\n', 0, match.start()) + 1
            page = _page_for_line(parsed, tags, line)
            if page is None:
                continue
            rows.append({'level': match.group(1), 'title': match.group(2).strip()[:160],
                         'file': path, 'line': line, 'page': page})
            if len(rows) >= _MAX_SECTIONS:
                return rows
    return rows


def layout_report(build, *, source_root: Path | None = None) -> dict:
    """Return the PaperBuildLayoutV1 report for one opened, verified build."""
    receipt = build.receipt
    result = {'build_id': receipt['build_id'], 'pages': 0, 'sections': [], 'end': None}
    declared = {row['name'] for row in receipt['artifacts']}
    if receipt['status'] != 'success' or 'document.synctex.gz' not in declared:
        return validate(result, 'PaperBuildLayoutV1')
    with build.open_artifact('document.synctex.gz') as (stream, _):
        parsed = parse_synctex(_decode(stream))
    pages = int(parsed.get('max_page') or 0)
    result['pages'] = pages

    sources: dict[str, str] = {}
    if source_root is not None and 'sources.json' in declared:
        import json
        from .sources import read_source
        with build.open_artifact('sources.json') as (stream, _):
            manifest = {row['path']: row['sha256'] for row in json.load(stream)}
        for path, sha in manifest.items():
            if not path.lower().endswith(('.tex', '.ltx')) or len(sources) >= 64:
                continue
            try:
                data = read_source(source_root, source_root / path)
            except (BuildError, OSError):
                continue  # live file vanished or changed shape: pages stay valid
            if hashlib.sha256(data).hexdigest() != sha:
                continue  # drifted since the frozen build: titles would lie
            sources[path] = data.decode('utf-8', errors='replace')
    result['sections'] = _section_rows(sources, parsed)

    page_height = None
    if 'document.pdf' in declared:
        with build.open_artifact('document.pdf') as (stream, _):
            chunk = stream.read(4 * 1024 * 1024)
        page_height = _parse_mediabox_height(chunk)
    text_bottom = None
    if 'build.log' in declared:
        with build.open_artifact('build.log') as (stream, _):
            log_text = stream.read(1024 * 1024).decode('utf-8', errors='replace')
        text_bottom = _text_block_bottom_from_log(log_text)
    if pages:
        body_bottom = _body_bottom_on_page(parsed, pages, text_bottom)
        if body_bottom is not None and page_height:
            fullness = max(0.0, min(1.0, body_bottom / page_height))
            result['end'] = {'page': pages,
                             'fullness_page': round(fullness, 4),
                             'fullness_text': (round(body_bottom / text_bottom, 4)
                                               if text_bottom else None)}
        elif body_bottom is not None:
            result['end'] = {'page': pages, 'fullness_page': None, 'fullness_text': None}
    return validate(result, 'PaperBuildLayoutV1')


def _text_block_bottom_from_log(log_text: str) -> float | None:
    from lib.paper.latex_layout import _text_block_bottom_bp
    return _text_block_bottom_bp(_parse_geometry_fields(log_text))


def document_layout(document: dict, build_id: str, *, owner_user_id: int,
                    scheduler=None) -> dict:
    """Open one owned build and its (optional) live source root for layout."""
    from .runtime import get_scheduler
    scheduler = scheduler or get_scheduler()
    source_root = _live_source_root(document)
    from .artifact_repository import PaperBuildArtifactRepository
    with PaperBuildArtifactRepository(owner_user_id).open_build(
            scheduler.cache, document['document_id'], build_id) as build:
        return layout_report(build, source_root=source_root)


def _live_source_root(document: dict) -> Path | None:
    source = (document or {}).get('source') or {}
    if source.get('source') != 'local':
        return None
    try:
        from .documents import _root, _safe
        root = _root(source['project_root'])
        main = _safe(root, source.get('main_file') or source['path'])
        return main.parent
    except (BuildError, OSError):
        return None
