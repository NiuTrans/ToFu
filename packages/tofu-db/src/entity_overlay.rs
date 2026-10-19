//! Deferred-application write overlay ("the WAL is the database").
//!
//! With the overlay enabled (`TOFU_DB_WRITE_OVERLAY`), a plain-writes commit
//! no longer applies its mutations to the copy-on-write entity tree on the
//! commit path. Instead the commit records one [`FamilyRecordKind::EntityOverlay`]
//! delta in the transaction envelope — a few hundred bytes instead of dozens
//! of rewritten 4 KiB pages — and installs the delta in this in-memory store.
//! Readers merge tree + overlay through one logical view; a checkpoint-time
//! merge applies the accumulated deltas as a single batch with per-key
//! original commit versions, so the tree's content and OCC versions are
//! exactly what synchronous application would have produced.
//!
//! Correctness rules:
//! * WAL deltas remain authoritative until a root publishes exact replay
//!   coverage, including a prefix within one logical transaction. Skipped
//!   obsolete positions require a newer committed version in the suffix.
//! * Each reader captures its root plus immutable overlay index roots.
//!   Writer pruning cannot change that view or its original OCC versions.
//! * Pending work and resident reader-held bytes have separate accounting.
//! * Structural operations materialize the captured logical view before
//!   extracting a persistent capsule, then retain ordinary OCC fencing.

use std::collections::BTreeMap;
use std::io;
use std::sync::{Arc, RwLock};

use crate::entity::invalid_data;

/// Wire encoding of one committed delta: a count-prefixed sorted run of
/// (key, value-or-tombstone) entries. Bounded by
/// The count field is u32; transaction staging validates representable counts.
pub(crate) fn encode_overlay_delta(writes: &BTreeMap<Vec<u8>, Option<Vec<u8>>>) -> Vec<u8> {
    let mut encoded = Vec::with_capacity(4 + writes.len() * 16);
    encoded.extend_from_slice(&(writes.len() as u32).to_le_bytes());
    for (key, value) in writes {
        encoded.extend_from_slice(&(key.len() as u32).to_le_bytes());
        encoded.extend_from_slice(key);
        match value {
            Some(value) => {
                encoded.extend_from_slice(&(value.len() as u32).to_le_bytes());
                encoded.extend_from_slice(value);
            }
            None => encoded.extend_from_slice(&u32::MAX.to_le_bytes()),
        }
    }
    encoded
}

pub(crate) fn decode_overlay_delta(
    payload: &[u8],
) -> io::Result<BTreeMap<Vec<u8>, Option<Vec<u8>>>> {
    if payload.len() < 4 {
        return Err(invalid_data("entity overlay delta header is truncated"));
    }
    let count = u32::from_le_bytes(payload[0..4].try_into().unwrap()) as usize;
    // Every row needs key-length and value-length words before any bytes.
    // Reject impossible counts from the actual payload before iterating.
    if count > (payload.len() - 4) / 8 {
        return Err(invalid_data("entity overlay delta count exceeds its encoded bytes"));
    }
    let mut offset = 4_usize;
    let mut writes = BTreeMap::new();
    for _ in 0..count {
        let key_len = read_u32(payload, &mut offset)? as usize;
        if key_len > crate::generated_tofudb_ir::MAX_ENTITY_KEY_BYTES + 82 {
            return Err(invalid_data("entity overlay delta key exceeds its bound"));
        }
        let key = take(payload, &mut offset, key_len)?.to_vec();
        let value_len = read_u32(payload, &mut offset)?;
        let value = if value_len == u32::MAX {
            None
        } else {
            let value_len = value_len as usize;
            if value_len > crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES {
                return Err(invalid_data("entity overlay delta value exceeds its bound"));
            }
            Some(take(payload, &mut offset, value_len)?.to_vec())
        };
        writes.insert(key, value);
    }
    if offset != payload.len() {
        return Err(invalid_data("entity overlay delta has trailing bytes"));
    }
    Ok(writes)
}

fn read_u32(payload: &[u8], offset: &mut usize) -> io::Result<u32> {
    let bytes = payload
        .get(*offset..*offset + 4)
        .ok_or_else(|| invalid_data("entity overlay delta is truncated"))?;
    *offset += 4;
    Ok(u32::from_le_bytes(bytes.try_into().unwrap()))
}

fn take<'a>(payload: &'a [u8], offset: &mut usize, len: usize) -> io::Result<&'a [u8]> {
    let end = offset
        .checked_add(len)
        .ok_or_else(|| invalid_data("entity overlay delta length overflow"))?;
    let slice = payload
        .get(*offset..end)
        .ok_or_else(|| invalid_data("entity overlay delta is truncated"))?;
    *offset = end;
    Ok(slice)
}

/// Durable replay coverage: the consumed prefix is materialized or dominated
/// by a newer committed suffix value for the same key. `key == None` covers
/// the complete commit. Older readers own their original tree/index pair.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct OverlayPosition {
    pub(crate) sequence: u64,
    pub(crate) key: Option<Vec<u8>>,
}

impl OverlayPosition {
    pub(crate) fn covers(&self, sequence: u64, key: &[u8]) -> bool {
        sequence < self.sequence || (sequence == self.sequence
            && self.key.as_ref().is_none_or(|last| key <= last.as_slice()))
    }
}

type Delta = BTreeMap<Vec<u8>, Option<Vec<u8>>>;

pub(crate) struct OverlayMergeChunk {
    pub(crate) entries: Vec<(Vec<u8>, Option<Vec<u8>>, u64)>,
    pub(crate) position: OverlayPosition,
    pub(crate) consumed_positions: usize,
    pub(crate) superseded_positions: usize,
}

#[path = "entity_overlay_index.rs"]
mod index;
use index::{Index, Record};

/// The two persistent roots share each payload once. Reader/replica capture
/// clones roots, never the commit backlog; pruning copies only leased paths.
#[derive(Clone, Debug, Default)]
pub(crate) struct EntityOverlay {
    by_commit: Index<true>,
    by_key: Index<false>,
    position: OverlayPosition,
    last_sequence: u64,
    bytes: usize,
    entries: usize,
    resident: Arc<std::sync::atomic::AtomicUsize>,
}

#[derive(Clone, Debug, Default)]
pub(crate) struct EntityOverlayView {
    by_key: Index<false>,
    max_sequence: u64,
}

impl EntityOverlayView {
    fn resolve(&self, key: &[u8], max_sequence: u64) -> Option<&Record> {
        let limit = max_sequence.min(self.max_sequence);
        self.by_key.floor(|record| record.key.as_slice() < key
            || (record.key.as_slice() == key && record.sequence <= limit))
            .filter(|record| record.key.as_slice() == key).map(AsRef::as_ref)
    }

    /// Version-only OCC lookup; tombstones retain their commit version.
    pub(crate) fn resolve_version(&self, key: &[u8], max_sequence: u64) -> Option<u64> {
        self.resolve(key, max_sequence).map(|record| record.sequence)
    }

    /// Pending keys cover overlay phantoms; tree witnesses cover materialized
    /// changes. A newer tombstone is a change even when the key is now absent.
    pub(crate) fn changed_in_range_since(&self, start: &[u8], end: &[u8], sequence: u64) -> bool {
        self.by_key.cursor(false, |record| record.key.as_slice() >= start)
            .take_while(|record| record.key.as_slice() < end)
            .any(|record| record.sequence > sequence && record.sequence <= self.max_sequence)
    }

    pub(crate) fn resolve_versioned(&self, key: &[u8], max_sequence: u64)
        -> Option<(u64, Option<Vec<u8>>)>
    {
        let _stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_POINT);
        self.resolve(key, max_sequence).map(|record| (record.sequence, record.value.clone()))
    }

    pub(crate) fn materialization_batch(&self) -> Vec<(Vec<u8>, Option<Vec<u8>>, u64)> {
        let mut result = Vec::new();
        let mut cursor = self.cursor(&[], None, self.max_sequence, false);
        while let Some(record) = cursor.next_record() {
            result.push((record.key.clone(), record.value.clone(), record.sequence));
        }
        result
    }

    fn cursor<'a>(&'a self, start: &'a [u8], end: Option<&'a [u8]>,
        max_sequence: u64, reverse: bool) -> OverlayCursor<'a> {
        let cursor = self.by_key.cursor(reverse, |record| {
            if reverse { end.is_none_or(|end| record.key.as_slice() < end) }
            else { record.key.as_slice() >= start }
        });
        OverlayCursor { cursor: cursor.peekable(), start, end, reverse,
            max_sequence: max_sequence.min(self.max_sequence) }
    }

    pub(crate) fn range<'a>(&'a self, start: &'a [u8], end: &'a [u8],
        max_sequence: u64, reverse: bool) -> OverlayCursor<'a> {
        self.cursor(start, Some(end), max_sequence, reverse)
    }
}

pub(crate) struct OverlayCursor<'a> {
    cursor: std::iter::Peekable<index::Cursor<'a>>,
    start: &'a [u8],
    end: Option<&'a [u8]>,
    max_sequence: u64,
    reverse: bool,
}

impl<'a> OverlayCursor<'a> {
    fn next_record(&mut self) -> Option<&'a Record> {
        loop {
            let first = self.cursor.next()?;
            if first.key.as_slice() < self.start
                || self.end.is_some_and(|end| first.key.as_slice() >= end) { return None; }
            let mut visible = (first.sequence <= self.max_sequence).then_some(first.as_ref());
            while self.cursor.peek().is_some_and(|next| next.key == first.key) {
                let next = self.cursor.next().unwrap();
                if next.sequence <= self.max_sequence && (!self.reverse || visible.is_none()) {
                    visible = Some(next.as_ref());
                }
            }
            if visible.is_some() { return visible; }
        }
    }
}

impl<'a> Iterator for OverlayCursor<'a> {
    type Item = (&'a Vec<u8>, &'a Option<Vec<u8>>);
    fn next(&mut self) -> Option<Self::Item> {
        self.next_record().map(|record| (&record.key, &record.value))
    }
}

impl EntityOverlay {
    pub(crate) fn is_empty(&self) -> bool { self.entries == 0 }
    pub(crate) fn bytes(&self) -> usize { self.bytes }
    pub(crate) fn entries(&self) -> usize { self.entries }
    pub(crate) fn resident_bytes(&self) -> usize { self.resident.load(std::sync::atomic::Ordering::Relaxed) }

    pub(crate) fn view(&self, max_sequence: u64) -> EntityOverlayView {
        let _stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_OVERLAY_VIEW);
        // Production captures the head of this writer or its frozen replica.
        // An explicit historical bound retains only the requested records.
        let by_key = if max_sequence >= self.last_sequence { self.by_key.clone() }
        else {
            let mut limited = Index::default();
            for record in self.by_commit.cursor(false, |_| true)
                .take_while(|record| record.sequence <= max_sequence) {
                limited.insert(Arc::clone(record));
            }
            limited
        };
        EntityOverlayView { by_key, max_sequence }
    }

    pub(crate) fn apply_delta(&mut self, sequence: u64, writes: Delta) {
        assert!(sequence > self.last_sequence, "non-monotonic overlay commit");
        self.last_sequence = sequence;
        for (key, value) in writes {
            if self.position.covers(sequence, &key) { continue; }
            let record = Record::new(key, value, sequence, &self.resident);
            self.bytes += record.pending_bytes();
            self.entries += 1;
            self.by_commit.insert(Arc::clone(&record));
            self.by_key.insert(record);
        }
    }

    pub(crate) fn materialized_position(&self) -> OverlayPosition { self.position.clone() }

    pub(crate) fn has_work_after(&self, position: &OverlayPosition) -> bool {
        self.by_commit.floor(|_| true).is_some_and(|record| !position.covers(record.sequence, &record.key))
    }

    pub(crate) fn note_merged(&mut self, position: OverlayPosition) {
        let covered: Vec<_> = self.by_commit.cursor(false, |_| true)
            .take_while(|record| position.covers(record.sequence, &record.key))
            .cloned().collect();
        for record in covered {
            self.bytes -= record.pending_bytes();
            self.entries -= 1;
            self.by_commit.remove(&record);
            self.by_key.remove(&record);
        }
        self.position = position;
    }

    /// Consume a bounded exact prefix in commit order, writing only versions
    /// not superseded in the committed suffix. Never skip a prefix position,
    /// copy a newer value forward, or change an original logical version.
    pub(crate) fn merge_chunk(&self, max_entries: usize, max_positions: usize)
        -> Option<OverlayMergeChunk>
    {
        if max_entries == 0 || max_positions == 0 { return None; }
        let mut batch = BTreeMap::new();
        let mut count = 0;
        let mut superseded_positions = 0;
        let mut position = self.position.clone();
        let mut pending = self.by_commit.cursor(false, |_| true).peekable();
        while let Some(record) = pending.next() {
            // A newer committed WAL position for this exact key dominates this
            // value at the current head. Its suffix remains replayable until a
            // later root covers it; old readers retain their original indexes.
            // Do not materialize obsolete values or pull newer values forward.
            let latest = self.by_key.floor(|next| next.key <= record.key)
                .expect("pending record has a key index entry");
            if latest.sequence > record.sequence {
                superseded_positions += 1;
            } else {
                if batch.len() == max_entries { break; }
                batch.insert(&record.key, record);
            }
            count += 1;
            position = OverlayPosition {
                sequence: record.sequence,
                key: pending.peek().filter(|next| next.sequence == record.sequence).map(|_| record.key.clone()),
            };
            if count == max_positions { break; }
        }
        if count == 0 { return None; }
        Some(OverlayMergeChunk {
            entries: batch.into_values().map(|record|
                (record.key.clone(), record.value.clone(), record.sequence)).collect(),
            position,
            consumed_positions: count,
            superseded_positions,
        })
    }
}

/// Soft background-drain threshold in chain positions: one third of the
/// hard commit-path scan trigger (`OVERLAY_MAX_SCAN_ENTRIES` = 12,000 in
/// entity.rs), so the maintenance drain keeps the overlay well below the
/// level reads are sized for, and the hard trigger stays a backstop.
pub(crate) const BACKGROUND_SOFT_ENTRIES: usize = 4_096;

/// Lock-free probe the maintenance worker consults to decide whether the
/// overlay needs a background drain slice. Entries/bytes reads take only
/// the overlay read lock — never the authority mutex — and the soft
/// thresholds are snapshotted from the configured hard commit-path bounds
/// at daemon startup (`BACKGROUND_SOFT_ENTRIES` for the scan bound, half
/// the byte budget), so an operator-tightened hard bound simply leaves
/// more draining to its own backstop. The WAL watermark keeps only its
/// commit-path backstop: a quieter overlay never needs a pre-rotation
/// merge, and background draining on a WAL level alone would commit
/// near-empty merges that only grow the WAL further.
pub(crate) struct EntityOverlaySoftTrigger {
    overlay: Arc<RwLock<EntityOverlay>>,
    soft_entries: usize,
    soft_bytes: usize,
}

impl EntityOverlaySoftTrigger {
    pub(crate) fn new(overlay: Arc<RwLock<EntityOverlay>>, budget_bytes: usize) -> Self {
        Self {
            overlay,
            soft_entries: BACKGROUND_SOFT_ENTRIES,
            soft_bytes: budget_bytes / 2,
        }
    }

    /// Whether entries or bytes crossed their soft thresholds.
    pub(crate) fn needs_drain(&self) -> bool {
        self.overlay
            .read()
            .map(|overlay| {
                overlay.entries() >= self.soft_entries || overlay.bytes() >= self.soft_bytes
            })
            .unwrap_or(false)
    }

    /// Whether entries and bytes are both below their soft thresholds.
    pub(crate) fn below_soft_thresholds(&self) -> bool {
        !self.needs_drain()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_batches_coalesce_repeated_keys_without_skipping_prefix_positions() {
        let mut overlay = EntityOverlay::default();
        for sequence in 1..=2 {
            overlay.apply_delta(sequence, [(b"a".to_vec(), Some(vec![sequence as u8])),
                (b"b".to_vec(), Some(vec![sequence as u8]))].into());
        }
        overlay.apply_delta(3, [(b"c".to_vec(), Some(vec![3]))].into());
        let pinned = overlay.view(1);
        let batch = overlay.merge_chunk(2, 100).unwrap();
        assert_eq!(batch.entries, vec![(b"a".to_vec(), Some(vec![2]), 2),
            (b"b".to_vec(), Some(vec![2]), 2)]);
        assert_eq!(batch.consumed_positions, 4);
        assert_eq!(batch.position, OverlayPosition { sequence: 2, key: None });
        overlay.note_merged(batch.position);
        assert_eq!(overlay.entries(), 1);
        assert_eq!(pinned.resolve_versioned(b"a", 1), Some((1, Some(vec![1]))));
        assert_eq!(overlay.view(3).resolve_versioned(b"c", 3), Some((3, Some(vec![3]))));
    }

    #[test]
    fn merge_scan_budget_cuts_exactly_inside_a_repeated_commit() {
        let mut overlay = EntityOverlay::default();
        for sequence in 1..=2 {
            overlay.apply_delta(sequence, [(b"a".to_vec(), Some(vec![sequence as u8])),
                (b"b".to_vec(), Some(vec![sequence as u8]))].into());
        }
        let batch = overlay.merge_chunk(2, 3).unwrap();
        assert_eq!(batch.consumed_positions, 3);
        assert_eq!(batch.entries, vec![(b"a".to_vec(), Some(vec![2]), 2)]);
        assert_eq!(batch.position, OverlayPosition { sequence: 2, key: Some(b"a".to_vec()) });
        overlay.note_merged(batch.position);
        assert_eq!(overlay.entries(), 1);
        assert_eq!(overlay.merge_chunk(2, 3).unwrap().entries,
            vec![(b"b".to_vec(), Some(vec![2]), 2)]);
        assert!(overlay.merge_chunk(0, 3).is_none());
        assert!(overlay.merge_chunk(2, 0).is_none());
    }

    #[test]
    fn repeated_single_key_history_cannot_exceed_the_scan_budget() {
        let mut overlay = EntityOverlay::default();
        for sequence in 1..=100 {
            overlay.apply_delta(sequence, [(b"a".to_vec(),
                if sequence % 2 == 0 { None } else { Some(vec![sequence as u8; 1024]) })].into());
        }
        let batch = overlay.merge_chunk(64, 17).unwrap();
        assert_eq!(batch.consumed_positions, 17);
        assert!(batch.entries.is_empty());
        assert_eq!(batch.superseded_positions, 17);
        overlay.note_merged(batch.position);
        assert_eq!(overlay.entries(), 83);
    }

    #[test]
    fn delta_codec_round_trips_and_rejects_garbage() {
        let mut writes = BTreeMap::new();
        writes.insert(b"a".to_vec(), Some(b"1".to_vec()));
        writes.insert(b"b".to_vec(), None);
        writes.insert(b"c".to_vec(), Some(vec![7; 128]));
        let encoded = encode_overlay_delta(&writes);
        assert_eq!(decode_overlay_delta(&encoded).unwrap(), writes);
        assert!(decode_overlay_delta(&encoded[..encoded.len() - 1]).is_err());
        assert!(decode_overlay_delta(&[]).is_err());
        let mut trailing = encoded.clone();
        trailing.push(0);
        assert!(decode_overlay_delta(&trailing).is_err());
    }

    #[test]
    fn resolve_honours_snapshot_sequences() {
        let mut overlay = EntityOverlay::default();
        let mut first = BTreeMap::new();
        first.insert(b"k".to_vec(), Some(b"v1".to_vec()));
        overlay.apply_delta(3, first);
        let mut second = BTreeMap::new();
        second.insert(b"k".to_vec(), Some(b"v2".to_vec()));
        second.insert(b"x".to_vec(), None);
        overlay.apply_delta(5, second);
        assert_eq!(overlay.view(100).resolve_versioned(b"k", 2), None);
        assert_eq!(overlay.view(100).resolve_versioned(b"k", 3), Some((3, Some(b"v1".to_vec()))));
        assert_eq!(overlay.view(100).resolve_versioned(b"k", 5), Some((5, Some(b"v2".to_vec()))));
        assert_eq!(overlay.view(100).resolve_versioned(b"k", 100), Some((5, Some(b"v2".to_vec()))));
        assert_eq!(overlay.view(100).resolve_versioned(b"x", 4), None);
        assert_eq!(overlay.view(100).resolve_versioned(b"x", 5), Some((5, None)));
        assert_eq!(overlay.view(100).resolve_versioned(b"absent", 100), None);
    }

    #[test]
    fn cursors_borrow_values_and_resolve_versions_in_both_directions() {
        let mut overlay = EntityOverlay::default();
        overlay.apply_delta(1, [(b"a".to_vec(), Some(vec![7; 1024])), (b"b".to_vec(), Some(vec![8; 1024]))].into());
        overlay.apply_delta(2, [(b"a".to_vec(), None), (b"c".to_vec(), Some(vec![9; 1024]))].into());
        let view = overlay.view(2);
        let forward: Vec<_> = view.range(b"a", b"z", 2, false).collect();
        let reverse: Vec<_> = view.range(b"a", b"z", 2, true).collect();
        assert_eq!(forward[0].1, &None);
        assert_eq!(forward.iter().rev().copied().collect::<Vec<_>>(), reverse);
        assert!(std::ptr::eq(forward[1].1, &view.resolve(b"b", 2).unwrap().value));
        assert_eq!(view.range(b"a", b"z", 1, false).count(), 2);
        overlay.note_merged(OverlayPosition { sequence: 1, key: Some(b"a".to_vec()) });
        let pending = overlay.view(2);
        assert_eq!(pending.range(b"a", b"z", 1, false).count(), 1);
        assert_eq!(view.range(b"a", b"z", 1, false).count(), 2);
    }

    #[test]
    fn prune_retains_pre_merge_views_until_pins_advance() {
        let mut overlay = EntityOverlay::default();
        for sequence in 1..=4 {
            let mut writes = BTreeMap::new();
            writes.insert(format!("k{sequence}").into_bytes(), Some(vec![sequence as u8]));
            overlay.apply_delta(sequence, writes);
        }
        let before = overlay.view(2);
        let resident = overlay.resident_bytes();
        overlay.note_merged(OverlayPosition { sequence: 3, key: None });
        assert_eq!(before.resolve_versioned(b"k1", 2), Some((1, Some(vec![1]))));
        assert_eq!(overlay.view(4).resolve_versioned(b"k1", 4), None);
        assert_eq!(overlay.entries(), 1);
        assert!(overlay.resident_bytes() > overlay.bytes());
        assert!(overlay.resident_bytes() < resident);
        drop(before);
        assert_eq!(overlay.resident_bytes(), overlay.bytes());
        assert_eq!(overlay.merge_chunk(1, 1).unwrap().entries,
            vec![(b"k4".to_vec(), Some(vec![4]), 4)]);

    }

    #[test]
    fn indexed_views_match_history_oracle_across_partial_merges_and_old_bounds() {
        type History = BTreeMap<(u64, Vec<u8>), Option<Vec<u8>>>;
        fn verify(view: &EntityOverlayView, history: &History, maximum: u64) {
            let mut expected = BTreeMap::new();
            for ((sequence, key), value) in history {
                if *sequence <= maximum { expected.insert(key.clone(), (*sequence, value.clone())); }
            }
            for key in 0..65u16 {
                let key = key.to_be_bytes();
                assert_eq!(view.resolve_versioned(&key, maximum), expected.get(key.as_slice()).cloned());
                assert_eq!(view.resolve_version(&key, maximum), expected.get(key.as_slice()).map(|row| row.0));
            }
            for (start, end) in [(0u16, 65u16), (17, 32), (32, 33), (63, 64)] {
                let start = start.to_be_bytes(); let end = end.to_be_bytes();
                let expected_rows: Vec<_> = expected.range(start.to_vec()..end.to_vec())
                    .map(|(key, (_, value))| (key.clone(), value.clone())).collect();
                for reverse in [false, true] {
                    let actual: Vec<_> = view.range(&start, &end, maximum, reverse)
                        .map(|(key, value)| (key.clone(), value.clone())).collect();
                    let mut rows = expected_rows.clone();
                    if reverse { rows.reverse(); }
                    assert_eq!(actual, rows);
                }
                let since = maximum.saturating_sub(7);
                assert_eq!(view.changed_in_range_since(&start, &end, since), history.iter()
                    .any(|((sequence, key), _)| *sequence > since && *sequence <= view.max_sequence
                        && key.as_slice() >= start.as_slice() && key.as_slice() < end.as_slice()));
            }
        }
        let mut overlay = EntityOverlay::default();
        let mut history = History::new();
        let mut pinned = Vec::new();
        let mut random = 92191u64;
        for sequence in 1..=240u64 {
            let mut writes = BTreeMap::new();
            for _ in 0..(sequence % 9) {
                random ^= random << 13; random ^= random >> 7; random ^= random << 17;
                let key = ((random % 64) as u16).to_be_bytes().to_vec();
                let value = (random % 3 != 0).then(|| sequence.to_le_bytes().to_vec());
                writes.insert(key, value);
            }
            for (key, value) in &writes { history.insert((sequence, key.clone()), value.clone()); }
            overlay.apply_delta(sequence, writes);
            let view = overlay.view(sequence);
            verify(&view, &history, sequence);
            verify(&view, &history, sequence.saturating_sub(13));
            if sequence % 17 == 0 { pinned.push((view, history.clone(), sequence)); }
            if sequence % 3 == 0 {
                if let Some(chunk) = overlay.merge_chunk(1 + (sequence as usize % 13), 17) {
                    assert!(chunk.consumed_positions <= 17);
                    let mut expected_batch = BTreeMap::new();
                    let mut consumed = 0;
                    let latest: BTreeMap<_, _> = history.keys().map(|(sequence, key)| (key.clone(), *sequence)).collect();
                    history.retain(|(sequence, key), value| {
                        if !chunk.position.covers(*sequence, key) { return true; }
                        if latest.get(key) == Some(sequence) {
                            expected_batch.insert(key.clone(), (value.clone(), *sequence));
                        }
                        consumed += 1; false
                    });
                    assert_eq!(chunk.consumed_positions, consumed);
                    assert_eq!(chunk.entries, expected_batch.into_iter()
                        .map(|(key, (value, sequence))| (key, value, sequence)).collect::<Vec<_>>());
                    overlay.note_merged(chunk.position);
                }
                verify(&overlay.view(sequence), &history, sequence);
                for (view, history, maximum) in &pinned { verify(view, history, *maximum); }
            }
        }
        overlay.note_merged(OverlayPosition { sequence: 240, key: None });
        assert_eq!(overlay.bytes(), 0);
        assert!(overlay.resident_bytes() > 0);
        drop(pinned);
        assert_eq!(overlay.resident_bytes(), 0);
    }
}
