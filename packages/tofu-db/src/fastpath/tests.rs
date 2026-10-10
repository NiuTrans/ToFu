use super::*;

fn setup() -> (tempfile::TempDir, FastpathConfig) {
    let root = tempfile::tempdir().unwrap();
    let durable = root.path().join("authority");
    let base = root.path().join("local");
    fs::create_dir(&durable).unwrap();
    fs::create_dir(&base).unwrap();
    let mut engine = Engine::initialize(&durable).unwrap();
    engine
        .commit_transaction(b"first", &[b"initial payload"])
        .unwrap();
    drop(engine);
    let config = FastpathConfig::new(&durable, &base, Some(1_000)).unwrap();
    (root, config)
}

#[test]
fn activation_fences_classic_opens_and_keeps_the_same_full_native_authority() {
    let (_root, config) = setup();
    let initial = Engine::open(&config.durable_authority)
        .unwrap()
        .state()
        .clone();
    let lease = config.prepare_with_probe(None, false).unwrap();
    assert_eq!(lease.shadow_sequence, initial.durable_sequence);
    assert!(Engine::open(&config.durable_authority).is_err());
    let front = Engine::open(&config.local_authority).unwrap();
    assert_eq!(front.state().authority_uuid, initial.authority_uuid);
    assert_eq!(front.state().durable_sequence, initial.durable_sequence);
    let transaction = front.transaction_at(1).unwrap().unwrap();
    assert_eq!(transaction.envelope.inline_payload, b"first");
    assert_eq!(
        front.read_block(transaction.envelope.block_ids[0]).unwrap(),
        b"initial payload"
    );
}

#[test]
fn surviving_front_keeps_unreplicated_crash_tail_and_exports_it_losslessly() {
    let (root, config) = setup();
    let lease = config.prepare_with_probe(None, false).unwrap();
    let mut front = Engine::open(&config.local_authority).unwrap();
    front
        .commit_transaction(b"tail", &[b"unshipped payload"])
        .unwrap();
    let expected = front.state().clone();
    drop(front);
    drop(lease);
    let restarted = config.prepare_with_probe(None, false).unwrap();
    let front = Engine::open(&config.local_authority).unwrap();
    assert_eq!(front.state().durable_sequence, expected.durable_sequence);
    assert_eq!(front.state().root_hash, expected.root_hash);
    drop(front);
    drop(restarted);
    let target = root.path().join("export");
    let report = export(&config.durable_authority, &target, None).unwrap();
    assert_eq!(report.durable_sequence, expected.durable_sequence);
    let exported = Engine::open(&target).unwrap();
    let transaction = exported
        .transaction_at(expected.durable_sequence)
        .unwrap()
        .unwrap();
    assert_eq!(
        exported
            .read_block(transaction.envelope.block_ids[0])
            .unwrap(),
        b"unshipped payload"
    );
    assert!(config.durable_authority.join(MARKER).exists());
}

#[test]
fn lost_front_restores_only_the_complete_shadow_and_never_the_stale_seed() {
    let (_root, config) = setup();
    let lease = config.prepare_with_probe(None, false).unwrap();
    let mut front = Engine::open(&config.local_authority).unwrap();
    front.commit(b"replicated").unwrap();
    backup::create_incremental_backup(&mut front, &config.shadow_directory).unwrap();
    let replicated = front.state().clone();
    front.commit(b"unreplicated").unwrap();
    drop(front);
    drop(lease);
    // Fault scenario: only this hermetic test's local disk disappears.
    fs::remove_dir_all(&config.local_authority).unwrap();
    let recovered = config.prepare_with_probe(None, false).unwrap();
    assert_eq!(recovered.shadow_sequence, replicated.durable_sequence);
    let front = Engine::open(&config.local_authority).unwrap();
    assert_eq!(front.state().durable_sequence, replicated.durable_sequence);
    assert_eq!(front.state().root_hash, replicated.root_hash);
    assert!(Engine::open(&config.durable_authority).is_err());
}

#[test]
fn foreign_or_corrupt_front_never_causes_automatic_shadow_fallback() {
    let (_root, config) = setup();
    let lease = config.prepare_with_probe(None, false).unwrap();
    drop(lease);
    fs::remove_dir_all(&config.local_authority).unwrap();
    fs::create_dir(&config.local_authority).unwrap();
    let foreign = Engine::initialize(&config.local_authority).unwrap();
    drop(foreign);
    assert!(config.prepare_with_probe(None, false).is_err());
    let before = fs::read(config.local_authority.join("CONTROL")).unwrap();
    fs::write(config.local_authority.join("CONTROL"), b"broken").unwrap();
    assert!(config.prepare_with_probe(None, false).is_err());
    assert_eq!(
        fs::read(config.local_authority.join("CONTROL")).unwrap(),
        b"broken"
    );
    assert!(!before.is_empty());
}

#[test]
fn source_lease_and_recorded_path_prevent_two_fastpath_writers() {
    let (root, config) = setup();
    let lease = config.prepare_with_probe(None, false).unwrap();
    assert!(config.prepare_with_probe(None, false).is_err());
    drop(lease);
    let alternate = root.path().join("alternate");
    fs::create_dir(&alternate).unwrap();
    let changed = FastpathConfig::new(&config.durable_authority, &alternate, None).unwrap();
    assert!(changed.prepare_with_probe(None, false).is_err());
    assert!(!changed.local_authority.exists());
}

#[test]
fn shadow_corruption_or_foreign_lineage_fails_before_restoring_a_lost_front() {
    let (_root, config) = setup();
    let lease = config.prepare_with_probe(None, false).unwrap();
    drop(lease);
    fs::remove_dir_all(&config.local_authority).unwrap();
    for entry in fs::read_dir(config.shadow_directory.join("snapshots")).unwrap() {
        fs::write(entry.unwrap().path(), b"torn publication").unwrap();
    }
    assert!(config.prepare_with_probe(None, false).is_err());
    assert!(!config.local_authority.exists());
    assert!(config.durable_authority.join(MARKER).exists());
}

#[test]
fn existing_unknown_artifacts_and_overlapping_paths_are_preserved() {
    let (_root, config) = setup();
    assert!(
        FastpathConfig::new(&config.durable_authority, &config.durable_authority, None).is_err()
    );
    fs::create_dir_all(&config.local_authority).unwrap();
    fs::write(config.local_authority.join("evidence"), b"keep").unwrap();
    assert!(config.prepare_with_probe(None, false).is_err());
    assert_eq!(
        fs::read(config.local_authority.join("evidence")).unwrap(),
        b"keep"
    );
    assert!(Engine::open(&config.durable_authority).is_ok());
}

#[test]
fn classic_marker_faults_fail_closed_at_the_vfs_boundary() {
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule};
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    drop(engine);
    vfs.arm_fault(Some(FaultRule {
        operation_number: 1,
        action: FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
    }))
    .unwrap();
    assert!(require_classic_authority(Path::new("/data"), vfs.as_ref()).is_err());
}

#[test]
fn marker_publication_error_short_write_and_lost_sync_crash_matrix() {
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    let base = DeterministicVfs::new(None);
    base.create_dir(Path::new("/data")).unwrap();
    base.sync_directory(Path::new("/")).unwrap();
    let marker = Marker {
        format: FORMAT.into(),
        authority_uuid: [7; 16],
        local_authority: PathBuf::from("/local/front"),
        shadow_directory: PathBuf::from("/shadow"),
        seed_sequence: 1,
        seed_hash: [9; 32],
        interval_ms: 1000,
    };
    let path = Path::new("/data/FASTPATH");
    let baseline = base.fork_durable().unwrap();
    publish_marker_with_vfs(path, &marker, &baseline).unwrap();
    let trace = baseline.trace().unwrap();
    let mut exercised = 0;
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(3));
        }
        if matches!(operation, Operation::SyncAll | Operation::SyncDirectory) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let trial = base.fork_durable().unwrap();
            trial
                .arm_fault(Some(FaultRule {
                    operation_number: (index + 1) as u64,
                    action,
                }))
                .unwrap();
            let outcome = publish_marker_with_vfs(path, &marker, &trial);
            assert!(
                trial.trace().unwrap().len() > index,
                "fault must be reached"
            );
            trial.arm_fault(None).unwrap();
            trial.crash().unwrap();
            let recovered = read_marker_with_vfs(path, &trial);
            if outcome.is_ok() {
                assert_eq!(recovered.unwrap().seed_hash, marker.seed_hash);
                assert!(require_classic_authority(Path::new("/data"), &trial).is_err());
            } else if let Ok(recovered) = recovered {
                assert_eq!(recovered.seed_hash, marker.seed_hash);
                assert!(require_classic_authority(Path::new("/data"), &trial).is_err());
            } else {
                assert!(require_classic_authority(Path::new("/data"), &trial).is_ok());
            }
            exercised += 1;
        }
    }
    assert!(exercised > trace.len());
}

#[test]
fn encrypted_front_shadow_recovery_and_export_preserve_payload_confidentiality() {
    let root = tempfile::tempdir().unwrap();
    let durable = root.path().join("authority");
    let base = root.path().join("local");
    fs::create_dir(&durable).unwrap();
    fs::create_dir(&base).unwrap();
    let key = AtRestKey::from_key([42; 32]);
    let payload = b"private-fastpath-payload-never-plaintext";
    let mut source = Engine::initialize_with_key(&durable, Some(key.clone())).unwrap();
    let result = source.commit_transaction(b"encrypted", &[payload]).unwrap();
    drop(source);
    let config = FastpathConfig::new(&durable, &base, None).unwrap();
    let lease = config.prepare_with_probe(Some(key.clone()), false).unwrap();
    drop(lease);
    let block = result.block_ids[0];
    let front = Engine::open_with_key(&config.local_authority, Some(key.clone())).unwrap();
    assert_eq!(front.read_block(block).unwrap(), payload);
    drop(front);
    assert!(Engine::open(&config.local_authority).is_err());
    fs::remove_dir_all(&config.local_authority).unwrap();
    let lease = config.prepare_with_probe(Some(key.clone()), false).unwrap();
    drop(lease);
    let target = root.path().join("export");
    export(&durable, &target, Some(key.clone())).unwrap();
    let engine = Engine::open_with_key(&target, Some(key)).unwrap();
    assert_eq!(engine.read_block(block).unwrap(), payload);
    for directory in [&config.local_authority, &config.shadow_directory, &target] {
        let mut paths = vec![directory.to_path_buf()];
        while let Some(path) = paths.pop() {
            if path.is_dir() {
                paths.extend(fs::read_dir(path).unwrap().map(|row| row.unwrap().path()));
            } else {
                assert!(!fs::read(path)
                    .unwrap()
                    .windows(payload.len())
                    .any(|window| window == payload));
            }
        }
    }
}

#[test]
#[ignore = "explicit disposable filesystem comparison; set TOFU_NATIVE_FASTPATH_BENCH_ROOT"]
fn measured_whole_authority_fastpath_on_disposable_filesystems() {
    let slow_base = std::env::var("TOFU_NATIVE_FASTPATH_BENCH_ROOT")
        .expect("explicit disposable benchmark root");
    let slow = tempfile::tempdir_in(slow_base).unwrap();
    let local = tempfile::tempdir().unwrap();
    let source = slow.path().join("authority");
    fs::create_dir(&source).unwrap();
    let mut engine = Engine::initialize(&source).unwrap();
    let mut ids = Vec::new();
    for index in 0u64..128 {
        let mut payload = vec![0x5a; 128 * 1024];
        payload[..8].copy_from_slice(&index.to_le_bytes());
        ids.push(
            engine
                .commit_transaction(b"benchmark-seed", &[&payload])
                .unwrap()
                .block_ids[0],
        );
    }
    engine.checkpoint().unwrap();
    drop(engine);
    fn summarize(mut samples: Vec<f64>) -> serde_json::Value {
        samples.sort_by(f64::total_cmp);
        serde_json::json!({"samples":samples.len(),"mean_ms":samples.iter().sum::<f64>()/samples.len() as f64,
            "p50_ms":samples[samples.len()/2],"p95_ms":samples[samples.len()*95/100],"max_ms":samples[samples.len()-1]})
    }
    fn reads(engine: &Engine, ids: &[crate::block::BlockId]) -> serde_json::Value {
        engine
            .block_store_shared()
            .set_cache_capacity_bytes(0)
            .unwrap();
        let mut samples = Vec::new();
        for index in 0..512 {
            let start = Instant::now();
            let payload = engine.read_block(ids[(index * 37) % ids.len()]).unwrap();
            assert_eq!(payload.len(), 128 * 1024);
            samples.push(start.elapsed().as_secs_f64() * 1000.0);
        }
        summarize(samples)
    }
    fn commits(engine: &mut Engine, first: u64) -> serde_json::Value {
        let mut samples = Vec::new();
        for index in first..first + 64 {
            let mut payload = vec![0x7b; 4096];
            payload[..8].copy_from_slice(&index.to_le_bytes());
            let start = Instant::now();
            engine
                .commit_transaction(b"benchmark-commit", &[&payload])
                .unwrap();
            samples.push(start.elapsed().as_secs_f64() * 1000.0);
        }
        summarize(samples)
    }
    let mut baseline = Engine::open(&source).unwrap();
    let source_reads = reads(&baseline, &ids);
    let source_commits = commits(&mut baseline, 10_000);
    drop(baseline);
    let config = FastpathConfig::new(&source, local.path(), Some(1000)).unwrap();
    let seed_start = Instant::now();
    let lease = config.prepare(None).unwrap();
    let seed_ms = seed_start.elapsed().as_secs_f64() * 1000.0;
    let mut front = Engine::open(&config.local_authority).unwrap();
    let local_reads = reads(&front, &ids);
    let local_commits = commits(&mut front, 20_000);
    let copy_start = Instant::now();
    let copy = backup::create_incremental_backup(&mut front, &config.shadow_directory).unwrap();
    let copy_ms = copy_start.elapsed().as_secs_f64() * 1000.0;
    let source_bytes = backup::bounded_directory_bytes(&source).unwrap();
    let local_bytes = backup::bounded_directory_bytes(&config.local_authority).unwrap();
    let shadow_bytes = backup::bounded_directory_bytes(&config.shadow_directory).unwrap();
    let memory = fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|value| {
            value
                .lines()
                .find(|line| line.starts_with("VmHWM:"))
                .map(str::to_owned)
        });
    let report = serde_json::json!({"format":"tofu.native-fastpath-measurement.v1", "source_sha256":env!("TOFUDB_SOURCE_SHA256"),
        "workload":"generic strict engine; 128 unique 128KiB blocks, 512 reads with payload cache disabled, 64 unique 4KiB commits per path; OS cache uncontrolled",
        "source_volume":format!("{:?}",crate::resource_probe::volume_class(&source)),
        "local_volume":format!("{:?}",crate::resource_probe::volume_class(&config.local_authority)),
        "source_reads":source_reads,"local_reads":local_reads,"source_commits":source_commits,"local_commits":local_commits,
        "initial_copy_and_restore_ms":seed_ms,"incremental_shadow_ms":copy_ms,"incremental_copied_blocks":copy.copied_blocks,
        "incremental_copied_block_bytes":copy.copied_block_bytes,"source_bytes":source_bytes,"local_bytes":local_bytes,"shadow_bytes":shadow_bytes,
        "peak_process_memory":memory,"production_latency_proven":false,"whole_database_relocation":true,"live_user_data_touched":false});
    let output =
        std::env::var("TOFU_NATIVE_FASTPATH_BENCH_REPORT").expect("explicit measurement output");
    fs::write(output, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    println!("native_fastpath_measurement {}", report);
    drop(front);
    drop(lease);
}

#[test]
fn stale_and_divergent_same_uuid_fronts_are_preserved_and_refused() {
    let (root, config) = setup();
    let lease = config.prepare_for_test(None).unwrap();
    drop(lease);
    let old = root.path().join("old-front");
    fs::rename(&config.local_authority, &old).unwrap();
    backup::restore_latest_backup(&config.shadow_directory, &config.local_authority).unwrap();
    let mut remote_branch = Engine::open(&config.local_authority).unwrap();
    remote_branch.commit(b"remote-branch").unwrap();
    backup::create_incremental_backup(&mut remote_branch, &config.shadow_directory).unwrap();
    drop(remote_branch);
    fs::remove_dir_all(&config.local_authority).unwrap();
    fs::rename(&old, &config.local_authority).unwrap();
    assert!(config.prepare_for_test(None).is_err());
    let mut local_branch = Engine::open(&config.local_authority).unwrap();
    assert_eq!(local_branch.state().durable_sequence, 1);
    local_branch.commit(b"different-branch").unwrap();
    local_branch.commit(b"ahead-but-diverged").unwrap();
    let expected = local_branch.state().clone();
    drop(local_branch);
    assert!(config.prepare_for_test(None).is_err());
    let preserved = Engine::open(&config.local_authority).unwrap();
    assert_eq!(preserved.state().root_hash, expected.root_hash);
    assert_eq!(
        preserved.state().durable_sequence,
        expected.durable_sequence
    );
}

#[test]
fn independently_named_durable_authorities_have_disjoint_front_and_shadow_namespaces() {
    let (root, first) = setup();
    let second = root.path().join("another-authority");
    fs::create_dir(&second).unwrap();
    drop(Engine::initialize(&second).unwrap());
    let shared_base = first.local_authority.parent().unwrap().parent().unwrap();
    let other = FastpathConfig::new(&second, shared_base, None).unwrap();
    assert_ne!(first.local_authority, other.local_authority);
    assert_ne!(first.shadow_directory, other.shadow_directory);
    let first_lease = first.prepare_for_test(None).unwrap();
    let other_lease = other.prepare_for_test(None).unwrap();
    assert!(Engine::open(&first.local_authority).is_ok());
    assert!(Engine::open(&other.local_authority).is_ok());
    drop(first_lease);
    drop(other_lease);
}

#[test]
fn interrupted_owned_shadow_copy_resumes_only_when_its_source_is_unchanged() {
    let (_root, config) = setup();
    let engine = Engine::open(&config.durable_authority).unwrap();
    let pending = Marker {
        format: FORMAT.into(),
        authority_uuid: *engine.state().authority_uuid.as_bytes(),
        local_authority: config.local_authority.clone(),
        shadow_directory: config.shadow_directory.clone(),
        seed_sequence: engine.state().durable_sequence,
        seed_hash: engine.state().root_hash,
        interval_ms: 1000,
    };
    drop(engine);
    publish_marker(&config.durable_authority.join(PENDING_MARKER), &pending).unwrap();
    fs::create_dir_all(&config.shadow_directory).unwrap();
    backup::initialize_or_open_backup(&config.shadow_directory, Arc::new(RealVfs)).unwrap();
    let lease = config.prepare_for_test(None).unwrap();
    drop(lease);
    assert!(config.durable_authority.join(MARKER).exists());
    assert!(!config.durable_authority.join(PENDING_MARKER).exists());
    assert_eq!(
        Engine::open(&config.local_authority)
            .unwrap()
            .state()
            .durable_sequence,
        pending.seed_sequence
    );
}

#[test]
fn changed_source_refuses_pending_adoption_and_keeps_its_newer_classic_data() {
    let (_root, config) = setup();
    let mut source = Engine::open(&config.durable_authority).unwrap();
    let pending = Marker {
        format: FORMAT.into(),
        authority_uuid: *source.state().authority_uuid.as_bytes(),
        local_authority: config.local_authority.clone(),
        shadow_directory: config.shadow_directory.clone(),
        seed_sequence: source.state().durable_sequence,
        seed_hash: source.state().root_hash,
        interval_ms: 1000,
    };
    publish_marker(&config.durable_authority.join(PENDING_MARKER), &pending).unwrap();
    source.commit(b"newer-classic-data").unwrap();
    let expected = source.state().clone();
    drop(source);
    assert!(config.prepare_for_test(None).is_err());
    assert!(!config.local_authority.exists());
    assert!(!config.durable_authority.join(MARKER).exists());
    let preserved = Engine::open(&config.durable_authority).unwrap();
    assert_eq!(preserved.state().root_hash, expected.root_hash);
}

#[test]
fn complete_local_base_loss_recreates_only_the_recorded_base_and_restores_the_shadow() {
    let (_root, config) = setup();
    let lease = config.prepare_for_test(None).unwrap();
    drop(lease);
    let base = config.local_authority.parent().unwrap().parent().unwrap();
    fs::remove_dir_all(base).unwrap();
    let selected = FastpathConfig::new(&config.durable_authority, base, Some(1000)).unwrap();
    let recovered = selected.prepare_for_test(None).unwrap();
    assert_eq!(recovered.shadow_sequence, 1);
    assert_eq!(
        Engine::open(&selected.local_authority)
            .unwrap()
            .state()
            .durable_sequence,
        1
    );
}

#[test]
fn namespace_directory_publication_survives_each_lost_sync_and_preserves_ancestors_on_errors() {
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    let seed = DeterministicVfs::new(None);
    seed.create_dir(Path::new("/existing")).unwrap();
    seed.sync_directory(Path::new("/")).unwrap();
    let target = Path::new("/existing/base/namespaced");
    let baseline = seed.fork_durable().unwrap();
    create_directory_durable_with_vfs(target, &baseline).unwrap();
    let trace = baseline.trace().unwrap();
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::SyncDirectory {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let trial = seed.fork_durable().unwrap();
            trial
                .arm_fault(Some(FaultRule {
                    operation_number: (index + 1) as u64,
                    action,
                }))
                .unwrap();
            let outcome = create_directory_durable_with_vfs(target, &trial);
            assert!(
                trial.trace().unwrap().len() > index,
                "every fault must fire"
            );
            trial.arm_fault(None).unwrap();
            trial.crash().unwrap();
            assert_eq!(
                trial.metadata(Path::new("/existing")).unwrap(),
                FileKind::Directory
            );
            if outcome.is_ok() {
                assert_eq!(trial.metadata(target).unwrap(), FileKind::Directory);
            }
        }
    }
}
