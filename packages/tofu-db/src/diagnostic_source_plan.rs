//! One lazy source-location hint per servicing thread, not a projection cache.
//! Retains only bounded entity envelopes and a byte span (at most two entity
//! values), replaced by the next Turn and released with the worker. Both the
//! owner-scoped Turn and checkpoint envelopes must still match the current
//! snapshot before reuse. A miss streams the structure again and recomputes
//! the immutable source token; it must never accept an old descriptor's token.

use super::*;
use std::cell::RefCell;

struct Plan {
    tenant: u64,
    owner: u64,
    conversation_id: String,
    turn: Vec<u8>,
    lane_key: EntityKey,
    epoch: u64,
    token: String,
    checkpoint_key: Option<EntityKey>,
    checkpoint: Option<Vec<u8>>,
    span: Range<u64>,
}
thread_local! { static LAST: RefCell<Option<Plan>> = const { RefCell::new(None) }; }

/// Fence the reachable source generation, not its byte length. Blob envelopes
/// include the immutable manifest/content witnesses; inline envelopes include
/// the exact bytes. Length-prefix every field and include the scoped lane key
/// so a cache miss, alias, same-length replacement, or restored epoch cannot
/// splice bytes from another descriptor into an in-progress reconstruction.
pub(super) fn token(
    lane_key: &EntityKey,
    turn: &[u8],
    epoch: u64,
    checkpoint: Option<&(EntityKey, Vec<u8>)>,
    span: &Range<u64>,
) -> String {
    let mut hasher = blake3::Hasher::new_derive_key("tofu.conversation.diagnostic.source.v1");
    hasher.update(&epoch.to_be_bytes());
    hasher.update(&span.start.to_be_bytes());
    hasher.update(&span.end.to_be_bytes());
    let mut field = |bytes: &[u8]| {
        hasher.update(&(bytes.len() as u64).to_be_bytes());
        hasher.update(bytes);
    };
    field(lane_key.encoded());
    field(turn);
    if let Some((key, stored)) = checkpoint {
        field(key.encoded());
        field(stored);
    }
    hasher.finalize().to_hex().to_string()
}

pub(super) fn remember(
    tx: &AuthorityTransaction,
    conversation_id: &str,
    lane_key: &EntityKey,
    turn: &[u8],
    epoch: u64,
    checkpoint: Option<(EntityKey, Vec<u8>)>,
    span: Range<u64>,
    token: &str,
) {
    let (checkpoint_key, checkpoint) = match checkpoint {
        Some((key, value)) => (Some(key), Some(value)),
        None => (None, None),
    };
    LAST.with(|last| {
        *last.borrow_mut() = Some(Plan {
            tenant: tx.tenant_id(),
            owner: tx.owner_user_id(),
            conversation_id: conversation_id.to_owned(),
            turn: turn.to_vec(),
            lane_key: lane_key.clone(),
            epoch,
            token: token.to_owned(),
            checkpoint_key,
            checkpoint,
            span,
        })
    });
}

pub(super) fn chunk(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    lane_key: &EntityKey,
    stored_turn: &[u8],
    epoch: u64,
    request: &Request,
    revision: u64,
) -> io::Result<Option<Vec<u8>>> {
    LAST.with(|last| {
        let last = last.borrow();
        let Some(plan) = last.as_ref().filter(|plan| {
            plan.tenant == tx.tenant_id()
                && plan.owner == tx.owner_user_id()
                && plan.conversation_id == request.conversation_id
                && plan.turn == stored_turn
                && plan.lane_key == *lane_key
                && plan.epoch == epoch
        }) else {
            return Ok(None);
        };
        if let Some(key) = &plan.checkpoint_key {
            let stored = database.entity_get(tx, key)?;
            if stored.as_ref() != plan.checkpoint.as_ref() {
                return Ok(None);
            }
            let source = Source::open(
                database,
                tx,
                stored.as_ref().unwrap(),
                PROJECTION_HEAD_DOCUMENT_IDENTITY,
            )?;
            encode_chunk(&source, &plan.span, request, revision, &plan.token).map(Some)
        } else {
            let source = Source::open(database, tx, stored_turn, DOCUMENT_IDENTITY)?;
            encode_chunk(&source, &plan.span, request, revision, &plan.token).map(Some)
        }
    })
}

#[cfg(test)]
pub(super) fn clear() {
    LAST.with(|last| *last.borrow_mut() = None);
}
