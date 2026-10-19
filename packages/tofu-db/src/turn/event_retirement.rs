//! Deferred canonical-event reclamation for atomically detached conversations.
//!
//! Enqueue captures immutable identity sources in the delete transaction. The
//! existing Attempt-event maintenance operation advances a bounded durable cursor.
//! Identity claims remain reserved until this cursor is gone or purge reclaims
//! the complete retained identity set synchronously. No worker or authority lives here.

use super::*;
use crate::generated_tofudb_ir::CONVERSATION_EVENT_RETIREMENT_NAMESPACE as NAMESPACE;

#[derive(serde::Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Job {
    conversation_id: String,
    execution_epoch: u64,
    phase: u8,
    after: Vec<u8>,
    pending: Vec<String>,
    pending_index: usize,
    next_sequence: u64,
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct TurnIdentity {
    conversation_id: String,
    turn_id: String,
    current_attempt_id: Option<String>,
}

fn job_prefix(conversation_id: &str) -> io::Result<Vec<u8>> {
    let mut key = vec![b'j'];
    key.extend_from_slice(&conversation_prefix(conversation_id)?);
    Ok(key)
}

fn job_key(tx: &AuthorityTransaction, conversation_id: &str, epoch: u64) -> io::Result<EntityKey> {
    let mut key = job_prefix(conversation_id)?;
    key.extend_from_slice(&epoch.to_be_bytes());
    entity_key(tx, NAMESPACE, &key)
}

fn pin_id(tx: &AuthorityTransaction, key: &EntityKey) -> Vec<u8> {
    let mut digest = Sha256::new();
    digest.update(b"conversation-event-retirement/v1");
    digest.update(tx.tenant_id().to_be_bytes());
    digest.update(tx.owner_user_id().to_be_bytes());
    digest.update(key.key_bytes());
    digest.finalize().to_vec()
}

fn decode(tx: &AuthorityTransaction, key: &EntityKey, encoded: &[u8]) -> io::Result<Job> {
    if encoded.len() > crate::generated_tofudb_ir::MAX_ATTEMPT_TURN_DIRECTORY_BYTES {
        return Err(invalid_data(
            "conversation event retirement cursor exceeds its bound",
        ));
    }
    let job: Job = serde_json::from_slice(encoded)
        .map_err(|_| invalid_data("conversation event retirement cursor is malformed"))?;
    let prefix = conversation_prefix(&job.conversation_id)?;
    if job_key(tx, &job.conversation_id, job.execution_epoch)? != *key
        || job.phase > 2
        || job.pending.len() > MAX_ATTEMPTS_PER_TURN
        || job.pending_index > job.pending.len()
        || job.next_sequence == 0
        // Public limits count Unicode characters; keys carry UTF-8 bytes.
        // Tombstone keys include an eight-byte clock after the CID prefix.
        || job.after.len() > 2 + 4 * 256 + 8 + 4 * 128
        || (!job.after.is_empty() && !job.after.starts_with(&prefix))
        || job.pending.iter().any(|id| id.is_empty() || id.chars().count() > 128)
    {
        return Err(invalid_data(
            "conversation event retirement identity differs",
        ));
    }
    Ok(job)
}

pub(crate) fn enqueue(
    database: &mut AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    execution_epoch: u64,
) -> io::Result<()> {
    let key = job_key(tx, conversation_id, execution_epoch)?;
    if database.entity_get(tx, &key)?.is_some() {
        return Err(invalid_data(
            "conversation event retirement generation already exists",
        ));
    }
    let prefix = conversation_prefix(conversation_id)?;
    let ranges = [
        TURN_DOCUMENT_NAMESPACE,
        TURN_TOMBSTONE_NAMESPACE,
        ATTEMPT_TURN_DIRECTORY_NAMESPACE,
        ATTEMPT_TURN_COUNT_NAMESPACE,
    ]
    .into_iter()
    .map(|namespace| {
        EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), namespace, &prefix)
    })
    .collect::<io::Result<Vec<_>>>()?;
    // A header-only conversation has no identity source to pin. This probes
    // at most one small stored envelope, without materializing any payload.
    if database
        .entity_scan(tx, &ranges[0].0, &ranges[0].1, 1)?
        .is_empty()
        && database
            .entity_scan(tx, &ranges[1].0, &ranges[1].1, 1)?
            .is_empty()
    {
        return Ok(());
    }
    database.stage_persistent_range_snapshot_pin(tx, &pin_id(tx, &key), &ranges)?;
    let job = Job {
        conversation_id: conversation_id.to_owned(),
        execution_epoch,
        phase: 0,
        after: Vec::new(),
        pending: Vec::new(),
        pending_index: 0,
        next_sequence: 1,
    };
    database.entity_put(
        tx,
        key,
        serde_json::to_vec(&job)
            .map_err(|_| invalid_data("conversation event retirement cannot be encoded"))?,
    )
}

pub(super) fn pending(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<bool> {
    let (start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        NAMESPACE,
        &job_prefix(conversation_id)?,
    )?;
    Ok(!database.entity_scan(tx, &start, &end, 1)?.is_empty())
}

/// Return None when normal settled-event retention can use this transaction.
pub(super) fn sweep(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &EventPruneRequest,
) -> io::Result<Option<Vec<u8>>> {
    let cursor_key = entity_key(tx, NAMESPACE, b"cursor")?;
    let (first, end) =
        EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), NAMESPACE, b"j")?;
    let start = if let Some(mut after) = database.entity_get(tx, &cursor_key)? {
        if after.first() != Some(&b'j') || after.len() > 1 + 2 + 4 * 256 + 8 {
            return Err(invalid_data(
                "conversation event retirement scheduler cursor is malformed",
            ));
        }
        after.push(0);
        entity_key(tx, NAMESPACE, &after)?
    } else {
        first.clone()
    };
    let mut candidates = database.entity_scan(tx, &start, &end, 1)?;
    if candidates.is_empty() && start != first {
        candidates = database.entity_scan(tx, &first, &end, 1)?;
    }
    let Some((key, encoded)) = candidates.pop() else {
        if database.entity_get(tx, &cursor_key)?.is_some() {
            database.entity_delete(tx, cursor_key)?;
        }
        return Ok(None);
    };
    let mut job = decode(tx, &key, &encoded)?;
    let pin = pin_id(tx, &key);
    let mut source = database
        .begin_persistent_snapshot(tx.tenant_id(), tx.owner_user_id(), &pin)?
        .ok_or_else(|| invalid_data("conversation event retirement source is missing"))?;
    let step_limit = request
        .max_attempts
        .min(MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION);
    let mut documents = 0;
    let mut attempts = 0;
    let mut deleted_rows = 0;
    let mut deleted_attempts = 0;
    let mut source_bytes = 0;
    while attempts < step_limit && deleted_rows < request.max_rows {
        if job.pending_index == job.pending.len() {
            job.pending.clear();
            job.pending_index = 0;
            job.next_sequence = 1;
            if job.phase == 2 || documents == step_limit {
                break;
            }
            let namespace = if job.phase == 0 {
                TURN_DOCUMENT_NAMESPACE
            } else {
                TURN_TOMBSTONE_NAMESPACE
            };
            let prefix = conversation_prefix(&job.conversation_id)?;
            let (first, end) = EntityKey::prefix_range(
                source.tenant_id(),
                source.owner_user_id(),
                namespace,
                &prefix,
            )?;
            let start = if job.after.is_empty() {
                first
            } else {
                let mut after = job.after.clone();
                after.push(0);
                entity_key(&source, namespace, &after)?
            };
            let Some((source_key, stored)) =
                database.entity_scan(&mut source, &start, &end, 1)?.pop()
            else {
                job.phase += 1;
                job.after.clear();
                continue;
            };
            if job.phase == 0 {
                let bytes = usize::try_from(
                    crate::versioned_document::stored_document_logical_bytes(
                        &stored,
                        DOCUMENT_IDENTITY,
                    )?,
                )
                .map_err(|_| invalid_data("conversation event retirement source size overflows"))?;
                if bytes > MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES {
                    return Err(invalid_data(
                        "conversation event retirement source exceeds its bound",
                    ));
                }
                if source_bytes + bytes > MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES {
                    break;
                }
                let (_, raw) = crate::versioned_document::materialize_stored_document_bounded(
                    database,
                    source.tenant_id(),
                    source.owner_user_id(),
                    &stored,
                    DOCUMENT_IDENTITY,
                    MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES - source_bytes,
                )?;
                let identity: TurnIdentity = serde_json::from_slice(&raw)
                    .map_err(|_| invalid_data("conversation event retirement Turn is malformed"))?;
                if identity.conversation_id != job.conversation_id
                    || turn_key(&source, &identity.conversation_id, &identity.turn_id)?
                        != source_key
                {
                    return Err(invalid_data(
                        "conversation event retirement source identity differs",
                    ));
                }
                source_bytes += bytes;
                job.pending = load_attempt_identity_ids(
                    database,
                    &mut source,
                    &identity.conversation_id,
                    &identity.turn_id,
                    identity.current_attempt_id.as_deref(),
                )?;
            } else {
                decode_tombstone_turn_id(&source_key, prefix.len())?;
                job.pending = decode_tombstone_attempt_ids(&stored)?;
            }
            documents += 1;
            job.after = source_key.key_bytes().to_vec();
            if job.pending.is_empty() {
                continue;
            }
        }
        let attempt_id = &job.pending[job.pending_index];
        let head_key = attempt_event_head_key(tx, attempt_id)?;
        let stored_head = database.entity_get(tx, &head_key)?;
        let head = decode_u64(
            stored_head.clone(),
            "attempt event retirement head is malformed",
        )?;
        let (range_start, range_end) = EntityKey::prefix_range(
            tx.tenant_id(),
            tx.owner_user_id(),
            ATTEMPT_EVENT_NAMESPACE,
            &attempt_event_prefix(attempt_id)?,
        )?;
        if database
            .entity_scan(tx, &range_start, &range_end, 1)?
            .is_empty()
        {
            if stored_head.is_some() {
                database.entity_delete(tx, head_key)?;
            }
            attempts += 1;
            job.pending_index += 1;
            job.next_sequence = 1;
            continue;
        }
        if head == 0 || job.next_sequence > head {
            return Err(invalid_data(
                "attempt event retirement source head is missing or stale",
            ));
        }
        let available = usize::try_from(head - job.next_sequence + 1).unwrap_or(usize::MAX);
        let retiring = available.min(request.max_rows - deleted_rows);
        let completes = retiring == available;
        let start = attempt_event_key(tx, attempt_id, job.next_sequence)?;
        let end = if completes {
            range_end
        } else {
            attempt_event_key(tx, attempt_id, job.next_sequence + retiring as u64)?
        };
        database.entity_retire_range(tx, &start, &end)?;
        deleted_rows += retiring;
        attempts += 1;
        if completes {
            database.entity_delete(tx, head_key)?;
            deleted_attempts += 1;
            job.pending_index += 1;
            job.next_sequence = 1;
        } else {
            job.next_sequence += retiring as u64;
            break;
        }
    }
    drop(source);
    if job.phase == 2 && job.pending_index == job.pending.len() {
        if !database.remove_persistent_snapshot_pin(tx, &pin)? {
            return Err(invalid_data(
                "conversation event retirement source disappeared",
            ));
        }
        database.entity_delete(tx, key.clone())?;
    } else {
        database.entity_put(
            tx,
            key.clone(),
            serde_json::to_vec(&job)
                .map_err(|_| invalid_data("conversation event retirement cannot be encoded"))?,
        )?;
    }
    let remaining = !database.entity_scan(tx, &first, &end, 1)?.is_empty();
    if remaining {
        database.entity_put(tx, cursor_key, key.key_bytes().to_vec())?;
    } else {
        database.entity_delete(tx, cursor_key)?;
    }
    encode_response(
        &json!({"deleted_rows": deleted_rows, "deleted_attempts": deleted_attempts,
        "remaining": usize::from(remaining)}),
    )
    .map(Some)
}

/// Purge already proves the complete bounded identity set before releasing any
/// claim. Reclaim all its events first, then discard every older cleanup cursor.
pub(crate) fn reclaim_for_purge(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    records: &[ConversationIdentityRecord],
) -> io::Result<()> {
    let (start, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        NAMESPACE,
        &job_prefix(conversation_id)?,
    )?;
    let jobs = database.entity_scan(
        tx,
        &start,
        &end,
        MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION + 1,
    )?;
    if jobs.len() > MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "conversation event retirement must make progress before purge",
        ));
    }
    for record in records {
        for attempt_id in &record.attempt_ids {
            database.entity_delete(tx, attempt_event_head_key(tx, attempt_id)?)?;
            let (start, end) = EntityKey::prefix_range(
                tx.tenant_id(),
                tx.owner_user_id(),
                ATTEMPT_EVENT_NAMESPACE,
                &attempt_event_prefix(attempt_id)?,
            )?;
            if !database.entity_scan(tx, &start, &end, 1)?.is_empty() {
                database.entity_retire_range(tx, &start, &end)?;
            }
        }
    }
    for (key, raw) in jobs {
        decode(tx, &key, &raw)?;
        if !database.remove_persistent_snapshot_pin(tx, &pin_id(tx, &key))? {
            return Err(invalid_data("purged event retirement source disappeared"));
        }
        database.entity_delete(tx, key)?;
    }
    let (first, end) =
        EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), NAMESPACE, b"j")?;
    if database.entity_scan(tx, &first, &end, 1)?.is_empty() {
        let cursor = entity_key(tx, NAMESPACE, b"cursor")?;
        if database.entity_get(tx, &cursor)?.is_some() {
            database.entity_delete(tx, cursor)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
