"""Lightweight magic-byte authority for browser-safe image MIME types.

Responsibility: recognize the PNG/JPEG/GIF/WebP formats accepted by model and
conversation media boundaries without importing image codecs or the model
catalog. Callers own size, base64, provenance, and authorization validation.
"""

from __future__ import annotations


IMAGE_MIME_MAGICS = {
    b"\x89PNG": "image/png",
    b"\xff\xd8": "image/jpeg",
    b"GIF8": "image/gif",
}


def sniff_image_mime(head: bytes) -> str | None:
    """Return the true image MIME from magic bytes, or ``None``."""
    if not isinstance(head, (bytes, bytearray)):
        return None
    for magic, media_type in IMAGE_MIME_MAGICS.items():
        if head.startswith(magic):
            return media_type
    # WebP is a RIFF container. A bare RIFF prefix also identifies WAV/AVI,
    # so both the container and form-type signatures are mandatory.
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


__all__ = ["IMAGE_MIME_MAGICS", "sniff_image_mime"]
