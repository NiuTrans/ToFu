//! Offline operator contract; all authorities are disposable test directories.
mod common;

use std::path::Path;
use std::process::{Command, Output};

use serde_json::json;
use tofu_db::authority::AuthorityDatabase;
use tofu_db::entity::EntityKey;
use tofu_db::generated_tofudb_ir::{
    INTEGRATION_ROW_LOCATOR_NAMESPACE, INTEGRATION_WORKSPACE_NAMESPACE,
};

fn command(path: &Path, flags: &[&str]) -> Output {
    Command::new(common::native_binary())
        .args(["backfill-integration-checkout-claims", "--data-dir"])
        .arg(path)
        .args(flags)
        .output()
        .unwrap()
}

// Old-format fixture deliberately bypasses the checkout writer. No test-only
// CLI or production repair endpoint is exposed for seeding historical records.
fn seed_legacy(path: &Path) {
    let mut database = AuthorityDatabase::initialize(path).unwrap();
    for first in (1u64..=129).step_by(64) {
        let mut tx = database.begin(7, u64::MAX).unwrap();
        for id in first..=(first + 63).min(129) {
            let owner = if id % 2 == 0 { 11u64 } else { 12u64 };
            let task = format!("task-{id}");
            let mut raw = owner.to_be_bytes().to_vec();
            for text in ["/repo", &task] {
                raw.extend_from_slice(&(text.len() as u16).to_be_bytes());
                raw.extend_from_slice(text.as_bytes());
            }
            let workspace = json!({"id":id,"owner_user_id":owner,"project_root":"/repo",
                "task_id":task,"title":"","workspace_path":format!("/checkout/{id}"),
                "managed":true,"state":"running","base_sha":"base","checkpoint_sha":"",
                "candidate_sha":"","error":"","origin_raw":"{}","created_at":1.0,"updated_at":1.0});
            let locator = json!({"id":id,"owner_user_id":owner,"project_root":"/repo","task_id":task});
            database.entity_put(&mut tx,
                EntityKey::new(7, u64::MAX, INTEGRATION_WORKSPACE_NAMESPACE, &raw).unwrap(),
                serde_json::to_vec(&workspace).unwrap()).unwrap();
            database.entity_put(&mut tx,
                EntityKey::new(7, u64::MAX, INTEGRATION_ROW_LOCATOR_NAMESPACE, &id.to_be_bytes()).unwrap(),
                serde_json::to_vec(&locator).unwrap()).unwrap();
        }
        database.commit(tx).unwrap();
    }
}

#[test]
fn offline_cli_reopens_resumes_and_reports_complete_only_after_all_owners() {
    let directory = tempfile::tempdir().unwrap();
    seed_legacy(directory.path());
    let flags = ["--tenant-id", "7", "--owner-id", "11", "--maximum-rows", "128", "--execute"];
    let first = command(directory.path(), &flags);
    assert!(first.status.success(), "{}", String::from_utf8_lossy(&first.stderr));
    let receipt = String::from_utf8(first.stdout).unwrap();
    assert!(receipt.contains("format=tofu-db.integration-checkout-backfill.v1"));
    assert!(receipt.contains("processed_rows=128 after_id=128 complete=false committed=true"));
    assert!(receipt.contains("maximum_rows_per_transaction=128"));
    // Each executable invocation closes/reopens the exclusive authority lease.
    let second = command(directory.path(), &flags);
    assert!(second.status.success(), "{}", String::from_utf8_lossy(&second.stderr));
    assert!(String::from_utf8(second.stdout).unwrap()
        .contains("processed_rows=1 after_id=129 complete=true committed=true"));
    let repeated = command(directory.path(), &flags);
    assert!(repeated.status.success());
    assert!(String::from_utf8(repeated.stdout).unwrap()
        .contains("processed_rows=0 after_id=129 complete=true committed=false"));
}

#[test]
fn offline_cli_requires_execute_positive_scope_and_at_most_128_rows_before_open() {
    let parent = tempfile::tempdir().unwrap();
    let missing = parent.path().join("must-not-be-opened");
    for flags in [
        vec!["--tenant-id", "7", "--owner-id", "11"],
        vec!["--tenant-id", "0", "--owner-id", "11", "--execute"],
        vec!["--tenant-id", "7", "--owner-id", "0", "--execute"],
        vec!["--tenant-id", "7", "--execute"],
        vec!["--owner-id", "11", "--execute"],
        vec!["--tenant-id", "7", "--owner-id", "11", "--maximum-rows", "0", "--execute"],
        vec!["--tenant-id", "7", "--owner-id", "11", "--maximum-rows", "129", "--execute"],
    ] {
        assert_eq!(command(&missing, &flags).status.code(), Some(2), "{flags:?}");
        assert!(!missing.exists());
    }
}

#[test]
fn offline_cli_refuses_an_authority_with_a_live_writer_lease() {
    let directory = tempfile::tempdir().unwrap();
    let database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let flags = ["--tenant-id", "7", "--owner-id", "11", "--execute"];
    assert!(!command(directory.path(), &flags).status.success());
    drop(database);
    let result = command(directory.path(), &flags);
    assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stderr));
    assert!(String::from_utf8(result.stdout).unwrap()
        .contains("processed_rows=0 after_id=0 complete=true committed=true"));
}
