"""Tool-call NAME accumulation must treat the name as a one-shot identifier.

Root cause this pins (2026-07-27, conv ms2vpi7jned92h): the SSE accumulator
appended tool names with ``+=`` and keyed slots on ``tc.get('index', 0)``. Two
distinct tool calls landing in one slot therefore produced a CONCATENATED name
(``read_filesrun_command``) that no tool registry can dispatch.

Attribution was settled offline, not by guesswork: every concatenated name seen
in production splits EXACTLY ONCE into real tool names from the builtin schema
set, while genuine model inventions (``module_buffer_manager``, ``phantomzz``)
do not split at all. A model fabricating a name that happens to be a lossless,
uniquely-segmentable concatenation of two real tools — five times over — is not
a credible explanation; ``+=`` on slot collision produces it by construction.

The contract these tests pin:
  * a tool NAME is assigned once, never appended to;
  * a second name arriving into an occupied slot opens a NEW slot;
  * a delta with no ``index`` gets the next free slot, not slot 0;
  * every LATER delta bearing a reused upstream index — arguments included —
    follows the call it belongs to, via the upstream-index → internal-slot map.

Two slot-routing paths exist and they are NOT redundant:
  * ``_tc_index_map`` is REACTIVE — it only re-points an upstream index after a
    name conflict has already opened a new slot;
  * ``_tc_unindexed_slot`` is PROACTIVE — it hands each newly-named unindexed
    call the next free slot at the moment the name arrives.
With neither an ``index`` nor an ``id``, the map has no trigger at all, so only
the cursor keeps the calls apart.

NEUTER anchors (verified to bite; a mutation that does NOT bite is a bad anchor,
not evidence of redundancy):
  * name-as-identifier → rewrite the ``if not _prev_name:`` assignment back to
    ``name += _incoming`` (bites 4).
  * index→slot map → replace the ``self._tc_index_map.get(...)`` lookup with the
    raw upstream index (bites 3, all argument-routing assertions).
  * unindexed cursor → the anchor MUST be the ``else`` BRANCH BODY, rewritten to
    resolve through the map (e.g. ``_upstream_idx = 0; idx =
    self._tc_index_map.get(0, 0)``). Editing only the ``if 'index' in tc``
    CONDITION does not bite: the branch body still runs and the cursor still
    allocates, so nothing was actually neutered.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm._sse_core import SSEAccumulator  # noqa: E402
from lib.llm.diagnostics import RawSSEDumper  # noqa: E402


def _acc(model='yuju-claude-opus-5-evaDaily'):
    body = {'model': model, 'messages': [], 'tools': []}
    return SSEAccumulator(body, 'trace-test', RawSSEDumper(model, 'trace-test', body),
                          None, 0.0, log_prefix='[test]')


def _names(acc):
    return [tc['function']['name'] for tc in acc.tool_calls_acc.values()]


class ToolNameSlotCollisionTest(unittest.TestCase):
    """Two distinct calls must never merge into one concatenated name."""

    def test_two_calls_sharing_index_zero_stay_separate(self):
        """Upstream labels BOTH calls index 0 — they must not fuse."""
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'id': 'tc_1', 'index': 0, 'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"a"}'}}]})
        acc._handle_delta({'tool_calls': [{'id': 'tc_2', 'index': 0, 'type': 'function',
                                           'function': {'name': 'run_command', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"command":"ls"}'}}]})
        got = _names(acc)
        self.assertNotIn('read_filesrun_command', got,
                         'names were concatenated — the += bug is back')
        self.assertEqual(['read_files', 'run_command'], got)
        # Routing the NAME alone is not enough: every later delta bearing the
        # reused upstream index — arguments included — must follow the call it
        # belongs to. Getting this wrong is WORSE than the fused name, because
        # both calls stay dispatchable while carrying wrong/empty arguments.
        args = [tc['function']['arguments'] for tc in acc.tool_calls_acc.values()]
        self.assertEqual(['{"path":"a"}', '{"command":"ls"}'], args,
                         'argument deltas were mis-routed across the slot split')
        ids = [tc['id'] for tc in acc.tool_calls_acc.values()]
        self.assertEqual(['tc_1', 'tc_2'], ids)

    def test_three_calls_sharing_index_zero_stay_separate(self):
        """Three calls fused into one upstream index — names AND args each own."""
        acc = _acc()
        for tid, nm, ar in (('tc_1', 'read_files', '{"path":"a"}'),
                            ('tc_2', 'run_command', '{"command":"ls"}'),
                            ('tc_3', 'grep_search', '{"pattern":"z"}')):
            acc._handle_delta({'tool_calls': [{'id': tid, 'index': 0, 'type': 'function',
                                               'function': {'name': nm, 'arguments': ''}}]})
            acc._handle_delta({'tool_calls': [{'index': 0,
                                               'function': {'arguments': ar}}]})
        self.assertEqual(['read_files', 'run_command', 'grep_search'], _names(acc))
        args = [tc['function']['arguments'] for tc in acc.tool_calls_acc.values()]
        self.assertEqual(['{"path":"a"}', '{"command":"ls"}', '{"pattern":"z"}'], args)

    def test_two_calls_without_index_stay_separate(self):
        """No ``index`` field at all — must not all collapse into slot 0."""
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'id': 'tc_1', 'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'function': {'arguments': '{"path":"a"}'}}]})
        acc._handle_delta({'tool_calls': [{'id': 'tc_2', 'type': 'function',
                                           'function': {'name': 'grep_search', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'function': {'arguments': '{"pattern":"x"}'}}]})
        got = _names(acc)
        self.assertNotIn('read_filesgrep_search', got,
                         'names were concatenated — the += bug is back')
        self.assertEqual(['read_files', 'grep_search'], got)

    def test_unindexed_calls_without_ids_stay_separate(self):
        """No ``index`` AND no ``id`` — slot assignment is the only defence.

        This is what isolates the ``tc.get('index', 0)`` half of the fix: with
        ids present, the name-conflict branch would rescue the call anyway, so
        that test alone cannot tell whether slot allocation works. Here nothing
        but sequential slot assignment can keep the two calls apart.
        """
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'function': {'arguments': '{"path":"a"}'}}]})
        acc._handle_delta({'tool_calls': [{'type': 'function',
                                           'function': {'name': 'run_command', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'function': {'arguments': '{"command":"ls"}'}}]})
        got = _names(acc)
        self.assertNotIn('read_filesrun_command', got,
                         'unindexed deltas collapsed into slot 0')
        self.assertEqual(['read_files', 'run_command'], got)
        args = [tc['function']['arguments'] for tc in acc.tool_calls_acc.values()]
        self.assertEqual(['{"path":"a"}', '{"command":"ls"}'], args,
                         'arguments were routed to the wrong slot')

    def test_same_name_twice_stays_two_calls(self):
        """Same tool called twice — the dominant production shape (10/11)."""
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'id': 'tc_1', 'index': 0, 'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"a"}'}}]})
        acc._handle_delta({'tool_calls': [{'id': 'tc_2', 'index': 0, 'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"b"}'}}]})
        got = _names(acc)
        self.assertNotIn('read_filesread_files', got,
                         'names were concatenated — the += bug is back')
        self.assertEqual(['read_files', 'read_files'], got)
        args = [tc['function']['arguments'] for tc in acc.tool_calls_acc.values()]
        self.assertEqual(['{"path":"a"}', '{"path":"b"}'], args,
                         'argument deltas were mis-routed across the slot split')

    def test_upstream_reissues_full_name_is_idempotent(self):
        """Non-incremental upstream re-sends the SAME full name: no dup, no fuse."""
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'id': 'tc_1', 'index': 0, 'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0, 'type': 'function',
                                           'function': {'name': 'read_files'}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"a"}'}}]})
        self.assertEqual(['read_files'], _names(acc))

    def test_healthy_two_calls_unchanged(self):
        """Control: properly indexed calls keep their existing behaviour."""
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'id': 'tc_1', 'index': 0, 'type': 'function',
                                           'function': {'name': 'read_files', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"a"}'}}]})
        acc._handle_delta({'tool_calls': [{'id': 'tc_2', 'index': 1, 'type': 'function',
                                           'function': {'name': 'run_command', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 1,
                                           'function': {'arguments': '{"command":"ls"}'}}]})
        self.assertEqual(['read_files', 'run_command'], _names(acc))
        args = [tc['function']['arguments'] for tc in acc.tool_calls_acc.values()]
        self.assertEqual(['{"path":"a"}', '{"command":"ls"}'], args)

    def test_streamed_name_fragments_still_assemble(self):
        """A name split across frames within ONE call must still assemble.

        Guards against over-correcting: if some upstream ever streams the name
        in pieces, the fix must not shred it into bogus extra slots. The
        distinguishing signal is that a fragment continues the current name
        (prefix relationship), not that it restates a complete one.
        """
        acc = _acc()
        acc._handle_delta({'tool_calls': [{'id': 'tc_1', 'index': 0, 'type': 'function',
                                           'function': {'name': 'read_', 'arguments': ''}}]})
        acc._handle_delta({'tool_calls': [{'index': 0, 'function': {'name': 'files'}}]})
        acc._handle_delta({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"a"}'}}]})
        self.assertEqual(['read_files'], _names(acc))


class ConcatenatedNameSegmentationTest(unittest.TestCase):
    """The five production names must be recognizable as our own concatenation."""

    NAMES = [
        ('read_filesrun_command', ['read_files', 'run_command']),
        ('read_filesread_files', ['read_files', 'read_files']),
        ('run_commandrun_command', ['run_command', 'run_command']),
        ('grep_searchgrep_search', ['grep_search', 'grep_search']),
        ('read_filesgrep_search', ['read_files', 'grep_search']),
    ]
    # Genuine model inventions seen in the same logs — must NOT be mistaken
    # for our concatenation, or the detector would blame us for real
    # hallucinations.
    NON_CONCAT = ['module_buffer_manager', 'phantomzz', 'totally_made_up_xyz',
                  'read_files', 'run_command']

    def test_production_names_split_uniquely(self):
        from lib.tool_input_repair._concat import split_concatenated_tool_name
        from lib.tool_input_repair._schema import _schemas
        known = set(_schemas().keys())
        for name, expected in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(expected, split_concatenated_tool_name(name, known))

    def test_real_inventions_do_not_split(self):
        from lib.tool_input_repair._concat import split_concatenated_tool_name
        from lib.tool_input_repair._schema import _schemas
        known = set(_schemas().keys())
        for name in self.NON_CONCAT:
            with self.subTest(name=name):
                self.assertIsNone(split_concatenated_tool_name(name, known))

    def test_ambiguous_split_is_refused(self):
        """More than one valid segmentation ⇒ inconclusive, refuse to guess."""
        from lib.tool_input_repair._concat import split_concatenated_tool_name
        # 'ab' + 'c' and 'a' + 'bc' both reconstruct 'abc'.
        known = {'a', 'b', 'c', 'ab', 'bc'}
        self.assertIsNone(split_concatenated_tool_name('abc', known))


class ConcatenatedNameIngestionTest(unittest.TestCase):
    """Drive the real ingestion seam, not just the splitter underneath it.

    The segmentation tests above call ``split_concatenated_tool_name``
    directly, so every one of them stayed green while the branch that
    CONSUMES it raised ``NameError: audit_log is not defined`` on the first
    real concatenated name. Testing a helper is not testing its call site.
    """

    def _known(self):
        from lib.tool_input_repair._schema import _schemas
        return set(_schemas().keys())

    def test_concatenated_name_is_rejected_without_raising(self):
        from lib.tool_input_repair._ingest import ingest_tool_call
        known = self._known()
        result = ingest_tool_call(
            {'function': {'name': 'read_filesrun_command', 'arguments': '{}'},
             'id': 'tc_concat'},
            known_tools=known,
        )
        self.assertIsNotNone(result.rejection)
        self.assertEqual('concatenated', result.rejection.get('kind'))
        self.assertEqual(['read_files', 'run_command'],
                         result.rejection.get('suggestions'))

    def test_audit_emission_is_the_default_path(self):
        """``emit_audit`` defaults True, so the audit branch is what production
        actually runs — the NameError needed no special configuration."""
        import inspect

        from lib.tool_input_repair._ingest import ingest_tool_call
        default = inspect.signature(ingest_tool_call).parameters['emit_audit'].default
        self.assertTrue(default)

    def test_every_production_concat_name_survives_ingestion(self):
        from lib.tool_input_repair._ingest import ingest_tool_call
        known = self._known()
        for name, parts in ConcatenatedNameSegmentationTest.NAMES:
            with self.subTest(name=name):
                result = ingest_tool_call(
                    {'function': {'name': name, 'arguments': '{}'}, 'id': 'tc'},
                    known_tools=known,
                )
                self.assertIsNotNone(result.rejection)
                self.assertEqual(parts, result.rejection.get('suggestions'))


if __name__ == '__main__':
    unittest.main()
