"""Wire receipts observe actual requests without leaking or correcting them."""
from copy import deepcopy
import io
import json
from types import SimpleNamespace

import httpx
import pytest

from evaluations.model_request import request_parameter_receipt
from evaluations.opencode_proxy.server import ProxyHandler

pytestmark = pytest.mark.unit


def test_receipt_keeps_missing_parameters_unknown_and_redacts_untrusted_strings():
    request = {"messages": [{"content": "secret"}], "temperature": float("nan"),
               "top_p": True, "reasoning_effort": "secret", "thinking": {"type": "secret"}}
    receipt = request_parameter_receipt(request)
    assert all(value is None for value in receipt.values())
    assert "secret" not in json.dumps(receipt, allow_nan=False)


def test_proxy_records_parameters_and_forwards_original_body():
    request = {"model": "model", "messages": [{"role": "user", "content": "private-prompt"}],
               "max_tokens": 65536, "temperature": 1.0, "top_p": 1,
               "reasoning_effort": "max", "thinking": {"type": "enabled"}, "stream": False}
    original = deepcopy(request)
    raw = json.dumps(request).encode()
    sent, metrics = [], []

    def upstream(incoming):
        sent.append(incoming.content)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length"}],
                                        "usage": {"completion_tokens": 65536}})

    handler = ProxyHandler.__new__(ProxyHandler)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
    handler.path = "/v1/chat/completions"
    handler._trial_token = lambda trace: "trial"
    handler.send_response = lambda *args: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        handler.server = SimpleNamespace(client=client, metrics=SimpleNamespace(append=metrics.append),
            config=SimpleNamespace(model="model", upstream_base_url="https://example.test/v1",
                                   upstream_api_key="private-key"))
        handler.do_POST()
    assert sent == [raw]
    assert request == original
    assert metrics[0]["requestParameters"] == {
        "max_tokens": 65536, "max_completion_tokens": None, "temperature": 1.0,
        "top_p": 1, "reasoning_effort": "max", "thinking_type": "enabled"}
    assert metrics[0]["finishReasons"] == ["length"]
    assert "private-prompt" not in json.dumps(metrics)
    assert "private-key" not in json.dumps(metrics)
