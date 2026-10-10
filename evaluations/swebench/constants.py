"""Pinned Harbor adapter dependencies; benchmark identities live in evaluations.catalog."""
from __future__ import annotations

import os
from pathlib import Path

from evaluations.catalog import BENCHMARKS, BenchmarkDefinition as BenchmarkDefinition, load_task_digest_lock

FRAMEWORK_VERSION = "1.4.0"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Minor upgrades require rerunning the Harbor compatibility tests.
HARBOR_MIN_VERSION = (0, 21, 0)
HARBOR_MAX_VERSION = (0, 22, 0)
HARBOR_COMMIT = "ea2fee78517f2e591bad69fcf1e6731f9c23ec99"
HARBOR_REQUIREMENT = (
    "harbor @ git+https://github.com/harbor-framework/harbor.git@" + HARBOR_COMMIT
)
HARBOR_CLOUD_REQUIREMENT = (
    "harbor[modal,daytona,e2b,runloop,novita] @ "
    "git+https://github.com/harbor-framework/harbor.git@" + HARBOR_COMMIT
)
SWEBENCH_REQUIREMENT = "swebench==4.1.0"
SWEBENCH_VERSION = "4.1.0"
OFFICIAL_DATASET = "SWE-bench/SWE-bench_Verified"

# Compatibility exports for release evidence consumers; never duplicate locks.
SWEBENCH_VERIFIED_DATASET_REF = BENCHMARKS["swebench-verified"].dataset_source_revision
TBENCH21_DATASET_REF = BENCHMARKS["terminal-bench-2.1"].dataset_source_revision
TBENCH21_REPOSITORY_COMMIT = BENCHMARKS["terminal-bench-2.1"].source_commit
SWEBENCH_VERIFIED_TASK_DIGESTS_PATH = BENCHMARKS["swebench-verified"].task_digest_lock
TBENCH21_TASK_DIGESTS_PATH = BENCHMARKS["terminal-bench-2.1"].task_digest_lock
_load_task_digest_lock = load_task_digest_lock


def swebench_verified_task_digests() -> dict[str, str]:
    return BENCHMARKS["swebench-verified"].task_digests()


def terminal_bench_21_task_digests() -> dict[str, str]:
    return BENCHMARKS["terminal-bench-2.1"].task_digests()


DEFAULT_BENCHMARK = "swebench-verified"
DEFAULT_DATASET = BENCHMARKS[DEFAULT_BENCHMARK].dataset
DEFAULT_DATASET_SIZE = BENCHMARKS[DEFAULT_BENCHMARK].task_count

# Never enable direct host/udocker/proot modes. Singularity is serial-only.
ISOLATED_BACKENDS = (
    "rootless-qemu", "singularity", "modal", "daytona", "e2b", "runloop",
    "novita", "docker",
)
LOCAL_BACKENDS = ("rootless-qemu", "singularity", "docker")
DEFAULT_AGENT_BACKEND = "rootless-qemu"


def default_output_root() -> Path:
    """Stay on the checkout filesystem (DolphinFS here), never ambient HOME/tmp."""
    configured = os.environ.get("TOFU_EVAL_ROOT")
    return Path(configured).expanduser() if configured else PROJECT_ROOT / ".eval-runs" / "benchmarks"
