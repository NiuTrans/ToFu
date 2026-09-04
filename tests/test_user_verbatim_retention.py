"""tests/test_user_verbatim_retention.py — User-verbatim retention tests.

Covers the Codex-inspired retention (codex-rs compact.rs keeps user messages
intact): when L2 summarizes the old region, the user's literal instructions
must survive VERBATIM — inside ONE ``_isMeta`` wrapper that is transparent
to the turn-boundary machinery and immune to feedback duplication.

  1. ``_collect_user_verbatim`` selection policy (order / budget / skips /
     dedupe).
  2. ``execute_compact_tool`` rebuild places the wrapper between the
     objective anchor and the preserved recent region.
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit

# Boot the Flask→Quart shim BEFORE any lib.* imports (see test_hook_taxonomy).
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location(
    'server_for_shim_verbatim_test', 'server.py')
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
del _spec, _mod, _importlib_util

import lib.tasks_pkg.compaction._layer2._compact as _l2
from lib.tasks_pkg.compaction._constants import (
    _USER_VERBATIM_BUDGET_TOKENS,
    _USER_VERBATIM_MAX_MESSAGES,
)
from lib.tasks_pkg.compaction._layer2._anchor import (
    _collect_user_verbatim,
    _find_turn_boundary,
)
from lib.tasks_pkg.compaction.api import (
    execute_compact_tool,
)
from lib.tasks_pkg.wire_messages import apply_wire_sanitize
from lib.llm_sanitize import (
    _merge_consecutive_same_role,
    _strip_non_api_fields,
)


def _u(text, **flags):
    m = {'role': 'user', 'content': text}
    m.update(flags)
    return m


def _a(text):
    return {'role': 'assistant', 'content': text}


class TestCollectUserVerbatim(unittest.TestCase):

    def test_newest_first_under_count_cap_chronological_output(self):
        old = [_u(f'requirement {k}') for k in range(12)]
        picked = _collect_user_verbatim(old)
        self.assertEqual(len(picked), _USER_VERBATIM_MAX_MESSAGES)
        # The NEWEST 8 survive, presented oldest-first.
        self.assertEqual(picked[0], 'requirement 4')
        self.assertEqual(picked[-1], 'requirement 11')

    def test_skips_synthetic_and_empty(self):
        old = [
            _u('real instruction'),
            _u('<token_budget>…</token_budget>', _isMeta=True),
            _u('virtual turn', _initiator='autopilot'),
            _u('directive', _isVuDirective=True),
            _u('inbox notice', _isInboxInject=True),
            _u('brain control', _initiator='brain'),
            _u('timer control', _initiator='timer'),
            _u('peer agent control', _initiator='peer'),
            _u('   '),
            {'role': 'user',
             'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]},
        ]
        picked = _collect_user_verbatim(old)
        self.assertEqual(picked, ['real instruction'])

    def test_human_operator_guidance_is_retained(self):
        old = [_u('operator says use option B', _initiator='operator')]
        self.assertEqual(_collect_user_verbatim(old),
                         ['operator says use option B'])

    def test_inbox_with_human_steer_is_kept(self):
        old = [_u('human steer inside inbox', _isInboxInject=True,
                  _containsHumanSteer=True)]
        self.assertEqual(_collect_user_verbatim(old),
                         ['human steer inside inbox'])

    def test_exact_duplicates_collapsed(self):
        old = [_u('continue'), _a('ok'), _u('continue'), _u('unique')]
        picked = _collect_user_verbatim(old)
        self.assertEqual(picked, ['continue', 'unique'])

    def test_list_content_text_blocks(self):
        old = [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'first part'},
            {'type': 'text', 'text': 'second part'},
        ]}]
        self.assertEqual(_collect_user_verbatim(old),
                         ['first part\nsecond part'])

    def test_budget_cap_greedy_fill(self):
        # Newest message always retained (even if it alone busts the
        # budget); older messages fill greedily while they fit.
        huge = 'x' * (8 * _USER_VERBATIM_BUDGET_TOKENS)
        old = [_u('small one'), _u('small two'), _u(huge)]
        picked = _collect_user_verbatim(old, budget_tokens=100)
        self.assertEqual(picked, [huge])
        # Without the huge one, both small messages fit.
        picked = _collect_user_verbatim(old[:2], budget_tokens=100)
        self.assertEqual(picked, ['small one', 'small two'])


class TestExecuteCompactRetainsUserVerbatim(unittest.TestCase):

    def setUp(self):
        self._orig_summary = _l2._generate_query_aware_summary
        self._orig_archive = _l2._archive_transcript
        _l2._generate_query_aware_summary = (
            lambda msgs, query, pfx, **kw: 'SUMMARY TEXT')
        _l2._archive_transcript = lambda *a, **kw: 1

    def tearDown(self):
        _l2._generate_query_aware_summary = self._orig_summary
        _l2._archive_transcript = self._orig_archive

    def _run(self, messages, budget):
        task = {'id': 'tuv-001', 'convId': 'cuv-001', '_userId': 1,
                'config': {}}
        result = execute_compact_tool(
            messages, task=task, preserve_budget_tokens=budget)
        return result, messages

    def test_wrapper_between_anchor_and_recent(self):
        messages = [
            {'role': 'system', 'content': 'sys'},
            _u('the original goal'),
            _a('working on it…'),
            _u('requirement alpha'),
            _a('noted alpha'),
            _u('requirement beta'),
            _a('noted beta'),
            _u('current turn: finish it'),
            _a('on it'),
        ]
        result, msgs = self._run(messages, budget=1)
        self.assertIn('SUMMARY TEXT', result)

        wrappers = [m for m in msgs
                    if m.get('role') == 'user'
                    and '<retained_user_messages>' in str(m.get('content'))]
        self.assertEqual(len(wrappers), 1, 'exactly one retention wrapper')
        wrapper = wrappers[0]
        self.assertTrue(wrapper.get('_isMeta'))
        self.assertIn('requirement alpha', wrapper['content'])
        self.assertIn('requirement beta', wrapper['content'])
        self.assertNotIn('the original goal', wrapper['content'],
                         'the anchor is re-inserted separately, never '
                         'duplicated inside the wrapper')

        # Order: system → anchor → wrapper → recent (current turn).
        roles = [(m.get('role'),
                  str(m.get('content'))[:24]) for m in msgs]
        self.assertEqual(roles[0][0], 'system')
        self.assertEqual(roles[1], ('user', 'the original goal'))
        self.assertIs(msgs[2], wrapper)
        self.assertEqual(roles[3], ('user', 'current turn: finish it'))

        # The anchor survives VERBATIM exactly once.
        self.assertEqual(
            sum(1 for m in msgs if m.get('content') == 'the original goal'),
            1)

        # The wrapper is turn-boundary-transparent: with a TIGHT budget the
        # current turn still starts at the REAL last user message — the
        # _isMeta wrapper must never become a turn start.
        boundary = _find_turn_boundary(msgs, budget_tokens=1)
        self.assertEqual(msgs[boundary]['content'], 'current turn: finish it')

        # The exact rebuild then crosses build_body's real field-strip +
        # same-role boundary.  The retained wrapper is synthetic on BOTH
        # original edges: anchor→wrapper and wrapper→current user.  It must
        # not create the production false-positive warning that used to fire
        # on every later LLM round after compaction.
        clean = _strip_non_api_fields(msgs)
        with self.assertNoLogs('lib.llm_sanitize._messages', level='WARNING'):
            wire = _merge_consecutive_same_role(clean)
        self.assertEqual(wire[1]['role'], 'user')
        self.assertIn('the original goal', str(wire[1]['content']))
        self.assertIn('current turn: finish it', str(wire[1]['content']))

    def test_no_real_user_in_old_region_no_wrapper(self):
        authoritative_anchor = _u('the original goal')
        messages = [
            {'role': 'system', 'content': 'sys'},
            authoritative_anchor,
            _a('a very long earlier reply with no user text in between'),
            _u('current turn: finish it'),
            _a('on it'),
        ]
        _result, msgs = self._run(messages, budget=1)
        self.assertFalse(any('<retained_user_messages>' in str(m.get('content'))
                             for m in msgs),
                         'no real user message in the old region → no '
                         'wrapper (byte-identical to pre-feature behavior)')
        self.assertNotIn('_isObjectiveAnchor', authoritative_anchor,
                         'L2 must not mutate the authoritative anchor object')
        self.assertIsNot(msgs[1], authoritative_anchor)
        self.assertTrue(msgs[1].get('_isObjectiveAnchor'))

        # Zero retained-user rows rebuilds ``system → objective anchor →
        # current user``.  That is a designed L2 seam, not a duplicate user
        # producer.  Exercise the complete model-neutral wire tail so private
        # structure can guide diagnostics but can never leave the process.
        with self.assertNoLogs('lib.llm_sanitize._messages', level='WARNING'):
            wire = apply_wire_sanitize(msgs)
        self.assertEqual(wire, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user',
             'content': 'the original goal\n\ncurrent turn: finish it'},
            {'role': 'assistant', 'content': 'on it'},
        ])
        self.assertFalse(any(
            key.startswith('_tofu')
            for message in wire
            for key in message
        ), 'short-lived L2 classification hints must never reach wire output')

    def test_second_compaction_does_not_duplicate_wrapper(self):
        messages = [
            {'role': 'system', 'content': 'sys'},
            _u('the original goal'),
            _a('work 1'),
            _u('requirement alpha'),
            _a('noted'),
            _u('turn two'),
            _a('work 2'),
            _u('turn three'),
            _a('work 3'),
        ]
        _r1, msgs1 = self._run(messages, budget=1)
        wrappers1 = [m for m in msgs1 if '<retained_user_messages>'
                     in str(m.get('content'))]
        self.assertEqual(len(wrappers1), 1)

        # Second compaction over the rebuilt list: the previous wrapper is
        # _isMeta → excluded from extraction → no accumulation.
        _r2, msgs2 = self._run(msgs1, budget=1)
        wrappers2 = [m for m in msgs2 if '<retained_user_messages>'
                     in str(m.get('content'))]
        self.assertLessEqual(len(wrappers2), 1,
                             'retention wrappers must never accumulate '
                             'across successive compactions')


if __name__ == '__main__':
    unittest.main()
