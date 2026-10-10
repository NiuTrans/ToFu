//! Source-change witnesses, including sparse-sequence retention ABA.
use super::*;

fn add(db: &mut AuthorityDatabase, task: &str, seq: u64, at: i64, text: &str) -> Value {
    add_type(db, task, seq, at, text, "round_end")
}

fn add_type(db: &mut AuthorityDatabase, task: &str, seq: u64, at: i64, text: &str, event_type: &str) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    let result = append(db, &mut tx, &AppendRequest {
        task_id: task.into(), application_sequence: seq, event_type: event_type.into(),
        payload_json: serde_json::to_vec(&json!({"type":event_type, "v":1, "text":text})).unwrap(),
        created_at_ms: at,
    }).unwrap();
    let parsed: Value = serde_json::from_slice(&result).unwrap();
    if parsed["inserted"] == true { db.commit(tx).unwrap(); }
    parsed
}

fn scoped(db: &AuthorityDatabase, task: &str, types: &[&str]) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    let types = types.iter().map(|value| value.to_string()).collect::<Vec<_>>();
    serde_json::from_slice(&bounds(db, &mut tx, task, true, Some(&types)).unwrap()).unwrap()
}

#[test]
fn scoped_read_guard_ignores_noise_but_binds_selected_appends_and_exact_types() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let types = ["messages_snapshot", "tool_wire_projection"];
    let initial = scoped(&db, "target", &types);
    add_type(&mut db, "target", 900, 1000, "noise", "round_usage");
    assert_eq!(scoped(&db, "target", &types), initial);
    add_type(&mut db, "target", 4, 1000, "selected", "messages_snapshot");
    let selected = scoped(&db, "target", &types);
    assert_ne!(selected["read_guard"], initial["read_guard"]);
    assert_eq!(selected["retained_count"], 1);
    assert_eq!(selected["base_cursor"], 4);
    assert_eq!(selected["next_cursor"], 5);
    assert_eq!(selected["read_guard"]["contract"], crate::generated_storage_operations::EVENT_READ_GUARD_SCOPED_CONTRACT);
    assert_eq!(scoped(&db, "target", &["tool_wire_projection", "messages_snapshot", "messages_snapshot"]), selected);
    assert_ne!(scoped(&db, "target", &["messages_snapshot"])["read_guard"], selected["read_guard"]);
    let whole_before = view(&db, "target", true);
    add_type(&mut db, "target", 1000, 1000, "noise", "round_usage");
    add_type(&mut db, "target", 2, 1000, "noise", "round_usage");
    assert_eq!(scoped(&db, "target", &types), selected);
    assert_ne!(view(&db, "target", true), whole_before);
    let replica = db.build_read_replica().unwrap();
    assert_eq!(scoped(&replica, "target", &types), selected);
    drop(replica);
    assert_eq!(add_type(&mut db, "target", 4, 2000, "selected", "messages_snapshot")["inserted"], false);
    assert_eq!(scoped(&db, "target", &types), selected);
    add_type(&mut db, "target", 3, 3000, "late", "messages_snapshot");
    assert_ne!(scoped(&db, "target", &types)["read_guard"], selected["read_guard"]);
}

#[test]
fn scoped_read_guard_fences_retention_aba_empty_source_and_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let types = ["round_end"];
    add(&mut db, "target", 1, 1000, "before");
    add(&mut db, "target", 0, 3000, "left");
    add(&mut db, "target", 2, 3000, "right");
    let before = scoped(&db, "target", &types);
    expire(&mut db, 2000);
    add(&mut db, "target", 1, 4000, "replacement");
    let after = scoped(&db, "target", &types);
    assert_eq!(before["retained_count"], after["retained_count"]);
    assert_eq!(before["base_cursor"], after["base_cursor"]);
    assert_eq!(before["next_cursor"], after["next_cursor"]);
    assert_ne!(before["read_guard"], after["read_guard"]);
    expire(&mut db, 5000);
    let empty = scoped(&db, "target", &types);
    assert_eq!(empty["retained_count"], 0);
    add_type(&mut db, "target", 0, 6000, "noise", "round_usage");
    assert_eq!(scoped(&db, "target", &types), empty);
    expire(&mut db, 7000);
    assert_eq!(view(&db, "target", false)["retained_count"], 0);
    let pruned_noise = scoped(&db, "target", &types);
    assert_ne!(pruned_noise["read_guard"], empty["read_guard"]);
    drop(db);
    let db = AuthorityDatabase::open(directory.path()).unwrap();
    assert_ne!(scoped(&db, "target", &types)["read_guard"], pruned_noise["read_guard"]);
}

fn view(db: &AuthorityDatabase, task: &str, guard: bool) -> Value {
    let mut tx = db.begin(7, 11).unwrap();
    serde_json::from_slice(&bounds(db, &mut tx, task, guard, None).unwrap()).unwrap()
}

fn expire(db: &mut AuthorityDatabase, cutoff: i64) {
    for _ in 0..16 {
        let mut tx = db.begin(7, 11).unwrap();
        let result: Value = serde_json::from_slice(&prune(db, &mut tx, &PruneRequest {
            created_before_ms: cutoff, limit: 1000, retention_class: RetentionClass::Structural,
            now_unix_ms: 10_000,
        }, &|| false).unwrap()).unwrap();
        if tx.has_business_mutation() { db.commit(tx).unwrap(); }
        if result["has_more"] == false { return; }
    }
    panic!("tiny private fixture did not drain within its budget");
}

#[test]
fn event_read_guard_is_stable_scoped_and_shared_by_read_replicas() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let original = view(&db, "target", true);
    assert_eq!(original["read_guard"]["contract"], crate::generated_storage_operations::EVENT_READ_GUARD_CONTRACT);
    assert_eq!(view(&db, "target", true), original);
    assert_ne!(view(&db, "other", true)["read_guard"], original["read_guard"]);
    let replica = db.build_read_replica().unwrap();
    assert_eq!(view(&replica, "target", true), original);
    drop(replica);
    add(&mut db, "other", 0, 1000, "other");
    assert_eq!(view(&db, "target", true), original);
    add(&mut db, "target", 0, 1000, "target");
    let changed = view(&db, "target", true);
    assert_ne!(changed["read_guard"], original["read_guard"]);
    assert_eq!(add(&mut db, "target", 0, 2000, "target")["inserted"], false);
    assert_eq!(view(&db, "target", true), changed);
}

#[test]
fn event_read_guard_detects_equal_bounds_after_retention_and_reinsert() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    add(&mut db, "target", 1, 1000, "before");
    add(&mut db, "target", 0, 3000, "left");
    add(&mut db, "target", 2, 3000, "right");
    let before = view(&db, "target", true);
    let plain = view(&db, "target", false);
    expire(&mut db, 2000);
    assert_eq!(view(&db, "target", false)["retained_count"], 2);
    assert_ne!(view(&db, "target", true)["read_guard"], before["read_guard"]);
    add(&mut db, "target", 1, 4000, "after");
    assert_eq!(view(&db, "target", false), plain);
    assert_ne!(view(&db, "target", true)["read_guard"], before["read_guard"]);
}

#[test]
fn event_read_guard_does_not_recycle_empty_or_reopened_source_identity() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let initial = view(&db, "target", true);
    add(&mut db, "target", 0, 1000, "before");
    expire(&mut db, 2000);
    let empty = view(&db, "target", true);
    assert_eq!(empty["retained_count"], 0);
    assert_ne!(empty["read_guard"], initial["read_guard"]);
    add(&mut db, "target", 0, 3000, "recreated");
    let before = view(&db, "target", true);
    let plain = view(&db, "target", false);
    drop(db);
    let reopened = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(view(&reopened, "target", false), plain);
    assert_ne!(view(&reopened, "target", true)["read_guard"], before["read_guard"]);
}
