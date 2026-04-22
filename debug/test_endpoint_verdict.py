"""Unit tests for the three-way endpoint verdict parser.

Covers:
  1. Explicit [VERDICT: STOP]
  2. Explicit [VERDICT: CONTINUE_WORKER]
  3. Explicit [VERDICT: CONTINUE_PLANNER]
  4. Legacy bare [VERDICT: CONTINUE] (must map to 'worker')
  5. No tag at all (must default to 'worker')
  6. Double tag (last wins)
  7. Defense-in-depth override: STOP with ❌ → 'planner'
  8. Defense-in-depth override: STOP with "still NOT met" → 'planner'
  9. Kill switch: CHATUI_ENDPOINT_REPLAN=0 downgrades planner→worker
     and disables the STOP-with-❌ override.

Run: python debug/test_endpoint_verdict.py
Exits 0 on success, raises on failure.
"""

import os
import sys

# Ensure project root is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _reload_endpoint_review():
    """Re-import endpoint_review so env changes to CHATUI_ENDPOINT_REPLAN take effect."""
    import importlib

    import lib.tasks_pkg.endpoint_review as mod
    importlib.reload(mod)
    return mod


def _test_replan_enabled():
    """Helper: run the parser tests with replan enabled."""
    os.environ['CHATUI_ENDPOINT_REPLAN'] = '1'
    mod = _reload_endpoint_review()
    _parse_verdict = mod._parse_verdict

    # 1. STOP
    fb, ph = _parse_verdict("All items ✅\n[VERDICT: STOP]")
    assert ph == 'stop', f"STOP: expected 'stop', got {ph!r}"
    assert fb == "All items ✅", f"STOP: feedback mismatch: {fb!r}"

    # 2. CONTINUE_WORKER
    fb, ph = _parse_verdict("Needs iter 2.\n[VERDICT: CONTINUE_WORKER]")
    assert ph == 'worker', f"CONT_WORKER: expected 'worker', got {ph!r}"

    # 3. CONTINUE_PLANNER
    fb, ph = _parse_verdict(
        "Plan is wrong — user changed scope mid-turn.\n[VERDICT: CONTINUE_PLANNER]"
    )
    assert ph == 'planner', f"CONT_PLANNER: expected 'planner', got {ph!r}"

    # 4. Legacy bare CONTINUE
    fb, ph = _parse_verdict("Needs work. [VERDICT: CONTINUE]")
    assert ph == 'worker', f"Legacy CONT: expected 'worker', got {ph!r}"

    # 5. No tag
    fb, ph = _parse_verdict("Some feedback without a verdict tag.")
    assert ph == 'worker', f"No-tag: expected 'worker', got {ph!r}"

    # 6. Double tag — last wins
    fb, ph = _parse_verdict(
        "First draft said [VERDICT: CONTINUE_WORKER] but on reflection "
        "[VERDICT: STOP]"
    )
    assert ph == 'stop', f"Double-tag: expected 'stop', got {ph!r}"

    # 7. Defense-in-depth: STOP with ❌ → planner
    fb, ph = _parse_verdict(
        "- ❌ Item 1: failing\n- ❌ Item 2: also failing\n[VERDICT: STOP]"
    )
    assert ph == 'planner', (
        f"Override STOP→planner (❌): expected 'planner', got {ph!r}"
    )

    # 8. Defense-in-depth: STOP with "still NOT met"
    fb, ph = _parse_verdict(
        "Acceptance criterion 1 is still NOT met.\n[VERDICT: STOP]"
    )
    assert ph == 'planner', (
        f"Override STOP→planner (phrase): expected 'planner', got {ph!r}"
    )

    # STOP without any unresolved markers should remain STOP
    fb, ph = _parse_verdict("Everything passes ✅ ✅ ✅.\n[VERDICT: STOP]")
    assert ph == 'stop', f"Clean STOP: expected 'stop', got {ph!r}"

    # Verdict tag correctly stripped from feedback
    fb, _ph = _parse_verdict("Test body.\n### Verdict\n[VERDICT: STOP]")
    assert '[VERDICT' not in fb, f"Tag not stripped: {fb!r}"
    assert '### Verdict' not in fb, f"Header not stripped: {fb!r}"

    print('[test_endpoint_verdict] replan-enabled: all 10 checks passed ✅')


def _test_replan_disabled():
    """When CHATUI_ENDPOINT_REPLAN=0: planner → worker, override disabled."""
    os.environ['CHATUI_ENDPOINT_REPLAN'] = '0'
    mod = _reload_endpoint_review()
    _parse_verdict = mod._parse_verdict

    # planner → worker downgrade
    fb, ph = _parse_verdict("Replan needed. [VERDICT: CONTINUE_PLANNER]")
    assert ph == 'worker', (
        f"Kill-switch planner→worker: expected 'worker', got {ph!r}"
    )

    # STOP-with-❌ override suppressed (stays STOP)
    fb, ph = _parse_verdict("- ❌ Item 1 failing\n[VERDICT: STOP]")
    assert ph == 'stop', (
        f"Kill-switch STOP-with-❌: expected 'stop' (override disabled), "
        f"got {ph!r}"
    )

    # Restore default for any follow-up tests
    os.environ['CHATUI_ENDPOINT_REPLAN'] = '1'
    _reload_endpoint_review()

    print('[test_endpoint_verdict] replan-disabled: all 2 checks passed ✅')


def main():
    _test_replan_enabled()
    _test_replan_disabled()
    print('\n[test_endpoint_verdict] ALL TESTS PASSED')


if __name__ == '__main__':
    main()
