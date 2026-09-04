"""Lazy historical Turn images stay bounded, owner-scoped, and byte-true."""

from __future__ import annotations

import base64
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _ImageRepository:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def turn_image(
        self,
        conversation_id,
        turn_id,
        user_id,
        *,
        projection_revision,
        image_index,
    ):
        self.calls.append((
            conversation_id,
            turn_id,
            user_id,
            projection_revision,
            image_index,
        ))
        return self.result


def test_turn_image_service_decodes_and_sniffs_the_true_media_type():
    from lib.conversation_sync.service import ConversationSyncService

    raw = b"\x89PNG\r\n\x1a\n" + (b"payload" * 20)
    repository = _ImageRepository({
        "stale": False,
        "mediaType": "image/jpeg",
        "base64": base64.b64encode(raw).decode("ascii"),
    })

    image = ConversationSyncService(repository).turn_image(
        "conv-a",
        "turn-a",
        7,
        projection_revision=4,
        image_index=2,
    )

    assert image.content == raw
    assert image.media_type == "image/png"
    assert len(image.digest) == 64
    assert repository.calls == [("conv-a", "turn-a", 7, 4, 2)]


def test_turn_image_service_preserves_not_found_and_stale_outcomes():
    from lib.conversation_sync.service import ConversationSyncService
    from lib.conversation_sync.turn_images import (
        ConversationTurnImageNotFound,
        ConversationTurnImageStale,
    )

    with pytest.raises(ConversationTurnImageNotFound):
        ConversationSyncService(_ImageRepository(None)).turn_image(
            "conv-a", "turn-a", 7,
            projection_revision=4, image_index=0,
        )
    with pytest.raises(ConversationTurnImageStale) as caught:
        ConversationSyncService(_ImageRepository({
            "stale": True,
            "projectionRevision": 9,
        })).turn_image(
            "conv-a", "turn-a", 7,
            projection_revision=4, image_index=0,
        )
    assert caught.value.current_projection_revision == 9


@pytest.mark.parametrize(
    "encoded",
    [
        "not base64!",
        base64.b64encode(b"plain text, not an image").decode("ascii"),
    ],
)
def test_turn_image_corruption_is_a_stable_storage_integrity_error(encoded):
    from lib.conversation_sync.turn_images import decode_stored_turn_image
    from lib.storage.errors import StorageError

    with pytest.raises(StorageError) as caught:
        decode_stored_turn_image(encoded)

    assert caught.value.code == "database_integrity"


def test_turn_image_owner_scope_is_stable_but_owner_partitioned():
    from lib.conversation_sync.turn_images import turn_image_owner_scope

    first = turn_image_owner_scope(7, "conv-a")
    assert first == turn_image_owner_scope(7, "conv-a")
    assert first != turn_image_owner_scope(8, "conv-a")
    assert first != turn_image_owner_scope(7, "conv-b")
    assert len(first) == 24


def test_legacy_preview_only_payload_is_recoverable_without_decoding():
    from lib.turn_image_transport import legacy_turn_image_payload

    encoded = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + (b"preview-only" * 100)
    ).decode("ascii")

    payload = legacy_turn_image_payload({
        "preview": f"data:image/png;base64,{encoded}",
    })

    assert payload is not None
    assert payload.encoded_source.startswith("data:image/png;base64,")
    assert payload.encoded_start == len("data:image/png;base64,")
    assert payload.encoded_length == len(encoded)
    assert payload.base64_data == encoded
    assert payload.media_type == "image/png"


def test_legacy_turn_image_transport_has_fixed_personal_resource_ceilings():
    from lib.turn_image_transport import (
        MAX_TURN_IMAGES,
        MAX_TURN_IMAGE_BYTES,
        MAX_TURN_IMAGE_ENCODED_CHARS,
        MIN_LAZY_TURN_IMAGE_ENCODED_CHARS,
    )

    assert MAX_TURN_IMAGES == 20
    assert MAX_TURN_IMAGE_BYTES == 8 * 1024 * 1024
    assert MAX_TURN_IMAGE_ENCODED_CHARS == 11_184_812
    assert MIN_LAZY_TURN_IMAGE_ENCODED_CHARS == 1024


def test_turn_image_decoder_does_not_eagerly_load_the_model_stack():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import lib.conversation_sync.turn_images; "
            "assert 'lib.model_info' not in sys.modules; "
            "assert 'lib.llm.body' not in sys.modules",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
