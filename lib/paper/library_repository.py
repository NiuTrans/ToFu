"""Owner-scoped paper-library repository backed exclusively by the Sidecar.

Responsibility
--------------
This module is the sole application-facing boundary for ``paper_library``.
Callers provide an explicit owner once, then use typed entries instead of wire
operation names, JSON columns, or SQL. The storage Sidecar remains responsible
for transactions, backend portability, and command idempotency.

Entry points: :class:`PaperLibraryRepository`, :class:`PaperLibraryEntry`, and
:class:`PaperIdentity`. Dependencies: ``lib.identity`` and ``lib.storage``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lib.identity import require_user_id
from lib.paper.contracts import (
    PAPER_FANIN_MAX_PAPERS,
    PAPER_FANIN_MAX_TEXT_CHARS,
    PAPER_QA_MAX_SOURCE_CHARS,
)


StorageClientFactory = Callable[..., Any]


def _default_client_factory(*, write: bool = False) -> Any:
    from lib.storage import get_storage_client

    return get_storage_client(write=write)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


@dataclass(frozen=True)
class PaperLibraryEntry:
    """Canonical in-process representation of one owned bookshelf row."""

    paper_id: str
    title: str = ''
    pdf_url: str = ''
    pdf_filename: str = ''
    arxiv_id: str = ''
    paper_hash: str = ''
    parsed_text: str = ''
    parsed_text_length: int = 0
    parser_version: str = ''
    qa_history: list[Any] = field(default_factory=list)
    images: list[Any] = field(default_factory=list)
    babel_cache: dict[str, Any] = field(default_factory=dict)
    page_count: int = 0
    folder_id: str = ''
    created_at: int = 0
    updated_at: int = 0
    has_report: bool = False

    @classmethod
    def from_projection(cls, value: Mapping[str, Any]) -> 'PaperLibraryEntry':
        """Decode the Sidecar's API-shaped read projection."""
        parsed_text = str(value.get('parsedText') or '')
        return cls(
            paper_id=str(value.get('id') or ''),
            title=str(value.get('title') or ''),
            pdf_url=str(value.get('pdfUrl') or ''),
            pdf_filename=str(value.get('pdfFilename') or ''),
            arxiv_id=str(value.get('arxivId') or ''),
            paper_hash=str(value.get('paperHash') or ''),
            parsed_text=parsed_text,
            parsed_text_length=int(
                value.get('parsedTextLength') or len(parsed_text)),
            parser_version=str(value.get('parserVersion') or ''),
            qa_history=_list(value.get('qaHistory')),
            images=_list(value.get('images')),
            babel_cache=_mapping(value.get('babelCache')),
            page_count=int(value.get('pageCount') or 0),
            folder_id=str(value.get('folderId') or ''),
            created_at=int(value.get('createdAt') or 0),
            updated_at=int(value.get('updatedAt') or 0),
            has_report=bool(value.get('hasReport')),
        )

    def to_projection(self) -> dict[str, Any]:
        """Return the stable camelCase HTTP projection."""
        return {
            'id': self.paper_id,
            'title': self.title,
            'pdfUrl': self.pdf_url,
            'pdfFilename': self.pdf_filename,
            'arxivId': self.arxiv_id,
            'paperHash': self.paper_hash,
            'parsedText': self.parsed_text,
            'parserVersion': self.parser_version,
            'qaHistory': list(self.qa_history),
            'images': list(self.images),
            'babelCache': dict(self.babel_cache),
            'pageCount': self.page_count,
            'folderId': self.folder_id,
            'createdAt': self.created_at,
            'updatedAt': self.updated_at,
            'hasReport': self.has_report,
        }

    def to_summary_projection(self) -> dict[str, Any]:
        """Return bookshelf metadata without content or auxiliary JSON."""
        return {
            'id': self.paper_id,
            'title': self.title,
            'pdfUrl': self.pdf_url,
            'pdfFilename': self.pdf_filename,
            'arxivId': self.arxiv_id,
            'paperHash': self.paper_hash,
            'pageCount': self.page_count,
            'folderId': self.folder_id,
            'createdAt': self.created_at,
            'updatedAt': self.updated_at,
            'hasReport': self.has_report,
        }

    def to_storage_payload(self, *, owner_user_id: int) -> dict[str, Any]:
        """Encode JSON columns exactly once at the repository boundary."""
        return {
            'id': self.paper_id,
            'user_id': owner_user_id,
            'title': self.title,
            'pdf_url': self.pdf_url,
            'pdf_filename': self.pdf_filename,
            'arxiv_id': self.arxiv_id,
            'paper_hash': self.paper_hash,
            'parsed_text': self.parsed_text,
            'parser_version': self.parser_version,
            'qa_history': json.dumps(self.qa_history, ensure_ascii=False),
            'images': json.dumps(self.images, ensure_ascii=False),
            'babel_cache': json.dumps(self.babel_cache, ensure_ascii=False),
            'page_count': self.page_count,
            'folder_id': self.folder_id,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass(frozen=True)
class PaperIdentity:
    """Minimal owner-scoped content identity used by paper pipelines."""

    title: str
    arxiv_id: str
    parsed_text: str
    parsed_text_length: int = 0


class PaperLibraryRepository:
    """Typed repository for exactly one authenticated storage owner."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        client_factory: StorageClientFactory = _default_client_factory,
    ) -> None:
        self.owner_user_id = require_user_id(
            owner_user_id, context='paper library repository owner')
        self._client_factory = client_factory

    def list_summaries(self) -> list[PaperLibraryEntry]:
        """Load the bookshelf without paper bodies or auxiliary JSON."""
        rows = self._client_factory(write=False).query(
            'paper.library.summaries', {'user_id': self.owner_user_id}) or []
        return [
            PaperLibraryEntry.from_projection(row)
            for row in rows if isinstance(row, Mapping)
        ]

    def list_entries(self) -> list[PaperLibraryEntry]:
        """Load complete rows for the compatibility HTTP list only."""
        rows = self._client_factory(write=False).query(
            'paper.library.list', {'user_id': self.owner_user_id}) or []
        return [
            PaperLibraryEntry.from_projection(row)
            for row in rows if isinstance(row, Mapping)
        ]

    def get(self, paper_id: str) -> PaperLibraryEntry | None:
        normalized_paper_id = str(paper_id or '').strip()
        if not normalized_paper_id:
            return None
        row = self._client_factory(write=False).query(
            'paper.library.get', {
                'user_id': self.owner_user_id,
                'id': normalized_paper_id,
            })
        return (
            PaperLibraryEntry.from_projection(row)
            if isinstance(row, Mapping) else None
        )

    def reader_detail(self, paper_id: str) -> PaperLibraryEntry | None:
        """Load reader state without the legacy duplicate translation cache."""
        normalized_paper_id = str(paper_id or '').strip()
        if not normalized_paper_id:
            return None
        row = self._client_factory(write=False).query(
            'paper.library.reader', {
                'user_id': self.owner_user_id,
                'id': normalized_paper_id,
            })
        return (
            PaperLibraryEntry.from_projection(row)
            if isinstance(row, Mapping) else None
        )

    def by_arxiv_ids(
        self,
        arxiv_ids,
        *,
        max_text_chars: int = 0,
    ) -> list[PaperLibraryEntry]:
        """Load at most 40 targets with a server-truncated text projection."""
        if (
            not isinstance(max_text_chars, int)
            or isinstance(max_text_chars, bool)
            or not 0 <= max_text_chars <= PAPER_FANIN_MAX_TEXT_CHARS
        ):
            raise ValueError(
                'paper input lookup max_text_chars must be '
                f'0..{PAPER_FANIN_MAX_TEXT_CHARS}')
        normalized = []
        seen = set()
        for index, value in enumerate(arxiv_ids or ()):
            if index >= PAPER_FANIN_MAX_PAPERS:
                raise ValueError(
                    'paper input lookup accepts at most '
                    f'{PAPER_FANIN_MAX_PAPERS} arxiv ids')
            if not isinstance(value, str):
                raise ValueError('paper input lookup arxiv ids must be strings')
            candidate = value.strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)
        if not normalized:
            return []
        rows = self._client_factory(write=False).query(
            'paper.library.inputs', {
                'user_id': self.owner_user_id,
                'arxiv_ids': normalized,
                'max_text_chars': max_text_chars,
            }) or []
        return [
            PaperLibraryEntry.from_projection(row)
            for row in rows if isinstance(row, Mapping)
        ]

    def put(self, entry: PaperLibraryEntry, *, command_id: str) -> bool:
        if not command_id:
            raise ValueError('paper library command_id is required')
        result = self._client_factory(write=True).command(
            'paper.library.put',
            entry.to_storage_payload(owner_user_id=self.owner_user_id),
            command_id,
        )
        return bool(result and result.get('saved'))

    def delete(self, paper_id: str, *, command_id: str) -> bool:
        if not command_id:
            raise ValueError('paper library command_id is required')
        result = self._client_factory(write=True).command(
            'paper.library.delete',
            {'id': paper_id, 'user_id': self.owner_user_id},
            command_id,
        )
        return bool(result and result.get('deleted'))

    def recent(
        self,
        *,
        exclude_paper_hash: str = '',
        limit: int = 40,
    ) -> list[dict[str, str]]:
        rows = self._client_factory(write=False).query(
            'paper.library.recent',
            {
                'user_id': self.owner_user_id,
                'exclude_paper_hash': exclude_paper_hash,
                'limit': limit,
            },
        ) or []
        return [
            {
                'title': str(row.get('title') or ''),
                'arxiv_id': str(row.get('arxiv_id') or ''),
            }
            for row in rows if isinstance(row, Mapping)
        ]

    def identity(
        self,
        paper_hash: str,
        *,
        max_text_chars: int | None = None,
        include_text_length: bool = True,
    ) -> PaperIdentity | None:
        """Load identity with an optional bounded parsed-text projection.

        ``None`` preserves the legacy full-text contract. ``0`` is the cheap
        metadata-only path; report and podcast callers request their explicit
        prompt ceilings so unused tail text never crosses the Sidecar RPC.
        A zero-text caller may also omit length calculation when owner/hash
        existence is the complete content-addressed revision check.
        """
        if (
            max_text_chars is not None
            and (
                not isinstance(max_text_chars, int)
                or isinstance(max_text_chars, bool)
                or not 0 <= max_text_chars <= PAPER_QA_MAX_SOURCE_CHARS
            )
        ):
            raise ValueError(
                'paper identity max_text_chars must be '
                f'0..{PAPER_QA_MAX_SOURCE_CHARS} or None')
        if not isinstance(include_text_length, bool):
            raise ValueError('paper identity include_text_length must be boolean')
        if not include_text_length and max_text_chars != 0:
            raise ValueError(
                'paper identity may omit text length only for a zero-text '
                'projection')
        payload = {
            'user_id': self.owner_user_id,
            'paper_hash': paper_hash,
        }
        if max_text_chars is not None:
            payload['max_text_chars'] = max_text_chars
        if not include_text_length:
            payload['include_text_length'] = False
        row = self._client_factory(write=False).query(
            'paper.library.identity', payload)
        if not isinstance(row, Mapping):
            return None
        parsed_text = str(row.get('parsed_text') or '')
        return PaperIdentity(
            title=str(row.get('title') or ''),
            arxiv_id=str(row.get('arxiv_id') or ''),
            parsed_text=parsed_text,
            parsed_text_length=int(
                row.get('parsed_text_length') or len(parsed_text)),
        )

    def backfill_title(
        self,
        paper_hash: str,
        title: str,
        *,
        command_id: str,
    ) -> dict[str, Any]:
        if not command_id:
            raise ValueError('paper library command_id is required')
        result = self._client_factory(write=True).command(
            'paper.library.title.backfill',
            {
                'user_id': self.owner_user_id,
                'paper_hash': paper_hash,
                'title': title,
            },
            command_id,
        )
        return dict(result or {})


__all__ = [
    'PaperIdentity',
    'PaperLibraryEntry',
    'PaperLibraryRepository',
]
