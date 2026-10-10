"""Interrupted two-file visual repairs recover originals with digest/path fences."""

from pathlib import Path

import pytest

from lib.slides.visual_repair import VisualRepairTransaction, recover_visual_repair

pytestmark = pytest.mark.unit


def files(tmp_path):
    source = tmp_path / 'pages' / '01.page'
    preview = tmp_path / 'preview' / '01.png'
    source.parent.mkdir()
    preview.parent.mkdir()
    source.write_text('original YAML')
    preview.write_bytes(b'original pixels')
    return source, preview


def test_restart_restores_both_files_after_interruption_between_writes(tmp_path):
    source, preview = files(tmp_path)
    transaction = VisualRepairTransaction(str(tmp_path)).__enter__()
    transaction.remember(0, str(source), str(preview), source.read_text())
    source.write_text('unverified new YAML')
    recover_visual_repair(str(tmp_path))
    assert source.read_text() == 'original YAML'
    assert preview.read_bytes() == b'original pixels'
    assert not transaction.scratch.exists()
    assert not transaction.journal.exists()
    recover_visual_repair(str(tmp_path))  # idempotent after recovery


def test_abort_restores_both_files_and_cleans_scratch(tmp_path):
    source, preview = files(tmp_path)
    with pytest.raises(InterruptedError):
        with VisualRepairTransaction(str(tmp_path)) as transaction:
            transaction.remember(0, str(source), str(preview), source.read_text())
            source.write_text('new YAML')
            preview.write_bytes(b'new pixels')
            raise InterruptedError('cancelled')
    assert source.read_text() == 'original YAML'
    assert preview.read_bytes() == b'original pixels'
    assert not transaction.journal.exists()


def test_verified_commit_retains_changes_and_removes_backups(tmp_path):
    source, preview = files(tmp_path)
    with VisualRepairTransaction(str(tmp_path)) as transaction:
        transaction.remember(0, str(source), str(preview), source.read_text())
        source.write_text('verified YAML')
        preview.write_bytes(b'verified pixels')
        transaction.commit()
    assert source.read_text() == 'verified YAML'
    assert preview.read_bytes() == b'verified pixels'
    assert not transaction.scratch.exists()


def test_corrupt_backup_fails_loudly_and_retains_recovery_journal(tmp_path):
    source, preview = files(tmp_path)
    transaction = VisualRepairTransaction(str(tmp_path)).__enter__()
    transaction.remember(0, str(source), str(preview), source.read_text())
    original = Path(transaction.scratch) / 'original-0.png'
    original.write_bytes(b'corrupt')
    source.write_text('new YAML')
    with pytest.raises(ValueError, match='digest mismatch'):
        recover_visual_repair(str(tmp_path))
    assert source.read_text() == 'new YAML'
    assert transaction.journal.exists()


def test_oversize_preview_rejected_before_mutation(tmp_path, monkeypatch):
    source, preview = files(tmp_path)
    monkeypatch.setattr('lib.slides.visual_repair._MAX_PREVIEW_BYTES', 4)
    with VisualRepairTransaction(str(tmp_path)) as transaction:
        with pytest.raises(ValueError, match='byte limit'):
            transaction.remember(0, str(source), str(preview), source.read_text())
    assert source.read_text() == 'original YAML'
    assert preview.read_bytes() == b'original pixels'


def test_symlink_target_cannot_escape_deck(tmp_path):
    source, preview = files(tmp_path)
    outside = tmp_path.parent / 'outside.png'
    outside.write_bytes(b'outside')
    preview.unlink()
    preview.symlink_to(outside)
    with VisualRepairTransaction(str(tmp_path)) as transaction:
        with pytest.raises(ValueError, match='escapes'):
            transaction.remember(0, str(source), str(preview), source.read_text())
    assert outside.read_bytes() == b'outside'


def test_symlink_scratch_parent_is_rejected_before_creating_files(tmp_path):
    outside = tmp_path.parent / 'outside-scratch'
    outside.mkdir()
    (tmp_path / '.tofu-qa').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='escapes'):
        VisualRepairTransaction(str(tmp_path))
    assert list(outside.iterdir()) == []
