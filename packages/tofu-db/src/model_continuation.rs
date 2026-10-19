//! Native durable model continuation state, never transcript authority.
//!
//! One latest checkpoint per owner/conversation/lane survives process and cache
//! loss. A settled Turn ordinal is the monotonic CAS frontier, and its
//! model-visible witness fences edited or foreign writers. Equal-frontier
//! retries are read-only: no command stores a second payload. At most one
//! 8 MiB record plus at most 1 KiB in its existing lane-directory value; every
//! read-modify-write happens inside one authority
//! transaction so the frontier CAS is atomic with settlement.

use std::io;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::versioned_document::{
    self, materialize_stored_document_bounded, put_with_blob_owner_bounded, PutRequest,
};

pub(crate) mod migration;

pub(crate) const CONTINUATION_NAMESPACE: &str = "model.continuation.v1";
/// Conversation-prefixed lane directory for lifecycle cleanup: one tiny entry
/// per stored continuation lets conversation/turn deletion find and remove
/// the hash-keyed derived records, matching the legacy authority's
/// `delete_conversation_continuations` (which prefix-deletes the SQL rows).
/// Its optional value is an authority-certified context prefix (empty on legacy
/// or invalidated state); model_context owns that codec and invalidation.
pub(crate) const LANE_INDEX_NAMESPACE: &str = "model.continuation.v1.lanes";
/// Hard bound for one conversation's lane directory. Conversations have a
/// handful of branch lanes; beyond this the directory is corrupt, so
/// lifecycle cleanup fails closed instead of running an unbounded scan. The
/// cleanup probes one row past the bound inside the platform's canonical
/// single-scan row budget (1,000).
pub(crate) const MAX_CONTINUATION_LANES_PER_CONVERSATION: usize = 999;
pub(crate) const MAX_PROJECTION_BYTES: usize = 8 * 1024 * 1024;
pub(crate) const MAX_MESSAGES: usize = 16_384;
pub(crate) const MAX_TURN_WITNESSES: usize = 4_096;

use crate::generated_model_context_projection::{
    DISPLAY_ONLY_FIELDS, ROUND_DISPLAY_ONLY_FIELDS,
    SEGMENT_DISPLAY_ONLY_FIELDS, TRANSLATED_SEGMENT_TYPES,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

pub(crate) fn continuation_raw_key(conversation_id: &str, lane_id: &str) -> String {
    // The owner component of the entity key already scopes the record; the raw
    // key carries collision-resistant conversation/lane identities only.
    let conversation_digest = Sha256::digest(conversation_id.as_bytes());
    let lane_digest = Sha256::digest(lane_id.as_bytes());
    let mut raw = String::with_capacity(130);
    for byte in conversation_digest {
        use std::fmt::Write as _;
        write!(&mut raw, "{byte:02x}").expect("writing to String cannot fail");
    }
    raw.push(':');
    for byte in lane_digest {
        use std::fmt::Write as _;
        write!(&mut raw, "{byte:02x}").expect("writing to String cannot fail");
    }
    raw
}

fn continuation_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONTINUATION_NAMESPACE,
        continuation_raw_key(conversation_id, lane_id).as_bytes(),
    )
}

pub(crate) fn lane_index_key(transaction: &AuthorityTransaction, raw_key: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LANE_INDEX_NAMESPACE,
        raw_key.as_bytes(),
    )
}

/// Remove every derived continuation of one conversation, in the caller's
/// transaction. The legacy authority performs the same cleanup inside the
/// conversation delete/purge and turn-deletion transactions.
pub(crate) fn delete_conversation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    let mut prefix = String::with_capacity(65);
    for byte in Sha256::digest(conversation_id.as_bytes()) {
        use std::fmt::Write as _;
        write!(&mut prefix, "{byte:02x}").expect("writing to String cannot fail");
    }
    prefix.push(':');
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        LANE_INDEX_NAMESPACE,
        prefix.as_bytes(),
    )?;
    let entries = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_CONTINUATION_LANES_PER_CONVERSATION + 1,
    )?;
    if entries.len() == MAX_CONTINUATION_LANES_PER_CONVERSATION + 1 {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "model continuation lane directory exceeds its bound",
        ));
    }
    for (index_key, _) in entries {
        let continuation_key = EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            CONTINUATION_NAMESPACE,
            index_key.key_bytes(),
        )?;
        database.entity_delete(transaction, continuation_key)?;
        database.entity_delete(transaction, index_key)?;
    }
    Ok(())
}

fn valid_witness(witness: &Value) -> bool {
    witness.as_array().is_some_and(|witness| {
        witness.len() == 2
            && witness[0]
                .as_str()
                .is_some_and(|id| !id.is_empty() && id.chars().count() <= 256)
            && witness[1].as_str().is_some_and(|digest| {
                digest.len() == 64
                    && digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
    })
}

/// Bounded canonical encoding; malformed or oversized input never replaces
/// state. Returns None for anything the legacy authority would reject.
pub(crate) fn encode_projection(projection: &Value) -> Option<Vec<u8>> {
    let object = projection.as_object()?;
    let messages_valid = object
        .get("messages")
        .and_then(Value::as_array)
        .is_some_and(|messages| {
            !messages.is_empty()
                && messages.len() <= MAX_MESSAGES
                && messages.iter().all(Value::is_object)
                && messages
                    .last()
                    .and_then(|message| message.get("role"))
                    .and_then(Value::as_str)
                    == Some("assistant")
        });
    let turns_valid = object
        .get("turns")
        .and_then(Value::as_array)
        .is_some_and(|turns| {
            !turns.is_empty()
                && turns.len() <= MAX_TURN_WITNESSES
                && turns.iter().all(valid_witness)
        });
    if let Some(proof) = object.get("historyProof") {
        let valid = proof.as_object().is_some_and(|proof| {
            proof.len() == 3
                && matches!(proof.get("version").and_then(Value::as_u64), Some(1 | 2))
                && proof.get("count").and_then(Value::as_u64).is_some_and(|count| count < 10000)
                && proof.get("digest").and_then(Value::as_str).is_some_and(|digest| {
                    digest.len() == 64 && digest.bytes().all(|byte|
                        byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                })
                && object.get("turns").and_then(Value::as_array).is_some_and(|turns| turns.len() == 1)
        });
        if !valid { return None; }
    }
    if !messages_valid || !turns_valid {
        return None;
    }
    let mut encoded = Vec::new();
    if serde_json::to_writer(&mut encoded, projection).is_err()
        || encoded.len() > MAX_PROJECTION_BYTES
    {
        return None;
    }
    Some(encoded)
}

fn normalized_round(value: &Value) -> Value {
    match value.as_object() {
        Some(round) => Value::Object(round.iter()
            .filter(|(key, _)| !ROUND_DISPLAY_ONLY_FIELDS.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone())).collect()),
        None => value.clone(),
    }
}

fn normalized_projection(projection: &Map<String, Value>) -> Map<String, Value> {
    projection.iter().filter(|(key, _)| !DISPLAY_ONLY_FIELDS.contains(&key.as_str()))
        .map(|(key, value)| {
            let value = match (key.as_str(), value) {
                ("toolRounds", Value::Array(rounds)) =>
                    Value::Array(rounds.iter().map(normalized_round).collect()),
                ("segments", Value::Array(segments)) => Value::Array(segments.iter().map(|segment| {
                    let mut segment = segment.clone();
                    let translated = segment.get("type").and_then(Value::as_str)
                        .is_some_and(|kind| TRANSLATED_SEGMENT_TYPES.contains(&kind));
                    if translated {
                        if let Some(object) = segment.as_object_mut() {
                            for field in SEGMENT_DISPLAY_ONLY_FIELDS { object.remove(*field); }
                        }
                    }
                    if let Some(round) = segment.get_mut("_round") {
                        *round = normalized_round(round);
                    }
                    segment
                }).collect()),
                _ => value.clone(),
            };
            (key.clone(), value)
        }).collect()
}

fn same_entries<F>(left: &Map<String, Value>, right: &Map<String, Value>,
                   excluded: &[&str], compare: F) -> bool
where F: Fn(&str, &Value, &Value) -> bool {
    let visible = |key: &str| !excluded.contains(&key);
    left.keys().filter(|key| visible(key)).count() == right.keys().filter(|key| visible(key)).count()
        && left.iter().filter(|(key, _)| visible(key)).all(|(key, value)|
            right.get(key).is_some_and(|other| compare(key, value, other)))
}

fn same_round(left: &Value, right: &Value) -> bool {
    match (left.as_object(), right.as_object()) {
        (Some(left), Some(right)) => same_entries(left, right, ROUND_DISPLAY_ONLY_FIELDS,
                                                 |_, left, right| left == right),
        _ => left == right,
    }
}

fn same_array<F>(left: &Value, right: &Value, compare: F) -> bool
where F: Fn(&Value, &Value) -> bool {
    match (left.as_array(), right.as_array()) {
        (Some(left), Some(right)) => left.len() == right.len()
            && left.iter().zip(right).all(|(left, right)| compare(left, right)),
        _ => left == right,
    }
}

/// Borrow evidence text while comparing: never clone/serialize large results
/// merely to decide whether an edit is display-only.
pub(crate) fn model_projections_equal(left: &Map<String, Value>, right: &Map<String, Value>) -> bool {
    same_entries(left, right, DISPLAY_ONLY_FIELDS, |key, left, right| match key {
        "toolRounds" => same_array(left, right, same_round),
        "segments" => same_array(left, right, |left, right| match (left.as_object(), right.as_object()) {
            (Some(left), Some(right)) => {
                let translated = left.get("type").and_then(Value::as_str)
                    .is_some_and(|kind| TRANSLATED_SEGMENT_TYPES.contains(&kind));
                let excluded = if translated { SEGMENT_DISPLAY_ONLY_FIELDS } else { &[] };
                same_entries(left, right, excluded, |key, left, right|
                    if key == "_round" { same_round(left, right) } else { left == right })
            },
            _ => left == right,
        }),
        _ => left == right,
    })
}

/// Model-visible Turn witness; declared display-only locations are excluded.
pub(crate) fn settled_witness(turn: &Map<String, Value>) -> Value {
    let mut source = Map::new();
    for field in ["actor", "laneId", "parentTurnId", "status", "currentAttemptId"] {
        source.insert(
            field.to_owned(),
            turn.get(field).cloned().unwrap_or(Value::Null),
        );
    }
    if turn.contains_key("modelContextRevision") || turn.contains_key("projectionRevision") {
        let revision = turn.get("modelContextRevision").cloned().or_else(||
            crate::turn::model_context_revision(turn).ok().map(Value::from));
        source.insert("modelContextRevision".to_owned(), revision.unwrap_or(Value::Null));
    }
    let projection = turn
        .get("projection")
        .and_then(Value::as_object)
        .map(normalized_projection)
        .unwrap_or_default();
    source.insert("projection".to_owned(), Value::Object(projection));
    // serde_json maps sort keys, matching the legacy sort_keys digest.
    let canonical = serde_json::to_string(&Value::Object(source))
        .expect("JSON values always encode to a string");
    let digest = Sha256::digest(canonical.as_bytes());
    let turn_id = turn
        .get("turnId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let mut hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut hex, "{byte:02x}").expect("writing to String cannot fail");
    }
    Value::Array(vec![Value::String(turn_id), Value::String(hex)])
}

// Legacy records are canonical JSON objects. New records use the shared
// document codec so GC sees their owner-bound blob references.
fn materialize_record(
    database: &AuthorityDatabase,
    transaction: &AuthorityTransaction,
    key: &EntityKey,
    stored: &[u8],
) -> io::Result<Vec<u8>> {
    if stored.starts_with(b"{") {
        if stored.len() > MAX_PROJECTION_BYTES + 4 * 1024 {
            return Err(invalid_data("model continuation record exceeds its bound"));
        }
        return Ok(stored.to_vec());
    }
    let (logical_key, raw) = materialize_stored_document_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        stored,
        CONTINUATION_NAMESPACE,
        MAX_PROJECTION_BYTES + 4 * 1024,
    )?;
    if logical_key.as_bytes() != key.key_bytes() {
        return Err(invalid_data("model continuation document identity differs"));
    }
    Ok(raw)
}

// Shared bounded materialization remains the codec owner; this adapter adds
// the optional lookup needed by migration and the derived lane directory.
fn read_record_bytes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
) -> io::Result<Option<Vec<u8>>> {
    database.entity_get(transaction, key)?
        .map(|stored| materialize_record(database, transaction, key, &stored))
        .transpose()
}

fn read_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
) -> io::Result<Option<(u64, Map<String, Value>)>> {
    let Some(raw) = read_record_bytes(database, transaction, key)? else {
        return Ok(None);
    };
    let record = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("model continuation record is malformed"))?;
    let frontier = record
        .get("frontier")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("model continuation frontier is malformed"))?;
    let projection = record
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("model continuation projection is malformed"))?;
    Ok(Some((frontier, projection)))
}

fn write_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    frontier: u64,
    projection: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let record = json_record(frontier, projection, updated_at_ms);
    let encoded = serde_json::to_vec(&record)
        .map_err(|_| invalid_data("model continuation record cannot be encoded"))?;
    if encoded.len() > MAX_PROJECTION_BYTES + 4 * 1024 {
        return Err(invalid_input("model continuation record exceeds its bound"));
    }
    let logical_key = std::str::from_utf8(key.key_bytes())
        .map_err(|_| invalid_data("model continuation key is malformed"))?
        .to_owned();
    if database
        .entity_get(transaction, &key)?
        .is_some_and(|stored| stored.starts_with(b"{"))
    {
        // Upgrade only on a real frontier advance, within the same transaction.
        // Equal-frontier replay and reads never rewrite legacy state.
        database.entity_delete(transaction, key.clone())?;
    }
    let owner = transaction.owner_user_id();
    put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key,
            namespace: CONTINUATION_NAMESPACE.to_owned(),
            logical_key,
            value_json: encoded,
            expected_version: None,
            updated_at_ms,
        },
        owner,
        MAX_PROJECTION_BYTES + 4 * 1024,
    )?;
    Ok(())
}

fn json_record(frontier: u64, projection: &Map<String, Value>, updated_at_ms: u64) -> Value {
    Value::Object(Map::from_iter([
        ("frontier".to_owned(), Value::from(frontier)),
        ("projection".to_owned(), Value::Object(projection.clone())),
        ("updatedAtMs".to_owned(), Value::from(updated_at_ms)),
    ]))
}

/// Monotonic ordinal-frontier CAS. The frontier comes only from authority
/// ordinals, never an untrusted client counter. An equal-frontier retry is
/// read-only and acknowledges only an identical stored payload.
fn put_frontier(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    raw_key: &str,
    frontier: u64,
    projection: &Map<String, Value>,
    updated_at_ms: u64,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<bool> {
    match read_record(database, transaction, &key)? {
        Some((actual, stored)) => {
            if frontier < actual {
                return Ok(false);
            }
            if frontier == actual {
                return Ok(Value::Object(stored) == Value::Object(projection.clone()));
            }
            write_record(
                database,
                transaction,
                key,
                frontier,
                projection,
                updated_at_ms,
            )?;
            let certificate = crate::turn::model_context::certify_frontier(
                database, transaction, conversation_id, lane_id, frontier - 1, projection)?;
            database.entity_put(
                transaction,
                lane_index_key(transaction, raw_key)?,
                certificate,
            )?;
            Ok(true)
        }
        None => {
            write_record(
                database,
                transaction,
                key,
                frontier,
                projection,
                updated_at_ms,
            )?;
            let certificate = crate::turn::model_context::certify_frontier(
                database, transaction, conversation_id, lane_id, frontier - 1, projection)?;
            database.entity_put(
                transaction,
                lane_index_key(transaction, raw_key)?,
                certificate,
            )?;
            Ok(true)
        }
    }
}

/// Validates the candidate witness against the settled authority Turn and
/// stores the projection when the ordinal frontier admits it. Called in the
/// terminal settlement transaction; failures roll back settlement.
pub(crate) fn settle(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    ordinal: u64,
    settled_turn: &Map<String, Value>,
    candidate_projection: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<&'static str> {
    let lane_id = settled_turn
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("settled Turn lane identity is malformed"))?;
    let witness = settled_witness(settled_turn);
    let mut projection = candidate_projection.clone();
    let mut turns = projection
        .get("turns")
        .and_then(Value::as_array)
        .cloned()
        .ok_or_else(|| invalid_data("model continuation witnesses are malformed"))?;
    let last = turns
        .last_mut()
        .ok_or_else(|| invalid_data("model continuation witnesses are malformed"))?;
    *last = witness;
    projection.insert("turns".to_owned(), Value::Array(turns));
    let frontier = ordinal
        .checked_add(1)
        .ok_or_else(|| invalid_data("model continuation frontier overflow"))?;
    let key = continuation_key(transaction, conversation_id, lane_id)?;
    let raw_key = continuation_raw_key(conversation_id, lane_id);
    Ok(
        if put_frontier(
            database,
            transaction,
            key,
            &raw_key,
            frontier,
            &projection,
            updated_at_ms,
            conversation_id,
            lane_id,
        )? {
            "stored"
        } else {
            "rejected"
        },
    )
}

#[derive(Clone, Debug)]
pub enum Request {
    Get {
        conversation_id: String,
        lane_id: String,
    },
    Put {
        conversation_id: String,
        lane_id: String,
        projection: Map<String, Value>,
        updated_at_ms: u64,
    },
    Delete {
        conversation_id: String,
        lane_id: String,
    },
}

impl Request {
    pub(crate) fn validate(&self) -> io::Result<usize> {
        let valid_text =
            |value: &str, maximum: usize| !value.is_empty() && value.chars().count() <= maximum;
        let (conversation_id, lane_id) = match self {
            Self::Get {
                conversation_id,
                lane_id,
            }
            | Self::Put {
                conversation_id,
                lane_id,
                ..
            }
            | Self::Delete {
                conversation_id,
                lane_id,
            } => (conversation_id, lane_id),
        };
        if !valid_text(conversation_id, 256) || !valid_text(lane_id, 256) {
            return Err(invalid_input("model continuation identity is invalid"));
        }
        // Projection bodies are bounded blob inputs. Count only routing
        // metadata against the inline IR literal budget.
        let mut literal_bytes = conversation_id.len() + lane_id.len() + 64;
        if let Self::Put { projection, .. } = self {
            // Candidate validity is settled at execution (invalid candidates
            // reject with `false`, matching the legacy authority); the IR only
            // enforces the hard encoding budget.
            let encoded = serde_json::to_vec(projection)
                .map_err(|_| invalid_input("model continuation projection cannot be encoded"))?;
            if encoded.len() > MAX_PROJECTION_BYTES {
                return Err(invalid_input(
                    "model continuation projection exceeds its bound",
                ));
            }
            literal_bytes += MAX_PROJECTION_BYTES.min(1024);
        }
        Ok(literal_bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_round_locations_ignore_ui_but_keep_nested_user_evidence() {
        let before = serde_json::json!({
            "content": "answer", "translation": "display",
            "toolRounds": [{"toolContent": "receipt", "toolArgs": {"_swarmSnapshot": "data"},
                            "_swarmSnapshot": {"status": "running"}}],
            "segments": [{"_round": {"toolCallId": "c1", "usage": 1, "toolContent": "receipt"}}]
        });
        let mut display = before.clone();
        display["translation"] = serde_json::json!("translated");
        display["toolRounds"][0]["_swarmSnapshot"]["status"] = serde_json::json!("completed");
        display["segments"][0]["_round"]["usage"] = serde_json::json!(100);
        let same = |left: &Value, right: &Value| model_projections_equal(
            left.as_object().unwrap(), right.as_object().unwrap());
        assert!(same(&before, &display));
        assert_eq!(normalized_projection(before.as_object().unwrap()),
                   normalized_projection(display.as_object().unwrap()));
        let mut genuine = display.clone();
        genuine["toolRounds"][0]["toolArgs"]["_swarmSnapshot"] = serde_json::json!("changed data");
        assert!(!same(&before, &genuine));
        genuine = display;

        let numeric = serde_json::json!({"toolRounds": [{"toolArgs": {"value": 1}}]});
        let boolean = serde_json::json!({"toolRounds": [{"toolArgs": {"value": true}}]});
        let floating = serde_json::json!({"toolRounds": [{"toolArgs": {"value": 1.0}}]});
        assert!(!same(&numeric, &boolean));
        assert!(!same(&numeric, &floating));
        genuine["segments"][0]["_round"]["toolContent"] = serde_json::json!("changed receipt");
        assert!(!same(&before, &genuine));
    }

    #[test]
    fn real_translation_locations_preserve_context_but_keep_source_and_nested_evidence() {
        let before = serde_json::json!({"content": "answer", "segments": [
            {"type": "text", "text": "narration"},
            {"type": "thinking", "text": "reasoning"}],
            "toolRounds": [{"toolArgs": {"translatedText": "evidence"}}]});
        let mut after = before.clone();
        after["translatedContent"] = serde_json::json!("display answer");
        after["_translateDone"] = serde_json::json!(true);
        after["_showingTranslation"] = serde_json::json!(true);
        after["segments"][0]["translatedText"] = serde_json::json!("display narration");
        after["segments"][1]["translatedText"] = serde_json::json!("display reasoning");
        let same = |left: &Value, right: &Value| model_projections_equal(
            left.as_object().unwrap(), right.as_object().unwrap());
        assert!(same(&before, &after));
        assert_eq!(normalized_projection(before.as_object().unwrap()),
                   normalized_projection(after.as_object().unwrap()));
        let mut changed = after.clone();
        changed["segments"][0]["text"] = serde_json::json!("edited narration");
        assert!(!same(&before, &changed));
        changed = after.clone();
        changed["toolRounds"][0]["toolArgs"]["translatedText"] = serde_json::json!("edited evidence");
        assert!(!same(&before, &changed));
        after["content"] = serde_json::json!("translated model input");
        assert!(!same(&before, &after));
    }

    #[test]
    fn terminal_revision_binds_public_and_private_documents_identically() {
        let stored = serde_json::json!({"turnId": "a", "actor": "assistant", "laneId": "main",
            "parentTurnId": "u", "status": "completed", "projectionRevision": 9,
            "_modelContextDisplayOffset": 3, "projection": {"content": "answer"}});
        let mut public = stored.clone();
        public.as_object_mut().unwrap().remove("_modelContextDisplayOffset");
        public["modelContextRevision"] = serde_json::json!(6);
        assert_eq!(settled_witness(stored.as_object().unwrap()),
                   settled_witness(public.as_object().unwrap()));
        public["projectionRevision"] = serde_json::json!(10);
        assert_eq!(settled_witness(stored.as_object().unwrap()),
                   settled_witness(public.as_object().unwrap()));
        public["modelContextRevision"] = serde_json::json!(7);
        assert_ne!(settled_witness(stored.as_object().unwrap()),
                   settled_witness(public.as_object().unwrap()));

        public["modelContextRevision"] = serde_json::json!(6);
        public["currentAttemptId"] = serde_json::json!("new-attempt");
        assert_ne!(settled_witness(stored.as_object().unwrap()),
                   settled_witness(public.as_object().unwrap()));
    }

    #[test]
    fn legacy_inline_continuation_upgrades_to_durable_bounded_blob() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let small = Map::from_iter([("messages".to_owned(), serde_json::json!([
            {"role": "assistant", "content": "old"}
        ]))]);
        let large = Map::from_iter([("messages".to_owned(), serde_json::json!([
            {"role": "system", "content": "context".repeat(4096)},
            {"role": "assistant", "content": "new"}
        ]))]);
        let mut transaction = database.begin(7, 11).unwrap();
        let key = continuation_key(&transaction, "legacy", "main").unwrap();
        database.entity_put(&mut transaction, key.clone(),
            serde_json::to_vec(&json_record(1, &small, 100)).unwrap()).unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(read_record(&database, &mut transaction, &key).unwrap(), Some((1, small)));
        write_record(&database, &mut transaction, key.clone(), 2, &large, 200).unwrap();
        let physical = database.entity_get(&mut transaction, &key).unwrap().unwrap();
        assert!(physical.len() < 8192, "large continuation must not occupy an inline entity");
        assert!(crate::versioned_document::stored_blob_reference(&physical).unwrap().is_some());
        database.commit(transaction).unwrap();
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(read_record(&database, &mut transaction, &key).unwrap(), Some((2, large)));
    }

    #[test]
    fn delete_conversation_removes_only_its_own_lane_directory() {
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let keys_for = |transaction: &AuthorityTransaction, conv: &str, lane: &str| {
            let continuation = continuation_key(transaction, conv, lane).unwrap();
            let index = lane_index_key(transaction, &continuation_raw_key(conv, lane)).unwrap();
            (continuation, index)
        };
        for lane in ["main", "branch"] {
            let (continuation, index) = keys_for(&transaction, "conv-a", lane);
            database
                .entity_put(&mut transaction, continuation, b"record".to_vec())
                .unwrap();
            database
                .entity_put(&mut transaction, index, Vec::new())
                .unwrap();
        }
        let (other_continuation, other_index) = keys_for(&transaction, "conv-b", "main");
        database
            .entity_put(&mut transaction, other_continuation, b"record".to_vec())
            .unwrap();
        database
            .entity_put(&mut transaction, other_index, Vec::new())
            .unwrap();

        delete_conversation(&database, &mut transaction, "conv-a").unwrap();
        for lane in ["main", "branch"] {
            let (continuation, index) = keys_for(&transaction, "conv-a", lane);
            assert!(database
                .entity_get(&mut transaction, &continuation)
                .unwrap()
                .is_none());
            assert!(database
                .entity_get(&mut transaction, &index)
                .unwrap()
                .is_none());
        }
        let (other_continuation, _) = keys_for(&transaction, "conv-b", "main");
        assert!(database
            .entity_get(&mut transaction, &other_continuation)
            .unwrap()
            .is_some());
        // Idempotent: an empty directory is a no-op.
        delete_conversation(&database, &mut transaction, "conv-a").unwrap();
        delete_conversation(&database, &mut transaction, "conv-missing").unwrap();
    }
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    match request {
        Request::Get {
            conversation_id,
            lane_id,
        } => {
            if !crate::conversation_header::active_exists(database, transaction, conversation_id)? {
                return Ok(None);
            }
            let key = continuation_key(transaction, conversation_id, lane_id)?;
            let Some((_, projection)) = read_record(database, transaction, &key)? else {
                return Ok(None);
            };
            Ok(encode_projection(&Value::Object(projection)))
        }
        Request::Put {
            conversation_id,
            lane_id,
            projection,
            updated_at_ms,
        } => {
            let rejected = || Ok(Some(b"false".to_vec()));
            if !crate::conversation_header::active_exists(database, transaction, conversation_id)? {
                return rejected();
            }
            let Some(encoded) = encode_projection(&Value::Object(projection.clone())) else {
                return rejected();
            };
            let candidate: Map<String, Value> = serde_json::from_slice::<Value>(&encoded)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("model continuation projection is malformed"))?;
            let witness = candidate
                .get("turns")
                .and_then(Value::as_array)
                .and_then(|turns| turns.last())
                .cloned()
                .ok_or_else(|| invalid_data("model continuation witnesses are malformed"))?;
            let witness_turn_id = witness
                .get(0)
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("model continuation witness is malformed"))?;
            let Some(public) =
                crate::turn::get(database, transaction, conversation_id, witness_turn_id)?
            else {
                return rejected();
            };
            let turn = serde_json::from_slice::<Value>(&public)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("witness Turn document is malformed"))?;
            if turn.get("status").and_then(Value::as_str) != Some("completed")
                || turn.get("laneId").and_then(Value::as_str) != Some(lane_id.as_str())
                || settled_witness(&turn) != witness
            {
                return rejected();
            }
            let ordinal = turn
                .get("ordinal")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("witness Turn ordinal is malformed"))?;
            let frontier = ordinal
                .checked_add(1)
                .ok_or_else(|| invalid_data("model continuation frontier overflow"))?;
            let key = continuation_key(transaction, conversation_id, lane_id)?;
            let stored = put_frontier(
                database,
                transaction,
                key,
                &continuation_raw_key(conversation_id, lane_id),
                frontier,
                &candidate,
                *updated_at_ms,
                conversation_id,
                lane_id,
            )?;
            Ok(Some(if stored {
                b"true".to_vec()
            } else {
                b"false".to_vec()
            }))
        }
        Request::Delete {
            conversation_id,
            lane_id,
        } => {
            if !crate::conversation_header::active_exists(database, transaction, conversation_id)? {
                return Ok(Some(b"false".to_vec()));
            }
            let key = continuation_key(transaction, conversation_id, lane_id)?;
            let existed = database.entity_get(transaction, &key)?.is_some();
            if existed {
                database.entity_delete(transaction, key)?;
                database.entity_delete(
                    transaction,
                    lane_index_key(
                        transaction,
                        &continuation_raw_key(conversation_id, lane_id),
                    )?,
                )?;
            }
            Ok(Some(if existed {
                b"true".to_vec()
            } else {
                b"false".to_vec()
            }))
        }
    }
}
