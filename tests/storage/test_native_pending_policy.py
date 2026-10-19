"""Real selected-store recovery through the Python pending-tool policy.

The fixture domain owns an operation receipt; this tests the callback contract,
not application write_file proof or automatic ROOT/Flow takeover.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

import pytest

from lib.agent_core.native_process import CheckpointRequest
from lib.identity import PrincipalContext
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from tofu_db.embedded import EmbeddedRuntime

pytestmark = pytest.mark.api


def _snapshot():
    return dict(
        format="tofu.native-agent-checkpoint/v1",
        owner_user_id=7,
        task_id="pending-policy",
        epoch="epoch",
        binding="binding",
        next_round=1,
        tool_calls=1,
        tools_pending=True,
        usage=[dict(total_tokens=23, cost_usd=0.01)],
        round_limit=3,
        tool_limit=3,
        no_progress_limit=0,
        failure_limit=0,
        messages=[
            dict(role="user", content="publish"),
            dict(
                role="assistant",
                tool_calls=[
                    dict(
                        id="write-1",
                        type="function",
                        function=dict(name="write_file", arguments="{}"),
                    )
                ],
            ),
        ],
        progress=dict(
            codec="sha256-token-sequence/v1",
            last_calls=None,
            last_world="",
            last_evidence=None,
            last_failure=None,
            no_progress_streak=0,
            failure_streak=0,
        ),
    )


def _policy(client, executor, recovery, restore, *, recover=False):
    principal = PrincipalContext.user(
        subject_id="pending-policy-fixture", owner_user_id=7
    )
    policy = NativeCheckpointPolicy(
        principal=principal,
        task={"id": "pending-policy", "_userId": 7},
        storage=client,
        executor=executor,
        epoch="epoch",
        binding="binding",
        token=uuid.uuid4().hex,
        header_bytes=4096,
        snapshot_bytes=1048576,
        identity_bytes=1024,
        io_timeout_ms=30000,
        restore=restore,
        recover_abandoned=recover,
        tool_recovery=recovery,
    )
    return policy, CheckpointRequest(
        principal, "pending-policy", "load", 0, "epoch", "binding", threading.Event()
    )


def _crash_writer(values):
    directory = Path(values["directory"])
    operation = uuid.uuid4().hex

    class Domain:
        async def capture(self, request):
            return dict(request.state, tool_recovery={"operation_id": operation})

        async def committed(self, request):
            assert request.state["tool_recovery"]["operation_id"] == operation

        async def reconcile(self, request):
            raise AssertionError("writer must not reconcile")

    async def no_restore(*args):
        raise AssertionError("new writer must not restore")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with EmbeddedRuntime(
            Path(values["library"]),
            directory / "authority",
            owner_id=7,
            tenant_id=3,
            secret=b"x" * 32,
        ) as runtime:
            assert runtime.build_identity()["source_sha256"] == values["source"]
            client = EmbeddedStorageV2Client(
                runtime, b"x" * 32, owner_id=7, tenant_id=3
            )

            async def invoke():
                policy, request = _policy(client, executor, Domain(), no_restore)
                assert await policy.load(request) is None
                await policy.begin(replace(request, action="begin"))
                await policy.pending(
                    replace(request, action="pending", state=_snapshot())
                )
                # The fixture domain publishes only after the checkpoint ACK.
                with (directory / "publication").open("ab") as stream:
                    stream.write(b"published\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                with (directory / "receipt").open("w") as stream:
                    stream.write(operation)
                    stream.flush()
                    os.fsync(stream.fileno())
                (directory / "ready").write_text(operation)
                await asyncio.Event().wait()

            asyncio.run(invoke())


def test_python_policy_reopens_dead_writer_and_keeps_exact_domain_metadata(tmp_path):
    values = {
        key: os.environ.get(name)
        for key, name in (
            ("library", "TOFU_CHECKPOINT_CABI_TEST_LIBRARY"),
            ("binary", "TOFU_CHECKPOINT_CABI_TEST_BINARY"),
            ("source", "TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256"),
        )
    }
    if not all(values.values()):
        pytest.skip("explicit matching private checkpoint library and engine required")
    if sys.platform != "linux":
        pytest.skip("kernel process-death proof requires Linux")
    values["directory"] = str(tmp_path)
    (tmp_path / "authority").mkdir()
    subprocess.run(
        [values["binary"], "init", "--data-dir", str(tmp_path / "authority")],
        check=True,
        capture_output=True,
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values))
    process = None
    try:
        with (tmp_path / "writer.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import json,sys; from tests.storage.test_native_pending_policy import _crash_writer; "
                    "_crash_writer(json.load(open(sys.argv[1])))",
                    str(config),
                ],
                cwd=Path(__file__).resolve().parents[2],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            expires = time.monotonic() + 30
            while not (tmp_path / "ready").exists():
                assert process.poll() is None, (tmp_path / "writer.log").read_text()[
                    -2500:
                ]
                assert time.monotonic() < expires, (
                    "writer never reached the paid pending boundary"
                )
                time.sleep(0.02)
            process.kill()
            assert process.wait(timeout=10) == -signal.SIGKILL
        queries = []

        class Domain:
            async def capture(self, request):
                raise AssertionError("recovery cannot dispatch a new capture")

            async def committed(self, request):
                raise AssertionError("recovery cannot arm the old tool")

            async def reconcile(self, request):
                operation = request.state["tool_recovery"]["operation_id"]
                assert (tmp_path / "receipt").read_text() == operation
                assert (tmp_path / "publication").read_bytes() == b"published\n"
                queries.append(operation)
                return [
                    dict(call_id="write-1", content="fixture domain proved publication")
                ]

        restored = []

        async def restore(request, state):
            restored.append(state)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with EmbeddedRuntime(
                Path(values["library"]),
                tmp_path / "authority",
                owner_id=7,
                tenant_id=3,
                secret=b"x" * 32,
            ) as runtime:
                assert runtime.build_identity()["source_sha256"] == values["source"]
                client = EmbeddedStorageV2Client(
                    runtime, b"x" * 32, owner_id=7, tenant_id=3
                )

                async def recover():
                    policy, request = _policy(
                        client, executor, Domain(), restore, recover=True
                    )
                    try:
                        saved = await policy.load(request)
                        assert saved["usage"] == _snapshot()["usage"] and restored == [
                            saved
                        ]
                        with pytest.raises(ValueError, match="unresolved"):
                            await policy.complete(
                                replace(request, action="complete", state={})
                            )
                        observations = await policy.reconcile(
                            replace(request, action="reconcile", state=saved)
                        )
                        completed = {
                            key: value
                            for key, value in saved.items()
                            if key not in {"tools_pending", "tool_recovery"}
                        }
                        completed["messages"] = [
                            *saved["messages"],
                            dict(
                                role="tool",
                                tool_call_id=observations[0]["call_id"],
                                content=observations[0]["content"],
                            ),
                        ]
                        assert (
                            await policy.complete(
                                replace(request, action="complete", state=completed)
                            )
                            == "continue"
                        )
                        await policy.begin(replace(request, action="begin", round=1))
                    finally:
                        await policy.close()

                asyncio.run(recover())
                client.close()
        assert queries == [(tmp_path / "ready").read_text()]
        assert (tmp_path / "publication").read_bytes() == b"published\n"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
