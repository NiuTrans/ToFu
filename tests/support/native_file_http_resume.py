"""Resume a crashed file ROOT through real v4 admission in a private application.

Called only by the owned SIGKILL probe. Uses the supplied selected authority,
normal application/task composition and loopback provider; no live user server.
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from unittest.mock import patch
import uuid


async def resume_file_root_http(values, storage, turn, principal, settings):
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.app.app_assembly import create_application
    from lib.model_routing.repository import OwnerBoundary
    from lib.server.server_loop_runtime import ServingLoopRuntime
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from lib.turn_lifecycle import get_turn, get_attempt
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository
    from tests.tasks_pkg.test_native_application import profile

    directory = Path(values["directory"])
    archive = Path(values["archive"])
    owner = principal.require_owner(context="file recovery HTTP fixture")
    repo = Repository()
    repo.boundary = OwnerBoundary.create(owner)
    repo.document["connections"][1]["enabled"] = False
    repo.document["connections"][0]["base_url"] = values["endpoint"]
    budget = Budget(1, 16 << 20, 1 << 20, 1 << 20, 5)
    manifest = verify(archive)
    image = NativeArchiveImage(
        archive=archive,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        application_version=manifest["build"]["application_version"],
        budget=budget,
    )
    restored = []
    created = []
    checkpoints = []

    def pricing(actor, task):
        created.append(task)
        return NativeRoutePricing(
            principal=actor,
            margin=0,
            lookup=lambda owner, model, provider, **kwargs: {
                "input": 2.5,
                "output": 10,
            },
            rates=lambda provider: 7.24,
        )

    def checkpoint(request, actor, executor, configuration):
        async def restore(callback, state):
            restored.append(state)

        policy = NativeCheckpointPolicy(
            principal=actor,
            task=request.task,
            storage=storage,
            executor=executor,
            epoch="root-crash-epoch",
            binding="same-model-tools-account",
            token=uuid.uuid4().hex,
            header_bytes=4096,
            snapshot_bytes=1 << 20,
            identity_bytes=1024,
            io_timeout_ms=30000,
            restore=restore,
            handoff_from_accepted=True,
            tool_recovery=NativeFileToolRecovery(
                principal=actor, task=request.task, executor=executor
            ),
        )
        checkpoints.append(policy)
        return policy

    configuration = profile()
    configuration.update(checkpoint_shared_account=True, checkpoint_frame_bytes=1 << 20)
    for field in ("input_bytes", "text_bytes", "tool_json_bytes"):
        configuration["model_counter"][field] = 1 << 20
    options = NativeRootConfiguration(
        configuration=configuration,
        working_directory=directory,
        repository_factory=lambda actor: repo,
        pricing_factory=pricing,
        checkpoint_factory=checkpoint,
    )
    from lib.tasks_pkg.orchestrator.api import run_task as actual_run_task

    execution_joined = threading.Event()

    def observe_execution(*args, **kwargs):
        try:
            return actual_run_task(*args, **kwargs)
        finally:
            execution_joined.set()

    runtime = None

    async def start():
        nonlocal runtime
        runtime = ServingLoopRuntime(
            asyncio.get_running_loop(),
            threading.Event(),
            environ={
                "TOFU_TASK_CLEANUP_INTERVAL": "0",
                "TOFU_EXECUTOR_IDLE_SECONDS": "0",
                "TOFU_SYNC_WORKERS": "2",
                "TOFU_AGENT_WORKERS": "2",
            },
        ).start()
        app.extensions["tofu_serving_loop_lifecycle"] = {"runtime": runtime}

    async def stop():
        if runtime is not None:
            await runtime.stop()

    with (
        patch("lib.tasks_pkg.orchestrator.api.run_task", side_effect=observe_execution),
        patch("lib.model_routing.ModelRoutingRepository", return_value=repo),
        patch("routes.conversation_sync_v3._request_user_id", return_value=owner),
        patch("routes.conversation_sync_v3._request_principal", return_value=principal),
    ):
        app = create_application(
            __name__,
            static_dir=str(directory),
            logger=logging.getLogger(__name__),
            secret_key="fixture",
            config={"TESTING": True},
            native_image=image,
            native_root=options,
            startup_handlers=[("fixture.serving", start)],
            shutdown_handlers=[("fixture.serving", stop)],
        )
        async with app.test_app():
            client = app.test_client()
            supplied = {
                key: value for key, value in settings.items() if not key.startswith("_")
            }
            supplied["modelRef"] = {"creator_id": "creator", "model_id": "alpha"}
            response = await client.post(
                f"/api/v4/conversations/{values['conversation_id']}/turns/{turn['turnId']}/attempts",
                json={
                    "commandId": uuid.uuid4().hex,
                    "operation": "continue",
                    "expectedProjectionRevision": turn["projectionRevision"],
                    "config": supplied,
                },
            )
            reply = await response.get_json()
            assert response.status_code == 200 and reply["ok"], reply
            attempt_id = reply["attempt"]["attemptId"]
            expires = time.monotonic() + 40
            while True:
                attempt = await asyncio.to_thread(
                    get_attempt, attempt_id, user_id=owner
                )
                if attempt["status"] in {"completed", "failed", "interrupted"}:
                    break
                assert time.monotonic() < expires, attempt
                await asyncio.sleep(0.02)
            # Durable Turn completion precedes native ACK and post-commit
            # callbacks. Join this private worker before closing its authority.
            assert await asyncio.to_thread(
                execution_joined.wait, max(0, expires - time.monotonic())
            ), "owned HTTP task failed to join"
            final = await asyncio.to_thread(
                get_turn, values["conversation_id"], turn["turnId"], user_id=owner
            )
            assert len(created) == 1
            task = created[0]
            account = task["_executionBudget"]
            receipt = dict(
                status=task["status"],
                attempt_status=attempt["status"],
                turn_status=final["status"],
                task_id=task["id"],
                task_error=task.get("error"),
                error=None,
                paid_tokens=account.consumption_snapshot()[0],
                tool_calls=account.consumption_snapshot()[1],
                restored_frontiers=[state["next_round"] for state in restored],
                restored_pending=[
                    state.get("tools_pending", False) for state in restored
                ],
                restored_operations=[
                    [
                        row["inspection"]["operationId"]
                        for row in state["tool_recovery"]["calls"]
                    ]
                    for state in restored
                ],
                producer_tasks=[
                    state["tool_recovery"]["producer_task_id"] for state in restored
                ],
                fee_rounds=len(task.get("apiRounds", [])),
                fee_credits=[
                    row["nativeCredits"]["total"] for row in task.get("apiRounds", [])
                ],
                fee_executor_ids=[row["taskId"] for row in task.get("apiRounds", [])],
                pending_model_requests=account.continuity_snapshot()[
                    "pending_model_requests"
                ],
                recovery_state_bytes=[
                    len(json.dumps(state, separators=(",", ":")).encode())
                    for state in restored
                ],
                public_http_continue=True,
            )
        from lib.tasks_pkg.event_delivery import shutdown_event_delivery

        delivery_closed = await asyncio.to_thread(shutdown_event_delivery, 10.0)
        receipt["joined_cleanup"] = image._closed and all(
            policy.closed for policy in checkpoints
        )
        receipt["joined_cleanup"] = receipt["joined_cleanup"] and delivery_closed
        Path(values["result"]).write_text(json.dumps(receipt) + "\n")
