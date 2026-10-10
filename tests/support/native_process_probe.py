"""Opt-in real native host/process/TCP probe over one verified built archive.

Run as a module with --archive. The Python host only supplies read-tool replies;
provider/model rounds run in the sealed Rust executable. No real model or key.
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import tarfile
import threading

from lib.agent_core.native_process import (
    Budget,
    NativeProcessClient,
    NativeToolError,
    NativeRoundError,
    NativePersistenceError,
)
from lib.identity import PrincipalContext
from scripts.build_native_runtime import verify


def config(endpoint, mode):
    return {
        "version": "tofu.native-agent-stdio/v1",
        "type": "configure",
        "owner_user_id": 7,
        "task_id": mode,
        "epoch": "epoch",
        "endpoint": endpoint,
        "headers": {},
        "template": {"model": "fixture"},
        "messages": [{"role": "user", "content": "hi"}],
        "tools": {
            "lookup": {
                "name": "lookup",
                "permission": "read",
                "contractVersion": "tofu.tool-contract/v2",
                "arguments_schema": {"type": "object"},
            }
        },
        "event_bytes": 65536,
        "output_bytes": 65536,
        "tool_calls": 8,
        "request_bytes": 65536,
        "rounds": 3,
        "retained_bytes": 65536,
        "feedback_bytes": 4096,
        "tool_frame_bytes": 8192,
        "deadline_ms": 3000,
        "policy": {
            "connections": 1,
            "idle_connections": 1,
            "keepalive_ms": 1000,
            "connect_ms": 1000,
            "request_headers_ms": 2000,
            "pool_wait_ms": 1000,
            "stream_idle_ms": 2000,
            "drain_timeout_ms": 100,
            "drain_bytes": 65536,
            "drain_chunks": 16,
        },
    }


async def probe(binary, manifest, directory):
    principal = PrincipalContext.user(subject_id="native-probe", owner_user_id=7)
    client = NativeProcessClient(
        binary=binary,
        sha256=manifest["files"]["bin/tofu-native"]["sha256"],
        protocol_version=manifest["build"]["stdio_contract"],
        principal=principal,
        budget=Budget(1, 16 * 1024 * 1024, 65536, 131072, 1),
    )
    observations = []
    try:
        for mode in [
            "ok",
            "callback_fault",
            "round_ok",
            "round_deny",
            "round_settle_fault",
            "round_cancel",
            "token_ok",
            "token_unknown",
            "persist_ok",
            "persist_retry",
            "persist_fault",
            "persist_cancel",
            "cancel",
            "close",
        ]:
            seen = []
            started = threading.Event()
            release = threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    self.connection.settimeout(3)
                    body = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    seen.append(body)
                    if mode in ["cancel", "close", "round_cancel", "persist_cancel"]:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", "10000")
                        self.end_headers()
                        self.wfile.flush()
                        started.set()
                        release.wait(3)
                        return
                    if len(seen) == 1:
                        delta = {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                        finish = "tool_calls"
                    else:
                        assert body["messages"][-1]["content"] == "native host evidence"
                        delta = {"content": "native success"}
                        finish = "stop"
                    reported_usage = (
                        (
                            {
                                "prompt_tokens": 100,
                                "completion_tokens": 5,
                                "cached_tokens": 100,
                            }
                            if len(seen) == 1
                            else {
                                "input_tokens": 10,
                                "output_tokens": 7,
                                "cache_read_input_tokens": 60,
                            }
                        )
                        if mode in {"round_ok", "token_ok"}
                        else {}
                    )
                    wire = (
                        "data: "
                        + json.dumps(
                            {
                                "choices": [{"delta": delta, "finish_reason": finish}],
                                "usage": reported_usage,
                            }
                        )
                        + "\n\ndata: [DONE]\n\n"
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(wire)))
                    self.end_headers()
                    self.wfile.write(wire)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}
            )
            thread.start()
            commits = []

            class Persistence:
                async def persist(self, request):
                    assert request.principal is principal and request.task_id == mode
                    assert request.receipt["invariants_satisfied"]
                    commits.append(request)
                    if mode == "persist_fault":
                        raise OSError("fixture durable failure")
                    if mode == "persist_retry" and request.attempt == 1:
                        return "retry"
                    return "accepted"

            terminal_policy = Persistence() if mode.startswith("persist_") else None
            calls = 0
            reserves, settlements = [], []
            reserved_requests, settled_requests = [], []

            class Policy:
                async def reserve(self, request):
                    assert request.principal is principal and request.task_id == mode
                    reserves.append(request.round)
                    reserved_requests.append(request)
                    return not (mode == "round_deny" and request.round == 1)

                async def settle(self, result):
                    assert any(request is result.request for request in reserved_requests)
                    assert not any(request is result.request for request in settled_requests)
                    settled_requests.append(result.request)
                    settlements.append(dict(result.evidence))
                    if mode == "round_settle_fault":
                        raise OSError("fixture accounting fault")

            policy = Policy() if mode.startswith("round_") else None

            async def read_tool(request):
                nonlocal calls
                assert (
                    request.principal is principal
                    and request.task_id == mode
                    and request.name == "lookup"
                )
                calls += 1
                if mode == "callback_fault":
                    raise ValueError("fixture tool fault")
                return "native host evidence"

            settings = config(f"http://127.0.0.1:{server.server_port}/chat", mode)
            if mode.startswith("token_"):
                settings["template"]["max_tokens"] = 100
                settings["token_admission"] = dict(
                    encoding="cl100k_base",
                    output_field="max_tokens",
                    total_tokens=2000,
                    input_bytes=65536,
                    text_bytes=65536,
                    tool_json_bytes=65536,
                    depth=128,
                    actor_records=1,
                    attempt_records=1,
                    identity_bytes=128,
                    worker_stack_bytes=1048576,
                )
            if terminal_policy is not None:
                settings["terminal_persistence"] = dict(
                    operation_id="commit-" + mode,
                    ack_ms=2000,
                    attempts=2,
                    frame_bytes=65536,
                )
            try:
                if mode == "persist_fault":
                    try:
                        await client.run(
                            settings,
                            callbacks={"lookup": read_tool},
                            working_directory=directory,
                            terminal_policy=terminal_policy,
                        )
                    except NativePersistenceError as error:
                        assert isinstance(error.__cause__, OSError)
                    else:
                        raise AssertionError("commit failure became terminal success")
                    assert len(seen) == 2 and calls == 1 and len(commits) == 1
                elif mode in [
                    "ok",
                    "round_ok",
                    "round_deny",
                    "round_settle_fault",
                    "token_ok",
                    "token_unknown",
                    "persist_ok",
                    "persist_retry",
                ]:
                    try:
                        result = await client.run(
                            settings,
                            callbacks={"lookup": read_tool},
                            working_directory=directory,
                            round_policy=policy,
                            terminal_policy=terminal_policy,
                        )
                    except NativeRoundError as error:
                        assert mode == "round_settle_fault" and isinstance(
                            error.__cause__, OSError
                        )
                        assert len(seen) == 1 and calls == 0 and len(settlements) == 1
                    else:
                        assert mode != "round_settle_fault"
                        assert result["status"] == (
                            "error"
                            if mode in {"round_deny", "token_unknown"}
                            else "done"
                        )
                        assert (
                            len(seen)
                            == (1 if mode in {"round_deny", "token_unknown"} else 2)
                            and calls == 1
                        )
                        if policy:
                            assert reserves == [0, 1]
                            assert len(settlements) == len(seen)
                            assert all(s["kind"] == "returned" for s in settlements)
                            if mode == "round_ok":
                                assert [
                                    s["usage_observation"] for s in settlements
                                ] == [
                                    {"tokens": 105, "complete": True, "error": None},
                                    {"tokens": 77, "complete": True, "error": None},
                                ]
                                assert settlements[0]["usage"] == {
                                    "prompt_tokens": 100,
                                    "completion_tokens": 5,
                                    "cached_tokens": 100,
                                }
                            else:
                                assert all(
                                    s["usage"] == {}
                                    and s["usage_observation"]
                                    == {"tokens": 0, "complete": False, "error": None}
                                    for s in settlements
                                )
                elif mode == "callback_fault":
                    try:
                        await client.run(
                            settings,
                            callbacks={"lookup": read_tool},
                            working_directory=directory,
                            round_policy=policy,
                            terminal_policy=terminal_policy,
                        )
                    except NativeToolError as error:
                        assert isinstance(error.__cause__, ValueError)
                    else:
                        raise AssertionError("tool failure became success")
                    assert len(seen) == 1
                else:
                    run = asyncio.create_task(
                        client.run(
                            settings,
                            callbacks={"lookup": read_tool},
                            working_directory=directory,
                            round_policy=policy,
                            terminal_policy=terminal_policy,
                        )
                    )
                    assert await asyncio.to_thread(started.wait, 2)
                    other = asyncio.create_task(client.run(settings,
                        callbacks={"lookup": read_tool}, working_directory=directory,
                        round_policy=policy, terminal_policy=terminal_policy))
                    for _ in range(200):
                        if len(seen) == 2:
                            break
                        await asyncio.sleep(.01)
                    assert len(seen) == 2 and client.active_runs == 2
                    if mode == "close":
                        await asyncio.gather(client.close(), client.close())
                    else:
                        run.cancel()
                        run.cancel()
                        other.cancel()
                    for active in (run, other):
                        try:
                            await active
                        except asyncio.CancelledError:
                            pass
                        else:
                            raise AssertionError("host cancellation disappeared")
                    assert calls == 0 and len(seen) == 2
                    if policy:
                        assert reserves == [0, 0] and len(settlements) == 2
                        assert all(outcome['kind'] in {
                            "stopped",
                            "returned",
                            "failed",
                            "abandoned",
                        } for outcome in settlements)
                if terminal_policy is not None:
                    assert len(commits) == (2 if mode in {"persist_retry", "persist_cancel"} else 1)
                    if mode == "persist_retry":
                        assert (
                            commits[0].event == commits[1].event
                            and commits[0].receipt == commits[1].receipt
                        )
                    if mode == "persist_cancel":
                        assert all(request.event["status"] == "aborted" for request in commits)
                assert client.active_runs == 0
                observations.append(
                    {
                        "mode": mode,
                        "model_rounds": len(seen),
                        "host_callbacks": calls,
                        "active_runs_after": client.active_runs,
                        "reservations": reserves,
                        "settlements": settlements,
                        "commit_attempts": len(commits),
                    }
                )
            finally:
                release.set()
                server.shutdown()
                thread.join()
                server.server_close()
    finally:
        await client.close()
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    manifest = verify(args.archive)
    with tempfile.TemporaryDirectory(prefix="tofu-native-host-") as temporary:
        directory = Path(temporary)
        with tarfile.open(args.archive) as archive:
            archive.extractall(directory, filter="data")
        binary = directory / "bin/tofu-native"
        binary.chmod(0o755)
        assert (
            hashlib.sha256(binary.read_bytes()).hexdigest()
            == manifest["files"]["bin/tofu-native"]["sha256"]
        )
        result = asyncio.run(probe(binary, manifest, directory))
    print(
        json.dumps(
            {
                "native_binary_bytes": manifest["files"]["bin/tofu-native"]["bytes"],
                "observations": result,
            }
        )
    )


if __name__ == "__main__":
    main()
