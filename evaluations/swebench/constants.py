from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


FRAMEWORK_VERSION = "1.3.0"
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


SWEBENCH_VERIFIED_DATASET_REF = (
    "sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341"
)
OFFICIAL_DATASET = "SWE-bench/SWE-bench_Verified"
TBENCH21_DATASET_REF = (
    "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
)
TBENCH21_REPOSITORY_COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"
SWEBENCH_VERIFIED_TASK_DIGESTS_PATH = Path(__file__).with_name(
    "swebench_verified_task_digests.json"
)
TBENCH21_TASK_DIGESTS_PATH = Path(__file__).with_name(
    "terminal_bench_21_task_digests.json"
)


def _load_task_digest_lock(
    path: Path,
    *,
    label: str,
    expected_count: int,
) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    valid = (
        isinstance(value, dict)
        and len(value) == expected_count
        and list(value) == sorted(value)
        and all(
            isinstance(name, str)
            and bool(name)
            and isinstance(ref, str)
            and ref.startswith("sha256:")
            and len(ref) == 71
            and all(char in "0123456789abcdef" for char in ref[7:])
            for name, ref in value.items()
        )
    )
    if not valid:
        raise ValueError(f"invalid {label} task digest lock")
    return value


def swebench_verified_task_digests() -> dict[str, str]:
    return _load_task_digest_lock(
        SWEBENCH_VERIFIED_TASK_DIGESTS_PATH,
        label="SWE-bench Verified",
        expected_count=500,
    )


def terminal_bench_21_task_digests() -> dict[str, str]:
    return _load_task_digest_lock(
        TBENCH21_TASK_DIGESTS_PATH,
        label="Terminal-Bench 2.1",
        expected_count=89,
    )

BENCHMARKS = {
    "swebench-verified": BenchmarkDefinition(
        key="swebench-verified",
        dataset=f"swe-bench/swe-bench-verified@{SWEBENCH_VERIFIED_DATASET_REF}",
        task_count=500,
        dataset_source_revision=SWEBENCH_VERIFIED_DATASET_REF,
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
    "rootless-qemu",
    "singularity",
    "modal",
    "daytona",
    "e2b",
    "runloop",
    "novita",
    "docker",
)
LOCAL_BACKENDS = ("rootless-qemu", "singularity", "docker")
DEFAULT_AGENT_BACKEND = "rootless-qemu"


def default_output_root() -> Path:
    configured = os.environ.get("TOFU_EVAL_ROOT")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "tofu-evals" / "agent-benchmarks"
