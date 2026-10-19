//! Deterministic WAL envelope for one atomic transaction and its block roots.

use std::collections::BTreeSet;
use std::io;

use crate::block::BlockId;

const MAGIC: &[u8; 8] = b"TDBTXN01";
const FAMILY_MAGIC: &[u8; 8] = b"TDBFAM01";
const FAMILY_VERSION: u32 = 1;
const ENVELOPE_VERSION: u32 = 2;
/// Version 3: identical header and block id list, followed by one
/// `[u32 length | payload]` slot per block id plus one authority-state slot
/// (`CARRIED_ABSENT` marks an identifier-only entry), then the inline
/// payload. Carried copies let the WAL alone rebuild freshly written loose
/// blocks after a crash that lost files not yet fsynced; blocks already
/// durable keep identifier-only entries so record growth stays bounded to
/// new writes.
const ENVELOPE_VERSION_CARRIED: u32 = 3;
const CARRIED_ABSENT: u32 = u32::MAX;
const HEADER_BYTES: usize = 8 + 4 + 4 + 4 + 1 + 32;
const FAMILY_HEADER_BYTES: usize = 8 + 4 + 2 + 2;
const FAMILY_RECORD_HEADER_BYTES: usize = 1 + 3 + 4;
pub const MAX_INLINE_BYTES: usize = 256 * 1024;
/// Total carried payload bytes embedded in one version 3 envelope. Above
/// this bound the commit spills back to identifier-only references and
/// fsyncs the loose blocks instead, preserving the 4 MiB history segment
/// ceiling and bounded WAL replay memory.
pub const MAX_CARRIED_PAYLOAD_BYTES: usize = 2 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
#[repr(u8)]
pub enum FamilyRecordKind {
    EntityRoot = 1,
    StreamCommit = 2,
    CommandReceipt = 3,
    LogicalOutbox = 4,
    EntityOverlay = 5,
}

impl FamilyRecordKind {
    fn decode(value: u8) -> io::Result<Self> {
        match value {
            1 => Ok(Self::EntityRoot),
            2 => Ok(Self::StreamCommit),
            3 => Ok(Self::CommandReceipt),
            4 => Ok(Self::LogicalOutbox),
            5 => Ok(Self::EntityOverlay),
            _ => Err(invalid_data("unknown transaction family record kind")),
        }
    }

    const fn allows_multiple(self) -> bool {
        matches!(self, Self::StreamCommit)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FamilyRecord<'payload> {
    pub kind: FamilyRecordKind,
    pub payload: &'payload [u8],
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OwnedFamilyRecord {
    kind: FamilyRecordKind,
    payload: Vec<u8>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct FamilyTransactionBuilder {
    records: Vec<OwnedFamilyRecord>,
    block_ids: BTreeSet<BlockId>,
    inline_bytes: usize,
}

impl FamilyTransactionBuilder {
    fn collect_new_block_ids(
        &self,
        block_ids: impl IntoIterator<Item = BlockId>,
    ) -> io::Result<BTreeSet<BlockId>> {
        let mut additional_block_ids = BTreeSet::new();
        for block_id in block_ids {
            if !self.block_ids.contains(&block_id) {
                additional_block_ids.insert(block_id);
            }
            if self.block_ids.len().checked_add(additional_block_ids.len()).and_then(|count| u32::try_from(count).ok()).is_none() {
                return Err(invalid_input("transaction block count exceeds its u32 representation"));
            }
        }
        Ok(additional_block_ids)
    }

    pub fn add_block_references(
        &mut self,
        block_ids: impl IntoIterator<Item = BlockId>,
    ) -> io::Result<()> {
        let additional_block_ids = self.collect_new_block_ids(block_ids)?;
        self.block_ids.extend(additional_block_ids);
        Ok(())
    }

    pub fn add_record(
        &mut self,
        kind: FamilyRecordKind,
        payload: Vec<u8>,
        block_ids: impl IntoIterator<Item = BlockId>,
    ) -> io::Result<()> {
        if payload.is_empty() {
            return Err(invalid_input("transaction family record is empty"));
        }
        if self.records.len() == usize::from(u16::MAX) {
            return Err(invalid_input("transaction family count exceeds its u16 representation"));
        }
        if !kind.allows_multiple() && self.records.iter().any(|record| record.kind == kind) {
            return Err(invalid_input("transaction family record is duplicated"));
        }
        let next_inline_bytes = self
            .inline_bytes
            .checked_add(FAMILY_RECORD_HEADER_BYTES)
            .and_then(|length| length.checked_add(payload.len()))
            .ok_or_else(|| invalid_input("transaction family payload length overflow"))?;
        if FAMILY_HEADER_BYTES
            .checked_add(next_inline_bytes)
            .filter(|length| *length <= MAX_INLINE_BYTES)
            .is_none()
        {
            return Err(invalid_input(
                "transaction family records exceed inline payload bound",
            ));
        }
        let additional_block_ids = self.collect_new_block_ids(block_ids)?;
        self.inline_bytes = next_inline_bytes;
        self.block_ids.extend(additional_block_ids);
        self.records.push(OwnedFamilyRecord { kind, payload });
        Ok(())
    }

    pub fn prepare(mut self) -> io::Result<TransactionEnvelope> {
        self.records.sort_by(|left, right| {
            left.kind
                .cmp(&right.kind)
                .then_with(|| left.payload.cmp(&right.payload))
        });
        let borrowed = self
            .records
            .iter()
            .map(|record| FamilyRecord {
                kind: record.kind,
                payload: &record.payload,
            })
            .collect::<Vec<_>>();
        Ok(TransactionEnvelope {
            block_ids: self.block_ids.into_iter().collect(),
            inline_payload: encode_family_records(&borrowed)?,
            authority_state_update: None,
        })
    }
}

/// Result of [`TransactionEnvelope::decode_with_carried`]: the envelope,
/// one carried payload slot per referenced block (`None` = identifier-only
/// reference), and the optional carried authority-state payload.
pub type DecodedCarriedEnvelope = (TransactionEnvelope, Vec<Option<Vec<u8>>>, Option<Vec<u8>>);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TransactionEnvelope {
    pub block_ids: Vec<BlockId>,
    pub inline_payload: Vec<u8>,
    /// `None` leaves the prior authority state unchanged; `Some(None)`
    /// publishes an empty state; `Some(Some(root))` publishes that immutable
    /// root. This tri-state lets recovery distinguish a generic engine record
    /// from an authoritative empty database.
    pub authority_state_update: Option<Option<BlockId>>,
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub fn encode_family_records(records: &[FamilyRecord<'_>]) -> io::Result<Vec<u8>> {
    if records.is_empty() || u16::try_from(records.len()).is_err() {
        return Err(invalid_input(
            "transaction family record count is empty or exceeds its u16 representation",
        ));
    }
    let mut previous: Option<FamilyRecord<'_>> = None;
    let mut encoded_length = FAMILY_HEADER_BYTES;
    for record in records {
        if record.payload.is_empty() {
            return Err(invalid_input("transaction family record is empty"));
        }
        if let Some(previous_record) = previous {
            if record.kind < previous_record.kind
                || (record.kind == previous_record.kind
                    && (!record.kind.allows_multiple()
                        || record.payload <= previous_record.payload))
            {
                return Err(invalid_input(
                    "transaction family records are not canonical",
                ));
            }
        }
        previous = Some(*record);
        u32::try_from(record.payload.len())
            .map_err(|_| invalid_input("transaction family record length overflow"))?;
        encoded_length = encoded_length
            .checked_add(FAMILY_RECORD_HEADER_BYTES)
            .and_then(|length| length.checked_add(record.payload.len()))
            .ok_or_else(|| invalid_input("transaction family record length overflow"))?;
        if encoded_length > MAX_INLINE_BYTES {
            return Err(invalid_input(
                "transaction family records exceed inline payload bound",
            ));
        }
    }
    let mut encoded = Vec::with_capacity(encoded_length);
    encoded.extend_from_slice(FAMILY_MAGIC);
    encoded.extend_from_slice(&FAMILY_VERSION.to_le_bytes());
    encoded.extend_from_slice(&(records.len() as u16).to_le_bytes());
    encoded.extend_from_slice(&0_u16.to_le_bytes());
    for record in records {
        encoded.push(record.kind as u8);
        encoded.extend_from_slice(&[0; 3]);
        encoded.extend_from_slice(&(record.payload.len() as u32).to_le_bytes());
        encoded.extend_from_slice(record.payload);
    }
    Ok(encoded)
}

pub fn decode_family_records(bytes: &[u8]) -> io::Result<Option<Vec<FamilyRecord<'_>>>> {
    if !bytes.starts_with(FAMILY_MAGIC) {
        return Ok(None);
    }
    if bytes.len() < FAMILY_HEADER_BYTES || bytes.len() > MAX_INLINE_BYTES {
        return Err(invalid_data("invalid transaction family header length"));
    }
    let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
    let count = u16::from_le_bytes(bytes[12..14].try_into().unwrap()) as usize;
    let reserved = u16::from_le_bytes(bytes[14..16].try_into().unwrap());
    if version != FAMILY_VERSION || count == 0 || reserved != 0 {
        return Err(invalid_data("invalid transaction family header"));
    }
    // Each record requires a header and a nonempty payload. Check the
    // encoded evidence before reserving space for the claimed count.
    if count > (bytes.len() - FAMILY_HEADER_BYTES) / (FAMILY_RECORD_HEADER_BYTES + 1) {
        return Err(invalid_data("transaction family count exceeds encoded records"));
    }
    let mut records = Vec::with_capacity(count);
    let mut offset = FAMILY_HEADER_BYTES;
    let mut previous: Option<FamilyRecord<'_>> = None;
    for _ in 0..count {
        let header_end = offset
            .checked_add(FAMILY_RECORD_HEADER_BYTES)
            .ok_or_else(|| invalid_data("transaction family offset overflow"))?;
        let header = bytes
            .get(offset..header_end)
            .ok_or_else(|| invalid_data("truncated transaction family record"))?;
        let kind = FamilyRecordKind::decode(header[0])?;
        if header[1..4] != [0, 0, 0] {
            return Err(invalid_data(
                "transaction family reserved bytes are nonzero",
            ));
        }
        let payload_length = u32::from_le_bytes(header[4..8].try_into().unwrap()) as usize;
        if payload_length == 0 {
            return Err(invalid_data("empty transaction family record"));
        }
        offset = header_end;
        let payload_end = offset
            .checked_add(payload_length)
            .ok_or_else(|| invalid_data("transaction family payload length overflow"))?;
        let payload = bytes
            .get(offset..payload_end)
            .ok_or_else(|| invalid_data("truncated transaction family payload"))?;
        let record = FamilyRecord { kind, payload };
        if let Some(previous_record) = previous {
            if kind < previous_record.kind
                || (kind == previous_record.kind
                    && (!kind.allows_multiple() || payload <= previous_record.payload))
            {
                return Err(invalid_data("noncanonical transaction family record order"));
            }
        }
        previous = Some(record);
        records.push(record);
        offset = payload_end;
    }
    if offset != bytes.len() {
        return Err(invalid_data(
            "transaction family payload has trailing bytes",
        ));
    }
    Ok(Some(records))
}

impl TransactionEnvelope {
    pub fn encode(&self) -> io::Result<Vec<u8>> {
        if u32::try_from(self.block_ids.len()).is_err() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction block count exceeds its u32 representation",
            ));
        }
        if self.inline_payload.len() > MAX_INLINE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction inline payload exceeds 256 KiB",
            ));
        }
        let mut bytes = Vec::with_capacity(
            HEADER_BYTES + self.block_ids.len() * 32 + self.inline_payload.len(),
        );
        bytes.extend_from_slice(MAGIC);
        bytes.extend_from_slice(&ENVELOPE_VERSION.to_le_bytes());
        bytes.extend_from_slice(&(self.block_ids.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&(self.inline_payload.len() as u32).to_le_bytes());
        match self.authority_state_update {
            None => bytes.push(0),
            Some(None) => bytes.push(1),
            Some(Some(root)) => {
                bytes.push(2);
                bytes.extend_from_slice(&root.0);
            }
        }
        if !matches!(self.authority_state_update, Some(Some(_))) {
            bytes.extend_from_slice(&[0; 32]);
        }
        for block_id in &self.block_ids {
            bytes.extend_from_slice(&block_id.0);
        }
        bytes.extend_from_slice(&self.inline_payload);
        Ok(bytes)
    }

    /// Version 3 encoding. `carried` aligns with `block_ids` element by
    /// element; at least one payload (block or authority state) must be
    /// present so the version marker is never ambiguous — identifier-only
    /// transactions use `encode()`.
    pub fn encode_with_carried(
        &self,
        carried: &[Option<&[u8]>],
        authority_state_carried: Option<&[u8]>,
    ) -> io::Result<Vec<u8>> {
        if u32::try_from(self.block_ids.len()).is_err() {
            return Err(invalid_input("transaction block count exceeds its u32 representation"));
        }
        if self.inline_payload.len() > MAX_INLINE_BYTES {
            return Err(invalid_input("transaction inline payload exceeds 256 KiB"));
        }
        if carried.len() != self.block_ids.len() {
            return Err(invalid_input("carried payload count mismatch"));
        }
        if authority_state_carried.is_some()
            && !matches!(self.authority_state_update, Some(Some(_)))
        {
            return Err(invalid_input("authority state carried without root"));
        }
        let mut carried_bytes = authority_state_carried.map_or(0, <[u8]>::len);
        for payload in carried.iter().flatten() {
            carried_bytes = carried_bytes
                .checked_add(payload.len())
                .ok_or_else(|| invalid_input("transaction carried length overflow"))?;
        }
        if carried_bytes == 0 {
            return Err(invalid_input("carried envelope embeds no payload"));
        }
        if carried_bytes > MAX_CARRIED_PAYLOAD_BYTES {
            return Err(invalid_input("transaction carried payload exceeds 2 MiB"));
        }
        let slot_bytes = (self.block_ids.len() + 1)
            .checked_mul(4)
            .ok_or_else(|| invalid_input("transaction envelope length overflow"))?;
        let mut bytes = Vec::with_capacity(
            HEADER_BYTES
                + self.block_ids.len() * 32
                + slot_bytes
                + carried_bytes
                + self.inline_payload.len(),
        );
        bytes.extend_from_slice(MAGIC);
        bytes.extend_from_slice(&ENVELOPE_VERSION_CARRIED.to_le_bytes());
        bytes.extend_from_slice(&(self.block_ids.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&(self.inline_payload.len() as u32).to_le_bytes());
        match self.authority_state_update {
            None => {
                bytes.push(0);
                bytes.extend_from_slice(&[0; 32]);
            }
            Some(None) => {
                bytes.push(1);
                bytes.extend_from_slice(&[0; 32]);
            }
            Some(Some(root)) => {
                bytes.push(2);
                bytes.extend_from_slice(&root.0);
            }
        }
        for block_id in &self.block_ids {
            bytes.extend_from_slice(&block_id.0);
        }
        for payload in carried {
            match payload {
                Some(payload) => {
                    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
                    bytes.extend_from_slice(payload);
                }
                None => bytes.extend_from_slice(&CARRIED_ABSENT.to_le_bytes()),
            }
        }
        match authority_state_carried {
            Some(payload) => {
                bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
                bytes.extend_from_slice(payload);
            }
            None => bytes.extend_from_slice(&CARRIED_ABSENT.to_le_bytes()),
        }
        bytes.extend_from_slice(&self.inline_payload);
        Ok(bytes)
    }

    pub fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < HEADER_BYTES || &bytes[..8] != MAGIC {
            return Err(invalid_data("transaction envelope magic mismatch"));
        }
        let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        let block_count = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        let inline_len = u32::from_le_bytes(bytes[16..20].try_into().unwrap()) as usize;
        let authority_state_update = match bytes[20] {
            0 if bytes[21..53] == [0; 32] => None,
            1 if bytes[21..53] == [0; 32] => Some(None),
            2 => Some(Some(BlockId(bytes[21..53].try_into().unwrap()))),
            _ => return Err(invalid_data("invalid transaction authority state update")),
        };
        if version != ENVELOPE_VERSION
            || inline_len > MAX_INLINE_BYTES
        {
            return Err(invalid_data(
                "unsupported or unbounded transaction envelope",
            ));
        }
        let block_bytes = block_count
            .checked_mul(32)
            .ok_or_else(|| invalid_data("transaction block length overflow"))?;
        let expected = HEADER_BYTES
            .checked_add(block_bytes)
            .and_then(|length| length.checked_add(inline_len))
            .ok_or_else(|| invalid_data("transaction envelope length overflow"))?;
        if expected != bytes.len() {
            return Err(invalid_data("transaction envelope length mismatch"));
        }
        let mut block_ids = Vec::with_capacity(block_count);
        let mut offset = HEADER_BYTES;
        for _ in 0..block_count {
            block_ids.push(BlockId(bytes[offset..offset + 32].try_into().unwrap()));
            offset += 32;
        }
        Ok(Self {
            block_ids,
            inline_payload: bytes[offset..].to_vec(),
            authority_state_update,
        })
    }

    /// Accepts version 2 and version 3 envelopes, returning the envelope
    /// plus the carried payload slots (all `None` for version 2). Decode is
    /// parse-only by design: content addressing makes a mismatched payload
    /// land at its own hash-derived id — never the referenced one — and
    /// block reads verify content hashes before returning bytes.
    pub fn decode_with_carried(bytes: &[u8]) -> io::Result<DecodedCarriedEnvelope> {
        Self::decode_carried_impl(bytes, true, None)
    }

    /// Parses version 2 and version 3 envelopes without copying carried
    /// payload bytes; the exact logged bytes are retained separately by the
    /// caller, so only the parsed header is needed.
    pub fn decode_skipping_carried(bytes: &[u8]) -> io::Result<Self> {
        let (envelope, _, _) = Self::decode_carried_impl(bytes, false, None)?;
        Ok(envelope)
    }

    /// Visit borrowed carried bytes and their offsets in the canonical
    /// envelope. History indexes use this parser instead of copying payloads
    /// or maintaining a second interpretation of the transaction format.
    pub(crate) fn visit_carried(
        bytes: &[u8],
        visitor: &mut dyn FnMut(BlockId, usize, &[u8]) -> io::Result<()>,
    ) -> io::Result<Self> {
        let (envelope, _, _) = Self::decode_carried_impl(bytes, false, Some(visitor))?;
        Ok(envelope)
    }

    fn decode_carried_impl(
        bytes: &[u8], collect: bool,
        mut visitor: Option<&mut dyn FnMut(BlockId, usize, &[u8]) -> io::Result<()>>,
    ) -> io::Result<DecodedCarriedEnvelope> {
        if bytes.len() < HEADER_BYTES || &bytes[..8] != MAGIC {
            return Err(invalid_data("transaction envelope magic mismatch"));
        }
        let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        if version == ENVELOPE_VERSION {
            let envelope = Self::decode(bytes)?;
            let slots = vec![None; envelope.block_ids.len()];
            return Ok((envelope, slots, None));
        }
        if version != ENVELOPE_VERSION_CARRIED {
            return Err(invalid_data(
                "unsupported or unbounded transaction envelope",
            ));
        }
        let block_count = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        let inline_len = u32::from_le_bytes(bytes[16..20].try_into().unwrap()) as usize;
        if inline_len > MAX_INLINE_BYTES {
            return Err(invalid_data(
                "unsupported or unbounded transaction envelope",
            ));
        }
        let authority_state_update = match bytes[20] {
            0 if bytes[21..53] == [0; 32] => None,
            1 if bytes[21..53] == [0; 32] => Some(None),
            2 => Some(Some(BlockId(bytes[21..53].try_into().unwrap()))),
            _ => return Err(invalid_data("invalid transaction authority state update")),
        };
        let ids_end = HEADER_BYTES
            .checked_add(
                block_count
                    .checked_mul(32)
                    .ok_or_else(|| invalid_data("transaction block length overflow"))?,
            )
            .ok_or_else(|| invalid_data("transaction block length overflow"))?;
        if bytes.len() < ids_end + (block_count + 1) * 4 + inline_len {
            return Err(invalid_data("transaction envelope length mismatch"));
        }
        let mut block_ids = Vec::with_capacity(block_count);
        let mut offset = HEADER_BYTES;
        for _ in 0..block_count {
            block_ids.push(BlockId(bytes[offset..offset + 32].try_into().unwrap()));
            offset += 32;
        }
        let mut carried_total = 0usize;
        let mut carried_any = false;
        let mut carried = Vec::with_capacity(block_count);
        for slot in 0..block_count {
            let length_bytes = bytes.get(offset..offset + 4)
                .ok_or_else(|| invalid_data("transaction carried slot is truncated"))?;
            let length = u32::from_le_bytes(length_bytes.try_into().unwrap());
            offset += 4;
            if length == CARRIED_ABSENT {
                carried.push(None);
                continue;
            }
            let length = length as usize;
            carried_total = carried_total
                .checked_add(length)
                .ok_or_else(|| invalid_data("transaction carried length overflow"))?;
            if carried_total > MAX_CARRIED_PAYLOAD_BYTES {
                return Err(invalid_data("transaction carried payload exceeds 2 MiB"));
            }
            let end = offset
                .checked_add(length)
                .ok_or_else(|| invalid_data("transaction carried length overflow"))?;
            if end > bytes.len() {
                return Err(invalid_data("transaction envelope length mismatch"));
            }
            if let Some(visit) = visitor.as_deref_mut() {
                visit(block_ids[slot], offset, &bytes[offset..end])?;
            }
            if collect {
                carried.push(Some(bytes[offset..end].to_vec()));
            } else {
                carried.push(None);
            }
            offset = end;
            carried_any = true;
        }
        let authority_state_carried = {
            let length_bytes = bytes.get(offset..offset + 4)
                .ok_or_else(|| invalid_data("transaction authority carried slot is truncated"))?;
            let length = u32::from_le_bytes(length_bytes.try_into().unwrap());
            offset += 4;
            if length == CARRIED_ABSENT {
                None
            } else {
                if !matches!(authority_state_update, Some(Some(_))) {
                    return Err(invalid_data("authority state carried without root"));
                }
                let length = length as usize;
                carried_total = carried_total
                    .checked_add(length)
                    .ok_or_else(|| invalid_data("transaction carried length overflow"))?;
                if carried_total > MAX_CARRIED_PAYLOAD_BYTES {
                    return Err(invalid_data("transaction carried payload exceeds 2 MiB"));
                }
                let end = offset
                    .checked_add(length)
                    .ok_or_else(|| invalid_data("transaction carried length overflow"))?;
                if end > bytes.len() {
                    return Err(invalid_data("transaction envelope length mismatch"));
                }
                if let Some(visit) = visitor.as_deref_mut() {
                    let root = authority_state_update.flatten().ok_or_else(|| invalid_data("authority state carried without root"))?;
                    visit(root, offset, &bytes[offset..end])?;
                }
                carried_any = true;
                let payload = collect.then(|| bytes[offset..end].to_vec());
                offset = end;
                payload
            }
        };
        if !carried_any {
            return Err(invalid_data("carried envelope embeds no payload"));
        }
        let end = offset
            .checked_add(inline_len)
            .ok_or_else(|| invalid_data("transaction envelope length overflow"))?;
        if end != bytes.len() {
            return Err(invalid_data("transaction envelope length mismatch"));
        }
        Ok((
            Self {
                block_ids,
                inline_payload: bytes[offset..end].to_vec(),
                authority_state_update,
            },
            carried,
            authority_state_carried,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn block_reference_demand_exceeds_former_quota_and_reopens_every_payload() {
        let directory = tempfile::tempdir().unwrap();
        let payloads = (0..2101_u64).map(|ordinal| ordinal.to_be_bytes().to_vec()).collect::<Vec<_>>();
        let ids;
        {
            let mut engine = crate::engine::Engine::initialize(directory.path()).unwrap();
            let borrowed = payloads.iter().map(Vec::as_slice).collect::<Vec<_>>();
            let committed = engine.commit_transaction(b"block-demand", &borrowed).unwrap();
            assert_eq!(committed.sequence, 1);
            assert_eq!(committed.block_ids.len(), 2101);
            ids = committed.block_ids;
            let mut builder = FamilyTransactionBuilder::default();
            builder.add_record(FamilyRecordKind::EntityRoot, b"root".to_vec(), ids.iter().copied()).unwrap();
            builder.add_block_references(ids.iter().copied()).unwrap();
            assert_eq!(builder.prepare().unwrap().block_ids.len(), 2101);
            let persisted = engine.transaction_at(1).unwrap().unwrap();
            let mut corrupt = persisted.encoded_envelope.clone();
            corrupt[12..16].copy_from_slice(&u32::MAX.to_le_bytes());
            assert!(TransactionEnvelope::decode(&corrupt).is_err());
        }
        let engine = crate::engine::Engine::open(directory.path()).unwrap();
        let restored = engine.transaction_at(1).unwrap().unwrap();
        assert_eq!(restored.envelope.block_ids.len(), 2101);
        assert_eq!(restored.envelope.inline_payload, b"block-demand");
        for (id, payload) in ids.iter().zip(payloads.iter()) {
            assert_eq!(&engine.read_block(*id).unwrap(), payload);
        }
        let ordinary = TransactionEnvelope { block_ids: ids,
            inline_payload: b"ordinary".to_vec(), authority_state_update: None };
        assert_eq!(TransactionEnvelope::decode(&ordinary.encode().unwrap()).unwrap(), ordinary);
    }

    #[test]
    fn envelope_round_trips_without_implicit_fields() {
        let envelope = TransactionEnvelope {
            block_ids: vec![BlockId([3; 32]), BlockId([9; 32])],
            inline_payload: b"transaction-ir".to_vec(),
            authority_state_update: Some(Some(BlockId([9; 32]))),
        };
        assert_eq!(
            TransactionEnvelope::decode(&envelope.encode().unwrap()).unwrap(),
            envelope
        );
    }

    #[test]
    fn decoder_rejects_trailing_or_truncated_data() {
        let envelope = TransactionEnvelope {
            block_ids: vec![],
            inline_payload: b"bounded".to_vec(),
            authority_state_update: Some(None),
        };
        let mut encoded = envelope.encode().unwrap();
        encoded.push(0);
        assert_eq!(
            TransactionEnvelope::decode(&encoded).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn decoder_rejects_noncanonical_authority_state_discriminants() {
        let envelope = TransactionEnvelope {
            block_ids: vec![BlockId([7; 32])],
            inline_payload: b"stateful".to_vec(),
            authority_state_update: Some(Some(BlockId([7; 32]))),
        };
        let mut unknown = envelope.encode().unwrap();
        unknown[20] = 3;
        assert_eq!(
            TransactionEnvelope::decode(&unknown).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        let mut nonzero_absent = envelope.encode().unwrap();
        nonzero_absent[20] = 0;
        assert_eq!(
            TransactionEnvelope::decode(&nonzero_absent)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn family_records_round_trip_without_copying_payloads() {
        let entity = b"entity-root";
        let stream_one = b"stream-one";
        let stream_two = b"stream-two";
        let encoded = encode_family_records(&[
            FamilyRecord {
                kind: FamilyRecordKind::EntityRoot,
                payload: entity,
            },
            FamilyRecord {
                kind: FamilyRecordKind::StreamCommit,
                payload: stream_one,
            },
            FamilyRecord {
                kind: FamilyRecordKind::StreamCommit,
                payload: stream_two,
            },
        ])
        .unwrap();
        let decoded = decode_family_records(&encoded).unwrap().unwrap();
        assert_eq!(decoded.len(), 3);
        assert_eq!(decoded[0].payload, entity);
        assert_eq!(decoded[1].payload, stream_one);
        assert_eq!(decoded[2].payload, stream_two);
        assert!(decoded
            .iter()
            .all(|record| encoded.as_ptr_range().contains(&record.payload.as_ptr())));
    }

    #[test]
    fn family_records_reject_noncanonical_unbounded_and_corrupt_input() {
        let duplicate_entity = [
            FamilyRecord {
                kind: FamilyRecordKind::EntityRoot,
                payload: b"one",
            },
            FamilyRecord {
                kind: FamilyRecordKind::EntityRoot,
                payload: b"two",
            },
        ];
        assert_eq!(
            encode_family_records(&duplicate_entity).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let wrong_order = [
            FamilyRecord {
                kind: FamilyRecordKind::LogicalOutbox,
                payload: b"outbox",
            },
            FamilyRecord {
                kind: FamilyRecordKind::StreamCommit,
                payload: b"stream",
            },
        ];
        assert_eq!(
            encode_family_records(&wrong_order).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized = vec![1; MAX_INLINE_BYTES];
        assert_eq!(
            encode_family_records(&[FamilyRecord {
                kind: FamilyRecordKind::CommandReceipt,
                payload: &oversized,
            }])
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidInput
        );
        let mut corrupt = encode_family_records(&[FamilyRecord {
            kind: FamilyRecordKind::CommandReceipt,
            payload: b"receipt",
        }])
        .unwrap();
        corrupt[17] = 1;
        assert_eq!(
            decode_family_records(&corrupt).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn family_records_follow_demand_beyond_former_count_quota() {
        let mut builder = FamilyTransactionBuilder::default();
        for ordinal in (0..1101_u64).rev() {
            builder.add_record(FamilyRecordKind::StreamCommit,
                ordinal.to_be_bytes().to_vec(), std::iter::empty()).unwrap();
        }
        let envelope = builder.prepare().unwrap();
        let encoded = envelope.encode().unwrap();
        let recovered = TransactionEnvelope::decode(&encoded).unwrap();
        let directory = tempfile::tempdir().unwrap();
        {
            let mut engine = crate::engine::Engine::initialize(directory.path()).unwrap();
            assert_eq!(engine.commit(&recovered.inline_payload).unwrap(), 1);
        }
        let engine = crate::engine::Engine::open(directory.path()).unwrap();
        let persisted = engine.transaction_at(1).unwrap().unwrap();
        assert_eq!(persisted.envelope.inline_payload, recovered.inline_payload);
        let records = decode_family_records(&persisted.envelope.inline_payload).unwrap().unwrap();
        assert_eq!(records.len(), 1101);
        for (ordinal, record) in records.iter().enumerate() {
            assert_eq!(record.kind, FamilyRecordKind::StreamCommit);
            assert_eq!(record.payload, (ordinal as u64).to_be_bytes());
        }
        // A corrupt count cannot reserve memory unsupported by payload bytes.
        let mut corrupt = recovered.inline_payload.clone();
        corrupt[12..14].copy_from_slice(&u16::MAX.to_le_bytes());
        assert_eq!(decode_family_records(&corrupt).unwrap_err().kind(), io::ErrorKind::InvalidData);
        // Representation overflow remains explicit before the narrowing cast.
        let excessive = vec![records[0]; usize::from(u16::MAX) + 1];
        assert_eq!(encode_family_records(&excessive).unwrap_err().kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn family_builder_canonicalizes_records_and_deduplicates_block_references() {
        let mut builder = FamilyTransactionBuilder::default();
        builder
            .add_record(
                FamilyRecordKind::LogicalOutbox,
                b"outbox".to_vec(),
                [BlockId([2; 32]), BlockId([1; 32])],
            )
            .unwrap();
        builder
            .add_record(
                FamilyRecordKind::EntityRoot,
                b"entity".to_vec(),
                [BlockId([1; 32])],
            )
            .unwrap();
        let transaction = builder.prepare().unwrap();
        assert_eq!(
            transaction.block_ids,
            vec![BlockId([1; 32]), BlockId([2; 32])]
        );
        let records = decode_family_records(&transaction.inline_payload)
            .unwrap()
            .unwrap();
        assert_eq!(records[0].kind, FamilyRecordKind::EntityRoot);
        assert_eq!(records[1].kind, FamilyRecordKind::LogicalOutbox);
    }

    #[test]
    fn family_builder_rejects_duplicate_singletons_without_mutating_itself() {
        let mut builder = FamilyTransactionBuilder::default();
        builder
            .add_record(
                FamilyRecordKind::CommandReceipt,
                b"first".to_vec(),
                [BlockId([1; 32])],
            )
            .unwrap();
        assert_eq!(
            builder
                .add_record(
                    FamilyRecordKind::CommandReceipt,
                    b"second".to_vec(),
                    [BlockId([2; 32])],
                )
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        let transaction = builder.prepare().unwrap();
        assert_eq!(transaction.block_ids, vec![BlockId([1; 32])]);
        let records = decode_family_records(&transaction.inline_payload)
            .unwrap()
            .unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].payload, b"first");
    }
}

#[cfg(test)]
mod carried_offset_tests {
    use super::*;
    #[test]
    fn borrowed_carried_offsets_include_authority_and_reject_every_truncation() {
        let first = b"first payload"; let authority = b"authority payload";
        let envelope = TransactionEnvelope { block_ids: vec![BlockId::for_payload(first), BlockId([9; 32])],
            inline_payload: b"inline".to_vec(), authority_state_update: Some(Some(BlockId::for_payload(authority))) };
        let encoded = envelope.encode_with_carried(&[Some(first), None], Some(authority)).unwrap();
        let mut visited = Vec::new();
        assert_eq!(TransactionEnvelope::visit_carried(&encoded, &mut |id, offset, payload| {
            assert_eq!(&encoded[offset..offset + payload.len()], payload);
            assert_eq!(BlockId::for_payload(payload), id);
            visited.push(payload.to_vec()); Ok(())
        }).unwrap(), envelope);
        assert_eq!(visited, vec![first.to_vec(), authority.to_vec()]);
        for length in 0..encoded.len() {
            assert!(TransactionEnvelope::visit_carried(&encoded[..length], &mut |_, _, _| Ok(())).is_err());
        }
    }
}
