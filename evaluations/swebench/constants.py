from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


FRAMEWORK_VERSION = "1.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Harbor 0.21 is the version whose config schema and SWE-bench adapter parity
# were audited while this runner was implemented.  Patch upgrades are allowed;
# a minor upgrade requires re-running the compatibility tests first.
HARBOR_MIN_VERSION = (0, 21, 0)
HARBOR_MAX_VERSION = (0, 22, 0)
HARBOR_COMMIT = "ea2fee78517f2e591bad69fcf1e6731f9c23ec99"
HARBOR_REQUIREMENT = (
    "harbor @ "
    "git+https://github.com/harbor-framework/harbor.git@"
    + HARBOR_COMMIT
)
HARBOR_CLOUD_REQUIREMENT = (
    "harbor[modal,daytona,e2b,runloop,novita] @ "
    "git+https://github.com/harbor-framework/harbor.git@"
    + HARBOR_COMMIT
)
SWEBENCH_REQUIREMENT = "swebench==4.1.0"
SWEBENCH_VERSION = "4.1.0"


@dataclass(frozen=True)
class BenchmarkDefinition:
    key: str
    dataset: str
    task_count: int
    dataset_source_revision: str
    default_attempts: int
    official_min_attempts: int
    source_url: str
    source_commit: str


HARBOR_DATASET_COMMIT = "86723674f04e4209ac479d0fb75d9d9f44b4377e"
OFFICIAL_DATASET = "SWE-bench/SWE-bench_Verified"
TBENCH21_DATASET_REF = (
    "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
)
TBENCH21_REPOSITORY_COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
TBENCH21_TASK_DIGESTS_PATH = Path(__file__).with_name(
    "terminal_bench_21_task_digests.json"
)


def terminal_bench_21_task_digests() -> dict[str, str]:
    value = json.loads(TBENCH21_TASK_DIGESTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(ref, str)
        for name, ref in value.items()
    ):
        raise ValueError("invalid Terminal-Bench 2.1 task digest lock")
    return value

BENCHMARKS = {
    "swebench-verified": BenchmarkDefinition(
        key="swebench-verified",
        dataset="swebench-verified@1.0",
        task_count=500,
        dataset_source_revision=HARBOR_DATASET_COMMIT,
        default_attempts=1,
        official_min_attempts=1,
        source_url="https://github.com/harbor-framework/harbor/tree/main/adapters/swebench",
        source_commit=HARBOR_COMMIT,
    ),
    "terminal-bench-2.1": BenchmarkDefinition(
        key="terminal-bench-2.1",
        dataset=f"terminal-bench/terminal-bench-2-1@{TBENCH21_DATASET_REF}",
        task_count=89,
        dataset_source_revision=TBENCH21_DATASET_REF,
        default_attempts=5,
        official_min_attempts=5,
        source_url="https://github.com/harbor-framework/terminal-bench-2-1",
        source_commit=TBENCH21_REPOSITORY_COMMIT,
    ),
}
DEFAULT_BENCHMARK = "swebench-verified"

# Compatibility names for the upstream SWE-bench patch-grading path.
DEFAULT_DATASET = BENCHMARKS[DEFAULT_BENCHMARK].dataset
DEFAULT_DATASET_SIZE = BENCHMARKS[DEFAULT_BENCHMARK].task_count

# Deliberately excludes direct host/udocker/proot modes. Singularity provides
# local filesystem/PID isolation but is separately constrained to serial runs
# because it shares host networking and lacks strict per-trial cgroups.
ISOLATED_BACKENDS = (
    "singularity",
    "modal",
    "daytona",
    "e2b",
    "runloop",
    "novita",
    "docker",
)
LOCAL_BACKENDS = ("singularity", "docker")
DEFAULT_AGENT_BACKEND = "singularity"


def default_output_root() -> Path:
    configured = os.environ.get("TOFU_EVAL_ROOT")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "tofu-evals" / "agent-benchmarks"
