"""Pinned OpenCode × GLM baseline runtime contract.

This module defines only host-side launch metadata.  Credential values are
read from explicitly named environment variables at execution time and never
serialized into Harbor job configs, manifests, commands, or guest state.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluations.opencode_contract import (
    OPENCODE_VERSION,
    verify_cache_seed,
    verify_opencode_binary,
)
from evaluations.opencode_proxy.server import ProxyConfig
from evaluations.opencode_proxy.supervisor import private_metrics_directory
from rootless_vm.session import LoopbackServiceForward


OPENCODE_AGENT = (
    "rootless_vm.harbor_opencode_agent:OpenCodeGuestAgent"
)
OPENCODE_PROFILE_ID = "opencode"
OPENCODE_RUNTIME_SCHEMA = "tofu-opencode-runtime/v1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class OpenCodeBaselineSettings:
    """Secret-free inputs required to construct the pinned baseline arm."""

    opencode_binary: Path
    opencode_sha256: str
    opencode_cache_seed: Path
    model: str
    provider_face: str
    provider_slot_id: str
    agent_timeout_seconds: int = 3600
    upstream_base_url_env: str = "GLM_CHAT_BASE_URL"
    upstream_api_key_env: str = "GLM_API_KEY"
    proxy_service_name: str = "benchmark-proxy"
    proxy_guest_port: int = 8765
    proxy_timeout_seconds: float = 300.0
    context_tokens: int = 200_000
    # GLM-5.3 supports long completions; the old 32_768 cap truncated 15 of
    # 16 clean-fail trials mid-reasoning (see tb21-opencode-v2 evidence).
    output_tokens: int = 131_072

    @property
    def child_environment_exclusions(self) -> tuple[str, ...]:
        return (self.upstream_base_url_env, self.upstream_api_key_env)

    def validate(self, *, verify_binary: bool) -> dict[str, str] | None:
        if not _PUBLIC_ID.fullmatch(str(self.model)):
            raise ValueError(
                "OpenCode model must be a non-secret public identifier"
            )
        for value, label in (
            (self.provider_face, "provider face"),
            (self.provider_slot_id, "provider slot ID"),
        ):
            if not _PUBLIC_ID.fullmatch(str(value)):
                raise ValueError(
                    f"OpenCode {label} must be a non-secret public identifier"
                )
        for name in self.child_environment_exclusions:
            if not _ENV_NAME.fullmatch(str(name)):
                raise ValueError(f"invalid OpenCode host environment name: {name!r}")
        if self.upstream_base_url_env == self.upstream_api_key_env:
            raise ValueError("GLM upstream URL and API key must use different variables")
        digest = str(self.opencode_sha256).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("OpenCode SHA-256 must contain 64 lowercase hexadecimal digits")
        if not 1 <= int(self.proxy_guest_port) <= 65535:
            raise ValueError("OpenCode proxy guest port must be between 1 and 65535")
        if not 1 <= float(self.proxy_timeout_seconds) <= 3600:
            raise ValueError("OpenCode proxy timeout must be between 1 and 3600 seconds")
        if isinstance(self.agent_timeout_seconds, bool) \
                or not 1 <= int(self.agent_timeout_seconds) <= 86_400:
            raise ValueError("OpenCode agent timeout must be between 1 and 86400 seconds")
        if type(self.context_tokens) is not int \
                or type(self.output_tokens) is not int \
                or not 1 <= self.output_tokens < self.context_tokens <= 2_000_000:
            raise ValueError("OpenCode explicit model token limits are invalid")
        # This also validates the fixed guest endpoint and service-name grammar.
        self.service_forward(host_port=1).validate()
        if not verify_binary:
            return None
        verify_cache_seed(self.opencode_cache_seed)
        return verify_opencode_binary(
            str(self.opencode_binary), expected_sha256=digest
        )

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
                "OpenCode host-only environment variables are missing: "
                + ", ".join(missing)
            )
        return upstream, api_key

    def runtime_paths(self, run_dir: Path) -> dict[str, Path]:
        root = private_metrics_directory(run_dir / "opencode-proxy")
        trials = private_metrics_directory(root / "trials")
        return {
            "root": root,
            "trials": trials,
            "global_metrics": root / "proxy-metrics.jsonl",
        }

    def proxy_config(self, run_dir: Path) -> ProxyConfig:
        upstream, api_key = self.credentials_from_environment()
        paths = self.runtime_paths(run_dir)
        return ProxyConfig(
            upstream_base_url=upstream,
            upstream_api_key=api_key,
            model=str(self.model),
            metrics_jsonl=str(paths["global_metrics"]),
            trial_metrics_dir=str(paths["trials"]),
            timeout_seconds=float(self.proxy_timeout_seconds),
            require_trial_header=True,
        )

    def service_forward(self, *, host_port: int) -> LoopbackServiceForward:
        return LoopbackServiceForward(
            name=self.proxy_service_name,
            guest_port=self.proxy_guest_port,
            host_port=host_port,
        )

    def agent_kwargs(self, run_dir: Path) -> dict[str, object]:
        paths = self.runtime_paths(run_dir)
        return {
            "opencode_binary": str(
                self.opencode_binary.expanduser().resolve(strict=True)),
            "opencode_sha256": str(self.opencode_sha256).lower(),
            "opencode_cache_seed": str(
                self.opencode_cache_seed.expanduser().resolve(strict=True)),
            "model": str(self.model),
            "proxy_trial_metrics_dir": str(paths["trials"]),
            "proxy_service_name": self.proxy_service_name,
            "context_tokens": int(self.context_tokens),
            "output_tokens": int(self.output_tokens),
            "timeout_sec": int(self.agent_timeout_seconds),
        }

    def manifest_record(self, run_dir: Path, *, host_port: int) -> dict[str, Any]:
        paths = self.runtime_paths(run_dir)
        service = self.service_forward(host_port=host_port)
        service.validate()
        return {
            "schema": OPENCODE_RUNTIME_SCHEMA,
            "agentImport": OPENCODE_AGENT,
            "opencodeVersion": OPENCODE_VERSION,
            "opencodeBinary": str(
                self.opencode_binary.expanduser().resolve(strict=True)),
            "opencodeSha256": str(self.opencode_sha256).lower(),
            "opencodeCacheSeed": verify_cache_seed(self.opencode_cache_seed),
            "model": str(self.model),
            "providerFace": self.provider_face,
            "providerSlotId": self.provider_slot_id,
            "upstreamBaseUrlEnv": self.upstream_base_url_env,
            "upstreamApiKeyEnv": self.upstream_api_key_env,
            "listenHost": "127.0.0.1",
            "listenPort": int(host_port),
            "guestServiceName": self.proxy_service_name,
            "guestHost": service.guest_host,
            "guestPort": service.guest_port,
            "requireTrialHeader": True,
            "trialMetricsDir": str(paths["trials"]),
            "globalMetricsJsonl": str(paths["global_metrics"]),
            "proxyTimeoutSeconds": float(self.proxy_timeout_seconds),
            "agentTimeoutSeconds": int(self.agent_timeout_seconds),
            "contextTokens": int(self.context_tokens),
            "outputTokens": int(self.output_tokens),
            "credentialBoundary": "launcher-host-only",
        }

    @classmethod
    def from_manifest_record(
        cls, value: dict[str, Any], *, run_dir: Path
    ) -> tuple["OpenCodeBaselineSettings", int]:
        if not isinstance(value, dict) or value.get("schema") != OPENCODE_RUNTIME_SCHEMA:
            raise ValueError("OpenCode runtime manifest has an unsupported schema")
        if value.get("agentImport") != OPENCODE_AGENT \
                or value.get("opencodeVersion") != OPENCODE_VERSION \
                or value.get("listenHost") != "127.0.0.1" \
                or value.get("guestHost") != "10.0.2.101" \
                or value.get("requireTrialHeader") is not True \
                or value.get("credentialBoundary") != "launcher-host-only":
            raise ValueError("OpenCode runtime manifest violates the baseline contract")
        settings = cls(
            opencode_binary=Path(str(value.get("opencodeBinary") or "")),
            opencode_sha256=str(value.get("opencodeSha256") or ""),
            opencode_cache_seed=Path(str(
                (value.get("opencodeCacheSeed") or {}).get("path") or "")),
            model=str(value.get("model") or ""),
            provider_face=str(value.get("providerFace") or ""),
            provider_slot_id=str(value.get("providerSlotId") or ""),
            agent_timeout_seconds=int(value.get("agentTimeoutSeconds") or 0),
            upstream_base_url_env=str(value.get("upstreamBaseUrlEnv") or ""),
            upstream_api_key_env=str(value.get("upstreamApiKeyEnv") or ""),
            proxy_service_name=str(value.get("guestServiceName") or ""),
            proxy_guest_port=int(value.get("guestPort") or 0),
            proxy_timeout_seconds=float(value.get("proxyTimeoutSeconds") or 0),
            context_tokens=int(value.get("contextTokens") or 0),
            output_tokens=int(value.get("outputTokens") or 0),
        )
        settings.validate(verify_binary=True)
        host_port = int(value.get("listenPort") or 0)
        settings.service_forward(host_port=host_port).validate()
        paths = settings.runtime_paths(run_dir)
        expected = {
            "trialMetricsDir": str(paths["trials"]),
            "globalMetricsJsonl": str(paths["global_metrics"]),
        }
        if any(str(value.get(key) or "") != child for key, child in expected.items()):
            raise ValueError("OpenCode metrics paths do not match the run-owned layout")
        return settings, host_port


__all__ = [
    "OPENCODE_AGENT",
    "OPENCODE_PROFILE_ID",
    "OPENCODE_RUNTIME_SCHEMA",
    "OpenCodeBaselineSettings",
]
