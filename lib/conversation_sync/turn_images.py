"""Application-boundary decoding for lazy historical Turn images.

The storage sidecar returns one owner/revision-fenced encoded payload. This
module enforces the byte budget, verifies base64 and sniffs the real image MIME
before an HTTP adapter may expose bytes to a browser.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any

from lib.image_mime import sniff_image_mime
from lib.storage.errors import StorageError
from lib.turn_image_transport import (
    MAX_TURN_IMAGE_BYTES,
    MAX_TURN_IMAGE_ENCODED_CHARS,
)


class ConversationTurnImageNotFound(LookupError):
    pass


class ConversationTurnImageStale(RuntimeError):
    def __init__(self, current_projection_revision: int) -> None:
        super().__init__("Conversation Turn image revision is stale")
        self.current_projection_revision = current_projection_revision


@dataclass(frozen=True, slots=True)
class ConversationTurnImage:
    content: bytes
    media_type: str
    digest: str


def turn_image_owner_scope(user_id: int, conversation_id: str) -> str:
    """Partition private browser caches without making scope a credential."""
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ValueError("Invalid Turn image owner scope")
    owner = int(user_id)
    if owner < 1 or not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("Invalid Turn image owner scope")
    digest = hashlib.blake2s(digest_size=12, person=b"tofuimg")
    digest.update(str(owner).encode("ascii"))
    digest.update(b"\0")
    digest.update(conversation_id.encode("utf-8"))
    return digest.hexdigest()


def decode_stored_turn_image(value: Any) -> ConversationTurnImage:
    """Validate one sidecar record as durable image evidence."""
    if not isinstance(value, str) or not value:
        raise StorageError(
            "database_integrity",
            "Stored Turn image payload must be non-empty base64",
        )
    if len(value) > MAX_TURN_IMAGE_ENCODED_CHARS:
        raise StorageError(
            "database_integrity",
            "Stored Turn image exceeds its byte budget",
        )
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StorageError(
            "database_integrity",
            "Stored Turn image base64 is corrupt",
        ) from exc
    if not content or len(content) > MAX_TURN_IMAGE_BYTES:
        raise StorageError(
            "database_integrity",
            "Stored Turn image exceeds its byte budget",
        )
    media_type = sniff_image_mime(content)
    if media_type is None:
        raise StorageError(
            "database_integrity",
            "Stored Turn image has an unsupported binary format",
        )
    return ConversationTurnImage(
        content=content,
        media_type=media_type,
        digest=hashlib.sha256(content).hexdigest(),
    )


__all__ = [
    "ConversationTurnImage",
    "ConversationTurnImageNotFound",
    "ConversationTurnImageStale",
    "decode_stored_turn_image",
    "turn_image_owner_scope",
]
