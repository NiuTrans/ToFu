"""Hermetic host admission and sealed image boundaries; no model/network calls."""

import asyncio
import time
import fcntl
import hashlib
import os
import threading
from unittest.mock import patch
from pathlib import Path
import unittest

from lib.agent_core.native_process import (
    Budget,
    NativeConfigurationError,
    NativeProcessClient,
    NativeProcessError,
    _seal,
    _RoundControl,
    _TerminalControl,
    NativePersistenceError,
    NativeRoundError,
    _GET_SEALS,
    _WRITE_SEAL,
)
from lib.identity import PrincipalContext


def budget():
    return Budget(1, 16 * 1024 * 1024, 65536, 65536, 0.2)


def principal():
    return PrincipalContext.user(subject_id="native-host-test", owner_user_id=7)


class NativeHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_and_tool_catalog_refuse_before_binary_acquisition(self):
        client = NativeProcessClient(
            binary=Path("/does-not-exist"),
            sha256="0" * 64,
            protocol_version="fixture/v1",
            principal=principal(),
            budget=budget(),
        )
        for config in [
            dict(owner_user_id=8),
            dict(
                owner_user_id=7,
                version="fixture/v1",
                type="configure",
                tools={"x": {"permission": "write"}},
            ),
        ]:
            with self.assertRaises(NativeConfigurationError):
                await client.run(config, callbacks={}, working_directory=Path("/tmp"))
        self.assertEqual(client.active_runs, 0)
        await client.close()

    async def test_checksum_failure_and_protocol_exit_release_admission(self):
        binary = Path("/usr/bin/true")
        for digest in ["0" * 64, hashlib.sha256(binary.read_bytes()).hexdigest()]:
            client = NativeProcessClient(
                binary=binary,
                sha256=digest,
                protocol_version="fixture/v1",
                principal=principal(),
                budget=budget(),
            )
            with self.assertRaises(NativeProcessError):
                await client.run(
                    {
                        "owner_user_id": 7,
                        "version": "fixture/v1",
                        "type": "configure",
                        "tools": {},
                        "task_id": "one",
                    },
                    callbacks={},
                    working_directory=Path("/tmp"),
                )
            self.assertEqual(client.active_runs, 0)
            await client.close()

    async def test_sealed_execution_image_cannot_be_changed(self):
        binary = Path("/usr/bin/true")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        image = await asyncio.to_thread(_seal, binary, digest, 16 * 1024 * 1024)
        try:
            with self.assertRaises(OSError):
                os.write(image.fileno(), b"corrupt")
            self.assertEqual(
                fcntl.fcntl(image.fileno(), _GET_SEALS) & _WRITE_SEAL, _WRITE_SEAL
            )
            process = await asyncio.create_subprocess_exec(
                f"/proc/self/fd/{image.fileno()}", pass_fds=(image.fileno(),)
            )
            self.assertEqual(await process.wait(), 0)
        finally:
            image.close()

    async def test_all_runs_above_legacy_hint_own_shared_image_until_joined_close(self):
        binary = Path('/usr/bin/true')
        client = NativeProcessClient(binary=binary,
            sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            protocol_version='fixture/v1', principal=principal(),
            budget=Budget(0, 16 * 1024 * 1024, 65536, 65536, .2))
        entered, release = threading.Event(), threading.Event()
        descriptors = []
        real_seal = _seal
        def seal(*args):
            entered.set()
            assert release.wait(5)
            descriptor = real_seal(*args)
            descriptors.append(descriptor)
            return descriptor
        runs = []
        with patch('lib.agent_core.native_process._seal', side_effect=seal), patch(
            'lib.agent_core.native_process.asyncio.create_subprocess_exec',
            wraps=asyncio.create_subprocess_exec) as spawn:
            try:
                runs = [asyncio.create_task(client.run(
                    {'owner_user_id': 7, 'version': 'fixture/v1', 'type': 'configure',
                     'tools': {}, 'task_id': f'demand-{index}'}, callbacks={},
                    working_directory=Path('/tmp'))) for index in range(32)]
                assert await asyncio.to_thread(entered.wait, 2)
                for _ in range(20):
                    if client.active_runs == 32:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(client.active_runs, 32)
                close = asyncio.create_task(client.close())
                await asyncio.sleep(.02)
                self.assertFalse(close.done())
                release.set()
                await close
                results = await asyncio.gather(*runs, return_exceptions=True)
                self.assertTrue(all(isinstance(item, asyncio.CancelledError) for item in results))
                self.assertEqual(client.active_runs, 0)
                self.assertEqual(spawn.call_count, 0)
                self.assertEqual(len(descriptors), 1)
                self.assertTrue(descriptors[0].closed)
            finally:
                release.set()
                await client.close()
                await asyncio.gather(*runs, return_exceptions=True)

    async def test_cancelled_image_acquisition_is_joined_without_starting_process(self):
        binary = Path("/usr/bin/true")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        client = NativeProcessClient(
            binary=binary,
            sha256=digest,
            protocol_version="fixture/v1",
            principal=principal(),
            budget=budget(),
        )
        entered = threading.Event()
        release = threading.Event()
        real_seal = _seal

        def slow_seal(*args):
            entered.set()
            assert release.wait(2)
            return real_seal(*args)

        with (
            patch("lib.agent_core.native_process._seal", side_effect=slow_seal),
            patch(
                "lib.agent_core.native_process.asyncio.create_subprocess_exec",
                wraps=asyncio.create_subprocess_exec,
            ) as spawn,
        ):
            run = asyncio.create_task(
                client.run(
                    {
                        "owner_user_id": 7,
                        "version": "fixture/v1",
                        "type": "configure",
                        "tools": {},
                        "task_id": "one",
                    },
                    callbacks={},
                    working_directory=Path("/tmp"),
                )
            )
            assert await asyncio.to_thread(entered.wait, 1)
            run.cancel()
            await asyncio.sleep(0)
            run.cancel()
            await asyncio.sleep(0)
            self.assertEqual(client.active_runs, 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await run
            self.assertEqual(spawn.await_count, 0)
            self.assertEqual(client.active_runs, 0)
        await client.close()

    async def test_structure_and_shutdown_budgets_are_checked(self):
        for value in [0, float("nan"), float("inf"), True]:
            with self.assertRaises(NativeConfigurationError):
                Budget(1, 100, 100, 100, value)
        client = NativeProcessClient(
            binary=Path("/does-not-exist"),
            sha256="0" * 64,
            protocol_version="fixture/v1",
            principal=principal(),
            budget=Budget(1, 100, 100, 100, 0.2),
        )
        with self.assertRaises(NativeConfigurationError):
            await client.run(
                {
                    "owner_user_id": 7,
                    "version": "fixture/v1",
                    "type": "configure",
                    "tools": {},
                    "task_id": "one",
                    "large": "x" * 1000,
                },
                callbacks={},
                working_directory=Path("/tmp"),
            )
        self.assertEqual(client.active_runs, 0)
        await client.close()


class RoundHostTests(unittest.IsolatedAsyncioTestCase):
    def control(self, policy):
        client = NativeProcessClient(
            binary=Path("/does-not-exist"),
            sha256="0" * 64,
            protocol_version="fixture/v1",
            principal=principal(),
            budget=budget(),
        )
        return _RoundControl(client, policy, "task")

    def frame(self, kind="round_request"):
        return dict(
            version="fixture/v1",
            type=kind,
            owner_user_id=7,
            task_id="task",
            round=0,
            history_messages=1,
            request_id="one" if kind == "round_request" else "one-settle",
            evidence={"kind": "returned", "usage": None},
        )

    async def test_prepare_precedes_reserve_and_invalid_history_never_reserves(self):
        for output in [[{'role': 'user', 'content': 'prepared'}], [7], 'invalid', [{'content': 'x' * (budget().frame_bytes + 1)}]]:
            seen, replies = [], []
            class Policy:
                async def prepare(self, request):
                    seen.append('prepare')
                    self_test.assertEqual(request.messages, [{'role': 'user', 'content': 'original'}])
                    return output
                async def reserve(self, request):
                    seen.append('reserve')
                    self_test.assertEqual(request.messages, output)
                    return True
            self_test = self
            control = self.control(Policy())
            control.prepare_history = True
            async def send(process, kind, request_id, **values):
                replies.append(values)
            control.send = send
            frame = self.frame();frame['messages'] = [{'role': 'user', 'content': 'original'}]
            valid = isinstance(output, list) and output[0].get('content') == 'prepared' if isinstance(output, list) and isinstance(output[0], dict) else False
            if valid:
                await control.handle(frame, None)
                self.assertEqual(seen, ['prepare', 'reserve'])
                self.assertEqual(replies[0]['messages'], output)
            else:
                await control.handle(frame, None)
                self.assertIsNotNone(control.preparation_failure)
                self.assertEqual(replies, [{'approved': False, 'preparation_failed': True}])
                self.assertEqual(seen, ['prepare'])
                self.assertIsNone(control.pending)

    async def test_cancelled_preparation_joins_before_no_reservation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        class Policy:
            async def prepare(self, request):
                entered.set()
                try:
                    await release.wait()
                finally:
                    await release.wait()
                    seen.append('joined')
            async def reserve(self, request):
                seen.append('reserved')
                return True
        control = self.control(Policy());control.prepare_history = True
        frame = self.frame();frame['messages'] = [{'role': 'user', 'content': 'original'}]
        run = asyncio.create_task(control.handle(frame, None))
        await entered.wait();run.cancel();await asyncio.sleep(0)
        self.assertFalse(run.done());release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertEqual(seen, ['joined'])
        self.assertIsNone(control.pending)

    async def test_expired_preparation_never_sends_old_decision_or_reserves(self):
        seen=[]
        class Policy:
            async def prepare(self, request):
                raise TimeoutError('expired fixture')
            async def reserve(self, request):
                seen.append('reserve');return True
        control=self.control(Policy());control.prepare_history=True
        async def send(*args,**kwargs):seen.append('send')
        control.send=send
        frame=self.frame();frame.update(messages=[{'role':'user','content':'input'}],deadline_monotonic_ns=time.monotonic_ns()-1)
        await control.handle(frame,None)
        self.assertIsInstance(control.preparation_failure,TimeoutError)
        self.assertEqual(seen,[])
        for value in [True,-1,1.5,18446744073709551616]:
            other=self.control(Policy());other.prepare_history=True
            frame['deadline_monotonic_ns']=value
            with self.assertRaises(NativeProcessError):await other.handle(frame,None)

    async def test_cancelled_reservation_joins_and_abandons_exactly_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        class Policy:
            async def reserve(self, request):
                entered.set()
                await release.wait()
                return True

            async def settle(self, result):
                seen.append(result.evidence)

        control = self.control(Policy())
        run = asyncio.create_task(control.handle(self.frame(), None))
        await entered.wait()
        run.cancel()
        await asyncio.sleep(0)
        self.assertFalse(run.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run
        await control.abandon()
        await control.abandon()
        self.assertEqual(seen, [{"kind": "abandoned"}])

    async def test_failed_settlement_rejects_ack_without_repeating_accounting(self):
        seen, replies = [], []

        class Policy:
            async def reserve(self, request):
                return True

            async def settle(self, result):
                seen.append(result.evidence)
                raise OSError("accounting fixture")

        control = self.control(Policy())

        async def send(process, kind, request_id, **values):
            replies.append((kind, values))

        control.send = send
        await control.handle(self.frame(), None)
        with self.assertRaises(NativeRoundError) as raised:
            await control.handle(self.frame("round_settled"), None)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        await control.abandon()
        self.assertEqual(len(seen), 1)
        self.assertEqual(replies[-1], ("round_ack", {"accepted": False}))

    async def test_cancelled_settlement_joins_before_positive_ack(self):
        entered, release = asyncio.Event(), asyncio.Event()
        replies = []

        class Policy:
            async def reserve(self, request):
                return True

            async def settle(self, result):
                entered.set()
                await release.wait()

        control = self.control(Policy())

        async def send(process, kind, request_id, **values):
            replies.append((kind, values))

        control.send = send
        await control.handle(self.frame(), None)
        run = asyncio.create_task(control.handle(self.frame("round_settled"), None))
        await entered.wait()
        run.cancel()
        await asyncio.sleep(0)
        self.assertEqual(len(replies), 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertEqual(replies[-1], ("round_ack", {"accepted": True}))
        self.assertIsNone(control.pending)

    async def test_ack_timeout_remains_bounded_after_cancellation(self):
        class Policy:
            async def reserve(self, request):
                return True

            async def settle(self, result):
                return None

        control = self.control(Policy())

        async def blocked(process, kind, request_id, **values):
            if kind == "round_ack":
                await asyncio.Event().wait()

        control.send = blocked
        await control.handle(self.frame(), None)
        run = asyncio.create_task(control.handle(self.frame("round_settled"), None))
        await asyncio.sleep(0.01)
        run.cancel()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(run, 1)
        self.assertIsNone(control.pending)


class TerminalHostTests(unittest.IsolatedAsyncioTestCase):
    def control(self, policy):
        client = NativeProcessClient(
            binary=Path("/does-not-exist"),
            sha256="0" * 64,
            protocol_version="fixture/v1",
            principal=principal(),
            budget=budget(),
        )
        return _TerminalControl(
            client, policy, "task", {"operation_id": "commit", "attempts": 2}
        )

    def frame(self, attempt=1):
        return dict(
            version="fixture/v1",
            type="terminal_request",
            owner_user_id=7,
            task_id="task",
            request_id="commit",
            attempt=attempt,
            event={"type": "done", "status": "done", "result": {"text": "frozen"}},
            receipt={"invariants_satisfied": True},
        )

    def process(self):
        class Input:
            def __init__(self):
                self.frames = []

            def write(self, data):
                self.frames.append(__import__("json").loads(data))

            async def drain(self):
                pass

        return type("Process", (), {"stdin": Input()})()

    async def test_retry_and_mutable_callback_view_cannot_change_frozen_terminal(self):
        seen = []

        class Policy:
            async def persist(self, request):
                seen.append(request)
                request.event["result"]["text"] = "caller mutation"
                return "retry" if request.attempt == 1 else "accepted"

        control = self.control(Policy())
        process = self.process()
        await control.handle(self.frame(), process)
        with self.assertRaises(NativePersistenceError):
            control.confirm(self.frame()["event"])
        await control.handle(self.frame(2), process)
        control.confirm(self.frame()["event"])
        self.assertEqual(seen[0].event, seen[1].event)
        self.assertTrue(process.stdin.frames[0]["retry"])
        self.assertTrue(process.stdin.frames[1]["accepted"])

    async def test_changed_retry_and_wrong_owner_fail_before_persistence(self):
        calls = []

        class Policy:
            async def persist(self, request):
                calls.append(request)
                return "retry"

        control = self.control(Policy())
        process = self.process()
        value = self.frame()
        value["owner_user_id"] = 8
        with self.assertRaises(NativePersistenceError):
            await control.handle(value, process)
        await control.handle(self.frame(), process)
        value = self.frame(2)
        value["event"]["status"] = "error"
        with self.assertRaises(NativePersistenceError):
            await control.handle(value, process)
        self.assertEqual(len(calls), 1)

    async def test_cancelled_persistence_is_joined_before_ack(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Policy:
            async def persist(self, request):
                entered.set()
                await release.wait()
                return "accepted"

        control = self.control(Policy())
        process = self.process()
        run = asyncio.create_task(control.handle(self.frame(), process))
        await entered.wait()
        run.cancel()
        await asyncio.sleep(0)
        self.assertFalse(run.done())
        self.assertEqual(process.stdin.frames, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertTrue(control.accepted)
        self.assertTrue(process.stdin.frames[0]["accepted"])

    async def test_callback_failure_rejects_persistence_without_success(self):
        class Policy:
            async def persist(self, request):
                raise OSError("fixture commit failure")

        control = self.control(Policy())
        process = self.process()
        with self.assertRaises(NativePersistenceError) as caught:
            await control.handle(self.frame(), process)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertFalse(process.stdin.frames[0]["accepted"])
        self.assertFalse(control.accepted)


if __name__ == "__main__":
    unittest.main()


class ProgressHostTests(unittest.IsolatedAsyncioTestCase):
    def control(self, observe):
        from lib.agent_core.native_process import ProgressLimits
        class Policy:
            limits = ProgressLimits(no_progress_rounds=2)
        policy = Policy()
        policy.observe = observe
        control = RoundHostTests().control(object())
        from lib.agent_core.native_process import _ProgressControl
        control.progress = _ProgressControl(control, policy)
        control.settled_round = 0
        control.next_round = 1
        control.settled_progress_ready = True
        return control

    @staticmethod
    def frame():
        return dict(version='fixture/v1', type='progress_request', request_id='progress-0', owner_user_id=7, task_id='task', round=0, tool_calls=1)

    @staticmethod
    def observation():
        return dict(worldVersion='v1', evidenceIds=['visible'], resultEvidenceComplete=True,
                    verification='', nonretryableFailureSignatures=[])

    async def test_progress_requires_settlement_and_one_exact_owned_observation(self):
        seen, replies = [], []
        async def observe(request):
            seen.append(request)
            return self.observation()
        control = self.control(observe)
        async def send(*args, **kwargs): replies.append(kwargs)
        control.send = send
        control.settled_round = None
        with self.assertRaises(NativeProcessError): await control.handle(self.frame(), None)
        control.settled_round = 0
        invalid = self.frame(); invalid['owner_user_id'] = 8
        with self.assertRaises(NativeProcessError): await control.handle(invalid, None)
        await control.handle(self.frame(), None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].task_id, 'task')
        self.assertEqual(replies, [dict(accepted=True, observation=self.observation())])
        with self.assertRaises(NativeProcessError): await control.handle(self.frame(), None)

    async def test_invalid_observation_returns_failure_without_inventing_evidence(self):
        replies = []
        async def observe(request):
            result = self.observation(); result['resultEvidenceComplete'] = 'yes'
            return result
        control = self.control(observe)
        async def send(*args, **kwargs): replies.append(kwargs)
        control.send = send
        await control.handle(self.frame(), None)
        self.assertFalse(replies[0]['accepted'])
        self.assertFalse(replies[0]['observation']['resultEvidenceComplete'])
        self.assertIsInstance(control.progress.failure, TypeError)

    async def test_cancel_signals_and_joins_observer_before_reply(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests, replies = [], []
        async def observe(request):
            requests.append(request); entered.set()
            await release.wait(); finished.set()
            return self.observation()
        control = self.control(observe)
        async def send(*args, **kwargs):
            self.assertTrue(finished.is_set()); replies.append(kwargs)
        control.send = send
        run = asyncio.create_task(control.handle(self.frame(), None))
        await entered.wait(); run.cancel()
        await asyncio.sleep(0)
        self.assertFalse(run.done())
        self.assertTrue(requests[0].cancelled.is_set())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await run
        self.assertFalse(replies[0]['accepted'])

    async def test_deadline_joins_observer_and_never_sends_stale_decision(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        async def observe(request):
            entered.set()
            while not request.cancelled.is_set(): await asyncio.sleep(.001)
            finished.set()
            return self.observation()
        control = self.control(observe)
        control.settled_deadline = time.monotonic_ns() + 20_000_000
        async def send(*args, **kwargs): self.fail('expired decision was sent')
        control.send = send
        await control.handle(self.frame(), None)
        self.assertTrue(entered.is_set()); self.assertTrue(finished.is_set())
        self.assertIsInstance(control.progress.failure, TimeoutError)


class CheckpointHostTests(unittest.IsolatedAsyncioTestCase):
    def control(self, policy):
        from lib.agent_core.native_process import _CheckpointControl
        rounds = RoundHostTests().control(object())
        rounds.checkpoint = _CheckpointControl(rounds, policy, 'epoch')
        return rounds

    def frame(self, action, number=0, state=None):
        return dict(version='fixture/v1', type='checkpoint_request', request_id=f'{action}-{number}',
                    owner_user_id=7, task_id='task', epoch='epoch', binding='bound', round=number, action=action,
                    **({'state':state} if state is not None else {}))

    def state(self, number=2):
        return dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=7,
                    task_id='task', epoch='epoch', binding='bound', next_round=number)

    async def test_resume_rounds_and_complete_require_owned_settlement(self):
        test=self;seen=[];replies=[]
        class Policy:
            binding='bound'
            async def load(self, request): seen.append(('load',request.round)); return test.state()
            async def begin(self, request): seen.append(('begin',request.round))
            async def complete(self, request):
                seen.append(('complete',request.round));assert request.state['next_round']==3;return 'suspend'
        control=self.control(Policy())
        async def send(*args,**kwargs):replies.append(kwargs)
        control.send=send
        await control.handle(self.frame('load'),None)
        self.assertEqual(control.next_round,2)
        await control.handle(self.frame('begin',2),None)
        with self.assertRaises(NativeProcessError): await control.handle(self.frame('complete',2,self.state(3)),None)
        control.settled_round=2;control.next_round=3;control.settled_progress_ready=True
        await control.handle(self.frame('complete',2,self.state(3)),None)
        self.assertEqual(seen,[('load',0),('begin',2),('complete',2)])
        self.assertEqual(replies[-1],dict(accepted=True,disposition='suspend'))
        with self.assertRaises(NativeProcessError):await control.handle(self.frame('load'),None)

    async def test_callback_fault_returns_refusal_and_preserves_original_failure(self):
        failure=OSError('checkpoint store unavailable');replies=[]
        class Policy:
            binding='bound'
            async def load(self, request):raise failure
        control=self.control(Policy())
        async def send(*args,**kwargs):replies.append(kwargs)
        control.send=send
        await control.handle(self.frame('load'),None)
        self.assertIs(control.checkpoint.failure,failure)
        self.assertEqual(replies,[dict(accepted=False,disposition='continue')])

    async def test_cancel_joins_owned_checkpoint_work_before_refusal(self):
        entered,release,finished=asyncio.Event(),asyncio.Event(),asyncio.Event();requests=[];replies=[]
        class Policy:
            binding='bound'
            async def load(self, request):
                requests.append(request);entered.set();await release.wait();finished.set();return None
        control=self.control(Policy())
        async def send(*args,**kwargs):
            self.assertTrue(finished.is_set());replies.append(kwargs)
        control.send=send
        run=asyncio.create_task(control.handle(self.frame('load'),None));await entered.wait();run.cancel();await asyncio.sleep(0)
        self.assertTrue(requests[0].cancelled.is_set());self.assertFalse(run.done());release.set()
        with self.assertRaises(asyncio.CancelledError):await run
        self.assertFalse(replies[0]['accepted'])

    async def test_expired_checkpoint_reply_is_never_sent_to_terminal_carrier(self):
        finished=asyncio.Event()
        class Policy:
            binding='bound'
            async def load(self, request):
                while not request.cancelled.is_set():await asyncio.sleep(.001)
                finished.set();return None
        control=self.control(Policy())
        async def send(*args,**kwargs):self.fail('expired checkpoint decision sent')
        control.send=send
        value=self.frame('load');value['deadline_monotonic_ns']=time.monotonic_ns()+20_000_000
        await control.handle(value,None)
        self.assertTrue(finished.is_set());self.assertIsInstance(control.checkpoint.failure,TimeoutError)


class CheckpointTerminalHostTests(unittest.IsolatedAsyncioTestCase):
    control = TerminalHostTests.control
    frame = TerminalHostTests.frame
    process = TerminalHostTests.process
    def fenced_control(self, seal, persist, *, suspended=False):
        from types import SimpleNamespace
        control = self.control(SimpleNamespace(persist=persist))
        control.checkpoint = SimpleNamespace(
            policy=SimpleNamespace(seal=seal), epoch='epoch', binding='bound', suspended=suspended,
        )
        return control

    def fenced_frame(self, attempt=1, *, suspended=False):
        frame = self.frame(attempt)
        frame['checkpoint_suspended'] = suspended
        if suspended:
            frame['event']['type'] = frame['event']['status'] = 'aborted'
        return frame

    async def test_seal_retry_skips_persist_and_preserves_candidate(self):
        calls = []
        async def seal(request):
            self.assertEqual((request.epoch, request.binding), ('epoch', 'bound'))
            calls.append(('seal', request.terminal.attempt))
            return 'retry' if request.terminal.attempt == 1 else 'accepted'
        async def persist(request):
            calls.append(('persist', request.attempt))
            return 'accepted'
        control = self.fenced_control(seal, persist)
        process = self.process()
        await control.handle(self.fenced_frame(), process)
        self.assertFalse(control.checkpoint_sealed)
        await control.handle(self.fenced_frame(2), process)
        self.assertEqual(calls, [('seal', 1), ('seal', 2), ('persist', 2)])
        self.assertTrue(process.stdin.frames[0]['retry'])
        control.confirm(self.frame()['event'])

    async def test_task_retry_does_not_repeat_accepted_seal(self):
        calls = []
        async def seal(request):
            calls.append('seal')
            return 'accepted'
        async def persist(request):
            calls.append('persist')
            return 'retry' if request.attempt == 1 else 'accepted'
        control = self.fenced_control(seal, persist)
        await control.handle(self.fenced_frame(), self.process())
        await control.handle(self.fenced_frame(2), self.process())
        self.assertEqual(calls, ['seal', 'persist', 'persist'])

    async def test_seal_failure_keeps_cause_and_never_persists(self):
        cause = OSError('seal delivery unknown')
        async def seal(request):
            raise cause
        async def persist(request):
            self.fail('task persistence preceded checkpoint fence')
        control = self.fenced_control(seal, persist)
        process = self.process()
        with self.assertRaises(NativePersistenceError) as caught:
            await control.handle(self.fenced_frame(), process)
        self.assertIs(caught.exception.__cause__, cause)
        self.assertFalse(process.stdin.frames[0]['accepted'])

    async def test_pause_marker_requires_committed_suspend_and_frozen_retry(self):
        calls = []
        async def seal(request):
            calls.append(request.suspended)
            return 'retry'
        async def persist(request):
            self.fail('retry may not persist')
        control = self.fenced_control(seal, persist)
        with self.assertRaises(NativePersistenceError):
            await control.handle(self.fenced_frame(suspended=True), self.process())
        control.checkpoint.suspended = True
        await control.handle(self.fenced_frame(suspended=True), self.process())
        changed = self.fenced_frame(2, suspended=True)
        changed['checkpoint_suspended'] = False
        with self.assertRaises(NativePersistenceError):
            await control.handle(changed, self.process())
        self.assertEqual(calls, [True])

    async def test_cancelled_seal_is_joined_before_task_persistence_and_ack(self):
        entered, release = asyncio.Event(), asyncio.Event()
        requests, calls = [], []
        async def seal(request):
            requests.append(request)
            entered.set()
            await release.wait()
            self.assertTrue(request.cancelled.is_set())
            calls.append('seal')
            return 'accepted'
        async def persist(request):
            calls.append('persist')
            return 'accepted'
        control = self.fenced_control(seal, persist)
        process = self.process()
        run = asyncio.create_task(control.handle(self.fenced_frame(), process))
        await entered.wait()
        run.cancel()
        await asyncio.sleep(0)
        self.assertFalse(run.done())
        self.assertEqual(process.stdin.frames, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertEqual(calls, ['seal', 'persist'])
        self.assertTrue(process.stdin.frames[0]['accepted'])

    async def test_cleanup_error_after_pause_seals_instead_of_preserving_resume(self):
        decisions = []
        async def seal(request):
            decisions.append(request.suspended)
            return 'accepted'
        async def persist(request):
            return 'accepted'
        control = self.fenced_control(seal, persist, suspended=True)
        frame = self.fenced_frame()
        frame['event']['type'] = frame['event']['status'] = 'error'
        await control.handle(frame, self.process())
        self.assertEqual(decisions, [False])

    async def test_invalid_seal_decision_refuses_before_task_persistence(self):
        for result in [None, True, 'continue', 'rejected']:
            async def seal(request):
                return result
            async def persist(request):
                self.fail('invalid or rejected seal reached task persistence')
            control = self.fenced_control(seal, persist)
            with self.assertRaises(NativePersistenceError):
                await control.handle(self.fenced_frame(), self.process())
            self.assertFalse(control.checkpoint_sealed)

    async def test_composed_checkpoint_requires_seal_before_binary_acquisition(self):
        from types import SimpleNamespace
        async def nothing(request):
            return None
        client = self.control(SimpleNamespace()).client
        settings = dict(owner_user_id=7, version='fixture/v1', type='configure',
                        tools={}, task_id='task', epoch='epoch', terminal_persistence=dict(
                            operation_id='commit', attempts=2, ack_ms=100, frame_bytes=65536))
        policy = SimpleNamespace(binding='bound', load=nothing, begin=nothing, complete=nothing)
        with patch('lib.agent_core.native_process._seal') as acquire:
            with self.assertRaisesRegex(NativeConfigurationError, 'seal callback'):
                await client.run(settings, callbacks={}, working_directory=Path('/tmp'),
                                 round_policy=SimpleNamespace(reserve=nothing, settle=nothing),
                                 checkpoint_policy=policy, terminal_policy=SimpleNamespace(persist=nothing))
            acquire.assert_not_called()
        self.assertEqual(client.active_runs, 0)
        await client.close()
