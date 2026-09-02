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
        return cls(
            paper_id=str(value.get('id') or ''),
            title=str(value.get('title') or ''),
            pdf_url=str(value.get('pdfUrl') or ''),
            pdf_filename=str(value.get('pdfFilename') or ''),
            arxiv_id=str(value.get('arxivId') or ''),
            paper_hash=str(value.get('paperHash') or ''),
            parsed_text=str(value.get('parsedText') or ''),
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

    def list_entries(self, *, paper_id: str = '') -> list[PaperLibraryEntry]:
        payload: dict[str, Any] = {'user_id': self.owner_user_id}
        if paper_id:
            payload['id'] = paper_id
        rows = self._client_factory(write=False).query(
            'paper.library.list', payload) or []
        return [
            PaperLibraryEntry.from_projection(row)
            for row in rows if isinstance(row, Mapping)
        ]

    def get(self, paper_id: str) -> PaperLibraryEntry | None:
        normalized_paper_id = str(paper_id or '').strip()
        if not normalized_paper_id:
            return None
        rows = self.list_entries(paper_id=normalized_paper_id)
        return rows[0] if rows else None

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

    def identity(self, paper_hash: str) -> PaperIdentity | None:
        row = self._client_factory(write=False).query(
            'paper.library.identity',
            {'user_id': self.owner_user_id, 'paper_hash': paper_hash},
        )
        if not isinstance(row, Mapping):
            return None
        return PaperIdentity(
            title=str(row.get('title') or ''),
            arxiv_id=str(row.get('arxiv_id') or ''),
            parsed_text=str(row.get('parsed_text') or ''),
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
