//! Canonical bounded catalog for immutable payload segments.
//!
//! CONTROL owns publication of the content-addressed manifest block. This
//! module only validates its canonical contents and bounds point-read fanout.

use std::collections::{BTreeMap, BTreeSet};
use std::io;

use uuid::Uuid;

use crate::block::{BlockId, MAX_BLOCK_BYTES};
use crate::payload_segment::{
    PayloadSegmentMetadata, MAX_SEGMENT_BLOCKS, MAX_SEGMENT_FILE_BYTES, MAX_SEGMENT_PAYLOAD_BYTES,
};

const MAGIC: &[u8; 8] = b"TDBPMF01";
const VERSION: u32 = 1;
const HEADER_BYTES: usize = 8 + 4 + 4;
const ENTRY_BYTES: usize = 16 + 1 + 3 + 4 + 8 + 8 + 32 + 32;
pub const MAX_PAYLOAD_SEGMENTS: usize = 4_096;
pub const MAX_PAYLOAD_SEGMENTS_PER_SHARD: usize = 16;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PayloadSegmentReference {
    pub segment_id: Uuid,
    pub shard: u8,
    pub block_count: u32,
    pub payload_bytes: u64,
    pub file_bytes: u64,
    pub first_block_id: BlockId,
    pub last_block_id: BlockId,
}

impl PayloadSegmentReference {
    pub fn new(
        metadata: PayloadSegmentMetadata,
        first_block_id: BlockId,
        last_block_id: BlockId,
    ) -> io::Result<Self> {
        let reference = Self {
            segment_id: metadata.segment_id,
            shard: first_block_id.0[0],
            block_count: metadata.block_count,
            payload_bytes: metadata.payload_bytes,
            file_bytes: metadata.file_bytes,
            first_block_id,
            last_block_id,
        };
        reference.validate()?;
        Ok(reference)
    }

    fn validate(&self) -> io::Result<()> {
        if self.block_count == 0
            || self.block_count as usize > MAX_SEGMENT_BLOCKS
            || self.payload_bytes > MAX_SEGMENT_PAYLOAD_BYTES
            || self.file_bytes < self.payload_bytes
            || self.file_bytes > MAX_SEGMENT_FILE_BYTES
            || self.first_block_id > self.last_block_id
            || self.first_block_id.0[0] != self.shard
            || self.last_block_id.0[0] != self.shard
        {
            return Err(invalid_data("invalid payload segment reference"));
        }
        Ok(())
    }

    pub fn may_contain(&self, block_id: BlockId) -> bool {
        block_id.0[0] == self.shard
            && self.first_block_id <= block_id
            && block_id <= self.last_block_id
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct PayloadManifest {
    pub segments: Vec<PayloadSegmentReference>,
}

impl PayloadManifest {
    pub fn new(mut segments: Vec<PayloadSegmentReference>) -> io::Result<Self> {
        segments.sort_by_key(canonical_key);
        let manifest = Self { segments };
        manifest.validate()?;
        Ok(manifest)
    }

    pub fn candidate_segments(
        &self,
        block_id: BlockId,
    ) -> impl DoubleEndedIterator<Item = &PayloadSegmentReference> {
        self.segments
            .iter()
            .filter(move |reference| reference.may_contain(block_id))
    }

    pub fn encode(&self) -> io::Result<Vec<u8>> {
        self.validate()?;
        let mut encoded = Vec::with_capacity(HEADER_BYTES + self.segments.len() * ENTRY_BYTES);
        encoded.extend_from_slice(MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&(self.segments.len() as u32).to_le_bytes());
        for reference in &self.segments {
            encoded.extend_from_slice(reference.segment_id.as_bytes());
            encoded.push(reference.shard);
            encoded.extend_from_slice(&[0; 3]);
            encoded.extend_from_slice(&reference.block_count.to_le_bytes());
            encoded.extend_from_slice(&reference.payload_bytes.to_le_bytes());
            encoded.extend_from_slice(&reference.file_bytes.to_le_bytes());
            encoded.extend_from_slice(&reference.first_block_id.0);
            encoded.extend_from_slice(&reference.last_block_id.0);
        }
        if encoded.len() > MAX_BLOCK_BYTES {
            return Err(invalid_data("payload manifest exceeds block bound"));
        }
        Ok(encoded)
    }

    pub fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < HEADER_BYTES
            || encoded.len() > MAX_BLOCK_BYTES
            || &encoded[..8] != MAGIC
            || u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != VERSION
        {
            return Err(invalid_data(
                "payload manifest magic, version, or size mismatch",
            ));
        }
        let count = u32::from_le_bytes(encoded[12..16].try_into().unwrap()) as usize;
        let expected_bytes = count
            .checked_mul(ENTRY_BYTES)
            .and_then(|bytes| HEADER_BYTES.checked_add(bytes))
            .ok_or_else(|| invalid_data("payload manifest length overflow"))?;
        if count > MAX_PAYLOAD_SEGMENTS || expected_bytes != encoded.len() {
            return Err(invalid_data("payload manifest count or length mismatch"));
        }
        let mut segments = Vec::with_capacity(count);
        let mut offset = HEADER_BYTES;
        for _ in 0..count {
            let entry = &encoded[offset..offset + ENTRY_BYTES];
            if entry[17..20] != [0; 3] {
                return Err(invalid_data("payload manifest reserved bytes are nonzero"));
            }
            segments.push(PayloadSegmentReference {
                segment_id: Uuid::from_bytes(entry[..16].try_into().unwrap()),
                shard: entry[16],
                block_count: u32::from_le_bytes(entry[20..24].try_into().unwrap()),
                payload_bytes: u64::from_le_bytes(entry[24..32].try_into().unwrap()),
                file_bytes: u64::from_le_bytes(entry[32..40].try_into().unwrap()),
                first_block_id: BlockId(entry[40..72].try_into().unwrap()),
                last_block_id: BlockId(entry[72..104].try_into().unwrap()),
            });
            offset += ENTRY_BYTES;
        }
        let manifest = Self { segments };
        manifest.validate()?;
        Ok(manifest)
    }

    fn validate(&self) -> io::Result<()> {
        if self.segments.len() > MAX_PAYLOAD_SEGMENTS {
            return Err(invalid_data("payload manifest exceeds segment bound"));
        }
        let mut identifiers = BTreeSet::new();
        let mut shard_counts = BTreeMap::<u8, usize>::new();
        let mut previous_key = None;
        for reference in &self.segments {
            reference.validate()?;
            let key = canonical_key(reference);
            if previous_key.is_some_and(|previous| previous >= key) {
                return Err(invalid_data("payload manifest entries are not canonical"));
            }
            previous_key = Some(key);
            if !identifiers.insert(reference.segment_id) {
                return Err(invalid_data("payload manifest segment ID is duplicated"));
            }
            let count = shard_counts.entry(reference.shard).or_default();
            *count += 1;
            if *count > MAX_PAYLOAD_SEGMENTS_PER_SHARD {
                return Err(invalid_data("payload manifest shard fanout exceeds 16"));
            }
        }
        Ok(())
    }
}

fn canonical_key(reference: &PayloadSegmentReference) -> (u8, BlockId, BlockId, Uuid) {
    (
        reference.shard,
        reference.first_block_id,
        reference.last_block_id,
        reference.segment_id,
    )
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reference(seed: u8, first_tail: u8, last_tail: u8) -> PayloadSegmentReference {
        let mut first = [first_tail; 32];
        let mut last = [last_tail; 32];
        first[0] = seed;
        last[0] = seed;
        PayloadSegmentReference {
            segment_id: Uuid::from_bytes([first_tail; 16]),
            shard: seed,
            block_count: 2,
            payload_bytes: 20,
            file_bytes: 300,
            first_block_id: BlockId(first),
            last_block_id: BlockId(last),
        }
    }

    #[test]
    fn manifest_round_trip_is_canonical_and_bounds_point_fanout() {
        let second = reference(8, 20, 30);
        let first = reference(7, 10, 40);
        let manifest = PayloadManifest::new(vec![second, first]).unwrap();
        assert_eq!(
            PayloadManifest::decode(&manifest.encode().unwrap()).unwrap(),
            manifest
        );
        let mut target = [25; 32];
        target[0] = 8;
        assert_eq!(
            manifest
                .candidate_segments(BlockId(target))
                .map(|entry| entry.segment_id)
                .collect::<Vec<_>>(),
            vec![second.segment_id]
        );
    }

    #[test]
    fn malformed_bounds_order_duplicates_and_reserved_bytes_fail_closed() {
        let mut too_many = Vec::new();
        for value in 0..=MAX_PAYLOAD_SEGMENTS_PER_SHARD {
            too_many.push(reference(1, value as u8, value as u8));
        }
        assert!(PayloadManifest::new(too_many).is_err());

        let duplicate = reference(2, 3, 4);
        assert!(PayloadManifest {
            segments: vec![duplicate, duplicate]
        }
        .encode()
        .is_err());

        let manifest = PayloadManifest::new(vec![reference(3, 4, 5)]).unwrap();
        let mut encoded = manifest.encode().unwrap();
        encoded[HEADER_BYTES + 17] = 1;
        assert_eq!(
            PayloadManifest::decode(&encoded).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }
}
