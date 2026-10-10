//! Retention sweep slicing: durable watermark, tombstone-field crossing,
//! adaptive slice bounds, and cursor persistence (2026-09-22 deadlock).
use super::*;

fn streaming_append(task_id: &str, sequence: u64, created_at_ms: i64) -> AppendRequest {
    AppendRequest {
        task_id: task_id.to_owned(),
        application_sequence: sequence,
        // Not one of STRUCTURAL_EVENT_TYPES: the streaming retention class.
        event_type: "assistant_delta".to_owned(),
        created_at_ms,
        payload_json: serde_json::to_vec(
            &json!({"type":"assistant_delta", "text":"x"}),
        )
        .unwrap(),
    }
}

fn write(db: &mut AuthorityDatabase, item: &AppendRequest) -> io::Result<()> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let result: Value = serde_json::from_slice(&append(db, &mut tx, item)?).unwrap();
    if result["inserted"] == true {
        db.commit(tx)?;
    }
    Ok(())
}

fn prune_once(
    db: &mut AuthorityDatabase,
    cutoff: i64,
    limit: usize,
) -> io::Result<Value> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let result: Value = serde_json::from_slice(&prune(
        db,
        &mut tx,
        &PruneRequest {
            created_before_ms: cutoff,
            limit,
            retention_class: RetentionClass::Streaming,
            now_unix_ms: cutoff.max(0) as u64,
        },
        &|| false,
    )?)?;
    db.commit(tx)?;
    Ok(result)
}

fn sweep_cursor(db: &AuthorityDatabase) -> io::Result<Option<(i64, i64)>> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let key = retention_sweep_cursor_key(7, 11, RetentionClass::Streaming)?;
    Ok(db
        .entity_get(&mut tx, &key)?
        .map(|raw| decode_retention_sweep_cursor(&raw).unwrap()))
}

fn live_events(db: &AuthorityDatabase, task_id: &str) -> io::Result<usize> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let raw = list(
        db,
        &mut tx,
        &ListRequest {
            task_id: task_id.to_owned(),
            after_sequence: None,
            limit: 1_000,
            types: vec![],
            type_prefixes: vec![],
            payload_types: None,
        },
    )?;
    let page: Value = serde_json::from_slice(&raw)?;
    Ok(page.as_array().map_or(0, Vec::len))
}

#[test]
fn sweep_drains_live_backlog_across_bounded_calls() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    for index in 0..10_u64 {
        write(
            &mut db,
            &streaming_append("sweep-task", index + 1, 1_000 + index as i64),
        )
        .unwrap();
    }
    // Each call deletes at most `limit` rows; the watermark only advances
    // once a slice comes back drained.
    let mut deleted_total = 0_u64;
    let mut rounds = 0;
    loop {
        let result = prune_once(&mut db, 2_000, 3).unwrap();
        deleted_total += result["deleted"].as_u64().unwrap();
        rounds += 1;
        assert!(rounds <= 8, "sweep did not converge: {result}");
        if result["has_more"] == false {
            break;
        }
    }
    assert_eq!(deleted_total, 10);
    assert_eq!(live_events(&db, "sweep-task").unwrap(), 0);
    // The drained sweep advanced the durable watermark to the cutoff.
    let (swept, _slice) = sweep_cursor(&db).unwrap().expect("cursor persisted");
    assert_eq!(swept, 2_000);
}

#[test]
fn sweep_crosses_tombstone_field_to_reach_newer_live_rows() {
    // Regression shape of the 2026-09-22 deadlock: a region of already
    // pruned (tombstoned) entries sits below newer live rows. The bounded
    // scan must cross the tombstones without requiring live rows from them,
    // then keep the watermark past them so later calls never rescan.
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    for index in 0..40_u64 {
        write(
            &mut db,
            &streaming_append("churn-task", index + 1, 1_000 + index as i64),
        )
        .unwrap();
    }
    // Delete the entire first generation, creating the tombstone field.
    loop {
        let result = prune_once(&mut db, 2_000, 25).unwrap();
        if result["has_more"] == false {
            break;
        }
    }
    assert_eq!(live_events(&db, "churn-task").unwrap(), 0);
    let (swept, _) = sweep_cursor(&db).unwrap().expect("cursor persisted");
    assert_eq!(swept, 2_000);

    // New live rows land above the tombstone field.
    for index in 0..5_u64 {
        write(
            &mut db,
            &streaming_append("churn-task", 100 + index + 1, 3_000 + index as i64),
        )
        .unwrap();
    }
    let mut deleted_total = 0_u64;
    loop {
        let result = prune_once(&mut db, 4_000, 25).unwrap();
        deleted_total += result["deleted"].as_u64().unwrap();
        if result["has_more"] == false {
            break;
        }
    }
    assert_eq!(deleted_total, 5);
    assert_eq!(live_events(&db, "churn-task").unwrap(), 0);
    let (swept, _) = sweep_cursor(&db).unwrap().expect("cursor persisted");
    assert_eq!(swept, 4_000);
}

#[test]
fn sweep_cursor_survives_reopen_and_resumes() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    for index in 0..6_u64 {
        write(
            &mut db,
            &streaming_append("resume-task", index + 1, 1_000 + index as i64),
        )
        .unwrap();
    }
    // One bounded call deletes a prefix without draining the slice: the
    // watermark must NOT advance past un-scanned live rows.
    let first = prune_once(&mut db, 2_000, 2).unwrap();
    assert_eq!(first["deleted"], 2);
    assert_eq!(first["has_more"], true);
    assert!(sweep_cursor(&db).unwrap().is_none());
    drop(db);

    let mut db = AuthorityDatabase::open(dir.path()).unwrap();
    let mut deleted_total = 0_u64;
    loop {
        let result = prune_once(&mut db, 2_000, 2).unwrap();
        deleted_total += result["deleted"].as_u64().unwrap();
        if result["has_more"] == false {
            break;
        }
    }
    assert_eq!(deleted_total, 4);
    assert_eq!(live_events(&db, "resume-task").unwrap(), 0);
    let (swept, _) = sweep_cursor(&db).unwrap().expect("cursor persisted");
    assert_eq!(swept, 2_000);
}

#[test]
fn sweep_cursor_codec_round_trips_and_rejects_garbage() {
    let encoded = encode_retention_sweep_cursor(1_234_567, RETENTION_SWEEP_MIN_SLICE_MS).unwrap();
    assert_eq!(
        decode_retention_sweep_cursor(&encoded).unwrap(),
        (1_234_567, RETENTION_SWEEP_MIN_SLICE_MS)
    );
    assert!(decode_retention_sweep_cursor(b"").is_err());
    assert!(decode_retention_sweep_cursor(&encoded[..12]).is_err());
    let mut wrong_magic = encoded.clone();
    wrong_magic[0] ^= 0xFF;
    assert!(decode_retention_sweep_cursor(&wrong_magic).is_err());
    assert!(encode_retention_sweep_cursor(0, RETENTION_SWEEP_MIN_SLICE_MS).is_err());
    assert!(encode_retention_sweep_cursor(1, RETENTION_SWEEP_MIN_SLICE_MS - 1).is_err());
    assert!(encode_retention_sweep_cursor(1, RETENTION_SWEEP_MAX_SLICE_MS + 1).is_err());
}

#[test]
fn sweep_reaches_present_timestamps_in_one_call_from_cold_cursor() {
    // The differential contract: events appended at wall-clock timestamps
    // (~1.8e12 ms) with a far-future cutoff (4e12) must drain in a SINGLE
    // bounded call — empty-history slices chain inside the call.
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let base_ms = 1_800_000_000_000_i64;
    for index in 0..3_u64 {
        write(
            &mut db,
            &streaming_append("now-task", index + 1, base_ms + index as i64),
        )
        .unwrap();
    }
    let result = prune_once(&mut db, 4_000_000_000_000, 100).unwrap();
    assert_eq!(result["deleted"], 3, "single call must drain: {result}");
    assert_eq!(result["has_more"], false);
    assert_eq!(live_events(&db, "now-task").unwrap(), 0);
}

fn prune_once_at(
    db: &mut AuthorityDatabase,
    cutoff: i64,
    limit: usize,
    now_unix_ms: u64,
) -> io::Result<Value> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let result: Value = serde_json::from_slice(&prune(
        db,
        &mut tx,
        &PruneRequest {
            created_before_ms: cutoff,
            limit,
            retention_class: RetentionClass::Streaming,
            now_unix_ms,
        },
        &|| false,
    )?)?;
    db.commit(tx)?;
    Ok(result)
}

#[test]
fn far_future_cutoff_never_leaks_later_appends() {
    // The exact differential divergence: a prune with a cutoff years ahead
    // of the daemon clock must sweep everything that exists NOW without
    // pushing the watermark past now — appends that land afterwards (with
    // earlier timestamps than the old cutoff) must stay prunable forever.
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let now = 1_800_000_000_000_i64;
    let first = prune_once_at(&mut db, 4_000_000_000_000, 100, now as u64).unwrap();
    assert_eq!(first["deleted"], 0);
    assert_eq!(first["has_more"], false);
    let (swept, _) = sweep_cursor(&db).unwrap().expect("cursor persisted");
    assert!(swept <= now, "watermark {swept} ran ahead of now {now}");

    for index in 0..3_u64 {
        write(
            &mut db,
            &streaming_append("late-task", index + 1, now + index as i64),
        )
        .unwrap();
    }
    let second = prune_once_at(&mut db, 4_000_000_000_000, 100, now as u64).unwrap();
    assert_eq!(second["deleted"], 3, "later appends leaked: {second}");
    assert_eq!(second["has_more"], false);
    assert_eq!(live_events(&db, "late-task").unwrap(), 0);

    // And once more, proving the watermark keeps tracking the clock.
    write(&mut db, &streaming_append("late-task", 9, now + 9)).unwrap();
    let third = prune_once_at(&mut db, 4_000_000_000_000, 100, (now + 10) as u64).unwrap();
    assert_eq!(third["deleted"], 1, "frontier append leaked: {third}");
    assert_eq!(third["has_more"], false);
}

#[test]
fn delete_batch_truncation_rolls_back_watermark_and_resumes() {
    // The delete loop's authority-friendliness budget can stop mid-batch; the
    // watermark must roll back to the first unprocessed row so the next call
    // resumes exactly there without leaking (or double-counting) rows. Force
    // it deterministically: the injected deadline stays false for the single
    // scan-loop check, then trips every delete-loop check.
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    for index in 0..5_u64 {
        write(
            &mut db,
            &streaming_append("trunc-task", index + 1, 10 + index as i64),
        )
        .unwrap();
    }
    let calls = std::cell::Cell::new(0_usize);
    let deadline = || {
        let seen = calls.get();
        calls.set(seen + 1);
        // Call 0 is the scan loop's budget check; every later call is a
        // delete-loop check, so the batch truncates after one processed row.
        seen >= 1
    };
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let encoded = prune(
        &db,
        &mut tx,
        &PruneRequest {
            created_before_ms: 2_000,
            limit: 100,
            retention_class: RetentionClass::Streaming,
            now_unix_ms: 2_000,
        },
        &deadline,
    )
    .unwrap();
    let first: Value = serde_json::from_slice(&encoded).unwrap();
    db.commit(tx).unwrap();
    assert_eq!(first["deleted"], 1, "truncated batch: {first}");
    assert_eq!(first["has_more"], true);
    assert_eq!(live_events(&db, "trunc-task").unwrap(), 4);
    // The watermark rolled back to the first unprocessed row (created_at 11).
    let (swept, _) = sweep_cursor(&db).unwrap().expect("cursor persisted");
    assert_eq!(swept, 11, "watermark must not claim the unprocessed row");

    // The next call resumes inclusively at the rolled-back watermark and
    // drains the remainder exactly once.
    let second = prune_once(&mut db, 2_000, 100).unwrap();
    assert_eq!(second["deleted"], 4, "resume leaked rows: {second}");
    assert_eq!(second["has_more"], false);
    assert_eq!(live_events(&db, "trunc-task").unwrap(), 0);
}
