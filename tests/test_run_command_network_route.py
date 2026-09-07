"""Network-aware run_command routing: probe races, never command races."""

from __future__ import annotations

import shlex
import sys
from types import SimpleNamespace

import pytest
import requests

from lib.project_mod import network_command as network
from lib.project_mod.proxy_connect import (
    ProxyConnectError,
    open_http_connect_tunnel,
)
from lib.project_mod.run_command import tool_run_command
from lib.subscription_routes import ProbeResult, Route, RouteManager


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_command_network(monkeypatch):
    monkeypatch.setenv("TOFU_RUN_NETWORK_ROUTE", "auto")
    monkeypatch.delenv("TOFU_RUN_NETWORK_RESPECT_NO_PROXY", raising=False)
    network.reset_for_test()
    yield
    network.reset_for_test()


def _proxy() -> Route:
    return Route(
        "proxy:environment",
        "environment proxy",
        "proxy",
        priority=100,
        proxy_url="http://proxy.test:8080",
    )


def _direct() -> Route:
    return Route("direct", "direct", "direct")


def _https_target() -> network.NetworkTarget:
    return network.NetworkTarget(
        "git:clone",
        "https",
        "dev.example.test",
        443,
        "https://dev.example.test/repo.git/info/refs?service=git-upload-pack",
    )


def test_detects_git_https_inside_output_pipeline():
    target = network.detect_network_target(
        "GIT_TERMINAL_PROMPT=0 git clone --depth 1 "
        "https://dev.example.test/team/repo.git checkout 2>&1 | tail -5; "
        "du -sh checkout"
    )
    assert target is not None
    assert target.route_key == "https://dev.example.test:443"
    assert target.probe_url.endswith("/team/repo.git/info/refs?service=git-upload-pack")


def test_ssh_target_uses_effective_configured_port(monkeypatch):
    monkeypatch.setattr(
        network.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "host dev.example.test\n"
                "hostname dev.example.test\n"
                "port 3022\n"
                "proxycommand none\n"
            ),
            returncode=0,
        ),
    )
    target = network.detect_network_target(
        "git clone git@dev.example.test:team/repo.git checkout"
    )
    assert target is not None
    assert target.scheme == "ssh"
    assert target.port == 3022
    assert target.route_key == "ssh://dev.example.test:3022"


@pytest.mark.parametrize(
    "command",
    [
        "HTTPS_PROXY=http://manual.test:8080 git clone "
        "https://dev.example.test/repo.git checkout",
        'GIT_SSH_COMMAND="ssh -o BatchMode=yes" git clone '
        "git@dev.example.test:repo.git checkout",
        "git -c http.proxy=http://manual.test:8080 clone "
        "https://dev.example.test/repo.git checkout",
        "curl --proxy http://manual.test:8080 https://dev.example.test/",
        "curl --proxy=http://manual.test:8080 https://dev.example.test/",
        "curl --resolve dev.example.test:443:127.0.0.1 https://dev.example.test/",
    ],
)
def test_explicit_route_configuration_is_never_overridden(command):
    assert network.detect_network_target(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "curl https://one.example.test/; curl https://two.example.test/",
        "curl https://one.example.test/ https://two.example.test/",
    ],
)
def test_multiple_destinations_keep_original_shell_semantics(command):
    assert network.detect_network_target(command) is None


def test_ssh_config_proxycommand_keeps_ssh_authority(monkeypatch):
    monkeypatch.setattr(
        network.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "hostname dev.example.test\n"
                "port 3022\n"
                "proxycommand custom-connect %h %p\n"
            ),
            returncode=0,
        ),
    )
    assert (
        network.detect_network_target(
            "git clone git@dev.example.test:repo.git checkout"
        )
        is None
    )


class _FakeManager:
    def __init__(self, candidates):
        self.result = list(candidates)
        self.calls = []
        self.reports = []
        self.resets = 0

    def cached_candidates(self, url, routes):
        self.calls.append(("cached", url, tuple(r.route_id for r in routes)))
        return []

    def candidates(self, url, routes, wait_timeout, **kwargs):
        self.calls.append(
            (
                "race",
                url,
                tuple(r.route_id for r in routes),
                wait_timeout,
                kwargs,
            )
        )
        return list(self.result)

    def report(self, url, route, ok, latency_ms=None, failure_kind="network_fail"):
        self.reports.append((url, route.route_id, ok, failure_kind))

    def reset(self):
        self.resets += 1


def test_parallel_probe_selects_proxy_and_projects_only_child_env(monkeypatch):
    fake = _FakeManager([_proxy()])
    monkeypatch.setattr(network, "_route_manager", fake)
    monkeypatch.setattr(
        "lib.proxy.global_egress_route_specs",
        lambda *args, **kwargs: [_direct(), _proxy()],
    )
    child_env = {
        "HTTP_PROXY": "http://old.test:1",
        "NO_PROXY": ".example.test",
    }

    prepared = network.prepare_network_command(
        "git clone https://dev.example.test/repo.git checkout", child_env
    )
    assert prepared is not None and prepared.selected
    assert prepared.route.route_id == "proxy:environment"
    assert prepared.decision_reason == "parallel_probe"
    assert [call[0] for call in fake.calls] == ["cached", "race"]
    assert fake.calls[1][4]["wait_for_all"] is True
    assert fake.calls[1][4]["minimum_quality"] == 2

    network.apply_network_environment(child_env, prepared)
    assert child_env["HTTP_PROXY"] == "http://proxy.test:8080"
    assert child_env["https_proxy"] == "http://proxy.test:8080"
    assert child_env["NO_PROXY"] == ""
    assert child_env["no_proxy"] == ""


def test_direct_projection_removes_every_proxy_variable():
    prepared = network.PreparedNetworkCommand(
        _https_target(), _direct(), "forced_direct", 0, ("direct",)
    )
    child_env = {name: "http://secret.test:8080" for name in network._PROXY_ENV_NAMES}
    network.apply_network_environment(child_env, prepared)
    assert not any(name in child_env for name in network._PROXY_ENV_NAMES)
    assert child_env["NO_PROXY"] == "*"
    assert child_env["no_proxy"] == "*"


def test_ssh_proxy_projection_hides_proxy_url_from_git_command():
    target = network.NetworkTarget(
        "git:clone", "ssh", "dev.example.test", 3022, "ssh://dev.example.test:3022/"
    )
    prepared = network.PreparedNetworkCommand(
        target, _proxy(), "parallel_probe", 12, ("direct", "proxy:environment")
    )
    child_env = {}
    network.apply_network_environment(child_env, prepared)
    assert "lib.project_mod.proxy_connect %h %p" in child_env["GIT_SSH_COMMAND"]
    assert "http://proxy.test:8080" not in child_env["GIT_SSH_COMMAND"]
    assert child_env["TOFU_COMMAND_PROXY_URL"] == "http://proxy.test:8080"


def test_inherited_git_ssh_proxycommand_remains_authoritative(monkeypatch):
    target = network.NetworkTarget(
        "git:clone", "ssh", "dev.example.test", 22, "ssh://dev.example.test:22/"
    )
    monkeypatch.setattr(network, "detect_network_target", lambda _command: target)
    called = []
    monkeypatch.setattr(
        "lib.proxy.global_egress_route_specs",
        lambda *args, **kwargs: called.append(True),
    )
    prepared = network.prepare_network_command(
        "git clone git@dev.example.test:repo.git checkout",
        {"GIT_SSH_COMMAND": "ssh -o 'ProxyCommand=custom-connect %h %p'"},
    )
    assert prepared is None
    assert called == []


def test_inherited_git_ssh_port_participates_in_route_identity(monkeypatch):
    target = network.NetworkTarget(
        "git:clone", "ssh", "dev.example.test", 22, "ssh://dev.example.test:22/"
    )
    fake = _FakeManager([_proxy()])
    monkeypatch.setattr(network, "_route_manager", fake)
    monkeypatch.setattr(network, "detect_network_target", lambda _command: target)
    monkeypatch.setattr(
        "lib.proxy.global_egress_route_specs",
        lambda *args, **kwargs: [_direct(), _proxy()],
    )
    prepared = network.prepare_network_command(
        "git clone git@dev.example.test:repo.git checkout",
        {"GIT_SSH_COMMAND": "ssh -p 3022"},
    )
    assert prepared is not None
    assert prepared.target.route_key == "ssh://dev.example.test:3022"
    assert fake.calls[0][1] == "ssh://dev.example.test:3022/"


def test_all_routes_failed_refuses_before_spawn(monkeypatch, tmp_path):
    marker = tmp_path / "must-not-exist"
    target = _https_target()
    prepared = network.PreparedNetworkCommand(
        target,
        None,
        "parallel_probe_exhausted",
        30,
        ("direct", "proxy:environment"),
        "Direct and configured proxy probes could not reach the target.",
    )
    monkeypatch.setattr(
        network, "prepare_network_command", lambda *args, **kwargs: prepared
    )
    command = f"touch {shlex.quote(str(marker))}"

    output = tool_run_command(str(tmp_path), command)
    assert not marker.exists()
    assert "The command was not started" in output
    assert "[exit code:" not in output


def test_selected_route_executes_original_command_exactly_once(monkeypatch, tmp_path):
    counter = tmp_path / "counter.txt"
    target = _https_target()
    prepared = network.PreparedNetworkCommand(
        target, _proxy(), "parallel_probe", 8, ("direct", "proxy:environment")
    )
    fake = _FakeManager([_proxy()])
    monkeypatch.setattr(network, "_route_manager", fake)
    monkeypatch.setattr(
        network, "prepare_network_command", lambda *args, **kwargs: prepared
    )
    script = (
        "import os; from pathlib import Path; "
        f"p=Path({str(counter)!r}); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x'); "
        "print(os.environ['HTTPS_PROXY'])"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    output = tool_run_command(str(tmp_path), command)
    assert counter.read_text() == "x"
    assert "http://proxy.test:8080" in output
    assert "[network route: mode=proxy" in output
    assert "[exit code: 0]" in output


def test_sso_redirect_is_auth_not_network_and_pipeline_mask_is_visible(monkeypatch):
    fake = _FakeManager([_direct()])
    monkeypatch.setattr(network, "_route_manager", fake)
    prepared = network.PreparedNetworkCommand(
        _https_target(), _direct(), "parallel_probe", 9, ("direct", "proxy:environment")
    )
    output = network.finalize_network_command(
        prepared,
        "$ git clone URL dst | tail\n"
        "fatal: unable to update url base from redirection:\n"
        " redirect: https://sso.example.test/sson/login\n\n"
        "[exit code: 0]",
    )
    assert "network outcome: authentication_required" in output
    assert "pipeline exit code masked" in output
    assert fake.reports[-1][2] is True


def test_connect_timeout_marks_route_unhealthy_even_when_shell_returns_zero(
    monkeypatch,
):
    fake = _FakeManager([_direct()])
    monkeypatch.setattr(network, "_route_manager", fake)
    target = network.NetworkTarget(
        "git:clone", "ssh", "dev.example.test", 3022, "ssh://dev.example.test:3022/"
    )
    prepared = network.PreparedNetworkCommand(
        target, _direct(), "parallel_probe", 9, ("direct", "proxy:environment")
    )
    output = network.finalize_network_command(
        prepared,
        "$ git clone URL dst | tail\n"
        "ssh: connect to host dev.example.test port 3022: "
        "Connection timed out\n\n[exit code: 0]",
    )
    assert "network outcome: connect_timeout" in output
    assert fake.reports[-1][2] is False


def test_http_probe_classifies_connect_407_as_proxy_auth(monkeypatch):
    error = requests.exceptions.ProxyError(
        "Tunnel connection failed: 407 Proxy Authentication Required"
    )

    def fail_with_proxy_auth(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(requests.Session, "get", fail_with_proxy_auth)
    result = network._probe_http(
        "https://dev.example.test/repo.git/info/refs", _proxy()
    )
    assert result.verdict == "proxy_auth"
    assert result.status_code == 407


@pytest.mark.parametrize(
    ("status", "headers", "quality"),
    [
        (302, {"Location": "https://sso.example.test/sson/login"}, 1),
        (401, {}, 2),
        (
            200,
            {"Content-Type": "application/x-git-upload-pack-advertisement"},
            3,
        ),
    ],
)
def test_git_http_probe_distinguishes_sso_from_protocol_response(
    monkeypatch, status, headers, quality
):
    response = SimpleNamespace(
        status_code=status,
        headers=headers,
        close=lambda: None,
    )
    monkeypatch.setattr(requests.Session, "get", lambda *_args, **_kwargs: response)
    result = network._probe_http(_https_target().probe_url, _direct())
    assert result.verdict == "ok"
    assert result.quality == quality


def test_route_manager_can_isolate_protocol_and_port_for_same_host():
    manager = RouteManager(
        probe=lambda _url, _route: ProbeResult("ok", 10, 200),
        target_key=network._route_target_key,
        jitter=lambda value: value,
        max_workers=2,
    )
    try:
        assert manager.candidates(
            "https://same.example.test/", [_direct()], wait_timeout=1
        )
        assert manager.candidates(
            "ssh://same.example.test:3022/", [_direct()], wait_timeout=1
        )
        keys = set(manager.status()["routes"])
        assert keys == {
            "https://same.example.test:443",
            "ssh://same.example.test:3022",
        }
    finally:
        manager.close()


def test_tracked_target_cache_is_hard_bounded(monkeypatch):
    fake = _FakeManager([])
    monkeypatch.setattr(network, "_route_manager", fake)
    monkeypatch.setattr(network, "_MAX_TRACKED_TARGETS", 2)
    network._tracked_targets.clear()
    network._touch_target("https://one.test:443")
    network._touch_target("https://two.test:443")
    network._touch_target("https://three.test:443")
    assert list(network._tracked_targets) == ["https://three.test:443"]
    assert fake.resets == 1


def test_proxy_topology_epoch_invalidates_command_route_health(monkeypatch):
    proxy_one = Route(
        "proxy:environment:g1",
        "environment proxy",
        "proxy",
        proxy_url="http://one.test:8080",
    )
    proxy_two = Route(
        "proxy:environment:g2",
        "environment proxy",
        "proxy",
        proxy_url="http://two.test:8080",
    )
    current = [[_direct(), proxy_one]]
    fake = _FakeManager([proxy_one])
    monkeypatch.setattr(network, "_route_manager", fake)
    monkeypatch.setattr(
        "lib.proxy.global_egress_route_specs",
        lambda *args, **kwargs: current[0],
    )
    command = "curl https://dev.example.test/"
    assert network.prepare_network_command(command, {}) is not None
    assert fake.resets == 0

    current[0] = [_direct(), proxy_two]
    fake.result = [proxy_two]
    assert network.prepare_network_command(command, {}) is not None
    assert fake.resets == 1


class _FakeSocket:
    def __init__(self, response: bytes):
        self.response = response
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, _size):
        response, self.response = self.response, b""
        return response

    def close(self):
        self.closed = True


def test_http_connect_tunnel_supports_basic_auth_without_error_leak(monkeypatch):
    stream = _FakeSocket(b"HTTP/1.1 200 Connection established\r\n\r\nSSH-2.0-test\r\n")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: stream)
    opened, prefetched = open_http_connect_tunnel(
        "http://user:secret@proxy.test:8080", "dev.example.test", 3022, timeout=1
    )
    assert opened is stream
    assert prefetched == b"SSH-2.0-test\r\n"
    request = bytes(stream.sent)
    assert b"CONNECT dev.example.test:3022 HTTP/1.1" in request
    assert b"Proxy-Authorization: Basic " in request
    assert b"secret" not in request


def test_http_connect_407_has_typed_credential_free_failure(monkeypatch):
    stream = _FakeSocket(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: stream)
    with pytest.raises(ProxyConnectError) as raised:
        open_http_connect_tunnel(
            "http://user:secret@proxy.test:8080", "dev.example.test", 3022, timeout=1
        )
    assert raised.value.kind == "proxy_auth"
    assert "secret" not in str(raised.value)
