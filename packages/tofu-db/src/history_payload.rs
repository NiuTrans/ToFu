//! Durable block locations inside immutable, authenticated history segments.
//!
//! SegmentIndex witnesses offsets against the canonical transaction parser.
//! RoutingRoot keeps sorted immutable runs, with at most one candidate page
//! per level. Engine owns publication through HistoryManifest/CONTROL;
//! BlockStore verifies every returned payload's content hash. Index blocks
//! use the ordinary block cache and never require a full-history startup scan.

use std::collections::BTreeMap;
use std::io;
use std::sync::{Arc, Mutex, Weak};

use crate::block::{BlockId, BlockStore, MAX_BLOCK_BYTES};
use crate::history::{
    decode_segment_views, HistorySegmentReference, SEGMENT_HEADER_BYTES, TRANSACTION_HEADER_BYTES,
};
use crate::transaction::TransactionEnvelope;

const INDEX_MAGIC: &[u8; 8] = b"TDBHIP01";
const PAGE_MAGIC: &[u8; 8] = b"TDBHLP01";
const ROOT_MAGIC: &[u8; 8] = b"TDBHLR01";
const VERSION: u32 = 1;
const INDEX_HEADER_BYTES: usize = 8 + 4 + 32 + 4 + 4;
const INDEX_ENTRY_BYTES: usize = 32 + 4 + 4;
const PAGE_HEADER_BYTES: usize = 8 + 4 + 4;
const LOCATION_BYTES: usize = 32 + 32 + 4 + 4 + 4;
const PAGE_REFERENCE_BYTES: usize = 32 + 32 + 32 + 4;
const MAX_PAGE_ENTRIES: usize = (MAX_BLOCK_BYTES - PAGE_HEADER_BYTES) / LOCATION_BYTES;
const BASE_LEVEL_ENTRIES: u64 = 4096;
const MAX_LEVELS: usize = 64;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct PayloadLocation {
    pub source: BlockId,
    pub source_bytes: u32,
    pub offset: u32,
    pub length: u32,
}
impl PayloadLocation {
    fn validate(self) -> io::Result<()> {
        if self.source_bytes as usize > MAX_BLOCK_BYTES
            || self
                .offset
                .checked_add(self.length)
                .is_none_or(|end| end > self.source_bytes)
        {
            return Err(invalid("history payload location exceeds its source"));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub(crate) struct SegmentIndex {
    pub source: BlockId,
    pub source_bytes: u32,
    pub entries: Vec<(BlockId, u32, u32)>,
}
impl SegmentIndex {
    pub(crate) fn build(encoded: &[u8], reference: &HistorySegmentReference) -> io::Result<Self> {
        if BlockId::for_payload(encoded) != reference.block_id {
            return Err(invalid("history payload index source hash mismatch"));
        }
        let transactions = decode_segment_views(encoded, reference)?;
        let mut entries = BTreeMap::new();
        let mut envelope_offset = SEGMENT_HEADER_BYTES;
        for transaction in transactions {
            envelope_offset += TRANSACTION_HEADER_BYTES;
            TransactionEnvelope::visit_carried(
                &transaction.encoded_envelope,
                &mut |id, offset, payload| {
                    if BlockId::for_payload(payload) != id {
                        return Err(invalid(
                            "history carried payload does not match its block ID",
                        ));
                    }
                    let absolute = envelope_offset
                        .checked_add(offset)
                        .ok_or_else(|| invalid("history payload offset overflow"))?;
                    entries.insert(id, (absolute as u32, payload.len() as u32));
                    Ok(())
                },
            )?;
            envelope_offset += transaction.encoded_envelope.len();
        }
        if envelope_offset != encoded.len() {
            return Err(invalid("history payload offsets do not reconcile"));
        }
        Ok(Self {
            source: reference.block_id,
            source_bytes: encoded.len() as u32,
            entries: entries
                .into_iter()
                .map(|(id, (offset, length))| (id, offset, length))
                .collect(),
        })
    }

    pub(crate) fn encode(&self) -> io::Result<Vec<u8>> {
        let mut out =
            Vec::with_capacity(INDEX_HEADER_BYTES + self.entries.len() * INDEX_ENTRY_BYTES);
        out.extend_from_slice(INDEX_MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&self.source.0);
        out.extend_from_slice(&self.source_bytes.to_le_bytes());
        out.extend_from_slice(&(self.entries.len() as u32).to_le_bytes());
        let mut previous = None;
        for &(id, offset, length) in &self.entries {
            PayloadLocation {
                source: self.source,
                source_bytes: self.source_bytes,
                offset,
                length,
            }
            .validate()?;
            if previous.is_some_and(|previous| previous >= id) {
                return Err(invalid("history payload index is not sorted"));
            }
            out.extend_from_slice(&id.0);
            out.extend_from_slice(&offset.to_le_bytes());
            out.extend_from_slice(&length.to_le_bytes());
            previous = Some(id);
        }
        if out.len() > MAX_BLOCK_BYTES {
            return Err(invalid("history payload index exceeds block bound"));
        }
        Ok(out)
    }

    pub(crate) fn decode(bytes: &[u8], source: BlockId) -> io::Result<Self> {
        if bytes.len() < INDEX_HEADER_BYTES
            || bytes.len() > MAX_BLOCK_BYTES
            || &bytes[..8] != INDEX_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
            || BlockId(bytes[12..44].try_into().unwrap()) != source
        {
            return Err(invalid("history payload index header mismatch"));
        }
        let source_bytes = u32::from_le_bytes(bytes[44..48].try_into().unwrap());
        let count = u32::from_le_bytes(bytes[48..52].try_into().unwrap()) as usize;
        if count
            .checked_mul(INDEX_ENTRY_BYTES)
            .and_then(|n| n.checked_add(INDEX_HEADER_BYTES))
            != Some(bytes.len())
        {
            return Err(invalid("history payload index length mismatch"));
        }
        let mut entries = Vec::with_capacity(count);
        for entry in bytes[INDEX_HEADER_BYTES..].chunks_exact(INDEX_ENTRY_BYTES) {
            entries.push((
                BlockId(entry[..32].try_into().unwrap()),
                u32::from_le_bytes(entry[32..36].try_into().unwrap()),
                u32::from_le_bytes(entry[36..40].try_into().unwrap()),
            ));
        }
        let index = Self {
            source,
            source_bytes,
            entries,
        };
        if index.encode()? != bytes {
            return Err(invalid("history payload index is not canonical"));
        }
        Ok(index)
    }

    pub(crate) fn locations(&self) -> impl Iterator<Item = (BlockId, PayloadLocation)> + '_ {
        self.entries.iter().map(|&(id, offset, length)| {
            (
                id,
                PayloadLocation {
                    source: self.source,
                    source_bytes: self.source_bytes,
                    offset,
                    length,
                },
            )
        })
    }
}

pub(crate) fn persist_segment(
    blocks: &BlockStore,
    encoded: &[u8],
    mut reference: HistorySegmentReference,
) -> io::Result<(HistorySegmentReference, SegmentIndex)> {
    let index = SegmentIndex::build(encoded, &reference)?;
    blocks.put_loose(encoded)?;
    reference.payload_index_block_id = if index.entries.is_empty() {
        None
    } else {
        Some(blocks.put_loose(&index.encode()?)?)
    };
    Ok((reference, index))
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PageReference {
    id: BlockId,
    first: BlockId,
    last: BlockId,
    entries: u32,
}
#[derive(Clone, Debug, Eq, PartialEq)]
struct Run {
    entries: u64,
    pages: Vec<PageReference>,
}
#[derive(Clone, Debug, Default)]
pub(crate) struct RoutingRoot {
    levels: Vec<Option<Run>>,
    // Weak witnesses are valid only for the exact verified byte allocation
    // still retained by BlockStore's bounded cache. New roots start fresh.
    verified_pages: Arc<Mutex<BTreeMap<BlockId, Weak<Vec<u8>>>>>,
}
impl PartialEq for RoutingRoot {
    fn eq(&self, other: &Self) -> bool {
        self.levels == other.levels
    }
}
impl Eq for RoutingRoot {}

pub(crate) struct Catalog {
    pub root: RoutingRoot,
    pub sources: std::collections::BTreeSet<BlockId>,
}
impl Catalog {
    pub(crate) fn new(manifest: &crate::history::HistoryManifest, root: RoutingRoot) -> Self {
        Self {
            root,
            sources: manifest
                .segments
                .iter()
                .filter(|reference| reference.payload_index_block_id.is_some())
                .map(|reference| reference.block_id)
                .collect(),
        }
    }
}

fn page_entry(bytes: &[u8], position: usize) -> (BlockId, PayloadLocation) {
    let begin = PAGE_HEADER_BYTES + position * LOCATION_BYTES;
    let entry = &bytes[begin..begin + LOCATION_BYTES];
    (
        BlockId(entry[..32].try_into().unwrap()),
        PayloadLocation {
            source: BlockId(entry[32..64].try_into().unwrap()),
            source_bytes: u32::from_le_bytes(entry[64..68].try_into().unwrap()),
            offset: u32::from_le_bytes(entry[68..72].try_into().unwrap()),
            length: u32::from_le_bytes(entry[72..76].try_into().unwrap()),
        },
    )
}
fn validate_page(bytes: &[u8], reference: &PageReference) -> io::Result<()> {
    if reference.entries == 0
        || reference.entries as usize > MAX_PAGE_ENTRIES
        || bytes.len() != PAGE_HEADER_BYTES + reference.entries as usize * LOCATION_BYTES
        || bytes.len() > MAX_BLOCK_BYTES
        || &bytes[..8] != PAGE_MAGIC
        || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        || u32::from_le_bytes(bytes[12..16].try_into().unwrap()) != reference.entries
    {
        return Err(invalid("history routing page header mismatch"));
    }
    let mut previous = None;
    for position in 0..reference.entries as usize {
        let (id, location) = page_entry(bytes, position);
        location.validate()?;
        if previous.is_some_and(|previous| previous >= id) {
            return Err(invalid("history routing page is not sorted"));
        }
        previous = Some(id);
    }
    if page_entry(bytes, 0).0 != reference.first
        || page_entry(bytes, reference.entries as usize - 1).0 != reference.last
    {
        return Err(invalid("history routing page witness mismatch"));
    }
    Ok(())
}

fn encode_page(entries: &[(BlockId, PayloadLocation)]) -> io::Result<Vec<u8>> {
    if entries.is_empty() || entries.len() > MAX_PAGE_ENTRIES {
        return Err(invalid("history routing page bounds invalid"));
    }
    let mut out = Vec::with_capacity(PAGE_HEADER_BYTES + entries.len() * LOCATION_BYTES);
    out.extend_from_slice(PAGE_MAGIC);
    out.extend_from_slice(&VERSION.to_le_bytes());
    out.extend_from_slice(&(entries.len() as u32).to_le_bytes());
    let mut previous = None;
    for &(id, location) in entries {
        location.validate()?;
        if previous.is_some_and(|previous| previous >= id) {
            return Err(invalid("history routing page is not sorted"));
        }
        out.extend_from_slice(&id.0);
        out.extend_from_slice(&location.source.0);
        out.extend_from_slice(&location.source_bytes.to_le_bytes());
        out.extend_from_slice(&location.offset.to_le_bytes());
        out.extend_from_slice(&location.length.to_le_bytes());
        previous = Some(id);
    }
    Ok(out)
}
fn decode_page(
    bytes: &[u8],
    reference: &PageReference,
) -> io::Result<Vec<(BlockId, PayloadLocation)>> {
    validate_page(bytes, reference)?;
    Ok((0..reference.entries as usize)
        .map(|position| page_entry(bytes, position))
        .collect())
}

struct RunWriter<'a> {
    blocks: &'a BlockStore,
    entries: Vec<(BlockId, PayloadLocation)>,
    run: Run,
}
impl<'a> RunWriter<'a> {
    fn new(blocks: &'a BlockStore) -> Self {
        Self {
            blocks,
            entries: Vec::new(),
            run: Run {
                entries: 0,
                pages: Vec::new(),
            },
        }
    }
    fn push(&mut self, entry: (BlockId, PayloadLocation)) -> io::Result<()> {
        self.entries.push(entry);
        if self.entries.len() == MAX_PAGE_ENTRIES {
            self.flush()?;
        }
        Ok(())
    }
    fn flush(&mut self) -> io::Result<()> {
        if self.entries.is_empty() {
            return Ok(());
        }
        let bytes = encode_page(&self.entries)?;
        self.run.pages.push(PageReference {
            id: self.blocks.put_loose(&bytes)?,
            first: self.entries.first().unwrap().0,
            last: self.entries.last().unwrap().0,
            entries: self.entries.len() as u32,
        });
        self.run.entries += self.entries.len() as u64;
        self.entries.clear();
        Ok(())
    }
    fn finish(mut self) -> io::Result<Run> {
        self.flush()?;
        Ok(self.run)
    }
}
struct RunCursor<'a> {
    blocks: &'a BlockStore,
    run: &'a Run,
    next_page: usize,
    entries: std::vec::IntoIter<(BlockId, PayloadLocation)>,
}
impl<'a> RunCursor<'a> {
    fn new(blocks: &'a BlockStore, run: &'a Run) -> Self {
        Self {
            blocks,
            run,
            next_page: 0,
            entries: Vec::new().into_iter(),
        }
    }
    fn next(&mut self) -> io::Result<Option<(BlockId, PayloadLocation)>> {
        if let Some(entry) = self.entries.next() {
            return Ok(Some(entry));
        }
        let Some(reference) = self.run.pages.get(self.next_page) else {
            return Ok(None);
        };
        self.next_page += 1;
        self.entries = decode_page(
            &self.blocks.get_materialized_shared(reference.id)?,
            reference,
        )?
        .into_iter();
        Ok(self.entries.next())
    }
}
fn merge_runs(blocks: &BlockStore, older: &Run, newer: &Run) -> io::Result<Run> {
    let mut old = RunCursor::new(blocks, older);
    let mut new = RunCursor::new(blocks, newer);
    let mut a = old.next()?;
    let mut b = new.next()?;
    let mut writer = RunWriter::new(blocks);
    while a.is_some() || b.is_some() {
        match (a, b) {
            (Some(left), Some(right)) if left.0 < right.0 => {
                writer.push(left)?;
                a = old.next()?;
            }
            (Some(left), Some(right)) if left.0 == right.0 => {
                writer.push(right)?;
                a = old.next()?;
                b = new.next()?;
            }
            (_, Some(right)) => {
                writer.push(right)?;
                b = new.next()?;
            }
            (Some(left), None) => {
                writer.push(left)?;
                a = old.next()?;
            }
            (None, None) => break,
        }
    }
    writer.finish()
}
fn size_level(entries: u64) -> usize {
    let quotient = (entries / BASE_LEVEL_ENTRIES).max(1);
    (63 - quotient.leading_zeros()) as usize
}

impl RoutingRoot {
    pub(crate) fn append(
        &self,
        blocks: &BlockStore,
        entries: &BTreeMap<BlockId, PayloadLocation>,
    ) -> io::Result<Self> {
        if entries.is_empty() {
            return Ok(self.clone());
        }
        let mut writer = RunWriter::new(blocks);
        for (&id, &location) in entries {
            writer.push((id, location))?;
        }
        let mut run = writer.finish()?;
        let mut result = Self {
            levels: self.levels.clone(),
            verified_pages: Arc::default(),
        };
        let mut level = size_level(run.entries);
        loop {
            if level >= MAX_LEVELS {
                return Err(invalid("history routing level capacity exceeded"));
            }
            result
                .levels
                .resize_with(result.levels.len().max(level + 1), || None);
            if let Some(older) = result.levels[level].take() {
                run = merge_runs(blocks, &older, &run)?;
                level = (level + 1).max(size_level(run.entries));
            } else {
                result.levels[level] = Some(run);
                break;
            }
        }
        Ok(result)
    }

    pub(crate) fn visit_locations(
        &self,
        blocks: &BlockStore,
        id: BlockId,
        visit: &mut dyn FnMut(PayloadLocation) -> io::Result<bool>,
    ) -> io::Result<()> {
        for run in self.levels.iter().flatten() {
            let position = run.pages.partition_point(|page| page.last < id);
            let Some(page) = run.pages.get(position).filter(|page| page.first <= id) else {
                continue;
            };
            let bytes = blocks.get_materialized_shared(page.id)?;
            let verified = self
                .verified_pages
                .lock()
                .map_err(|_| io::Error::other("history page verification lock poisoned"))?
                .get(&page.id)
                .and_then(Weak::upgrade)
                .is_some_and(|old| Arc::ptr_eq(&old, &bytes));
            if !verified {
                validate_page(&bytes, page)?;
                self.verified_pages
                    .lock()
                    .map_err(|_| io::Error::other("history page verification lock poisoned"))?
                    .insert(page.id, Arc::downgrade(&bytes));
            }
            let mut low = 0;
            let mut high = page.entries as usize;
            while low < high {
                let middle = low + (high - low) / 2;
                let entry = page_entry(&bytes, middle);
                match entry.0.cmp(&id) {
                    std::cmp::Ordering::Less => low = middle + 1,
                    std::cmp::Ordering::Greater => high = middle,
                    std::cmp::Ordering::Equal => {
                        if visit(entry.1)? {
                            return Ok(());
                        }
                        break;
                    }
                }
            }
        }
        Ok(())
    }

    pub(crate) fn lookup(
        &self,
        blocks: &BlockStore,
        id: BlockId,
    ) -> io::Result<Option<PayloadLocation>> {
        let mut found = None;
        self.visit_locations(blocks, id, &mut |location| {
            found = Some(location);
            Ok(true)
        })?;
        Ok(found)
    }

    /// Explicit history pruning rewrites only metadata after every pruned
    /// source payload has a durable ordinary copy. Streaming cursors bound
    /// memory independently of the total retained history size.
    pub(crate) fn retain_sources(
        &self,
        blocks: &BlockStore,
        keep: &std::collections::BTreeSet<BlockId>,
    ) -> io::Result<Self> {
        let mut merged: Option<Run> = None;
        for run in self.levels.iter().flatten().rev() {
            merged = Some(match merged {
                Some(previous) => merge_runs(blocks, &previous, run)?,
                None => run.clone(),
            });
        }
        let Some(merged) = merged else {
            return Ok(Self::default());
        };
        let mut cursor = RunCursor::new(blocks, &merged);
        let mut writer = RunWriter::new(blocks);
        while let Some(entry) = cursor.next()? {
            if keep.contains(&entry.1.source) {
                writer.push(entry)?;
            }
        }
        let run = writer.finish()?;
        if run.entries == 0 {
            return Ok(Self::default());
        }
        let level = size_level(run.entries);
        let mut levels = vec![None; level + 1];
        levels[level] = Some(run);
        Ok(Self {
            levels,
            verified_pages: Arc::default(),
        })
    }

    pub(crate) fn page_ids(&self) -> impl Iterator<Item = BlockId> + '_ {
        self.levels
            .iter()
            .flatten()
            .flat_map(|run| run.pages.iter().map(|page| page.id))
    }

    pub(crate) fn encode(&self) -> io::Result<Vec<u8>> {
        if self.levels.len() > MAX_LEVELS || self.levels.last().is_some_and(Option::is_none) {
            return Err(invalid("history routing root level bounds invalid"));
        }
        let mut out = Vec::new();
        out.extend_from_slice(ROOT_MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&(self.levels.len() as u32).to_le_bytes());
        for run in &self.levels {
            out.push(u8::from(run.is_some()));
            let Some(run) = run else {
                continue;
            };
            if run.pages.is_empty() || run.entries == 0 {
                return Err(invalid("empty history routing run"));
            }
            out.extend_from_slice(&run.entries.to_le_bytes());
            out.extend_from_slice(&(run.pages.len() as u32).to_le_bytes());
            let mut previous = None;
            let mut count = 0_u64;
            for page in &run.pages {
                if page.entries == 0
                    || page.entries as usize > MAX_PAGE_ENTRIES
                    || page.first > page.last
                    || previous.is_some_and(|last| last >= page.first)
                {
                    return Err(invalid("history routing run is not ordered"));
                }
                out.extend_from_slice(&page.id.0);
                out.extend_from_slice(&page.first.0);
                out.extend_from_slice(&page.last.0);
                out.extend_from_slice(&page.entries.to_le_bytes());
                count += u64::from(page.entries);
                previous = Some(page.last);
            }
            if count != run.entries {
                return Err(invalid("history routing run entry count mismatch"));
            }
        }
        if out.len() > MAX_BLOCK_BYTES {
            return Err(invalid("history routing root exceeds block bound"));
        }
        Ok(out)
    }

    pub(crate) fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.len() < 16
            || bytes.len() > MAX_BLOCK_BYTES
            || &bytes[..8] != ROOT_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        {
            return Err(invalid("history routing root header mismatch"));
        }
        let count = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        if count > MAX_LEVELS {
            return Err(invalid("history routing root exceeds level bound"));
        }
        let mut offset = 16;
        let mut levels = Vec::with_capacity(count);
        fn take<'a>(bytes: &'a [u8], offset: &mut usize, length: usize) -> io::Result<&'a [u8]> {
            let end = offset
                .checked_add(length)
                .ok_or_else(|| invalid("history routing length overflow"))?;
            let result = bytes
                .get(*offset..end)
                .ok_or_else(|| invalid("history routing root is truncated"))?;
            *offset = end;
            Ok(result)
        }
        for _ in 0..count {
            match take(bytes, &mut offset, 1)?[0] {
                0 => levels.push(None),
                1 => {
                    let entries =
                        u64::from_le_bytes(take(bytes, &mut offset, 8)?.try_into().unwrap());
                    let pages = u32::from_le_bytes(take(bytes, &mut offset, 4)?.try_into().unwrap())
                        as usize;
                    if pages > (bytes.len() - offset) / PAGE_REFERENCE_BYTES {
                        return Err(invalid("history routing page count invalid"));
                    }
                    let mut references = Vec::with_capacity(pages);
                    for _ in 0..pages {
                        references.push(PageReference {
                            id: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                            first: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                            last: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                            entries: u32::from_le_bytes(
                                take(bytes, &mut offset, 4)?.try_into().unwrap(),
                            ),
                        });
                    }
                    levels.push(Some(Run {
                        entries,
                        pages: references,
                    }));
                }
                _ => return Err(invalid("history routing level presence invalid")),
            }
        }
        if offset != bytes.len() {
            return Err(invalid("history routing root has trailing bytes"));
        }
        let root = Self {
            levels,
            verified_pages: Arc::default(),
        };
        if root.encode()? != bytes {
            return Err(invalid("history routing root is not canonical"));
        }
        Ok(root)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;

    fn store() -> BlockStore {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        BlockStore::initialize_with_vfs(Path::new("/data"), vfs).unwrap()
    }

    #[test]
    fn routing_merges_overlapping_runs_and_rewrites_pruned_sources() {
        let blocks = store();
        let old = BlockId([11; 32]);
        let new = BlockId([12; 32]);
        let mut root = RoutingRoot::default();
        for batch in 0..7_u32 {
            let source = if batch < 3 { old } else { new };
            let entries = (0..5000_u32)
                .map(|index| {
                    let id = BlockId::for_payload(&index.to_le_bytes());
                    (
                        id,
                        PayloadLocation {
                            source,
                            source_bytes: 16,
                            offset: batch,
                            length: 1,
                        },
                    )
                })
                .collect();
            root = root.append(&blocks, &entries).unwrap();
            assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
        }
        for index in [0_u32, 2500, 4999] {
            let id = BlockId::for_payload(&index.to_le_bytes());
            let mut candidates = Vec::new();
            root.visit_locations(&blocks, id, &mut |location| {
                candidates.push(location);
                Ok(false)
            })
            .unwrap();
            assert!(!candidates.is_empty());
            assert!(candidates.iter().all(|location| location.source == new));
        }
        let retained = root
            .retain_sources(&blocks, &std::collections::BTreeSet::from([new]))
            .unwrap();
        assert!(retained
            .lookup(&blocks, BlockId::for_payload(&4999_u32.to_le_bytes()))
            .unwrap()
            .is_some());
        assert!(retained
            .retain_sources(&blocks, &Default::default())
            .unwrap()
            .levels
            .is_empty());
    }

    #[test]
    fn routing_page_boundaries_and_zero_cache_lookup_are_correct() {
        let blocks = store();
        let entries: BTreeMap<_, _> = (0..MAX_PAGE_ENTRIES + 3)
            .map(|index| {
                (
                    BlockId::for_payload(&index.to_le_bytes()),
                    PayloadLocation {
                        source: BlockId([42; 32]),
                        source_bytes: 1,
                        offset: 0,
                        length: 1,
                    },
                )
            })
            .collect();
        let root = RoutingRoot::default().append(&blocks, &entries).unwrap();
        assert_eq!(root.page_ids().count(), 2);
        blocks.set_cache_capacity_bytes(0).unwrap();
        for (&id, &location) in entries
            .iter()
            .step_by(10000)
            .chain(entries.iter().rev().take(2))
        {
            assert_eq!(root.lookup(&blocks, id).unwrap(), Some(location));
        }
        assert_eq!(root.lookup(&blocks, BlockId([255; 32])).unwrap(), None);
    }

    #[test]
    fn index_and_root_reject_truncation_invalid_bounds_and_order() {
        let source = BlockId([3; 32]);
        let index = SegmentIndex {
            source,
            source_bytes: 50,
            entries: vec![(BlockId([1; 32]), 40, 10)],
        };
        let bytes = index.encode().unwrap();
        for length in 0..bytes.len() {
            assert!(SegmentIndex::decode(&bytes[..length], source).is_err());
        }
        let mut bad = index.clone();
        bad.entries[0].2 = 11;
        assert!(bad.encode().is_err());
        bad = index.clone();
        bad.entries.push(index.entries[0]);
        assert!(bad.encode().is_err());
        let blocks = store();
        let root = RoutingRoot::default()
            .append(&blocks, &index.locations().collect())
            .unwrap();
        let bytes = root.encode().unwrap();
        for length in 0..bytes.len() {
            assert!(RoutingRoot::decode(&bytes[..length]).is_err());
        }
    }
}
