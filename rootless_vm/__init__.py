"""Rootless, pure-userspace virtual-machine isolation.

The package intentionally has no dependency on Tofu or an evaluation harness.
Integrations should build on :class:`QemuRuntime` and :class:`SandboxSession`
without weakening their fail-closed defaults.
"""

from .guest_agent import GuestAgent, GuestAgentError, GuestExecResult
from .base_disk import (
    BaseDiskBuildResult,
    BaseDiskLock,
    LockedDownload,
    build_base_disk,
    load_base_disk_lock,
    write_offline_seed_iso,
)
from .image_cache import (
    PreparedImageCache,
    PreparedImageRecipe,
    PreparedImageResult,
    PreparedImageSpec,
    sha256_file,
)
from .integrity import TreeSnapshot, snapshot_tree
from .harbor_runner import HarborRunSpec, harbor_argv, run_harbor
from .harness_profiles import (
    HarnessProfile,
    harness_profile,
    harness_profile_ids,
    harness_profiles,
)
from .qemu import PreflightReport, QemuRuntime, QemuUnavailableError
from .session import (
    LoopbackServiceForward,
    NetworkMode,
    SandboxSession,
    SandboxSpec,
)

__all__ = [
    "NetworkMode",
    "LoopbackServiceForward",
    "PreflightReport",
    "QemuRuntime",
    "QemuUnavailableError",
    "SandboxSession",
    "SandboxSpec",
    "TreeSnapshot",
    "GuestAgent",
    "GuestAgentError",
    "GuestExecResult",
    "BaseDiskBuildResult",
    "BaseDiskLock",
    "LockedDownload",
    "build_base_disk",
    "load_base_disk_lock",
    "write_offline_seed_iso",
    "PreparedImageCache",
    "PreparedImageRecipe",
    "PreparedImageResult",
    "PreparedImageSpec",
    "sha256_file",
    "snapshot_tree",
    "HarborRunSpec",
    "harbor_argv",
    "run_harbor",
    "HarnessProfile",
    "harness_profile",
    "harness_profile_ids",
    "harness_profiles",
]
