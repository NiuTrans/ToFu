//! Stable-segment normalization applied before client projection patches.
//!
//! Responsibility: mirror the application/Sidecar patch-target contract
//! (`lib/turn_projection_segments.py::projection_with_stable_segments`) at the
//! one boundary where the authority applies an authenticated producer patch:
//! producers diff against the stable-segment read, so the locked storage
//! document must cross the same normalization before the delta is applied —
//! otherwise positional `segments[i]` operations cannot resolve (the 2026-09-16
//! tofudb cutover wedge: every structural `turn.event.record` failed closed
//! with `database_protocol_error`).
//!
//! Entry point: [`stabilize_for_patch`]. Dependencies: serde_json only; the
//! patch machinery itself lives in `turn_projection_patch.rs`.
//!
//! Deliberate scope boundary: this ports only the segment-level invariants
//! (validation, note dedup, terminal-block sync, narrated-mirror drop, stable
//! block ids). Document-level cosmetic normalizers of the Python entry point
//! (sidecar blockId assignment, legacy route-snapshot inference, compacted
//! tool-result mirroring) do not change which patch operations resolve and are
//! TODO(native-parity) if stored-byte parity with the legacy authority is ever
//! required. States that would need full finished-turn segment assembly
//! (missing/invalid `segments`, or uncovered tool rounds on terminal
//! projections) fail closed with `database_integrity`. Valid running projections
//! use the same recoverable normalization as the producer:
//! missing live tool blocks are reconstructed from their durable rounds under
//! the revision lock, matching the producer and emitting the repair in replay.

use serde_json::{Map, Value};
use std::io;

const SEGMENT_TYPES: [&str; 4] = ["text", "thinking", "tool_use", "system_note"];
const RESUMABLE_STATUSES: [&str; 2] = ["interrupted", "truncated"];
const SYNTHETIC_INBOX_MARKERS: [&str; 5] = [
    "_inboxInject",
    "_peerInject",
    "_userSteerInject",
    "_stallNudge",
    "_programSynthetic",
];
const NOTE_KINDS: [&str; 5] = [
    "intent-stall",
    "todo-continuation",
    "system-intervention",
    "round-context",
    "session-prefix",
];
const NOTE_ACTIONS: [&str; 4] = ["prompt", "stop", "tool-policy", "continue"];

fn integrity(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// Python truthiness for JSON values (False/None/0/""/[]/{} are falsy).
fn json_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64() != Some(0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
}

fn text_of(projection: &Map<String, Value>, field: &str) -> String {
    projection
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

/// Port of `_valid_segments`: every item must be an object of a known segment
/// type; text/thinking/system_note require a string `text`, tool_use requires
/// an object `result`. `None` (invalid) means the producer base would have
/// required finished-turn assembly, which this authority does not reproduce.
fn valid_segments(value: Option<&Value>) -> Option<Vec<Map<String, Value>>> {
    let Value::Array(items) = value? else {
        return None;
    };
    let mut segments = Vec::with_capacity(items.len());
    for item in items {
        let segment = item.as_object()?.clone();
        let segment_type = segment.get("type").and_then(Value::as_str)?;
        if !SEGMENT_TYPES.contains(&segment_type) {
            return None;
        }
        if matches!(segment_type, "text" | "thinking" | "system_note") {
            if segment.get("text").and_then(Value::as_str).is_none() {
                return None;
            }
        } else if !segment.get("result").is_some_and(Value::is_object) {
            return None;
        }
        segments.push(segment);
    }
    Some(segments)
}

/// Port of `_preserved_notes`: salvage display-only notes with their defaults.
fn preserved_notes(value: Option<&Value>) -> Vec<Map<String, Value>> {
    let mut notes = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let Some(Value::Array(items)) = value else {
        return notes;
    };
    for item in items {
        let Some(note) = item.as_object() else {
            continue;
        };
        if note.get("type").and_then(Value::as_str) != Some("system_note") {
            continue;
        }
        if note.get("text").and_then(Value::as_str).is_none() {
            continue;
        }
        let mut note = note.clone();
        if !note
            .get("noteKind")
            .and_then(Value::as_str)
            .is_some_and(|kind| NOTE_KINDS.contains(&kind))
        {
            note.insert(
                "noteKind".to_owned(),
                Value::String("system-intervention".to_owned()),
            );
        }
        if !note
            .get("action")
            .and_then(Value::as_str)
            .is_some_and(|action| NOTE_ACTIONS.contains(&action))
        {
            note.insert("action".to_owned(), Value::String("prompt".to_owned()));
        }
        let identity = note
            .get("blockId")
            .and_then(Value::as_str)
            .filter(|identity| !identity.is_empty());
        if let Some(identity) = identity {
            if !seen.insert(identity.to_owned()) {
                continue;
            }
        }
        notes.push(note);
    }
    notes
}

/// Port of `_segments_missing_tool_round`: one-directional coverage check.
/// A round with a real `toolCallId` and no covering `tool_use` segment id
/// needs live tool-block assembly from the durable round evidence.
fn segments_missing_tool_round(
    segments: &[Map<String, Value>],
    tool_rounds: Option<&Value>,
) -> bool {
    let Some(Value::Array(rounds)) = tool_rounds else {
        return false;
    };
    if rounds.is_empty() {
        return false;
    }
    let mut covered: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    for segment in segments {
        if segment.get("type").and_then(Value::as_str) != Some("tool_use") {
            continue;
        }
        let call_id = segment
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if !call_id.is_empty() {
            *covered.entry(call_id.to_owned()).or_default() += 1;
        }
    }
    let mut seen: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    for round in rounds {
        let Some(round) = round.as_object() else {
            continue;
        };
        if SYNTHETIC_INBOX_MARKERS
            .iter()
            .any(|marker| json_truthy(round.get(*marker)))
        {
            continue;
        }
        let call_id = round
            .get("toolCallId")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if call_id.is_empty() {
            continue;
        }
        let occurrence = seen.entry(call_id.to_owned()).or_default();
        *occurrence += 1;
        if *occurrence > covered.get(call_id).copied().unwrap_or(0) {
            return true;
        }
    }
    false
}

/// Port of `_live_content_already_narrated`: only the latest non-terminal
/// narration can own the current accumulator text, and only while running.
fn live_content_already_narrated(
    segments: &[Map<String, Value>],
    content: &str,
    status: &str,
) -> bool {
    if status != "running" || content.trim().is_empty() {
        return false;
    }
    for segment in segments.iter().rev() {
        if segment.get("type").and_then(Value::as_str) == Some("text")
            && !json_truthy(segment.get("terminal"))
            && !json_truthy(segment.get("deliverable"))
        {
            return segment
                .get("text")
                .and_then(Value::as_str)
                .map(str::trim)
                == Some(content.trim());
        }
    }
    false
}

/// Port of `_synchronize_terminal_blocks`: ensure exactly one terminal text
/// deliverable (placeholder for non-human actors) and mirror the current
/// content/thinking into the terminal blocks.
fn synchronize_terminal_blocks(
    segments: &mut Vec<Map<String, Value>>,
    content: &str,
    thinking: &str,
    actor: &str,
    status: &str,
    ensure_assistant_text_placeholder: bool,
) {
    let terminal_text_index = (0..segments.len())
        .rev()
        .find(|index| {
            segments[*index].get("type").and_then(Value::as_str) == Some("text")
                && (json_truthy(segments[*index].get("terminal"))
                    || json_truthy(segments[*index].get("deliverable")))
        });
    let mut terminal_text_index = terminal_text_index;
    if terminal_text_index.is_none()
        && (!content.is_empty() || (ensure_assistant_text_placeholder && actor != "human"))
        && !live_content_already_narrated(segments, content, status)
    {
        segments.push(Map::from_iter([
            ("type".to_owned(), Value::String("text".to_owned())),
            ("blockId".to_owned(), Value::String("text:terminal".to_owned())),
            ("text".to_owned(), Value::String(content.to_owned())),
            ("deliverable".to_owned(), Value::Bool(true)),
            ("terminal".to_owned(), Value::Bool(true)),
        ]));
        terminal_text_index = Some(segments.len() - 1);
    }
    if let Some(index) = terminal_text_index {
        let terminal_text = &mut segments[index];
        terminal_text.insert("text".to_owned(), Value::String(content.to_owned()));
        terminal_text.insert("deliverable".to_owned(), Value::Bool(true));
        terminal_text.insert("terminal".to_owned(), Value::Bool(true));
        if RESUMABLE_STATUSES.contains(&status) {
            terminal_text.insert("resumable".to_owned(), Value::Bool(true));
        } else {
            terminal_text.remove("resumable");
        }
    }
    let terminal_thinking_index = (0..segments.len()).rev().find(|index| {
        segments[*index].get("type").and_then(Value::as_str) == Some("thinking")
            && json_truthy(segments[*index].get("terminal"))
    });
    if let Some(index) = terminal_thinking_index {
        segments[index].insert("text".to_owned(), Value::String(thinking.to_owned()));
    } else if !thinking.is_empty() {
        let thinking_segment = Map::from_iter([
            ("type".to_owned(), Value::String("thinking".to_owned())),
            (
                "blockId".to_owned(),
                Value::String("thinking:terminal".to_owned()),
            ),
            ("text".to_owned(), Value::String(thinking.to_owned())),
            ("deliverable".to_owned(), Value::Bool(false)),
            ("terminal".to_owned(), Value::Bool(true)),
        ]);
        match terminal_text_index {
            None => segments.push(thinking_segment),
            Some(index) => segments.insert(index, thinking_segment),
        }
    }
}

/// Port of `_drop_narrated_live_terminal_mirror`: an existing terminal mirror
/// that duplicates live narration is removed (running turns only).
fn drop_narrated_live_terminal_mirror(
    segments: Vec<Map<String, Value>>,
    content: &str,
    status: &str,
) -> Vec<Map<String, Value>> {
    if !live_content_already_narrated(segments.as_slice(), content, status) {
        return segments;
    }
    segments
        .into_iter()
        .filter(|segment| {
            !(segment.get("type").and_then(Value::as_str) == Some("text")
                && (json_truthy(segment.get("terminal"))
                    || json_truthy(segment.get("deliverable"))))
        })
        .collect()
}

/// Port of `_fallback_block_id` for segments without a declared identity.
fn fallback_block_id(segment: &Map<String, Value>, position: usize) -> String {
    let segment_type = segment
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("text");
    if segment_type == "tool_use" {
        let call_id = segment
            .get("id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if !call_id.is_empty() {
            return format!("tool:{call_id}");
        }
    }
    if json_truthy(segment.get("terminal")) {
        return format!("{segment_type}:terminal");
    }
    if segment
        .get("llmRound")
        .and_then(Value::as_u64)
        .is_some()
    {
        let llm_round = segment.get("llmRound").and_then(Value::as_u64).unwrap();
        let attempt_id = segment.get("attemptId").and_then(Value::as_str);
        let task_id = segment.get("taskId").and_then(Value::as_str);
        let scope = attempt_id.or(task_id).unwrap_or("");
        let prefix = if scope.is_empty() {
            String::new()
        } else {
            format!("attempt-{scope}:")
        };
        return format!("{segment_type}:{prefix}llm-{llm_round}");
    }
    format!("{segment_type}:legacy-{position}")
}

/// Port of `_stable_block_ids` (no sidecar reservations on this boundary):
/// keep declared block ids, derive fallback ids, disambiguate repeats with
/// the `~n` suffix, and retire the `_round` mirror from stored documents.
fn stable_block_ids(segments: &[Map<String, Value>]) -> Vec<Value> {
    let mut claimed: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    let mut normalized = Vec::with_capacity(segments.len());
    for (position, source) in segments.iter().enumerate() {
        let mut segment = source.clone();
        let preferred = segment
            .get("blockId")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|declared| !declared.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| fallback_block_id(&segment, position));
        let occurrence = claimed.entry(preferred.clone()).or_default();
        *occurrence += 1;
        let block_id = if *occurrence == 1 {
            preferred
        } else {
            format!("{preferred}~{occurrence}")
        };
        segment.insert("blockId".to_owned(), Value::String(block_id));
        segment.remove("_round");
        normalized.push(Value::Object(segment));
    }
    normalized
}

/// Normalize one locked storage projection to the producer patch baseline.
///
/// Returns the normalized document; the caller compares it with the stored
/// document to decide whether the committed replay patch must be rebuilt
/// from the raw wire base (normalization delta) or may reuse the client's
/// validated patch verbatim.
pub(crate) fn stabilize_for_patch(
    previous: &Map<String, Value>,
    actor: &str,
    status: &str,
) -> io::Result<Map<String, Value>> {
    let mut projection = previous.clone();
    let raw_segments = projection.get("segments").cloned();
    let Some(mut segments) = valid_segments(raw_segments.as_ref()) else {
        return Err(integrity(
            "stored turn projection segments require full assembly",
        ));
    };
    // System-note identity dedup mirrors the producer boundary: keep the
    // first occurrence of each declared blockId, with note defaults applied.
    let notes = preserved_notes(raw_segments.as_ref());
    if !notes.is_empty() {
        let notes_by_id: std::collections::HashMap<&str, &Map<String, Value>> = notes
            .iter()
            .filter_map(|note| {
                note.get("blockId")
                    .and_then(Value::as_str)
                    .map(|identity| (identity, note))
            })
            .collect();
        let mut seen_notes = std::collections::HashSet::new();
        let mut deduped = Vec::with_capacity(segments.len());
        for mut item in segments {
            if item.get("type").and_then(Value::as_str) == Some("system_note") {
                let identity = item
                    .get("blockId")
                    .and_then(Value::as_str)
                    .filter(|identity| !identity.is_empty());
                if let Some(identity) = identity {
                    if !seen_notes.insert(identity.to_owned()) {
                        continue;
                    }
                    if let Some(note) = notes_by_id.get(identity) {
                        item = (*note).clone();
                    }
                }
            }
            deduped.push(item);
        }
        segments = deduped;
    }
    let content = text_of(&projection, "content");
    let thinking = text_of(&projection, "thinking");
    if segments_missing_tool_round(&segments, projection.get("toolRounds")) {
        if status != "running" {
            return Err(integrity(
                "stored turn projection is missing live tool segments",
            ));
        }
        // The producer materializes the live prose prefix before absent tool
        // blocks. Preserve existing order/identities and count repeated call
        // IDs, which can recur across continued provider attempts.
        synchronize_terminal_blocks(
            &mut segments, &content, &thinking, actor, status, false,
        );
        let mut covered = std::collections::HashMap::<String, usize>::new();
        for segment in &segments {
            if segment.get("type").and_then(Value::as_str) == Some("tool_use") {
                if let Some(id) = segment.get("id").and_then(Value::as_str) {
                    *covered.entry(id.trim().to_owned()).or_default() += 1;
                }
            }
        }
        let mut seen = std::collections::HashMap::<String, usize>::new();
        if let Some(rounds) = projection.get("toolRounds").and_then(Value::as_array) {
            for (position, round) in rounds.iter().enumerate() {
                let Some(round) = round.as_object() else { continue; };
                if SYNTHETIC_INBOX_MARKERS.iter().any(|field| json_truthy(round.get(*field))) {
                    continue;
                }
                let id = round.get("toolCallId").and_then(Value::as_str)
                    .map(str::trim).unwrap_or("");
                if id.is_empty() { continue; }
                let occurrence = seen.entry(id.to_owned()).or_default();
                *occurrence += 1;
                if *occurrence > covered.get(id).copied().unwrap_or(0) {
                    let block = crate::turn::visible_tool_segment(round, position);
                    segments.push(block.as_object().unwrap().clone());
                    *covered.entry(id.to_owned()).or_default() += 1;
                }
            }
        }
    }
    synchronize_terminal_blocks(
        &mut segments,
        &content,
        &thinking,
        actor,
        status,
        true,
    );
    let segments = drop_narrated_live_terminal_mirror(segments, &content, status);
    projection.insert(
        "segments".to_owned(),
        Value::Array(stable_block_ids(&segments)),
    );
    Ok(projection)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn empty_live_projection() -> Map<String, Value> {
        json!({"content": "", "thinking": "", "segments": [], "toolRounds": []})
            .as_object()
            .unwrap()
            .clone()
    }

    #[test]
    fn empty_live_projection_grows_the_terminal_seed() {
        let stabilized =
            stabilize_for_patch(&empty_live_projection(), "assistant", "pending").unwrap();
        assert_eq!(
            stabilized["segments"],
            json!([{
                "type": "text",
                "blockId": "text:terminal",
                "text": "",
                "deliverable": true,
                "terminal": true,
            }])
        );
    }

    #[test]
    fn terminal_seed_is_idempotent() {
        let once =
            stabilize_for_patch(&empty_live_projection(), "assistant", "running").unwrap();
        let twice = stabilize_for_patch(&once, "assistant", "running").unwrap();
        assert_eq!(once, twice);
    }

    #[test]
    fn terminal_blocks_track_content_and_thinking() {
        let projection = json!({
            "content": "answer",
            "thinking": "reason",
            "segments": [],
            "toolRounds": [],
        })
        .as_object()
        .unwrap()
        .clone();
        let stabilized = stabilize_for_patch(&projection, "assistant", "running").unwrap();
        assert_eq!(
            stabilized["segments"],
            json!([
                {
                    "type": "thinking",
                    "blockId": "thinking:terminal",
                    "text": "reason",
                    "deliverable": false,
                    "terminal": true,
                },
                {
                    "type": "text",
                    "blockId": "text:terminal",
                    "text": "answer",
                    "deliverable": true,
                    "terminal": true,
                },
            ])
        );
    }

    #[test]
    fn human_turns_get_no_placeholder_seed() {
        let stabilized =
            stabilize_for_patch(&empty_live_projection(), "human", "completed").unwrap();
        assert_eq!(stabilized["segments"], json!([]));
    }

    #[test]
    fn uncovered_terminal_tool_rounds_fail_closed() {
        let projection = json!({
            "content": "",
            "thinking": "",
            "segments": [],
            "toolRounds": [{"toolCallId": "call-1"}],
        })
        .as_object()
        .unwrap()
        .clone();
        let error = stabilize_for_patch(&projection, "assistant", "completed").unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }

    #[test]
    fn continued_live_rounds_repair_prefix_and_duplicate_ids_idempotently() {
        let projection = json!({
            "content": "prefix", "thinking": "reasoning",
            "segments": [{"type":"system_note", "blockId":"note", "text":"keep"},
                {"type":"tool_use", "id":"same", "blockId":"original", "result":{"content":"first"}}],
            "toolRounds": [
                {"toolCallId":"same", "toolContent":"first"},
                {"toolCallId":"same", "toolName":"read", "toolArgs":"{}", "toolContent":"second", "status":"completed", "attemptId":"resumed", "llmRound":2},
                {"toolCallId":"synthetic", "_inboxInject":true},
                {"toolCallId":"third", "toolContent":"third"}
            ]
        }).as_object().unwrap().clone();
        let normalized = stabilize_for_patch(&projection, "assistant", "running").unwrap();
        let blocks = normalized["segments"].as_array().unwrap();
        assert_eq!(blocks[0]["blockId"], "note");
        assert_eq!(blocks[1]["blockId"], "original");
        let tools: Vec<_> = blocks.iter().filter(|v| v["type"] == "tool_use").collect();
        assert_eq!(tools.len(), 3);
        assert_eq!(tools[1]["result"]["content"], "second");
        assert_eq!(tools[1]["attemptId"], "resumed");
        let prefix = blocks.iter().position(|v| v["text"] == "prefix").unwrap();
        let appended = blocks.iter().position(|v| v["id"] == "same" && v["blockId"] != "original").unwrap();
        assert!(prefix < appended);
        assert_eq!(normalized["toolRounds"], projection["toolRounds"]);
        assert_eq!(stabilize_for_patch(&normalized, "assistant", "running").unwrap(), normalized);
    }

    #[test]
    fn covered_tool_rounds_pass_through() {
        let projection = json!({
            "content": "",
            "thinking": "",
            "segments": [{
                "type": "tool_use",
                "id": "call-1",
                "blockId": "tool:call-1",
                "result": {"content": "ok", "status": "completed"},
            }],
            "toolRounds": [{"toolCallId": "call-1"}],
        })
        .as_object()
        .unwrap()
        .clone();
        let stabilized = stabilize_for_patch(&projection, "assistant", "running").unwrap();
        let segments = stabilized["segments"].as_array().unwrap();
        assert_eq!(segments.len(), 2);
        assert_eq!(segments[0]["blockId"], "tool:call-1");
        assert_eq!(segments[1]["blockId"], "text:terminal");
    }

    #[test]
    fn duplicate_terminal_mirror_is_dropped_while_narrated() {
        let projection = json!({
            "content": "same prose",
            "thinking": "",
            "segments": [
                {"type": "text", "blockId": "text:1", "text": "same prose"},
                {
                    "type": "text",
                    "blockId": "text:terminal",
                    "text": "same prose",
                    "deliverable": true,
                    "terminal": true,
                },
            ],
            "toolRounds": [],
        })
        .as_object()
        .unwrap()
        .clone();
        let stabilized = stabilize_for_patch(&projection, "assistant", "running").unwrap();
        assert_eq!(
            stabilized["segments"],
            json!([{"type": "text", "blockId": "text:1", "text": "same prose"}])
        );
    }

    #[test]
    fn resumable_status_marks_the_terminal_block() {
        let stabilized =
            stabilize_for_patch(&empty_live_projection(), "assistant", "interrupted")
                .unwrap();
        assert_eq!(stabilized["segments"][0]["resumable"], json!(true));
    }
}
