"""Proxy shutdown settles accepted requests before releasing shared resources."""

import json
import socket
import socketserver
import threading

import pytest

from evaluations.codex_kimi_proxy.codex_contract import benchmark_trial_token
from evaluations.codex_kimi_proxy.server import CodexKimiProxy, ProxyConfig


pytestmark = pytest.mark.unit


def test_close_waits_for_both_metrics_sinks(tmp_path, monkeypatch):
    metrics = tmp_path / "metrics.jsonl"
    trial_directory = tmp_path / "trials"
    token = benchmark_trial_token("shutdown-finalizer")
    proxy = CodexKimiProxy(("127.0.0.1", 0), ProxyConfig(
        upstream_base_url="http://127.0.0.1:1/v1", upstream_api_key="test",
        metrics_jsonl=str(metrics), trial_metrics_dir=str(trial_directory)))
    entered = threading.Event()
    listener_closed = threading.Event()
    settled = threading.Event()
    workers = []
    close_observations = []
    row = {"trialToken": token, "upstreamCalls": 1}

    def finish_request(_request, _address, server):
        workers.append(threading.current_thread())
        entered.set()
        assert listener_closed.wait(5), "shutdown did not close the listener"
        server.metrics.append(row)
        settled.set()

    close_listener = socketserver.TCPServer.server_close

    def release_finalizer(server):
        close_listener(server)
        listener_closed.set()

    close_client = proxy.client.close

    def record_client_close():
        close_observations.append(settled.is_set())
        close_client()

    monkeypatch.setattr(socketserver.TCPServer, "server_close", release_finalizer)
    monkeypatch.setattr(proxy.client, "close", record_client_close)
    proxy.RequestHandlerClass = finish_request
    accepted, peer = socket.socketpair()
    try:
        proxy.process_request(accepted, ("127.0.0.1", 1))
        assert entered.wait(5), "accepted request did not start"
        proxy.server_close()
    finally:
        listener_closed.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        accepted.close()
        peer.close()
    assert close_observations == [True]
    assert json.loads(metrics.read_text()) == row
    assert json.loads((trial_directory / f"{token}.jsonl").read_text()) == row
