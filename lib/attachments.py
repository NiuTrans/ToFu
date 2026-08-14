"""lib/attachments.py — Centralized re-access of uploaded chat attachments.

Single entry point (:func:`resolve_attachment`) that turns a *stable
reference* to a user-uploaded file back into its ORIGINAL bytes (images) or
extracted text (PDF / TXT / office docs), regardless of how the attachment
was stored:

  - **Images** live on disk under ``uploads_root()/images`` and are referenced
    from the DB by ``/api/images/<filename>`` URLs. They may also arrive as a
    ``data:`` URI or a remote ``http(s)://`` URL.
  - **PDF / TXT / doc** uploads do NOT keep the original file — the chat send
    path extracts text into the message row's ``pdfTexts`` list. So their only
    re-accessible form is that stored text, located by scanning the
    conversation messages for the matching entry.

Why this module exists (root cause it fixes): tools that want to re-read an
uploaded file (e.g. ``inspect_image`` zooming into a photo) previously only
accepted a local *filesystem path*. A chat-uploaded image has no such path the
model can name — it only ever sees an inline ``image_url`` block — so the model
would invent a bogus path (``/dev/null``) and the tool failed with
``File not found``. This module is the ONE place that maps a stable reference
(the one the backend computes and shows the model) to the real source.

Design invariants (see the project charter's front/back-contract decision):
  - The reference is a backend-computed FACT with a stable id; the model never
    fabricates a path. ``inspect_image`` and any future read-back tool call
    THIS resolver — they do not each re-implement resolution.
  - Source-first from disk / DB. For an image we read the on-disk file (or the
    DB base64), never the possibly-compacted-away inline data URI in the live
    context window.

This module is deliberately dependency-free of the ``executor_image`` package
(the image-gen path now delegates its ``_resolve_source_image`` here), so the
import graph stays acyclic.
"""

from __future__ import annotations

import base64
import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)

# Reference prefixes that denote an image the resolver can fetch directly
# (no conversation context needed).
_DIRECT_IMAGE_PREFIXES = ('/api/images/', 'data:', 'http://', 'https://')

# Stable-id prefix for a text attachment (PDF / TXT / doc). The id is a content
# hash so the SAME entry always maps to the SAME ref across turns/reloads, and
# the resolver can locate it by re-hashing candidates in the message list.
_TEXT_REF_PREFIX = 'att_txt_'

_IMG_EXT_MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
}

_MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024

# Marker for an uploaded image URL. A reverse proxy (VS Code / code-server
# ``/proxy/<port>/``) prepends a base path, so the stored URL can be
# ``/proxy/15002/api/images/<f>`` rather than a bare ``/api/images/<f>``. We
# recognise the marker ANYWHERE in the string (``.find`` semantics, mirroring
# lib/llm/body/_images.py) and canonicalize to the ``/api/images/...`` tail so
# both freshly-uploaded and already-proxy-polluted DB rows resolve.
_API_IMAGES_MARKER = '/api/images/'


def canonical_image_ref(ref: str) -> str:
    """Return the canonical ``/api/images/<f>`` tail of an uploaded-image URL.

    Tolerates a reverse-proxy base-path prefix (``/proxy/<port>/api/images/x``
    → ``/api/images/x``). Returns ``''`` when *ref* is not an uploaded-image
    URL, so callers can use a truthy check as the recognition predicate.
    """
    if not ref or not isinstance(ref, str):
        return ''
    idx = ref.find(_API_IMAGES_MARKER)
    if idx < 0:
        return ''
    return ref[idx:]


def _images_dir() -> str:
    """Absolute path to the uploads/images dir under the resolved runtime base.

    Uploaded images are USER STATE referenced from the DB by ``/api/images/``
    URLs, so they co-locate with the DB via ``runtime_paths.uploads_root()``.
    Falls back to the in-tree ``<repo>/uploads/images`` if runtime_paths is
    somehow unavailable (byte-identical in the default single-box layout).
    """
    try:
        from lib.runtime_paths import uploads_root
        return os.path.join(uploads_root(), 'images')
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[Attachments] uploads_root() unavailable, using in-tree: %s', e)
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(_root, 'uploads', 'images')


def attachment_text_ref(pdf: dict) -> str:
    """Compute the stable reference id for a stored text attachment.

    The id is a content hash of ``name`` + extracted ``text`` so the same
    upload always yields the same ref and the resolver can re-derive it while
    scanning message rows. Used both when EMITTING the ref into the model's
    user message and when RESOLVING it back.
    """
    name = str(pdf.get('name', ''))
    text = str(pdf.get('text', ''))
    digest = hashlib.sha1(f'{name}\x00{text}'.encode('utf-8', 'replace')).hexdigest()
    return f'{_TEXT_REF_PREFIX}{digest[:12]}'


def is_attachment_ref(ref: str) -> bool:
    """True if *ref* is any reference this module knows how to resolve."""
    if not ref or not isinstance(ref, str):
        return False
    # An uploaded image may arrive with a reverse-proxy prefix
    # (``/proxy/<port>/api/images/<f>``) — recognise the ``/api/images/``
    # marker anywhere, not only at the start.
    if _API_IMAGES_MARKER in ref:
        return True
    return ref.startswith(_DIRECT_IMAGE_PREFIXES) or ref.startswith(_TEXT_REF_PREFIX)


def _resolve_image_bytes(ref: str) -> dict | None:
    """Resolve an image reference to ``{kind, image_b64, mime_type, raw}``.

    Handles ``data:`` URIs, ``/api/images/<f>`` disk reads, remote
    ``http(s)://`` downloads, and local filesystem paths. Returns ``None`` on
    any failure (always logged).
    """
    # ── Data URI ──
    if ref.startswith('data:'):
        try:
            header, b64_part = ref.split(',', 1)
            mime_type = header.split(':')[1].split(';')[0]
            if not mime_type.lower().startswith('image/'):
                raise ValueError('data URI is not an image')
            # Reject before decoding: a 500 MiB base64 string otherwise creates
            # another hundreds-of-megabytes allocation on this hot tool path.
            max_encoded = ((_MAX_REMOTE_IMAGE_BYTES + 2) // 3) * 4 + 8
            if len(b64_part) > max_encoded:
                raise ValueError('inline image exceeds 10 MiB decoded limit')
            raw = base64.b64decode(b64_part)
            if len(raw) > _MAX_REMOTE_IMAGE_BYTES:
                raise ValueError('inline image exceeds 10 MiB decoded limit')
            return {'kind': 'image', 'image_b64': b64_part, 'mime_type': mime_type, 'raw': raw}
        except (ValueError, IndexError) as e:
            logger.warning('[Attachments] Failed to parse data URI: %s', e)
            return None

    # ── Local uploaded image: /api/images/xxx.png → read from disk ──
    # Tolerate a reverse-proxy prefix (``/proxy/<port>/api/images/<f>``) by
    # canonicalizing to the ``/api/images/...`` tail first.
    _canon = canonical_image_ref(ref)
    if _canon:
        ref = _canon
    if ref.startswith('/api/images/'):
        filename = os.path.basename(ref)
        filepath = os.path.join(_images_dir(), filename)
        try:
            with open(filepath, 'rb') as f:
                raw = f.read()
        except Exception as e:
            logger.warning('[Attachments] Failed to read local image %s: %s', filepath, e)
            return None
        ext = os.path.splitext(filename)[1].lower()
        mime_type = _IMG_EXT_MIME.get(ext, 'image/png')
        return {'kind': 'image', 'image_b64': base64.b64encode(raw).decode('ascii'),
                'mime_type': mime_type, 'raw': raw}

    # ── Remote URL ──
    if ref.startswith(('http://', 'https://')):
        try:
            from lib.safe_fetch import fetch_public_bytes
            raw, content_type, _final_url = fetch_public_bytes(
                ref,
                max_bytes=_MAX_REMOTE_IMAGE_BYTES,
                timeout=30,
                max_redirects=3,
                allow_hosts_env='TOFU_IMAGE_FETCH_ALLOW_HOSTS',
            )
        except Exception as e:
            logger.warning('[Attachments] Failed to download image %.80s: %s', ref[:80], e)
            return None
        mime_type = content_type if content_type.startswith('image/') else 'image/png'
        return {'kind': 'image', 'image_b64': base64.b64encode(raw).decode('ascii'),
                'mime_type': mime_type, 'raw': raw}

    # ── Local filesystem path (absolute or relative to app root) ──
    filepath = ref
    if not os.path.isabs(filepath):
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        filepath = os.path.join(_root, filepath)
    if os.path.isfile(filepath):
        try:
            with open(filepath, 'rb') as f:
                raw = f.read()
        except Exception as e:
            logger.warning('[Attachments] Failed to read local file %.80s: %s', ref[:80], e)
            return None
        ext = os.path.splitext(filepath)[1].lower()
        mime_type = _IMG_EXT_MIME.get(ext, 'image/png')
        logger.info('[Attachments] Resolved local file path: %.80s (%d KB)', ref[:80], len(raw) // 1024)
        return {'kind': 'image', 'image_b64': base64.b64encode(raw).decode('ascii'),
                'mime_type': mime_type, 'raw': raw}

    logger.warning('[Attachments] Unrecognized image ref or file not found: %.80s', ref[:80])
    return None


def _resolve_text_ref(ref: str, messages: list | None) -> dict | None:
    """Resolve a text-attachment ref to ``{kind:'text', text, name}``.

    Scans the conversation ``messages`` for a ``pdfTexts`` entry whose content
    hash matches *ref*. Returns ``None`` if not found (logged at debug).
    """
    if not messages:
        logger.debug('[Attachments] text ref %s given but no messages to search', ref)
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for pdf in (msg.get('pdfTexts') or []):
            if not isinstance(pdf, dict):
                continue
            if attachment_text_ref(pdf) == ref:
                return {'kind': 'text', 'text': str(pdf.get('text', '')),
                        'name': str(pdf.get('name', 'document'))}
    logger.debug('[Attachments] text ref %s not found among message attachments', ref)
    return None


def resolve_attachment(ref: str, *, messages: list | None = None) -> dict | None:
    """Resolve a stable attachment reference to its original source.

    This is the single re-access entry point for uploaded files. It maps the
    backend-computed reference the model was shown back to the real bytes/text.

    Args:
        ref: The reference. One of:
            - ``/api/images/<filename>`` — uploaded image, read from disk.
            - ``data:<mime>;base64,...`` — inline image data URI.
            - ``http(s)://...`` — remote image URL (downloaded).
            - an absolute/relative local image path.
            - ``att_txt_<hash>`` — a PDF/TXT/doc text attachment, located by
              scanning *messages* for the matching stored text.
        messages: Conversation messages (required only to resolve a text
            ``att_txt_*`` ref; ignored for image refs).

    Returns:
        On success one of:
            - ``{'kind': 'image', 'image_b64': str, 'mime_type': str, 'raw': bytes}``
            - ``{'kind': 'text',  'text': str, 'name': str}``
        or ``None`` on any failure (always logged).
    """
    if not ref or not isinstance(ref, str):
        return None
    if ref.startswith(_TEXT_REF_PREFIX):
        return _resolve_text_ref(ref, messages)
    return _resolve_image_bytes(ref)
