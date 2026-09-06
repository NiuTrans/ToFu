//! Cross-process contract for the explicit authority garbage collector.

use std::io;
use std::process::Command;

use tofu_db::authority::AuthorityDatabase;
use tofu_db::block::BlockStore;
use tofu_db::engine::Engine;
use tofu_db::entity::EntityKey;
use uuid::Uuid;

#[test]
fn plan_preserves_orphans_and_execute_removes_them_without_harming_live_state() {
    let directory = tempfile::tempdir().unwrap();
    let key = EntityKey::new(7, 11, "record", b"live").unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key.clone(), b"live-value".to_vec())
        .unwrap();
    database.commit(transaction).unwrap();
    drop(database);

    let block_store = BlockStore::open(directory.path()).unwrap();
    let orphan = block_store.put(b"operator-orphan").unwrap();
    drop(block_store);

    let plan = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(
        plan.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&plan.stdout),
        String::from_utf8_lossy(&plan.stderr)
    );
    let plan_stdout = String::from_utf8(plan.stdout).unwrap();
    assert!(plan_stdout.contains("format=tofu-db.authority-gc.v1 mode=plan"));
    assert!(plan_stdout.contains("candidate_blocks=1"));
    assert!(plan_stdout.contains("removed_blocks=0"));
    assert_eq!(
        BlockStore::open(directory.path())
            .unwrap()
            .get(orphan)
            .unwrap(),
        b"operator-orphan"
    );

    let execute = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .arg("--execute")
        .output()
        .unwrap();
    assert!(
        execute.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&execute.stdout),
        String::from_utf8_lossy(&execute.stderr)
    );
    let execute_stdout = String::from_utf8(execute.stdout).unwrap();
    assert!(execute_stdout.contains("format=tofu-db.authority-gc.v1 mode=execute"));
    assert!(execute_stdout.contains("candidate_blocks=1"));
    assert!(execute_stdout.contains("removed_blocks=1"));
    assert_eq!(
        BlockStore::open(directory.path())
            .unwrap()
            .get(orphan)
            .unwrap_err()
            .kind(),
        io::ErrorKind::NotFound
    );

    let database = AuthorityDatabase::open(directory.path()).unwrap();
    let mut read = database.begin(7, 11).unwrap();
    assert_eq!(
        database.entity_get(&mut read, &key).unwrap(),
        Some(b"live-value".to_vec())
    );
}

#[test]
fn command_refuses_missing_authority_without_creating_it() {
    let parent = tempfile::tempdir().unwrap();
    let missing = parent.path().join("missing-authority");
    let output = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(&missing)
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!missing.exists());
}

#[test]
fn command_plans_then_removes_one_abandoned_segment_temporary() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let temporary = directory
        .path()
        .join("payload-segments")
        .join(format!(".new-{}", Uuid::from_bytes([0x4a; 16]).simple()));
    std::fs::write(&temporary, b"abandoned-segment-prefix").unwrap();

    let plan = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(plan.status.success());
    let plan_stdout = String::from_utf8(plan.stdout).unwrap();
    assert!(plan_stdout.contains("candidate_orphan_segment_files=1"));
    assert!(plan_stdout.contains("removed_orphan_segment_files=0"));
    assert!(temporary.exists());

    let execute = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .arg("--execute")
        .output()
        .unwrap();
    assert!(execute.status.success());
    let execute_stdout = String::from_utf8(execute.stdout).unwrap();
    assert!(execute_stdout.contains("removed_orphan_segment_files=1"));
    assert!(!temporary.exists());
    AuthorityDatabase::open(directory.path()).unwrap();
}

#[test]
fn command_removes_an_abandoned_loose_block_temporary_before_other_gc() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let store = BlockStore::open(directory.path()).unwrap();
    let orphan = store.put(b"loose-orphan-after-temporary").unwrap();
    drop(store);
    let temporary = directory
        .path()
        .join("blocks")
        .join(&orphan.to_hex()[..2])
        .join(format!(".new-{}", Uuid::from_bytes([0x5b; 16]).simple()));
    std::fs::write(&temporary, b"abandoned-block-prefix").unwrap();

    let plan = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(plan.status.success());
    let plan_stdout = String::from_utf8(plan.stdout).unwrap();
    assert!(plan_stdout.contains("candidate_temporary_block_files=1"));
    assert!(plan_stdout.contains("removed_temporary_block_files=0"));
    assert!(plan_stdout.contains("scanned_blocks=0"));
    assert!(temporary.exists());

    let execute = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .arg("--execute")
        .output()
        .unwrap();
    assert!(execute.status.success());
    let execute_stdout = String::from_utf8(execute.stdout).unwrap();
    assert!(execute_stdout.contains("removed_temporary_block_files=1"));
    assert!(execute_stdout.contains("more_candidates=true"));
    assert!(!temporary.exists());
    assert_eq!(
        BlockStore::open(directory.path())
            .unwrap()
            .get(orphan)
            .unwrap(),
        b"loose-orphan-after-temporary"
    );
}

#[test]
fn command_plans_then_compacts_a_profitable_live_loose_shard() {
    let directory = tempfile::tempdir().unwrap();
    AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut payloads_by_shard = std::collections::BTreeMap::<u8, Vec<Vec<u8>>>::new();
    let payloads = (0..1_000_000_u32)
        .find_map(|value| {
            let payload = format!("process-auto-compaction-{value}").into_bytes();
            let shard = tofu_db::block::BlockId::for_payload(&payload).0[0];
            let payloads = payloads_by_shard.entry(shard).or_default();
            payloads.push(payload);
            (payloads.len() == 128).then(|| std::mem::take(payloads))
        })
        .unwrap();
    let slices = payloads.iter().map(Vec::as_slice).collect::<Vec<_>>();
    let mut engine = Engine::open(directory.path()).unwrap();
    let block_ids = engine
        .commit_transaction(b"process-compaction-fixture", &slices)
        .unwrap()
        .block_ids;
    drop(engine);

    let plan = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
        .arg("collect-garbage")
        .arg("--data-dir")
        .arg(directory.path())
        .output()
        .unwrap();
    assert!(plan.status.success());
    let plan_stdout = String::from_utf8(plan.stdout).unwrap();
    assert!(plan_stdout.contains("compacted_payload_blocks=0"));

    let mut compacted = false;
    for _ in 0..=256 {
        let execute = Command::new(env!("CARGO_BIN_EXE_tofu-db"))
            .arg("collect-garbage")
            .arg("--data-dir")
            .arg(directory.path())
            .arg("--execute")
            .output()
            .unwrap();
        assert!(execute.status.success());
        let execute_stdout = String::from_utf8(execute.stdout).unwrap();
        let compacted_blocks = execute_stdout
            .split_ascii_whitespace()
            .find_map(|field| field.strip_prefix("compacted_payload_blocks="))
            .unwrap()
            .parse::<u32>()
            .unwrap();
        if compacted_blocks >= 128 {
            compacted = true;
            break;
        }
        assert!(execute_stdout.contains("more_candidates=true"));
    }
    assert!(
        compacted,
        "durable cursor did not reach the profitable shard"
    );
    let engine = Engine::open(directory.path()).unwrap();
    for (block_id, payload) in block_ids.into_iter().zip(payloads) {
        assert_eq!(engine.read_block(block_id).unwrap(), payload);
    }
}
