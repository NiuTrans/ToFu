//! Offline authority verification and disaster salvage.
//!
//! `verify` walks every durable structure read-only and reports exactly
//! what is provably intact: CONTROL slots, the active WAL hash chain (via
//! the never-truncating `ActiveLog::scan`), history manifest and segments,
//! the payload catalog and segment indexes, and the full entity graph with
//! content-hash verification. `salvage` extracts everything verifiably
//! reachable from the newest valid CONTROL anchor into backup layout,
//! skipping unreadable blocks with an explicit loss list — it never
//! guesses: an unusable CONTROL pair fails closed. Neither command repairs
//! the source.

use std::io;
use std::path::Path;

use crate::authority::AuthorityDatabase;
use crate::backup::{self, BackupManifest};
use crate::block::BlockId;
use crate::control::ControlFile;
use crate::engine::Engine;
use crate::entity::visit_entity_page_graph_with_values;
use crate::history::decode_segment;
use crate::wal::ActiveLog;

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct WalVerifySection {
    pub valid_records: u64,
    pub valid_bytes: u64,
    pub truncated_tail_bytes: u64,
    pub fatal: Option<String>,
}

#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct PayloadVerifySection {
    pub segments_total: u32,
    pub segments_ok: u32,
    pub failed_segment_ids: Vec<String>,
    pub loose_blocks_verified: u64,
    pub corrupt_block_ids: Vec<String>,
    pub fatal: Option<String>,
}

#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct GraphVerifySection {
    pub pages_verified: u64,
    pub semantic_references_verified: u64,
    pub history_blocks_verified: u64,
    pub wal_blocks_verified: u64,
    pub missing_block_ids: Vec<String>,
    pub fatal: Option<String>,
}

/// Sequential calling-thread phases; decode work includes nested recovery.
/// Verify includes authority teardown; salvage keeps the returned authority open.
/// Do not sum these durations with overlapping process/component stage totals.
#[derive(Clone, Debug, serde::Serialize)]
pub struct VerifyPhaseTiming {
    pub phase: &'static str,
    pub elapsed_ns: u64,
    pub history_decode_attempts: u64,
    pub history_decode_source_bytes: u64,
    pub history_decoded_transactions: u64,
}

struct VerifyPhaseTimer<'a> {
    output: &'a mut Vec<VerifyPhaseTiming>,
    phase: &'static str,
    started: std::time::Instant,
    before: crate::history::HistoryDecodeWork,
    _stage: crate::fsync_stats::StageTimer,
}

impl<'a> VerifyPhaseTimer<'a> {
    fn new(output: &'a mut Vec<VerifyPhaseTiming>, phase: &'static str, stage: usize) -> Self {
        Self { output, phase, started: std::time::Instant::now(),
            before: crate::history::decode_work(), _stage: crate::fsync_stats::StageTimer::new(stage) }
    }
}

impl Drop for VerifyPhaseTimer<'_> {
    fn drop(&mut self) {
        let work = crate::history::decode_work().since(self.before);
        self.output.push(VerifyPhaseTiming { phase: self.phase,
            elapsed_ns: self.started.elapsed().as_nanos().min(u64::MAX as u128) as u64,
            history_decode_attempts: work.attempts, history_decode_source_bytes: work.source_bytes,
            history_decoded_transactions: work.transactions });
    }
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct VerifyReport {
    pub format: &'static str,
    pub verdict: &'static str,
    pub control_generation: Option<u64>,
    pub durable_sequence: Option<u64>,
    pub wal: WalVerifySection,
    pub payload: PayloadVerifySection,
    pub graph: GraphVerifySection,
    pub performance: Vec<VerifyPhaseTiming>,
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct SalvageReport {
    pub format: &'static str,
    pub verdict: &'static str,
    pub durable_sequence: u64,
    pub copied_blocks: u64,
    pub copied_block_bytes: u64,
    pub manifest_bytes: u64,
    pub lost_block_ids: Vec<String>,
    pub verify: Box<VerifyReport>,
}

/// Full read-only verification of one offline authority. Individual
/// section failures degrade the verdict instead of aborting the walk;
/// `fatal` is reserved for an unusable CONTROL pair or an authority root
/// that cannot be read at all.
pub fn verify_authority(data_dir: &Path) -> io::Result<VerifyReport> {
    let (mut report, database) = verify_authority_inner(data_dir, false)?;
    if database.is_some() {
        let _phase = VerifyPhaseTimer::new(&mut report.performance, "close", crate::fsync_stats::STAGE_VERIFY_CLOSE);
        drop(database);
    }
    Ok(report)
}

fn verify_authority_inner(
    data_dir: &Path,
    _unused: bool,
) -> io::Result<(VerifyReport, Option<AuthorityDatabase>)> {
    let mut performance = Vec::with_capacity(8);
    let result = verify_authority_timed(data_dir, &mut performance);
    result.map(|(mut report, database)| {
        report.performance = performance;
        (report, database)
    })
}

fn verify_authority_timed(
    data_dir: &Path,
    performance: &mut Vec<VerifyPhaseTiming>,
) -> io::Result<(VerifyReport, Option<AuthorityDatabase>)> {
    let mut report = VerifyReport {
        format: "tofu-db.verify.v1",
        verdict: "ok",
        control_generation: None,
        durable_sequence: None,
        wal: WalVerifySection::default(),
        payload: PayloadVerifySection::default(),
        graph: GraphVerifySection::default(),
        performance: Vec::new(),
    };
    let mut degrade = |report: &mut VerifyReport| {
        if report.verdict == "ok" {
            report.verdict = "degraded";
        }
    };

    let phase = VerifyPhaseTimer::new(performance, "control", crate::fsync_stats::STAGE_VERIFY_CONTROL);
    // CONTROL: the alternating slots select the newest valid state.
    let control_state = match ControlFile::open(data_dir).and_then(|mut control| control.read_current()) {
        Ok(state) => state,
        Err(error) => {
            report.verdict = "fatal";
            report.graph.fatal = Some(format!("CONTROL unreadable: {error}"));
            return Ok((report, None));
        }
    };
    report.control_generation = Some(control_state.generation);
    report.durable_sequence = Some(control_state.durable_sequence);

    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "wal", crate::fsync_stats::STAGE_VERIFY_WAL);
    // CONTROL-selected sealed predecessor and active successor: never truncate.
    match ActiveLog::open_generation_with_vfs(
        data_dir,
        control_state.active_log_generation,
        std::sync::Arc::new(crate::vfs::RealVfs),
    )
    .and_then(|mut log| {
        crate::wal::scan_selected_generation_chain(data_dir, &control_state, &mut log,
            std::sync::Arc::new(crate::vfs::RealVfs))
    }) {
        Ok(selected) => {
            report.wal.valid_records = selected.recovery.records.len() as u64;
            report.wal.valid_bytes = selected.valid_bytes;
            report.wal.truncated_tail_bytes = selected.recovery.truncated_bytes;
            if selected.recovery.truncated_bytes > 0 {
                degrade(&mut report);
            }
        }
        Err(error) => {
            report.wal.fatal = Some(error.to_string());
            report.verdict = "fatal";
            return Ok((report, None));
        }
    }

    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "open", crate::fsync_stats::STAGE_VERIFY_OPEN);
    // The persona open replays deterministic recovery and the overlay; the
    // mandatory pre-backup merge makes the reported root the complete one
    // (docs/TOFUDB_WRITE_PATH_OVERLAY.md: merges are state-preserving).
    let mut database = match AuthorityDatabase::open(data_dir) {
        Ok(database) => database,
        Err(error) => {
            report.graph.fatal = Some(format!("authority open failed: {error}"));
            report.verdict = "fatal";
            return Ok((report, None));
        }
    };
    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "merge", crate::fsync_stats::STAGE_VERIFY_MERGE);
    if let Err(error) = database.merge_overlay_fully_for_operators() {
        report.graph.fatal = Some(format!("overlay merge failed: {error}"));
        report.verdict = "fatal";
        return Ok((report, None));
    }
    let engine = database.writer_engine()?;
    let blocks = engine.block_store_shared();

    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "payload", crate::fsync_stats::STAGE_VERIFY_PAYLOAD);
    // Payload catalog: every segment index verifies.
    for reference in engine.payload_segment_references() {
        report.payload.segments_total += 1;
        match blocks.verify_payload_segment(reference) {
            Ok(()) => report.payload.segments_ok += 1,
            Err(error) => {
                report
                    .payload
                    .failed_segment_ids
                    .push(format!("{}: {error}", reference.segment_id.simple()));
                degrade(&mut report);
            }
        }
    }

    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "loose", crate::fsync_stats::STAGE_VERIFY_LOOSE);
    // Loose block universe: every file on disk verifies CRC + content hash,
    // referenced or not (orphans and temporaries included).
    let mut loose_verified = 0_u64;
    let mut corrupt_ids: Vec<String> = Vec::new();
    let loose_result = blocks.visit_block_shards(
        crate::authority_gc::MAX_AUTHORITY_GC_PHYSICAL_BLOCKS,
        |_, block_ids| {
            for block_id in block_ids {
                match blocks.get(block_id) {
                    Ok(_) => loose_verified += 1,
                    Err(error) => {
                        corrupt_ids.push(format!("{}: {error}", block_id.to_hex()));
                    }
                }
            }
            Ok(())
        },
    );
    if let Err(error) = loose_result {
        report.payload.fatal = Some(format!("loose block scan failed: {error}"));
    }
    report.payload.loose_blocks_verified = loose_verified;
    if !corrupt_ids.is_empty() {
        report.payload.corrupt_block_ids = corrupt_ids;
        degrade(&mut report);
    }

    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "graph", crate::fsync_stats::STAGE_VERIFY_GRAPH);
    // Entity graph + semantic references: every read verifies content.
    let mut missing = Vec::new();
    let mut pages = 0_u64;
    let mut references = 0_u64;
    let graph_result = visit_entity_page_graph_with_values(
        engine.state().authority_state_root.flatten(),
        |block_id| blocks.get(block_id),
        |block_id, _| {
            pages += 1;
            let _ = block_id;
            Ok(())
        },
        |key, value| {
            backup::visit_semantic_value_blocks(&blocks, key, value, |block_id, _| {
                references += 1;
                let _ = block_id;
                Ok(())
            })
        },
    );
    if let Err(error) = graph_result {
        report.graph.fatal = Some(format!("entity graph walk failed: {error}"));
        report.verdict = "fatal";
        return Ok((report, Some(database)));
    }
    report.graph.pages_verified = pages;
    report.graph.semantic_references_verified = references;

    drop(phase);
    let phase = VerifyPhaseTimer::new(performance, "history", crate::fsync_stats::STAGE_VERIFY_HISTORY);
    // History projection + retained WAL envelopes: every referenced block resolves.
    let snapshot = engine
        .capture_online_gc_mark_snapshot(Vec::new())
        .map_err(|error| invalid_data(format!("snapshot capture failed: {error}")))?;
    let mut history_verified = 0_u64;
    let mut wal_verified = 0_u64;
    let mut degraded = false;
    {
        let blocks = &blocks;
        let missing = &mut missing;
        let mut probe = |block_id: BlockId, counter: &mut u64| {
            match blocks.get(block_id) {
                Ok(_) => *counter += 1,
                Err(error) => {
                    missing.push(format!("{}: {error}", block_id.to_hex()));
                    degraded = true;
                }
            }
        };
        if let Some(manifest_id) = snapshot.history_manifest_block_id {
            probe(manifest_id, &mut history_verified);
        }
        for reference in &snapshot.history_segments {
            probe(reference.block_id, &mut history_verified);
            if let Ok(segment_bytes) = blocks.get(reference.block_id) {
                if let Ok(transactions) = decode_segment(&segment_bytes, reference) {
                    for transaction in transactions {
                        for block_id in transaction.envelope.block_ids {
                            probe(block_id, &mut history_verified);
                        }
                    }
                }
            }
        }
        for id in &snapshot.history_payload_metadata_ids { probe(*id, &mut history_verified); }
        for block_id in &snapshot.committed_block_ids {
            probe(*block_id, &mut wal_verified);
        }
    }
    for reference in &snapshot.history_segments {
        if let Some(index_id) = reference.payload_index_block_id {
            let checked = (|| -> io::Result<()> {
                let source = blocks.get(reference.block_id)?;
                let expected = crate::history_payload::SegmentIndex::build(&source, reference)?.encode()?;
                if blocks.get(index_id)? != expected {
                    return Err(invalid_data("history payload offset index disagrees with authenticated source"));
                }
                Ok(())
            })();
            if let Err(error) = checked {
                missing.push(format!("{}: {error}", index_id.to_hex()));
                degraded = true;
            }
        }
    }
    report.graph.history_blocks_verified = history_verified;
    report.graph.wal_blocks_verified = wal_verified;
    if degraded {
        degrade(&mut report);
    }
    report.graph.missing_block_ids = missing;

    drop(phase);
    Ok((report, Some(database)))
}

/// Extracts everything verifiably reachable from the newest valid CONTROL
/// anchor into backup layout at `target_dir`. Unreadable blocks move to
/// the explicit loss list; the checksummed salvage manifest publishes
/// last, so the target is always restorable to a complete prefix.
pub fn salvage_authority(data_dir: &Path, target_dir: &Path) -> io::Result<SalvageReport> {
    let (verify, engine) = verify_authority_inner(data_dir, false)?;
    if verify.verdict == "fatal" {
        return Err(invalid_data(format!(
            "salvage refuses a fatally damaged authority: {}",
            verify
                .graph
                .fatal
                .clone()
                .or(verify.wal.fatal.clone())
                .unwrap_or_else(|| "unknown fatal condition".to_owned())
        )));
    }
    let Some(database) = engine else {
        return Err(invalid_data("salvage requires an openable authority"));
    };
    let engine = database.writer_engine()?;
    if target_dir.exists() && std::fs::read_dir(target_dir)?.next().is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "salvage target must be empty",
        ));
    }
    std::fs::create_dir_all(target_dir)?;

    let state = engine.state().clone();
    let manifest = BackupManifest {
        authority_uuid: state.authority_uuid,
        source_control_generation: state.generation,
        durable_sequence: state.durable_sequence,
        root_hash: state.root_hash,
        checkpoint_sequence: state.checkpoint_sequence,
        checkpoint_hash: state.checkpoint_hash,
        history_manifest_block_id: state.history_manifest_block_id,
        authority_state_root: state.authority_state_root,
    };
    let snapshot = engine.capture_online_gc_mark_snapshot(Vec::new())?;
    let blocks = engine.block_store_shared();

    let (destination, snapshots_path) = backup::initialize_or_open_backup(
        target_dir,
        std::sync::Arc::new(crate::vfs::RealVfs),
    )?;
    let _lease = backup::acquire_backup_lease(target_dir, &crate::vfs::RealVfs)?;

    // Copy with skip-on-error: every loss is explicit, never silent.
    let lost = std::cell::RefCell::new(verify.graph.missing_block_ids.clone());
    let copy_failed = std::cell::RefCell::new(false);
    let copy_block_lossy = |block_id: BlockId, payload: &[u8]| {
        if destination.put(payload).ok() != Some(block_id) {
            *copy_failed.borrow_mut() = true;
            let mut lost = lost.borrow_mut();
            let hex = block_id.to_hex();
            if !lost.iter().any(|candidate| candidate == &hex) {
                lost.push(hex);
            }
        }
    };
    let graph_result = visit_entity_page_graph_with_values(
        snapshot.authority_state_root,
        |block_id| blocks.get(block_id),
        |block_id, payload| {
            copy_block_lossy(block_id, payload);
            Ok(())
        },
        |key, value| {
            backup::visit_semantic_value_blocks(&blocks, key, value, |block_id, payload| {
                copy_block_lossy(block_id, payload);
                Ok(())
            })
        },
    );
    if graph_result.is_err() {
        // The degraded walk still copied everything above the failure.
        *copy_failed.borrow_mut() = true;
    }

    // History + retained WAL projections, skip-on-error.
    let copied_probe = |block_id: BlockId| -> bool {
        match blocks.get(block_id) {
            Ok(payload) => {
                copy_block_lossy(block_id, &payload);
                true
            }
            Err(_) => {
                let mut lost = lost.borrow_mut();
                let hex = block_id.to_hex();
                if !lost.iter().any(|candidate| candidate == &hex) {
                    lost.push(hex);
                }
                false
            }
        }
    };
    if let Some(manifest_id) = snapshot.history_manifest_block_id {
        copied_probe(manifest_id);
    }
    for reference in &snapshot.history_segments {
        if copied_probe(reference.block_id) {
            if let Ok(segment_bytes) = blocks.get(reference.block_id) {
                if let Ok(transactions) = decode_segment(&segment_bytes, reference) {
                    for transaction in transactions {
                        for block_id in transaction.envelope.block_ids {
                            copied_probe(block_id);
                        }
                    }
                }
            }
        }
    }
    for id in &snapshot.history_payload_metadata_ids { copied_probe(*id); }
    for block_id in &snapshot.committed_block_ids {
        copied_probe(*block_id);
    }
    let mut lost = lost.into_inner();
    let copy_failed = copy_failed.into_inner();

    let metrics = destination.write_metrics();
    let manifest_bytes = backup::publish_manifest(&snapshots_path, &manifest, &crate::vfs::RealVfs)?;
    let mut report = SalvageReport {
        format: "tofu-db.salvage.v1",
        verdict: if copy_failed || !lost.is_empty() {
            "degraded"
        } else {
            "ok"
        },
        durable_sequence: manifest.durable_sequence,
        copied_blocks: metrics.blocks_written,
        copied_block_bytes: metrics.bytes_written,
        manifest_bytes,
        lost_block_ids: lost,
        verify: Box::new(verify),
    };
    report.lost_block_ids.sort();
    report.lost_block_ids.dedup();
    Ok(report)
}

#[cfg(test)]
mod tests;
