"""No silent tool-call discards — every dropped call must leave a RECEIPT.

Root cause (owner report, conv ms8nmugr3gsjqc, 2026-07-31): the model's reply
repeated the SAME paragraph 3 times — "代码执行没有返回输出，并且我已经达到了
本回合的工具调用限制" (no tool output + I hit the per-turn tool-call limit).
Measured truth in the task log (task 695e4b39):

  * Rounds 1-3 each came back with ONE tool call carrying an EMPTY function
    name, EMPTY id, and the byte-identical canned payload
    ``{"code": "print('HELLO_CHECK')"}`` — an upstream-injected probe blob
    (21 sightings across 9 unrelated tasks in 2 days; zero hits in our
    codebase; only on Opus-5 wire ids).
  * ``parse_tool_calls``' drop guard answered each with ``logger.warning`` +
    ``continue`` — no execution, no round, and fatally NO tool_result. The
    orphan-stripper then removed the trace from the wire, so the model stared
    at an unexplained hole and INVENTED an explanation ("tool-call limit" —
    a limit that does not exist; ``max_tool_rounds = 999_999_999``).
  * The swarm sub-agent path never had this bug: it returns
    ``f'Error: ignored malformed tool name ...'`` to the model
    (lib/swarm/agent.py). Only the CHAT path was silent.

Fix (pt_914bb7308c294b02):
  A. Every discarded call (missing name / internal artefact / malformed name /
     phantom empty-args duplicate) is converted to a REJECTED round + a
     model-facing receipt that rides the same pipeline lane as hallucinated
     tools — the model always gets a ``role:'tool'`` answer, in original
     tool-call order, explicitly defusing the "limit" confabulation.
  B. Known upstream probe payloads (``HELLO_CHECK``) are recognised at the
     shared ingestion seam and counted via ``audit_log('upstream_tool_probe')``
     so the pollution's size is a greppable number, not a user screenshot.

Run directly (conda env pytest is flaky):

    python3 tests/test_tool_parse_discard_receipt.py
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.tasks_pkg.tool_dispatch import parse_tool_calls
from lib.tool_input_repair import ingest_tool_call

pytestmark = pytest.mark.unit


def _schema(names):
    return [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
            for n in names]


def _make_task(tool_names=('list_dir', 'read_files')):
    return {
        'id': 'task_discard_' + 'x' * 6,
        'convId': 'convdiscard',
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        'toolRounds': [],
        'aborted': False,
        '_tool_schema': _schema(tool_names),
    }


def _assistant(tool_calls, content=''):
    return {'content': content, 'tool_calls': tool_calls}


def _tc(name, args='{}', tc_id='call_x1'):
    return {'id': tc_id, 'type': 'function',
            'function': {'name': name, 'arguments': args}}


PROBE_ARGS = '{"code": "print(\'HELLO_CHECK\')"}'


def _probe_tc():
    """The byte-identical upstream probe blob seen in production."""
    return {'id': '', 'type': 'function',
            'function': {'name': '', 'arguments': PROBE_ARGS}}


class TestDiscardReceipt(unittest.TestCase):

    # ── A1: the production case — nameless probe gets a receipt, not silence ──

    def test_nameless_probe_produces_rejected_receipt(self):
        task = _make_task()
        probe = _probe_tc()
        parsed, _ = parse_tool_calls(_assistant([probe]), task,
                                     round_num=0, tool_round_num=0,
                                     project_enabled=False)
        self.assertEqual(len(parsed), 1,
                         'a discarded call must still produce a parsed entry '
                         '(the receipt) — silence is the bug')
        _tc_obj, _fn, tc_id, _args, _rn, round_entry, parse_err = parsed[0]
        # The receipt explicitly defuses the confabulation the model invented.
        self.assertIsNotNone(parse_err)
        self.assertIn('DID NOT RUN', parse_err)
        self.assertIn('NOT a tool-call limit', parse_err)
        # A rejected round renders in the UI instead of vanishing.
        self.assertEqual(round_entry.get('status'), 'rejected')
        self.assertEqual(round_entry.get('_rejected', {}).get('kind'),
                         'dropped_artifact')
        self.assertEqual(round_entry.get('_rejected', {}).get('drop_reason'),
                         'missing')
        self.assertIn(round_entry, task['toolRounds'])

    def test_empty_id_is_minted_and_written_back_for_wire_pairing(self):
        """The probe arrived with id=''. The synthetic tool_result must pair
        with the tool_use ON THE WIRE — the assistant message shares the tc
        dict, so the minted id has to be written back onto it, else the pair
        is orphaned again and the stripper re-silences it."""
        task = _make_task()
        probe = _probe_tc()
        parsed, _ = parse_tool_calls(_assistant([probe]), task,
                                     round_num=0, tool_round_num=0,
                                     project_enabled=False)
        tc_id = parsed[0][2]
        self.assertTrue(tc_id, 'an empty wire id must be replaced by a mint')
        self.assertEqual(probe['id'], tc_id,
                         'the minted id must be written back onto the shared '
                         'tool_call dict so the role:tool message pairs with '
                         'the tool_use the next API round carries')

    # ── A2: every drop reason converts, each with its own diagnosis ──

    def test_internal_artifact_gets_receipt(self):
        task = _make_task()
        parsed, _ = parse_tool_calls(
            _assistant([_tc('antml:thinking', '{}', 'call_a')]), task,
            round_num=0, tool_round_num=0, project_enabled=False)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0][5].get('_rejected', {}).get('drop_reason'),
                         'internal_artifact')
        self.assertIn('DID NOT RUN', parsed[0][6])

    def test_malformed_name_gets_receipt(self):
        task = _make_task()
        parsed, _ = parse_tool_calls(
            _assistant([_tc('list_dir">.</parameter>', '{}', 'call_m')]), task,
            round_num=0, tool_round_num=0, project_enabled=False)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0][5].get('_rejected', {}).get('drop_reason'),
                         'malformed')
        self.assertIn('DID NOT RUN', parsed[0][6])

    def test_phantom_empty_args_duplicate_gets_receipt(self):
        """Valid name + empty args + a same-named sibling WITH real args:
        the phantom used to be skipped silently too."""
        task = _make_task()
        parsed, _ = parse_tool_calls(
            _assistant([_tc('list_dir', '{"path": "."}', 'call_real'),
                        _tc('list_dir', '', 'call_phantom')]), task,
            round_num=0, tool_round_num=0, project_enabled=False)
        self.assertEqual(len(parsed), 2)
        real, phantom = parsed[0], parsed[1]
        self.assertIsNone(real[6], 'the real sibling must parse cleanly')
        self.assertNotEqual(real[5].get('status'), 'rejected')
        self.assertIsNotNone(phantom[6])
        self.assertIn('DID NOT RUN', phantom[6])
        self.assertEqual(phantom[5].get('_rejected', {}).get('kind'),
                         'phantom_empty_args')

    # ── THE invariant: no input call ever vanishes ──

    def test_no_input_call_ever_vanishes(self):
        """Property: len(parsed) == len(tool_calls) for a mixed junk batch.
        Every call is either dispatchable or carries a receipt — there is no
        third, silent, outcome."""
        task = _make_task()
        batch = [_probe_tc(),                                  # missing name
                 _tc('antml:thinking', '{}', 'c2'),            # internal
                 _tc('list_dir">.</parameter>', '{}', 'c3'),   # malformed
                 _tc('list_dir', '{"path": "."}', 'c4'),       # real
                 _tc('list_dir', '', 'c5')]                    # phantom
        parsed, _ = parse_tool_calls(_assistant(batch), task,
                                     round_num=0, tool_round_num=0,
                                     project_enabled=False)
        self.assertEqual(len(parsed), len(batch))
        self.assertEqual(len(task['toolRounds']), len(batch))

    # ── Presentation decisions pinned ──

    def test_discard_does_not_consume_the_round_prose_tag(self):
        """The per-round assistant prose belongs with the first REAL entry.
        A junk artefact is not model content — letting it consume the
        ``_ac_tagged`` slot would orphan the round's narration."""
        task = _make_task()
        parsed, _ = parse_tool_calls(
            _assistant([_probe_tc(),
                        _tc('list_dir', '{"path": "."}', 'call_real')],
                       content='Let me list the directory.'),
            task, round_num=0, tool_round_num=0, project_enabled=False)
        junk, real = parsed[0][5], parsed[1][5]
        self.assertNotIn('assistantContent', junk)
        self.assertEqual(real.get('assistantContent'),
                         'Let me list the directory.')

    def test_real_call_untouched_by_the_conversion(self):
        """Regression: the normal dispatch path is byte-identical."""
        task = _make_task()
        parsed, _ = parse_tool_calls(
            _assistant([_tc('list_dir', '{"path": "."}')]), task,
            round_num=0, tool_round_num=0, project_enabled=False)
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0][6])
        self.assertNotEqual(parsed[0][5].get('status'), 'rejected')
        self.assertNotIn('_rejected', parsed[0][5])


class TestUpstreamProbeAudit(unittest.TestCase):
    """B: known canned probes are counted at the shared ingestion seam."""

    def test_hello_check_probe_emits_audit(self):
        with mock.patch('lib.tool_input_repair._ingest.audit_log') as m_audit:
            ing = ingest_tool_call(_probe_tc(), known_tools={'list_dir'},
                                   model='opus-5', conv_id='convprobe')
        self.assertTrue(ing.dropped)
        kinds = [c.args[0] for c in m_audit.call_args_list]
        self.assertIn('upstream_tool_probe', kinds)
        probe_calls = [c for c in m_audit.call_args_list
                       if c.args[0] == 'upstream_tool_probe']
        self.assertEqual(probe_calls[0].kwargs.get('marker'), 'HELLO_CHECK')

    def test_benign_empty_name_does_not_emit_probe_audit(self):
        """The audit is marker-keyed, not drop-keyed: an empty-named call
        WITHOUT a known canary payload is a plain drop, not a probe sighting."""
        with mock.patch('lib.tool_input_repair._ingest.audit_log') as m_audit:
            ing = ingest_tool_call(_tc('', '{"q": 1}', 'call_b'),
                                   known_tools={'list_dir'},
                                   model='m', conv_id='c')
        self.assertTrue(ing.dropped)
        kinds = [c.args[0] for c in m_audit.call_args_list]
        self.assertNotIn('upstream_tool_probe', kinds)

    def test_probe_marker_constant_exists(self):
        """Structural: the marker list is exported so adding the next canary
        is a one-line change, not a code dig."""
        from lib.tool_input_repair._ingest import _UPSTREAM_PROBE_MARKERS
        self.assertIn('HELLO_CHECK', _UPSTREAM_PROBE_MARKERS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
