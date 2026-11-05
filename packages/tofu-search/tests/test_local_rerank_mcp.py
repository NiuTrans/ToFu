"""The MCP lifetime owns optional models and forwards request cancellation."""

import asyncio
import threading
import time
from concurrent.futures import CancelledError

import anyio
import pytest

from tofu_search.config import SearchConfig, bind_call_config, get_config
from tofu_search.mcp_server import _bridge as bridge

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["asyncio", "anyio"])
def test_request_cancel_reaches_local_scorer_and_joins_before_return(mode):
    entered, settled = threading.Event(), threading.Event()
    parent_stop = threading.Event()

    def score():
        stop = get_config().local_rerank_cancel_event
        entered.set()
        deadline = time.monotonic() + 3
        try:
            while not stop.is_set():
                assert time.monotonic() < deadline, "MCP cancellation did not reach scorer"
                time.sleep(0.005)
            raise CancelledError("owned scorer stopped")
        finally:
            settled.set()

    async def run():
        with bind_call_config(SearchConfig(local_rerank_cancel_event=parent_stop)):
            if mode == "asyncio":
                task = asyncio.create_task(bridge.run_blocking(score))
                assert await asyncio.to_thread(entered.wait, 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(bridge.run_blocking, score)
                    assert await asyncio.to_thread(entered.wait, 1)
                    tasks.cancel_scope.cancel()
        assert settled.is_set()
        assert not parent_stop.is_set(), "one request cancelled its shared parent"

    try:
        anyio.run(run)
    finally:
        bridge.shutdown_bridge()
    assert bridge.bridge_snapshot()["resident_threads"] == 0


def test_bridge_preserves_inherited_cancellation():
    stop = threading.Event()
    stop.set()

    async def run():
        with bind_call_config(SearchConfig(local_rerank_cancel_event=stop)):
            assert await bridge.run_blocking(
                lambda: get_config().local_rerank_cancel_event.is_set()
            )

    try:
        anyio.run(run)
    finally:
        bridge.shutdown_bridge()


@pytest.mark.parametrize("failure", [None, "build", "close"])
def test_cli_model_owner_closes_on_success_and_failure(monkeypatch, failure):
    pytest.importorskip("mcp")
    from tofu_search import local_rerank
    from tofu_search.mcp_server import __main__ as cli
    from tofu_search.mcp_server import server

    events = []
    original = get_config().local_reranker

    class Ranker:
        def __init__(self, path, *, python_executable):
            assert (path, python_executable) == ("/explicit/snapshot", "/explicit/python")
            events.append("created")

        def close(self):
            events.append("closed")
            if failure == "close":
                raise RuntimeError("cleanup remains owned")

    class Server:
        def run(self, **kwargs):
            assert kwargs == {"transport": "stdio"}
            assert isinstance(get_config().local_reranker, Ranker)
            events.append("run")

    def build():
        if failure == "build":
            raise RuntimeError("startup failed")
        return Server()

    monkeypatch.setattr(local_rerank, "LocalReranker", Ranker)
    monkeypatch.setattr(server, "build_server", build)
    monkeypatch.setattr(bridge, "shutdown_bridge", lambda: events.append("bridge_joined"))
    args = [
        "--local-reranker-model-dir",
        "/explicit/snapshot",
        "--local-reranker-python",
        "/explicit/python",
    ]
    if failure:
        with pytest.raises(RuntimeError):
            cli.main(args)
    else:
        assert cli.main(args) == 0
    assert events == (
        ["created", "closed", "bridge_joined"]
        if failure == "build"
        else ["created", "run", "closed", "bridge_joined"]
    )
    assert get_config().local_reranker is original


def test_cli_rejects_orphan_interpreter_option():
    from tofu_search.mcp_server.__main__ import main

    with pytest.raises(SystemExit) as error:
        main(["--local-reranker-python", "/explicit/python"])
    assert error.value.code == 2
