"""Machine-readable harness registry for rootless benchmark runs.

This module owns harness identity, Harbor import paths, credential boundaries,
and score-provenance defaults. It deliberately has no Harbor dependency so
configuration and audit commands can inspect profiles before an eval virtual
environment exists.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


CredentialBoundary = Literal["host-only", "guest-explicit"]


@dataclass(frozen=True)
class HarnessProfile:
    """Stable configuration and security contract for one agent harness."""

    profile_id: str
    harbor_agent: str
    agent_name: str
    agent_version: str | None
    credential_boundary: CredentialBoundary
    trajectory_format: str
    comparison_target: str | None
    default_max_output_tokens: int | None
    default_context_window_tokens: int | None
    default_max_rounds: int | None
    default_context_checkpoint_tokens: int | None
    host_dispatch: bool
    notes: str

    @property
    def requires_guest_credentials(self) -> bool:
        return self.credential_boundary == "guest-explicit"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["requires_guest_credentials"] = self.requires_guest_credentials
        return payload


_PROFILES = {
    "tofu": HarnessProfile(
        profile_id="tofu",
        harbor_agent="rootless_vm.harbor_tofu_agent:TofuHostAgent",
        agent_name="tofu-host",
        agent_version="0.8.4",
        credential_boundary="host-only",
        trajectory_format="ATIF-v1.7",
        comparison_target=None,
        default_max_output_tokens=32_768,
        default_context_window_tokens=None,
        default_max_rounds=4_096,
        default_context_checkpoint_tokens=300_000,
        host_dispatch=True,
        notes="Tofu recovery/checkpoint harness; optimized for robust completion.",
    ),
    "tofu-kimi": HarnessProfile(
        profile_id="tofu-kimi",
        harbor_agent=(
            "rootless_vm.harbor_tofu_runtime_agent:TofuKimiRuntimeAgent"
        ),
        agent_name="tofu-kimi-runtime",
        agent_version="0.17.0",
        credential_boundary="host-only",
        trajectory_format="ATIF-v1.7",
        comparison_target=(
            "Production Tofu AgentRuntime over the same Meituan kimi-k3"
        ),
        default_max_output_tokens=None,
        default_context_window_tokens=272_000,
        default_max_rounds=None,
        default_context_checkpoint_tokens=None,
        host_dispatch=True,
        notes=(
            "Formal candidate path: production AgentRuntime owns model/context/"
            "orchestration; only exclusive client tools cross into rootless QEMU."
        ),
    ),
    "deepseek-minimal": HarnessProfile(
        profile_id="deepseek-minimal",
        harbor_agent=(
            "rootless_vm.harbor_deepseek_minimal_agent:DeepSeekMinimalHostAgent"
        ),
        agent_name="deepseek-minimal-host",
        agent_version="1.0.2",
        credential_boundary="host-only",
        trajectory_format="ATIF-v1.7",
        comparison_target="DeepSeek Harness Minimal",
        default_max_output_tokens=256_000,
        default_context_window_tokens=393_216,
        default_max_rounds=4_096,
        default_context_checkpoint_tokens=None,
        host_dispatch=True,
        notes=(
            "Official Minimal prompt and bash/str_replace_editor surface over the "
            "host-side physically pinned model dispatcher."
        ),
    ),
    "codex": HarnessProfile(
        profile_id="codex",
        harbor_agent="codex",
        agent_name="codex",
        agent_version=None,
        credential_boundary="guest-explicit",
        trajectory_format="ATIF-v1.7",
        comparison_target="Harbor Codex installed agent",
        default_max_output_tokens=None,
        default_context_window_tokens=None,
        default_max_rounds=None,
        default_context_checkpoint_tokens=None,
        host_dispatch=False,
        notes=(
            "Harbor installs and runs Codex CLI inside the task container; use only "
            "with explicitly authorized short-lived guest credentials."
        ),
    ),
    "codex-kimi": HarnessProfile(
        profile_id="codex-kimi",
        harbor_agent=(
            "rootless_vm.harbor_codex_kimi_agent:CodexKimiGuestAgent"
        ),
        agent_name="codex-kimi-guest",
        agent_version="0.149.1",
        credential_boundary="host-only",
        trajectory_format="ATIF-v1.7",
        comparison_target="Codex CLI 0.149.1 over the same Meituan kimi-k3",
        default_max_output_tokens=None,
        default_context_window_tokens=272_000,
        default_max_rounds=None,
        default_context_checkpoint_tokens=244_800,
        host_dispatch=False,
        notes=(
            "Pinned Codex runs in the disposable guest; the Kimi credential stays "
            "inside the formal launcher's loopback-only Responses→Chat proxy."
        ),
    ),
    "claude-code": HarnessProfile(
        profile_id="claude-code",
        harbor_agent="claude-code",
        agent_name="claude-code",
        agent_version=None,
        credential_boundary="guest-explicit",
        trajectory_format="ATIF-v1.7",
        comparison_target="Harbor Claude Code installed agent",
        default_max_output_tokens=None,
        default_context_window_tokens=None,
        default_max_rounds=None,
        default_context_checkpoint_tokens=None,
        host_dispatch=False,
        notes=(
            "Harbor installs and runs Claude Code inside the task container; use "
            "only with explicitly authorized short-lived guest credentials."
        ),
    ),
}


def harness_profile(profile_id: str) -> HarnessProfile:
    """Resolve a stable profile id or fail with the complete allowed set."""

    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        choices = ", ".join(sorted(_PROFILES))
        raise ValueError(
            f"unknown harness {profile_id!r}; choose one of: {choices}"
        ) from exc


def harness_profile_ids() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def harness_profiles() -> tuple[HarnessProfile, ...]:
    return tuple(_PROFILES[key] for key in sorted(_PROFILES))


def profile_for_agent(agent_name_or_import: str | None) -> HarnessProfile | None:
    if not agent_name_or_import:
        return None
    for profile in _PROFILES.values():
        if agent_name_or_import in {profile.harbor_agent, profile.agent_name}:
            return profile
    return None
