"""Directory-picker request admission is identical over HTTP and control RPC."""

from __future__ import annotations

import asyncio
from pathlib import Path

import jsonschema
import pytest
import yaml
from quart import Blueprint

pytestmark = pytest.mark.unit


@pytest.fixture
def browse_app(monkeypatch):
    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope
    from lib.app.app_factory import create_base_app
    from lib.http_transport.error_handlers import register_http_error_handlers
    from routes.api_v1.project import project_browse

    context = AuthContext(
        key_id="browse-test",
        owner_user_id=17,
        account_user_id="browse-test",
        scopes=frozenset({"project"}),
    )
    monkeypatch.setattr("routes.api_v1.auth.current_auth", lambda: context)
    app = create_base_app("browse-transport-test", {"TESTING": True})
    register_http_error_handlers(app)
    blueprint = Blueprint("browse_transport_test", __name__)
    blueprint.add_url_rule(
        "/api/v4/project/browse", view_func=project_browse, methods=["POST"]
    )
    app.register_blueprint(attach_v4_envelope(blueprint))
    calls = []

    def browse(path, *, show_hidden):
        calls.append((path, show_hidden))
        return {"path": path, "dirs": [], "showHidden": show_hidden}

    monkeypatch.setattr("lib.project_mod.browse_directory", browse)
    return app, calls


async def rpc_browse(params):
    from lib.control_rpc import ControlRpcSession

    class Client:
        def __init__(self):
            self.frames = asyncio.Queue()

        def enqueue_rpc(self, frame):
            self.frames.put_nowait(frame)
            return True

    client = Client()
    session = ControlRpcSession(client, user_id=17, request_id="browse-test")
    try:
        assert session.receive(
            {
                "jsonrpc": "2.0",
                "id": "browse-test-1",
                "method": "project.browse",
                "params": params,
            }
        )
        return await asyncio.wait_for(client.frames.get(), timeout=2)
    finally:
        await session.close()


@pytest.mark.parametrize(
    ("params", "normalized"),
    [
        ({}, (None, False)),
        ({"path": None}, (None, False)),
        ({"path": "  "}, (None, False)),
        ({"path": " /tmp/example ", "showHidden": True}, ("/tmp/example", True)),
        ({"path": "a" * 4096}, ("a" * 4096, False)),
        ({"path": 1}, None),
        ({"path": False}, None),
        ({"path": []}, None),
        ({"path": {}}, None),
        ({"path": "a" * 4097}, None),
        ({"path": " " * 4097}, None),
        ({"path": "a\0b"}, None),
        ({"showHidden": 1}, None),
        ({"showHidden": "false"}, None),
        ({"showHidden": None}, None),
        ({"typo": True}, None),
    ],
)
def test_browse_admission_matches_schema_and_transports(browse_app, params, normalized):
    from lib.agent_core.push import hub
    from lib.control_rpc import INVALID_PARAMS
    from lib.project_contracts.browse import browse_request_schema

    app, calls = browse_app
    assert jsonschema.Draft202012Validator(browse_request_schema()).is_valid(
        params
    ) is (normalized is not None)

    async def scenario():
        # The synchronous HTTP adapter resolves request JSON on the serving loop.
        previous_loop = hub._loop
        hub._loop = asyncio.get_running_loop()
        try:
            response = await app.test_client().post(
                "/api/v4/project/browse", json=params
            )
            body = await response.get_json()
            frame = await rpc_browse(params)
            return response, body, frame
        finally:
            hub._loop = previous_loop

    response, body, frame = asyncio.run(scenario())
    if normalized is None:
        assert response.status_code == 400
        assert response.mimetype == "application/problem+json"
        assert frame["error"]["code"] == INVALID_PARAMS
        assert calls == []
    else:
        assert response.status_code == 200
        assert body["data"] == {
            key: value for key, value in frame["result"].items() if key != "ok"
        }
        assert "meta" in body
        assert calls == [normalized, normalized]


def test_published_http_schema_and_control_rpc_contract_match(browse_app):
    from lib.openapi import build_spec
    from lib.project_contracts.browse import browse_request_schema

    app, _ = browse_app
    operation = build_spec(app)["paths"]["/api/v4/project/browse"]["post"]
    assert (
        operation["requestBody"]["content"]["application/json"]["schema"]
        == browse_request_schema()
    )
    root = Path(__file__).resolve().parents[2]
    contract = yaml.safe_load((root / "contracts/control_rpc_v1.yaml").read_text())
    params = contract["methods"]["project.browse"]["params"]
    fields = browse_request_schema()["properties"]
    assert set(fields) == set(params)
    assert fields["path"]["maxLength"] == params["path"]["maximumLength"]
    assert fields["path"]["type"] == [params["path"]["type"], "null"]
    assert params["path"]["nullable"] is True
    assert fields["path"]["pattern"] == params["path"]["pattern"]
    assert fields["showHidden"]["type"] == params["showHidden"]["type"]


def test_http_auth_denial_precedes_browse(browse_app, monkeypatch):
    app, calls = browse_app
    monkeypatch.setattr("routes.api_v1.auth.current_auth", lambda: None)
    response = asyncio.run(app.test_client().post("/api/v4/project/browse", json={}))
    assert response.status_code == 401
    assert calls == []


@pytest.mark.parametrize("payload", ["{broken", "[]", "1", '"path"'])
def test_malformed_http_body_fails_before_filesystem_access(browse_app, payload):
    from lib.agent_core.push import hub

    app, calls = browse_app

    async def scenario():
        previous_loop = hub._loop
        hub._loop = asyncio.get_running_loop()
        try:
            return await app.test_client().post(
                "/api/v4/project/browse",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
        finally:
            hub._loop = previous_loop

    response = asyncio.run(scenario())
    assert response.status_code == 400
    assert response.mimetype == "application/problem+json"
    assert calls == []


def test_domain_failure_keeps_http_and_rpc_error_classification(
    browse_app, monkeypatch
):
    from lib.control_rpc import DOMAIN_ERROR
    from lib.agent_core.push import hub

    app, _ = browse_app
    domain_error = {"error": "Not a directory", "path": "/missing"}
    monkeypatch.setattr(
        "lib.project_mod.browse_directory", lambda *a, **kw: domain_error
    )

    async def scenario():
        previous_loop = hub._loop
        hub._loop = asyncio.get_running_loop()
        try:
            response = await app.test_client().post(
                "/api/v4/project/browse", json={"path": "/missing"}
            )
            return response, await rpc_browse({"path": "/missing"})
        finally:
            hub._loop = previous_loop

    response, frame = asyncio.run(scenario())
    assert response.status_code == 400
    assert frame["error"]["code"] == DOMAIN_ERROR
    assert frame["error"]["data"] == domain_error


def test_protocol_import_does_not_load_filesystem_or_tool_services():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import lib.project_contracts.browse; "
            "assert 'lib.project_mod' not in sys.modules; "
            "assert 'lib.tools' not in sys.modules; "
            "assert 'lib.storage' not in sys.modules",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
