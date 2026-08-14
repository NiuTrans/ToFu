"""The manually appended audit trail must have a hard storage bound."""

from __future__ import annotations

import pytest

import lib.log as log_mod

pytestmark = pytest.mark.unit


def test_audit_write_rotates_at_size_limit(tmp_path, monkeypatch):
    audit = tmp_path / 'audit.log'
    audit.write_text('old-audit\n', encoding='utf-8')
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(audit))
    monkeypatch.setenv('TOFU_AUDIT_LOG_MAX_BYTES', str(1 << 20))
    monkeypatch.setenv('TOFU_AUDIT_LOG_BACKUPS', '2')
    # The production lower clamp is 1 MiB; make the existing file cross it.
    audit.write_bytes(b'x' * (1 << 20))

    log_mod._audit_write_line('new-audit\n')

    assert audit.read_text(encoding='utf-8') == 'new-audit\n'
    assert (tmp_path / 'audit.log.1').stat().st_size == 1 << 20


def test_audit_rotation_prunes_oldest_backup(tmp_path, monkeypatch):
    audit = tmp_path / 'audit.log'
    audit.write_bytes(b'x' * (1 << 20))
    (tmp_path / 'audit.log.1').write_text('one', encoding='utf-8')
    (tmp_path / 'audit.log.2').write_text('two', encoding='utf-8')
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(audit))
    monkeypatch.setenv('TOFU_AUDIT_LOG_MAX_BYTES', str(1 << 20))
    monkeypatch.setenv('TOFU_AUDIT_LOG_BACKUPS', '2')

    log_mod._audit_write_line('fresh\n')

    assert (tmp_path / 'audit.log.1').stat().st_size == 1 << 20
    assert (tmp_path / 'audit.log.2').read_text(encoding='utf-8') == 'one'
