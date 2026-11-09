//! Production-shaped `turn.event.record` load over one large live Turn.
//!
//! Opt-in measurement, never a timing gate. One IR transaction per event (the
//! production single-event path) over a projection of production dimensions
//! (~4 MB: tool rounds whose results are mirrored by their render segments,
//! API rounds, an activity timeline and cumulative text). Event classes follow
//! `lib/turn_lifecycle.py`: throttled text appends diff only content/thinking;
//! 3 s structural folds and tool start/result frames diff the stable-segment
//! projection and set `projection_segments_stable`. Prints per-class decoded
//! checkpoint bytes, replayed patches, checkpoint writes, WAL bytes and
//! prepare+commit latency, plus 1 MiB chunk reuse between consecutive
//! checkpoints. Run on the baseline and the changed engine with the same
//! shape:
//! `cargo test --release --lib turn::event_load_tests -- --ignored --nocapture --test-threads=1`
use super::*;
use crate::generated_storage_operations::StorageOperationKind;
use crate::transaction_ir::{
    prepare_transaction_ir_with_deadline, IndexedStreamAppendItem, TransactionIr,
    TransactionIrCommandEffects, TransactionIrResult, TransactionIrStep,
};
use std::time::{Duration, Instant};

pub(super) const CONVERSATION: &str = "load-conversation";
pub(super) const ATTEMPT: &str = "load-attempt";
pub(super) const TASK: &str = "load-task";
pub(super) const TURN: &str = "load-turn";
const ACTOR: &str = "assistant";
pub(super) const HISTORY_ROUNDS: usize = 150;
const RESULT_BYTES: usize = 10 * 1024;
const LLM_ROUNDS: usize = 12;

fn filler(seed: u64, bytes: usize) -> String {
    const WORDS: [&str; 16] = [
        "fn", "let", "match", "Some(value)", "=>", "{", "}", "\"quoted\"",
        "path/to/file.rs:42", "error:", "汉字", "😀", "\\n", "tab\t", "0x1f", "result",
    ];
    let mut state = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
    let mut text = String::with_capacity(bytes + 32);
    while text.len() < bytes {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        text.push_str(WORDS[(state % 16) as usize]);
        text.push(if state % 11 == 0 { '\n' } else { ' ' });
    }
    text
}

fn tool_round(index: usize, done: bool) -> Value {
    let mut round = json!({
        "roundNum": index + 1, "llmRound": index + 1,
        "toolCallId": format!("call_{index:05}"), "toolName": "run_command",
        "toolArgs": {"command": format!("rg -n pattern_{index} lib/"), "timeout": 120},
        "status": if done { "done" } else { "running" },
        "startedAt": 1_790_000_000_000_u64 + index as u64 * 1000,
    });
    if done {
        round["toolContent"] = json!(filler(index as u64, RESULT_BYTES));
        round["finishedAt"] = json!(1_790_000_000_500_u64 + index as u64 * 1000);
    }
    round
}

fn api_round(index: usize) -> Value {
    json!({"round": index + 1, "model": "claude-opus", "providerId": "provider-a",
        "usage": {"input_tokens": 90_000 + index, "output_tokens": 800 + index,
            "cache_read_input_tokens": 85_000 + index, "cache_creation_input_tokens": 1200},
        "cost": {"input": 0.27, "output": 0.06, "cacheRead": 0.04, "total": 0.37},
        "routeSnapshot": {"route": "primary", "region": "a", "attempt": 1,
            "headers": {"x-request-id": format!("req-{index:08}")}},
        "latencyMs": 2400 + index})
}

fn timeline_entry(index: usize) -> Value {
    json!({"kind": if index % 2 == 0 { "tool" } else { "llm" }, "at": 1_790_000_000_000_u64 + index as u64 * 500,
        "label": format!("step {index}"), "durationMs": 120 + index})
}

fn stable(projection: &Map<String, Value>) -> Map<String, Value> {
    crate::turn_projection_segments::stabilize_for_patch(projection, ACTOR, "running").unwrap()
}

pub(super) fn production_projection() -> Map<String, Value> {
    let mut projection = Map::new();
    projection.insert("content".into(), json!(filler(1, 40 * 1024)));
    projection.insert("thinking".into(), json!(filler(2, 120 * 1024)));
    projection.insert("segments".into(), json!([]));
    projection.insert("toolRounds".into(),
        Value::Array((0..HISTORY_ROUNDS).map(|i| tool_round(i, true)).collect()));
    projection.insert("apiRounds".into(),
        Value::Array((0..HISTORY_ROUNDS).map(api_round).collect()));
    projection.insert("activityTimeline".into(),
        Value::Array((0..2 * HISTORY_ROUNDS).map(timeline_entry).collect()));
    projection.insert("timingTrace".into(), json!({"spans": (0..400).map(|i| json!({
        "name": format!("span-{i}"), "startMs": i * 10, "endMs": i * 10 + 7})).collect::<Vec<_>>()}));
    projection.insert("model".into(), json!("claude-opus"));
    projection.insert("usage".into(), json!({"input_tokens": 1, "output_tokens": 1}));
    stable(&projection)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Ord, PartialOrd)]
pub(super) enum Class {
    Text,
    Fold,
    Phase,
    ToolStart,
    ToolProgress,
    ToolResult,
}

pub(super) struct Producer {
    pub(super) committed: Map<String, Value>,
    pub(super) revision: u64,
    pub(super) sequence: u64,
    pub(super) now_ms: u64,
    pub(super) rounds: usize,
}

impl Producer {
    pub(super) fn request(&mut self, class: Class) -> EventRecordRequest {
        self.sequence += 1;
        self.now_ms += 300;
        let (patch, next, stable_flag) = match class {
            Class::Text | Class::ToolProgress => {
                let mut next = self.committed.clone();
                if class == Class::Text {
                    let field = if self.sequence % 3 == 0 { "content" } else { "thinking" };
                    let mut text = next[field].as_str().unwrap().to_owned();
                    text.push_str(&filler(self.sequence, 240));
                    next.insert(field.into(), json!(text));
                }
                let pick = |projection: &Map<String, Value>| {
                    Map::from_iter(["content", "thinking"].iter()
                        .map(|field| ((*field).to_owned(), projection[*field].clone())))
                };
                let patch = crate::turn_projection_patch::build_projection_patch(
                    &pick(&self.committed), &pick(&next), self.revision, self.revision + 1).unwrap();
                (patch, next, false)
            }
            Class::Fold | Class::Phase | Class::ToolStart | Class::ToolResult => {
                let previous = stable(&self.committed);
                let mut next = previous.clone();
                match class {
                    // A model-fallback phase: one top-level field, the
                    // execution-frame witness grammar.
                    Class::Phase => {
                        next.insert("fallbackModel".into(), json!(format!("model-{}", self.sequence % 4)));
                    }
                    Class::Fold => {
                        let mut text = next["content"].as_str().unwrap().to_owned();
                        text.push_str(&filler(self.sequence, 240));
                        next.insert("content".into(), json!(text));
                    }
                    Class::ToolStart => {
                        let index = self.rounds;
                        next["toolRounds"].as_array_mut().unwrap().push(tool_round(index, false));
                        next["apiRounds"].as_array_mut().unwrap().push(api_round(index));
                        next["activityTimeline"].as_array_mut().unwrap().push(timeline_entry(2 * index));
                    }
                    Class::ToolResult => {
                        let index = self.rounds;
                        let rounds = next["toolRounds"].as_array_mut().unwrap();
                        *rounds.last_mut().unwrap() = tool_round(index, true);
                        next["activityTimeline"].as_array_mut().unwrap().push(timeline_entry(2 * index + 1));
                        self.rounds += 1;
                    }
                    _ => unreachable!(),
                }
                let next = stable(&next);
                let patch = crate::turn_projection_patch::build_projection_patch(
                    &previous, &next, self.revision, self.revision + 1).unwrap();
                (patch, next, true)
            }
        };
        self.committed = next;
        self.revision += 1;
        let kind = match class {
            Class::Text => "delta", Class::Fold => "delta", Class::Phase => "phase",
            Class::ToolStart => "tool_start",
            Class::ToolProgress => "tool_progress", Class::ToolResult => "tool_result",
        };
        base_request(self.sequence, self.now_ms, kind, Some(patch), stable_flag)
    }
}

/// The settling event of the producer's attempt.
pub(super) fn terminal_request(producer: &mut Producer) -> EventRecordRequest {
    producer.sequence += 1;
    producer.now_ms += 300;
    let patch = json!({"version": 1, "baseRevision": producer.revision,
        "targetRevision": producer.revision + 1, "operations": []});
    let mut request = base_request(producer.sequence, producer.now_ms, "done",
        patch.as_object().cloned(), false);
    request.terminal = true;
    request.status = "completed".into();
    request.settlement = json!({"outcome": "completed", "cause": "finished", "resumeOptions": []})
        .as_object().unwrap().clone();
    producer.revision += 1;
    request
}

pub(super) fn base_request(sequence: u64, now_ms: u64, kind: &str, patch: Option<Map<String, Value>>,
    projection_segments_stable: bool) -> EventRecordRequest
{
    EventRecordRequest {
        worker_claim: None, model_continuation: Value::Null, attempt_id: ATTEMPT.into(),
        task_id: TASK.into(), terminal: false, status: "running".into(), slim: false,
        content: String::new(), thinking: String::new(), projection: Value::Null,
        projection_patch: patch, projection_segments_stable,
        settlement: Map::new(), error: Map::new(), event_type: "projection_updated".into(),
        event_payload: Map::from_iter([("phase".into(), Value::Null),
            ("updateKind".into(), json!(kind))]),
        background_command_ack: None,
        task_event: Some(IndexedStreamAppendItem {
            worker_claim: None, task_id: TASK.into(), application_sequence: sequence,
            event_type: kind.into(),
            payload_json: serde_json::to_vec(&json!({"type": kind, "sequence": sequence})).unwrap(),
            created_at_ms: now_ms as i64, require_attempts_live: Vec::new() }),
        preceding_task_events: Vec::new(), flow_root_attempt_id: String::new(), now_ms,
    }
}

pub(super) fn single_event_ir(request: EventRecordRequest) -> TransactionIr {
    let payload = serde_json::to_vec(&json!({"sequence": request.now_ms,
        "patch": request.projection_patch})).unwrap();
    TransactionIr { tenant_id: 7, owner_user_id: 11, operation: "turn.event.record".into(),
        operation_kind: StorageOperationKind::Command,
        result: TransactionIrResult::SlotOrLiteral { slot: 0, missing: b"null".to_vec() },
        command_effects: Some(TransactionIrCommandEffects { legacy_receipt_owner_bound: true,
            command_id: format!("load-event-{}", request.now_ms),
            request_digest: Sha256::digest(&payload).into(), alternate_request_digest: None,
            request_id: format!("load-request-{}", request.now_ms), schema_version: 1,
            registry_version: 1, committed_at_ms: request.now_ms, outbox_payload: payload,
            store_receipt: false }),
        steps: vec![TransactionIrStep::TurnEventRecord { request, slot: 0 }] }
}

pub(super) fn seed(database: &mut AuthorityDatabase, projection: &Map<String, Value>) -> u64 {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(database, &mut tx, &AppendSettledRequest {
        conversation_id: CONVERSATION.into(), actor: ACTOR.into(), status: "pending".into(),
        projection_json: b"{}".to_vec(), projection_timestamp: None,
        settlement_json: b"{}".to_vec(), lane_id: "main".into(), command_id: "seed-load".into(),
        kind: "reply".into(), run_id: String::new(), turn_id: TURN.into(),
        attempt_id: Some(ATTEMPT.into()), created_at_ms: 1000, committed_at_ms: 1000,
        defaults: TurnDefaults { allow_create: true, title: "Load".into(),
            settings_json: b"{}".to_vec(), created_at_ms: 1000 },
    }).unwrap();
    database.commit(tx).unwrap();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    attempt_bind(database, &mut tx, ATTEMPT, TASK, "", 1100).unwrap();
    database.commit(tx).unwrap();
    let mut first = base_request(1, 1200, "tool_progress", None, false);
    first.projection = Value::Object(projection.clone());
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let response: Value = serde_json::from_slice(&event_record(database, &mut tx, &first).unwrap()).unwrap();
    database.commit(tx).unwrap();
    response["projection_revision"].as_u64().unwrap()
}

pub(super) fn current_head(database: &AuthorityDatabase) -> Option<ProjectionHeadDescriptor> {
    let mut probe = base_request(0, 0, "tool_progress", None, false);
    probe.task_event = None;
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    load_event_record_state(database, &mut tx, &probe, false).unwrap().unwrap().1.projection_head
}

fn chunk_hashes(revision: u64, projection: &Map<String, Value>) -> (usize, Vec<[u8; 32]>) {
    let bytes = serde_json::to_vec(&json!({"attemptId": ATTEMPT, "revision": revision,
        "projection": projection})).unwrap();
    (bytes.len(), bytes.chunks(crate::blob::BLOB_CHUNK_BYTES)
        .map(|chunk| *blake3::hash(chunk).as_bytes()).collect())
}

const STAGES: [(&str, usize); 10] = [
    ("materialize", crate::fsync_stats::STAGE_PROJECTION_HEAD_MATERIALIZE),
    ("load", crate::fsync_stats::STAGE_TURN_EVENT_LOAD),
    ("projection", crate::fsync_stats::STAGE_TURN_EVENT_PROJECTION),
    ("store", crate::fsync_stats::STAGE_TURN_EVENT_STORE),
    ("publish", crate::fsync_stats::STAGE_TURN_EVENT_PUBLISH),
    ("blobEncode", crate::fsync_stats::STAGE_BLOB_ENCODE),
    ("blobStage", crate::fsync_stats::STAGE_BLOB_STAGE),
    ("irSteps", crate::fsync_stats::STAGE_IR_STEPS),
    ("engineCommit", crate::fsync_stats::STAGE_ENGINE_COMMIT),
    ("wal", crate::fsync_stats::STAGE_WAL),
];

#[derive(Default)]
struct ClassTotals {
    events: usize,
    materializations: u64,
    checkpoint_decoded: u64,
    patches_applied: u64,
    wal: u64,
    checkpoint_writes: usize,
    micros: Vec<u64>,
    stage_nanos: [u64; STAGES.len()],
}

fn percentile(sorted: &[u64], fraction: f64) -> f64 {
    if sorted.is_empty() { return 0.0; }
    let index = ((sorted.len() as f64 - 1.0) * fraction).round() as usize;
    sorted[index] as f64 / 1000.0
}

fn stage_snapshot() -> [u64; STAGES.len()] {
    STAGES.map(|(_, stage)| crate::fsync_stats::stage_nanos(stage))
}

fn summary(entry: &mut ClassTotals) -> Value {
    entry.micros.sort_unstable();
    let events = entry.events.max(1) as f64;
    json!({
        "events": entry.events,
        "materializations": entry.materializations,
        "checkpointDecodedBytesPerEvent": (entry.checkpoint_decoded as f64 / events).round(),
        "patchesReplayedPerEvent": entry.patches_applied as f64 / events,
        "walBytesPerEvent": (entry.wal as f64 / events).round(),
        "checkpointWrites": entry.checkpoint_writes,
        "p50Ms": percentile(&entry.micros, 0.5), "p99Ms": percentile(&entry.micros, 0.99),
        "meanMs": entry.micros.iter().sum::<u64>() as f64 / events / 1000.0,
        "stageMsPerEvent": Map::from_iter(STAGES.iter().zip(entry.stage_nanos).map(|((name, _), nanos)|
            ((*name).to_owned(), json!((nanos as f64 / events / 1e4).round() / 100.0)))),
    })
}

/// One production-path transaction per scheduled event over a fresh large Turn.
fn run_scenario(scenario: &str, schedule: &[Class]) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let projection = production_projection();
    let projection_bytes = serde_json::to_vec(&projection).unwrap().len();
    let revision = seed(&mut database, &projection);
    let mut producer = Producer { committed: projection, revision, sequence: 10, now_ms: 5000,
        rounds: HISTORY_ROUNDS };
    let mut totals = std::collections::BTreeMap::<Class, ClassTotals>::new();
    let mut previous_head = current_head(&database);
    let mut previous_chunks: Option<Vec<[u8; 32]>> = None;
    let (mut written_logical, mut written_new_chunk_bytes) = (0_usize, 0_usize);
    let started = Instant::now();
    for &class in schedule {
        let request = producer.request(class);
        let target_revision = producer.revision;
        let ir = single_event_ir(request);
        let before = crate::fsync_stats::counters();
        let stages_before = stage_snapshot();
        let materialize_before = crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_PROJECTION_HEAD_MATERIALIZE);
        let clock = Instant::now();
        let prepared = prepare_transaction_ir_with_deadline(&mut database, &ir, Duration::MAX).unwrap();
        database.commit(prepared.transaction.expect("applied event commits")).unwrap();
        let micros = clock.elapsed().as_micros() as u64;
        let after = crate::fsync_stats::counters();
        let stages_after = stage_snapshot();
        let materialize_after = crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_PROJECTION_HEAD_MATERIALIZE);
        let response: Value = serde_json::from_slice(&prepared.response.into_bytes()).unwrap();
        assert_eq!(response["applied"], true, "{class:?}");
        assert_eq!(response["projection_revision"], target_revision);
        let head = current_head(&database);
        let rewrote = head.as_ref().is_some_and(|head| head.patch_count == 0
            && previous_head.as_ref().is_none_or(|old| old.head_id != head.head_id));
        if rewrote {
            let (bytes, chunks) = chunk_hashes(head.as_ref().unwrap().base_revision, &producer.committed);
            written_logical += bytes;
            let reused: std::collections::HashSet<_> = previous_chunks.iter().flatten().collect();
            written_new_chunk_bytes += chunks.iter().enumerate()
                .filter(|(_, hash)| !reused.contains(hash))
                .map(|(index, _)| (bytes - index * crate::blob::BLOB_CHUNK_BYTES).min(crate::blob::BLOB_CHUNK_BYTES))
                .sum::<usize>();
            previous_chunks = Some(chunks);
        }
        previous_head = head;
        let entry = totals.entry(class).or_default();
        entry.events += 1;
        entry.materializations += materialize_after - materialize_before;
        entry.checkpoint_decoded += after.projection_checkpoint_bytes - before.projection_checkpoint_bytes;
        entry.patches_applied += after.projection_patches_applied - before.projection_patches_applied;
        entry.wal += after.wal_encoded_bytes - before.wal_encoded_bytes;
        entry.checkpoint_writes += usize::from(rewrote);
        entry.micros.push(micros);
        for (slot, (after, before)) in entry.stage_nanos.iter_mut().zip(stages_after.iter().zip(stages_before)) {
            *slot += after - before;
        }
    }
    let elapsed = started.elapsed();
    let mut all = ClassTotals::default();
    for (class, entry) in &mut totals {
        all.events += entry.events;
        all.materializations += entry.materializations;
        all.checkpoint_decoded += entry.checkpoint_decoded;
        all.patches_applied += entry.patches_applied;
        all.wal += entry.wal;
        all.checkpoint_writes += entry.checkpoint_writes;
        all.micros.extend(&entry.micros);
        for (slot, nanos) in all.stage_nanos.iter_mut().zip(entry.stage_nanos) { *slot += nanos; }
        println!("event_load scenario={scenario} class={class:?} {}", summary(entry));
    }
    let mut total = summary(&mut all);
    total["projectionBytes"] = json!(projection_bytes);
    total["checkpointDecodedBytes"] = json!(all.checkpoint_decoded);
    total["walBytes"] = json!(all.wal);
    total["checkpointWrittenLogicalBytes"] = json!(written_logical);
    total["checkpointWrittenNewChunkBytes"] = json!(written_new_chunk_bytes);
    total["elapsedMs"] = json!(elapsed.as_millis() as u64);
    println!("event_load scenario={scenario} total {total}");
    // A reader of the live Turn materializes the durable head on both engines.
    let expected = producer.committed.clone();
    let head = current_head(&database).unwrap();
    let before = crate::fsync_stats::counters();
    let clock = Instant::now();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let turn: Value = serde_json::from_slice(&get(&database, &mut tx, CONVERSATION, TURN).unwrap().unwrap()).unwrap();
    drop(tx);
    let read_micros = clock.elapsed().as_micros() as u64;
    let after = crate::fsync_stats::counters();
    assert_eq!(turn["projection"], Value::Object(expected), "{scenario}: final public projection");
    println!("event_load scenario={scenario} liveRead {}", json!({
        "ms": read_micros as f64 / 1000.0, "chainPatches": head.patch_count,
        "textTail": head.text_tail.is_some(),
        "checkpointDecodedBytes": after.projection_checkpoint_bytes - before.projection_checkpoint_bytes,
        "patchesReplayed": after.projection_patches_applied - before.projection_patches_applied,
    }));
    // Settlement retires the live head in one transaction.
    let ir = single_event_ir(terminal_request(&mut producer));
    let (before, stages_before) = (crate::fsync_stats::counters(), stage_snapshot());
    let materialize_before = crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_PROJECTION_HEAD_MATERIALIZE);
    let clock = Instant::now();
    let prepared = prepare_transaction_ir_with_deadline(&mut database, &ir, Duration::MAX).unwrap();
    database.commit(prepared.transaction.expect("terminal event commits")).unwrap();
    let micros = clock.elapsed().as_micros() as u64;
    let (after, stages_after) = (crate::fsync_stats::counters(), stage_snapshot());
    let response: Value = serde_json::from_slice(&prepared.response.into_bytes()).unwrap();
    assert_eq!(response["applied"], true, "{scenario}: terminal");
    println!("event_load scenario={scenario} terminal {}", json!({
        "ms": micros as f64 / 1000.0,
        "materializations": crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_PROJECTION_HEAD_MATERIALIZE) - materialize_before,
        "checkpointDecodedBytes": after.projection_checkpoint_bytes - before.projection_checkpoint_bytes,
        "walBytes": after.wal_encoded_bytes - before.wal_encoded_bytes,
        "stageMs": Map::from_iter(STAGES.iter().zip(stages_after.iter().zip(stages_before)).map(|((name, _), (after, before))|
            ((*name).to_owned(), json!(((after - before) as f64 / 1e4).round() / 100.0)))),
    }));
    // Every public surface after a cold reopen, for a byte diff between engines.
    drop(database);
    let reopened = AuthorityDatabase::open(directory.path()).unwrap();
    let dump = std::env::temp_dir().join(format!("turn-event-load-{scenario}.json"));
    std::fs::write(&dump, serde_json::to_vec_pretty(&public_state(&reopened)).unwrap()).unwrap();
    println!("event_load scenario={scenario} state={}", dump.display());
}

pub(super) fn public_state(database: &AuthorityDatabase) -> Value {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let parse = |bytes: Vec<u8>| serde_json::from_slice::<Value>(&bytes).unwrap();
    let turn = parse(get(database, &mut tx, CONVERSATION, TURN).unwrap().unwrap());
    let attempt = parse(attempt_get(database, &mut tx, ATTEMPT).unwrap().unwrap());
    let events = parse(events_list(database, &mut tx, ATTEMPT, 0, 1000, true, &mut || false).unwrap().unwrap());
    let hydrated = parse(events_list(database, &mut tx, ATTEMPT, 0, 1000, false, &mut || false).unwrap().unwrap());
    let sync = parse(sync_changes(database, &mut tx, CONVERSATION, 0, 1000, &mut || false).unwrap().unwrap());
    let header = parse(crate::conversation_header::get(database, &mut tx, CONVERSATION, false, 0, None, &mut || false).unwrap().unwrap());
    let raw = parse(crate::indexed_stream::list(database, &mut tx, &crate::indexed_stream::ListRequest {
        task_id: TASK.into(), after_sequence: None, limit: 1000, types: Vec::new(), type_prefixes: Vec::new(),
        payload_types: None, inspector_projection: false }).unwrap());
    json!({"turn": turn, "attempt": attempt, "events": events, "hydrated": hydrated, "sync": sync,
        "header": header, "raw": raw})
}

/// Streaming LLM rounds: throttled text with one 3 s structural fold, then a
/// tool call (start, two progress ticks, result).
#[test]
#[ignore = "opt-in production-shape measurement; run alone with --nocapture --test-threads=1"]
fn production_shaped_streaming_rounds() {
    let mut schedule = Vec::new();
    for _ in 0..LLM_ROUNDS {
        schedule.push(Class::Phase);
        for step in 0..10 {
            schedule.push(if step == 5 { Class::Fold } else { Class::Text });
        }
        schedule.extend([Class::ToolStart, Class::ToolProgress, Class::ToolProgress, Class::ToolResult]);
    }
    run_scenario("streaming", &schedule);
}

/// Back-to-back tool calls with no model text between them: the patch chain
/// grows past its 64-patch checkpoint boundary.
#[test]
#[ignore = "opt-in production-shape measurement; run alone with --nocapture --test-threads=1"]
fn production_shaped_tool_burst() {
    let schedule: Vec<_> = (0..48).flat_map(|_| [Class::ToolStart, Class::ToolResult]).collect();
    run_scenario("tool-burst", &schedule);
}
