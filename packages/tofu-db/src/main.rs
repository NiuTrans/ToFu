//! Minimal pre-authority operator binary; it is not an application protocol.

use std::env;
use std::io;
use std::path::PathBuf;

use tofu_db::authority::AuthorityDatabase;
use tofu_db::authority_gc::AuthorityGarbageCollectionBudget;
use tofu_db::backup::{create_incremental_backup, prune_backup, restore_latest_backup};
use tofu_db::certification::{
    certify_filesystem, read_stage, write_stage, CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS,
};
use tofu_db::daemon::{serve_supervised, DaemonConfig};
use tofu_db::engine::Engine;
use tofu_db::generated_tofudb_ir::MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION;
use tofu_db::generated_tofudb_ir::MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION;
use tofu_db::resource_probe::{probe_launch_resources, DaemonResourceBudget};

fn usage() -> ! {
    eprintln!("usage: tofu-db <init|inspect> --data-dir <persistent-path>\n       tofu-db serve --data-dir <existing-authority> --owner-id <positive-id> [--tenant-id <positive-id>] [--search-projection-dir <persistent-path>]\n       tofu-db certify-filesystem --data-dir <persistent-empty-path>\n       tofu-db collect-garbage --data-dir <existing-authority> [--execute]\n       tofu-db backfill-activity-index --data-dir <existing-authority> --tenant-id <positive-id> --owner-id <positive-id> [--maximum-rows <1..256>] --execute\n       tofu-db backup --data-dir <source> --backup-dir <persistent-path>\n       tofu-db restore --backup-dir <source> --data-dir <restore-target>\n       tofu-db prune-backup --backup-dir <persistent-path> --retain-generations <count>");
    std::process::exit(2)
}

struct Arguments {
    command: String,
    data_dir: PathBuf,
    backup_dir: Option<PathBuf>,
    retain_generations: Option<usize>,
    owner_id: Option<u64>,
    tenant_id: Option<u64>,
    search_projection_dir: Option<PathBuf>,
    maximum_rows: Option<usize>,
    execute: bool,
}

fn arguments() -> Arguments {
    let mut args = env::args().skip(1);
    let command = args.next().unwrap_or_else(|| usage());
    let mut data_dir = None;
    let mut backup_dir = None;
    let mut retain_generations = None;
    let mut owner_id = None;
    let mut tenant_id = None;
    let mut search_projection_dir = None;
    let mut maximum_rows = None;
    let mut execute = false;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--data-dir" if data_dir.is_none() => {
                let value = PathBuf::from(args.next().unwrap_or_else(|| usage()));
                if !value.is_absolute() {
                    usage();
                }
                data_dir = Some(value);
            }
            "--backup-dir" if backup_dir.is_none() => {
                let value = PathBuf::from(args.next().unwrap_or_else(|| usage()));
                if !value.is_absolute() {
                    usage();
                }
                backup_dir = Some(value);
            }
            "--retain-generations" if retain_generations.is_none() => {
                retain_generations = Some(
                    args.next()
                        .unwrap_or_else(|| usage())
                        .parse()
                        .unwrap_or_else(|_| usage()),
                );
            }
            "--owner-id" if owner_id.is_none() => {
                owner_id = Some(
                    args.next()
                        .unwrap_or_else(|| usage())
                        .parse()
                        .unwrap_or_else(|_| usage()),
                );
            }
            "--tenant-id" if tenant_id.is_none() => {
                tenant_id = Some(
                    args.next()
                        .unwrap_or_else(|| usage())
                        .parse()
                        .unwrap_or_else(|_| usage()),
                );
            }
            "--search-projection-dir" if search_projection_dir.is_none() => {
                let value = PathBuf::from(args.next().unwrap_or_else(|| usage()));
                if !value.is_absolute() {
                    usage();
                }
                search_projection_dir = Some(value);
            }
            "--maximum-rows" if maximum_rows.is_none() => {
                maximum_rows = Some(
                    args.next()
                        .unwrap_or_else(|| usage())
                        .parse()
                        .unwrap_or_else(|_| usage()),
                );
            }
            "--execute" if !execute => execute = true,
            _ => usage(),
        }
    }
    let data_dir = match command.as_str() {
        "prune-backup" if data_dir.is_none() => PathBuf::new(),
        _ => data_dir.unwrap_or_else(|| usage()),
    };
    match command.as_str() {
        "init" | "inspect" | "certify-filesystem" | "__certify-write" | "__certify-read"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && !execute => {}
        "collect-garbage"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none() => {}
        "backfill-activity-index"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_some()
                && tenant_id.is_some()
                && search_projection_dir.is_none()
                && execute => {}
        "serve"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_some()
                && maximum_rows.is_none()
                && !execute => {}
        "backup" | "restore"
            if backup_dir.is_some()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && !execute => {}
        "prune-backup"
            if backup_dir.is_some()
                && retain_generations.is_some()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && data_dir.as_os_str().is_empty()
                && !execute => {}
        _ => usage(),
    }
    Arguments {
        command,
        data_dir,
        backup_dir,
        retain_generations,
        owner_id,
        tenant_id,
        search_projection_dir,
        maximum_rows,
        execute,
    }
}

fn run() -> io::Result<()> {
    let arguments = arguments();
    match arguments.command.as_str() {
        "serve" => {
            let mut auth_secret = env::var("TOFU_STORAGE_TOKEN")
                .map_err(|_| {
                    io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "TOFU_STORAGE_TOKEN is required for tofu-db serve",
                    )
                })?
                .into_bytes();
            env::remove_var("TOFU_STORAGE_TOKEN");
            let mut config = DaemonConfig::new(
                &arguments.data_dir,
                arguments.owner_id.unwrap(),
                arguments.tenant_id,
                &mut auth_secret,
            )?;
            if let Some(search_projection_dir) = arguments.search_projection_dir.as_ref() {
                config = config.with_search_projection_dir(search_projection_dir)?;
            }
            let mut stdout = io::stdout().lock();
            serve_supervised(config, io::stdin(), &mut stdout)?;
            return Ok(());
        }
        "certify-filesystem" => {
            let report = certify_filesystem(&arguments.data_dir)?;
            println!(
                "format=tofu-db.filesystem-certification.v1 authority_uuid={} durable_sequence={} checkpoint_sequence={} payload_segment_count={} payload_block_bytes={} write_process_micros={} first_reopen_micros={} post_reopen_commit_micros={} second_reopen_process_micros={} total_micros={} retained_file_count={} retained_file_bytes={} measurements_are_observations_not_release_certification=true exclusive_lock=true immutable_block=true control_rotation=true payload_segment_publication=true payload_segment_random_read=true payload_loose_reclaim=true destructor_free_reopen=true cross_process_reopen=true pre_authority=true",
                report.authority_uuid,
                report.durable_sequence,
                report.checkpoint_sequence,
                report.payload_segment_count,
                report.payload_block_bytes,
                report.write_process_micros,
                report.first_reopen_micros,
                report.post_reopen_commit_micros,
                report.second_reopen_process_micros,
                report.total_micros,
                report.retained_file_count,
                report.retained_file_bytes
            );
            return Ok(());
        }
        "__certify-write" => {
            write_stage(&arguments.data_dir)?;
            std::process::exit(CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS);
        }
        "__certify-read" => {
            read_stage(&arguments.data_dir)?;
            return Ok(());
        }
        "backup" => {
            let mut engine = Engine::open(&arguments.data_dir)?;
            let metrics =
                create_incremental_backup(&mut engine, arguments.backup_dir.as_ref().unwrap())?;
            println!(
                "format=tofu-db.backup.v1 durable_sequence={} copied_blocks={} copied_block_bytes={} manifest_bytes={}",
                metrics.durable_sequence,
                metrics.copied_blocks,
                metrics.copied_block_bytes,
                metrics.manifest_bytes
            );
            return Ok(());
        }
        "restore" => {
            let manifest =
                restore_latest_backup(arguments.backup_dir.as_ref().unwrap(), &arguments.data_dir)?;
            println!(
                "format=tofu-db.restore.v1 authority_uuid={} durable_sequence={}",
                manifest.authority_uuid, manifest.durable_sequence
            );
            return Ok(());
        }
        "prune-backup" => {
            let metrics = prune_backup(
                arguments.backup_dir.as_ref().unwrap(),
                arguments.retain_generations.unwrap(),
            )?;
            println!(
                "format=tofu-db.prune.v1 retained_generations={} removed_generations={} removed_blocks={} removed_block_bytes={} mark_references={} spill_bytes={}",
                metrics.retained_generations,
                metrics.removed_generations,
                metrics.removed_blocks,
                metrics.removed_block_bytes,
                metrics.mark_references,
                metrics.spill_bytes
            );
            return Ok(());
        }
        "collect-garbage" => {
            let resource_budget =
                DaemonResourceBudget::from_snapshot(probe_launch_resources(&arguments.data_dir));
            let budget = AuthorityGarbageCollectionBudget::from_resource_budget(resource_budget);
            let mut database = AuthorityDatabase::open(&arguments.data_dir)?;
            let metrics = if arguments.execute {
                database.collect_garbage(budget)?
            } else {
                database.plan_garbage_collection(budget)?
            };
            println!(
                "format=tofu-db.authority-gc.v1 mode={} scanned_loose_block_entries={} candidate_temporary_block_files={} candidate_temporary_block_bytes={} removed_temporary_block_files={} removed_temporary_block_bytes={} scanned_blocks={} live_references={} spill_bytes={} candidate_blocks={} candidate_bytes={} removed_blocks={} removed_bytes={} scanned_payload_segment_files={} candidate_orphan_segment_files={} candidate_orphan_segment_bytes={} removed_orphan_segment_files={} removed_orphan_segment_bytes={} payload_compaction_shard={} candidate_payload_compaction_blocks={} candidate_payload_compaction_bytes={} compacted_payload_blocks={} compacted_payload_bytes={} compacted_segment_bytes={} compacted_loose_bytes_reclaimed={} payload_compaction_catalog_blocked={} scanned_payload_segments={} candidate_segment_blocks={} candidate_segment_dead_blocks={} candidate_segment_bytes={} rewritten_payload_segments={} retired_payload_segments={} replacement_segment_bytes={} removed_segment_bytes={} segment_budget_blocked={} more_candidates={}",
                if arguments.execute { "execute" } else { "plan" },
                metrics.scanned_loose_block_entries,
                metrics.candidate_temporary_block_files,
                metrics.candidate_temporary_block_bytes,
                metrics.removed_temporary_block_files,
                metrics.removed_temporary_block_bytes,
                metrics.scanned_blocks,
                metrics.live_references,
                metrics.spill_bytes,
                metrics.candidate_blocks,
                metrics.candidate_bytes,
                metrics.removed_blocks,
                metrics.removed_bytes,
                metrics.scanned_payload_segment_files,
                metrics.candidate_orphan_segment_files,
                metrics.candidate_orphan_segment_bytes,
                metrics.removed_orphan_segment_files,
                metrics.removed_orphan_segment_bytes,
                metrics
                    .payload_compaction_shard
                    .map_or_else(|| "none".to_owned(), |shard| format!("{shard:02x}")),
                metrics.candidate_payload_compaction_blocks,
                metrics.candidate_payload_compaction_bytes,
                metrics.compacted_payload_blocks,
                metrics.compacted_payload_bytes,
                metrics.compacted_segment_bytes,
                metrics.compacted_loose_bytes_reclaimed,
                metrics.payload_compaction_catalog_blocked,
                metrics.scanned_payload_segments,
                metrics.candidate_segment_blocks,
                metrics.candidate_segment_dead_blocks,
                metrics.candidate_segment_bytes,
                metrics.rewritten_payload_segments,
                metrics.retired_payload_segments,
                metrics.replacement_segment_bytes,
                metrics.removed_segment_bytes,
                metrics.segment_budget_blocked,
                metrics.more_candidates
            );
            return Ok(());
        }
        "backfill-activity-index" => {
            let mut database = AuthorityDatabase::open(&arguments.data_dir)?;
            let progress = database.backfill_conversation_activity_candidates(
                arguments.tenant_id.unwrap(),
                arguments.owner_id.unwrap(),
                arguments
                    .maximum_rows
                    .unwrap_or(MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION),
            )?;
            println!(
                "format=tofu-db.activity-index-backfill.v1 processed_rows={} source_bytes={} complete={} committed={} durable_sequence={} maximum_rows_per_transaction={} maximum_source_bytes_per_transaction={}",
                progress.processed_rows,
                progress.source_bytes,
                progress.complete,
                progress.committed,
                progress.durable_sequence,
                MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION,
                MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION
            );
            return Ok(());
        }
        _ => {}
    }
    let engine = match arguments.command.as_str() {
        "init" => Engine::initialize(&arguments.data_dir)?,
        "inspect" => Engine::open(&arguments.data_dir)?,
        _ => usage(),
    };
    let state = engine.state();
    println!(
        "format=tofu-db.v1 authority_uuid={} generation={} durable_sequence={} pre_authority=true",
        state.authority_uuid, state.generation, state.durable_sequence
    );
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("tofu-db: {error}");
        std::process::exit(1);
    }
}
