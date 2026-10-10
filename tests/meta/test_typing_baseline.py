"""The typing ratchet attributes diagnostics to the right scope and fails in
both directions (growth is debt; an untightened improvement is a loose gate).
"""

from __future__ import annotations

import json

import pytest

from scripts import check_typing_baseline as typing_gate

pytestmark = pytest.mark.unit


def _document(entries: list[tuple[str, str]]) -> dict:
    return {
        "generalDiagnostics": [
            {"file": path, "severity": severity}
            for path, severity in entries
        ]
    }


def test_scoped_counts_attribute_and_ignore_outside_files(monkeypatch, tmp_path):
    monkeypatch.setattr(typing_gate, "ROOT", str(tmp_path))
    document = _document([
        (str(tmp_path / "routes" / "chat.py"), "error"),
        (str(tmp_path / "lib" / "storage_sidecar" / "schema.py"), "warning"),
        (str(tmp_path / "lib" / "agent_core" / "events.py"), "error"),
        (str(tmp_path / "lib" / "api_v4.py"), "error"),
        (str(tmp_path / "lib" / "tasks_pkg" / "manager.py"), "error"),
        (str(tmp_path / "lib" / "api_v4.py"), "information"),
    ])
    counts = typing_gate.scoped_counts(document)
    assert counts == {
        "routes": 1,
        "lib/storage_sidecar": 1,
        "lib/agent_core": 1,
        "lib-strict-files": 1,
    }


def test_compare_fails_on_growth_and_on_loose_baseline(
    monkeypatch, tmp_path,
):
    baseline = tmp_path / "typing_baseline.json"
    baseline.write_text(json.dumps({"counts": {
        "routes": 10,
        "lib/storage_sidecar": 5,
        "lib/agent_core": 3,
        "lib-strict-files": 2,
    }}), encoding="utf-8")
    monkeypatch.setattr(typing_gate, "BASELINE", str(baseline))

    same = typing_gate.compare({
        "routes": 10, "lib/storage_sidecar": 5,
        "lib/agent_core": 3, "lib-strict-files": 2,
    })
    assert same == []

    grew = typing_gate.compare({
        "routes": 11, "lib/storage_sidecar": 5,
        "lib/agent_core": 3, "lib-strict-files": 2,
    })
    assert len(grew) == 1 and "grew 10 -> 11" in grew[0]

    shrank = typing_gate.compare({
        "routes": 10, "lib/storage_sidecar": 4,
        "lib/agent_core": 3, "lib-strict-files": 2,
    })
    assert len(shrank) == 1 and "SHRANK 5 -> 4" in shrank[0]
