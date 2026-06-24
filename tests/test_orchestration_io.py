"""tests/test_orchestration_io.py — typed node I/O contract.

Covers the dataflow axis added on top of the role/emits axes:
  * lib.orchestration — validate_definition rules for params.io, plus the
    node_output_names / parse_io_ref pure helpers.
  * lib.orchestration_engine — FlowExecutor honoring io.inputs (a node reads
    ONLY its wired producer outputs, not the whole scratchpad) and io.outputs
    (a tool-heavy worker exposing a synthesized 'changes' manifest).

A mock agent_runner keeps it LLM-free: it echoes the context it received so a
test can assert exactly what a downstream node saw.
"""

import threading
import unittest

from lib.orchestration import (
    DEFAULT_OUTPUT_NAME, node_output_names, parse_io_ref, validate_definition,
)
from lib.orchestration_engine import FlowExecutor


def _role(nid, role, **params):
    return {'id': nid, 'type': 'role', 'role': role, 'params': params}


def _ctrl(nid, kind, **params):
    return {'id': nid, 'type': 'control', 'kind': kind, 'params': params}


# ── Pure helpers ────────────────────────────────────────────────────

class IoHelpersTest(unittest.TestCase):
    def test_default_output_name_when_no_io(self):
        self.assertEqual(node_output_names(_role('w', 'worker')),
                         [DEFAULT_OUTPUT_NAME])

    def test_declared_output_names(self):
        n = _role('w', 'worker', io={'outputs': [
            {'name': 'summary', 'type': 'text'},
            {'name': 'changes', 'type': 'artifact'}]})
        self.assertEqual(node_output_names(n), ['summary', 'changes'])

    def test_empty_outputs_fall_back_to_default(self):
        n = _role('w', 'worker', io={'outputs': []})
        self.assertEqual(node_output_names(n), [DEFAULT_OUTPUT_NAME])

    def test_parse_io_ref(self):
        self.assertEqual(parse_io_ref('planner'), ('planner', None))
        self.assertEqual(parse_io_ref('worker.changes'), ('worker', 'changes'))
        self.assertEqual(parse_io_ref('start'), ('start', None))
        self.assertEqual(parse_io_ref('  w.out  '), ('w', 'out'))


# ── Validator ───────────────────────────────────────────────────────

class IoValidatorTest(unittest.TestCase):
    def _base(self, w_params):
        return {
            'schema': 'tofu.orchestration/v1', 'name': 'IO',
            'nodes': [
                _ctrl('s1', 'start'),
                _role('p1', 'planner'),
                _role('w1', 'worker', **w_params),
                _ctrl('e1', 'stop'),
            ],
            'edges': [{'from': 's1', 'to': 'p1'}, {'from': 'p1', 'to': 'w1'},
                      {'from': 'w1', 'to': 'e1'}],
        }

    def test_valid_io_passes_clean(self):
        d = self._base({'io': {
            'inputs': [{'name': 'brief', 'type': 'text', 'from': 'p1'}],
            'outputs': [{'name': 'summary', 'type': 'text'},
                        {'name': 'changes', 'type': 'artifact'}]}})
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertEqual(v['errors'], [])

    def test_io_must_be_object(self):
        v = validate_definition(self._base({'io': 'nope'}))
        self.assertFalse(v['ok'])
        self.assertTrue(any('io must be an object' in e for e in v['errors']))

    def test_invalid_port_type_is_error(self):
        d = self._base({'io': {'outputs': [{'name': 'x', 'type': 'blob'}]}})
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('invalid type' in e for e in v['errors']))

    def test_duplicate_port_name_is_error(self):
        d = self._base({'io': {'outputs': [
            {'name': 'x', 'type': 'text'}, {'name': 'x', 'type': 'json'}]}})
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('duplicate port name' in e for e in v['errors']))

    def test_input_from_unknown_node_is_error(self):
        d = self._base({'io': {'inputs': [
            {'name': 'b', 'type': 'text', 'from': 'ghost'}]}})
        v = validate_definition(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('unknown node' in e for e in v['errors']))

    def test_input_from_start_is_valid(self):
        d = self._base({'io': {'inputs': [
            {'name': 'b', 'type': 'text', 'from': 'start'}]}})
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])

    def test_input_from_unknown_output_name_is_warning(self):
        # planner exposes only the implicit 'text' output; referencing
        # 'p1.headline' is a soft warning, not a hard error.
        d = self._base({'io': {'inputs': [
            {'name': 'b', 'type': 'text', 'from': 'p1.headline'}]}})
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertTrue(any('headline' in w for w in v['warnings']))

    def test_named_output_ref_resolves_clean(self):
        d = {
            'schema': 'tofu.orchestration/v1', 'name': 'IO',
            'nodes': [
                _ctrl('s1', 'start'),
                _role('w1', 'worker', io={'outputs': [
                    {'name': 'summary', 'type': 'text'},
                    {'name': 'changes', 'type': 'artifact'}]}),
                _role('w2', 'writer', io={'inputs': [
                    {'name': 'src', 'type': 'artifact', 'from': 'w1.changes'}]}),
                _ctrl('e1', 'stop'),
            ],
            'edges': [{'from': 's1', 'to': 'w1'}, {'from': 'w1', 'to': 'w2'},
                      {'from': 'w2', 'to': 'e1'}],
        }
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertEqual(v['warnings'], [])

    def test_no_io_block_is_byte_identical_clean(self):
        # The canonical no-io endpoint def must still validate with zero
        # warnings (guards the back-compat invariant).
        d = self._base({})
        v = validate_definition(d)
        self.assertTrue(v['ok'], v['errors'])


# ── Engine dataflow ─────────────────────────────────────────────────

class _EchoRunner:
    """Echoes the context each node received + optional scripted tools."""
    def __init__(self, tools=None):
        self.tools = tools or {}
        self.seen = {}
        self.lock = threading.Lock()

    def __call__(self, node, context, iteration):
        nid = node['id']
        with self.lock:
            self.seen[nid] = context
        res = {'output': f'{nid}-out', 'status': 'completed', 'error': ''}
        if nid in self.tools:
            res['tool_names'] = self.tools[nid]
        return res


class IoEngineTest(unittest.TestCase):
    def _run(self, defn, runner, initial=''):
        ex = FlowExecutor(defn, agent_runner=runner)
        return ex.run(initial_context=initial)

    def test_typed_input_isolates_from_scratchpad(self):
        # b1 declares io.inputs=[from a1]; it must see ONLY a1's output,
        # NOT the start seed / planner blob that the legacy scratchpad holds.
        defn = {
            'schema': 'tofu.orchestration/v1', 'name': 'IO',
            'nodes': [
                _ctrl('s1', 'start', seed='SEED'),
                _role('a1', 'researcher'),
                _role('b1', 'writer', io={'inputs': [
                    {'name': 'findings', 'type': 'text', 'from': 'a1'}]}),
                _ctrl('e1', 'stop'),
            ],
            'edges': [{'from': 's1', 'to': 'a1'}, {'from': 'a1', 'to': 'b1'},
                      {'from': 'b1', 'to': 'e1'}],
        }
        runner = _EchoRunner()
        self._run(defn, runner, initial='SEED')
        b_ctx = runner.seen['b1']
        self.assertIn('## findings', b_ctx)
        self.assertIn('a1-out', b_ctx)
        self.assertNotIn('SEED', b_ctx)        # scratchpad bypassed
        self.assertNotIn('[researcher]', b_ctx)  # no role-labelled blob

    def test_input_from_start_seed(self):
        defn = {
            'schema': 'tofu.orchestration/v1', 'name': 'IO',
            'nodes': [
                _ctrl('s1', 'start', seed='THE-REQUEST'),
                _role('a1', 'coder', io={'inputs': [
                    {'name': 'req', 'type': 'text', 'from': 'start'}]}),
                _ctrl('e1', 'stop'),
            ],
            'edges': [{'from': 's1', 'to': 'a1'}, {'from': 'a1', 'to': 'e1'}],
        }
        runner = _EchoRunner()
        self._run(defn, runner, initial='THE-REQUEST')
        self.assertIn('## req', runner.seen['a1'])
        self.assertIn('THE-REQUEST', runner.seen['a1'])

    def test_named_artifact_output_carries_change_manifest(self):
        # w1 declares a 'changes' artifact output; w2 wires to w1.changes and
        # must receive the synthesized manifest listing w1's state-changing
        # tool calls — NOT w1's prose.
        defn = {
            'schema': 'tofu.orchestration/v1', 'name': 'IO',
            'nodes': [
                _ctrl('s1', 'start'),
                _role('w1', 'worker', io={'outputs': [
                    {'name': 'summary', 'type': 'text'},
                    {'name': 'changes', 'type': 'artifact'}]}),
                _role('w2', 'writer', io={'inputs': [
                    {'name': 'manifest', 'type': 'artifact', 'from': 'w1.changes'}]}),
                _ctrl('e1', 'stop'),
            ],
            'edges': [{'from': 's1', 'to': 'w1'}, {'from': 'w1', 'to': 'w2'},
                      {'from': 'w2', 'to': 'e1'}],
        }
        runner = _EchoRunner(tools={'w1': [
            'write_file', 'write_file', 'apply_diff', 'read_files']})
        self._run(defn, runner)
        ctx = runner.seen['w2']
        self.assertIn('## manifest', ctx)
        self.assertIn('Change manifest', ctx)
        self.assertIn('write_file ×2', ctx)
        self.assertIn('apply_diff', ctx)
        # The exploratory read_files is counted but not listed as a change.
        self.assertNotIn('read_files', ctx)

    def test_legacy_flow_without_io_uses_scratchpad(self):
        # No io anywhere → the downstream node still sees the accumulating
        # role-labelled scratchpad (back-compat).
        defn = {
            'schema': 'tofu.orchestration/v1', 'name': 'Legacy',
            'nodes': [
                _ctrl('s1', 'start', seed='SEED'),
                _role('a1', 'researcher'),
                _role('b1', 'writer'),
                _ctrl('e1', 'stop'),
            ],
            'edges': [{'from': 's1', 'to': 'a1'}, {'from': 'a1', 'to': 'b1'},
                      {'from': 'b1', 'to': 'e1'}],
        }
        runner = _EchoRunner()
        self._run(defn, runner, initial='SEED')
        b_ctx = runner.seen['b1']
        self.assertIn('[researcher]', b_ctx)
        self.assertIn('a1-out', b_ctx)


if __name__ == '__main__':
    unittest.main()
