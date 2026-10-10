"""Host-only scratch policy; guest/jail /tmp is deliberately outside this API.

Entry points: validate_host_path checks placement without creating anything;
private_temporary_root creates/checks a mode-0700, current-user-owned root.
Resolution is dynamic: ROOTLESS_VM_TMPDIR, TOFU_EVAL_ROOT/.runtime/tmp, then
checkout .eval-runs/.runtime/tmp. An explicit owner_root selects scratch below
an existing state/cache/log owner instead. No environment or tempfile globals
are modified. Callers own and clean up their unique tempfile children.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


_FORBIDDEN = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
_DOLPHINFS = Path("/mnt/dolphinfs")


def _local_storage_opt_in() -> bool:
    """Explicit operator opt-in for private HOME-directory state roots.

    Some deployments (this host's DolphinFS FUSE, errno 38/ENOSYS) cannot
    create pathname Unix sockets anywhere on the shared volume, which makes
    the pathname-IPC backend physically unusable there while the placement
    policy still requires it. Setting
    ``ROOTLESS_VM_ALLOW_PRIVATE_LOCAL_ROOTS=1`` records an informed operator
    decision to keep durable state on user-private (0700, euid-owned)
    HOME-directory storage instead. Ownership and mode are still enforced
    downstream by ``private_temporary_root``; symlink and '..' checks below
    always apply. This never weakens the credential boundary: VM state
    carries no provider credentials.
    """
    return os.environ.get(
        "ROOTLESS_VM_ALLOW_PRIVATE_LOCAL_ROOTS", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _local_scratch_base() -> Path:
    """HOME-directory base for the opt-in scratch layout (never /tmp).

    ``TOFU_EVAL_LOCAL_SCRATCH`` wins when set; otherwise the XDG-style
    ``~/.local/share/tofu/rootless-vm`` root — private by convention and
    enforced 0700 by callers.
    """
    override = os.environ.get("TOFU_EVAL_LOCAL_SCRATCH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "tofu" / "rootless-vm"


def validate_host_path(path: str | os.PathLike[str]) -> Path:
    """Return an absolute host path, rejecting shared temp and symlink components.

    DolphinFS checkouts keep host storage on that volume. Nonexistent tails
    are allowed. Existing ancestors must be directories and
    not world-writable; group-shared checkout ancestors are allowed, but the
    actual scratch root must be private (checked by private_temporary_root).
    This is validation, not an fd-based defense against a malicious host user
    concurrently renaming ancestors; the checkout/owner ancestors are trusted.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"host path must be absolute without '..': {candidate}")
    local_opt_in = _local_storage_opt_in()
    if candidate == Path("/") or (not local_opt_in and any(
        candidate == root or root in candidate.parents for root in _FORBIDDEN
    )):
        raise ValueError(f"shared host temporary path is forbidden: {candidate}")
    checkout = Path(__file__).absolute().parent.parent
    if not local_opt_in and checkout.is_relative_to(_DOLPHINFS) \
            and not candidate.is_relative_to(_DOLPHINFS):
        raise ValueError("this checkout requires host storage under /mnt/dolphinfs")
    for component in reversed((candidate, *candidate.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"host path must not contain symlinks: {component}")
        if component != candidate and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"host path ancestor is not a directory: {component}")
        if stat.S_ISDIR(metadata.st_mode) and metadata.st_mode & 0o002:
            raise PermissionError(f"host path directory is world-writable: {component}")
    return candidate


def private_temporary_root(
    *, owner_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Create/check private scratch; invalid explicit settings fail closed.

    owner_root, when supplied, uses <owner_root>/.runtime/tmp and takes
    precedence over environment defaults. Existing roots are never chmodded.
    """
    if owner_root is not None:
        root = validate_host_path(owner_root) / ".runtime" / "tmp"
    elif "ROOTLESS_VM_TMPDIR" in os.environ:
        value = os.environ["ROOTLESS_VM_TMPDIR"]
        if not value.strip():
            raise ValueError("ROOTLESS_VM_TMPDIR must not be empty")
        root = Path(value).expanduser()
    elif "TOFU_EVAL_ROOT" in os.environ:
        value = os.environ["TOFU_EVAL_ROOT"]
        if not value.strip():
            raise ValueError("TOFU_EVAL_ROOT must not be empty")
        root = validate_host_path(value) / ".runtime" / "tmp"
    else:
        if _local_storage_opt_in():
            # The default scratch must also leave the shared volume on
            # socket-incapable deployments — QEMU's literal sun_path binds
            # overflow 108 bytes on long checkout prefixes there anyway, and
            # this host's FUSE cannot create pathname Unix sockets at all.
            root = _local_scratch_base() / "tmp"
        else:
            root = Path(__file__).absolute().parent.parent / ".eval-runs" / ".runtime" / "tmp"
    root = validate_host_path(root)
    missing = []
    current = root
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    validate_host_path(root)
    metadata = root.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise PermissionError(f"host scratch root must be owned and private (0700): {root}")
    return root


def child_storage_environment(owner_root: Path) -> dict[str, str]:
    """Private child HOME/tmp/caches tied to an explicit owner lifecycle.

    No global environment mutation or credential copying. The fixed directory
    set is shared by launchers and preparation tools; immutable input caches
    are separately owned. Existing unsafe directories fail closed.
    """
    temporary = private_temporary_root(owner_root=owner_root)
    runtime = temporary.parent
    info = runtime.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError(f"child runtime root must be owned and private: {runtime}")
    paths = {}
    for name in ("tmp", "cache", "state", "data", "home", "config"):
        path = validate_host_path(runtime / name)
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError(f"child runtime directory must be owned and private: {path}")
        paths[name] = path
    cache = paths["cache"]
    return {
        "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
        "ROOTLESS_VM_TMPDIR": str(temporary),
        "HOME": str(paths["home"]), "XDG_CONFIG_HOME": str(paths["config"]),
        "XDG_CACHE_HOME": str(cache), "XDG_STATE_HOME": str(paths["state"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "UV_CACHE_DIR": str(cache / "uv"), "PIP_CACHE_DIR": str(cache / "pip"),
        "HF_HOME": str(cache / "huggingface"),
        "HF_DATASETS_CACHE": str(cache / "huggingface" / "datasets"),
        "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface" / "hub"),
        "HF_HUB_CACHE": str(cache / "huggingface" / "hub"),
        "HF_ASSETS_CACHE": str(cache / "huggingface" / "assets"),
        "HF_MODULES_CACHE": str(cache / "huggingface" / "modules"),
        "TRANSFORMERS_CACHE": str(cache / "huggingface" / "transformers"),
        "TORCH_HOME": str(cache / "torch"),
        "APPTAINER_TMPDIR": str(temporary), "SINGULARITY_TMPDIR": str(temporary),
        "APPTAINER_CACHEDIR": str(cache / "apptainer"),
        "SINGULARITY_CACHEDIR": str(cache / "singularity"),
        "npm_config_cache": str(cache / "npm"),
        "PYTHONPYCACHEPREFIX": str(cache / "pycache"),
    }
