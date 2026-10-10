"""Opt-in v2 authority -> native sealed process -> real loopback HTTP proof.

Requires the repository's test environment and an explicitly verified archive.
Synthetic identities/keys only; no application store or real provider calls.
"""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tarfile
import tempfile
import threading

from lib.agent_core.native_process import Budget, NativeProcessClient
from lib.model_routing.native_route import compile_native_route, run_native_route
from scripts.build_native_runtime import verify
from tests.model_routing.test_native_route import Repository, principal, selection
from tests.support.native_process_probe import config


async def probe(binary, manifest, directory):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer private-fixture-token"
            assert self.headers["X-Region"] == "fixture"
            assert body["model"] == "alpha-wire-a" and body["max_tokens"] == 65535
            seen.append(body)
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
                assert body["messages"][-1]["content"] == "routed native evidence"
                delta = {"content": "routed native success"}
                finish = "stop"
            wire = (
                "data: "
                + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    client = NativeProcessClient(
        binary=binary,
        sha256=manifest["files"]["bin/tofu-native"]["sha256"],
        protocol_version=manifest["build"]["stdio_contract"],
        principal=principal(),
        budget=Budget(1, 16 * 1024 * 1024, 65536, 131072, 1),
    )
    repo = Repository()
    repo.document["connections"][0]["base_url"] = (
        f"http://127.0.0.1:{server.server_port}/v1"
    )
    route = compile_native_route(repo, repo.boundary, selection())
    settings = config("ignored", "v2-native-route")
    settings.pop("endpoint")
    settings.pop("headers")
    settings.pop("template")
    calls = []

    async def lookup(request):
        assert (
            request.principal is client.principal
            and request.task_id == "v2-native-route"
        )
        calls.append(request.name)
        return "routed native evidence"

    try:
        result = await run_native_route(
            client,
            route,
            settings,
            prepare_template=lambda metadata: {
                "model": metadata["wire_model"],
                "max_tokens": 100000,
            },
            callbacks={"lookup": lookup},
            working_directory=directory,
        )
        assert (
            result.event["status"] == "done" and len(seen) == 2 and calls == ["lookup"]
        )
        assert (
            result.route["provider_id"] == "provider-a"
            and result.route["revision"] == 3
        )
        assert len(repo.secret_calls) == 1 and client.active_runs == 0
        return dict(
            physical_model_requests=len(seen),
            host_tool_calls=len(calls),
            secret_resolutions=len(repo.secret_calls),
            active_runs_after=client.active_runs,
            provider_id=result.route["provider_id"],
            wire_model=result.route["wire_model"],
        )
    finally:
        await client.close()
        server.shutdown()
        thread.join()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    manifest = verify(args.archive)
    with tempfile.TemporaryDirectory(prefix="tofu-native-route-") as folder:
        directory = Path(folder)
        with tarfile.open(args.archive) as archive:
            archive.extractall(directory, filter="data")
        print(
            json.dumps(
                asyncio.run(probe(directory / "bin/tofu-native", manifest, directory))
            )
        )


if __name__ == "__main__":
    main()
