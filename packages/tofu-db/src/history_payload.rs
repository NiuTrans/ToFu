//! Durable block locations inside immutable, authenticated history segments.
//!
//! SegmentIndex witnesses offsets against the canonical transaction parser.
//! RoutingRoot keeps sorted immutable runs, with at most one candidate page
//! per level. Engine owns publication through HistoryManifest/CONTROL;
//! BlockStore verifies every returned payload's content hash. Index blocks
//! use the ordinary block cache and never require a full-history startup scan.

use std::collections::{BTreeMap, BTreeSet};
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
/// A routing root that may still hold locations of sources its manifest
/// retired (lazy retirement). Engines before 2026-11-08 read such a location
/// as an integrity error, and they accept only `VERSION`: they refuse this
/// root at open instead of failing reads later.
const LAZY_ROOT_VERSION: u32 = 2;
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
    /// Encoded as `LAZY_ROOT_VERSION`: some run may still hold locations of
    /// retired sources. Only a full rewrite (`retain_sources`) clears it.
    holds_retired: bool,
}
impl PartialEq for RoutingRoot {
    fn eq(&self, other: &Self) -> bool {
        self.levels == other.levels && self.holds_retired == other.holds_retired
    }
}
impl Eq for RoutingRoot {}

pub(crate) struct Catalog {
    pub root: RoutingRoot,
    pub sources: std::collections::BTreeSet<BlockId>,
}

/// Exact candidates for one exclusive recovery pass, in ordinary lookup order.
/// Page failures are retained at their original position: a newer valid copy
/// can still answer before an older broken page, while a needed I/O error is
/// never consumed by preparation. No page bytes survive in this lookup.
pub(crate) struct RecoveryLookup {
    entries: BTreeMap<BlockId, Vec<RecoveryCandidate>>,
}

enum RecoveryCandidate {
    Location(PayloadLocation),
    PageError(Arc<io::Error>),
}

impl RecoveryLookup {
    pub(crate) fn visit_locations(
        &self, id: BlockId, visit: &mut dyn FnMut(PayloadLocation) -> io::Result<bool>,
    ) -> Option<io::Result<()>> {
        let entries = self.entries.get(&id)?;
        Some((|| {
            for entry in entries {
                match entry {
                    RecoveryCandidate::Location(location) => {
                        if visit(*location)? { break; }
                    }
                    RecoveryCandidate::PageError(error) => {
                        return Err(io::Error::new(error.kind(), Arc::clone(error)));
                    }
                }
            }
            Ok(())
        })())
    }
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

    /// Locations of `id` in the sources this manifest retains, in lookup
    /// order. Retirement leaves routing pages untouched, so a retired
    /// source's location stays in its run until a merge rewrites that run
    /// (`RoutingRoot::append`); it is never an answer.
    pub(crate) fn visit_locations(
        &self,
        blocks: &BlockStore,
        id: BlockId,
        visit: &mut dyn FnMut(PayloadLocation) -> io::Result<bool>,
    ) -> io::Result<()> {
        self.root.visit_locations(blocks, id, &mut |location| {
            if self.sources.contains(&location.source) { visit(location) } else { Ok(false) }
        })
    }

    pub(crate) fn lookup(&self, blocks: &BlockStore, id: BlockId) -> io::Result<Option<PayloadLocation>> {
        let mut found = None;
        self.visit_locations(blocks, id, &mut |location| {
            found = Some(location);
            Ok(true)
        })?;
        Ok(found)
    }

    pub(crate) fn prepare_recovery_lookup(
        &self, blocks: &BlockStore, ids: impl Iterator<Item = BlockId>,
        should_stop: &dyn Fn() -> bool,
    ) -> io::Result<RecoveryLookup> {
        self.root.prepare_recovery_lookup(blocks, ids, &self.sources, should_stop)
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
    /// The next location whose source the publishing manifest retains.
    fn next_retained(&mut self, retained: &BTreeSet<BlockId>) -> io::Result<Option<(BlockId, PayloadLocation)>> {
        while let Some(entry) = self.next()? {
            if retained.contains(&entry.1.source) {
                return Ok(Some(entry));
            }
        }
        Ok(None)
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
/// Merges two runs, dropping every location whose source `retained` no
/// longer names. Both sides are filtered before duplicate IDs resolve: any
/// retained location of a content-addressed block answers with its bytes.
fn merge_runs(blocks: &BlockStore, older: &Run, newer: &Run, retained: &BTreeSet<BlockId>) -> io::Result<Run> {
    let mut old = RunCursor::new(blocks, older);
    let mut new = RunCursor::new(blocks, newer);
    let mut a = old.next_retained(retained)?;
    let mut b = new.next_retained(retained)?;
    let mut writer = RunWriter::new(blocks);
    while a.is_some() || b.is_some() {
        match (a, b) {
            (Some(left), Some(right)) if left.0 < right.0 => {
                writer.push(left)?;
                a = old.next_retained(retained)?;
            }
            (Some(left), Some(right)) if left.0 == right.0 => {
                writer.push(right)?;
                a = old.next_retained(retained)?;
                b = new.next_retained(retained)?;
            }
            (_, Some(right)) => {
                writer.push(right)?;
                b = new.next_retained(retained)?;
            }
            (Some(left), None) => {
                writer.push(left)?;
                a = old.next_retained(retained)?;
            }
            (None, None) => break,
        }
    }
    writer.finish()
}
fn lookup_page(bytes: &[u8], page: &PageReference, id: BlockId) -> Option<PayloadLocation> {
    let mut low = 0;
    let mut high = page.entries as usize;
    while low < high {
        let middle = low + (high - low) / 2;
        let (candidate, location) = page_entry(bytes, middle);
        match candidate.cmp(&id) {
            std::cmp::Ordering::Less => low = middle + 1,
            std::cmp::Ordering::Greater => high = middle,
            std::cmp::Ordering::Equal => return Some(location),
        }
    }
    None
}

fn size_level(entries: u64) -> usize {
    let quotient = (entries / BASE_LEVEL_ENTRIES).max(1);
    (63 - quotient.leading_zeros()) as usize
}

impl RoutingRoot {
    /// Resolve only this open's selected IDs, reading each relevant page once
    /// regardless of payload-cache capacity or WAL order. Errors stay attached
    /// to affected IDs until replay actually needs that position in the run.
    /// Locations in sources `retained` does not name are not candidates.
    pub(crate) fn prepare_recovery_lookup(
        &self, blocks: &BlockStore, ids: impl Iterator<Item = BlockId>,
        retained: &BTreeSet<BlockId>, should_stop: &dyn Fn() -> bool,
    ) -> io::Result<RecoveryLookup> {
        let mut entries: BTreeMap<_, Vec<_>> = ids.map(|id| (id, Vec::new())).collect();
        for run in self.levels.iter().flatten() {
            for page in &run.pages {
                if should_stop() {
                    return Err(io::Error::new(io::ErrorKind::Interrupted, "history recovery lookup cancelled"));
                }
                if entries.range(page.first..=page.last).next().is_none() { continue; }
                let bytes = self.read_verified_page(blocks, page).map_err(Arc::new);
                for (id, candidates) in entries.range_mut(page.first..=page.last) {
                    if should_stop() {
                        return Err(io::Error::new(io::ErrorKind::Interrupted, "history recovery lookup cancelled"));
                    }
                    match &bytes {
                        Ok(bytes) => {
                            if let Some(location) = lookup_page(bytes, page, *id)
                                .filter(|location| retained.contains(&location.source))
                            {
                                candidates.push(RecoveryCandidate::Location(location));
                            }
                        }
                        Err(error) => candidates.push(RecoveryCandidate::PageError(Arc::clone(error))),
                    }
                }
            }
        }
        Ok(RecoveryLookup { entries })
    }

    fn read_verified_page(&self, blocks: &BlockStore, page: &PageReference) -> io::Result<Arc<Vec<u8>>> {
        let bytes = blocks.get_materialized_shared(page.id)?;
        let verified = self.verified_pages.lock()
            .map_err(|_| io::Error::other("history page verification lock poisoned"))?
            .get(&page.id).and_then(Weak::upgrade)
            .is_some_and(|old| Arc::ptr_eq(&old, &bytes));
        if !verified {
            validate_page(&bytes, page)?;
            self.verified_pages.lock()
                .map_err(|_| io::Error::other("history page verification lock poisoned"))?
                .insert(page.id, Arc::downgrade(&bytes));
        }
        Ok(bytes)
    }

    /// Appends a run of new locations and carries it through occupied levels
    /// like a binary counter. Every merge drops the locations whose source
    /// `retained` (the segments of the manifest this root is published with)
    /// no longer names: history retirement leaves routing pages untouched
    /// (`Catalog` skips such locations), and the merge that rewrites a run
    /// anyway is where they leave routing. Appended locations must name
    /// retained sources.
    pub(crate) fn append(
        &self,
        blocks: &BlockStore,
        entries: &BTreeMap<BlockId, PayloadLocation>,
        retained: &BTreeSet<BlockId>,
    ) -> io::Result<Self> {
        if entries.is_empty() {
            return Ok(self.clone());
        }
        if entries.values().any(|location| !retained.contains(&location.source)) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput,
                "history routing appends a location its manifest does not retain"));
        }
        let mut writer = RunWriter::new(blocks);
        for (&id, &location) in entries {
            writer.push((id, location))?;
        }
        let mut run = writer.finish()?;
        let mut result = Self {
            levels: self.levels.clone(),
            verified_pages: Arc::default(),
            holds_retired: self.holds_retired,
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
                run = merge_runs(blocks, &older, &run, retained)?;
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
            let bytes = self.read_verified_page(blocks, page)?;
            if let Some(location) = lookup_page(&bytes, page, id) {
                if visit(location)? { return Ok(()); }
            }
        }
        Ok(())
    }

    /// Every source a location of any run names, reading every page.
    #[cfg(test)]
    pub(crate) fn location_sources(&self, blocks: &BlockStore) -> io::Result<BTreeSet<BlockId>> {
        let mut sources = BTreeSet::new();
        for run in self.levels.iter().flatten() {
            let mut cursor = RunCursor::new(blocks, run);
            while let Some((_, location)) = cursor.next()? {
                sources.insert(location.source);
            }
        }
        Ok(sources)
    }

    /// Unfiltered: may answer a retired source. Readers use `Catalog::lookup`.
    #[cfg(test)]
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

    /// The same pages, published with a manifest that retired some of their
    /// sources: encoded as `LAZY_ROOT_VERSION`, which older engines refuse.
    pub(crate) fn with_retired_sources(&self) -> Self {
        Self {
            levels: self.levels.clone(),
            verified_pages: Arc::clone(&self.verified_pages),
            holds_retired: true,
        }
    }

    pub(crate) fn holds_retired(&self) -> bool {
        self.holds_retired
    }

    pub(crate) fn version(&self) -> u32 {
        if self.holds_retired { LAZY_ROOT_VERSION } else { VERSION }
    }

    pub(crate) fn entries(&self) -> u64 {
        self.levels.iter().flatten().map(|run| run.entries).sum()
    }

    /// Bulk operator compaction and the downgrade rewrite stream every run
    /// once through one page writer instead of collecting every retired
    /// source's locations: a k-way merge in which, as in `Catalog` lookups,
    /// the lowest level holding a retained location of an ID answers.
    /// Merging the runs pairwise wrote one complete run per extra level.
    /// Automatic slices leave routing untouched (lazy retirement, see
    /// `append`). The result holds only `keep`'s locations, so it is encoded
    /// as `VERSION` again.
    pub(crate) fn retain_sources(
        &self,
        blocks: &BlockStore,
        keep: &BTreeSet<BlockId>,
    ) -> io::Result<Self> {
        let mut cursors: Vec<RunCursor> =
            self.levels.iter().flatten().map(|run| RunCursor::new(blocks, run)).collect();
        let mut heads = cursors
            .iter_mut()
            .map(|cursor| cursor.next_retained(keep))
            .collect::<io::Result<Vec<_>>>()?;
        let mut writer = RunWriter::new(blocks);
        loop {
            let lowest = heads
                .iter()
                .enumerate()
                .filter_map(|(level, head)| head.map(|(id, location)| (id, level, location)))
                .min_by_key(|&(id, level, _)| (id, level));
            let Some((id, _, location)) = lowest else { break };
            writer.push((id, location))?;
            for (cursor, head) in cursors.iter_mut().zip(heads.iter_mut()) {
                if head.is_some_and(|(other, _)| other == id) {
                    *head = cursor.next_retained(keep)?;
                }
            }
        }
        let run = writer.finish()?;
        if run.entries == 0 {
            return Ok(Self::default());
        }
        let level = size_level(run.entries);
        let mut levels = vec![None; level + 1];
        levels[level] = Some(run);
        Ok(Self { levels, verified_pages: Arc::default(), holds_retired: false })
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
        let version = if self.holds_retired { LAZY_ROOT_VERSION } else { VERSION };
        out.extend_from_slice(&version.to_le_bytes());
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
        let version = bytes.get(8..12).map(|field| u32::from_le_bytes(field.try_into().unwrap()));
        if bytes.len() < 16
            || bytes.len() > MAX_BLOCK_BYTES
            || &bytes[..8] != ROOT_MAGIC
            || !matches!(version, Some(VERSION | LAZY_ROOT_VERSION))
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
            holds_retired: version == Some(LAZY_ROOT_VERSION),
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
    fn recovery_lookup_matches_ordered_candidates_absence_and_unselected_ids() {
        let blocks = store();
        let id = BlockId([20; 32]);
        let absent = BlockId([21; 32]);
        let location = |source| PayloadLocation { source: BlockId([source; 32]), source_bytes: 16, offset: 0, length: 1 };
        let make_run = |source| {
            let mut writer = RunWriter::new(&blocks);
            writer.push((id, location(source))).unwrap();
            writer.push((BlockId([22; 32]), location(source))).unwrap();
            writer.finish().unwrap()
        };
        let root = RoutingRoot { levels: vec![Some(make_run(2)), Some(make_run(1))], verified_pages: Arc::default(), holds_retired: false };
        blocks.set_cache_capacity_bytes(0).unwrap();
        let both = BTreeSet::from([BlockId([1; 32]), BlockId([2; 32])]);
        let prepared = root.prepare_recovery_lookup(&blocks, [id, absent].into_iter(), &both, &|| false).unwrap();
        for selected in [id, absent] {
            let mut ordinary = Vec::new();
            root.visit_locations(&blocks, selected, &mut |location| { ordinary.push(location); Ok(false) }).unwrap();
            let mut batched = Vec::new();
            prepared.visit_locations(selected, &mut |location| { batched.push(location); Ok(false) }).unwrap().unwrap();
            assert_eq!(batched, ordinary);
        }
        assert!(prepared.visit_locations(BlockId([23; 32]), &mut |_| panic!("unselected ID")).is_none());
        let mut first = Vec::new();
        prepared.visit_locations(id, &mut |location| { first.push(location); Ok(true) }).unwrap().unwrap();
        assert_eq!(first, vec![location(2)]);
        let newer_retired = root.prepare_recovery_lookup(
            &blocks, [id].into_iter(), &BTreeSet::from([BlockId([1; 32])]), &|| false).unwrap();
        let mut remaining = Vec::new();
        newer_retired.visit_locations(id, &mut |location| { remaining.push(location); Ok(false) }).unwrap().unwrap();
        assert_eq!(remaining, vec![location(1)], "a retired source is never a recovery candidate");
    }

    #[test]
    fn recovery_lookup_keeps_page_errors_at_their_original_candidate_position() {
        use crate::vfs::{FaultAction, FaultRule, OpenRequest};
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let blocks = BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let id = BlockId([20; 32]);
        let run = |source| {
            let mut writer = RunWriter::new(&blocks);
            writer.push((id, PayloadLocation { source: BlockId([source; 32]), source_bytes: 1, offset: 0, length: 1 })).unwrap();
            writer.finish().unwrap()
        };
        let root = RoutingRoot { levels: vec![Some(run(2)), Some(run(1))], verified_pages: Arc::default(), holds_retired: false };
        let both = BTreeSet::from([BlockId([1; 32]), BlockId([2; 32])]);
        let old_page = root.levels[1].as_ref().unwrap().pages[0].id.to_hex();
        let path = format!("/data/blocks/{}/{old_page}.blk", &old_page[..2]);
        vfs.open(Path::new(&path), OpenRequest { write: true, ..OpenRequest::default() }).unwrap()
            .write_all_at(16, b"!").unwrap();
        blocks.set_cache_capacity_bytes(0).unwrap();
        let prepared = root.prepare_recovery_lookup(&blocks, [id].into_iter(), &both, &|| false).unwrap();
        prepared.visit_locations(id, &mut |_| Ok(true)).unwrap().unwrap();
        assert_eq!(prepared.visit_locations(id, &mut |_| Ok(false)).unwrap().unwrap_err().kind(), io::ErrorKind::InvalidData);
        // A non-repairable I/O error encountered during preparation must not
        // disappear just because the transient fault has been consumed.
        vfs.arm_fault(Some(FaultRule { operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied) })).unwrap();
        let failed = root.prepare_recovery_lookup(&blocks, [id].into_iter(), &both, &|| false).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(failed.visit_locations(id, &mut |_| panic!("failed first page")).unwrap()
            .unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(root.prepare_recovery_lookup(&blocks, [id].into_iter(), &both, &|| true).err().unwrap().kind(), io::ErrorKind::Interrupted);
        assert!(vfs.trace().unwrap().is_empty(), "cancelled preparation performed I/O");
    }

    #[test]
    fn merges_drop_retired_sources_and_keep_retained_duplicates() {
        // A merge rewrites its runs anyway, so it is where retired locations
        // leave routing. A retained copy in the older run survives even when
        // the newer run's copy of the same block names a retired source.
        let blocks = store();
        let (retired, older, newer) = (BlockId([11; 32]), BlockId([12; 32]), BlockId([13; 32]));
        let location = |source| PayloadLocation { source, source_bytes: 16, offset: 0, length: 1 };
        let shared = BlockId([20; 32]);
        let mut writer = RunWriter::new(&blocks);
        writer.push((shared, location(older))).unwrap();
        writer.push((BlockId([21; 32]), location(retired))).unwrap();
        let old_run = writer.finish().unwrap();
        let mut writer = RunWriter::new(&blocks);
        writer.push((shared, location(retired))).unwrap();
        writer.push((BlockId([22; 32]), location(newer))).unwrap();
        let new_run = writer.finish().unwrap();
        let merged = merge_runs(&blocks, &old_run, &new_run, &BTreeSet::from([older, newer])).unwrap();
        assert_eq!(merged.entries, 2);
        let root = RoutingRoot { levels: vec![Some(merged)], verified_pages: Arc::default(), holds_retired: false };
        assert_eq!(root.lookup(&blocks, shared).unwrap(), Some(location(older)));
        assert_eq!(root.lookup(&blocks, BlockId([21; 32])).unwrap(), None);
        assert_eq!(root.lookup(&blocks, BlockId([22; 32])).unwrap(), Some(location(newer)));
        assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
    }

    #[test]
    fn catalog_lookups_skip_locations_of_retired_sources() {
        // Retirement leaves a location in its page; only the manifest's
        // sources answer, for ordinary and prepared recovery lookups alike.
        let blocks = store();
        let (retired, retained) = (BlockId([11; 32]), BlockId([12; 32]));
        let location = |source| PayloadLocation { source, source_bytes: 16, offset: 0, length: 1 };
        let (id, kept) = (BlockId([20; 32]), BlockId([21; 32]));
        let root = RoutingRoot::default()
            .append(&blocks, &BTreeMap::from([(id, location(retired)), (kept, location(retained))]),
                &BTreeSet::from([retired, retained]))
            .unwrap();
        let catalog = Catalog { root, sources: BTreeSet::from([retained]) };
        assert_eq!(catalog.root.lookup(&blocks, id).unwrap(), Some(location(retired)), "the page still holds it");
        assert_eq!(catalog.lookup(&blocks, id).unwrap(), None);
        assert_eq!(catalog.lookup(&blocks, kept).unwrap(), Some(location(retained)));
        let prepared = catalog.prepare_recovery_lookup(&blocks, [id, kept].into_iter(), &|| false).unwrap();
        prepared.visit_locations(id, &mut |_| panic!("a retired source answered")).unwrap().unwrap();
        let mut found = Vec::new();
        prepared.visit_locations(kept, &mut |location| { found.push(location); Ok(false) }).unwrap().unwrap();
        assert_eq!(found, vec![location(retained)]);
    }

    /// Three runs on separate levels; some IDs repeat across levels under
    /// other sources, and source 4 is retired by `keep`.
    fn layered_root(blocks: &BlockStore) -> (RoutingRoot, BTreeSet<BlockId>) {
        let source = |n: u8| BlockId([n; 32]);
        let id = |i: usize| BlockId::for_payload(format!("layered {i}").as_bytes());
        let location = |source, i: usize| PayloadLocation { source, source_bytes: 4096, offset: (i % 4000) as u32, length: 1 };
        let all = BTreeSet::from([source(1), source(2), source(3), source(4)]);
        let run = |range: std::ops::Range<usize>, pick: &dyn Fn(usize) -> BlockId| -> BTreeMap<_, _> {
            range.map(|i| (id(i), location(pick(i), i))).collect()
        };
        let root = RoutingRoot::default()
            .append(blocks, &run(0..16_384, &|_| source(1)), &all).unwrap()
            .append(blocks, &run(8_192..16_384, &|i| source(if i % 2 == 0 { 2 } else { 4 })), &all).unwrap()
            .append(blocks, &run(12_288..16_384, &|i| source(if i % 3 == 0 { 4 } else { 3 })), &all).unwrap();
        assert_eq!(root.levels.iter().flatten().count(), 3, "the fixture keeps three separate runs");
        (root, BTreeSet::from([source(1), source(2), source(3)]))
    }

    fn counting_store() -> (Arc<DeterministicVfs>, BlockStore) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let blocks = BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, blocks)
    }

    fn files_written(vfs: &DeterministicVfs) -> usize {
        vfs.trace().unwrap().iter().filter(|operation| **operation == crate::vfs::Operation::Rename).count()
    }

    /// Bulk retention merged the runs pairwise from the oldest level down and
    /// wrote every intermediate run (live 2026-11-08: 6 runs of ~11.6 GB
    /// together). One k-way pass writes the retained run once and answers
    /// like the pairwise merge and `Catalog`: the lowest level holding a
    /// retained location of an ID wins.
    #[test]
    fn retain_sources_writes_one_run_with_the_answers_of_a_pairwise_merge() {
        let (vfs, blocks) = counting_store();
        let (root, keep) = layered_root(&blocks);
        vfs.arm_fault(None).unwrap();
        let streamed = root.retain_sources(&blocks, &keep).unwrap();
        let streamed_pages = streamed.page_ids().count();
        assert_eq!(files_written(&vfs), streamed_pages, "only the retained run is written");

        let (pairwise_vfs, pairwise_blocks) = counting_store();
        let (pairwise_root, _) = layered_root(&pairwise_blocks);
        pairwise_vfs.arm_fault(None).unwrap();
        let mut merged: Option<Run> = None;
        for run in pairwise_root.levels.iter().flatten().rev() {
            merged = Some(match merged {
                Some(previous) => merge_runs(&pairwise_blocks, &previous, run, &keep).unwrap(),
                None => run.clone(),
            });
        }
        let merged = merged.unwrap();
        assert!(files_written(&pairwise_vfs) > streamed_pages, "the pairwise merge writes an intermediate run");
        assert_eq!(streamed.levels.iter().flatten().collect::<Vec<_>>(), vec![&merged]);
        assert_eq!(streamed.version(), VERSION);

        let catalog = Catalog { root: root.clone(), sources: keep.clone() };
        for i in 0..16_384 {
            let id = BlockId::for_payload(format!("layered {i}").as_bytes());
            assert_eq!(streamed.lookup(&blocks, id).unwrap(), catalog.lookup(&blocks, id).unwrap(), "ID {i}");
        }
        assert_eq!(streamed.entries(), 16_384, "every ID keeps its run-1 location at least");
    }

    #[test]
    fn lazily_retired_roots_carry_a_version_older_engines_refuse() {
        // Before 2026-11-08 an engine read a location outside its manifest as
        // an integrity error and accepted only version 1: a root that may
        // still hold retired locations must fail its open instead.
        let blocks = store();
        let (retired, retained) = (BlockId([11; 32]), BlockId([12; 32]));
        let location = |source| PayloadLocation { source, source_bytes: 16, offset: 0, length: 1 };
        let root = RoutingRoot::default()
            .append(&blocks, &BTreeMap::from([(BlockId([20; 32]), location(retired)),
                (BlockId([21; 32]), location(retained))]), &BTreeSet::from([retired, retained]))
            .unwrap();
        let version = |root: &RoutingRoot| u32::from_le_bytes(root.encode().unwrap()[8..12].try_into().unwrap());
        assert_eq!(version(&root), VERSION);
        let lazy = root.with_retired_sources();
        assert_eq!(version(&lazy), LAZY_ROOT_VERSION);
        assert_eq!(RoutingRoot::decode(&lazy.encode().unwrap()).unwrap(), lazy);
        assert!(lazy.page_ids().eq(root.page_ids()), "no page is rewritten");
        let appended = lazy
            .append(&blocks, &BTreeMap::from([(BlockId([22; 32]), location(retained))]), &BTreeSet::from([retained]))
            .unwrap();
        assert_eq!(version(&appended), LAZY_ROOT_VERSION, "an untouched run may still hold retired locations");
        let rewritten = lazy.retain_sources(&blocks, &BTreeSet::from([retained])).unwrap();
        assert_eq!(version(&rewritten), VERSION);
        assert_eq!(rewritten.lookup(&blocks, BlockId([20; 32])).unwrap(), None);
        let mut unknown = lazy.encode().unwrap();
        unknown[8..12].copy_from_slice(&3_u32.to_le_bytes());
        assert_eq!(RoutingRoot::decode(&unknown).unwrap_err().kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn appends_refuse_locations_their_manifest_does_not_retain() {
        let blocks = store();
        let location = PayloadLocation { source: BlockId([11; 32]), source_bytes: 16, offset: 0, length: 1 };
        let error = RoutingRoot::default()
            .append(&blocks, &BTreeMap::from([(BlockId([20; 32]), location)]), &BTreeSet::new())
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn routing_merges_overlapping_runs_and_drop_retired_sources() {
        let blocks = store();
        let (old, new, newest) = (BlockId([11; 32]), BlockId([12; 32]), BlockId([13; 32]));
        let location = |source, batch| PayloadLocation { source, source_bytes: 16, offset: batch, length: 1 };
        let id = |index: u32| BlockId::for_payload(&index.to_le_bytes());
        let mut root = RoutingRoot::default();
        // Batches 0-2 route IDs 0..5000 to `old`; batches 3-6 route 2500..7500
        // to `new`, superseding half of them.
        for batch in 0..7_u32 {
            let (source, ids) = if batch < 3 { (old, 0..5000) } else { (new, 2500..7500) };
            let entries = ids.map(|index| (id(index), location(source, batch))).collect();
            root = root.append(&blocks, &entries, &BTreeSet::from([old, new])).unwrap();
            assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
        }
        for index in [2500_u32, 4999, 7499] {
            let mut candidates = Vec::new();
            root.visit_locations(&blocks, id(index), &mut |location| { candidates.push(location); Ok(false) }).unwrap();
            assert!(!candidates.is_empty());
            assert!(candidates.iter().all(|location| location.source == new));
        }
        assert_eq!(root.lookup(&blocks, id(0)).unwrap().map(|location| location.source), Some(old));
        // `old` retires. The eighth append carries through every level, so
        // its merges rewrite every run and drop `old`'s remaining locations.
        let entries = (10_000..10_010).map(|index| (id(index), location(newest, 7))).collect();
        root = root.append(&blocks, &entries, &BTreeSet::from([new, newest])).unwrap();
        assert_eq!(root.levels.iter().flatten().count(), 1);
        assert_eq!(root.levels.iter().flatten().map(|run| run.entries).sum::<u64>(), 5010);
        assert_eq!(root.lookup(&blocks, id(0)).unwrap(), None);
        assert_eq!(root.lookup(&blocks, id(2500)).unwrap().map(|location| location.source), Some(new));
        assert_eq!(root.lookup(&blocks, id(10_009)).unwrap().map(|location| location.source), Some(newest));
        assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
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
        let root = RoutingRoot::default().append(&blocks, &entries, &BTreeSet::from([BlockId([42; 32])])).unwrap();
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
            .append(&blocks, &index.locations().collect(), &BTreeSet::from([source]))
            .unwrap();
        let bytes = root.encode().unwrap();
        for length in 0..bytes.len() {
            assert!(RoutingRoot::decode(&bytes[..length]).is_err());
        }
    }
}
