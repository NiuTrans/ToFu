//! Deterministic WAL envelope for one atomic transaction and its block roots.

use std::collections::BTreeSet;
use std::io;

use crate::block::BlockId;

const MAGIC: &[u8; 8] = b"TDBTXN01";
const FAMILY_MAGIC: &[u8; 8] = b"TDBFAM01";
const FAMILY_VERSION: u32 = 1;
const ENVELOPE_VERSION: u32 = 2;
const HEADER_BYTES: usize = 8 + 4 + 4 + 4 + 1 + 32;
const FAMILY_HEADER_BYTES: usize = 8 + 4 + 2 + 2;
const FAMILY_RECORD_HEADER_BYTES: usize = 1 + 3 + 4;
pub const MAX_REFERENCED_BLOCKS: usize = 2_048;
pub const MAX_INLINE_BYTES: usize = 256 * 1024;
// A maximum-size event.append_batch may touch 500 distinct streams. Reserve
// bounded room for the entity root, receipt, outbox, and future singleton
// families without relaxing the independent 256 KiB inline byte ceiling.
pub const MAX_FAMILY_RECORDS: usize = 512;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
#[repr(u8)]
pub enum FamilyRecordKind {
    EntityRoot = 1,
    StreamCommit = 2,
    CommandReceipt = 3,
    LogicalOutbox = 4,
}

impl FamilyRecordKind {
    fn decode(value: u8) -> io::Result<Self> {
        match value {
            1 => Ok(Self::EntityRoot),
            2 => Ok(Self::StreamCommit),
            3 => Ok(Self::CommandReceipt),
            4 => Ok(Self::LogicalOutbox),
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
            if self.block_ids.len() + additional_block_ids.len() > MAX_REFERENCED_BLOCKS {
                return Err(invalid_input("transaction references too many blocks"));
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
        if self.records.len() == MAX_FAMILY_RECORDS {
            return Err(invalid_input("transaction has too many family records"));
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
    if records.is_empty() || records.len() > MAX_FAMILY_RECORDS {
        return Err(invalid_input(
            "transaction family record count is empty or unbounded",
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
    if version != FAMILY_VERSION || count == 0 || count > MAX_FAMILY_RECORDS || reserved != 0 {
        return Err(invalid_data("invalid transaction family header"));
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
        if self.block_ids.len() > MAX_REFERENCED_BLOCKS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction references too many blocks",
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
            || block_count > MAX_REFERENCED_BLOCKS
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
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn family_record_count_covers_five_hundred_streams_but_remains_bounded() {
        let payloads = (0..MAX_FAMILY_RECORDS as u64)
            .map(u64::to_be_bytes)
            .collect::<Vec<_>>();
        let records = payloads
            .iter()
            .map(|payload| FamilyRecord {
                kind: FamilyRecordKind::StreamCommit,
                payload,
            })
            .collect::<Vec<_>>();
        let encoded = encode_family_records(&records).unwrap();
        assert_eq!(
            decode_family_records(&encoded).unwrap().unwrap().len(),
            MAX_FAMILY_RECORDS
        );

        let extra_payload = (MAX_FAMILY_RECORDS as u64).to_be_bytes();
        let mut excessive = records;
        excessive.push(FamilyRecord {
            kind: FamilyRecordKind::StreamCommit,
            payload: &extra_payload,
        });
        assert_eq!(
            encode_family_records(&excessive).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
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
