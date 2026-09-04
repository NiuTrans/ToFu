"""Bounded compatibility transport for historical inline Turn images.

Responsibility: identify one recoverable PNG/JPEG/GIF/WebP payload without
decoding it. Snapshot projection uses the descriptor to publish a lazy URL;
the storage query uses the same authority to return exactly one encoded image.
Persistent Turn projections and modern attachment references are untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


MAX_TURN_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TURN_IMAGE_ENCODED_CHARS = ((MAX_TURN_IMAGE_BYTES + 2) // 3) * 4
MIN_LAZY_TURN_IMAGE_ENCODED_CHARS = 1024
MAX_TURN_IMAGES = 20

_ALLOWED_IMAGE_MEDIA_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})
_DATA_IMAGE_PREFIX = "data:"
_BASE64_MARKER = ";base64,"


@dataclass(frozen=True, slots=True)
class LegacyTurnImagePayload:
    """One bounded encoded payload and its untrusted declared media type."""

    encoded_source: str
    encoded_start: int
    media_type: str

    @property
    def encoded_length(self) -> int:
        return len(self.encoded_source) - self.encoded_start

    @property
    def base64_data(self) -> str:
        if self.encoded_start == 0:
            return self.encoded_source
        return self.encoded_source[self.encoded_start:]


def _data_uri_payload(value: Any) -> LegacyTurnImagePayload | None:
    if not isinstance(value, str) or not value.startswith(_DATA_IMAGE_PREFIX):
        return None
    marker_index = value.find(_BASE64_MARKER, len(_DATA_IMAGE_PREFIX))
    if marker_index < 0:
        return None
    media_type = value[len(_DATA_IMAGE_PREFIX):marker_index].strip().lower()
    if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
        return None
    payload_start = marker_index + len(_BASE64_MARKER)
    payload_length = len(value) - payload_start
    if (
        not 1 <= payload_length <= MAX_TURN_IMAGE_ENCODED_CHARS
        or payload_length % 4 != 0
    ):
        return None
    return LegacyTurnImagePayload(value, payload_start, media_type)


def legacy_turn_image_payload(
    image: Any,
) -> LegacyTurnImagePayload | None:
    """Return a bounded historical inline payload, or ``None`` fail-closed.

    Historical records usually contain both ``base64`` and a duplicate data
    URI ``preview``. Prefer the standalone string so identifying an image does
    not copy the multi-megabyte preview. Byte validity and MIME truth are
    checked only at the binary application boundary, where they are paid once
    on an actual image request rather than on every conversation snapshot.
    """
    if not isinstance(image, Mapping):
        return None
    encoded = image.get("base64")
    if isinstance(encoded, str) and encoded:
        if (
            len(encoded) > MAX_TURN_IMAGE_ENCODED_CHARS
            or len(encoded) % 4 != 0
        ):
            return None
        media_type = str(
            image.get("mediaType") or image.get("mimeType") or ""
        ).strip().lower()
        if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
            preview_payload = _data_uri_payload(image.get("preview"))
            if preview_payload is None:
                return None
            media_type = preview_payload.media_type
        return LegacyTurnImagePayload(encoded, 0, media_type)
    return _data_uri_payload(image.get("preview"))


__all__ = [
    "LegacyTurnImagePayload",
    "MAX_TURN_IMAGES",
    "MAX_TURN_IMAGE_BYTES",
    "MAX_TURN_IMAGE_ENCODED_CHARS",
    "MIN_LAZY_TURN_IMAGE_ENCODED_CHARS",
    "legacy_turn_image_payload",
]
