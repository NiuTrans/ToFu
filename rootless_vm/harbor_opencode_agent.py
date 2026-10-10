"""Pinned OpenCode guest runner over the host-only GLM benchmark proxy.

The OpenCode binary, the offline provider-package cache seed, and the task
prompt enter the disposable task guest.  The GLM credential, upstream URL,
proxy metrics authority, and release evidence store remain in the trusted
Harbor process.  OpenCode can reach only the one predeclared QEMU guestfwd
endpoint supplied by ``RootlessQemuEnvironment``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shlex
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from rootless_vm.host_paths import private_temporary_root

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from evaluations.codex_kimi_proxy.codex_contract import (
    benchmark_trial_token,
    write_trial_proxy_metrics,
)
from evaluations.opencode_contract import (
    OPENCODE_TRIAL_IDENTITY_FIELD,
    OPENCODE_PROVIDER_NPM_VERSION,
    OPENCODE_VERSION,
    guest_config,
    guest_shell_command,
    verify_cache_seed,
    verify_opencode_binary,
)
from evaluations.opencode_proxy.projection import (
    OpenCodeProjectionError,
    project_opencode_trial,
)
from rootless_vm.harbor_codex_kimi_agent import (
    _atomic_private_text,
    _private_directory,
)
from rootless_vm.trajectory import write_collected_trajectory


_GUEST_ROOT = "/var/tmp/tofu-opencode"
_EVIDENCE_SCHEMA = "tofu-opencode-guest-evidence/v1"


def _opencode_atif(
    *,
    instruction: str,
    projection: dict[str, Any],
    binary_sha256: str,
    trial_token: str,
    model: str,
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
            "model_name": model,
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
        "model_name": model,
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
            "name": "opencode-guest",
            "version": OPENCODE_VERSION,
            "model_name": model,
            "tool_definitions": [],
            "extra": {
                "credential_boundary": "host-loopback-proxy",
                "harness_profile": "opencode",
                "binary_sha256": binary_sha256,
                "provider_npm_version": OPENCODE_PROVIDER_NPM_VERSION,
                OPENCODE_TRIAL_IDENTITY_FIELD: trial_token,
            },
        },
        "steps": steps,
        "notes": (
            "Raw OpenCode JSON events are retained separately. Tool "
            "arguments/results are digest-only in ATIF; exact provider usage "
            "comes from tagged proxy metrics."
        ),
        "final_metrics": {
            "total_prompt_tokens": projection["aggregateUsage"]["prompt_tokens"],
            "total_completion_tokens": projection["aggregateUsage"]["completion_tokens"],
            "total_steps": len(steps),
        },
    }


class OpenCodeGuestAgent(BaseAgent):
    """Execute the pinned OpenCode CLI inside one disposable QEMU task guest."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        opencode_binary: str,
        opencode_sha256: str,
        opencode_cache_seed: str,
        model: str,
        proxy_trial_metrics_dir: str,
        proxy_service_name: str = "benchmark-proxy",
        context_tokens: int = 200_000,
        output_tokens: int = 131_072,
        timeout_sec: int = 3600,
        max_continuations: int = 16,
        proxy_metrics_settle_timeout_sec: float = 60.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if not model or model_name != str(model):
            raise ValueError(
                "OpenCodeGuestAgent requires model_name to equal its pinned model"
            )
        self._model = str(model)
        if not opencode_sha256 or len(opencode_sha256) != 64:
            raise ValueError("OpenCodeGuestAgent requires a pinned OpenCode SHA-256")
        self._opencode_binary = str(opencode_binary)
        self._opencode_sha256 = str(opencode_sha256).lower()
        self._cache_seed = str(opencode_cache_seed)
        self._proxy_metrics_dir = _private_directory(
            proxy_trial_metrics_dir, "proxy trial metrics directory"
        )
        self._proxy_service_name = str(proxy_service_name)
        if not self._proxy_service_name:
            raise ValueError("proxy_service_name must not be empty")
        if type(context_tokens) is not int or type(output_tokens) is not int \
                or not 1 <= output_tokens < context_tokens <= 2_000_000:
            raise ValueError("invalid explicit model token limits")
        self._context_tokens = int(context_tokens)
        self._output_tokens = int(output_tokens)
        self._timeout_sec = int(timeout_sec)
        if not 1 <= self._timeout_sec <= 86_400:
            raise ValueError("timeout_sec must be between 1 and 86400")
        self._max_continuations = int(max_continuations)
        if not 0 <= self._max_continuations <= 64:
            raise ValueError("max_continuations must be between 0 and 64")
        self._proxy_metrics_settle_timeout_sec = float(
            proxy_metrics_settle_timeout_sec)
        if not 1.0 <= self._proxy_metrics_settle_timeout_sec <= 600.0:
            raise ValueError(
                "proxy_metrics_settle_timeout_sec must be between 1 and 600")
        self._binary_info: dict[str, str] | None = None
        self._seed_info: dict[str, str] | None = None
        self._guest_proxy_url = ""
        self._setup_started_at_unix_ns = 0
        self._setup_completed_at_unix_ns = 0

    @staticmethod
    def name() -> str:
        return "opencode-guest"

    def version(self) -> str:
        return OPENCODE_VERSION

    async def setup(self, environment: BaseEnvironment) -> None:
        self._setup_started_at_unix_ns = time.time_ns()
        resolver = getattr(environment, "loopback_service_url", None)
        if not callable(resolver):
            raise TypeError(
                "OpenCodeGuestAgent requires RootlessQemuEnvironment control-plane support"
            )
        self._guest_proxy_url = str(resolver(self._proxy_service_name))
        self._binary_info = await asyncio.to_thread(
            verify_opencode_binary,
            self._opencode_binary,
            expected_sha256=self._opencode_sha256,
        )
        self._seed_info = await asyncio.to_thread(
            verify_cache_seed, self._cache_seed,
        )
        await environment.upload_file(
            self._binary_info["path"], f"{_GUEST_ROOT}/opencode"
        )
        guest_binary = f"{_GUEST_ROOT}/opencode"
        seed_archive = await asyncio.to_thread(self._stage_seed_archive)
        try:
            await environment.upload_file(
                seed_archive, f"{_GUEST_ROOT}/provider-cache.tar.gz"
            )
        finally:
            Path(seed_archive).unlink(missing_ok=True)
        guest_seed = f"{_GUEST_ROOT}/provider-cache.tar.gz"
        guest_cache = f"{_GUEST_ROOT}/cache/opencode"
        verified = await environment.exec(
            " && ".join((
                f"test \"$(sha256sum {shlex.quote(guest_binary)} | "
                "cut -d ' ' -f 1)\" = "
                f"{shlex.quote(self._binary_info['sha256'])}",
                f"chmod 0555 {shlex.quote(guest_binary)}",
                f"mkdir -p {shlex.quote(guest_cache)}",
                f"tar -xzf {shlex.quote(guest_seed)} "
                f"-C {shlex.quote(guest_cache)}",
                f"test -f {shlex.quote(guest_cache)}/node_modules/"
                "@ai-sdk/openai-compatible/package.json",
                f"rm -f {shlex.quote(guest_seed)}",
            )),
            timeout_sec=180,
        )
        if int(verified.return_code) != 0:
            raise RuntimeError(
                "uploaded OpenCode assets failed guest verification"
            )
        self._setup_completed_at_unix_ns = time.time_ns()

    def _stage_seed_archive(self) -> str:
        assert self._seed_info is not None
        source = Path(self._seed_info["path"])
        descriptor, name = tempfile.mkstemp(
            prefix="opencode-provider-cache-", suffix=".tar.gz",
            dir=private_temporary_root(owner_root=self.logs_dir),
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                with tarfile.open(fileobj=handle, mode="w:gz") as archive:
                    for child in ("package.json", "bun.lock", "node_modules"):
                        candidate = source / child
                        if candidate.exists():
                            archive.add(
                                candidate, arcname=child,
                                filter=self._seed_tar_filter,
                            )
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return name

    @staticmethod
    def _seed_tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if info.issym() or info.islnk() or info.isdev():
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    async def _wait_for_trial_metrics(
        self,
        *,
        trial_token: str,
        target: Path,
    ) -> None:
        # The proxy appends one row per completed upstream call and flushes
        # asynchronously; on this host's FUSE mount the 5s/100ms-poll window
        # used previously expired while the proxy was still flushing (tb21
        # v11: gcode-to-text, sam-cell-seg). Wait up to the configured
        # deadline and require sustained size stability across longer polls.
        source = self._proxy_metrics_dir / f"{trial_token}.jsonl"
        deadline = time.monotonic() + self._proxy_metrics_settle_timeout_sec
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
            if stable_observations >= 3:
                await asyncio.to_thread(
                    write_trial_proxy_metrics,
                    str(source),
                    str(target),
                    trial_token=trial_token,
                )
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("tagged OpenCode proxy metrics did not settle")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self._binary_info is None or self._seed_info is None \
                or not self._guest_proxy_url:
            raise RuntimeError("OpenCodeGuestAgent.setup() did not complete")
        agent_run_started_at_unix_ns = time.time_ns()
        identity = str(self.context_id or self.session_id or self.logs_dir.resolve())
        trial_token = benchmark_trial_token(
            identity,
            hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            str(agent_run_started_at_unix_ns),
        )
        evidence_dir = self.logs_dir / "opencode-evidence"
        evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        evidence_dir.chmod(0o700)
        prompt_host = evidence_dir / "prompt.txt"
        config_host = evidence_dir / "opencode.json"
        raw_host = evidence_dir / "opencode-events.jsonl"
        stderr_host = evidence_dir / "opencode-stderr.log"
        metrics_host = evidence_dir / "proxy-metrics.jsonl"
        _atomic_private_text(prompt_host, instruction)
        _atomic_private_text(config_host, json.dumps(
            guest_config(
                proxy_url=self._guest_proxy_url,
                model=self._model,
                context_tokens=self._context_tokens,
                output_tokens=self._output_tokens,
                trial_token=trial_token,
            ),
            ensure_ascii=False, indent=2,
        ) + "\n")

        guest_attempt = f"{_GUEST_ROOT}/{trial_token[:20]}"
        guest_config_path = f"{guest_attempt}/opencode.json"
        guest_raw = f"{guest_attempt}/opencode-events.jsonl"
        guest_stderr = f"{guest_attempt}/opencode-stderr.log"
        await environment.upload_file(prompt_host, f"{guest_attempt}/prompt.txt")
        await environment.upload_file(config_host, guest_config_path)
        continuation = guest_shell_command(
            binary=f"{_GUEST_ROOT}/opencode",
            model=self._model,
            instruction_path=f"{guest_attempt}/prompt.txt",
            raw_output_path=guest_raw,
            stderr_path=guest_stderr,
            max_continuations=self._max_continuations,
        )
        shell_command = (
            f"umask 077 && mkdir -p {shlex.quote(guest_attempt)} && "
            f"{continuation}"
        )
        guest_env = {
            "HOME": f"{guest_attempt}/home",
            "XDG_CONFIG_HOME": f"{guest_attempt}/config",
            "XDG_DATA_HOME": f"{guest_attempt}/data",
            "XDG_STATE_HOME": f"{guest_attempt}/state",
            "XDG_CACHE_HOME": f"{_GUEST_ROOT}/cache",
            "OPENCODE_CONFIG": guest_config_path,
            "LANG": "C",
            "LC_ALL": "C",
        }
        result = None
        projection: dict[str, Any] | None = None
        projection_error = ""
        try:
            result = await environment.exec(
                shell_command,
                env=guest_env,
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
                projection = project_opencode_trial(
                    raw_events=raw_host,
                    proxy_metrics=metrics_host,
                    trial_token=trial_token,
                )
            except OpenCodeProjectionError as exc:
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
            "model": self._model,
            "opencodeVersion": OPENCODE_VERSION,
            "opencodeBinarySha256": self._binary_info["sha256"],
            "providerNpmVersion": self._seed_info["version"],
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
            "rawTrajectory": "opencode-evidence/opencode-events.jsonl",
            "proxyMetrics": "opencode-evidence/proxy-metrics.jsonl",
            "stderrDigest": hashlib.sha256(stderr_host.read_bytes()).hexdigest(),
            "projectionError": projection_error,
        }
        if projection is not None:
            usage = projection["aggregateUsage"]
            context.n_input_tokens = int(usage["prompt_tokens"])
            context.n_output_tokens = int(usage["completion_tokens"])
            metadata.update({
                "chatCompletions": len(projection["rounds"]),
                "finalOutputDigest": projection["finalOutputDigest"],
                "modelMs": projection["timing"]["modelMs"],
                "proxyCpuMs": projection["timing"]["proxyCpuMs"],
            })
            write_collected_trajectory(
                self.logs_dir / "trajectory.json",
                _opencode_atif(
                    instruction=instruction,
                    projection=projection,
                    binary_sha256=self._binary_info["sha256"],
                    trial_token=trial_token,
                    model=self._model,
                ),
            )
        context.metadata = {"opencodeEvidence": metadata}
        if projection is None:
            raise RuntimeError(
                "OpenCode trial evidence could not be reconciled; retry as infrastructure"
            )
        if exit_code != 0:
            raise RuntimeError(
                "OpenCode CLI exited nonzero; the trial cannot be projected as complete"
            )
        if not math.isfinite(float(metadata["agentRunWallMs"])):
            raise RuntimeError("OpenCode trial timing is invalid")


__all__ = ["OpenCodeGuestAgent"]
