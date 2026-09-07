"""Orchestration validation, owner-scoped persistence, and HTTP contracts.

Covers:
  * lib.orchestration.validate_definition — pure validation rules.
  * /api/v1/orchestrations CRUD + /validate — end-to-end through a Quart
    test client, mirroring the fixture style of test_api_v1_integration.

The module-scoped Sidecar fixture owns every persisted definition, run, and
event. No test redirects legacy JSON or application-database paths.
"""

import asyncio
import unittest

import pytest

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.api


# ── Pure validator tests (no app needed) ────────────────────────────

class ValidatorTest(unittest.TestCase):
    def _v(self, d):
        from lib.orchestration._validate import validate_definition
        return validate_definition(d)

    def _verifier_loop_def(self):
        return {
            'schema': 'tofu.orchestration/v1',
            'name': 'Verifier Loop',
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

    def test_valid_verifier_loop_passes_clean(self):
        v = self._v(self._verifier_loop_def())
        self.assertTrue(v['ok'], v['errors'])
        self.assertEqual(v['errors'], [])
        self.assertEqual(v['warnings'], [])

    def test_missing_name_is_error(self):
        d = self._verifier_loop_def(); d['name'] = ''
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('name' in e for e in v['errors']))

    def test_duplicate_id_is_error(self):
        d = self._verifier_loop_def()
        d['nodes'].append({'id': 's1', 'type': 'control', 'kind': 'barrier'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('duplicate' in e for e in v['errors']))

    def test_two_start_nodes_is_error(self):
        d = self._verifier_loop_def()
        d['nodes'].append({'id': 's2', 'type': 'control', 'kind': 'start'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('start' in e for e in v['errors']))

    def test_edge_to_unknown_node_is_error(self):
        d = self._verifier_loop_def()
        d['edges'].append({'from': 's1', 'to': 'ghost'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('ghost' in e for e in v['errors']))

    def test_bad_tier_and_isolation_are_errors(self):
        d = self._verifier_loop_def()
        d['nodes'][3]['params'] = {'tier': 'ultra', 'isolation': 'weird'}
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('tier' in e for e in v['errors']))
        self.assertTrue(any('isolation' in e for e in v['errors']))

    def test_unknown_role_is_warning_not_error(self):
        d = self._verifier_loop_def()
        d['nodes'][1]['role'] = 'wizard'
        v = self._v(d)
        self.assertTrue(v['ok'], v['errors'])
        self.assertTrue(any('wizard' in w for w in v['warnings']))

    def test_edge_into_start_rejected(self):
        d = self._verifier_loop_def()
        d['edges'].append({'from': 'w1', 'to': 's1'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('start' in e for e in v['errors']))

    def test_stop_has_no_output(self):
        d = self._verifier_loop_def()
        d['edges'].append({'from': 'e1', 'to': 'w1'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('stop' in e for e in v['errors']))

    def test_human_node_valid_modes(self):
        for mode in ('approve', 'input', 'notify'):
            d = self._verifier_loop_def()
            # splice a human gate between planner and loop
            d['nodes'].append({'id': 'h1', 'type': 'control', 'kind': 'human',
                               'params': {'mode': mode, 'prompt': 'ok?'}})
            d['edges'] = [e for e in d['edges'] if e != {'from': 'p1', 'to': 'l1'}]
            d['edges'] += [{'from': 'p1', 'to': 'h1'}, {'from': 'h1', 'to': 'l1'}]
            v = self._v(d)
            self.assertTrue(v['ok'], (mode, v['errors']))

    def test_human_invalid_mode_is_error(self):
        d = self._verifier_loop_def()
        d['nodes'].append({'id': 'h1', 'type': 'control', 'kind': 'human',
                           'params': {'mode': 'teleport'}})
        d['edges'].append({'from': 'p1', 'to': 'h1'})
        v = self._v(d)
        self.assertFalse(v['ok'])
        self.assertTrue(any('human mode' in e for e in v['errors']))


# ── REST CRUD tests (Quart test client) ─────────────────────────────

class _AppFixture:
    def __init__(self):
        # Install the flask→quart shim the same way the server does
        # (server._install_flask_shim handles the sync get_json wrapper +
        # Quart config defaults that a bare inline copy misses). This MUST
        # run before importing routes.* — routes/__init__ → routes/push.py
        # calls Blueprint.websocket(), which only exists after the shim is
        # installed. Otherwise a standalone run of this file errors at import.
        import server  # noqa: F401  — import side-effect installs the shim

        import routes.api_v1.orchestrations as orch_mod

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
        _row, cls.token = create_key(owner_user_id=1, name='orch-test', scopes=[], admin=True)
        _row, cls.other_token = create_key(
            owner_user_id=82_002,
            name='orch-test-other-owner',
            scopes=[],
            admin=True,
        )

    def _cli(self):
        return self.fix.app.test_client()

    def _hdr(self):
        return {'Authorization': f'Bearer {self.token}'}

    def _other_hdr(self):
        return {'Authorization': f'Bearer {self.other_token}'}

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
            cli = self._cli()
            for method, path in (
                ('get', '/api/v1/orchestrations'),
                ('get', '/api/v1/orchestrations/run/poll/missing'),
                ('post', '/api/v1/orchestrations/run/abort/missing'),
            ):
                r = await getattr(cli, method)(path)
                self.assertEqual(r.status_code, 401, path)
        _run(go())

    def test_repository_failure_uses_typed_500_envelope(self):
        from lib.orchestration.definition_service import DefinitionServiceError
        import routes.api_v1.orchestrations as orch_mod

        class BrokenDefinitions:
            @staticmethod
            def list_summaries():
                raise DefinitionServiceError('repository offline')

        original = orch_mod._definitions
        orch_mod._definitions = lambda: BrokenDefinitions()
        try:
            async def go():
                response = await self._cli().get(
                    '/api/v1/orchestrations', headers=self._hdr())
                self.assertEqual(response.status_code, 500)
                body = await response.get_json()
                self.assertFalse(body['ok'])
                self.assertEqual(body['error']['kind'], 'internal')
                self.assertEqual(
                    body['error']['context'],
                    'api_v1.orchestrations.list',
                )
            _run(go())
        finally:
            orch_mod._definitions = original

    def test_definition_resolution_failure_is_shared_by_all_consumers(self):
        from lib.orchestration.definition_service import DefinitionServiceError
        import routes.api_v1.orchestrations as orch_mod

        class BrokenDefinitions:
            @staticmethod
            def resolve(**_selection):
                raise DefinitionServiceError('repository offline')

        original = orch_mod._definitions
        orch_mod._definitions = lambda: BrokenDefinitions()
        try:
            async def go():
                cli = self._cli()
                for path in (
                    '/api/v1/orchestrations/layout',
                    '/api/v1/orchestrations/plan',
                    '/api/v1/orchestrations/run',
                    '/api/v1/orchestrations/tasks',
                ):
                    response = await cli.post(
                        path, headers=self._hdr(), json={'id': 'stored-flow'})
                    self.assertEqual(response.status_code, 500, path)
                    body = await response.get_json()
                    self.assertFalse(body['ok'], path)
                    self.assertEqual(body['error']['kind'], 'internal', path)
                    self.assertEqual(
                        body['error']['context'],
                        'api_v1.orchestrations.resolve_definition',
                        path,
                    )
                    self.assertEqual(
                        body['error']['source'],
                        'orchestration:application-service',
                        path,
                    )
            _run(go())
        finally:
            orch_mod._definitions = original

    def test_validate_endpoint(self):
        async def go():
            cli = self._cli()
            r = await cli.post('/api/v1/orchestrations/validate',
                               headers=self._hdr(), json=self._def())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertEqual(
                body['format'], 'tofu.orchestration.inspection/v1')
            self.assertEqual(body['diagnostics'], [])

            bad = self._def(); bad['name'] = ''
            r = await cli.post('/api/v1/orchestrations/validate',
                               headers=self._hdr(), json=bad)
            body = await r.get_json()
            self.assertFalse(body['ok'])
            self.assertTrue(body['diagnostics'])
            self.assertEqual(body['diagnostics'][0]['severity'], 'error')

            # Diagnostic ingress intentionally accepts an incomplete object;
            # generated clients must not apply the persistence schema here.
            incomplete = await cli.post(
                '/api/v1/orchestrations/validate',
                headers=self._hdr(),
                json={},
            )
            self.assertEqual(incomplete.status_code, 200)
            incomplete_body = await incomplete.get_json()
            self.assertFalse(incomplete_body['ok'])
            self.assertIn('/name', {
                item['path'] for item in incomplete_body['diagnostics']
            })
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
            self.assertEqual(created['format'],
                             'tofu.orchestration.definition-entry/v1')
            self.assertEqual(created['name'], 'CycleFlow')
            self.assertEqual(r.headers['ETag'],
                             f'"{created["updatedAt"]}"')

            # List
            r = await cli.get('/api/v1/orchestrations', headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            list_body = await r.get_json()
            self.assertEqual(list_body['format'],
                             'tofu.orchestration.definition-list/v1')
            lst = list_body['items']
            listed = next(e for e in lst if e['id'] == oid)
            self.assertEqual(listed['nodeCount'], 3)
            self.assertNotIn('definition', listed)

            # Get
            r = await cli.get(f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            got = await r.get_json()
            self.assertEqual(got['format'],
                             'tofu.orchestration.definition-entry/v1')
            self.assertEqual(len(got['definition']['nodes']), 3)
            self.assertEqual(r.headers['ETag'], f'"{got["updatedAt"]}"')

            # Update (rename)
            upd = self._def('CycleFlow v2')
            r = await cli.put(f'/api/v1/orchestrations/{oid}',
                              headers={
                                  **self._hdr(),
                                  'If-Match': f'"{got["updatedAt"]}"',
                              }, json=upd)
            self.assertEqual(r.status_code, 200)
            updated = await r.get_json()
            self.assertEqual(updated['format'],
                             'tofu.orchestration.definition-entry/v1')
            self.assertEqual(updated['name'], 'CycleFlow v2')
            self.assertEqual(r.headers['ETag'],
                             f'"{updated["updatedAt"]}"')

            # Delete
            r = await cli.delete(
                f'/api/v1/orchestrations/{oid}',
                headers={
                    **self._hdr(),
                    'If-Match': f'"{updated["updatedAt"]}"',
                },
            )
            self.assertEqual(r.status_code, 200)
            r = await cli.get(f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_definition_http_boundary_is_owner_scoped(self):
        async def go():
            client = self._cli()
            created_response = await client.post(
                '/api/v1/orchestrations',
                headers=self._hdr(),
                json=self._def('Private owner flow'),
            )
            self.assertEqual(created_response.status_code, 201)
            created = await created_response.get_json()
            orchestration_id = created['id']
            guarded_other_headers = {
                **self._other_hdr(),
                'If-Match': f'"{created["updatedAt"]}"',
            }
            try:
                other_list = await client.get(
                    '/api/v1/orchestrations', headers=self._other_hdr())
                self.assertEqual(other_list.status_code, 200)
                self.assertNotIn(orchestration_id, {
                    item['id']
                    for item in (await other_list.get_json())['items']
                })

                other_read = await client.get(
                    f'/api/v1/orchestrations/{orchestration_id}',
                    headers=self._other_hdr(),
                )
                self.assertEqual(other_read.status_code, 404)
                other_update = await client.put(
                    f'/api/v1/orchestrations/{orchestration_id}',
                    headers=guarded_other_headers,
                    json=self._def('Cross-owner overwrite'),
                )
                self.assertEqual(other_update.status_code, 404)
                other_delete = await client.delete(
                    f'/api/v1/orchestrations/{orchestration_id}',
                    headers=guarded_other_headers,
                )
                self.assertEqual(other_delete.status_code, 404)

                owner_read = await client.get(
                    f'/api/v1/orchestrations/{orchestration_id}',
                    headers=self._hdr(),
                )
                self.assertEqual(owner_read.status_code, 200)
            finally:
                await client.delete(
                    f'/api/v1/orchestrations/{orchestration_id}',
                    headers={
                        **self._hdr(),
                        'If-Match': f'"{created["updatedAt"]}"',
                    },
                )

        _run(go())

    def test_guarded_update_rejects_stale_tab_without_overwrite(self):
        async def go():
            cli = self._cli()
            created_response = await cli.post(
                '/api/v1/orchestrations',
                headers=self._hdr(),
                json=self._def('Shared draft'),
            )
            created = await created_response.get_json()
            oid = created['id']
            initial_version = created['updatedAt']
            guarded_headers = {
                **self._hdr(),
                'If-Match': f'"{initial_version}"',
            }

            first = await cli.put(
                f'/api/v1/orchestrations/{oid}',
                headers=guarded_headers,
                json=self._def('First tab'),
            )
            self.assertEqual(first.status_code, 200)
            first_body = await first.get_json()
            self.assertGreater(first_body['updatedAt'], initial_version)
            self.assertEqual(first.headers['ETag'],
                             f'"{first_body["updatedAt"]}"')

            stale = await cli.put(
                f'/api/v1/orchestrations/{oid}',
                headers=guarded_headers,
                json=self._def('Stale tab'),
            )
            self.assertEqual(stale.status_code, 409)
            stale_body = await stale.get_json()
            self.assertEqual(stale_body['conflict'], 'stale_definition')
            self.assertEqual(stale_body['write']['format'],
                             'tofu.orchestration.definition-write/v1')
            self.assertEqual(stale_body['write']['expectedUpdatedAt'],
                             initial_version)
            self.assertEqual(stale_body['write']['currentUpdatedAt'],
                             first_body['updatedAt'])

            stored = await cli.get(
                f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            stored_body = await stored.get_json()
            self.assertEqual(stored_body['definition']['name'], 'First tab')

            stale_delete = await cli.delete(
                f'/api/v1/orchestrations/{oid}',
                headers=guarded_headers,
            )
            self.assertEqual(stale_delete.status_code, 409)
            stale_delete_body = await stale_delete.get_json()
            self.assertEqual(stale_delete_body['write']['operation'], 'delete')
            still_stored = await cli.get(
                f'/api/v1/orchestrations/{oid}', headers=self._hdr())
            self.assertEqual(still_stored.status_code, 200)

            invalid = await cli.put(
                f'/api/v1/orchestrations/{oid}',
                headers={**self._hdr(), 'If-Match': 'not-a-version'},
                json=self._def('Never written'),
            )
            self.assertEqual(invalid.status_code, 400)

        _run(go())

    def test_compose_empty_requirement_is_400(self):
        async def go():
            r = await self._cli().post('/api/v1/orchestrations/compose',
                                       headers=self._hdr(), json={'requirement': '  '})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_builtin_autopilot(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations/builtin/autopilot',
                                      headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            ids = [n['id'] for n in body['definition']['nodes']]
            self.assertIn('worker', ids)
            self.assertIn('vu', ids)
        _run(go())

    def test_all_studio_builtins_use_the_same_api(self):
        async def go():
            client = self._cli()
            for name in ('autopilot', 'fanout', 'adversarial', 'blank'):
                r = await client.get(
                    f'/api/v1/orchestrations/builtin/{name}',
                    headers=self._hdr(),
                )
                self.assertEqual(r.status_code, 200, name)
                body = await r.get_json()
                self.assertTrue(body['ok'], name)
                self.assertEqual(body['definition']['schema'],
                                 'tofu.orchestration/v1')
                self.assertTrue(body['inspection']['ok'], name)
        _run(go())

    def test_builtin_unknown_is_404(self):
        async def go():
            r = await self._cli().get('/api/v1/orchestrations/builtin/nope',
                                      headers=self._hdr())
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_authoring_contract_full_map(self):
        async def go():
            r = await self._cli().get(
                                      '/api/v1/orchestrations/authoring-contract',
                                      headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('critic', body['roles'])
            self.assertIn('worker', body['roles'])
            self.assertTrue(body['generic'])
            self.assertIn('select', body['kinds'])
            self.assertEqual(set(body['controlSchemas']), set(body['controls']))
            self.assertEqual(body['executionOptions']['tiers'],
                             ['light', 'standard', 'heavy'])
            self.assertEqual(body['executionOptions']['scopes'],
                             ['isolated', 'inline'])
            loop_keys = [f['key'] for f in body['controlSchemas']['loop']]
            self.assertEqual(loop_keys,
                             ['max_iterations', 'stop_condition', 'verifier'])
            self.assertEqual(body['controlSchemas']['parallel'], [])
            io_contract = body['ioContract']
            self.assertEqual(io_contract['maxPorts'], 12)
            self.assertEqual(io_contract['defaultOutput'],
                             {'name': 'text', 'type': 'text'})
            self.assertEqual(io_contract['startRef'], 'start')
            self.assertEqual(
                io_contract['presets']['toolHeavyWorker']['outputs'],
                [{'name': 'summary', 'type': 'text'},
                 {'name': 'changes', 'type': 'artifact'}],
            )
            node_defaults = body['nodeDefaults']
            self.assertEqual(node_defaults['roles']['worker']['tier'],
                             body['personas']['worker']['tier'])
            self.assertEqual(node_defaults['controls']['loop']['max_iterations'],
                             10)
            self.assertEqual(node_defaults['controls']['human']['mode'],
                             'approve')
            self.assertEqual(node_defaults['subflow']['scope'], 'isolated')
            self.assertEqual(node_defaults['blankSubflow']['schema'],
                             'tofu.orchestration/v1')
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
            self.assertEqual(body['replayContract']['format'],
                             'tofu.task-replay/v1')
            self.assertTrue(body['replayContract']['cursor'][
                'producerOwned'])
            self.assertTrue(body['replayContract']['cursor'][
                'futureCursorReset'])
            self.assertEqual(body['replayContract']['terminalSnapshot'], {
                'field': 'run',
                'when': {'field': 'done', 'equals': True},
                'optional': True,
            })
            self.assertEqual(body['fieldValueContract']['format'],
                             'tofu.orchestration.field-value/v1')
            self.assertEqual(body['fieldValueContract']['kinds']['list'][
                'wire'], 'array<string>')
            self.assertEqual(body['definitionWriteContract']['format'],
                             'tofu.orchestration.definition-write/v1')
            self.assertEqual(
                body['definitionWriteContract']['preconditionHeader'],
                'If-Match')
            self.assertEqual(body['definitionWriteContract']['conflictStatus'],
                             409)
            self.assertEqual(body['definitionWriteContract']['operations'],
                             ['replace', 'delete'])
            self.assertEqual(body['definitionListContract']['format'],
                             'tofu.orchestration.definition-list/v1')
            self.assertEqual(body['definitionListContract']['orderBy'][0], {
                'field': 'updatedAt', 'direction': 'desc',
            })
            self.assertEqual(body['definitionEntryContract']['format'],
                             'tofu.orchestration.definition-entry/v1')
            self.assertEqual(body['definitionEntryContract']['versionField'],
                             'updatedAt')
            self.assertEqual(body['runtimeStartContract'], {
                'format': 'tofu.orchestration.runtime-start/v1',
                'kinds': ['ephemeral', 'durable'],
                'idField': 'id',
                'kindField': 'kind',
                'successStatuses': {
                    'ephemeral': 200,
                    'durable': 201,
                },
            })
            self.assertEqual(
                body['durableRunContract']['format'],
                'tofu.orchestration.durable-run/v1')
            self.assertIn(
                'definition', body['durableRunContract']['readFields'])
        _run(go())

    def test_authoring_contract_requires_auth(self):
        async def go():
            r = await self._cli().get(
                '/api/v1/orchestrations/authoring-contract')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_plan_returns_steps(self):
        async def go():
            cli = self._cli()
            r = await cli.post('/api/v1/orchestrations/plan',
                               headers=self._hdr(),
                               json={'definition': self._def()})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertEqual(body['definitionSource'], 'inline')
            actions = [s['action'] for s in body['steps']]
            self.assertIn('run-agent', actions)

            created = await cli.post(
                '/api/v1/orchestrations', headers=self._hdr(),
                json=self._def('Stored plan'),
            )
            orchestration_id = (await created.get_json())['id']
            stored = await cli.post(
                '/api/v1/orchestrations/plan', headers=self._hdr(),
                json={'id': orchestration_id},
            )
            stored_body = await stored.get_json()
            self.assertEqual(
                stored_body['definitionSource'],
                f'stored:{orchestration_id}',
            )
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
    def _verifier_loop_payload(self):
        import json
        g = {'reply': 'Built a verifier loop.', 'definition': {
            'name': 'Verifier Loop', 'nodes': [
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
        payload = self._verifier_loop_payload()
        r = compose('verifier loop', llm_override=lambda m: (payload, {}))
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
        from lib.orchestration._layout import layout_definition
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
        from lib.orchestration._layout import layout_definition
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


class TemplateBackendCoordsTest(unittest.TestCase):
    """Server-owned builtins carry the exact canonical layout coordinates.

    Templates used to be parsed out of a second catalogue baked into
    ``orchestration.js``. They now come directly from the backend builders;
    this guard strips their positions and re-runs the layout engine to ensure
    each builder still returns the final first-paint coordinates.
    """

    _TEMPLATES = ('autopilot', 'fanout', 'adversarial')

    def test_each_template_matches_layout_engine(self):
        import copy

        from lib.orchestration._layout import layout_definition
        from lib.orchestration.authoring_builtin_registry import (
            build_builtin_definition,
        )

        for which in self._TEMPLATES:
            with self.subTest(template=which):
                built = build_builtin_definition(which)
                self.assertTrue(built and built['nodes'])
                expected = {node['id']: node['pos'] for node in built['nodes']}
                unlaid = copy.deepcopy(built)
                for node in unlaid['nodes']:
                    node.pop('pos', None)
                layout_definition(unlaid)
                computed = {node['id']: node['pos'] for node in unlaid['nodes']}
                self.assertEqual(computed, expected)


# ── Durable run-instance persistence (Task Mode, Phase 2) ───────────

class RunInstanceTest(unittest.TestCase):
    """Direct owner-bound Sidecar run/event repository coverage."""

    OWNER_USER_ID = 82_001

    def _store(self):
        from lib.orchestration.sidecar_run_store import (
            SidecarOrchestrationRunStore,
        )

        return SidecarOrchestrationRunStore(self.OWNER_USER_ID)

    def _defn(self):
        return {'schema': 'tofu.orchestration/v1', 'name': 'Screener',
                'nodes': [], 'edges': []}

    def test_create_get_list_and_definition_snapshot(self):
        r = self._store()
        rid = r.new_run_id()
        self.assertTrue(rid.startswith('run_'))
        self.assertTrue(r.create_run(
            rid, definition=self._defn(), input_text='go',
            orch_id='orch_x', name='Screener', created_by='k1'))
        run = r.get_run(rid)
        self.assertEqual(run['status'], 'pending')
        self.assertFalse(run['terminal'])
        self.assertEqual(run['orch_id'], 'orch_x')
        self.assertEqual(run['definition']['name'], 'Screener')  # snapshot
        self.assertEqual(run['input'], 'go')
        # list omits the definition blob (cheap listing)
        listed = [x for x in r.list_runs() if x['id'] == rid]
        self.assertEqual(len(listed), 1)
        self.assertNotIn('definition', listed[0])
        r.delete_run(rid)

    def test_event_log_cursor_replay_and_dup_seq_is_benign(self):
        r = self._store()
        rid = r.new_run_id()
        r.create_run(rid, definition=self._defn())
        completed = {'type': 'step_complete', 'node_id': 'n1'}
        try:
            self.assertTrue(r.append_event(
                rid, 0, {'type': 'flow_start', 'nodes': 2}))
            self.assertTrue(r.append_event(
                rid, 1, {'type': 'step_start', 'node_id': 'n1'}))
            self.assertTrue(r.append_event(rid, 2, completed))
            # An identical retry is idempotent; a conflicting payload is fenced.
            self.assertTrue(r.append_event(rid, 2, completed))
            self.assertFalse(r.append_event(rid, 2, {'type': 'dup'}))
            evs = r.get_events(rid, 0)
            self.assertEqual([e['type'] for e in evs],
                             ['flow_start', 'step_start', 'step_complete'])
            self.assertTrue(all('seq' in e for e in evs))
            # cursor replay from the middle
            self.assertEqual([e['type'] for e in r.get_events(rid, 2)],
                             ['step_complete'])

            # A corrupt/future client cursor is reset to the durable boundary.
            future_page = r.get_event_page(rid, 999)
            self.assertEqual(future_page.events, [])
            self.assertEqual(future_page.next_cursor, 3)
            self.assertTrue(future_page.cursor_reset)
            self.assertTrue(r.append_event(
                rid, future_page.next_cursor, {'type': 'flow_complete'}))
            resumed = r.get_event_page(rid, future_page.next_cursor)
            self.assertEqual(
                [e['type'] for e in resumed.events], ['flow_complete'])
            self.assertEqual(resumed.next_cursor, 4)
            self.assertFalse(resumed.cursor_reset)
        finally:
            r.delete_run(rid)

    def test_terminal_status_sets_finished_and_final(self):
        r = self._store()
        rid = r.new_run_id()
        r.create_run(rid, definition=self._defn())
        try:
            self.assertTrue(r.update_status(
                rid, 'done', final='12 shortlisted', error=None))
            run = r.get_run(rid)
            self.assertEqual(run['status'], 'done')
            self.assertTrue(run['terminal'])
            self.assertEqual(run['final'], '12 shortlisted')
            self.assertGreater(run['finished_at'], 0)
            self.assertIsNone(run['error'])
            first_finished_at = run['finished_at']

            # Idempotent final enrichment is allowed, but the first terminal
            # timestamp is stable and a terminal row cannot be resurrected.
            self.assertTrue(r.update_status(rid, 'done', final='enriched'))
            self.assertFalse(r.update_status(rid, 'running'))
            fenced = r.get_run(rid)
            self.assertEqual(fenced['status'], 'done')
            self.assertEqual(fenced['final'], 'enriched')
            self.assertEqual(fenced['finished_at'], first_finished_at)
        finally:
            r.delete_run(rid)

    def test_startup_recovery_retires_active_headers_and_preserves_events(self):
        r = self._store()
        active = []
        for status in ('pending', 'running', 'paused'):
            rid = r.new_run_id()
            self.assertTrue(r.create_run(rid, definition=self._defn()))
            if status != 'pending':
                self.assertTrue(r.update_status(rid, status))
            self.assertTrue(r.append_event(
                rid, 0, {'type': 'flow_start', 'status': status}))
            active.append(rid)
        done = r.new_run_id()
        self.assertTrue(r.create_run(done, definition=self._defn()))
        self.assertTrue(r.update_status(done, 'done', final='kept'))
        reason = {
            'kind': 'worker_lost',
            'message': 'Run interrupted by a server restart before completion.',
        }
        try:
            self.assertEqual(r.retire_interrupted_runs(reason), 3)
            for rid in active:
                run = r.get_run(rid)
                self.assertEqual(run['status'], 'error')
                self.assertTrue(run['terminal'])
                self.assertEqual(run['error'], reason)
                self.assertEqual(r.get_events(rid, 0)[0]['type'], 'flow_start')
            self.assertEqual(r.get_run(done)['status'], 'done')
            self.assertEqual(r.get_run(done)['final'], 'kept')
        finally:
            for rid in active + [done]:
                r.delete_run(rid)

    def test_status_filter_and_delete(self):
        r = self._store()
        rid = r.new_run_id()
        r.create_run(rid, definition=self._defn(), orch_id='orch_f')
        r.update_status(rid, 'error', error='boom')
        self.assertTrue(any(x['id'] == rid for x in r.list_runs(status='error')))
        self.assertFalse(any(x['id'] == rid for x in r.list_runs(status='done')))
        run = r.get_run(rid)
        self.assertEqual(run['error'], 'boom')
        self.assertTrue(r.delete_run(rid))
        self.assertIsNone(r.get_run(rid))
        self.assertFalse(r.delete_run(rid))

    def test_unknown_status_is_rejected_by_storage_contract(self):
        from lib.orchestration.run_store_port import OrchestrationRunStoreError

        r = self._store()
        rid = r.new_run_id()
        self.assertTrue(r.create_run(rid, definition=self._defn()))
        try:
            self.assertFalse(r.update_status(rid, 'dnne'))
            self.assertEqual(r.get_run(rid)['status'], 'pending')
            with self.assertRaises(OrchestrationRunStoreError):
                r.list_runs(status='dnne')
        finally:
            r.delete_run(rid)


class TaskRunHttpTest(unittest.TestCase):
    """End-to-end through the REAL /api/v1/orchestrations/tasks routes:
    POST a flow → poll /events to completion → assert the durable event log
    and final result persisted.

    This is the load-bearing coverage for the runtime + Sidecar projection:
    it drives the actual route handler, the real FlowExecutor, and the real
    owner-scoped orchestration tables. The only thing stubbed is the LLM;
    engine execution, event projection, and durable persistence remain real.
    """

    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        cls.fix = _AppFixture()
        from lib.api_keys import create_key
        _row, cls.token = create_key(owner_user_id=1, name='orch-task-test', scopes=[], admin=True)
        _row, cls.other_token = create_key(
            owner_user_id=82_003,
            name='orch-task-other-owner',
            scopes=[],
            admin=True,
        )

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

    def _hdr(self):
        return {'Authorization': f'Bearer {self.token}'}

    def _other_hdr(self):
        return {'Authorization': f'Bearer {self.other_token}'}

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

    def test_create_fails_closed_before_runtime_when_persistence_fails(self):
        from unittest.mock import patch
        import routes.api_v1.orchestrations as route_module

        class FailedRuns:
            def create_new(self, **_kwargs):
                return ''

        async def go():
            cli = self.fix.app.test_client()
            with patch.object(route_module, '_run_instances',
                              return_value=FailedRuns()), \
                    patch.object(
                        route_module.orchestration_run_runtime,
                        'create',
                    ) as runtime_create:
                response = await cli.post(
                    '/api/v1/orchestrations/tasks',
                    headers=self._hdr(),
                    json={'definition': self._def(), 'input': 'go'},
                )
            self.assertEqual(response.status_code, 500)
            runtime_create.assert_not_called()

        _run(go())

    def test_ephemeral_start_publishes_shared_runtime_identity(self):
        async def go():
            cli = self.fix.app.test_client()
            response = await cli.post(
                '/api/v1/orchestrations/run',
                headers=self._hdr(),
                json={'definition': self._def(), 'input': 'go'},
            )
            self.assertEqual(response.status_code, 200)
            body = await response.get_json()
            self.assertTrue(body['start']['id'])
            self.assertEqual(body['start'], {
                'format': 'tofu.orchestration.runtime-start/v1',
                'kind': 'ephemeral',
                'id': body['start']['id'],
            })

        _run(go())

    def test_worker_start_failure_closes_durable_projection(self):
        from unittest.mock import patch
        import lib.orchestration.runtime_start_service as start_module
        import routes.api_v1.orchestrations as route_module

        class Runs:
            def __init__(self):
                self.transitions = []

            def create_new(self, **_kwargs):
                return 'run-start-failed'

            def transition_status(self, run_id, status, **values):
                self.transitions.append((run_id, status, values))
                return type('Transition', (), {
                    'ok': True,
                    'reason': 'accepted',
                    'run_status': status,
                })()

        runs = Runs()

        async def go():
            cli = self.fix.app.test_client()
            with patch.object(route_module, '_run_instances',
                              return_value=runs), \
                    patch.object(
                        start_module,
                        'spawn_runtime_flow',
                        side_effect=RuntimeError('worker unavailable'),
                    ):
                response = await cli.post(
                    '/api/v1/orchestrations/tasks',
                    headers=self._hdr(),
                    json={'definition': self._def(), 'input': 'go'},
                )
            self.assertEqual(response.status_code, 500)
            self.assertFalse((await response.get_json())['ok'])
            self.assertEqual(runs.transitions[0][0:2], (
                'run-start-failed', 'error'))
            self.assertEqual(
                runs.transitions[0][2]['error']['kind'],
                'internal',
            )

        _run(go())

    def test_ephemeral_abort_uses_the_same_mutation_contract(self):
        from unittest.mock import patch
        import routes.api_v1.orchestrations as route_module

        task_id = 'mutation-contract-ephemeral'
        runtime = route_module.orchestration_run_runtime
        runtime.create(user_id=1, task_id=task_id)

        async def go():
            cli = self.fix.app.test_client()
            response = await cli.post(
                f'/api/v1/orchestrations/run/abort/{task_id}',
                headers=self._hdr(),
            )
            self.assertEqual(response.status_code, 200)
            accepted = await response.get_json()
            self.assertEqual(
                accepted['mutation']['resource_status'], 'aborting')
            self.assertEqual(accepted['mutation']['action'], 'abort_run')
            self.assertEqual(accepted['mutation']['reason'], 'accepted')
            self.assertFalse(accepted['mutation']['reconcile_required'])

            runtime.finish(task_id)
            response = await cli.post(
                f'/api/v1/orchestrations/run/abort/{task_id}',
                headers=self._hdr(),
            )
            self.assertEqual(response.status_code, 409)
            terminal = await response.get_json()
            self.assertEqual(
                terminal['mutation']['resource_status'], 'aborted')
            self.assertEqual(terminal['mutation']['reason'], 'terminal')
            self.assertTrue(terminal['mutation']['reconcile_required'])

            response = await cli.post(
                '/api/v1/orchestrations/run/abort/missing-ephemeral',
                headers=self._hdr(),
            )
            self.assertEqual(response.status_code, 404)
            missing = await response.get_json()
            self.assertEqual(missing['mutation']['reason'], 'not_found')

            with patch.object(
                    runtime, 'abort_owned',
                    side_effect=OSError('registry offline')):
                response = await cli.post(
                    '/api/v1/orchestrations/run/abort/runtime-failure',
                    headers=self._hdr(),
                )
            self.assertEqual(response.status_code, 500)
            failure = await response.get_json()
            self.assertFalse(failure['ok'])
            self.assertEqual(failure['error']['kind'], 'internal')
            self.assertEqual(
                failure['error']['source'],
                'orchestration:application-service',
            )

        _run(go())

    def test_ephemeral_abort_cannot_cross_owner_boundary(self):
        import routes.api_v1.orchestrations as route_module

        task_id = 'owner-bound-ephemeral'
        runtime = route_module.orchestration_run_runtime
        runtime.create(user_id=1, task_id=task_id)

        async def go():
            client = self.fix.app.test_client()
            denied = await client.post(
                f'/api/v1/orchestrations/run/abort/{task_id}',
                headers=self._other_hdr(),
            )
            self.assertEqual(denied.status_code, 404)
            denied_body = await denied.get_json()
            self.assertEqual(
                denied_body['mutation']['reason'], 'not_found')
            self.assertFalse(runtime.get(task_id).get('aborted', False))

            accepted = await client.post(
                f'/api/v1/orchestrations/run/abort/{task_id}',
                headers=self._hdr(),
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(
                (await accepted.get_json())['mutation']['reason'], 'accepted')

        try:
            _run(go())
        finally:
            runtime.finish(task_id)

    def test_create_maps_service_exception_to_api_error(self):
        from unittest.mock import patch
        import routes.api_v1.orchestrations as route_module
        from lib.orchestration.run_service import RunServiceError

        class FailedRuns:
            def create_new(self, **_kwargs):
                raise RunServiceError('database offline')

        async def go():
            cli = self.fix.app.test_client()
            with patch.object(route_module, '_run_instances',
                              return_value=FailedRuns()), \
                    patch.object(
                        route_module.orchestration_run_runtime,
                        'create',
                    ) as runtime_create:
                response = await cli.post(
                    '/api/v1/orchestrations/tasks',
                    headers=self._hdr(),
                    json={'definition': self._def(), 'input': 'go'},
                )
            self.assertEqual(response.status_code, 500)
            self.assertTrue((await response.get_json())['ok'] is False)
            runtime_create.assert_not_called()

        _run(go())

    def test_read_failure_is_500_not_empty_or_missing(self):
        from unittest.mock import patch
        import routes.api_v1.orchestrations as route_module
        from lib.orchestration.run_service import RunServiceError

        class FailedReads:
            def list(self, **_filters):
                raise RunServiceError('database offline')

            def get(self, _run_id):
                raise RunServiceError('database offline')

        async def go():
            cli = self.fix.app.test_client()
            with patch.object(route_module, '_run_instances',
                              return_value=FailedReads()):
                listed = await cli.get(
                    '/api/v1/orchestrations/tasks', headers=self._hdr())
                fetched = await cli.get(
                    '/api/v1/orchestrations/tasks/run_any',
                    headers=self._hdr())
            self.assertEqual(listed.status_code, 500)
            self.assertEqual(fetched.status_code, 500)

        _run(go())

    def test_list_rejects_unknown_status_filter(self):
        async def go():
            cli = self.fix.app.test_client()
            response = await cli.get(
                '/api/v1/orchestrations/tasks?status=dnne',
                headers=self._hdr(),
            )
            self.assertEqual(response.status_code, 400)
            body = await response.get_json()
            self.assertIn('pending', body['statuses'])
            self.assertIn('done', body['statuses'])

        _run(go())

    def test_create_then_poll_events_to_completion(self):
        async def go():
            cli = self.fix.app.test_client()
            # 1. POST a durable run.
            definition = self._def()
            definition['nodes'][1]['params']['must_do'] = \
                '  inspect inputs\n\n publish result '
            r = await cli.post('/api/v1/orchestrations/tasks',
                               headers=self._hdr(),
                               json={'definition': definition, 'input': 'go'})
            self.assertEqual(r.status_code, 201)
            created = await r.get_json()
            self.assertTrue(created['ok'])
            self.assertEqual(created['definitionSource'], 'inline')
            run_id = created['start']['id']
            self.assertTrue(run_id.startswith('run_'))
            self.assertEqual(created['start'], {
                'format': 'tofu.orchestration.runtime-start/v1',
                'kind': 'durable',
                'id': run_id,
            })

            # 2. Poll /events until done. The worker runs via
            # asyncio.ensure_future on THIS loop, so the awaits below both
            # advance it and consume the cursor stream.
            cursor, status, seen, deadline = 0, 'pending', [], 0
            terminal_page = None
            while deadline < 100:   # ~10s cap
                deadline += 1
                requested_cursor = cursor
                r = await cli.get(
                    f'/api/v1/orchestrations/tasks/{run_id}/events?cursor={cursor}',
                    headers=self._hdr())
                self.assertEqual(r.status_code, 200)
                body = await r.get_json()
                self.assertTrue(body['ok'])
                self.assertEqual(body['format'], 'tofu.task-replay/v1')
                self.assertEqual(body['cursor']['requested'], requested_cursor)
                self.assertEqual(body['cursor']['next'], body['next_cursor'])
                self.assertFalse(body['cursor']['reset'])
                for ev in body['events']:
                    seen.append(ev['type'])
                cursor = body['next_cursor']
                status = body['status']
                if body['done']:
                    terminal_page = body
                    break
                self.assertNotIn('run', body)
                await asyncio.sleep(0.1)

            # 3. The run completed and the durable event log replays the
            # real engine vocabulary.
            self.assertEqual(status, 'done', f'did not finish; saw={seen}')
            self.assertIn('flow_start', seen)
            self.assertIn('step_complete', seen)
            self.assertIn('flow_complete', seen)
            self.assertIsNotNone(terminal_page)
            self.assertEqual(terminal_page['run']['id'], run_id)
            self.assertTrue(terminal_page['run']['terminal'])
            self.assertEqual(terminal_page['run']['status'], 'done')

            # 4. The header row persisted the terminal status + final.
            r = await cli.get(f'/api/v1/orchestrations/tasks/{run_id}',
                              headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            run = (await r.get_json())['run']
            self.assertEqual(run['status'], 'done')
            self.assertEqual(run['definition']['name'], 'TaskFlow')  # snapshot
            self.assertEqual(run['definition']['nodes'][1]['params'][
                'must_do'], ['inspect inputs', 'publish result'])

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

            # An over-advanced browser cursor is corrected by the producer;
            # keeping the caller's 1000+ value would strand future streams.
            r = await cli.get(
                f'/api/v1/orchestrations/tasks/{run_id}/events'
                f'?cursor={cursor + 1000}', headers=self._hdr())
            corrected = await r.get_json()
            self.assertEqual(corrected['format'], 'tofu.task-replay/v1')
            self.assertEqual(corrected['events'], [])
            self.assertEqual(corrected['next_cursor'], cursor)
            self.assertEqual(corrected['cursor'], {
                'requested': cursor + 1000,
                'next': cursor,
                'reset': True,
            })

            # Terminal state is a persistence fence, not just a UI hint.
            r = await cli.post(
                f'/api/v1/orchestrations/tasks/{run_id}/abort',
                headers=self._hdr(), json={})
            self.assertEqual(r.status_code, 409)
            conflict = await r.get_json()
            self.assertEqual(conflict['mutation']['resource_status'], 'done')
            self.assertEqual(
                conflict['mutation']['format'],
                'tofu.orchestration.mutation/v1',
            )
            self.assertEqual(conflict['mutation']['action'], 'abort_run')
            self.assertEqual(conflict['mutation']['reason'], 'terminal')
            self.assertFalse(conflict['mutation']['retryable'])

            # 6. The run shows up in the list, then deletes cleanly.
            r = await cli.get('/api/v1/orchestrations/tasks', headers=self._hdr())
            runs = (await r.get_json())['runs']
            self.assertTrue(any(x['id'] == run_id for x in runs))
            r = await cli.delete(f'/api/v1/orchestrations/tasks/{run_id}',
                                 headers=self._hdr())
            self.assertEqual(r.status_code, 200)
            deleted = await r.get_json()
            self.assertEqual(deleted['mutation']['action'], 'delete_run')
            self.assertEqual(deleted['mutation']['reason'], 'accepted')
            r = await cli.get(f'/api/v1/orchestrations/tasks/{run_id}',
                              headers=self._hdr())
            self.assertEqual(r.status_code, 404)

            r = await cli.get(
                f'/api/v1/orchestrations/tasks/{run_id}/events?cursor=9',
                headers=self._hdr())
            self.assertEqual(r.status_code, 404)
            missing = await r.get_json()
            self.assertEqual(missing['format'], 'tofu.task-replay/v1')
            self.assertFalse(missing['ok'])
            self.assertEqual(missing['error'], 'not_found')
            self.assertTrue(missing['done'])
            self.assertEqual(missing['events'], [])
            self.assertEqual(missing['cursor'], {
                'requested': 9, 'next': 9, 'reset': False,
            })
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
            run_id = (await r.get_json())['start']['id']

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
            run_id = (await r.get_json())['start']['id']

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

            # Active headers fence deletion so the still-running worker cannot
            # append events into a run record that has disappeared.
            r = await cli.delete(f'/api/v1/orchestrations/tasks/{run_id}',
                                 headers=self._hdr())
            self.assertEqual(r.status_code, 409)
            active_delete = await r.get_json()
            self.assertEqual(
                active_delete['mutation']['resource_status'], 'paused')
            self.assertEqual(active_delete['mutation']['action'], 'delete_run')
            self.assertEqual(active_delete['mutation']['reason'], 'active')
            r = await cli.get(f'/api/v1/orchestrations/tasks/{run_id}',
                              headers=self._hdr())
            self.assertEqual(r.status_code, 200)

            # Resolve the gate (approve) via the existing endpoint.
            r = await cli.post('/api/v1/orchestrations/run/human-approve',
                               headers=self._hdr(),
                               json={'requestId': req_id, 'approved': True})
            self.assertEqual(r.status_code, 200)
            gate_result = await r.get_json()
            self.assertEqual(gate_result['mutation']['action'], 'approve_gate')
            self.assertEqual(gate_result['mutation']['reason'], 'accepted')

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

    def test_expired_human_gate_uses_shared_mutation_contract(self):
        async def go():
            cli = self.fix.app.test_client()
            r = await cli.post(
                '/api/v1/orchestrations/run/human-input',
                headers=self._hdr(),
                json={'requestId': 'expired-gate', 'response': 'answer'},
            )
            self.assertEqual(r.status_code, 404)
            body = await r.get_json()
            self.assertFalse(body['ok'])
            self.assertEqual(
                body['mutation'],
                {
                    'format': 'tofu.orchestration.mutation/v1',
                    'ok': False,
                    'action': 'input_gate',
                    'reason': 'not_found',
                    'target_id': 'expired-gate',
                    'resource_status': '',
                    'resource_terminal': None,
                    'target_exists': False,
                    'retryable': False,
                    'reconcile_required': True,
                },
            )
        _run(go())

    def test_tasks_require_auth(self):
        async def go():
            r = await self.fix.app.test_client().get('/api/v1/orchestrations/tasks')
            self.assertEqual(r.status_code, 401)
        _run(go())


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_orchestrations.__main__')
    unittest.main()
