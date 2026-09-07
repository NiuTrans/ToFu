//! Bounded tenant-global billing wallet and immutable ledger authority.

use crate::generated_tofudb_ir::{
    BILLING_IDEMPOTENCY_NAMESPACE, BILLING_LEDGER_ID_CLAIM_NAMESPACE, BILLING_LEDGER_NAMESPACE,
    BILLING_PAYMENT_COUNT_NAMESPACE, BILLING_PAYMENT_CREATED_INDEX_NAMESPACE,
    BILLING_PAYMENT_DOCUMENT_NAMESPACE, BILLING_PAYMENT_ID_CLAIM_NAMESPACE,
    BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE, BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE,
    BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE, BILLING_REDEEM_BATCH_SHARDS,
    BILLING_REDEEM_CODE_LOCATOR_NAMESPACE, BILLING_REDEEM_CODE_STATE_NAMESPACE,
    BILLING_REDEEM_COUNT_NAMESPACE, BILLING_REDEEM_CREATED_INDEX_NAMESPACE,
    BILLING_REDEEM_LOCATOR_SHARDS, BILLING_RESERVE_AGE_INDEX_NAMESPACE,
    BILLING_RESERVE_STATE_NAMESPACE, BILLING_USER_AGGREGATE_NAMESPACE,
    BILLING_USER_TIME_INDEX_NAMESPACE, BILLING_WALLET_NAMESPACE,
    MAX_BILLING_LEDGER_ENTRIES_PER_USER, MAX_BILLING_LIST_ROWS, MAX_BILLING_LIST_SCAN_ROWS,
    MAX_BILLING_NOTE_CHARACTERS, MAX_BILLING_PAYMENTS, MAX_BILLING_PAYMENT_DOCUMENT_BYTES,
    MAX_BILLING_REDEEM_BATCH_DOCUMENT_BYTES, MAX_BILLING_REDEEM_BATCH_SHARD_BYTES,
    MAX_BILLING_REDEEM_CODES, MAX_BILLING_REDEEM_CODES_PER_MINT,
    MAX_BILLING_REDEEM_LOCATOR_SHARD_BYTES, MAX_BILLING_REDEEM_STATE_SHARD_BYTES,
    MAX_BILLING_RESPONSE_BYTES,
};
use crate::{
    authority::{AuthorityDatabase, AuthorityTransaction},
    conversation_header::TENANT_GLOBAL_OWNER_ID,
    entity::EntityKey,
    versioned_document::{self, PutRequest},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{btree_map::Entry, BTreeMap, BTreeSet},
    io,
};

const KINDS: [&str; 9] = [
    "adjust_credit",
    "adjust_debit",
    "bonus",
    "debit",
    "redeem",
    "refund",
    "reserve",
    "reserve_release",
    "topup",
];
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct LedgerEntry {
    pub id: String,
    pub user_id: String,
    pub ts: u64,
    pub amount_micro: i64,
    pub kind: String,
    pub ref_type: String,
    pub ref_id: String,
    pub balance_after_micro: i64,
    pub note: String,
}
#[derive(Clone, Debug)]
pub struct LedgerListRequest {
    pub user_id: String,
    pub limit: usize,
    pub offset: usize,
    pub kinds: BTreeSet<String>,
    pub since_ts: Option<u64>,
}
#[derive(Clone, Debug)]
pub struct WalletApplyRequest {
    pub user_id: String,
    pub amount_micro: i64,
    pub kind: String,
    pub ref_type: String,
    pub ref_id: String,
    pub note: String,
    pub ledger_id: String,
    pub occurred_at: u64,
    pub allow_negative: bool,
}
#[derive(Clone, Debug)]
pub struct WalletSettleRequest {
    pub user_id: String,
    pub ref_id: String,
    pub reserved_micro: u64,
    pub actual_micro: u64,
    pub note: String,
    pub release_id: String,
    pub debit_id: String,
    pub occurred_at: u64,
}
#[derive(Clone, Debug)]
pub struct PaymentListRequest {
    pub user_id: String,
    pub provider: String,
    pub status: String,
    pub limit: usize,
    pub offset: usize,
}
#[derive(Clone, Debug)]
pub struct RedeemMintRequest {
    pub codes: Vec<String>,
    pub amount_micro: u64,
    pub batch: String,
    pub created_by: String,
    pub created_at: u64,
    pub expires_at: u64,
    pub note: String,
    pub physical_updated_at_ms: u64,
}
#[derive(Clone, Debug)]
pub struct RedeemApplyRequest {
    pub code: String,
    pub user_id: String,
    pub redeemed_at: u64,
    pub ledger_id: String,
    pub physical_updated_at_ms: u64,
}
#[derive(Clone, Debug)]
pub struct RedeemListRequest {
    pub batch: String,
    pub status: String,
    pub limit: usize,
    pub offset: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Payment {
    id: String,
    user_id: String,
    provider: String,
    provider_id: String,
    amount_minor: u64,
    currency: String,
    credit_micro: u64,
    status: String,
    created_at: u64,
    settled_at: u64,
    raw: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct PaymentClaim {
    identity: Vec<String>,
    payment_id: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RedeemBatch {
    record_id: String,
    codes: Vec<String>,
    amount_micro: u64,
    batch: String,
    created_by: String,
    created_at: u64,
    expires_at: u64,
    note: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RedeemBatchShard {
    shard: u16,
    batches: Vec<RedeemBatch>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RedeemLocator {
    code: String,
    record_id: String,
    ordinal: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RedeemLocatorShard {
    shard: u16,
    locators: Vec<RedeemLocator>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RedeemState {
    code: String,
    redeemed_by: String,
    redeemed_at: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RedeemStateShard {
    shard: u16,
    states: Vec<RedeemState>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Wallet {
    user_id: String,
    balance_micro: i64,
    currency: String,
    low_balance_alert_micro: i64,
    updated_at: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Aggregate {
    user_id: String,
    count: usize,
    total_micro: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Claim {
    identity: Vec<String>,
    ledger_id: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Reserve {
    user_id: String,
    ref_id: String,
    held_micro: i64,
    last_reserve_ts: u64,
}

fn err(k: io::ErrorKind, m: &str) -> io::Error {
    io::Error::new(k, m)
}
fn digest(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(domain);
    for p in parts {
        h.update((p.len() as u64).to_be_bytes());
        h.update(p);
    }
    h.finalize().into()
}
fn key(t: &AuthorityTransaction, ns: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(t.tenant_id(), TENANT_GLOBAL_OWNER_ID, ns, raw)
}
fn ud(user: &str) -> [u8; 32] {
    digest(b"tofu-db:billing-user:v1\0", &[user.as_bytes()])
}
fn wallet_key(t: &AuthorityTransaction, user: &str) -> io::Result<EntityKey> {
    key(t, BILLING_WALLET_NAMESPACE, &ud(user))
}
fn aggregate_key(t: &AuthorityTransaction, user: &str) -> io::Result<EntityKey> {
    key(t, BILLING_USER_AGGREGATE_NAMESPACE, &ud(user))
}
fn ledger_raw(id: &str) -> [u8; 32] {
    digest(b"tofu-db:billing-ledger:v1\0", &[id.as_bytes()])
}
fn ledger_key(t: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    key(t, BILLING_LEDGER_NAMESPACE, &ledger_raw(id))
}
fn claim_key(t: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    key(t, BILLING_LEDGER_ID_CLAIM_NAMESPACE, &ledger_raw(id))
}
fn idem_raw(e: &LedgerEntry) -> [u8; 32] {
    digest(
        b"tofu-db:billing-idem:v1\0",
        &[
            e.user_id.as_bytes(),
            e.kind.as_bytes(),
            e.ref_type.as_bytes(),
            e.ref_id.as_bytes(),
        ],
    )
}
fn idem_key(t: &AuthorityTransaction, e: &LedgerEntry) -> io::Result<EntityKey> {
    key(t, BILLING_IDEMPOTENCY_NAMESPACE, &idem_raw(e))
}
fn time_prefix(user: &str) -> Vec<u8> {
    let mut r = ud(user).to_vec();
    r.push(b't');
    r
}
fn time_key(t: &AuthorityTransaction, e: &LedgerEntry) -> io::Result<EntityKey> {
    let mut r = time_prefix(&e.user_id);
    r.extend_from_slice(&(!e.ts).to_be_bytes());
    for b in e.id.bytes() {
        r.extend_from_slice(&[!b, 0]);
    }
    r.push(255);
    key(t, BILLING_USER_TIME_INDEX_NAMESPACE, &r)
}
fn reserve_raw(user: &str, reference: &str) -> [u8; 32] {
    digest(
        b"tofu-db:billing-reserve:v1\0",
        &[user.as_bytes(), reference.as_bytes()],
    )
}
fn reserve_key(t: &AuthorityTransaction, user: &str, reference: &str) -> io::Result<EntityKey> {
    key(
        t,
        BILLING_RESERVE_STATE_NAMESPACE,
        &reserve_raw(user, reference),
    )
}
fn payment_raw(id: &str) -> [u8; 32] {
    digest(b"tofu-db:billing-payment:v1\0", &[id.as_bytes()])
}
fn payment_key(t: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    key(t, BILLING_PAYMENT_DOCUMENT_NAMESPACE, &payment_raw(id))
}
fn payment_id_claim_key(t: &AuthorityTransaction, id: &str) -> io::Result<EntityKey> {
    key(t, BILLING_PAYMENT_ID_CLAIM_NAMESPACE, &payment_raw(id))
}
fn payment_provider_claim_key(
    t: &AuthorityTransaction,
    provider: &str,
    provider_id: &str,
) -> io::Result<EntityKey> {
    key(
        t,
        BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE,
        &digest(
            b"tofu-db:billing-payment-provider:v1\0",
            &[provider.as_bytes(), provider_id.as_bytes()],
        ),
    )
}
fn payment_count_key(t: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(t, BILLING_PAYMENT_COUNT_NAMESPACE, b"all")
}
fn payment_created_key(t: &AuthorityTransaction, p: &Payment) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(9 + p.id.len() * 2);
    raw.push(b'p');
    raw.extend_from_slice(&(!p.created_at).to_be_bytes());
    for byte in p.id.bytes() {
        raw.extend_from_slice(&[!byte, 0]);
    }
    raw.push(255);
    key(t, BILLING_PAYMENT_CREATED_INDEX_NAMESPACE, &raw)
}
fn redeem_code_raw(code: &str) -> [u8; 32] {
    digest(b"tofu-db:billing-redeem-code:v1\0", &[code.as_bytes()])
}
fn redeem_locator_shard(code: &str) -> u16 {
    let raw = redeem_code_raw(code);
    (u16::from_be_bytes([raw[0], raw[1]]) >> 4) % BILLING_REDEEM_LOCATOR_SHARDS as u16
}
fn redeem_locator_key(t: &AuthorityTransaction, shard: u16) -> io::Result<EntityKey> {
    let mut raw = vec![b's'];
    raw.extend_from_slice(&shard.to_be_bytes());
    key(t, BILLING_REDEEM_CODE_LOCATOR_NAMESPACE, &raw)
}
fn redeem_state_key(t: &AuthorityTransaction, shard: u16) -> io::Result<EntityKey> {
    let mut raw = vec![b's'];
    raw.extend_from_slice(&shard.to_be_bytes());
    key(t, BILLING_REDEEM_CODE_STATE_NAMESPACE, &raw)
}
fn redeem_batch_shard(record_id: &str) -> u16 {
    let raw = digest(
        b"tofu-db:billing-redeem-batch:v1\0",
        &[record_id.as_bytes()],
    );
    (u16::from_be_bytes([raw[0], raw[1]]) >> 4) % BILLING_REDEEM_BATCH_SHARDS as u16
}
fn redeem_batch_key(t: &AuthorityTransaction, shard: u16) -> io::Result<EntityKey> {
    let mut raw = vec![b's'];
    raw.extend_from_slice(&shard.to_be_bytes());
    key(t, BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE, &raw)
}
fn redeem_count_key(t: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(t, BILLING_REDEEM_COUNT_NAMESPACE, b"all")
}
fn ascending_text(raw: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        raw.extend_from_slice(&[byte, 0]);
    }
    raw.push(255);
}
fn redeem_created_raw(created_at: u64, record_id: &str) -> Vec<u8> {
    let mut raw = vec![b'r'];
    raw.extend_from_slice(&(!created_at).to_be_bytes());
    ascending_text(&mut raw, record_id);
    raw
}
fn redeem_created_key(t: &AuthorityTransaction, batch: &RedeemBatch) -> io::Result<EntityKey> {
    key(
        t,
        BILLING_REDEEM_CREATED_INDEX_NAMESPACE,
        &redeem_created_raw(batch.created_at, &batch.record_id),
    )
}
fn redeem_batch_created_key(
    t: &AuthorityTransaction,
    batch: &RedeemBatch,
) -> io::Result<EntityKey> {
    let mut raw = vec![b'b'];
    raw.extend_from_slice(batch.batch.as_bytes());
    raw.push(0);
    raw.extend_from_slice(&redeem_created_raw(batch.created_at, &batch.record_id));
    key(t, BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE, &raw)
}
fn age_key(t: &AuthorityTransaction, r: &Reserve) -> io::Result<EntityKey> {
    let mut raw = vec![b'r'];
    raw.extend_from_slice(&r.last_reserve_ts.to_be_bytes());
    raw.extend_from_slice(&reserve_raw(&r.user_id, &r.ref_id));
    key(t, BILLING_RESERVE_AGE_INDEX_NAMESPACE, &raw)
}
fn text(v: &str, n: usize, required: bool) -> bool {
    (!required || !v.is_empty()) && v.chars().count() <= n
}
fn valid_redeem_batch(batch: &RedeemBatch) -> bool {
    let unique_codes: BTreeSet<&str> = batch.codes.iter().map(String::as_str).collect();
    text(&batch.record_id, 64, true)
        && batch.codes.first() == Some(&batch.record_id)
        && !batch.codes.is_empty()
        && batch.codes.len() <= MAX_BILLING_REDEEM_CODES_PER_MINT
        && unique_codes.len() == batch.codes.len()
        && batch.codes.iter().all(|code| text(code, 64, true))
        && (1..=10_000_000_000_000).contains(&batch.amount_micro)
        && text(&batch.batch, 80, true)
        && text(&batch.created_by, 200, false)
        && text(&batch.note, 200, false)
        && batch.created_at <= i64::MAX as u64
        && batch.expires_at <= i64::MAX as u64
        && serde_json::to_vec(batch)
            .is_ok_and(|raw| raw.len() <= MAX_BILLING_REDEEM_BATCH_DOCUMENT_BYTES)
}
fn valid_entry(e: &LedgerEntry) -> bool {
    text(&e.id, 200, true)
        && text(&e.user_id, 200, true)
        && KINDS.contains(&e.kind.as_str())
        && text(&e.ref_type, 100, false)
        && text(&e.ref_id, 300, false)
        && text(&e.note, MAX_BILLING_NOTE_CHARACTERS, false)
}
fn encode(v: &Value) -> io::Result<Vec<u8>> {
    let raw = serde_json::to_vec(v).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing response encoding failed",
        )
    })?;
    if raw.len() > MAX_BILLING_RESPONSE_BYTES {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "billing response exceeds 8 MiB",
        ));
    }
    Ok(raw)
}
fn entry_value(e: &LedgerEntry) -> Value {
    json!({"id":e.id,"user_id":e.user_id,"ts":e.ts,"amount_micro":e.amount_micro,"kind":e.kind,"ref_type":e.ref_type,"ref_id":e.ref_id,"balance_after_micro":e.balance_after_micro,"note":e.note})
}
fn wallet_value(w: &Wallet) -> Value {
    json!({"user_id":w.user_id,"balance_micro":w.balance_micro,"currency":w.currency,"low_balance_alert_micro":w.low_balance_alert_micro,"updated_at":w.updated_at})
}
fn decode<T: for<'a> Deserialize<'a>>(raw: &[u8], m: &str) -> io::Result<T> {
    serde_json::from_slice(raw).map_err(|_| err(io::ErrorKind::InvalidData, m))
}
fn read_wallet(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    user: &str,
) -> io::Result<Wallet> {
    let w = db
        .entity_get(t, &wallet_key(t, user)?)?
        .map(|v| decode(&v, "billing wallet malformed"))
        .transpose()?
        .unwrap_or(Wallet {
            user_id: user.into(),
            balance_micro: 0,
            currency: "CREDIT".into(),
            low_balance_alert_micro: 0,
            updated_at: 0,
        });
    if w.user_id != user || w.currency != "CREDIT" {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing wallet identity differs",
        ));
    }
    Ok(w)
}
fn write_wallet(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    w: &Wallet,
) -> io::Result<()> {
    db.entity_put(
        t,
        wallet_key(t, &w.user_id)?,
        serde_json::to_vec(w).unwrap(),
    )
}
fn read_entry(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<LedgerEntry>> {
    let e: Option<LedgerEntry> = db
        .entity_get(t, &ledger_key(t, id)?)?
        .map(|v| decode(&v, "billing ledger malformed"))
        .transpose()?;
    if e.as_ref().is_some_and(|x| x.id != id) {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing ledger digest collision",
        ));
    }
    Ok(e)
}
fn read_aggregate(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    user: &str,
) -> io::Result<Aggregate> {
    let a = db
        .entity_get(t, &aggregate_key(t, user)?)?
        .map(|v| decode(&v, "billing aggregate malformed"))
        .transpose()?
        .unwrap_or(Aggregate {
            user_id: user.into(),
            count: 0,
            total_micro: 0,
        });
    if a.user_id != user || a.count > MAX_BILLING_LEDGER_ENTRIES_PER_USER {
        return Err(err(io::ErrorKind::InvalidData, "billing aggregate differs"));
    }
    Ok(a)
}
fn existing(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    e: &LedgerEntry,
) -> io::Result<Option<LedgerEntry>> {
    if e.ref_type.is_empty() || e.ref_id.is_empty() {
        return Ok(None);
    }
    let Some(raw) = db.entity_get(t, &idem_key(t, e)?)? else {
        return Ok(None);
    };
    let c: Claim = decode(&raw, "billing idempotency malformed")?;
    let exact = vec![
        e.user_id.clone(),
        e.kind.clone(),
        e.ref_type.clone(),
        e.ref_id.clone(),
    ];
    if c.identity != exact {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing idempotency collision",
        ));
    }
    read_entry(db, t, &c.ledger_id)?
        .ok_or_else(|| {
            err(
                io::ErrorKind::InvalidData,
                "billing idempotency target missing",
            )
        })
        .map(Some)
}
fn reserve_update(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    e: &LedgerEntry,
) -> io::Result<()> {
    if e.ref_type != "reserve"
        || e.ref_id.is_empty()
        || !matches!(e.kind.as_str(), "reserve" | "reserve_release")
    {
        return Ok(());
    }
    let k = reserve_key(t, &e.user_id, &e.ref_id)?;
    let old = db
        .entity_get(t, &k)?
        .map(|v| decode::<Reserve>(&v, "billing reserve malformed"))
        .transpose()?;
    if let Some(r) = &old {
        if r.user_id != e.user_id || r.ref_id != e.ref_id {
            return Err(err(io::ErrorKind::InvalidData, "billing reserve collision"));
        }
        if r.held_micro > 0 && r.last_reserve_ts > 0 {
            db.entity_delete(t, age_key(t, r)?)?;
        }
    }
    let mut r = old.unwrap_or(Reserve {
        user_id: e.user_id.clone(),
        ref_id: e.ref_id.clone(),
        held_micro: 0,
        last_reserve_ts: 0,
    });
    r.held_micro = r
        .held_micro
        .checked_sub(e.amount_micro)
        .ok_or_else(|| err(io::ErrorKind::InvalidInput, "billing reserve overflow"))?;
    if e.kind == "reserve" {
        r.last_reserve_ts = r.last_reserve_ts.max(e.ts)
    }
    db.entity_put(t, k, serde_json::to_vec(&r).unwrap())?;
    if r.held_micro > 0 && r.last_reserve_ts > 0 {
        db.entity_put(t, age_key(t, &r)?, serde_json::to_vec(&r).unwrap())?;
    }
    Ok(())
}
fn append(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    e: &LedgerEntry,
) -> io::Result<(LedgerEntry, bool)> {
    if !valid_entry(e) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing ledger entry",
        ));
    }
    if let Some(found) = existing(db, t, e)? {
        return Ok((found, false));
    }
    if let Some(raw) = db.entity_get(t, &claim_key(t, &e.id)?)? {
        let c: Claim = decode(&raw, "billing ID claim malformed")?;
        if c.identity != vec![e.id.clone()] {
            return Err(err(io::ErrorKind::InvalidData, "billing ID collision"));
        }
        return Err(err(
            io::ErrorKind::AlreadyExists,
            "billing ledger ID exists",
        ));
    }
    let mut a = read_aggregate(db, t, &e.user_id)?;
    if a.count >= MAX_BILLING_LEDGER_ENTRIES_PER_USER {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "billing ledger quota reached",
        ));
    }
    a.count += 1;
    a.total_micro = a
        .total_micro
        .checked_add(e.amount_micro)
        .ok_or_else(|| err(io::ErrorKind::InvalidInput, "billing sum overflow"))?;
    db.entity_put(t, ledger_key(t, &e.id)?, serde_json::to_vec(e).unwrap())?;
    db.entity_put(
        t,
        claim_key(t, &e.id)?,
        serde_json::to_vec(&Claim {
            identity: vec![e.id.clone()],
            ledger_id: e.id.clone(),
        })
        .unwrap(),
    )?;
    if !e.ref_type.is_empty() && !e.ref_id.is_empty() {
        db.entity_put(
            t,
            idem_key(t, e)?,
            serde_json::to_vec(&Claim {
                identity: vec![
                    e.user_id.clone(),
                    e.kind.clone(),
                    e.ref_type.clone(),
                    e.ref_id.clone(),
                ],
                ledger_id: e.id.clone(),
            })
            .unwrap(),
        )?;
    }
    db.entity_put(t, time_key(t, e)?, e.id.as_bytes().to_vec())?;
    db.entity_put(
        t,
        aggregate_key(t, &e.user_id)?,
        serde_json::to_vec(&a).unwrap(),
    )?;
    reserve_update(db, t, e)?;
    Ok((e.clone(), true))
}

pub(crate) fn ledger_append(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    e: &LedgerEntry,
) -> io::Result<Vec<u8>> {
    let (e, _) = append(db, t, e)?;
    encode(&entry_value(&e))
}
pub(crate) fn ledger_find(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    user: &str,
    kind: &str,
    rt: &str,
    rid: &str,
) -> io::Result<Option<Vec<u8>>> {
    if !text(user, 200, true)
        || !text(kind, 64, true)
        || !text(rt, 100, false)
        || !text(rid, 300, false)
    {
        return Err(err(io::ErrorKind::InvalidInput, "invalid billing lookup"));
    }
    let p = LedgerEntry {
        id: String::new(),
        user_id: user.into(),
        ts: 0,
        amount_micro: 0,
        kind: kind.into(),
        ref_type: rt.into(),
        ref_id: rid.into(),
        balance_after_micro: 0,
        note: String::new(),
    };
    existing(db, t, &p)?
        .map(|e| encode(&entry_value(&e)))
        .transpose()
}
fn next(t: &AuthorityTransaction, raw: &[u8]) -> io::Result<EntityKey> {
    let mut r = raw.to_vec();
    r.push(0);
    key(t, BILLING_USER_TIME_INDEX_NAMESPACE, &r)
}
pub(crate) fn ledger_list(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    q: &LedgerListRequest,
) -> io::Result<Vec<u8>> {
    let wanted = q
        .offset
        .checked_add(q.limit)
        .filter(|v| *v <= MAX_BILLING_LIST_SCAN_ROWS)
        .ok_or_else(|| {
            err(
                io::ErrorKind::OutOfMemory,
                "billing list scan exceeds bound",
            )
        })?;
    if !text(&q.user_id, 200, true)
        || !(1..=MAX_BILLING_LIST_ROWS).contains(&q.limit)
        || q.kinds.iter().any(|k| !KINDS.contains(&k.as_str()))
    {
        return Err(err(io::ErrorKind::InvalidInput, "invalid billing list"));
    }
    let prefix = time_prefix(&q.user_id);
    let (mut cursor, end) = EntityKey::prefix_range(
        t.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        BILLING_USER_TIME_INDEX_NAMESPACE,
        &prefix,
    )?;
    let (mut rows, mut scanned, mut ended) = (Vec::new(), 0, false);
    while scanned < MAX_BILLING_LIST_SCAN_ROWS && rows.len() < wanted {
        let n = (MAX_BILLING_LIST_SCAN_ROWS - scanned).min(1000);
        let page = db.entity_scan(t, &cursor, &end, n)?;
        if page.is_empty() {
            ended = true;
            break;
        }
        for (_, raw) in &page {
            scanned += 1;
            let id = std::str::from_utf8(raw)
                .map_err(|_| err(io::ErrorKind::InvalidData, "billing index malformed"))?;
            let e = read_entry(db, t, id)?
                .ok_or_else(|| err(io::ErrorKind::InvalidData, "billing index target missing"))?;
            if e.user_id != q.user_id {
                return Err(err(
                    io::ErrorKind::InvalidData,
                    "billing index owner differs",
                ));
            }
            if q.since_ts.is_none_or(|x| e.ts >= x)
                && (q.kinds.is_empty() || q.kinds.contains(&e.kind))
            {
                rows.push(e);
                if rows.len() == wanted {
                    break;
                }
            }
        }
        cursor = next(t, page.last().unwrap().0.key_bytes())?;
        if page.len() < n {
            ended = true;
            break;
        }
    }
    if rows.len() < wanted && !ended {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "billing filters exceed scan bound",
        ));
    }
    encode(&Value::Array(
        rows.into_iter()
            .skip(q.offset)
            .map(|e| entry_value(&e))
            .collect(),
    ))
}
pub(crate) fn ledger_recompute(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    user: &str,
) -> io::Result<Vec<u8>> {
    if !text(user, 200, true) {
        return Err(err(io::ErrorKind::InvalidInput, "invalid billing user"));
    }
    encode(&json!({"balance_micro":read_aggregate(db,t,user)?.total_micro}))
}
pub(crate) fn wallet_get(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    user: &str,
) -> io::Result<Vec<u8>> {
    if !text(user, 200, true) {
        return Err(err(io::ErrorKind::InvalidInput, "invalid billing user"));
    }
    encode(&wallet_value(&read_wallet(db, t, user)?))
}
pub(crate) fn wallet_apply(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    q: &WalletApplyRequest,
) -> io::Result<Vec<u8>> {
    let mut e = LedgerEntry {
        id: q.ledger_id.clone(),
        user_id: q.user_id.clone(),
        ts: q.occurred_at,
        amount_micro: q.amount_micro,
        kind: q.kind.clone(),
        ref_type: q.ref_type.clone(),
        ref_id: q.ref_id.clone(),
        balance_after_micro: 0,
        note: q.note.clone(),
    };
    if !valid_entry(&e) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing wallet apply",
        ));
    }
    if let Some(old) = existing(db, t, &e)? {
        let w = read_wallet(db, t, &q.user_id)?;
        return encode(
            &json!({"applied":false,"duplicate":true,"insufficient":false,"wallet":wallet_value(&w),"entry":entry_value(&old)}),
        );
    }
    let mut w = read_wallet(db, t, &q.user_id)?;
    let updated = w
        .balance_micro
        .checked_add(q.amount_micro)
        .ok_or_else(|| err(io::ErrorKind::InvalidInput, "billing wallet overflow"))?;
    if updated < 0 && !q.allow_negative {
        return encode(
            &json!({"applied":false,"duplicate":false,"insufficient":true,"balance_micro":w.balance_micro,"needed_micro":q.amount_micro.checked_neg().ok_or_else(||err(io::ErrorKind::InvalidInput,"billing needed overflow"))?,"wallet":wallet_value(&w)}),
        );
    }
    w.balance_micro = updated;
    w.updated_at = q.occurred_at;
    e.balance_after_micro = updated;
    append(db, t, &e)?;
    write_wallet(db, t, &w)?;
    encode(
        &json!({"applied":true,"duplicate":false,"insufficient":false,"wallet":wallet_value(&w),"entry":entry_value(&e)}),
    )
}
pub(crate) fn wallet_settle(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    q: &WalletSettleRequest,
) -> io::Result<Vec<u8>> {
    if ledger_find(db, t, &q.user_id, "debit", "task", &q.ref_id)?.is_some() {
        return encode(
            &json!({"applied":false,"wallet":wallet_value(&read_wallet(db,t,&q.user_id)?)}),
        );
    }
    let mut w = read_wallet(db, t, &q.user_id)?;
    if ledger_find(db, t, &q.user_id, "reserve_release", "reserve", &q.ref_id)?.is_none() {
        let amount = i64::try_from(q.reserved_micro)
            .map_err(|_| err(io::ErrorKind::InvalidInput, "reserved amount too large"))?;
        w.balance_micro = w
            .balance_micro
            .checked_add(amount)
            .ok_or_else(|| err(io::ErrorKind::InvalidInput, "settlement overflow"))?;
        append(
            db,
            t,
            &LedgerEntry {
                id: q.release_id.clone(),
                user_id: q.user_id.clone(),
                ts: q.occurred_at,
                amount_micro: amount,
                kind: "reserve_release".into(),
                ref_type: "reserve".into(),
                ref_id: q.ref_id.clone(),
                balance_after_micro: w.balance_micro,
                note: q.note.clone(),
            },
        )?;
    }
    let actual = i64::try_from(q.actual_micro)
        .map_err(|_| err(io::ErrorKind::InvalidInput, "actual amount too large"))?;
    w.balance_micro = w
        .balance_micro
        .checked_sub(actual)
        .ok_or_else(|| err(io::ErrorKind::InvalidInput, "settlement overflow"))?;
    append(
        db,
        t,
        &LedgerEntry {
            id: q.debit_id.clone(),
            user_id: q.user_id.clone(),
            ts: q.occurred_at,
            amount_micro: -actual,
            kind: "debit".into(),
            ref_type: "task".into(),
            ref_id: q.ref_id.clone(),
            balance_after_micro: w.balance_micro,
            note: q.note.clone(),
        },
    )?;
    w.updated_at = q.occurred_at;
    write_wallet(db, t, &w)?;
    encode(&json!({"applied":true,"wallet":wallet_value(&w)}))
}

fn payment_value(payment: &Payment) -> Value {
    serde_json::to_value(payment).unwrap()
}

fn read_payment(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    payment_id: &str,
) -> io::Result<Option<Payment>> {
    versioned_document::get_value_with_blob_owner_bounded(
        db,
        t,
        &payment_key(t, payment_id)?,
        BILLING_PAYMENT_DOCUMENT_NAMESPACE,
        payment_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_PAYMENT_DOCUMENT_BYTES,
    )?
    .map(|raw| decode::<Payment>(&raw, "billing payment malformed"))
    .transpose()
    .and_then(|payment| {
        if payment.as_ref().is_some_and(|value| value.id != payment_id) {
            Err(err(
                io::ErrorKind::InvalidData,
                "billing payment identity differs",
            ))
        } else {
            Ok(payment)
        }
    })
}

fn write_payment(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    payment: &Payment,
    expected_version: Option<u64>,
    physical_updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(payment).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing payment encoding failed",
        )
    })?;
    versioned_document::put_with_blob_owner_bounded(
        db,
        t,
        PutRequest {
            key: payment_key(t, &payment.id)?,
            namespace: BILLING_PAYMENT_DOCUMENT_NAMESPACE.to_owned(),
            logical_key: payment.id.clone(),
            value_json,
            expected_version,
            updated_at_ms: physical_updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_PAYMENT_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn read_payment_claim(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    provider: &str,
    provider_id: &str,
) -> io::Result<Option<Payment>> {
    let Some(raw) = db.entity_get(t, &payment_provider_claim_key(t, provider, provider_id)?)?
    else {
        return Ok(None);
    };
    let claim: PaymentClaim = decode(&raw, "billing payment provider claim malformed")?;
    if claim.identity != [provider, provider_id] {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing payment provider collision",
        ));
    }
    let payment = read_payment(db, t, &claim.payment_id)?.ok_or_else(|| {
        err(
            io::ErrorKind::InvalidData,
            "billing payment provider target missing",
        )
    })?;
    if payment.provider != provider || payment.provider_id != provider_id {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing payment provider target differs",
        ));
    }
    Ok(Some(payment))
}

fn payload_text(
    map: &serde_json::Map<String, Value>,
    name: &str,
    maximum: usize,
) -> io::Result<String> {
    let value = map.get(name).and_then(Value::as_str).unwrap_or_default();
    if !text(value, maximum, true) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment text",
        ));
    }
    Ok(value.to_owned())
}

fn payload_unsigned(map: &serde_json::Map<String, Value>, name: &str) -> io::Result<u64> {
    map.get(name)
        .and_then(Value::as_u64)
        .filter(|value| *value <= i64::MAX as u64)
        .ok_or_else(|| {
            err(
                io::ErrorKind::InvalidInput,
                "invalid billing payment integer",
            )
        })
}

fn payment_raw_document(value: Option<&Value>) -> io::Result<Value> {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Ok(json!({})),
        Some(Value::Number(number)) if number.as_i64() == Some(0) => Ok(json!({})),
        Some(Value::String(value)) if value.is_empty() => Ok(json!({})),
        Some(Value::Array(value)) if value.is_empty() => Ok(json!({})),
        Some(Value::Object(value)) => Ok(Value::Object(value.clone())),
        _ => Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment raw document",
        )),
    }
}

pub(crate) fn payment_find(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    provider: &str,
    provider_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    if !text(provider, 100, true) || !text(provider_id, 300, true) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment lookup",
        ));
    }
    read_payment_claim(db, t, provider, provider_id)?
        .map(|payment| encode(&payment_value(&payment)))
        .transpose()
}

pub(crate) fn payment_record(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    provider: &str,
    provider_id: &str,
    payload_json: &[u8],
    created_at: u64,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    if !text(provider, 100, true) || !text(provider_id, 300, true) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment provider",
        ));
    }
    if let Some(payment) = read_payment_claim(db, t, provider, provider_id)? {
        return encode(&json!({"created":false,"payment":payment_value(&payment)}));
    }
    let payload: Value = decode(payload_json, "billing payment payload malformed")?;
    let map = payload.as_object().ok_or_else(|| {
        err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment payload",
        )
    })?;
    let payment = Payment {
        id: payload_text(map, "id", 200)?,
        user_id: payload_text(map, "user_id", 200)?,
        provider: provider.to_owned(),
        provider_id: provider_id.to_owned(),
        amount_minor: payload_unsigned(map, "amount_minor")?,
        currency: payload_text(map, "currency", 16)?,
        credit_micro: payload_unsigned(map, "credit_micro")?,
        status: payload_text(map, "status", 32)?,
        created_at,
        settled_at: 0,
        raw: payment_raw_document(map.get("raw"))?,
    };
    if !matches!(payment.status.as_str(), "pending" | "settled" | "failed") {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment status",
        ));
    }
    if let Some(raw) = db.entity_get(t, &payment_id_claim_key(t, &payment.id)?)? {
        let claim: PaymentClaim = decode(&raw, "billing payment ID claim malformed")?;
        if claim.identity != [payment.id.as_str()] || claim.payment_id != payment.id {
            return Err(err(
                io::ErrorKind::InvalidData,
                "billing payment ID claim differs",
            ));
        }
        return Err(err(
            io::ErrorKind::AlreadyExists,
            "billing payment ID exists",
        ));
    }
    let count_key = payment_count_key(t)?;
    let count = db
        .entity_get(t, &count_key)?
        .map(|raw| decode::<usize>(&raw, "billing payment count malformed"))
        .transpose()?
        .unwrap_or(0);
    if count >= MAX_BILLING_PAYMENTS {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "billing payment quota reached",
        ));
    }
    write_payment(db, t, &payment, Some(0), physical_updated_at_ms)?;
    let id_claim = PaymentClaim {
        identity: vec![payment.id.clone()],
        payment_id: payment.id.clone(),
    };
    db.entity_put(
        t,
        payment_id_claim_key(t, &payment.id)?,
        serde_json::to_vec(&id_claim).unwrap(),
    )?;
    let provider_claim = PaymentClaim {
        identity: vec![provider.to_owned(), provider_id.to_owned()],
        payment_id: payment.id.clone(),
    };
    db.entity_put(
        t,
        payment_provider_claim_key(t, provider, provider_id)?,
        serde_json::to_vec(&provider_claim).unwrap(),
    )?;
    db.entity_put(
        t,
        payment_created_key(t, &payment)?,
        payment.id.as_bytes().to_vec(),
    )?;
    db.entity_put(t, count_key, serde_json::to_vec(&(count + 1)).unwrap())?;
    encode(&json!({"created":true,"payment":payment_value(&payment)}))
}

pub(crate) fn payment_settle(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    payment_id: &str,
    payload_json: &[u8],
    settled_at: u64,
    physical_updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    if !text(payment_id, 200, true) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment ID",
        ));
    }
    let Some(mut payment) = read_payment(db, t, payment_id)? else {
        return encode(&json!({"found":false,"settled":false,"payment":null}));
    };
    if payment.status == "settled" {
        return encode(&json!({"found":true,"settled":false,"payment":payment_value(&payment)}));
    }
    let payload: Value = decode(payload_json, "billing payment settlement payload malformed")?;
    let map = payload.as_object().ok_or_else(|| {
        err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment settlement",
        )
    })?;
    if payment.credit_micro > 0 {
        let ledger_id = payload_text(map, "ledger_id", 200)?;
        let amount_micro = i64::try_from(payment.credit_micro)
            .map_err(|_| err(io::ErrorKind::InvalidInput, "payment credit too large"))?;
        wallet_apply(
            db,
            t,
            &WalletApplyRequest {
                user_id: payment.user_id.clone(),
                amount_micro,
                kind: "topup".into(),
                ref_type: "payment".into(),
                ref_id: if payment.provider_id.is_empty() {
                    payment.id.clone()
                } else {
                    payment.provider_id.clone()
                },
                note: format!("{} payment settled", payment.provider),
                ledger_id,
                occurred_at: settled_at,
                allow_negative: false,
            },
        )?;
    }
    if let Some(raw) = map.get("raw") {
        if !raw.is_null() {
            payment.raw = match raw {
                Value::Object(value) => Value::Object(value.clone()),
                _ => {
                    return Err(err(
                        io::ErrorKind::InvalidInput,
                        "invalid billing payment raw document",
                    ))
                }
            };
        }
    }
    payment.status = "settled".into();
    payment.settled_at = settled_at;
    write_payment(db, t, &payment, None, physical_updated_at_ms)?;
    encode(&json!({"found":true,"settled":true,"payment":payment_value(&payment)}))
}

pub(crate) fn payment_list(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    request: &PaymentListRequest,
) -> io::Result<Vec<u8>> {
    let wanted = request
        .offset
        .checked_add(request.limit)
        .filter(|value| *value <= MAX_BILLING_LIST_SCAN_ROWS)
        .ok_or_else(|| {
            err(
                io::ErrorKind::OutOfMemory,
                "billing payment list scan exceeds bound",
            )
        })?;
    if !(1..=MAX_BILLING_LIST_ROWS).contains(&request.limit)
        || request.offset > 10_000_000
        || !text(&request.user_id, 200, false)
        || !text(&request.provider, 200, false)
        || !text(&request.status, 200, false)
    {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid billing payment list",
        ));
    }
    let (mut cursor, end) = EntityKey::prefix_range(
        t.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        BILLING_PAYMENT_CREATED_INDEX_NAMESPACE,
        b"p",
    )?;
    let mut rows = Vec::new();
    let mut scanned = 0;
    let mut ended = false;
    while scanned < MAX_BILLING_LIST_SCAN_ROWS && rows.len() < wanted {
        let maximum = (MAX_BILLING_LIST_SCAN_ROWS - scanned).min(1000);
        let page = db.entity_scan(t, &cursor, &end, maximum)?;
        if page.is_empty() {
            ended = true;
            break;
        }
        for (_, raw) in &page {
            scanned += 1;
            let id = std::str::from_utf8(raw).map_err(|_| {
                err(
                    io::ErrorKind::InvalidData,
                    "billing payment index malformed",
                )
            })?;
            let payment = read_payment(db, t, id)?.ok_or_else(|| {
                err(
                    io::ErrorKind::InvalidData,
                    "billing payment index target missing",
                )
            })?;
            if (request.user_id.is_empty() || request.user_id == payment.user_id)
                && (request.provider.is_empty() || request.provider == payment.provider)
                && (request.status.is_empty() || request.status == payment.status)
            {
                rows.push(payment);
                if rows.len() == wanted {
                    break;
                }
            }
        }
        let mut next_raw = page.last().unwrap().0.key_bytes().to_vec();
        next_raw.push(0);
        cursor = key(t, BILLING_PAYMENT_CREATED_INDEX_NAMESPACE, &next_raw)?;
        if page.len() < maximum {
            ended = true;
            break;
        }
    }
    if rows.len() < wanted && !ended {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "billing payment filters exceed scan bound",
        ));
    }
    encode(&Value::Array(
        rows.into_iter()
            .skip(request.offset)
            .map(|payment| payment_value(&payment))
            .collect(),
    ))
}

fn read_redeem_batch(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    record_id: &str,
) -> io::Result<RedeemBatch> {
    let shard = redeem_batch_shard(record_id);
    let directory = read_redeem_batch_shard(db, t, shard)?.ok_or_else(|| {
        err(
            io::ErrorKind::InvalidData,
            "billing redeem batch shard missing",
        )
    })?;
    directory
        .batches
        .binary_search_by(|batch| batch.record_id.as_str().cmp(record_id))
        .ok()
        .map(|index| directory.batches[index].clone())
        .ok_or_else(|| err(io::ErrorKind::InvalidData, "billing redeem batch missing"))
}

fn read_redeem_batch_shard(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    shard: u16,
) -> io::Result<Option<RedeemBatchShard>> {
    let logical_key = shard.to_string();
    let raw = versioned_document::get_value_with_blob_owner_bounded(
        db,
        t,
        &redeem_batch_key(t, shard)?,
        BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE,
        &logical_key,
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_BATCH_SHARD_BYTES,
    )?;
    let directory = raw
        .map(|raw| decode::<RedeemBatchShard>(&raw, "billing redeem batch shard malformed"))
        .transpose()?;
    if directory.as_ref().is_some_and(|value| {
        value.shard != shard
            || value.batches.is_empty()
            || value.batches.len() > MAX_BILLING_REDEEM_CODES
            || value.batches.iter().any(|batch| {
                redeem_batch_shard(&batch.record_id) != shard || !valid_redeem_batch(batch)
            })
            || value
                .batches
                .windows(2)
                .any(|pair| pair[0].record_id >= pair[1].record_id)
    }) {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing redeem batch shard differs",
        ));
    }
    Ok(directory)
}

fn write_redeem_batch_shard(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    directory: &RedeemBatchShard,
    expected_version: Option<u64>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(directory).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing redeem batch shard encoding failed",
        )
    })?;
    versioned_document::put_with_blob_owner_bounded(
        db,
        t,
        PutRequest {
            key: redeem_batch_key(t, directory.shard)?,
            namespace: BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE.to_owned(),
            logical_key: directory.shard.to_string(),
            value_json,
            expected_version,
            updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_BATCH_SHARD_BYTES,
    )?;
    Ok(())
}

fn read_redeem_state_shard(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    shard: u16,
) -> io::Result<Option<RedeemStateShard>> {
    let logical_key = shard.to_string();
    let directory = versioned_document::get_value_with_blob_owner_bounded(
        db,
        t,
        &redeem_state_key(t, shard)?,
        BILLING_REDEEM_CODE_STATE_NAMESPACE,
        &logical_key,
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_STATE_SHARD_BYTES,
    )?
    .map(|raw| decode::<RedeemStateShard>(&raw, "billing redeem state shard malformed"))
    .transpose()?;
    if directory.as_ref().is_some_and(|value| {
        value.shard != shard
            || value.states.is_empty()
            || value.states.len() > MAX_BILLING_REDEEM_CODES
            || value.states.iter().any(|state| {
                redeem_locator_shard(&state.code) != shard
                    || !text(&state.code, 64, true)
                    || !text(&state.redeemed_by, 200, true)
                    || state.redeemed_at > i64::MAX as u64
            })
            || value
                .states
                .windows(2)
                .any(|pair| pair[0].code >= pair[1].code)
    }) {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing redeem state shard differs",
        ));
    }
    Ok(directory)
}

fn write_redeem_state_shard(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    directory: &RedeemStateShard,
    expected_version: Option<u64>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(directory).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing redeem state shard encoding failed",
        )
    })?;
    versioned_document::put_with_blob_owner_bounded(
        db,
        t,
        PutRequest {
            key: redeem_state_key(t, directory.shard)?,
            namespace: BILLING_REDEEM_CODE_STATE_NAMESPACE.to_owned(),
            logical_key: directory.shard.to_string(),
            value_json,
            expected_version,
            updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_STATE_SHARD_BYTES,
    )?;
    Ok(())
}

fn redeem_value(batch: &RedeemBatch, code: &str, state: Option<&RedeemState>) -> Value {
    json!({
        "code":code, "amount_micro":batch.amount_micro, "batch":batch.batch,
        "created_by":batch.created_by, "created_at":batch.created_at,
        "expires_at":batch.expires_at,
        "redeemed_by":state.map_or("", |value| value.redeemed_by.as_str()),
        "redeemed_at":state.map_or(0, |value| value.redeemed_at), "note":batch.note,
    })
}

fn redeem_locator(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    code: &str,
) -> io::Result<Option<(RedeemLocator, RedeemBatch)>> {
    let shard = redeem_locator_shard(code);
    let Some(directory) = read_redeem_locator_shard(db, t, shard)? else {
        return Ok(None);
    };
    let Some(locator) = directory
        .locators
        .into_iter()
        .find(|value| value.code == code)
    else {
        return Ok(None);
    };
    let batch = read_redeem_batch(db, t, &locator.record_id)?;
    if batch.codes.get(locator.ordinal).map(String::as_str) != Some(code) {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing redeem locator target differs",
        ));
    }
    Ok(Some((locator, batch)))
}

fn read_redeem_locator_shard(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    shard: u16,
) -> io::Result<Option<RedeemLocatorShard>> {
    let logical_key = shard.to_string();
    let directory = versioned_document::get_value_with_blob_owner_bounded(
        db,
        t,
        &redeem_locator_key(t, shard)?,
        BILLING_REDEEM_CODE_LOCATOR_NAMESPACE,
        &logical_key,
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_LOCATOR_SHARD_BYTES,
    )?
    .map(|raw| decode::<RedeemLocatorShard>(&raw, "billing redeem locator shard malformed"))
    .transpose()?;
    if directory.as_ref().is_some_and(|value| {
        value.shard != shard
            || value.locators.is_empty()
            || value.locators.len() > MAX_BILLING_REDEEM_CODES
            || value.locators.iter().any(|locator| {
                redeem_locator_shard(&locator.code) != shard
                    || !text(&locator.code, 64, true)
                    || !text(&locator.record_id, 64, true)
                    || locator.ordinal >= MAX_BILLING_REDEEM_CODES_PER_MINT
            })
            || value
                .locators
                .windows(2)
                .any(|pair| pair[0].code >= pair[1].code)
    }) {
        return Err(err(
            io::ErrorKind::InvalidData,
            "billing redeem locator shard differs",
        ));
    }
    Ok(directory)
}

fn write_redeem_locator_shard(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    directory: &RedeemLocatorShard,
    expected_version: Option<u64>,
    physical_updated_at_ms: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(directory).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing redeem locator shard encoding failed",
        )
    })?;
    versioned_document::put_with_blob_owner_bounded(
        db,
        t,
        PutRequest {
            key: redeem_locator_key(t, directory.shard)?,
            namespace: BILLING_REDEEM_CODE_LOCATOR_NAMESPACE.to_owned(),
            logical_key: directory.shard.to_string(),
            value_json,
            expected_version,
            updated_at_ms: physical_updated_at_ms,
        },
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_LOCATOR_SHARD_BYTES,
    )?;
    Ok(())
}

pub(crate) fn redeem_codes_mint(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    request: &RedeemMintRequest,
) -> io::Result<Vec<u8>> {
    if !(1..=MAX_BILLING_REDEEM_CODES_PER_MINT).contains(&request.codes.len())
        || !(1..=10_000_000_000_000).contains(&request.amount_micro)
        || !text(&request.batch, 80, true)
        || !text(&request.created_by, 200, false)
        || !text(&request.note, 200, false)
        || request.created_at > i64::MAX as u64
        || request.expires_at > i64::MAX as u64
        || request.codes.iter().any(|code| !text(code, 64, true))
    {
        return Err(err(io::ErrorKind::InvalidInput, "invalid redeem-code mint"));
    }
    let unique: BTreeSet<&str> = request.codes.iter().map(String::as_str).collect();
    if unique.len() != request.codes.len() {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "redemption codes must be unique",
        ));
    }
    let mut additions: BTreeMap<u16, Vec<(usize, &String)>> = BTreeMap::new();
    for (ordinal, code) in request.codes.iter().enumerate() {
        additions
            .entry(redeem_locator_shard(code))
            .or_default()
            .push((ordinal, code));
    }
    let mut directories = Vec::with_capacity(additions.len());
    for (shard, values) in &additions {
        let existing = read_redeem_locator_shard(db, t, *shard)?;
        let directory = existing.clone().unwrap_or(RedeemLocatorShard {
            shard: *shard,
            locators: Vec::new(),
        });
        if values.iter().any(|(_, code)| {
            directory
                .locators
                .binary_search_by(|locator| locator.code.as_str().cmp(code.as_str()))
                .is_ok()
        }) {
            return Err(err(
                io::ErrorKind::AlreadyExists,
                "billing redemption code exists",
            ));
        }
        directories.push((directory, existing.is_some(), values));
    }
    let count_key = redeem_count_key(t)?;
    let count = db
        .entity_get(t, &count_key)?
        .map(|raw| decode::<usize>(&raw, "billing redeem count malformed"))
        .transpose()?
        .unwrap_or(0);
    let updated_count = count
        .checked_add(request.codes.len())
        .filter(|value| *value <= MAX_BILLING_REDEEM_CODES)
        .ok_or_else(|| {
            err(
                io::ErrorKind::OutOfMemory,
                "billing redeem-code quota reached",
            )
        })?;
    let record_id = request.codes[0].clone();
    let batch = RedeemBatch {
        record_id: record_id.clone(),
        codes: request.codes.clone(),
        amount_micro: request.amount_micro,
        batch: request.batch.clone(),
        created_by: request.created_by.clone(),
        created_at: request.created_at,
        expires_at: request.expires_at,
        note: request.note.clone(),
    };
    let value_json = serde_json::to_vec(&batch).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing redeem batch encoding failed",
        )
    })?;
    if value_json.len() > MAX_BILLING_REDEEM_BATCH_DOCUMENT_BYTES {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "billing redeem batch exceeds logical bound",
        ));
    }
    let batch_shard = redeem_batch_shard(&record_id);
    let existing_batches = read_redeem_batch_shard(db, t, batch_shard)?;
    let mut batch_directory = existing_batches.clone().unwrap_or(RedeemBatchShard {
        shard: batch_shard,
        batches: Vec::new(),
    });
    if batch_directory
        .batches
        .binary_search_by(|value| value.record_id.cmp(&record_id))
        .is_ok()
    {
        return Err(err(
            io::ErrorKind::AlreadyExists,
            "billing redeem batch exists",
        ));
    }
    batch_directory.batches.push(batch.clone());
    batch_directory
        .batches
        .sort_unstable_by(|left, right| left.record_id.cmp(&right.record_id));
    write_redeem_batch_shard(
        db,
        t,
        &batch_directory,
        existing_batches.is_none().then_some(0),
        request.physical_updated_at_ms,
    )?;
    for (mut directory, existed, values) in directories {
        directory
            .locators
            .extend(values.iter().map(|(ordinal, code)| RedeemLocator {
                code: (*code).clone(),
                record_id: record_id.clone(),
                ordinal: *ordinal,
            }));
        directory
            .locators
            .sort_unstable_by(|left, right| left.code.cmp(&right.code));
        write_redeem_locator_shard(
            db,
            t,
            &directory,
            (!existed).then_some(0),
            request.physical_updated_at_ms,
        )?;
    }
    db.entity_put(
        t,
        redeem_created_key(t, &batch)?,
        record_id.as_bytes().to_vec(),
    )?;
    db.entity_put(
        t,
        redeem_batch_created_key(t, &batch)?,
        record_id.as_bytes().to_vec(),
    )?;
    db.entity_put(t, count_key, serde_json::to_vec(&updated_count).unwrap())?;
    encode(&json!({"created":request.codes.len(),"codes":request.codes}))
}

pub(crate) fn redeem_code_apply(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    request: &RedeemApplyRequest,
) -> io::Result<Vec<u8>> {
    if !text(&request.code, 64, true)
        || !text(&request.user_id, 200, true)
        || !text(&request.ledger_id, 200, true)
        || request.redeemed_at > i64::MAX as u64
    {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid redeem-code apply",
        ));
    }
    let Some((_, batch)) = redeem_locator(db, t, &request.code)? else {
        return encode(&json!({"status":"not_found"}));
    };
    let state_shard = redeem_locator_shard(&request.code);
    let existing_states = read_redeem_state_shard(db, t, state_shard)?;
    if let Some(state) = existing_states.as_ref().and_then(|directory| {
        directory
            .states
            .binary_search_by(|state| state.code.as_str().cmp(request.code.as_str()))
            .ok()
            .map(|index| &directory.states[index])
    }) {
        return encode(
            &json!({"status":"already_redeemed","code":redeem_value(&batch,&request.code,Some(state))}),
        );
    }
    if batch.expires_at != 0 && batch.expires_at < request.redeemed_at {
        return encode(&json!({"status":"expired","code":redeem_value(&batch,&request.code,None)}));
    }
    let amount_micro = i64::try_from(batch.amount_micro).map_err(|_| {
        err(
            io::ErrorKind::InvalidData,
            "billing redeem amount too large",
        )
    })?;
    let wallet_raw = wallet_apply(
        db,
        t,
        &WalletApplyRequest {
            user_id: request.user_id.clone(),
            amount_micro,
            kind: "redeem".into(),
            ref_type: "redeem_code".into(),
            ref_id: request.code.clone(),
            note: format!("redeemed code {}", request.code),
            ledger_id: request.ledger_id.clone(),
            occurred_at: request.redeemed_at,
            allow_negative: false,
        },
    )?;
    let wallet_result: Value = decode(&wallet_raw, "billing redeem wallet result malformed")?;
    let state = RedeemState {
        code: request.code.clone(),
        redeemed_by: request.user_id.clone(),
        redeemed_at: request.redeemed_at,
    };
    let mut state_directory = existing_states.unwrap_or(RedeemStateShard {
        shard: state_shard,
        states: Vec::new(),
    });
    state_directory.states.push(state.clone());
    state_directory
        .states
        .sort_unstable_by(|left, right| left.code.cmp(&right.code));
    write_redeem_state_shard(
        db,
        t,
        &state_directory,
        (state_directory.states.len() == 1).then_some(0),
        request.physical_updated_at_ms,
    )?;
    encode(&json!({
        "status":"redeemed", "code":redeem_value(&batch,&request.code,Some(&state)),
        "wallet":wallet_result.get("wallet").cloned().ok_or_else(||err(io::ErrorKind::InvalidData,"billing redeem wallet missing"))?,
    }))
}

pub(crate) fn redeem_codes_list(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    request: &RedeemListRequest,
) -> io::Result<Vec<u8>> {
    if !text(&request.batch, 80, false)
        || !matches!(request.status.as_str(), "all" | "redeemed" | "unredeemed")
        || !(1..=MAX_BILLING_LIST_ROWS).contains(&request.limit)
        || request.offset > 10_000_000
        || request
            .offset
            .checked_add(request.limit)
            .is_none_or(|value| value > MAX_BILLING_LIST_SCAN_ROWS)
    {
        return Err(err(io::ErrorKind::InvalidInput, "invalid redeem-code list"));
    }
    let (namespace, prefix) = if request.batch.is_empty() {
        (BILLING_REDEEM_CREATED_INDEX_NAMESPACE, vec![b'r'])
    } else {
        let mut prefix = vec![b'b'];
        prefix.extend_from_slice(request.batch.as_bytes());
        prefix.push(0);
        (BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE, prefix)
    };
    let (mut cursor, end) =
        EntityKey::prefix_range(t.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, &prefix)?;
    let mut candidates: Vec<(u64, String, Value)> = Vec::new();
    let mut batch_directories: BTreeMap<u16, RedeemBatchShard> = BTreeMap::new();
    let mut state_directories: BTreeMap<u16, RedeemStateShard> = BTreeMap::new();
    let mut scanned_codes = 0_usize;
    let mut ended = false;
    while scanned_codes < MAX_BILLING_LIST_SCAN_ROWS {
        let page = db.entity_scan(t, &cursor, &end, 1000)?;
        if page.is_empty() {
            ended = true;
            break;
        }
        let mut page_complete = true;
        for (batch_position, (_, raw)) in page.iter().enumerate() {
            let record_id = std::str::from_utf8(raw)
                .map_err(|_| err(io::ErrorKind::InvalidData, "billing redeem index malformed"))?;
            let batch_shard = redeem_batch_shard(record_id);
            if let Entry::Vacant(entry) = batch_directories.entry(batch_shard) {
                entry.insert(read_redeem_batch_shard(db, t, batch_shard)?.ok_or_else(|| {
                    err(
                        io::ErrorKind::InvalidData,
                        "billing redeem batch shard missing",
                    )
                })?);
            }
            let batch = batch_directories[&batch_shard]
                .batches
                .binary_search_by(|batch| batch.record_id.as_str().cmp(record_id))
                .ok()
                .map(|index| batch_directories[&batch_shard].batches[index].clone())
                .ok_or_else(|| err(io::ErrorKind::InvalidData, "billing redeem batch missing"))?;
            if !request.batch.is_empty() && batch.batch != request.batch {
                return Err(err(
                    io::ErrorKind::InvalidData,
                    "billing redeem batch index differs",
                ));
            }
            let mut codes = batch.codes.clone();
            codes.sort();
            let code_count = codes.len();
            for (code_position, code) in codes.into_iter().enumerate() {
                if scanned_codes == MAX_BILLING_LIST_SCAN_ROWS {
                    page_complete = false;
                    break;
                }
                scanned_codes += 1;
                let shard = redeem_locator_shard(&code);
                if let Entry::Vacant(entry) = state_directories.entry(shard) {
                    entry.insert(read_redeem_state_shard(db, t, shard)?.unwrap_or(
                        RedeemStateShard {
                            shard,
                            states: Vec::new(),
                        },
                    ));
                }
                let state = state_directories[&shard]
                    .states
                    .binary_search_by(|state| state.code.as_str().cmp(code.as_str()))
                    .ok()
                    .map(|index| &state_directories[&shard].states[index]);
                let selected = match request.status.as_str() {
                    "redeemed" => state.is_some(),
                    "unredeemed" => state.is_none(),
                    _ => true,
                };
                if selected {
                    candidates.push((
                        batch.created_at,
                        code.clone(),
                        redeem_value(&batch, &code, state),
                    ));
                }
                if scanned_codes == MAX_BILLING_LIST_SCAN_ROWS
                    && (code_position + 1 < code_count || batch_position + 1 < page.len())
                {
                    page_complete = false;
                }
            }
            if !page_complete {
                break;
            }
        }
        if !page_complete {
            break;
        }
        let mut raw = page.last().unwrap().0.key_bytes().to_vec();
        raw.push(0);
        cursor = key(t, namespace, &raw)?;
        if page.len() < 1000 {
            ended = true;
            break;
        }
    }
    if !ended {
        return Err(err(
            io::ErrorKind::OutOfMemory,
            "redeem-code list exceeds scan bound",
        ));
    }
    candidates.sort_by(|left, right| right.0.cmp(&left.0).then_with(|| left.1.cmp(&right.1)));
    encode(&Value::Array(
        candidates
            .into_iter()
            .skip(request.offset)
            .take(request.limit)
            .map(|row| row.2)
            .collect(),
    ))
}

pub(crate) fn reserve_stale(
    db: &AuthorityDatabase,
    t: &mut AuthorityTransaction,
    cutoff_ts: u64,
    limit: usize,
) -> io::Result<Vec<u8>> {
    if cutoff_ts > i64::MAX as u64 || !(1..=10_000).contains(&limit) {
        return Err(err(
            io::ErrorKind::InvalidInput,
            "invalid stale-reserve limit",
        ));
    }
    let (mut cursor, end) = EntityKey::prefix_range(
        t.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        BILLING_RESERVE_AGE_INDEX_NAMESPACE,
        b"r",
    )?;
    let mut rows = Vec::new();
    while rows.len() < limit {
        let maximum = (limit - rows.len()).min(1000);
        let page = db.entity_scan(t, &cursor, &end, maximum)?;
        if page.is_empty() {
            break;
        }
        for (found_key, raw) in &page {
            let reserve: Reserve = decode(raw, "billing reserve age index malformed")?;
            if reserve.held_micro <= 0 || reserve.last_reserve_ts == 0 {
                return Err(err(
                    io::ErrorKind::InvalidData,
                    "billing reserve age state differs",
                ));
            }
            if &age_key(t, &reserve)? != found_key {
                return Err(err(
                    io::ErrorKind::InvalidData,
                    "billing reserve age key differs",
                ));
            }
            if reserve.last_reserve_ts > cutoff_ts {
                return encode(&Value::Array(rows));
            }
            rows.push(json!({
                "user_id":reserve.user_id,"ref_id":reserve.ref_id,"held_micro":reserve.held_micro,
            }));
        }
        let mut raw = page.last().unwrap().0.key_bytes().to_vec();
        raw.push(0);
        cursor = key(t, BILLING_RESERVE_AGE_INDEX_NAMESPACE, &raw)?;
        if page.len() < maximum {
            break;
        }
    }
    encode(&Value::Array(rows))
}
