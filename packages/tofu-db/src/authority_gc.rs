//! Explicit bounded garbage collection for one exclusively owned authority.
//!
//! The collector marks every block reachable from the current Entity graph,
//! retained transaction suffix, and active WAL projection. It refuses live
//! in-memory snapshots. Before deleting any candidate it republishes the
//! unchanged CONTROL state, so both alternating slots reference the same
//! roots and a checksum fallback cannot select already-collected history.

use std::cell::RefCell;
use std::collections::BTreeSet;
use std::io;
use std::path::Path;
use std::sync::Arc;

use crate::backup::visit_semantic_value_blocks;
use crate::backup_gc::SpillMarkSet;
use crate::engine::Engine;
use crate::entity::visit_entity_page_graph_with_values;
use crate::generated_tofudb_ir::{
    AUTHORITY_GC_TEMPORARY_FREE_SPACE_PERCENT, LEAN_AUTHORITY_GC_TEMPORARY_BYTES,
};
pub use crate::generated_tofudb_ir::{
    MAX_AUTHORITY_GC_BLOCKS_PER_ROUND, MAX_AUTHORITY_GC_ORPHAN_PAYLOAD_SEGMENT_FILES_REMOVED,
    MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED, MAX_AUTHORITY_GC_PAYLOAD_SEGMENT_FILES_SCANNED,
    MAX_AUTHORITY_GC_PHYSICAL_BLOCKS, MAX_AUTHORITY_GC_TEMPORARY_BLOCK_FILES_REMOVED,
    MAX_AUTHORITY_GC_TEMPORARY_BYTES, MAX_AUTHORITY_GC_VICTIM_BYTES,
    MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS,
};
use crate::payload_manifest::{MAX_PAYLOAD_SEGMENTS, MAX_PAYLOAD_SEGMENTS_PER_SHARD};
use crate::payload_segment::{MAX_SEGMENT_BLOCKS, MAX_SEGMENT_PAYLOAD_BYTES};
const _: () = assert!(MAX_AUTHORITY_GC_ORPHAN_PAYLOAD_SEGMENT_FILES_REMOVED == 1);
const _: () = assert!(MAX_AUTHORITY_GC_TEMPORARY_BLOCK_FILES_REMOVED == 1);
use crate::resource_probe::DaemonResourceBudget;
use crate::vfs::{sync_directory_barrier, FileKind, Vfs};

const GC_WORK_DIRECTORY: &str = "authority-gc";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AuthorityGarbageCollectionBudget {
    pub maximum_victim_bytes: u64,
    pub maximum_temporary_bytes: u64,
    pub maximum_blocks_per_round: usize,
    pub maximum_physical_blocks: usize,
}

impl AuthorityGarbageCollectionBudget {
    pub const fn conservative() -> Self {
        Self {
            maximum_victim_bytes: MAX_AUTHORITY_GC_VICTIM_BYTES,
            maximum_temporary_bytes: LEAN_AUTHORITY_GC_TEMPORARY_BYTES,
            maximum_blocks_per_round: MAX_AUTHORITY_GC_BLOCKS_PER_ROUND,
            maximum_physical_blocks: MAX_AUTHORITY_GC_PHYSICAL_BLOCKS,
        }
    }

    pub fn from_resource_budget(resource_budget: DaemonResourceBudget) -> Self {
        let maximum_temporary_bytes = resource_budget
            .snapshot
            .volume_free_bytes
            .map_or(LEAN_AUTHORITY_GC_TEMPORARY_BYTES, percentage_of_free_space)
            .min(MAX_AUTHORITY_GC_TEMPORARY_BYTES);
        Self {
            maximum_victim_bytes: MAX_AUTHORITY_GC_VICTIM_BYTES,
            maximum_temporary_bytes,
            maximum_blocks_per_round: MAX_AUTHORITY_GC_BLOCKS_PER_ROUND,
            maximum_physical_blocks: MAX_AUTHORITY_GC_PHYSICAL_BLOCKS,
        }
    }

    fn validate(self) -> io::Result<Self> {
        if self.maximum_victim_bytes == 0
            || self.maximum_victim_bytes > MAX_AUTHORITY_GC_VICTIM_BYTES
            || self.maximum_temporary_bytes == 0
            || self.maximum_temporary_bytes > MAX_AUTHORITY_GC_TEMPORARY_BYTES
            || self.maximum_blocks_per_round == 0
            || self.maximum_blocks_per_round > MAX_AUTHORITY_GC_BLOCKS_PER_ROUND
            || self.maximum_physical_blocks == 0
            || self.maximum_physical_blocks > MAX_AUTHORITY_GC_PHYSICAL_BLOCKS
        {
            return Err(invalid_input("authority GC budget exceeds its hard bounds"));
        }
        Ok(self)
    }
}

fn percentage_of_free_space(free_bytes: u64) -> u64 {
    let whole = (free_bytes / 100).saturating_mul(AUTHORITY_GC_TEMPORARY_FREE_SPACE_PERCENT);
    let remainder =
        (free_bytes % 100).saturating_mul(AUTHORITY_GC_TEMPORARY_FREE_SPACE_PERCENT) / 100;
    whole.saturating_add(remainder)
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct AuthorityGarbageCollectionMetrics {
    pub scanned_blocks: u64,
    pub live_references: u64,
    pub spill_bytes: u64,
    pub candidate_blocks: u32,
    pub candidate_bytes: u64,
    pub removed_blocks: u32,
    pub removed_bytes: u64,
    pub scanned_loose_block_entries: u64,
    pub candidate_temporary_block_files: u32,
    pub candidate_temporary_block_bytes: u64,
    pub removed_temporary_block_files: u32,
    pub removed_temporary_block_bytes: u64,
    pub scanned_payload_segment_files: u32,
    pub candidate_orphan_segment_files: u32,
    pub candidate_orphan_segment_bytes: u64,
    pub removed_orphan_segment_files: u32,
    pub removed_orphan_segment_bytes: u64,
    pub payload_compaction_shard: Option<u8>,
    pub candidate_payload_compaction_blocks: u32,
    pub candidate_payload_compaction_bytes: u64,
    pub compacted_payload_blocks: u32,
    pub compacted_payload_bytes: u64,
    pub compacted_segment_bytes: u64,
    pub compacted_loose_bytes_reclaimed: u64,
    pub payload_compaction_catalog_blocked: bool,
    pub scanned_payload_segments: u32,
    pub candidate_segment_blocks: u32,
    pub candidate_segment_dead_blocks: u32,
    pub candidate_segment_bytes: u64,
    pub rewritten_payload_segments: u32,
    pub retired_payload_segments: u32,
    pub replacement_segment_bytes: u64,
    pub removed_segment_bytes: u64,
    pub segment_budget_blocked: bool,
    pub more_candidates: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum AuthorityGarbageCollectionMode {
    Plan,
    Execute,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn ensure_directory(path: &Path, parent: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    match vfs.metadata(path) {
        Ok(FileKind::Directory) => Ok(()),
        Ok(FileKind::File) => Err(invalid_data("authority GC work path is a file")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            vfs.create_dir(path)?;
            sync_directory_barrier(vfs, parent)
        }
        Err(error) => Err(error),
    }
}

pub(crate) fn collect_authority_garbage(
    engine: &mut Engine,
    budget: AuthorityGarbageCollectionBudget,
    mode: AuthorityGarbageCollectionMode,
) -> io::Result<AuthorityGarbageCollectionMetrics> {
    let budget = budget.validate()?;
    let root = engine
        .state()
        .authority_state_root
        .ok_or_else(|| invalid_input("authority GC refuses a legacy CONTROL root"))?;
    let vfs = engine.vfs();
    let work_directory = engine.data_dir().join(GC_WORK_DIRECTORY);
    ensure_directory(&work_directory, engine.data_dir(), vfs.as_ref())?;
    let run_directory = work_directory.join("gc-runs");
    ensure_directory(&run_directory, &work_directory, vfs.as_ref())?;

    let temporary_plan = engine
        .block_store()
        .plan_temporary_file(budget.maximum_physical_blocks)?;
    if let Some(candidate) = &temporary_plan.candidate {
        if mode == AuthorityGarbageCollectionMode::Plan {
            return Ok(AuthorityGarbageCollectionMetrics {
                scanned_loose_block_entries: temporary_plan.scanned_entries,
                candidate_temporary_block_files: 1,
                candidate_temporary_block_bytes: candidate.file_bytes,
                more_candidates: true,
                ..AuthorityGarbageCollectionMetrics::default()
            });
        }
        let removed_bytes = engine.block_store().remove_temporary_file(candidate)?;
        return Ok(AuthorityGarbageCollectionMetrics {
            scanned_loose_block_entries: temporary_plan.scanned_entries,
            candidate_temporary_block_files: 1,
            candidate_temporary_block_bytes: candidate.file_bytes,
            removed_temporary_block_files: 1,
            removed_temporary_block_bytes: removed_bytes,
            more_candidates: true,
            ..AuthorityGarbageCollectionMetrics::default()
        });
    }

    let mut live = SpillMarkSet::initialize_with_spill_limit(
        &work_directory,
        Arc::clone(&vfs),
        budget.maximum_temporary_bytes,
    )?;
    let mark_result = (|| {
        let live_cell = RefCell::new(&mut live);
        visit_entity_page_graph_with_values(
            root,
            |block_id| engine.block_store().get(block_id),
            |block_id, _| live_cell.borrow_mut().insert(block_id),
            |key, value| {
                visit_semantic_value_blocks(engine.block_store(), key, value, |block_id, _| {
                    live_cell.borrow_mut().insert(block_id)
                })
            },
        )?;
        engine.visit_retained_transaction_blocks(|block_id| {
            live_cell.borrow_mut().insert(block_id)
        })?;
        let result = live_cell.borrow_mut().finish();
        result
    })();
    let mark_metrics = match mark_result {
        Ok(metrics) => metrics,
        Err(error) => {
            let _ = live.cleanup();
            return Err(error);
        }
    };

    let mut candidates = Vec::new();
    let mut candidate_bytes = 0_u64;
    let mut scanned_blocks = 0_u64;
    let mut more_candidates = false;
    let payload_references = engine.payload_segment_references().to_vec();
    let loose_shards = match engine.block_store().loose_block_shards() {
        Ok(shards) => shards,
        Err(error) => {
            let _ = live.cleanup();
            return Err(error);
        }
    };
    let catalog_has_capacity = payload_references.len() < MAX_PAYLOAD_SEGMENTS;
    let compaction_shards = if catalog_has_capacity {
        loose_shards
            .into_iter()
            .filter(|shard| {
                payload_references
                    .iter()
                    .filter(|reference| reference.shard == *shard)
                    .count()
                    < MAX_PAYLOAD_SEGMENTS_PER_SHARD
            })
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let payload_compaction_shard = (!compaction_shards.is_empty())
        .then(|| compaction_shards[engine.state().generation as usize % compaction_shards.len()]);
    let mut already_segmented = BTreeSet::new();
    if let Some(shard) = payload_compaction_shard {
        for reference in payload_references
            .iter()
            .filter(|reference| reference.shard == shard)
        {
            let block_ids = match engine.payload_segment_block_ids(reference) {
                Ok(block_ids) => block_ids,
                Err(error) => {
                    let _ = live.cleanup();
                    return Err(error);
                }
            };
            already_segmented.extend(block_ids);
        }
    }
    let mut live_loose_blocks_by_shard = [0_u32; 256];
    let mut payload_compaction_ids = Vec::new();
    let mut payload_compaction_bytes = 0_u64;
    let payload_compaction_byte_limit = budget
        .maximum_temporary_bytes
        .min(MAX_SEGMENT_PAYLOAD_BYTES);
    let mut more_payload_compaction_candidates = false;
    let scan_result = engine.block_store().visit_block_shards(
        budget.maximum_physical_blocks,
        |shard, block_ids| {
            let live_for_shard = live.live_for_shard(shard)?;
            for block_id in block_ids {
                scanned_blocks = scanned_blocks
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("authority GC scanned-block count overflow"))?;
                if live_for_shard.binary_search(&block_id).is_ok() {
                    live_loose_blocks_by_shard[shard as usize] =
                        live_loose_blocks_by_shard[shard as usize].saturating_add(1);
                    if Some(shard) == payload_compaction_shard
                        && !already_segmented.contains(&block_id)
                    {
                        let block_bytes = engine.block_store().block_file_bytes(block_id)?;
                        let fits = payload_compaction_ids.len() < MAX_SEGMENT_BLOCKS
                            && payload_compaction_bytes
                                .checked_add(block_bytes)
                                .is_some_and(|bytes| bytes <= payload_compaction_byte_limit);
                        if fits {
                            payload_compaction_ids.push(block_id);
                            payload_compaction_bytes += block_bytes;
                        } else {
                            more_payload_compaction_candidates = true;
                        }
                    }
                    continue;
                }
                let block_bytes = engine.block_store().block_file_bytes(block_id)?;
                let fits = candidates.len() < budget.maximum_blocks_per_round
                    && candidate_bytes
                        .checked_add(block_bytes)
                        .is_some_and(|bytes| bytes <= budget.maximum_victim_bytes);
                if fits {
                    candidates.push(block_id);
                    candidate_bytes += block_bytes;
                } else {
                    more_candidates = true;
                }
            }
            Ok(())
        },
    );
    if let Err(error) = scan_result {
        let _ = live.cleanup();
        return Err(error);
    }
    let payload_compaction_ready =
        payload_compaction_ids.len() >= MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS;
    let other_compaction_shard_may_be_ready = compaction_shards.iter().any(|shard| {
        Some(*shard) != payload_compaction_shard
            && live_loose_blocks_by_shard[*shard as usize]
                >= MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS as u32
    });
    let payload_compaction_catalog_blocked =
        live_loose_blocks_by_shard
            .iter()
            .enumerate()
            .any(|(shard, count)| {
                *count >= MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS as u32
                    && (!catalog_has_capacity
                        || payload_references
                            .iter()
                            .filter(|reference| reference.shard == shard as u8)
                            .count()
                            >= MAX_PAYLOAD_SEGMENTS_PER_SHARD)
            });
    more_candidates |= more_payload_compaction_candidates
        || other_compaction_shard_may_be_ready
        || payload_compaction_catalog_blocked;
    if !candidates.is_empty() && !engine.payload_segment_references().is_empty() {
        more_candidates = true;
    }

    let orphan_plan = match engine.plan_payload_segment_orphan() {
        Ok(plan) => plan,
        Err(error) => {
            let _ = live.cleanup();
            return Err(error);
        }
    };
    more_candidates |= orphan_plan.more_candidates;
    if orphan_plan.candidate.is_some() {
        if !candidates.is_empty()
            || !engine.payload_segment_references().is_empty()
            || payload_compaction_ready
        {
            more_candidates = true;
        }
        if candidates.is_empty() {
            let candidate = orphan_plan.candidate.as_ref().unwrap();
            if mode == AuthorityGarbageCollectionMode::Plan {
                live.cleanup()?;
                return Ok(AuthorityGarbageCollectionMetrics {
                    scanned_blocks,
                    scanned_loose_block_entries: temporary_plan.scanned_entries,
                    live_references: mark_metrics.references,
                    spill_bytes: mark_metrics.spill_bytes,
                    scanned_payload_segment_files: orphan_plan.scanned_files,
                    candidate_orphan_segment_files: 1,
                    candidate_orphan_segment_bytes: candidate.file_bytes,
                    payload_compaction_shard,
                    candidate_payload_compaction_blocks: payload_compaction_ids.len() as u32,
                    candidate_payload_compaction_bytes: payload_compaction_bytes,
                    payload_compaction_catalog_blocked,
                    more_candidates,
                    ..AuthorityGarbageCollectionMetrics::default()
                });
            }
            if let Err(error) = engine.stabilize_control_slots() {
                let _ = live.cleanup();
                return Err(error);
            }
            let removal = engine.remove_payload_segment_orphan(candidate);
            let cleanup = live.cleanup();
            let removed_bytes = removal?;
            cleanup?;
            return Ok(AuthorityGarbageCollectionMetrics {
                scanned_blocks,
                scanned_loose_block_entries: temporary_plan.scanned_entries,
                live_references: mark_metrics.references,
                spill_bytes: mark_metrics.spill_bytes,
                scanned_payload_segment_files: orphan_plan.scanned_files,
                candidate_orphan_segment_files: 1,
                candidate_orphan_segment_bytes: candidate.file_bytes,
                removed_orphan_segment_files: 1,
                removed_orphan_segment_bytes: removed_bytes,
                payload_compaction_shard,
                candidate_payload_compaction_blocks: payload_compaction_ids.len() as u32,
                candidate_payload_compaction_bytes: payload_compaction_bytes,
                payload_compaction_catalog_blocked,
                more_candidates,
                ..AuthorityGarbageCollectionMetrics::default()
            });
        }
    }

    let mut scanned_payload_segments = 0_u32;
    let mut segment_budget_blocked = false;
    let mut segment_candidate = None;
    let mut candidate_segment_blocks = 0_u32;
    let mut candidate_segment_dead_blocks = 0_u32;
    let mut candidate_segment_bytes = 0_u64;
    if candidates.is_empty() {
        let references = payload_references.clone();
        let mut shards = references
            .iter()
            .map(|reference| reference.shard)
            .collect::<Vec<_>>();
        shards.dedup();
        let selected_shard = (!shards.is_empty()).then(|| {
            let position = engine.state().generation as usize % shards.len();
            shards[position]
        });
        let selected_references = references
            .iter()
            .filter(|reference| Some(reference.shard) == selected_shard)
            .collect::<Vec<_>>();
        let mut cached_shard = None;
        let mut cached_live = Vec::new();
        for (index, reference) in selected_references.iter().enumerate() {
            scanned_payload_segments += 1;
            let block_ids = match engine.payload_segment_block_ids(reference) {
                Ok(block_ids) => block_ids,
                Err(error) => {
                    let _ = live.cleanup();
                    return Err(error);
                }
            };
            if cached_shard != Some(reference.shard) {
                cached_live = match live.live_for_shard(reference.shard) {
                    Ok(live_ids) => live_ids,
                    Err(error) => {
                        let _ = live.cleanup();
                        return Err(error);
                    }
                };
                cached_shard = Some(reference.shard);
            }
            let retained_ids = block_ids
                .iter()
                .copied()
                .filter(|block_id| cached_live.binary_search(block_id).is_ok())
                .collect::<Vec<_>>();
            let dead_blocks = block_ids.len() - retained_ids.len();
            if dead_blocks == 0 {
                continue;
            }
            if candidate_segment_blocks == 0 {
                candidate_segment_blocks = reference.block_count;
                candidate_segment_dead_blocks = dead_blocks as u32;
                candidate_segment_bytes = reference.file_bytes;
            }
            if !retained_ids.is_empty() && reference.file_bytes > budget.maximum_temporary_bytes {
                segment_budget_blocked = true;
                more_candidates = true;
                continue;
            }
            candidate_segment_blocks = reference.block_count;
            candidate_segment_dead_blocks = dead_blocks as u32;
            candidate_segment_bytes = reference.file_bytes;
            segment_candidate = Some((**reference, retained_ids, dead_blocks as u32));
            if index + 1 < selected_references.len() || shards.len() > 1 {
                more_candidates = true;
            }
            break;
        }
        if segment_candidate.is_none() && shards.len() > 1 {
            more_candidates = true;
        }
        debug_assert!(selected_references.len() <= MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED);
    }

    if segment_candidate.is_some() && payload_compaction_ready {
        more_candidates = true;
    }

    if mode == AuthorityGarbageCollectionMode::Plan
        || (candidates.is_empty() && segment_candidate.is_none() && !payload_compaction_ready)
    {
        if mode == AuthorityGarbageCollectionMode::Execute && more_candidates {
            if let Err(error) = engine.stabilize_control_slots() {
                let _ = live.cleanup();
                return Err(error);
            }
        }
        live.cleanup()?;
        return Ok(AuthorityGarbageCollectionMetrics {
            scanned_blocks,
            scanned_loose_block_entries: temporary_plan.scanned_entries,
            live_references: mark_metrics.references,
            spill_bytes: mark_metrics.spill_bytes,
            candidate_blocks: candidates.len() as u32,
            candidate_bytes,
            scanned_payload_segment_files: orphan_plan.scanned_files,
            candidate_orphan_segment_files: u32::from(orphan_plan.candidate.is_some()),
            candidate_orphan_segment_bytes: orphan_plan
                .candidate
                .as_ref()
                .map_or(0, |candidate| candidate.file_bytes),
            payload_compaction_shard,
            candidate_payload_compaction_blocks: payload_compaction_ids.len() as u32,
            candidate_payload_compaction_bytes: payload_compaction_bytes,
            payload_compaction_catalog_blocked,
            scanned_payload_segments,
            candidate_segment_blocks,
            candidate_segment_dead_blocks,
            candidate_segment_bytes,
            segment_budget_blocked,
            more_candidates,
            ..AuthorityGarbageCollectionMetrics::default()
        });
    }

    if !candidates.is_empty() {
        if let Err(error) = engine.stabilize_control_slots() {
            let _ = live.cleanup();
            return Err(error);
        }
        let removal = engine.block_store().remove_blocks(&candidates);
        let cleanup = live.cleanup();
        let removal = removal?;
        cleanup?;
        return Ok(AuthorityGarbageCollectionMetrics {
            scanned_blocks,
            scanned_loose_block_entries: temporary_plan.scanned_entries,
            live_references: mark_metrics.references,
            spill_bytes: mark_metrics.spill_bytes,
            candidate_blocks: candidates.len() as u32,
            candidate_bytes,
            removed_blocks: removal.blocks_removed as u32,
            removed_bytes: removal.bytes_removed,
            scanned_payload_segment_files: orphan_plan.scanned_files,
            candidate_orphan_segment_files: u32::from(orphan_plan.candidate.is_some()),
            candidate_orphan_segment_bytes: orphan_plan
                .candidate
                .as_ref()
                .map_or(0, |candidate| candidate.file_bytes),
            payload_compaction_shard,
            candidate_payload_compaction_blocks: payload_compaction_ids.len() as u32,
            candidate_payload_compaction_bytes: payload_compaction_bytes,
            payload_compaction_catalog_blocked,
            more_candidates,
            ..AuthorityGarbageCollectionMetrics::default()
        });
    }

    if segment_candidate.is_none() {
        live.cleanup()?;
        let compaction = engine.compact_payload_blocks(&payload_compaction_ids)?;
        return Ok(AuthorityGarbageCollectionMetrics {
            scanned_blocks,
            scanned_loose_block_entries: temporary_plan.scanned_entries,
            live_references: mark_metrics.references,
            spill_bytes: mark_metrics.spill_bytes,
            scanned_payload_segment_files: orphan_plan.scanned_files,
            payload_compaction_shard,
            candidate_payload_compaction_blocks: payload_compaction_ids.len() as u32,
            candidate_payload_compaction_bytes: payload_compaction_bytes,
            compacted_payload_blocks: compaction.blocks_packed,
            compacted_payload_bytes: compaction.payload_bytes,
            compacted_segment_bytes: compaction.segment_file_bytes,
            compacted_loose_bytes_reclaimed: compaction.loose_bytes_reclaimed,
            payload_compaction_catalog_blocked,
            more_candidates,
            ..AuthorityGarbageCollectionMetrics::default()
        });
    }

    let (reference, retained_ids, dead_blocks) = segment_candidate.unwrap();
    live.cleanup()?;
    let segment_gc = engine.collect_payload_segment(reference.segment_id, &retained_ids)?;
    Ok(AuthorityGarbageCollectionMetrics {
        scanned_blocks,
        scanned_loose_block_entries: temporary_plan.scanned_entries,
        live_references: mark_metrics.references,
        spill_bytes: mark_metrics.spill_bytes,
        scanned_payload_segments,
        candidate_segment_blocks: reference.block_count,
        candidate_segment_dead_blocks: dead_blocks,
        candidate_segment_bytes: reference.file_bytes,
        rewritten_payload_segments: u32::from(segment_gc.replacement_segment_id.is_some()),
        retired_payload_segments: u32::from(segment_gc.replacement_segment_id.is_none()),
        replacement_segment_bytes: segment_gc.replacement_file_bytes,
        removed_segment_bytes: segment_gc.retired_file_bytes,
        payload_compaction_shard,
        candidate_payload_compaction_blocks: payload_compaction_ids.len() as u32,
        candidate_payload_compaction_bytes: payload_compaction_bytes,
        payload_compaction_catalog_blocked,
        segment_budget_blocked,
        more_candidates,
        ..AuthorityGarbageCollectionMetrics::default()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::authority::AuthorityDatabase;
    use crate::block::BlockId;
    use crate::entity::EntityKey;
    use crate::stream::{StreamEvent, StreamKey};
    use crate::vfs::{
        sync_all_barrier, sync_directory_barrier, DeterministicVfs, FaultAction, FaultRule,
        OpenRequest, Operation,
    };

    fn test_budget() -> AuthorityGarbageCollectionBudget {
        AuthorityGarbageCollectionBudget {
            maximum_victim_bytes: MAX_AUTHORITY_GC_VICTIM_BYTES,
            maximum_temporary_bytes: LEAN_AUTHORITY_GC_TEMPORARY_BYTES,
            maximum_blocks_per_round: MAX_AUTHORITY_GC_BLOCKS_PER_ROUND,
            maximum_physical_blocks: MAX_AUTHORITY_GC_PHYSICAL_BLOCKS,
        }
    }

    fn authority_with_orphan() -> (Arc<DeterministicVfs>, AuthorityDatabase, BlockId, EntityKey) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let key = EntityKey::new(7, 11, "record", b"live").unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
            .unwrap();
        database.commit(transaction).unwrap();
        let orphan = database
            .write_orphan_block_for_test(b"unreferenced-block")
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, database, orphan, key)
    }

    fn assert_live_value(database: &AuthorityDatabase, key: &EntityKey) {
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            database.entity_get(&mut transaction, key).unwrap(),
            Some(b"live-value".to_vec())
        );
    }

    fn create_temporary_block_file(vfs: &DeterministicVfs, block_id: BlockId) {
        let shard = block_id.to_hex()[..2].to_owned();
        let shard_path = Path::new("/data/blocks").join(shard);
        let temporary_path = shard_path.join(format!(
            ".new-{}",
            uuid::Uuid::from_bytes([0x3c; 16]).simple()
        ));
        let mut file = vfs
            .open(
                &temporary_path,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(0, b"torn-block-prefix").unwrap();
        sync_all_barrier(file.as_mut()).unwrap();
        sync_directory_barrier(vfs, &shard_path).unwrap();
    }

    fn two_payloads_in_one_shard() -> (Vec<u8>, Vec<u8>) {
        let mut first_by_shard = std::collections::BTreeMap::<u8, Vec<u8>>::new();
        for value in 0..10_000_u32 {
            let payload = format!("authority-gc-segment-{value}").into_bytes();
            let shard = BlockId::for_payload(&payload).0[0];
            if let Some(first) = first_by_shard.remove(&shard) {
                return (first, payload);
            }
            first_by_shard.insert(shard, payload);
        }
        panic!("failed to find same-shard authority GC fixtures");
    }

    fn two_payloads_in_different_shards() -> (Vec<u8>, Vec<u8>) {
        let first = b"authority-gc-live-shard".to_vec();
        for value in 0..10_000_u32 {
            let second = format!("authority-gc-dead-shard-{value}").into_bytes();
            if BlockId::for_payload(&first).0[0] != BlockId::for_payload(&second).0[0] {
                return (first, second);
            }
        }
        panic!("failed to find different-shard authority GC fixtures");
    }

    fn payloads_in_one_shard(count: usize) -> Vec<Vec<u8>> {
        let mut by_shard = std::collections::BTreeMap::<u8, Vec<Vec<u8>>>::new();
        for value in 0..1_000_000_u32 {
            let payload = format!("authority-gc-auto-compaction-{value}").into_bytes();
            let shard = BlockId::for_payload(&payload).0[0];
            let payloads = by_shard.entry(shard).or_default();
            payloads.push(payload);
            if payloads.len() == count {
                return std::mem::take(payloads);
            }
        }
        panic!("failed to find enough same-shard payload compaction fixtures");
    }

    fn authority_with_partially_live_payload_segment() -> (
        Arc<DeterministicVfs>,
        AuthorityDatabase,
        BlockId,
        Vec<u8>,
        BlockId,
    ) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let (live_payload, dead_payload) = two_payloads_in_one_shard();
        let live_id = database
            .commit_block_payload_for_test(&live_payload)
            .unwrap();
        let dead_id = database.write_orphan_block_for_test(&dead_payload).unwrap();
        database
            .compact_payload_blocks_for_test(&[live_id, dead_id])
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, database, live_id, live_payload, dead_id)
    }

    #[test]
    fn bounded_gc_removes_only_orphans_and_defers_live_snapshots() {
        let (vfs, mut database, orphan, key) = authority_with_orphan();
        let snapshot = database.begin(7, 11).unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            database.collect_garbage(test_budget()).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert!(vfs.trace().unwrap().is_empty());
        drop(snapshot);

        let metrics = database.collect_garbage(test_budget()).unwrap();
        assert_eq!(metrics.candidate_blocks, 1);
        assert_eq!(metrics.removed_blocks, 1);
        assert!(!metrics.more_candidates);
        assert_eq!(
            database.read_block_for_test(orphan).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        assert_live_value(&database, &key);
        drop(database);
        vfs.crash().unwrap();
        let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_live_value(&reopened, &key);
    }

    #[test]
    fn temporary_loose_block_files_are_planned_and_removed_before_marking() {
        let (vfs, mut database, orphan, key) = authority_with_orphan();
        create_temporary_block_file(vfs.as_ref(), orphan);
        vfs.arm_fault(None).unwrap();

        let plan = database.plan_garbage_collection(test_budget()).unwrap();
        assert_eq!(plan.candidate_temporary_block_files, 1);
        assert_eq!(plan.removed_temporary_block_files, 0);
        assert_eq!(plan.scanned_blocks, 0);
        assert_eq!(plan.live_references, 0);
        let executed = database.collect_garbage(test_budget()).unwrap();
        assert_eq!(executed.removed_temporary_block_files, 1);
        assert_eq!(
            executed.removed_temporary_block_bytes,
            plan.candidate_temporary_block_bytes
        );
        assert!(executed.more_candidates);
        assert_eq!(
            database.read_block_for_test(orphan).unwrap(),
            b"unreferenced-block"
        );
        assert_live_value(&database, &key);
    }

    #[test]
    fn every_temporary_block_removal_fault_preserves_live_authority() {
        let (baseline_vfs, mut baseline, orphan, _) = authority_with_orphan();
        create_temporary_block_file(baseline_vfs.as_ref(), orphan);
        baseline_vfs.arm_fault(None).unwrap();
        baseline.collect_garbage(test_budget()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(trace.contains(&Operation::SyncDirectory));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if matches!(operation, Operation::SyncDirectory) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut database, orphan, key) = authority_with_orphan();
            create_temporary_block_file(vfs.as_ref(), orphan);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = database.collect_garbage(test_budget());
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_live_value(&reopened, &key);
        }
    }

    #[test]
    fn authority_gc_plans_and_rewrites_one_partially_live_payload_segment() {
        let (vfs, mut database, live_id, live_payload, dead_id) =
            authority_with_partially_live_payload_segment();
        let plan = database.plan_garbage_collection(test_budget()).unwrap();
        assert_eq!(plan.candidate_blocks, 0);
        assert_eq!(plan.scanned_payload_segments, 1);
        assert_eq!(plan.candidate_segment_blocks, 2);
        assert_eq!(plan.candidate_segment_dead_blocks, 1);
        assert_eq!(plan.rewritten_payload_segments, 0);
        assert_eq!(database.payload_segment_count_for_test(), 1);

        let executed = database.collect_garbage(test_budget()).unwrap();
        assert_eq!(executed.rewritten_payload_segments, 1);
        assert_eq!(executed.retired_payload_segments, 0);
        assert_eq!(database.read_block_for_test(live_id).unwrap(), live_payload);
        assert_eq!(
            database.read_block_for_test(dead_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        drop(database);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.read_block_for_test(live_id).unwrap(), live_payload);
    }

    #[test]
    fn authority_gc_retires_one_fully_dead_payload_segment_without_rewrite_space() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let orphan = database
            .write_orphan_block_for_test(&vec![0x5a; 4 * 1024 * 1024])
            .unwrap();
        database.compact_payload_blocks_for_test(&[orphan]).unwrap();
        let mut budget = test_budget();
        budget.maximum_temporary_bytes = 3 * 1024 * 1024;
        let metrics = database.collect_garbage(budget).unwrap();
        assert_eq!(metrics.retired_payload_segments, 1);
        assert_eq!(metrics.rewritten_payload_segments, 0);
        assert_eq!(database.payload_segment_count_for_test(), 0);
        assert_eq!(
            database.read_block_for_test(orphan).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn executed_empty_shard_advances_until_a_later_shard_is_reclaimed() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let (live_payload, dead_payload) = two_payloads_in_different_shards();
        let live_id = database
            .commit_block_payload_for_test(&live_payload)
            .unwrap();
        let dead_id = database.write_orphan_block_for_test(&dead_payload).unwrap();
        database
            .compact_payload_blocks_for_test(&[live_id])
            .unwrap();
        database
            .compact_payload_blocks_for_test(&[dead_id])
            .unwrap();
        vfs.arm_fault(None).unwrap();

        let mut retired_segments = 0;
        for _ in 0..8 {
            let metrics = database.collect_garbage(test_budget()).unwrap();
            assert!(
                metrics.scanned_payload_segments as usize
                    <= MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED
            );
            retired_segments += metrics.retired_payload_segments;
            if retired_segments == 1 {
                break;
            }
            assert!(metrics.more_candidates);
        }
        assert_eq!(retired_segments, 1);
        assert_eq!(database.read_block_for_test(live_id).unwrap(), live_payload);
        assert_eq!(
            database.read_block_for_test(dead_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn authority_gc_plans_and_removes_one_unpublished_payload_segment() {
        let (vfs, mut database, orphan, key) = authority_with_orphan();
        database
            .create_orphan_payload_segment_for_test(orphan)
            .unwrap();
        let loose_round = database.collect_garbage(test_budget()).unwrap();
        assert_eq!(loose_round.removed_blocks, 1);
        assert!(loose_round.more_candidates);

        let plan = database.plan_garbage_collection(test_budget()).unwrap();
        assert_eq!(plan.candidate_orphan_segment_files, 1);
        assert!(plan.candidate_orphan_segment_bytes > 0);
        assert_eq!(plan.removed_orphan_segment_files, 0);
        let executed = database.collect_garbage(test_budget()).unwrap();
        assert_eq!(executed.removed_orphan_segment_files, 1);
        assert_eq!(
            executed.removed_orphan_segment_bytes,
            plan.candidate_orphan_segment_bytes
        );
        assert_live_value(&database, &key);
        drop(database);
        vfs.crash().unwrap();
        assert_live_value(
            &AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap(),
            &key,
        );
    }

    #[test]
    fn every_orphan_segment_removal_fault_preserves_live_authority() {
        let (baseline_vfs, mut baseline, orphan, _) = authority_with_orphan();
        baseline
            .create_orphan_payload_segment_for_test(orphan)
            .unwrap();
        baseline.collect_garbage(test_budget()).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.collect_garbage(test_budget()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(trace.contains(&Operation::SyncDirectory));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if matches!(operation, Operation::SyncDirectory) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut database, orphan, key) = authority_with_orphan();
            database
                .create_orphan_payload_segment_for_test(orphan)
                .unwrap();
            database.collect_garbage(test_budget()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = database.collect_garbage(test_budget());
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_live_value(&reopened, &key);
        }
    }

    #[test]
    fn authority_gc_automatically_compacts_one_profitable_live_loose_shard() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let payloads = payloads_in_one_shard(MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS);
        let block_ids = database.commit_block_payloads_for_test(&payloads).unwrap();
        vfs.arm_fault(None).unwrap();

        let initial_plan = database.plan_garbage_collection(test_budget()).unwrap();
        assert_eq!(initial_plan.compacted_payload_blocks, 0);
        let mut compacted = None;
        for _ in 0..=256 {
            let metrics = database.collect_garbage(test_budget()).unwrap();
            if metrics.compacted_payload_blocks > 0 {
                compacted = Some(metrics);
                break;
            }
            assert!(metrics.more_candidates);
        }
        let metrics = compacted.expect("generation cursor must reach the profitable loose shard");
        assert_eq!(
            metrics.compacted_payload_blocks as usize,
            MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS
        );
        assert!(metrics.compacted_loose_bytes_reclaimed > metrics.compacted_segment_bytes);
        assert_eq!(database.payload_segment_count_for_test(), 1);
        for (block_id, payload) in block_ids.iter().zip(&payloads) {
            assert_eq!(database.read_block_for_test(*block_id).unwrap(), *payload);
        }
        drop(database);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        for (block_id, payload) in block_ids.iter().zip(&payloads) {
            assert_eq!(reopened.read_block_for_test(*block_id).unwrap(), *payload);
        }
    }

    #[test]
    fn authority_gc_does_not_compact_below_the_file_count_benefit_floor() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let payloads = payloads_in_one_shard(MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS - 1);
        database.commit_block_payloads_for_test(&payloads).unwrap();
        vfs.arm_fault(None).unwrap();

        for _ in 0..=256 {
            let metrics = database.collect_garbage(test_budget()).unwrap();
            assert_eq!(metrics.compacted_payload_blocks, 0);
            if !metrics.more_candidates {
                return;
            }
        }
        panic!("sub-threshold loose blocks never converged to an idle GC plan");
    }

    #[test]
    fn every_authority_segment_gc_fault_preserves_live_references() {
        let (baseline_vfs, mut baseline, _, _, _) = authority_with_partially_live_payload_segment();
        baseline.collect_garbage(test_budget()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(trace.contains(&Operation::Rename));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(7))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut database, live_id, live_payload, _) =
                authority_with_partially_live_payload_segment();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = database.collect_garbage(test_budget());
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.read_block_for_test(live_id).unwrap(), live_payload);
        }
    }

    #[test]
    fn gc_preserves_semantic_blocks_after_their_transaction_history_is_retired() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let stream_key = StreamKey::new(7, 11, "events", b"gc-live-stream").unwrap();
        let mut initial = database.begin(7, 11).unwrap();
        database
            .stream_append(
                &mut initial,
                stream_key.clone(),
                1,
                vec![StreamEvent {
                    created_at_ms: 1,
                    event_type: "turn.created".to_owned(),
                    payload: vec![5; 16 * 1024],
                }],
            )
            .unwrap();
        let mut state = 0x1234_5678_u32;
        let response = (0..10_000)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                state as u8
            })
            .collect::<Vec<_>>();
        database
            .receipt_insert(
                &mut initial,
                "gc-live-receipt",
                "record.put",
                [6; 32],
                &response,
                1,
            )
            .unwrap();
        database.commit(initial).unwrap();
        database.checkpoint_for_test().unwrap();
        for index in 0..2_u8 {
            let mut later = database.begin(7, 11).unwrap();
            database
                .entity_put(
                    &mut later,
                    EntityKey::new(7, 11, "record", &[index]).unwrap(),
                    vec![index],
                )
                .unwrap();
            database.commit(later).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        assert_eq!(database.compact_history(1).unwrap().retired_segments, 2);
        let orphan = database
            .write_orphan_block_for_test(b"semantic-gc-orphan")
            .unwrap();
        assert!(
            database
                .collect_garbage(test_budget())
                .unwrap()
                .removed_blocks
                >= 1
        );
        assert!(database.read_block_for_test(orphan).is_err());
        let page = database.stream_read(7, 11, &stream_key, 1, 10).unwrap();
        assert_eq!(page.events[0].event.payload, vec![5; 16 * 1024]);
        let mut receipt = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .receipt_lookup(&mut receipt, "gc-live-receipt", "record.put", [6; 32])
                .unwrap(),
            Some(response.clone())
        );
        drop(receipt);
        drop(database);

        let database = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(
            database
                .stream_read(7, 11, &stream_key, 1, 10)
                .unwrap()
                .events[0]
                .event
                .payload,
            vec![5; 16 * 1024]
        );
        let mut receipt = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .receipt_lookup(&mut receipt, "gc-live-receipt", "record.put", [6; 32])
                .unwrap(),
            Some(response)
        );
    }

    #[test]
    fn gc_temporary_budget_is_launch_probe_derived_and_hard_capped() {
        let snapshot = |free| crate::resource_probe::LaunchResourceSnapshot {
            logical_cpus: Some(4),
            memory_capacity_bytes: Some(8 * 1024 * 1024 * 1024),
            memory_headroom_bytes: Some(4 * 1024 * 1024 * 1024),
            volume_free_bytes: free,
        };
        let capped = AuthorityGarbageCollectionBudget::from_resource_budget(
            DaemonResourceBudget::from_snapshot(snapshot(Some(100 * 1024 * 1024 * 1024))),
        );
        assert_eq!(capped.maximum_temporary_bytes, 1024 * 1024 * 1024);
        let proportional = AuthorityGarbageCollectionBudget::from_resource_budget(
            DaemonResourceBudget::from_snapshot(snapshot(Some(1024 * 1024 * 1024))),
        );
        assert_eq!(proportional.maximum_temporary_bytes, 21_474_836);
        let lean = AuthorityGarbageCollectionBudget::from_resource_budget(
            DaemonResourceBudget::from_snapshot(Default::default()),
        );
        assert_eq!(lean.maximum_temporary_bytes, 64 * 1024 * 1024);
    }

    #[test]
    fn victim_bytes_bound_requires_repeatable_rounds() {
        let (vfs, mut database, first, key) = authority_with_orphan();
        let second = database
            .write_orphan_block_for_test(&vec![3; 1024 * 1024])
            .unwrap();
        let budget = AuthorityGarbageCollectionBudget {
            maximum_victim_bytes: 1024 * 1024 + 60,
            ..test_budget()
        };
        let first_round = database.collect_garbage(budget).unwrap();
        assert_eq!(first_round.removed_blocks, 1);
        assert!(first_round.more_candidates);
        let second_round = database.collect_garbage(budget).unwrap();
        assert_eq!(second_round.removed_blocks, 1);
        assert!(!second_round.more_candidates);
        assert!(database.read_block_for_test(first).is_err());
        assert!(database.read_block_for_test(second).is_err());
        assert_live_value(&database, &key);
        drop(database);
        vfs.crash().unwrap();
        assert_live_value(
            &AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap(),
            &key,
        );
    }

    #[test]
    fn every_gc_fault_preserves_all_live_authority_blocks() {
        let (baseline_vfs, mut baseline, _, _) = authority_with_orphan();
        baseline.collect_garbage(test_budget()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(trace.contains(&Operation::Write));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                cases.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
            if matches!(
                operation,
                Operation::SyncAll | Operation::SyncDirectory | Operation::SyncData
            ) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut database, _, key) = authority_with_orphan();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = database.collect_garbage(test_budget());
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_live_value(&reopened, &key);
        }
    }
}
