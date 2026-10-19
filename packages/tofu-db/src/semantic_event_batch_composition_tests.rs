// Real semantic/encoder boundaries for the optional native composition model.

fn composition_fee(budget: &Value, event: &Value, task_id: &str) -> usize {
    let number = |name| budget[name].as_u64().unwrap() as usize;
    let logical_type = event.get("type").and_then(Value::as_str).unwrap_or("");
    let logical_type = logical_type.chars().take(128).collect::<String>();
    let physical_type = if logical_type.is_empty() { "task_event" } else { &logical_type };
    canonical_json(event).unwrap().len() + number("eventEnvelopeBytes")
        + number("streamSegmentHeaderBytes") + task_id.len()
        + number("streamEventHeaderBytes") + physical_type.len()
        + number("metadataBaseBytes") + number("metadataTaskIdCopies") * task_id.len()
        + number("metadataTypeCopies") * logical_type.len()
}

#[test]
fn event_batch_composition_real_metadata_and_raw_encoders_match_unicode_and_stamp() {
    let budget = crate::indexed_stream::event_batch_composition_budget();
    assert_eq!(budget["maximumCarriedPayloadBytes"].as_u64().unwrap() as usize,
        crate::transaction::MAX_CARRIED_PAYLOAD_BYTES);
    for (task, event) in [
        ("ascii".to_owned(), json!({"type":"delta", "content":"a".repeat(8192)})),
        ("界".repeat(170), json!({"type":"型".repeat(20), "content":"λ".repeat(4096), "extra":{"keep":true}})),
        ("snapshot".to_owned(), json!({"type":"messages_snapshot", "kind":"request", "messages":[]})),
        ("empty".to_owned(), json!({})),
        ("scalar".to_owned(), json!(["legacy", 1])),
    ] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let result = execute(&mut database, &request("event.append_batch", 11,
            json!({"events":[{"task_id":task,"sequence":1,"event":event}]}), Some("composition-codec")), 1000).unwrap();
        assert_eq!(result["inserted"], 1);
        let mut transaction = database.begin(7, 11).unwrap();
        let mut metadata_bytes = 0;
        let mut rows = 0;
        let mut raw_segment_bytes = 0;
        for namespace in ["task_event_index", "stream_catalog"] {
            let (start, end) = crate::entity::EntityKey::prefix_range(7, 11, namespace, b"").unwrap();
            for (key, value) in database.entity_scan(&mut transaction, &start, &end, 100).unwrap() {
                metadata_bytes += 8 + key.encoded().len() + value.len();
                rows += 1;
                if value.starts_with(b"TDBSIX01") {
                    let id = crate::block::BlockId(value[12..44].try_into().unwrap());
                    raw_segment_bytes = database.block_store_shared().get(id).unwrap().len();
                }
            }
        }
        let logical_type = event.get("type").and_then(Value::as_str).unwrap_or("");
        let predicted_metadata = budget["metadataBaseBytes"].as_u64().unwrap() as usize
            + budget["metadataTaskIdCopies"].as_u64().unwrap() as usize * task.len()
            + budget["metadataTypeCopies"].as_u64().unwrap() as usize * logical_type.len();
        if !logical_type.is_empty() {
            assert_eq!(rows, 9);
            assert_eq!(predicted_metadata, metadata_bytes);
        } else {
            assert_eq!(rows, 6);
            assert!(predicted_metadata >= metadata_bytes);
        }
        assert!(composition_fee(&budget, &event, &task) >= metadata_bytes + raw_segment_bytes);
        if !logical_type.is_empty() {
            assert_eq!(composition_fee(&budget, &event, &task), metadata_bytes + raw_segment_bytes);
        }
        let read = execute(&mut database, &request("event.list", 11,
            json!({"task_id":task,"after_sequence":0,"limit":2}), None), 1000).unwrap();
        assert_eq!(read.as_array().unwrap().len(), 1);
        let expected = stamp_event_format_envelope(event).unwrap();
        assert_eq!(read[0]["event"], expected);
    }
}

#[test]
fn event_batch_composition_selected_groups_avoid_real_loose_spill_without_admission_change() {
    use crate::vfs::Operation;
    use std::sync::Arc;
    let budget = crate::indexed_stream::event_batch_composition_budget();
    let event = json!({"type":"delta","content":"a".repeat(8192)});
    let fee = composition_fee(&budget, &event, "composition-0000");
    let group_size = ((budget["maximumCarriedPayloadBytes"].as_u64().unwrap()
        - budget["transactionHeadroomBytes"].as_u64().unwrap()) as usize / fee).min(500);
    assert!((125..256).contains(&group_size));
    for size in [500, group_size] {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
        let before = vfs.trace().unwrap().len();
        for first in (0..500).step_by(size) {
            let events = (first..(first + size).min(500)).map(|index| json!({
                "task_id":format!("composition-{index:04}"),"sequence":1,"event":event})).collect::<Vec<_>>();
            let result = execute(&mut database, &request("event.append_batch", 11,
                json!({"events":events}), Some(&format!("composition-{first}"))), 1000).unwrap();
            assert_eq!(result["inserted"].as_u64().unwrap() as usize, (500 - first).min(size));
        }
        let file_syncs = vfs.trace().unwrap()[before..].iter().filter(|operation| **operation == Operation::SyncAll).count();
        if size == 500 { assert!(file_syncs >= 1000, "oversized batch must spill actual loose files"); }
        else { assert_eq!(file_syncs, 0, "selected groups stay WAL-carried"); }
        // Crash loses all non-durable file/namespace effects; WAL or spill
        // barriers must preserve every acknowledged row on a fresh reopen.
        vfs.crash().unwrap();
        let crash_image = Arc::new(vfs.fork_durable().unwrap());
        drop(database);
        let mut reopened = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), crash_image).unwrap();
        for index in 0..500 {
            let read = execute(&mut reopened, &request("event.list", 11,
                json!({"task_id":format!("composition-{index:04}"),"after_sequence":0,"limit":2}), None), 1000).unwrap();
            assert_eq!(read.as_array().unwrap().len(), 1);
            assert_eq!(read[0]["event"], stamp_event_format_envelope(event.clone()).unwrap());
        }
    }
}

#[test]
fn event_batch_demand_exceeds_former_stream_quota_and_reopens_exactly() {
    let directory = tempfile::tempdir().unwrap();
    let batch = request("event.append_batch", 11, json!({"events":
        (0..513).map(|ordinal| json!({"task_id":format!("demand-stream-{ordinal}"),
            "sequence":1,"event":{"type":"created","ordinal":ordinal}})).collect::<Vec<_>>()
    }), Some("demand-streams-command"));
    {
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let result = execute(&mut database, &batch, 1000).unwrap();
        assert_eq!(result["inserted"], 513);
        assert_eq!(result["results"].as_array().unwrap().len(), 513);
        let replay = execute(&mut database, &batch, 1000).unwrap();
        assert_eq!(replay["inserted"], 0);
        assert_eq!(replay["deduplicated"], 513);
    }
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(execute(&mut database, &batch, 1000).unwrap()["deduplicated"], 513);
    for ordinal in 0..513 {
        let rows = execute(&mut database, &request("event.list", 11,
            json!({"task_id":format!("demand-stream-{ordinal}"),"after_sequence":0,"limit":2}),None),1000).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["event"]["ordinal"], ordinal);
    }
    let foreign = execute(&mut database, &request("event.list", 12,
        json!({"task_id":"demand-stream-0","after_sequence":0,"limit":2}),None),1000).unwrap();
    assert!(foreign.as_array().unwrap().is_empty());
}
