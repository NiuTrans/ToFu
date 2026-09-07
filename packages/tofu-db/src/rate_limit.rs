//! Exact owner-scoped sliding-window admission without event-range scans.
//!
//! Each event updates a fixed nine-node radix-256 timestamp counter path.
//! A window count reads the root plus at most 8*255 sibling counters, while a
//! global expiry index removes at most 256 events and their counter deltas in
//! the same authority transaction.

use std::collections::BTreeMap;
use std::io;

use serde_json::json;
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_RATE_LIMIT_CLIENT_KEY_CHARACTERS, MAX_RATE_LIMIT_ENDPOINT_CHARACTERS,
    MAX_RATE_LIMIT_EVENTS_PER_WINDOW, MAX_RATE_LIMIT_EVENT_ID_CHARACTERS,
    MAX_RATE_LIMIT_PRUNE_ROWS, MAX_RATE_LIMIT_WINDOW_COUNTER_READS, MAX_RATE_LIMIT_WINDOW_SECONDS,
    RATE_LIMIT_BUCKET_NAMESPACE, RATE_LIMIT_COUNTER_DEPTH, RATE_LIMIT_COUNTER_RADIX,
    RATE_LIMIT_COUNT_NAMESPACE, RATE_LIMIT_EVENT_NAMESPACE, RATE_LIMIT_EXPIRY_NAMESPACE,
};

const BUCKET_MAGIC: &[u8; 8] = b"TDBRLB01";
const EVENT_MAGIC: &[u8; 8] = b"TDBRLE01";
const TIMESTAMP_BYTES: usize = 8;
const COUNTER_DEPTH: usize = TIMESTAMP_BYTES;
const _: () = assert!(RATE_LIMIT_COUNTER_RADIX == 256);
const _: () = assert!(RATE_LIMIT_COUNTER_DEPTH == COUNTER_DEPTH);
const _: () = assert!(MAX_RATE_LIMIT_WINDOW_COUNTER_READS == COUNTER_DEPTH * 255);

#[derive(Clone, Debug)]
pub struct RecordAndCheckRequest {
    pub endpoint: String,
    pub client_key: String,
    pub event_id: String,
    pub limit: u64,
    pub per_seconds: u64,
    pub now_ms: u64,
}

#[derive(Clone, Debug)]
struct Bucket {
    endpoint: String,
    client_key: String,
    last_now_ms: u64,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn bucket_digest(endpoint: &str, client_key: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:rate-limit-bucket:v1\0");
    for value in [endpoint.as_bytes(), client_key.as_bytes()] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value);
    }
    hasher.finalize().into()
}

fn scoped_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    key: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        key,
    )
}

fn bucket_key(transaction: &AuthorityTransaction, digest: &[u8; 32]) -> io::Result<EntityKey> {
    scoped_key(transaction, RATE_LIMIT_BUCKET_NAMESPACE, digest)
}

fn event_key(transaction: &AuthorityTransaction, event_id: &str) -> io::Result<EntityKey> {
    scoped_key(transaction, RATE_LIMIT_EVENT_NAMESPACE, event_id.as_bytes())
}

fn expiry_key(
    transaction: &AuthorityTransaction,
    expires_at_ms: u64,
    event_id: &str,
) -> io::Result<EntityKey> {
    let mut key = Vec::with_capacity(8 + event_id.len());
    key.extend_from_slice(&expires_at_ms.to_be_bytes());
    key.extend_from_slice(event_id.as_bytes());
    scoped_key(transaction, RATE_LIMIT_EXPIRY_NAMESPACE, &key)
}

fn expiry_range(
    transaction: &AuthorityTransaction,
    now_ms: u64,
) -> io::Result<(EntityKey, EntityKey)> {
    let start = scoped_key(transaction, RATE_LIMIT_EXPIRY_NAMESPACE, b"")?;
    let end = match now_ms.checked_add(1) {
        Some(exclusive) => scoped_key(
            transaction,
            RATE_LIMIT_EXPIRY_NAMESPACE,
            &exclusive.to_be_bytes(),
        )?,
        None => {
            EntityKey::prefix_range(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                RATE_LIMIT_EXPIRY_NAMESPACE,
                b"",
            )?
            .1
        }
    };
    Ok((start, end))
}

fn count_key(
    transaction: &AuthorityTransaction,
    digest: &[u8; 32],
    prefix: &[u8],
) -> io::Result<EntityKey> {
    if prefix.len() > COUNTER_DEPTH {
        return Err(invalid_input("rate-limit counter prefix exceeds its depth"));
    }
    let mut key = Vec::with_capacity(33 + prefix.len());
    key.extend_from_slice(digest);
    key.push(prefix.len() as u8);
    key.extend_from_slice(prefix);
    scoped_key(transaction, RATE_LIMIT_COUNT_NAMESPACE, &key)
}

fn encode_bucket(bucket: &Bucket) -> io::Result<Vec<u8>> {
    let endpoint = bucket.endpoint.as_bytes();
    let client_key = bucket.client_key.as_bytes();
    let endpoint_len = u16::try_from(endpoint.len())
        .map_err(|_| invalid_input("rate-limit endpoint encoding exceeds its bound"))?;
    let client_len = u16::try_from(client_key.len())
        .map_err(|_| invalid_input("rate-limit client-key encoding exceeds its bound"))?;
    let mut encoded = Vec::with_capacity(20 + endpoint.len() + client_key.len());
    encoded.extend_from_slice(BUCKET_MAGIC);
    encoded.extend_from_slice(&endpoint_len.to_be_bytes());
    encoded.extend_from_slice(&client_len.to_be_bytes());
    encoded.extend_from_slice(&bucket.last_now_ms.to_be_bytes());
    encoded.extend_from_slice(endpoint);
    encoded.extend_from_slice(client_key);
    Ok(encoded)
}

fn decode_bucket(encoded: &[u8]) -> io::Result<Bucket> {
    if encoded.len() < 20 || &encoded[..8] != BUCKET_MAGIC {
        return Err(invalid_data("rate-limit bucket is malformed"));
    }
    let endpoint_len = usize::from(u16::from_be_bytes(encoded[8..10].try_into().unwrap()));
    let client_len = usize::from(u16::from_be_bytes(encoded[10..12].try_into().unwrap()));
    let expected = 20_usize
        .checked_add(endpoint_len)
        .and_then(|value| value.checked_add(client_len))
        .ok_or_else(|| invalid_data("rate-limit bucket length overflows"))?;
    if encoded.len() != expected {
        return Err(invalid_data("rate-limit bucket length is malformed"));
    }
    let endpoint = std::str::from_utf8(&encoded[20..20 + endpoint_len])
        .map_err(|_| invalid_data("rate-limit endpoint is malformed"))?
        .to_owned();
    let client_key = std::str::from_utf8(&encoded[20 + endpoint_len..])
        .map_err(|_| invalid_data("rate-limit client key is malformed"))?
        .to_owned();
    Ok(Bucket {
        endpoint,
        client_key,
        last_now_ms: u64::from_be_bytes(encoded[12..20].try_into().unwrap()),
    })
}

fn encode_event(digest: &[u8; 32], occurred_at_ms: u64, expires_at_ms: u64) -> Vec<u8> {
    let mut encoded = Vec::with_capacity(56);
    encoded.extend_from_slice(EVENT_MAGIC);
    encoded.extend_from_slice(digest);
    encoded.extend_from_slice(&occurred_at_ms.to_be_bytes());
    encoded.extend_from_slice(&expires_at_ms.to_be_bytes());
    encoded
}

fn decode_event(encoded: &[u8]) -> io::Result<([u8; 32], u64, u64)> {
    if encoded.len() != 56 || &encoded[..8] != EVENT_MAGIC {
        return Err(invalid_data("rate-limit event is malformed"));
    }
    Ok((
        encoded[8..40].try_into().unwrap(),
        u64::from_be_bytes(encoded[40..48].try_into().unwrap()),
        u64::from_be_bytes(encoded[48..56].try_into().unwrap()),
    ))
}

fn read_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &[u8; 32],
    prefix: &[u8],
) -> io::Result<u64> {
    let key = count_key(transaction, digest, prefix)?;
    let Some(encoded) = database.entity_get(transaction, &key)? else {
        return Ok(0);
    };
    let bytes: [u8; 8] = encoded
        .try_into()
        .map_err(|_| invalid_data("rate-limit counter is malformed"))?;
    Ok(u64::from_be_bytes(bytes))
}

fn adjust_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &[u8; 32],
    prefix: &[u8],
    delta: i64,
) -> io::Result<()> {
    let current = read_count(database, transaction, digest, prefix)?;
    let next = if delta >= 0 {
        current.checked_add(delta as u64)
    } else {
        current.checked_sub(delta.unsigned_abs())
    }
    .ok_or_else(|| invalid_data("rate-limit counter arithmetic failed"))?;
    let key = count_key(transaction, digest, prefix)?;
    if next == 0 {
        database.entity_delete(transaction, key)
    } else {
        database.entity_put(transaction, key, next.to_be_bytes().to_vec())
    }
}

fn add_path_deltas(
    deltas: &mut BTreeMap<([u8; 32], Vec<u8>), i64>,
    digest: [u8; 32],
    timestamp_ms: u64,
    delta: i64,
) -> io::Result<()> {
    let timestamp = timestamp_ms.to_be_bytes();
    for depth in 0..=COUNTER_DEPTH {
        let value = deltas
            .entry((digest, timestamp[..depth].to_vec()))
            .or_default();
        *value = value
            .checked_add(delta)
            .ok_or_else(|| invalid_data("rate-limit counter delta overflow"))?;
    }
    Ok(())
}

fn count_before(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    digest: &[u8; 32],
    timestamp_ms: u64,
) -> io::Result<u64> {
    let bound = timestamp_ms.to_be_bytes();
    let mut prefix = Vec::with_capacity(COUNTER_DEPTH);
    let mut total = 0_u64;
    for byte in bound {
        for sibling in 0..byte {
            prefix.push(sibling);
            total = total
                .checked_add(read_count(database, transaction, digest, &prefix)?)
                .ok_or_else(|| invalid_data("rate-limit range count overflow"))?;
            prefix.pop();
        }
        prefix.push(byte);
    }
    Ok(total)
}

fn prune_expired(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    now_ms: u64,
) -> io::Result<usize> {
    let (start, end) = expiry_range(transaction, now_ms)?;
    let rows = database.entity_scan(transaction, &start, &end, MAX_RATE_LIMIT_PRUNE_ROWS)?;
    let mut deltas = BTreeMap::new();
    for (expiry, encoded) in &rows {
        let key = expiry.key_bytes();
        if key.len() <= 8 {
            return Err(invalid_data("rate-limit expiry identity is malformed"));
        }
        let expires_at_ms = u64::from_be_bytes(key[..8].try_into().unwrap());
        let event_id = std::str::from_utf8(&key[8..])
            .map_err(|_| invalid_data("rate-limit expiry event ID is malformed"))?;
        let (digest, occurred_at_ms, recorded_expiry_ms) = decode_event(encoded)?;
        if recorded_expiry_ms != expires_at_ms {
            return Err(invalid_data("rate-limit expiry index is inconsistent"));
        }
        let event = event_key(transaction, event_id)?;
        let stored = database
            .entity_get(transaction, &event)?
            .ok_or_else(|| invalid_data("rate-limit expiry references a missing event"))?;
        if stored != *encoded {
            return Err(invalid_data("rate-limit event and expiry index differ"));
        }
        add_path_deltas(&mut deltas, digest, occurred_at_ms, -1)?;
        database.entity_delete(transaction, event)?;
        database.entity_delete(transaction, expiry.clone())?;
    }
    for ((digest, prefix), delta) in deltas {
        adjust_count(database, transaction, &digest, &prefix, delta)?;
    }
    Ok(rows.len())
}

pub(crate) fn record_and_check(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RecordAndCheckRequest,
) -> io::Result<Vec<u8>> {
    let pruned = prune_expired(database, transaction, request.now_ms)?;
    let digest = bucket_digest(&request.endpoint, &request.client_key);
    let bucket_entity = bucket_key(transaction, &digest)?;
    let mut bucket = match database.entity_get(transaction, &bucket_entity)? {
        Some(encoded) => {
            let bucket = decode_bucket(&encoded)?;
            if bucket.endpoint != request.endpoint || bucket.client_key != request.client_key {
                return Err(invalid_data("rate-limit bucket digest collision"));
            }
            bucket
        }
        None => Bucket {
            endpoint: request.endpoint.clone(),
            client_key: request.client_key.clone(),
            last_now_ms: 0,
        },
    };
    let effective_now_ms = request.now_ms.max(bucket.last_now_ms);
    let window_ms = request
        .per_seconds
        .checked_mul(1_000)
        .ok_or_else(|| invalid_input("rate-limit window overflows"))?;
    let window_start_ms = effective_now_ms.saturating_sub(window_ms);
    let total = read_count(database, transaction, &digest, b"")?;
    let before = count_before(database, transaction, &digest, window_start_ms)?;
    let current = total
        .checked_sub(before)
        .ok_or_else(|| invalid_data("rate-limit window count underflows"))?;
    bucket.last_now_ms = effective_now_ms;
    database.entity_put(transaction, bucket_entity, encode_bucket(&bucket)?)?;
    if current >= request.limit {
        return serde_json::to_vec(&json!({
            "allowed": false,
            "count": current,
            "pruned": pruned,
        }))
        .map_err(|_| invalid_data("rate-limit response cannot be encoded"));
    }

    let event = event_key(transaction, &request.event_id)?;
    if database.entity_get(transaction, &event)?.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "rate-limit event ID already exists",
        ));
    }
    let expires_at_ms = effective_now_ms
        .checked_add(window_ms)
        .ok_or_else(|| invalid_input("rate-limit expiry overflows"))?;
    let encoded = encode_event(&digest, effective_now_ms, expires_at_ms);
    database.entity_put(transaction, event, encoded.clone())?;
    database.entity_put(
        transaction,
        expiry_key(transaction, expires_at_ms, &request.event_id)?,
        encoded,
    )?;
    let mut deltas = BTreeMap::new();
    add_path_deltas(&mut deltas, digest, effective_now_ms, 1)?;
    for ((path_digest, prefix), delta) in deltas {
        adjust_count(database, transaction, &path_digest, &prefix, delta)?;
    }
    serde_json::to_vec(&json!({
        "allowed": true,
        "count": current + 1,
        "pruned": pruned,
    }))
    .map_err(|_| invalid_data("rate-limit response cannot be encoded"))
}

pub(crate) fn valid_request(request: &RecordAndCheckRequest) -> bool {
    !request.endpoint.is_empty()
        && request.endpoint.chars().count() <= MAX_RATE_LIMIT_ENDPOINT_CHARACTERS
        && !request.client_key.is_empty()
        && request.client_key.chars().count() <= MAX_RATE_LIMIT_CLIENT_KEY_CHARACTERS
        && !request.event_id.is_empty()
        && request.event_id.chars().count() <= MAX_RATE_LIMIT_EVENT_ID_CHARACTERS
        && (1..=MAX_RATE_LIMIT_EVENTS_PER_WINDOW).contains(&request.limit)
        && (1..=MAX_RATE_LIMIT_WINDOW_SECONDS).contains(&request.per_seconds)
        && request.now_ms > 0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(event_id: &str, now_ms: u64, limit: u64, per_seconds: u64) -> RecordAndCheckRequest {
        RecordAndCheckRequest {
            endpoint: "/v1/chat".to_owned(),
            client_key: "198.51.100.8".to_owned(),
            event_id: event_id.to_owned(),
            limit,
            per_seconds,
            now_ms,
        }
    }

    fn execute(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        request: &RecordAndCheckRequest,
    ) -> serde_json::Value {
        serde_json::from_slice(&record_and_check(database, transaction, request).unwrap()).unwrap()
    }

    #[test]
    fn sliding_window_is_exact_when_window_changes_and_clock_moves_back() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut first = database.begin(7, 11).unwrap();
        assert_eq!(
            execute(&database, &mut first, &request("event-1", 1_000, 2, 60)),
            json!({"allowed":true,"count":1,"pruned":0})
        );
        database.commit(first).unwrap();

        let mut second = database.begin(7, 11).unwrap();
        assert_eq!(
            execute(&database, &mut second, &request("event-2", 2_000, 2, 1)),
            json!({"allowed":true,"count":2,"pruned":0})
        );
        database.commit(second).unwrap();

        let mut denied = database.begin(7, 11).unwrap();
        assert_eq!(
            execute(&database, &mut denied, &request("event-3", 1_500, 2, 1)),
            json!({"allowed":false,"count":2,"pruned":0})
        );
        database.commit(denied).unwrap();

        let mut advanced = database.begin(7, 11).unwrap();
        assert_eq!(
            execute(&database, &mut advanced, &request("event-4", 2_001, 2, 1),),
            json!({"allowed":true,"count":2,"pruned":0})
        );
        database.commit(advanced).unwrap();
    }

    #[test]
    fn maximum_radix_window_probe_stays_below_point_witness_capacity() {
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            count_before(&database, &mut transaction, &[0x5a; 32], u64::MAX).unwrap(),
            0
        );
    }

    #[test]
    fn expiry_prune_is_owner_scoped_and_stops_at_256_rows() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for index in 0..257 {
            let mut item = request(&format!("expired-{index:03}"), 1, 1, 1);
            item.endpoint = format!("/bucket/{index:03}");
            assert_eq!(execute(&database, &mut seed, &item)["allowed"], true);
        }
        database.commit(seed).unwrap();

        let mut other_owner = database.begin(7, 12).unwrap();
        let mut other = request("other-owner", 1, 1, 1);
        other.endpoint = "/other-owner".to_owned();
        execute(&database, &mut other_owner, &other);
        database.commit(other_owner).unwrap();

        let mut prune = database.begin(7, 11).unwrap();
        let result = execute(&database, &mut prune, &request("fresh", 2_000, 2, 60));
        assert_eq!(result, json!({"allowed":true,"count":1,"pruned":256}));
        database.commit(prune).unwrap();

        let mut other_check = database.begin(7, 12).unwrap();
        let result = execute(
            &database,
            &mut other_check,
            &request("other-fresh", 2_000, 2, 60),
        );
        assert_eq!(result["pruned"], 1);
    }
}
