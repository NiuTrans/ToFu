"""Host-side DeepSeek Harness Minimal adapter for rootless Harbor trials.

The prompt and two model-facing tools match DeepSeek Harness Minimal. Provider
credentials and physical model routing stay in the trusted Harbor process;
only tool requests and bounded results cross into the disposable QEMU guest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from rootless_vm.deepseek_minimal_tools import (
    MINIMAL_TOOLS,
    PersistentBash,
    StrReplaceEditor,
    tool_schema_digest,
)
from rootless_vm.harbor_tofu_agent import (
    _audit_usage,
    _dispatch_model,
    _persist_transcript,
    _usage_prompt_tokens,
)
from rootless_vm.trajectory import persist_host_atif


SYSTEM_PROMPT = "You are a helpful software engineer assistant."
_CONTEXT_ESTIMATE_RATIO_FLOOR = 1.50
_CONTEXT_OBSERVED_RATIO_MARGIN = 1.05
_CONTEXT_FIXED_MARGIN_TOKENS = 2_048
_MIN_COMPLETION_TOKENS = 1_024


class DeepSeekMinimalHostAgent(BaseAgent):
    """Official-Minimal-compatible tool loop over the host model dispatcher."""

    SUPPORTS_ATIF = True

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        max_rounds: int = 4096,
        max_output_tokens: int = 256_000,
        context_window_tokens: int = 393_216,
        dispatch_timeout_sec: float = 300.0,
        dispatch_max_retries: int = 8,
        global_dispatch_concurrency: int = 4,
        dispatch_gate_dir: str | os.PathLike[str] = "/tmp/tofu-harbor-dispatch-gate",
        bash_timeout_sec: int = 300,
        reasoning_effort: str = "max",
        temperature: float = 1.0,
        top_p: float = 0.95,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if not model_name:
            raise ValueError("DeepSeekMinimalHostAgent requires --model")
        self._max_rounds = max(1, min(16_384, int(max_rounds)))
        self._max_output_tokens = max(256, int(max_output_tokens))
        self._context_window_tokens = int(context_window_tokens)
        if self._context_window_tokens < 4_096:
            raise ValueError("context_window_tokens must be at least 4096")
        self._dispatch_timeout_sec = max(1.0, float(dispatch_timeout_sec))
        self._dispatch_max_retries = max(1, min(32, int(dispatch_max_retries)))
        self._global_dispatch_concurrency = int(global_dispatch_concurrency)
        if not 1 <= self._global_dispatch_concurrency <= 32:
            raise ValueError("global_dispatch_concurrency must be between 1 and 32")
        self._dispatch_gate_dir = Path(dispatch_gate_dir)
        self._bash_timeout_sec = max(1, min(1800, int(bash_timeout_sec)))
        self._reasoning_effort = reasoning_effort.strip().lower()
        if self._reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("reasoning_effort must be low, high, or max")
        self._temperature = float(temperature)
        self._top_p = float(top_p)
        if not math.isfinite(self._temperature):
            raise ValueError("temperature must be finite")
        if not 0 <= self._top_p <= 1:
            raise ValueError("top_p must be between 0 and 1")

    @staticmethod
    def name() -> str:
        return "deepseek-minimal-host"

    def version(self) -> str:
        return "1.0.2"

    async def setup(self, environment: BaseEnvironment) -> None:
        # The model runtime is host-side and the two tools use only Harbor's
        # environment API. No agent package or credential is installed in guest.
        return

    def _dispatch(
        self,
        messages: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        assert self.model_name is not None
        return _dispatch_model(
            messages,
            self.model_name,
            max_output_tokens,
            self._dispatch_timeout_sec,
            self._dispatch_max_retries,
            self._reasoning_effort,
            self._temperature,
            self._top_p,
            self._global_dispatch_concurrency,
            self._dispatch_gate_dir,
            MINIMAL_TOOLS,
            log_prefix="[rootless-vm/harbor/deepseek-minimal]",
        )

    def _request_output_budget(
        self,
        messages: list[dict[str, Any]],
        observed_input_ratio: float = 1.0,
    ) -> tuple[int, int]:
        """Reserve provider context for the growing prompt and exact tools."""

        from lib.token_counter.heuristic import cheap_estimate

        estimated_input_tokens = cheap_estimate(messages, tools=MINIMAL_TOOLS)
        estimate_ratio = max(
            _CONTEXT_ESTIMATE_RATIO_FLOOR,
            observed_input_ratio * _CONTEXT_OBSERVED_RATIO_MARGIN,
        )
        reserved_input_tokens = (
            math.ceil(estimated_input_tokens * estimate_ratio)
            + _CONTEXT_FIXED_MARGIN_TOKENS
        )
        available_output_tokens = self._context_window_tokens - reserved_input_tokens
        if available_output_tokens < _MIN_COMPLETION_TOKENS:
            raise RuntimeError(
                "DeepSeek Minimal prompt exhausted the configured physical "
                f"context window ({self._context_window_tokens} tokens)"
            )
        return min(
            self._max_output_tokens, available_output_tokens
        ), estimated_input_tokens

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        assert self.model_name is not None
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
        transcript: list[dict[str, Any]] = []
        bash = PersistentBash(environment, timeout_sec=self._bash_timeout_sec)
        editor = StrReplaceEditor(environment)
        input_tokens = 0
        output_tokens = 0
        provider_latency_ms = 0
        tool_call_count = 0
        observed_input_ratio = 1.0
        started_at = time.monotonic()
        exit_reason = "round_limit"

        def checkpoint() -> None:
            _persist_transcript(
                self.logs_dir,
                transcript,
                filename="host-dispatch-transcript.json",
            )
            persist_host_atif(
                self.logs_dir,
                transcript=transcript,
                instruction=instruction,
                system_prompt=SYSTEM_PROMPT,
                agent_name=self.name(),
                agent_version=self.version(),
                model_name=self.model_name or "unknown",
                tool_definitions=MINIMAL_TOOLS,
                session_id=str(self.context_id or self.session_id or "") or None,
                credential_boundary="host-only",
                harness_profile="deepseek-minimal",
            )

        try:
            for round_index in range(self._max_rounds):
                request_output_tokens, estimated_input_tokens = (
                    self._request_output_budget(messages, observed_input_ratio)
                )
                content, usage = await asyncio.to_thread(
                    self._dispatch,
                    messages,
                    request_output_tokens,
                )
                prompt_tokens = _usage_prompt_tokens(usage)
                if prompt_tokens > 0 and estimated_input_tokens > 0:
                    observed_input_ratio = max(
                        observed_input_ratio,
                        prompt_tokens / estimated_input_tokens,
                    )
                usage["_harness_request"] = {
                    "context_window_tokens": self._context_window_tokens,
                    "estimated_input_tokens": estimated_input_tokens,
                    "observed_input_ratio": round(observed_input_ratio, 6),
                    "max_output_tokens": request_output_tokens,
                }
                reported_calls = usage.pop("_tool_calls", []) or []
                reasoning_content = usage.pop("_reasoning_content", "") or ""
                # Preserve every provider-reported call. Execution remains
                # serialized by the two stateful Minimal tools, but silently
                # dropping calls above an arbitrary threshold would leave an
                # invalid assistant/tool transcript.
                tool_calls = [
                    call for call in reported_calls if isinstance(call, dict)
                ]
                completion_tokens = int(
                    usage.get("output_tokens") or usage.get("completion_tokens") or 0
                )
                input_tokens += prompt_tokens
                output_tokens += completion_tokens
                dispatch = usage.get("_dispatch")
                if isinstance(dispatch, dict):
                    provider_latency_ms += int(dispatch.get("latency_ms") or 0)

                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": content or "",
                }
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                    # DeepSeek thinking-mode tool turns require the exact
                    # reasoning_content to be replayed on the next API request.
                    if reasoning_content:
                        assistant["reasoning_content"] = reasoning_content
                audit_assistant = dict(assistant)
                if reasoning_content:
                    audit_assistant["reasoning_content"] = {
                        "redacted": True,
                        "characters": len(reasoning_content),
                        "sha256": hashlib.sha256(
                            reasoning_content.encode()
                        ).hexdigest(),
                    }
                transcript.append(
                    {
                        "round": round_index,
                        "assistant": audit_assistant,
                        "usage": _audit_usage(usage),
                    }
                )
                checkpoint()
                messages.append(assistant)
                if not tool_calls:
                    exit_reason = "completed"
                    break

                for call_index, call in enumerate(tool_calls):
                    call_id = str(
                        call.get("id") or f"round-{round_index}-call-{call_index}"
                    )
                    function = call.get("function")
                    name = function.get("name") if isinstance(function, dict) else ""
                    raw_arguments = (
                        function.get("arguments", "{}")
                        if isinstance(function, dict)
                        else "{}"
                    )
                    try:
                        arguments = (
                            raw_arguments
                            if isinstance(raw_arguments, dict)
                            else json.loads(raw_arguments or "{}")
                        )
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be a JSON object")
                        if name == "bash":
                            command = arguments.get("command")
                            if not isinstance(command, str):
                                raise ValueError(
                                    "Parameter `command` is required for command: bash"
                                )
                            result_text = await bash.run(command)
                        elif name == "str_replace_editor":
                            result_text = await editor.run(arguments)
                        else:
                            raise ValueError(f"unsupported tool {name!r}")
                    except Exception as exc:
                        result_text = f"Error: {exc}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result_text,
                        }
                    )
                    transcript.append(
                        {
                            "round": round_index,
                            "tool_call_id": call_id,
                            "tool": name,
                            "result": result_text,
                            "effective_timeout_sec": (
                                self._bash_timeout_sec if name == "bash" else None
                            ),
                        }
                    )
                    tool_call_count += 1
                    checkpoint()
        finally:
            await bash.close()
            checkpoint()

        context.n_input_tokens = input_tokens
        context.n_output_tokens = output_tokens
        context.metadata = {
            "exit_reason": exit_reason,
            "rounds": len([row for row in transcript if "assistant" in row]),
            "elapsed_sec": round(time.monotonic() - started_at, 3),
            "model": self.model_name,
            "harness_profile": "deepseek-minimal",
            "comparison_target": "DeepSeek Harness Minimal",
            "credential_boundary": "host-only",
            "tool_call_count": tool_call_count,
            "tool_schema_sha256": tool_schema_digest(),
            "provider_latency_sec": round(provider_latency_ms / 1000, 3),
            "max_output_tokens": self._max_output_tokens,
            "context_window_tokens": self._context_window_tokens,
            "max_rounds": self._max_rounds,
            "bash_timeout_sec": self._bash_timeout_sec,
            "reasoning_effort": self._reasoning_effort,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "physical_model_strict": True,
        }
