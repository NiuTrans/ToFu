"""Local shared-session discovery and private source fencing; no OAuth grants.

Only credential presence, MIS metadata and a content digest escape this module.
The SDK owns existing session files; this adapter never copies their tickets.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

_LIMIT = 65536
_MIS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MIS_KEYS = ("MTSSO_MIS", "MLP_MIS", "LLM_MIS", "HOPE_USERNAME", "XUECHENG_MIS")


class SessionConfigurationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def sdk_path():
    """Find an already installed upstream auth module without npm/network work."""
    explicit = os.environ.get("MTSSO_SESSION_SDK")
    if explicit:
        if not os.path.isabs(explicit) or not os.path.isfile(explicit):
            raise SessionConfigurationError("SSO_SESSION_SDK_UNAVAILABLE", "配置的共享 SSO SDK 不存在；请修复已安装的 DataFE CLI。")
        return os.path.realpath(explicit)
    candidates = []
    for cli in ("llm", "mlp"):
        executable = shutil.which(cli)
        if executable:
            target = Path(os.path.realpath(executable))
            for parent in tuple(target.parents)[:4]:
                candidates.append(parent / "lib/auth.js")
                candidates.append(parent / "dist/lib/auth.js")
                candidates.append(parent / "lib/node_modules/@datafe/llm-cli/dist/lib/auth.js")
    home = Path(os.path.expanduser("~"))
    candidates.extend(home / prefix / "lib/node_modules/@datafe/llm-cli/dist/lib/auth.js"
                      for prefix in (".local", ".npm-global"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def token_path():
    explicit = os.environ.get("MTSSO_SESSION_TOKEN_FILE")
    if explicit:
        if not os.path.isabs(explicit):
            raise SessionConfigurationError("SSO_SESSION_CONFIGURATION", "共享 SSO 会话文件必须是绝对路径。")
        return explicit
    home = Path(os.path.expanduser("~"))
    primary = home / ".mlp-cli-token.json"
    return str(primary if primary.exists() or primary.is_symlink() else
               home / ".llm-cli-token.json" if (home / ".llm-cli-token.json").exists() else primary)


def local_metadata(path):
    """Bounded private-file admission, with no tickets in the returned projection."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {"present": False, "mis": None, "usable": False, "fingerprint": "missing"}
    except OSError:
        raise SessionConfigurationError("SSO_SESSION_CONFIGURATION", "共享 SSO 会话不可读取或为符号链接；请修复私有会话文件。") from None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size > _LIMIT):
            raise SessionConfigurationError("SSO_SESSION_CONFIGURATION", "共享 SSO 会话必须是当前用户的普通私有文件（0600），且不超过 64 KiB。")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError("oversize")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("object required")
        fingerprint = hashlib.sha256(raw).hexdigest()
        mis = data.get("mis")
        if mis is not None and (not isinstance(mis, str) or not _MIS.fullmatch(mis)):
            raise ValueError("invalid mis")
        access, refresh = data.get("access_token"), data.get("refresh_token")
        usable = any(isinstance(value, str) and bool(value.strip()) for value in (access, refresh))
        return {"present": True, "mis": mis, "usable": usable, "fingerprint": fingerprint}
    except (ValueError, UnicodeError, RecursionError):
        raise SessionConfigurationError("SSO_SESSION_CONFIGURATION", "共享 SSO 会话格式无效；请通过上游 CLI 修复会话。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def expected_mis(explicit=None, *, discovered=None):
    values = {value.strip() for key in _MIS_KEYS if (value := os.environ.get(key, "").strip())}
    if explicit:
        values.add(explicit)
    if len(values) > 1:
        raise SessionConfigurationError("SSO_IDENTITY_MISMATCH", "MCP 配置的 MIS 目标不一致；请统一同一用户的认证配置。")
    mis = next(iter(values), discovered)
    if not isinstance(mis, str) or not _MIS.fullmatch(mis):
        raise SessionConfigurationError("SSO_IDENTITY_REQUIRED", "共享 SSO 需要明确的 MIS 目标；请配置 MTSSO_MIS 或通过显式登录工具指定账号。")
    return mis


def discover_mis(explicit=None):
    """Login target when the caller does not pin an account.

    Order: an explicitly configured MIS, then the MIS of the existing shared
    DataFE login session. A carried internal export deliberately ships NO
    per-person MIS, so the destination's own session supplies it instead of
    the exporter's. Returns '' when no session exists yet — the caller then
    reports that an explicit login/MIS is required and never guesses one.
    """
    metadata = local_metadata(token_path())
    discovered = metadata.get("mis") if metadata.get("usable") else None
    return expected_mis(explicit, discovered=discovered)


def configured():
    """Explicit selection pins even incomplete config; automatic selection is local."""
    if (os.environ.get("MTSSO_SHARED_SESSION") == "1" or
            os.environ.get("MTSSO_SESSION_SDK") or os.environ.get("MTSSO_SESSION_TOKEN_FILE")):
        return True
    if not sdk_path():
        return False
    metadata = local_metadata(token_path())
    if not metadata["usable"] or not metadata["mis"]:
        return False
    mis = expected_mis(discovered=metadata["mis"])
    if metadata["mis"] != mis:
        raise SessionConfigurationError("SSO_IDENTITY_MISMATCH", "已有共享 SSO 会话属于另一用户；不能自动切换身份。")
    return True


def configuration(*, mis=None, environment="PROD"):
    if environment != "PROD":
        raise SessionConfigurationError("SSO_ENV_INVALID", "共享 DataFE 登录会话仅支持 PROD；TEST 必须使用配套的官方 Agent 身份。")
    path = token_path()
    metadata = local_metadata(path)
    target = expected_mis(mis, discovered=metadata["mis"])
    if metadata["mis"] and metadata["mis"] != target:
        raise SessionConfigurationError("SSO_IDENTITY_MISMATCH", "已有共享 SSO 会话属于另一用户；请先由用户在上游完成账号切换。")
    sdk = sdk_path()
    if not sdk:
        raise SessionConfigurationError("SSO_SESSION_SDK_UNAVAILABLE", "共享 SSO SDK 未安装；请由部署管理员安装 DataFE CLI。")
    return {**metadata, "sdk": sdk, "session_file": path, "mis": target}


def scope(configuration):
    return ("mtsso_shared_session", configuration["mis"], configuration["sdk"],
            configuration["session_file"], configuration["fingerprint"])
