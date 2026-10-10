//! Immutable transaction-history segments and their bounded checkpoint manifest.

use std::io;

use crate::block::BlockId;
use crate::engine::CommittedTransaction;
use crate::transaction::TransactionEnvelope;
use crate::wal::calculate_record_hash;

/// Calling-thread totals isolate recovery/verification work from other workers.
/// Attempts and source bytes include failed decodes; transactions count complete
/// validated segments. Fixed counters retain no transaction or payload identity.
#[derive(Clone, Copy, Debug, Default, serde::Serialize)]
pub(crate) struct HistoryDecodeWork {
    pub attempts: u64,
    pub source_bytes: u64,
    pub transactions: u64,
}

thread_local! {
    static DECODE_WORK: std::cell::Cell<HistoryDecodeWork> = const {
        std::cell::Cell::new(HistoryDecodeWork { attempts: 0, source_bytes: 0, transactions: 0 })
    };
}

pub(crate) fn decode_work() -> HistoryDecodeWork { DECODE_WORK.with(|work| work.get()) }

impl HistoryDecodeWork {
    pub(crate) fn since(self, before: Self) -> Self {
        Self { attempts: self.attempts.saturating_sub(before.attempts),
            source_bytes: self.source_bytes.saturating_sub(before.source_bytes),
            transactions: self.transactions.saturating_sub(before.transactions) }
    }
}

const SEGMENT_MAGIC: &[u8; 8] = b"TDBHIS01";
const MANIFEST_MAGIC: &[u8; 8] = b"TDBHMF01";
const SEGMENT_VERSION: u32 = 1;
const LEGACY_MANIFEST_VERSION: u32 = 1;
const MANIFEST_VERSION: u32 = 2;
const INDEXED_MANIFEST_VERSION: u32 = 3;
pub(crate) const SEGMENT_HEADER_BYTES: usize = 8 + 4 + 8 + 32 + 4;
pub(crate) const TRANSACTION_HEADER_BYTES: usize = 8 + 32 + 4;
const MANIFEST_HEADER_BYTES: usize = 8 + 4 + 8 + 32 + 4;
const MANIFEST_ENTRY_BYTES: usize = 32 + 8 + 8 + 32 + 32;
const INDEXED_MANIFEST_ENTRY_BYTES: usize = MANIFEST_ENTRY_BYTES + 1 + 32;
pub const HISTORY_SEGMENT_MAX_BYTES: usize =
    crate::generated_tofudb_ir::MAX_HISTORY_PREPARATION_BYTES;
pub const MAX_HISTORY_SEGMENTS: usize = 32_768;

pub(crate) fn transaction_history_bytes(transaction: &CommittedTransaction) -> usize {
    TRANSACTION_HEADER_BYTES + transaction.encoded_envelope.len()
}

/// Largest prefix that fits one existing-format segment; sizing touches only
/// transaction lengths, never copies or hashes the carried block payloads.
pub(crate) fn segment_prefix_len(transactions: &[CommittedTransaction]) -> usize {
    let mut bytes = SEGMENT_HEADER_BYTES;
    transactions
        .iter()
        .take_while(|transaction| {
            let entry_bytes = transaction_history_bytes(transaction);
            if entry_bytes > HISTORY_SEGMENT_MAX_BYTES - bytes {
                return false;
            }
            bytes += entry_bytes;
            true
        })
        .count()
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct HistorySegmentReference {
    pub block_id: BlockId,
    /// Version-3 manifests authenticate a block-ID/offset index over carried
    /// bytes in this exact history block. Older manifests require ordinary
    /// loose/packed materialization and decode with no history payload source.
    pub payload_index_block_id: Option<BlockId>,
    pub first_sequence: u64,
    pub last_sequence: u64,
    pub parent_hash: [u8; 32],
    pub terminal_hash: [u8; 32],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct HistoryManifest {
    pub checkpoint_sequence: u64,
    pub checkpoint_hash: [u8; 32],
    /// The bounded-level, immutable lookup directory for carried block bytes.
    pub payload_index_root_id: Option<BlockId>,
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

/// One existing-format segment encoded over bounded foreground steps. The
/// source stays in Engine's authoritative active WAL until checkpoint. This
/// buffer replaces, rather than accompanies, a pending staged history block.
pub(crate) struct HistorySegmentEncoder {
    encoded: Vec<u8>,
    transaction_count: usize,
    encoded_transactions: usize,
    first_sequence: u64,
    parent_hash: [u8; 32],
    terminal_hash: [u8; 32],
}

impl HistorySegmentEncoder {
    pub(crate) fn new(
        transactions: &[CommittedTransaction],
        parent_hash: [u8; 32],
    ) -> io::Result<Self> {
        let transaction_count = segment_prefix_len(transactions);
        if transaction_count == 0 {
            return Err(invalid_input(
                "one history transaction exceeds segment bound",
            ));
        }
        let encoded_bytes = SEGMENT_HEADER_BYTES
            + transactions[..transaction_count]
                .iter()
                .map(transaction_history_bytes)
                .sum::<usize>();
        let first_sequence = transactions[0].sequence;
        let mut encoded = Vec::with_capacity(encoded_bytes);
        encoded.extend_from_slice(SEGMENT_MAGIC);
        encoded.extend_from_slice(&SEGMENT_VERSION.to_le_bytes());
        encoded.extend_from_slice(&first_sequence.to_le_bytes());
        encoded.extend_from_slice(&parent_hash);
        encoded.extend_from_slice(&(transaction_count as u32).to_le_bytes());
        Ok(Self {
            encoded,
            transaction_count,
            encoded_transactions: 0,
            first_sequence,
            parent_hash,
            terminal_hash: parent_hash,
        })
    }

    /// Hash and copy complete records up to the target. One larger record
    /// still advances the encoder, within the unchanged 4 MiB segment bound.
    pub(crate) fn advance(
        &mut self,
        transactions: &[CommittedTransaction],
        byte_target: usize,
    ) -> io::Result<bool> {
        if byte_target == 0 || transactions.len() < self.transaction_count {
            return Err(invalid_input(
                "history preparation source or target is invalid",
            ));
        }
        let mut step_bytes = 0;
        while self.encoded_transactions < self.transaction_count {
            let transaction = &transactions[self.encoded_transactions];
            let entry_bytes = transaction_history_bytes(transaction);
            if step_bytes > 0 && entry_bytes > byte_target.saturating_sub(step_bytes) {
                break;
            }
            let expected_sequence = self
                .first_sequence
                .checked_add(self.encoded_transactions as u64)
                .ok_or_else(|| invalid_data("history sequence overflow"))?;
            if transaction.sequence != expected_sequence {
                return Err(invalid_data("active history sequence is not contiguous"));
            }
            if entry_bytes > HISTORY_SEGMENT_MAX_BYTES - self.encoded.len() {
                return Err(invalid_input("history preparation exceeds segment bound"));
            }
            let envelope = transaction.encoded_envelope.as_slice();
            let expected_hash =
                calculate_record_hash(transaction.sequence, self.terminal_hash, envelope)?;
            if transaction.record_hash != expected_hash {
                return Err(invalid_data("active history record hash mismatch"));
            }
            self.encoded
                .extend_from_slice(&transaction.sequence.to_le_bytes());
            self.encoded.extend_from_slice(&transaction.record_hash);
            self.encoded
                .extend_from_slice(&(envelope.len() as u32).to_le_bytes());
            self.encoded.extend_from_slice(envelope);
            self.terminal_hash = transaction.record_hash;
            self.encoded_transactions += 1;
            step_bytes += entry_bytes;
        }
        Ok(self.encoded_transactions == self.transaction_count)
    }

    pub(crate) fn transaction_count(&self) -> usize {
        self.transaction_count
    }

    pub(crate) fn finish(self) -> io::Result<UnstoredHistorySegment> {
        if self.encoded_transactions != self.transaction_count {
            return Err(invalid_input("history preparation is incomplete"));
        }
        Ok(UnstoredHistorySegment {
            encoded: self.encoded,
            first_sequence: self.first_sequence,
            last_sequence: self.first_sequence + (self.transaction_count as u64 - 1),
            parent_hash: self.parent_hash,
            terminal_hash: self.terminal_hash,
        })
    }
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

#[cfg(test)]
pub(crate) fn build_segments(
    transactions: &[CommittedTransaction],
    initial_parent_hash: [u8; 32],
) -> io::Result<Vec<UnstoredHistorySegment>> {
    let mut segments = Vec::new();
    visit_segments(transactions, initial_parent_hash, |segment| {
        segments.push(segment);
        Ok(())
    })?;
    Ok(segments)
}

/// Encode and consume at most one 4 MiB segment at a time. The callback must
/// durably store it before returning; publication remains the engine's job.
/// Exact sizing avoids reserving 4 MiB for every tiny checkpoint suffix.
pub(crate) fn visit_segments<F>(
    transactions: &[CommittedTransaction],
    initial_parent_hash: [u8; 32],
    mut consume: F,
) -> io::Result<()>
where
    F: FnMut(UnstoredHistorySegment) -> io::Result<()>,
{
    if transactions.is_empty() {
        return Err(invalid_input("cannot checkpoint empty active history"));
    }
    let mut start = 0;
    let mut parent_hash = initial_parent_hash;
    while start < transactions.len() {
        let remaining = &transactions[start..];
        let mut encoder = HistorySegmentEncoder::new(remaining, parent_hash)?;
        encoder.advance(remaining, usize::MAX)?;
        let count = encoder.transaction_count();
        let segment = encoder.finish()?;
        parent_hash = segment.terminal_hash;
        consume(segment)?;
        start += count;
    }
    Ok(())
}

/// Metadata plus exact logged bytes borrowed from one verified segment.
/// The common parser validates the complete segment before returning any view.
pub(crate) struct HistoryTransactionView<'a> {
    pub sequence: u64,
    pub record_hash: [u8; 32],
    pub envelope: TransactionEnvelope,
    pub encoded_envelope: &'a [u8],
}

pub(crate) fn decode_segment_views<'a>(encoded: &'a [u8], reference: &HistorySegmentReference)
    -> io::Result<Vec<HistoryTransactionView<'a>>> {
    decode_segment_with(encoded, reference, |sequence, record_hash, envelope, encoded_envelope|
        HistoryTransactionView { sequence, record_hash, envelope, encoded_envelope })
}

pub(crate) fn decode_segment(encoded: &[u8], reference: &HistorySegmentReference)
    -> io::Result<Vec<CommittedTransaction>> {
    decode_segment_with(encoded, reference, |sequence, record_hash, envelope, bytes|
        CommittedTransaction { sequence, record_hash, envelope, encoded_envelope: bytes.to_vec() })
}

fn decode_segment_with<'a, T>(
    encoded: &'a [u8],
    reference: &HistorySegmentReference,
    mut make: impl FnMut(u64, [u8; 32], TransactionEnvelope, &'a [u8]) -> T,
) -> io::Result<Vec<T>> {
    let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_HISTORY_DECODE);
    DECODE_WORK.with(|work| {
        let mut value = work.get();
        value.attempts = value.attempts.saturating_add(1);
        value.source_bytes = value.source_bytes.saturating_add(encoded.len() as u64);
        work.set(value);
    });
    crate::fsync_stats::note_history_decode_attempt(encoded.len());
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
    let mut last_sequence = None;
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
        let envelope = TransactionEnvelope::decode_skipping_carried(envelope_bytes)?;
        if calculate_record_hash(sequence, previous_hash, envelope_bytes)? != record_hash {
            return Err(invalid_data("history transaction hash chain mismatch"));
        }
        previous_hash = record_hash;
        last_sequence = Some(sequence);
        transactions.push(make(sequence, record_hash, envelope, envelope_bytes));
    }
    if offset != encoded.len()
        || last_sequence
            != Some(reference.last_sequence)
        || previous_hash != reference.terminal_hash
    {
        return Err(invalid_data("history segment terminal witness mismatch"));
    }
    DECODE_WORK.with(|work| {
        let mut value = work.get();
        value.transactions = value.transactions.saturating_add(transactions.len() as u64);
        work.set(value);
    });
    crate::fsync_stats::note_history_decoded_transactions(transactions.len());
    Ok(transactions)
}

impl HistoryManifest {
    fn validate_chain(&self, require_origin: bool) -> io::Result<()> {
        if self.payload_index_root_id.is_none()
            && self.segments.iter().any(|segment| segment.payload_index_block_id.is_some()) {
            return Err(invalid_data("indexed history has no payload routing root"));
        }
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
        let indexed = self.payload_index_root_id.is_some();
        let entry_bytes = if indexed { INDEXED_MANIFEST_ENTRY_BYTES } else { MANIFEST_ENTRY_BYTES };
        let mut encoded =
            Vec::with_capacity(MANIFEST_HEADER_BYTES + self.segments.len() * entry_bytes);
        encoded.extend_from_slice(MANIFEST_MAGIC);
        encoded.extend_from_slice(&(if indexed { INDEXED_MANIFEST_VERSION } else { MANIFEST_VERSION }).to_le_bytes());
        encoded.extend_from_slice(&self.checkpoint_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.checkpoint_hash);
        encoded.extend_from_slice(&(self.segments.len() as u32).to_le_bytes());
        if indexed { encoded.extend_from_slice(&self.payload_index_root_id.unwrap().0); }
        for segment in &self.segments {
            encoded.extend_from_slice(&segment.block_id.0);
            encoded.extend_from_slice(&segment.first_sequence.to_le_bytes());
            encoded.extend_from_slice(&segment.last_sequence.to_le_bytes());
            encoded.extend_from_slice(&segment.parent_hash);
            encoded.extend_from_slice(&segment.terminal_hash);
            if indexed {
                encoded.push(u8::from(segment.payload_index_block_id.is_some()));
                encoded.extend_from_slice(&segment.payload_index_block_id.unwrap_or(BlockId([0; 32])).0);
            }
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
        let entry_bytes = if version == INDEXED_MANIFEST_VERSION { INDEXED_MANIFEST_ENTRY_BYTES } else { MANIFEST_ENTRY_BYTES };
        let expected_len = count
            .checked_mul(entry_bytes)
            .and_then(|length| (MANIFEST_HEADER_BYTES + if version == INDEXED_MANIFEST_VERSION { 32 } else { 0 }).checked_add(length))
            .ok_or_else(|| invalid_data("history manifest length overflow"))?;
        if (version != LEGACY_MANIFEST_VERSION && version != MANIFEST_VERSION && version != INDEXED_MANIFEST_VERSION)
            || count == 0
            || count > MAX_HISTORY_SEGMENTS
            || expected_len != encoded.len()
        {
            return Err(invalid_data("invalid history manifest header"));
        }
        let mut offset = MANIFEST_HEADER_BYTES;
        let payload_index_root_id = if version == INDEXED_MANIFEST_VERSION {
            Some(BlockId(take(encoded, &mut offset, 32)?.try_into().unwrap()))
        } else { None };
        let mut segments = Vec::with_capacity(count);
        for _ in 0..count {
            let mut reference = HistorySegmentReference {
                block_id: BlockId(take(encoded, &mut offset, 32)?.try_into().unwrap()),
                payload_index_block_id: None,
                first_sequence: u64::from_le_bytes(
                    take(encoded, &mut offset, 8)?.try_into().unwrap(),
                ),
                last_sequence: u64::from_le_bytes(
                    take(encoded, &mut offset, 8)?.try_into().unwrap(),
                ),
                parent_hash: take(encoded, &mut offset, 32)?.try_into().unwrap(),
                terminal_hash: take(encoded, &mut offset, 32)?.try_into().unwrap(),
            };
            if version == INDEXED_MANIFEST_VERSION {
                let presence = take(encoded, &mut offset, 1)?[0];
                let index_id = BlockId(take(encoded, &mut offset, 32)?.try_into().unwrap());
                reference.payload_index_block_id = match presence {
                    0 if index_id == BlockId([0; 32]) => None,
                    1 => Some(index_id),
                    _ => return Err(invalid_data("invalid history payload index presence")),
                };
            }
            segments.push(reference);
        }
        let manifest = Self {
            checkpoint_sequence,
            checkpoint_hash,
            payload_index_root_id,
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
    include!("history_borrowed_tests.rs");

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
            encoded_envelope: encoded,
        }
    }

    #[test]
    fn incremental_history_encoding_bounds_each_step_and_preserves_exact_bytes() {
        let mut transactions = Vec::new();
        let mut parent = [0; 32];
        for sequence in 1..=96 {
            let mut transaction = transaction(sequence, parent);
            transaction.envelope.inline_payload = vec![sequence as u8; 64 * 1024];
            transaction.encoded_envelope = transaction.envelope.encode().unwrap();
            transaction.record_hash =
                calculate_record_hash(sequence, parent, &transaction.encoded_envelope).unwrap();
            parent = transaction.record_hash;
            transactions.push(transaction);
        }
        let expected = build_segments(&transactions, [0; 32]).unwrap();
        let mut encoder = HistorySegmentEncoder::new(&transactions, [0; 32]).unwrap();
        let capacity = encoder.encoded.capacity();
        assert!(capacity <= HISTORY_SEGMENT_MAX_BYTES);
        let mut steps = 0;
        loop {
            let before = encoder.encoded.len();
            let done = encoder
                .advance(
                    &transactions,
                    crate::generated_tofudb_ir::HISTORY_PREPARATION_TARGET_BYTES,
                )
                .unwrap();
            let added = encoder.encoded.len() - before;
            assert!(added > 0);
            assert!(added <= crate::generated_tofudb_ir::HISTORY_PREPARATION_TARGET_BYTES);
            assert_eq!(encoder.encoded.capacity(), capacity);
            steps += 1;
            if done {
                break;
            }
        }
        assert!(steps > 1 && steps < 32);
        assert_eq!(encoder.finish().unwrap().encoded, expected[0].encoded);

        // A request larger than the target progresses as one whole record.
        let mut encoder = HistorySegmentEncoder::new(&transactions, [0; 32]).unwrap();
        assert!(!encoder.advance(&transactions, 1).unwrap());
        assert_eq!(encoder.encoded_transactions, 1);
        assert_eq!(
            encoder.encoded.len(),
            SEGMENT_HEADER_BYTES + transaction_history_bytes(&transactions[0])
        );
        assert!(encoder.finish().is_err());
    }

    #[test]
    fn incremental_history_encoding_rejects_corruption_without_consuming_the_record() {
        let first = transaction(1, [0; 32]);
        let second = transaction(2, first.record_hash);
        let mut transactions = vec![first, second];
        let mut encoder = HistorySegmentEncoder::new(&transactions, [0; 32]).unwrap();
        assert!(!encoder.advance(&transactions, 1).unwrap());
        let encoded_before = encoder.encoded.clone();
        transactions[1].encoded_envelope[0] ^= 1;
        assert_eq!(
            encoder
                .advance(&transactions, usize::MAX)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(encoder.encoded, encoded_before);
        assert_eq!(encoder.encoded_transactions, 1);
        transactions[1].encoded_envelope[0] ^= 1;
        transactions[1].sequence = 3;
        assert!(encoder.advance(&transactions, usize::MAX).is_err());
        transactions[1].sequence = 2;
        assert!(encoder.advance(&transactions, usize::MAX).unwrap());
        assert_eq!(
            encoder.finish().unwrap().encoded,
            build_segments(&transactions, [0; 32]).unwrap()[0].encoded
        );

        let terminal = transaction(u64::MAX, [0; 32]);
        let mut encoder =
            HistorySegmentEncoder::new(std::slice::from_ref(&terminal), [0; 32]).unwrap();
        assert!(encoder.advance(&[terminal], usize::MAX).unwrap());
        assert_eq!(encoder.finish().unwrap().last_sequence, u64::MAX);
    }

    #[test]
    fn streaming_history_is_exact_sized_and_stops_on_sink_failure() {
        let mut transactions = Vec::new();
        let mut parent = [0; 32];
        for sequence in 1..=20 {
            let mut transaction = transaction(sequence, parent);
            transaction.envelope.inline_payload = vec![sequence as u8; 256 * 1024];
            transaction.encoded_envelope = transaction.envelope.encode().unwrap();
            transaction.record_hash =
                calculate_record_hash(sequence, parent, &transaction.encoded_envelope).unwrap();
            parent = transaction.record_hash;
            transactions.push(transaction);
        }
        let expected = build_segments(&transactions, [0; 32]).unwrap();
        assert_eq!(expected.len(), 2);
        let mut index = 0;
        visit_segments(&transactions, [0; 32], |segment| {
            assert_eq!(segment.encoded, expected[index].encoded);
            assert_eq!(segment.encoded.capacity(), segment.encoded.len());
            assert!(segment.encoded.capacity() <= HISTORY_SEGMENT_MAX_BYTES);
            index += 1;
            Ok(())
        })
        .unwrap();
        assert_eq!(index, 2);
        let mut calls = 0;
        let error = visit_segments(&transactions, [0; 32], |_| {
            calls += 1;
            Err(io::Error::new(io::ErrorKind::Interrupted, "sink fault"))
        })
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Interrupted);
        assert_eq!(calls, 1);
        let tiny = build_segments(&[transaction(1, [0; 32])], [0; 32]).unwrap();
        assert_eq!(tiny[0].encoded.capacity(), tiny[0].encoded.len());
        assert!(tiny[0].encoded.capacity() < 256);
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
            payload_index_root_id: None,
            segments: vec![HistorySegmentReference {
                block_id: BlockId([9; 32]),
                payload_index_block_id: None,
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
            payload_index_root_id: None,
            segments: vec![HistorySegmentReference {
                block_id: BlockId([8; 32]),
                payload_index_block_id: None,
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
    #[test]
    fn indexed_manifest_round_trip_and_legacy_boundaries_are_strict() {
        let first = transaction(1, [0; 32]);
        let reference = HistorySegmentReference { block_id: BlockId([1; 32]), payload_index_block_id: Some(BlockId([2; 32])),
            first_sequence: 1, last_sequence: 1, parent_hash: [0; 32], terminal_hash: first.record_hash };
        let mut manifest = HistoryManifest { checkpoint_sequence: 1, checkpoint_hash: first.record_hash,
            payload_index_root_id: Some(BlockId([3; 32])), segments: vec![reference] };
        let bytes = manifest.encode().unwrap();
        assert_eq!(u32::from_le_bytes(bytes[8..12].try_into().unwrap()), 3);
        assert_eq!(HistoryManifest::decode(&bytes).unwrap(), manifest);
        for length in 0..bytes.len() { assert!(HistoryManifest::decode(&bytes[..length]).is_err()); }
        manifest.payload_index_root_id = None;
        assert!(manifest.encode().is_err());
        manifest.segments[0].payload_index_block_id = None;
        let legacy = manifest.encode().unwrap();
        assert_eq!(u32::from_le_bytes(legacy[8..12].try_into().unwrap()), 2);
        assert_eq!(HistoryManifest::decode(&legacy).unwrap(), manifest);
    }

}
