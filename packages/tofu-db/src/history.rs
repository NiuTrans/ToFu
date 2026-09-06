//! Immutable transaction-history segments and their bounded checkpoint manifest.

use std::io;

use crate::block::BlockId;
use crate::engine::CommittedTransaction;
use crate::transaction::TransactionEnvelope;
use crate::wal::calculate_record_hash;

const SEGMENT_MAGIC: &[u8; 8] = b"TDBHIS01";
const MANIFEST_MAGIC: &[u8; 8] = b"TDBHMF01";
const SEGMENT_VERSION: u32 = 1;
const LEGACY_MANIFEST_VERSION: u32 = 1;
const MANIFEST_VERSION: u32 = 2;
const SEGMENT_HEADER_BYTES: usize = 8 + 4 + 8 + 32 + 4;
const TRANSACTION_HEADER_BYTES: usize = 8 + 32 + 4;
const MANIFEST_HEADER_BYTES: usize = 8 + 4 + 8 + 32 + 4;
const MANIFEST_ENTRY_BYTES: usize = 32 + 8 + 8 + 32 + 32;
pub const HISTORY_SEGMENT_MAX_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_HISTORY_SEGMENTS: usize = 32_768;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct HistorySegmentReference {
    pub block_id: BlockId,
    pub first_sequence: u64,
    pub last_sequence: u64,
    pub parent_hash: [u8; 32],
    pub terminal_hash: [u8; 32],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct HistoryManifest {
    pub checkpoint_sequence: u64,
    pub checkpoint_hash: [u8; 32],
    pub segments: Vec<HistorySegmentReference>,
}

#[derive(Debug)]
pub(crate) struct UnstoredHistorySegment {
    pub encoded: Vec<u8>,
    pub first_sequence: u64,
    pub last_sequence: u64,
    pub parent_hash: [u8; 32],
    pub terminal_hash: [u8; 32],
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn take<'a>(bytes: &'a [u8], offset: &mut usize, length: usize) -> io::Result<&'a [u8]> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| invalid_data("history offset overflow"))?;
    let value = bytes
        .get(*offset..end)
        .ok_or_else(|| invalid_data("truncated history data"))?;
    *offset = end;
    Ok(value)
}

pub(crate) fn build_segments(
    transactions: &[CommittedTransaction],
    initial_parent_hash: [u8; 32],
) -> io::Result<Vec<UnstoredHistorySegment>> {
    if transactions.is_empty() {
        return Err(invalid_input("cannot checkpoint empty active history"));
    }
    let mut segments = Vec::new();
    let mut start = 0;
    let mut parent_hash = initial_parent_hash;
    while start < transactions.len() {
        let segment_parent = parent_hash;
        let mut encoded = Vec::with_capacity(HISTORY_SEGMENT_MAX_BYTES);
        encoded.extend_from_slice(SEGMENT_MAGIC);
        encoded.extend_from_slice(&SEGMENT_VERSION.to_le_bytes());
        encoded.extend_from_slice(&transactions[start].sequence.to_le_bytes());
        encoded.extend_from_slice(&segment_parent);
        encoded.extend_from_slice(&0_u32.to_le_bytes());
        let mut end = start;
        while end < transactions.len() {
            let transaction = &transactions[end];
            let envelope = transaction.envelope.encode()?;
            let entry_bytes = TRANSACTION_HEADER_BYTES
                .checked_add(envelope.len())
                .ok_or_else(|| invalid_input("history transaction length overflow"))?;
            if encoded.len() + entry_bytes > HISTORY_SEGMENT_MAX_BYTES {
                break;
            }
            let expected_sequence = transactions[start]
                .sequence
                .checked_add((end - start) as u64)
                .ok_or_else(|| invalid_data("history sequence overflow"))?;
            if transaction.sequence != expected_sequence {
                return Err(invalid_data("active history sequence is not contiguous"));
            }
            let expected_hash =
                calculate_record_hash(transaction.sequence, parent_hash, &envelope)?;
            if transaction.record_hash != expected_hash {
                return Err(invalid_data("active history record hash mismatch"));
            }
            encoded.extend_from_slice(&transaction.sequence.to_le_bytes());
            encoded.extend_from_slice(&transaction.record_hash);
            encoded.extend_from_slice(&(envelope.len() as u32).to_le_bytes());
            encoded.extend_from_slice(&envelope);
            parent_hash = transaction.record_hash;
            end += 1;
        }
        if end == start {
            return Err(invalid_input(
                "one history transaction exceeds segment bound",
            ));
        }
        let count = u32::try_from(end - start)
            .map_err(|_| invalid_input("history segment transaction count overflow"))?;
        encoded[52..56].copy_from_slice(&count.to_le_bytes());
        segments.push(UnstoredHistorySegment {
            encoded,
            first_sequence: transactions[start].sequence,
            last_sequence: transactions[end - 1].sequence,
            parent_hash: segment_parent,
            terminal_hash: transactions[end - 1].record_hash,
        });
        start = end;
    }
    Ok(segments)
}

pub(crate) fn decode_segment(
    encoded: &[u8],
    reference: &HistorySegmentReference,
) -> io::Result<Vec<CommittedTransaction>> {
    if encoded.len() < SEGMENT_HEADER_BYTES
        || encoded.len() > HISTORY_SEGMENT_MAX_BYTES
        || &encoded[..8] != SEGMENT_MAGIC
    {
        return Err(invalid_data("history segment magic or size mismatch"));
    }
    let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
    let first_sequence = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
    let parent_hash: [u8; 32] = encoded[20..52].try_into().unwrap();
    let count = u32::from_le_bytes(encoded[52..56].try_into().unwrap()) as usize;
    if version != SEGMENT_VERSION
        || count == 0
        || count > (encoded.len() - SEGMENT_HEADER_BYTES) / TRANSACTION_HEADER_BYTES
        || first_sequence != reference.first_sequence
        || parent_hash != reference.parent_hash
    {
        return Err(invalid_data("history segment header witness mismatch"));
    }
    let mut offset = SEGMENT_HEADER_BYTES;
    let mut transactions = Vec::with_capacity(count);
    let mut previous_hash = parent_hash;
    for index in 0..count {
        let sequence = u64::from_le_bytes(take(encoded, &mut offset, 8)?.try_into().unwrap());
        let record_hash: [u8; 32] = take(encoded, &mut offset, 32)?.try_into().unwrap();
        let envelope_len =
            u32::from_le_bytes(take(encoded, &mut offset, 4)?.try_into().unwrap()) as usize;
        let expected_sequence = first_sequence
            .checked_add(index as u64)
            .ok_or_else(|| invalid_data("history sequence overflow"))?;
        if sequence != expected_sequence {
            return Err(invalid_data(
                "history transaction sequence is not contiguous",
            ));
        }
        let envelope_bytes = take(encoded, &mut offset, envelope_len)?;
        let envelope = TransactionEnvelope::decode(envelope_bytes)?;
        if calculate_record_hash(sequence, previous_hash, envelope_bytes)? != record_hash {
            return Err(invalid_data("history transaction hash chain mismatch"));
        }
        previous_hash = record_hash;
        transactions.push(CommittedTransaction {
            sequence,
            record_hash,
            envelope,
        });
    }
    if offset != encoded.len()
        || transactions.last().map(|transaction| transaction.sequence)
            != Some(reference.last_sequence)
        || previous_hash != reference.terminal_hash
    {
        return Err(invalid_data("history segment terminal witness mismatch"));
    }
    Ok(transactions)
}

impl HistoryManifest {
    fn validate_chain(&self, require_origin: bool) -> io::Result<()> {
        if self.checkpoint_sequence == 0
            || self.segments.is_empty()
            || self.segments.len() > MAX_HISTORY_SEGMENTS
        {
            return Err(invalid_input("invalid history manifest bounds"));
        }
        let first = &self.segments[0];
        if require_origin && (first.first_sequence != 1 || first.parent_hash != [0; 32]) {
            return Err(invalid_data(
                "legacy history manifest does not begin at origin",
            ));
        }
        let mut expected_sequence = first.first_sequence;
        let mut expected_parent = first.parent_hash;
        for segment in &self.segments {
            if segment.first_sequence != expected_sequence
                || segment.last_sequence < segment.first_sequence
                || segment.parent_hash != expected_parent
            {
                return Err(invalid_data("history manifest segment chain mismatch"));
            }
            expected_sequence = segment
                .last_sequence
                .checked_add(1)
                .ok_or_else(|| invalid_data("history manifest sequence overflow"))?;
            expected_parent = segment.terminal_hash;
        }
        if expected_sequence - 1 != self.checkpoint_sequence
            || expected_parent != self.checkpoint_hash
        {
            return Err(invalid_data("history manifest checkpoint witness mismatch"));
        }
        Ok(())
    }

    pub fn encode(&self) -> io::Result<Vec<u8>> {
        self.validate_chain(false)?;
        let mut encoded =
            Vec::with_capacity(MANIFEST_HEADER_BYTES + self.segments.len() * MANIFEST_ENTRY_BYTES);
        encoded.extend_from_slice(MANIFEST_MAGIC);
        encoded.extend_from_slice(&MANIFEST_VERSION.to_le_bytes());
        encoded.extend_from_slice(&self.checkpoint_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.checkpoint_hash);
        encoded.extend_from_slice(&(self.segments.len() as u32).to_le_bytes());
        for segment in &self.segments {
            encoded.extend_from_slice(&segment.block_id.0);
            encoded.extend_from_slice(&segment.first_sequence.to_le_bytes());
            encoded.extend_from_slice(&segment.last_sequence.to_le_bytes());
            encoded.extend_from_slice(&segment.parent_hash);
            encoded.extend_from_slice(&segment.terminal_hash);
        }
        if encoded.len() > HISTORY_SEGMENT_MAX_BYTES {
            return Err(invalid_input("history manifest exceeds block bound"));
        }
        Ok(encoded)
    }

    pub fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < MANIFEST_HEADER_BYTES
            || encoded.len() > HISTORY_SEGMENT_MAX_BYTES
            || &encoded[..8] != MANIFEST_MAGIC
        {
            return Err(invalid_data("history manifest magic or size mismatch"));
        }
        let version = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let checkpoint_sequence = u64::from_le_bytes(encoded[12..20].try_into().unwrap());
        let checkpoint_hash = encoded[20..52].try_into().unwrap();
        let count = u32::from_le_bytes(encoded[52..56].try_into().unwrap()) as usize;
        let expected_len = count
            .checked_mul(MANIFEST_ENTRY_BYTES)
            .and_then(|length| MANIFEST_HEADER_BYTES.checked_add(length))
            .ok_or_else(|| invalid_data("history manifest length overflow"))?;
        if (version != LEGACY_MANIFEST_VERSION && version != MANIFEST_VERSION)
            || count == 0
            || count > MAX_HISTORY_SEGMENTS
            || expected_len != encoded.len()
        {
            return Err(invalid_data("invalid history manifest header"));
        }
        let mut offset = MANIFEST_HEADER_BYTES;
        let mut segments = Vec::with_capacity(count);
        for _ in 0..count {
            segments.push(HistorySegmentReference {
                block_id: BlockId(take(encoded, &mut offset, 32)?.try_into().unwrap()),
                first_sequence: u64::from_le_bytes(
                    take(encoded, &mut offset, 8)?.try_into().unwrap(),
                ),
                last_sequence: u64::from_le_bytes(
                    take(encoded, &mut offset, 8)?.try_into().unwrap(),
                ),
                parent_hash: take(encoded, &mut offset, 32)?.try_into().unwrap(),
                terminal_hash: take(encoded, &mut offset, 32)?.try_into().unwrap(),
            });
        }
        let manifest = Self {
            checkpoint_sequence,
            checkpoint_hash,
            segments,
        };
        manifest
            .validate_chain(version == LEGACY_MANIFEST_VERSION)
            .map_err(|_| invalid_data("invalid history manifest chain"))?;
        Ok(manifest)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::CommittedTransaction;

    fn transaction(sequence: u64, parent_hash: [u8; 32]) -> CommittedTransaction {
        let envelope = TransactionEnvelope {
            block_ids: Vec::new(),
            inline_payload: vec![sequence as u8],
            authority_state_update: None,
        };
        let encoded = envelope.encode().unwrap();
        CommittedTransaction {
            sequence,
            record_hash: calculate_record_hash(sequence, parent_hash, &encoded).unwrap(),
            envelope,
        }
    }

    #[test]
    fn legacy_manifest_decodes_but_cannot_claim_a_suffix_origin() {
        let first = transaction(1, [0; 32]);
        let segment = build_segments(std::slice::from_ref(&first), [0; 32])
            .unwrap()
            .remove(0);
        let manifest = HistoryManifest {
            checkpoint_sequence: 1,
            checkpoint_hash: first.record_hash,
            segments: vec![HistorySegmentReference {
                block_id: BlockId([9; 32]),
                first_sequence: segment.first_sequence,
                last_sequence: segment.last_sequence,
                parent_hash: segment.parent_hash,
                terminal_hash: segment.terminal_hash,
            }],
        };
        let mut legacy = manifest.encode().unwrap();
        legacy[8..12].copy_from_slice(&LEGACY_MANIFEST_VERSION.to_le_bytes());
        assert_eq!(HistoryManifest::decode(&legacy).unwrap(), manifest);

        let second = transaction(2, first.record_hash);
        let suffix = build_segments(std::slice::from_ref(&second), first.record_hash)
            .unwrap()
            .remove(0);
        let suffix_manifest = HistoryManifest {
            checkpoint_sequence: 2,
            checkpoint_hash: second.record_hash,
            segments: vec![HistorySegmentReference {
                block_id: BlockId([8; 32]),
                first_sequence: suffix.first_sequence,
                last_sequence: suffix.last_sequence,
                parent_hash: suffix.parent_hash,
                terminal_hash: suffix.terminal_hash,
            }],
        };
        let mut invalid_legacy_suffix = suffix_manifest.encode().unwrap();
        invalid_legacy_suffix[8..12].copy_from_slice(&LEGACY_MANIFEST_VERSION.to_le_bytes());
        assert!(HistoryManifest::decode(&invalid_legacy_suffix).is_err());
    }
}
