//! Real authority/reopen, abrupt-process and commit-fault journal specifications.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};
use serde_json::json;
use tofu_runtime::checkpoint_journal::{Journal, Scope};

fn scope() -> Scope {
    Scope {
        owner: 7,
        task_id: "task".into(),
        epoch: "epoch".into(),
        binding: "model-tools-account".into(),
    }
}
fn store(database: AuthorityDatabase) -> NativeCheckpointStore {
    NativeCheckpointStore::new(Arc::new(Mutex::new(database)), 3, 7, 4096, 1048576).unwrap()
}
fn journal(store: &NativeCheckpointStore) -> Journal<'_> {
    Journal::new(scope(), store, 1048576, 1024).unwrap()
}
fn state() -> Value {
    json!({"format":"tofu.native-agent-checkpoint/v1","owner_user_id":7,
    "task_id":"task","epoch":"epoch","binding":"model-tools-account","next_round":1,
    "tool_calls":1,"messages":[{"role":"user","content":"question"},{"role":"tool","content":"x".repeat(32768)}],
    "usage":[{}],"round_limit":3,"tool_limit":3,"no_progress_limit":0,"failure_limit":0,
    "progress":{"codec":"sha256-token-sequence/v1","last_calls":null,"last_world":"","last_evidence":null,
    "last_failure":null,"no_progress_streak":0,"failure_streak":0}})
}

#[test]
fn blob_snapshot_survives_reopen_and_keep_preserves_its_version() {
    let directory = tempfile::tempdir().unwrap();
    let db = store(AuthorityDatabase::initialize(directory.path()).unwrap());
    let j = journal(&db);
    let first = j.acquire("first").unwrap();
    assert!(first.load().unwrap().is_none());
    first.begin(0).unwrap();
    first.complete(&state()).unwrap();
    first.release().unwrap();
    drop(first);
    drop(j);
    drop(db);
    let db = store(AuthorityDatabase::open(directory.path()).unwrap());
    let j = journal(&db);
    let second = j.acquire("second").unwrap();
    let header = db.header(7, "task").unwrap().unwrap();
    assert_eq!(second.load().unwrap(), Some(state()));
    let bounded = NativeCheckpointStore::new(db.authority.clone(), 3, 7, 4096, 32).unwrap();
    assert!(bounded.snapshot(7, "task", header.version).is_err());
    let foreign_tenant =
        NativeCheckpointStore::new(db.authority.clone(), 4, 7, 4096, 1048576).unwrap();
    assert!(foreign_tenant.header(7, "task").unwrap().is_none());
    assert!(db.snapshot(7, "task", header.version - 1).is_err());
    second.begin(1).unwrap();
    let database = db.authority.lock().unwrap();
    let mut tx = database.begin(3, 7).unwrap();
    let key = versioned_document::record_key(3, 7, SNAPSHOTS, "task").unwrap();
    let (_, snapshot_version) =
        versioned_document::get_value_with_version(&database, &mut tx, &key, SNAPSHOTS, "task")
            .unwrap()
            .unwrap();
    assert_eq!(snapshot_version, 1);
    drop(tx);
    drop(database);
    assert!(db.header(8, "task").is_err());
    assert!(j.acquire("competing").is_err());
}

#[test]
fn checkpoint_crash_child() {
    let Ok(path) = std::env::var("TOFU_CHECKPOINT_STORE_TEST_DIRECTORY") else {
        return;
    };
    let mode = std::env::var("TOFU_CHECKPOINT_STORE_TEST_STAGE").unwrap();
    let db = store(AuthorityDatabase::open(std::path::Path::new(&path)).unwrap());
    let j = journal(&db);
    let lease = j.acquire("child").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    if mode == "tools_pending_kill" {
        lease.pending(&pending_state()).unwrap();
        let mut file = std::fs::OpenOptions::new().create(true).append(true)
            .open(std::path::Path::new(&path).join("effect.txt")).unwrap();
        std::io::Write::write_all(&mut file, b"once\n").unwrap();
        file.sync_all().unwrap();
        std::fs::write(std::path::Path::new(&path).join("tool-returned"), b"ready").unwrap();
        loop { std::thread::park(); }
    } else if mode != "inflight" {
        lease.complete(&state()).unwrap();
    }
    if mode == "released" {
        lease.release().unwrap();
    }
    // Simulate process loss without any database/lease Drop or graceful shutdown.
    unsafe { libc::_exit(0) }
}

fn pending_state() -> Value {
    let mut value = state();
    value["tools_pending"] = json!(true);
    value["usage"] = json!([{"total_tokens":23,"cost_usd":0.01}]);
    value["messages"][1] = json!({"role":"assistant","tool_calls":[
        {"id":"write-1","type":"function","function":{"name":"write_file","arguments":"{\"path\":\"effect.txt\",\"append\":true,\"content\":\"once\\n\"}"}}
    ]});
    value
}

#[test]
#[cfg(target_os = "linux")]
fn paid_tool_frontier_survives_sigkill_and_never_replays_the_write() {
    struct Child(std::process::Child);
    impl Drop for Child {
        fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
    }
    let directory = tempfile::tempdir().unwrap();
    drop(AuthorityDatabase::initialize(directory.path()).unwrap());
    let mut child = Child(std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact","native_checkpoint_store::adapter::tests::checkpoint_crash_child","--nocapture"])
        .env("TOFU_CHECKPOINT_STORE_TEST_DIRECTORY", directory.path())
        .env("TOFU_CHECKPOINT_STORE_TEST_STAGE", "tools_pending_kill")
        .stdout(std::process::Stdio::null()).spawn().unwrap());
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while !directory.path().join("tool-returned").exists() {
        assert!(child.0.try_wait().unwrap().is_none(), "writer exited before its durable barrier");
        assert!(std::time::Instant::now() < deadline, "writer did not reach the tool boundary");
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    child.0.kill().unwrap();
    assert!(!child.0.wait().unwrap().success());
    let db = store(AuthorityDatabase::open(directory.path()).unwrap());
    let j = journal(&db);
    assert_eq!(j.acquire("ordinary-retry").err(),Some(Error::Ineligible));
    let lease = j.recover("verified-dead-writer").unwrap();
    let mut saved = lease.load().unwrap().unwrap();
    assert_eq!(j.recover("competing-recovery").err(),Some(Error::Ineligible));
    assert_eq!(saved,pending_state());
    assert_eq!(std::fs::read(directory.path().join("effect.txt")).unwrap(),b"once\n");
    saved.as_object_mut().unwrap().remove("tools_pending");
    saved["messages"].as_array_mut().unwrap().push(json!({"role":"tool","tool_call_id":"write-1","content":"existing publication proved"}));
    lease.complete(&saved).unwrap();
    lease.begin(1).unwrap();
    assert_eq!(std::fs::read(directory.path().join("effect.txt")).unwrap(),b"once\n");
    assert_eq!(saved["usage"],json!([{"total_tokens":23,"cost_usd":0.01}]));
}

#[test]
fn abrupt_process_loss_recovers_only_released_completed_frontier() {
    for mode in ["inflight", "completed", "released"] {
        let directory = tempfile::tempdir().unwrap();
        drop(AuthorityDatabase::initialize(directory.path()).unwrap());
        let result = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "native_checkpoint_store::adapter::tests::checkpoint_crash_child",
                "--nocapture",
            ])
            .env("TOFU_CHECKPOINT_STORE_TEST_DIRECTORY", directory.path())
            .env("TOFU_CHECKPOINT_STORE_TEST_STAGE", mode)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{mode}: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        let db = store(AuthorityDatabase::open(directory.path()).unwrap());
        let j = journal(&db);
        if mode == "released" {
            let lease = j.acquire("parent").unwrap();
            assert_eq!(lease.load().unwrap(), Some(state()));
            lease.begin(1).unwrap();
            lease.seal().unwrap();
            assert!(j.acquire("later").is_err());
        } else {
            assert_eq!(j.acquire("parent").err(), Some(Error::Ineligible));
            if mode == "completed" {
                #[cfg(target_os = "linux")]
                {
                    let old = db.header(7, "task").unwrap().unwrap();
                    let lease = j.recover("parent").unwrap();
                    let new = db.header(7, "task").unwrap().unwrap();
                    assert!(
                        new.value["lease_generation"].as_u64().unwrap()
                            > old.value["lease_generation"].as_u64().unwrap()
                    );
                    assert_eq!(lease.load().unwrap(), Some(state()));
                    assert_eq!(j.recover("competing").err(), Some(Error::Ineligible));
                    lease.begin(1).unwrap();
                }
            } else {
                assert_eq!(j.recover("parent").err(), Some(Error::Ineligible));
            }
        }
    }
}

#[test]
fn real_store_cas_has_one_owner_and_bounds_reads_before_decoding() {
    let directory = tempfile::tempdir().unwrap();
    let db = Arc::new(store(
        AuthorityDatabase::initialize(directory.path()).unwrap(),
    ));
    let barrier = Arc::new(std::sync::Barrier::new(8));
    let threads = (0..8)
        .map(|i| {
            let db = db.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                let j = journal(db.as_ref());
                barrier.wait();
                j.acquire(&format!("worker-{i}")).is_ok()
            })
        })
        .collect::<Vec<_>>();
    assert_eq!(
        threads
            .into_iter()
            .map(|t| usize::from(t.join().unwrap()))
            .sum::<usize>(),
        1
    );
    let small = NativeCheckpointStore::new(db.authority.clone(), 3, 7, 8, 32).unwrap();
    assert!(small.header(7, "task").is_err());
    let header = db.header(7, "task").unwrap().unwrap();
    assert_eq!(
        db.compare_exchange(7, "task", 0, &header.value, SnapshotWrite::Keep),
        Err(Error::Conflict)
    );
    assert_eq!(
        db.compare_exchange(
            8,
            "task",
            header.version,
            &header.value,
            SnapshotWrite::Keep
        ),
        Err(Error::Invalid)
    );
}

#[test]
fn write_and_sync_failures_poison_lease_and_never_publish_split_frontier() {
    for operation in [Operation::Write, Operation::SyncData] {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/checkpoint")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let database = AuthorityDatabase::initialize_with_vfs(
            std::path::Path::new("/checkpoint"),
            vfs.clone() as Arc<dyn Vfs>,
        )
        .unwrap();
        let db = store(database);
        let j = journal(&db);
        let lease = j.acquire("first").unwrap();
        lease.load().unwrap();
        lease.begin(0).unwrap();
        vfs.arm_operation_fault(operation, 1, FaultAction::ErrorBefore(io::ErrorKind::Other))
            .unwrap();
        assert!(lease.complete(&state()).is_err());
        assert!(vfs.operation_fault_fired().unwrap());
        assert_eq!(lease.begin(1), Err(Error::Poisoned));
        drop(lease);
        drop(j);
        drop(db);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let db = store(
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/checkpoint"), vfs).unwrap(),
        );
        let header = db.header(7, "task").unwrap().unwrap();
        assert_eq!(header.value["phase"], "inflight");
        assert_eq!(header.value["next_round"], 0);
        assert!(db.snapshot(7, "task", header.version).unwrap().is_none());
        assert!(journal(&db).acquire("restarted").is_err());
    }
}

#[test]
fn linked_provider_order_is_rejected_before_checkpoint_effects() {
    let directory = tempfile::tempdir().unwrap();
    let database = Arc::new(Mutex::new(
        AuthorityDatabase::initialize(directory.path()).unwrap(),
    ));
    let mut map = serde_json::Map::new();
    map.insert("z".into(), Value::Null);
    map.insert("a".into(), Value::Null);
    let insertion_order = map.keys().next().unwrap() == "z";
    let result = NativeCheckpointStore::new(database.clone(), 3, 7, 4096, 1048576);
    if insertion_order {
        assert_eq!(result.err(), Some(Error::Invalid));
    } else {
        assert!(result.is_ok());
    }
    let db = database.lock().unwrap();
    let mut tx = db.begin(3, 7).unwrap();
    let key = versioned_document::record_key(3, 7, HEADERS, "task").unwrap();
    assert!(db.entity_get(&mut tx, &key).unwrap().is_none());
}

#[test]
fn outbox_capture_replays_atomically_with_scope_and_latest_retry_fences() {
    let source_dir = tempfile::tempdir().unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let source = store(AuthorityDatabase::initialize(source_dir.path()).unwrap());
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
        .unwrap();
    let j = journal(&source);
    let lease = j.acquire("source").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    lease.complete(&state()).unwrap();
    lease.release().unwrap();
    let database = source.authority.lock().unwrap();
    let origin = database.authority_uuid().to_string();
    let pending = database.logical_outbox_pending(3, 7, 8).unwrap();
    assert_eq!(pending.len(), 4);
    let cipher = LogicalOutboxCipher::new(&[72; 32]);
    drop(database);
    let target = store(AuthorityDatabase::initialize(target_dir.path()).unwrap());
    assert_eq!(
        target.replay(&cipher, &origin, &pending[1].record),
        Err(Error::Conflict)
    );
    assert_eq!(
        target.replay(
            &LogicalOutboxCipher::new(&[73; 32]),
            &origin,
            &pending[0].record
        ),
        Err(Error::Invalid)
    );
    assert_eq!(
        target.replay(
            &cipher,
            &uuid::Uuid::new_v4().to_string(),
            &pending[0].record
        ),
        Err(Error::Invalid)
    );
    // Authenticated envelopes still require the closed mutation contract.
    for mode in 0..3 {
        let mut value: Value =
            serde_json::from_slice(&cipher.open(&pending[0].record).unwrap()).unwrap();
        match mode {
            0 => {
                value["unexpected"] = json!(true);
            }
            1 => {
                value.as_object_mut().unwrap().remove("snapshot");
            }
            _ => {
                value["owner_user_id"] = json!(8);
            }
        }
        let bytes = serde_json::to_vec(&value).unwrap();
        let mut identity = pending[0].record.identity.clone();
        identity.request_digest = Sha256::digest(&bytes).into();
        let forged = cipher.seal(identity, &bytes).unwrap();
        assert_eq!(
            target.replay(&cipher, &origin, &forged),
            Err(Error::Invalid)
        );
    }
    for (i, item) in pending.iter().enumerate() {
        assert_eq!(
            target.replay(&cipher, &origin, &item.record).unwrap(),
            i as u64 + 1
        );
        assert_eq!(
            target.replay(&cipher, &origin, &item.record).unwrap(),
            i as u64 + 1
        );
        let header = target.header(7, "task").unwrap().unwrap();
        assert_eq!(header.version, i as u64 + 1);
        assert_eq!(target.writer_quiescent(7, "task", &json!({})), Ok(false));
        let clear = cipher.open(&item.record).unwrap();
        let value: Value = serde_json::from_slice(&clear).unwrap();
        assert_eq!(value["snapshot"].is_null(), i != 2);
        let contract: Value = serde_json::from_str(include_str!(
            "../../../contracts/native_checkpoint_outbox_v1.schema.json"
        ))
        .unwrap();
        let actual = std::collections::BTreeSet::<_>::from_iter(
            value.as_object().unwrap().keys().map(String::as_str),
        );
        let required = std::collections::BTreeSet::<_>::from_iter(
            contract["required"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap()),
        );
        assert_eq!(actual, required);
        assert_eq!(value["format"], contract["properties"]["format"]["enum"][0]);
    }
    assert_eq!(
        target.replay(&cipher, &origin, &pending[0].record),
        Err(Error::Conflict)
    );
    assert_eq!(target.snapshot(7, "task", 4).unwrap(), Some(state()));
    assert_eq!(
        target.header(7, "task").unwrap().unwrap().value,
        source.header(7, "task").unwrap().unwrap().value
    );
    drop(target);
    let target = store(AuthorityDatabase::open(target_dir.path()).unwrap());
    assert_eq!(
        target.replay(&cipher, &origin, &pending[3].record).unwrap(),
        4
    );
    let resumed = journal(&target);
    assert_eq!(
        resumed.acquire("resumed").unwrap().load().unwrap(),
        Some(state())
    );
}

#[test]
fn outbox_failure_and_replay_fault_leave_one_whole_frontier() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/outbox")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let source = store(
        AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/outbox"), vfs.clone())
            .unwrap(),
    );
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
        .unwrap();
    let j = journal(&source);
    let lease = j.acquire("a").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    vfs.arm_operation_fault(
        Operation::SyncData,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    assert!(lease.complete(&state()).is_err());
    assert!(vfs.operation_fault_fired().unwrap());
    assert_eq!(lease.begin(1), Err(Error::Poisoned));
    drop(lease);
    drop(j);
    drop(source);
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let source =
        store(AuthorityDatabase::open_with_vfs(std::path::Path::new("/outbox"), vfs).unwrap());
    let mut db = source.authority.lock().unwrap();
    db.configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
        .unwrap();
    let pending = db.logical_outbox_pending(3, 7, 8).unwrap();
    assert_eq!(pending.len(), 2);
    let origin = db.authority_uuid().to_string();
    drop(db);
    assert_eq!(
        source.header(7, "task").unwrap().unwrap().value["phase"],
        "inflight"
    );
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/target")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let target = store(
        AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/target"), vfs.clone())
            .unwrap(),
    );
    vfs.arm_operation_fault(
        Operation::SyncData,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    let cipher = LogicalOutboxCipher::new(&[72; 32]);
    assert!(target.replay(&cipher, &origin, &pending[0].record).is_err());
    assert!(vfs.operation_fault_fired().unwrap());
    drop(target);
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let target =
        store(AuthorityDatabase::open_with_vfs(std::path::Path::new("/target"), vfs).unwrap());
    assert!(target.header(7, "task").unwrap().is_none());
    assert_eq!(
        target.replay(&cipher, &origin, &pending[0].record).unwrap(),
        1
    );
}

#[test]
fn whole_batch_retry_verifies_archive_prefix_then_acknowledges_source() {
    use crate::native_checkpoint_store::NativeCheckpointSink;
    use crate::outbox_publisher::{publish_batch, LogicalOutboxPublishBudget};
    use crate::outbox_sink::EngineLogicalOutboxSink;
    let source_dir = tempfile::tempdir().unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let archive_dir = tempfile::tempdir().unwrap();
    let source = store(AuthorityDatabase::initialize(source_dir.path()).unwrap());
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
        .unwrap();
    let j = journal(&source);
    let lease = j.acquire("source").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    lease.complete(&state()).unwrap();
    lease.release().unwrap();
    drop(lease);
    drop(j);
    let pending = source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap();
    let target = store(AuthorityDatabase::initialize(target_dir.path()).unwrap());
    let archive =
        EngineLogicalOutboxSink::initialize(archive_dir.path(), 3, 7, 1024 * 1024).unwrap();
    let origin = source.source_uuid().unwrap();
    let mut sink = NativeCheckpointSink::new(
        target,
        archive,
        LogicalOutboxCipher::new(&[72; 32]),
        origin.clone(),
    )
    .unwrap();
    let budget = LogicalOutboxPublishBudget::new(8, 1048576).unwrap();
    assert_eq!(
        publish_batch(&mut sink, &pending, budget)
            .unwrap()
            .receipts
            .len(),
        4
    );
    assert_eq!(
        source
            .authority
            .lock()
            .unwrap()
            .logical_outbox_pending(3, 7, 8)
            .unwrap()
            .len(),
        4
    );
    let (target, archive) = sink.into_parts();
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 4);
    assert_eq!(
        target.replay(
            &LogicalOutboxCipher::new(&[72; 32]),
            &origin,
            &pending[0].record
        ),
        Err(Error::Conflict)
    );
    drop(target);
    drop(archive);
    let target = store(AuthorityDatabase::open(target_dir.path()).unwrap());
    let archive = EngineLogicalOutboxSink::open(archive_dir.path(), 3, 7, 1024 * 1024).unwrap();
    let mut sink =
        NativeCheckpointSink::new(target, archive, LogicalOutboxCipher::new(&[72; 32]), origin)
            .unwrap();
    assert_eq!(
        source
            .publish_pending(&mut sink, budget)
            .unwrap()
            .receipts
            .len(),
        4
    );
    assert!(source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap()
        .is_empty());
    let (target, archive) = sink.into_parts();
    assert_eq!(archive.status().durable_sequence, 4);
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 4);
    assert_eq!(target.snapshot(7, "task", 4).unwrap(), Some(state()));
}

#[test]
fn archive_failure_and_replacement_cannot_forge_applied_prefix() {
    use crate::native_checkpoint_store::NativeCheckpointSink;
    use crate::outbox_publisher::{publish_batch, LogicalOutboxPublishBudget, LogicalOutboxSink};
    use crate::outbox_sink::EngineLogicalOutboxSink;
    let source_dir = tempfile::tempdir().unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let source = store(AuthorityDatabase::initialize(source_dir.path()).unwrap());
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
        .unwrap();
    let j = journal(&source);
    let lease = j.acquire("a").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    let pending = source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap();
    let origin = source.source_uuid().unwrap();
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/archive")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let archive = EngineLogicalOutboxSink::initialize_with_vfs(
        std::path::Path::new("/archive"),
        vfs.clone(),
        3,
        7,
        1048576,
    )
    .unwrap();
    let target = store(AuthorityDatabase::initialize(target_dir.path()).unwrap());
    let mut sink = NativeCheckpointSink::new(
        target,
        archive,
        LogicalOutboxCipher::new(&[72; 32]),
        origin.clone(),
    )
    .unwrap();
    vfs.arm_operation_fault(
        Operation::SyncData,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    assert!(sink.append_durable(&pending[0].record).is_err());
    assert!(vfs.operation_fault_fired().unwrap());
    assert!(sink.is_restart_required());
    let (target, archive) = sink.into_parts();
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 1);
    drop(archive);
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let archive = EngineLogicalOutboxSink::open_with_vfs(
        std::path::Path::new("/archive"),
        vfs.clone(),
        3,
        7,
        1048576,
    )
    .unwrap();
    let mut sink = NativeCheckpointSink::new(
        target,
        archive,
        LogicalOutboxCipher::new(&[72; 32]),
        origin.clone(),
    )
    .unwrap();
    let budget = LogicalOutboxPublishBudget::new(8, 1048576).unwrap();
    assert_eq!(
        publish_batch(&mut sink, &pending, budget)
            .unwrap()
            .receipts
            .len(),
        2
    );
    let (target, archive) = sink.into_parts();
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 2);
    drop(archive);
    let other_dir = tempfile::tempdir().unwrap();
    let other = EngineLogicalOutboxSink::initialize(other_dir.path(), 3, 7, 1048576).unwrap();
    let mut replacement =
        NativeCheckpointSink::new(target, other, LogicalOutboxCipher::new(&[72; 32]), origin)
            .unwrap();
    assert!(replacement.append_durable(&pending[0].record).is_err());
    let (target, archive) = replacement.into_parts();
    assert_eq!(archive.status().durable_sequence, 0);
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 2);
}

#[test]
fn source_ack_sync_failure_reopens_and_retries_exact_durable_delivery() {
    use crate::native_checkpoint_store::NativeCheckpointSink;
    use crate::outbox_publisher::{publish_batch, LogicalOutboxPublishBudget};
    use crate::outbox_sink::EngineLogicalOutboxSink;
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/source")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let source = store(
        AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/source"), vfs.clone())
            .unwrap(),
    );
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 1048576, 131072)
        .unwrap();
    let j = journal(&source);
    let lease = j.acquire("a").unwrap();
    lease.load().unwrap();
    drop(lease);
    drop(j);
    let pending = source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let archive_dir = tempfile::tempdir().unwrap();
    let target = store(AuthorityDatabase::initialize(target_dir.path()).unwrap());
    let archive = EngineLogicalOutboxSink::initialize(archive_dir.path(), 3, 7, 1048576).unwrap();
    let mut sink = NativeCheckpointSink::new(
        target,
        archive,
        LogicalOutboxCipher::new(&[72; 32]),
        source.source_uuid().unwrap(),
    )
    .unwrap();
    let budget = LogicalOutboxPublishBudget::new(8, 1048576).unwrap();
    publish_batch(&mut sink, &pending, budget).unwrap();
    vfs.arm_operation_fault(
        Operation::SyncData,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    assert!(source.publish_pending(&mut sink, budget).is_err());
    assert!(vfs.operation_fault_fired().unwrap());
    drop(source);
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let source =
        store(AuthorityDatabase::open_with_vfs(std::path::Path::new("/source"), vfs).unwrap());
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 1048576, 131072)
        .unwrap();
    assert_eq!(
        source
            .publish_pending(&mut sink, budget)
            .unwrap()
            .receipts
            .len(),
        1
    );
    assert!(source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap()
        .is_empty());
    let (target, archive) = sink.into_parts();
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 1);
    assert_eq!(archive.status().durable_sequence, 1);
}

#[test]
fn persisted_task_terminal_fence_survives_reopen_and_delegate_failure() {
    use tofu_runtime::{
        checkpoint_journal::{OwnedLease, OwnedPolicy, Recovery},
        session::Registry,
        task::{
            PortError, Task, TaskError, TaskIdentity, TerminalCandidate, TerminalPersistence,
            TerminalPublisher,
        },
    };
    struct Restore;
    impl Recovery for Restore {
        fn restore(
            &self,
            _: tofu_runtime::agent_checkpoint::Identity<'_>,
            _: &Value,
        ) -> Result<(), Value> {
            Ok(())
        }
    }
    struct Ports {
        store: Arc<NativeCheckpointStore>,
        fail: std::sync::atomic::AtomicBool,
        published: std::sync::atomic::AtomicUsize,
    }
    impl TerminalPersistence for Ports {
        fn persist_once(&self, _: &TerminalCandidate) -> Result<(), PortError> {
            assert_eq!(
                self.store.header(7, "task").unwrap().unwrap().value["phase"],
                "terminal"
            );
            if self.fail.swap(false, std::sync::atomic::Ordering::AcqRel) {
                return Err(PortError);
            }
            Ok(())
        }
    }
    impl TerminalPublisher for Ports {
        fn publish_once(&self, _: &TerminalCandidate, _: &Value) -> Result<(), PortError> {
            self.published
                .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(())
        }
    }
    let directory = tempfile::tempdir().unwrap();
    let db = Arc::new(store(
        AuthorityDatabase::initialize(directory.path()).unwrap(),
    ));
    let j = journal(db.as_ref());
    let lease = OwnedLease::acquire(scope(), db.clone(), 1048576, 1024, "run").unwrap();
    lease.load().unwrap();
    lease.begin(0).unwrap();
    let policy = OwnedPolicy::new(lease, Arc::new(Restore), false);
    let ports = Arc::new(Ports {
        store: db.clone(),
        fail: true.into(),
        published: 0.into(),
    });
    let persistence = policy.terminal_persistence(ports.clone()).unwrap();
    let sessions = Registry::new();
    let session = sessions.start("task", "task", 7, "", None).unwrap();
    let task = Task::new(
        TaskIdentity {
            owner_user_id: 7,
            task_id: "task".into(),
        },
        session,
        Some(persistence),
        ports.clone(),
    )
    .unwrap();
    let work = std::sync::atomic::AtomicUsize::new(0);
    assert_eq!(
        task.run(7, |_| {
            work.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            Ok(json!("done"))
        })
        .unwrap_err(),
        TaskError::PersistencePending
    );
    assert!(j.acquire("raced-recovery").is_err());
    assert_eq!(ports.published.load(std::sync::atomic::Ordering::SeqCst), 0);
    task.finish(7, Some(json!("replacement")), None).unwrap();
    assert_eq!(work.load(std::sync::atomic::Ordering::SeqCst), 1);
    assert_eq!(ports.published.load(std::sync::atomic::Ordering::SeqCst), 1);
    drop(task);
    drop(ports);
    drop(policy);
    drop(j);
    drop(db);
    let db = store(AuthorityDatabase::open(directory.path()).unwrap());
    assert_eq!(
        db.header(7, "task").unwrap().unwrap().value["phase"],
        "terminal"
    );
    assert!(journal(&db).acquire("restart").is_err());
}

fn manual_barrier_store() -> (Arc<NativeCheckpointStore>, crate::wal::WalBarrier, Arc<DeterministicVfs>) {
    manual_barrier_store_with_durability(crate::wal::WalDurability::Strict)
}
fn manual_barrier_store_with_durability(durability: crate::wal::WalDurability) -> (
    Arc<NativeCheckpointStore>, crate::wal::WalBarrier, Arc<DeterministicVfs>,
) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/data")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
    database.set_wal_durability(durability).unwrap();
    let barrier = crate::wal::WalBarrier::default();
    database.attach_wal_barrier(&barrier).unwrap();
    let db = Arc::new(store(database));
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    (db, barrier, vfs)
}
#[test]
fn shared_wal_checkpoint_cas_and_reads_wait_outside_writer_for_durable_prefix() {
    checkpoint_cas_and_reads_wait_for_durable_prefix(crate::wal::WalDurability::Strict);
}
#[test]
fn normal_wal_checkpoint_cas_and_reads_wait_outside_writer_for_durable_prefix() {
    checkpoint_cas_and_reads_wait_for_durable_prefix(crate::wal::WalDurability::Normal);
}
fn checkpoint_cas_and_reads_wait_for_durable_prefix(durability: crate::wal::WalDurability) {
    use crate::native_checkpoint_store::test_support::{wait_for_append, BarrierTask};
    use tofu_runtime::checkpoint_journal::OwnedLease;
    let (db, barrier, vfs) = manual_barrier_store_with_durability(durability);
    let before = db.authority.lock().unwrap().wal_len().unwrap();
    let target = db.clone();
    let acquire = BarrierTask::spawn(&barrier, move || {
        OwnedLease::acquire(scope(), target, 1048576, 1024, "a")
    });
    wait_for_append(&db.authority, before);
    assert!(!acquire.is_finished(), "unflushed lease was acknowledged");
    assert!(
        db.authority.try_lock().is_ok(),
        "WAL waiter retained the writer"
    );
    let target = db.clone();
    let read = BarrierTask::spawn(&barrier, move || target.header(7, "task"));
    std::thread::sleep(std::time::Duration::from_millis(10));
    assert!(!read.is_finished(), "unflushed header was returned");
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    let lease = Arc::new(acquire.finish().unwrap());
    assert_eq!(read.finish().unwrap().unwrap().value["phase"], "new");
    lease.load().unwrap();
    let before = db.authority.lock().unwrap().wal_len().unwrap();
    let active = lease.clone();
    let begin = BarrierTask::spawn(&barrier, move || active.begin(0));
    wait_for_append(&db.authority, before);
    assert!(!begin.is_finished());
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    begin.finish().unwrap();
    let before = db.authority.lock().unwrap().wal_len().unwrap();
    let active = lease.clone();
    let complete = BarrierTask::spawn(&barrier, move || active.complete(&state()));
    wait_for_append(&db.authority, before);
    assert!(!complete.is_finished());
    let target = db.clone();
    let version = {
        let database = db.authority.lock().unwrap();
        let mut tx = database.begin(3, 7).unwrap();
        let key = versioned_document::record_key(3, 7, HEADERS, "task").unwrap();
        versioned_document::get_value_with_version(&database, &mut tx, &key, HEADERS, "task")
            .unwrap()
            .unwrap()
            .1
    };
    let snapshot = BarrierTask::spawn(&barrier, move || target.snapshot(7, "task", version));
    std::thread::sleep(std::time::Duration::from_millis(10));
    assert!(!snapshot.is_finished());
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    complete.finish().unwrap();
    assert_eq!(snapshot.finish().unwrap(), Some(state()));
}
#[test]
fn shared_wal_checkpoint_flush_failure_never_certifies_committed_metadata_or_retry() {
    checkpoint_flush_failure_never_certifies_metadata(crate::wal::WalDurability::Strict);
}
#[test]
fn normal_wal_checkpoint_flush_failure_never_certifies_committed_metadata_or_retry() {
    checkpoint_flush_failure_never_certifies_metadata(crate::wal::WalDurability::Normal);
}
fn checkpoint_flush_failure_never_certifies_metadata(durability: crate::wal::WalDurability) {
    use crate::native_checkpoint_store::test_support::{wait_for_append, BarrierTask};
    use tofu_runtime::checkpoint_journal::OwnedLease;
    let (db, barrier, vfs) = manual_barrier_store_with_durability(durability);
    let before = db.authority.lock().unwrap().wal_len().unwrap();
    let target = db.clone();
    let acquire = BarrierTask::spawn(&barrier, move || {
        OwnedLease::acquire(scope(), target, 1048576, 1024, "a")
    });
    wait_for_append(&db.authority, before);
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    let lease = Arc::new(acquire.finish().unwrap());
    lease.load().unwrap();
    let before = db.authority.lock().unwrap().wal_len().unwrap();
    let active = lease.clone();
    let begin = BarrierTask::spawn(&barrier, move || active.begin(0));
    wait_for_append(&db.authority, before);
    barrier.inject_background_error_for_test(std::io::Error::other("physical WAL sync failed"));
    assert_eq!(begin.finish(), Err(Error::Storage));
    assert_eq!(lease.begin(0), Err(Error::Poisoned));
    assert_eq!(lease.release(), Err(Error::Poisoned));
    assert!(matches!(db.header(7, "task"), Err(Error::Storage)));
    assert!(db.is_restart_required());
    assert!(NativeCheckpointStore::new(db.authority.clone(), 3, 7, 4096, 1048576).is_err());
}
#[test]
fn checkpoint_store_refuses_deferred_wal_without_a_barrier_before_any_lease_write() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database
        .set_wal_durability(crate::wal::WalDurability::Normal)
        .unwrap();
    let database = Arc::new(Mutex::new(database));
    assert!(NativeCheckpointStore::new(database.clone(), 3, 7, 4096, 1048576).is_err());
    let database = database.lock().unwrap();
    let mut transaction = database.begin(3, 7).unwrap();
    let key = versioned_document::record_key(3, 7, HEADERS, "task").unwrap();
    assert!(versioned_document::get_value_with_version(
        &database,
        &mut transaction,
        &key,
        HEADERS,
        "task"
    )
    .unwrap()
    .is_none());
}

#[test]
fn checkpoint_passive_replay_and_current_retry_refuse_unconfirmed_shared_wal() {
    use crate::native_checkpoint_store::test_support::{wait_for_append, BarrierTask};
    let directory = tempfile::tempdir().unwrap();
    let source = store(AuthorityDatabase::initialize(directory.path()).unwrap());
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 1048576, 131072)
        .unwrap();
    let journal = journal(&source);
    let lease = journal.acquire("a").unwrap();
    lease.load().unwrap();
    let record = source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap()
        .remove(0)
        .record;
    let origin = source.source_uuid().unwrap();
    let (target, barrier, _vfs) = manual_barrier_store();
    let before = target.authority.lock().unwrap().wal_len().unwrap();
    let active = target.clone();
    let uuid = origin.clone();
    let frame = record.clone();
    let replay = BarrierTask::spawn(&barrier, move || {
        active.replay(&LogicalOutboxCipher::new(&[72; 32]), &uuid, &frame)
    });
    wait_for_append(&target.authority, before);
    assert!(!replay.is_finished());
    assert!(target.authority.try_lock().is_ok());
    barrier.inject_background_error_for_test(std::io::Error::other("passive WAL sync failed"));
    assert_eq!(replay.finish(), Err(Error::Storage));
    assert_eq!(
        target.replay(&LogicalOutboxCipher::new(&[72; 32]), &origin, &record),
        Err(Error::Storage)
    );
}
#[test]
fn checkpoint_source_ack_waits_for_its_shared_wal_after_target_and_archive_delivery() {
    use crate::native_checkpoint_store::{
        test_support::{wait_for_append, BarrierTask},
        NativeCheckpointSink,
    };
    use crate::outbox_publisher::LogicalOutboxPublishBudget;
    use crate::outbox_sink::EngineLogicalOutboxSink;
    let directory = tempfile::tempdir().unwrap();
    let source = Arc::new(store(
        AuthorityDatabase::initialize(directory.path()).unwrap(),
    ));
    source
        .authority
        .lock()
        .unwrap()
        .configure_logical_outbox(&[72; 32], 1048576, 131072)
        .unwrap();
    let journal = journal(source.as_ref());
    let lease = journal.acquire("a").unwrap();
    lease.load().unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let archive_dir = tempfile::tempdir().unwrap();
    let target = store(AuthorityDatabase::initialize(target_dir.path()).unwrap());
    let archive = EngineLogicalOutboxSink::initialize(archive_dir.path(), 3, 7, 1048576).unwrap();
    let mut sink = NativeCheckpointSink::new(
        target,
        archive,
        LogicalOutboxCipher::new(&[72; 32]),
        source.source_uuid().unwrap(),
    )
    .unwrap();
    let barrier = crate::wal::WalBarrier::default();
    source
        .authority
        .lock()
        .unwrap()
        .attach_wal_barrier(&barrier)
        .unwrap();
    barrier.sync_for_test(&crate::vfs::RealVfs).unwrap();
    let before = source.authority.lock().unwrap().wal_len().unwrap();
    let active = source.clone();
    let publish = BarrierTask::spawn(&barrier, move || {
        let result = active.publish_pending(
            &mut sink,
            LogicalOutboxPublishBudget::new(8, 131072).unwrap(),
        );
        (result, sink)
    });
    wait_for_append(&source.authority, before);
    assert!(!publish.is_finished());
    assert!(source.authority.try_lock().is_ok());
    barrier.sync_for_test(&crate::vfs::RealVfs).unwrap();
    let (result, sink) = publish.finish();
    assert_eq!(result.unwrap().receipts.len(), 1);
    assert!(source
        .authority
        .lock()
        .unwrap()
        .logical_outbox_pending(3, 7, 8)
        .unwrap()
        .is_empty());
    let (target, archive) = sink.into_parts();
    assert_eq!(target.header(7, "task").unwrap().unwrap().version, 1);
    assert_eq!(archive.status().durable_sequence, 1);
}

#[test]
#[ignore = "manual pending checkpoint component measurement"]
fn measure_pending_checkpoint_cost() {
    for bytes in [32768,262144] {
        for pending in [false,true] {
            let directory=tempfile::tempdir().unwrap();
            let db=store(AuthorityDatabase::initialize(directory.path()).unwrap());
            let j=journal(&db);let lease=j.acquire("measurement").unwrap();lease.load().unwrap();
            let mut timings=Vec::new();let mut logical_bytes=0;
            for index in 0..40 {
                lease.begin(index).unwrap();
                let mut saved=pending_state();
                saved["messages"][0]["content"]=json!("x".repeat(bytes));
                saved["next_round"]=json!(index+1);saved["tool_calls"]=json!(index+1);
                saved["round_limit"]=Value::Null;saved["tool_limit"]=Value::Null;
                saved["usage"]=json!(vec![json!({"total_tokens":23});index as usize+1]);
                let mut complete=saved.clone();complete.as_object_mut().unwrap().remove("tools_pending");
                complete["messages"].as_array_mut().unwrap().push(json!({"role":"tool","tool_call_id":"write-1","content":"observed"}));
                let start=std::time::Instant::now();
                if pending { lease.pending(&saved).unwrap(); }
                lease.complete(&complete).unwrap();
                let elapsed=start.elapsed().as_secs_f64()*1000.0;
                if index>=8 {
                    timings.push(elapsed);
                    logical_bytes+=serde_json::to_vec(&complete).unwrap().len();
                    if pending {logical_bytes+=serde_json::to_vec(&saved).unwrap().len();}
                }
            }
            timings.sort_by(f64::total_cmp);
            println!("PENDING_COST {}",json!({"source_bytes":bytes,"pending":pending,"samples":timings.len(),"p50_ms":timings[timings.len()/2],"p95_ms":timings[timings.len()*95/100],"logical_snapshot_bytes_per_round":logical_bytes/timings.len(),"snapshot_replacements_per_round":if pending {2} else {1},"settlement_snapshot_reads_per_round":usize::from(pending)}));
        }
    }
}
