"""A local model is inert until explicitly selected and its bytes are verified."""

import builtins
import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tofu_search import local_rerank
from tofu_search._local_rerank_worker import WorkerFailure, score_request, verify_snapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cpus,threads", [(1, 1), (8, 2), (64, 16)])
@pytest.mark.parametrize("available_mb", [None, 1, 65536])
def test_host_capacity_sizes_default_threads_only_never_a_gate_or_allowance(
    monkeypatch, tmp_path, cpus, threads, available_mb
):
    monkeypatch.setattr(
        local_rerank,
        "probe_resources",
        lambda _path: SimpleNamespace(
            effective_cpu_count=cpus, effective_memory_available_mb=available_mb
        ),
    )
    ranker = local_rerank.LocalReranker(tmp_path)
    assert ranker.threads == threads
    assert ranker.max_parallel is None and ranker._slots is None
    assert ranker.max_rss_bytes is None


def test_only_explicit_allowances_gate_or_refuse(monkeypatch, tmp_path):
    monkeypatch.setattr(
        local_rerank,
        "probe_resources",
        lambda _path: SimpleNamespace(effective_cpu_count=1, effective_memory_available_mb=None),
    )
    configured = local_rerank.LocalReranker(tmp_path, threads=3, max_parallel=2, max_rss_mb=8192)
    assert (configured.threads, configured.max_parallel, configured.max_rss_bytes) == (
        3,
        2,
        8192 * 1024 * 1024,
    )
    below_working_set = local_rerank.LocalReranker(tmp_path, max_rss_mb=1)
    with pytest.raises(local_rerank.LocalRerankError) as error:
        below_working_set.score("query", ["document"])
    assert error.value.kind == "insufficient_memory"


def test_snapshot_size_and_digest_are_both_required(tmp_path):
    path = tmp_path / "weights.safetensors"
    path.write_bytes(b"trusted bytes")
    artifact = {
        "source": {
            "files": [
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ]
        }
    }
    verify_snapshot(tmp_path, artifact)
    path.write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="hash"):
        verify_snapshot(tmp_path, artifact)
    path.write_bytes(b"short")
    with pytest.raises(ValueError, match="size"):
        verify_snapshot(tmp_path, artifact)


def test_invalid_snapshot_fails_before_any_model_dependency_load(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in ("torch", "transformers"), "unverified artifact reached model imports"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(WorkerFailure, match="snapshot_invalid"):
        score_request(
            {
                "model_dir": str(tmp_path),
                "artifact": {
                    "source": {
                        "files": [{"name": "missing.safetensors", "bytes": 1, "sha256": "0" * 64}]
                    }
                },
            }
        )


def test_public_import_is_lazy_and_needs_no_host_or_model_runtime(tmp_path):
    package_root = Path(__file__).resolve().parents[1]
    script = (
        "import sys\nsys.path.insert(0, sys.argv[1])\n"
        "import tofu_search\n"
        "assert 'tofu_search.local_rerank' not in sys.modules\n"
        "from tofu_search import LocalReranker\n"
        "assert not {'lib', 'torch', 'transformers', 'jsonschema'} & set(sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(package_root)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
