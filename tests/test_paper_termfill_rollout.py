#!/usr/bin/env python3
"""Rollout gating for the terminology-backfill pass: interactive ON, headless opt-in.

Measurement (2026-07-11, real corpus, en+zh, 6 reports / 106 defs): after
detector-precision suppression the backfill closes 75% of flagged gaps with
correctness 4.91/5 and only 1% wrong/misleading. That clears the bar, so the
CURE is turned on for the INTERACTIVE reader by default — while the headless /
BYO API stays opt-in, because the pass SILENTLY BILLS an LLM call (same class as
``langCorrectionEnabled`` in the personal-scope registry).

The gating mirrors the personal-scope discipline EXACTLY:
  * ``paperTermfillEnabled`` registered in PERSONAL_CAPABILITIES with
    headless_default=False, ui_default=True → every headless cfg-builder stamps
    it False via ``apply_headless_personal_defaults`` unless the caller opts in.
  * ``resolve_paper_termfill_enabled(cfg)``: explicit key honoured; ABSENT → True
    (the interactive report route passes no such key).
  * A global env master switch ``TOFU_PAPER_TERMFILL`` can force-DISABLE
    fleet-wide (set to 0/off) even for interactive — a kill switch — but is no
    longer REQUIRED to turn the feature on.

Pure-unit, offline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_registered_in_personal_scope_fail_closed():
    from lib.agent_core.personal_scope import PERSONAL_CAPABILITIES
    assert 'paperTermfillEnabled' in PERSONAL_CAPABILITIES, \
        'termfill must be registered so headless fails closed'
    cap = PERSONAL_CAPABILITIES['paperTermfillEnabled']
    assert cap.headless_default is False, 'headless default must be fail-closed'
    assert cap.ui_default is True, 'interactive default must be ON'
    _ok('paperTermfillEnabled registered: headless_default=False, ui_default=True')


def test_headless_builder_stamps_it_false():
    from lib.agent_core.personal_scope import apply_headless_personal_defaults
    cfg = {}
    apply_headless_personal_defaults(cfg)
    assert cfg.get('paperTermfillEnabled') is False, \
        'a bare headless cfg must have termfill stamped fail-closed'
    # Explicit opt-in survives.
    cfg2 = {'paperTermfillEnabled': True}
    apply_headless_personal_defaults(cfg2)
    assert cfg2['paperTermfillEnabled'] is True, 'explicit opt-in must win'
    _ok('headless cfg-builder stamps termfill False; explicit opt-in survives')


def test_resolver_interactive_default_on():
    from lib.agent_core.personal_scope import resolve_paper_termfill_enabled
    # Interactive report route passes no cfg / no key → ON.
    assert resolve_paper_termfill_enabled(None) is True
    assert resolve_paper_termfill_enabled({}) is True
    assert resolve_paper_termfill_enabled({'model': 'x'}) is True
    # Explicit values honoured both ways.
    assert resolve_paper_termfill_enabled({'paperTermfillEnabled': True}) is True
    assert resolve_paper_termfill_enabled({'paperTermfillEnabled': False}) is False
    _ok('resolver: absent → ON (interactive); explicit honoured both ways')


def test_env_master_kill_switch():
    """TOFU_PAPER_TERMFILL is no longer REQUIRED to enable, but an explicit
    0/off is a fleet-wide kill switch that overrides the interactive default."""
    from lib.paper.terminology_backfill import termfill_globally_disabled
    os.environ.pop('TOFU_PAPER_TERMFILL', None)
    assert termfill_globally_disabled() is False, 'absent env → not disabled (default on)'
    os.environ['TOFU_PAPER_TERMFILL'] = '0'
    assert termfill_globally_disabled() is True, 'explicit 0 → kill switch engaged'
    os.environ['TOFU_PAPER_TERMFILL'] = 'off'
    assert termfill_globally_disabled() is True
    os.environ['TOFU_PAPER_TERMFILL'] = '1'
    assert termfill_globally_disabled() is False, 'explicit 1 → not disabled'
    os.environ.pop('TOFU_PAPER_TERMFILL', None)
    _ok('env master switch: absent/1 → enabled; 0/off → fleet-wide kill switch')


def test_engine_resolves_interactive_on_headless_off():
    """The engine hook must fire for an interactive task (no cfg key) and NOT
    fire for a headless task (cfg stamped False), with NO env flag set."""
    import lib.paper.report_engine.worker as re_mod
    import lib.paper.terminology_backfill as tb
    from lib.paper.artifact_repository import PaperArtifactRepository
    from lib.paper.report_runtime import _new_report_task
    from unittest.mock import patch

    os.environ.pop('TOFU_PAPER_TERMFILL', None)  # rely on the new default, not the env

    calls = []
    orig_run = tb.run_report_termfill
    tb.run_report_termfill = lambda *a, **k: calls.append(k.get('phash', '?')) or {'markdown': '', 'closed': False}

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        body = ('# T\n\n## 🔑 Core Terminology\n| Term | Definition | Why |\n'
                '|--|--|--|\n| RLHF | rlhf. | x |\n\n## 💡 Method\nWe use an SFT stage.\n\n'
                '## 📝 Technical Reference\nEnd.\n')
        if on_content:
            on_content(body)
        return {'role': 'assistant', 'content': body, 'tool_calls': []}, 'stop', \
               {'prompt_tokens': 5, 'completion_tokens': 5, '_dispatch': {}}
    orig_disp = re_mod.dispatch_stream
    re_mod.dispatch_stream = _fake
    try:
        # Interactive: no termfill cfg key → resolver True → hook fires.
        # (paperInsightEnabled=False keeps the UNRELATED insight second pass
        # offline — its real LLM dispatch 401-cycles forever on CI. The
        # termfill scenario only needs paperTermfillEnabled to stay absent.)
        t1 = _new_report_task('rpt_int', 'phashint0000000000000000000000', 'en', None,
                              client_title='T', config={'paperInsightEnabled': False,
                                        'paperCheckpointsEnabled': False}, user_id=TEST_OWNER_USER_ID)
        with patch.object(
                PaperArtifactRepository, 'put_report', return_value=True):
            re_mod.run_report_task(
                t1, [{'role': 'system', 'content': 's'},
                     {'role': 'user', 'content': 'p'}], [])
        assert calls, 'interactive task (no cfg) must fire the backfill by default'

        calls.clear()
        # Headless: config stamped fail-closed → resolver False → hook skips.
        t2 = _new_report_task('rpt_hl', 'phashhl00000000000000000000000', 'en', None,
                              client_title='T', config={'paperTermfillEnabled': False,
                                                        'paperInsightEnabled': False,
                                                        'paperCheckpointsEnabled': False}, user_id=TEST_OWNER_USER_ID)
        with patch.object(
                PaperArtifactRepository, 'put_report', return_value=True):
            re_mod.run_report_task(
                t2, [{'role': 'system', 'content': 's'},
                     {'role': 'user', 'content': 'p'}], [])
        assert not calls, 'headless task (termfill stamped False) must NOT fire the backfill'
    finally:
        re_mod.dispatch_stream = orig_disp
        tb.run_report_termfill = orig_run
        os.environ.pop('TOFU_PAPER_TERMFILL', None)
    _ok('engine: interactive (no cfg) fires; headless (stamped False) skips — no env flag needed')


def main():
    print()
    print(_color('═══ Paper Termfill Rollout-Gating Tests ═══', '36'))
    print()
    tests = [
        test_registered_in_personal_scope_fail_closed,
        test_headless_builder_stamps_it_false,
        test_resolver_interactive_default_on,
        test_env_master_kill_switch,
        test_engine_resolves_interactive_on_headless_off,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
