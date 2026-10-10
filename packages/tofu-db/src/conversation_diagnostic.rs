//! Read-only, revision-fenced diagnostic views of one main-lane Turn.
//! Lives under `turn` to reuse lane/projection identities without exposing them
//! to semantic compilers. Never hydrates a projection, including old inline
//! Turn blobs. The only retained JSON is bounded metadata and the patch head.

use super::*;
use crate::semantic_executor::SemanticExecutionError;
use crate::transaction_ir::{TransactionIr, TransactionIrResult, TransactionIrStep};
use crate::versioned_document::streaming::Source;
use base64::Engine;
use std::ops::Range;

#[path = "diagnostic_json.rs"]
mod streaming_json;

#[path = "diagnostic_source_plan.rs"]
mod source_plan;
pub(crate) const MAX_CHUNK_BYTES: usize = 65_536;

#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Kind {
    Header,
    Turn,
    Chunk,
}
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Request {
    pub conversation_id: String,
    pub kind: Kind,
    pub revision: Option<u64>,
    pub index: Option<u64>,
    pub offset: u64,
    pub limit: usize,
    pub source_token: Option<String>,
}
impl Request {
    pub(crate) fn validate(&self) -> io::Result<()> {
        if self.conversation_id.is_empty()
            || self.conversation_id.chars().count() > 256
            || self.conversation_id.contains('\0')
            || !(1..=MAX_CHUNK_BYTES).contains(&self.limit)
            || (self.kind != Kind::Header && (self.index.is_none() || self.revision.is_none()))
            || (self.kind == Kind::Header && (self.index.is_some() || self.revision.is_some()))
            || (self.kind != Kind::Chunk && (self.offset != 0 || self.limit != MAX_CHUNK_BYTES))
            || (self.kind == Kind::Chunk
                && self.source_token.as_ref().map_or(true, |token| {
                    token.len() != 64
                        || !token
                            .bytes()
                            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                }))
            || (self.kind != Kind::Chunk && self.source_token.is_some())
        {
            return Err(invalid_input("invalid diagnostic request"));
        }
        Ok(())
    }
}

// Deserialize the raw semantic payload, not a Value map: a map would erase
// duplicate keys before validation. Optional means absent, never JSON null.
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct WirePayload {
    conv_id: String,
    user_id: u64,
    kind: Kind,
    #[serde(default, deserialize_with = "present_field")]
    revision: Option<u64>,
    #[serde(default, deserialize_with = "present_field")]
    index: Option<u64>,
    #[serde(default, deserialize_with = "present_field")]
    offset: Option<u64>,
    #[serde(default, deserialize_with = "present_field")]
    limit: Option<usize>,
    #[serde(default, deserialize_with = "present_field")]
    source_token: Option<String>,
}

fn present_field<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: serde::Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

pub(crate) fn compile(
    admitted: crate::semantic::AdmittedRequest<'_>,
) -> Result<TransactionIr, SemanticExecutionError> {
    let invalid = || SemanticExecutionError::DiagnosticInvalidPayload;
    let payload: WirePayload =
        serde_json::from_slice(&admitted.request.payload).map_err(|_| invalid())?;
    let owner = payload.user_id;
    if owner == 0
        || (payload.kind == Kind::Header && (payload.index.is_some() || payload.revision.is_some()))
        || (payload.kind == Kind::Chunk && payload.offset.is_none())
        || (payload.kind != Kind::Chunk
            && (payload.offset.is_some()
                || payload.limit.is_some()
                || payload.source_token.is_some()))
    {
        return Err(invalid());
    }
    if owner != admitted.request.owner_id {
        return Err(SemanticExecutionError::AuthorityScopeMismatch);
    }
    let request = Request {
        conversation_id: payload.conv_id,
        kind: payload.kind,
        revision: payload.revision,
        index: payload.index,
        offset: payload.offset.unwrap_or(0),
        limit: payload.limit.unwrap_or(MAX_CHUNK_BYTES),
        source_token: payload.source_token,
    };

    request.validate().map_err(|_| invalid())?;
    Ok(TransactionIr {
        tenant_id: admitted
            .request
            .tenant_id
            .ok_or(SemanticExecutionError::AuthorityScopeMismatch)?,
        owner_user_id: owner,
        operation: admitted.operation.name.to_owned(),
        operation_kind: admitted.operation.kind,
        steps: vec![TransactionIrStep::ConversationDiagnostic { request, slot: 0 }],
        result: TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        },
        command_effects: None,
    })
}

pub(crate) fn map_error(error: io::Error) -> SemanticExecutionError {
    match error.kind() {
        io::ErrorKind::InvalidInput => SemanticExecutionError::DiagnosticInvalidPayload,
        io::ErrorKind::Unsupported => SemanticExecutionError::DiagnosticUnsupported,
        _ => crate::semantic_executor::map_storage_error(error),
    }
}

fn unsupported() -> io::Error {
    io::Error::new(
        io::ErrorKind::Unsupported,
        "diagnostic streaming of frozen legacy messages is unsupported",
    )
}
fn stale() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "diagnostic conversation revision or source generation differs",
    )
}
fn encode(value: Value) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&value).map_err(|_| invalid_data("diagnostic response cannot be encoded"))
}

fn check_deadline(deadline_elapsed: &mut dyn FnMut() -> bool) -> io::Result<()> {
    if deadline_elapsed() {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "diagnostic read deadline elapsed",
        ));
    }
    Ok(())
}

fn main_lane_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<u64> {
    if let Some(count) = read_lane_state(database, tx, id, "main")?.and_then(|state| state.count) {
        return Ok(count);
    }
    if let Some(raw) = database.entity_get(tx, &lane_count_key(tx, id, "main")?)? {
        return decode_u64(Some(raw), "diagnostic main lane count is malformed");
    }
    // Retained native lanes can predate maintained counters. Count only their
    // bounded envelopes; absence of a counter is never evidence of no turns.
    let (mut start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &lane_prefix(id, "main")?,
    )?;
    let mut count = 0_u64;
    loop {
        check_deadline(deadline_elapsed)?;
        let rows = database.entity_scan(tx, &start, &end, 64)?;
        count = count
            .checked_add(rows.len() as u64)
            .ok_or_else(|| invalid_data("diagnostic lane count overflow"))?;
        let Some((last, _)) = rows.last() else {
            return Ok(count);
        };
        start = last.clone().exact_range()?.1;
        if rows.len() < 64 {
            return Ok(count);
        }
    }
}

fn selected_turn(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    rank: u64,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<(EntityKey, Vec<u8>)> {
    let (mut start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &lane_prefix(id, "main")?,
    )?;
    let mut remaining = rank;
    loop {
        // Stored lane values are tiny envelopes/references, never projections.

        check_deadline(deadline_elapsed)?;
        let limit = remaining
            .saturating_add(1)
            .min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS as u64)
            as usize;
        let rows = database.entity_scan(tx, &start, &end, limit)?;
        if rows.is_empty() {
            return Err(invalid_data("diagnostic lane rank is missing"));
        }
        if remaining < rows.len() as u64 {
            return Ok(rows[remaining as usize].clone());
        }
        remaining -= rows.len() as u64;
        start = rows.last().unwrap().0.clone().exact_range()?.1;
    }
}

fn patch_head(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
    turn_id: &str,
    head: &ProjectionHeadDescriptor,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<Value>> {
    let mut patches = Vec::with_capacity(head.patch_count);
    let mut observed_bytes = 0;
    for ordinal in 1..=head.patch_count {
        check_deadline(deadline_elapsed)?;
        let revision = head
            .base_revision
            .checked_add(ordinal as u64)
            .ok_or_else(|| invalid_data("diagnostic patch revision overflow"))?;
        let stored = database
            .entity_get(
                tx,
                &projection_patch_key(tx, id, turn_id, &head.head_id, revision)?,
            )?
            .ok_or_else(|| invalid_data("diagnostic projection patch is missing"))?;
        let mut source = Source::open(database, tx, &stored, PROJECTION_HEAD_DOCUMENT_IDENTITY)?;
        if source.logical_key != format!("{}\u{1f}{revision:020}", head.head_id)
            || source.total_bytes > (MAX_TURN_PROJECTION_PATCH_BYTES + 1024) as u64
        {
            return Err(invalid_data(
                "diagnostic projection patch exceeds its bound",
            ));
        }
        let mut raw = Vec::with_capacity(source.total_bytes as usize);
        std::io::Read::read_to_end(&mut source, &mut raw)?;
        let mut envelope: Map<String, Value> = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("diagnostic projection patch is malformed"))?;
        if envelope.get("attemptId").and_then(Value::as_str) != Some(&head.attempt_id) {
            return Err(invalid_data("diagnostic patch attempt differs"));
        }
        let patch = envelope
            .remove("patch")
            .filter(Value::is_object)
            .ok_or_else(|| invalid_data("diagnostic projection patch is missing"))?;
        if patch.get("version").and_then(Value::as_u64)
            != Some(crate::turn_projection_patch::PROJECTION_PATCH_VERSION)
            || !patch.get("operations").is_some_and(Value::is_array)
            || patch.get("baseRevision").and_then(Value::as_u64) != revision.checked_sub(1)
            || patch.get("targetRevision").and_then(Value::as_u64) != Some(revision)
        {
            return Err(invalid_data("diagnostic patch chain differs"));
        }
        observed_bytes += serde_json::to_vec(&patch)?.len();
        if observed_bytes > MAX_TURN_PROJECTION_PATCH_BYTES {
            return Err(invalid_data("diagnostic patch head exceeds 1 MiB"));
        }
        patches.push(json!({"projectionRevision": revision, "projectionPatch": patch}));
    }
    if observed_bytes != head.patch_bytes {
        return Err(invalid_data("diagnostic patch bytes differ"));
    }
    Ok(patches)
}

fn encode_chunk(
    source: &Source<'_>,
    span: &Range<u64>,
    request: &Request,
    revision: u64,
    source_token: &str,
) -> io::Result<Vec<u8>> {
    if request.source_token.as_deref() != Some(source_token) {
        return Err(stale());
    }
    let total = span.end - span.start;
    if request.offset > total {
        return Err(invalid_input("diagnostic offset exceeds source"));
    }
    let length = (total - request.offset).min(request.limit as u64) as usize;
    let bytes = source.range(span.start + request.offset, length)?;
    let next = request.offset + bytes.len() as u64;
    encode(
        json!({"revision":revision,"index":request.index.unwrap(),"offset":request.offset,"totalBytes":total,
        "sourceToken":source_token,"data":base64::engine::general_purpose::STANDARD.encode(bytes),"nextOffset":if next < total {Some(next)} else {None}}),
    )
}

pub(crate) fn read(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Request,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<Vec<u8>>> {
    request.validate()?;
    check_deadline(deadline_elapsed)?;
    let id = &request.conversation_id;
    let key = EntityKey::new(
        tx.tenant_id(),
        tx.owner_user_id(),
        crate::generated_tofudb_ir::CONVERSATION_DOCUMENT_NAMESPACE,
        id.as_bytes(),
    )?;
    let Some(stored_header) = database.entity_get(tx, &key)? else {
        return Ok(None);
    };
    let header_source = Source::open(database, tx, &stored_header, "conversations")?;
    if header_source.logical_key != *id {
        return Err(invalid_data("diagnostic conversation identity differs"));
    }
    let header = streaming_json::object(
        header_source,
        &["messages", "search_text"],
        deadline_elapsed,
    )?;
    let mut document = header.metadata;
    if document.get("id").and_then(Value::as_str) != Some(id)
        || document.get("user_id").and_then(Value::as_u64) != Some(tx.owner_user_id())
    {
        return Err(invalid_data("diagnostic conversation owner differs"));
    }
    let revision = document
        .get("rev")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("diagnostic conversation revision is malformed"))?;
    if (request.kind != Kind::Header && request.revision != Some(revision))
        || request.revision.is_some_and(|v| v != revision)
    {
        return Err(stale());
    }
    let count = main_lane_count(database, tx, id, deadline_elapsed)?;
    let frozen_key = EntityKey::new(
        tx.tenant_id(),
        tx.owner_user_id(),
        crate::generated_tofudb_ir::FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE,
        id.as_bytes(),
    )?;
    // Never turn a nonempty frozen/legacy history into a successful empty read.
    if database.entity_get(tx, &frozen_key)?.is_some()
        || (count == 0
            && document
                .get("msg_count")
                .and_then(Value::as_u64)
                .unwrap_or(0)
                != 0)
        || header
            .skipped
            .get("messages")
            .is_some_and(|span| span.end - span.start > 2)
    {
        return Err(unsupported());
    }
    document.insert("msg_count".into(), json!(count));
    document.insert("search_text".into(), json!(""));
    if request.kind == Kind::Header {
        return encode(json!({"revision":revision,"totalCount":count,
            "document":{"metadata":document,"messages":[],"source":"sidecar"},"turnNative":true}))
        .map(Some);
    }
    let index = request.index.unwrap();
    if index >= count {
        return Err(invalid_input("diagnostic Turn rank is out of range"));
    }
    let (lane_key, stored) = selected_turn(database, tx, id, index, deadline_elapsed)?;
    let epoch = crate::conversation_header::execution_epoch(database, tx, id)?;
    if request.kind == Kind::Chunk {
        if let Some(bytes) =
            source_plan::chunk(database, tx, &lane_key, &stored, epoch, request, revision)?
        {
            return Ok(Some(bytes));
        }
    }
    let mut source = Source::open(database, tx, &stored, DOCUMENT_IDENTITY)?;
    let fields = streaming_json::object(&mut source, &["projection"], deadline_elapsed)?;
    let mut metadata = fields.metadata;
    let turn_id = metadata
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("diagnostic Turn identity is malformed"))?
        .to_owned();
    if metadata.get("conversationId").and_then(Value::as_str) != Some(id)
        || metadata.get("laneId").and_then(Value::as_str) != Some("main")
        || source.logical_key != turn_logical_key(id, &turn_id)
        || fields.kinds.get("projection") != Some(&b'{')
    {
        return Err(invalid_data("diagnostic Turn lane identity differs"));
    }
    let ordinal = metadata
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("diagnostic Turn ordinal is malformed"))?;
    if lane_key != lane_index_key(tx, id, "main", ordinal)? {
        return Err(invalid_data(
            "diagnostic lane key differs from Turn ordinal",
        ));
    }
    metadata.insert("projection".into(), json!({}));
    let head = projection_head_from_document(&metadata)?;

    let projection_revision = metadata
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("diagnostic projection revision is malformed"))?;
    if head.as_ref().is_some_and(|head| {
        head.base_revision.checked_add(head.patch_count as u64) != Some(projection_revision)
    }) {
        return Err(invalid_data(
            "diagnostic projection revision differs from patch head",
        ));
    }
    let mut span: Range<u64> = 0..source.total_bytes;
    let mut format = "turn";
    let mut patches = Vec::new();
    let mut checkpoint_hint = None;
    if let Some(head) = head {
        let checkpoint_key = projection_checkpoint_key(tx, id, &turn_id, &head.head_id)?;
        let stored_checkpoint = database
            .entity_get(tx, &checkpoint_key)?
            .ok_or_else(|| invalid_data("diagnostic checkpoint is missing"))?;
        source = Source::open(
            database,
            tx,
            &stored_checkpoint,
            PROJECTION_HEAD_DOCUMENT_IDENTITY,
        )?;
        if source.logical_key != format!("{}\u{1f}checkpoint", head.head_id) {
            return Err(invalid_data("diagnostic checkpoint identity differs"));
        }
        let checkpoint = streaming_json::object(&mut source, &["projection"], deadline_elapsed)?;
        if checkpoint.metadata.get("attemptId").and_then(Value::as_str) != Some(&head.attempt_id)
            || checkpoint.metadata.get("revision").and_then(Value::as_u64)
                != Some(head.base_revision)
            || checkpoint.kinds.get("projection") != Some(&b'{')
        {
            return Err(invalid_data("diagnostic checkpoint fence differs"));
        }
        span = checkpoint
            .skipped
            .get("projection")
            .cloned()
            .ok_or_else(|| invalid_data("diagnostic checkpoint projection is missing"))?;
        format = "projection";
        if request.kind == Kind::Turn {
            patches = patch_head(database, tx, id, &turn_id, &head, deadline_elapsed)?;
        }

        checkpoint_hint = Some((checkpoint_key, stored_checkpoint));
    }
    let total = span.end - span.start;
    let source_token =
        source_plan::token(&lane_key, &stored, epoch, checkpoint_hint.as_ref(), &span);
    source_plan::remember(
        tx,
        id,
        &lane_key,
        &stored,
        epoch,
        checkpoint_hint,
        span.clone(),
        &source_token,
    );
    if request.kind == Kind::Chunk {
        return encode_chunk(&source, &span, request, revision, &source_token).map(Some);
    }
    metadata.remove("projection");
    metadata.remove("_projectionHead");
    let stored_epoch = match metadata.get("_executionEpoch") {
        None => 0,
        Some(value) => value
            .as_u64()
            .ok_or_else(|| invalid_data("turn execution epoch is malformed"))?,
    };
    let turn = public_turn(metadata, epoch)?;
    let mut response = json!({"revision":revision,"index":index,"totalCount":count,"turn":turn,
        "source":{"format":format,"totalBytes":total,"token":source_token},"patches":patches});
    if stored_epoch != epoch {
        // Public reads transform restored projections, including terminal turns.
        // An unbounded recursive transform cannot fit a bounded patch head;
        // the client applies it after codec/patch replay, retaining raw bytes.
        response["projectionTransform"] = json!("terminalize_restored");
    }
    encode(response).map(Some)
}

#[cfg(test)]
#[path = "conversation_diagnostic_tests.rs"]
mod tests;
