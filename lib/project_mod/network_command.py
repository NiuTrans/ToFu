"""Network-aware planning for bounded ``run_command`` invocations.

Responsibility: recognize one explicit network destination, race lightweight
direct/proxy probes, and project the selected route into one child process.
The original command is never hedged or replayed.  Command execution, approval,
filesystem attribution, timeout, and cancellation remain owned by
``run_command.py``; proxy configuration remains owned by ``lib.proxy``.

Entry points:
``prepare_network_command`` -> ``apply_network_environment`` ->
``finalize_network_command``.
"""

from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import urlparse, urlsplit, urlunsplit

from lib.log import get_logger
from lib.project_mod.command_analysis import _split_pipeline
from lib.project_mod.proxy_connect import (
    ProxyConnectError,
    open_http_connect_tunnel,
)
from lib.subscription_routes import ProbeResult, Route, RouteManager


logger = get_logger(__name__)

__all__ = [
    "NetworkTarget",
    "PreparedNetworkCommand",
    "detect_network_target",
    "prepare_network_command",
    "apply_network_environment",
    "finalize_network_command",
    "format_network_preflight_failure",
    "reset_for_test",
]


_PROXY_ENV_NAMES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)
_NO_PROXY_ENV_NAMES = ("no_proxy", "NO_PROXY")
_ROUTE_ASSIGNMENT_NAMES = frozenset(
    {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "git_proxy_command",
    }
)
_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_SCP_REMOTE_RE = re.compile(
    r"^(?:(?P<user>[^@\s/:]+)@)?"
    r"(?P<host>[A-Za-z0-9_.-]+):(?P<path>.+)$"
)
_GIT_NETWORK_SUBCOMMANDS = frozenset(
    {
        "clone",
        "fetch",
        "ls-remote",
        "pull",
        "push",
        "submodule",
    }
)
_HTTP_EXECUTABLES = frozenset({"curl", "wget"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ssh": 22}
_AUTH_REDIRECT_RE = re.compile(
    r"(?:^|[./?&=_-])(sso|login|signin|oauth|authorize)(?:[./?&=_-]|$)",
    re.IGNORECASE,
)


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not low <= value <= high:
        value = default
    return value


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


_CONNECT_TIMEOUT_S = _bounded_float("TOFU_RUN_NETWORK_CONNECT_TIMEOUT", 2.0, 0.2, 10.0)
_READ_TIMEOUT_S = _bounded_float("TOFU_RUN_NETWORK_READ_TIMEOUT", 3.0, 0.2, 10.0)
_RACE_TIMEOUT_S = _bounded_float("TOFU_RUN_NETWORK_RACE_TIMEOUT", 5.5, 0.5, 12.0)
_MAX_TRACKED_TARGETS = _bounded_int("TOFU_RUN_NETWORK_MAX_TARGETS", 64, 8, 256)


@dataclass(frozen=True)
class NetworkTarget:
    """One credential-free network destination extracted from a command."""

    command_kind: str
    scheme: str
    host: str
    port: int
    probe_url: str

    @property
    def route_key(self) -> str:
        return f"{self.scheme}://{self.host.lower()}:{self.port}"

    @property
    def display_origin(self) -> str:
        return self.route_key


@dataclass(frozen=True)
class PreparedNetworkCommand:
    """A route decision, or a terminal preflight failure before spawn."""

    target: NetworkTarget
    route: "Route | None"
    decision_reason: str
    probe_ms: float
    attempted_route_ids: tuple[str, ...]
    error: str = ""

    @property
    def selected(self) -> bool:
        return self.route is not None and not self.error


def _route_target_key(url: str) -> str:
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port or _DEFAULT_PORTS.get(scheme, 0)
    except (TypeError, ValueError):
        return ""
    if not scheme or not host or not port:
        return ""
    return f"{scheme}://{host}:{port}"


def _probe_http(url: str, route: Route) -> ProbeResult:
    import requests

    started = time.monotonic()
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            url,
            headers={"Accept": "*/*", "User-Agent": "Tofu-Netpath-Probe/1"},
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            proxies=(route.requests_proxies() if route.mode == "proxy" else None),
            allow_redirects=False,
            stream=True,
        )
        status = int(response.status_code or 0)
        headers = getattr(response, "headers", {}) or {}
        location = str(headers.get("Location", ""))
        content_type = str(headers.get("Content-Type", "")).lower()
        response.close()
        latency_ms = (time.monotonic() - started) * 1000.0
        if status == 407:
            return ProbeResult("proxy_auth", latency_ms, status)
        # Authentication redirects/401/403 prove that the target application
        # was reached. They are command outcomes, not broken network paths.
        if status > 0:
            quality = 1
            if "service=git-upload-pack" in urlsplit(url).query:
                if 300 <= status < 400 and _AUTH_REDIRECT_RE.search(location):
                    quality = 1
                elif content_type.startswith(
                    "application/x-git-upload-pack-advertisement"
                ):
                    quality = 3
                else:
                    quality = 2
            return ProbeResult("ok", latency_ms, status, quality)
        return ProbeResult("network_fail", latency_ms, status)
    except requests.exceptions.ProxyError as error:
        # Squid commonly surfaces CONNECT 407 only inside ProxyError rather
        # than as a response. Preserve the actionable credential verdict
        # without copying the credential-bearing exception into diagnostics.
        if "407" in str(error):
            return ProbeResult("proxy_auth", None, 407)
        return ProbeResult("proxy_connect")
    except requests.exceptions.SSLError:
        return ProbeResult("tls_handshake")
    except requests.exceptions.Timeout:
        return ProbeResult("connect_timeout")
    except requests.exceptions.ConnectionError:
        return ProbeResult("network_fail")
    except Exception as error:
        logger.debug("[CommandNet] HTTP probe failed (%s)", type(error).__name__)
        return ProbeResult("network_fail")
    finally:
        session.close()


def _probe_ssh(url: str, route: Route) -> ProbeResult:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 22
    except (TypeError, ValueError):
        return ProbeResult("invalid_target")
    started = time.monotonic()
    stream = None
    try:
        if route.mode == "proxy":
            stream, _prefetched = open_http_connect_tunnel(
                route.proxy_url, host, port, timeout=_CONNECT_TIMEOUT_S
            )
        else:
            stream = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S)
        latency_ms = (time.monotonic() - started) * 1000.0
        # CONNECT/TCP establishment is sufficient path proof. Do not present
        # SSH credentials during probes merely to learn reachability.
        return ProbeResult("ok", latency_ms, 200)
    except ProxyConnectError as error:
        return ProbeResult(error.kind, None, error.status_code)
    except socket.timeout:
        return ProbeResult("connect_timeout")
    except socket.gaierror:
        return ProbeResult("dns_failure")
    except ConnectionRefusedError:
        return ProbeResult("connection_refused")
    except OSError:
        return ProbeResult("network_fail")
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _probe_route(url: str, route: Route) -> ProbeResult:
    try:
        scheme = (urlparse(url).scheme or "").lower()
    except (TypeError, ValueError):
        return ProbeResult("invalid_target")
    if scheme == "ssh":
        return _probe_ssh(url, route)
    if scheme in ("http", "https"):
        return _probe_http(url, route)
    return ProbeResult("unsupported_protocol")


_route_manager = RouteManager(
    probe=_probe_route,
    target_key=_route_target_key,
    max_workers=8,
)
_tracked_targets: OrderedDict[str, None] = OrderedDict()
_tracked_targets_lock = threading.Lock()
_observed_proxy_topology_epoch: "int | None" = None


def _observe_proxy_topology(routes: list[Route]) -> None:
    """Discard command health when the central proxy topology changes."""
    global _observed_proxy_topology_epoch
    epochs = []
    for route in routes:
        match = re.search(r":g(\d+)$", route.route_id)
        if match:
            epochs.append(int(match.group(1)))
    if not epochs:
        return
    current_epoch = max(epochs)
    should_reset = False
    with _tracked_targets_lock:
        if (
            _observed_proxy_topology_epoch is not None
            and current_epoch != _observed_proxy_topology_epoch
        ):
            _tracked_targets.clear()
            should_reset = True
        _observed_proxy_topology_epoch = current_epoch
    if should_reset:
        _route_manager.reset()


def _touch_target(route_key: str) -> None:
    """Bound reconstructible route state without leaving resident workers."""
    should_reset = False
    with _tracked_targets_lock:
        if route_key in _tracked_targets:
            _tracked_targets.move_to_end(route_key)
            return
        if len(_tracked_targets) >= _MAX_TRACKED_TARGETS:
            _tracked_targets.clear()
            should_reset = True
        _tracked_targets[route_key] = None
    if should_reset:
        # Reset invalidates late probe generations and retires the lazy
        # executor. A cache-wide reset is deliberately simpler and safer than
        # evicting one target while one of its probes is still in flight.
        _route_manager.reset()


def _effective_mode() -> str:
    raw = os.environ.get("TOFU_RUN_NETWORK_ROUTE", "auto").strip().lower()
    if raw in ("0", "off", "false", "no", "inherit"):
        return "inherit"
    if raw in ("direct", "proxy"):
        return raw
    return "auto"


def _split_command_words(segment: str) -> tuple[list[str], dict[str, str]]:
    try:
        words = shlex.split(segment, posix=(os.name != "nt"))
    except ValueError:
        return [], {}
    assignments: dict[str, str] = {}
    index = 0
    if words and PurePath(words[0]).name == "env":
        index = 1
        while index < len(words) and words[index].startswith("-"):
            index += 1
    while index < len(words):
        match = _ASSIGNMENT_RE.match(words[index])
        if not match:
            break
        assignments[match.group(1)] = match.group(2)
        index += 1
    return words[index:], assignments


def _contains_explicit_route(words: list[str], assignments: dict[str, str]) -> bool:
    for name in assignments:
        if name.lower() in _ROUTE_ASSIGNMENT_NAMES:
            return True
    # An inline GIT_SSH_COMMAND wins over the child environment at shell
    # evaluation time. Even when it only carries benign options, silently
    # claiming a proxy route would be dishonest because our environment
    # overlay could not append its ProxyCommand.
    if "GIT_SSH_COMMAND" in assignments:
        return True
    ssh_command = assignments.get("GIT_SSH_COMMAND", "")
    ssh_lower = ssh_command.lower()
    if (
        "proxycommand" in ssh_lower
        or "proxyjump" in ssh_lower
        or re.search(r"(?:^|\s)-j(?:\s|$)", ssh_lower)
    ):
        return True

    lowered = [str(word).lower() for word in words]
    for index, word in enumerate(lowered):
        if word in (
            "--proxy",
            "--preproxy",
            "--noproxy",
            "--resolve",
            "--connect-to",
            "--interface",
            "--unix-socket",
            "--bind-address",
            "--no-proxy",
        ):
            return True
        if word.startswith(
            (
                "--proxy=",
                "--preproxy=",
                "--noproxy=",
                "--resolve=",
                "--connect-to=",
                "--interface=",
                "--unix-socket=",
                "--socks4=",
                "--socks4a=",
                "--socks5=",
                "--socks5-hostname=",
            )
        ):
            return True
        if word == "-x" and words and PurePath(words[0]).name in _HTTP_EXECUTABLES:
            return True
        if word in ("-j", "--proxyjump"):
            return True
        if word.startswith("proxycommand=") or word.startswith("proxyjump="):
            return True
        if (
            word == "-c"
            and index + 1 < len(lowered)
            and lowered[index + 1].startswith("http.proxy")
        ):
            return True
        if word.startswith("-c") and "http.proxy" in word:
            return True
    return False


def _ssh_command_port(command: str) -> "int | None":
    if not command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for index, word in enumerate(words):
        if word == "-p" and index + 1 < len(words):
            try:
                return int(words[index + 1])
            except ValueError:
                return None
        lower = word.lower()
        if lower.startswith("-oport="):
            try:
                return int(word.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _resolve_ssh_endpoint(
    host: str, port: "int | None", ssh_command: str = ""
) -> tuple[str, int, bool]:
    """Resolve Host/Port and whether ssh_config already owns the route."""
    command_port = _ssh_command_port(ssh_command)
    effective_port = command_port or port
    argv = ["ssh", "-G"]
    if effective_port:
        argv.extend(["-p", str(effective_port)])
    argv.append(host)
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return host.lower(), int(effective_port or 22), False

    values = {}
    for line in (result.stdout or "").splitlines():
        key, separator, value = line.partition(" ")
        if separator and key in ("hostname", "port", "proxycommand", "proxyjump"):
            values[key] = value.strip()
    resolved_host = values.get("hostname") or host
    try:
        resolved_port = int(effective_port or values.get("port") or 22)
    except ValueError:
        resolved_port = int(effective_port or 22)
    proxy_command = (values.get("proxycommand") or "").strip().lower()
    proxy_jump = (values.get("proxyjump") or "").strip().lower()
    config_owns_route = bool(proxy_command and proxy_command != "none") or bool(
        proxy_jump and proxy_jump != "none"
    )
    return resolved_host.lower(), resolved_port, config_owns_route


def _sanitized_http_probe_url(raw_url: str, command_kind: str) -> NetworkTarget:
    parsed = urlsplit(raw_url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or _DEFAULT_PORTS[scheme]
    hostport = f"[{host}]" if ":" in host else host
    if port != _DEFAULT_PORTS[scheme]:
        hostport += f":{port}"
    path = parsed.path or "/"
    if command_kind.startswith("git:"):
        path = path.rstrip("/") + "/info/refs"
        query = "service=git-upload-pack"
    else:
        # Probe only the origin for generic HTTP commands. Signed query/path
        # material is neither needed for reachability nor safe to multiply.
        path = "/"
        query = ""
    probe_url = urlunsplit((scheme, hostport, path, query, ""))
    return NetworkTarget(command_kind, scheme, host, port, probe_url)


def _target_from_url(
    token: str, command_kind: str, ssh_command: str = ""
) -> tuple["NetworkTarget | None", bool]:
    try:
        parsed = urlsplit(token)
    except (TypeError, ValueError):
        return None, False
    scheme = (parsed.scheme or "").lower()
    if scheme in ("http", "https") and parsed.hostname:
        return _sanitized_http_probe_url(token, command_kind), False
    if scheme == "ssh" and parsed.hostname:
        host, port, configured = _resolve_ssh_endpoint(
            parsed.hostname, parsed.port, ssh_command
        )
        target = NetworkTarget(command_kind, "ssh", host, port, f"ssh://{host}:{port}/")
        return target, configured

    match = _SCP_REMOTE_RE.match(token)
    if not match:
        return None, False
    host = match.group("host") or ""
    # Avoid treating ordinary relative ``name:path`` operands as SSH remotes.
    if not match.group("user") and "." not in host and host != "localhost":
        return None, False
    host, port, configured = _resolve_ssh_endpoint(host, None, ssh_command)
    target = NetworkTarget(command_kind, "ssh", host, port, f"ssh://{host}:{port}/")
    return target, configured


def _git_target(
    words: list[str], assignments: dict[str, str]
) -> tuple["NetworkTarget | None", bool]:
    if not words or PurePath(words[0]).name != "git":
        return None, False
    subcommand = ""
    subcommand_index = -1
    for index, word in enumerate(words[1:], 1):
        if word in _GIT_NETWORK_SUBCOMMANDS:
            subcommand = word
            subcommand_index = index
            break
    if not subcommand:
        return None, False
    # Recursive clone/submodule commands may discover additional destinations
    # after spawn, so one environment-level route cannot honestly cover them.
    if any(
        word in ("--recurse-submodules", "--recursive")
        for word in words[subcommand_index + 1 :]
    ):
        return None, False
    ssh_command = assignments.get("GIT_SSH_COMMAND", "")
    for token in words[subcommand_index + 1 :]:
        target, configured = _target_from_url(token, f"git:{subcommand}", ssh_command)
        if target is not None:
            return target, configured
    return None, False


def _http_command_targets(words: list[str]) -> list[NetworkTarget]:
    if not words or PurePath(words[0]).name not in _HTTP_EXECUTABLES:
        return []
    command_kind = PurePath(words[0]).name
    targets = []
    for token in words[1:]:
        target, _configured = _target_from_url(token, command_kind)
        if target is not None and target.scheme in ("http", "https"):
            targets.append(target)
    return targets


def detect_network_target(command: str) -> "NetworkTarget | None":
    """Return one unambiguous eligible target, else leave shell semantics."""
    targets: dict[str, NetworkTarget] = {}
    for segment in _split_pipeline(command or ""):
        words, assignments = _split_command_words(segment)
        if not words:
            continue
        if _contains_explicit_route(words, assignments):
            return None
        target, ssh_configured = _git_target(words, assignments)
        if ssh_configured:
            return None
        segment_targets = (
            [target] if target is not None else _http_command_targets(words)
        )
        for segment_target in segment_targets:
            targets[segment_target.route_key] = segment_target
            if len(targets) > 1:
                return None
    if len(targets) != 1:
        return None
    return next(iter(targets.values()))


def prepare_network_command(
    command: str, child_env: dict, task=None
) -> "PreparedNetworkCommand | None":
    """Select a route without ever launching or replaying ``command``."""
    mode = _effective_mode()
    if mode == "inherit":
        return None
    target = detect_network_target(command)
    if target is None:
        return None
    if target.scheme == "ssh":
        inherited_ssh_command = str(child_env.get("GIT_SSH_COMMAND") or "")
        inherited_lower = inherited_ssh_command.lower()
        try:
            inherited_ssh_words = shlex.split(inherited_ssh_command)
        except ValueError:
            return None
        if inherited_ssh_words and (
            PurePath(inherited_ssh_words[0]).name != "ssh"
            or "-F" in inherited_ssh_words
        ):
            return None
        if (
            "proxycommand" in inherited_lower
            or "proxyjump" in inherited_lower
            or re.search(r"(?:^|\s)-j(?:\s|$)", inherited_lower)
        ):
            return None
        inherited_port = _ssh_command_port(inherited_ssh_command)
        if inherited_port and inherited_port != target.port:
            target = NetworkTarget(
                target.command_kind,
                target.scheme,
                target.host,
                inherited_port,
                f"ssh://{target.host}:{inherited_port}/",
            )

    from lib.proxy import global_egress_route_specs

    respect_no_proxy = os.environ.get(
        "TOFU_RUN_NETWORK_RESPECT_NO_PROXY", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    routes = global_egress_route_specs(
        target.probe_url,
        include_bypassed=not respect_no_proxy,
        limit=4,
    )
    _observe_proxy_topology(routes)
    direct = next((route for route in routes if route.mode == "direct"), None)
    proxies = [route for route in routes if route.mode == "proxy"]
    attempted = tuple(route.route_id for route in routes)

    if mode == "direct":
        if direct is None:
            return PreparedNetworkCommand(
                target,
                None,
                "forced_direct",
                0.0,
                attempted,
                "No direct route is eligible for this destination.",
            )
        return PreparedNetworkCommand(target, direct, "forced_direct", 0.0, attempted)
    if mode == "proxy":
        if not proxies:
            return PreparedNetworkCommand(
                target,
                None,
                "forced_proxy",
                0.0,
                attempted,
                "No configured proxy route is eligible for this destination.",
            )
        return PreparedNetworkCommand(
            target, proxies[0], "forced_proxy", 0.0, attempted
        )

    # With no alternative there is nothing to race; preserve the historical
    # inherited shell path and add zero probe latency.
    if direct is None or not proxies:
        return None
    if task and task.get("aborted"):
        return PreparedNetworkCommand(
            target,
            None,
            "aborted",
            0.0,
            attempted,
            "Command aborted before network route selection.",
        )

    _touch_target(target.route_key)
    cached = _route_manager.cached_candidates(target.probe_url, routes)
    started = time.monotonic()
    candidates = _route_manager.candidates(
        target.probe_url,
        routes,
        wait_timeout=_RACE_TIMEOUT_S,
        cancelled=((lambda: bool(task.get("aborted"))) if task is not None else None),
        wait_for_all=(
            target.command_kind.startswith("git:")
            and target.scheme in ("http", "https")
        ),
        minimum_quality=2,
    )
    probe_ms = (time.monotonic() - started) * 1000.0
    if task and task.get("aborted"):
        return PreparedNetworkCommand(
            target,
            None,
            "aborted",
            probe_ms,
            attempted,
            "Command aborted during network route selection.",
        )
    if not candidates:
        return PreparedNetworkCommand(
            target,
            None,
            "parallel_probe_exhausted",
            probe_ms,
            attempted,
            "Direct and configured proxy probes could not reach the target.",
        )
    return PreparedNetworkCommand(
        target,
        candidates[0],
        "cached_health" if cached else "parallel_probe",
        probe_ms,
        attempted,
    )


def apply_network_environment(
    child_env: dict, prepared: "PreparedNetworkCommand | None"
) -> None:
    """Apply one selected route to one child env; never mutate ``os.environ``."""
    if prepared is None or not prepared.selected:
        return
    route = prepared.route
    target = prepared.target
    assert route is not None

    if target.scheme in ("http", "https"):
        if route.mode == "direct":
            for name in _PROXY_ENV_NAMES:
                child_env.pop(name, None)
            for name in _NO_PROXY_ENV_NAMES:
                child_env[name] = "*"
        else:
            for name in _PROXY_ENV_NAMES:
                child_env[name] = route.proxy_url
            for name in _NO_PROXY_ENV_NAMES:
                child_env[name] = ""
        return

    if target.scheme == "ssh" and route.mode == "proxy":
        connector = (
            f"{shlex.quote(sys.executable)} -m lib.project_mod.proxy_connect %h %p"
        )
        existing = str(child_env.get("GIT_SSH_COMMAND") or "ssh").strip()
        child_env["GIT_SSH_COMMAND"] = (
            existing + " -o " + shlex.quote("ProxyCommand=" + connector)
        )
        child_env["TOFU_COMMAND_PROXY_URL"] = route.proxy_url
        child_env["TOFU_COMMAND_PROXY_CONNECT_TIMEOUT"] = str(_CONNECT_TIMEOUT_S)


_OUTCOME_PATTERNS = (
    (
        "dns_failure",
        re.compile(
            r"could not resolve host|temporary failure in name resolution|"
            r"name or service not known",
            re.I,
        ),
    ),
    (
        "connect_timeout",
        re.compile(
            r"connection timed out|connect(?:ion)? timeout|operation timed out", re.I
        ),
    ),
    ("connection_refused", re.compile(r"connection refused", re.I)),
    ("network_unreachable", re.compile(r"network is unreachable", re.I)),
    (
        "proxy_connect",
        re.compile(
            r"proxy connector failed|proxy error|proxy connect|"
            r"could not connect to proxy|http 407|"
            r"407 proxy authentication required",
            re.I,
        ),
    ),
    (
        "connection_reset",
        re.compile(r"connection reset by peer|remote end hung up", re.I),
    ),
    (
        "tls_handshake",
        re.compile(
            r"certificate verify failed|ssl certificate problem|tls handshake", re.I
        ),
    ),
    (
        "client_configuration",
        re.compile(
            r"unsupported option|bad configuration option|unknown option|"
            r"host key verification failed",
            re.I,
        ),
    ),
    (
        "authentication_required",
        re.compile(
            r"unable to update url base from redirection|/sson/login|"
            r"permission denied \(publickey|authentication failed|"
            r"could not read username|terminal prompts disabled",
            re.I,
        ),
    ),
    (
        "repository_access",
        re.compile(
            r"repository not found|does not appear to be a git repository|"
            r"could not read from remote repository",
            re.I,
        ),
    ),
)
_NETWORK_FAILURE_OUTCOMES = frozenset(
    {
        "dns_failure",
        "connect_timeout",
        "connection_refused",
        "network_unreachable",
        "proxy_connect",
        "tls_handshake",
        "connection_reset",
    }
)


def _classify_network_output(output: str) -> str:
    classified_text = output or ""
    if classified_text.startswith("$ ") and "\n" in classified_text:
        classified_text = classified_text.split("\n", 1)[1]
    for outcome, pattern in _OUTCOME_PATTERNS:
        if pattern.search(classified_text):
            return outcome
    match = re.search(r"\[exit code: (-?\d+)\]\s*$", output or "")
    if match and match.group(1) == "0":
        return "success"
    return "command_failure"


def _insert_annotations(output: str, annotations: list[str]) -> str:
    if not annotations:
        return output
    marker = "\n".join(annotations) + "\n"
    first_newline = output.find("\n")
    if first_newline < 0:
        return output + "\n" + marker.rstrip()
    return output[: first_newline + 1] + marker + output[first_newline + 1 :]


def finalize_network_command(
    prepared: "PreparedNetworkCommand | None", output: str
) -> str:
    """Report the real outcome and add credential-free model diagnostics."""
    if prepared is None or not prepared.selected:
        return output
    route = prepared.route
    assert route is not None
    outcome = _classify_network_output(output)
    route_ok = outcome not in _NETWORK_FAILURE_OUTCOMES
    try:
        _route_manager.report(
            prepared.target.probe_url,
            route,
            route_ok,
            failure_kind=outcome if not route_ok else "network_fail",
        )
        if route.pool_id:
            from lib.proxy import pool_note_outcome

            pool_note_outcome(route.pool_id, route_ok)
    except Exception as error:
        logger.debug("[CommandNet] outcome report failed (%s)", type(error).__name__)

    annotations = [
        "[network route: mode=%s route=%s target=%s decision=%s probe_ms=%d]"
        % (
            route.mode,
            route.route_id,
            prepared.target.display_origin,
            prepared.decision_reason,
            round(prepared.probe_ms),
        ),
    ]
    if outcome != "success":
        annotations.append(
            "[network outcome: %s; command_attempts=1; automatic_replay=false]"
            % outcome
        )
        if re.search(r"\[exit code: 0\]\s*$", output or ""):
            annotations.append(
                "[network warning: shell pipeline exit code masked an "
                "upstream network-command failure]"
            )
    return _insert_annotations(output, annotations)


def format_network_preflight_failure(
    command: str, prepared: PreparedNetworkCommand
) -> str:
    """Return a normal run-command refusal (no fake process exit marker)."""
    attempted = ", ".join(prepared.attempted_route_ids) or "none"
    return (
        f"$ {command}\n\n"
        f"Error: Network route unavailable for "
        f"{prepared.target.display_origin}. {prepared.error} "
        f"Routes checked: {attempted}. The command was not started and the "
        f"same command should not be retried until network or proxy "
        f"configuration changes."
    )


def reset_for_test() -> None:
    """Reset bounded process-local health without touching proxy settings."""
    global _observed_proxy_topology_epoch
    with _tracked_targets_lock:
        _tracked_targets.clear()
        _observed_proxy_topology_epoch = None
    _route_manager.reset()
