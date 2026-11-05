"""Host-local bytecode cache for Python run-command workloads.

Responsibility: accelerate repeated Python subprocesses whose workspace lives
on a network/userspace filesystem by routing CPython's reconstructible ``.pyc``
files to verified host-local storage.  The command, interpreter, process model,
and exit/output semantics remain unchanged; callers receive only an optional
environment overlay plus one exact sandbox bind path.

Entry points: :func:`prepare_python_bytecode_cache` and
:func:`release_python_bytecode_cache`.  Cache authority is explicit in the
task owner, workspace, and interpreter fingerprints.  Retention has no size
quota and shares its primitives with the managed server cache
(``serverctl_pkg.python_bytecode_cache``): namespaces in use are never
removed; others go when unused for the TTL, when their workspace or
interpreter was provably removed, or while the cache volume is below the
shared free-space reserve; a detached sweep removes bytecode whose source
directory was removed.  These bytes are disposable, so cleanup is
best-effort and symlink-safe.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile
import threading

from lib.log import get_logger
from serverctl_pkg.python_bytecode_cache import (
    RUN_NAMESPACE_NAME, reclaim_namespaces, record_namespace_identity,
    request_bytecode_sweep, volume_has_room,
)
logger = get_logger(__name__)

_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
_HARD_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_AUTO_OBSERVATIONS = 256
_MAX_MOUNT_DECISIONS = 256
_NAMESPACE_NAME_RE = RUN_NAMESPACE_NAME
_PYTHON_EXECUTABLE_RE = re.compile(
    r"^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$", re.IGNORECASE)
_OFF_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
_ON_VALUES = frozenset({"1", "true", "yes", "on", "enabled", "force"})
_AUTO_SEED_MODULES = frozenset({
    "build", "compileall", "coverage", "mypy", "nox", "pip", "pytest",
    "tox", "unittest",
})
_LOCAL_CACHE_CLASSES = frozenset({
    "local-block", "container-overlay", "memory-filesystem",
})
_REMOTE_SOURCE_CLASSES = frozenset({
    "network-filesystem", "userspace-filesystem",
})

_state_lock = threading.RLock()
_active_namespaces: dict[str, int] = {}
_auto_observations: OrderedDict[str, int] = OrderedDict()
_mount_decisions: OrderedDict[tuple[str, str], bool] = OrderedDict()


def describe_mount(path):
    """Lazy mount probe so non-Python run commands load no storage stack."""
    from lib.storage_authority_sql.storage_capabilities import describe_mount as probe
    return probe(path)


def _process_user_id() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else 0


@dataclass(frozen=True, slots=True)
class PythonBytecodeCachePolicy:
    """Mode, location and inactivity TTL of one process-wide cache root."""

    mode: str = "auto"
    cache_root: str = ""
    ttl_seconds: int = _DEFAULT_TTL_SECONDS

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @classmethod
    def from_environment(cls) -> "PythonBytecodeCachePolicy":
        enabled_value = os.environ.get(
            "TOFU_RUN_PYTHON_CACHE", "auto").strip().lower()
        if enabled_value in _OFF_VALUES:
            mode = "off"
        elif enabled_value in _ON_VALUES:
            mode = "on"
        else:
            mode = "auto"
        default_root = os.path.join(
            tempfile.gettempdir(), f"tofu-python-pycache-{_process_user_id()}")
        configured_root = os.environ.get(
            "TOFU_RUN_PYTHON_CACHE_DIR", "").strip()
        return cls(
            mode=mode,
            cache_root=os.path.abspath(configured_root or default_root),
            ttl_seconds=_bounded_environment_integer(
                "TOFU_RUN_PYTHON_CACHE_TTL_DAYS",
                _DEFAULT_TTL_SECONDS // (24 * 60 * 60),
                minimum=1,
                maximum=_HARD_TTL_SECONDS // (24 * 60 * 60),
            ) * 24 * 60 * 60,
        )


@dataclass(frozen=True, slots=True)
class PythonBytecodeCacheActivation:
    """One active namespace; release it after the subprocess terminates."""

    cache_root: str
    namespace: str
    pycache_prefix: str
    policy: PythonBytecodeCachePolicy
    lock_fd: int | None = None


def _bounded_environment_integer(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _has_shell_control_syntax(source: str) -> bool:
    """Reject everything that needs shell expansion or command composition."""
    in_single = False
    in_double = False
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\" and not in_single and index + 1 < len(source):
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char in "\n\r" or (
            not in_single and not in_double and char in "$`;&|<>(){}*?[]#"
        ):
            return True
        index += 1
    return in_single or in_double


def parse_python_workload(command: str) -> tuple[str, ...] | None:
    """Return argv only for one static, import-capable Python invocation.

    ``-c`` and informational/isolated/no-site shapes stay untouched: measured
    shell-launch overhead is sub-millisecond, while redirecting a one-shot
    interpreter to a cold prefix can cost hundreds of milliseconds.
    """
    source = str(command or "").strip()
    if not source or _has_shell_control_syntax(source):
        return None
    try:
        argv = tuple(shlex.split(source, posix=True))
    except ValueError:
        return None
    if not argv or not _PYTHON_EXECUTABLE_RE.fullmatch(
        os.path.basename(argv[0])
    ):
        return None

    arguments = argv[1:]
    if not arguments:
        return None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return argv if index + 1 < len(arguments) else None
        if argument in {
            "-B", "-c", "-E", "-h", "--help", "-I", "-S", "-V", "--version",
        }:
            return None
        if (
            re.fullmatch(r"-[bBdEhiIOPqRsSuvVx]+", argument)
            and any(flag in argument[1:] for flag in "BEIS")
        ):
            return None
        if argument == "-m":
            return argv if index + 1 < len(arguments) else None
        if argument == "-X":
            if index + 1 >= len(arguments):
                return None
            if arguments[index + 1].startswith("pycache_prefix"):
                return None
            index += 2
            continue
        if argument.startswith("-Xpycache_prefix"):
            return None
        if argument == "-W":
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        return argv
    return None


def _short_hash(value: str, length: int) -> str:
    return hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:length]


def _resolve_interpreter(
    executable: str,
    cwd: str,
    environment: dict[str, str],
) -> str | None:
    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        candidate = executable
        if not os.path.isabs(candidate):
            candidate = os.path.join(cwd, candidate)
        resolved = os.path.realpath(candidate)
        return resolved if os.path.isfile(resolved) and os.access(resolved, os.X_OK) else None
    found = shutil.which(executable, path=environment.get("PATH"))
    return os.path.realpath(found) if found else None


def _namespace_name(
    owner_user_id: int,
    cwd: str,
    interpreter: str,
    environment: dict[str, str],
) -> str | None:
    try:
        workspace = os.path.realpath(cwd)
        workspace_stat = os.stat(workspace)
        interpreter_stat = os.stat(interpreter)
    except OSError:
        return None
    owner_key = _short_hash(f"owner:{owner_user_id}", 12)
    workspace_key = _short_hash(
        f"{workspace}\0{workspace_stat.st_dev}\0{workspace_stat.st_ino}", 16)
    interpreter_key = _short_hash(
        "\0".join((
            interpreter,
            str(interpreter_stat.st_dev),
            str(interpreter_stat.st_ino),
            str(interpreter_stat.st_size),
            str(interpreter_stat.st_mtime_ns),
            environment.get("VIRTUAL_ENV", ""),
        )),
        16,
    )
    return f"ns-{owner_key}-{workspace_key}-{interpreter_key}"


def _auto_seed_workload(argv: tuple[str, ...]) -> bool:
    """Whether one cold-prefix cost is reasonable in zero-config mode."""
    arguments = argv[1:]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            if index + 1 >= len(arguments):
                return False
            script_name = os.path.basename(arguments[index + 1]).lower()
            return (
                script_name.startswith("test_")
                or script_name.endswith("_test.py")
            )
        if argument == "-m":
            if index + 1 >= len(arguments):
                return False
            module = arguments[index + 1].split(".", 1)[0].lower()
            module_arguments = arguments[index + 2:]
            if any(value in {"-h", "--help", "--version"}
                   for value in module_arguments):
                return False
            return module in _AUTO_SEED_MODULES
        if argument in {"-W", "-X"}:
            index += 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        script_name = os.path.basename(argument).lower()
        return (
            script_name.startswith("test_")
            or script_name.endswith("_test.py")
        )
    return False


def _has_seeded_marker(namespace: Path) -> bool:
    try:
        metadata = (namespace / ".seeded").lstat()
        return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
    except OSError:
        return False


def _auto_cache_ready(namespace: Path, argv: tuple[str, ...]) -> bool:
    """Avoid a cold-prefix tax until a repeat-heavy workload actually repeats."""
    namespace_key = str(namespace)
    with _state_lock:
        if _has_seeded_marker(namespace):
            _auto_observations.pop(namespace_key, None)
            return True
        if not _auto_seed_workload(argv):
            return False
        observations = _auto_observations.pop(namespace_key, 0) + 1
        _auto_observations[namespace_key] = observations
        while len(_auto_observations) > _MAX_AUTO_OBSERVATIONS:
            _auto_observations.popitem(last=False)
        return observations >= 2


def _contains_bytecode(prefix: Path, *, max_entries: int = 10_000) -> bool:
    scanned = 0
    try:
        for _directory, directory_names, file_names in os.walk(
            prefix, topdown=True, followlinks=False
        ):
            scanned += len(directory_names) + len(file_names)
            if any(name.endswith(".pyc") for name in file_names):
                return True
            if scanned >= max_entries:
                return False
    except OSError:
        return False
    return False


def _mounts_support_local_cache(cwd: str, cache_root: Path) -> bool:
    cache_probe_path = (
        cache_root
        if cache_root.is_dir() and not cache_root.is_symlink()
        else cache_root.parent
    )
    key = (os.path.realpath(cwd), os.path.realpath(cache_probe_path))
    with _state_lock:
        cached = _mount_decisions.pop(key, None)
        if cached is not None:
            _mount_decisions[key] = cached
            return cached
    try:
        source_mount = describe_mount(cwd)
        cache_mount = describe_mount(cache_probe_path)
        eligible = (
            source_mount.storage_class in _REMOTE_SOURCE_CLASSES
            and cache_mount.storage_class in _LOCAL_CACHE_CLASSES
        )
    except (OSError, RuntimeError, ValueError):
        eligible = False
    with _state_lock:
        _mount_decisions[key] = eligible
        while len(_mount_decisions) > _MAX_MOUNT_DECISIONS:
            _mount_decisions.popitem(last=False)
    return eligible


def _ensure_private_directory(path: Path) -> None:
    existed = path.exists() or path.is_symlink()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _process_user_id()
        or (existed and stat.S_IMODE(metadata.st_mode) & 0o077)
    ):
        raise ValueError("python bytecode cache directory is not privately owned")
    if not existed:
        os.chmod(path, 0o700)


def _try_namespace_lock(namespace: Path, *, exclusive: bool) -> int | None:
    """Acquire a non-blocking cross-process namespace lease on POSIX."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - cache selection is Unix-only today
        return -1
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(namespace / ".active.lock", flags, 0o600)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        return descriptor
    except (OSError, BlockingIOError):
        if descriptor is not None:
            os.close(descriptor)
        return None


def _close_namespace_lock(descriptor: int | None) -> None:
    if descriptor is None or descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _reclaim(root: Path, policy: PythonBytecodeCachePolicy,
             keep: frozenset[str] = frozenset()) -> int:
    """Remove namespaces nothing can use; never one active in this process."""
    with _state_lock:
        active = {Path(namespace).name for namespace in _active_namespaces}
    return reclaim_namespaces(
        root, pattern=_NAMESPACE_NAME_RE, ttl_seconds=policy.ttl_seconds,
        keep=active | keep, has_space=lambda: volume_has_room(root))


def prepare_python_bytecode_cache(
    command: str,
    cwd: str | None,
    task: dict | None,
    environment: dict[str, str],
    *,
    policy: PythonBytecodeCachePolicy | None = None,
) -> PythonBytecodeCacheActivation | None:
    """Opt one eligible subprocess into a host-local ``.pyc`` namespace."""
    resolved_policy = policy or PythonBytecodeCachePolicy.from_environment()
    if (
        not resolved_policy.enabled
        or resolved_policy.mode not in {"auto", "on", "off"}
        or not cwd
    ):
        return None
    argv = parse_python_workload(command)
    if argv is None:
        return None
    if environment.get("PYTHONPYCACHEPREFIX") or environment.get(
        "PYTHONDONTWRITEBYTECODE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}:
        return None
    try:
        owner_user_id = int((task or {}).get("_userId") or 0)
    except (TypeError, ValueError, OverflowError):
        owner_user_id = 0
    if owner_user_id < 1:
        return None

    try:
        cache_root = Path(resolved_policy.cache_root)
        if cache_root.is_symlink():
            return None
        resolved_cache_root = cache_root.resolve(strict=False)
        broad_roots = {
            Path(resolved_cache_root.anchor),
            Path(tempfile.gettempdir()).resolve(strict=False),
            Path.home().resolve(strict=False),
            Path(cwd).resolve(strict=False),
        }
        if (
            not cache_root.is_absolute()
            or resolved_cache_root in broad_roots
            or resolved_cache_root.parent == resolved_cache_root
        ):
            return None
        cache_root = resolved_cache_root
    except (OSError, RuntimeError, ValueError):
        return None

    interpreter = _resolve_interpreter(argv[0], cwd, environment)
    if not interpreter:
        return None
    namespace_name = _namespace_name(
        owner_user_id, cwd, interpreter, environment)
    if not namespace_name:
        return None
    namespace = cache_root / namespace_name
    if (
        resolved_policy.mode == "auto"
        and not _auto_cache_ready(namespace, argv)
    ):
        return None
    if not _mounts_support_local_cache(cwd, cache_root):
        return None

    namespace_lock_fd = None
    try:
        with _state_lock:
            _ensure_private_directory(cache_root)
            _reclaim(cache_root, resolved_policy, frozenset({namespace_name}))
            if not volume_has_room(cache_root):
                return None
            _ensure_private_directory(namespace)
            namespace_lock_fd = _try_namespace_lock(
                namespace, exclusive=False)
            if namespace_lock_fd is None:
                return None
            prefix = namespace / "pycache"
            _ensure_private_directory(prefix)
            marker = namespace / ".last-used"
            descriptor = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT,
                0o600,
            )
            os.close(descriptor)
            os.utime(marker, None)
            record_namespace_identity(namespace, (cwd, interpreter))
            namespace_text = str(namespace)
            _active_namespaces[namespace_text] = (
                _active_namespaces.get(namespace_text, 0) + 1)
    except (OSError, ValueError) as exc:
        _close_namespace_lock(namespace_lock_fd)
        logger.debug("[python-cache] activation skipped: %s", exc)
        return None

    environment["PYTHONPYCACHEPREFIX"] = str(prefix)
    logger.info(
        "[python-cache] active owner=%s workspace=%s interpreter=%s",
        owner_user_id,
        _short_hash(os.path.realpath(cwd), 12),
        os.path.basename(interpreter),
    )
    return PythonBytecodeCacheActivation(
        cache_root=str(cache_root),
        namespace=str(namespace),
        pycache_prefix=str(prefix),
        policy=resolved_policy,
        lock_fd=(
            namespace_lock_fd
            if namespace_lock_fd is not None and namespace_lock_fd >= 0
            else None
        ),
    )


def release_python_bytecode_cache(
    activation: PythonBytecodeCacheActivation | None,
) -> None:
    """Release one namespace, then reclaim what nothing can use."""
    if activation is None:
        return
    root = Path(activation.cache_root)
    with _state_lock:
        count = _active_namespaces.get(activation.namespace, 0)
        if count <= 1:
            _active_namespaces.pop(activation.namespace, None)
        else:
            _active_namespaces[activation.namespace] = count - 1
        _close_namespace_lock(activation.lock_fd)
        try:
            namespace = Path(activation.namespace)
            if namespace.is_dir() and _contains_bytecode(
                Path(activation.pycache_prefix)
            ):
                marker = namespace / ".seeded"
                descriptor = os.open(
                    marker,
                    os.O_WRONLY | os.O_CREAT,
                    0o600,
                )
                os.close(descriptor)
                os.utime(marker, None)
                _auto_observations.pop(str(namespace), None)
            _reclaim(root, activation.policy)
            if namespace.is_dir():
                request_bytecode_sweep(root, namespace.name)
        except (OSError, ValueError) as exc:
            logger.debug("[python-cache] post-run cleanup skipped: %s", exc)


__all__ = [
    "PythonBytecodeCacheActivation",
    "PythonBytecodeCachePolicy",
    "parse_python_workload",
    "prepare_python_bytecode_cache",
    "release_python_bytecode_cache",
]
