"""Regression + acceptance tests for prefix-cache BREAK #7 (tool_result CAS digest).

ROOT CAUSE (named, byte-level): the model-visible budget envelope's
``artifactRef`` is the CAS digest ``sha256(manifest)``, and the manifest
embedded a REQUIRED random-UUID ``resultId`` (``CapturedToolResult.result_id``,
``result_value.py``; required to be a UUID by
``tool_result_query_contract.validate_manifest``). Two captures of the SAME
logical content minted two DIFFERENT manifests → two different ``artifactRef``
digests → the ``tool_result(<id>).tool_result`` wire bytes changed between
turns → PREFIX MUTATION BREAK (776/~830 of breaks 2026-09-24).

THE FIX (landed, both backends, in parity): the CAS content address is computed
over the manifest with the provenance ``resultId`` EXCLUDED — i.e. the address
is a pure function of the retained evidence, not the per-occurrence label. The
stored manifest still carries the real ``resultId`` (provenance intact); only
the digest input drops it. Implemented identically in:

  * SQLite sidecar — ``lib/storage_authority_sql/operations_pkg/_artifacts.py``
    (``_tool_result_content_digest``), and
  * TofuDB authority — ``packages/tofu-db/src/artifact.rs``
    (``canonical_tool_result_digest``).

Because the digest is a pure CAS lookup key (minted once at ``put``, never
re-derived from stored bytes on read/children/prune), excluding ``resultId``
keeps dedup/retrieval intact while making the address content-deterministic.

These tests drive the REAL SQLite repository (no digest mocking) and assert the
three invariants the fix must hold simultaneously:
  * byte-stability: identical content → identical ``artifactRef`` (the fix);
  * identity: in-memory ``result_id`` stays a random per-occurrence UUID;
  * provenance: the stored manifest's ``resultId`` survives read-back.
"""
from __future__ import annotations

import pytest

pytest_plugins = ['tests._artifact_sidecar']
pytestmark = pytest.mark.unit


def test_persist_result_artifact_ref_is_deterministic_across_captures_FIXED():
    """Acceptance criterion: identical logical content → identical artifactRef.

    Drives the real ``ToolResultArtifactRepository.put`` (canonical digest).
    Before the fix this asserted the opposite (xfail); it now must PASS.
    """
    from lib.tools.result_value import capture_result, persist_result

    ra = persist_result(capture_result('identical logical content'), user_id=1)
    rb = persist_result(capture_result('identical logical content'), user_id=1)
    assert ra == rb, (
        f'artifactRef must be content-deterministic after the #7 fix; got '
        f'{ra!r} != {rb!r}')


def test_capture_identity_is_occurrence_not_content_hash_PROTECTED():
    """Protected invariant: in-memory ``result_id`` stays a random per-occurrence
    UUID even for identical content (the fix must NOT collapse identity)."""
    import uuid
    from lib.tools.result_value import capture_result
    a, b = capture_result('same'), capture_result('same')
    assert a.result_id != b.result_id
    assert uuid.UUID(a.result_id)
    assert a.parts == b.parts


def test_canonical_digest_excludes_result_id_and_preserves_stored_manifest():
    """Byte-stability AND provenance together, against the real repository.

    Two captures of identical content must share one CAS address (the model
    never sees a re-billed prefix), while the stored manifest still resolves to
    a valid resultId (provenance for ``read_tool_artifact``/search).
    """
    import uuid
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_value import (CanonicalReceipt, capture_result,
                                        compose_result, persist_result)

    repo = ToolResultArtifactRepository()
    leaf_a = capture_result('shared evidence body')
    leaf_b = capture_result('shared evidence body')
    ref_a = persist_result(leaf_a, user_id=1)
    ref_b = persist_result(leaf_b, user_id=1)
    # Byte-stability: identical evidence collapses to one CAS address, so the
    # model-visible artifactRef never re-bills the prefix across turns.
    assert ref_a == ref_b

    # Identity + provenance: DISTINCT-content children keep their own
    # resultIds in read-back provenance (the protected integration contract
    # path[0]['resultId'] == leaf.result_id). Identical-content children are
    # the SAME content and correctly coalesce onto the single retained manifest
    # (that coalescing is the entire point of CAS dedup — it is what stops the
    # prefix re-bill). So we assert distinct leaves keep distinct provenance.
    distinct_a = capture_result('evidence body ALPHA')
    distinct_b = capture_result('evidence body BETA')
    receipts = [
        CanonicalReceipt(name='t-a', call_id='call-a', canonical=distinct_a,
                         output='preview-a'),
        CanonicalReceipt(name='t-b', call_id='call-b', canonical=distinct_b,
                         output='preview-b'),
    ]
    batch_ref = persist_result(
        capture_result(compose_result('display only', receipts)), user_id=1)
    page = repo.read_range(user_id=1, artifact_ref=batch_ref, limit=65536)
    assert page is not None
    provenance_ids = [entry['path'][0]['resultId']
                      for entry in page['provenance']]
    assert provenance_ids == [distinct_a.result_id, distinct_b.result_id]
    assert distinct_a.result_id != distinct_b.result_id
    for pid in provenance_ids:
        assert uuid.UUID(pid)


def test_non_manifest_content_digests_raw_bytes_unchanged():
    """Only the canonical manifest media type is canonicalized; plain-text and
    other content still hash their raw bytes (no behavior change for legacy
    artifacts and non-manifest blobs)."""
    import hashlib
    from lib.storage_authority_sql.operations_pkg._artifacts import (
        _tool_result_content_digest)

    raw = 'not a manifest at all'
    encoded = raw.encode('utf-8')
    assert _tool_result_content_digest(raw, encoded, 'text/plain') == \
        hashlib.sha256(encoded).hexdigest()

    # A manifest body stored under a NON-manifest media type is not
    # canonicalized (media type gates the canonicalization).
    manifest = '{"resultId":"x","parts":[]}'
    assert _tool_result_content_digest(manifest, manifest.encode(), 'text/plain') == \
        hashlib.sha256(manifest.encode()).hexdigest()
