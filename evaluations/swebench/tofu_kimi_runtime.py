"""Formal production-Tofu × Kimi candidate runtime contract.

Only public, reproducible launch metadata is serialized. The Harbor host
agent resolves the two explicitly named credential environment variables at
execution time; their values never enter the job configuration or guest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


TOFU_KIMI_AGENT = (
    "rootless_vm.harbor_tofu_runtime_agent:TofuKimiRuntimeAgent"
)
TOFU_KIMI_PROFILE_ID = "tofu-kimi"
TOFU_KIMI_RUNTIME_SCHEMA = "tofu-production-kimi-runtime/v1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUNTIME_RESERVED_KEYS = frozenset({
    "model", "thinking", "thinkingDepth", "thinkingEnabled",
    "disableModelFallback", "_customToolSchemas", "_customToolsMode",
    "_explicitToolSchemas",
})

TOFU_KIMI_HARBOR_SYSTEM_PROMPT = """You are solving an isolated benchmark task. Use
custom__run_command to inspect and modify the task environment and to run focused
checks. Work autonomously until the requested behavior is complete. Use
custom__submit_result only after a meaningful validation command passes. Do not
request credentials or try to access host/private services. The external benchmark
verifier, not a non-empty answer, decides success."""

_TOFU_KIMI_CUSTOM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "custom__run_command",
            "description": "Run one POSIX shell command in the isolated task guest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {
                        "type": "integer", "minimum": 1, "maximum": 1800,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        "execution": {"mode": "client"},
        "write": True,
    },
    {
        "type": "function",
        "function": {
            "name": "custom__submit_result",
            "description": (
                "Validate the current artifact and record a completion claim. "
                "The model must still return its final answer afterward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "validation_command": {"type": "string"},
                    "timeout_sec": {
                        "type": "integer", "minimum": 1, "maximum": 1800,
                    },
                },
                "required": ["summary", "validation_command"],
                "additionalProperties": False,
            },
        },
        "execution": {"mode": "client"},
        "write": True,
    },
]


def tofu_kimi_custom_tools() -> list[dict[str, Any]]:
    """Return a detached copy of the formal guest-facing tool contracts."""
    return json.loads(json.dumps(_TOFU_KIMI_CUSTOM_TOOLS))


def tofu_kimi_clean_tool_schemas() -> list[dict[str, Any]]:
    """Return exactly the schemas the production model is allowed to see."""
    return [
        {"type": "function", "function": tool["function"]}
        for tool in tofu_kimi_custom_tools()
    ]


def tofu_kimi_tool_schema_sha256() -> str:
    encoded = json.dumps(
        tofu_kimi_clean_tool_schemas(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tofu_kimi_prompt_contract_sha256(
    runtime_config: Mapping[str, Any],
) -> str:
    """Digest the adapter prompt plus the requested production profile.

    The runtime additionally emits each round's resolved static-prompt digest.
    This manifest digest freezes the request-side choice without pretending a
    profile-independent hash is the full model-visible prompt.
    """
    responses = runtime_config.get("responses")
    requested_profile = (
        responses.get("promptProfile", "auto")
        if isinstance(responses, Mapping) else "auto"
    )
    payload = {
        "contractVersion": "tofu-kimi-prompt-contract/v1",
        "harborSystemPromptSha256": hashlib.sha256(
            TOFU_KIMI_HARBOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "requestedPromptProfile": str(requested_profile or "auto"),
        "systemPromptMode": str(
            runtime_config.get("systemPromptMode") or "append"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_config(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise ValueError("Tofu Kimi runtime_config must be an object")

    def inspect(candidate: Any, path: str = "runtime_config") -> None:
        if isinstance(candidate, Mapping):
            for key, child in candidate.items():
                name = str(key)
                separated = re.sub(
                    r"([a-z0-9])([A-Z])", r"\1_\2", name)
                words = [
                    word for word in re.split(r"[^A-Za-z0-9]+", separated.lower())
                    if word
                ]
                credential_key = (
                    any(word in {"token", "secret", "password", "passwd"}
                        for word in words)
                    or any(words[index:index + 2] == ["api", "key"]
                           for index in range(max(0, len(words) - 1)))
                )
                if credential_key:
                    raise ValueError(
                        f"Tofu Kimi {path}.{name} looks credential-bearing"
                    )
                inspect(child, f"{path}.{name}")
        elif isinstance(candidate, (list, tuple)):
            for index, child in enumerate(candidate):
                inspect(child, f"{path}[{index}]")

    inspect(value)
    reserved = sorted(_RUNTIME_RESERVED_KEYS & set(value))
    if reserved:
        raise ValueError(
            "Tofu Kimi runtime_config contains runner-owned keys: "
            + ", ".join(reserved)
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        detached = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Tofu Kimi runtime_config must be finite JSON"
        ) from exc
    if not isinstance(detached, dict):
        raise ValueError("Tofu Kimi runtime_config must be an object")
    if len(encoded) > 256 * 1024:
        raise ValueError("Tofu Kimi runtime_config exceeds 256 KiB")
    return detached, hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TofuKimiCandidateSettings:
    """Secret-free inputs for one frozen production candidate arm."""

    provider_face: str
    provider_slot_id: str
    agent_version: str
    experiment_arm: str
    runtime_config: Mapping[str, Any]
    agent_timeout_seconds: int = 3600
    command_timeout_seconds: int = 480
    upstream_base_url_env: str = "KIMI_CHAT_BASE_URL"
    upstream_api_key_env: str = "KIMI_API_KEY"
    thinking_format: str = ""

    def __post_init__(self) -> None:
        detached, _ = _canonical_config(self.runtime_config)
        object.__setattr__(self, "runtime_config", detached)

    @property
    def credential_environment_names(self) -> tuple[str, str]:
        return (self.upstream_base_url_env, self.upstream_api_key_env)

    @property
    def runtime_config_sha256(self) -> str:
        return _canonical_config(self.runtime_config)[1]

    def validate(self) -> None:
        for value, label in (
            (self.provider_face, "provider face"),
            (self.provider_slot_id, "provider slot ID"),
            (self.agent_version, "agent version"),
            (self.experiment_arm, "experiment arm"),
        ):
            if not _PUBLIC_ID.fullmatch(str(value)):
                raise ValueError(
                    f"Tofu Kimi {label} must be a non-secret public identifier"
                )
        for name in self.credential_environment_names:
            if not _ENV_NAME.fullmatch(str(name)):
                raise ValueError(
                    f"invalid Tofu Kimi host environment name: {name!r}"
                )
        if self.upstream_base_url_env == self.upstream_api_key_env:
            raise ValueError(
                "Kimi upstream URL and API key must use different variables"
            )
        if isinstance(self.agent_timeout_seconds, bool) or not (
                1 <= int(self.agent_timeout_seconds) <= 86_400):
            raise ValueError(
                "Tofu Kimi agent timeout must be between 1 and 86400 seconds"
            )
        if isinstance(self.command_timeout_seconds, bool) or not (
                1 <= int(self.command_timeout_seconds) <= 1_800):
            raise ValueError(
                "Tofu Kimi command timeout must be between 1 and 1800 seconds"
            )
        if len(str(self.thinking_format)) > 128:
            raise ValueError("Tofu Kimi thinking_format exceeds 128 characters")
        _canonical_config(self.runtime_config)

    def credentials_from_environment(self) -> tuple[str, str]:
        upstream = str(os.environ.get(self.upstream_base_url_env) or "").strip()
        api_key = str(os.environ.get(self.upstream_api_key_env) or "")
        missing = [
            name
            for name, value in (
                (self.upstream_base_url_env, upstream),
                (self.upstream_api_key_env, api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Tofu Kimi host-only environment variables are missing: "
                + ", ".join(missing)
            )
        return upstream, api_key

    def agent_kwargs(self) -> dict[str, object]:
        self.validate()
        return {
            "upstream_base_url_env": self.upstream_base_url_env,
            "upstream_api_key_env": self.upstream_api_key_env,
            "provider_face": self.provider_face,
            "provider_slot_id": self.provider_slot_id,
            "experiment_arm": self.experiment_arm,
            "runtime_config": dict(self.runtime_config),
            "runtime_config_sha256": self.runtime_config_sha256,
            "thinking_format": self.thinking_format,
            "timeout_sec": int(self.agent_timeout_seconds),
            "command_timeout_sec": int(self.command_timeout_seconds),
        }

    def manifest_record(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": TOFU_KIMI_RUNTIME_SCHEMA,
            "agentImport": TOFU_KIMI_AGENT,
            "agentVersion": self.agent_version,
            "model": "kimi-k3",
            "providerFace": self.provider_face,
            "providerSlotId": self.provider_slot_id,
            "experimentArm": self.experiment_arm,
            "runtimeConfig": dict(self.runtime_config),
            "runtimeConfigSha256": self.runtime_config_sha256,
            "toolSchemaSha256": tofu_kimi_tool_schema_sha256(),
            "promptContractSha256": tofu_kimi_prompt_contract_sha256(
                self.runtime_config),
            "harborPromptSha256": hashlib.sha256(
                TOFU_KIMI_HARBOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "upstreamBaseUrlEnv": self.upstream_base_url_env,
            "upstreamApiKeyEnv": self.upstream_api_key_env,
            "thinkingFormat": self.thinking_format,
            "agentTimeoutSeconds": int(self.agent_timeout_seconds),
            "commandTimeoutSeconds": int(self.command_timeout_seconds),
            "credentialBoundary": "harbor-host-only",
            "guestCredentialValues": False,
        }

    @classmethod
    def from_manifest_record(
        cls, value: dict[str, Any]
    ) -> "TofuKimiCandidateSettings":
        if not isinstance(value, dict) \
                or value.get("schema") != TOFU_KIMI_RUNTIME_SCHEMA:
            raise ValueError("Tofu Kimi runtime manifest has an unsupported schema")
        if value.get("agentImport") != TOFU_KIMI_AGENT \
                or value.get("model") != "kimi-k3" \
                or value.get("credentialBoundary") != "harbor-host-only" \
                or value.get("guestCredentialValues") is not False:
            raise ValueError("Tofu Kimi runtime manifest violates its trust boundary")
        settings = cls(
            provider_face=str(value.get("providerFace") or ""),
            provider_slot_id=str(value.get("providerSlotId") or ""),
            agent_version=str(value.get("agentVersion") or ""),
            experiment_arm=str(value.get("experimentArm") or ""),
            runtime_config=(value.get("runtimeConfig")
                            if isinstance(value.get("runtimeConfig"), dict)
                            else {}),
            agent_timeout_seconds=int(value.get("agentTimeoutSeconds") or 0),
            command_timeout_seconds=int(
                value.get("commandTimeoutSeconds") or 0),
            upstream_base_url_env=str(value.get("upstreamBaseUrlEnv") or ""),
            upstream_api_key_env=str(value.get("upstreamApiKeyEnv") or ""),
            thinking_format=str(value.get("thinkingFormat") or ""),
        )
        settings.validate()
        if value.get("runtimeConfigSha256") != settings.runtime_config_sha256:
            raise ValueError("Tofu Kimi runtime config digest drifted")
        if value.get("toolSchemaSha256") != tofu_kimi_tool_schema_sha256():
            raise ValueError("Tofu Kimi formal tool schema digest drifted")
        if value.get("promptContractSha256") != \
                tofu_kimi_prompt_contract_sha256(settings.runtime_config):
            raise ValueError("Tofu Kimi formal prompt contract digest drifted")
        expected_prompt = hashlib.sha256(
            TOFU_KIMI_HARBOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        if value.get("harborPromptSha256") != expected_prompt:
            raise ValueError("Tofu Kimi Harbor prompt digest drifted")
        return settings


__all__ = [
    "TOFU_KIMI_AGENT",
    "TOFU_KIMI_PROFILE_ID",
    "TOFU_KIMI_RUNTIME_SCHEMA",
    "TOFU_KIMI_HARBOR_SYSTEM_PROMPT",
    "TofuKimiCandidateSettings",
    "tofu_kimi_clean_tool_schemas",
    "tofu_kimi_custom_tools",
    "tofu_kimi_prompt_contract_sha256",
    "tofu_kimi_tool_schema_sha256",
]
