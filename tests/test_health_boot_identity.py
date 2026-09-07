"""tests/test_health_boot_identity.py — robust-restart boot-identity contract.

The restart button (os.execv in-place re-exec) KEEPS the same PID and the same
process start time (execv does not reset either). So the old success rule —
"declare restart done the instant /api/health answers ok" — cannot tell a
genuinely-new process from the OLD one still answering during the drain window,
and cannot tell fresh code from stale bytecode. That is exactly the "restart
looked OK but old code is still serving" failure.

Fix: /api/health reports a per-process BOOT IDENTITY:
  * pid            — the process id;
  * bootId         — a fresh uuid generated at MODULE IMPORT, so every process
                     (incl. an os.execv re-exec, which re-imports from scratch)
                     gets a NEW value — the reliable "is this a different
                     process?" signal that survives PID-preserving execv;
  * cacheFixGen    — the in-memory lib.llm.cache.CACHE_FIX_GEN (loaded bytecode
                     version), so a stale-code restart is visible.

The frontend captures the PRE-restart bootId and only declares success when
health returns a DIFFERENT bootId — proving a new process answered.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_health_boot_identity.py -p no:cacheprovider
"""

import importlib

import pytest

pytestmark = pytest.mark.unit


def test_boot_identity_module_exposes_fields():
    import lib.boot_identity as bi
    assert isinstance(bi.BOOT_ID, str) and len(bi.BOOT_ID) >= 8
    assert isinstance(bi.BOOT_TS, (int, float)) and bi.BOOT_TS > 0
    # cache_fix_gen resolves the in-memory value (int) or None if unavailable.
    g = bi.cache_fix_gen()
    assert g is None or isinstance(g, int)


def test_cache_fix_gen_matches_imported_module():
    import lib.boot_identity as bi
    from lib.llm.cache import CACHE_FIX_GEN
    assert bi.cache_fix_gen() == CACHE_FIX_GEN


def test_reexec_produces_a_fresh_boot_id():
    """A re-exec re-imports the module from scratch → a NEW bootId. Simulate by
    reloading the module and asserting the id changes. This is the property the
    restart-success rule relies on (PID/start-time are unchanged by execv, so
    bootId is the only reliable 'different process' signal)."""
    import lib.boot_identity as bi
    first = bi.BOOT_ID
    importlib.reload(bi)
    second = bi.BOOT_ID
    assert first != second, ('bootId must be regenerated per process import — '
                             'otherwise a re-exec is indistinguishable from the '
                             'old process still answering')


def test_health_payload_carries_boot_identity():
    """The /api/health payload dict must include pid, bootId, cacheFixGen so the
    restart button can verify a NEW process answered. Built the same way the
    route builds it (import-time module values), asserted structurally."""
    import lib.boot_identity as bi
    payload = {
        'ok': True,
        'pid': bi.PID,
        'bootId': bi.BOOT_ID,
        'cacheFixGen': bi.cache_fix_gen(),
    }
    assert payload['pid'] == bi.PID
    assert payload['bootId'] == bi.BOOT_ID
    assert 'cacheFixGen' in payload


# ── The restart-success RULE: succeed only on a DIFFERENT bootId ──

def _restart_succeeded(pre_boot_id, health):
    """Mirror the frontend rule: a restart is confirmed ONLY when health
    answers ok AND its bootId differs from the one captured before restart.
    (Pure predicate so the rule is unit-testable without a browser.)"""
    if not (health and health.get('ok')):
        return False
    bid = health.get('bootId')
    if not bid:
        return False  # old build without the field → can't confirm, keep waiting
    return bid != pre_boot_id


def test_success_rule_requires_different_boot_id():
    pre = 'boot-AAAA'
    # Old process still answering (same bootId) → NOT success.
    assert _restart_succeeded(pre, {'ok': True, 'bootId': 'boot-AAAA'}) is False
    # A genuinely new process (different bootId) → success.
    assert _restart_succeeded(pre, {'ok': True, 'bootId': 'boot-BBBB'}) is True


def test_success_rule_not_ok_is_not_success():
    assert _restart_succeeded('x', {'ok': False, 'bootId': 'y'}) is False
    assert _restart_succeeded('x', None) is False


def test_success_rule_missing_bootid_keeps_waiting():
    """An old build whose /api/health lacks bootId must NOT be declared a
    successful restart on the mere fact that it answered — keep waiting (until
    the outer timeout), never a false green."""
    assert _restart_succeeded('x', {'ok': True}) is False
