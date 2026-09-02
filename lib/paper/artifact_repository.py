"""Owner-scoped repository for generated paper artifacts and reader notes.

Responsibility
--------------
This module is the only application-facing boundary for ``paper_reports``,
``paper_translations``, ``paper_podcasts``, and ``paper_notes``. Callers bind an
explicit owner once and exchange typed values; SQL, JSON columns, operation
names, and backend choices remain inside the storage Sidecar.

Entry points: :class:`PaperArtifactRepository` and the immutable projection
types below. Dependencies: ``lib.identity`` and ``lib.storage``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lib.identity import require_user_id


StorageClientFactory = Callable[..., Any]


def _default_client_factory(*, write: bool = False) -> Any:
    from lib.storage import get_storage_client

    return get_storage_client(write=write)


def _document(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class PaperReport:
    paper_hash: str
    lang: str
    report: str
    model: str = ''
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0

    @classmethod
    def from_projection(cls, value: Mapping[str, Any]) -> 'PaperReport':
        return cls(
            paper_hash=str(value.get('paper_hash') or ''),
            lang=str(value.get('lang') or ''),
            report=str(value.get('report') or ''),
            model=str(value.get('model') or ''),
            meta=_document(value.get('meta')),
            created_at=int(value.get('created_at') or 0),
        )


@dataclass(frozen=True)
class PaperTranslation:
    paper_hash: str
    lang: str
    text: str
    model: str = ''
    created_at: int = 0

    @classmethod
    def from_projection(cls, value: Mapping[str, Any]) -> 'PaperTranslation':
        return cls(
            paper_hash=str(value.get('paper_hash') or ''),
            lang=str(value.get('lang') or ''),
            text=str(value.get('text') or ''),
            model=str(value.get('model') or ''),
            created_at=int(value.get('created_at') or 0),
        )


@dataclass(frozen=True)
class PaperNote:
    note_id: str
    paper_hash: str
    lang: str
    anchor: dict[str, Any]
    note: str
    created_at: int
    updated_at: int

    @classmethod
    def from_projection(cls, value: Mapping[str, Any]) -> 'PaperNote':
        return cls(
            note_id=str(value.get('id') or ''),
            paper_hash=str(value.get('paper_hash') or ''),
            lang=str(value.get('lang') or ''),
            anchor=_document(value.get('anchor')),
            note=str(value.get('note') or ''),
            created_at=int(value.get('created_at') or 0),
            updated_at=int(value.get('updated_at') or 0),
        )

    def to_projection(self) -> dict[str, Any]:
        return {
            'id': self.note_id,
            'paper_hash': self.paper_hash,
            'lang': self.lang,
            'anchor': dict(self.anchor),
            'note': self.note,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass(frozen=True)
class PaperPodcast:
    paper_hash: str
    mode: str
    lang: str
    voice: str
    status: str
    script: dict[str, Any] = field(default_factory=dict)
    file_path: str = ''
    duration_sec: float = 0.0
    model: str = ''
    tts_model: str = ''
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_projection(cls, value: Mapping[str, Any]) -> 'PaperPodcast':
        return cls(
            paper_hash=str(value.get('paper_hash') or ''),
            mode=str(value.get('mode') or ''),
            lang=str(value.get('lang') or ''),
            voice=str(value.get('voice') or ''),
            status=str(value.get('status') or ''),
            script=_document(value.get('script_json')),
            file_path=str(value.get('file_path') or ''),
            duration_sec=float(value.get('duration_sec') or 0),
            model=str(value.get('model') or ''),
            tts_model=str(value.get('tts_model') or ''),
            meta=_document(value.get('meta')),
            created_at=int(value.get('created_at') or 0),
            updated_at=int(value.get('updated_at') or 0),
        )

    def to_projection(self) -> dict[str, Any]:
        return {
            'paper_hash': self.paper_hash,
            'mode': self.mode,
            'lang': self.lang,
            'voice': self.voice,
            'status': self.status,
            'script_json': dict(self.script),
            'file_path': self.file_path,
            'duration_sec': self.duration_sec,
            'model': self.model,
            'tts_model': self.tts_model,
            'meta': dict(self.meta),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class PaperArtifactRepository:
    """Typed paper-artifact repository bound to exactly one owner."""

    def __init__(
        self,
        owner_user_id: int,
        *,
        client_factory: StorageClientFactory = _default_client_factory,
    ) -> None:
        self.owner_user_id = require_user_id(
            owner_user_id, context='paper artifact repository owner')
        self._client_factory = client_factory

    def _query(self, operation: str, payload: Mapping[str, Any]) -> Any:
        return self._client_factory(write=False).query(
            operation, {'user_id': self.owner_user_id, **dict(payload)})

    def _command(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        command_id: str,
    ) -> dict[str, Any]:
        if not command_id:
            raise ValueError(f'{operation} command_id is required')
        result = self._client_factory(write=True).command(
            operation,
            {'user_id': self.owner_user_id, **dict(payload)},
            command_id,
        )
        return _document(result)

    def get_report(self, paper_hash: str, lang: str) -> PaperReport | None:
        row = self._query(
            'paper.report.get', {'paper_hash': paper_hash, 'lang': lang})
        return PaperReport.from_projection(row) if isinstance(row, Mapping) else None

    def latest_report(self, paper_hash: str) -> PaperReport | None:
        row = self._query('paper.report.latest', {'paper_hash': paper_hash})
        return PaperReport.from_projection(row) if isinstance(row, Mapping) else None

    def put_report(self, report: PaperReport, *, command_id: str) -> bool:
        result = self._command(
            'paper.report.upsert',
            {
                'paper_hash': report.paper_hash,
                'lang': report.lang,
                'report': report.report,
                'model': report.model,
                'meta': dict(report.meta),
                'created_at': report.created_at,
            },
            command_id=command_id,
        )
        return bool(result.get('saved'))

    def merge_report_second_pass(
        self,
        paper_hash: str,
        lang: str,
        name: str,
        entry: Mapping[str, Any],
        *,
        command_id: str,
    ) -> dict[str, Any] | None:
        result = self._command(
            'paper.report.second_pass.merge',
            {'paper_hash': paper_hash, 'lang': lang, 'name': name,
             'entry': dict(entry)},
            command_id=command_id,
        )
        meta = result.get('meta')
        return _document(meta) if result.get('found') else None

    def accumulate_report_second_pass(
        self,
        paper_hash: str,
        lang: str,
        name: str,
        usage: Mapping[str, Any],
        *,
        cost_cny: float | None = None,
        cost_usd: float | None = None,
        command_id: str,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            'paper_hash': paper_hash,
            'lang': lang,
            'name': name,
            'usage': dict(usage),
        }
        if cost_cny is not None:
            payload['costCny'] = cost_cny
        if cost_usd is not None:
            payload['costUsd'] = cost_usd
        result = self._command(
            'paper.report.second_pass.accumulate', payload,
            command_id=command_id)
        meta = result.get('meta')
        return _document(meta) if result.get('found') else None

    def get_translation(
        self, paper_hash: str, lang: str,
    ) -> PaperTranslation | None:
        row = self._query(
            'paper.translation.get', {'paper_hash': paper_hash, 'lang': lang})
        return (
            PaperTranslation.from_projection(row)
            if isinstance(row, Mapping) else None
        )

    def put_translation(
        self, translation: PaperTranslation, *, command_id: str,
    ) -> bool:
        result = self._command(
            'paper.translation.upsert',
            {
                'paper_hash': translation.paper_hash,
                'lang': translation.lang,
                'text': translation.text,
                'model': translation.model,
                'created_at': translation.created_at,
            },
            command_id=command_id,
        )
        return bool(result.get('saved'))

    def list_notes(self, paper_hash: str, lang: str) -> list[PaperNote]:
        rows = self._query(
            'paper.note.list', {'paper_hash': paper_hash, 'lang': lang}) or []
        return [
            PaperNote.from_projection(row)
            for row in rows if isinstance(row, Mapping)
        ]

    def create_note(self, note: PaperNote, *, command_id: str) -> bool:
        result = self._command(
            'paper.note.create',
            {
                'id': note.note_id,
                'paper_hash': note.paper_hash,
                'lang': note.lang,
                'anchor': dict(note.anchor),
                'note': note.note,
                'created_at': note.created_at,
                'updated_at': note.updated_at,
            },
            command_id=command_id,
        )
        return bool(result.get('saved'))

    def update_note(
        self,
        note_id: str,
        note: str,
        updated_at: int,
        *,
        command_id: str,
    ) -> bool:
        result = self._command(
            'paper.note.update',
            {'id': note_id, 'note': note, 'updated_at': updated_at},
            command_id=command_id,
        )
        return bool(result.get('updated'))

    def delete_note(self, note_id: str, *, command_id: str) -> bool:
        result = self._command(
            'paper.note.delete', {'id': note_id}, command_id=command_id)
        return bool(result.get('deleted'))

    def get_podcast(
        self, paper_hash: str, mode: str, lang: str, voice: str,
    ) -> PaperPodcast | None:
        row = self._query(
            'paper.podcast.get',
            {'paper_hash': paper_hash, 'mode': mode, 'lang': lang, 'voice': voice},
        )
        return PaperPodcast.from_projection(row) if isinstance(row, Mapping) else None

    def put_podcast(self, podcast: PaperPodcast, *, command_id: str) -> bool:
        result = self._command(
            'paper.podcast.upsert',
            {
                'paper_hash': podcast.paper_hash,
                'mode': podcast.mode,
                'lang': podcast.lang,
                'voice': podcast.voice,
                'status': podcast.status,
                'script': dict(podcast.script),
                'file_path': podcast.file_path,
                'duration_sec': podcast.duration_sec,
                'model': podcast.model,
                'tts_model': podcast.tts_model,
                'meta': dict(podcast.meta),
                'created_at': podcast.created_at,
                'updated_at': podcast.updated_at,
            },
            command_id=command_id,
        )
        return bool(result.get('saved'))


def mark_all_generating_podcasts_interrupted(
    *,
    updated_at: int,
    command_id: str,
    client_factory: StorageClientFactory = _default_client_factory,
) -> int:
    """Fleet-startup maintenance operation; intentionally spans all owners."""
    if not command_id:
        raise ValueError('podcast interruption command_id is required')
    result = client_factory(write=True).command(
        'paper.podcast.mark_interrupted',
        {'updated_at': updated_at},
        command_id,
    )
    return int((result or {}).get('changed') or 0)


__all__ = [
    'PaperArtifactRepository',
    'PaperNote',
    'PaperPodcast',
    'PaperReport',
    'PaperTranslation',
    'mark_all_generating_podcasts_interrupted',
]
