"""Host-side Tofu agent for Harbor's rootless QEMU environment.

The model dispatcher and its provider credentials stay in the Harbor host
process.  Only terminal commands and their bounded output cross the
guest-agent channel into or out of the disposable VM.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from lib.llm_errors import PromptTooLongError


_TERMINAL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run a POSIX shell command inside the isolated benchmark task container. "
            "Use this to inspect files, edit files with shell tools, and run tests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "timeout_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1800,
                    "description": "Command timeout in seconds.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

_SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_result",
        "description": (
            "Finish the task only after running relevant checks. The harness reruns "
            "validation_command and accepts submission only when it exits zero."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise description of the completed work.",
                },
                "validation_command": {
                    "type": "string",
                    "description": (
                        "A focused, non-interactive end-to-end command that executes "
                        "the produced artifact through its requested consumer or "
                        "interface. File existence, grep, or log-only checks are not "
                        "sufficient."
                    ),
                },
                "timeout_sec": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1800,
                },
            },
            "required": ["summary", "validation_command"],
            "additionalProperties": False,
        },
    },
}

_TOOLS = [_TERMINAL_TOOL, _SUBMIT_TOOL]

_SYSTEM_PROMPT = """You are solving a Terminal-Bench task in an isolated Linux container.
Use run_command to inspect the environment, make the requested changes, and run relevant
checks. Continue autonomously until the task is complete. Network may be absent or limited
to a controlled public HTTP(S) proxy; never request credentials or attempt to reach host or
private services. Put a minimally functional deliverable at every path required by the task
early, then iterate on that deliverable in place; do not leave the only working copy under a
temporary or reference filename. Start required long-running computation as soon as its
prerequisites are known. During iteration prefer cheap representative smoke checks, then run
the required full-scale validation once for the final artifact. You may call submit_result
with a focused validation command, or finish normally once you have completed and checked
the task. Once a task-specific validation passes, submit promptly instead of making
unrelated improvements or repeating the check. Validation must exercise the produced
artifact through its real requested consumer or interface; file-existence, grep, and
log-only checks do not prove runtime compatibility."""

_TRUNCATION_NUDGE = """Your previous response hit its output limit without taking action.
Do not continue the explanation. Invoke run_command now to create or update the requested
artifact, validate it, then call submit_result."""

_EMPTY_ASSISTANT_PLACEHOLDER = "[The model response ended before a tool call.]"
_AUDIT_DISPATCH_FIELDS = frozenset(
    {
        "model",
        "provider_id",
        "protocol",
        "responses_profile",
        "latency_ms",
        "gate_wait_ms",
        "attempt",
        "429_retries",
    }
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?i)(https?://)[^/@\s:'\"]+:[^/@\s'\"]+@"
)
_PROXY_AUTH_RE = re.compile(
    r"(?i)(proxy-authorization\s*:\s*(?:basic\s+)?)([^\s'\"\\]+)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(auth|password|passwd|token|secret|api[_-]?key)="
    r"([A-Za-z0-9_+./:-]{12,})"
)
_ROOTLESS_TOKEN_RE = re.compile(r"(?i)\brootless:[0-9a-f]{32,}\b")


def _recovery_messages(
    instruction: str,
    transcript: list[dict[str, Any]],
    reason: str,
) -> list[dict[str, Any]]:
    """Build a clean context after a trajectory starts repeating itself.

    The task container is stateful, so dropping the polluted chat history does not
    discard work. A small amount of terminal evidence is retained to stop the fresh
    trajectory from blindly repeating the same failed experiment.
    """

    evidence: list[str] = []
    for row in reversed(transcript):
        result = row.get("result")
        if isinstance(result, str) and result.strip():
            evidence.append(result[-1500:])
            if len(evidence) == 3:
                break
    evidence.reverse()
    rendered_evidence = "\n\n---\n\n".join(evidence)
    recovery = (
        "Recovery checkpoint: the previous trajectory became stuck "
        f"({reason}). The container workspace and all files persist, but the old "
        "reasoning has been discarded. Do not repeat the same syntax or command. "
        "Use run_command immediately to inspect the current artifact and error, then "
        "try a fundamentally different implementation. Validate both sides of every "
        "multi-runtime or multi-interface contract before submitting."
    )
    if rendered_evidence:
        recovery += f"\n\nRecent terminal evidence:\n{rendered_evidence}"
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{instruction}\n\n{recovery}"[:12000],
        },
    ]


def _context_checkpoint_messages(
    instruction: str,
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop old chat before overflow while retaining state and recent evidence."""

    evidence: list[str] = []
    for row in reversed(transcript):
        result = row.get("result")
        if isinstance(result, str) and result.strip():
            evidence.append(result[-1500:])
            if len(evidence) == 3:
                break
    evidence.reverse()
    checkpoint = (
        "Context checkpoint: the task container, workspace, processes, and all "
        "files persist, but older chat turns were dropped before reaching the "
        "model context limit. Continue from the current on-disk implementation. "
        "Use run_command immediately to inspect status, diffs, artifacts, and "
        "failing tests; do not restart completed work. Validate the requested "
        "behavior before submitting."
    )
    if evidence:
        checkpoint += "\n\nRecent terminal evidence:\n" + "\n\n---\n\n".join(evidence)
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{instruction}\n\n{checkpoint}"[:12000],
        },
    ]


def _usage_prompt_tokens(usage: dict[str, Any]) -> int:
    """Normalize OpenAI-style and internal prompt-token usage fields."""

    value = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_output(stdout: str | None, stderr: str | None, return_code: int) -> str:
    rendered = f"exit_code={return_code}\n"
    if stdout:
        rendered += f"stdout:\n{stdout}"
    if stderr:
        rendered += f"\nstderr:\n{stderr}"
    limit = 24 * 1024
    if len(rendered) <= limit:
        return rendered
    half = limit // 2
    return rendered[:half] + "\n...[tool output truncated]...\n" + rendered[-half:]


def _bounded_timeout(
    requested: Any,
    default: int,
    multiplier: float = 1.0,
) -> int:
    """Translate a model-requested native timeout to the slower local runtime.

    ``default`` is already runtime-scaled by the runner.  Only an explicit
    model timeout needs the multiplier; applying it to the default as well
    would scale that budget twice.
    """

    if requested is None:
        value = default
    else:
        try:
            value = math.ceil(float(requested) * multiplier)
        except (TypeError, ValueError, OverflowError):
            value = default
    return max(1, min(1800, int(value)))


def _audit_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Persist useful routing evidence without credential-derived metadata."""

    result = {
        key: value
        for key, value in usage.items()
        if key not in {"trace_id", "_dispatch"}
    }
    dispatch = usage.get("_dispatch")
    if isinstance(dispatch, dict):
        result["_dispatch"] = {
            key: dispatch[key]
            for key in _AUDIT_DISPATCH_FIELDS
            if key in dispatch
        }
    return result


def _redact_audit_text(value: str) -> str:
    """Remove credential-shaped values from the persisted audit copy only."""

    value = _CREDENTIAL_URL_RE.sub(r"\1<redacted>@", value)
    value = _PROXY_AUTH_RE.sub(r"\1<redacted>", value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1=<redacted>", value)
    return _ROOTLESS_TOKEN_RE.sub("rootless:<redacted>", value)


def _redact_audit_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_audit_text(value)
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_audit_value(item) for key, item in value.items()}
    return value


def _persist_transcript(logs_dir: Path, transcript: list[dict[str, Any]]) -> None:
    """Atomically checkpoint the redacted audit trail before risky tool waits."""

    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "tofu-host-transcript.json"
    temporary = logs_dir / ".tofu-host-transcript.json.partial"
    temporary.write_text(
        json.dumps(_redact_audit_value(transcript), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextlib.contextmanager
def _dispatch_slot(gate_dir: Path, limit: int):
    """Bound model calls across independent Harbor worker processes.

    Harbor's ``n_concurrent`` limit is local to one job. Terminal-Bench retry
    jobs commonly overlap, so their independent limits can otherwise multiply
    into a 429 storm. Advisory locks provide a host-local semaphore without a
    daemon, root privileges, sockets, or credentials in shared state.
    """

    if not 1 <= limit <= 32:
        raise ValueError("global dispatch concurrency must be between 1 and 32")
    gate_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    gate_stat = gate_dir.stat(follow_symlinks=False)
    if not stat.S_ISDIR(gate_stat.st_mode) or gate_stat.st_uid != os.getuid():
        raise PermissionError(f"unsafe dispatch gate directory: {gate_dir}")
    if gate_stat.st_mode & 0o077:
        raise PermissionError(
            f"dispatch gate directory is group/world accessible: {gate_dir}"
        )

    directory_fd = os.open(
        gate_dir,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors: list[int] = []
    acquired: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        for slot in range(limit):
            descriptor = os.open(
                f"slot-{slot:02d}.lock",
                flags,
                0o600,
                dir_fd=directory_fd,
            )
            descriptors.append(descriptor)
        offset = (os.getpid() + threading.get_ident()) % limit
        while acquired is None:
            for index in range(limit):
                descriptor = descriptors[(offset + index) % limit]
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                acquired = descriptor
                break
            if acquired is None:
                time.sleep(0.05)
        yield
    finally:
        if acquired is not None:
            fcntl.flock(acquired, fcntl.LOCK_UN)
        for descriptor in descriptors:
            os.close(descriptor)
        os.close(directory_fd)


class TofuHostAgent(BaseAgent):
    """A small external-tool loop over Tofu's host-side model dispatcher."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        max_rounds: int = 4096,
        max_output_tokens: int = 32768,
        dispatch_timeout_sec: float = 300.0,
        dispatch_max_retries: int = 8,
        global_dispatch_concurrency: int = 4,
        dispatch_gate_dir: str | os.PathLike[str] = "/tmp/tofu-harbor-dispatch-gate",
        context_checkpoint_tokens: int = 300_000,
        command_timeout_sec: int = 480,
        command_timeout_multiplier: float = 1.0,
        reasoning_effort: str = "max",
        temperature: float = 1.0,
        top_p: float = 0.95,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if not model_name:
            raise ValueError("TofuHostAgent requires --model")
        self._max_rounds = max(1, int(max_rounds))
        self._max_output_tokens = max(256, int(max_output_tokens))
        self._dispatch_timeout_sec = max(1.0, float(dispatch_timeout_sec))
        self._dispatch_max_retries = max(1, min(32, int(dispatch_max_retries)))
        self._global_dispatch_concurrency = int(global_dispatch_concurrency)
        if not 1 <= self._global_dispatch_concurrency <= 32:
            raise ValueError("global_dispatch_concurrency must be between 1 and 32")
        self._dispatch_gate_dir = Path(dispatch_gate_dir)
        self._context_checkpoint_tokens = int(context_checkpoint_tokens)
        if not 1024 <= self._context_checkpoint_tokens <= 1_000_000:
            raise ValueError("context_checkpoint_tokens must be between 1024 and 1000000")
        self._command_timeout_sec = max(1, min(1800, int(command_timeout_sec)))
        self._command_timeout_multiplier = float(command_timeout_multiplier)
        if not math.isfinite(self._command_timeout_multiplier) or (
            self._command_timeout_multiplier < 1
        ):
            raise ValueError("command_timeout_multiplier must be finite and at least 1")
        self._reasoning_effort = reasoning_effort.strip().lower()
        if self._reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("reasoning_effort must be low, high, or max")
        self._temperature = float(temperature)
        self._top_p = float(top_p)
        if not 0 <= self._top_p <= 1:
            raise ValueError("top_p must be between 0 and 1")

    @staticmethod
    def name() -> str:
        return "tofu-host"

    def version(self) -> str:
        return "0.8.4"

    async def setup(self, environment: BaseEnvironment) -> None:
        # The agent and Tofu runtime are host-side; nothing is installed in the
        # untrusted task container.
        return

    @staticmethod
    def _dispatch(
        messages: list[dict[str, Any]],
        model_name: str,
        max_tokens: int,
        timeout: float,
        max_retries: int,
        reasoning_effort: str,
        temperature: float,
        top_p: float,
        global_dispatch_concurrency: int,
        dispatch_gate_dir: Path,
    ) -> tuple[str, dict[str, Any]]:
        from lib.llm_dispatch import dispatch_chat, get_dispatcher

        # Tofu's strict_model intentionally permits every physical route with
        # the same logical model id. Benchmarks need a stronger invariant: a
        # request for the Meituan route must never silently use Huawei/Tencent.
        dispatcher = get_dispatcher()
        dispatcher.initialize()
        physical_models = {slot.model for slot in dispatcher.slots}
        if model_name not in physical_models:
            raise RuntimeError(f"requested physical model is unavailable: {model_name}")
        excluded_models = physical_models - {model_name}

        extra: dict[str, Any] = {"top_p": top_p}
        if "deepseek-v4" in model_name.lower():
            extra.update(
                {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": reasoning_effort,
                }
            )

        gate_started = time.monotonic()
        with _dispatch_slot(dispatch_gate_dir, global_dispatch_concurrency):
            gate_wait_ms = round((time.monotonic() - gate_started) * 1000)
            result = dispatch_chat(
                messages,
                prefer_model=model_name,
                strict_model=True,
                exclude_models=excluded_models,
                tools=_TOOLS,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_enabled=True,
                effort=reasoning_effort,
                extra=extra,
                timeout=timeout,
                max_retries=max_retries,
                log_prefix="[rootless-vm/harbor]",
            )
        metadata = result[1].get("_dispatch")
        if isinstance(metadata, dict):
            metadata["gate_wait_ms"] = gate_wait_ms
        served_model = metadata.get("model") if isinstance(metadata, dict) else None
        if served_model != model_name:
            raise RuntimeError(
                f"physical model routing mismatch: requested {model_name}, got {served_model}"
            )
        return result

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
        transcript: list[dict[str, Any]] = []
        input_tokens = 0
        output_tokens = 0
        provider_latency_ms = 0
        repeated_fingerprint: str | None = None
        repeat_count = 0
        repeated_result: str | None = None
        result_repeat_count = 0
        consecutive_lengths = 0
        recovery_count = 0
        context_checkpoint_count = 0
        length_retry_count = 0
        command_count = 0
        submit_count = 0
        validation_reuse_count = 0
        ran_command = False
        last_successful_command: str | None = None
        validation_summary: str | None = None
        started_at = time.monotonic()
        exit_reason = "round_limit"
        dispatch_output_budget = self._max_output_tokens
        dispatch_succeeded_since_checkpoint = True

        for round_index in range(self._max_rounds):
            try:
                content, usage = await asyncio.to_thread(
                    self._dispatch,
                    messages,
                    self.model_name,
                    dispatch_output_budget,
                    self._dispatch_timeout_sec,
                    self._dispatch_max_retries,
                    self._reasoning_effort,
                    self._temperature,
                    self._top_p,
                    self._global_dispatch_concurrency,
                    self._dispatch_gate_dir,
                )
            except PromptTooLongError:
                # The proactive threshold leaves ample room for one maximal
                # response plus bounded terminal output. Keep this reactive
                # path for provider accounting changes and unusually large
                # single turns. A second immediate rejection is not recoverable.
                if not dispatch_succeeded_since_checkpoint:
                    raise
                messages = _context_checkpoint_messages(instruction, transcript)
                context_checkpoint_count += 1
                dispatch_succeeded_since_checkpoint = False
                transcript.append(
                    {
                        "round": round_index,
                        "control": "fresh_context_checkpoint",
                        "reason": "provider_prompt_too_long",
                    }
                )
                _persist_transcript(self.logs_dir, transcript)
                continue
            dispatch_succeeded_since_checkpoint = True
            reported_tool_calls = usage.pop("_tool_calls", []) or []
            reasoning_content = usage.pop("_reasoning_content", "") or ""
            tool_calls = [
                call for call in reported_tool_calls if isinstance(call, dict)
            ][:8]
            finish_reason = str(usage.get("finish_reason") or "")
            dispatch_metadata = usage.get("_dispatch")
            if isinstance(dispatch_metadata, dict):
                provider_latency_ms += int(dispatch_metadata.get("latency_ms") or 0)
            request_prompt_tokens = _usage_prompt_tokens(usage)
            input_tokens += request_prompt_tokens
            output_tokens += int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            )
            assistant: dict[str, Any] = {"role": "assistant", "content": content or ""}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
                if reasoning_content:
                    assistant["reasoning_content"] = reasoning_content
            audit_assistant = dict(assistant)
            if reasoning_content:
                audit_assistant["reasoning_content"] = {
                    "redacted": True,
                    "characters": len(reasoning_content),
                    "sha256": hashlib.sha256(reasoning_content.encode()).hexdigest(),
                }
            transcript.append(
                {
                    "round": round_index,
                    # The on-disk audit transcript keeps the complete provider
                    # response even though the next model turn gets a compact
                    # working history below.
                    "assistant": audit_assistant,
                    "usage": _audit_usage(usage),
                }
            )
            # A command can consume the rest of Harbor's trial budget. Persist
            # its exact (reasoning-redacted) request first so timeout diagnosis
            # never depends on a normally returned agent result.
            _persist_transcript(self.logs_dir, transcript)
            if tool_calls and len(assistant["content"]) > 2048:
                assistant["content"] = assistant["content"][-2048:]
            messages.append(assistant)
            if not tool_calls:
                if finish_reason == "length":
                    length_retry_count += 1
                    # Do not carry an arbitrarily large, truncated monologue
                    # into every later request. Preserve enough tail for local
                    # coherence, then force the next turn back onto tools.
                    assistant["content"] = (
                        (content or "")[-2048:] or _EMPTY_ASSISTANT_PLACEHOLDER
                    )
                    messages[-1] = assistant
                    consecutive_lengths += 1
                    dispatch_output_budget = min(
                        self._max_output_tokens,
                        max(4096, dispatch_output_budget * 2),
                    )
                    if request_prompt_tokens >= self._context_checkpoint_tokens:
                        context_checkpoint_count += 1
                        messages = _context_checkpoint_messages(instruction, transcript)
                        dispatch_succeeded_since_checkpoint = False
                        consecutive_lengths = 0
                        dispatch_output_budget = self._max_output_tokens
                        transcript.append(
                            {
                                "round": round_index,
                                "control": "fresh_context_checkpoint",
                                "reason": "proactive_context_pressure",
                                "prompt_tokens": request_prompt_tokens,
                            }
                        )
                    elif consecutive_lengths >= 2 and recovery_count < 3:
                        recovery_count += 1
                        messages = _recovery_messages(
                            instruction,
                            transcript,
                            "repeated output-limit responses without a tool call",
                        )
                        consecutive_lengths = 0
                        dispatch_output_budget = self._max_output_tokens
                        transcript.append(
                            {
                                "round": round_index,
                                "control": "fresh_context_recovery",
                                "reason": "repeated_length",
                            }
                        )
                    else:
                        messages.append(
                            {"role": "user", "content": _TRUNCATION_NUDGE}
                        )
                        transcript.append(
                            {"round": round_index, "control": "retry_after_length"}
                        )
                    continue
                consecutive_lengths = 0
                if ran_command and finish_reason in {"stop", "completed", "end_turn"}:
                    exit_reason = "completed"
                    validation_summary = (content or "Task completed.").strip()[:4096]
                    break
                assistant["content"] = (
                    (content or "")[-2048:] or _EMPTY_ASSISTANT_PLACEHOLDER
                )
                messages[-1] = assistant
                if request_prompt_tokens >= self._context_checkpoint_tokens:
                    context_checkpoint_count += 1
                    messages = _context_checkpoint_messages(instruction, transcript)
                    dispatch_succeeded_since_checkpoint = False
                    transcript.append(
                        {
                            "round": round_index,
                            "control": "fresh_context_checkpoint",
                            "reason": "proactive_context_pressure",
                            "prompt_tokens": request_prompt_tokens,
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Use run_command or submit_result now.",
                        }
                    )
                    transcript.append(
                        {"round": round_index, "control": "retry_without_tool"}
                    )
                continue

            consecutive_lengths = 0
            dispatch_output_budget = self._max_output_tokens

            fingerprint = repr(
                [
                    (
                        (call.get("function") or {}).get("name"),
                        (call.get("function") or {}).get("arguments"),
                    )
                    for call in tool_calls
                    if isinstance(call, dict)
                ]
            )
            repeat_count = repeat_count + 1 if fingerprint == repeated_fingerprint else 0
            repeated_fingerprint = fingerprint
            if repeat_count >= 3:
                if recovery_count >= 3:
                    exit_reason = "no_progress"
                    break

            recovery_reason: str | None = None
            for call_index, call in enumerate(tool_calls):
                effective_timeout_sec: int | None = None
                call_id = str(call.get("id") or f"round-{round_index}-call-{call_index}")
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else ""
                raw_arguments = function.get("arguments", "{}") if isinstance(function, dict) else "{}"
                try:
                    arguments = (
                        raw_arguments
                        if isinstance(raw_arguments, dict)
                        else json.loads(raw_arguments or "{}")
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    result_text = "Error: tool arguments are not valid JSON"
                else:
                    if not isinstance(arguments, dict):
                        result_text = "Error: tool arguments must be a JSON object"
                    elif name == "submit_result":
                        submit_count += 1
                        summary = arguments.get("summary")
                        validation = arguments.get("validation_command")
                        if not ran_command:
                            result_text = "Error: run_command must be used before submission"
                        elif not isinstance(summary, str) or not summary.strip():
                            result_text = "Error: summary must be a non-empty string"
                        elif not isinstance(validation, str) or not validation.strip():
                            result_text = "Error: validation_command must be non-empty"
                        elif len(validation) > 32 * 1024:
                            result_text = "Error: validation_command exceeds 32 KiB"
                        elif validation == last_successful_command:
                            # The state has not been touched since this exact
                            # command returned zero. Re-running a long compile
                            # or network-backed test wastes the task wall clock
                            # without adding evidence.
                            validation_reuse_count += 1
                            result_text = (
                                "exit_code=0\nstdout:\n"
                                "Reused the immediately preceding successful "
                                "validation result."
                            )
                            validation_summary = summary.strip()[:4096]
                            exit_reason = "submitted"
                        else:
                            requested_timeout = arguments.get("timeout_sec")
                            timeout_sec = _bounded_timeout(
                                requested_timeout,
                                self._command_timeout_sec,
                                self._command_timeout_multiplier,
                            )
                            effective_timeout_sec = timeout_sec
                            last_successful_command = None
                            result = await environment.exec(
                                command=validation,
                                timeout_sec=timeout_sec,
                            )
                            result_text = _bounded_output(
                                result.stdout, result.stderr, result.return_code
                            )
                            if result.return_code == 0:
                                last_successful_command = validation
                                validation_summary = summary.strip()[:4096]
                                exit_reason = "submitted"
                    elif name != "run_command":
                        result_text = f"Error: unsupported tool {name!r}"
                    elif not isinstance(arguments.get("command"), str) or not arguments[
                        "command"
                    ].strip():
                        result_text = "Error: command must be a non-empty string"
                    elif len(arguments["command"]) > 32 * 1024:
                        result_text = "Error: command exceeds the 32 KiB safety limit"
                    else:
                        command_count += 1
                        command = arguments["command"]
                        requested_timeout = arguments.get("timeout_sec")
                        timeout_sec = _bounded_timeout(
                            requested_timeout,
                            self._command_timeout_sec,
                            self._command_timeout_multiplier,
                        )
                        effective_timeout_sec = timeout_sec
                        result = await environment.exec(
                            command=command,
                            timeout_sec=timeout_sec,
                        )
                        ran_command = True
                        last_successful_command = (
                            command if result.return_code == 0 else None
                        )
                        result_text = _bounded_output(
                            result.stdout, result.stderr, result.return_code
                        )
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_text,
                }
                messages.append(tool_message)
                transcript.append(
                    {
                        "round": round_index,
                        "tool_call_id": call_id,
                        "tool": name,
                        "result": result_text,
                        "effective_timeout_sec": effective_timeout_sec,
                    }
                )
                # Do not make timeout diagnosis wait for the next provider
                # response: at this boundary the guest command has definitely
                # returned, while the following dispatch may retry for minutes.
                _persist_transcript(self.logs_dir, transcript)
                if result_text == repeated_result:
                    result_repeat_count += 1
                else:
                    repeated_result = result_text
                    result_repeat_count = 1
                if result_repeat_count >= 3:
                    recovery_reason = "the same terminal result occurred three times"
                if exit_reason == "submitted":
                    break
            if exit_reason == "submitted":
                break
            if repeat_count >= 3 and recovery_reason is None:
                recovery_reason = "the same tool request was repeated four times"
            if recovery_reason is not None:
                if recovery_count >= 3:
                    exit_reason = "no_progress"
                    break
                recovery_count += 1
                messages = _recovery_messages(
                    instruction,
                    transcript,
                    recovery_reason,
                )
                repeated_fingerprint = None
                repeat_count = 0
                repeated_result = None
                result_repeat_count = 0
                dispatch_output_budget = self._max_output_tokens
                transcript.append(
                    {
                        "round": round_index,
                        "control": "fresh_context_recovery",
                        "reason": "repeated_tool_evidence",
                    }
                )
            elif request_prompt_tokens >= self._context_checkpoint_tokens:
                context_checkpoint_count += 1
                messages = _context_checkpoint_messages(instruction, transcript)
                dispatch_succeeded_since_checkpoint = False
                repeated_fingerprint = None
                repeat_count = 0
                repeated_result = None
                result_repeat_count = 0
                transcript.append(
                    {
                        "round": round_index,
                        "control": "fresh_context_checkpoint",
                        "reason": "proactive_context_pressure",
                        "prompt_tokens": request_prompt_tokens,
                    }
                )

        _persist_transcript(self.logs_dir, transcript)
        context.n_input_tokens = input_tokens
        context.n_output_tokens = output_tokens
        context.metadata = {
            "exit_reason": exit_reason,
            "rounds": len([row for row in transcript if "assistant" in row]),
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "model": self.model_name,
            "credential_boundary": "host-only",
            "validation_summary": validation_summary,
            "command_count": command_count,
            "submit_count": submit_count,
            "validation_reuse_count": validation_reuse_count,
            "recovery_count": recovery_count,
            "context_checkpoint_count": context_checkpoint_count,
            "length_retry_count": length_retry_count,
            "provider_latency_sec": round(provider_latency_ms / 1000, 3),
            "max_output_tokens": self._max_output_tokens,
            "context_checkpoint_tokens": self._context_checkpoint_tokens,
            "command_timeout_sec": self._command_timeout_sec,
            "command_timeout_multiplier": self._command_timeout_multiplier,
            "reasoning_effort": self._reasoning_effort,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "physical_model_strict": True,
        }
