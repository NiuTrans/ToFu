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

import pytest


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

        # Install the flask→quart shim the same way the server does
        # (server._install_flask_shim handles the sync get_json wrapper +
        # Quart config defaults that a bare inline copy misses). This MUST
        # run before importing routes.* — routes/__init__ → routes/push.py
        # calls Blueprint.websocket(), which only exists after the shim is
        # installed. Otherwise a standalone run of this file errors at import.
        import server  # noqa: F401  — import side-effect installs the shim

        # Redirect the orchestrations store to a tmp file.
        import routes.api_v1.orchestrations as orch_mod
        self._orig_orch_path = orch_mod._ORCH_PATH
        orch_mod._ORCH_PATH = os.path.join(self._tmp.name, 'orchestrations.json')

        os.environ['TUNNEL_TOKEN'] = 'test-tunnel-token-not-real'
        # Auth mode is pinned to 'private' per-test via the ``auth_mode``
        # marker on the test classes (CrudTest / TaskRunHttpTest), honoured
        # by the conftest fixture — not mutated here (a fixture-level env
        # change wouldn't re-apply per test and would leak).

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
    # 'requires auth → 401' assertions need the gate active (private mode);
    # conftest defaults to 'open'. The per-test fixture honours this marker.
    pytestmark = pytest.mark.auth_mode('private')

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
            lst = (await r.get_json())['items']  # {items} envelope
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

    def test_role_schema_full_map(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations/role-schema',
                                      headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('critic', body['roles'])
            self.assertIn('worker', body['roles'])
            self.assertTrue(body['generic'])
            self.assertIn('select', body['kinds'])
            # Field labels are i18n keys, not user-facing strings.
            crit = body['roles']['critic']
            self.assertEqual(crit[0]['key'], 'objective')
            self.assertTrue(all(f['label'].startswith('orch.') for f in crit))
            # Read-only personas: every role carries its fixed prompt design
            # (the character's behaviour), shown but not editable in the studio.
            self.assertIn('personas', body)
            self.assertIn('worker', body['personas'])
            wp = body['personas']['worker']
            self.assertIn('prompt', wp)
            self.assertTrue(wp['prompt'])              # non-empty system prompt
            self.assertEqual(wp['tier'], 'heavy')      # mirrors registry model_hint
        _run(go())

    def test_role_schema_single_role(self):
        async def go():
            r = await self._cli().get(
                '/api/v1/orchestrations/role-schema?role=worker',
                headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertEqual(body['role'], 'worker')
            keys = [f['key'] for f in body['fields']]
            self.assertIn('must_do', keys)
            self.assertIn('must_not_do', keys)
            # Single-role responses also carry the read-only persona.
            self.assertIn('persona', body)
            self.assertTrue(body['persona']['prompt'])
        _run(go())

    def test_role_schema_unknown_role_gets_generic(self):
        async def go():
            r = await self._cli().get(
                '/api/v1/orchestrations/role-schema?role=made-up',
                headers=self._hdr())
            body = await r.get_json()
            self.assertTrue(body['ok'])
            keys = [f['key'] for f in body['fields']]
            self.assertEqual(keys[0], 'objective')  # generic fallback
        _run(go())

    def test_role_schema_requires_auth(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations/role-schema')
            self.assertEqual(r.status_code, 401)
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
    _TEMPLATES = ('endpoint', 'autopilot', 'fanout', 'adversarial')

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


# ── Durable run-instance persistence (Task Mode, Phase 2) ───────────

class RunInstanceTest(unittest.TestCase):
    """lib.orchestration_runs — durable run header + event log against a
    fresh SQLite DB. Exercises the load-bearing persistence layer directly
    (no app/HTTP), mirroring the dual-sink contract the /tasks routes rely on.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._dbpath = os.path.join(cls._tmpdir.name, 'orch_runs.db')
        os.environ['TOFU_DB_BACKEND'] = 'sqlite'
        os.environ['TOFU_DB_PATH'] = cls._dbpath
        from lib.database import init_db
        init_db()

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _defn(self):
        return {'schema': 'tofu.orchestration/v1', 'name': 'Screener',
                'nodes': [], 'edges': []}

    def test_create_get_list_and_definition_snapshot(self):
        from lib import orchestration_runs as r
        rid = r.new_run_id()
        self.assertTrue(rid.startswith('run_'))
        r.create_run(rid, definition=self._defn(), input_text='go',
                     orch_id='orch_x', name='Screener', created_by='k1')
        run = r.get_run(rid)
        self.assertEqual(run['status'], 'pending')
        self.assertEqual(run['orch_id'], 'orch_x')
        self.assertEqual(run['definition']['name'], 'Screener')  # snapshot
        self.assertEqual(run['input'], 'go')
        # list omits the definition blob (cheap listing)
        listed = [x for x in r.list_runs() if x['id'] == rid]
        self.assertEqual(len(listed), 1)
        self.assertNotIn('definition', listed[0])
        r.delete_run(rid)

    def test_event_log_cursor_replay_and_dup_seq_is_benign(self):
        from lib import orchestration_runs as r
        rid = r.new_run_id()
        r.create_run(rid, definition=self._defn())
        r.append_event(rid, 0, {'type': 'flow_start', 'nodes': 2})
        r.append_event(rid, 1, {'type': 'step_start', 'node_id': 'n1'})
        r.append_event(rid, 2, {'type': 'step_complete', 'node_id': 'n1'})
        # duplicate seq (replay race) must not raise and must not duplicate
        r.append_event(rid, 2, {'type': 'dup'})
        evs = r.get_events(rid, 0)
        self.assertEqual([e['type'] for e in evs],
                         ['flow_start', 'step_start', 'step_complete'])
        self.assertTrue(all('seq' in e for e in evs))
        # cursor replay from the middle
        self.assertEqual([e['type'] for e in r.get_events(rid, 2)],
                         ['step_complete'])
        r.delete_run(rid)

    def test_terminal_status_sets_finished_and_final(self):
        from lib import orchestration_runs as r
        rid = r.new_run_id()
        r.create_run(rid, definition=self._defn())
        r.update_status(rid, 'done', final='12 shortlisted', error=None)
        run = r.get_run(rid)
        self.assertEqual(run['status'], 'done')
        self.assertEqual(run['final'], '12 shortlisted')
        self.assertGreater(run['finished_at'], 0)
        self.assertIsNone(run['error'])
        r.delete_run(rid)

    def test_status_filter_and_delete(self):
        from lib import orchestration_runs as r
        rid = r.new_run_id()
        r.create_run(rid, definition=self._defn(), orch_id='orch_f')
        r.update_status(rid, 'error', error='boom')
        self.assertTrue(any(x['id'] == rid for x in r.list_runs(status='error')))
        self.assertFalse(any(x['id'] == rid for x in r.list_runs(status='done')))
        run = r.get_run(rid)
        self.assertEqual(run['error'], 'boom')
        self.assertTrue(r.delete_run(rid))
        self.assertIsNone(r.get_run(rid))


class TaskRunHttpTest(unittest.TestCase):
    """End-to-end through the REAL /api/v1/orchestrations/tasks routes:
    POST a flow → poll /events to completion → assert the durable event log
    and final result persisted.

    This is the load-bearing coverage for the DUAL-SINK durability claim:
    it drives the actual route handler, the real FlowExecutor, and the real
    orchestration_runs DB tables. The only thing stubbed is the LLM —
    FlowExecutor._default_runner is patched to a canned response so no
    network/model call happens (the engine, event emission, dual-sink
    on_event, and DB persistence are all exercised for real).
    """

    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        # Fresh SQLite DB BEFORE the app/runtime touch it. The worker thread
        # acquires its own thread-local connection against this global path,
        # so it must be set + initialized before any run is spawned.
        cls._dbtmp = tempfile.TemporaryDirectory()
        os.environ['TOFU_DB_BACKEND'] = 'sqlite'
        os.environ['TOFU_DB_PATH'] = os.path.join(cls._dbtmp.name, 'tasks.db')
        from lib.database import init_db
        init_db()

        cls.fix = _AppFixture()
        from lib.api_keys import create_key
        _row, cls.token = create_key(name='orch-task-test', scopes=[], admin=True)

        # Stub the LLM-backed agent runner: every role node returns a canned
        # deliverable. Keeps the engine + dual-sink real, the model fake.
        import lib.orchestration_engine as eng
        cls._orig_runner = eng.FlowExecutor._default_runner
        eng.FlowExecutor._default_runner = (
            lambda self, node, context, iteration: {
                'output': 'stub output for ' + str(node.get('role') or node.get('id')),
                'status': 'completed', 'error': ''})

    @classmethod
    def tearDownClass(cls):
        import lib.orchestration_engine as eng
        eng.FlowExecutor._default_runner = cls._orig_runner
        cls.fix.cleanup()
        cls._dbtmp.cleanup()

    def _hdr(self):
        return {'Authorization': f'Bearer {self.token}'}

    def _def(self, name='TaskFlow'):
        return {
            'schema': 'tofu.orchestration/v1', 'name': name,
            'nodes': [
                {'id': 's1', 'type': 'control', 'kind': 'start',
                 'params': {'seed': 'screen these candidates'}},
                {'id': 'w1', 'type': 'role', 'role': 'worker',
                 'params': {'tier': 'heavy', 'isolation': 'shared-context'}},
                {'id': 'e1', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 's1', 'to': 'w1'}, {'from': 'w1', 'to': 'e1'}],
        }

    def test_create_then_poll_events_to_completion(self):
        async def go():
            cli = self.fix.app.test_client()
            # 1. POST a durable run.
            r = await cli.post('/api/v1/orchestrations/tasks',
                               headers=self._hdr(),
                               json={'definition': self._def(), 'input': 'go'})
            self.assertEqual(r.status_code, 201)
            created = await r.get_json()
            self.assertTrue(created['ok'])
            run_id = created['run_id']
            self.assertTrue(run_id.startswith('run_'))

            # 2. Poll /events until done. The worker runs via
            # asyncio.ensure_future on THIS loop, so the awaits below both
            # advance it and consume the cursor stream.
            cursor, status, seen, deadline = 0, 'pending', [], 0
            while deadline < 100:   # ~10s cap
                deadline += 1
                r = await cli.get(
                    f'/api/v1/orchestrations/tasks/{run_id}/events?cursor={cursor}',
                    headers=self._hdr())
                self.assertEqual(r.status_code, 200)
                body = await r.get_json()
                self.assertTrue(body['ok'])
                for ev in body['events']:
                    seen.append(ev['type'])
                cursor = body['next_cursor']
                status = body['status']
                if body['done']:
                    break
                await asyncio.sleep(0.1)

            # 3. The run completed and the durable event log replays the
            # real engine vocabulary.
            self.assertEqual(status, 'done', f'did not finish; saw={seen}')
            self.assertIn('flow_start', seen)
            self.assertIn('step_complete', seen)
            self.assertIn('flow_complete', seen)

            # 4. The header row persisted the terminal status + final.
            r = await cli.get(f'/api/v1/orchestrations/tasks/{run_id}',
                              headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            run = (await r.get_json())['run']
            self.assertEqual(run['status'], 'done')
            self.assertEqual(run['definition']['name'], 'TaskFlow')  # snapshot

            # 5. Durability: a SECOND poll from cursor 0 replays the SAME
            # events from the DB (not the in-memory runtime) — the dual-sink
            # claim. Re-fetch via the route after completion.
            r = await cli.get(
                f'/api/v1/orchestrations/tasks/{run_id}/events?cursor=0',
                headers=self._hdr())
            replay = await r.get_json()
            replay_types = [e['type'] for e in replay['events']]
            self.assertIn('flow_start', replay_types)
            self.assertIn('flow_complete', replay_types)
            self.assertTrue(replay['done'])

            # 6. The run shows up in the list, then deletes cleanly.
            r = await cli.get('/api/v1/orchestrations/tasks', headers=self._hdr())
            runs = (await r.get_json())['runs']
            self.assertTrue(any(x['id'] == run_id for x in runs))
            r = await cli.delete(f'/api/v1/orchestrations/tasks/{run_id}',
                                 headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            r = await cli.get(f'/api/v1/orchestrations/tasks/{run_id}',
                              headers=self._hdr())
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_durable_log_carries_step_trace_not_deltas(self):
        """The durable event log must persist a self-contained ``step_trace``
        per node (resolved brief + bounded input + full output) so a REOPENED
        run can rebuild the per-node data-flow overlay — but must NOT persist
        the high-frequency per-token ``step_delta`` stream (that exists only
        to paint a live chat bubble; persisting it would bloat the log)."""
        async def go():
            cli = self.fix.app.test_client()
            r = await cli.post('/api/v1/orchestrations/tasks',
                               headers=self._hdr(),
                               json={'definition': self._def('TraceFlow'), 'input': 'go'})
            run_id = (await r.get_json())['run_id']

            cursor, deadline = 0, 0
            evs = []
            while deadline < 100:
                deadline += 1
                r = await cli.get(
                    f'/api/v1/orchestrations/tasks/{run_id}/events?cursor={cursor}',
                    headers=self._hdr())
                body = await r.get_json()
                evs.extend(body['events'])
                cursor = body['next_cursor']
                if body['done']:
                    break
                await asyncio.sleep(0.1)

            types = [e['type'] for e in evs]
            # step_trace persisted, step_delta filtered out of the durable log.
            self.assertIn('step_trace', types)
            self.assertNotIn('step_delta', types)
            # The worker node's trace is self-contained: resolved brief + the
            # full output, keyed by node_id.
            wtrace = [e for e in evs if e['type'] == 'step_trace'
                      and e.get('node_id') == 'w1']
            self.assertTrue(wtrace, 'worker step_trace missing from durable log')
            tr = wtrace[-1]
            self.assertIn('brief', tr)
            self.assertIn('output', tr)
            self.assertIn('stub output', tr['output'])
            self.assertEqual(tr['role'], 'worker')

            # Replay from cursor 0 (DB, not memory) still has the trace.
            r = await cli.get(
                f'/api/v1/orchestrations/tasks/{run_id}/events?cursor=0',
                headers=self._hdr())
            replay = await r.get_json()
            self.assertIn('step_trace', [e['type'] for e in replay['events']])

            await cli.delete(f'/api/v1/orchestrations/tasks/{run_id}',
                             headers=self._hdr())
        _run(go())

    def _gated_def(self, name='GatedFlow'):
        """A flow with a human APPROVE gate between worker and stop, so the
        run parks in status='paused' until the gate is resolved."""
        return {
            'schema': 'tofu.orchestration/v1', 'name': name,
            'nodes': [
                {'id': 's1', 'type': 'control', 'kind': 'start',
                 'params': {'seed': 'screen these candidates'}},
                {'id': 'w1', 'type': 'role', 'role': 'worker',
                 'params': {'tier': 'heavy', 'isolation': 'shared-context'}},
                {'id': 'h1', 'type': 'control', 'kind': 'human',
                 'params': {'mode': 'approve', 'prompt': 'Send outreach?'}},
                {'id': 'e1', 'type': 'control', 'kind': 'stop'},
            ],
            'edges': [{'from': 's1', 'to': 'w1'}, {'from': 'w1', 'to': 'h1'},
                      {'from': 'h1', 'to': 'e1'}],
        }

    def test_human_gate_pauses_then_resolves_to_done(self):
        """Phase 3: a run blocked on a human approve gate reports
        status='paused', and resolving via /run/human-approve unblocks the
        engine and drives it to 'done'. Exercises the real gate primitive +
        the status-transition wiring in the worker."""
        async def go():
            cli = self.fix.app.test_client()
            r = await cli.post('/api/v1/orchestrations/tasks',
                               headers=self._hdr(),
                               json={'definition': self._gated_def(), 'input': 'go'})
            self.assertEqual(r.status_code, 201)
            run_id = (await r.get_json())['run_id']

            # Poll until the gate request appears; capture its request_id and
            # assert the header parked in 'paused'.
            cursor, req_id, status, deadline = 0, None, 'pending', 0
            while deadline < 100 and req_id is None:
                deadline += 1
                r = await cli.get(
                    f'/api/v1/orchestrations/tasks/{run_id}/events?cursor={cursor}',
                    headers=self._hdr())
                body = await r.get_json()
                for ev in body['events']:
                    if ev['type'] == 'human_request':
                        req_id = ev.get('request_id')
                cursor = body['next_cursor']
                status = body['status']
                if req_id is not None:
                    break
                await asyncio.sleep(0.1)

            self.assertIsNotNone(req_id, 'human_request gate never emitted')
            # The header status reflects the paused gate.
            r = await cli.get(f'/api/v1/orchestrations/tasks/{run_id}',
                              headers=self._hdr())
            self.assertEqual((await r.get_json())['run']['status'], 'paused')

            # Resolve the gate (approve) via the existing endpoint.
            r = await cli.post('/api/v1/orchestrations/run/human-approve',
                               headers=self._hdr(),
                               json={'requestId': req_id, 'approved': True})
            self.assertEqual(r.status_code, 200)

            # The engine unblocks and the run drives to completion.
            status, seen, deadline = 'paused', [], 0
            while deadline < 100:
                deadline += 1
                r = await cli.get(
                    f'/api/v1/orchestrations/tasks/{run_id}/events?cursor={cursor}',
                    headers=self._hdr())
                body = await r.get_json()
                for ev in body['events']:
                    seen.append(ev['type'])
                cursor = body['next_cursor']
                status = body['status']
                if body['done']:
                    break
                await asyncio.sleep(0.1)

            self.assertEqual(status, 'done', f'did not finish; saw={seen}')
            self.assertIn('human_resolved', seen)
            self.assertIn('flow_complete', seen)
            await cli.delete(f'/api/v1/orchestrations/tasks/{run_id}',
                             headers=self._hdr())
        _run(go())

    def test_create_invalid_definition_is_400(self):
        async def go():
            bad = self._def(); bad['name'] = ''
            r = await self.fix.app.test_client().post(
                '/api/v1/orchestrations/tasks', headers=self._hdr(),
                json={'definition': bad})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_tasks_require_auth(self):
        async def go():
            r = await self.fix.app.test_client().get('/api/v1/orchestrations/tasks')
            self.assertEqual(r.status_code, 401)
        _run(go())


if __name__ == '__main__':
    unittest.main()
