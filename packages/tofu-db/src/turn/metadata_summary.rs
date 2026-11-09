//! Header-bound Turn metadata summaries.
//!
//! Entry points: put_document, bound, key. Every Turn document write goes
//! through put_document; a blob-carried value also publishes, in the same
//! transaction, the exact raw texts of its metadata members bound to the
//! SHA-256 of the stored envelope that write produced. The envelope names the
//! immutable content-addressed blob, so a matching digest proves the summary
//! was taken from exactly the current bytes; no invalidation can be missed.
//! Inline documents need no summary. Legacy rows, digest mismatches and any
//! envelope anomaly use the whole-document reader, which reports its own error.

use super::*;

const MAGIC: &[u8; 8] = b"TDBTMS01";
const DIGEST_BYTES: usize = 32;

/// Same owner/tenant scope and key suffix as the Turn document, so every
/// conversation range that carries Turn documents also carries summaries.
pub(super) fn key(document_key: &EntityKey) -> io::Result<EntityKey> {
    EntityKey::new(
        document_key.tenant_id(),
        document_key.owner_user_id(),
        TURN_METADATA_SUMMARY_NAMESPACE,
        document_key.key_bytes(),
    )
}

/// Store one Turn document with its owner-bound blob placement. The summary
/// is extracted from the exact bytes being stored, and published only after
/// the ordinary writer accepted them; a write the writer refuses publishes
/// nothing. A summary beyond the entity value bound is omitted (exact fallback).
pub(super) fn put_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: crate::versioned_document::PutRequest,
    maximum_bytes: usize,
) -> io::Result<(Vec<u8>, Vec<u8>)> {
    // The writer that carries its successor projection publishes it after
    // this; every other committed write ends the committed entry.
    transaction.forget_resident_projection(&request.key);
    let summary = if crate::versioned_document::stores_inline(
        &request.namespace,
        &request.logical_key,
        request.value_json.len(),
    ) {
        None
    } else {
        match metadata_read::summarize(&request.value_json) {
            Some(metadata) => Some((key(&request.key)?, metadata)),
            None => None,
        }
    };
    let owner_user_id = transaction.owner_user_id();
    let (response, envelope) =
        crate::versioned_document::put_with_blob_owner_bounded_returning_envelope(
            database,
            transaction,
            request,
            owner_user_id,
            maximum_bytes,
        )?;
    if let Some((summary_key, metadata)) = summary {
        let mut summary = Vec::with_capacity(MAGIC.len() + DIGEST_BYTES + metadata.len());
        summary.extend_from_slice(MAGIC);
        summary.extend_from_slice(&Sha256::digest(&envelope));
        summary.extend_from_slice(&metadata);
        if summary.len() <= crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES {
            database.entity_put(transaction, summary_key, summary)?;
        }
    }
    Ok((response, envelope))
}

/// The metadata members of exactly this stored envelope, or None when the
/// caller must read the whole document: inline or unsummarized rows, another
/// envelope's summary, or an identity/byte-bound anomaly.
pub(super) fn bound(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document_key: &EntityKey,
    stored: &[u8],
    logical_key: &str,
) -> io::Result<Option<Vec<u8>>> {
    let blob_within_bound = matches!(
        crate::versioned_document::stored_blob_reference(stored),
        Ok(Some(reference))
            if reference.logical_bytes
                <= crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES as u64
    );
    if !blob_within_bound
        || crate::versioned_document::stored_document_logical_key(stored, DOCUMENT_IDENTITY)
            .ok()
            .as_deref()
            != Some(logical_key)
    {
        return Ok(None);
    }
    let Some(summary) = database.entity_get(transaction, &key(document_key)?)? else {
        return Ok(None);
    };
    let Some(bound) = summary.strip_prefix(MAGIC.as_slice()) else {
        return Ok(None);
    };
    if bound.len() < DIGEST_BYTES || bound[..DIGEST_BYTES] != Sha256::digest(stored)[..] {
        return Ok(None);
    }
    Ok(Some(bound[DIGEST_BYTES..].to_vec()))
}
