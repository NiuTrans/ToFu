"""Trajectory redaction must keep task answers, strip genuine credentials.

tb21 v12 (password-recovery): the agent's final answer legitimately reads
``PASSWORD=<the recovered password>`` — that IS the task answer, not a
leaked host credential. Blanket assignment redaction corrupted the ATIF
step so it diverged from the raw native stream and the paired audit failed
the trial for "ATIF final output drifted".
"""

from __future__ import annotations

import pytest

from rootless_vm.trajectory import sanitize_collected_trajectory

pytestmark = pytest.mark.unit


def _trajectory(answer: str) -> dict:
    return {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "tofu-kimi-runtime", "version": "0.17.4",
                  "model_name": "glm-5.3-m17"},
        "steps": [
            {"step_id": "s1", "source": "agent", "message": answer,
             "reasoning_content": "private chain"},
        ],
        "final_metrics": {},
    }


def test_task_answer_password_assignment_survives():
    answer = ("Recovered password: `PASSWORD=8XDP5Q2RT9ZK7VB3BV4WW54`\n"
              "written to /app/recovered_passwords.txt")
    sanitized = sanitize_collected_trajectory(_trajectory(answer))

    assert sanitized["steps"][0]["message"] == answer
    # reasoning is still stripped
    assert sanitized["steps"][0].get("reasoning_content") is None


def test_genuine_credential_shapes_still_redacted_in_answer():
    answer = ("note: Authorization: Bearer abcdefghijklmnop123456\n"
              "and key sk-abcdefghijklmnopqrstuvwxyz")
    sanitized = sanitize_collected_trajectory(_trajectory(answer))

    message = sanitized["steps"][0]["message"]
    assert "abcdefghijklmnop123456" not in message
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in message
    assert "<redacted>" in message


def test_structured_secret_fields_still_redacted():
    payload = _trajectory("done")
    payload["steps"][0]["extra"] = {"api_key": "supersecretvalue123"}
    sanitized = sanitize_collected_trajectory(payload)

    assert sanitized["steps"][0]["extra"]["api_key"] == "<redacted>"


def test_credential_url_redacted_in_answer():
    answer = "endpoint was https://user:hunter2secret@internal.host/path"
    sanitized = sanitize_collected_trajectory(_trajectory(answer))

    message = sanitized["steps"][0]["message"]
    assert "hunter2secret" not in message
    assert "https://" in message and "<redacted>@" in message
