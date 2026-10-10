use super::*;
use crate::migration::{restore_document, SnapshotDocument};
use crate::versioned_document;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use sha2::{Digest, Sha256};
use std::{path::Path, sync::Arc};

const TENANT: u64 = 7;
const OWNER: u64 = 11;

fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/paperbudget")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database =
        AuthorityDatabase::initialize_with_vfs(Path::new("/paperbudget"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, database)
}

fn receipt(owner: u64, build: char, data: &[u8]) -> Value {
    json!({"schema_version":1,"owner_user_id":owner,"document_id":"d".repeat(64),
        "build_id":build.to_string().repeat(32),
        "target":"host","source_digest":"e".repeat(64),"version_verified":true,
        "main_file":"main.tex","engine":"pdflatex","toolchain":"fixture","status":"success",
        "started_at":1,"completed_at":2,"diagnostics":[],
        "artifacts":[{"name":"document.pdf","sha256":format!("{:x}",Sha256::digest(data)),
            "size_bytes":data.len()}]})
}

fn reservation_name(owner: u64, build: char) -> String {
    format!(
        "{owner}:{}:{}",
        "d".repeat(64),
        build.to_string().repeat(32)
    )
}

/// Plant the retained artifact metadata exactly as the operator importer
/// restores it: exact document bytes under the versioned record authority,
/// without touching the budget ledger.
fn plant_metadata(database: &mut AuthorityDatabase, owner: u64, build: char, data: &[u8]) -> u64 {
    let receipt = receipt(owner, build, data);
    let reserved = crate::paper_build_receipt::validate(&receipt).unwrap() as u64;
    let key = reservation_name(owner, build);
    let meta = json!({
        "receipt": receipt,
        "upload_id": "a".repeat(32),
        "state": "sealed",
        "next_sequence": 1,
        "readers": {},
        "expires_at_ms": 9_999_999_999_999_u64,
    });
    let mut transaction = database.begin(TENANT, owner).unwrap();
    restore_document(
        database,
        &mut transaction,
        database.authority_uuid(),
        SnapshotDocument {
            key: versioned_document::record_key(TENANT, owner, "paper.build.artifact.v1", &key)
                .unwrap(),
            namespace: "paper.build.artifact.v1".to_owned(),
            logical_key: key,
            version: 1,
            updated_at_ms: 1000,
            value_json: serde_json::to_vec(&meta).unwrap(),
            blob_owner_user_id: owner,
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
    reserved
}

fn build_snapshot(records: &[(&str, u64)], version: u64, updated_at_ms: u64) -> Value {
    let entries: BTreeMap<String, u64> = records
        .iter()
        .map(|(key, bytes)| ((*key).to_owned(), *bytes))
        .collect();
    json!({
        "record_key": "capacity",
        "value_json": build_ledger_json(&entries),
        "version": version,
        "updated_at_ms": updated_at_ms,
    })
}

fn document_snapshot(count: u64, associations: u64, version: u64, updated_at_ms: u64) -> Value {
    json!({
        "record_key": "capacity",
        "value_json": format!("{{\"count\":{count},\"associations\":{associations}}}"),
        "version": version,
        "updated_at_ms": updated_at_ms,
    })
}

fn restore_build_op(database: &mut AuthorityDatabase, snapshot: Value) -> io::Result<Value> {
    execute(
        database,
        TENANT,
        OWNER,
        "migration.paper.build.budget.restore",
        snapshot,
    )
}

fn get_build_op(database: &mut AuthorityDatabase, owner: u64) -> Value {
    execute(
        database,
        TENANT,
        owner,
        "migration.paper.build.budget.get",
        json!({"record_key": "capacity"}),
    )
    .unwrap()
}

fn restore_document_op(database: &mut AuthorityDatabase, snapshot: Value) -> io::Result<Value> {
    execute(
        database,
        TENANT,
        OWNER,
        "migration.paper.document.budget.restore",
        snapshot,
    )
}

fn get_document_op(database: &mut AuthorityDatabase, owner: u64) -> Value {
    execute(
        database,
        TENANT,
        owner,
        "migration.paper.document.budget.get",
        json!({"record_key": "capacity"}),
    )
    .unwrap()
}

fn live_prepare(database: &mut AuthorityDatabase, owner: u64, build: char, data: &[u8]) {
    let payload = json!({
        "user_id": owner,
        "document_id": "d".repeat(64),
        "build_id": build.to_string().repeat(32),
        "upload_id": "a".repeat(32),
        "receipt": receipt(owner, build, data),
    });
    let mut transaction = database
        .begin_with_paper_document_scope(TENANT, owner)
        .unwrap();
    let request = crate::paper_build_artifact::Request::parse(
        "paper.artifact.prepare",
        payload.as_object().unwrap(),
        1000,
    )
    .unwrap();
    crate::paper_build_artifact::execute(database, &mut transaction, &request).unwrap();
    database.commit(transaction).unwrap();
}

fn native_usage(database: &mut AuthorityDatabase, owner: u64) -> (u64, u64) {
    let mut transaction = database
        .begin_with_paper_document_scope(TENANT, owner)
        .unwrap();
    paper_build_artifact::usage(database, &mut transaction).unwrap()
}

#[test]
fn build_budget_restores_exact_ledger_reservations_and_provenance() {
    let (vfs, mut database) = prepared();
    let first = plant_metadata(&mut database, OWNER, 'b', b"pdf-one");
    let second = plant_metadata(&mut database, OWNER, 'c', b"pdf-two!");
    let key_b = reservation_name(OWNER, 'b');
    let key_c = reservation_name(OWNER, 'c');
    let snapshot = build_snapshot(
        &[(key_b.as_str(), first), (key_c.as_str(), second)],
        9,
        1_700_000_000_000,
    );
    assert_eq!(
        restore_build_op(&mut database, snapshot.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(native_usage(&mut database, OWNER), (2, first + second));
    // Identical replay is read-only; the exact snapshot round-trips.
    assert_eq!(
        restore_build_op(&mut database, snapshot.clone()).unwrap()["inserted"],
        false
    );
    assert_eq!(get_build_op(&mut database, OWNER), snapshot);
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/paperbudget"), vfs).unwrap();
    assert_eq!(get_build_op(&mut database, OWNER), snapshot);
    // The live path continues from the restored budget.
    live_prepare(&mut database, OWNER, 'd', b"pdf-three");
    let third = crate::paper_build_receipt::validate(&receipt(OWNER, 'd', b"pdf-three")).unwrap();
    assert_eq!(
        native_usage(&mut database, OWNER),
        (3, first + second + third as u64)
    );
}

#[test]
fn build_budget_rejects_divergence_foreign_owner_and_orphans() {
    let (_, mut database) = prepared();
    let first = plant_metadata(&mut database, OWNER, 'b', b"pdf-one");
    let second = plant_metadata(&mut database, OWNER, 'c', b"pdf-two!");
    let key_b = reservation_name(OWNER, 'b');
    let key_c = reservation_name(OWNER, 'c');
    let snapshot = build_snapshot(&[(key_b.as_str(), first)], 9, 1_700_000_000_000);
    restore_build_op(&mut database, snapshot.clone()).unwrap();
    for variant in 0..3 {
        let mut changed = snapshot.clone();
        match variant {
            0 => changed["version"] = json!(10),
            1 => changed["updated_at_ms"] = json!(1_700_000_000_001_i64),
            // A second valid reservation diverges from the restored ledger.
            _ => {
                changed["value_json"] = json!(build_ledger_json(&BTreeMap::from([
                    (key_b.clone(), first),
                    (key_c.clone(), second),
                ])))
            }
        }
        let error = restore_build_op(&mut database, changed).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "variant {variant}");
    }
    // A reservation the snapshot does not claim is foreign live state.
    let subset = build_snapshot(&[], 9, 1_700_000_000_000);
    assert_eq!(
        restore_build_op(&mut database, subset).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
    // A foreign owner id inside the ledger is outside the operator scope.
    let other = plant_metadata(&mut database, 12, 'b', b"pdf-one");
    let key_foreign = reservation_name(12, 'b');
    let foreign = build_snapshot(&[(key_foreign.as_str(), other)], 1, 1000);
    assert_eq!(
        restore_build_op(&mut database, foreign).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    // A reservation without retained artifact metadata is corrupt source state.
    let key_e = reservation_name(OWNER, 'e');
    let orphan = build_snapshot(
        &[(key_b.as_str(), first), (key_e.as_str(), 64)],
        9,
        1_700_000_000_000,
    );
    assert_eq!(
        restore_build_op(&mut database, orphan).unwrap_err().kind(),
        io::ErrorKind::InvalidData
    );
    // A reservation that disagrees with the artifact's accounted bytes fails.
    let disagrees = build_snapshot(&[(key_b.as_str(), first + 8)], 9, 1_700_000_000_000);
    assert_eq!(
        restore_build_op(&mut database, disagrees)
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidData
    );
    // Malformed identities and metadata never reach storage.
    for variant in 0..5 {
        let mut bad = snapshot.clone();
        match variant {
            0 => bad["record_key"] = json!("other"),
            1 => bad["version"] = json!(0),
            2 => bad["updated_at_ms"] = json!(0),
            3 => {
                bad["value_json"] = json!(format!(
                    "{{\"records\":{{\"{OWNER}:nothex:{}\":1}}}}",
                    "b".repeat(32)
                ))
            }
            _ => bad["value_json"] = json!("not json"),
        }
        assert!(
            restore_build_op(&mut database, bad).is_err(),
            "variant {variant}"
        );
    }
    assert_eq!(get_build_op(&mut database, OWNER), snapshot);
}

#[test]
fn build_ledger_counts_are_unlimited_and_numeric_overflow_is_invalid() {
    let (_, mut database) = prepared();
    // The empty ledger is a valid exact snapshot.
    let empty = build_snapshot(&[], 3, 1_700_000_000_000);
    assert_eq!(
        restore_build_op(&mut database, empty.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(get_build_op(&mut database, OWNER), empty);
    // Over-bound record counts and byte totals fail closed.
    let mut entries = BTreeMap::new();
    for index in 0..=128 {
        entries.insert(
            format!("{OWNER}:{}:{:032x}", "d".repeat(64), index + 16),
            1_u64,
        );
    }
    let mut over_records = build_snapshot(&[], 1, 1000);
    over_records["value_json"] = json!(build_ledger_json(&entries));
    let request = parse_restore(over_records, MAX_BUILD_LEDGER_JSON_BYTES).unwrap();
    assert_eq!(parse_build_ledger(&request, OWNER).unwrap().reservations.len(), 129);
    let over_bytes = BTreeMap::from([
        (reservation_name(OWNER, 'b'), u64::MAX),
        (reservation_name(OWNER, 'c'), 1_u64),
    ]);
    let mut over_total = build_snapshot(&[], 1, 1000);
    over_total["value_json"] = json!(build_ledger_json(&over_bytes));
    assert_eq!(
        restore_build_op(&mut database, over_total)
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidInput
    );
}

#[test]
fn document_budget_restores_exact_counts_and_provenance() {
    let (_vfs, mut database) = prepared();
    let snapshot = document_snapshot(3, 1, 5, 1_700_000_000_000);
    assert_eq!(
        restore_document_op(&mut database, snapshot.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(
        restore_document_op(&mut database, snapshot.clone()).unwrap()["inserted"],
        false
    );
    assert_eq!(get_document_op(&mut database, OWNER), snapshot);
    let mut transaction = database
        .begin_with_paper_document_scope(TENANT, OWNER)
        .unwrap();
    assert_eq!(
        paper_document::budget_usage(&database, &mut transaction).unwrap(),
        Some((3, 1))
    );
    drop(transaction);
    // The source may omit the optional associations member entirely; the
    // exact round-trip exposes the defaulted zero.
    let mut legacy = document_snapshot(7, 0, 6, 1_700_000_000_001_i64 as u64);
    legacy["value_json"] = json!("{\"count\":7}");
    drop(database);
    let (vfs, mut database) = prepared();
    assert_eq!(
        restore_document_op(&mut database, legacy).unwrap()["inserted"],
        true
    );
    assert_eq!(
        get_document_op(&mut database, OWNER),
        document_snapshot(7, 0, 6, 1_700_000_000_001)
    );
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/paperbudget"), vfs).unwrap();
    assert_eq!(
        get_document_op(&mut database, OWNER),
        document_snapshot(7, 0, 6, 1_700_000_000_001)
    );
    // Divergent occupants fail closed; out-of-bound counts never reach storage.
    let diverged = document_snapshot(8, 0, 6, 1_700_000_000_001);
    assert_eq!(
        restore_document_op(&mut database, diverged)
            .unwrap_err()
            .kind(),
        io::ErrorKind::WouldBlock
    );
    let (_, mut fresh) = prepared();
    let larger = document_snapshot(129, 513, 1, 1000);
    restore_document_op(&mut fresh, larger.clone()).unwrap();
    assert_eq!(get_document_op(&mut fresh, OWNER), larger);

}

#[test]
fn paper_budget_snapshots_recover_atomically_under_commit_faults() {
    let (baseline, mut database) = prepared();
    let first = plant_metadata(&mut database, OWNER, 'b', b"pdf-one");
    let second = plant_metadata(&mut database, OWNER, 'c', b"pdf-two!");
    let key_b = reservation_name(OWNER, 'b');
    let key_c = reservation_name(OWNER, 'c');
    let snapshot = build_snapshot(
        &[(key_b.as_str(), first), (key_c.as_str(), second)],
        9,
        1_700_000_000_000,
    );
    restore_build_op(&mut database, snapshot.clone()).unwrap();
    let trace = baseline.trace().unwrap();
    assert!(!trace.is_empty());
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
        if operation == &Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        if matches!(
            operation,
            Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let (vfs, mut database) = prepared();
            plant_metadata(&mut database, OWNER, 'b', b"pdf-one");
            plant_metadata(&mut database, OWNER, 'c', b"pdf-two!");
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = restore_build_op(&mut database, snapshot.clone()).is_ok();
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut database =
                AuthorityDatabase::open_with_vfs(Path::new("/paperbudget"), vfs).unwrap();
            let exists = !get_build_op(&mut database, OWNER).is_null();
            assert!(
                !acknowledged || exists,
                "lost acknowledged build budget at {index}: {action:?}"
            );
            if exists {
                assert_eq!(get_build_op(&mut database, OWNER), snapshot);
            }
            // The restore is idempotent after any crash point.
            restore_build_op(&mut database, snapshot.clone()).unwrap();
            assert_eq!(get_build_op(&mut database, OWNER), snapshot);
            // An unacknowledged document budget restore leaves either nothing
            // or the complete provenance, never a partial ledger.
            let document = document_snapshot(2, 0, 5, 1_700_000_000_000);
            restore_document_op(&mut database, document.clone()).unwrap();
            assert_eq!(get_document_op(&mut database, OWNER), document);
        }
    }
}
