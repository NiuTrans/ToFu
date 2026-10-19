"""Harbor adapter for Tofu's real production ``AgentRuntime`` kernel.

The model runtime and Kimi credential remain in the trusted Harbor process.
Two tool surfaces are supported:

* ``exclusive`` (default, rollback arm) — exactly two client-mode tools cross
  into the disposable benchmark guest; no host filesystem path or built-in
  Tofu execution authority is exposed.
* ``production`` — the real production tool face. Production-verbatim
  registry schemas (read_files/write_file/edit_file/grep_search/find_files/
  repo_map/fetch_url/todo_write/…) are served through the runtime's
  caller-supplied explicit-catalog precedence and dispatched by the REAL
  built-in registry against the host-visible guest rootfs (the same bytes the
  verifier inspects). The host-executing shell tools (run_command,
  check/kill_background_command) are deliberately withheld from the
  model-visible catalog — same-name shadowing through the custom-tool channel
  is impossible (lib/tools/tool_env.py enforces the ``custom__`` prefix and
  rejects built-in collisions at mint) — so shell capability is carried by
  ``custom__run_command`` routed to guest exec inside the sandbox.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import os
import tempfile
import time
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from rootless_vm.trajectory import write_collected_trajectory
from tofu_agent import AgentRuntime, ModelRoutingConfig, __version__
from evaluations.swebench.tofu_kimi_runtime import (
    TOFU_BENCHMARK_MODELS as _BENCHMARK_MODELS,
    TOFU_KIMI_HARBOR_SYSTEM_PROMPT as _SYSTEM_PROMPT,
    TOFU_KIMI_TOOL_SURFACES as _TOOL_SURFACES,
    tofu_kimi_clean_tool_schemas,
    tofu_kimi_custom_tools,
    tofu_kimi_prompt_contract_sha256,
    tofu_kimi_production_custom_tools,
    tofu_kimi_production_tool_catalog,
    tofu_kimi_production_tool_schema_sha256,
    tofu_kimi_search_enabled,
    tofu_kimi_tool_schema_sha256,
)


_EVENT_OBSERVATION_SCHEMA = "tofu.harbor-runtime-event-observation/v1"
_HARBOR_EVIDENCE_SCHEMA = "tofu.harbor-production-kimi-evidence/v1"
_TOOL_AUDIT_SCHEMA = "tofu.harbor-custom-tool-audit/v1"
_CUSTOM_TOOLS = tofu_kimi_custom_tools()


async def _durable_io(function, /, *args, **kwargs):
    """Await one blocking operation without freezing Harbor's shared loop.

    Callers serialize writes: at most one outstanding operation per adapter,
    on asyncio's bounded executor. Drain a cancelled write before its owner
    closes the file or publishes final hashes; never weaken fsync durability.
    """
    # Keep the owned worker as a Future, not a sibling Task that a global
    # task-cancellation sweep could cancel before its thread stops writing.
    context = contextvars.copy_context()
    pending = asyncio.get_running_loop().run_in_executor(
        None, partial(context.run, function, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
        # Retrieve write errors even during cancellation (no orphaned task).
        pending.result()
        raise


def _harbor_model_routing(
    *, base_url: str, api_key: str, thinking_format: str, model: str,
) -> ModelRoutingConfig:
    """Build the benchmark's single, explicit v2 access authority.

    Harbor owns one host-side credential and one fixed model deployment.
    Keep that integration fact in the adapter, but express it through the
    same creator/model/provider/access/connection graph as every other
    runtime.  This avoids reviving the removed inline-provider shortcut
    while keeping the secret outside the serializable aggregate and
    benchmark artifacts.
    """
    spec = _BENCHMARK_MODELS[str(model)]
    model_ref = {'creator_id': spec['creator_id'], 'model_id': spec['model_id']}
    from lib.model_info._context import context_profile
    official_context = context_profile(spec['model_id'])['window']
    provider_id = 'harbor-benchmark'
    access_id = f'{provider_id}-access'
    connection_id = f'{provider_id}-connection'
    credential_id = f'{provider_id}-credential'
    secret_reference = f'{provider_id}-secret'
    offering_id = f'{provider_id}-offering'
    # The official Model uses maintained exact catalog facts/defaults through
    # normal admission. Provider limits belong to the Offering; its wire alias
    # belongs to the Deployment. A private alias is not a new trained model,
    # and one provider's reasoning profile must not be copied to other models.
    return ModelRoutingConfig(
        document={
            'contract_version': 'tofu.model-routing/v2',
            'revision': 0,
            'creators': [{
                'creator_id': model_ref['creator_id'],
                'name': spec['creator_name'],
            }],
            'models': [{
                **model_ref,
                'display_name': spec['display_name'],
                'capabilities': ['text', 'thinking', 'tools'],
                'context_window': int(official_context),
                'quality_rank': 10,
            }],
            'providers': [{
                'provider_id': provider_id,
                'name': 'Harbor benchmark host access',
                'scope': 'owner',
            }],
            'provider_accesses': [{
                'provider_access_id': access_id,
                'provider_id': provider_id,
                'enabled': True,
                'quota_policy': {},
            }],
            'connections': [{
                'connection_id': connection_id,
                'provider_access_id': access_id,
                'base_url': base_url,
                'protocol': 'openai',
                'enabled': True,
                'priority': 0,
                'extra_headers': {},
                'thinking_format': thinking_format,
            }],
            'credentials': [{
                'credential_id': credential_id,
                'provider_access_id': access_id,
                'kind': 'api_key',
                'secret_reference': secret_reference,
                'key_hint': 'configured',
                'enabled': True,
                'authorization': {
                    'connection_ids': [connection_id],
                    'models': [dict(model_ref)],
                },
                'quota_policy': {},
            }],
            'offerings': [{
                'offering_id': offering_id,
                'provider_access_id': access_id,
                'identity_state': 'confirmed',
                'model': dict(model_ref),
                'enabled': True,
                'capabilities': ['text', 'thinking', 'tools'],
                'context_window': int(spec['context_window']),
                'priority': 0,
            }],
            'deployments': [{
                'deployment_id': f'{provider_id}-deployment',
                'offering_id': offering_id,
                'connection_id': connection_id,
                'wire_model_id': spec['wire_model_id'],
                'enabled': True,
                'identity_confidence': 'high',
                'probe_status': 'passed',
                'priority': 0,
            }],
        },
        model=model_ref,
        routing={'preferred_provider_id': provider_id},
        credential_secrets={secret_reference: api_key},
    )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class _PrivateEventWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        self.path = path
        self._handle = os.fdopen(descriptor, "w", encoding="utf-8")
        self.write_count = 0
        self.write_total_ms = 0.0
        self.write_max_ms = 0.0

    def append(self, event: dict[str, Any], observed_at: int | None = None) -> int:
        observed_at = time.time_ns() if observed_at is None else observed_at
        row = {
            "contractVersion": _EVENT_OBSERVATION_SCHEMA,
            "observedAtUnixNs": observed_at,
            "event": event,
        }
        self._handle.write(json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return observed_at

    async def append_async(self, event: dict[str, Any]) -> int:
        observed_at = time.time_ns()
        started = time.monotonic()
        try:
            return await _durable_io(self.append, event, observed_at)
        finally:
            elapsed = (time.monotonic() - started) * 1000
            self.write_count += 1
            self.write_total_ms += elapsed
            self.write_max_ms = max(self.write_max_ms, elapsed)

    def metrics(self) -> dict[str, Any]:
        return {
            'contractVersion': 'tofu.harbor-evidence-io/v1',
            'eventWriteCount': self.write_count,
            'eventWriteTotalMs': self.write_total_ms,
            'eventWriteMaxMs': self.write_max_ms,
            'measurement': 'Serialized off-loop write and fsync, including executor queue wait.',
        }

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


def _bounded_timeout(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        timeout = math.ceil(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(1800, timeout))


def _bounded_output(
    stdout: str | None,
    stderr: str | None,
    return_code: int,
    *,
    suffix: str = "",
) -> tuple[str, dict[str, Any]]:
    rendered = f"exit_code={int(return_code)}\n"
    if stdout:
        rendered += f"stdout:\n{stdout}"
    if stderr:
        rendered += f"\nstderr:\n{stderr}"
    rendered += suffix
    limit = 24 * 1024
    encoded = rendered.encode("utf-8")
    raw_bytes = len(encoded)
    raw_sha256 = hashlib.sha256(encoded).hexdigest()
    was_truncated = raw_bytes > limit
    if was_truncated:
        marker = b"\n...[tool output truncated by Harbor adapter]...\n"
        content_budget = limit - len(marker)
        head_bytes = content_budget // 2
        tail_bytes = content_budget - head_bytes
        head = encoded[:head_bytes].decode("utf-8", errors="ignore")
        tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
        rendered = head + marker.decode("ascii") + tail
    visible_bytes = len(rendered.encode("utf-8"))
    return rendered, {
        "rawBytes": raw_bytes,
        "visibleBytes": visible_bytes,
        "truncated": was_truncated,
        "rawResultSha256": raw_sha256,
        "visibleResultSha256": hashlib.sha256(
            rendered.encode("utf-8")).hexdigest(),
    }


def _literal_result(content: str) -> tuple[str, dict[str, Any]]:
    visible_bytes = len(content.encode("utf-8"))
    return content, {
        "rawBytes": visible_bytes,
        "visibleBytes": visible_bytes,
        "truncated": False,
        "rawResultSha256": hashlib.sha256(
            content.encode("utf-8")).hexdigest(),
        "visibleResultSha256": hashlib.sha256(
            content.encode("utf-8")).hexdigest(),
    }


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


def _clean_tool_schemas() -> list[dict[str, Any]]:
    return tofu_kimi_clean_tool_schemas()


# ── Production tool-surface helpers ─────────────────────────────────────────

_GUEST_WORKSPACE_DEFAULT = "/app"


def _guest_os_version(uname_sr: str | None) -> str:
    """Mirror production ``_short_os_version`` for a guest ``uname -sr`` line.

    Production renders ``f"{sysname} {short}"`` where ``short`` is the release
    up to the first hyphen on Linux. The guest probe returns e.g.
    ``"Linux 6.1.0-25-generic"`` → ``"Linux 6.1.0"``.
    """
    parts = str(uname_sr or "").strip().split()
    if len(parts) < 2:
        return ""
    sysname, release = parts[0], parts[1]
    short = release.split("-", 1)[0] if sysname == "Linux" else release
    return f"{sysname} {short}".strip()


def _guest_environment_message(
    *,
    project_path: str,
    is_git: bool,
    guest_os_version: str,
    guest_workspace: str,
) -> str:
    """Adapter-side replacement for the composer's ``environment`` tail block.

    The composer renders that block from HOST platform facts
    (lib/tasks_pkg/system_prompt_base/_environment.py reads platform.release()
    and sys.platform of the Harbor process). No composer override channel
    exists, and the production host semantics must stay untouched, so the
    production arm disables the built-in block via the declared
    ``systemPromptBlocks.disabled`` channel and ships this guest-measured
    replica as an ordinary user message. The bullet order/format mirrors
    ``section_environment`` (drift-guarded by
    tests/tools/test_harbor_tofu_runtime_agent.py); only the OS Version bullet is
    substituted with the guest ``uname -sr`` probe, and one sandbox bullet is
    appended to document the shell/file-tool namespace split.
    """
    lines = [
        "# Environment",
        "You have been invoked in the following environment: ",
        f" - Primary working directory: {project_path}",
        f"   - Is a git repository: {'true' if is_git else 'false'}",
        " - Platform: linux",
        " - Shell: bash",
        f" - OS Version: {guest_os_version}",
        (
            " - Sandbox shell: custom__run_command executes inside the "
            f"isolated task guest whose workspace is {guest_workspace} — the "
            "same files as the primary working directory above, reached "
            "through the host-mounted guest rootfs. Use guest-absolute "
            f"({guest_workspace}/...) or relative paths in shell commands; "
            "host-absolute paths exist only for the file tools."
        ),
    ]
    return "\n".join(lines)


def _guest_cwd(working_dir: Any, *, guest_rootfs: str) -> str | None:
    """Map a model-supplied ``working_dir`` to a guest-container cwd.

    The model may pass a guest-absolute path (used as-is), or echo the
    host-absolute rootfs path shown by the file tools — that prefix is mapped
    back to the container namespace. Anything else (relative, ``..``) is
    rejected by the caller with a tool error.
    """
    text = str(working_dir or "").strip()
    if not text:
        return None
    rootfs = str(guest_rootfs or "").rstrip("/")
    if rootfs:
        if text == rootfs:
            return "/"
        prefix = rootfs + "/"
        if text.startswith(prefix):
            text = "/" + text[len(prefix):]
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"working_dir must be a guest-absolute path without '..': "
            f"{working_dir!r}")
    return str(path)


def _guest_rootfs_of(environment: BaseEnvironment) -> str:
    """Return the host-visible guest rootfs of the rootless QEMU environment.

    ``RootlessQemuEnvironment`` publishes ``_guest_rootfs`` once the prepared
    image cache resolves (rootless_vm/harbor_environment.py). The attribute is
    read, never written; production mode fails closed when it is absent (a
    different environment implementation).
    """
    rootfs = getattr(environment, "_guest_rootfs", None) or getattr(
        environment, "guest_rootfs", None)
    return str(rootfs or "").strip()


def _atif(
    *,
    instruction: str,
    final_output: str,
    usage: dict[str, Any],
    tool_audit: list[dict[str, Any]],
    experiment_arm: str,
    runtime_config_sha256: str,
    prompt_contract_sha256: str,
    model: str,
    system_message: str | None = None,
    tool_schema_sha256: str | None = None,
    tool_surface: str = "exclusive",
    tool_definitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        {"step_id": 1, "source": "system",
         "message": _SYSTEM_PROMPT if system_message is None else system_message},
        {"step_id": 2, "source": "user", "message": instruction},
    ]
    for row in tool_audit:
        steps.append({
            "step_id": len(steps) + 1,
            "source": "agent",
            "model_name": model,
            "message": "",
            "llm_call_count": 0,
            "tool_calls": [{
                "tool_call_id": row["callId"],
                "function_name": row["toolName"],
                "arguments": row["arguments"],
            }],
            "observation": {"results": [{
                "source_call_id": row["callId"],
                "content": row["result"],
            }]},
        })
    steps.append({
        "step_id": len(steps) + 1,
        "source": "agent",
        "model_name": model,
        "message": final_output,
        "llm_call_count": len(usage.get("apiRounds") or []),
        "metrics": {
            "prompt_tokens": _usage_int(
                usage, "prompt_tokens", "input_tokens"),
            "completion_tokens": _usage_int(
                usage, "completion_tokens", "output_tokens"),
        },
    })
    return {
        "schema_version": "ATIF-v1.7",
        "agent": {
            "name": "tofu-kimi-runtime",
            "version": __version__,
            "model_name": model,
            "tool_definitions": (
                _clean_tool_schemas()
                if tool_definitions is None else tool_definitions
            ),
            "extra": {
                "credential_boundary": "harbor-host-only",
                "harness_profile": "tofu-kimi",
                "experiment_arm": experiment_arm,
                "tool_surface": str(tool_surface),
                "runtime_config_sha256": runtime_config_sha256,
                "prompt_contract_sha256": prompt_contract_sha256,
                "tool_schema_sha256": (
                    tool_schema_sha256
                    if tool_schema_sha256 is not None
                    else tofu_kimi_tool_schema_sha256()
                ),
            },
        },
        "steps": steps,
        "notes": (
            "Production AgentRuntime native events are retained separately; "
            "explicit model reasoning is intentionally absent from ATIF."
        ),
        "final_metrics": {
            "total_prompt_tokens": _usage_int(
                usage, "prompt_tokens", "input_tokens"),
            "total_completion_tokens": _usage_int(
                usage, "completion_tokens", "output_tokens"),
            "total_steps": len(steps),
        },
    }


class TofuKimiRuntimeAgent(BaseAgent):
    """Run a Harbor task through Tofu's normal production orchestrator."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        upstream_base_url_env: str,
        upstream_api_key_env: str,
        provider_face: str,
        provider_slot_id: str,
        experiment_arm: str,
        runtime_config: dict[str, Any],
        runtime_config_sha256: str,
        model: str = "kimi-k3",
        reasoning_effort: str = "high",
        thinking_format: str = "",
        timeout_sec: int = 3600,
        command_timeout_sec: int = 480,
        tool_surface: str = "exclusive",
        guest_workspace: str = _GUEST_WORKSPACE_DEFAULT,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if str(model) not in _BENCHMARK_MODELS:
            raise ValueError(
                "TofuKimiRuntimeAgent model must be one of "
                + ", ".join(sorted(_BENCHMARK_MODELS))
            )
        if model_name != str(model):
            raise ValueError(
                "TofuKimiRuntimeAgent requires model_name to equal its "
                "pinned benchmark model"
            )
        self._model = str(model)
        self._upstream_base_url_env = str(upstream_base_url_env)
        self._upstream_api_key_env = str(upstream_api_key_env)
        self._provider_face = str(provider_face)
        self._provider_slot_id = str(provider_slot_id)
        self._experiment_arm = str(experiment_arm)
        self._runtime_config = json.loads(json.dumps(runtime_config))
        self._runtime_config_sha256 = str(runtime_config_sha256).lower()
        if _canonical_sha256(self._runtime_config) != self._runtime_config_sha256:
            raise ValueError("Tofu runtime config digest mismatch")
        self._reasoning_effort = str(reasoning_effort).strip().lower()
        if self._reasoning_effort not in {
            "default", "low", "medium", "high", "xhigh", "max", "ultra",
        }:
            raise ValueError("unsupported Tofu thinking setting")
        self._thinking_format = str(thinking_format or "")
        self._timeout_sec = int(timeout_sec)
        self._command_timeout_sec = int(command_timeout_sec)
        if not 1 <= self._timeout_sec <= 86_400:
            raise ValueError("timeout_sec must be between 1 and 86400")
        if not 1 <= self._command_timeout_sec <= 1_800:
            raise ValueError("command_timeout_sec must be between 1 and 1800")
        self._tool_surface = str(tool_surface).strip().lower()
        if self._tool_surface not in _TOOL_SURFACES:
            raise ValueError(
                "TofuKimiRuntimeAgent tool_surface must be one of "
                + ", ".join(_TOOL_SURFACES)
            )
        workspace = PurePosixPath(str(guest_workspace or ""))
        if not workspace.is_absolute() or ".." in workspace.parts:
            raise ValueError(
                "guest_workspace must be an absolute guest path without '..'")
        self._guest_workspace = str(workspace)

    @staticmethod
    def name() -> str:
        return "tofu-kimi-runtime"

    def version(self) -> str:
        return __version__

    async def setup(self, environment: BaseEnvironment) -> None:
        del environment

    async def _execute_tool(
        self,
        event: dict[str, Any],
        environment: BaseEnvironment,
        state: dict[str, Any],
    ) -> tuple[str, bool, dict[str, Any]]:
        name = str(event.get("toolName") or "")
        arguments = event.get("arguments")
        if not isinstance(arguments, dict):
            content, audit = _literal_result(
                "Error: tool arguments must be a JSON object")
            return content, True, audit
        if name == "custom__run_command":
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                content, audit = _literal_result(
                    "Error: command must be a non-empty string")
                return content, True, audit
            if len(command) > 32 * 1024:
                content, audit = _literal_result(
                    "Error: command exceeds 32 KiB")
                return content, True, audit
            exec_kwargs: dict[str, Any] = {"command": command}
            if self._tool_surface == "production":
                # Production-shaped schema: timeout_s (timeout_sec accepted
                # for continuity with the exclusive arm) and an optional
                # guest working_dir mapped through the host rootfs prefix.
                timeout = _bounded_timeout(
                    arguments.get("timeout_s", arguments.get("timeout_sec")),
                    self._command_timeout_sec)
                try:
                    cwd = _guest_cwd(
                        arguments.get("working_dir"),
                        guest_rootfs=str(state.get("guestRootfs") or ""),
                    )
                except ValueError as exc:
                    content, audit = _literal_result(f"Error: {exc}")
                    return content, True, audit
                if cwd is not None:
                    exec_kwargs["cwd"] = cwd
            else:
                timeout = _bounded_timeout(
                    arguments.get("timeout_sec"), self._command_timeout_sec)
            exec_kwargs["timeout_sec"] = timeout
            result = await environment.exec(**exec_kwargs)
            state["ranCommand"] = True
            state["lastSuccessfulCommand"] = (
                command if int(result.return_code) == 0 else None)
            content, audit = _bounded_output(
                result.stdout, result.stderr, result.return_code)
            return content, False, audit
        if name != "custom__submit_result":
            content, audit = _literal_result(
                f"Error: unsupported tool {name!r}")
            return content, True, audit
        summary = arguments.get("summary")
        validation = arguments.get("validation_command")
        if not state.get("ranCommand"):
            content, audit = _literal_result(
                "Error: custom__run_command must be used first")
            return content, True, audit
        if not isinstance(summary, str) or not summary.strip():
            content, audit = _literal_result(
                "Error: summary must be a non-empty string")
            return content, True, audit
        if not isinstance(validation, str) or not validation.strip():
            content, audit = _literal_result(
                "Error: validation_command must be non-empty")
            return content, True, audit
        if len(validation) > 32 * 1024:
            content, audit = _literal_result(
                "Error: validation_command exceeds 32 KiB")
            return content, True, audit
        if validation == state.get("lastSuccessfulCommand"):
            state["submittedSummary"] = summary.strip()[:4096]
            content, audit = _literal_result(
                "exit_code=0\nstdout:\nReused the immediately preceding "
                "successful validation. Return the final answer now."
            )
            return content, False, audit
        timeout = _bounded_timeout(
            arguments.get("timeout_sec"), self._command_timeout_sec)
        state["lastSuccessfulCommand"] = None
        result = await environment.exec(command=validation, timeout_sec=timeout)
        suffix = "\nValidation passed. Return the final answer now."
        rendered, audit = _bounded_output(
            result.stdout,
            result.stderr,
            result.return_code,
            suffix=suffix if int(result.return_code) == 0 else "",
        )
        if int(result.return_code) == 0:
            state["lastSuccessfulCommand"] = validation
            state["submittedSummary"] = summary.strip()[:4096]
            return rendered, False, audit
        return rendered, True, audit

    def _compose_production_static_prompt(
        self,
        *,
        project_path: str,
        tool_catalog: list[dict[str, Any]],
        environment_disabled: bool,
        runtime_config: dict[str, Any],
    ) -> str:
        """Reproduce the exact static prompt the composer renders for this trial.

        The production assembler is deterministic, so the adapter can replay
        it with the composer's own inputs and record the result in ATIF step 1
        and the evidence metadata. This is a read-only projection — the live
        composition still happens inside the task engine.
        """
        from lib.tasks_pkg import system_prompt_base
        requested_profile = "auto"
        try:
            from lib.context_experiment_flags import (
                normalize_context_experiment_flags,
            )
            requested_profile = normalize_context_experiment_flags(
                runtime_config)["responses"]["promptProfile"]
        except Exception:
            pass
        profile = system_prompt_base.resolve_static_prompt_profile(
            self._model, requested_profile)
        tool_names = {
            str((tool.get("function") or {}).get("name") or "")
            for tool in tool_catalog
        }
        tool_names.discard("")
        return system_prompt_base.build_static_prompt(
            cwd=project_path,
            is_git=os.path.isdir(os.path.join(project_path, ".git")),
            model=self._model,
            has_real_tools=True,
            is_code_context=True,
            tool_names=tool_names,
            tool_search_available=False,
            disabled_blocks=(
                {"environment"} if environment_disabled else None),
            include_date=False,
            include_environment=False,
            profile=profile,
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        upstream = str(os.environ.get(self._upstream_base_url_env) or "").strip()
        api_key = str(os.environ.get(self._upstream_api_key_env) or "")
        if not upstream or not api_key:
            raise RuntimeError("Tofu Kimi host-only provider inputs are missing")
        model_routing = _harbor_model_routing(
            base_url=upstream,
            api_key=api_key,
            thinking_format=self._thinking_format,
            model=self._model,
        )
        runtime_config = json.loads(json.dumps(self._runtime_config))
        runtime_config["disableModelFallback"] = True
        if self._reasoning_effort != "default":
            runtime_config["thinking"] = self._reasoning_effort
        production = self._tool_surface == "production"
        # Production-surface trial state. All guest side effects cross the
        # RootlessQemuEnvironment exec boundary; the host-visible rootfs is
        # only READ from the environment object.
        project_path = ""
        guest_os_version = ""
        environment_message = ""
        tool_catalog: list[dict[str, Any]] = []
        tool_catalog_sha256 = ""
        production_system_prompt = ""
        search_enabled = False
        if production:
            guest_rootfs = _guest_rootfs_of(environment)
            if not guest_rootfs:
                raise RuntimeError(
                    "production tool surface requires the rootless QEMU "
                    "environment's host-visible guest rootfs"
                )
            from rootless_vm.harbor_environment import _container_rootfs_path
            project_path = _container_rootfs_path(
                guest_rootfs, self._guest_workspace)
            # One bounded guest probe at trial setup: the composed
            # ``environment`` prompt block is host-derived in production, and
            # no composer override channel exists, so the guest kernel is
            # measured here and shipped as a guest-accurate replica while the
            # built-in block is disabled through the declared
            # systemPromptBlocks channel. A probe failure never fails the
            # trial — the host-rendered block simply stays in place.
            try:
                probe = await environment.exec(
                    command="uname -sr", timeout_sec=30)
                if int(probe.return_code) == 0:
                    guest_os_version = _guest_os_version(probe.stdout)
            except Exception:
                guest_os_version = ""
            search_enabled = tofu_kimi_search_enabled(self._runtime_config)
            tool_catalog = tofu_kimi_production_tool_catalog(
                search_enabled=search_enabled)
            tool_catalog_sha256 = tofu_kimi_production_tool_schema_sha256(
                search_enabled=search_enabled)
            # Injected post-validation, exactly like ``thinking`` above: the
            # manifest digest pins the operator-supplied config; these keys
            # are adapter-computed trial state (the per-trial rootfs path and
            # the derived catalog), never manifest inputs.
            runtime_config["projectPath"] = project_path
            runtime_config["_explicitToolSchemas"] = tool_catalog
            if guest_os_version:
                blocks = dict(runtime_config.get("systemPromptBlocks") or {})
                disabled = [str(item) for item in (blocks.get("disabled") or ())]
                if "environment" not in disabled:
                    disabled.append("environment")
                blocks["disabled"] = disabled
                runtime_config["systemPromptBlocks"] = blocks
                environment_message = _guest_environment_message(
                    project_path=project_path,
                    is_git=os.path.isdir(os.path.join(project_path, ".git")),
                    guest_os_version=guest_os_version,
                    guest_workspace=self._guest_workspace,
                )
            try:
                production_system_prompt = self._compose_production_static_prompt(
                    project_path=project_path,
                    tool_catalog=tool_catalog,
                    environment_disabled=bool(environment_message),
                    runtime_config=runtime_config,
                )
            except Exception:
                production_system_prompt = ""
        evidence_dir = self.logs_dir / "tofu-kimi-evidence"
        events_path = evidence_dir / "events.jsonl"
        runtime_evidence_path = evidence_dir / "runtime-evidence.json"
        tool_audit_path = evidence_dir / "tool-audit.json"
        writer = _PrivateEventWriter(events_path)
        started_at = time.time_ns()
        tool_audit: list[dict[str, Any]] = []
        state: dict[str, Any] = {
            "ranCommand": False,
            "lastSuccessfulCommand": None,
            "submittedSummary": "",
        }
        if production:
            # Thread the bound rootfs into _execute_tool's working_dir mapping.
            state["guestRootfs"] = guest_rootfs
        first_event_at = 0
        execution = None
        result = None
        runtime_evidence: dict[str, Any] | None = None
        runtime = AgentRuntime.local(
            model_routing=model_routing,
            model_routing_source="harbor-formal-kimi",
            subject_id="benchmark:harbor-tofu-kimi",
            max_inflight=1,
        )
        if production:
            initial_messages: list[dict[str, Any]] = [
                {"role": "user", "content": instruction},
            ]
            if environment_message:
                initial_messages.append(
                    {"role": "user", "content": environment_message})
            custom_tools = tofu_kimi_production_custom_tools()
            custom_tools_mode = "augment"
        else:
            initial_messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": instruction},
            ]
            custom_tools = _CUSTOM_TOOLS
            custom_tools_mode = "exclusive"
        try:
            execution = runtime.start(
                initial_messages,
                config=runtime_config,
                custom_tools=custom_tools,
                custom_tools_mode=custom_tools_mode,
                request_id=str(
                    self.context_id or self.session_id or f"harbor-{started_at}"
                ),
                timeout_s=float(self._timeout_sec),
            )
            async for event in execution.events_async(
                timeout_s=float(self._timeout_sec)
            ):
                observed_at = await writer.append_async(event)
                if event.get("type") == "done" and event.get("error"):
                    from evaluations.native_failure import native_failure_receipt
                    state["runtimeFailure"] = native_failure_receipt(event["error"])
                if not first_event_at:
                    first_event_at = observed_at
                if event.get("type") != "custom_tool_call":
                    continue
                call_id = str(event.get("callId") or "")
                if not call_id:
                    raise RuntimeError("custom tool event is missing callId")
                tool_started_at = time.time_ns()
                tool_result, is_error, result_audit = await self._execute_tool(
                    event, environment, state)
                resolved_at = time.time_ns()
                resolution_accepted = execution.resolve_custom_tool_call(
                    call_id, tool_result, is_error=is_error
                )
                tool_audit.append({
                    "callId": call_id,
                    "toolName": str(event.get("toolName") or ""),
                    "arguments": event.get("arguments") or {},
                    "result": tool_result,
                    "isError": bool(is_error),
                    "resolutionAccepted": resolution_accepted,
                    "observedAtUnixNs": observed_at,
                    "resolvedAtUnixNs": resolved_at,
                    "durationMs": (
                        resolved_at - tool_started_at) / 1_000_000,
                    "resultSha256": hashlib.sha256(
                        tool_result.encode("utf-8")).hexdigest(),
                    **result_audit,
                })
                await _durable_io(_atomic_private_json, tool_audit_path, {
                    "contractVersion": _TOOL_AUDIT_SCHEMA,
                    "calls": tool_audit,
                })
                if not resolution_accepted:
                    raise RuntimeError(
                        "production runtime rejected its owned custom tool result"
                    )
            result = await execution.result_async(timeout_s=1.0)
        finally:
            try:
                await _durable_io(writer.close)
            finally:
                try:
                    await _durable_io(runtime.close, abort=True)
                finally:
                    if execution is not None:
                        runtime_evidence = execution.evidence_snapshot()
                        await _durable_io(
                            _atomic_private_json, runtime_evidence_path, runtime_evidence)
                        evidence_usage = runtime_evidence.get("usage")
                        if isinstance(evidence_usage, dict):
                            context.n_input_tokens = _usage_int(
                                evidence_usage,
                                "prompt_tokens", "input_tokens",
                            )
                            context.n_output_tokens = _usage_int(
                                evidence_usage,
                                "completion_tokens", "output_tokens",
                            )
                    if not tool_audit_path.exists():
                        await _durable_io(_atomic_private_json, tool_audit_path, {
                            "contractVersion": _TOOL_AUDIT_SCHEMA,
                            "calls": [],
                        })
                    await _durable_io(_atomic_private_json,
                                     evidence_dir / 'io-metrics.json', writer.metrics())
        if execution is None or result is None or runtime_evidence is None:
            raise RuntimeError("production AgentRuntime did not return a result")
        completed_at = time.time_ns()
        usage = dict(result.usage)
        usage["apiRounds"] = list(
            runtime_evidence.get("apiRounds") or [])
        prompt_contract_sha256 = tofu_kimi_prompt_contract_sha256(
            self._runtime_config,
            tool_surface=self._tool_surface,
            tool_catalog_sha256=(
                tool_catalog_sha256 if production else ""),
        )
        tool_schema_sha256 = (
            tool_catalog_sha256 if production
            else tofu_kimi_tool_schema_sha256()
        )
        await _durable_io(write_collected_trajectory,
            self.logs_dir / "trajectory.json",
            _atif(
                instruction=instruction,
                final_output=result.content,
                usage=usage,
                tool_audit=tool_audit,
                experiment_arm=self._experiment_arm,
                runtime_config_sha256=self._runtime_config_sha256,
                prompt_contract_sha256=prompt_contract_sha256,
                model=self._model,
                system_message=(
                    production_system_prompt if production else None),
                tool_schema_sha256=tool_schema_sha256,
                tool_surface=self._tool_surface,
                tool_definitions=(
                    [
                        {"type": "function", "function": tool["function"]}
                        for tool in tool_catalog
                    ]
                    if production else None
                ),
            ),
        )
        metadata = {
            "contractVersion": _HARBOR_EVIDENCE_SCHEMA,
            "model": self._model,
            "tofuVersion": __version__,
            "providerFace": self._provider_face,
            "providerSlotId": self._provider_slot_id,
            "experimentArm": self._experiment_arm,
            "toolSurface": self._tool_surface,
            "runtimeConfigSha256": self._runtime_config_sha256,
            "promptContractSha256": prompt_contract_sha256,
            "toolSchemaSha256": tool_schema_sha256,
            "harborPromptSha256": (
                None if production else hashlib.sha256(
                    _SYSTEM_PROMPT.encode("utf-8")).hexdigest()),
            "credentialBoundary": "harbor-host-only",
            "agentRunStartedAtUnixNs": started_at,
            "firstNativeEventObservedAtUnixNs": first_event_at,
            "agentCompletedAtUnixNs": completed_at,
            "agentRunWallMs": (completed_at - started_at) / 1_000_000,
            "runtimeEvidence": "tofu-kimi-evidence/runtime-evidence.json",
            "nativeEvents": "tofu-kimi-evidence/events.jsonl",
            "toolAudit": "tofu-kimi-evidence/tool-audit.json",
            "runtimeEvidenceSha256": hashlib.sha256(
                runtime_evidence_path.read_bytes()).hexdigest(),
            "nativeEventsSha256": hashlib.sha256(
                events_path.read_bytes()).hexdigest(),
            "toolAuditSha256": hashlib.sha256(
                tool_audit_path.read_bytes()).hexdigest(),
            "toolCalls": len(tool_audit),
            "submitted": bool(state.get("submittedSummary")),
            "status": result.status,
            "finishReason": result.finish_reason,
            "finalOutputSha256": hashlib.sha256(
                result.content.encode("utf-8")).hexdigest(),
        }
        if production:
            metadata["productionSurface"] = {
                "projectPath": project_path,
                "guestWorkspace": self._guest_workspace,
                "guestOsVersion": guest_os_version or None,
                "environmentBlock": (
                    "guest-override" if environment_message
                    else "host-fallback"),
                "toolCatalogSha256": tool_catalog_sha256,
                "systemPromptSha256": (
                    hashlib.sha256(
                        production_system_prompt.encode("utf-8")).hexdigest()
                    if production_system_prompt else None),
                "searchEnabled": bool(search_enabled),
            }
        context.metadata = {"tofuKimiEvidence": metadata}
        if state.get("runtimeFailure"):
            metadata["runtimeFailure"] = state["runtimeFailure"]
        if not result.ok:
            raise RuntimeError(
                "production AgentRuntime did not reach a successful terminal state"
            )
        if not math.isfinite(float(metadata["agentRunWallMs"])):
            raise RuntimeError("Tofu trial timing is invalid")


__all__ = ["TofuKimiRuntimeAgent"]
