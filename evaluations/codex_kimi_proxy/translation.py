"""Pure request/response translation for the benchmark-only proxy."""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class TranslationError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status

    def to_response(self) -> dict[str, Any]:
        return {"error": {"type": self.code, "message": str(self)}}


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _chat_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _text_content(content)
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise TranslationError("unsupported_content", "content block must be an object")
        kind = str(block.get("type") or "")
        if kind in {"input_text", "output_text", "text"}:
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind == "input_image":
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url:
                raise TranslationError("invalid_image", "input_image requires image_url")
            blocks.append({"type": "image_url", "image_url": {"url": image_url}})
        else:
            raise TranslationError(
                "unsupported_content", f"unsupported Responses content type: {kind}")
    return blocks


def _append_input_item(messages: list[dict[str, Any]], item: Any) -> None:
    if isinstance(item, str):
        messages.append({"role": "user", "content": item})
        return
    if not isinstance(item, dict):
        raise TranslationError("invalid_input", "Responses input item must be an object")
    kind = str(item.get("type") or "message")
    if kind == "message":
        role = str(item.get("role") or "user")
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant", "tool"}:
            raise TranslationError("unsupported_role", f"unsupported role: {role}")
        message = {"role": role, "content": _chat_content(item.get("content", ""))}
        if item.get("name"):
            message["name"] = str(item["name"])
        messages.append(message)
        return
    if kind == "function_call":
        call = {
            "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
            "type": "function",
            "function": {
                "name": str(item.get("name") or ""),
                "arguments": _text_content(item.get("arguments") or "{}"),
            },
        }
        if not call["function"]["name"]:
            raise TranslationError("invalid_function_call", "function_call requires name")
        if messages and messages[-1].get("role") == "assistant" \
                and messages[-1].get("tool_calls"):
            messages[-1]["tool_calls"].append(call)
        else:
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": [call]})
        return
    if kind == "function_call_output":
        call_id = str(item.get("call_id") or "")
        if not call_id:
            raise TranslationError(
                "invalid_function_output", "function_call_output requires call_id")
        messages.append({
            "role": "tool", "tool_call_id": call_id,
            "content": _text_content(item.get("output")),
        })
        return
    if kind == "reasoning":
        summary = item.get("summary") or []
        reasoning = "".join(
            str(part.get("text") or "") for part in summary
            if isinstance(part, dict)) if isinstance(summary, list) else str(summary)
        messages.append({"role": "assistant", "content": "",
                         "reasoning_content": reasoning})
        return
    raise TranslationError("unsupported_input_item",
                           f"unsupported Responses input item: {kind}")


def _chat_tools(tools: Any) -> list[dict[str, Any]]:
    """Flatten Responses namespace wrappers into Chat function schemas.

    Codex currently includes its provider-native ``web_search`` descriptor in
    Responses requests even when the benchmark command disables web search.
    Research trials use the frozen MCP backend instead, represented as an
    ordinary function tool, so forwarding the native descriptor would create
    a second data arm that Kimi Chat cannot execute.  Suppress only that known
    native type; unknown types remain hard failures so protocol drift cannot
    pass unnoticed.
    """
    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    def append_tool(tool: Any, namespace_context: str = "") -> None:
        if not isinstance(tool, dict):
            raise TranslationError(
                "unsupported_tool",
                f"unsupported Responses tool type: {type(tool).__name__}")
        tool_type = str(tool.get("type") or "missing")
        if tool_type == "web_search":
            return
        if tool_type == "namespace":
            namespace_name = str(tool.get("name") or "").strip()
            nested = tool.get("tools")
            if not namespace_name or not isinstance(nested, list) or not nested:
                raise TranslationError(
                    "invalid_tool_namespace",
                    "tool namespace requires a name and non-empty tools list")
            description = str(tool.get("description") or "").strip()
            context = f"Namespace {namespace_name}"
            if description:
                context += f": {description}"
            for child in nested:
                append_tool(child, context)
            return
        if tool_type != "function":
            raise TranslationError(
                "unsupported_tool",
                f"unsupported Responses tool type: {tool_type}")
        name = str(tool.get("name") or "")
        if not name:
            raise TranslationError("invalid_tool", "function tool requires name")
        if name in seen_names:
            raise TranslationError(
                "duplicate_tool_name",
                f"flattened tool catalog contains duplicate name: {name}")
        seen_names.add(name)
        parameters = tool.get("parameters") or {
            "type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise TranslationError(
                "invalid_tool", f"function tool {name} parameters must be an object")
        function = {
            "name": name,
            "description": "\n\n".join(
                value for value in (
                    namespace_context,
                    str(tool.get("description") or "").strip(),
                ) if value),
            "parameters": parameters,
        }
        if tool.get("strict") is not None:
            function["strict"] = bool(tool["strict"])
        result.append({
            "type": "function",
            "function": function,
        })

    for raw_tool in tools or ():
        append_tool(raw_tool)
    return result


def suppressed_native_tool_types(tools: Any) -> tuple[str, ...]:
    """Return native tool types intentionally excluded from the Kimi arm."""
    suppressed: set[str] = set()

    def inspect(tool: Any) -> None:
        if not isinstance(tool, dict):
            return
        tool_type = str(tool.get("type") or "")
        if tool_type == "web_search":
            suppressed.add(tool_type)
        if tool_type == "namespace":
            for child in tool.get("tools") or ():
                inspect(child)

    for raw_tool in tools or ():
        inspect(raw_tool)
    return tuple(sorted(suppressed))


def _tool_choice(value: Any) -> Any:
    if value in (None, "auto", "none", "required"):
        return value
    if isinstance(value, dict) and value.get("type") == "function":
        name = value.get("name") or (value.get("function") or {}).get("name")
        if name:
            return {"type": "function", "function": {"name": str(name)}}
    raise TranslationError("unsupported_tool_choice", "unsupported tool_choice")


def responses_request_to_chat(request: dict[str, Any]) -> dict[str, Any]:
    """Translate without issuing a model call or reading application config."""
    if not isinstance(request, dict):
        raise TranslationError("invalid_request", "request body must be an object")
    if request.get("previous_response_id"):
        raise TranslationError(
            "stateful_responses_unsupported",
            "Benchmark trials must be ephemeral and replay full local history.")
    messages: list[dict[str, Any]] = []
    instructions = request.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _chat_content(instructions)})
    input_value = request.get("input", "")
    if isinstance(input_value, list):
        for item in input_value:
            _append_input_item(messages, item)
    else:
        _append_input_item(messages, input_value)
    model = str(request.get("model") or "kimi-k3")
    if model != "kimi-k3":
        raise TranslationError(
            "invalid_benchmark_model", "benchmark proxy requires model kimi-k3")
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(request.get("stream", False)),
    }
    tools = _chat_tools(request.get("tools"))
    if tools:
        body["tools"] = tools
    choice = _tool_choice(request.get("tool_choice"))
    if choice is not None:
        body["tool_choice"] = choice
    if request.get("parallel_tool_calls") is not None:
        body["parallel_tool_calls"] = bool(request["parallel_tool_calls"])
    if request.get("max_output_tokens") is not None:
        try:
            maximum = int(request["max_output_tokens"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise TranslationError(
                "invalid_max_output_tokens", "max_output_tokens must be an integer") from exc
        if maximum <= 0:
            raise TranslationError(
                "invalid_max_output_tokens", "max_output_tokens must be positive")
        body["max_tokens"] = maximum
    reasoning = request.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        effort = str(reasoning["effort"]).lower()
        body["reasoning_effort"] = {
            "none": "low", "minimal": "low", "low": "low",
            "medium": "high", "high": "high",
            "xhigh": "max", "max": "max", "ultra": "max",
        }.get(effort, "high")
    if body["stream"]:
        body["stream_options"] = {"include_usage": True}
    if request.get("temperature") is not None:
        temperature = request["temperature"]
        if (not isinstance(temperature, (int, float))
                or isinstance(temperature, bool)
                or not math.isfinite(float(temperature))):
            raise TranslationError(
                "invalid_temperature", "temperature must be a finite number")
        body["temperature"] = temperature
    return body


def _usage(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    prompt = int(value.get("prompt_tokens") or 0)
    cached = int(((value.get("prompt_tokens_details") or {})
                  .get("cached_tokens") or value.get("cached_tokens") or 0))
    completion = int(value.get("completion_tokens") or 0)
    reasoning = int(((value.get("completion_tokens_details") or {})
                     .get("reasoning_tokens") or 0))
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": int(value.get("total_tokens") or prompt + completion),
    }


def _unique_call_id(raw: Any, seen: set[str], index: int) -> str:
    base = str(raw or "").strip() or f"call_{index}"
    candidate = base
    suffix = 1
    while candidate in seen:
        suffix += 1
        candidate = f"{base}_{suffix}"
    seen.add(candidate)
    return candidate


def chat_response_to_responses(response: dict[str, Any], *,
                               response_id: str | None = None) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise TranslationError(
            "invalid_upstream_response", "Kimi response must be an object",
            status=502)
    choices = response.get("choices") or []
    if not isinstance(choices, list) or len(choices) != 1 \
            or not isinstance(choices[0], dict):
        raise TranslationError(
            "invalid_upstream_response",
            "Kimi response must contain exactly one choice", status=502)
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise TranslationError(
            "invalid_upstream_response", "Kimi choice.message is invalid",
            status=502)
    output: list[dict[str, Any]] = []
    reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
    if reasoning:
        output.append({
            "type": "reasoning", "id": f"rs_{uuid.uuid4().hex}",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    content = message.get("content")
    if content not in (None, ""):
        output.append({
            "type": "message", "id": f"msg_{uuid.uuid4().hex}",
            "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": _text_content(content),
                         "annotations": []}],
        })
    seen: set[str] = set()
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        if not str(function.get("name") or ""):
            raise TranslationError(
                "invalid_function_call", "Kimi function call is missing a name",
                status=502)
        call_id = _unique_call_id(call.get("id"), seen, index)
        output.append({
            "type": "function_call", "id": f"fc_{uuid.uuid4().hex}",
            "call_id": call_id, "name": str(function.get("name") or ""),
            "arguments": _text_content(function.get("arguments") or "{}"),
            "status": "completed",
        })
    finish_reason = str(choices[0].get("finish_reason") or "")
    if finish_reason not in {"stop", "tool_calls", "length", "content_filter"}:
        raise TranslationError(
            "invalid_finish_reason",
            f"Kimi returned an unsupported finish reason: {finish_reason!r}",
            status=502)
    if finish_reason == "tool_calls" \
            and not any(item.get("type") == "function_call" for item in output):
        raise TranslationError(
            "invalid_function_call",
            "Kimi ended for tool calls without emitting a function call",
            status=502)
    if finish_reason == "stop" and not output:
        raise TranslationError(
            "empty_upstream_response",
            "Kimi completed without content, reasoning, or a function call",
            status=502)
    status = ("incomplete" if finish_reason == "length" else
              "failed" if finish_reason == "content_filter" else
              "completed")
    result = {
        "id": response_id or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(response.get("created") or time.time()),
        "status": status,
        "model": str(response.get("model") or "kimi-k3"),
        "output": output,
        "usage": _usage(response.get("usage")),
        "error": None,
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    elif status == "failed":
        result["error"] = {
            "code": "upstream_content_filter",
            "message": "Kimi stopped the response because of its content filter.",
        }
    return result


@dataclass
class ChatSSETranslator:
    """Stateful Chat chunk → ordered Responses SSE event translator."""

    model: str = "kimi-k3"
    response_id: str = field(default_factory=lambda: f"resp_{uuid.uuid4().hex}")
    sequence: int = 0
    created: bool = False
    saw_done: bool = False
    output: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    _text_item: dict[str, Any] | None = None
    _reasoning_item: dict[str, Any] | None = None
    _calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    _seen_call_ids: set[str] = field(default_factory=set)
    _finish_reason: str = ""

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        value = {"type": kind, "sequence_number": self.sequence, **fields}
        self.sequence += 1
        return value

    def start(self) -> list[dict[str, Any]]:
        if self.created:
            return []
        self.created = True
        response = {
            "id": self.response_id, "object": "response",
            "created_at": int(time.time()), "status": "in_progress",
            "model": self.model, "output": [], "error": None,
        }
        return [self._event("response.created", response=response),
                self._event("response.in_progress", response=response)]

    def _ensure_text(self) -> list[dict[str, Any]]:
        if self._text_item is not None:
            return []
        item = {"type": "message", "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant", "status": "in_progress", "content": []}
        self._text_item = item
        self.output.append(item)
        index = len(self.output) - 1
        part = {"type": "output_text", "text": "", "annotations": []}
        return [self._event("response.output_item.added", output_index=index,
                            item=dict(item)),
                self._event("response.content_part.added", output_index=index,
                            content_index=0, part=part)]

    def _ensure_reasoning(self) -> list[dict[str, Any]]:
        if self._reasoning_item is not None:
            return []
        item = {"type": "reasoning", "id": f"rs_{uuid.uuid4().hex}",
                "status": "in_progress", "summary": []}
        self._reasoning_item = item
        self.output.append(item)
        index = len(self.output) - 1
        return [self._event("response.output_item.added", output_index=index,
                            item=dict(item)),
                self._event("response.reasoning_summary_part.added",
                            output_index=index, summary_index=0,
                            part={"type": "summary_text", "text": ""})]

    def _ensure_call(self, state: dict[str, Any], index: int
                     ) -> list[dict[str, Any]]:
        if state.get("item") is not None or not state.get("name"):
            return []
        call_id = _unique_call_id(
            state.get("raw_id"), self._seen_call_ids, index)
        item = {
            "type": "function_call", "id": f"fc_{uuid.uuid4().hex}",
            "call_id": call_id, "name": state["name"],
            "arguments": "", "status": "in_progress",
        }
        state["item"] = item
        self.output.append(item)
        return [self._event(
            "response.output_item.added",
            output_index=self.output.index(item), item=dict(item))]

    def feed(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise TranslationError(
                "invalid_upstream_chunk", "Kimi SSE chunk must be an object",
                status=502)
        events = self.start()
        if payload.get("usage"):
            self.usage = _usage(payload["usage"])
        choices = payload.get("choices") or []
        if not isinstance(choices, list) or len(choices) > 1:
            raise TranslationError(
                "invalid_upstream_chunk",
                "Kimi SSE chunk must contain at most one choice", status=502)
        for choice in choices:
            if not isinstance(choice, dict):
                raise TranslationError(
                    "invalid_upstream_chunk", "Kimi SSE choice is invalid",
                    status=502)
            if choice.get("finish_reason"):
                self._finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                events.extend(self._ensure_reasoning())
                self._reasoning_item.setdefault("_text", "")
                self._reasoning_item["_text"] += str(reasoning)
                events.append(self._event(
                    "response.reasoning_summary_text.delta",
                    item_id=self._reasoning_item["id"],
                    output_index=self.output.index(self._reasoning_item),
                    summary_index=0, delta=str(reasoning)))
            content = delta.get("content")
            if content:
                events.extend(self._ensure_text())
                self._text_item.setdefault("_text", "")
                self._text_item["_text"] += str(content)
                events.append(self._event(
                    "response.output_text.delta", item_id=self._text_item["id"],
                    output_index=self.output.index(self._text_item),
                    content_index=0, delta=str(content)))
            for raw_call in delta.get("tool_calls") or ():
                if not isinstance(raw_call, dict):
                    raise TranslationError(
                        "invalid_function_call",
                        "Kimi SSE tool call must be an object", status=502)
                index = int(raw_call.get("index") or 0)
                if index < 0:
                    raise TranslationError(
                        "invalid_function_call",
                        "Kimi SSE tool call index must be non-negative",
                        status=502)
                state = self._calls.setdefault(index, {
                    "raw_id": "", "name": "", "arguments": "",
                    "item": None, "pending": ""})
                state["raw_id"] += str(raw_call.get("id") or "")
                function = raw_call.get("function") or {}
                state["name"] += str(function.get("name") or "")
                if state["item"] is not None:
                    state["item"]["name"] = state["name"]
                arguments = str(function.get("arguments") or "")
                state["pending"] += arguments
                # Tool names/IDs may arrive in fragments. Do not publish the
                # item until argument streaming starts; a call with no
                # argument delta is materialized from its complete state at
                # finish().
                if arguments:
                    events.extend(self._ensure_call(state, index))
                if state["item"] is not None and state["pending"]:
                    emitted = state["pending"]
                    state["pending"] = ""
                    state["arguments"] += emitted
                    state["item"]["arguments"] = state["arguments"]
                    events.append(self._event(
                        "response.function_call_arguments.delta",
                        item_id=state["item"]["id"],
                        output_index=self.output.index(state["item"]),
                        delta=emitted))
        return events

    def finish(self, *, completed: bool, error_code: str = "",
               error_message: str = "") -> list[dict[str, Any]]:
        events = self.start()
        if not completed:
            response = self._response("failed")
            response["error"] = {
                "code": error_code or "upstream_stream_truncated",
                "message": error_message or "Kimi stream ended before [DONE].",
            }
            events.append(self._event("response.failed", response=response))
            return events
        for index, state in sorted(self._calls.items()):
            events.extend(self._ensure_call(state, index))
            if state.get("item") is not None and state.get("pending"):
                emitted = state["pending"]
                state["pending"] = ""
                state["arguments"] += emitted
                state["item"]["arguments"] = state["arguments"]
                events.append(self._event(
                    "response.function_call_arguments.delta",
                    item_id=state["item"]["id"],
                    output_index=self.output.index(state["item"]),
                    delta=emitted))
        if any(not str(state.get("name") or "")
               for state in self._calls.values()):
            return self.finish(
                completed=False, error_code="invalid_function_call",
                error_message="Kimi emitted a function call without a name.")
        if self._finish_reason not in {
                "stop", "tool_calls", "length", "content_filter"}:
            return self.finish(
                completed=False, error_code="invalid_finish_reason",
                error_message=(
                    "Kimi stream ended without a supported finish reason."))
        if self._finish_reason == "tool_calls" and not self._calls:
            return self.finish(
                completed=False, error_code="invalid_function_call",
                error_message=(
                    "Kimi ended for tool calls without emitting a call."))
        if self._finish_reason == "stop" and not self.output:
            return self.finish(
                completed=False, error_code="empty_upstream_response",
                error_message=(
                    "Kimi completed without content, reasoning, or a call."))
        for item in self.output:
            index = self.output.index(item)
            if item["type"] == "message":
                text = item.pop("_text", "")
                item["content"] = [{"type": "output_text", "text": text,
                                   "annotations": []}]
                events.append(self._event(
                    "response.output_text.done", item_id=item["id"],
                    output_index=index, content_index=0, text=text))
                events.append(self._event(
                    "response.content_part.done", output_index=index,
                    content_index=0, part=item["content"][0]))
            elif item["type"] == "reasoning":
                text = item.pop("_text", "")
                item["summary"] = [{"type": "summary_text", "text": text}]
                events.append(self._event(
                    "response.reasoning_summary_text.done", item_id=item["id"],
                    output_index=index, summary_index=0, text=text))
                events.append(self._event(
                    "response.reasoning_summary_part.done", output_index=index,
                    summary_index=0, part=item["summary"][0]))
            elif item["type"] == "function_call":
                events.append(self._event(
                    "response.function_call_arguments.done", item_id=item["id"],
                    output_index=index, arguments=item["arguments"]))
            item["status"] = "completed"
            events.append(self._event(
                "response.output_item.done", output_index=index, item=dict(item)))
        if self._finish_reason == "length":
            response = self._response("incomplete")
            response["incomplete_details"] = {"reason": "max_output_tokens"}
            events.append(self._event("response.incomplete", response=response))
        elif self._finish_reason == "content_filter":
            response = self._response("failed")
            response["error"] = {
                "code": "upstream_content_filter",
                "message": "Kimi stopped the stream because of its content filter.",
            }
            events.append(self._event("response.failed", response=response))
        else:
            events.append(self._event(
                "response.completed", response=self._response("completed")))
        self.saw_done = True
        return events

    def _response(self, status: str) -> dict[str, Any]:
        output = []
        for raw in self.output:
            item = {key: value for key, value in raw.items()
                    if not str(key).startswith("_")}
            if raw.get("type") == "message" and not item.get("content"):
                item["content"] = [{
                    "type": "output_text", "text": str(raw.get("_text") or ""),
                    "annotations": [],
                }]
            elif raw.get("type") == "reasoning" and not item.get("summary"):
                item["summary"] = [{
                    "type": "summary_text", "text": str(raw.get("_text") or ""),
                }]
            output.append(item)
        return {
            "id": self.response_id, "object": "response",
            "created_at": int(time.time()), "status": status,
            "model": self.model, "output": output,
            "usage": self.usage or _usage({}), "error": None,
        }


def sse_line(event: dict[str, Any]) -> bytes:
    return ("event: " + str(event.get("type") or "message") + "\n"
            + "data: " + json.dumps(event, ensure_ascii=False,
                                     separators=(",", ":")) + "\n\n").encode("utf-8")


__all__ = [
    "ChatSSETranslator", "TranslationError", "chat_response_to_responses",
    "responses_request_to_chat", "sse_line",
]
