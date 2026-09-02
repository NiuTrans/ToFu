"""Pinned Codex 0.149.1 guest runner over the host-only Kimi proxy.

The Codex binary and task prompt enter the disposable task container.  The
Kimi credential, upstream URL, proxy metrics authority, and release evidence
store remain in the trusted Harbor process.  Codex can reach only the one
predeclared QEMU guestfwd endpoint supplied by ``RootlessQemuEnvironment``.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from evaluations.codex_kimi_proxy.codex_contract import (
    CODEX_VERSION,
    benchmark_trial_token,
    build_codex_command,
    verify_codex_binary,
    write_trial_proxy_metrics,
)
from evaluations.long_agent_release.codex_projection import (
    CodexProjectionError,
    project_codex_trial,
)
from rootless_vm.trajectory import write_collected_trajectory


_GUEST_ROOT = "/var/tmp/tofu-codex-kimi"
_EVIDENCE_SCHEMA = "tofu-codex-kimi-guest-evidence/v1"


def _atomic_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _private_directory(path: str | os.PathLike[str], label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    resolved = candidate.resolve(strict=True)
    info = resolved.stat()
    if not resolved.is_dir() or info.st_uid != os.getuid() \
            or info.st_mode & 0o077:
        raise PermissionError(f"{label} must be a private owner-scoped directory")
    return resolved


def _codex_atif(
    *,
    instruction: str,
    projection: dict[str, Any],
    binary_sha256: str,
    trial_token: str,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [{
        "step_id": 1,
        "source": "user",
        "message": instruction,
    }]
    for tool_index, result in enumerate(projection["toolResults"], 1):
        steps.append({
            "step_id": len(steps) + 1,
            "source": "agent",
            "model_name": "kimi-k3",
            "message": "",
            "llm_call_count": 0,
            "tool_calls": [{
                "tool_call_id": result["callId"],
                "function_name": result["toolName"],
                "arguments": {
                    "redacted": True,
                    "item_digest": result["itemDigest"],
                    "sequence": tool_index,
                },
            }],
            "observation": {"results": [{
                "source_call_id": result["callId"],
                "content": {
                    "redacted": True,
                    "result_digest": result["resultDigest"],
                    "visible_bytes": result["visibleBytes"],
                    "status": result["status"],
                },
            }]},
        })
    steps.append({
        "step_id": len(steps) + 1,
        "source": "agent",
        "model_name": "kimi-k3",
        "message": str(projection["finalOutput"]),
        "llm_call_count": len(projection["rounds"]),
        "metrics": {
            "prompt_tokens": projection["aggregateUsage"]["prompt_tokens"],
            "completion_tokens": projection["aggregateUsage"]["completion_tokens"],
            "extra": {
                "cache_read_tokens": projection["aggregateUsage"]["cache_read_tokens"],
                "reasoning_tokens": projection["aggregateUsage"]["reasoning_tokens"],
            },
        },
    })
    return {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": "codex-kimi-guest",
            "version": CODEX_VERSION,
            "model_name": "kimi-k3",
            "tool_definitions": [],
            "extra": {
                "credential_boundary": "host-loopback-proxy",
                "harness_profile": "codex-kimi",
                "binary_sha256": binary_sha256,
                "trial_token": trial_token,
            },
        },
        "steps": steps,
        "notes": (
            "Raw Codex JSONL is retained separately. Tool arguments/results are "
            "digest-only in ATIF; exact provider usage comes from tagged proxy metrics."
        ),
        "final_metrics": {
            "total_prompt_tokens": projection["aggregateUsage"]["prompt_tokens"],
            "total_completion_tokens": projection["aggregateUsage"]["completion_tokens"],
            "total_steps": len(steps),
        },
    }


class CodexKimiGuestAgent(BaseAgent):
    """Execute the pinned Codex CLI inside one disposable QEMU task guest."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        codex_binary: str,
        codex_sha256: str,
        proxy_trial_metrics_dir: str,
        proxy_service_name: str = "benchmark-proxy",
        reasoning_effort: str = "high",
        timeout_sec: int = 3600,
        sandbox: str = "danger-full-access",
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if model_name != "kimi-k3":
            raise ValueError("CodexKimiGuestAgent requires model_name='kimi-k3'")
        if not codex_sha256 or len(codex_sha256) != 64:
            raise ValueError("CodexKimiGuestAgent requires a pinned Codex SHA-256")
        self._codex_binary = str(codex_binary)
        self._codex_sha256 = str(codex_sha256).lower()
        self._proxy_metrics_dir = _private_directory(
            proxy_trial_metrics_dir, "proxy trial metrics directory"
        )
        self._proxy_service_name = str(proxy_service_name)
        if not self._proxy_service_name:
            raise ValueError("proxy_service_name must not be empty")
        self._reasoning_effort = str(reasoning_effort).lower()
        if self._reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("reasoning_effort is not supported by Codex")
        self._timeout_sec = int(timeout_sec)
        if not 1 <= self._timeout_sec <= 86_400:
            raise ValueError("timeout_sec must be between 1 and 86400")
        self._sandbox = str(sandbox)
        if self._sandbox not in {
            "read-only", "workspace-write", "danger-full-access",
        }:
            raise ValueError("sandbox is not a supported Codex policy")
        self._binary_info: dict[str, str] | None = None
        self._guest_proxy_url = ""
        self._setup_started_at_unix_ns = 0
        self._setup_completed_at_unix_ns = 0

    @staticmethod
    def name() -> str:
        return "codex-kimi-guest"

    def version(self) -> str:
        return CODEX_VERSION

    async def setup(self, environment: BaseEnvironment) -> None:
        self._setup_started_at_unix_ns = time.time_ns()
        resolver = getattr(environment, "loopback_service_url", None)
        if not callable(resolver):
            raise TypeError(
                "CodexKimiGuestAgent requires RootlessQemuEnvironment control-plane support"
            )
        self._guest_proxy_url = str(resolver(self._proxy_service_name))
        self._binary_info = await asyncio.to_thread(
            verify_codex_binary,
            self._codex_binary,
            expected_sha256=self._codex_sha256,
        )
        await environment.upload_file(
            self._binary_info["path"], f"{_GUEST_ROOT}/codex"
        )
        guest_binary = f"{_GUEST_ROOT}/codex"
        verified = await environment.exec(
            " && ".join((
                f"test \"$(sha256sum {shlex.quote(guest_binary)} | "
                "cut -d ' ' -f 1)\" = "
                f"{shlex.quote(self._binary_info['sha256'])}",
                f"chmod 0555 {shlex.quote(guest_binary)}",
            )),
            timeout_sec=120,
        )
        if int(verified.return_code) != 0:
            raise RuntimeError("uploaded Codex binary failed guest SHA-256 verification")
        self._setup_completed_at_unix_ns = time.time_ns()

    async def _wait_for_trial_metrics(
        self,
        *,
        trial_token: str,
        target: Path,
    ) -> None:
        source = self._proxy_metrics_dir / f"{trial_token}.jsonl"
        deadline = time.monotonic() + 5.0
        last_size = -1
        stable_observations = 0
        while time.monotonic() < deadline:
            try:
                size = source.stat().st_size
            except FileNotFoundError:
                size = -1
            if size > 0 and size == last_size:
                stable_observations += 1
            else:
                stable_observations = 0
            last_size = size
            if stable_observations >= 2:
                await asyncio.to_thread(
                    write_trial_proxy_metrics,
                    str(source),
                    str(target),
                    trial_token=trial_token,
                )
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("tagged Codex proxy metrics did not settle")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self._binary_info is None or not self._guest_proxy_url:
            raise RuntimeError("CodexKimiGuestAgent.setup() did not complete")
        agent_run_started_at_unix_ns = time.time_ns()
        identity = str(self.context_id or self.session_id or self.logs_dir.resolve())
        trial_token = benchmark_trial_token(
            identity,
            hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            str(agent_run_started_at_unix_ns),
        )
        evidence_dir = self.logs_dir / "codex-kimi-evidence"
        evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        evidence_dir.chmod(0o700)
        prompt_host = evidence_dir / "prompt.txt"
        raw_host = evidence_dir / "codex-events.jsonl"
        stderr_host = evidence_dir / "codex-stderr.log"
        metrics_host = evidence_dir / "proxy-metrics.jsonl"
        _atomic_private_text(prompt_host, instruction)

        guest_attempt = f"{_GUEST_ROOT}/{trial_token[:20]}"
        guest_prompt = f"{guest_attempt}/prompt.txt"
        guest_raw = f"{guest_attempt}/codex-events.jsonl"
        guest_stderr = f"{guest_attempt}/codex-stderr.log"
        await environment.upload_file(prompt_host, guest_prompt)
        command = build_codex_command(
            binary=f"{_GUEST_ROOT}/codex",
            proxy_base_url=self._guest_proxy_url,
            prompt="-",
            reasoning_effort=self._reasoning_effort,
            trial_token=trial_token,
            sandbox=self._sandbox,
        )
        command[-1:-1] = [
            "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
        ]
        rendered = shlex.join(command)
        shell_command = (
            f"umask 077 && mkdir -p {shlex.quote(guest_attempt)}/home && "
            f"{rendered} < {shlex.quote(guest_prompt)} "
            f"> {shlex.quote(guest_raw)} 2> {shlex.quote(guest_stderr)}"
        )
        result = None
        projection: dict[str, Any] | None = None
        projection_error = ""
        try:
            result = await environment.exec(
                shell_command,
                env={
                    "CODEX_HOME": f"{guest_attempt}/home",
                    "HOME": f"{guest_attempt}/home",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
                timeout_sec=self._timeout_sec,
            )
            await environment.download_file(guest_raw, raw_host)
            raw_host.chmod(0o600)
            try:
                await environment.download_file(guest_stderr, stderr_host)
                stderr_host.chmod(0o600)
            except Exception:
                _atomic_private_text(stderr_host, "")
            await self._wait_for_trial_metrics(
                trial_token=trial_token, target=metrics_host
            )
            try:
                projection = project_codex_trial(
                    raw_trajectory=raw_host,
                    proxy_metrics=metrics_host,
                )
            except CodexProjectionError as exc:
                projection_error = str(exc)
        finally:
            try:
                await environment.exec(
                    f"rm -rf {shlex.quote(guest_attempt)}",
                    timeout_sec=30,
                )
            except Exception:
                pass

        completed_at_unix_ns = time.time_ns()
        exit_code = int(result.return_code) if result is not None else -1
        metadata = {
            "contractVersion": _EVIDENCE_SCHEMA,
            "trialToken": trial_token,
            "model": "kimi-k3",
            "codexVersion": CODEX_VERSION,
            "codexBinarySha256": self._binary_info["sha256"],
            "proxyService": self._proxy_service_name,
            "credentialBoundary": "host-loopback-proxy",
            "agentSetupStartedAtUnixNs": self._setup_started_at_unix_ns,
            "agentSetupCompletedAtUnixNs": self._setup_completed_at_unix_ns,
            "agentRunStartedAtUnixNs": agent_run_started_at_unix_ns,
            "agentCompletedAtUnixNs": completed_at_unix_ns,
            "agentRunWallMs": (
                completed_at_unix_ns - agent_run_started_at_unix_ns
            ) / 1_000_000,
            "latencyScope": (
                "agent setup/run timestamps are diagnostic only; release latency "
                "uses the outer task-start to oracle-ready interval"
            ),
            "exitCode": exit_code,
            "rawTrajectory": "codex-kimi-evidence/codex-events.jsonl",
            "proxyMetrics": "codex-kimi-evidence/proxy-metrics.jsonl",
            "stderrDigest": hashlib.sha256(stderr_host.read_bytes()).hexdigest(),
            "projectionError": projection_error,
        }
        if projection is not None:
            usage = projection["aggregateUsage"]
            context.n_input_tokens = int(usage["prompt_tokens"])
            context.n_output_tokens = int(usage["completion_tokens"])
            metadata.update({
                "responsesRequests": len(projection["rounds"]),
                "finalOutputDigest": projection["finalOutputDigest"],
                "modelMs": projection["timing"]["modelMs"],
                "translationCpuMs": projection["timing"]["translationCpuMs"],
                "proxyCpuMs": projection["timing"]["proxyCpuMs"],
            })
            write_collected_trajectory(
                self.logs_dir / "trajectory.json",
                _codex_atif(
                    instruction=instruction,
                    projection=projection,
                    binary_sha256=self._binary_info["sha256"],
                    trial_token=trial_token,
                ),
            )
        context.metadata = {"codexKimiEvidence": metadata}
        if projection is None:
            raise RuntimeError(
                "Codex trial evidence could not be reconciled; retry as infrastructure"
            )
        if exit_code != 0:
            raise RuntimeError(
                "Codex CLI exited nonzero; the trial cannot be projected as complete"
            )
        if not math.isfinite(float(metadata["agentRunWallMs"])):
            raise RuntimeError("Codex trial timing is invalid")


__all__ = ["CodexKimiGuestAgent"]
