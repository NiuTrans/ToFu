//! Append-only live text behind a revision-fenced Turn projection head.
//!
//! Entry points: classify, append, materialize. Pure text appends freeze the
//! structural checkpoint/patch prefix and touch only bounded final text pages.
//! Structural writes hydrate and retire the same head through the Turn owner.
//! No cache, worker, independent authority or relaxed acknowledgement is added.

use super::*;
use crate::generated_tofudb_ir::{
    MAX_TURN_PROJECTION_TEXT_APPEND_BYTES, MAX_TURN_PROJECTION_TEXT_PAGE_BYTES,
    MAX_TURN_SNAPSHOT_BYTES,
};
use serde::{Deserialize, Serialize};

#[cfg(test)]
mod tests;

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct TextPages {
    pages: usize,
    bytes: usize,
    last_page_bytes: usize,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct TextTail {
    format: String,
    pub revisions: u64,
    pub projection_bytes: usize,
    content: TextPages,
    thinking: TextPages,
}

impl TextTail {
    pub fn page_count(&self) -> usize { self.content.pages + self.thinking.pages }

    pub fn decode(raw: &Value) -> io::Result<Self> {
        let tail: Self = serde_json::from_value(raw.clone())
            .map_err(|_| invalid_data("Turn text tail is malformed"))?;
        if tail.format != "tofu.turn-text-tail/v1" || tail.revisions == 0
            || tail.projection_bytes > MAX_TURN_SNAPSHOT_BYTES
            || tail.content.bytes.saturating_add(tail.thinking.bytes) > tail.projection_bytes
        {
            return Err(invalid_data("Turn text tail identity or size is invalid"));
        }
        for cursor in [&tail.content, &tail.thinking] {
            let maximum_pages = MAX_TURN_SNAPSHOT_BYTES / (MAX_TURN_PROJECTION_TEXT_PAGE_BYTES - 3) + 1;
            if cursor.pages > maximum_pages || cursor.bytes > tail.projection_bytes
                || (cursor.pages == 0) != (cursor.bytes == 0)
                || (cursor.pages == 0) != (cursor.last_page_bytes == 0)
                || cursor.last_page_bytes > MAX_TURN_PROJECTION_TEXT_PAGE_BYTES
                || cursor.last_page_bytes > cursor.bytes
                || cursor.pages.saturating_sub(1).saturating_mul(MAX_TURN_PROJECTION_TEXT_PAGE_BYTES - 3)
                    .saturating_add(cursor.last_page_bytes) > cursor.bytes
                || cursor.pages.saturating_mul(MAX_TURN_PROJECTION_TEXT_PAGE_BYTES) < cursor.bytes
            {
                return Err(invalid_data("Turn text page cursor exceeds its bounds"));
            }
        }
        Ok(tail)
    }
}

pub(super) struct TextAppend {
    base_revision: u64,
    content: Option<String>,
    thinking: Option<String>,
}

/// Recognize only the existing wire grammar's two top-level append_text ops.
/// All other patches stay with the ordinary, fully validated projection path.
pub(super) fn classify(request: &EventRecordRequest) -> Option<TextAppend> {
    if request.slim || request.terminal || request.status != "running"
        || request.projection_segments_stable || request.background_command_ack.is_some()
    { return None; }
    let patch = request.projection_patch.as_ref()?;
    if patch.get("version")?.as_u64()? != 1 { return None; }
    let base_revision = patch.get("baseRevision")?.as_u64()?;
    if patch.get("targetRevision")?.as_u64()? != base_revision.checked_add(1)? { return None; }
    let operations = patch.get("operations")?.as_array()?;
    if operations.len() > 2 { return None; }
    let mut append = TextAppend { base_revision, content: None, thinking: None };
    let mut bytes = 0_usize;
    for operation in operations {
        if operation.get("op")?.as_str()? != "append_text" { return None; }
        let path = operation.get("path")?.as_array()?;
        if path.len() != 1 { return None; }
        let target = match path[0].as_str()? {
            "content" => &mut append.content,
            "thinking" => &mut append.thinking,
            _ => return None,
        };
        if target.is_some() { return None; }
        let value = operation.get("value")?.as_str()?;
        bytes = bytes.checked_add(value.len())?;
        if bytes > MAX_TURN_PROJECTION_TEXT_APPEND_BYTES { return None; }
        *target = Some(value.to_owned());
    }
    Some(append)
}

fn page_key(transaction: &AuthorityTransaction, conversation_id: &str,
    turn_id: &str, head_id: &str, field: &str, page: usize) -> io::Result<EntityKey>
{
    let mut encoded = projection_head_prefix(conversation_id, turn_id, head_id)?;
    encoded.push(2);
    encoded.push(if field == "content" { 0 } else { 1 });
    encoded.extend_from_slice(&(page as u64).to_be_bytes());
    entity_key(transaction, TURN_PROJECTION_HEAD_NAMESPACE, &encoded)
}

fn logical_key(head: &ProjectionHeadDescriptor, field: &str, page: usize) -> String {
    format!("{}\u{1f}text\u{1f}{field}\u{1f}{page:020}", head.head_id)
}

fn read_page(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, turn_id: &str, head: &ProjectionHeadDescriptor,
    field: &str, page: usize) -> io::Result<(String, u64)>
{
    let key = page_key(transaction, conversation_id, turn_id, &head.head_id, field, page)?;
    let stored = database.entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("Turn text page is missing"))?;
    let version = crate::versioned_document::stored_document_version(
        &stored, PROJECTION_HEAD_DOCUMENT_IDENTITY, &logical_key(head, field, page),
    )?;
    let (_, bytes) = crate::versioned_document::materialize_stored_document_bounded(
        database, transaction.tenant_id(), transaction.owner_user_id(), &stored,
        PROJECTION_HEAD_DOCUMENT_IDENTITY, MAX_TURN_PROJECTION_TEXT_PAGE_BYTES * 6 + 1024,
    )?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| invalid_data("Turn text page cannot be decoded"))?;
    let text = value.get("text").and_then(Value::as_str)
        .filter(|text| !text.is_empty() && text.len() <= MAX_TURN_PROJECTION_TEXT_PAGE_BYTES)
        .ok_or_else(|| invalid_data("Turn text page exceeds its bound"))?;
    if value.get("page").and_then(Value::as_u64) != Some(page as u64)
        || value.get("field").and_then(Value::as_str) != Some(field)
        || value.get("attemptId").and_then(Value::as_str) != Some(&head.attempt_id)
    { return Err(invalid_data("Turn text page witness differs")); }
    Ok((text.to_owned(), version))
}

fn store_page(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, turn_id: &str, head: &ProjectionHeadDescriptor,
    field: &str, page: usize, text: &str, version: u64, now_ms: u64) -> io::Result<()>
{
    crate::versioned_document::put(database, transaction, crate::versioned_document::PutRequest {
        key: page_key(transaction, conversation_id, turn_id, &head.head_id, field, page)?,
        namespace: PROJECTION_HEAD_DOCUMENT_IDENTITY.to_owned(),
        logical_key: logical_key(head, field, page),
        value_json: serde_json::to_vec(&json!({"attemptId": head.attempt_id,
            "field": field, "page": page, "text": text}))
            .map_err(|_| invalid_data("Turn text page cannot be encoded"))?,
        expected_version: Some(version), updated_at_ms: now_ms,
    }).map(|_| ())
}

fn append_pages(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, turn_id: &str, head: &ProjectionHeadDescriptor,
    field: &str, cursor: &TextPages, suffix: &str, now_ms: u64) -> io::Result<TextPages>
{
    if suffix.is_empty() { return Ok(cursor.clone()); }
    let (mut text, mut version) = if cursor.pages == 0 { (String::new(), 0) } else {
        let loaded = read_page(database, transaction, conversation_id, turn_id, head, field, cursor.pages)?;
        if loaded.0.len() != cursor.last_page_bytes {
            return Err(invalid_data("Turn final text page length differs"));
        }
        loaded
    };
    let mut page = cursor.pages.max(1);
    let mut remaining = suffix;
    loop {
        let mut take = remaining.len().min(MAX_TURN_PROJECTION_TEXT_PAGE_BYTES - text.len());
        while !remaining.is_char_boundary(take) { take -= 1; }
        if take > 0 {
            text.push_str(&remaining[..take]);
            remaining = &remaining[take..];
            store_page(database, transaction, conversation_id, turn_id, head, field, page, &text, version, now_ms)?;
        }
        if remaining.is_empty() { break; }
        page += 1;
        text.clear();
        version = 0;
    }
    Ok(TextPages { pages: page, bytes: cursor.bytes.checked_add(suffix.len())
        .ok_or_else(|| invalid_data("Turn text length overflows"))?, last_page_bytes: text.len() })
}

pub(super) struct AppendOutcome {
    pub head: ProjectionHeadDescriptor,
    pub projection_bytes: usize,
}

pub(super) fn append(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    loaded: &LoadedTurnForAttempt, append: &TextAppend, request: &EventRecordRequest,
    conversation_id: &str, turn_id: &str) -> io::Result<Option<AppendOutcome>>
{
    let revision = loaded.document.get("projectionRevision").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Turn text append revision is malformed"))?;
    if revision != append.base_revision { return Err(typed_conflict(TurnConflictKind::ProjectionStale)); }
    let mut head = loaded.projection_head.clone().unwrap_or_else(|| ProjectionHeadDescriptor {
        head_id: projection_head_id(&request.attempt_id, revision),
        attempt_id: request.attempt_id.clone(), base_revision: revision,
        patch_count: 0, patch_bytes: 0, text_tail: None,
    });
    let mut baseline = None;
    let mut tail = if let Some(tail) = &head.text_tail { tail.clone() } else {
        let mut document = loaded.document.clone();
        if loaded.projection_head.is_some() { materialize_projection_head(database, transaction, &mut document, &head)?; }
        let projection = document.get("projection").and_then(Value::as_object)
            .ok_or_else(|| invalid_data("Turn text append base is malformed"))?;
        for (field, suffix) in [("content", &append.content), ("thinking", &append.thinking)] {
            if suffix.is_some() && !projection.get(field).is_some_and(Value::is_string) {
                return Ok(None);
            }
        }
        let projection_bytes = serde_json::to_vec(projection)
            .map_err(|_| invalid_data("Turn text append base cannot be encoded"))?.len();
        if loaded.projection_head.is_none() { baseline = Some(projection.clone()); }
        TextTail { format: "tofu.turn-text-tail/v1".to_owned(), revisions: 0,
            projection_bytes, content: TextPages::default(), thinking: TextPages::default() }
    };
    for suffix in [&append.content, &append.thinking].into_iter().flatten() {
        let encoded = serde_json::to_vec(suffix)
            .map_err(|_| invalid_data("Turn text suffix cannot be encoded"))?.len().saturating_sub(2);
        tail.projection_bytes = tail.projection_bytes.checked_add(encoded)
            .ok_or_else(|| invalid_data("Turn text projection size overflows"))?;
    }
    // Stay within the existing terminal/recovery reserve without hydrating a
    // restart anchor near the limit. The ordinary path owns those large cases.
    let reserve = MAX_TRANSACTION_IR_LITERAL_BYTES + MAX_TIMING_TRACE_PERSISTED_BYTES + 4096;
    if tail.projection_bytes.saturating_mul(2).saturating_add(reserve) > MAX_TURN_SNAPSHOT_BYTES {
        return Ok(None);
    }
    if let Some(projection) = baseline {
        store_projection_checkpoint(database, transaction, conversation_id, turn_id, &head, &projection, request.now_ms)?;
    }
    tail.content = append_pages(database, transaction, conversation_id, turn_id, &head,
        "content", &tail.content, append.content.as_deref().unwrap_or(""), request.now_ms)?;
    tail.thinking = append_pages(database, transaction, conversation_id, turn_id, &head,
        "thinking", &tail.thinking, append.thinking.as_deref().unwrap_or(""), request.now_ms)?;
    tail.revisions = tail.revisions.checked_add(1)
        .ok_or_else(|| invalid_data("Turn text revision count overflows"))?;
    let projection_bytes = tail.projection_bytes;
    head.text_tail = Some(tail);
    Ok(Some(AppendOutcome { head, projection_bytes }))
}

pub(super) fn materialize(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, turn_id: &str, head: &ProjectionHeadDescriptor,
    projection: &mut Map<String, Value>) -> io::Result<()>
{
    let Some(tail) = &head.text_tail else { return Ok(()); };
    for (field, cursor) in [("content", &tail.content), ("thinking", &tail.thinking)] {
        if cursor.pages == 0 { continue; }
        let Some(Value::String(text)) = projection.get_mut(field) else {
            return Err(invalid_data("Turn text append checkpoint field is not a string"));
        };
        let base_bytes = text.len();
        if base_bytes.saturating_add(cursor.bytes) > MAX_TURN_SNAPSHOT_BYTES {
            return Err(invalid_data("Turn materialized text exceeds its bound"));
        }
        text.reserve(cursor.bytes);
        for page in 1..=cursor.pages {
            let (suffix, _) = read_page(database, transaction, conversation_id, turn_id, head, field, page)?;
            if page == cursor.pages && suffix.len() != cursor.last_page_bytes {
                return Err(invalid_data("Turn final text page length differs"));
            }
            if text.len().saturating_sub(base_bytes).saturating_add(suffix.len()) > cursor.bytes {
                return Err(invalid_data("Turn text pages exceed their length witness"));
            }
            text.push_str(&suffix);
        }
        if text.len() - base_bytes != cursor.bytes {
            return Err(invalid_data("Turn text page byte witness differs"));
        }
    }
    // The cached complete size is a durability/recovery admission witness.
    // Verify it on hydration without allocating another complete JSON buffer.
    struct Counter(usize);
    impl io::Write for Counter {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0 = self.0.checked_add(bytes.len()).filter(|n| *n <= MAX_TURN_SNAPSHOT_BYTES)
                .ok_or_else(|| invalid_data("Turn projection encoding exceeds its bound"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> { Ok(()) }
    }
    let mut counter = Counter(0);
    serde_json::to_writer(&mut counter, projection)
        .map_err(|_| invalid_data("Turn text projection cannot be encoded"))?;
    if counter.0 != tail.projection_bytes { return Err(invalid_data("Turn text projection size witness differs")); }
    Ok(())
}
