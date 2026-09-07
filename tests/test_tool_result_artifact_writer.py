"""Unit tests for the bounded incremental ToolResultArtifactWriter."""

from __future__ import annotations

import gc
import hashlib
import os
import stat
import time

import pytest

from lib.tool_result_artifact_writer import (
    TaskArtifactBudget,
    ToolResultArtifactWriter,
    cleanup_abandoned_spools,
    resolve_artifact_quota_bytes,
    resolve_task_quota_bytes,
)
from runtime_guards import SystemResourceSnapshot


pytestmark = pytest.mark.unit


class FakeRepository:
    """Duck-typed owner-scoped repository for persistence seams."""

    def __init__(self, artifact_ref="tool-result:deadbeef", *, fail=False):
        self.artifact_ref = artifact_ref
        self.fail = fail
        self.calls = []

    def put(self, *, user_id, content, media_type="text/plain"):
        self.calls.append({
            "user_id": user_id, "content": content, "media_type": media_type,
        })
        if self.fail:
            raise RuntimeError("storage unavailable")
        return {"artifactRef": self.artifact_ref,
                "sizeBytes": len(content.encode("utf-8"))}


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _writer(user_id=7, threshold=1000, repository=None, spool_dir=None,
            **kwargs):
    kwargs.setdefault("artifact_quota_bytes", 1_000_000)
    kwargs.setdefault("task_quota_bytes", 1_000_000)
    return ToolResultArtifactWriter(
        user_id=user_id, threshold_chars=threshold,
        repository=repository or FakeRepository(), spool_dir=spool_dir,
        **kwargs)


# ── Threshold behaviour ─────────────────────────────────────────────────


def test_inline_below_threshold():
    repo = FakeRepository()
    writer = _writer(repository=repo)
    content = "hello " * 50  # 300 chars < 1000

    assert writer.write(content) is True
    result = writer.finalize()

    assert result.text == content
    assert result.artifact_ref is None
    assert result.sha256 == _sha256(content)
    assert result.size_bytes == len(content.encode("utf-8"))
    assert result.spilled is False
    assert result.truncated is False
    assert result.complete is True
    assert result.degraded is False
    assert repo.calls == []


def test_at_threshold_is_not_spilled():
    repo = FakeRepository()
    threshold = 1000
    writer = _writer(threshold=threshold, repository=repo)
    content = "x" * threshold

    assert writer.write(content) is True
    result = writer.finalize()

    assert result.spilled is False
    assert result.truncated is False
    assert result.text == content
    assert result.artifact_ref is None
    assert repo.calls == []


def test_above_threshold_spills_and_persists(tmp_path):
    repo = FakeRepository(artifact_ref="tool-result:deadbeef")
    threshold = 1000
    spool_dir = str(tmp_path)
    writer = _writer(threshold=threshold, repository=repo,
                     spool_dir=spool_dir)
    content = "A" * 4000 + "M" * 5000 + "B" * 2000

    assert writer.write(content) is True
    assert writer.spooled is True
    result = writer.finalize()

    assert result.spilled is True
    assert result.truncated is True
    assert result.artifact_ref == "tool-result:deadbeef"
    assert result.sha256 == _sha256(content)
    assert result.size_bytes == len(content.encode("utf-8"))
    assert result.complete is True
    assert result.degraded is False
    assert result.text.startswith("A" * (threshold * 3 // 4))
    assert result.text.endswith("B" * (threshold // 4))
    assert "output truncated" in result.text
    assert len(repo.calls) == 1
    assert repo.calls[0]["user_id"] == 7
    assert repo.calls[0]["content"] == content
    assert os.listdir(spool_dir) == []  # spool removed after finalize


# ── Partial finalization ────────────────────────────────────────────────


def test_partial_finalization_persists_and_flags(tmp_path):
    repo = FakeRepository()
    writer = _writer(repository=repo, spool_dir=str(tmp_path))
    writer.write("x" * 5000)

    result = writer.finalize_partial()

    assert result.complete is False
    assert result.spilled is True
    assert result.artifact_ref == "tool-result:deadbeef"
    assert len(repo.calls) == 1


# ── Quota failure degrades to bounded preview ───────────────────────────


def test_artifact_quota_degrades_to_preview():
    repo = FakeRepository()
    writer = ToolResultArtifactWriter(
        user_id=7, threshold_chars=100, artifact_quota_bytes=50,
        task_quota_bytes=1_000_000, repository=repo)

    assert writer.write("a" * 50) is True
    assert writer.write("b") is False
    assert writer.degraded is True

    result = writer.finalize()
    assert result.degraded is True
    assert result.degraded_reason == "quota"
    assert result.artifact_ref is None
    assert result.sha256 is None
    assert result.text == "a" * 50
    assert result.truncated is False  # accepted content still fits inline
    assert result.size_bytes == 50
    assert repo.calls == []


def test_shared_task_budget_is_aggregate(tmp_path):
    repo = FakeRepository()
    budget = TaskArtifactBudget(100)
    writer_a = ToolResultArtifactWriter(
        user_id=7, threshold_chars=1000, artifact_quota_bytes=1_000_000,
        task_budget=budget, repository=repo, spool_dir=str(tmp_path))
    writer_b = ToolResultArtifactWriter(
        user_id=7, threshold_chars=1000, artifact_quota_bytes=1_000_000,
        task_budget=budget, repository=repo, spool_dir=str(tmp_path))

    assert writer_a.write("a" * 60) is True
    assert writer_b.write("b" * 50) is False
    assert budget.used_bytes == 60
    assert writer_b.degraded is True

    writer_a.finalize()
    assert budget.used_bytes == 0


# ── Disk failure degrades to bounded preview ────────────────────────────


def test_disk_failure_degrades_to_preview(tmp_path, monkeypatch):
    import lib.tool_result_artifact_writer as module

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module.tempfile, "mkstemp", _boom)
    repo = FakeRepository()
    threshold = 1000
    writer = _writer(threshold=threshold, repository=repo,
                     spool_dir=str(tmp_path))

    assert writer.write("x" * 2000) is True  # accepted, spill then degrades
    assert writer.degraded is True
    assert writer.write("more") is False

    result = writer.finalize()
    assert result.degraded is True
    assert result.degraded_reason == "disk"
    assert result.artifact_ref is None
    assert result.sha256 is None
    assert result.truncated is True
    assert result.text.startswith("x" * (threshold * 3 // 4))
    assert result.text.endswith("x" * (threshold // 4))
    assert repo.calls == []


# ── Cleanup ─────────────────────────────────────────────────────────────


def test_discard_removes_spool(tmp_path):
    repo = FakeRepository()
    spool_dir = str(tmp_path)
    writer = _writer(repository=repo, spool_dir=spool_dir)
    writer.write("x" * 5000)
    assert writer.spooled is True
    assert len(os.listdir(spool_dir)) == 1

    writer.discard()

    assert writer.discarded is True
    assert os.listdir(spool_dir) == []
    assert repo.calls == []


def test_cleanup_abandoned_spools(tmp_path):
    spool_dir = str(tmp_path)
    stale = os.path.join(spool_dir, "tofu-tool-result-stale.spool")
    fresh = os.path.join(spool_dir, "tofu-tool-result-fresh.spool")
    other = os.path.join(spool_dir, "unrelated.txt")
    for path in (stale, fresh, other):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
    now = time.time()
    os.utime(stale, (now - 7200, now - 7200))
    os.utime(fresh, (now, now))

    removed = cleanup_abandoned_spools(
        spool_dir=spool_dir, max_age_seconds=3600, now=now)

    assert removed == 1
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)
    assert os.path.exists(other)


def test_context_manager_cleans_on_exception(tmp_path):
    repo = FakeRepository()
    spool_dir = str(tmp_path)
    with pytest.raises(RuntimeError):
        with _writer(repository=repo, spool_dir=spool_dir) as writer:
            writer.write("x" * 5000)
            raise RuntimeError("boom")
    assert os.listdir(spool_dir) == []
    assert repo.calls == []


# ── Permissions and owner validation ────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_spool_has_restrictive_permissions(tmp_path):
    writer = _writer(spool_dir=str(tmp_path))
    writer.write("x" * 5000)
    (name,) = os.listdir(tmp_path)
    mode = stat.S_IMODE(os.stat(os.path.join(tmp_path, name)).st_mode)
    assert mode == 0o600
    writer.discard()


@pytest.mark.parametrize("user_id", [0, -1, -100])
def test_owner_must_be_positive(user_id):
    with pytest.raises(ValueError):
        ToolResultArtifactWriter(
            user_id=user_id, artifact_quota_bytes=100, task_quota_bytes=100)


# ── Launch resource-budget seams ────────────────────────────────────────


def _snapshot():
    return SystemResourceSnapshot(
        host_cpu_count=8, affinity_cpu_count=8, cgroup_cpu_count=None,
        effective_cpu_count=8, host_memory_total_mb=8192,
        host_memory_available_mb=4096, cgroup_memory_limit_mb=None,
        cgroup_memory_current_mb=None, effective_memory_capacity_mb=8192,
        effective_memory_available_mb=4096, disk_total_mb=100_000,
        disk_free_mb=50_000)


def test_quota_resolvers_are_bounded():
    snapshot = _snapshot()

    huge_artifact = resolve_artifact_quota_bytes(
        {"TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB": "999999"}, snapshot=snapshot)
    assert huge_artifact == 16 * 1024 * 1024

    small_artifact = resolve_artifact_quota_bytes(
        {"TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB": "2"}, snapshot=snapshot)
    assert small_artifact == 2 * 1024 * 1024

    huge_task = resolve_task_quota_bytes(
        {"TOFU_BROWSER_STAGING_MAX_MIB": "999999"}, snapshot=snapshot)
    assert huge_task == 4096 * 1024 * 1024


class _FailingSpool:
    """Duck-typed spool whose write/read raise the requested OSError."""

    def __init__(self, *, fail_write=False, fail_read=False):
        self.fail_write = fail_write
        self.fail_read = fail_read

    def write(self, text):
        if self.fail_write:
            raise OSError("disk full")
        return len(text)

    def flush(self):
        return None

    def seek(self, offset, whence=0):
        return 0

    def read(self):
        if self.fail_read:
            raise OSError("read failed")
        return ""

    def close(self):
        return None


def test_persist_failure_degrades_to_preview(tmp_path):
    repo = FakeRepository(fail=True)
    spool_dir = str(tmp_path)
    writer = _writer(repository=repo, spool_dir=spool_dir)
    assert writer.write("A" * 4000 + "Z" * 2000) is True

    result = writer.finalize()

    assert result.degraded is True
    assert result.degraded_reason == "persist"
    assert result.artifact_ref is None
    assert result.sha256 is None
    assert result.truncated is True
    assert result.spilled is True
    assert result.text.startswith("A" * 750)
    assert result.text.endswith("Z" * 250)
    assert len(repo.calls) == 1
    assert os.listdir(spool_dir) == []


def test_empty_artifact_ref_degrades_to_persist(tmp_path):
    repo = FakeRepository(artifact_ref="")
    writer = _writer(repository=repo, spool_dir=str(tmp_path))
    assert writer.write("x" * 5000) is True

    result = writer.finalize()

    assert result.degraded is True
    assert result.degraded_reason == "persist"
    assert result.artifact_ref is None
    assert result.sha256 is None


def test_spool_write_failure_degrades_to_preview(tmp_path):
    repo = FakeRepository()
    spool_dir = str(tmp_path)
    writer = _writer(repository=repo, spool_dir=spool_dir)
    assert writer.write("x" * 5000) is True
    assert writer.spooled is True

    real_spool = writer._spool
    real_spool.close()
    writer._spool = _FailingSpool(fail_write=True)

    assert writer.write("x") is False
    assert writer.degraded is True

    result = writer.finalize()
    assert result.degraded is True
    assert result.degraded_reason == "disk"
    assert result.artifact_ref is None
    assert result.sha256 is None
    assert result.truncated is True
    assert result.text.startswith("x" * 750)
    assert result.text.endswith("x" * 250)
    assert repo.calls == []
    assert os.listdir(spool_dir) == []


def test_spool_read_failure_degrades_to_preview(tmp_path):
    repo = FakeRepository()
    spool_dir = str(tmp_path)
    writer = _writer(repository=repo, spool_dir=spool_dir)
    assert writer.write("x" * 5000) is True
    assert writer.spooled is True

    real_spool = writer._spool
    real_spool.close()
    writer._spool = _FailingSpool(fail_read=True)

    result = writer.finalize()

    assert result.degraded is True
    assert result.degraded_reason == "disk"
    assert result.artifact_ref is None
    assert result.sha256 is None
    assert result.truncated is True
    assert "output truncated" in result.text
    assert repo.calls == []
    assert os.listdir(spool_dir) == []


def test_discard_releases_reservation(tmp_path):
    repo = FakeRepository()
    budget = TaskArtifactBudget(100)
    writer = ToolResultArtifactWriter(
        user_id=7, threshold_chars=1000, artifact_quota_bytes=1_000_000,
        task_budget=budget, repository=repo, spool_dir=str(tmp_path))
    assert writer.write("x" * 60) is True
    assert budget.used_bytes == 60

    writer.discard()

    assert budget.used_bytes == 0
    assert writer.discarded is True


def test_writer_del_releases_reservation(tmp_path):
    repo = FakeRepository()
    budget = TaskArtifactBudget(100)
    writer = ToolResultArtifactWriter(
        user_id=7, threshold_chars=1000, artifact_quota_bytes=1_000_000,
        task_budget=budget, repository=repo, spool_dir=str(tmp_path))
    assert writer.write("x" * 60) is True
    assert budget.used_bytes == 60

    del writer
    gc.collect()

    assert budget.used_bytes == 0


def test_spilled_preview_projection_preserves_terminal_and_provenance():
    from lib.tasks_pkg.handlers.code_exec import (
        _project_output_artifact,
        _register_output_artifact_origin,
    )
    from lib.tool_result_artifacts import artifact_provenance

    artifact = _writer(
        threshold=10,
        repository=FakeRepository('tool-result:preview-ref'),
    )
    artifact.write('A' * 30 + 'B' * 30)
    result = artifact.finalize(complete=False)
    task = {}
    round_entry = {'llmRound': 4}

    projected = _project_output_artifact(
        '$ printf lots\nlegacy bounded body\n'
        '[Command timed out]\n[exit code: -1]',
        result,
    )
    _register_output_artifact_origin(
        task, round_entry, result,
        tool_name='run_command', display='printf lots', tool_call_id='tc-1')

    assert projected.startswith('$ printf lots\n' + result.text)
    assert 'legacy bounded body' not in projected
    assert 'cancellation-partial' in projected
    assert projected.endswith('[Command timed out]\n[exit code: -1]')
    assert artifact_provenance(task, 'tool-result:preview-ref') == {
        'toolName': 'run_command',
        'display': 'printf lots',
        'llmRound': 4,
        'toolCallId': 'tc-1',
    }


def test_utf8_multibyte_bytes_and_hash(tmp_path):
    repo = FakeRepository()
    content = "é" * 4000  # 4000 chars / 8000 UTF-8 bytes
    writer = _writer(threshold=1000, repository=repo, spool_dir=str(tmp_path))
    assert writer.write(content) is True

    result = writer.finalize()

    assert result.spilled is True
    assert result.size_bytes == len(content.encode("utf-8")) == 8000
    assert result.sha256 == _sha256(content)
    assert repo.calls[0]["content"] == content


def test_quota_is_utf8_bytes_not_chars():
    repo = FakeRepository()
    writer = ToolResultArtifactWriter(
        user_id=7, threshold_chars=1000, artifact_quota_bytes=100,
        task_quota_bytes=1_000_000, repository=repo)

    assert writer.write("é" * 50) is True  # 50 chars, 100 bytes
    assert writer.write("a") is False      # 101st byte rejected

    result = writer.finalize()
    assert result.degraded is True
    assert result.degraded_reason == "quota"
    assert result.text == "é" * 50
    assert result.size_bytes == 100
    assert repo.calls == []
