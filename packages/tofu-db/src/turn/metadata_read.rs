//! Metadata-only Turn reads over the existing versioned-document byte codec.
//!
//! Entry points: get, summarize. Physical owner/tenant scope, logical Turn
//! identity, JSON grammar/depth and byte admission stay with the current
//! authority readers. Only public fields and the two normalization inputs are
//! decoded; projection and private head bodies are skipped, never reconstructed
//! or cached here. A blob-carried document whose header-bound summary matches
//! its current envelope is answered from that summary without reading the
//! blob; summarize defines its exact member texts. Legacy, inline and
//! unsummarized documents still require bounded whole-byte validation; the
//! existing codec retains its Serde-marker compatibility fallback.

use super::*;
use serde::de::{Deserialize, Deserializer, IgnoredAny, MapAccess, Visitor};

/// Public fields and the two normalization inputs; every other member is
/// skipped. The selective decoder and summarize must select the same set.
const METADATA_FIELDS: [&str; 18] = [
    "turnId",
    "presentationId",
    "conversationId",
    "laneId",
    "parentTurnId",
    "ordinal",
    "actor",
    "kind",
    "runId",
    "status",
    "currentAttemptId",
    "projectionRevision",
    "modelContextRevision",
    "settlement",
    "createdAt",
    "updatedAt",
    "_executionEpoch",
    "_modelContextDisplayOffset",
];

pub(super) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let key = turn_key(transaction, conversation_id, turn_id)?;
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let logical_key = turn_logical_key(conversation_id, turn_id);
    let summarized =
        metadata_summary::bound(database, transaction, &key, &stored, &logical_key)?
            .and_then(|summary| serde_json::from_slice::<Metadata>(&summary).ok());
    let Metadata(document) = match summarized {
        Some(metadata) => metadata,
        None => whole_document(database, transaction, &stored, &logical_key)?,
    };
    validate_metadata(&document, conversation_id, turn_id)?;
    encode_response(&public_turn(document, execution_epoch)?).map(Some)
}

fn whole_document(
    database: &AuthorityDatabase,
    transaction: &AuthorityTransaction,
    stored: &[u8],
    logical_key: &str,
) -> io::Result<Metadata> {
    let (stored_key, bytes) = crate::versioned_document::materialize_stored_document_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    if stored_key != logical_key {
        return Err(invalid_data("Turn metadata document identity differs"));
    }
    // The byte reader above has already applied the strict JSON/depth fence.
    // IgnoredAny is only a selective decode, never a JSON-validation authority.
    serde_json::from_slice(&bytes).map_err(|_| invalid_data("Turn metadata is malformed"))
}

/// The exact raw texts of the selected members, as one JSON object. As in the
/// selective decoder, the final duplicate wins, at the position of its first
/// occurrence; parsing these texts yields the Values the whole document would.
/// The caller's writer still validates the complete document before publication.
pub(super) fn summarize(document_json: &[u8]) -> Option<Vec<u8>> {
    let RawMetadata(members) = serde_json::from_slice(document_json).ok()?;
    let mut summary = vec![b'{'];
    for (index, (field, value)) in members.into_iter().enumerate() {
        if index > 0 {
            summary.push(b',');
        }
        // Field names are fixed ASCII identifiers; they need no JSON escaping.
        summary.push(b'"');
        summary.extend_from_slice(field.as_bytes());
        summary.extend_from_slice(b"\":");
        summary.extend_from_slice(value.get().as_bytes());
    }
    summary.push(b'}');
    Some(summary)
}

struct Metadata(Map<String, Value>);

impl<'de> Deserialize<'de> for Metadata {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(MetadataVisitor)
    }
}

struct MetadataVisitor;

impl<'de> Visitor<'de> for MetadataVisitor {
    type Value = Metadata;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a Turn metadata object")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut fields: A) -> Result<Self::Value, A::Error> {
        let mut document = Map::new();
        while let Some(key) = fields.next_key::<String>()? {
            if METADATA_FIELDS.contains(&key.as_str()) {
                // As in Value, the final duplicate field wins. Validate the
                // resulting metadata, not an overwritten legacy spelling.
                document.insert(key, fields.next_value::<Value>()?);
            } else {
                fields.next_value::<IgnoredAny>()?;
            }
        }
        Ok(Metadata(document))
    }
}

struct RawMetadata<'a>(Vec<(&'static str, &'a RawValue)>);

impl<'de> Deserialize<'de> for RawMetadata<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(RawMetadataVisitor)
    }
}

struct RawMetadataVisitor;

impl<'de> Visitor<'de> for RawMetadataVisitor {
    type Value = RawMetadata<'de>;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a Turn document object")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut fields: A) -> Result<Self::Value, A::Error> {
        let mut members = Vec::<(&'static str, &'de RawValue)>::new();
        while let Some(key) = fields.next_key::<String>()? {
            let Some(field) = METADATA_FIELDS.iter().find(|field| **field == key) else {
                fields.next_value::<IgnoredAny>()?;
                continue;
            };
            let value = fields.next_value::<&'de RawValue>()?;
            match members.iter_mut().find(|(name, _)| name == field) {
                Some(member) => member.1 = value,
                None => members.push((field, value)),
            }
        }
        Ok(RawMetadata(members))
    }
}

fn validate_metadata(
    document: &Map<String, Value>,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<()> {
    if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
        || document.get("turnId").and_then(Value::as_str) != Some(turn_id)
    {
        return Err(invalid_data("Turn metadata identity differs"));
    }
    for field in ["laneId", "actor", "kind", "runId", "status"] {
        if document.get(field).and_then(Value::as_str).is_none() {
            return Err(invalid_data("Turn metadata text field is malformed"));
        }
    }
    // Earlier Turn documents can omit presentationId and nullable pointers.
    // Retain absence exactly; never manufacture a second public normalizer.
    if document
        .get("presentationId")
        .is_some_and(|value| !value.is_string())
    {
        return Err(invalid_data("Turn presentation identity is malformed"));
    }
    for field in ["parentTurnId", "currentAttemptId"] {
        if document
            .get(field)
            .is_some_and(|value| !value.is_null() && !value.is_string())
        {
            return Err(invalid_data("Turn metadata pointer is malformed"));
        }
    }
    for field in ["ordinal", "projectionRevision", "createdAt", "updatedAt"] {
        if document.get(field).and_then(Value::as_u64).is_none() {
            return Err(invalid_data("Turn metadata integer field is malformed"));
        }
    }
    for field in [
        "modelContextRevision",
        "_executionEpoch",
        "_modelContextDisplayOffset",
    ] {
        if document
            .get(field)
            .is_some_and(|value| value.as_u64().is_none())
        {
            return Err(invalid_data(
                "Turn metadata normalization input is malformed",
            ));
        }
    }
    // Validate stored settlement even when an epoch transition will replace it.
    if document
        .get("settlement")
        .and_then(Value::as_object)
        .is_none()
    {
        return Err(invalid_data("turn settlement is malformed"));
    }
    Ok(())
}
