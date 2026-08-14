from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from .artifacts import output_guard_status
from .constants import (
    BENCHMARKS,
    DEFAULT_BENCHMARK,
    HARBOR_MAX_VERSION,
    HARBOR_MIN_VERSION,
    ISOLATED_BACKENDS,
    PROJECT_ROOT,
    SWEBENCH_VERSION,
    terminal_bench_21_task_digests,
)
from .process import resolve_executable, singularity_runtime


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _tool_version(executable: str) -> tuple[tuple[int, int, int] | None, str]:
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    rendered = (result.stdout or result.stderr).strip()
    return _version_tuple(rendered), rendered


def _backend_check(backend: str) -> Check:
    if backend not in ISOLATED_BACKENDS:
        return Check("isolation_backend", "fail", f"unsupported or non-isolating backend: {backend}")
    if backend == "docker":
        docker = shutil.which("docker")
        if not docker:
            return Check("backend_credentials", "fail", "docker executable not found")
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("backend_credentials", "fail", f"docker daemon probe failed: {exc}")
        if result.returncode:
            return Check(
                "backend_credentials", "fail", f"docker daemon unavailable: {(result.stderr or result.stdout).strip()}"
            )
        return Check("backend_credentials", "pass", f"docker daemon {result.stdout.strip()}")

    if backend == "singularity":
        runtime = singularity_runtime()
        if runtime is None:
            return Check(
                "local_runtime",
                "fail",
                "singularity/apptainer not found; install it outside the app environment",
            )
        try:
            version_result = subprocess.run(
                [runtime, "version"], capture_output=True, text=True, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("local_runtime", "fail", f"runtime probe failed: {exc}")
        if version_result.returncode:
            return Check(
                "local_runtime",
                "fail",
                (version_result.stderr or version_result.stdout).strip(),
            )

        prefix = Path(runtime).resolve().parent.parent
        starters = (
            prefix / "libexec" / "apptainer" / "bin" / "starter-suid",
            prefix / "libexec" / "singularity" / "bin" / "starter-suid",
            Path("/usr/libexec/apptainer/bin/starter-suid"),
            Path("/usr/libexec/singularity/bin/starter-suid"),
        )
        has_setuid_starter = any(
            path.is_file() and path.stat().st_mode & stat.S_ISUID
            for path in starters
        )
        if not has_setuid_starter:
            unshare = shutil.which("unshare")
            if unshare is None:
                return Check(
                    "local_runtime",
                    "fail",
                    "rootless runtime requires unshare or an administrator-installed setuid runtime",
                )
            namespace_result = subprocess.run(
                [
                    unshare,
                    "-Ur",
                    "-m",
                    "--propagation",
                    "private",
                    "true",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if namespace_result.returncode:
                detail = (namespace_result.stderr or namespace_result.stdout).strip()
                return Check(
                    "local_runtime",
                    "fail",
                    "host blocks the mount namespace/propagation required by "
                    f"rootless Apptainer: {detail}",
                )
        session_size_result = subprocess.run(
            [runtime, "config", "global", "--get", "sessiondir max size"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        try:
            session_size_mb = int(session_size_result.stdout.strip())
        except ValueError:
            return Check(
                "local_runtime",
                "fail",
                "cannot read Apptainer 'sessiondir max size'; require at least 10240 MiB",
            )
        if session_size_mb < 10240:
            return Check(
                "local_runtime",
                "fail",
                "Apptainer 'sessiondir max size' is "
                f"{session_size_mb} MiB; benchmark tasks require at least 10240 MiB",
            )
        rendered = (version_result.stdout or version_result.stderr).strip()
        return Check(
            "local_runtime",
            "pass",
            f"{Path(runtime).name} {rendered}; sessiondir={session_size_mb} MiB; "
            "local serial execution",
        )

    sdk_modules = {
        "modal": "modal",
        "daytona": "daytona",
        "e2b": "e2b",
        "runloop": "runloop_api_client",
        "novita": "novita_sandbox",
    }
    module = sdk_modules[backend]
    if importlib.util.find_spec(module) is None:
        return Check(
            "backend_sdk",
            "fail",
            f"{backend}: Python module {module!r} missing; install "
            "evaluations/swebench/requirements-cloud.txt",
        )

    credentials = {
        "modal": (
            bool(
                (os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"))
                or (Path.home() / ".modal.toml").is_file()
            ),
            "MODAL_TOKEN_ID + MODAL_TOKEN_SECRET or ~/.modal.toml",
        ),
        "daytona": (
            bool(
                os.environ.get("DAYTONA_API_KEY")
                or (
                    os.environ.get("DAYTONA_JWT_TOKEN")
                    and os.environ.get("DAYTONA_ORGANIZATION_ID")
                )
            ),
            "DAYTONA_API_KEY or DAYTONA_JWT_TOKEN + DAYTONA_ORGANIZATION_ID",
        ),
        "e2b": (bool(os.environ.get("E2B_API_KEY")), "E2B_API_KEY"),
        "runloop": (bool(os.environ.get("RUNLOOP_API_KEY")), "RUNLOOP_API_KEY"),
        "novita": (bool(os.environ.get("NOVITA_API_KEY")), "NOVITA_API_KEY"),
    }
    present, expected = credentials[backend]
    return Check(
        "backend_credentials",
        "pass" if present else "fail",
        f"{backend}: {expected}" + (" found" if present else " missing"),
    )


def _free_bytes_for(root: Path) -> int:
    probe = root.expanduser().resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _benchmark_check(benchmark: str) -> Check:
    definition = BENCHMARKS.get(benchmark)
    if definition is None:
        return Check("benchmark", "fail", f"unsupported benchmark: {benchmark!r}")
    if benchmark != "terminal-bench-2.1":
        return Check(
            "benchmark",
            "pass",
            f"{definition.dataset} ({definition.task_count} tasks, audited source)",
        )
    try:
        from harbor.registry.client.package import PackageDatasetClient

        async def fetch_metadata():
            return await PackageDatasetClient().get_dataset_metadata(definition.dataset)

        metadata = asyncio.run(fetch_metadata())
    except Exception as exc:
        return Check(
            "benchmark",
            "fail",
            f"cannot resolve pinned Terminal-Bench 2.1 metadata: {exc}",
        )
    actual_digests = {
        task_id.get_name(): str(task_id.ref)
        for task_id in metadata.task_ids
    }
    locked_digests = terminal_bench_21_task_digests()
    actual_count = len(actual_digests)
    actual_ref = str(metadata.version)
    valid = (
        actual_count == definition.task_count
        and actual_ref == definition.dataset_source_revision
        and actual_digests == locked_digests
    )
    return Check(
        "benchmark",
        "pass" if valid else "fail",
        f"{metadata.name}@{actual_ref}: {actual_count} tasks, task digests "
        + ("match" if actual_digests == locked_digests else "DO NOT MATCH"),
    )


def harbor_checks(
    *,
    backend: str,
    output_root: Path,
    benchmark: str = DEFAULT_BENCHMARK,
    harbor_bin: str = "harbor",
) -> list[Check]:
    checks: list[Check] = []
    executable = resolve_executable(harbor_bin)
    if not executable or not Path(executable).is_file():
        checks.append(Check("harbor", "fail", f"{harbor_bin!r} not found; install the pinned eval requirements"))
    else:
        version, rendered = _tool_version(executable)
        if version is None:
            checks.append(Check("harbor", "fail", f"could not parse version: {rendered}"))
        elif not (HARBOR_MIN_VERSION <= version < HARBOR_MAX_VERSION):
            checks.append(
                Check(
                    "harbor",
                    "fail",
                    f"found {version}, require >= {HARBOR_MIN_VERSION} and < {HARBOR_MAX_VERSION}",
                )
            )
        else:
            checks.append(Check("harbor", "pass", rendered))
    checks.append(_benchmark_check(benchmark))
    checks.append(_backend_check(backend))
    guarded, detail = output_guard_status(output_root, PROJECT_ROOT)
    checks.append(Check("artifact_location", "pass" if guarded else "fail", detail))
    free = _free_bytes_for(output_root)
    threshold = 120 * 1024**3 if backend in {"docker", "singularity"} else 2 * 1024**3
    checks.append(
        Check(
            "free_space",
            "pass" if free >= threshold else "fail",
            f"{free / 1024**3:.1f} GiB free; require {threshold / 1024**3:.0f} GiB for {backend}",
        )
    )
    return checks


def official_checks(*, backend: str, output_root: Path) -> list[Check]:
    if backend not in {"modal", "docker"}:
        checks = [
            Check(
                "isolation_backend",
                "fail",
                "the upstream SWE-bench harness supports only modal or docker",
            )
        ]
    else:
        checks = [_backend_check(backend)]
    guarded, detail = output_guard_status(output_root, PROJECT_ROOT)
    checks.append(Check("artifact_location", "pass" if guarded else "fail", detail))
    free = _free_bytes_for(output_root)
    threshold = 120 * 1024**3 if backend == "docker" else 2 * 1024**3
    checks.append(
        Check(
            "free_space",
            "pass" if free >= threshold else "fail",
            f"{free / 1024**3:.1f} GiB free; require {threshold / 1024**3:.0f} GiB for {backend}",
        )
    )
    try:
        version = metadata.version("swebench")
    except metadata.PackageNotFoundError:
        checks.append(Check("swebench", "fail", "package not installed; install evaluations/swebench/requirements.txt"))
    else:
        checks.append(
            Check(
                "swebench",
                "pass" if version == SWEBENCH_VERSION else "fail",
                f"found {version}; reproducibility pin is {SWEBENCH_VERSION}",
            )
        )
    return checks


def render_checks(checks: list[Check], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(
            {"ok": not any(check.failed for check in checks), "checks": [asdict(check) for check in checks]},
            ensure_ascii=False,
            indent=2,
        )
    return "\n".join(f"[{check.status.upper():4}] {check.name}: {check.detail}" for check in checks)
