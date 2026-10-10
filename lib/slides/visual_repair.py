"""Recoverable YAML/preview transactions for the visual-QA stage.

Entry points: recover_visual_repair and VisualRepairTransaction. Dependencies:
stdlib and atomic JSON/text writers. A private, bounded journal retains the
original pair before either file changes; restart or abort restores both.
Verified completion removes only this transaction's reconstructible scratch.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from lib.json_store import read_json, write_json_atomic, write_text_atomic

_JOURNAL = 'visual-repair.json'
_VERSION = 'slide-visual-repair-v1'
_MAX_BACKUP_BYTES = 192 * 1024 * 1024
_MAX_PAGE_BYTES = 512 * 1024
_MAX_PREVIEW_BYTES = 16 * 1024 * 1024


def _contained(root: Path, value: str) -> Path:
    path = root / value
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('visual repair journal path escapes the deck')
    return path


def _restore(root: Path, row: dict) -> None:
    source = _contained(root, row['source'])
    preview = _contained(root, row['preview'])
    original_yaml = _contained(root, row['yaml'])
    original_png = _contained(root, row['png'])
    if (original_yaml.stat().st_size > _MAX_PAGE_BYTES
            or original_png.stat().st_size > _MAX_PREVIEW_BYTES):
        raise ValueError('visual repair backup exceeds byte limit')
    for path, digest in ((original_yaml, row['yaml_sha256']),
                         (original_png, row['png_sha256'])):
        if _digest(path) != digest:
            raise ValueError('visual repair backup digest mismatch')
    write_text_atomic(str(source), original_yaml.read_text(encoding='utf-8'))
    with tempfile.NamedTemporaryFile(dir=preview.parent, suffix='.png', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copyfile(original_png, temporary)
        os.replace(temporary, preview)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def recover_visual_repair(deck_root: str) -> None:
    """Restore an interrupted pair before reading pages or reviewing pixels."""
    root = Path(deck_root).resolve()
    journal = _contained(root, '.tofu-qa') / _JOURNAL
    if not journal.exists():
        return
    if journal.is_symlink() or journal.stat().st_size > 64 * 1024:
        raise ValueError('invalid visual repair journal')
    record = read_json(str(journal), default=None)
    if not isinstance(record, dict) or record.get('version') != _VERSION:
        raise ValueError('unreadable visual repair journal')
    rows = record.get('pages')
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError('visual repair journal exceeds page limit')
    scratch = _contained(root, record['scratch'])
    if scratch.parent != root / '.tofu-qa' or not scratch.name.startswith('repair-'):
        raise ValueError('invalid visual repair scratch directory')
    for row in rows:
        _restore(root, row)
    # Remove the journal only after every original pair was restored. If a
    # restore fails, the next retry still has the recovery authority.
    journal.unlink()
    shutil.rmtree(scratch)


class VisualRepairTransaction:
    """One deck-local transaction; backup growth is capped before mutation."""

    def __init__(self, deck_root: str):
        self.root = Path(deck_root).resolve()
        self.journal = _contained(self.root, '.tofu-qa') / _JOURNAL
        self.scratch = None
        self.rows = {}
        self.bytes = 0
        self.committed = False

    def __enter__(self):
        recover_visual_repair(str(self.root))
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(prefix='repair-', dir=self.journal.parent))
        try:
            self._checkpoint()
        except BaseException:
            shutil.rmtree(self.scratch)
            raise
        return self

    def _checkpoint(self):
        write_json_atomic(str(self.journal), {
            'version': _VERSION, 'scratch': str(self.scratch.relative_to(self.root)),
            'pages': list(self.rows.values())})

    def remember(self, index: int, source: str, preview: str, seed: str):
        if index in self.rows or len(self.rows) >= 64:
            raise ValueError('visual repair backup identity/count exceeded')
        yaml_bytes = len(seed.encode('utf-8'))
        png_bytes = os.path.getsize(preview)
        if (yaml_bytes > _MAX_PAGE_BYTES or png_bytes > _MAX_PREVIEW_BYTES
                or self.bytes + yaml_bytes + png_bytes > _MAX_BACKUP_BYTES):
            raise ValueError('visual repair backup byte limit exceeded')
        source_path = _contained(self.root, str(Path(source).relative_to(self.root)))
        preview_path = _contained(self.root, str(Path(preview).relative_to(self.root)))
        yaml_backup = self.scratch / f'original-{index}.page'
        png_backup = self.scratch / f'original-{index}.png'
        write_text_atomic(str(yaml_backup), seed)
        shutil.copyfile(preview_path, png_backup)
        self.rows[index] = {
            'source': str(source_path.relative_to(self.root)),
            'preview': str(preview_path.relative_to(self.root)),
            'yaml': str(yaml_backup.relative_to(self.root)),
            'png': str(png_backup.relative_to(self.root)),
            'yaml_sha256': _digest(yaml_backup), 'png_sha256': _digest(png_backup)}
        self.bytes += yaml_bytes + png_bytes
        self._checkpoint()

    def rollback(self, index: int):
        _restore(self.root, self.rows[index])

    def commit(self):
        self.committed = True

    def __exit__(self, kind, value, traceback):
        if kind is not None or not self.committed:
            for index in self.rows:
                self.rollback(index)
        self.journal.unlink()
        shutil.rmtree(self.scratch)
