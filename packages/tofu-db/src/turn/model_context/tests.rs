//! Real authority contracts for persisted context certification and invalidation.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};
use std::{path::Path, sync::Arc};
const CONVERSATION: &str = "context-frontier";
fn append(db: &mut AuthorityDatabase, index: usize) {
    append_projection(db, index, json!({"content":format!("content-{index}")}));
}
fn append_projection(db: &mut AuthorityDatabase, index: usize, projection: Value) {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(
        db,
        &mut tx,
        &AppendSettledRequest {
            conversation_id: CONVERSATION.into(),
            actor: "human".into(),
            status: "completed".into(),
            projection_json: serde_json::to_vec(&projection).unwrap(),
            projection_timestamp: None,
            settlement_json: br#"{"outcome":"completed","cause":"ingested","resumeOptions":[]}"#
                .to_vec(),
            lane_id: "main".into(),
            command_id: format!("append-{index}"),
            kind: "message".into(),
            run_id: String::new(),
            turn_id: format!("turn-{index}"),
            attempt_id: None,
            created_at_ms: 1000 + index as u64,
            committed_at_ms: 1000 + index as u64,
            defaults: TurnDefaults {
                allow_create: true,
                title: "Frontier".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1000,
            },
        },
    )
    .unwrap();
    db.commit(tx).unwrap();
}
fn context(db: &AuthorityDatabase, index: usize, replay: bool) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    let bytes = read(
        db,
        &mut tx,
        CONVERSATION,
        &format!("turn-{index}"),
        replay,
        None,
    )
    .unwrap()
    .unwrap();
    serde_json::from_slice(&bytes.into_bytes()).unwrap()
}
fn turn(db: &AuthorityDatabase, index: usize) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    read_turn(db, &mut tx, CONVERSATION, &format!("turn-{index}"))
        .unwrap()
        .unwrap()
        .0
}
fn candidate(db: &AuthorityDatabase, index: usize) -> Map<String, Value> {
    let read = context(db, index, true);
    let row = turn(db, index);
    json!({"messages":[{"role":"assistant","content":format!("through-{index}")}],
        "turns":[crate::model_continuation::settled_witness(row.as_object().unwrap())],
        "historyProof":read["historyProof"]})
    .as_object()
    .unwrap()
    .clone()
}
fn stage_candidate(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    value: Map<String, Value>,
) -> Vec<u8> {
    crate::model_continuation::execute(
        db,
        tx,
        &crate::model_continuation::Request::Put {
            conversation_id: CONVERSATION.into(),
            lane_id: "main".into(),
            projection: value,
            updated_at_ms: 99999,
        },
    )
    .unwrap()
    .unwrap()
}
fn save(db: &mut AuthorityDatabase, index: usize) -> usize {
    let value = candidate(db, index);
    let mut tx = db.begin(7, 11).unwrap();
    SCANNED_HEADERS.with(|n| n.set(0));
    assert_eq!(stage_candidate(db, &mut tx, value), b"true");
    let count = SCANNED_HEADERS.with(|n| n.get());
    db.commit(tx).unwrap();
    count
}
fn edit(db: &AuthorityDatabase, tx: &mut AuthorityTransaction, index: usize, projection: Value) {
    let row = turn(db, index);
    update_projection(
        db,
        tx,
        &ProjectionUpdateRequest {
            preserve_activity: false,
            conversation_id: CONVERSATION.into(),
            turn_id: format!("turn-{index}"),
            projection_json: serde_json::to_vec(&projection).unwrap(),
            expected_projection_revision: row["projectionRevision"].as_u64().unwrap(),
            updated_at_ms: 9999,
            committed_at_ms: 9999,
        },
    )
    .unwrap();
}
fn certificate(db: &AuthorityDatabase) -> Option<frontier::Frontier> {
    let mut tx = db.begin(7, 11).unwrap();
    frontier::load(db, &mut tx, CONVERSATION, "main").unwrap()
}
fn stored_certificate(db: &AuthorityDatabase) -> Option<frontier::Frontier> {
    let mut tx = db.begin(7, 11).unwrap();
    let key = crate::model_continuation::lane_index_key(
        &tx,
        &crate::model_continuation::continuation_raw_key(CONVERSATION, "main"),
    )
    .unwrap();
    db.entity_get(&mut tx, &key)
        .unwrap()
        .filter(|v| !v.is_empty())
        .map(|bytes| frontier::Frontier::decode(&key, &bytes).unwrap())
}
#[test]
fn a_turn_above_one_projection_page_travels_as_its_page_zero_head() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    append(&mut db, 0);
    let round = json!({"toolName": "read", "toolContent": "x".repeat(1 << 20)});
    append_projection(&mut db, 1, json!({"content": "paged", "toolRounds": vec![round; 9]}));
    append(&mut db, 2);
    let read = context(&db, 2, false);
    let mut tx = db.begin(7, 11).unwrap();
    let page = super::super::projection_page::read(&db, &mut tx, CONVERSATION, "turn-1", None, 0)
        .unwrap()
        .unwrap();
    let first: Value = serde_json::from_slice(&page).unwrap();
    assert!(first["pageCount"].as_u64().unwrap() > 1);
    assert_eq!(
        read["turns"],
        json!([turn(&db, 0), {"pagedTurn": first["turn"]}, turn(&db, 2)])
    );
    assert_eq!((read["nextTurnId"].clone(), read["proofEligible"].clone()), (Value::Null, json!(true)));
    let small = turn(&db, 0);
    assert_eq!(
        super::super::projection_page::paged_head(small.clone()).unwrap(),
        (small, false)
    );
}
#[test]
fn a_paged_row_names_its_turn_as_the_cursor_and_keeps_the_media_fence() {
    let mut page = Page::new(CONVERSATION, "target", None, None, None);
    page.turns.push(json!({"pagedTurn": {"turnId": "paged",
        "projection": {"attachments": [{"id": "media"}]}}}));
    page.full = true;
    let response: Value = serde_json::from_slice(
        &page
            .respond(0, 1, "0".repeat(64), "replay_disabled", None)
            .unwrap()
            .into_bytes(),
    )
    .unwrap();
    assert_eq!(response["nextTurnId"], "paged");
    assert_eq!(response["proofEligible"], false);
}
#[test]
fn authenticated_continuation_read_and_publication_work_stays_constant_as_history_grows() {
    for n in [8, 128, 2048] {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..n {
            append(&mut db, index);
        }
        assert_eq!(
            save(&mut db, n - 1),
            n,
            "first certification accounts for the full prefix"
        );
        append(&mut db, n);
        append(&mut db, n + 1);
        let cold = context(&db, n + 1, false);
        SCANNED_HEADERS.with(|v| v.set(0));
        let warm = context(&db, n + 1, true);
        let read_headers = SCANNED_HEADERS.with(|v| v.get());
        assert_eq!(read_headers, 3);
        assert_eq!(warm["covered"], n);
        assert_eq!(warm["historyProof"], cold["historyProof"]);
        assert_eq!(
            warm["turns"],
            Value::Array(cold["turns"].as_array().unwrap()[n..].to_vec())
        );
        let next_write_headers = save(&mut db, n + 1);
        assert_eq!(next_write_headers, 3);
        // Equal-frontier replay remains write-free, including private metadata.
        let replay = candidate(&db, n + 1);
        let wal = db.wal_len().unwrap();
        let mut tx = db.begin(7, 11).unwrap();
        SCANNED_HEADERS.with(|v| v.set(0));
        assert_eq!(stage_candidate(&db, &mut tx, replay), b"true");
        assert_eq!(SCANNED_HEADERS.with(|v| v.get()), 0);
        drop(tx);
        assert_eq!(db.wal_len().unwrap(), wal);
        println!("context_frontier_work history={n} initial_save_headers={n} suffix_read_headers={read_headers} advancing_save_headers={next_write_headers}");
        drop(db);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        assert!(stored_certificate(&db).is_some());
        assert!(
            certificate(&db).is_none(),
            "reopen never trusts an earlier writer epoch"
        );
    }
}
#[test]
fn display_edits_keep_certificate_and_real_prefix_edits_clear_it_without_resurrection() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    for index in 0..8 {
        append(&mut db, index);
    }
    save(&mut db, 5);
    let mut tx = db.begin(7, 11).unwrap();
    edit(
        &db,
        &mut tx,
        0,
        json!({"content":"content-0","translation":"display"}),
    );
    db.commit(tx).unwrap();
    assert!(certificate(&db).is_some());
    assert_eq!(context(&db, 7, true)["covered"], 6);
    let mut tx = db.begin(7, 11).unwrap();
    edit(&db, &mut tx, 0, json!({"content":"edited"}));
    db.commit(tx).unwrap();
    assert!(certificate(&db).is_none());
    assert_eq!(context(&db, 7, true)["covered"], 0);
    let mut tx = db.begin(7, 11).unwrap();
    edit(&db, &mut tx, 0, json!({"content":"content-0"}));
    db.commit(tx).unwrap();
    drop(db);
    let db = AuthorityDatabase::open(directory.path()).unwrap();
    assert!(certificate(&db).is_none());
    assert_eq!(context(&db, 7, true)["covered"], 0);
}
#[test]
fn older_edit_and_frontier_advance_conflict_in_either_commit_order() {
    for edit_first in [true, false] {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..10 {
            append(&mut db, index);
        }
        save(&mut db, 5);
        let value = candidate(&db, 9);
        let mut advance = db.begin(7, 11).unwrap();
        assert_eq!(stage_candidate(&db, &mut advance, value), b"true");
        let mut mutation = db.begin(7, 11).unwrap();
        // This ordinal is beyond the OLD anchor, but inside the concurrently minted NEW prefix.
        edit(&db, &mut mutation, 7, json!({"content":"concurrent edit"}));
        let (first, second) = if edit_first {
            (mutation, advance)
        } else {
            (advance, mutation)
        };
        db.commit(first).unwrap();
        assert!(
            db.commit(second).is_err(),
            "OCC must cover both certificate and newly scanned range"
        );
    }
}
fn simulated() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    (vfs, db)
}
#[test]
fn wal_failure_and_crash_never_publish_an_edited_prefix_with_its_old_certificate() {
    for operation in [Operation::Write, Operation::SyncData] {
        let (vfs, mut db) = simulated();
        for index in 0..8 {
            append(&mut db, index);
        }
        save(&mut db, 5);
        let mut tx = db.begin(7, 11).unwrap();
        edit(&db, &mut tx, 0, json!({"content":"edited"}));
        vfs.arm_operation_fault(operation, 1, FaultAction::ErrorBefore(io::ErrorKind::Other))
            .unwrap();
        assert!(db.commit(tx).is_err());
        assert!(vfs.operation_fault_fired().unwrap());
        vfs.arm_fault(None).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let row = turn(&db, 0);
        let warm = context(&db, 7, true);
        let cold = context(&db, 7, false);
        assert_eq!(warm["historyProof"], cold["historyProof"]);
        if row["projection"]["content"] == "edited" {
            assert!(certificate(&db).is_none());
            assert_eq!(warm["covered"], 0);
        } else {
            assert_eq!(row["projection"]["content"], "content-0");
            assert!(stored_certificate(&db).is_some());
            assert!(certificate(&db).is_none());
            assert_eq!(warm["covered"], 6);
        }
    }
}
#[test]
fn wal_failure_and_crash_keep_continuation_and_its_certificate_atomic() {
    for operation in [Operation::Write, Operation::SyncData] {
        let (vfs, mut db) = simulated();
        for index in 0..10 {
            append(&mut db, index);
        }
        save(&mut db, 5);
        let value = candidate(&db, 7);
        let mut tx = db.begin(7, 11).unwrap();
        assert_eq!(stage_candidate(&db, &mut tx, value), b"true");
        vfs.arm_operation_fault(operation, 1, FaultAction::ErrorBefore(io::ErrorKind::Other))
            .unwrap();
        assert!(db.commit(tx).is_err());
        assert!(vfs.operation_fault_fired().unwrap());
        vfs.arm_fault(None).unwrap();
        drop(db);
        vfs.crash().unwrap();
        let db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(certificate(&db).is_none());
        let cert = stored_certificate(&db).unwrap();
        let mut tx = db.begin(7, 11).unwrap();
        let value = crate::model_continuation::execute(
            &db,
            &mut tx,
            &crate::model_continuation::Request::Get {
                conversation_id: CONVERSATION.into(),
                lane_id: "main".into(),
            },
        )
        .unwrap()
        .unwrap();
        let projection: Value = serde_json::from_slice(&value).unwrap();
        assert!(cert.matches(&projection));
        assert!(matches!(cert.count, 5 | 7));
        assert_eq!(
            context(&db, 9, true)["historyProof"],
            context(&db, 9, false)["historyProof"]
        );
    }
}

#[test]
fn context_frontier_crash_child() {
    let Some(root) = std::env::var_os("TOFU_CONTEXT_FRONTIER_CRASH_ROOT") else {
        return;
    };
    let root = std::path::PathBuf::from(root);
    let mut db = AuthorityDatabase::initialize(&root.join("db")).unwrap();
    for index in 0..8 {
        append(&mut db, index);
    }
    save(&mut db, 5);
    if std::env::var("TOFU_CONTEXT_FRONTIER_CRASH_MODE").unwrap() == "invalidate" {
        let mut tx = db.begin(7, 11).unwrap();
        edit(&db, &mut tx, 0, json!({"content":"edited"}));
        db.commit(tx).unwrap();
    }
    // This follows the strict commit ACK; no authority close or drop occurs.
    std::fs::write(root.join("ack"), b"durable-command-returned").unwrap();
    loop {
        std::thread::park();
    }
}

#[cfg(unix)]
#[test]
fn acknowledged_frontier_and_invalidation_survive_actual_sigkill_without_close() {
    use std::os::unix::process::ExitStatusExt;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};
    struct Child(std::process::Child);
    impl Drop for Child {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    for mode in ["publish", "invalidate"] {
        let root = tempfile::tempdir().unwrap();
        let stderr_path = root.path().join("child.stderr");
        let mut child = Child(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "turn::model_context::tests::context_frontier_crash_child",
                    "--nocapture",
                ])
                .env("TOFU_CONTEXT_FRONTIER_CRASH_ROOT", root.path())
                .env("TOFU_CONTEXT_FRONTIER_CRASH_MODE", mode)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::from(std::fs::File::create(&stderr_path).unwrap()))
                .spawn()
                .unwrap(),
        );
        let deadline = Instant::now() + Duration::from_secs(30);
        while !root.path().join("ack").exists() {
            if let Some(status) = child.0.try_wait().unwrap() {
                panic!(
                    "child exited {status}: {}",
                    std::fs::read_to_string(&stderr_path).unwrap()
                );
            }
            assert!(
                Instant::now() < deadline,
                "child failed to produce a durable ACK"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        child.0.kill().unwrap();
        assert_eq!(child.0.wait().unwrap().signal(), Some(9));
        let db = AuthorityDatabase::open(&root.path().join("db")).unwrap();
        let warm = context(&db, 7, true);
        let cold = context(&db, 7, false);
        assert_eq!(warm["historyProof"], cold["historyProof"]);
        if mode == "publish" {
            assert!(stored_certificate(&db).is_some());
            assert!(certificate(&db).is_none());
            assert_eq!(warm["covered"], 6);
        } else {
            assert!(certificate(&db).is_none());
            assert_eq!(warm["covered"], 0);
            assert_eq!(turn(&db, 0)["projection"]["content"], "edited");
        }
    }
}

#[test]
fn reopened_epoch_recovers_by_full_validation_then_recertifies_and_replicas_share_frontier() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    for index in 0..10 {
        append(&mut db, index);
    }
    save(&mut db, 5);
    let epoch = db.event_read_epoch();
    assert!(
        AuthorityDatabase::open(directory.path()).is_err(),
        "one writer lease per directory"
    );
    let replica = db.build_read_replica().unwrap();
    assert_eq!(replica.event_read_epoch(), epoch);
    SCANNED_HEADERS.with(|n| n.set(0));
    assert_eq!(context(&replica, 7, true)["covered"], 6);
    assert_eq!(SCANNED_HEADERS.with(|n| n.get()), 3);
    drop(replica);
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    assert_ne!(db.event_read_epoch(), epoch);
    assert!(stored_certificate(&db).is_some());
    assert!(certificate(&db).is_none());
    SCANNED_HEADERS.with(|n| n.set(0));
    assert_eq!(context(&db, 7, true)["covered"], 6);
    assert_eq!(
        SCANNED_HEADERS.with(|n| n.get()),
        8,
        "reopen read validates the whole prefix"
    );
    assert_eq!(
        save(&mut db, 7),
        8,
        "first advancing save in this epoch reauthenticates"
    );
    SCANNED_HEADERS.with(|n| n.set(0));
    assert_eq!(context(&db, 9, true)["covered"], 8);
    assert_eq!(SCANNED_HEADERS.with(|n| n.get()), 3);
    assert_eq!(save(&mut db, 9), 3);
}

#[test]
fn writer_without_invalidation_cannot_reuse_an_earlier_epoch_certificate() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    for index in 0..8 {
        append(&mut db, index);
    }
    save(&mut db, 5);
    let mut tx = db.begin(7, 11).unwrap();
    let key = crate::model_continuation::lane_index_key(
        &tx,
        &crate::model_continuation::continuation_raw_key(CONVERSATION, "main"),
    )
    .unwrap();
    let original = db.entity_get(&mut tx, &key).unwrap().unwrap();
    drop(tx);
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    let mut tx = db.begin(7, 11).unwrap();
    edit(
        &db,
        &mut tx,
        0,
        json!({"content":"old writer changed prefix"}),
    );
    // Simulate an old binary: it leaves the previous private directory bytes
    // untouched while committing the changed source and compact index.
    db.entity_put(&mut tx, key.clone(), original.clone())
        .unwrap();
    db.commit(tx).unwrap();
    drop(db);
    let db = AuthorityDatabase::open(directory.path()).unwrap();
    let mut tx = db.begin(7, 11).unwrap();
    assert_eq!(db.entity_get(&mut tx, &key).unwrap().unwrap(), original);
    assert!(stored_certificate(&db).is_some());
    assert!(certificate(&db).is_none());
    SCANNED_HEADERS.with(|n| n.set(0));
    let read = context(&db, 7, true);
    // The stale anchor is refuted only at its position; the uncovered page
    // is then walked again from the lane head.
    assert_eq!(SCANNED_HEADERS.with(|n| n.get()), 8 + 8);
    assert_eq!(read["covered"], 0);
    assert_eq!(read["continuationStatus"], "history_changed");
    assert_eq!(read["historyProof"], context(&db, 7, false)["historyProof"]);
}

/// Production-shaped one-append transactions; the per-commit device barrier
/// is deferred while seeding because lane length, not WAL durability, is
/// under test. Reads and checkpoint saves run under the strict default.
fn long_lane(history: usize) -> (tempfile::TempDir, AuthorityDatabase) {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    db.set_wal_durability(crate::wal::WalDurability::Normal).unwrap();
    for index in 0..history {
        append(&mut db, index);
    }
    db.set_wal_durability(crate::wal::WalDurability::Strict).unwrap();
    (directory, db)
}

#[test]
fn lanes_beyond_ten_thousand_turns_keep_certified_and_cold_continuations() {
    // The removed ceiling refused every proof past 10,000 headers.
    const HISTORY: usize = 10_050;
    let (directory, mut db) = long_lane(HISTORY);
    assert_eq!(save(&mut db, HISTORY - 1), HISTORY, "first certification walks the lane once");
    assert_eq!(certificate(&db).unwrap().count, (HISTORY - 1) as u64);
    append(&mut db, HISTORY);
    append(&mut db, HISTORY + 1);
    SCANNED_HEADERS.with(|n| n.set(0));
    let warm = context(&db, HISTORY + 1, true);
    assert_eq!(SCANNED_HEADERS.with(|n| n.get()), 3, "certified reads walk terminal and suffix");
    assert_eq!(warm["covered"], HISTORY);
    assert_eq!(warm["continuationStatus"], "available");
    let cold = context(&db, HISTORY + 1, false);
    assert_eq!(cold["historyProof"], warm["historyProof"]);
    assert_eq!(cold["historyProof"]["count"], HISTORY + 1);
    assert_eq!(cold["nextTurnId"], Value::Null);
    let all = cold["turns"].as_array().unwrap();
    assert_eq!(all.len(), HISTORY + 2);
    assert_eq!(warm["turns"], Value::Array(all[HISTORY..].to_vec()));
    assert_eq!(save(&mut db, HISTORY + 1), 3, "advancing past 10,000 resumes certified state");
    assert_eq!(certificate(&db).unwrap().count, (HISTORY + 1) as u64);

    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    assert!(stored_certificate(&db).is_some());
    assert!(certificate(&db).is_none(), "a new epoch never trusts the old certificate");
    append(&mut db, HISTORY + 2);
    SCANNED_HEADERS.with(|n| n.set(0));
    let reopened = context(&db, HISTORY + 2, true);
    assert_eq!(SCANNED_HEADERS.with(|n| n.get()), HISTORY + 3, "one streamed walk");
    assert_eq!(reopened["covered"], HISTORY + 2);
    assert_eq!(reopened["continuationStatus"], "available");
    assert_eq!(reopened["turns"].as_array().unwrap().len(), 1);
    assert_eq!(reopened["historyProof"], context(&db, HISTORY + 2, false)["historyProof"]);
    assert_eq!(save(&mut db, HISTORY + 2), HISTORY + 3, "an epoch's first save reauthenticates");
    append(&mut db, HISTORY + 3);

    let mut tx = db.begin(7, 11).unwrap();
    edit(&db, &mut tx, 3, json!({"content":"edited long ago"}));
    db.commit(tx).unwrap();
    assert!(certificate(&db).is_none(), "an edit far below the anchor clears the certificate");
    SCANNED_HEADERS.with(|n| n.set(0));
    let changed = context(&db, HISTORY + 3, true);
    assert_eq!(SCANNED_HEADERS.with(|n| n.get()), 2 * (HISTORY + 4));
    assert_eq!(changed["covered"], 0);
    assert_eq!(changed["continuationStatus"], "history_changed");
    assert_eq!(changed["continuation"], Value::Null);
    assert_eq!(changed["turns"].as_array().unwrap().len(), HISTORY + 4);
    assert_eq!(changed["turns"][3]["projection"]["content"], "edited long ago");
    assert_eq!(changed["historyProof"], context(&db, HISTORY + 3, false)["historyProof"]);
}

fn page_after(
    db: &AuthorityDatabase,
    index: usize,
    replay: bool,
    after: &str,
) -> io::Result<Value> {
    let mut tx = db.begin(7, 11).unwrap();
    read(db, &mut tx, CONVERSATION, &format!("turn-{index}"), replay, Some(after))
        .map(|bytes| serde_json::from_slice(&bytes.unwrap().into_bytes()).unwrap())
}

fn turn_ids(page: &Value) -> Vec<String> {
    page["turns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["turnId"].as_str().unwrap().to_owned())
        .collect()
}

fn names(range: std::ops::Range<usize>) -> Vec<String> {
    range.map(|index| format!("turn-{index}")).collect()
}

fn assert_outside(result: io::Result<Value>) {
    let error = result.unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    assert_eq!(error.to_string(), "context page cursor is outside the selected suffix");
}

#[test]
fn cursors_select_after_themselves_on_certified_cold_and_stale_anchor_paths() {
    let (vfs, mut db) = simulated();
    for index in 0..10 {
        append(&mut db, index);
    }
    save(&mut db, 5);
    for replay in [true, false] {
        let page = page_after(&db, 9, replay, "turn-7").unwrap();
        assert_eq!(page["covered"], if replay { 6 } else { 0 });
        assert_eq!(turn_ids(&page), names(8..10));
        assert_eq!(page["historyProof"], context(&db, 9, replay)["historyProof"]);
        assert_outside(page_after(&db, 9, replay, "turn-9"));
        assert_outside(page_after(&db, 9, replay, "unknown"));
    }
    assert_eq!(turn_ids(&page_after(&db, 9, false, "turn-2").unwrap()), names(3..10));
    assert_outside(page_after(&db, 9, true, "turn-5"));
    assert_outside(page_after(&db, 9, true, "turn-2"));

    drop(db);
    let mut db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert!(certificate(&db).is_none());
    let page = page_after(&db, 9, true, "turn-6").unwrap();
    assert_eq!(page["covered"], 6);
    assert_eq!(turn_ids(&page), names(7..10));
    assert_outside(page_after(&db, 9, true, "turn-5"));
    assert_outside(page_after(&db, 9, true, "turn-1"));

    let mut tx = db.begin(7, 11).unwrap();
    edit(&db, &mut tx, 0, json!({"content":"edited"}));
    db.commit(tx).unwrap();
    for (after, selected) in [(1, 2..10), (5, 6..10), (7, 8..10)] {
        SCANNED_HEADERS.with(|n| n.set(0));
        let page = page_after(&db, 9, true, &format!("turn-{after}")).unwrap();
        assert_eq!(page["covered"], 0);
        assert_eq!(page["continuationStatus"], "history_changed");
        assert_eq!(turn_ids(&page), names(selected.clone()));
        // One full walk, then only the page after the cursor.
        assert_eq!(SCANNED_HEADERS.with(|n| n.get()), 10 + selected.len());
    }
    assert_outside(page_after(&db, 9, true, "turn-9"));
    assert_outside(page_after(&db, 9, true, "unknown"));
}
