//! Large provider catalogs retain ordering, redaction and all OCC witnesses.
use super::*;

fn document(owner: u64, label: &str, index: usize) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "id": format!("provider-{index:05}"), "owner_user_id": owner,
        "tenant_id": label, "name": format!("provider {index}"),
        "base_url": "http://127.0.0.1:8080/v1", "api_key_ciphertext": "opaque-test-envelope",
        "key_hint": "", "models": [], "extra_headers": {}, "thinking_format": "",
        "disabled": false, "created_at": index as f64, "updated_at": index as f64,
        "last_used_at": null,
    })).unwrap()
}

#[test]
fn seven_thousand_providers_survive_pages_and_keep_late_occ_conflicts() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_enabled(true);
    let started = std::time::Instant::now();
    for batch in 0..14 {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in batch*500..(batch+1)*500 {
            create(&database, &mut transaction, "workspace-a", &format!("provider-{index:05}"),
                &document(11, "workspace-a", index), 1000+index as u64).unwrap();
        }
        database.commit(transaction).unwrap();
    }
    let mut stale = database.begin(7, 11).unwrap();
    let encoded = list(&database, &mut stale, "workspace-a").unwrap();
    let public: Value = serde_json::from_slice(&encoded).unwrap();
    let rows = public.as_array().unwrap();
    assert_eq!(rows.len(), 7000);
    for (position, row) in rows.iter().enumerate() {
        assert_eq!(row["id"], format!("provider-{:05}", 6999-position));
        assert!(row.get("api_key_ciphertext").is_none());
    }
    eprintln!("provider demand: 7000 rows, {} public bytes, {:?}", encoded.len(), started.elapsed());
    let mut foreign = database.begin(7, 12).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&list(&database, &mut foreign, "workspace-a").unwrap()).unwrap(), json!([]));
    drop(foreign);
    let mut other_label = database.begin(7, 11).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&list(&database, &mut other_label, "workspace-b").unwrap()).unwrap(), json!([]));
    drop(other_label);
    // The oldest document is read after the retired point-witness threshold.
    let mut change = database.begin(7, 11).unwrap();
    update(&database, &mut change, "workspace-a", "provider-00000", br#"{"name":"changed"}"#, 10000.0, 10000).unwrap().unwrap();
    database.commit(change).unwrap();
    touch(&database, &mut stale, "workspace-a", "provider-06999", 10001.0, 10001).unwrap();
    assert_eq!(database.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    let mut current = database.begin(7, 11).unwrap();
    assert_eq!(read_document(&database, &mut current, "workspace-a", "provider-06999").unwrap().unwrap()["last_used_at"], Value::Null);
    drop(current);
    let mut duplicate = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    assert_eq!(create(&database, &mut duplicate, "workspace-b", "provider-00000", &document(12, "workspace-b", 0), 10002).unwrap_err().kind(), io::ErrorKind::AlreadyExists);
    drop(duplicate);
    let mut deletion = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    delete(&database, &mut deletion, "workspace-a", "provider-06999").unwrap();
    database.commit(deletion).unwrap();
    let mut remaining = database.begin(7, 11).unwrap();
    let listed: Value = serde_json::from_slice(&list(&database, &mut remaining, "workspace-a").unwrap()).unwrap();
    assert_eq!(listed.as_array().unwrap().len(), 6999);
    assert_eq!(listed[0]["id"], "provider-06998");
    drop(remaining);
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}

#[test]
fn provider_count_overflow_and_index_mismatch_remain_integrity_errors() {
    let directory = tempfile::tempdir().unwrap();
    let database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    write_count(&database, &mut transaction, "workspace-a", usize::MAX).unwrap();
    assert_eq!(create(&database, &mut transaction, "workspace-a", "provider-00000", &document(11, "workspace-a", 0), 1000).unwrap_err().kind(), io::ErrorKind::InvalidData);
    assert!(read_document(&database, &mut transaction, "workspace-a", "provider-00000").unwrap().is_none());
    assert_eq!(list(&database, &mut transaction, "workspace-a").unwrap_err().kind(), io::ErrorKind::InvalidData);
}
