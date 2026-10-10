//! Offline wallet/ledger snapshot restoration and independent index inspection.
//! Entry: execute, called only after operator UUID/deadline admission. Parent
//! billing codecs and append own every physical key and derived mutation. No
//! online wallet apply, synthetic transaction, timestamp rewrite or wallet
//! synthesis occurs. Each row and all its indexes commit together.

use super::*;
use crate::generated_tofudb_ir::{MAX_ENTITY_KEY_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RestoreRequest {
    table: String,
    document: Value,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetRequest {
    table: String,
    id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScanRequest {
    table: String,
    cursor: Option<String>,
}

fn invalid(message: &str) -> io::Error {
    err(io::ErrorKind::InvalidInput, message)
}
fn corrupt(message: &str) -> io::Error {
    err(io::ErrorKind::InvalidData, message)
}
fn conflict() -> io::Error {
    err(
        io::ErrorKind::WouldBlock,
        "billing snapshot already differs",
    )
}
fn namespace(table: &str) -> io::Result<&'static str> {
    match table {
        "wallets" => Ok(BILLING_WALLET_NAMESPACE),
        "ledger" => Ok(BILLING_LEDGER_NAMESPACE),
        "id_claims" => Ok(BILLING_LEDGER_ID_CLAIM_NAMESPACE),
        "idempotency" => Ok(BILLING_IDEMPOTENCY_NAMESPACE),
        "time_index" => Ok(BILLING_USER_TIME_INDEX_NAMESPACE),
        "aggregates" => Ok(BILLING_USER_AGGREGATE_NAMESPACE),
        "reserves" => Ok(BILLING_RESERVE_STATE_NAMESPACE),
        "reserve_age" => Ok(BILLING_RESERVE_AGE_INDEX_NAMESPACE),
        "payments" => Ok(BILLING_PAYMENT_DOCUMENT_NAMESPACE),
        "payment_id_claims" => Ok(BILLING_PAYMENT_ID_CLAIM_NAMESPACE),
        "payment_provider_claims" => Ok(BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE),
        "payment_created_index" => Ok(BILLING_PAYMENT_CREATED_INDEX_NAMESPACE),
        "payment_count" => Ok(BILLING_PAYMENT_COUNT_NAMESPACE),
        "redeem_batches" => Ok(BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE),
        "redeem_locators" => Ok(BILLING_REDEEM_CODE_LOCATOR_NAMESPACE),
        "redeem_states" => Ok(BILLING_REDEEM_CODE_STATE_NAMESPACE),
        "redeem_created_index" => Ok(BILLING_REDEEM_CREATED_INDEX_NAMESPACE),
        "redeem_batch_created_index" => Ok(BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE),
        "redeem_count" => Ok(BILLING_REDEEM_COUNT_NAMESPACE),
        _ => Err(invalid("unknown billing migration table")),
    }
}
/// Versioned-document envelopes carry their logical key in a binary header;
/// extract the key without materializing the value payload.
fn versioned_identity(raw: &[u8], message: &str) -> io::Result<String> {
    if raw.len() < 44 || &raw[..8] != b"TDBDOC01" {
        return Err(corrupt(message));
    }
    let namespace_length = u16::from_le_bytes([raw[28], raw[29]]) as usize;
    let key_length = u16::from_le_bytes([raw[30], raw[31]]) as usize;
    let start = 44usize
        .checked_add(namespace_length)
        .ok_or_else(|| corrupt(message))?;
    let key = raw
        .get(start..start + key_length)
        .ok_or_else(|| corrupt(message))?;
    std::str::from_utf8(key)
        .map(str::to_owned)
        .map_err(|_| corrupt(message))
}

/// Materialize a versioned-document payload through the shared codec so
/// catalog inspection validates identity the same way the live paths do.
fn versioned_payload(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    physical: &EntityKey,
    envelope: &[u8],
) -> io::Result<Vec<u8>> {
    let _ = envelope;
    let logical = versioned_identity(
        &db.entity_get(tx, physical)?
            .ok_or_else(|| corrupt("billing catalog target is missing"))?,
        "billing catalog envelope malformed",
    )?;
    let namespace = physical.namespace();
    versioned_document::get_value_with_blob_owner_bounded(
        db,
        tx,
        physical,
        namespace,
        &logical,
        TENANT_GLOBAL_OWNER_ID,
        MAX_BILLING_REDEEM_STATE_SHARD_BYTES,
    )?
    .ok_or_else(|| corrupt("billing catalog target is missing"))
}

fn equal_record(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    key: &EntityKey,
    expected: &[u8],
) -> io::Result<()> {
    if db.entity_get(tx, key)?.as_deref() != Some(expected) {
        return Err(corrupt("billing snapshot index is missing or differs"));
    }
    Ok(())
}
fn validate_reserve(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    value: &Reserve,
) -> io::Result<()> {
    if !text(&value.user_id, 200, true)
        || !text(&value.ref_id, 300, true)
        || value.last_reserve_ts > i64::MAX as u64
    {
        return Err(corrupt("invalid imported reserve state"));
    }
    let encoded = serde_json::to_vec(value).unwrap();
    equal_record(
        db,
        tx,
        &reserve_key(tx, &value.user_id, &value.ref_id)?,
        &encoded,
    )?;
    if value.held_micro > 0 && value.last_reserve_ts > 0 {
        equal_record(db, tx, &age_key(tx, value)?, &encoded)?;
    }
    Ok(())
}
fn checked_entry(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<LedgerEntry>> {
    let Some(entry) = read_entry(db, tx, id)? else {
        return Ok(None);
    };
    if !valid_entry(&entry) || entry.ts > i64::MAX as u64 {
        return Err(corrupt("invalid imported ledger entry"));
    }
    let claim = Claim {
        identity: vec![entry.id.clone()],
        ledger_id: entry.id.clone(),
    };
    equal_record(
        db,
        tx,
        &claim_key(tx, id)?,
        &serde_json::to_vec(&claim).unwrap(),
    )?;
    equal_record(db, tx, &time_key(tx, &entry)?, entry.id.as_bytes())?;
    if !entry.ref_type.is_empty()
        && !entry.ref_id.is_empty()
        && existing(db, tx, &entry)?.as_ref() != Some(&entry)
    {
        return Err(corrupt("billing snapshot idempotency target differs"));
    }
    if read_aggregate(db, tx, &entry.user_id)?.count == 0 {
        return Err(corrupt("billing snapshot aggregate is missing"));
    }
    if entry.ref_type == "reserve"
        && !entry.ref_id.is_empty()
        && matches!(entry.kind.as_str(), "reserve" | "reserve_release")
    {
        let raw = db
            .entity_get(tx, &reserve_key(tx, &entry.user_id, &entry.ref_id)?)?
            .ok_or_else(|| corrupt("billing snapshot reserve state is missing"))?;
        let value: Reserve = decode(&raw, "billing snapshot reserve state malformed")?;
        if value.user_id != entry.user_id || value.ref_id != entry.ref_id {
            return Err(corrupt("billing snapshot reserve identity differs"));
        }
        validate_reserve(db, tx, &value)?;
    }
    Ok(Some(entry))
}
fn get(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    table: &str,
    id: &str,
) -> io::Result<Value> {
    if !text(id, 200, true) {
        return Err(invalid("invalid billing snapshot identity"));
    }
    match table {
        "wallets" => {
            if db.entity_get(tx, &wallet_key(tx, id)?)?.is_none() {
                return Ok(Value::Null);
            }
            Ok(wallet_value(&read_wallet(db, tx, id)?))
        }
        "ledger" => Ok(checked_entry(db, tx, id)?
            .as_ref()
            .map(entry_value)
            .unwrap_or(Value::Null)),
        "payments" => checked_payment(db, tx, id)
            .map(|payment| payment.as_ref().map(payment_value).unwrap_or(Value::Null)),
        _ => Err(invalid("billing point reads require a primary table")),
    }
}

/// A payment is present only when its document, id claim, provider claim
/// (when the provider identity is non-empty), created index and aggregate
/// count all agree; every read validates the full derived layout.
fn checked_payment(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    id: &str,
) -> io::Result<Option<Payment>> {
    let Some(payment) = read_payment(db, tx, id)? else {
        return Ok(None);
    };
    if !text(&payment.user_id, 200, true)
        || !text(&payment.provider, 100, true)
        || !text(&payment.provider_id, 300, true)
        || !matches!(payment.status.as_str(), "pending" | "settled" | "failed")
        || payment.created_at > i64::MAX as u64
        || payment.settled_at > i64::MAX as u64
        || (payment.status == "settled") != (payment.settled_at > 0)
        || !payment.raw.is_object()
    {
        return Err(corrupt("invalid imported billing payment"));
    }
    let id_claim = PaymentClaim {
        identity: vec![payment.id.clone()],
        payment_id: payment.id.clone(),
    };
    equal_record(
        db,
        tx,
        &payment_id_claim_key(tx, &payment.id)?,
        &serde_json::to_vec(&id_claim).unwrap(),
    )?;
    if !payment.provider_id.is_empty() {
        let provider_claim = PaymentClaim {
            identity: vec![payment.provider.clone(), payment.provider_id.clone()],
            payment_id: payment.id.clone(),
        };
        equal_record(
            db,
            tx,
            &payment_provider_claim_key(tx, &payment.provider, &payment.provider_id)?,
            &serde_json::to_vec(&provider_claim).unwrap(),
        )?;
    }
    equal_record(
        db,
        tx,
        &payment_created_key(tx, &payment)?,
        payment.id.as_bytes(),
    )?;
    let count = db
        .entity_get(tx, &payment_count_key(tx)?)?
        .map(|raw| decode::<usize>(&raw, "billing payment count malformed"))
        .transpose()?
        .unwrap_or(0);
    if count == 0 {
        return Err(corrupt("billing payment count is missing"));
    }
    Ok(Some(payment))
}

fn payment_snapshot(document: &Value) -> io::Result<Payment> {
    let payment: Payment = serde_json::from_value(document.clone())
        .map_err(|_| invalid("invalid billing payment snapshot"))?;
    if payment_value(&payment) != *document
        || !text(&payment.id, 200, true)
        || !text(&payment.user_id, 200, true)
        || !text(&payment.provider, 100, true)
        || !text(&payment.provider_id, 300, true)
        || !text(&payment.currency, 16, true)
        || !matches!(payment.status.as_str(), "pending" | "settled" | "failed")
        || payment.created_at > i64::MAX as u64
        || payment.settled_at > i64::MAX as u64
        || (payment.status == "settled") != (payment.settled_at > 0)
        || !payment.raw.is_object()
    {
        return Err(invalid("unsupported billing payment snapshot"));
    }
    Ok(payment)
}

fn restore_payment(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Value,
) -> io::Result<bool> {
    let payment = payment_snapshot(document)?;
    if let Some(found) = checked_payment(db, tx, &payment.id)? {
        return if found == payment {
            Ok(false)
        } else {
            Err(conflict())
        };
    }
    // A source identity collision must never silently select another row.
    if db
        .entity_get(tx, &payment_id_claim_key(tx, &payment.id)?)?
        .is_some()
        || (!payment.provider_id.is_empty()
            && db
                .entity_get(
                    tx,
                    &payment_provider_claim_key(tx, &payment.provider, &payment.provider_id)?,
                )?
                .is_some())
    {
        return Err(conflict());
    }
    let count = db
        .entity_get(tx, &payment_count_key(tx)?)?
        .map(|raw| decode::<usize>(&raw, "billing payment count malformed"))
        .transpose()?
        .unwrap_or(0);
    if count >= MAX_BILLING_PAYMENTS {
        return Err(invalid("billing payment quota reached"));
    }
    write_payment(db, tx, &payment, Some(0), 1)?;
    let id_claim = PaymentClaim {
        identity: vec![payment.id.clone()],
        payment_id: payment.id.clone(),
    };
    db.entity_put(
        tx,
        payment_id_claim_key(tx, &payment.id)?,
        serde_json::to_vec(&id_claim).unwrap(),
    )?;
    if !payment.provider_id.is_empty() {
        let provider_claim = PaymentClaim {
            identity: vec![payment.provider.clone(), payment.provider_id.clone()],
            payment_id: payment.id.clone(),
        };
        db.entity_put(
            tx,
            payment_provider_claim_key(tx, &payment.provider, &payment.provider_id)?,
            serde_json::to_vec(&provider_claim).unwrap(),
        )?;
    }
    db.entity_put(
        tx,
        payment_created_key(tx, &payment)?,
        payment.id.as_bytes().to_vec(),
    )?;
    db.entity_put(
        tx,
        payment_count_key(tx)?,
        serde_json::to_vec(&(count + 1)).unwrap(),
    )?;
    let written = checked_payment(db, tx, &payment.id)?
        .ok_or_else(|| corrupt("billing payment snapshot is missing"))?;
    if written != payment {
        return Err(corrupt("billing payment snapshot readback differs"));
    }
    Ok(true)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RedeemRestoreRequest {
    codes: Vec<String>,
    amount_micro: u64,
    batch: String,
    created_by: String,
    created_at: u64,
    expires_at: u64,
    note: String,
    redeemed: Vec<RedeemState>,
}

fn restore_redeem(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: &Value,
) -> io::Result<bool> {
    let request: RedeemRestoreRequest = serde_json::from_value(payload.clone())
        .map_err(|_| invalid("invalid billing redeem restore request"))?;
    if !(1..=MAX_BILLING_REDEEM_CODES_PER_MINT).contains(&request.codes.len())
        || request.redeemed.len() > request.codes.len()
    {
        return Err(invalid("invalid billing redeem snapshot"));
    }
    let batch = RedeemBatch {
        record_id: request.codes[0].clone(),
        codes: request.codes.clone(),
        amount_micro: request.amount_micro,
        batch: request.batch.clone(),
        created_by: request.created_by.clone(),
        created_at: request.created_at,
        expires_at: request.expires_at,
        note: request.note.clone(),
    };
    if !valid_redeem_batch(&batch) {
        return Err(invalid("invalid billing redeem snapshot"));
    }
    let mut states: BTreeMap<String, RedeemState> = BTreeMap::new();
    for state in &request.redeemed {
        if !batch.codes.contains(&state.code)
            || !text(&state.code, 64, true)
            || !text(&state.redeemed_by, 200, true)
            || state.redeemed_at == 0
            || state.redeemed_at > i64::MAX as u64
            || states.insert(state.code.clone(), state.clone()).is_some()
        {
            return Err(invalid("invalid billing redeem state snapshot"));
        }
    }
    // Insert-or-match: an identical batch is read-only, a divergent one fails
    // closed. The first code locates the batch deterministically.
    if let Some((locator, found)) = redeem_locator(db, tx, &request.codes[0])? {
        if found != batch {
            return Err(conflict());
        }
        for (ordinal, code) in request.codes.iter().enumerate() {
            let Some((other, other_batch)) = redeem_locator(db, tx, code)? else {
                return Err(corrupt("billing redeem snapshot locator is missing"));
            };
            if other.ordinal != ordinal || other_batch != batch {
                return Err(corrupt("billing redeem snapshot locators differ"));
            }
        }
        let _ = locator;
        for state in states.values() {
            let Some(observed) = read_redeem_state(db, tx, &state.code)? else {
                return Err(corrupt("billing redeem snapshot state is missing"));
            };
            if observed != *state {
                return Err(conflict());
            }
        }
        return Ok(false);
    }
    let count = db
        .entity_get(tx, &redeem_count_key(tx)?)?
        .map(|raw| decode::<usize>(&raw, "billing redeem count malformed"))
        .transpose()?
        .unwrap_or(0);
    let updated_count = count
        .checked_add(request.codes.len())
        .filter(|value| *value <= MAX_BILLING_REDEEM_CODES)
        .ok_or_else(|| invalid("billing redeem-code quota reached"))?;
    // Batch shard.
    let batch_shard = redeem_batch_shard(&batch.record_id);
    let existing_batches = read_redeem_batch_shard(db, tx, batch_shard)?;
    let mut batch_directory = existing_batches.clone().unwrap_or(RedeemBatchShard {
        shard: batch_shard,
        batches: Vec::new(),
    });
    if batch_directory
        .batches
        .binary_search_by(|value| value.record_id.cmp(&batch.record_id))
        .is_ok()
    {
        return Err(conflict());
    }
    batch_directory.batches.push(batch.clone());
    batch_directory
        .batches
        .sort_unstable_by(|left, right| left.record_id.cmp(&right.record_id));
    write_redeem_batch_shard(
        db,
        tx,
        &batch_directory,
        existing_batches.is_none().then_some(0),
        1,
    )?;
    // Locator shards.
    let mut additions: BTreeMap<u16, Vec<(usize, &String)>> = BTreeMap::new();
    for (ordinal, code) in request.codes.iter().enumerate() {
        additions
            .entry(redeem_locator_shard(code))
            .or_default()
            .push((ordinal, code));
    }
    for (shard, values) in &additions {
        let existing = read_redeem_locator_shard(db, tx, *shard)?;
        let mut directory = existing.clone().unwrap_or(RedeemLocatorShard {
            shard: *shard,
            locators: Vec::new(),
        });
        for (ordinal, code) in values {
            if directory
                .locators
                .binary_search_by(|locator| locator.code.as_str().cmp(code.as_str()))
                .is_ok()
            {
                return Err(conflict());
            }
            directory.locators.push(RedeemLocator {
                code: (*code).clone(),
                record_id: batch.record_id.clone(),
                ordinal: *ordinal,
            });
        }
        directory
            .locators
            .sort_unstable_by(|left, right| left.code.cmp(&right.code));
        write_redeem_locator_shard(db, tx, &directory, (!existing.is_some()).then_some(0), 1)?;
    }
    // Redemption state shards.
    let mut state_additions: BTreeMap<u16, Vec<&RedeemState>> = BTreeMap::new();
    for state in states.values() {
        state_additions
            .entry(redeem_locator_shard(&state.code))
            .or_default()
            .push(state);
    }
    for (shard, values) in &state_additions {
        let existing = read_redeem_state_shard(db, tx, *shard)?;
        let mut directory = existing.clone().unwrap_or(RedeemStateShard {
            shard: *shard,
            states: Vec::new(),
        });
        for state in values {
            if directory
                .states
                .binary_search_by(|value| value.code.as_str().cmp(state.code.as_str()))
                .is_ok()
            {
                return Err(conflict());
            }
            directory.states.push((*state).clone());
        }
        directory
            .states
            .sort_unstable_by(|left, right| left.code.cmp(&right.code));
        write_redeem_state_shard(db, tx, &directory, existing.is_none().then_some(0), 1)?;
    }
    db.entity_put(
        tx,
        redeem_created_key(tx, &batch)?,
        batch.record_id.as_bytes().to_vec(),
    )?;
    db.entity_put(
        tx,
        redeem_batch_created_key(tx, &batch)?,
        batch.record_id.as_bytes().to_vec(),
    )?;
    db.entity_put(
        tx,
        redeem_count_key(tx)?,
        serde_json::to_vec(&updated_count).unwrap(),
    )?;
    Ok(true)
}

fn read_redeem_state(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    code: &str,
) -> io::Result<Option<RedeemState>> {
    let shard = redeem_locator_shard(code);
    let Some(directory) = read_redeem_state_shard(db, tx, shard)? else {
        return Ok(None);
    };
    Ok(directory
        .states
        .into_iter()
        .find(|state| state.code == code))
}

/// Read one code through the same locator path the live apply uses, then
/// project the exact source snapshot spelling.
fn redeem_get(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    code: &str,
) -> io::Result<Value> {
    if !text(code, 64, true) {
        return Err(invalid("invalid billing redeem identity"));
    }
    let Some((locator, batch)) = redeem_locator(db, tx, code)? else {
        return Ok(Value::Null);
    };
    if batch.codes.get(locator.ordinal).map(String::as_str) != Some(code) {
        return Err(corrupt("billing redeem locator target differs"));
    }
    let state = read_redeem_state(db, tx, code)?;
    Ok(json!({
        "code": code,
        "amount_micro": batch.amount_micro,
        "batch": batch.batch,
        "created_by": batch.created_by,
        "created_at": batch.created_at,
        "expires_at": batch.expires_at,
        "redeemed_by": state.as_ref().map_or("", |value| value.redeemed_by.as_str()),
        "redeemed_at": state.as_ref().map_or(0, |value| value.redeemed_at),
        "note": batch.note,
        "record_id": batch.record_id,
        "codes": batch.codes,
    }))
}
fn restore(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: RestoreRequest,
) -> io::Result<bool> {
    match request.table.as_str() {
        "wallets" => {
            let wallet: Wallet = serde_json::from_value(request.document.clone())
                .map_err(|_| invalid("invalid wallet snapshot"))?;
            if wallet_value(&wallet) != request.document
                || !text(&wallet.user_id, 200, true)
                || wallet.currency != "CREDIT"
                || wallet.updated_at > i64::MAX as u64
            {
                return Err(invalid("unsupported wallet snapshot"));
            }
            let found = get(db, tx, "wallets", &wallet.user_id)?;
            if !found.is_null() {
                return if found == request.document {
                    Ok(false)
                } else {
                    Err(conflict())
                };
            }
            write_wallet(db, tx, &wallet)?;
            Ok(true)
        }
        "ledger" => {
            let entry: LedgerEntry = serde_json::from_value(request.document.clone())
                .map_err(|_| invalid("invalid ledger snapshot"))?;
            if entry_value(&entry) != request.document
                || !valid_entry(&entry)
                || entry.ts > i64::MAX as u64
            {
                return Err(invalid("unsupported ledger snapshot"));
            }
            if let Some(found) = checked_entry(db, tx, &entry.id)? {
                return if found == entry {
                    Ok(false)
                } else {
                    Err(conflict())
                };
            }
            // A source idempotency collision must never silently select another
            // ledger row. The ordinary append path owns all index mutations.
            if existing(db, tx, &entry)?.is_some() {
                return Err(conflict());
            }
            let (found, inserted) = append(db, tx, &entry)?;
            if !inserted || found != entry {
                return Err(conflict());
            }
            Ok(true)
        }
        "payments" => restore_payment(db, tx, &request.document),
        _ => Err(invalid("billing restore requires a primary table")),
    }
}
fn cursor_start(table: &str, cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let encoded = cursor
        .strip_prefix(table)
        .and_then(|s| s.strip_prefix(':'))
        .ok_or_else(|| invalid("billing cursor belongs to another table"))?;
    if encoded.is_empty()
        || encoded.len() > MAX_ENTITY_KEY_BYTES * 2
        || !encoded.len().is_multiple_of(2)
    {
        return Err(invalid("invalid billing cursor"));
    }
    let mut raw = encoded
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            u8::from_str_radix(
                std::str::from_utf8(pair).map_err(|_| invalid("invalid billing cursor"))?,
                16,
            )
            .map_err(|_| invalid("invalid billing cursor"))
        })
        .collect::<io::Result<Vec<_>>>()?;
    if raw.len() < MAX_ENTITY_KEY_BYTES {
        raw.push(0);
    } else {
        let index = raw
            .iter()
            .rposition(|b| *b != 255)
            .ok_or_else(|| invalid("billing cursor has no successor"))?;
        raw[index] += 1;
        raw.truncate(index + 1);
    }
    Ok(raw)
}
fn inspect_row(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    table: &str,
    physical: &EntityKey,
    raw: &[u8],
) -> io::Result<Value> {
    match table {
        "wallets" => {
            let wallet: Wallet = decode(raw, "wallet catalog malformed")?;
            if &wallet_key(tx, &wallet.user_id)? != physical || wallet.currency != "CREDIT" {
                return Err(corrupt("wallet catalog identity differs"));
            }
            Ok(json!({"id":wallet.user_id}))
        }
        "ledger" => {
            let entry: LedgerEntry = decode(raw, "ledger catalog malformed")?;
            if &ledger_key(tx, &entry.id)? != physical {
                return Err(corrupt("ledger catalog identity differs"));
            }
            Ok(json!({"id":entry.id}))
        }
        "aggregates" => {
            let aggregate: Aggregate = decode(raw, "billing aggregate catalog malformed")?;
            if &aggregate_key(tx, &aggregate.user_id)? != physical
                || aggregate.count == 0
                || aggregate.count > MAX_BILLING_LEDGER_ENTRIES_PER_USER
            {
                return Err(corrupt("billing aggregate catalog differs"));
            }
            Ok(
                json!({"user_id":aggregate.user_id,"count":aggregate.count,"total_micro":aggregate.total_micro}),
            )
        }
        "reserves" | "reserve_age" => {
            let reserve: Reserve = decode(raw, "billing reserve catalog malformed")?;
            let expected = if table == "reserves" {
                reserve_key(tx, &reserve.user_id, &reserve.ref_id)?
            } else {
                age_key(tx, &reserve)?
            };
            if &expected != physical
                || (table == "reserve_age"
                    && (reserve.held_micro <= 0 || reserve.last_reserve_ts == 0))
            {
                return Err(corrupt("billing reserve catalog identity differs"));
            }
            validate_reserve(db, tx, &reserve)?;
            Ok(
                json!({"user_id":reserve.user_id,"ref_id":reserve.ref_id,"held_micro":reserve.held_micro,"last_reserve_ts":reserve.last_reserve_ts}),
            )
        }
        "payments" => {
            let id = versioned_identity(raw, "billing payment catalog malformed")?;
            let payment = checked_payment(db, tx, &id)?
                .ok_or_else(|| corrupt("billing payment catalog target missing"))?;
            if &payment_key(tx, &payment.id)? != physical {
                return Err(corrupt("billing payment catalog identity differs"));
            }
            Ok(json!({"id": payment.id}))
        }
        "payment_id_claims" | "payment_provider_claims" => {
            let claim: PaymentClaim = decode(raw, "billing payment claim catalog malformed")?;
            let payment = checked_payment(db, tx, &claim.payment_id)?
                .ok_or_else(|| corrupt("billing payment claim target missing"))?;
            let expected = if table == "payment_id_claims" {
                if claim.identity != [payment.id.clone()] {
                    return Err(corrupt("billing payment claim identity differs"));
                }
                payment_id_claim_key(tx, &payment.id)?
            } else {
                if payment.provider_id.is_empty()
                    || claim.identity != [payment.provider.clone(), payment.provider_id.clone()]
                {
                    return Err(corrupt("billing payment claim identity differs"));
                }
                payment_provider_claim_key(tx, &payment.provider, &payment.provider_id)?
            };
            if &expected != physical {
                return Err(corrupt("billing payment claim catalog identity differs"));
            }
            Ok(json!({"id": payment.id}))
        }
        "payment_created_index" => {
            let id = std::str::from_utf8(raw)
                .map_err(|_| corrupt("billing payment created index malformed"))?;
            let payment = checked_payment(db, tx, id)?
                .ok_or_else(|| corrupt("billing payment index target missing"))?;
            if &payment_created_key(tx, &payment)? != physical {
                return Err(corrupt("billing payment created catalog identity differs"));
            }
            Ok(json!({"id": payment.id}))
        }
        "payment_count" => {
            let count: usize = decode(raw, "billing payment count catalog malformed")?;
            if count == 0 || count > MAX_BILLING_PAYMENTS || physical.key_bytes() != b"all" {
                return Err(corrupt("billing payment count catalog differs"));
            }
            Ok(json!({"count": count}))
        }
        "redeem_batches" => {
            let raw = versioned_payload(db, tx, physical, raw)?;
            let directory: RedeemBatchShard =
                decode(&raw, "billing redeem batch catalog malformed")?;
            if &redeem_batch_key(tx, directory.shard)? != physical || directory.batches.is_empty() {
                return Err(corrupt("billing redeem batch catalog identity differs"));
            }
            let mut rows = Vec::with_capacity(directory.batches.len());
            for batch in &directory.batches {
                if !valid_redeem_batch(batch)
                    || redeem_batch_shard(&batch.record_id) != directory.shard
                {
                    return Err(corrupt("billing redeem batch catalog differs"));
                }
                rows.push(json!({"record_id": batch.record_id, "batch": batch.batch,
                    "created_at": batch.created_at, "codes": batch.codes.len()}));
            }
            Ok(json!(rows))
        }
        "redeem_locators" => {
            let raw = versioned_payload(db, tx, physical, raw)?;
            let directory: RedeemLocatorShard =
                decode(&raw, "billing redeem locator catalog malformed")?;
            if &redeem_locator_key(tx, directory.shard)? != physical
                || directory.locators.is_empty()
            {
                return Err(corrupt("billing redeem locator catalog identity differs"));
            }
            let mut rows = Vec::with_capacity(directory.locators.len());
            for locator in &directory.locators {
                let batch = read_redeem_batch(db, tx, &locator.record_id)?;
                if batch.codes.get(locator.ordinal).map(String::as_str)
                    != Some(locator.code.as_str())
                    || redeem_locator_shard(&locator.code) != directory.shard
                {
                    return Err(corrupt("billing redeem locator catalog differs"));
                }
                rows.push(json!({"code": locator.code, "record_id": locator.record_id}));
            }
            Ok(json!(rows))
        }
        "redeem_states" => {
            let raw = versioned_payload(db, tx, physical, raw)?;
            let directory: RedeemStateShard =
                decode(&raw, "billing redeem state catalog malformed")?;
            if &redeem_state_key(tx, directory.shard)? != physical || directory.states.is_empty() {
                return Err(corrupt("billing redeem state catalog identity differs"));
            }
            let mut rows = Vec::with_capacity(directory.states.len());
            for state in &directory.states {
                if redeem_locator(db, tx, &state.code)?.is_none()
                    || redeem_locator_shard(&state.code) != directory.shard
                {
                    return Err(corrupt("billing redeem state catalog differs"));
                }
                rows.push(json!({"code": state.code, "redeemed_by": state.redeemed_by,
                    "redeemed_at": state.redeemed_at}));
            }
            Ok(json!(rows))
        }
        "redeem_created_index" | "redeem_batch_created_index" => {
            let record_id = std::str::from_utf8(raw)
                .map_err(|_| corrupt("billing redeem created index malformed"))?;
            let batch = read_redeem_batch(db, tx, record_id)?;
            let expected = if table == "redeem_created_index" {
                redeem_created_key(tx, &batch)?
            } else {
                redeem_batch_created_key(tx, &batch)?
            };
            if &expected != physical {
                return Err(corrupt("billing redeem created catalog identity differs"));
            }
            Ok(json!({"record_id": record_id}))
        }
        "redeem_count" => {
            let count: usize = decode(raw, "billing redeem count catalog malformed")?;
            if count == 0 || count > MAX_BILLING_REDEEM_CODES || physical.key_bytes() != b"all" {
                return Err(corrupt("billing redeem count catalog differs"));
            }
            Ok(json!({"count": count}))
        }
        "id_claims" | "idempotency" | "time_index" => {
            let id = if table == "time_index" {
                std::str::from_utf8(raw)
                    .map_err(|_| corrupt("billing time index malformed"))?
                    .to_owned()
            } else {
                decode::<Claim>(raw, "billing claim catalog malformed")?.ledger_id
            };
            let entry = checked_entry(db, tx, &id)?
                .ok_or_else(|| corrupt("billing index target missing"))?;
            let expected = match table {
                "id_claims" => claim_key(tx, &id)?,
                "idempotency" => idem_key(tx, &entry)?,
                _ => time_key(tx, &entry)?,
            };
            if &expected != physical
                || (table == "idempotency"
                    && (entry.ref_type.is_empty() || entry.ref_id.is_empty()))
            {
                return Err(corrupt("billing index catalog identity differs"));
            }
            Ok(json!({"id":id}))
        }
        _ => Err(invalid("unknown billing migration table")),
    }
}
fn scan(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: ScanRequest,
) -> io::Result<Value> {
    let ns = namespace(&request.table)?;
    let raw = cursor_start(&request.table, request.cursor.as_deref())?;
    let start = key(tx, ns, &raw)?;
    let (_, end) = EntityKey::prefix_range(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, ns, b"")?;
    let page = db.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, value) in &page {
        rows.push(inspect_row(db, tx, &request.table, physical, value)?);
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let encoded: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("{}:{encoded}", request.table))
    } else {
        None
    };
    Ok(json!({"rows":rows,"cursor":cursor}))
}
pub(crate) fn execute(
    db: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut tx = db.begin_billing_migration(tenant, owner)?;
    match operation {
        "migration.billing.restore" => {
            let request = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing restore request"))?;
            let inserted = restore(db, &mut tx, request)?;
            if inserted {
                db.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.billing.payment.restore" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct PaymentRestore {
                document: Value,
            }
            let request: PaymentRestore = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing payment restore request"))?;
            let inserted = restore_payment(db, &mut tx, &request.document)?;
            if inserted {
                db.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.billing.payment.get" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct PaymentGet {
                id: String,
            }
            let request: PaymentGet = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing payment get request"))?;
            get(db, &mut tx, "payments", &request.id)
        }
        "migration.billing.payment.scan" => {
            let request = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing scan request"))?;
            scan(db, &mut tx, request)
        }
        "migration.billing.redeem.restore" => {
            let inserted = restore_redeem(db, &mut tx, &payload)?;
            if inserted {
                db.commit(tx)?;
            }
            Ok(json!({"inserted":inserted}))
        }
        "migration.billing.redeem.get" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct RedeemGet {
                code: String,
            }
            let request: RedeemGet = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing redeem get request"))?;
            redeem_get(db, &mut tx, &request.code)
        }
        "migration.billing.redeem.scan" => {
            let request = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing scan request"))?;
            scan(db, &mut tx, request)
        }
        "migration.billing.get" => {
            let request: GetRequest = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing get request"))?;
            get(db, &mut tx, &request.table, &request.id)
        }
        "migration.billing.scan" => {
            let request = serde_json::from_value(payload)
                .map_err(|_| invalid("invalid billing scan request"))?;
            scan(db, &mut tx, request)
        }
        _ => Err(invalid("unknown billing migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::{path::Path, sync::Arc};

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/billing")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let db =
            AuthorityDatabase::initialize_with_vfs(Path::new("/billing"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, db)
    }
    fn entry(id: &str, kind: &str, amount: i64, ts: u64) -> Value {
        json!({"id":id,"user_id":"alice","ts":ts,"amount_micro":amount,
            "kind":kind,"ref_type":"reserve","ref_id":"task","balance_after_micro":-711,"note":"original"})
    }
    fn import(db: &mut AuthorityDatabase, table: &str, document: Value) -> io::Result<Value> {
        execute(
            db,
            7,
            11,
            "migration.billing.restore",
            json!({"table":table,"document":document}),
        )
    }
    fn point(db: &mut AuthorityDatabase, table: &str, id: &str) -> Value {
        execute(
            db,
            7,
            11,
            "migration.billing.get",
            json!({"table":table,"id":id}),
        )
        .unwrap()
    }
    fn catalog(db: &mut AuthorityDatabase, table: &str) -> Vec<Value> {
        let mut cursor = Value::Null;
        let mut rows = Vec::new();
        loop {
            let value = execute(
                db,
                7,
                11,
                "migration.billing.scan",
                json!({"table":table,"cursor":cursor}),
            )
            .unwrap();
            let page = value["rows"].as_array().unwrap();
            assert!(page.len() <= MIGRATION_SESSION_MAX_PAGE_ROWS);
            rows.extend(page.iter().cloned());
            cursor = value["cursor"].clone();
            if cursor.is_null() {
                return rows;
            }
        }
    }

    #[test]
    fn billing_snapshots_preserve_wallet_metadata_negative_balances_and_absence() {
        let (vfs, mut db) = prepared();
        let wallet = json!({"user_id":"alice","balance_micro":-711,"currency":"CREDIT",
            "low_balance_alert_micro":33,"updated_at":987});
        assert_eq!(
            import(&mut db, "wallets", wallet.clone()).unwrap()["inserted"],
            true
        );
        assert_eq!(
            import(&mut db, "wallets", wallet.clone()).unwrap()["inserted"],
            false
        );
        let mut changed = wallet.clone();
        changed["updated_at"] = json!(988);
        assert!(import(&mut db, "wallets", changed).is_err());
        let mut ledger = entry("ledger-only", "debit", -10, 25);
        ledger["user_id"] = json!("bob");
        import(&mut db, "ledger", ledger.clone()).unwrap();
        assert!(point(&mut db, "wallets", "bob").is_null());
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/billing"), vfs).unwrap();
        assert_eq!(point(&mut db, "wallets", "alice"), wallet);
        assert_eq!(point(&mut db, "ledger", "ledger-only"), ledger);
        let mut tx = db.begin_billing_migration(7, 11).unwrap();
        wallet_apply(
            &db,
            &mut tx,
            &WalletApplyRequest {
                user_id: "alice".into(),
                amount_micro: 12,
                kind: "topup".into(),
                ref_type: "".into(),
                ref_id: "".into(),
                note: "".into(),
                allow_negative: true,
                ledger_id: "after-migration".into(),
                occurred_at: 1000,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        let updated = point(&mut db, "wallets", "alice");
        assert_eq!(updated["balance_micro"], -699);
        assert_eq!(updated["low_balance_alert_micro"], 33);
        assert_eq!(updated["updated_at"], 1000);
    }

    #[test]
    fn billing_snapshot_indexes_are_order_independent_and_identity_exact() {
        let (_, mut db) = prepared();
        let release = entry("a-release", "reserve_release", 30, 20);
        let reserve = entry("z-reserve", "reserve", -80, 10);
        import(&mut db, "ledger", release.clone()).unwrap();
        import(&mut db, "ledger", reserve.clone()).unwrap();
        assert_eq!(
            catalog(&mut db, "aggregates"),
            vec![json!({"user_id":"alice","count":2,"total_micro":-50})]
        );
        assert_eq!(
            catalog(&mut db, "reserves"),
            vec![json!({"user_id":"alice","ref_id":"task","held_micro":50,"last_reserve_ts":10})]
        );
        assert_eq!(
            catalog(&mut db, "reserve_age"),
            catalog(&mut db, "reserves")
        );
        assert_eq!(
            catalog(&mut db, "time_index"),
            vec![json!({"id":"a-release"}), json!({"id":"z-reserve"})]
        );
        let mut collision = reserve.clone();
        collision["id"] = json!("other-id");
        assert!(import(&mut db, "ledger", collision).is_err());
        collision = reserve.clone();
        collision["amount_micro"] = json!(-81);
        assert!(import(&mut db, "ledger", collision).is_err());
        assert_eq!(
            import(&mut db, "ledger", reserve).unwrap()["inserted"],
            false
        );
        assert_eq!(catalog(&mut db, "id_claims").len(), 2);
        assert_eq!(catalog(&mut db, "idempotency").len(), 2);
        assert!(catalog(&mut db, "wallets").is_empty());
    }

    #[test]
    fn billing_snapshot_catalogs_page_and_bind_table_and_tenant() {
        let (_, mut db) = prepared();
        for index in 0..130 {
            import(
                &mut db,
                "wallets",
                json!({"user_id":format!("user-{index}"),"balance_micro":index,
                "currency":"CREDIT","low_balance_alert_micro":0,"updated_at":1}),
            )
            .unwrap();
        }
        assert_eq!(catalog(&mut db, "wallets").len(), 130);
        let page = execute(
            &mut db,
            7,
            11,
            "migration.billing.scan",
            json!({"table":"wallets","cursor":null}),
        )
        .unwrap();
        assert!(execute(
            &mut db,
            7,
            11,
            "migration.billing.scan",
            json!({"table":"ledger","cursor":page["cursor"]})
        )
        .is_err());
        assert_eq!(
            execute(
                &mut db,
                8,
                11,
                "migration.billing.scan",
                json!({"table":"wallets","cursor":null})
            )
            .unwrap()["rows"],
            json!([])
        );
        assert!(execute(
            &mut db,
            7,
            11,
            "migration.billing.restore",
            json!({"table":"aggregates","document":{}})
        )
        .is_err());
    }

    fn payment(id: &str, provider_id: &str) -> Value {
        json!({"id":id,"user_id":"alice","provider":"stripe","provider_id":provider_id,
            "amount_minor":1200,"currency":"USD","credit_micro":10_000_000,
            "status":"settled","created_at":100,"settled_at":200,"raw":{"k":"v"}})
    }

    #[test]
    fn payment_snapshots_preserve_exact_fields_and_rebuild_claims_and_count() {
        let (vfs, mut db) = prepared();
        let document = payment("pay-1", "pi_1");
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.billing.payment.restore",
                json!({"document":document.clone()})
            )
            .unwrap()["inserted"],
            true
        );
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/billing"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.billing.payment.restore",
                json!({"document":document.clone()})
            )
            .unwrap()["inserted"],
            false
        );
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.billing.payment.get",
                json!({"id":"pay-1"})
            )
            .unwrap(),
            document
        );
        let mut changed = document.clone();
        changed["settled_at"] = json!(201);
        assert!(execute(
            &mut db,
            7,
            11,
            "migration.billing.payment.restore",
            json!({"document":changed})
        )
        .is_err());
        // Derived catalogs enumerate independently and match the primary row.
        assert_eq!(catalog(&mut db, "payments"), vec![json!({"id":"pay-1"})]);
        assert_eq!(
            catalog(&mut db, "payment_id_claims"),
            vec![json!({"id":"pay-1"})]
        );
        assert_eq!(
            catalog(&mut db, "payment_provider_claims"),
            vec![json!({"id":"pay-1"})]
        );
        assert_eq!(
            catalog(&mut db, "payment_created_index"),
            vec![json!({"id":"pay-1"})]
        );
        assert_eq!(catalog(&mut db, "payment_count"), vec![json!({"count":1})]);
        // The live find path resolves the provider claim.
        let mut tx = db.begin_billing_migration(7, 11).unwrap();
        let found = payment_find(&db, &mut tx, "stripe", "pi_1")
            .unwrap()
            .unwrap();
        let found: Value = serde_json::from_slice(&found).unwrap();
        assert_eq!(found["id"], "pay-1");
    }

    fn redeem_batch(codes: &[&str]) -> Value {
        json!({"codes":codes,"amount_micro":75,"batch":"batch-a","created_by":"test",
            "created_at":100,"expires_at":0,"note":"n","redeemed":[]})
    }

    #[test]
    fn redeem_snapshots_preserve_batches_states_and_the_code_count() {
        let (vfs, mut db) = prepared();
        let mut request = redeem_batch(&["code-a", "code-b", "code-c"]);
        request["redeemed"] = json!([
            {"code":"code-b","redeemed_by":"alice","redeemed_at":150},
        ]);
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.billing.redeem.restore",
                request.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(db);
        vfs.crash().unwrap();
        let mut db = AuthorityDatabase::open_with_vfs(Path::new("/billing"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.billing.redeem.restore",
                request.clone()
            )
            .unwrap()["inserted"],
            false
        );
        let got = execute(
            &mut db,
            7,
            11,
            "migration.billing.redeem.get",
            json!({"code":"code-b"}),
        )
        .unwrap();
        assert_eq!(got["redeemed_by"], "alice");
        assert_eq!(got["redeemed_at"], 150);
        assert_eq!(got["amount_micro"], 75);
        let got = execute(
            &mut db,
            7,
            11,
            "migration.billing.redeem.get",
            json!({"code":"code-a"}),
        )
        .unwrap();
        assert_eq!(got["redeemed_by"], "");
        assert_eq!(got["redeemed_at"], 0);
        // Divergent batch fails closed; a state that targets a foreign code
        // fails before any write.
        let mut changed = request.clone();
        changed["amount_micro"] = json!(76);
        assert!(execute(&mut db, 7, 11, "migration.billing.redeem.restore", changed).is_err());
        let mut foreign = redeem_batch(&["code-z"]);
        foreign["redeemed"] = json!([
            {"code":"code-a","redeemed_by":"bob","redeemed_at":160},
        ]);
        assert!(execute(&mut db, 7, 11, "migration.billing.redeem.restore", foreign).is_err());
        // Counts and catalogs enumerate independently.
        assert_eq!(catalog(&mut db, "redeem_count"), vec![json!({"count":3})]);
        assert_eq!(
            catalog(&mut db, "redeem_batches"),
            vec![json!([{"record_id":"code-a","batch":"batch-a","created_at":100,"codes":3}])]
        );
        // States and locators catalog one row per touched shard; every row
        // validates its targets independently.
        assert_eq!(catalog(&mut db, "redeem_states").len(), 1);
        assert_eq!(catalog(&mut db, "redeem_locators").len(), 3);
        assert_eq!(
            catalog(&mut db, "redeem_created_index"),
            vec![json!({"record_id":"code-a"})]
        );
        // The live list path enumerates the restored batch.
        let mut tx = db.begin_billing_migration(7, 11).unwrap();
        let listed = redeem_codes_list(
            &db,
            &mut tx,
            &RedeemListRequest {
                batch: "batch-a".into(),
                status: "all".into(),
                limit: 10,
                offset: 0,
            },
        )
        .unwrap();
        let listed: Value = serde_json::from_slice(&listed).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 3);
        // A live apply on an imported unredeemed code redeems against the
        // preserved batch and leaves the count unchanged.
        let mut tx = db.begin_billing_migration(7, 11).unwrap();
        let applied = redeem_code_apply(
            &db,
            &mut tx,
            &RedeemApplyRequest {
                code: "code-c".into(),
                user_id: "carol".into(),
                redeemed_at: 500,
                ledger_id: "ledger-redeem-c".into(),
                physical_updated_at_ms: 500,
            },
        )
        .unwrap();
        let applied: Value = serde_json::from_slice(&applied).unwrap();
        assert_eq!(applied["status"], "redeemed");
        db.commit(tx).unwrap();
        assert_eq!(catalog(&mut db, "redeem_count"), vec![json!({"count":3})]);
        let state_codes: usize = catalog(&mut db, "redeem_states")
            .iter()
            .map(|row| row.as_array().unwrap().len())
            .sum();
        assert_eq!(state_codes, 2);
    }

    #[test]
    fn redeem_snapshot_preserves_a_ten_thousand_code_batch_below_write_budget() {
        let (_vfs, mut db) = prepared();
        let codes: Vec<String> = (0..10_000).map(|index| format!("c{index:05}")).collect();
        let request = json!({"codes":codes,"amount_micro":75,"batch":"maximum",
            "created_by":"test","created_at":100,"expires_at":0,"note":"",
            "redeemed":[]});
        assert_eq!(
            execute(
                &mut db,
                7,
                11,
                "migration.billing.redeem.restore",
                request.clone()
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(&mut db, 7, 11, "migration.billing.redeem.restore", request).unwrap()
                ["inserted"],
            false
        );
        assert_eq!(
            catalog(&mut db, "redeem_count"),
            vec![json!({"count":10_000})]
        );
        let got = execute(
            &mut db,
            7,
            11,
            "migration.billing.redeem.get",
            json!({"code":"c09999"}),
        )
        .unwrap();
        assert_eq!(got["codes"].as_array().unwrap().len(), 10_000);
    }

    #[test]
    fn billing_snapshot_and_every_index_recover_atomically_under_commit_faults() {
        let (baseline, mut db) = prepared();
        import(&mut db, "ledger", entry("one", "reserve", -80, 10)).unwrap();
        let trace = baseline.trace().unwrap();
        assert!(!trace.is_empty());
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
            if operation == &Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            if matches!(
                operation,
                Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
            ) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let (vfs, mut db) = prepared();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged =
                    import(&mut db, "ledger", entry("one", "reserve", -80, 10)).is_ok();
                drop(db);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut db = AuthorityDatabase::open_with_vfs(Path::new("/billing"), vfs).unwrap();
                let exists = !point(&mut db, "ledger", "one").is_null();
                assert!(
                    !acknowledged || exists,
                    "lost acknowledged snapshot at {index}: {action:?}"
                );
                for table in [
                    "ledger",
                    "id_claims",
                    "idempotency",
                    "time_index",
                    "aggregates",
                    "reserves",
                    "reserve_age",
                ] {
                    assert_eq!(
                        catalog(&mut db, table).len(),
                        usize::from(exists),
                        "{table} {index} {action:?}"
                    );
                }
                import(&mut db, "ledger", entry("one", "reserve", -80, 10)).unwrap();
                assert_eq!(catalog(&mut db, "aggregates")[0]["count"], 1);
            }
        }
    }
}
