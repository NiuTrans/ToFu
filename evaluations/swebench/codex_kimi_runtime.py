"""Formal Codex 0.149.1 × Kimi baseline runtime contract.

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

from evaluations.codex_kimi_proxy.codex_contract import (
    CODEX_VERSION,
    verify_codex_binary,
)
from evaluations.codex_kimi_proxy.server import ProxyConfig
from evaluations.codex_kimi_proxy.supervisor import private_metrics_directory
from rootless_vm.session import LoopbackServiceForward


CODEX_KIMI_AGENT = (
    "rootless_vm.harbor_codex_kimi_agent:CodexKimiGuestAgent"
)
CODEX_KIMI_PROFILE_ID = "codex-kimi"
CODEX_KIMI_RUNTIME_SCHEMA = "tofu-codex-kimi-runtime/v2"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class CodexKimiBaselineSettings:
    """Secret-free inputs required to construct the pinned baseline arm."""

    codex_binary: Path
    codex_sha256: str
    provider_face: str
    provider_slot_id: str
    agent_timeout_seconds: int = 3600
    upstream_base_url_env: str = "KIMI_CHAT_BASE_URL"
    upstream_api_key_env: str = "KIMI_API_KEY"
    proxy_service_name: str = "benchmark-proxy"
    proxy_guest_port: int = 8765
    proxy_timeout_seconds: float = 300.0

    @property
    def child_environment_exclusions(self) -> tuple[str, ...]:
        return (self.upstream_base_url_env, self.upstream_api_key_env)

    def validate(self, *, verify_binary: bool) -> dict[str, str] | None:
        for value, label in (
            (self.provider_face, "provider face"),
            (self.provider_slot_id, "provider slot ID"),
        ):
            if not _PUBLIC_ID.fullmatch(str(value)):
                raise ValueError(
                    f"Codex Kimi {label} must be a non-secret public identifier"
                )
        for name in self.child_environment_exclusions:
            if not _ENV_NAME.fullmatch(str(name)):
                raise ValueError(f"invalid Codex Kimi host environment name: {name!r}")
        if self.upstream_base_url_env == self.upstream_api_key_env:
            raise ValueError("Kimi upstream URL and API key must use different variables")
        digest = str(self.codex_sha256).lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("Codex SHA-256 must contain 64 lowercase hexadecimal digits")
        if not 1 <= int(self.proxy_guest_port) <= 65535:
            raise ValueError("Codex proxy guest port must be between 1 and 65535")
        if not 1 <= float(self.proxy_timeout_seconds) <= 3600:
            raise ValueError("Codex proxy timeout must be between 1 and 3600 seconds")
        if isinstance(self.agent_timeout_seconds, bool) \
                or not 1 <= int(self.agent_timeout_seconds) <= 86_400:
            raise ValueError("Codex agent timeout must be between 1 and 86400 seconds")
        # This also validates the fixed guest endpoint and service-name grammar.
        self.service_forward(host_port=1).validate()
        if not verify_binary:
            return None
        return verify_codex_binary(
            str(self.codex_binary), expected_sha256=digest
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
                "Codex Kimi host-only environment variables are missing: "
                + ", ".join(missing)
            )
        return upstream, api_key

    def runtime_paths(self, run_dir: Path) -> dict[str, Path]:
        root = private_metrics_directory(run_dir / "codex-kimi-proxy")
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
            "codex_binary": str(self.codex_binary.expanduser().resolve(strict=True)),
            "codex_sha256": str(self.codex_sha256).lower(),
            "proxy_trial_metrics_dir": str(paths["trials"]),
            "proxy_service_name": self.proxy_service_name,
            "timeout_sec": int(self.agent_timeout_seconds),
        }

    def manifest_record(self, run_dir: Path, *, host_port: int) -> dict[str, Any]:
        paths = self.runtime_paths(run_dir)
        service = self.service_forward(host_port=host_port)
        service.validate()
        return {
            "schema": CODEX_KIMI_RUNTIME_SCHEMA,
            "agentImport": CODEX_KIMI_AGENT,
            "codexVersion": CODEX_VERSION,
            "codexBinary": str(self.codex_binary.expanduser().resolve(strict=True)),
            "codexSha256": str(self.codex_sha256).lower(),
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
            "credentialBoundary": "launcher-host-only",
        }

    @classmethod
    def from_manifest_record(
        cls, value: dict[str, Any], *, run_dir: Path
    ) -> tuple["CodexKimiBaselineSettings", int]:
        if not isinstance(value, dict) or value.get("schema") != CODEX_KIMI_RUNTIME_SCHEMA:
            raise ValueError("Codex Kimi runtime manifest has an unsupported schema")
        if value.get("agentImport") != CODEX_KIMI_AGENT \
                or value.get("codexVersion") != CODEX_VERSION \
                or value.get("listenHost") != "127.0.0.1" \
                or value.get("guestHost") != "10.0.2.101" \
                or value.get("requireTrialHeader") is not True \
                or value.get("credentialBoundary") != "launcher-host-only":
            raise ValueError("Codex Kimi runtime manifest violates the baseline contract")
        settings = cls(
            codex_binary=Path(str(value.get("codexBinary") or "")),
            codex_sha256=str(value.get("codexSha256") or ""),
            provider_face=str(value.get("providerFace") or ""),
            provider_slot_id=str(value.get("providerSlotId") or ""),
            agent_timeout_seconds=int(value.get("agentTimeoutSeconds") or 0),
            upstream_base_url_env=str(value.get("upstreamBaseUrlEnv") or ""),
            upstream_api_key_env=str(value.get("upstreamApiKeyEnv") or ""),
            proxy_service_name=str(value.get("guestServiceName") or ""),
            proxy_guest_port=int(value.get("guestPort") or 0),
            proxy_timeout_seconds=float(value.get("proxyTimeoutSeconds") or 0),
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
            raise ValueError("Codex Kimi metrics paths do not match the run-owned layout")
        return settings, host_port


__all__ = [
    "CODEX_KIMI_AGENT",
    "CODEX_KIMI_PROFILE_ID",
    "CODEX_KIMI_RUNTIME_SCHEMA",
    "CodexKimiBaselineSettings",
]
