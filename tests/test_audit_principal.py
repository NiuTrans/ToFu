"""tests/test_audit_principal.py — audit_log auto-attaches the bound principal.

docs/ENTERPRISE_READINESS_AUDIT.md (R11): principal identity rides the same
ContextVar pattern as request_id, so every audit entry carries
key_id/user_id without each event caller remembering to pass them.
Explicit caller-supplied details always win over the bound principal.
"""

import json

import pytest

import lib.log as log_mod

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_principal():
    log_mod.set_req_id('')
    log_mod.set_principal('', '')
    log_mod.clear_log_context()
    yield
    log_mod.set_principal('', '')
    log_mod.clear_log_context()
    log_mod.set_req_id('')


def _read_last_entry(tmp_path):
    lines = (tmp_path / 'audit.log').read_text(encoding='utf-8').splitlines()
    return json.loads(lines[-1])


def test_audit_log_attaches_bound_principal(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.set_principal('key-abc', '42')
    log_mod.audit_log('principal_event')
    entry = _read_last_entry(tmp_path)
    assert entry['key_id'] == 'key-abc'
    assert entry['user_id'] == '42'


def test_explicit_details_win_over_bound_principal(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.set_principal('key-abc', '42')
    log_mod.audit_log('override_event', key_id='key-other', user_id='7')
    entry = _read_last_entry(tmp_path)
    assert entry['key_id'] == 'key-other'
    assert entry['user_id'] == '7'


def test_empty_principal_attaches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.audit_log('synthetic_event')
    entry = _read_last_entry(tmp_path)
    assert 'key_id' not in entry
    assert 'user_id' not in entry


def test_background_log_context_supplies_audit_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    with log_mod.bind_log_context(user_id='worker-owner', key_id='worker-key'):
        log_mod.audit_log('background_event')
    entry = _read_last_entry(tmp_path)
    assert entry['key_id'] == 'worker-key'
    assert entry['user_id'] == 'worker-owner'


def test_audit_correlation_lanes_redact_credential_shaped_identifiers(
        tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.set_req_id('ghp_requestcredentialvalue')
    log_mod.set_principal('sk-principalcredentialvalue', 'worker-owner')
    log_mod.audit_log('credential-shaped-identifiers')
    raw = (tmp_path / 'audit.log').read_text(encoding='utf-8')
    assert 'requestcredentialvalue' not in raw
    assert 'principalcredentialvalue' not in raw


def test_audit_timestamp_is_authoritative(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, 'AUDIT_LOG_FILE', str(tmp_path / 'audit.log'))
    monkeypatch.setattr(log_mod, 'LOG_DIR', str(tmp_path))
    log_mod.audit_log('clocked-event', timestamp='1970-01-01T00:00:00Z')
    entry = _read_last_entry(tmp_path)
    assert entry['event'] == 'clocked-event'
    assert entry['timestamp'] != '1970-01-01T00:00:00Z'
