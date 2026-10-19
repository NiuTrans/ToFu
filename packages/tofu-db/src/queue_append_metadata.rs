//! Owner-local queue append metadata maintained with the ordinary queue transaction.
//! Entries share the reserved state namespace: item states use tenant-global owner
//! zero, while append metadata uses the authenticated owner and conversation key.
//! Missing legacy metadata takes the complete queue scan; corruption fails visibly.
//! Entry points: eligible, rebuild, append and remove. Dependencies: queue codecs,
//! summary keys and the existing AuthorityDatabase transaction/ownership boundary.

use super::{
    decode, encode, invalid_data, owner_key, summary_key, AuthorityDatabase, AuthorityTransaction,
    EntityKey, IndexIdentity, KINDS, QUEUE_ITEM_STATE_NAMESPACE,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::io;

const FORMAT: &str = "tofu.queue-append/v1";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Metadata {
    format: String,
    owner_user_id: u64,
    conversation_id: String,
    pub(super) item_count: u64,
    oldest_by_kind: BTreeMap<String, u64>,
}

fn key(tx: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    owner_key(tx, QUEUE_ITEM_STATE_NAMESPACE, conversation_id.as_bytes())
}

pub(super) fn eligible(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<Metadata>> {
    let Some(raw) = database.entity_get(tx, &key(tx, conversation_id)?)? else {
        return Ok(None);
    };
    let value: Metadata = decode(&raw, "queue append metadata")?;
    if value.format != FORMAT
        || value.owner_user_id != tx.owner_user_id()
        || value.conversation_id != conversation_id
        || value.item_count == 0
        || value.oldest_by_kind.is_empty()
        || value
            .oldest_by_kind
            .keys()
            .any(|kind| !KINDS.contains(&kind.as_str()))
    {
        return Err(invalid_data(
            "queue append metadata identity or format differs",
        ));
    }
    if value.oldest_by_kind.contains_key("goal_continuation") {
        return Ok(None);
    }
    Ok(Some(value))
}

fn store(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    value: &Metadata,
) -> io::Result<()> {
    database.entity_put(
        tx,
        key(tx, &value.conversation_id)?,
        encode(value, "queue append metadata")?,
    )
}

pub(super) fn remove(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    database.entity_delete(tx, key(tx, conversation_id)?)
}

pub(super) fn rebuild(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    items: &[IndexIdentity],
) -> io::Result<()> {
    if items.is_empty() {
        return Ok(());
    }
    let mut oldest_by_kind = BTreeMap::<String, u64>::new();
    for item in items {
        if item.owner != tx.owner_user_id() || item.conversation_id != items[0].conversation_id {
            return Err(invalid_data(
                "queue append metadata crosses owner or conversation",
            ));
        }
        oldest_by_kind
            .entry(item.kind.clone())
            .and_modify(|time| *time = (*time).min(item.created_at_ms))
            .or_insert(item.created_at_ms);
    }
    store(
        database,
        tx,
        &Metadata {
            format: FORMAT.into(),
            owner_user_id: tx.owner_user_id(),
            conversation_id: items[0].conversation_id.clone(),
            item_count: u64::try_from(items.len())
                .map_err(|_| invalid_data("queue item count overflow"))?,
            oldest_by_kind,
        },
    )
}

fn summary_times(value: &Metadata) -> BTreeMap<Option<String>, u64> {
    let mut times = BTreeMap::new();
    for (kind, time) in &value.oldest_by_kind {
        times.insert(Some(kind.clone()), *time);
        if kind != "autopilot" && kind != "background_receipt" {
            times
                .entry(None)
                .and_modify(|old| *old = (*old).min(*time))
                .or_insert(*time);
        }
    }
    times
}

pub(super) fn append(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    mut value: Metadata,
    item: &IndexIdentity,
) -> io::Result<()> {
    if item.owner != value.owner_user_id
        || item.conversation_id != value.conversation_id
        || item.kind != "real"
    {
        return Err(invalid_data("queue append metadata item differs"));
    }
    let old = summary_times(&value);
    value.item_count = value
        .item_count
        .checked_add(1)
        .ok_or_else(|| invalid_data("queue item count overflow"))?;
    value
        .oldest_by_kind
        .entry(item.kind.clone())
        .and_modify(|time| *time = (*time).min(item.created_at_ms))
        .or_insert(item.created_at_ms);
    for (kind, time) in summary_times(&value) {
        if old.get(&kind) == Some(&time) {
            continue;
        }
        if let Some(previous) = old.get(&kind) {
            database.entity_delete(
                tx,
                summary_key(
                    tx,
                    kind.as_deref(),
                    *previous,
                    item.owner,
                    &item.conversation_id,
                )?,
            )?;
        }
        database.entity_put(
            tx,
            summary_key(tx, kind.as_deref(), time, item.owner, &item.conversation_id)?,
            encode(
                &serde_json::json!({"userId":item.owner,"convId":item.conversation_id}),
                "queue summary",
            )?,
        )?;
    }
    store(database, tx, &value)
}
