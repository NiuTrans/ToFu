"""Pinned Codex CLI launch contract for paired Kimi benchmark trials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


CODEX_VERSION = "0.149.1"
CODEX_UNKNOWN_MODEL_CONTEXT_WINDOW = 272_000
CODEX_UNKNOWN_MODEL_AUTO_COMPACT_LIMIT = 244_800
TRIAL_HEADER = "X-Tofu-Benchmark-Trial"
_TRIAL_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_GUEST_CONTROL_PLANE_HOST = "10.0.2.101"


class CodexContractError(RuntimeError):
    pass


def verify_codex_binary(path: str, *, expected_sha256: str) -> dict[str, str]:
    binary = Path(path).resolve()
    if not binary.is_file():
        raise CodexContractError("Codex binary does not exist")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != str(expected_sha256).lower():
        raise CodexContractError("Codex binary SHA-256 does not match manifest")
    try:
        version = subprocess.check_output(
            [str(binary), "--version"], text=True,
            stderr=subprocess.STDOUT, timeout=10,
            env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexContractError("Unable to read Codex version") from exc
    if CODEX_VERSION not in version:
        raise CodexContractError(
            f"Codex {CODEX_VERSION} is required; observed {version!r}")
    return {"path": str(binary), "version": CODEX_VERSION, "sha256": digest}


def validate_trial_token(value: str) -> str:
    token = str(value or "").strip().lower()
    if not _TRIAL_TOKEN.fullmatch(token):
        raise CodexContractError("trial token must be a lowercase SHA-256 value")
    return token


def benchmark_trial_token(*parts: str) -> str:
    """Derive a non-secret correlation token from a collision-safe tuple."""

    rendered = [str(part) for part in parts]
    if not rendered or any(not part for part in rendered):
        raise CodexContractError("trial token parts must be non-empty")
    digest = hashlib.sha256()
    digest.update(b"tofu-codex-kimi-trial-v1\0")
    for part in rendered:
        payload = part.encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _validated_proxy_base_url(value: str) -> str:
    base = str(value).rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.username is not None \
            or parsed.password is not None or parsed.query or parsed.fragment:
        raise CodexContractError(
            "proxy_base_url must be plaintext HTTP on an approved loopback "
            "or guest control-plane endpoint"
        )
    host = parsed.hostname or ""
    if not (
        host == "localhost"
        or host == "::1"
        or host.startswith("127.")
        or host == _GUEST_CONTROL_PLANE_HOST
    ):
        raise CodexContractError(
            "proxy_base_url must be loopback or the restricted guest control plane"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise CodexContractError("proxy_base_url port is invalid") from exc
    if port is None or not 1 <= port <= 65535 or parsed.path not in {"", "/"}:
        raise CodexContractError("proxy_base_url requires an explicit port and no path")
    return base


def build_codex_command(*, binary: str, proxy_base_url: str,
                        prompt: str, reasoning_effort: str,
                        trial_token: str | None = None,
                        sandbox: str = "workspace-write") -> list[str]:
    """Return an ephemeral command with local compaction forced on."""
    base = _validated_proxy_base_url(proxy_base_url)
    command = [
        str(Path(binary).resolve()),
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "--model", "kimi-k3",
        "--sandbox", str(sandbox),
        "-c", 'model_provider="tofu_kimi_proxy"',
        "-c", 'model_providers.tofu_kimi_proxy.name="Tofu Kimi benchmark proxy"',
        "-c", f'model_providers.tofu_kimi_proxy.base_url={json.dumps(base + "/v1")}',
        "-c", 'model_providers.tofu_kimi_proxy.wire_api="responses"',
        "-c", 'model_providers.tofu_kimi_proxy.requires_openai_auth=false',
        "-c", 'model_providers.tofu_kimi_proxy.supports_websockets=false',
        "-c", f'model_reasoning_effort={json.dumps(str(reasoning_effort))}',
        # Codex 0.149.1 resolves unknown model slugs to a 272k fallback and
        # derives local auto-compaction at 90%. Make that baseline explicit so
        # a future harness edit cannot silently change the paired arm.
        "-c", f"model_context_window={CODEX_UNKNOWN_MODEL_CONTEXT_WINDOW}",
        "-c", (
            "model_auto_compact_token_limit="
            f"{CODEX_UNKNOWN_MODEL_AUTO_COMPACT_LIMIT}"
        ),
        "-c", 'model_auto_compact_token_limit_scope="total"',
        "-c", 'features.remote_compaction_v2=false',
        # Browsing/research trials mount the frozen MCP backend explicitly;
        # Codex's provider-native search must not create a different data arm.
        "-c", 'tools.web_search=false',
    ]
    if trial_token is not None:
        token = validate_trial_token(trial_token)
        command.extend([
            "-c",
            (
                "model_providers.tofu_kimi_proxy.http_headers="
                f'{{"{TRIAL_HEADER}"={json.dumps(token)}}}'
            ),
        ])
    command.append(str(prompt))
    return command


def _metrics_rows(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source = Path(path).expanduser()
    if source.is_symlink():
        raise CodexContractError("proxy metrics source must not be a symlink")
    resolved = source.resolve(strict=True)
    if not resolved.is_file():
        raise CodexContractError("proxy metrics source must be a regular file")
    with resolved.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CodexContractError(
                    f"proxy metrics line {line_number} must be an object"
                )
            rows.append(value)
    return rows


def write_trial_proxy_metrics(
    source_path: str,
    target_path: str,
    *,
    trial_token: str,
) -> dict[str, Any]:
    """Atomically materialize one trial from a concurrent proxy metrics sink."""

    token = validate_trial_token(trial_token)
    rows = [
        row for row in _metrics_rows(source_path)
        if row.get("trialToken") == token
    ]
    if not rows:
        raise CodexContractError("proxy metrics do not contain the requested trial")
    target = Path(target_path).expanduser()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if target.is_symlink():
        raise CodexContractError("trial metrics target must not be a symlink")
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for row in rows
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".partial", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return {
        "trialToken": token,
        "records": len(rows),
        "responsesRequests": sum(
            row.get("event") == "responsesTranslation" for row in rows
        ),
        "path": str(target.resolve()),
    }


def validate_proxy_metrics(path: str, *, expected_request_count: int,
                           expected_trial_token: str | None = None,
                           require_trial_token: bool = False,
                           ) -> dict[str, Any]:
    """Reject compact calls, extra model calls, or incomplete trial evidence."""
    records = _metrics_rows(path)
    if expected_trial_token is not None:
        token = validate_trial_token(expected_trial_token)
        records = [row for row in records if row.get("trialToken") == token]
    trial_tokens = {str(row.get("trialToken") or "") for row in records}
    tagged = len(trial_tokens) == 1 and "" not in trial_tokens
    compact = [row for row in records
               if row.get("event") == "invalidCompactRequest"]
    translations = [row for row in records
                    if row.get("event") == "responsesTranslation"]
    upstream_calls = sum(int(row.get("upstreamCalls") or 0)
                         for row in translations)
    suppressed_native_tool_types = sorted({
        str(tool_type)
        for row in translations
        for tool_type in (row.get("suppressedNativeToolTypes") or ())
        if tool_type
    })
    terminal_statuses = {"completed", "incomplete", "failed"}
    valid = (not compact and len(translations) == int(expected_request_count)
             and upstream_calls == int(expected_request_count)
             and not any(row.get("invalidTrial") for row in translations)
             and all(row.get("status") in terminal_statuses
                     and row.get("clientDisconnected") is not True
                     for row in translations)
             and (tagged or not require_trial_token))
    return {
        "valid": valid,
        "responsesRequests": len(translations),
        "upstreamCalls": upstream_calls,
        "compactRequests": len(compact),
        "trialTagged": tagged,
        "trialToken": next(iter(trial_tokens)) if tagged else "",
        "suppressedNativeToolTypes": suppressed_native_tool_types,
        "translationCpuNs": sum(int(row.get("translationCpuNs") or 0)
                                for row in translations),
        "proxyCpuNs": sum(int(row.get("proxyCpuNs")
                              or row.get("translationCpuNs") or 0)
                          for row in translations),
        "rawWallNs": sum(int(row.get("rawWallNs") or 0)
                         for row in translations),
        "codexFavoredCorrectedWallNs": sum(max(
            0, int(row.get("rawWallNs") or 0)
            - int(row.get("translationCpuNs") or 0))
            for row in translations),
    }


__all__ = [
    "CODEX_UNKNOWN_MODEL_AUTO_COMPACT_LIMIT",
    "CODEX_UNKNOWN_MODEL_CONTEXT_WINDOW", "CODEX_VERSION",
    "TRIAL_HEADER", "CodexContractError", "benchmark_trial_token",
    "build_codex_command", "validate_proxy_metrics", "validate_trial_token",
    "verify_codex_binary", "write_trial_proxy_metrics",
]
