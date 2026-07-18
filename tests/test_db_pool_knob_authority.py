"""Regression coverage for the pool-knob silent-revert footgun.

Background
----------
Both launchers (``server.py`` / ``bootstrap.py``) load ``.env`` with a
*fill-if-absent* policy (``if key not in os.environ``). A value inherited from
the container / IDE launch environment therefore SHADOWS the operator's .env
file. For the DB pool ceiling / acquire timeout this is a silent-revert
footgun: on 2026-07-16 a stale exported ``TOFU_DB_MAX_CONNS=400`` kept
overriding an intended ``.env=800`` on every restart, so the storm-hardening
never armed even though the .env change was committed.

``lib.database._core._pool_knob`` closes that hole: for these specific knobs
the ``.env`` FILE is authoritative, and a disagreeing shell export triggers a
loud WARNING (never a silent override). These tests pin that contract.

Run:  pytest tests/test_db_pool_knob_authority.py -v
"""
from __future__ import annotations

import logging

import pytest

import lib.database._core as core


def _write_env(tmp_path, body):
    p = tmp_path / '.env'
    p.write_text(body)
    return str(tmp_path)


@pytest.mark.unit
class TestPoolKnobAuthority:

    def test_env_file_beats_shadowing_export(self, tmp_path, monkeypatch, caplog):
        """The exact 2026-07-16 trap: shell exports 400, .env says 800 →
        .env wins AND a WARNING is emitted naming both values."""
        monkeypatch.setattr(core, 'BASE_DIR', _write_env(tmp_path, 'TOFU_DB_MAX_CONNS=800\n'))
        monkeypatch.setenv('TOFU_DB_MAX_CONNS', '400')  # stale container export
        with caplog.at_level(logging.WARNING, logger='lib.database._core'):
            val = core._pool_knob('TOFU_DB_MAX_CONNS', default='1000')
        assert val == '800', f'.env must win over the stale export, got {val!r}'
        assert any('DISAGREES' in r.message and '400' in r.message and '800' in r.message
                   for r in caplog.records), 'drift WARNING with both values must fire'

    def test_env_file_wins_silently_when_export_agrees(self, tmp_path, monkeypatch, caplog):
        """When the export agrees with .env there is no drift → no WARNING."""
        monkeypatch.setattr(core, 'BASE_DIR', _write_env(tmp_path, 'TOFU_DB_MAX_CONNS=800\n'))
        monkeypatch.setenv('TOFU_DB_MAX_CONNS', '800')
        with caplog.at_level(logging.WARNING, logger='lib.database._core'):
            val = core._pool_knob('TOFU_DB_MAX_CONNS', default='1000')
        assert val == '800'
        assert not any('DISAGREES' in r.message for r in caplog.records), \
            'no drift → no WARNING'

    def test_env_used_when_no_dotenv_entry(self, tmp_path, monkeypatch):
        """No .env entry → fall back to the environment (legacy precedence)."""
        monkeypatch.setattr(core, 'BASE_DIR', _write_env(tmp_path, '# unrelated\nFOO=bar\n'))
        monkeypatch.setenv('TOFU_DB_MAX_CONNS', '512')
        assert core._pool_knob('TOFU_DB_MAX_CONNS', default='1000') == '512'

    def test_default_when_neither_present(self, tmp_path, monkeypatch):
        """Neither .env nor env → the code default."""
        monkeypatch.setattr(core, 'BASE_DIR', str(tmp_path))  # no .env file
        monkeypatch.delenv('TOFU_DB_MAX_CONNS', raising=False)
        assert core._pool_knob('TOFU_DB_MAX_CONNS', default='1000') == '1000'

    def test_last_assignment_wins_and_comments_skipped(self, tmp_path, monkeypatch):
        """Mirror shell semantics: a commented line is ignored and the last
        non-comment assignment wins."""
        monkeypatch.setattr(core, 'BASE_DIR', _write_env(
            tmp_path, '# TOFU_DB_MAX_CONNS=100\nTOFU_DB_MAX_CONNS=700\nTOFU_DB_MAX_CONNS=900\n'))
        monkeypatch.delenv('TOFU_DB_MAX_CONNS', raising=False)
        assert core._pool_knob('TOFU_DB_MAX_CONNS', default='1000') == '900'


# ── standalone runner (mirrors the project's other DB test files) ──
if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
