//! Transactional accepted-attempt catalogue maintenance and evidenced legacy repair.
//! The parent Turn owner calls these at attempt transitions, deletion and restart.
//! No user state is removed; unmatched/corrupt catalogue entries remain fenced.
use super::*;

/// Conversation deletion detaches attempt documents by range retirement, but
/// the tenant-global dispatch catalogue lies outside those ranges. Every
/// dispatchable attempt is lane-live, so only live attempts are read.
pub(crate) fn retire_conversation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    let prefix = conversation_prefix(conversation_id)?;
    let (mut cursor, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_LIVE_ATTEMPT_NAMESPACE,
        &prefix,
    )?;
    loop {
        let page = database.entity_scan(
            transaction,
            &cursor,
            &end,
            crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS,
        )?;
        let Some((last_key, _)) = page.last() else {
            return Ok(());
        };
        let next_cursor = after_key(last_key, transaction, TURN_LANE_LIVE_ATTEMPT_NAMESPACE)?;
        let full_page = page.len() == crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
        for (key, _) in page {
            let attempt_id = lane_live_attempt_id(&key, prefix.len())?;
            let loaded = load_attempt_for_update(database, transaction, &attempt_id)?
                .ok_or_else(|| invalid_data("lane live attempt target is missing"))?;
            if loaded.conversation_id != conversation_id
                || loaded.document.get("attemptId").and_then(Value::as_str)
                    != Some(attempt_id.as_str())
            {
                return Err(invalid_data("lane live attempt target differs"));
            }
            update(database, transaction, &loaded.document, None)?;
            let created_at = loaded
                .document
                .get("createdAt")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
            database.entity_delete(
                transaction,
                recovery_index_key(transaction, created_at, &attempt_id)?,
            )?;
        }
        if !full_page {
            return Ok(());
        }
        cursor = next_cursor;
    }
}

fn lane_live_attempt_id(key: &EntityKey, conversation_prefix_len: usize) -> io::Result<String> {
    let malformed = || invalid_data("lane live attempt key is malformed");
    let encoded = key
        .key_bytes()
        .get(conversation_prefix_len..)
        .ok_or_else(malformed)?;
    let text_range = |offset: usize| -> io::Result<(usize, usize)> {
        let length = encoded.get(offset..offset + 2).ok_or_else(malformed)?;
        let start = offset + 2;
        let end = start + usize::from(u16::from_be_bytes([length[0], length[1]]));
        if end > encoded.len() {
            return Err(malformed());
        }
        Ok((start, end))
    };
    let (_, lane_end) = text_range(0)?;
    let (attempt_start, attempt_end) = text_range(lane_end)?;
    if attempt_end != encoded.len() || attempt_start == attempt_end {
        return Err(malformed());
    }
    std::str::from_utf8(&encoded[attempt_start..attempt_end])
        .map(str::to_owned)
        .map_err(|_| malformed())
}

/// Exactly the attempt state `turn.attempt.dispatchable.list` may hand to the
/// accepted-attempt backstop: accepted, unbound, unqueued executor work.
pub(super) fn is_dispatchable(attempt: &Map<String, Value>) -> bool {
    attempt.get("status").and_then(Value::as_str) == Some("pending")
        && attempt.get("taskId").and_then(Value::as_str) == Some("")
        && attempt.get("_queueState").and_then(Value::as_str) == Some("")
        && attempt.get("_dispatchMode").and_then(Value::as_str) == Some("conversation_executor")
}

/// Derives the tenant-global dispatch catalogue from attempt state inside the
/// transaction that changes it; `next = None` means the attempt is removed.
/// The reader fails closed on any entry whose target left the state, so no
/// transition may leave one behind.
pub(super) fn update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    previous: &Map<String, Value>,
    next: Option<&Map<String, Value>>,
) -> io::Result<()> {
    let was_dispatchable = is_dispatchable(previous);
    let is_dispatchable = next.is_some_and(is_dispatchable);
    if was_dispatchable == is_dispatchable {
        return Ok(());
    }
    let attempt = match next {
        Some(next) if is_dispatchable => next,
        _ => previous,
    };
    let identity = |field: &str| {
        attempt
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt dispatchable identity is malformed"))
    };
    let created_at_ms = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt creation timestamp is malformed"))?;
    let attempt_id = identity("attemptId")?;
    let key = dispatchable_index_key(transaction, created_at_ms, attempt_id)?;
    if !is_dispatchable {
        return database.entity_delete(transaction, key);
    }
    let value = dispatchable_index_value(
        transaction.owner_user_id(),
        identity("conversationId")?,
        identity("turnId")?,
        attempt_id,
    )?;
    database.entity_put(transaction, key, value)
}

/// Restart repair for dispatch-catalogue entries that older engines stranded
/// when they settled an accepted attempt or deleted its conversation without
/// catalogue maintenance. Scans the tenant catalogue below the recovery
/// cutoff in bounded pages, and retires only this owner's entries whose target
/// is settled or has an exact retained trash-directory witness. Unexplained
/// entries remain for the strict reader; repair does not silently drop them.
pub(super) fn repair(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    created_before_ms: Option<u64>,
) -> io::Result<()> {
    let tenant_id = transaction.tenant_id();
    let global_key = |encoded: &[u8]| {
        EntityKey::new(
            tenant_id,
            TENANT_GLOBAL_OWNER_ID,
            ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
            encoded,
        )
    };
    let mut cursor = global_key(b"")?;
    let end = match created_before_ms {
        Some(cutoff) => global_key(&cutoff.to_be_bytes())?,
        None => {
            EntityKey::prefix_range(
                tenant_id,
                TENANT_GLOBAL_OWNER_ID,
                ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
                b"",
            )?
            .1
        }
    };
    loop {
        // A tenant prefix of another owner's entries must not starve this
        // owner's repair. Page size bounds materialization, not accepted work.
        let limit = crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
        let page = database.entity_scan(transaction, &cursor, &end, limit)?;
        let Some((last_key, _)) = page.last() else {
            break;
        };
        let mut next = last_key.key_bytes().to_vec();
        next.push(0);
        cursor = global_key(&next)?;
        let full_page = page.len() == limit;
        for (index_key, locator) in page {
            if let Some((attempt_id, reason)) =
                stranded_dispatchable_entry(database, transaction, &index_key, &locator)?
            {
                database.entity_delete(transaction, index_key)?;
                crate::engine_log::warn(
                    "integrity_fence",
                    &format!(
                        "operation=turn.recover staged_repair=attempt_dispatchable_index \
                         attemptId={attempt_id} reason={reason}"
                    ),
                );
            }
        }
        if !full_page {
            break;
        }
    }
    Ok(())
}

fn stranded_dispatchable_entry(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    index_key: &EntityKey,
    locator: &[u8],
) -> io::Result<Option<(String, &'static str)>> {
    let Some(locator) = serde_json::from_slice::<Value>(locator)
        .ok()
        .and_then(|value| value.as_object().cloned())
    else {
        return Ok(None);
    };
    if locator.get("userId").and_then(Value::as_u64) != Some(transaction.owner_user_id()) {
        return Ok(None);
    }
    let identity = |field: &str| {
        locator
            .get(field)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
    };
    let (Some(conversation_id), Some(turn_id), Some(attempt_id)) = (
        identity("conversationId"),
        identity("turnId"),
        identity("attemptId"),
    ) else {
        return Ok(None);
    };
    let Some(created_at_ms) = index_key
        .key_bytes()
        .get(..8)
        .map(|bytes| u64::from_be_bytes(bytes.try_into().unwrap()))
    else {
        return Ok(None);
    };
    if *index_key != dispatchable_index_key(transaction, created_at_ms, attempt_id)? {
        return Ok(None);
    }
    let Some(stored) = database.entity_get(
        transaction,
        &attempt_key(transaction, conversation_id, attempt_id)?,
    )?
    else {
        let deleted = deleted_attempt_witness(
            database,
            transaction,
            conversation_id,
            turn_id,
            attempt_id,
            created_at_ms,
            Some("conversation_executor"),
        )?;
        return Ok(deleted.then(|| (attempt_id.to_owned(), "conversation_deleted")));
    };
    let materialized = crate::versioned_document::stored_document_version(
        &stored,
        "generation_attempts",
        attempt_id,
    )
    .and_then(|_| {
        crate::versioned_document::materialize_stored_document(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &stored,
            "generation_attempts",
        )
    });
    let attempt = match materialized {
        Ok((_, document)) => serde_json::from_slice::<Value>(&document)
            .ok()
            .and_then(|value| value.as_object().cloned()),
        Err(error) if error.kind() == io::ErrorKind::InvalidData => None,
        Err(error) => return Err(error),
    };
    let settled = attempt.is_some_and(|attempt| {
        attempt.get("attemptId").and_then(Value::as_str) == Some(attempt_id)
            && attempt.get("conversationId").and_then(Value::as_str) == Some(conversation_id)
            && attempt.get("turnId").and_then(Value::as_str) == Some(turn_id)
            && attempt.get("createdAt").and_then(Value::as_u64) == Some(created_at_ms)
            && attempt.get("_dispatchMode").and_then(Value::as_str) == Some("conversation_executor")
            && matches!(
                attempt.get("status").and_then(Value::as_str),
                Some("completed" | "interrupted" | "truncated" | "failed" | "superseded")
            )
    });
    Ok(settled.then(|| (attempt_id.to_owned(), "settled")))
}

pub(super) fn deleted_attempt_witness(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    attempt_id: &str,
    created_at_ms: u64,
    required_dispatch_mode: Option<&str>,
) -> io::Result<bool> {
    if crate::conversation_header::active_exists(database, transaction, conversation_id)? {
        return Ok(false);
    }
    // Deletion intentionally excludes executable Attempt documents from the
    // trash capsule. The retained Turn directory is its exact identity witness.
    let pin_id = crate::conversation_header::trash_pin_id(transaction, conversation_id);
    let Some(mut trash) = database.begin_persistent_snapshot(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &pin_id,
    )?
    else {
        return Ok(false);
    };
    let entries =
        match load_attempt_turn_directory(database, &mut trash, conversation_id, turn_id, None) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::InvalidData => return Ok(false),
            Err(error) => return Err(error),
        };
    Ok(entries.iter().any(|entry| {
        entry.get("attemptId").and_then(Value::as_str) == Some(attempt_id)
            && entry.get("createdAt").and_then(Value::as_u64) == Some(created_at_ms)
            && required_dispatch_mode
                .is_none_or(|mode| entry.get("dispatchMode").and_then(Value::as_str) == Some(mode))
            && entry.get("identityOnly").and_then(Value::as_bool) != Some(true)
    }))
}
