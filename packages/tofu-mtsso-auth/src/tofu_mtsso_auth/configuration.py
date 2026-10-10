"""Local official SSO declaration discovery, with no grants or credential copying.

Entry point official_configuration_declared receives the exact process mapping.
Discovery mirrors official CLI 1.0.11: gateway/Agent environment, MTSSO_WORKDIR
agent_info override, root/user agent_info, ~/.mtsso/agents/.env extended routing.
Only declaration presence is inspected. The official CLI validates credentials,
routes and permissions; incomplete declared configuration must fail closed.
"""
from __future__ import annotations

import os
import stat

_CONFIG_LIMIT = 65536


def _fallback_declared(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Official SSO fallback must be a regular file")
        content = stream.read(_CONFIG_LIMIT + 1)
    if len(content) > _CONFIG_LIMIT:
        raise ValueError("Official SSO fallback exceeded its size limit")
    for line in content.decode("utf-8", "strict").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == "DEFAULT_MTSSO_AGENT_CONFIG_PATH":
            # Like the official CLI, use the first assignment and strip paired
            # quotes. Values are never evaluated, followed, retained or returned.
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
            return bool(value)
    return False


def official_configuration_declared(environment):
    if any(environment.get(key, "").strip() for key in (
            "MTSSO_OFFICIAL_EXECUTABLE", "MTSSO_AGENT_CONFIG_PATH",
            "AGENT_SSO_CLIENT_ID", "AGENT_SSO_CLIENT_SECRET")):
        return True
    # Official 1.0.11 treats gateway presence as a declaration; the transport
    # later rejects unsupported placeholders instead of changing identity.
    if environment.get("MTSSO_GATEWAY_INTERCEPT_FEATURE", "").strip():
        return True
    home = environment.get("HOME") or environment.get("USERPROFILE") or os.path.expanduser("~")
    workdir = environment.get("MTSSO_WORKDIR", "").strip()
    if workdir and os.path.isdir(workdir):
        paths = [os.path.join(workdir, "agent_info")]
    else:
        paths = ["/root/.openclaw/sso/agent_info", os.path.join(home, ".openclaw", "sso", "agent_info")]
    if any(os.path.isfile(path) for path in paths):
        return True
    return _fallback_declared(os.path.join(home, ".mtsso", "agents", ".env"))
