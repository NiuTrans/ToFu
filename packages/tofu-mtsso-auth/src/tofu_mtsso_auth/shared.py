"""Serialize upstream source refresh/login across MCP processes, without grants.

The source file stays SDK-owned. An adjacent private advisory lock coordinates
our MCP readers; external CLI edits fence cache insertion via the content digest.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import time


@contextmanager
def _source_lock(path, timeout):
    from . import OfficialAuthError
    import fcntl
    # Never follow a lock symlink or broaden permissions on an existing lock.
    try:
        descriptor = os.open(path + ".mtsso.lock", os.O_CREAT | os.O_RDWR |
                             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    except OSError:
        raise OfficialAuthError("SSO_SESSION_CONFIGURATION", "无法访问共享 SSO 的私有会话锁。") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OfficialAuthError("SSO_SESSION_CONFIGURATION", "共享 SSO 锁必须是当前用户的普通私有文件。")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OfficialAuthError("SSO_AUTH_REQUEST_PENDING", "共享 SSO 操作正在进行，未发起新的授权。") from None
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def _failure_code(stdout):
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    return data.get("code") if isinstance(data, dict) else None


def exchange(config, audiences, *, environment, login, timeout, run, node_command, login_timeout=None):
    """Run one bridge action for the source under its private lock.

    ``timeout`` bounds the action once the lock is held. ``login_timeout``
    makes a token request interactive: SSO_SESSION_REQUIRED escalates once,
    still under the lock, to the SDK login action, so concurrent MCP processes
    wait for that one approval instead of requesting their own. Returns
    ``(status, stdout, stderr, source, login_attempted)``.
    """
    from . import OfficialAuthError, _session_call
    from .session import configuration
    stable = ("sdk", "session_file", "mis")

    def revalidated():
        current = _session_call(configuration, mis=config["mis"], environment=environment)
        # A predecessor may legitimately rotate the same user's source while
        # we waited. Revalidate it, do not reuse an old snapshot or new identity.
        if any(current[field] != config[field] for field in stable):
            raise OfficialAuthError("SSO_SESSION_CHANGED", "共享 SSO 来源配置已变更，未调用新的来源。")
        return current

    def bridge(action, current, budget):
        deadline = time.monotonic() + budget
        request = {"action": action, "audiences": list(audiences),
                   "environment": environment, "sdk": current["sdk"], "mis": current["mis"],
                   "session_file": current["session_file"], "fingerprint": current["fingerprint"]}
        command = node_command(str(Path(__file__).with_name("shared_session.mjs")))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OfficialAuthError("SSO_TIMEOUT", "共享 SSO Node 解析超过操作时限，未发起新的授权。")
        return run(command, timeout=remaining, input_data=json.dumps(request))

    # Another process may hold the lock for a whole interactive approval.
    with _source_lock(config["session_file"], timeout + (login_timeout or 0)):
        current = revalidated()
        status, stdout, stderr = bridge("login" if login else "token", current, timeout)
        login_attempted = login
        if login_timeout and not login and status != 0 and _failure_code(stdout) == "SSO_SESSION_REQUIRED":
            current = revalidated()
            status, stdout, stderr = bridge("login", current, login_timeout)
            login_attempted = True
        refreshed = _session_call(configuration, mis=config["mis"], environment=environment)
        if any(refreshed[field] != current[field] for field in stable):
            raise OfficialAuthError("SSO_SESSION_CHANGED", "共享 SSO 来源配置在换票期间变更，已丢弃旧结果。")
        if status == 0:
            try:
                data = json.loads(stdout)
            except (ValueError, RecursionError):
                data = {}
            # Missing provenance is not a usable shared-provider success.
            if (not isinstance(data, dict) or data.get("session_fingerprint") != refreshed["fingerprint"] or
                    data.get("identity_verified") is not True or data.get("subject") != config["mis"]):
                raise OfficialAuthError("SSO_SESSION_CHANGED", "共享 SSO 返回的会话或身份已变更，请重新执行原业务操作。")
        return status, stdout, stderr, refreshed, login_attempted
