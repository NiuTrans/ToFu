"""Application commit ACK, frozen retries and physical writer quiescence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

from lib.agent_core.native_process import TerminalRequest
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy


def principal(owner=7):
    return PrincipalContext.user(subject_id='native-task-test', owner_user_id=owner)


def request(attempt=1, *, owner=7, text='answer'):
    return TerminalRequest(
        principal(owner), 'task', 'commit', attempt,
        json.dumps({'type': 'done', 'status': 'done', 'result': text}).encode(),
        b'{"invariants_satisfied":true}',
    )


class NativeTaskTerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.task = {'id': 'task', '_userId': 7, 'status': 'running'}
        self.projections = []

        def project(task, candidate):
            self.projections.append(candidate)
            task.update(status=candidate.event['status'], content=candidate.event['result'],
                        thinking='', finishReason='stop', _contentEpoch=1)

        self.policy = NativeTaskTerminalPolicy(
            principal=principal(), task=self.task, operation_id='commit',
            executor=self.executor, project=project, candidate_bytes=4096,
        )

    async def test_commit_debt_retries_without_reprojection_and_duplicates_do_not_write(self):
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=[False, True]) as commit:
            self.assertEqual(await self.policy.persist(request()), 'retry')
            self.assertTrue(self.task['_terminalPersistencePending'])
            self.assertEqual(await self.policy.persist(request(2)), 'accepted')
            self.assertEqual(await self.policy.persist(request(3)), 'accepted')
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(len(self.projections), 1)

    async def test_summary_bytes_participate_in_candidate_bound_and_retry_identity(self):
        from dataclasses import replace
        first=replace(request(),usage_summary_json=b'{"counts":{"x":3}}')
        changed=replace(request(2),usage_summary_json=b'{"counts":{"x":4}}')
        with patch('lib.tasks_pkg.native_terminal._persist',return_value=False):
            self.assertEqual(await self.policy.persist(first),'retry')
            with self.assertRaisesRegex(ValueError,'candidate changed'):await self.policy.persist(changed)
        oversized=replace(request(2),usage_summary_json=b'x'*5000)
        with self.assertRaisesRegex(ValueError,'exceeds bound'):await self.policy.persist(oversized)

    async def test_real_request_resources_settle_before_persistence_and_only_once(self):
        from lib.agent_core.execution_session import ExecutionSession,bind_model_route
        released=[]
        session=ExecutionSession(execution_id='task',kind='chat',owner_user_id=7)
        self.task['_executionSession']=session
        bind_model_route(session,lambda:released.append('route'))
        session.mark_dispatch_started()
        replies=iter([False,True])
        def persist(task):
            self.assertTrue(session.is_terminal)
            self.assertEqual(released,['route'])
            return next(replies)
        with patch('lib.tasks_pkg.native_terminal._persist',side_effect=persist):
            self.assertEqual(await self.policy.persist(request()),'retry')
            self.assertEqual(await self.policy.persist(request(2)),'accepted')
        self.assertEqual(released,['route'])

    async def test_failed_request_resource_never_acknowledges_success_or_releases_twice(self):
        from lib.agent_core.execution_session import ExecutionSession,bind_model_route
        released=[]
        session=ExecutionSession(execution_id='task',kind='chat',owner_user_id=7)
        self.task['_executionSession']=session
        def fail():
            released.append('route')
            raise OSError('fixture release failed')
        bind_model_route(session,fail)
        session.mark_dispatch_started()
        persisted=[];replies=iter([False,True])
        def persist(task):
            persisted.append(task['status'])
            return next(replies)
        with patch('lib.tasks_pkg.native_terminal._persist',side_effect=persist):
            self.assertEqual(await self.policy.persist(request()),'retry')
            self.assertEqual(await self.policy.persist(request(2)),'rejected')
            self.assertEqual(await self.policy.persist(request(3)),'rejected')
        self.assertFalse(self.policy.accepted)
        self.assertEqual(persisted,['error','error'])
        self.assertEqual(released,['route'])

    async def test_foreign_owner_operation_and_overflow_fail_before_projection(self):
        from dataclasses import replace
        cases = [request(owner=8), replace(request(), operation_id='other'),
                 replace(request(), task_id='other'), request(text='x' * 5000),
                 replace(request(), receipt_json=b'{"invariants_satisfied":false}')]
        with patch('lib.tasks_pkg.native_terminal._persist') as commit:
            for candidate in cases:
                with self.assertRaises(ValueError):
                    await self.policy.persist(candidate)
        commit.assert_not_called()
        self.assertEqual(self.projections, [])
        self.assertEqual(self.task['status'], 'running')

    async def test_terminal_retry_refuses_changed_frozen_financial_projection(self):
        self.task['apiRounds'] = [{'nativeCredits': {'total': 31}}]
        with patch('lib.tasks_pkg.native_terminal._persist', return_value=False) as commit:
            self.assertEqual(await self.policy.persist(request()), 'retry')
            self.task['apiRounds'][0]['nativeCredits']['total'] = 62
            with self.assertRaisesRegex(ValueError, 'projected outcome changed'):
                await self.policy.persist(request(2))
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(len(self.projections), 1)
        self.assertEqual(self.task['status'], 'done')

    async def test_changed_candidate_or_projected_result_cannot_retry_write(self):
        with patch('lib.tasks_pkg.native_terminal._persist', return_value=False) as commit:
            await self.policy.persist(request())
            with self.assertRaises(ValueError):
                await self.policy.persist(request(2, text='changed'))
            self.task['content'] = 'competing finalizer'
            with self.assertRaises(ValueError):
                await self.policy.persist(request(2))
        self.assertEqual(commit.call_count, 1)

    async def test_recovery_fence_is_rejected_and_never_retried(self):
        def commit(task):
            task['_workspaceDeliveryOwnershipRejected'] = True
            return False
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=commit) as writer:
            self.assertEqual(await self.policy.persist(request()), 'rejected')
            self.assertEqual(await self.policy.persist(request(2)), 'rejected')
        self.assertEqual(writer.call_count, 1)

    async def test_storage_error_retains_debt_and_cause_without_reprojecting(self):
        failure = OSError('fixture storage failure')
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=[failure, True]):
            with self.assertRaises(OSError) as caught:
                await self.policy.persist(request())
            self.assertIs(caught.exception, failure)
            self.assertTrue(self.task['_terminalPersistencePending'])
            self.assertTrue(self.task['_terminalPersistenceRetryReady'])
            self.assertEqual(await self.policy.persist(request(2)), 'accepted')
        self.assertEqual(len(self.projections), 1)

    async def test_projection_failure_never_repeats_partial_side_effects(self):
        def project(task, _request):
            task['content'] = 'partial projection'
            raise OSError('fixture projection failure')
        self.policy.project = project
        with patch('lib.tasks_pkg.native_terminal._persist') as writer:
            with self.assertRaises(OSError):
                await self.policy.persist(request())
            with self.assertRaises(RuntimeError) as caught:
                await self.policy.persist(request(2))
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(self.task['_terminalPersistencePending'])
        self.assertFalse(self.task['_terminalPersistenceRetryReady'])
        writer.assert_not_called()

    async def test_failed_settlement_can_persist_error_but_cannot_claim_success(self):
        from dataclasses import replace
        candidate = replace(request(),
            event_json=b'{"type":"error","status":"error","error":{"kind":"settlement"}}',
            receipt_json=b'{"invariants_satisfied":false}')
        def project(task, request):
            task.update(status=request.event['status'], error=request.event['error'])
        self.policy.project = project
        with patch('lib.tasks_pkg.native_terminal._persist', return_value=True):
            self.assertEqual(await self.policy.persist(candidate), 'accepted')
        self.assertEqual(self.task['status'], 'error')

    async def test_repeated_cancel_joins_physical_commit_and_keeps_serialization(self):
        entered, release = threading.Event(), threading.Event()
        def commit(_task):
            entered.set()
            release.wait(5)
            return True
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=commit) as writer:
            first = asyncio.create_task(self.policy.persist(request()))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(self.policy.persist(request(2)))
            await asyncio.sleep(0.01)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(await second, 'accepted')
        self.assertEqual(writer.call_count, 1)


if __name__ == '__main__':
    # Use the shared pytest storage and lifecycle isolation for direct runs.
    import sys as _runner_sys
    import pytest
    raise SystemExit(pytest.main([__file__, *_runner_sys.argv[1:]]))


class NativeTerminalProjectionTests(unittest.TestCase):
    def candidate(self, event):
        return TerminalRequest(principal(), 'task', 'commit', 1,
            json.dumps(event).encode(), b'{"invariants_satisfied":true}')

    def test_done_uses_final_message_and_actual_usage_without_publication(self):
        from lib.tasks_pkg.native_terminal import project_native_terminal
        task={'id':'task','_userId':7,'status':'running','thinking':'old thinking'}
        event={'type':'done','status':'done','result':{'messages':[
            {'role':'user','content':'question'},
            {'role':'assistant','content':'answer','reasoning_content':'actual thinking'}],
            'usage':[{'prompt_tokens':3,'completion_tokens':2},{'prompt_tokens':5,'completion_tokens':4}]}}
        with patch('lib.tasks_pkg.manager._events.append_event') as publish:
            project_native_terminal(task,self.candidate(event))
        publish.assert_not_called()
        self.assertEqual((task['status'],task['content'],task['thinking'],task['finishReason']),
            ('done','answer','actual thinking','stop'))
        self.assertEqual(task['usage']['prompt_tokens'],8)
        self.assertEqual(task['usage']['completion_tokens'],6)
        self.assertEqual(task['messages'],event['result']['messages'])
        self.assertTrue(task['_messages_include_current_run'])

    def test_invalid_success_refuses_before_mutation(self):
        from lib.tasks_pkg.native_terminal import project_native_terminal
        for final in [{'role':'user','content':'question'},
                      {'role':'assistant','content':{'text':'answer'}},
                      {'role':'assistant','content':'answer','tool_calls':[{}]}]:
            task={'id':'task','_userId':7,'status':'running','content':'partial'}
            with self.assertRaises(ValueError):
                project_native_terminal(task,self.candidate({'type':'done','status':'done',
                    'result':{'messages':[final],'usage':[]}}))
            self.assertEqual(task,{'id':'task','_userId':7,'status':'running','content':'partial'})

    def test_paid_round_usage_is_not_counted_again(self):
        from lib.tasks_pkg.native_terminal import project_native_terminal
        usage={'prompt_tokens':3,'completion_tokens':2}
        task={'id':'task','_userId':7,'status':'running','usage':usage,
              '_nativeFrozenCostProjection':True,'cost':{'costUsd':.0001}}
        with patch('lib.cost.accumulate_usage_into',side_effect=AssertionError('Frozen paid usage reprocessed')):
            project_native_terminal(task,self.candidate({'type':'done','status':'done','result':{
                'messages':[{'role':'assistant','content':'answer'}],'usage':[dict(usage)]}}))
        self.assertIs(task['usage'],usage)
        self.assertEqual(task['cost'],{'costUsd':.0001})

    def test_native_summary_replaces_python_addition(self):
        from dataclasses import replace
        from lib.tasks_pkg.native_terminal import project_native_terminal
        event={'type':'done','status':'done','result':{'messages':[{'role':'assistant','content':'answer'}],
            'usage':[{'prompt_tokens':3},{'prompt_tokens':5}]}}
        summary={'profile':'tofu.additive-usage/u64-f64-v1','counts':{'prompt_tokens':8}}
        candidate=replace(self.candidate(event),usage_summary_json=json.dumps(summary).encode())
        task={'id':'task','_userId':7,'status':'running'}
        with patch('lib.cost.accumulate_usage_into',side_effect=AssertionError('Python accumulation called')):
            project_native_terminal(task,candidate)
        self.assertEqual(task['usage'],summary['counts'])
        self.assertEqual(task['messages'],event['result']['messages'])

    def test_invalid_native_summary_refuses_before_task_mutation(self):
        from dataclasses import replace
        from lib.tasks_pkg.native_terminal import project_native_terminal
        event={'type':'done','status':'done','result':{'messages':[{'role':'assistant','content':'answer'}],'usage':[]}}
        for counts in [{'x':True},{'_private':1},{'x':-1},{'opaque':{}},{'x':float('inf')},{'cache_creation':{'_private':1}}]:
            task={'id':'task','_userId':7,'status':'running'};before=dict(task)
            candidate=replace(self.candidate(event),usage_summary_json=json.dumps({'profile':'tofu.additive-usage/u64-f64-v1','counts':counts}).encode())
            with self.assertRaises(ValueError):project_native_terminal(task,candidate)
            self.assertEqual(task,before)

    def test_error_and_abort_retain_history_without_rewriting_paid_usage(self):
        from lib.tasks_pkg.native_terminal import project_native_terminal
        from lib.agent_core.abort_signal import AbortSignal
        for status in ['error','aborted']:
            usage={'prompt_tokens':31}
            task={'id':'task','_userId':7,'status':'running','messages':[],
                  'usage':usage,'content':'partial','abort_event':AbortSignal()}
            history=[{'role':'assistant','tool_calls':[{'id':'call','type':'function',
                'function':{'name':'lookup','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'call','content':'observed effect'}]
            event={'type':status,'status':status,'execution':{'messages':history,'usage':[{'prompt_tokens':1}]}}
            if status=='error':event['error']={'kind':'internal','detail':'provider failed'}
            project_native_terminal(task,self.candidate(event))
            self.assertEqual(task['status'],status)
            self.assertEqual(task['messages'],history)
            self.assertIs(task['usage'],usage)
            self.assertEqual(task['content'],'partial')
            self.assertTrue(task['_messages_include_current_run'])

    def test_invalid_observed_history_refuses_before_mutation(self):
        from lib.tasks_pkg.native_terminal import project_native_terminal
        for execution in [None,{'messages':[]},{'messages':[False],'usage':[]},
                          {'messages':[],'usage':[],'unknown':True},
                          {'messages':[],'usage':[],'llm_rounds':True}]:
            task={'id':'task','_userId':7,'status':'running','messages':[],'content':'partial'}
            original=dict(task)
            event={'type':'error','status':'error','error':{'kind':'internal','detail':'failed'},'execution':execution}
            with self.assertRaises(ValueError):project_native_terminal(task,self.candidate(event))
            self.assertEqual(task,original)
