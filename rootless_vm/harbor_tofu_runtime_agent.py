"""Harbor adapter for Tofu's real production ``AgentRuntime`` kernel.

The model runtime and Kimi credential remain in the trusted Harbor process.
Exactly two client-mode tools cross into the disposable benchmark guest; no
host filesystem path or built-in Tofu execution authority is exposed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from rootless_vm.trajectory import write_collected_trajectory
from tofu_agent import AgentRuntime, ModelRoutingConfig, __version__
from evaluations.swebench.tofu_kimi_runtime import (
    TOFU_KIMI_HARBOR_SYSTEM_PROMPT as _SYSTEM_PROMPT,
    tofu_kimi_clean_tool_schemas,
    tofu_kimi_custom_tools,
    tofu_kimi_prompt_contract_sha256,
    tofu_kimi_tool_schema_sha256,
)


_EVENT_OBSERVATION_SCHEMA = "tofu.harbor-runtime-event-observation/v1"
_HARBOR_EVIDENCE_SCHEMA = "tofu.harbor-production-kimi-evidence/v1"
_TOOL_AUDIT_SCHEMA = "tofu.harbor-custom-tool-audit/v1"
_CUSTOM_TOOLS = tofu_kimi_custom_tools()


def _harbor_model_routing(
    *, base_url: str, api_key: str, thinking_format: str,
) -> ModelRoutingConfig:
    """Build the benchmark's single, explicit v2 access authority.

    Harbor owns one host-side credential and one fixed Kimi deployment.  Keep
    that integration fact in the adapter, but express it through the same
    creator/model/provider/access/connection graph as every other runtime.
    This avoids reviving the removed inline-provider shortcut while keeping
    the secret outside the serializable aggregate and benchmark artifacts.
    """
    model_ref = {'creator_id': 'moonshot', 'model_id': 'kimi-k3'}
    provider_id = 'harbor-kimi'
    access_id = f'{provider_id}-access'
    connection_id = f'{provider_id}-connection'
    credential_id = f'{provider_id}-credential'
    secret_reference = f'{provider_id}-secret'
    offering_id = f'{provider_id}-offering'
    return ModelRoutingConfig(
        document={
            'contract_version': 'tofu.model-routing/v2',
            'revision': 0,
            'creators': [{
                'creator_id': model_ref['creator_id'],
                'name': 'Moonshot AI',
            }],
            'models': [{
                **model_ref,
                'display_name': 'Kimi K3',
                'capabilities': ['text', 'thinking', 'tools'],
                'context_window': 262_144,
                'quality_rank': 10,
            }],
            'providers': [{
                'provider_id': provider_id,
                'name': 'Harbor Kimi host access',
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
                'context_window': 262_144,
                'priority': 0,
            }],
            'deployments': [{
                'deployment_id': f'{provider_id}-deployment',
                'offering_id': offering_id,
                'connection_id': connection_id,
                'wire_model_id': 'kimi-k3',
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

    def append(self, event: dict[str, Any]) -> int:
        observed_at = time.time_ns()
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


def _atif(
    *,
    instruction: str,
    final_output: str,
    usage: dict[str, Any],
    tool_audit: list[dict[str, Any]],
    experiment_arm: str,
    runtime_config_sha256: str,
    prompt_contract_sha256: str,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        {"step_id": 1, "source": "system", "message": _SYSTEM_PROMPT},
        {"step_id": 2, "source": "user", "message": instruction},
    ]
    for row in tool_audit:
        steps.append({
            "step_id": len(steps) + 1,
            "source": "agent",
            "model_name": "kimi-k3",
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
        "model_name": "kimi-k3",
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
            "model_name": "kimi-k3",
            "tool_definitions": _clean_tool_schemas(),
            "extra": {
                "credential_boundary": "harbor-host-only",
                "harness_profile": "tofu-kimi",
                "experiment_arm": experiment_arm,
                "runtime_config_sha256": runtime_config_sha256,
                "prompt_contract_sha256": prompt_contract_sha256,
                "tool_schema_sha256": tofu_kimi_tool_schema_sha256(),
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
        reasoning_effort: str = "high",
        thinking_format: str = "",
        timeout_sec: int = 3600,
        command_timeout_sec: int = 480,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if model_name != "kimi-k3":
            raise ValueError(
                "TofuKimiRuntimeAgent requires model_name='kimi-k3'"
            )
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
            "low", "medium", "high", "xhigh", "max", "ultra",
        }:
            raise ValueError("unsupported Tofu thinking setting")
        self._thinking_format = str(thinking_format or "")
        self._timeout_sec = int(timeout_sec)
        self._command_timeout_sec = int(command_timeout_sec)
        if not 1 <= self._timeout_sec <= 86_400:
            raise ValueError("timeout_sec must be between 1 and 86400")
        if not 1 <= self._command_timeout_sec <= 1_800:
            raise ValueError("command_timeout_sec must be between 1 and 1800")

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
            timeout = _bounded_timeout(
                arguments.get("timeout_sec"), self._command_timeout_sec)
            result = await environment.exec(command=command, timeout_sec=timeout)
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
        )
        runtime_config = json.loads(json.dumps(self._runtime_config))
        runtime_config.update({
            "thinking": self._reasoning_effort,
            "disableModelFallback": True,
        })
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
        try:
            execution = runtime.start(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": instruction},
                ],
                config=runtime_config,
                custom_tools=_CUSTOM_TOOLS,
                custom_tools_mode="exclusive",
                request_id=str(
                    self.context_id or self.session_id or f"harbor-{started_at}"
                ),
                timeout_s=float(self._timeout_sec),
            )
            async for event in execution.events_async(
                timeout_s=float(self._timeout_sec)
            ):
                observed_at = writer.append(event)
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
                if not execution.resolve_custom_tool_call(
                    call_id, tool_result, is_error=is_error
                ):
                    raise RuntimeError(
                        "production runtime rejected its owned custom tool result"
                    )
                tool_audit.append({
                    "callId": call_id,
                    "toolName": str(event.get("toolName") or ""),
                    "arguments": event.get("arguments") or {},
                    "result": tool_result,
                    "isError": bool(is_error),
                    "observedAtUnixNs": observed_at,
                    "resolvedAtUnixNs": resolved_at,
                    "durationMs": (
                        resolved_at - tool_started_at) / 1_000_000,
                    "resultSha256": hashlib.sha256(
                        tool_result.encode("utf-8")).hexdigest(),
                    **result_audit,
                })
                _atomic_private_json(tool_audit_path, {
                    "contractVersion": _TOOL_AUDIT_SCHEMA,
                    "calls": tool_audit,
                })
            result = await execution.result_async(timeout_s=1.0)
        finally:
            try:
                writer.close()
            finally:
                try:
                    runtime.close(abort=True)
                finally:
                    if execution is not None:
                        runtime_evidence = execution.evidence_snapshot()
                        _atomic_private_json(
                            runtime_evidence_path, runtime_evidence)
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
                        _atomic_private_json(tool_audit_path, {
                            "contractVersion": _TOOL_AUDIT_SCHEMA,
                            "calls": [],
                        })
        if execution is None or result is None or runtime_evidence is None:
            raise RuntimeError("production AgentRuntime did not return a result")
        completed_at = time.time_ns()
        usage = dict(result.usage)
        usage["apiRounds"] = list(
            runtime_evidence.get("apiRounds") or [])
        write_collected_trajectory(
            self.logs_dir / "trajectory.json",
            _atif(
                instruction=instruction,
                final_output=result.content,
                usage=usage,
                tool_audit=tool_audit,
                experiment_arm=self._experiment_arm,
                runtime_config_sha256=self._runtime_config_sha256,
                prompt_contract_sha256=tofu_kimi_prompt_contract_sha256(
                    self._runtime_config),
            ),
        )
        metadata = {
            "contractVersion": _HARBOR_EVIDENCE_SCHEMA,
            "model": "kimi-k3",
            "tofuVersion": __version__,
            "providerFace": self._provider_face,
            "providerSlotId": self._provider_slot_id,
            "experimentArm": self._experiment_arm,
            "runtimeConfigSha256": self._runtime_config_sha256,
            "promptContractSha256": tofu_kimi_prompt_contract_sha256(
                self._runtime_config),
            "toolSchemaSha256": tofu_kimi_tool_schema_sha256(),
            "harborPromptSha256": hashlib.sha256(
                _SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
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
        context.metadata = {"tofuKimiEvidence": metadata}
        if not result.ok:
            raise RuntimeError(
                "production AgentRuntime did not reach a successful terminal state"
            )
        if not math.isfinite(float(metadata["agentRunWallMs"])):
            raise RuntimeError("Tofu trial timing is invalid")


__all__ = ["TofuKimiRuntimeAgent"]
