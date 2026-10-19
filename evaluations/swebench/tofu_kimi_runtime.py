"""Formal production-Tofu × Kimi candidate runtime contract.

Only public, reproducible launch metadata is serialized. The Harbor host
agent resolves the two explicitly named credential environment variables at
execution time; their values never enter the job configuration or guest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


TOFU_KIMI_AGENT = (
    "rootless_vm.harbor_tofu_runtime_agent:TofuKimiRuntimeAgent"
)
TOFU_KIMI_PROFILE_ID = "tofu-kimi"
TOFU_KIMI_RUNTIME_SCHEMA = "tofu-production-kimi-runtime/v1"

# Supported candidate tool surfaces. ``exclusive`` is the historical arm (two
# adapter-made custom__ tools, no production registry surface). ``production``
# serves the real production tool face: production-verbatim file/search
# schemas dispatched by the built-in registry against the guest rootfs, with
# shell execution carried by a client-executed custom__ tool routed to guest
# exec (same-name shadowing of the built-in run_command is impossible — see
# docs in tofu_kimi_production_tool_catalog).
TOFU_KIMI_TOOL_SURFACES = ("exclusive", "production")

# Benchmark arms may run only these explicitly registered models.  Each entry
# carries the public, non-secret routing facts the Harbor adapter needs to
# build the same creator/model/provider graph as production.
TOFU_BENCHMARK_MODELS: dict[str, dict[str, Any]] = {
    "kimi-k3": {
        "creator_id": "moonshot",
        "model_id": "kimi-k3",
        "creator_name": "Moonshot AI",
        "display_name": "Kimi K3",
        "context_window": 262_144,
        "wire_model_id": "kimi-k3",
    },
    "glm-5.3-m17": {
        "creator_id": "zhipu",
        "model_id": "glm-5.3",
        "creator_name": "Z.ai",
        "display_name": "GLM-5.3",
        "context_window": 200_000,
        "wire_model_id": "glm-5.3-m17",
    },
}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUNTIME_RESERVED_KEYS = frozenset({
    "model", "thinking", "thinkingDepth", "thinkingEnabled",
    "disableModelFallback", "_customToolSchemas", "_customToolsMode",
    "_explicitToolSchemas",
})

TOFU_KIMI_HARBOR_SYSTEM_PROMPT = """You are solving an isolated benchmark task. Use
custom__run_command to inspect and modify the task environment and to run focused
checks. Work autonomously until the requested behavior is complete. Use
custom__submit_result only after a meaningful validation command passes. Do not
request credentials or try to access host/private services. The external benchmark
verifier, not a non-empty answer, decides success.

The guest runs under CPU emulation, so installs, builds, and test suites run many
times slower than native. Plan for that: prefer quiet non-interactive commands
with little output, give each command a realistic timeout_sec, and split long work
into resumable steps instead of one monolithic command. Command results report
your remaining agent time budget; keep enough reserve to validate and submit.

Write the required output artifact as early as possible, right after the core
computation succeeds. A task that produces the deliverable in the first half of
the run survives any later slowdown; a task that saves everything for the end
scores zero if time runs out first.

For any long-running step (training, compilation, package installation), launch it
in the background with nohup or `cmd &`, then poll its output with short commands
instead of blocking on `sleep`. A single `sleep 780` wastes the entire step; ten
short `sleep 5 && tail log` checks let you react the moment it finishes.

When the task mentions a verifier, test, or expected output file, read that exact
validation logic first and match it precisely: same file path, same format, same
tolerance, same comparison method. A result that is correct by your own measure
but differs from the verifier's expectation scores zero.

When Python stdout is piped or captured (not a TTY), always use `flush=True` in
print() or call sys.stdout.flush() before the program exits, otherwise the last
output is lost to the pipe buffer.

For Debian/Ubuntu package installation, check whether a .deb file already exists
in the image (find / -name '*.deb' 2>/dev/null) before running apt-get update +
apt-get install; installing a local deb with `dpkg -i` is many times faster under
emulation than fetching from a network mirror."""

_TOFU_KIMI_CUSTOM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "custom__run_command",
            "description": "Run one POSIX shell command in the isolated task guest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {
                        "type": "integer", "minimum": 1, "maximum": 1800,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        "execution": {"mode": "client", "timeout_s": 1830, "shell_command_argument": "command"},
        "write": True,
    },
    {
        "type": "function",
        "function": {
            "name": "custom__submit_result",
            "description": (
                "Validate the current artifact and record a completion claim. "
                "The model must still return its final answer afterward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "validation_command": {"type": "string"},
                    "timeout_sec": {
                        "type": "integer", "minimum": 1, "maximum": 1800,
                    },
                },
                "required": ["summary", "validation_command"],
                "additionalProperties": False,
            },
        },
        "execution": {"mode": "client", "timeout_s": 1830},
        "write": True,
    },
]


def tofu_kimi_custom_tools() -> list[dict[str, Any]]:
    """Return a detached copy of the formal guest-facing tool contracts."""
    return json.loads(json.dumps(_TOFU_KIMI_CUSTOM_TOOLS))


# ── Production tool surface (tool_surface='production') ─────────────────────
#
# Mechanism conclusion (verified against lib/tools/tool_env.py and
# tofu_agent/runtime.py): a client-executed custom tool can NEVER shadow a
# built-in tool by name. ``mint_tool_env`` enforces
# ``custom__<ident>`` naming (``_NAME_RE``) AND rejects any collision with a
# registered built-in name (``_validate_one``), so the production
# ``run_command`` cannot be overridden through the custom-tool channel, in
# either ``augment`` or ``exclusive`` mode. The remote-worktree bridge
# (``cfg['project_remote']``) is the only declared same-name reroute and is
# all-or-nothing per conversation — it would also pull the file tools off the
# host-visible guest rootfs, which the verifier contract forbids.
#
# The production surface therefore composes as:
#   * production-verbatim registry schemas for every capability that is safe
#     to execute host-side against the guest rootfs mount (file tools,
#     read/search/todo) — dispatched by the REAL built-in registry;
#   * the three host-executing shell tools (run_command +
#     check/kill_background_command) WITHHELD from the model-visible catalog
#     (they would execute on the trusted Harbor host, outside the guest
#     trust boundary);
#   * shell capability carried by ``custom__run_command`` — the only
#     client-executed channel the runtime permits — routed to guest exec,
#     with a schema shaped on the production run_command parameters.
_HOST_EXEC_TOOL_NAMES = frozenset({
    "run_command",
    "check_background_command",
    "kill_background_command",
})

_TOFU_KIMI_PRODUCTION_CUSTOM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "custom__run_command",
            "description": (
                "Run one POSIX shell command inside the isolated task guest — "
                "the same sandbox the benchmark verifier inspects. This is "
                "the guest-executed counterpart of the production run_command "
                "tool, with a matching parameter shape (command, description, "
                "timeout_s, working_dir). The guest workspace is /app: the "
                "same files the project file tools reach through the "
                "host-mounted guest rootfs. Use guest-absolute (/app/...) or "
                "relative paths inside commands; host-absolute paths exist "
                "only for the file tools. Background execution and vault "
                "credentials are unavailable in the benchmark guest."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "timeout_s": {
                        "type": "integer", "minimum": 1, "maximum": 1800,
                    },
                    "working_dir": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        "execution": {"mode": "client", "timeout_s": 1830, "shell_command_argument": "command"},
        "write": True,
    },
    _TOFU_KIMI_CUSTOM_TOOLS[1],
]


def tofu_kimi_production_custom_tools() -> list[dict[str, Any]]:
    """Detached copy of the production-surface client-executed tool pair."""
    return json.loads(json.dumps(_TOFU_KIMI_PRODUCTION_CUSTOM_TOOLS))


def _clean_schemas(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": tool["function"]}
        for tool in tools
    ]


def tofu_kimi_search_enabled(runtime_config: Mapping[str, Any]) -> bool:
    """Whether the arm's config enables host-side web search (prod default off)."""
    if not isinstance(runtime_config, Mapping):
        return False
    if runtime_config.get("search") is True:
        return True
    mode = str(runtime_config.get("searchMode") or "").strip().lower()
    return mode in ("single", "multi")


def tofu_kimi_production_tool_catalog(
    *, search_enabled: bool = False,
) -> list[dict[str, Any]]:
    """Model-visible catalog for ``tool_surface='production'``.

    Production schemas are imported verbatim from their owning production
    modules (``lib.tools.*``) — never re-declared here — and deep-copied so
    the request-local catalog cannot mutate shared module state. The three
    host-executing shell tools are withheld (see module note above). Global
    host-side tools follow the production headless defaults: read_files and
    inspect_image are always on, fetch_url is on by default, todo_write
    attaches with any base tool, and web_search only when the arm enables
    search. Environment-dependent families (skills, MCP, swarm, memory,
    scheduler, browser/desktop/image generation) stay excluded for trial
    determinism, matching the storage-free agent.run assembly.
    """
    import copy

    from lib.tools.image_edit import INSPECT_IMAGE_TOOL
    from lib.tools.project import READ_FILES_TOOL, project_tools_for_runtime
    from lib.tools.search import build_fetch_url_tool, build_search_tool
    from lib.tools.todo import TODO_WRITE_TOOL

    catalog: list[dict[str, Any]] = [
        copy.deepcopy(READ_FILES_TOOL),
        copy.deepcopy(INSPECT_IMAGE_TOOL),
    ]
    for tool in project_tools_for_runtime():
        name = str((tool.get("function") or {}).get("name") or "")
        if not name or name in _HOST_EXEC_TOOL_NAMES:
            continue
        catalog.append(copy.deepcopy(tool))
    catalog.append(build_fetch_url_tool())
    if search_enabled:
        catalog.append(build_search_tool())
    catalog.append(copy.deepcopy(TODO_WRITE_TOOL))
    catalog.extend(_clean_schemas(tofu_kimi_production_custom_tools()))
    return catalog


def tofu_kimi_production_tool_schema_sha256(
    *, search_enabled: bool = False,
) -> str:
    """Digest the exact production-surface catalog the model may see."""
    encoded = json.dumps(
        tofu_kimi_production_tool_catalog(search_enabled=search_enabled),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tofu_kimi_production_tool_execution_sha256() -> str:
    """Freeze the production-surface client-executed tool contracts."""
    return hashlib.sha256(json.dumps(
        tofu_kimi_production_custom_tools(), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode('utf-8')).hexdigest()


def tofu_kimi_clean_tool_schemas() -> list[dict[str, Any]]:
    """Return exactly the schemas the production model is allowed to see."""
    return [
        {"type": "function", "function": tool["function"]}
        for tool in tofu_kimi_custom_tools()
    ]


def tofu_kimi_tool_schema_sha256() -> str:
    encoded = json.dumps(
        tofu_kimi_clean_tool_schemas(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def tofu_kimi_tool_execution_sha256() -> str:
    """Freeze server-only execution budgets as well as model-visible schemas."""
    return hashlib.sha256(json.dumps(
        tofu_kimi_custom_tools(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode('utf-8')).hexdigest()


def tofu_kimi_prompt_contract_sha256(
    runtime_config: Mapping[str, Any],
    *,
    tool_surface: str = "exclusive",
    tool_catalog_sha256: str = "",
) -> str:
    """Digest the adapter prompt plus the requested production profile.

    The runtime additionally emits each round's resolved static-prompt digest.
    This manifest digest freezes the request-side choice without pretending a
    profile-independent hash is the full model-visible prompt.

    ``exclusive`` (default) keeps the v1 payload byte-identical. ``production``
    has no adapter-made system prompt at all — the model receives the
    production system_prompt_base composition — so the v2 payload pins the tool
    surface and the exact model-visible tool catalog instead.
    """
    responses = runtime_config.get("responses")
    requested_profile = (
        responses.get("promptProfile", "auto")
        if isinstance(responses, Mapping) else "auto"
    )
    if str(tool_surface) == "production":
        payload = {
            "contractVersion": "tofu-kimi-prompt-contract/v2",
            "toolSurface": "production",
            "requestedPromptProfile": str(requested_profile or "auto"),
            "systemPromptMode": str(
                runtime_config.get("systemPromptMode") or "append"),
            "toolCatalogSha256": str(tool_catalog_sha256 or ""),
        }
    else:
        payload = {
            "contractVersion": "tofu-kimi-prompt-contract/v1",
            "harborSystemPromptSha256": hashlib.sha256(
                TOFU_KIMI_HARBOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "requestedPromptProfile": str(requested_profile or "auto"),
            "systemPromptMode": str(
                runtime_config.get("systemPromptMode") or "append"),
        }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_config(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise ValueError("Tofu Kimi runtime_config must be an object")

    def inspect(candidate: Any, path: str = "runtime_config") -> None:
        if isinstance(candidate, Mapping):
            for key, child in candidate.items():
                name = str(key)
                separated = re.sub(
                    r"([a-z0-9])([A-Z])", r"\1_\2", name)
                words = [
                    word for word in re.split(r"[^A-Za-z0-9]+", separated.lower())
                    if word
                ]
                credential_key = (
                    any(word in {"token", "secret", "password", "passwd"}
                        for word in words)
                    or any(words[index:index + 2] == ["api", "key"]
                           for index in range(max(0, len(words) - 1)))
                )
                if credential_key:
                    raise ValueError(
                        f"Tofu Kimi {path}.{name} looks credential-bearing"
                    )
                inspect(child, f"{path}.{name}")
        elif isinstance(candidate, (list, tuple)):
            for index, child in enumerate(candidate):
                inspect(child, f"{path}[{index}]")

    inspect(value)
    reserved = sorted(_RUNTIME_RESERVED_KEYS & set(value))
    if reserved:
        raise ValueError(
            "Tofu Kimi runtime_config contains runner-owned keys: "
            + ", ".join(reserved)
        )
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        detached = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Tofu Kimi runtime_config must be finite JSON"
        ) from exc
    if not isinstance(detached, dict):
        raise ValueError("Tofu Kimi runtime_config must be an object")
    if len(encoded) > 256 * 1024:
        raise ValueError("Tofu Kimi runtime_config exceeds 256 KiB")
    return detached, hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TofuKimiCandidateSettings:
    """Secret-free inputs for one frozen production candidate arm."""

    provider_face: str
    provider_slot_id: str
    agent_version: str
    experiment_arm: str
    runtime_config: Mapping[str, Any]
    agent_timeout_seconds: int = 3600
    model: str = "kimi-k3"
    command_timeout_seconds: int = 480
    upstream_base_url_env: str = "KIMI_CHAT_BASE_URL"
    upstream_api_key_env: str = "KIMI_API_KEY"
    thinking_format: str = ""
    tool_surface: str = "exclusive"
    wire_proxy: bool = False
    wire_proxy_timeout_seconds: float = 1900.0

    def __post_init__(self) -> None:
        detached, _ = _canonical_config(self.runtime_config)
        object.__setattr__(self, "runtime_config", detached)

    @property
    def credential_environment_names(self) -> tuple[str, str]:
        return (self.upstream_base_url_env, self.upstream_api_key_env)

    @property
    def runtime_config_sha256(self) -> str:
        return _canonical_config(self.runtime_config)[1]

    def validate(self) -> None:
        if str(self.model) not in TOFU_BENCHMARK_MODELS:
            raise ValueError(
                "Tofu benchmark model must be one of "
                + ", ".join(sorted(TOFU_BENCHMARK_MODELS))
            )
        for value, label in (
            (self.provider_face, "provider face"),
            (self.provider_slot_id, "provider slot ID"),
            (self.agent_version, "agent version"),
            (self.experiment_arm, "experiment arm"),
        ):
            if not _PUBLIC_ID.fullmatch(str(value)):
                raise ValueError(
                    f"Tofu Kimi {label} must be a non-secret public identifier"
                )
        for name in self.credential_environment_names:
            if not _ENV_NAME.fullmatch(str(name)):
                raise ValueError(
                    f"invalid Tofu Kimi host environment name: {name!r}"
                )
        if self.upstream_base_url_env == self.upstream_api_key_env:
            raise ValueError(
                "Kimi upstream URL and API key must use different variables"
            )
        if isinstance(self.agent_timeout_seconds, bool) or not (
                1 <= int(self.agent_timeout_seconds) <= 86_400):
            raise ValueError(
                "Tofu Kimi agent timeout must be between 1 and 86400 seconds"
            )
        if isinstance(self.command_timeout_seconds, bool) or not (
                1 <= int(self.command_timeout_seconds) <= 1_800):
            raise ValueError(
                "Tofu Kimi command timeout must be between 1 and 1800 seconds"
            )
        if len(str(self.thinking_format)) > 128:
            raise ValueError("Tofu Kimi thinking_format exceeds 128 characters")
        if str(self.tool_surface) not in TOFU_KIMI_TOOL_SURFACES:
            raise ValueError(
                "Tofu Kimi tool_surface must be one of "
                + ", ".join(TOFU_KIMI_TOOL_SURFACES)
            )
        if not 60.0 <= float(self.wire_proxy_timeout_seconds) <= 21_600.0:
            raise ValueError(
                "Tofu Kimi wire proxy timeout must be between 60 and 21600"
            )
        _canonical_config(self.runtime_config)

    def credentials_from_environment(self) -> tuple[str, str]:
        upstream = str(os.environ.get(self.upstream_base_url_env) or "").strip()
        api_key = str(os.environ.get(self.upstream_api_key_env) or "")
        missing = [
            name
            for name, value in (
                (self.upstream_base_url_env, upstream),
                (self.upstream_api_key_env, api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Tofu Kimi host-only environment variables are missing: "
                + ", ".join(missing)
            )
        return upstream, api_key

    def agent_kwargs(self) -> dict[str, object]:
        self.validate()
        return {
            "upstream_base_url_env": self.upstream_base_url_env,
            "upstream_api_key_env": self.upstream_api_key_env,
            "provider_face": self.provider_face,
            "provider_slot_id": self.provider_slot_id,
            "experiment_arm": self.experiment_arm,
            "runtime_config": dict(self.runtime_config),
            "runtime_config_sha256": self.runtime_config_sha256,
            "model": str(self.model),
            "thinking_format": self.thinking_format,
            "timeout_sec": int(self.agent_timeout_seconds),
            "command_timeout_sec": int(self.command_timeout_seconds),
            "tool_surface": str(self.tool_surface),
            "wire_proxy": bool(self.wire_proxy),
            "wire_proxy_timeout_sec": float(self.wire_proxy_timeout_seconds),
        }

    def manifest_record(self) -> dict[str, Any]:
        self.validate()
        from lib.tasks_pkg.model_config import resolve_max_output_tokens
        production = str(self.tool_surface) == "production"
        search_enabled = tofu_kimi_search_enabled(self.runtime_config)
        tool_schema_digest = (
            tofu_kimi_production_tool_schema_sha256(
                search_enabled=search_enabled)
            if production else tofu_kimi_tool_schema_sha256()
        )
        tool_execution_digest = (
            tofu_kimi_production_tool_execution_sha256()
            if production else tofu_kimi_tool_execution_sha256()
        )
        return {
            "schema": TOFU_KIMI_RUNTIME_SCHEMA,
            "agentImport": TOFU_KIMI_AGENT,
            "agentVersion": self.agent_version,
            "providerFace": self.provider_face,
            "providerSlotId": self.provider_slot_id,
            "experimentArm": self.experiment_arm,
            "model": str(self.model),
            "toolSurface": str(self.tool_surface),
            "runtimeConfig": dict(self.runtime_config),
            "runtimeConfigSha256": self.runtime_config_sha256,
            "requestedMaxOutputTokens": resolve_max_output_tokens(self.runtime_config),
            "outputBudgetSource": "explicit" if "maxTokens" in self.runtime_config else "production_default",
            "toolSchemaSha256": tool_schema_digest,
            "toolExecutionSha256": tool_execution_digest,
            "contextWindowTokens": int(TOFU_BENCHMARK_MODELS[str(self.model)]['context_window']),
            "promptContractSha256": tofu_kimi_prompt_contract_sha256(
                self.runtime_config,
                tool_surface=str(self.tool_surface),
                tool_catalog_sha256=(
                    tool_schema_digest if production else ""),),
            "harborPromptSha256": hashlib.sha256(
                TOFU_KIMI_HARBOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "upstreamBaseUrlEnv": self.upstream_base_url_env,
            "upstreamApiKeyEnv": self.upstream_api_key_env,
            "thinkingFormat": self.thinking_format,
            "agentTimeoutSeconds": int(self.agent_timeout_seconds),
            "commandTimeoutSeconds": int(self.command_timeout_seconds),
            "wireProxy": bool(self.wire_proxy),
            "wireProxyTimeoutSeconds": float(self.wire_proxy_timeout_seconds),
            "credentialBoundary": "harbor-host-only",
            "guestCredentialValues": False,
        }

    def _expected_tool_digests(self) -> tuple[str, str]:
        """(toolSchemaSha256, toolExecutionSha256) for the active surface."""
        if str(self.tool_surface) == "production":
            return (
                tofu_kimi_production_tool_schema_sha256(
                    search_enabled=tofu_kimi_search_enabled(
                        self.runtime_config)),
                tofu_kimi_production_tool_execution_sha256(),
            )
        return (
            tofu_kimi_tool_schema_sha256(),
            tofu_kimi_tool_execution_sha256(),
        )

    @classmethod
    def from_manifest_record(
        cls, value: dict[str, Any]
    ) -> "TofuKimiCandidateSettings":
        if not isinstance(value, dict) \
                or value.get("schema") != TOFU_KIMI_RUNTIME_SCHEMA:
            raise ValueError("Tofu Kimi runtime manifest has an unsupported schema")
        if value.get("agentImport") != TOFU_KIMI_AGENT \
                or str(value.get("model") or "") not in TOFU_BENCHMARK_MODELS \
                or value.get("credentialBoundary") != "harbor-host-only" \
                or value.get("guestCredentialValues") is not False:
            raise ValueError("Tofu Kimi runtime manifest violates its trust boundary")
        settings = cls(
            provider_face=str(value.get("providerFace") or ""),
            provider_slot_id=str(value.get("providerSlotId") or ""),
            agent_version=str(value.get("agentVersion") or ""),
            experiment_arm=str(value.get("experimentArm") or ""),
            model=str(value.get("model") or ""),
            runtime_config=(value.get("runtimeConfig")
                            if isinstance(value.get("runtimeConfig"), dict)
                            else {}),
            agent_timeout_seconds=int(value.get("agentTimeoutSeconds") or 0),
            command_timeout_seconds=int(
                value.get("commandTimeoutSeconds") or 0),
            upstream_base_url_env=str(value.get("upstreamBaseUrlEnv") or ""),
            upstream_api_key_env=str(value.get("upstreamApiKeyEnv") or ""),
            thinking_format=str(value.get("thinkingFormat") or ""),
            # Manifests written before the tool-surface split carry no key;
            # they are the historical exclusive arm.
            tool_surface=str(value.get("toolSurface") or "exclusive"),
            wire_proxy=bool(value.get("wireProxy")),
            wire_proxy_timeout_seconds=float(
                value.get("wireProxyTimeoutSeconds") or 1900.0),
        )
        settings.validate()
        if value.get("runtimeConfigSha256") != settings.runtime_config_sha256:
            raise ValueError("Tofu Kimi runtime config digest drifted")
        expected_schema, expected_execution = settings._expected_tool_digests()
        if value.get("toolSchemaSha256") != expected_schema:
            raise ValueError("Tofu Kimi formal tool schema digest drifted")
        if value.get('toolExecutionSha256') != expected_execution:
            raise ValueError('Tofu tool execution budget digest missing or drifted; prepare a new run')
        if value.get('contextWindowTokens') != TOFU_BENCHMARK_MODELS[str(settings.model)]['context_window']:
            raise ValueError('Tofu model context budget missing or drifted; prepare a new run')
        expected_schema_digest, _ = settings._expected_tool_digests()
        if value.get("promptContractSha256") != \
                tofu_kimi_prompt_contract_sha256(
                    settings.runtime_config,
                    tool_surface=str(settings.tool_surface),
                    tool_catalog_sha256=(
                        expected_schema_digest
                        if str(settings.tool_surface) == "production"
                        else "")):
            raise ValueError("Tofu Kimi formal prompt contract digest drifted")
        expected_prompt = hashlib.sha256(
            TOFU_KIMI_HARBOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
        if value.get("harborPromptSha256") != expected_prompt:
            raise ValueError("Tofu Kimi Harbor prompt digest drifted")
        return settings


__all__ = [
    "TOFU_BENCHMARK_MODELS",
    "TOFU_KIMI_AGENT",
    "TOFU_KIMI_PROFILE_ID",
    "TOFU_KIMI_RUNTIME_SCHEMA",
    "TOFU_KIMI_HARBOR_SYSTEM_PROMPT",
    "TOFU_KIMI_TOOL_SURFACES",
    "TofuKimiCandidateSettings",
    "tofu_kimi_clean_tool_schemas",
    "tofu_kimi_custom_tools",
    "tofu_kimi_prompt_contract_sha256",
    "tofu_kimi_production_custom_tools",
    "tofu_kimi_production_tool_catalog",
    "tofu_kimi_production_tool_execution_sha256",
    "tofu_kimi_production_tool_schema_sha256",
    "tofu_kimi_search_enabled",
    "tofu_kimi_tool_schema_sha256",
]
