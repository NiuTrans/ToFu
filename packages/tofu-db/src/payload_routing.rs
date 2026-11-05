//! Reconstructible hash-prefix routing for one installed payload catalog.
//!
//! BlockStore publishes this with the manifest under the same lock boundary.
//! Verified segment indexes provide prefixes; missing/over-budget indexes stay
//! in an explicit fallback set. No negative routing result can hide a segment
//! that was not indexed. Retirement drops all routes from the old catalog.
//! An over-budget verified segment keeps only its immutable prefix count, so a
//! later catalog install re-reads its index only when that route can fit.
use std::collections::{HashMap, BTreeSet};
use std::sync::Arc;
use uuid::Uuid;
use crate::block::BlockId;
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::{PayloadSegment, PayloadSegmentStore};

#[derive(Default)]
pub(crate) struct PayloadRouting {
    indexed: HashMap<Uuid, (PayloadSegmentReference, Arc<Vec<u16>>)>,
    prefixes: HashMap<u16, Vec<PayloadSegmentReference>>,
    fallback: Vec<PayloadSegmentReference>,
    /// Distinct-prefix counts of verified fallback segments, at most one per
    /// catalogued segment.
    fallback_prefix_counts: HashMap<Uuid, (PayloadSegmentReference, usize)>,
}
impl PayloadRouting {
    /// Reuse an index the recovery pass already verified. Keep only prefixes
    /// admitted by the same routing budget; all others remain explicit fallback.
    pub(crate) fn remember_verified(
        &mut self, reference: PayloadSegmentReference, segment: &PayloadSegment,
        remaining: &mut usize,
    ) {
        if *remaining < 256 { return; }
        let prefixes: BTreeSet<u16> = segment.block_ids()
            .map(|id| u16::from_be_bytes([id.0[0], id.0[1]])).collect();
        let cost = prefixes.len().saturating_mul(256);
        if cost > *remaining {
            self.fallback_prefix_counts.insert(reference.segment_id, (reference, prefixes.len()));
            return;
        }
        *remaining -= cost;
        self.indexed.insert(reference.segment_id,
            (reference, Arc::new(prefixes.into_iter().collect())));
    }

    /// Routes for `manifest`, reusing what `previous` (the installed routes)
    /// or `verified` (indexes this publication already read) know about each
    /// segment before reading any index.
    pub(crate) fn build(manifest: &PayloadManifest, store: Option<&PayloadSegmentStore>,
        previous: &Self, verified: &Self, budget: usize, read_indexes: bool) -> Self {
        let mut routing = Self::default();
        let mut remaining = budget;
        for reference in &manifest.segments {
            let known_count = [previous, verified].into_iter().find_map(|known| {
                known.fallback_prefix_counts.get(&reference.segment_id)
                    .filter(|(old, _)| old == reference).map(|(_, count)| *count)
            });
            let prefixes = [previous, verified].into_iter()
                .find_map(|known| {
                    known.indexed.get(&reference.segment_id)
                        .filter(|(old, _)| old == reference).map(|(_, prefixes)| Arc::clone(prefixes))
                })
                .or_else(|| {
                    if !read_indexes || remaining < 256 { return None; }
                    if known_count.is_some_and(|count| count.saturating_mul(256) > remaining) { return None; }
                    let segment = store?.open_segment(reference.segment_id).ok()?;
                    if PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id()).ok().as_ref() != Some(reference) { return None; }
                    let prefixes: BTreeSet<u16> = segment.block_ids()
                        .map(|id| u16::from_be_bytes([id.0[0], id.0[1]])).collect();
                    Some(Arc::new(prefixes.into_iter().collect()))
                });
            let prefix_count = prefixes.as_ref().map(|prefixes| prefixes.len()).or(known_count);
            let Some(prefixes) = prefixes.filter(|prefixes| prefixes.len().saturating_mul(256) <= remaining) else {
                routing.fallback.push(*reference);
                if let Some(count) = prefix_count {
                    routing.fallback_prefix_counts.insert(reference.segment_id, (*reference, count));
                }
                continue;
            };
            remaining -= prefixes.len() * 256;
            for prefix in prefixes.iter() { routing.prefixes.entry(*prefix).or_default().push(*reference); }
            routing.indexed.insert(reference.segment_id, (*reference, prefixes));
        }
        routing
    }

    pub(crate) fn candidates(&self, id: BlockId) -> Vec<PayloadSegmentReference> {
        let prefix = u16::from_be_bytes([id.0[0], id.0[1]]);
        let mut result: Vec<_> = self.prefixes.get(&prefix).into_iter().flatten()
            .chain(self.fallback.iter()).filter(|reference| reference.may_contain(id)).copied().collect();
        result.sort_by_key(|reference| reference.segment_id);
        result
    }
}
