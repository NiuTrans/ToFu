"""Tests for the jobs tool module.

Uses plain ``asyncio.run`` to remain compatible with environments where
pytest-asyncio is unavailable.
"""

from __future__ import annotations

import asyncio


def test_submit_job_builds_args(fake_hope):
    from hope_mcp.tools.jobs import submit_job
    out = asyncio.run(submit_job(
        conf_path="my.conf",
        xml="cfg.xml",
        args="lr:0.1 bs:32",
        usergroup="hadoop-hdp",
        files=["a.py", "b.py"],
        annotation="sft run",
    ))
    assert out["ok"], out
    cmd = out["cmd"]
    assert " run " in cmd
    assert "--xml cfg.xml" in cmd
    assert "lr:0.1" in cmd
    assert "--usergroup hadoop-hdp" in cmd
    assert "--files a.py" in cmd
    assert "--files b.py" in cmd
    assert "sft run" in cmd


def test_submit_job_extracts_runid(fake_hope, monkeypatch):
    from hope_mcp.tools.jobs import submit_job
    monkeypatch.setenv(
        "HOPE_FAKE_STDOUT",
        "some banner\nRunId: 987654\nAppId=application_17_0042\nmore\n",
    )
    out = asyncio.run(submit_job())
    assert out["ok"]
    assert out["runid"] == "987654"
    assert out["appid"] == "application_17_0042"


def test_stop_job_single(fake_hope):
    from hope_mcp.tools.jobs import stop_job
    out = asyncio.run(stop_job(runid="42"))
    assert out["ok"]
    assert " stop " in out["cmd"]
    assert "--runid=42" in out["cmd"]


def test_stop_job_requires_runid(fake_hope):
    from hope_mcp.tools.jobs import stop_job
    out = asyncio.run(stop_job(runid=""))
    assert out["ok"] is False
    assert "runid" in out["error"]


def test_stop_jobs_batch_dry_run_default(fake_hope):
    from hope_mcp.tools.jobs import stop_jobs_batch
    out = asyncio.run(stop_jobs_batch(runids=["1", "2", "3"]))
    assert out["ok"]
    assert out["dry_run"] is True
    assert out["would_stop"] == ["1", "2", "3"]
    assert out["total"] == 3


def test_stop_jobs_batch_deduplicates(fake_hope):
    from hope_mcp.tools.jobs import stop_jobs_batch
    out = asyncio.run(stop_jobs_batch(runids=["1", "1", "2", "", "2"]))
    assert out["would_stop"] == ["1", "2"]
    assert out["total"] == 2


def test_stop_jobs_batch_real_stop(fake_hope):
    from hope_mcp.tools.jobs import stop_jobs_batch
    out = asyncio.run(stop_jobs_batch(runids=["a", "b"], dry_run=False))
    assert out["ok"]
    assert out["dry_run"] is False
    assert sorted(out["stopped"]) == ["a", "b"]
    assert out["failed"] == []


def test_stop_jobs_batch_partial_failure(fake_hope, monkeypatch):
    from hope_mcp.tools.jobs import stop_jobs_batch
    monkeypatch.setenv("HOPE_FAKE_RC", "1")
    monkeypatch.setenv("HOPE_FAKE_STDERR", "not found")
    out = asyncio.run(stop_jobs_batch(runids=["x", "y"], dry_run=False))
    assert out["dry_run"] is False
    assert out["stopped"] == []
    assert len(out["failed"]) == 2
    assert all(f["returncode"] == 1 for f in out["failed"])


def test_stop_jobs_batch_requires_list(fake_hope):
    from hope_mcp.tools.jobs import stop_jobs_batch
    out = asyncio.run(stop_jobs_batch(runids=[]))
    assert out["ok"] is False
