"""Bounded provider-wire capture for durable Request Inspector evidence.

The transport supplies the final protocol-specific request document and raw
response bytes. This owner scrubs secret-shaped values, keeps temporary
response growth bounded, compresses each part, and commits through semantic
storage operations without ever failing the model request.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import hashlib
import os
import shutil
import tempfile
from typing import Any
import uuid
import zlib

import orjson

from lib.log import get_logger
from lib.log_redaction import redact_text, sensitive_field_name
from lib.raw_archive_contract import RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES


logger = get_logger(__name__)

RAW_ARCHIVE_STORED_LIMIT_BYTES = 16 * 1024 * 1024
RAW_ARCHIVE_CAPTURE_LIMIT_BYTES = 64 * 1024 * 1024
_RAW_ARCHIVE_FALLBACK_BUDGET_MIB = 256
_RAW_ARCHIVE_MAX_BUDGET_MIB = 4096


def _redact_value(value: Any, *, field_name: str = "") -> tuple[Any, bool]:
    if sensitive_field_name(field_name):
        return "<redacted>", True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        redacted = redact_text(value)
        return redacted, redacted != value
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
        redacted = redact_text(text)
        return redacted, True
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        changed = False
        for raw_key, item in value.items():
            key = str(raw_key)
            projected, item_changed = _redact_value(item, field_name=key)
            result[key] = projected
            changed = changed or item_changed
        return result, changed
    if isinstance(value, Sequence):
        result = []
        changed = False
        for item in value:
            projected, item_changed = _redact_value(item)
            result.append(projected)
            changed = changed or item_changed
        return result, changed
    return redact_text(value), True


def _json_bytes(value: Any) -> tuple[bytes, bool]:
    projected, redacted = _redact_value(value)
    return orjson.dumps(projected, option=orjson.OPT_SORT_KEYS), redacted


def _compressed_prefix(raw: bytes, budget: int) -> bytes:
    """Return the longest deterministic prefix whose zlib body fits budget."""
    if budget <= 0 or not raw:
        return zlib.compress(b"", level=1) if budget >= 8 else b""
    encoded = zlib.compress(raw, level=1)
    if len(encoded) <= budget:
        return encoded
    low = 0
    high = len(raw)
    best = zlib.compress(b"", level=1)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = zlib.compress(raw[:midpoint], level=1)
        if len(candidate) <= budget:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _compressed_parts(request: bytes, response: bytes) -> tuple[bytes, bytes, bool]:
    request_blob = zlib.compress(request, level=1)
    response_blob = zlib.compress(response, level=1)
    if len(request_blob) + len(response_blob) <= RAW_ARCHIVE_STORED_LIMIT_BYTES:
        return request_blob, response_blob, False
    request_budget = RAW_ARCHIVE_STORED_LIMIT_BYTES // 2
    request_blob = _compressed_prefix(request, request_budget)
    response_blob = _compressed_prefix(
        response, RAW_ARCHIVE_STORED_LIMIT_BYTES - len(request_blob)
    )
    return request_blob, response_blob, True


def _configured_bytes(name: str, fallback_mib: int, maximum_mib: int) -> int:
    try:
        mib = int(os.environ.get(name, "") or fallback_mib)
    except (TypeError, ValueError, OverflowError):
        mib = fallback_mib
    return max(1, min(maximum_mib, mib)) * 1024 * 1024


def _configured_min_free_bytes() -> int:
    fallback = 256 * 1024 * 1024
    try:
        value = int(os.environ.get("TOFU_STORAGE_MIN_FREE_BYTES", "") or fallback)
    except (TypeError, ValueError, OverflowError):
        value = fallback
    return max(0, min(1024 * 1024 * 1024 * 1024, value))


class RawArchiveCapture:
    """One provider transport attempt with bounded temporary response state."""

    def __init__(
        self,
        context: Mapping[str, Any],
        wire_request: Mapping[str, Any],
        *,
        transport_attempt: int,
        trace_id: str,
    ) -> None:
        self.context = dict(context)
        self.transport_attempt = max(0, int(transport_attempt))
        self.trace_id = str(trace_id or "")
        self.archive_id = "raw-" + uuid.uuid4().hex
        self.request_bytes, self._request_redacted = _json_bytes(wire_request)
        self._response_file = tempfile.SpooledTemporaryFile(
            max_size=1024 * 1024, mode="w+b"
        )
        self._response_total_bytes = 0
        self._response_retained_bytes = 0
        self._closed = False

    @classmethod
    def create(
        cls,
        context: Any,
        wire_request: Mapping[str, Any],
        *,
        transport_attempt: int,
        trace_id: str,
    ) -> "RawArchiveCapture | None":
        if not isinstance(context, Mapping):
            return None
        required = (
            "userId", "conversationId", "turnId", "attemptId", "taskId",
            "roundNum",
        )
        if any(not context.get(key) for key in required):
            return None
        try:
            return cls(
                context,
                wire_request,
                transport_attempt=transport_attempt,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.debug("Raw archive capture initialization failed: %s", exc)
            return None

    def append_response(self, chunk: bytes) -> None:
        if self._closed or not isinstance(chunk, (bytes, bytearray, memoryview)):
            return
        raw = bytes(chunk)
        self._response_total_bytes += len(raw)
        remaining = RAW_ARCHIVE_CAPTURE_LIMIT_BYTES - self._response_retained_bytes
        if remaining > 0:
            retained = raw[:remaining]
            self._response_file.write(retained)
            self._response_retained_bytes += len(retained)

    def discard(self) -> None:
        """Release a prepared capture whose request was never sent."""
        if self._closed:
            return
        self._closed = True
        self._response_file.close()

    def commit(
        self,
        *,
        response_complete: bool,
        status_code: int | None = None,
    ) -> dict[str, Any] | None:
        if self._closed:
            return None
        self._closed = True
        try:
            self._response_file.seek(0)
            retained_response = self._response_file.read()
            response_text = retained_response.decode("utf-8", errors="replace")
            redacted_response_text = redact_text(response_text)
            response_redacted = redacted_response_text != response_text
            response_bytes = redacted_response_text.encode("utf-8")
            request_blob, response_blob, compressed_truncated = _compressed_parts(
                self.request_bytes, response_bytes
            )
            capture_truncated = (
                self._response_total_bytes > self._response_retained_bytes
            )
            integrity = "complete"
            truncation_reason = ""
            if not response_complete:
                integrity = "partial"
                truncation_reason = "transport_interrupted"
            if capture_truncated or compressed_truncated:
                integrity = "partial"
                truncation_reason = "attempt_limit"
            if (
                integrity == "complete"
                and (self._request_redacted or response_redacted)
            ):
                integrity = "partial"
                truncation_reason = "secret_scrubbed"

            request_sha256 = hashlib.sha256(self.request_bytes).hexdigest()
            response_sha256 = hashlib.sha256(response_bytes).hexdigest()
            combined_sha256 = hashlib.sha256(
                bytes.fromhex(request_sha256) + bytes.fromhex(response_sha256)
            ).hexdigest()
            budget_bytes = _configured_bytes(
                "TOFU_RAW_ARCHIVE_BUDGET_MIB",
                _RAW_ARCHIVE_FALLBACK_BUDGET_MIB,
                _RAW_ARCHIVE_MAX_BUDGET_MIB,
            )
            min_free_bytes = _configured_min_free_bytes()
            available_free_bytes = 0
            try:
                from lib.runtime_paths import data_root

                available_free_bytes = min(
                    int(shutil.disk_usage(data_root()).free),
                    RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES,
                )
            except Exception as exc:
                logger.debug("Raw archive free-space probe failed: %s", exc)

            from lib.storage import get_storage_client

            return get_storage_client(write=True).command(
                "raw_archive.put",
                {
                    "archive_id": self.archive_id,
                    "user_id": int(self.context["userId"]),
                    "conversation_id": str(self.context["conversationId"]),
                    "turn_id": str(self.context["turnId"]),
                    "attempt_id": str(self.context["attemptId"]),
                    "task_id": str(self.context["taskId"]),
                    "round_num": int(self.context["roundNum"]),
                    "transport_attempt": self.transport_attempt,
                    "request_blob_b64": base64.b64encode(request_blob).decode("ascii"),
                    "response_blob_b64": base64.b64encode(response_blob).decode("ascii"),
                    "request_bytes": len(self.request_bytes),
                    "response_bytes": self._response_total_bytes,
                    "request_sha256": request_sha256,
                    "response_sha256": response_sha256,
                    "integrity": integrity,
                    "truncation_reason": truncation_reason,
                    "summary": {
                        "text": "Provider request/response",
                        "model": str(self.context.get("model") or ""),
                        "traceId": self.trace_id,
                        "statusCode": status_code,
                        "combinedSha256": combined_sha256,
                    },
                    "budget_bytes": budget_bytes,
                    "min_free_bytes": min_free_bytes,
                    "available_free_bytes": available_free_bytes,
                },
                "raw-archive:" + self.archive_id,
                deadline=60,
            )
        except Exception as exc:
            # Request Inspector evidence must never replace a provider result.
            logger.warning("Raw archive commit failed: %s", exc)
            return None
        finally:
            self._response_file.close()


__all__ = [
    "RAW_ARCHIVE_CAPTURE_LIMIT_BYTES",
    "RAW_ARCHIVE_STORED_LIMIT_BYTES",
    "RawArchiveCapture",
]
