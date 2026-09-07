"""tests/test_push_frames.py — Push-channel frame contract conformance.

Owner-scoped push-hub frames (``conv_changed`` / ``conv_deleted`` /
``folders_changed`` / ``codex.reset_offer.updated``) historically existed
only as prose in ``docs/EVENTS.md``; the ``PUSH_FRAME_SPECS`` registry in
``lib/agent_core/events.py`` makes them machine-readable, and this suite is
its enforcement half:

  A. **Registry hygiene** — every push spec keeps schema ↔ prose in EXACT
     key sync, uses only the closed kind vocabulary, and never duplicates a
     field name.
  B. **Construction gate** — ``build_push_frame`` raises
     :class:`EventContractError` on an undeclared field, a missing required
     field, or a type mismatch (strict is the default under pytest);
     ``TOFU_EVENT_SCHEMA=warn`` degrades gracefully.
  C. **REAL emitter conformance** — the three production emitters
     (``notify_conv_changed``, ``_notify_folders_changed``,
     ``_publish_codex_usage_reset_update``) must publish only conforming
     frames with the exact documented wire shape.
  D. **Frontend narrowing** — ``frame-identity.ts`` guards accept declared
     frames and reject foreign payloads (node bundle of the real TS owner).

If this fails
-------------
You changed a declared push frame's shape. Update the ``FieldSpec`` tuple
AND the prose ``fields`` map in ``lib/agent_core/events.py`` together (the
sync test demands both), regenerate ``event-contract.generated.ts``, and on
a *breaking* change bump ``PUSH_FRAME_CONTRACT_VERSION``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import subprocess

import pytest

from lib.agent_core.events import (
    FIELD_KINDS,
    EventContractError,
    all_push_frame_specs,
    build_push_frame,
    get_push_frame_spec,
    validate_push_frame,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FRAME_IDENTITY = ROOT / "frontend/src/core/frame-identity.ts"


# ═══════════════════════════════════════════════════════════════════
#  A. Registry hygiene
# ═══════════════════════════════════════════════════════════════════

def test_push_registry_declares_the_notify_and_oauth_seam():
    specs = {spec.type: spec for spec in all_push_frame_specs()}
    assert set(specs) == {
        'conv_changed', 'conv_deleted', 'folders_changed',
        'codex.reset_offer.updated',
    }
    assert specs['conv_changed'].channel == 'notify'
    assert specs['conv_deleted'].channel == 'notify'
    assert specs['folders_changed'].channel == 'notify'
    assert specs['codex.reset_offer.updated'].channel == 'oauth'


def test_push_schema_field_names_match_prose_fields_exactly():
    for spec in all_push_frame_specs():
        assert spec.schema is not None, spec.type
        schema_names = {f.name for f in spec.schema}
        prose_names = set(spec.fields)
        assert schema_names == prose_names, (
            f'{spec.type}: schema/prose drift — '
            f'schema-only: {sorted(schema_names - prose_names)}, '
            f'prose-only: {sorted(prose_names - schema_names)}')


def test_push_schema_uses_only_the_closed_kind_vocabulary():
    for spec in all_push_frame_specs():
        for field_spec in spec.schema:
            alternatives = {a.strip() for a in field_spec.kind.split('|')}
            assert alternatives and alternatives <= FIELD_KINDS, (
                f'{spec.type}.{field_spec.name}: unknown kind(s) '
                f'{sorted(alternatives - FIELD_KINDS)}')


def test_push_schema_has_no_duplicate_field_names():
    for spec in all_push_frame_specs():
        names = [f.name for f in spec.schema]
        assert len(names) == len(set(names)), f'{spec.type}: duplicate names'


# ═══════════════════════════════════════════════════════════════════
#  B. Construction gate
# ═══════════════════════════════════════════════════════════════════

def test_build_push_frame_byte_shape_and_valid_minimal():
    frame = build_push_frame('conv_changed', convId='c1', userId=7)
    assert frame == {'type': 'conv_changed', 'convId': 'c1', 'userId': 7}
    assert list(frame) == ['type', 'convId', 'userId']
    assert validate_push_frame(frame) == ()


def test_build_push_frame_optional_field_accepted():
    frame = build_push_frame('conv_changed', convId='c1', userId=7, rev=42)
    assert frame['rev'] == 42
    assert validate_push_frame(frame) == ()


def test_build_push_frame_undeclared_field_raises():
    with pytest.raises(EventContractError, match='undeclared field'):
        build_push_frame('conv_deleted', convId='c1', userId=7, rev=1)


def test_build_push_frame_missing_required_raises():
    with pytest.raises(EventContractError, match='missing required field'):
        build_push_frame('conv_changed', userId=7)


def test_build_push_frame_type_mismatch_raises():
    with pytest.raises(EventContractError, match='expects int'):
        build_push_frame('conv_changed', convId='c1', userId='7')


def test_build_push_frame_warn_mode_logs_instead_of_raising(
        monkeypatch, caplog):
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'warn')
    with caplog.at_level(logging.WARNING):
        frame = build_push_frame('conv_changed', convId='c1', userId='7')
    # The frame still crosses the wire in production — a contract nit must
    # never break a wake hint — but the violation is reported.
    assert frame['userId'] == '7'
    assert any('wire contract violation' in r.message for r in caplog.records)


def test_validate_push_frame_passes_through_undeclared_types():
    # Self-contained producer↔consumer pairs (timer_changed, cookie capture,
    # project hints) stay permissive until they migrate into the registry.
    assert validate_push_frame({'type': 'timer_changed', 'change': 'x'}) == ()
    assert validate_push_frame('not a dict') == (
        'frame is str, not a dict',)
    assert get_push_frame_spec('timer_changed') is None


# ═══════════════════════════════════════════════════════════════════
#  C. Real emitter conformance
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def captured_push(monkeypatch):
    """Capture every frame the lazy ``push_event`` import would publish."""
    sent = []

    def _fake(channel, task_id, payload, *, user_id):
        sent.append({
            'channel': channel, 'task_id': task_id,
            'payload': payload, 'user_id': user_id,
        })

    monkeypatch.setattr('lib.agent_core.push.push_event', _fake)
    return sent


def test_notify_conv_changed_emits_conforming_frames(captured_push):
    from lib.conversations.change_notifications import notify_conv_changed

    notify_conv_changed('conv-1', rev=3, user_id=7)
    notify_conv_changed('conv-2', deleted=True, user_id=7)
    notify_conv_changed('conv-3', rev='not-an-int', user_id=7)

    assert [s['payload']['type'] for s in captured_push] == [
        'conv_changed', 'conv_deleted', 'conv_changed']
    assert captured_push[0]['payload'] == {
        'type': 'conv_changed', 'convId': 'conv-1', 'userId': 7, 'rev': 3}
    assert captured_push[1]['payload'] == {
        'type': 'conv_deleted', 'convId': 'conv-2', 'userId': 7}
    # A non-int rev is dropped, never coerced onto the wire.
    assert 'rev' not in captured_push[2]['payload']
    for sent in captured_push:
        assert sent['channel'] == 'notify'
        assert validate_push_frame(sent['payload']) == ()


def test_folders_changed_emitter_conforms(captured_push):
    from routes.api_v1.folders import _notify_folders_changed

    _notify_folders_changed(user_id=7)
    _notify_folders_changed(deleted_folder_id='f_1', user_id=7)

    assert captured_push[0]['payload'] == {
        'type': 'folders_changed', 'userId': 7}
    assert captured_push[1]['payload'] == {
        'type': 'folders_changed', 'userId': 7, 'deletedFolderId': 'f_1'}
    for sent in captured_push:
        assert sent['channel'] == 'notify'
        assert sent['task_id'] == '__folders__'
        assert validate_push_frame(sent['payload']) == ()


def test_codex_reset_offer_emitter_conforms(captured_push):
    from lib.oauth.codex_usage import _publish_codex_usage_reset_update

    offer = {'state': 'available', 'available_count': 1,
             'notification_key': 'a' * 24}
    _publish_codex_usage_reset_update(user_id='owner-7', reset_offer=offer)

    assert len(captured_push) == 1
    sent = captured_push[0]
    assert sent['channel'] == 'oauth'
    assert sent['task_id'] == 'codex-reset'
    assert sent['payload'] == {
        'type': 'codex.reset_offer.updated',
        'provider': 'codex',
        'reset_offer': offer,
    }
    assert validate_push_frame(sent['payload']) == ()


# ═══════════════════════════════════════════════════════════════════
#  D. Frontend narrowing — the real TS owner under node
# ═══════════════════════════════════════════════════════════════════

def _run_node(script: str, *paths: Path) -> dict:
    result = subprocess.run(
        ["node", "-e", script, *(str(path) for path in paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_frame_identity_narrows_only_declared_frames():
    from tests._runtime_sections import native_module_path

    bundle = native_module_path("frame-identity-owner.js", FRAME_IDENTITY)
    output = _run_node(
        r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const convChanged = { type: 'conv_changed', convId: 'c1', userId: 7, rev: 3 };
const convDeleted = { type: 'conv_deleted', convId: 'c2', userId: 7 };
const folders = { type: 'folders_changed', userId: 7, deletedFolderId: 'f1' };
const codex = {
  type: 'codex.reset_offer.updated', provider: 'codex', reset_offer: {},
};

const checks = {
  declaredAccepted: [convChanged, convDeleted, folders, codex]
    .every(isContractedPushFrame),
  foreignRejected: [
    null, undefined, 42, 'conv_changed', {}, { type: 'timer_changed' },
    { type: 'conv_changed2' }, { type: 7 },
  ].every((f) => !isContractedPushFrame(f)),
  convNarrowed: narrowConvCatalogFrame(convChanged) === convChanged
    && narrowConvCatalogFrame(convDeleted) === convDeleted,
  // A catalog frame without a usable id is not actionable — rejected.
  convMissingId: narrowConvCatalogFrame(
    { type: 'conv_changed', userId: 7 }) === null,
  convRejectsOthers: narrowConvCatalogFrame(folders) === null
    && narrowConvCatalogFrame(codex) === null,
  foldersNarrowed: narrowFoldersChangedFrame(folders) === folders,
  foldersRejectsConv: narrowFoldersChangedFrame(convChanged) === null,
  // Ownership policy regression: only an explicitly scoped owner match.
  ownerMatch: frameBelongsToOwner(7, 7) && frameBelongsToOwner('7', 7),
  ownerMismatch: !frameBelongsToOwner(7, 8) && !frameBelongsToOwner(null, 7)
    && !frameBelongsToOwner(7, undefined),
};
console.log(JSON.stringify(checks));
""",
        Path(bundle),
    )
    assert output == {
        "declaredAccepted": True,
        "foreignRejected": True,
        "convNarrowed": True,
        "convMissingId": True,
        "convRejectsOthers": True,
        "foldersNarrowed": True,
        "foldersRejectsConv": True,
        "ownerMatch": True,
        "ownerMismatch": True,
    }
