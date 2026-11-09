"""Official MTSSO CLI adapter; credentials stay outside model-visible results.

Entry points: enabled, user_token, login, session_status, verified_subject,
provider_status, invalidate, clear, OfficialAuthError. Upstream SDK/official CLI
own grants. This module owns bounded subprocess I/O, safe error projection and
process-session caching, keyed by owner/env/audience/source. README.md owns the
MCP authentication contract (states, self-healing business calls, envelope).
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
# One interactive bridge run: the SSO CIBA approval window (server
# expires_in, SDK default 120 s) plus SDK refresh, introspection and exchange.
_LOGIN_TIMEOUT = 180
_NODE_PROBE_BUDGET = 5
# engines.node of @mtfe/mtsso-auth-official (">=20"). An older Node still
# answers --version, but cannot load the ESM bridge: exit 1 without output.
_OFFICIAL_NODE_MAJOR = 20
_CACHE = OrderedDict()
_FAILURES = OrderedDict()
_SUBJECTS = OrderedDict()
# Local source validation is also used while the single-flight condition owns
# the cache lock; reentrancy avoids deadlocking that bounded revalidation.
_LOCK = threading.RLock()
_CHANGED = threading.Condition(_LOCK)
_FLIGHTS = {}
_SEQUENCE = 0
_GENERATION = 0
_AUTO_OFFICIAL = False
_AUTO_SHARED = False
_AUTO_SHARED_SOURCE = None
_PROBE_TIMEOUT = 5
_HUMAN_CODES = {"ric_feedback_required", "sub_access_denied", "act_access_denied",
                "sub_act_access_denied", "MOA_NOT_LOGGED_IN", "MOA_USER_REJECTED",
                "MOA_REJECT_COOLDOWN", "MOA_AUTH_REQUEST_PENDING", "SSO_SESSION_REQUIRED",
                "SSO_LOGIN_NOT_APPROVED"}
# auth_state vocabulary shared by status results and error envelopes.
_APPROVAL_CODES = {"SSO_SESSION_REQUIRED", "SSO_LOGIN_NOT_APPROVED"}
_PENDING_CODES = {"SSO_AUTH_REQUEST_PENDING", "MOA_AUTH_REQUEST_PENDING", "authorization_pending"}
_CONFIGURATION_CODES = {
    "SSO_AGENT_CONFIGURATION_MISSING", "SSO_AGENT_CONFIGURATION_INVALID",
    "SSO_TOOL_UNAVAILABLE", "SSO_NODE_UNAVAILABLE", "SSO_CONFIGURATION",
    "SSO_ROUTE_UNAVAILABLE", "SSO_CAPABILITY_UNKNOWN", "invalid_client",
    "SSO_NODE_RUNTIME_INCOMPATIBLE", "SSO_GATEWAY_REQUIRED",
    "SSO_SESSION_CONFIGURATION", "SSO_SESSION_SDK_UNAVAILABLE",
    "SSO_IDENTITY_REQUIRED", "SSO_IDENTITY_MISMATCH",
}


class OfficialAuthError(RuntimeError):
    """Provider failure for this call; never trigger a second login path.

    ``auth_state`` is ``login_required`` or ``pending`` when the user only has
    to approve (or finish approving) a 大象 login, otherwise ``blocked``.
    """
    def __init__(self, code, message, *, user_action=False, auth_source="mtsso_official",
                 login_attempted=False):
        self.code = code
        self.message = message
        self.user_action = user_action
        self.auth_source = auth_source
        self.login_attempted = login_attempted
        self.approval = None
        super().__init__(message)

    @property
    def auth_state(self):
        return ("login_required" if self.code in _APPROVAL_CODES else
                "pending" if self.code in _PENDING_CODES else "blocked")

    def envelope(self):
        awaiting_user = self.auth_state != "blocked"
        result = {"ok": False, "error_code": self.code, "error": self.message,
                  "auth_source": self.auth_source, "auth_state": self.auth_state,
                  "login_attempted": self.login_attempted,
                  "retryable": awaiting_user,
                  "user_action_required": self.user_action or awaiting_user,
                  "model_action": "ask_user_to_approve_then_retry" if awaiting_user else "stop_and_report",
                  "recovery_action": ("approve_daxiang_login" if awaiting_user else
                                      "follow_official_instruction" if self.user_action else
                                      "repair_agent_configuration" if self.code in _CONFIGURATION_CODES else
                                      "check_sso_service")}
        if awaiting_user and self.approval:
            result["approval"] = self.approval
        return result


def _session_call(function, *args, **kwargs):
    from .session import SessionConfigurationError
    try:
        return function(*args, **kwargs)
    except SessionConfigurationError as error:
        raise OfficialAuthError(error.code, str(error), auth_source="mtsso_shared_session") from None


def _provider():
    global _AUTO_SHARED, _AUTO_SHARED_SOURCE
    mode = os.environ.get("MTSSO_AUTH_MODE", "auto")
    if mode not in {"auto", "official", "shared", "legacy"}:
        raise OfficialAuthError("SSO_CONFIGURATION", "MTSSO_AUTH_MODE must be auto, official, shared or legacy.")
    if mode == "legacy":
        return "mcp_existing"
    if mode == "official":
        return "mtsso_official"
    if mode == "shared":
        return "mtsso_shared_session"
    if _select_official():
        return "mtsso_official"
    from .session import configured, sdk_path, token_path
    with _LOCK:
        if _AUTO_SHARED:
            return "mtsso_shared_session"
        generation = _GENERATION
    selected = _session_call(configured)
    selected_source = (_session_call(sdk_path), _session_call(token_path)) if selected else None
    with _LOCK:
        if generation != _GENERATION:
            raise OfficialAuthError("SSO_SESSION_CHANGED", "SSO 会话已变更，请重新执行原业务操作。")
        if selected and _AUTO_SHARED_SOURCE is None:
            _AUTO_SHARED_SOURCE = selected_source
        _AUTO_SHARED = _AUTO_SHARED or selected
        return "mtsso_shared_session" if _AUTO_SHARED else "mcp_existing"


def discover_login_mis(explicit=None):
    """The account to use when the operator config does not name one.

    Carried internal exports strip per-person MIS variables, so MCP servers
    resolve the destination's own shared-session account instead of the
    exporter's. Returns '' when no shared session exists yet; callers then
    report that an explicit login (or MIS) is required.
    """
    from .session import discover_mis
    try:
        return _session_call(discover_mis, explicit)
    except OfficialAuthError as error:
        if error.code == 'SSO_IDENTITY_REQUIRED':
            return ''
        raise


def _shared_configuration(*, mis=None, environment="PROD"):
    from .session import configuration
    result = _session_call(configuration, mis=mis, environment=environment)
    with _LOCK:
        pinned = _AUTO_SHARED_SOURCE if os.environ.get("MTSSO_AUTH_MODE", "auto") == "auto" else None
    if pinned and (result["sdk"], result["session_file"]) != pinned:
        raise OfficialAuthError("SSO_SESSION_CHANGED", "已选定的共享 SSO 来源消失或变更；请显式清理后重新配置，不切换另一会话。", auth_source="mtsso_shared_session")
    return result


def enabled():
    return _provider() != "mcp_existing"


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
        source = _provider()
        selected = source == "mtsso_official"
        result = {"ok": True, "auth_mode": os.environ.get("MTSSO_AUTH_MODE", "auto"),
                  "auth_source": source, "shared_selected": source == "mtsso_shared_session",
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
    global _GENERATION, _AUTO_OFFICIAL, _AUTO_SHARED, _AUTO_SHARED_SOURCE
    with _LOCK:
        _GENERATION += 1
        if reset_provider:
            _AUTO_OFFICIAL = False
            _AUTO_SHARED = False
            _AUTO_SHARED_SOURCE = None
        _CACHE.clear()
        _FAILURES.clear()
        _SUBJECTS.clear()


def clear():
    _clear_session(reset_provider=True)


def _scope(owner, audiences, *, environment=None, source=None, configuration=None):
    key = (owner, environment or os.environ.get("MTSSO_ENV", "PROD"),
           "user_ticket", tuple(sorted(set(audiences))))
    if source == "mtsso_shared_session":
        from .session import scope
        key += scope(configuration)
    return key


def cache_status(owner, audiences, *, environment=None):
    source = _provider()
    config = None
    if source == "mtsso_shared_session":
        from .session import configuration
        config = _shared_configuration(environment=environment or os.environ.get("MTSSO_ENV", "PROD"))
    key = _scope(owner, audiences, environment=environment, source=source, configuration=config)
    with _LOCK:
        entry = _CACHE.get(key)
        return {"logged_in": bool(entry and entry[1] > time.monotonic()),
                "auth_source": source, "subject": None, "identity_verified": False}


def _approval(config):
    """Where a shared-session approval comes from; not identity proof."""
    path = config["session_file"]
    home = os.path.expanduser("~").rstrip(os.sep)
    if home and path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    return {"channel": "大象", "login_target": config["mis"], "session_file": path,
            "requester": ("授权卡片由该共享会话所属 DataFE CLI（MLP/LLM CLI）的 SSO 登录应用发出，"
                          "而非当前 MCP；批准一次即供共享此会话的所有 MCP 使用。")}


def session_status(owner, audiences, *, mis=None, environment=None):
    """Whether a business call for ``audiences`` can proceed without a new approval.

    The shared provider answers by running the business credential path
    non-interactively (SDK refresh, official introspection, exchange); it never
    requests approval, and a ready answer caches the ticket for the business call.
    """
    if not isinstance(owner, str) or not owner:
        raise OfficialAuthError("SSO_OWNER_REQUIRED", "An explicit SSO owner is required.")
    try:
        source = _provider()
        if source == "mtsso_official":
            # The official CLI may need MOA interaction, so it is not probed;
            # only a ticket it already issued in this process proves readiness.
            cached = cache_status(owner, audiences, environment=environment)
            return {"ok": True, **cached, "auth_state": "ready" if cached["logged_in"] else "unknown"}
        if source != "mtsso_shared_session":
            return {"ok": True, "logged_in": False, "subject": None, "identity_verified": False,
                    "auth_source": source, "auth_state": "unknown"}
        approval = _approval(_shared_configuration(
            mis=mis, environment=environment or os.environ.get("MTSSO_ENV", "PROD")))
        try:
            acquired = _acquire(owner, audiences, environment=environment, mis=mis, interactive=False)
        except OfficialAuthError as error:
            if error.auth_state == "blocked":
                raise
            return {"ok": True, "logged_in": False, "subject": None, "identity_verified": False,
                    "auth_source": source, "auth_state": error.auth_state, "detail": error.message,
                    "approval": approval,
                    "next_step": ("直接调用原业务工具：它会向 login_target 的大象发送一次授权，等待批准后继续。"
                                  if error.auth_state == "login_required" else
                                  "同一账号已有授权在进行；请用户在大象批准，原业务工具会等待并复用该授权。")}
        return {"ok": True, "logged_in": True, "subject": acquired["subject"],
                "identity_verified": acquired["subject"] is not None, "auth_source": source,
                "auth_state": "ready", "approval": approval}
    except OfficialAuthError as error:
        return {**error.envelope(), "logged_in": False, "identity_verified": False}


def verified_subject(owner, *, environment=None):
    """Return only an unexpired, introspected identity matching the current source."""
    if _provider() != "mtsso_shared_session":
        return None
    from .session import configuration, scope
    environment = environment or os.environ.get("MTSSO_ENV", "PROD")
    config = _shared_configuration(environment=environment)
    with _LOCK:
        now = time.monotonic()
        for key, (subject, until) in reversed(_SUBJECTS.items()):
            if key[0] == owner and key[1] == environment and key[4:] == scope(config) and until > now:
                return subject
    return None


def login(owner, audiences, *, mis=None, environment=None, timeout_sec=None):
    """Prepare the business credential without a business request.

    Same path as an interactive business call: a usable session is reused
    (``login_performed=False``); otherwise one 大象 approval is requested and
    awaited for up to ``timeout_sec`` (default: the whole SSO approval window).
    """
    window = _LOGIN_TIMEOUT if timeout_sec is None else min(_LOGIN_TIMEOUT, max(10, timeout_sec))
    acquired = _acquire(owner, audiences, environment=environment, mis=mis,
                        interactive=True, login_timeout=window)
    source = _provider()
    result = {"ok": True, "approved": True, "auth_state": "ready", "auth_source": source,
              "subject": acquired["subject"], "identity_verified": acquired["subject"] is not None,
              "login_performed": acquired["login_performed"]}
    if source == "mtsso_shared_session":
        result["approval"] = _approval(_shared_configuration(
            mis=mis, environment=environment or os.environ.get("MTSSO_ENV", "PROD")))
    return result


def invalidate():
    # Endpoint ticket rejection invalidates credentials, never provider authority.
    _clear_session(reset_provider=False)


atexit.register(clear)


def _run(argv, *, timeout=None, input_data=None):
    """Read bounded pipes concurrently; timeout/output overflow kills and reaps."""
    encoded = None
    if input_data is not None:
        if not isinstance(input_data, str):
            raise OfficialAuthError("SSO_INPUT_INVALID", "SSO private request must be UTF-8 text.")
        encoded = input_data.encode("utf-8")
        if len(encoded) > _MAX_OUTPUT:
            raise OfficialAuthError("SSO_INPUT_LIMIT", "SSO private request exceeded its input budget.")
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
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
    writer = None
    if input_data is not None:
        def write():
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
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
        if writer:
            writer.join(timeout=1)
    if failure:
        raise OfficialAuthError(failure, "Official SSO tool exceeded its output or time budget; inspect Agent SSO configuration.")
    return (process.returncode, buffers[0].decode("utf-8", "replace"),
            buffers[1].decode("utf-8", "replace"))


def _node_command(executable):
    """Resolve a compatible Node for the official Node shebang, within five seconds.

    Explicit MTSSO_NODE_EXECUTABLE is authoritative. Automatic selection only
    probes installed PATH candidates; it never installs or changes credentials.
    The first candidate in PATH order that runs and meets the official engine
    floor wins. Each probe may use the whole remaining budget, so a slow cold
    start under load never hands the choice to a later, older Node.
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
            status, stdout, stderr = _run([candidate, "--version"], timeout=remaining)
        except OfficialAuthError:
            continue
        version = re.match(r"v(\d+)\.", stdout.strip()) if status == 0 else None
        if version and int(version.group(1)) >= _OFFICIAL_NODE_MAJOR:
            return [candidate, executable]
        incompatible = incompatible or bool(version) or ("GLIBC_" in stderr and "not found" in stderr)
    code = "SSO_NODE_RUNTIME_INCOMPATIBLE" if incompatible else "SSO_NODE_UNAVAILABLE"
    raise OfficialAuthError(code,
        "官方 SSO 命令的 Node 运行环境不可用（需要 Node " + str(_OFFICIAL_NODE_MAJOR) + "+）。"
        "请安装兼容主机的 Node，通过 MTSSO_NODE_EXECUTABLE 指定其路径，或将其加入 MCP 的 PATH；无需重复登录。")


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


def user_token(owner, audiences, *, environment=None, force_refresh=False, mis=None,
               interactive=True):
    """Business ticket for ``audiences``; the business call's only auth step.

    ``interactive`` (default) makes the call self-healing: when the shared
    session is missing, cannot be renewed or was revoked, one 大象 approval is
    requested and awaited, then the call continues. Concurrent calls in this
    process and in other MCP processes share that approval. Pass False only
    for status probes, which must never request approval.
    """
    return _acquire(owner, audiences, environment=environment, force_refresh=force_refresh,
                    mis=mis, interactive=interactive)["token"]


def _acquire(owner, audiences, *, environment=None, force_refresh=False, mis=None,
             interactive=True, login_timeout=_LOGIN_TIMEOUT):
    global _SEQUENCE
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
    # Direct adapter calls historically mean official; provider selection is used
    # by MCP seams. Preserve that API while adding explicitly selected sharing.
    source = _provider()
    if os.environ.get("MTSSO_AUTH_MODE") == "legacy":
        raise OfficialAuthError("SSO_NOT_SELECTED", "Explicit legacy mode uses the MCP's existing authentication provider.")
    shared = source == "mtsso_shared_session"
    config = None
    if shared:
        from .session import configuration
        config = _shared_configuration(mis=mis, environment=environment)
    key = _scope(owner, audience, environment=environment, source=source, configuration=config)
    # Only the shared bridge can request an approval; the official CLI owns its own.
    escalate = login_timeout if interactive and shared else None
    # Single flight per cache scope. A slow exchange never blocks unrelated
    # cached audiences/owners; simultaneous misses share one official request.
    # A flight may wait for the source lock and then run token + login actions.
    deadline = time.monotonic() + 2 * (_TIMEOUT + (escalate or 0)) + _NODE_PROBE_BUDGET + 2
    flight_key = key[:-1] if shared else key
    joined = None
    with _CHANGED:
        while flight_key in _FLIGHTS:
            joined = _FLIGHTS[flight_key]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfficialAuthError("SSO_AUTH_REQUEST_PENDING",
                    "同一账号的 SSO 换票或授权仍在进行；本次调用未发起新的授权。",
                    auth_source=source)
            _CHANGED.wait(remaining)
        if shared:
            config = _shared_configuration(mis=mis, environment=environment)
            key = _scope(owner, audience, environment=environment, source=source, configuration=config)
        now = time.monotonic()
        for expired in [k for k, (_, until) in _CACHE.items() if until <= now]:
            del _CACHE[expired]
        for expired in [k for k, entry in _FAILURES.items() if entry[1] <= now]:
            del _FAILURES[expired]
        failure = _FAILURES.get(flight_key)
        if failure:
            fields, _, from_interactive, sequence = failure
            # A status probe never requested approval, so it cannot veto a call
            # that may. An unanswered approval is shared only with the calls that
            # waited on it; a later call is a deliberate retry with a new request.
            if ((interactive and not from_interactive) or
                    (fields[0] == "SSO_LOGIN_NOT_APPROVED" and sequence != joined)):
                _FAILURES.pop(flight_key, None)
                failure = None
        if failure:
            code, message, user_action, attempted = failure[0]
            error = OfficialAuthError(code, message, user_action=user_action,
                                      auth_source=source if shared else "mtsso_official",
                                      login_attempted=attempted)
            if shared:
                error.approval = _approval(config)
            raise error
        cached = _CACHE.get(key)
        if cached and not force_refresh:
            _CACHE.move_to_end(key)
            subject = _SUBJECTS.get(key)
            return {"token": cached[0], "subject": subject[0] if subject else None,
                    "login_performed": False}
        if len(_FLIGHTS) >= _MAX_ENTRIES:
            raise OfficialAuthError("SSO_BUSY", "Official SSO exchange capacity is occupied; retry after pending authorization settles.")
        _CACHE.pop(key, None)
        _SUBJECTS.pop(key, None)
        _SEQUENCE += 1
        sequence = _SEQUENCE
        _FLIGHTS[flight_key] = sequence
        generation = _GENERATION
    login_attempted = False
    try:
        subject = None
        login_performed = False
        store_key = key
        if shared:
            from .shared import exchange
            status, output, stderr, current, login_attempted = exchange(
                config, audience, environment=environment, login=False, timeout=_TIMEOUT,
                login_timeout=escalate, run=_run, node_command=_node_command)
            token, ttl = _parse(status, output, stderr)
            data = json.loads(output)
            if data.get("identity_verified") is not True or data.get("subject") != config["mis"]:
                raise OfficialAuthError("SSO_IDENTITY_MISMATCH", "共享 SSO 未验证当前 MIS 身份；不能继续业务调用。")
            subject = data["subject"]
            login_performed = data.get("login_performed") is True
            store_key = _scope(owner, audience, environment=environment,
                               source=source, configuration=current)
        else:
            executable = _executable()
            if not executable:
                raise OfficialAuthError("SSO_TOOL_UNAVAILABLE", "Install @mtfe/mtsso-auth-official@1.0.11 and configure the Agent SSO environment.")
            status, output, stderr = _run([*_node_command(executable), "--audience", " ".join(audience), "--env", environment,
                                             "--auto_update", "false"])
            token, ttl = _parse(status, output, stderr)
        if shared and _provider() != source:
            raise OfficialAuthError("SSO_SESSION_CHANGED", "共享 SSO 来源在换票期间变更，已丢弃旧结果。")
        with _LOCK:
            if generation != _GENERATION:
                raise OfficialAuthError("SSO_SESSION_CHANGED", "The MCP SSO cache was cleared during exchange; discard the old session result.")
            if ttl > 0:
                until = time.monotonic() + ttl
                _CACHE[store_key] = (token, until)
                if subject:
                    _SUBJECTS[store_key] = (subject, until)
                    while len(_SUBJECTS) > _MAX_ENTRIES:
                        _SUBJECTS.popitem(last=False)
                while len(_CACHE) > _MAX_ENTRIES:
                    evicted, _ = _CACHE.popitem(last=False)
                    _SUBJECTS.pop(evicted, None)
        return {"token": token, "subject": subject, "login_performed": login_performed}
    except OfficialAuthError as error:
        if shared:
            error.auth_source = source
            error.login_attempted = error.login_attempted or login_attempted
            error.approval = _approval(config)
        with _LOCK:
            if generation == _GENERATION:
                if shared and (error.login_attempted or error.code in {
                        "SSO_SESSION_REQUIRED", "SSO_IDENTITY_MISMATCH", "SSO_SESSION_CHANGED"}):
                    # A refused source identity invalidates every audience using
                    # it, not just the ticket that happened to be revalidated.
                    revoked = [candidate for candidate in _CACHE if candidate[0] == owner and
                               candidate[1] == environment and candidate[4:-1] == key[4:-1]]
                    for candidate in revoked:
                        _CACHE.pop(candidate, None)
                        _SUBJECTS.pop(candidate, None)
                # Refreshing the same user's source cannot bypass a terminal denial.
                _FAILURES[flight_key] = ((error.code, error.message, error.user_action, error.login_attempted),
                                         time.monotonic() + 30, interactive, sequence)
                while len(_FAILURES) > _MAX_ENTRIES:
                    _FAILURES.popitem(last=False)
        raise
    finally:
        with _CHANGED:
            _FLIGHTS.pop(flight_key, None)
            _CHANGED.notify_all()
