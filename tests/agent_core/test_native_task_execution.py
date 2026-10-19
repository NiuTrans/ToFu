"""Application composition refuses before route consumption and binds commit."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
import asyncio
from unittest.mock import AsyncMock, patch

from lib.tasks_pkg.native_execution import run_native_task, project_native_text, NativeTaskRounds, prepare_native_application_template
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
from tests.agent_core.test_native_task_terminal import principal


class NativeTaskExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown)
        self.client = SimpleNamespace(principal=principal(), budget=SimpleNamespace(frame_bytes=4096))
        from lib.agent_core.abort_signal import AbortSignal
        self.task = {'id': 'task', '_userId': 7, 'status': 'running','abort_event':AbortSignal()}
        self.config = {'owner_user_id': 7, 'task_id': 'task', 'terminal_persistence': {
            'operation_id': 'commit', 'attempts': 2, 'ack_ms': 500, 'frame_bytes': 4096,
        }}

    async def run_task(self, config):
        return await run_native_task(
            self.client, object(), config, task=self.task, executor=self.executor,
            project_terminal=lambda *_args: None, prepare_template=lambda _meta: {},
            callbacks={}, working_directory=Path('/tmp'),
        )

    async def test_default_context_prepares_and_admits_before_reservation(self):
        from lib.agent_core.native_process import RoundRequest, RoundSettlement
        from lib.tasks_pkg.native_context import NativeContextPolicy
        self.task['config']={'model':'fixture'}
        captured=[]
        async def route(*args, **kwargs):
            rounds=kwargs['round_policy']
            self.assertIsInstance(rounds.policy,NativeContextPolicy)
            captured.append(rounds.policy)
            kwargs['bind_round_policy']({'owner_user_id':7,'logical_model':'fixture',
                'wire_model':'fixture-wire','context_window':131072,'provider_id':'fixture'},
                {'max_tokens':512}, {})
            request=RoundRequest(principal(),'task','request',0,1,[{'role':'user','content':'hello'}])
            messages=await rounds.prepare(request)
            self.assertEqual(messages[0]['content'],'hello')
            self.assertEqual(self.task['_lastPromptAdmission']['action'],'admit')
            self.assertTrue(await rounds.reserve(request))
            await rounds.settle(RoundSettlement(request,{'kind':'returned'}))
            return 'result'
        with patch('lib.tasks_pkg.native_execution.run_native_route',side_effect=route), \
                patch('lib.tasks_pkg.manager.append_event'):
            self.assertEqual(await self.run_task(self.config),'result')
        self.assertTrue(captured[0]._closed)
        self.assertIsNone(captured[0]._bound)

    async def test_default_model_account_uses_shared_parent_cap_and_exact_settlement(self):
        from lib.agent_core.execution_budget import ExecutionBudget
        from lib.agent_core.native_model_control import ModelRequest, ModelSettlement
        from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
        root={'id':'root','_userId':7,'config':{'executionLimits':{'max_tokens':50,'max_tool_calls':None,'deadline':None}}}
        budget=ExecutionBudget(root)
        self.task['config']={'executionLimits':{'max_tokens':None,'max_tool_calls':None,'deadline':None}}
        budget.attach(self.task);self.task['_executionBudget']=budget
        config=dict(self.config,model_control={'ack_ms':500},model_counter={})
        async def route(*args,**kwargs):
            policy=kwargs['model_policy']
            self.assertIsInstance(policy,NativeModelAccountPolicy)
            self.assertIs(policy.budget,budget)
            request=ModelRequest(principal(),'task',1,20,100,1,threading.Event())
            self.assertEqual(await policy.reserve(request),30)
            await policy.dispatch(request)
            await policy.settle(ModelSettlement(request,b'{"prompt_tokens":20,"completion_tokens":10}',False))
            return 'result'
        with patch('lib.tasks_pkg.native_execution.run_native_route',side_effect=route):
            self.assertEqual(await self.run_task(config),'result')
        self.assertEqual(budget.consumption_snapshot(),(30,0,False))
        budget.close(self.task)
        self.assertEqual(budget.consumption_snapshot(),(30,0,True))
        self.assertEqual(budget.continuity_snapshot()['pending_model_requests'],0)

    async def test_default_model_account_refuses_missing_foreign_and_second_authority_before_route(self):
        from lib.agent_core.execution_budget import ExecutionBudget
        base=dict(self.config,model_control={'ack_ms':500},model_counter={})
        with patch('lib.tasks_pkg.native_execution.run_native_route',new_callable=AsyncMock) as route:
            with self.assertRaises(ValueError):await self.run_task(base)
            foreign={'id':'foreign','_userId':8,'config':{'executionLimits':{'max_tokens':50,'max_tool_calls':None,'deadline':None}}}
            self.task['_executionBudget']=ExecutionBudget(foreign)
            with self.assertRaises(ValueError):await self.run_task(base)
            for config in [dict(base,token_admission={}),dict(base,model_counter=None),
                    dict(base,model_control={'pricing':True}),dict(base,model_control=None)]:
                with self.assertRaises(ValueError):await self.run_task(config)
        route.assert_not_awaited()

    async def test_counter_binding_uses_logical_model_without_loading_python_encoder(self):
        from lib.tasks_pkg.native_execution import prepare_native_application_counter
        from lib.agent_core.native_content_contract import MODEL_COUNTER_SCHEMA
        import copy
        bounds=dict(input_bytes=1048576,text_bytes=1048576,tool_json_bytes=1048576,
                    depth=128,worker_stack_bytes=1048576)
        before=copy.deepcopy(bounds)
        with patch('lib.token_counter.tiktoken_counter._get_encoder',side_effect=AssertionError('Python tokenizer must not load')):
            for model,encoding in [('GPT-5','o200k_base'),('gpt-4o','o200k_base'),
                    ('o3','o200k_base'),('gpt-4','cl100k_base'),('fixture','cl100k_base')]:
                for field in ('max_tokens','max_completion_tokens'):
                    result=prepare_native_application_counter({'logical_model':model,'wire_model':'alias'},
                        {'model':'alias',field:512},bounds)
                    self.assertEqual(result['encoding'],encoding)
                    self.assertEqual(result['output_field'],field)
                    self.assertEqual(set(result),set(MODEL_COUNTER_SCHEMA['required']))
        self.assertEqual(bounds,before)
        explicit=dict(bounds,encoding='cl100k_base',output_field='max_tokens')
        self.assertEqual(prepare_native_application_counter({'logical_model':'gpt-5'},
            {'max_tokens':512},explicit),explicit)
        for profile,template in [(dict(bounds,depth=129),{'max_tokens':512}),
                (dict(bounds,worker_stack_bytes=1),{'max_tokens':512}),
                (dict(bounds,input_bytes=True),{'max_tokens':512}),
                (dict(bounds,encoding='unsupported'),{'max_tokens':512}),
                (dict(bounds,output_field='max_completion_tokens'),{'max_tokens':512}),
                (dict(bounds,extra=1),{'max_tokens':512}), (bounds,{}),
                (bounds,{'max_tokens':512,'max_completion_tokens':512})]:
            with self.assertRaises(ValueError):
                prepare_native_application_counter({'logical_model':'fixture'},template,profile)

    async def test_completion_snapshot_once_before_commit_retry_without_history_append(self):
        import json
        from lib.agent_core.native_process import TerminalRequest
        messages=[{'role':'user','content':'question'},{'role':'assistant','content':'answer'}]
        snapshots=[]
        async def route(*args,**kwargs):
            kwargs['bind_round_policy']({'owner_user_id':7,'logical_model':'fixture',
                'wire_model':'wire-alias','context_window':131072,'provider_id':'fixture'},
                {'model':'wire-alias','max_tokens':512},{'lookup':{'description':'Lookup','arguments_schema':{'type':'object'}}})
            terminal=kwargs['terminal_policy']
            event=json.dumps({'type':'done','status':'done','result':{'messages':messages,'usage':[]}}).encode()
            first=TerminalRequest(principal(),'task','commit',1,event,b'{"invariants_satisfied":true}')
            retry=TerminalRequest(principal(),'task','commit',2,event,b'{"invariants_satisfied":true}')
            self.assertEqual(await terminal.persist(first),'retry')
            self.assertEqual(len(snapshots),1)
            self.assertEqual(await terminal.persist(retry),'accepted')
            self.assertEqual(len(snapshots),1)
            return 'result'
        def publish(task,event):snapshots.append(event)
        from lib.tasks_pkg.native_terminal import project_native_terminal
        with patch('lib.tasks_pkg.native_execution.run_native_route',side_effect=route), \
                patch('lib.tasks_pkg.manager.append_event',side_effect=publish), \
                patch('lib.tasks_pkg.native_terminal._persist',side_effect=[False,True]):
            await run_native_task(self.client,object(),self.config,task=self.task,executor=self.executor,
                project_terminal=project_native_terminal,prepare_template=lambda _: {},callbacks={},working_directory=Path('/tmp'))
        self.assertEqual(self.task['messages'],messages)
        snapshot=snapshots[0]
        self.assertEqual((snapshot['type'],snapshot['kind'],snapshot['roundNum']),('messages_snapshot','state','final'))
        self.assertEqual(snapshot['model'],'fixture')
        self.assertEqual(snapshot['messages'][-1]['content'],'answer')
        self.assertEqual(snapshot['tools'][0]['function']['name'],'lookup')

    async def test_owner_profile_and_task_refuse_before_routed_execution(self):
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            for config in [{}, dict(self.config, task_id='foreign'),
                           dict(self.config, owner_user_id=8),
                           dict(self.config, terminal_persistence={}),
                           dict(self.config, terminal_persistence=dict(
                               self.config['terminal_persistence'], frame_bytes=4097))]:
                with self.assertRaises(ValueError):
                    await self.run_task(config)
            self.task['_userId'] = 8
            with self.assertRaises(ValueError):
                await self.run_task(self.config)
        run.assert_not_awaited()

    async def test_explicit_nullable_ack_reaches_route_and_malformed_policy_refuses_before_it(self):
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as route:
            route.return_value='owned-result'
            for ack in (None, 1, 5000):
                config=dict(self.config,terminal_persistence=dict(self.config['terminal_persistence'],ack_ms=ack))
                self.assertEqual(await self.run_task(config),'owned-result')
            self.assertEqual(route.await_count,3)
            route.reset_mock()
            for ack in (False, 0, -1, '5000', 1.5, 1<<64, 'missing'):
                profile=dict(self.config['terminal_persistence'],ack_ms=ack)
                if ack=='missing':profile.pop('ack_ms')
                with self.assertRaisesRegex(ValueError,'owned terminal admission'):
                    await self.run_task(dict(self.config,terminal_persistence=profile))
            route.assert_not_awaited()

    async def test_default_generation_template_matches_canonical_intent_and_wire_alias(self):
        from lib.llm.body import build_body
        from tests.llm.test_reasoning_contract_compiler import profile
        import copy
        schema={'type':'json_schema','json_schema':{'name':'answer','schema':{'type':'object'}}}
        for depth in ('off','lowest','high','max'):
            self.task['config']={'model':'future','maxTokens':512,'temperature':.2,
                                 'thinkingDepth':depth,'responseFormat':copy.deepcopy(schema)}
            before=copy.deepcopy(self.task['config'])
            destination=profile();destination['sampling']={'omit':['temperature']}
            metadata={'owner_user_id':7,'logical_model':'future','wire_model':'provider-alias','max_output_tokens':256,
                      'inference_profile':destination}
            async def route(*args,**kwargs):
                template=kwargs['prepare_template'](metadata)
                oracle=build_body('future',[{'role':'user','content':'question'}],max_tokens=256,
                    temperature=.2,thinking_depth=depth,preset='future',response_format=schema,
                    inference_profile=destination)
                expected={key:value for key,value in oracle.items() if not key.startswith('_') and key not in {'messages','stream'}}
                expected['model']='provider-alias'
                self.assertEqual(template,expected)
                template['response_format']['json_schema']['schema']['type']='array'
                return 'result'
            with patch('lib.tasks_pkg.native_execution.run_native_route',side_effect=route):
                self.assertEqual(await run_native_task(self.client,object(),self.config,task=self.task,
                    executor=self.executor,project_terminal=lambda *_:None,
                    callbacks={},working_directory=Path('/tmp')),'result')
            self.assertEqual(self.task['config'],before)
            self.assertEqual(self.task['model'],'future')

    async def test_default_generation_refuses_foreign_profile_and_model_change_without_mutation(self):
        import copy
        from tests.llm.test_reasoning_contract_compiler import profile
        self.task.update(model='previous')
        metadata={'owner_user_id':7,'logical_model':'future','wire_model':'alias','inference_profile':profile()}
        for config,changed in (({},dict(metadata,owner_user_id=8)),
                               ({'thinkingDepth':'high'},dict(metadata,inference_profile={'unknown':True})),
                               ({'preset':'qwen'},metadata)):
            self.task['config']=config
            before=copy.deepcopy(config)
            with self.assertRaises(ValueError):
                prepare_native_application_template(self.task,changed)
            self.assertEqual(self.task['config'],before)
            self.assertEqual(self.task['model'],'previous')

    async def test_legacy_event_refuses_before_route_consumption(self):
        import threading
        self.task['abort_event'] = threading.Event()
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            with self.assertRaises(ValueError):
                await self.run_task(self.config)
        run.assert_not_awaited()

    async def test_default_stream_publishes_content_and_thinking_after_stop(self):
        from lib.agent_core.native_process import TextDelta
        self.task.update(content='prefix', thinking='', _contentEpoch=4,
                         content_lock=threading.Lock())
        self.task['abort_event'].set()
        observed = []
        def publish(task, event):
            with task['content_lock']:
                observed.append((dict(event), task['content'], task['thinking']))
        async def route(*args, **kwargs):
            await kwargs['on_text'](TextDelta(principal(), 'task', 1, 'content', '中文😀'))
            await kwargs['on_text'](TextDelta(principal(), 'task', 2, 'reasoning', '思考'))
        with patch('lib.tasks_pkg.native_execution.run_native_route', side_effect=route), \
             patch('lib.tasks_pkg.manager.append_event', side_effect=publish):
            await self.run_task(dict(self.config, text_progress=True))
        self.assertEqual(self.task['content'], 'prefix中文😀')
        self.assertEqual(self.task['thinking'], '思考')
        self.assertEqual(self.task['_contentEpoch'], 4)
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0][0]['content'], '中文😀')
        self.assertEqual(observed[1][0]['thinking'], '思考')

    async def test_default_text_refuses_foreign_or_invalid_before_mutation(self):
        from lib.agent_core.native_process import TextDelta
        self.task.update(content='paid', thinking='', content_lock=threading.Lock())
        with patch('lib.tasks_pkg.manager.append_event') as publish:
            for task_id, channel, text in [('foreign', 'content', 'x'),
                                           ('task', 'tool', 'x'), ('task', 'content', '')]:
                with self.assertRaises(ValueError):
                    project_native_text(self.task, TextDelta(principal(), task_id, 1, channel, text))
            self.task['_userId'] = 8
            with self.assertRaises(ValueError):
                project_native_text(self.task, TextDelta(principal(), 'task', 1, 'content', 'x'))
        publish.assert_not_called()
        self.assertEqual(self.task['content'], 'paid')

    async def test_text_delivery_failure_retains_received_partial_text(self):
        from lib.agent_core.native_process import TextDelta
        self.task.update(content='', content_lock=threading.Lock())
        with patch('lib.tasks_pkg.manager.append_event', side_effect=OSError('delivery debt')):
            with self.assertRaises(OSError):
                project_native_text(self.task, TextDelta(principal(), 'task', 1, 'content', 'paid'))
        self.assertEqual(self.task['content'], 'paid')

    async def test_round_delivery_closes_tools_before_next_model_and_terminal_once(self):
        from lib.agent_core.native_process import RoundRequest, RoundSettlement
        from tests.agent_core.test_native_task_terminal import request as terminal_request
        events = []
        async def route(*args, **kwargs):
            rounds = kwargs['round_policy']
            kwargs['bind_round_policy']({'owner_user_id':7,'logical_model':'fixture',
                'wire_model':'fixture-wire','context_window':131072,'provider_id':'fixture'},
                {'max_tokens':512}, {})
            for number in (0, 1):
                req = RoundRequest(principal(), 'task', f'round-{number}', number, 1)
                self.assertTrue(await rounds.reserve(req))
                await rounds.settle(RoundSettlement(req, {'kind': 'returned'}))
            terminal = kwargs['terminal_policy']
            self.assertEqual(await terminal.persist(terminal_request()), 'retry')
            self.assertEqual(await terminal.persist(terminal_request(2)), 'accepted')
        def project(task, candidate):
            task.update(status='done', content='answer', thinking='', finishReason='stop')
        with patch('lib.tasks_pkg.native_execution.run_native_route', side_effect=route), \
             patch('lib.tasks_pkg.manager.append_event', side_effect=lambda task,event: events.append(dict(event))), \
             patch('lib.tasks_pkg.native_terminal._persist', side_effect=[False, True]):
            await run_native_task(self.client, object(), self.config, task=self.task,
                executor=self.executor, project_terminal=project, prepare_template=lambda _: {},
                callbacks={}, working_directory=Path('/tmp'))
        self.assertEqual([(event['type'], event['roundNum']) for event in events],
                         [('round_start', 0), ('round_end', 0), ('round_start', 1), ('round_end', 1)])
        self.assertEqual([event['reason'] for event in events if event['type']=='round_end'], ['tools','stop'])

    async def test_refused_round_and_foreign_owner_publish_no_boundary(self):
        from lib.agent_core.native_process import RoundRequest
        policy = SimpleNamespace(reserve=AsyncMock(return_value=False), settle=AsyncMock())
        rounds = NativeTaskRounds(principal=principal(), task=self.task, executor=self.executor, policy=policy)
        with patch('lib.tasks_pkg.manager.append_event') as publish:
            self.assertFalse(await rounds.reserve(RoundRequest(principal(), 'task', 'r', 0, 1)))
            with self.assertRaises(ValueError):
                await rounds.reserve(RoundRequest(principal(8), 'task', 'foreign', 0, 1))
        publish.assert_not_called()
        policy.reserve.assert_awaited_once()

    async def test_owner_stop_withdraws_open_round_without_blocking_terminal_commit(self):
        rounds=NativeTaskRounds(principal=principal(),task=self.task,executor=self.executor,policy=None)
        rounds.open_round=3
        self.task['abort_event'].set()
        with patch('lib.tasks_pkg.manager.append_event',side_effect=AssertionError('cancelled presentation must not publish')):
            rounds.finish('aborted')
            rounds.finish('aborted')
        self.assertIsNone(rounds.open_round)

    async def test_round_delivery_failure_retains_its_boundary_and_cause(self):
        rounds=NativeTaskRounds(principal=principal(),task=self.task,executor=self.executor,policy=None)
        rounds.open_round=3
        failure=OSError('fixture event delivery failure')
        with patch('lib.tasks_pkg.manager.append_event',side_effect=failure):
            with self.assertRaises(OSError) as caught:rounds.finish('stop')
        self.assertIs(caught.exception,failure)
        self.assertEqual(rounds.open_round,3)

    async def test_round_publication_cancellation_joins_worker_and_paid_settlement(self):
        from lib.agent_core.native_process import RoundRequest, RoundSettlement
        policy = SimpleNamespace(reserve=AsyncMock(return_value=True), settle=AsyncMock(return_value=None))
        rounds = NativeTaskRounds(principal=principal(), task=self.task, executor=self.executor, policy=policy)
        entered, release = threading.Event(), threading.Event()
        def publish(*args):
            entered.set()
            assert release.wait(3)
        req = RoundRequest(principal(), 'task', 'r', 0, 1)
        with patch('lib.tasks_pkg.manager.append_event', side_effect=publish):
            work = asyncio.create_task(rounds.reserve(req))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                work.cancel()
                await asyncio.sleep(0)
                self.assertFalse(work.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.task['status'] = 'aborted'
        result = RoundSettlement(req, {'kind':'returned'})
        await rounds.settle(result)
        policy.settle.assert_awaited_once_with(result)

    async def test_explicit_task_commit_is_bound_to_existing_native_route(self):
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            run.return_value = object()
            self.assertIs(await self.run_task(self.config), run.return_value)
        policy = run.call_args.kwargs['terminal_policy']
        self.assertIsInstance(policy, NativeTaskTerminalPolicy)
        self.assertIs(policy.task, self.task)
        self.assertIs(policy.executor, self.executor)
        self.assertEqual(policy.operation_id, 'commit')

    async def test_priced_entry_preserves_context_owner_and_existing_account(self):
        from lib.agent_core.execution_budget import ExecutionBudget
        from lib.tasks_pkg.native_context import NativeContextPolicy
        from lib.tasks_pkg.native_pricing import NativeRoutePricing
        from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
        self.task['config'] = {'executionLimits': {'max_tokens': 1000, 'max_tool_calls': None, 'deadline': None}}
        self.task['_executionBudget'] = ExecutionBudget(self.task)
        delivery = SimpleNamespace(reserve=AsyncMock(return_value=True), settle=AsyncMock())
        context = NativeContextPolicy(principal=principal(), task=self.task,
            executor=self.executor, round_policy=delivery)
        resolver = NativeRoutePricing(principal=principal(), margin=0)
        config = dict(self.config, model_control={'pricing': True})
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            await run_native_task(self.client, object(), config, task=self.task,
                executor=self.executor, project_terminal=lambda *_: None,
                prepare_template=lambda _: {}, callbacks={}, working_directory=Path('/tmp'),
                round_policy=context, pricing_resolver=resolver)
        ports = run.call_args.kwargs
        self.assertIs(ports['model_policy'].budget, self.task['_executionBudget'])
        self.assertIs(ports['model_policy'].pricing_resolver, resolver)
        self.assertIsInstance(ports['round_policy'].policy, NativeTaskCostPolicy)
        self.assertIs(ports['round_policy'].policy.policy, context)
        self.assertEqual(ports['round_policy'].prepare, context.prepare)
        self.assertEqual(ports['bind_round_policy'], context.bind_route)
        self.assertTrue(context._closed)
        self.assertEqual(config, dict(self.config, model_control={'pricing': True}))

    async def test_priced_entry_refuses_duplicate_or_missing_owners_before_route(self):
        from lib.tasks_pkg.native_pricing import NativeRoutePricing
        resolver = NativeRoutePricing(principal=principal(), margin=0)
        delivery = SimpleNamespace(reserve=AsyncMock(), settle=AsyncMock())
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            for control, policy, model in [({}, delivery, None), ({'pricing': True}, None, None),
                                           ({'pricing': True}, delivery, object())]:
                with self.assertRaisesRegex(ValueError, 'one model and cost owner|owned existing tree account'):
                    await run_native_task(self.client, object(), dict(self.config, model_control=control),
                        task=self.task, executor=self.executor, project_terminal=lambda *_: None,
                        prepare_template=lambda _: {}, callbacks={}, working_directory=Path('/tmp'),
                        round_policy=policy, model_policy=model, pricing_resolver=resolver)
        run.assert_not_awaited()

    async def test_application_deadline_can_shorten_but_never_extend_task_expiry(self):
        self.task['config'] = {'executionLimits': {
            'max_tokens': None, 'max_tool_calls': None, 'deadline': 100.125}}
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            for supplied, expected in [(None, 100125), (200000, 100125), (90000, 90000), (0, 0)]:
                config = dict(self.config)
                if supplied is not None:
                    config['deadline_unix_ms'] = supplied
                await self.run_task(config)
                self.assertEqual(run.call_args.args[2]['deadline_unix_ms'], expected)
                self.assertEqual(config.get('deadline_unix_ms'), supplied)
            for supplied in [None, True, -1, 18446744073709551616]:
                with self.assertRaisesRegex(ValueError, 'absolute deadline'):
                    await self.run_task(dict(self.config, deadline_unix_ms=supplied))

    async def test_checkpoint_cleanup_preserves_primary_failure_and_terminal_ack(self):
        from tests.agent_core.test_native_checkpoint_policy import SelectedStorage
        from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
        checkpoint = NativeCheckpointPolicy(
            principal=principal(), task=self.task, storage=SelectedStorage(), executor=self.executor,
            epoch='epoch', binding='binding', token='lease', header_bytes=4096,
            snapshot_bytes=65536, identity_bytes=1024, io_timeout_ms=5000,
            restore=AsyncMock(),
        )
        config = dict(self.config, epoch='epoch')
        async def run(**kwargs):
            return await run_native_task(self.client, object(), config, task=self.task,
                executor=self.executor, project_terminal=lambda *_: None,
                prepare_template=lambda _: {}, callbacks={}, working_directory=Path('/tmp'),
                checkpoint_policy=checkpoint, **kwargs)
        checkpoint.close = AsyncMock(side_effect=OSError('cleanup unavailable'))
        primary = RuntimeError('native execution failed')
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as route:
            route.side_effect = primary
            with self.assertRaises(RuntimeError) as caught:
                await run()
        self.assertIs(caught.exception, primary)
        self.assertEqual(primary.__notes__, ['native checkpoint cleanup failed: OSError'])
        checkpoint.close.assert_awaited_once_with(terminal_saved=False)
        checkpoint.close = AsyncMock()
        async def accepted(*args, **kwargs):
            kwargs['terminal_policy']._accepted = True
            return 'committed'
        with patch('lib.tasks_pkg.native_execution.run_native_route', side_effect=accepted):
            self.assertEqual(await run(), 'committed')
        checkpoint.close.assert_awaited_once_with(terminal_saved=True)


if __name__ == '__main__':
    # Use the shared pytest storage and lifecycle isolation for direct runs.
    import sys as _runner_sys
    import pytest
    raise SystemExit(pytest.main([__file__, *_runner_sys.argv[1:]]))
