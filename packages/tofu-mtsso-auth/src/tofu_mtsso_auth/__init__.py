"""Official MTSSO CLI adapter; credentials stay outside model-visible results.

Entry points: enabled, user_token, provider_status, invalidate, clear, OfficialAuthError.
Only the official CLI performs SSO. This module owns bounded subprocess I/O,
error projection and process-session caching, keyed by owner/env/audience.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import OrderedDict

_MAX_OUTPUT = 65536
_MAX_ENTRIES = 64
_TIMEOUT = 30
_NODE_PROBE_BUDGET = 5
_CACHE = OrderedDict()
_FAILURES = OrderedDict()
_LOCK = threading.Lock()
_CHANGED = threading.Condition(_LOCK)
_FLIGHTS = set()
_GENERATION = 0
_AUTO_OFFICIAL = False
_PROBE_TIMEOUT = 5
_HUMAN_CODES = {"ric_feedback_required", "sub_access_denied", "act_access_denied",
                "sub_act_access_denied", "MOA_NOT_LOGGED_IN", "MOA_USER_REJECTED",
                "MOA_REJECT_COOLDOWN", "MOA_AUTH_REQUEST_PENDING"}


class OfficialAuthError(RuntimeError):
    """Terminal official-provider failure; never trigger a second login path."""
    def __init__(self, code, message, *, user_action=False):
        self.code = code
        self.message = message
        self.user_action = user_action
        super().__init__(message)

    def envelope(self):
        return {"ok": False, "error_code": self.code, "error": self.message,
                "auth_source": "mtsso_official", "login_required": False,
                "retryable": False, "user_action_required": self.user_action,
                "model_action": "stop_and_report",
                "recovery_action": ("follow_official_instruction" if self.user_action else
                                    "repair_agent_configuration" if self.code in {
                                        "SSO_AGENT_CONFIGURATION_MISSING", "SSO_AGENT_CONFIGURATION_INVALID",
                                        "SSO_TOOL_UNAVAILABLE", "SSO_NODE_UNAVAILABLE", "SSO_CONFIGURATION",
                                        "SSO_ROUTE_UNAVAILABLE", "SSO_CAPABILITY_UNKNOWN", "invalid_client",
                                        "SSO_NODE_RUNTIME_INCOMPATIBLE", "SSO_GATEWAY_REQUIRED",
                                    } else "check_sso_service")}


def enabled():
    mode = os.environ.get("MTSSO_AUTH_MODE", "auto")
    if mode not in {"auto", "official", "legacy"}:
        raise OfficialAuthError("SSO_CONFIGURATION", "MTSSO_AUTH_MODE must be auto, official or legacy.")
    if mode == "legacy":
        return False
    if mode == "official":
        return True
    return _select_official()


def _official_configuration_declared():
    from .configuration import official_configuration_declared
    try:
        return official_configuration_declared(os.environ)
    except (OSError, ValueError):
        raise OfficialAuthError("SSO_AGENT_CONFIGURATION_INVALID",
            "官方 Agent SSO 配置无法读取或超过大小限制。请由部署管理员修复配置；无需重复登录。") from None


def _probe_official(executable):
    """Explicit bounded diagnostic; normal status/business selection never runs this."""
    probe = os.path.join(os.path.dirname(executable), "mtsso-moa-feature-probe")
    if not os.path.isfile(probe):
        raise OfficialAuthError("SSO_CAPABILITY_UNKNOWN",
            "无法确认官方 SSO 换票能力：安装缺少能力探测命令。请修复官方 SSO 安装；无需重复登录或重试业务工具。")
    command = _node_command(probe)
    status, output, stderr = _run([*command, "--timeout", "1"], timeout=_PROBE_TIMEOUT)
    try:
        result = json.loads(output)
    except (ValueError, RecursionError):
        result = None
    support = result.get("support_type") if isinstance(result, dict) else None
    if support == "none" and status in {0, 1}:
        return False
    if status == 0 and support == "extend_agent":
        return True
    if status == 0 and support == "moa_wss_msgtype_120":
        # Local MOA still needs the caller's Agent identity. With no official
        # identity declared, installing the CLI must not replace an existing
        # MCP authentication provider.
        return _official_configuration_declared()
    if status == 0 and support == "gateway_intercept_feature":
        raise OfficialAuthError("SSO_GATEWAY_REQUIRED",
            "官方 SSO 使用网关占位符，但当前 MCP HTTP 传输未适配网关换票。请完成网关集成；无需重复登录。")
    raise OfficialAuthError("SSO_CAPABILITY_UNKNOWN",
        "官方 SSO 能力探测未返回有效结果。请检查 Agent SSO 服务；无需重复登录或重试业务工具。")


def _select_official():
    global _AUTO_OFFICIAL
    # Status checks stay local: no CLI spawn, network, login or credential copy.
    with _LOCK:
        if _AUTO_OFFICIAL:
            return True
        generation = _GENERATION
    declared = _official_configuration_declared()
    with _LOCK:
        if generation != _GENERATION:
            raise OfficialAuthError("SSO_SESSION_CHANGED", "SSO 会话已变更，请重新执行原业务操作。")
        _AUTO_OFFICIAL = _AUTO_OFFICIAL or declared
        return _AUTO_OFFICIAL


def provider_status(*, probe=False):
    """Secret-free local diagnosis; online capability probing is explicit opt-in."""
    try:
        selected = enabled()
        result = {"ok": True, "auth_mode": os.environ.get("MTSSO_AUTH_MODE", "auto"),
                  "auth_source": "mtsso_official" if selected else "mcp_existing",
                  "official_selected": selected, "official_installed": bool(_executable()),
                  "identity_verified": False,
                  "model_action": "continue_original_tool"}
        if probe:
            executable = _executable()
            if not executable:
                raise OfficialAuthError("SSO_TOOL_UNAVAILABLE", "官方 SSO 命令未安装，请由部署管理员修复安装；无需重复登录。")
            available = _probe_official(executable)
            if selected and not available:
                raise OfficialAuthError("SSO_ROUTE_UNAVAILABLE",
                    "官方 SSO 未发现可用换票路径。请由部署管理员完成 Agent SSO 适配；无需重复登录或重试业务工具。")
            result["official_route_available"] = available
        return result
    except OfficialAuthError as error:
        return error.envelope()


def _executable():
    configured = os.environ.get("MTSSO_OFFICIAL_EXECUTABLE")
    return shutil.which(configured or "mtsso-moa-local-exchange")


def _clear_session(*, reset_provider):
    global _GENERATION, _AUTO_OFFICIAL
    with _LOCK:
        _GENERATION += 1
        if reset_provider:
            _AUTO_OFFICIAL = False
        _CACHE.clear()
        _FAILURES.clear()


def clear():
    _clear_session(reset_provider=True)


def cache_status(owner, audiences, *, environment=None):
    key = (owner, environment or os.environ.get("MTSSO_ENV", "PROD"),
           "user_ticket", tuple(sorted(set(audiences))))
    with _LOCK:
        entry = _CACHE.get(key)
        return {"logged_in": bool(entry and entry[1] > time.monotonic()),
                "auth_source": "mtsso_official", "subject": None, "identity_verified": False}


def invalidate():
    # Endpoint ticket rejection invalidates credentials, never provider authority.
    _clear_session(reset_provider=False)


atexit.register(clear)


def _run(argv, *, timeout=None):
    """Read bounded pipes concurrently; timeout/output overflow kills and reaps."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError:
        raise OfficialAuthError("SSO_TOOL_UNAVAILABLE", "Install @mtfe/mtsso-auth-official@1.0.11 and configure the Agent SSO environment.") from None
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def read(pipe, target):
        try:
            while True:
                part = pipe.read(4096)
                if not part:
                    break
                if len(target) + len(part) > _MAX_OUTPUT:
                    overflow.set()
                    break
                target.extend(part)
        finally:
            pipe.close()

    readers = [threading.Thread(target=read, args=(pipe, target), daemon=True)
               for pipe, target in zip((process.stdout, process.stderr), buffers)]
    for thread in readers:
        thread.start()
    deadline = time.monotonic() + (_TIMEOUT if timeout is None else timeout)
    failure = None
    try:
        while process.poll() is None or any(thread.is_alive() for thread in readers):
            if overflow.is_set() or time.monotonic() >= deadline:
                failure = "SSO_OUTPUT_LIMIT" if overflow.is_set() else "SSO_TIMEOUT"
                break
            # Bounded waits keep cancellation/timeout responsive.
            overflow.wait(0.02)
    finally:
        if failure or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        for thread in readers:
            thread.join(timeout=1)
    if failure:
        raise OfficialAuthError(failure, "Official SSO tool exceeded its output or time budget; inspect Agent SSO configuration.")
    return (process.returncode, buffers[0].decode("utf-8", "replace"),
            buffers[1].decode("utf-8", "replace"))


def _node_command(executable):
    """Resolve a runnable Node for the official Node shebang, within five seconds.

    Explicit MTSSO_NODE_EXECUTABLE is authoritative. Automatic selection only
    probes installed PATH candidates; it never installs or changes credentials.
    Non-Node executable overrides retain their original launch contract.
    """
    try:
        with open(executable, "rb") as script:
            shebang = script.readline(256).strip()
    except OSError:
        return [executable]
    if shebang != b"#!/usr/bin/env node":
        return [executable]
    override = os.environ.get("MTSSO_NODE_EXECUTABLE")
    candidates = ([shutil.which(override)] if override else
                  [shutil.which("node", path=directory)
                   for directory in os.environ.get("PATH", os.defpath).split(os.pathsep)])
    deadline = time.monotonic() + _NODE_PROBE_BUDGET
    seen = set()
    incompatible = False
    for candidate in candidates:
        if not candidate:
            continue
        identity = os.path.realpath(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            status, stdout, stderr = _run([candidate, "--version"], timeout=min(2, remaining))
        except OfficialAuthError:
            continue
        if status == 0 and stdout.strip().startswith("v"):
            return [candidate, executable]
        incompatible = incompatible or ("GLIBC_" in stderr and "not found" in stderr)
    code = "SSO_NODE_RUNTIME_INCOMPATIBLE" if incompatible else "SSO_NODE_UNAVAILABLE"
    raise OfficialAuthError(code,
        "官方 SSO 命令的 Node 运行环境不可用。请安装兼容主机的 Node，"
        "通过 MTSSO_NODE_EXECUTABLE 指定其路径，或将其加入 MCP 的 PATH；无需重复登录。")


def _parse(exit_code, stdout, stderr=""):
    """V13 section 1.6: exit status is checked before token extraction."""
    if exit_code != 0 and "GLIBC_" in stderr and "not found" in stderr:
        raise OfficialAuthError("SSO_NODE_RUNTIME_INCOMPATIBLE",
            "官方 SSO 命令无法启动：Node 依赖主机缺少的 GLIBC。"
            "请通过 MTSSO_NODE_EXECUTABLE 指定兼容的 Node；无需重复登录。")
    text = stdout.strip()
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    # Some official CLI versions emit structured public errors on stderr.
    # Ignore arbitrary stderr prose, paths and credentials.
    original = obj
    if exit_code != 0 and not (obj.get("error") or obj.get("code") or obj.get("success") is False):
        try:
            diagnostic = json.loads(stderr.strip())
        except (ValueError, RecursionError):
            diagnostic = None
        if isinstance(diagnostic, dict) and (diagnostic.get("error") or diagnostic.get("code") or diagnostic.get("success") is False):
            obj = diagnostic
    error = obj.get("error")
    detail = error if isinstance(error, dict) else obj
    code = detail.get("code") or (error if isinstance(error, str) else None) or obj.get("code")
    has_error_code = bool(code) or bool(error)
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code):
        code = None
    message = detail.get("message") or obj.get("error_description") or obj.get("message")
    if isinstance(message, str):
        # Structured diagnostics may include credentials beside the public
        # instruction. Never echo those values through a human-action message.
        pending = [obj, original]
        for _ in range(128):
            if not pending:
                break
            entry = pending.pop()
            entries = entry.items() if isinstance(entry, dict) else enumerate(entry)
            for key, value in entries:
                if isinstance(value, (dict, list)):
                    pending.append(value)
                elif isinstance(value, str) and value and isinstance(key, str):
                    field = key.lower().replace("-", "_")
                    if (field.endswith(("_token", "_secret", "_proof", "_ticket")) or
                            field in {"token", "secret", "ticket", "client_assertion", "encrypted", "encrypted_key"}):
                        message = message.replace(value, "<redacted>")
        if pending:
            message = "官方 SSO 要求人工处理，但诊断信息超过安全解析限制。请由管理员检查官方授权结果。"
        message = re.sub(r"(?i)((?:Bearer|Basic)\s+)\S+", r"\1<redacted>", message)
        message = re.sub(
            r"(?i)(\b(?:[a-z0-9_-]*token|[a-z0-9_-]*secret|[a-z0-9_-]*proof|client_assertion|ticket)\s*[=:]\s*)[^\s,;\"'&<>]+",
            r"\1<redacted>", message)[:1000]
    # Known human-action responses carry an official instruction. All other
    # arbitrary output is withheld: it may contain a ticket or client secret.
    human = exit_code == 42 or code in _HUMAN_CODES
    if exit_code != 0 or has_error_code or obj.get("success") is False:
        if exit_code != 0 and not has_error_code and not human:
            for field in ("client_id", "client_secret"):
                if re.search(r"(?:错误:\s*)?缺少\s+" + field + r"(?:\s|（|\(|$)", stderr):
                    raise OfficialAuthError("SSO_AGENT_CONFIGURATION_MISSING",
                        "官方 SSO 缺少 Agent 身份配置（" + field +
                        "）。MCP 已自动检查官方环境变量和 agent_info；请由部署管理员完成 Agent SSO 注册。"
                        "业务工具无需重复登录或重试。")
            if re.search(r"Agent\s*配置文件(?:不是合法|缺少必要字段)", stderr):
                raise OfficialAuthError("SSO_AGENT_CONFIGURATION_INVALID",
                    "官方 Agent SSO 配置格式无效或缺少必要字段。请由部署管理员修复注册配置；无需重复登录。")
        public_failure = {
            "MOA_WSS_CONNECT_FAILED": "官方 SSO 无法连接 MOA 服务，官方命令已完成内部重试。请检查 Agent MOA 服务；无需重复登录或重试业务工具。",
            "invalid_client": "官方 SSO 拒绝 Agent 身份凭据。请检查 Agent 注册配置及 PROD/TEST 环境匹配；无需重复登录。",
        }.get(code)
        raise OfficialAuthError(str(code or "SSO_EXCHANGE_FAILED"),
                                str(message) if human and isinstance(message, str) else
                                public_failure or ("用户身份票据获取失败（官方命令退出码 " + str(exit_code) +
                                 "）：请检查 Agent SSO 登录态适配或 MOA 授权；联系 SSO 团队排障。"),
                                user_action=human)
    if text.startswith("AT_FOR_GW_BASE64_"):
        # This is a gateway instruction, not a bearer token. MCP stdio -> HTTP
        # has no verified gateway substitution contract, so never forward it.
        raise OfficialAuthError("SSO_GATEWAY_REQUIRED", "Official SSO returned a gateway placeholder; this MCP HTTP transport requires gateway integration.")
    token = obj.get("access_token")
    if not isinstance(token, str) or not token.strip() or len(token) > _MAX_OUTPUT:
        raise OfficialAuthError("SSO_TOKEN_MISSING", "Official SSO returned no usable user ticket; inspect Agent SSO configuration.")
    expires = obj.get("expires_in", 0)
    valid_expiry = (isinstance(expires, (int, float)) and not isinstance(expires, bool) and
                    (not isinstance(expires, float) or math.isfinite(expires)))
    ttl = min(7200, max(0, expires - 60)) if valid_expiry else 0
    return token, ttl


def user_token(owner, audiences, *, environment=None, force_refresh=False):
    if not isinstance(owner, str) or not owner:
        raise OfficialAuthError("SSO_OWNER_REQUIRED", "An explicit owner is required for SSO ticket caching.")
    if not isinstance(audiences, (list, tuple)) or any(not isinstance(a, str) or not a or any(c.isspace() for c in a) for a in audiences):
        raise OfficialAuthError("SSO_AUDIENCE_INVALID", "Declare one to five nonempty SSO audiences.")
    audience = tuple(sorted(set(audiences)))
    if not audience or len(audience) > 5:
        raise OfficialAuthError("SSO_AUDIENCE_INVALID", "Declare one to five nonempty SSO audiences.")
    environment = environment or os.environ.get("MTSSO_ENV", "PROD")
    if environment not in {"PROD", "TEST"}:
        raise OfficialAuthError("SSO_ENV_INVALID", "Official SSO environment must be PROD or TEST.")
    key = (owner, environment, "user_ticket", audience)
    # Single flight per cache scope. A slow exchange never blocks unrelated
    # cached audiences/owners; simultaneous misses share one official request.
    deadline = time.monotonic() + _TIMEOUT + _NODE_PROBE_BUDGET + 2
    with _CHANGED:
        while key in _FLIGHTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfficialAuthError("SSO_TIMEOUT", "An official SSO exchange is still in progress.")
            _CHANGED.wait(remaining)
        now = time.monotonic()
        for expired in [k for k, (_, until) in _CACHE.items() if until <= now]:
            del _CACHE[expired]
        for expired in [k for k, (_, until) in _FAILURES.items() if until <= now]:
            del _FAILURES[expired]
        failure = _FAILURES.get(key)
        if failure:
            raise OfficialAuthError(failure[0][0], failure[0][1], user_action=failure[0][2])
        _FAILURES.pop(key, None)
        cached = _CACHE.get(key)
        if cached and not force_refresh:
            _CACHE.move_to_end(key)
            return cached[0]
        if len(_FLIGHTS) >= _MAX_ENTRIES:
            raise OfficialAuthError("SSO_BUSY", "Official SSO exchange capacity is occupied; retry after pending authorization settles.")
        _CACHE.pop(key, None)
        _FLIGHTS.add(key)
        generation = _GENERATION
    try:
        executable = _executable()
        if not executable:
            raise OfficialAuthError("SSO_TOOL_UNAVAILABLE", "Install @mtfe/mtsso-auth-official@1.0.11 and configure the Agent SSO environment.")
        status, output, stderr = _run([*_node_command(executable), "--audience", " ".join(audience), "--env", environment,
                                         "--auto_update", "false"])
        token, ttl = _parse(status, output, stderr)
        with _LOCK:
            if generation != _GENERATION:
                raise OfficialAuthError("SSO_SESSION_CHANGED", "The MCP SSO cache was cleared during exchange; discard the old session result.")
            if ttl > 0:
                _CACHE[key] = (token, time.monotonic() + ttl)
                while len(_CACHE) > _MAX_ENTRIES:
                    _CACHE.popitem(last=False)
        return token
    except OfficialAuthError as error:
        with _LOCK:
            if generation == _GENERATION:
                _FAILURES[key] = ((error.code, error.message, error.user_action), time.monotonic() + 30)
                while len(_FAILURES) > _MAX_ENTRIES:
                    _FAILURES.popitem(last=False)
        raise
    finally:
        with _CHANGED:
            _FLIGHTS.discard(key)
            _CHANGED.notify_all()
