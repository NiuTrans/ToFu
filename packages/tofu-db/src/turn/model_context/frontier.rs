//! One authority-minted prefix certificate in the existing lane-directory value.
//! Empty/legacy values force a complete scan. Directory lifecycle owns cleanup;
//! callers never supply this state, and its key binds tenant, owner and lane.
//! The existing authority-open epoch fences older writers across every reopen.
use super::sha_state::ShaState;
use super::*;
const MAGIC: &[u8; 8] = b"TDBCTX01";
const MAX_CERTIFICATE_BYTES: usize = 1024;
#[derive(Clone)]
pub(super) struct Frontier {
    pub epoch: uuid::Uuid,
    pub ordinal: u64,
    pub count: usize,
    pub terminal_id: String,
    pub state: ShaState,
}
fn key(
    transaction: &AuthorityTransaction,
    conversation: &str,
    lane: &str,
) -> io::Result<EntityKey> {
    crate::model_continuation::lane_index_key(
        transaction,
        &crate::model_continuation::continuation_raw_key(conversation, lane),
    )
}
impl Frontier {
    fn encode(&self, key: &EntityKey) -> io::Result<Vec<u8>> {
        if self.count >= MAX_LIST_ROWS || self.terminal_id.len() > 512 {
            return Err(invalid_data("context frontier exceeds its bound"));
        }
        let mut out = MAGIC.to_vec();
        out.extend_from_slice(&Sha256::digest(key.encoded()));
        out.extend_from_slice(self.epoch.as_bytes());
        out.extend_from_slice(&self.ordinal.to_be_bytes());
        out.extend_from_slice(&(self.count as u64).to_be_bytes());
        out.extend_from_slice(&(self.terminal_id.len() as u16).to_be_bytes());
        out.extend_from_slice(self.terminal_id.as_bytes());
        let state = self.state.encode();
        out.extend_from_slice(&(state.len() as u16).to_be_bytes());
        out.extend_from_slice(&state);
        out.extend_from_slice(&Sha256::digest(&out).to_vec());
        Ok(out)
    }
    pub(super) fn decode(key: &EntityKey, encoded: &[u8]) -> io::Result<Self> {
        let bad = || invalid_data("context frontier certificate is malformed or out of scope");
        if !(149..=MAX_CERTIFICATE_BYTES).contains(&encoded.len()) || &encoded[..8] != MAGIC {
            return Err(bad());
        }
        let body = &encoded[..encoded.len() - 32];
        if Sha256::digest(body).as_slice() != &encoded[encoded.len() - 32..]
            || Sha256::digest(key.encoded()).as_slice() != &body[8..40]
        {
            return Err(bad());
        }
        let epoch = uuid::Uuid::from_slice(&body[40..56]).map_err(|_| bad())?;
        let ordinal = u64::from_be_bytes(body[56..64].try_into().unwrap());
        let count = u64::from_be_bytes(body[64..72].try_into().unwrap());
        let id_len = usize::from(u16::from_be_bytes(body[72..74].try_into().unwrap()));
        if count >= MAX_LIST_ROWS as u64 || id_len == 0 || id_len > 512 || 76 + id_len > body.len()
        {
            return Err(bad());
        }
        let terminal_id = std::str::from_utf8(&body[74..74 + id_len])
            .map_err(|_| bad())?
            .to_owned();
        let state_len = usize::from(u16::from_be_bytes(
            body[74 + id_len..76 + id_len].try_into().unwrap(),
        ));
        if body.len() != 76 + id_len + state_len {
            return Err(bad());
        }
        let state = ShaState::decode(&body[76 + id_len..])?;
        Ok(Self {
            epoch,
            ordinal,
            count: count as usize,
            terminal_id,
            state,
        })
    }
    pub(super) fn matches(&self, cached: &Value) -> bool {
        cached.get("historyProof")
            == Some(&json!({"version":2,"count":self.count,"digest":self.state.digest()}))
            && cached
                .get("turns")
                .and_then(Value::as_array)
                .is_some_and(|turns| {
                    turns.len() == 1
                        && turns[0].get(0).and_then(Value::as_str)
                            == Some(self.terminal_id.as_str())
                })
    }
}
pub(super) fn load(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    lane: &str,
) -> io::Result<Option<Frontier>> {
    let key = key(transaction, conversation, lane)?;
    let stored = database
        .entity_get(transaction, &key)?
        .filter(|v| !v.is_empty())
        .map(|encoded| Frontier::decode(&key, &encoded))
        .transpose()?;
    // The exclusive engine lease prevents another writer within this epoch.
    // Older binaries know nothing about invalidation, so no persisted certificate
    // can authorize skipping a prefix after any authority reopen. Read replicas
    // inherit this epoch, and still read the certificate in their own snapshot.
    Ok(stored.filter(|frontier| frontier.epoch == database.event_read_epoch()))
}
/// Called only after an accepted ordinal CAS, in that exact write transaction.
/// A client proof is never sufficient: compare with independently read authority
/// headers before installing resumable state. Forged proofs retain old cold behavior.
pub(crate) fn certify(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    lane: &str,
    ordinal: u64,
    projection: &Map<String, Value>,
) -> io::Result<Vec<u8>> {
    if lane != "main" {
        return Ok(Vec::new());
    }
    let Some(proof) = projection
        .get("historyProof")
        .filter(|p| p.get("version").and_then(Value::as_u64) == Some(2))
    else {
        return Ok(Vec::new());
    };
    let Some(terminal_id) = projection
        .get("turns")
        .and_then(Value::as_array)
        .filter(|t| t.len() == 1)
        .and_then(|t| t[0].get(0))
        .and_then(Value::as_str)
    else {
        return Ok(Vec::new());
    };
    let previous =
        load(database, transaction, conversation, lane)?.filter(|v| v.ordinal <= ordinal);
    let (mut state, count, start) = match previous {
        Some(previous) => (previous.state, previous.count, Some(previous.ordinal)),
        None => (ShaState::default(), 0, None),
    };
    let Some(headers) = scan_headers(
        database,
        transaction,
        conversation,
        start,
        ordinal,
        terminal_id,
        count,
    )?
    else {
        return Ok(Vec::new());
    };
    for header in &headers[..headers.len() - 1] {
        state.update(&header_encoding(header)?)?;
    }
    let count = count + headers.len() - 1;
    if proof != &json!({"version":2,"count":count,"digest":state.digest()}) {
        return Ok(Vec::new());
    }
    Frontier {
        epoch: database.event_read_epoch(),
        ordinal,
        count,
        terminal_id: terminal_id.to_owned(),
        state,
    }
    .encode(&key(transaction, conversation, lane)?)
}
/// Every compaction-header publication crosses this hook. A real prefix mutation
/// clears the certificate in the same transaction. The directory entry remains
/// so deletion can still find its continuation. Tail appends and display-only
/// enrichment do not change the authenticated prefix.
pub(crate) fn before_header_write(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    lane: &str,
    ordinal: u64,
    header_key: &EntityKey,
    next: &[u8],
) -> io::Result<()> {
    if lane != "main" {
        return Ok(());
    }
    let Some(frontier) = load(database, transaction, conversation, lane)? else {
        return Ok(());
    };
    if ordinal >= frontier.ordinal {
        return Ok(());
    }
    let old = database.entity_get(transaction, header_key)?;
    let parse = |bytes: &[u8]| {
        serde_json::from_slice::<Value>(bytes)
            .map_err(|_| invalid_data("context header is malformed"))
    };
    if let Some(old) = old {
        if header_encoding(&parse(&old)?)? == header_encoding(&parse(next)?)? {
            return Ok(());
        }
    }
    database.entity_put(
        transaction,
        key(transaction, conversation, lane)?,
        Vec::new(),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn certificate_binds_scope_and_rejects_every_changed_byte_and_length() {
        let key = EntityKey::new(
            7,
            11,
            crate::model_continuation::LANE_INDEX_NAMESPACE,
            b"lane",
        )
        .unwrap();
        let mut state = ShaState::default();
        state.update(b"authenticated prefix\n").unwrap();
        let value = Frontier {
            epoch: uuid::Uuid::new_v4(),
            ordinal: 7,
            count: 3,
            terminal_id: "turn-7".into(),
            state,
        };
        let encoded = value.encode(&key).unwrap();
        assert!(Frontier::decode(&key, &encoded).is_ok());
        for index in 0..encoded.len() {
            let mut corrupt = encoded.clone();
            corrupt[index] ^= 1;
            assert!(Frontier::decode(&key, &corrupt).is_err());
        }
        for length in 0..encoded.len() {
            assert!(Frontier::decode(&key, &encoded[..length]).is_err());
        }
        // Valid checksums do not excuse malformed internal length/count fields.
        for (offset, bytes) in [
            (64, (MAX_LIST_ROWS as u64).to_be_bytes().to_vec()),
            (72, 0_u16.to_be_bytes().to_vec()),
            (72, 513_u16.to_be_bytes().to_vec()),
        ] {
            let mut corrupt = encoded.clone();
            corrupt[offset..offset + bytes.len()].copy_from_slice(&bytes);
            let end = corrupt.len() - 32;
            let checksum = Sha256::digest(&corrupt[..end]);
            corrupt[end..].copy_from_slice(&checksum);
            assert!(Frontier::decode(&key, &corrupt).is_err());
        }
        for (tenant, owner, keybytes) in [
            (8, 11, b"lane".as_slice()),
            (7, 12, b"lane".as_slice()),
            (7, 11, b"other".as_slice()),
        ] {
            let foreign = EntityKey::new(
                tenant,
                owner,
                crate::model_continuation::LANE_INDEX_NAMESPACE,
                keybytes,
            )
            .unwrap();
            assert!(Frontier::decode(&foreign, &encoded).is_err());
        }
    }
}
