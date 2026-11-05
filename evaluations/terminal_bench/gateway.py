"""CodeLab sandboxGateway client for the native Terminal-Bench runner.

Responsibility: start and stop one sandbox VM session through pssdk and use
the VM ingress native exec channel (``/native/exec|status|read|kill``, added on
the sandbox ingress branch named by ``INGRESS_BRANCH``). Returns plain dicts;
the secret token comes from the caller and never reaches logs or files.

Entry points: ``SandboxSession``, ``start_body``, ``remote_error_text``.
Dependencies: pssdk (``mt-paas-sandbox-python-sdk``), imported lazily.
"""
from __future__ import annotations

import base64
import logging
import uuid

# Ingress branch carrying the native exec channel (nlp/harbor b35af6f).
INGRESS_BRANCH = "ruanjunhao/tofu-native-exec-20261102"
SESSION_ABSENT = "Session不存在或者已关闭"
SESSION_DONE = "当前状态为[DONE]，无法关闭"
# Canvas consumer / sandbox-direct-access defaults.
DEFAULT_MAX_IDLE_MIN = 20
START_TIMEOUT_SEC = 660
STOP_TIMEOUT_SEC = 60
CALL_TIMEOUT_SEC = 120
# Raw bytes per /native/read call; base64 inflates it by a third in the JSON reply.
READ_CHUNK_BYTES = 4 * 1024 * 1024


def _quiet_pssdk() -> None:
    # pssdk logs complete request bodies (task bundles, file payloads) on errors.
    for name in ("pssdk", "pssdk.base_sandbox_cluster_tool", "urllib3"):
        logging.getLogger(name).disabled = True


def remote_error_text(error: BaseException) -> str:
    """The gateway/ingress JSON ``error``/``detail`` field when present, else the SDK message."""
    response = getattr(error.__cause__, "response", None)
    try:
        body = response.json() if response is not None else None
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("error", "detail"):
            if isinstance(body.get(key), str):
                return body[key]
    return str(error)


def start_body(*, image: str, branch: str, proxy: str, cpus: int, memory: str, max_alive_min: int,
               max_idle_min: int = DEFAULT_MAX_IDLE_MIN) -> dict:
    """The container_config/runtime_config shape the canvas consumer sends (registry-pull mode)."""
    env = {"GIT_REPO_BRANCH": branch, "HARBOR_INACTIVITY_TIMEOUT": str(max_idle_min * 60),
           "HARBOR_DISABLE_RESOURCE_LIMITS": "true",
           "TB_CONTAINER_CPU_LIMIT": str(cpus), "TB_CONTAINER_MEMORY_LIMIT": memory}
    if proxy:
        env.update(http_proxy=proxy, https_proxy=proxy, no_proxy=".internal.example.com,internal.example.com")
    return {"container_config": {"image": image, "env": env, "cpus": cpus, "memory": memory},
            "runtime_config": {"max_idle_time": max_idle_min, "max_alive_time": max_alive_min}}


class SandboxSession:
    def __init__(self, endpoint: str, token: str, session_id: str | None = None):
        _quiet_pssdk()
        from pssdk import BaseSandboxClusterTool

        self.session_id = session_id or str(uuid.uuid4())
        self._api = BaseSandboxClusterTool(endpoint, token, session_id=self.session_id,
                                           global_call_timeout=CALL_TIMEOUT_SEC)

    def start(self, body: dict) -> dict:
        return self._api.sandbox_start(body=body, call_timeout=START_TIMEOUT_SEC)

    def stop(self) -> str:
        """Release the session; the gateway's absent/DONE replies also mean released."""
        try:
            self._api.sandbox_stop(call_timeout=STOP_TIMEOUT_SEC)
            return "stopped"
        except Exception as error:
            message = remote_error_text(error)
            if SESSION_ABSENT in message:
                return "absent"
            if SESSION_DONE in message:
                return "done"
            raise

    def _post(self, path: str, payload: dict, timeout: int = CALL_TIMEOUT_SEC) -> dict:
        return self._api._gateway_post(path, payload, call_timeout=timeout)

    def exec(self, argv: list[str], files: dict[str, bytes] | None = None, env: dict | None = None) -> dict:
        payload = {"argv": argv, "env": env or {},
                   "files": {path: base64.b64encode(data).decode() for path, data in (files or {}).items()}}
        return self._post("/native/exec", payload, timeout=START_TIMEOUT_SEC)

    def status(self, exec_id: str, tail_bytes: int = 4096) -> dict:
        return self._post("/native/status", {"exec_id": exec_id, "tail_bytes": tail_bytes})

    def kill(self, exec_id: str, force: bool = False) -> dict:
        return self._post("/native/kill", {"exec_id": exec_id, "force": force})

    def read_file(self, path: str) -> bytes:
        chunks, offset = [], 0
        while True:
            reply = self._post("/native/read", {"path": path, "offset": offset, "length": READ_CHUNK_BYTES})
            data = base64.b64decode(reply["data_b64"])
            chunks.append(data)
            offset += len(data)
            if reply.get("eof") or not data:
                break
        return b"".join(chunks)
