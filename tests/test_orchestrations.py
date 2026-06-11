"""tests/test_orchestrations.py — orchestration validator + REST CRUD.

Covers:
  * lib.orchestration.validate_definition — pure validation rules.
  * /api/v1/orchestrations CRUD + /validate — end-to-end through a Quart
    test client, mirroring the fixture style of test_api_v1_integration.

The store path (data/config/orchestrations.json) is redirected to a
tmp file so the real config is never touched.
"""

import asyncio
import os
import re
import sys
import tempfile
import unittest


# ── Pure validator tests (no app needed) ────────────────────────────

class ValidatorTest(unittest.TestCase):
    def _v(self, d):
        from lib.orchestration import validate_definition
        return validate_definition(d)

    def _endpoint_def(self):
        return {
            'schema': 'tofu.orchestration/v1',
            'name': 'Endpoint Loop',
            'nodes': [
                {'id': 's1', 'type': 'control', 'kind': 'start'},
                {'id': 'p1', 'type': 'role', 'role': 'planner'},
                {'id': 'l1', 'type': 'control', 'kind': 'loop',
                 'params': {'max_iterations': 10}},
                {'id': 'w1', 'type': 'role', 'role': 'worker',
                 'params': {'tier': 'heavy', 'isolation': 'shared-context'}},
                {'id': 'c1', 'type': 'role', 'role': 'critic'},
                {'id': 'e1', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [
                {'from': 's1', 'to': 'p1'}, {'from': 'p1', 'to': 'l1'},
                {'from': 'l1', 'to': 'w1'}, {'from': 'w1', 'to': 'c1'},
                {'from': 'c1', 'to': 'l1'}, {'from': 'l1', 'to': 'e1'},
            ],
        }

    def test_valid_endpoint_passes_clean(self):
        v = self._v(self._endpoint_def())
        self.assertTrue(v['ok'], v['errors'])
        self.assertEqual(v['errors'], [])
        self.assertEqual(v['warnings'], [])

    def test_missing_name_is_error(self):
        d = self._endpoint_def(); d['name'] = ''
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('name' in e for e in v['errors']))

    def test_duplicate_id_is_error(self):
        d = self._endpoint_def()
        d['nodes'].append({'id': 's1', 'type': 'control', 'kind': 'barrier'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('duplicate' in e for e in v['errors']))

    def test_two_start_nodes_is_error(self):
        d = self._endpoint_def()
        d['nodes'].append({'id': 's2', 'type': 'control', 'kind': 'start'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('start' in e for e in v['errors']))

    def test_edge_to_unknown_node_is_error(self):
        d = self._endpoint_def()
        d['edges'].append({'from': 's1', 'to': 'ghost'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('ghost' in e for e in v['errors']))

    def test_bad_tier_and_isolation_are_errors(self):
        d = self._endpoint_def()
        d['nodes'][3]['params'] = {'tier': 'ultra', 'isolation': 'weird'}
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('tier' in e for e in v['errors']))
        self.assertTrue(any('isolation' in e for e in v['errors']))

    def test_unknown_role_is_warning_not_error(self):
        d = self._endpoint_def()
        d['nodes'][1]['role'] = 'wizard'
        v = self._v(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertTrue(any('wizard' in w for w in v['warnings']))

    def test_edge_into_start_rejected(self):
        d = self._endpoint_def()
        d['edges'].append({'from': 'w1', 'to': 's1'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('start' in e for e in v['errors']))

    def test_stop_has_no_output(self):
        d = self._endpoint_def()
        d['edges'].append({'from': 'e1', 'to': 'w1'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('stop' in e for e in v['errors']))

    def test_human_node_valid_modes(self):
        for mode in ('approve', 'input', 'notify'):
            d = self._endpoint_def()
            # splice a human gate between planner and loop
            d['nodes'].append({'id': 'h1', 'type': 'control', 'kind': 'human',
                               'params': {'mode': mode, 'prompt': 'ok?'}})
            d['edges'] = [e for e in d['edges'] if e != {'from': 'p1', 'to': 'l1'}]
            d['edges'] += [{'from': 'p1', 'to': 'h1'}, {'from': 'h1', 'to': 'l1'}]
            v = self._v(d)
            self.assertTrue(v['ok'], (mode, v['errors']))

    def test_human_invalid_mode_is_error(self):
        d = self._endpoint_def()
        d['nodes'].append({'id': 'h1', 'type': 'control', 'kind': 'human',
                           'params': {'mode': 'teleport'}})
        d['edges'].append({'from': 'p1', 'to': 'h1'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('human mode' in e for e in v['errors']))


# ── REST CRUD tests (Quart test client) ─────────────────────────────

class _AppFixture:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Patch api_keys store before auth loads.
        from lib import api_keys
        self._orig_keys = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(self._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False

        # Redirect the orchestrations store to a tmp file.
        import routes.api_v1.orchestrations as orch_mod
        self._orig_orch_path = orch_mod._ORCH_PATH
        orch_mod._ORCH_PATH = os.path.join(self._tmp.name, 'orchestrations.json')

        # Install the flask→quart shim the same way the server does
        # (server._install_flask_shim handles the sync get_json wrapper +
        # Quart config defaults that a bare inline copy misses).
        import server  # noqa: F401  — import side-effect installs the shim

        os.environ['TUNNEL_TOKEN'] = 'test-tunnel-token-not-real'

        from quart import Quart
        self.app = Quart(__name__)
        self.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        self.app.before_request(bearer_auth_before_request)
        self.app.after_request(attach_rate_headers)
        self.app.register_blueprint(orch_mod.api_v1_orchestrations_bp)

    def cleanup(self):
        from lib import api_keys
        import routes.api_v1.orchestrations as orch_mod
        api_keys._STORE_PATH = self._orig_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        orch_mod._ORCH_PATH = self._orig_orch_path
        self._tmp.cleanup()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class CrudTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fix = _AppFixture()
        from lib.api_keys import create_key
        _row, cls.token = create_key(name='orch-test', scopes=[], admin=True)

    @classmethod
    def tearDownClass(cls):
        cls.fix.cleanup()

    def _cli(self):
        return self.fix.app.test_client()

    def _hdr(self):
        return {'Authorization': f'Bearer {self.token}'}

    def _def(self, name='Flow A'):
        return {
            'schema': 'tofu.orchestration/v1', 'name': name,
            'nodes': [
                {'id': 's1', 'type': 'control', 'kind': 'start'},
                {'id': 'w1', 'type': 'role', 'role': 'worker',
                 'params': {'tier': 'heavy', 'isolation': 'shared-context'}},
                {'id': 'e1', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 's1', 'to': 'w1'}, {'from': 'w1', 'to': 'e1'}],
        }

    def test_requires_auth(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_validate_endpoint(self):
        async def go():
            cli = self._cli()
            r = await cli.post('/api/v1/orchestrations/validate',
                               headers=self._hdr(), json=self._def())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])

            bad = self._def(); bad['name'] = ''
            r = await cli.post('/api/v1/orchestrations/validate',
                               headers=self._hdr(), json=bad)
            body = await r.get_json()
            self.assertFalse(body['ok'])
        _run(go())

    def test_full_crud_cycle(self):
        async def go():
            cli = self._cli()
            # Create
            r = await cli.post('/api/v1/orchestrations',
                               headers=self._hdr(), json=self._def('CycleFlow'))
            self.assertEqual(r.status_code, 201)
            created = await r.get_json()
            oid = created['id']
            self.assertEqual(created['name'], 'CycleFlow')

            # List
            r = await cli.get('/api/v1/orchestrations', headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            lst = await r.get_json()
            self.assertTrue(any(e['id'] == oid for e in lst))

            # Get
            r = await cli.get(f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            got = await r.get_json()
            self.assertEqual(len(got['definition']['nodes']), 3)

            # Update (rename)
            upd = self._def('CycleFlow v2')
            r = await cli.put(f'/api/v1/orchestrations/{oid}',
                              headers=self._hdr(), json=upd)
            self.assertEqual(r.status_code, 200)
            updated = await r.get_json()
            self.assertEqual(updated['name'], 'CycleFlow v2')

            # Delete
            r = await cli.delete(f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            r = await cli.get(f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_compose_empty_requirement_is_400(self):
        async def go():
            r = await self._cli().post('/api/v1/orchestrations/compose',
                                       headers=self._hdr(), json={'requirement': '  '})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_builtin_endpoint(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations/builtin/endpoint',
                                      headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            ids = [n['id'] for n in body['definition']['nodes']]
            self.assertIn('worker', ids)
            self.assertIn('critic', ids)
        _run(go())

    def test_builtin_unknown_is_404(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations/builtin/nope',
                                      headers=self._hdr())
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_plan_returns_steps(self):
        async def go():
            r = await self._cli().post('/api/v1/orchestrations/plan',
                                       headers=self._hdr(),
                                       json={'definition': self._def()})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            actions = [s['action'] for s in body['steps']]
            self.assertIn('run-agent', actions)
        _run(go())

    def test_run_invalid_definition_is_400(self):
        async def go():
            bad = self._def(); bad['name'] = ''
            r = await self._cli().post('/api/v1/orchestrations/run',
                                       headers=self._hdr(), json={'definition': bad})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_create_rejects_invalid(self):
        async def go():
            bad = self._def(); bad['nodes'].append(
                {'id': 's1', 'type': 'control', 'kind': 'start'})  # dup id + 2 starts
            r = await self._cli().post('/api/v1/orchestrations',
                                       headers=self._hdr(), json=bad)
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertFalse(body['ok'])
            self.assertTrue(body.get('errors'))
        _run(go())


class ComposerTest(unittest.TestCase):
    def _endpoint_payload(self):
        import json
        g = {'reply': 'Built an endpoint loop.', 'definition': {
            'name': 'EP', 'nodes': [
                {'id': 's1', 'type': 'control', 'kind': 'start'},
                {'id': 'p1', 'type': 'role', 'role': 'planner'},
                {'id': 'l1', 'type': 'control', 'kind': 'loop', 'params': {'max_iterations': 8}},
                {'id': 'w1', 'type': 'role', 'role': 'worker', 'params': {'isolation': 'shared-context'}},
                {'id': 'c1', 'type': 'role', 'role': 'critic'},
                {'id': 'e1', 'type': 'control', 'kind': 'stop'}],
            'edges': [{'from': 's1', 'to': 'p1'}, {'from': 'p1', 'to': 'l1'},
                      {'from': 'l1', 'to': 'w1'}, {'from': 'w1', 'to': 'c1'},
                      {'from': 'c1', 'to': 'l1'}, {'from': 'l1', 'to': 'e1'}]}}
        return '```json\n' + json.dumps(g) + '\n```'

    def test_compose_parses_fenced_json_and_lays_out(self):
        from lib.orchestration_composer import compose
        payload = self._endpoint_payload()
        r = compose('endpoint loop', llm_override=lambda m: (payload, {}))
        self.assertTrue(r['ok'], r.get('error'))
        self.assertEqual(r['definition']['schema'], 'tofu.orchestration/v1')
        # backend forced layout — every node has a position
        for n in r['definition']['nodes']:
            self.assertIn('pos', n)
        # loop back-edge must not inflate layers: stop is shallow, not deepest
        ys = {n['id']: n['pos']['y'] for n in r['definition']['nodes']}
        self.assertLess(ys['e1'], ys['c1'])

    def test_compose_rejects_invalid_graph(self):
        from lib.orchestration_composer import compose
        import json
        bad = json.dumps({'reply': 'x', 'definition': {
            'name': 'B', 'nodes': [
                {'id': 's1', 'type': 'control', 'kind': 'start'},
                {'id': 's2', 'type': 'control', 'kind': 'start'}], 'edges': []}})
        r = compose('x', llm_override=lambda m: (bad, {}))
        self.assertFalse(r['ok'])
        self.assertTrue(r['validation']['errors'])

    def test_compose_handles_non_json(self):
        from lib.orchestration_composer import compose
        r = compose('x', llm_override=lambda m: ('sorry, I cannot', {}))
        self.assertFalse(r['ok'])
        self.assertIsNone(r['definition'])

    def test_compose_empty_requirement(self):
        from lib.orchestration_composer import compose
        r = compose('   ', llm_override=lambda m: ('{}', {}))
        self.assertFalse(r['ok'])


class LayoutTest(unittest.TestCase):
    def test_indeg0_orphan_is_a_source(self):
        # A node with no incoming edge is a valid source → layer 0.
        from lib.orchestration import layout_definition
        d = {'nodes': [
            {'id': 's1', 'type': 'control', 'kind': 'start'},
            {'id': 'w1', 'type': 'role', 'role': 'worker'},
            {'id': 'orphan', 'type': 'role', 'role': 'writer'}],
            'edges': [{'from': 's1', 'to': 'w1'}]}
        layout_definition(d)
        ys = {n['id']: n['pos']['y'] for n in d['nodes']}
        self.assertEqual(ys['orphan'], ys['s1'])  # both sources at layer 0

    def test_unreachable_cycle_placed_last(self):
        # a→b→a is a disconnected cycle (both indeg>0, neither a source).
        from lib.orchestration import layout_definition
        d = {'nodes': [
            {'id': 's1', 'type': 'control', 'kind': 'start'},
            {'id': 'w1', 'type': 'role', 'role': 'worker'},
            {'id': 'a', 'type': 'role', 'role': 'coder'},
            {'id': 'b', 'type': 'role', 'role': 'analyst'}],
            'edges': [{'from': 's1', 'to': 'w1'},
                      {'from': 'a', 'to': 'b'}, {'from': 'b', 'to': 'a'}]}
        layout_definition(d)
        ys = {n['id']: n['pos']['y'] for n in d['nodes']}
        self.assertGreater(ys['a'], ys['w1'])
        self.assertGreater(ys['b'], ys['w1'])


class TemplateBakedCoordsTest(unittest.TestCase):
    """Guard against drift between the template coords baked into
    ``static/js/orchestration.js`` and what ``layout_definition`` produces.

    The templates ship FINAL layout coordinates so the studio renders them
    on the first paint with no backend round-trip (no flash). That only
    holds if the baked coords stay equal to the layout engine's output.
    This test parses the real coords + topology out of the JS source (so
    there is no second hand-maintained copy) and re-derives the layout,
    asserting an exact match for every node in every template.
    """

    _JS_PATH = os.path.join(os.path.dirname(__file__), '..',
                            'static', 'js', 'orchestration.js')
    _TEMPLATES = ('endpoint', 'fanout', 'adversarial')

    # var <name> = mk({ ptype: 'role', role: 'planner' }, 155, 180, ...);
    _MK_RE = re.compile(
        r"var\s+(\w+)\s*=\s*mk\(\{([^}]*)\},\s*(-?\d+),\s*(-?\d+)")
    _LINK_RE = re.compile(r"link\((\w+),\s*(\w+)\)")
    _PTYPE_RE = re.compile(r"ptype:\s*'(\w+)'")
    _ROLE_RE = re.compile(r"role:\s*'([\w-]+)'")
    _KIND_RE = re.compile(r"kind:\s*'([\w-]+)'")

    @classmethod
    def _load_template_function(cls):
        with open(cls._JS_PATH, encoding='utf-8') as f:
            src = f.read()
        start = src.index('function _orchLoadTemplate')
        # The function ends at the first column-0 close brace after start.
        end = src.index('\n}\n', start)
        return src[start:end]

    @classmethod
    def _parse_template(cls, body, which):
        """Return (nodes, edges) for one ``which === '<which>'`` block.

        Nodes are returned in declaration order (== insertion order the
        layout engine relies on for seed selection + within-layer order),
        each as ``{id, type, role, kind, baked_x, baked_y}``.
        """
        marker = f"which === '{which}'"
        seg_start = body.index(marker)
        # Block runs until the next ``which === '...'`` or end of function.
        nxt = body.find("which === '", seg_start + len(marker))
        segment = body[seg_start: nxt if nxt != -1 else len(body)]

        nodes = []
        for m in cls._MK_RE.finditer(segment):
            var, payload, x, y = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
            ptype = cls._PTYPE_RE.search(payload)
            role = cls._ROLE_RE.search(payload)
            kind = cls._KIND_RE.search(payload)
            nodes.append({
                'id': var,
                'type': ptype.group(1) if ptype else '',
                'role': role.group(1) if role else '',
                'kind': kind.group(1) if kind else '',
                'baked_x': x, 'baked_y': y,
            })
        edges = [{'from': a, 'to': b} for a, b in cls._LINK_RE.findall(segment)]
        return nodes, edges

    def test_each_template_matches_layout_engine(self):
        from lib.orchestration import layout_definition
        body = self._load_template_function()
        for which in self._TEMPLATES:
            with self.subTest(template=which):
                nodes, edges = self._parse_template(body, which)
                self.assertTrue(nodes, f'no nodes parsed for {which!r}')
                self.assertTrue(edges, f'no edges parsed for {which!r}')
                # Build a definition with NO positions, then lay it out.
                defn = {
                    'schema': 'tofu.orchestration/v1', 'name': which,
                    'nodes': [{'id': n['id'], 'type': n['type'],
                               'role': n['role'], 'kind': n['kind']}
                              for n in nodes],
                    'edges': edges,
                }
                layout_definition(defn)
                computed = {n['id']: n['pos'] for n in defn['nodes']}
                for n in nodes:
                    pos = computed[n['id']]
                    self.assertEqual(
                        (pos['x'], pos['y']), (n['baked_x'], n['baked_y']),
                        f"template {which!r} node {n['id']!r}: baked "
                        f"({n['baked_x']},{n['baked_y']}) != layout "
                        f"({pos['x']},{pos['y']}) — re-run layout_definition "
                        f"and update the baked coords in orchestration.js")


if __name__ == '__main__':
    unittest.main()
