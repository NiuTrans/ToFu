//! Minimal pre-authority operator binary; it is not an application protocol.

use std::env;
use std::fmt::Write as _;
use std::io;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use tofu_db::authority::{AuthorityDatabase, MAX_LEGACY_CHECKOUT_BACKFILL};
use tofu_db::authority_gc::{AuthorityGarbageCollectionBudget, AuthorityGarbageCollectionMetrics};
use tofu_db::backup::{
    create_incremental_backup_with_key, prune_backup_with_key, restore_latest_backup_with_key,
};
use tofu_db::certification::{
    certify_filesystem, read_stage, read_stage_at_rest, write_stage, write_stage_at_rest,
    CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS,
};
use tofu_db::certification_matrix::{
    certify_filesystem_matrix, format_matrix_report, MatrixCellOutcome,
};
use tofu_db::daemon::{serve_supervised, DaemonConfig};
use tofu_db::engine::Engine;
use tofu_db::generated_tofudb_ir::FILESYSTEM_CERTIFICATION_MATRIX_COMMAND;
use tofu_db::generated_tofudb_ir::MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION;
use tofu_db::generated_tofudb_ir::MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION;
use tofu_db::generated_tofudb_ir::{
    DEFAULT_AUTHORITY_GC_OPERATOR_MILLISECONDS, MAX_AUTHORITY_GC_OPERATOR_MILLISECONDS,
    MAX_AUTHORITY_GC_SESSION_CALLS,
};
use tofu_db::release_signature::ReleaseKeyPair;
use tofu_db::resource_gate::{
    certify_resource_gate, checkpoint_stage as gate_checkpoint_stage,
    commit_stage as gate_commit_stage, format_gate_report, reopen_stage as gate_reopen_stage,
    RESOURCE_GATE_COMMIT_EXIT_WITHOUT_DESTRUCTORS,
};
use tofu_db::resource_probe::{probe_launch_resources, DaemonResourceBudget};
use zeroize::Zeroize;

// The commit hot paths rewrite COW pages and stage receipts through many
// short-lived Vec allocations; glibc malloc accounted for ~38% of serve-side
// CPU in perf profiles of conversation.create bursts.
#[cfg(not(feature = "embedded-mimalloc"))]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn usage() -> ! {
    eprintln!("usage: tofu-db <init|inspect> --data-dir <persistent-path>\n       tofu-db serve --data-dir <existing-authority> --owner-id <positive-id> [--tenant-id <positive-id>] [--search-projection-dir <persistent-path>] [--durability strict|normal] [--wal-flush-interval-ms <1..1000>] [--backup-dir <persistent-path>] [--fastpath-dir <existing-local-base>] [--fastpath-sync-interval-ms <1000..3600000>]\n       tofu-db fastpath-export --data-dir <durable-seed> --backup-dir <NEW-authority-target>\n       tofu-db <certify-filesystem|{FILESYSTEM_CERTIFICATION_MATRIX_COMMAND}> --data-dir <persistent-empty-path>\n       tofu-db certify-resource-gate --data-dir <persistent-empty-path>\n       tofu-db collect-garbage --data-dir <existing-authority> [--execute [--max-rounds <1..{MAX_AUTHORITY_GC_SESSION_CALLS}>] [--budget-ms <1..{MAX_AUTHORITY_GC_OPERATOR_MILLISECONDS}>]]\n       tofu-db sql --data-dir <existing-authority> --tenant-id <positive-id> --owner-id <positive-id> < query.sql\n       tofu-db backfill-activity-index --data-dir <existing-authority> --tenant-id <positive-id> --owner-id <positive-id> [--maximum-rows <1..256>] --execute\n       tofu-db backfill-integration-checkout-claims --data-dir <existing-authority> --tenant-id <positive-id> --owner-id <positive-id> [--maximum-rows <1..128>] --execute\n       tofu-db migration-session --data-dir <existing-authority> --tenant-id <positive-id> --owner-id <positive-id> --execute\n       tofu-db backup --data-dir <source> --backup-dir <persistent-path>\n       tofu-db verify --data-dir <existing-authority>\n       tofu-db salvage --data-dir <existing-authority> --backup-dir <persistent-empty-target>\n       tofu-db restore --backup-dir <source> --data-dir <restore-target>\n       tofu-db certify-release --manifest <SHA256SUMS> --signature <SHA256SUMS.sig> --binary <packaged-tofu-db> [--pubkey <public-key.json>]\n       tofu-db certify-release-keygen --key-out <secret-key.json> --pubkey-out <public-key.json>");
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
    maximum_gc_rounds: Option<u32>,
    gc_budget_ms: Option<u64>,
    manifest: Option<PathBuf>,
    signature: Option<PathBuf>,
    binary: Option<PathBuf>,
    pubkey: Option<PathBuf>,
    key_out: Option<PathBuf>,
    pubkey_out: Option<PathBuf>,
    durability: Option<String>,
    wal_flush_interval_ms: Option<u64>,
    fastpath_dir: Option<PathBuf>,
    fastpath_interval_ms: Option<u64>,
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
    let mut maximum_gc_rounds: Option<u32> = None;
    let mut gc_budget_ms: Option<u64> = None;
    let mut manifest = None;
    let mut signature = None;
    let mut binary = None;
    let mut pubkey = None;
    let mut key_out = None;
    let mut pubkey_out = None;
    let mut durability = None;
    let mut wal_flush_interval_ms = None;
    let mut fastpath_dir = None;
    let mut fastpath_interval_ms = None;
    macro_rules! path_flag {
        ($slot:ident) => {
            if $slot.is_none() {
                let value = PathBuf::from(args.next().unwrap_or_else(|| usage()));
                if !value.is_absolute() {
                    usage();
                }
                $slot = Some(value);
            } else {
                usage();
            }
        };
    }
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--fastpath-dir" => { path_flag!(fastpath_dir); }
            "--fastpath-sync-interval-ms" if fastpath_interval_ms.is_none() => {
                let value: u64 = args.next().unwrap_or_else(|| usage()).parse().unwrap_or_else(|_| usage());
                if !(tofu_db::fastpath::MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS..=tofu_db::fastpath::MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS).contains(&value) { usage(); }
                fastpath_interval_ms = Some(value);
            }
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
            "--durability" if durability.is_none() => {
                let value = args.next().unwrap_or_else(|| usage());
                if value != "strict" && value != "normal" {
                    usage();
                }
                durability = Some(value);
            }
            "--wal-flush-interval-ms" if wal_flush_interval_ms.is_none() => {
                let value: u64 = args
                    .next()
                    .unwrap_or_else(|| usage())
                    .parse()
                    .unwrap_or_else(|_| usage());
                if !(1..=tofu_db::wal::MAX_WAL_FLUSH_INTERVAL.as_millis() as u64)
                    .contains(&value)
                {
                    usage();
                }
                wal_flush_interval_ms = Some(value);
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
            "--max-rounds" if maximum_gc_rounds.is_none() => {
                let value = args
                    .next()
                    .unwrap_or_else(|| usage())
                    .parse()
                    .unwrap_or_else(|_| usage());
                if !(1..=MAX_AUTHORITY_GC_SESSION_CALLS).contains(&value) {
                    usage();
                }
                maximum_gc_rounds = Some(value);
            }
            "--budget-ms" if gc_budget_ms.is_none() => {
                let value = args
                    .next()
                    .unwrap_or_else(|| usage())
                    .parse()
                    .unwrap_or_else(|_| usage());
                if !(1..=MAX_AUTHORITY_GC_OPERATOR_MILLISECONDS).contains(&value) {
                    usage();
                }
                gc_budget_ms = Some(value);
            }
            "--manifest" => path_flag!(manifest),
            "--signature" => path_flag!(signature),
            "--binary" => path_flag!(binary),
            "--pubkey" => path_flag!(pubkey),
            "--key-out" => path_flag!(key_out),
            "--pubkey-out" => path_flag!(pubkey_out),
            "--execute" if !execute => execute = true,
            _ => usage(),
        }
    }
    if (maximum_gc_rounds.is_some() || gc_budget_ms.is_some())
        && (command != "collect-garbage" || !execute)
    {
        usage();
    }
    let data_dir = match command.as_str() {
        "prune-backup" | "certify-release" | "certify-release-keygen" if data_dir.is_none() => {
            PathBuf::new()
        }
        _ => data_dir.unwrap_or_else(|| usage()),
    };
    if command != "serve" && (fastpath_dir.is_some() || fastpath_interval_ms.is_some()) { usage(); }
    if fastpath_interval_ms.is_some() && fastpath_dir.is_none() { usage(); }
    match command.as_str() {
        "init"
        | "inspect"
        | "certify-filesystem"
        | FILESYSTEM_CERTIFICATION_MATRIX_COMMAND
        | "certify-resource-gate"
        | "__certify-write"
        | "__certify-write-at-rest"
        | "__certify-read"
        | "__certify-read-at-rest"
        | "__gate-commit"
        | "__gate-checkpoint"
        | "__gate-reopen"
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
        "verify"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && !execute => {}
        "salvage"
            if backup_dir.is_some()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && !execute => {}
        "backfill-activity-index"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_some()
                && tenant_id.is_some()
                && search_projection_dir.is_none()
                && execute => {}
        "backfill-integration-checkout-claims"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_some_and(|id| id > 0)
                && tenant_id.is_some_and(|id| id > 0)
                && search_projection_dir.is_none()
                && maximum_rows.is_none_or(|rows| (1..=MAX_LEGACY_CHECKOUT_BACKFILL).contains(&rows))
                && execute => {}
        "sql"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_some()
                && tenant_id.is_some()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && !execute => {}
        "migration-session"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_some()
                && tenant_id.is_some()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && execute => {}
        "serve"
            if retain_generations.is_none()
                && owner_id.is_some()
                && maximum_rows.is_none()
                && !execute => {}
        "backup" | "restore" | "fastpath-export"
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
        "certify-release"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && manifest.is_some()
                && signature.is_some()
                && binary.is_some()
                && key_out.is_none()
                && pubkey_out.is_none()
                && !execute => {}
        "certify-release-keygen"
            if backup_dir.is_none()
                && retain_generations.is_none()
                && owner_id.is_none()
                && tenant_id.is_none()
                && search_projection_dir.is_none()
                && maximum_rows.is_none()
                && manifest.is_none()
                && signature.is_none()
                && binary.is_none()
                && pubkey.is_none()
                && key_out.is_some()
                && pubkey_out.is_some()
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
        maximum_gc_rounds,
        gc_budget_ms,
        manifest,
        signature,
        binary,
        pubkey,
        key_out,
        pubkey_out,
        durability,
        wal_flush_interval_ms,
        fastpath_dir,
        fastpath_interval_ms,
    }
}

fn print_gc_metrics(metrics: AuthorityGarbageCollectionMetrics, execute: bool) {
    println!(
        "format=tofu-db.authority-gc.v1 mode={} scanned_loose_block_entries={} candidate_temporary_block_files={} candidate_temporary_block_bytes={} removed_temporary_block_files={} removed_temporary_block_bytes={} scanned_blocks={} live_references={} spill_bytes={} candidate_blocks={} candidate_bytes={} removed_blocks={} removed_bytes={} scanned_payload_segment_files={} candidate_orphan_segment_files={} candidate_orphan_segment_bytes={} removed_orphan_segment_files={} removed_orphan_segment_bytes={} payload_compaction_shard={} candidate_payload_compaction_blocks={} candidate_payload_compaction_bytes={} compacted_payload_blocks={} compacted_payload_bytes={} compacted_segment_bytes={} compacted_loose_bytes_reclaimed={} payload_compaction_catalog_blocked={} scanned_payload_segments={} candidate_segment_blocks={} candidate_segment_dead_blocks={} candidate_segment_bytes={} rewritten_payload_segments={} retired_payload_segments={} replacement_segment_bytes={} removed_segment_bytes={} segment_budget_blocked={} more_candidates={}",
        if execute { "execute" } else { "plan" },
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
}

fn run() -> io::Result<()> {
    let arguments = arguments();
    match arguments.command.as_str() {
        "certify-release-keygen" => {
            let pair = ReleaseKeyPair::generate();
            pair.save(&arguments.key_out.unwrap())?;
            pair.save_public(&arguments.pubkey_out.unwrap())?;
            let mut key_id = String::with_capacity(64);
            for byte in tofu_db::release_signature::key_identity(&pair.verifying_key_bytes()) {
                write!(&mut key_id, "{byte:02x}").expect("writing to a String cannot fail");
            }
            println!("format=tofu-db.release-keygen.v1 key_id={key_id}");
            return Ok(());
        }
        "certify-release" => {
            // The release signing public key is the trust root. The packaged
            // default lives beside the binary's source tree; operators may
            // pin a different key with --pubkey.
            let pubkey_path = match arguments.pubkey {
                Some(path) => path,
                None => default_release_public_key()?,
            };
            let public_key = tofu_db::release_signature::load_public_key(&pubkey_path)?;
            let verification = tofu_db::release_signature::verify_release(
                &arguments.manifest.unwrap(),
                &arguments.signature.unwrap(),
                &arguments.binary.unwrap(),
                &public_key,
            )?;
            println!("{}", verification.format_line());
            return Ok(());
        }
        "migration-session" => {
            return tofu_db::migration_session::run(
                &arguments.data_dir,
                arguments.tenant_id.unwrap(),
                arguments.owner_id.unwrap(),
                io::BufReader::new(io::stdin().lock()),
                io::stdout().lock(),
            );
        }
        "fastpath-export" => {
            let key = tofu_db::at_rest::key_from_launch_environment()?;
            let manifest = tofu_db::fastpath::export(&arguments.data_dir, arguments.backup_dir.as_ref().unwrap(), key)?;
            println!("authority_uuid={} durable_sequence={} export_verified=true source_retained=true", manifest.authority_uuid, manifest.durable_sequence);
            return Ok(());
        }
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
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let mut config = DaemonConfig::new(
                &arguments.data_dir,
                arguments.owner_id.unwrap(),
                arguments.tenant_id,
                &mut auth_secret,
            )?
            .with_at_rest_key(at_rest_key);
            let durability = match arguments.durability.as_deref() {
                Some("strict") => tofu_db::wal::WalDurability::Strict,
                Some("normal") => tofu_db::wal::WalDurability::Normal,
                Some(_) => usage(),
                // Automatic tiering: network/fuse volumes acknowledge after
                // the in-memory commit (bounded RPO via the background
                // flusher) so mount fsync latency never taxes every command;
                // local or unobservable volumes keep the strict per-commit
                // barrier. An explicit --durability always wins.
                None if arguments.fastpath_dir.is_some() => tofu_db::wal::WalDurability::Strict,
                None => tofu_db::resource_probe::automatic_wal_durability(&arguments.data_dir),
            };
            let flush_interval = arguments
                .wal_flush_interval_ms
                .map(std::time::Duration::from_millis)
                .unwrap_or(tofu_db::daemon::DEFAULT_WAL_FLUSH_INTERVAL);
            if arguments.wal_flush_interval_ms.is_some()
                && durability != tofu_db::wal::WalDurability::Normal
            {
                usage();
            }
            config = config.with_wal_durability(durability, flush_interval)?;
            config = config.with_backup_dir(arguments.backup_dir.clone());
            if let Some(base) = arguments.fastpath_dir.as_ref() {
                if arguments.durability.as_deref() == Some("normal") || arguments.wal_flush_interval_ms.is_some() {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, "native fastpath requires strict local durability"));
                }
                config = config.with_fastpath(base, arguments.fastpath_interval_ms)?;
            }
            if let Some(search_projection_dir) = arguments.search_projection_dir.as_ref() {
                config = config.with_search_projection_dir(search_projection_dir)?;
            }
            let mut stdout = io::stdout().lock();
            serve_supervised(config, io::stdin(), &mut stdout)?;
            return Ok(());
        }
        "certify-filesystem" => {
            // Read the bounded launch secret first: consuming it from this
            // process's environment would also strip it from the child
            // stages, which re-derive the key from their inherited copy.
            let mut secret = match env::var("TOFU_DB_AT_REST_KEY") {
                Ok(value) => Some(value.into_bytes()),
                Err(env::VarError::NotPresent) => None,
                Err(env::VarError::NotUnicode(_)) => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "TOFU_DB_AT_REST_KEY must be ASCII",
                    ))
                }
            };
            let report = match secret.as_deref() {
                Some(secret) => {
                    tofu_db::certification::certify_filesystem_at_rest(&arguments.data_dir, secret)?
                }
                None => certify_filesystem(&arguments.data_dir)?,
            };
            if let Some(secret) = secret.as_mut() {
                env::remove_var("TOFU_DB_AT_REST_KEY");
                secret.zeroize();
            }
            println!(
                "format=tofu-db.filesystem-certification.v1 authority_uuid={} at_rest={} durable_sequence={} checkpoint_sequence={} payload_segment_count={} payload_block_bytes={} write_process_micros={} first_reopen_micros={} post_reopen_commit_micros={} second_reopen_process_micros={} total_micros={} retained_file_count={} retained_file_bytes={} measurements_are_observations_not_release_certification=true exclusive_lock=true immutable_block=true control_rotation=true payload_segment_publication=true payload_segment_random_read=true payload_loose_reclaim=true destructor_free_reopen=true cross_process_reopen=true pre_authority=true",
                report.authority_uuid,
                report.at_rest,
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
        FILESYSTEM_CERTIFICATION_MATRIX_COMMAND => {
            let reports = certify_filesystem_matrix(&arguments.data_dir)?;
            print!("{}", format_matrix_report(&reports));
            if reports
                .iter()
                .any(|report| report.outcome == MatrixCellOutcome::Fail)
            {
                return Err(io::Error::other("certification matrix has a failed cell"));
            }
            return Ok(());
        }
        "__certify-write" => {
            write_stage(&arguments.data_dir)?;
            std::process::exit(CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS);
        }
        "__certify-write-at-rest" => {
            write_stage_at_rest(&arguments.data_dir)?;
            std::process::exit(CERTIFICATION_EXIT_WITHOUT_DESTRUCTORS);
        }
        "__certify-read" => {
            read_stage(&arguments.data_dir)?;
            return Ok(());
        }
        "__certify-read-at-rest" => {
            read_stage_at_rest(&arguments.data_dir)?;
            return Ok(());
        }
        "certify-resource-gate" => {
            let report = certify_resource_gate(&arguments.data_dir)?;
            print!("{}", format_gate_report(&report, &arguments.data_dir));
            return Ok(());
        }
        "__gate-commit" => {
            gate_commit_stage(&arguments.data_dir)?;
            std::process::exit(RESOURCE_GATE_COMMIT_EXIT_WITHOUT_DESTRUCTORS);
        }
        "__gate-checkpoint" => {
            gate_checkpoint_stage(&arguments.data_dir)?;
            return Ok(());
        }
        "__gate-reopen" => {
            gate_reopen_stage(&arguments.data_dir)?;
            return Ok(());
        }
        "backup" => {
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            // Open through the entity layer so a deferred-application
            // overlay is replayed and merged before the checkpoint: the
            // backup must contain every committed mutation, not only the
            // tree's merged prefix.
            let mut entities = tofu_db::entity::EntityDatabase::open_with_key(
                &arguments.data_dir,
                at_rest_key.clone(),
            )?;
            entities.merge_overlay_fully()?;
            let metrics = create_incremental_backup_with_key(
                entities.writer_engine_mut()?,
                arguments.backup_dir.as_ref().unwrap(),
                at_rest_key,
            )?;
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
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let manifest = restore_latest_backup_with_key(
                arguments.backup_dir.as_ref().unwrap(),
                &arguments.data_dir,
                at_rest_key,
            )?;
            println!(
                "format=tofu-db.restore.v1 authority_uuid={} durable_sequence={}",
                manifest.authority_uuid, manifest.durable_sequence
            );
            return Ok(());
        }
        "prune-backup" => {
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let metrics = prune_backup_with_key(
                arguments.backup_dir.as_ref().unwrap(),
                arguments.retain_generations.unwrap(),
                at_rest_key,
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
        "verify" => {
            let report = tofu_db::verify::verify_authority(&arguments.data_dir)?;
            println!(
                "format={} verdict={} control_generation={} durable_sequence={} wal_records={} wal_tail_truncated_bytes={} payload_segments_ok={}/{} pages_verified={} semantic_verified={} history_blocks_verified={} wal_blocks_verified={} missing_blocks={}",
                report.format,
                report.verdict,
                report.control_generation.map_or("unknown".to_owned(), |v| v.to_string()),
                report.durable_sequence.map_or("unknown".to_owned(), |v| v.to_string()),
                report.wal.valid_records,
                report.wal.truncated_tail_bytes,
                report.payload.segments_ok,
                report.payload.segments_total,
                report.graph.pages_verified,
                report.graph.semantic_references_verified,
                report.graph.history_blocks_verified,
                report.graph.wal_blocks_verified,
                report.graph.missing_block_ids.len(),
            );
            println!("performance={}", serde_json::to_string(&report.performance)?);
            for lost in &report.graph.missing_block_ids {
                println!("missing_block={lost}");
            }
            for failed in &report.payload.failed_segment_ids {
                println!("failed_segment={failed}");
            }
            if let Some(fatal) = report.graph.fatal.as_ref().or(report.wal.fatal.as_ref()) {
                println!("fatal={fatal}");
            }
            return if report.verdict == "fatal" {
                Err(io::Error::new(io::ErrorKind::InvalidData, "authority verification is fatal"))
            } else {
                Ok(())
            };
        }
        "salvage" => {
            let target = arguments.backup_dir.as_ref().expect("validated above");
            let report = tofu_db::verify::salvage_authority(&arguments.data_dir, target)?;
            println!(
                "format={} verdict={} durable_sequence={} copied_blocks={} copied_block_bytes={} manifest_bytes={} lost_blocks={}",
                report.format,
                report.verdict,
                report.durable_sequence,
                report.copied_blocks,
                report.copied_block_bytes,
                report.manifest_bytes,
                report.lost_block_ids.len(),
            );
            for lost in &report.lost_block_ids {
                println!("lost_block={lost}");
            }
            return Ok(());
        }
        "collect-garbage" => {
            let resource_budget =
                DaemonResourceBudget::from_snapshot(probe_launch_resources(&arguments.data_dir));
            let budget = AuthorityGarbageCollectionBudget::from_resource_budget(resource_budget);
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let mut database = AuthorityDatabase::open_with_key(&arguments.data_dir, at_rest_key)?;
            if arguments.maximum_gc_rounds.is_none() && arguments.gc_budget_ms.is_none() {
                let metrics = if arguments.execute {
                    database.collect_garbage(budget)?
                } else {
                    database.plan_garbage_collection(budget)?
                };
                print_gc_metrics(metrics, arguments.execute);
                return Ok(());
            }
            let maximum_rounds = arguments.maximum_gc_rounds.unwrap_or(1);
            let time_budget = Duration::from_millis(
                arguments
                    .gc_budget_ms
                    .unwrap_or(DEFAULT_AUTHORITY_GC_OPERATOR_MILLISECONDS),
            );
            let mut session = database.begin_garbage_collection(budget)?;
            let started = Instant::now();
            let result: io::Result<bool> = (|| {
                let mut more_candidates = true;
                for round in 0..maximum_rounds {
                    if round > 0 && started.elapsed() >= time_budget {
                        break;
                    }
                    let metrics = session.collect()?;
                    print_gc_metrics(metrics, true);
                    more_candidates = metrics.more_candidates;
                    if !more_candidates {
                        break;
                    }
                }
                Ok(more_candidates)
            })();
            let cleanup = session.finish();
            let more_candidates = result?;
            let stats = cleanup?;
            println!("format=tofu-db.authority-gc-session.v1 rounds={} mark_builds={} completed={} more_candidates={} round_budget_exhausted={} time_budget_exhausted={}",
                stats.calls, stats.mark_builds, !more_candidates, more_candidates,
                more_candidates && stats.calls >= maximum_rounds,
                more_candidates && started.elapsed() >= time_budget);
            return Ok(());
        }
        "sql" => {
            let mut query = String::new();
            io::Read::read_to_string(&mut io::stdin().lock(), &mut query)?;
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let mut database = AuthorityDatabase::open_with_key(&arguments.data_dir, at_rest_key)?;
            let response = tofu_db::tofusql::run_operator_query(
                &mut database,
                arguments.tenant_id.unwrap(),
                arguments.owner_id.unwrap(),
                query.trim(),
            )
            .map_err(io::Error::other)?;
            io::Write::write_all(&mut io::stdout().lock(), &response)?;
            return Ok(());
        }
        "backfill-integration-checkout-claims" => {
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let mut database = AuthorityDatabase::open_with_key(&arguments.data_dir, at_rest_key)?;
            let progress = database.backfill_integration_checkout_claims(
                arguments.tenant_id.unwrap(),
                arguments.owner_id.unwrap(),
                arguments.maximum_rows.unwrap_or(MAX_LEGACY_CHECKOUT_BACKFILL),
            )?;
            println!(
                "format=tofu-db.integration-checkout-backfill.v1 processed_rows={} after_id={} complete={} committed={} durable_sequence={} maximum_rows_per_transaction={}",
                progress.processed_rows,
                progress.after_id,
                progress.complete,
                progress.committed,
                progress.durable_sequence,
                MAX_LEGACY_CHECKOUT_BACKFILL
            );
            return Ok(());
        }
        "backfill-activity-index" => {
            let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
            let mut database = AuthorityDatabase::open_with_key(&arguments.data_dir, at_rest_key)?;
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
    let at_rest_key = tofu_db::at_rest::key_from_launch_environment()?;
    let engine = match arguments.command.as_str() {
        "init" => Engine::initialize_with_key(&arguments.data_dir, at_rest_key)?,
        "inspect" => Engine::open_with_key(&arguments.data_dir, at_rest_key)?,
        _ => usage(),
    };
    let state = engine.state();
    println!(
        "format=tofu-db.v1 authority_uuid={} generation={} durable_sequence={} pre_authority=true",
        state.authority_uuid, state.generation, state.durable_sequence
    );
    Ok(())
}

/// Locates the packaged release signing public key relative to the running
/// binary's source checkout (`packages/tofu-db/keys/release-signing-public.json`).
/// An installed binary outside a source tree has no packaged trust root; the
/// operator must pass `--pubkey` explicitly.
fn default_release_public_key() -> io::Result<PathBuf> {
    let executable = env::current_exe()?;
    for ancestor in executable.ancestors() {
        let candidate = ancestor.join("packages/tofu-db/keys/release-signing-public.json");
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        "no packaged release signing public key found; pass --pubkey <path>",
    ))
}

fn main() {
    if env::args().skip(1).eq(["--build-info"]) {
        println!("{}", serde_json::json!({
            "format": "tofu-db-build.v1",
            "source_sha256": env!("TOFUDB_SOURCE_SHA256"),
            "version": env!("CARGO_PKG_VERSION"),
            "embedded_allocator": if cfg!(feature="embedded-mimalloc") { "mimalloc" } else { "system" },
        }));
        return;
    }
    if let Err(error) = run() {
        if env::args().nth(1).as_deref() == Some("serve") {
            // Preserve the admission verdict before EOF: the supervisor must
            // not retry a deterministic integrity failure as a lease race.
            let retryable = matches!(error.kind(), io::ErrorKind::WouldBlock
                | io::ErrorKind::Interrupted | io::ErrorKind::TimedOut);
            let code = if error.kind() == io::ErrorKind::InvalidData {
                "database_integrity"
            } else { "database_unavailable" };
            println!("{}", serde_json::json!({
                "type": "storage.error", "protocol": tofu_db::protocol::PROTOCOL_VERSION,
                "code": code, "retryable": retryable,
                "diagnostic": error.to_string().chars().take(500).collect::<String>(),
            }));
        }
        tofu_db::engine_log::error("main", &format!("tofu-db: {error}"));
        std::process::exit(1);
    }
}
