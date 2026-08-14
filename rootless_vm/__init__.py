"""Rootless, pure-userspace virtual-machine isolation.

The package intentionally has no dependency on Tofu or an evaluation harness.
Integrations should build on :class:`QemuRuntime` and :class:`SandboxSession`
without weakening their fail-closed defaults.
"""

from .guest_agent import GuestAgent, GuestAgentError, GuestExecResult
from .image_cache import (
    PreparedImageCache,
    PreparedImageRecipe,
    PreparedImageResult,
    PreparedImageSpec,
    sha256_file,
)
from .integrity import TreeSnapshot, snapshot_tree
from .harbor_runner import HarborRunSpec, harbor_argv, run_harbor
from .qemu import PreflightReport, QemuRuntime, QemuUnavailableError
from .session import NetworkMode, SandboxSession, SandboxSpec

__all__ = [
    "NetworkMode",
    "PreflightReport",
    "QemuRuntime",
    "QemuUnavailableError",
    "SandboxSession",
    "SandboxSpec",
    "TreeSnapshot",
    "GuestAgent",
    "GuestAgentError",
    "GuestExecResult",
    "PreparedImageCache",
    "PreparedImageRecipe",
    "PreparedImageResult",
    "PreparedImageSpec",
    "sha256_file",
    "snapshot_tree",
    "HarborRunSpec",
    "harbor_argv",
    "run_harbor",
]
