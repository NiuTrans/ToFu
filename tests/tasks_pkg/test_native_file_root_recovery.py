"""Actual priced ROOT/SIGKILL recovery uses write_file proof without replay."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.api


@pytest.mark.parametrize(
    "wallet,changed_file", [(False, False), (True, False), (True, True)]
)
def test_actual_root_restores_paid_file_batch_and_spent_tools(
    tmp_path,
    wallet,
    changed_file,
    *,
    fresh_executor=False,
    repeat_handoff=False,
    public_http=False,
    delivery_review=False,
    revise_accepted_output=False,
):
    assert not revise_accepted_output or (delivery_review and not changed_file and not repeat_handoff and not public_http)
    initial_tools = 2 if revise_accepted_output else 1
    expected_requests = 3 if revise_accepted_output else 2
    required = (
        "TOFU_NATIVE_TERMINAL_TEST_ARCHIVE",
        "TOFU_CHECKPOINT_CABI_TEST_LIBRARY",
        "TOFU_CHECKPOINT_CABI_TEST_BINARY",
        "TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256",
    )
    archive, library, binary, source = [os.environ.get(key) for key in required]
    if not all((archive, library, binary, source)):
        pytest.skip(
            "explicit matching private native archive and checkpoint engine required"
        )
    if sys.platform != "linux":
        pytest.skip("Linux process-death proof contract")
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            bodies.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            first = len(bodies) == 1
            delta = (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "file-publication",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    dict(
                                        description="Append the requested result once",
                                        path="result.txt",
                                        content="published once\n",
                                        append=True,
                                    )
                                ),
                            },
                        }
                    ]
                }
                if first
                else {"content": ("Report region: east" if delivery_review and
                    'Report region: east' in json.dumps(bodies[-1]) else "File publication verified.")}
            )
            if revise_accepted_output:
                if first:
                    delta['tool_calls'].append({'index': 1, 'id': 'report-west', 'type': 'function',
                        'function': {'name': 'write_file', 'arguments': json.dumps({
                            'description': 'Publish the initial regional report', 'path': 'report.md',
                            'content': 'Report region: west\n'})}})
                elif len(bodies) == 2:
                    assert 'Report region: east' in json.dumps(bodies[-1])
                    delta = {'tool_calls': [{'index': 0, 'id': 'report-east', 'type': 'function',
                        'function': {'name': 'write_file', 'arguments': json.dumps({
                            'description': 'Revise only the report for the changed region', 'path': 'report.md',
                            'content': 'Report region: east\n'})}}]}
            data = (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": delta,
                                "finish_reason": "tool_calls" if 'tool_calls' in delta else "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    }
                )
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    directory = tmp_path / "authority"
    directory.mkdir()
    subprocess.run(
        [binary, "init", "--data-dir", str(directory)], check=True, capture_output=True
    )
    values = dict(
        directory=str(tmp_path),
        archive=archive,
        library=library,
        source_sha256=source,
        task_id=uuid.uuid4().hex,
        conversation_id=uuid.uuid4().hex,
        deadline=time.time() + 300,
        endpoint=f"http://127.0.0.1:{server.server_port}/v1",
        resume=False,
        phase="file_pending",
        ready=str(tmp_path / "ready.json"),
        result=str(tmp_path / "result.json"),
        priced_tree=wallet,
        child_model="alpha",
        file_tools=True,
    )
    original_task_id = values["task_id"]
    values.update(
        original_task_id=original_task_id,
        fresh_executor=fresh_executor,
        public_http=public_http,
        delivery_review=delivery_review,
        revise_accepted_output=revise_accepted_output,
    )
    configuration = tmp_path / "configuration.json"
    environment = {
        **os.environ,
        "TOFU_PERSONAL_STORAGE_BACKEND": "tofudb",
        "TOFUDB_TRANSPORT": "embedded",
        "TOFUDB_BIN": binary,
    }
    process = None

    def launch(log):
        configuration.write_text(json.dumps(values) + "\n")
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.support.native_root_crash_probe",
                str(configuration),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    try:
        with (tmp_path / "first.log").open("wb") as log:
            process = launch(log)
            expires = time.monotonic() + 45
            while not Path(values["ready"]).exists():
                assert process.poll() is None, (tmp_path / "first.log").read_text()[
                    -4000:
                ]
                assert time.monotonic() < expires, (tmp_path / "first.log").read_text()[
                    -4000:
                ]
                time.sleep(0.02)
            first = json.loads(Path(values["ready"]).read_text())
            assert first["phase"] == "file_pending" and first["paid_tokens"] == 5
            assert first["tool_calls"] == initial_tools and len(bodies) == 1
            assert len(first["file_operations"]) == initial_tools
            assert (tmp_path / "project/result.txt").read_text() == "published once\n"
            os.killpg(process.pid, signal.SIGKILL)
            assert process.wait(timeout=10) == -signal.SIGKILL
        if changed_file:
            (tmp_path / "project/result.txt").write_text("later human content\n")
        values.update(resume=True, turn_id=first["turn_id"])
        if wallet:
            values["wallet_id"] = first["wallet_id"]
        if fresh_executor:
            values["retired_task_ids"] = [original_task_id]
            values["task_id"] = uuid.uuid4().hex
        if repeat_handoff:
            values.update(
                crash_after_handoff=True, ready=str(tmp_path / "handoff-ready.json")
            )
            with (tmp_path / "handoff.log").open("wb") as log:
                process = launch(log)
                expires = time.monotonic() + 45
                while not Path(values["ready"]).exists():
                    assert process.poll() is None, (
                        tmp_path / "handoff.log"
                    ).read_text()[-4000:]
                    assert time.monotonic() < expires
                    time.sleep(0.02)
                assert (
                    json.loads(Path(values["ready"]).read_text())["phase"]
                    == "handoff_loaded"
                )
                assert len(bodies) == 1
                os.killpg(process.pid, signal.SIGKILL)
                assert process.wait(timeout=10) == -signal.SIGKILL
            values["retired_task_ids"].append(values["task_id"])
            values.update(crash_after_handoff=False, task_id=uuid.uuid4().hex)
        with (tmp_path / "second.log").open("wb") as log:
            process = launch(log)
            assert process.wait(timeout=45) == 0, (tmp_path / "second.log").read_text()[
                -4500:
            ]
        result = json.loads(Path(values["result"]).read_text())
        assert result["status"] == "done" and result["attempt_status"] == "completed", (
            result
        )
        assert result["joined_cleanup"] and result["pending_model_requests"] == 0
        assert result["paid_tokens"] == 5 * expected_requests
        assert result["tool_calls"] == (3 if revise_accepted_output else 1)
        assert result["restored_frontiers"] == [1] and result["restored_pending"] == [
            True
        ]
        assert result["restored_operations"] == [first["file_operations"]]
        assert result["producer_tasks"] == [original_task_id]
        assert result["fee_executor_ids"] == [original_task_id] + [result["task_id"]] * (expected_requests - 1)
        if public_http:
            assert result["public_http_continue"] is True
        if fresh_executor:
            assert result["task_id"] != original_task_id
        assert (
            result["fee_rounds"] == expected_requests
            and result["fee_credits"] == first["fee_credits"] * expected_requests
        )
        assert len(bodies) == expected_requests
        if delivery_review:
            assert 'Report region: west' in json.dumps(bodies[0])
            assert 'Report region: east' in json.dumps(bodies[1])
            old = first['delivery_review']
            review = result['delivery_review']
            assert review['before_owner_review']['criteria'][0]['evidence'] == old['criteria'][0]['evidence']
            assert review['accepted']['status'] == 'accepted'
            assert review['accepted']['turnId'] == first['turn_id']
            assert review['accepted']['criteria'][1]['digest'] in json.dumps(bodies[1])
            if revise_accepted_output:
                assert old['artifactReviews']['report']['status'] == 'accepted'
                assert old['artifactReviews']['ledger']['status'] == 'accepted'
                prepared = review['after_requirement_change']
                assert prepared['artifactReviews']['report']['status'] == 'pending'
                assert prepared['artifactReviews']['report']['criteria'][0]['state'] == 'stale'
                assert prepared['artifactReviews']['ledger'] == old['artifactReviews']['ledger']
                assert review['artifactReviews']['ledger'] == old['artifactReviews']['ledger']
                assert review['artifactReviews']['report']['status'] == 'accepted'
                assert review['old_report_review']['criteria'][0]['state'] == 'stale'
                assert review['before_owner_review']['criteria'][1]['state'] == 'stale'
                assert (tmp_path / 'project/report.md').read_text() == 'Report region: east\n'
                calls = [call for body in bodies for message in body['messages']
                         if message['role'] == 'assistant' for call in message.get('tool_calls', [])]
                assert any(call['id'] == 'report-east' for call in calls)
        feedback = [row for row in bodies[1]["messages"] if row["role"] == "tool"]
        assert [row['tool_call_id'] for row in feedback] == (
            ['file-publication', 'report-west'] if revise_accepted_output else ['file-publication'])
        for row, operation_id in zip(feedback, first['file_operations'], strict=True):
            proof = json.loads(row['content'])
            assert proof['outcome'] == 'file_published' and proof['operationId'] == operation_id
        assert (tmp_path / "project/result.txt").read_text() == (
            "later human content\n" if changed_file else "published once\n"
        )
        if wallet:
            capsule = first["wallet_checkpoint_invoice"]
            assert capsule["componentsMicro"]["totalMicro"] == sum(first["fee_credits"])
            assert capsule["task_id"] == original_task_id and capsule["roundCount"] == 1
            assert capsule["wallet_user_id"] == first["wallet_id"]
            assert not first["terminal_invoice_frozen"]
            if fresh_executor:
                retired = result["retired_wallet_invoices"]
                assert retired[0] == capsule
                assert [row["task_id"] for row in retired] == values["retired_task_ids"]
                assert [row["componentsMicro"]["totalMicro"] for row in retired] == ([28, 0] if repeat_handoff else [28])
                assert result["wallet_current_charge"] == 28 * (expected_requests - 1)
                assert result["wallet_source_charge"] == 28
                assert result["source_wallet_ack_retry_idempotent"]
                restored_invoice = result["restored_wallet_checkpoint_invoices"][0]
                assert restored_invoice["task_id"] == result["task_id"]
                assert restored_invoice["componentsMicro"]["totalMicro"] == 0
            else:
                assert result["restored_wallet_checkpoint_invoices"] == [capsule]
            assert result["wallet_charge"] == sum(result["fee_credits"])
            assert result["wallet_balance"] == 10000 - result["wallet_charge"]
            assert result["wallet_ack_retry_idempotent"]
        destination = os.environ.get("TOFU_NATIVE_ROOT_RECOVERY_RECEIPTS")
        if destination:
            output = Path(destination)
            output.mkdir(parents=True, exist_ok=True)
            (
                output
                / (f"file-pending-wallet-{wallet}-changed-{changed_file}-fresh-{fresh_executor}-repeat-{repeat_handoff}-http-{public_http}-review-{delivery_review}" +
                   ('-revised-True' if revise_accepted_output else '') + '.json')
            ).write_text(
                json.dumps(
                    dict(
                        scope="actual_ROOT_file_pending_SIGKILL",
                        first=first,
                        resumed=result,
                        provider_requests=len(bodies),
                        changed_file=changed_file,
                        same_executor=not fresh_executor,
                        accepted_fresh_executor_takeover=fresh_executor,
                        repeated_handoff=repeat_handoff,
                        public_http_continue=public_http,
                        delivery_review=delivery_review,
                        revise_accepted_output=revise_accepted_output,
                    ),
                    indent=2,
                )
                + "\n"
            )
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        server.shutdown()
        thread.join()
        server.server_close()
        destination = os.environ.get("TOFU_NATIVE_ROOT_RECOVERY_RECEIPTS")
        if destination:
            debug = (
                Path(destination)
                / f"debug-wallet-{wallet}-changed-{changed_file}-fresh-{fresh_executor}-repeat-{repeat_handoff}-http-{public_http}"
            )
            debug.mkdir(parents=True, exist_ok=True)
            for name in (
                "first.log",
                "second.log",
                "ready.json",
                "result.json",
                "configuration.json",
            ):
                if (tmp_path / name).exists():
                    shutil.copyfile(tmp_path / name, debug / name)


@pytest.mark.parametrize("repeat_handoff", [False, True])
def test_actual_root_file_pending_follows_accepted_fresh_executor(
    tmp_path, repeat_handoff
):
    test_actual_root_restores_paid_file_batch_and_spent_tools(
        tmp_path, False, True, fresh_executor=True, repeat_handoff=repeat_handoff
    )


def test_actual_v4_continue_recovers_pending_file_without_replay(tmp_path):
    test_actual_root_restores_paid_file_batch_and_spent_tools(
        tmp_path, False, True, fresh_executor=True, public_http=True
    )


@pytest.mark.parametrize("repeat_handoff", [False, True])
def test_actual_wallet_pending_handoff_recovers_original_debt_with_lost_ack(tmp_path, repeat_handoff):
    test_actual_root_restores_paid_file_batch_and_spent_tools(
        tmp_path, True, True, fresh_executor=True, repeat_handoff=repeat_handoff
    )
