//! Search-only extraction from already admitted source documents.
//! Preserves public text fields and source roles; never stringifies tool/image
//! objects. Chunks overlap by the maximum query width from the shared contract.

use crate::generated_conversation_search::{CHUNK_CHARACTERS, CHUNK_OVERLAP_CHARACTERS};
use crate::turn_search_projection::TurnSearchDocument;
use serde_json::{Map, Value};
use std::borrow::Cow;

fn field_text(value: &Value) -> Cow<'_, str> {
    match value {
        Value::String(text) => Cow::Borrowed(text),
        Value::Array(parts) => Cow::Owned(
            parts
                .iter()
                .filter_map(|part| {
                    (part.get("type").and_then(Value::as_str) == Some("text"))
                        .then(|| part.get("text").and_then(Value::as_str))
                        .flatten()
                })
                .collect(),
        ),
        _ => Cow::Borrowed(""),
    }
}

pub(crate) fn chunks(
    turn_id: &str,
    ordinal: u64,
    actor: &str,
    projection: &Map<String, Value>,
    output: &mut Vec<TurnSearchDocument>,
) {
    let role = projection
        .get("role")
        .and_then(Value::as_str)
        .filter(|role| matches!(*role, "user" | "assistant"))
        .unwrap_or(
            if matches!(actor, "human" | "critic" | "virtual_user" | "user") {
                "user"
            } else {
                "assistant"
            },
        );
    collect_chunks(turn_id, ordinal, role, projection, output);
}

/// Settled Turn roles follow the authoritative actor, as the sidecar's
/// turn_snapshot/legacy-message projection does; archive messages keep role.
pub(crate) fn chunks_for_turn(
    turn_id: &str,
    ordinal: u64,
    actor: &str,
    projection: &Map<String, Value>,
    output: &mut Vec<TurnSearchDocument>,
) {
    let role = if matches!(actor, "human" | "critic" | "virtual_user" | "user") {
        "user"
    } else {
        "assistant"
    };
    collect_chunks(turn_id, ordinal, role, projection, output);
}

fn collect_chunks(
    turn_id: &str,
    ordinal: u64,
    role: &str,
    projection: &Map<String, Value>,
    output: &mut Vec<TurnSearchDocument>,
) {
    let mut chunk_index = 0;
    for field in [
        "originalContent",
        "content",
        "translatedContent",
        "thinking",
    ] {
        let Some(value) = projection.get(field) else {
            continue;
        };
        let text = field_text(value);
        let mut start = 0;
        while start < text.len() {
            let remaining = &text[start..];
            let end = remaining
                .char_indices()
                .nth(CHUNK_CHARACTERS)
                .map_or(remaining.len(), |(byte, _)| byte);
            output.push(TurnSearchDocument {
                turn_id: turn_id.to_owned(),
                ordinal,
                chunk_index,
                role: role.to_owned(),
                match_field: field.to_owned(),
                search_text: remaining[..end].to_owned(),
            });
            chunk_index += 1;
            if end == remaining.len() {
                break;
            }
            start += remaining
                .char_indices()
                .nth(CHUNK_CHARACTERS - CHUNK_OVERLAP_CHARACTERS)
                .unwrap()
                .0;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn chunks_preserve_tail_overlap_roles_and_only_public_text_parts() {
        let value = json!({"role":"user", "originalContent":format!("{}tail", "界".repeat(12_000)),
            "content":[{"type":"image","text":"private-image"}, {"type":"text","text":"typed"}, "not-a-part"],
            "thinking":"reason", "translatedContent":"translation"});
        let mut documents = Vec::new();
        chunks(
            "turn",
            4,
            "assistant",
            value.as_object().unwrap(),
            &mut documents,
        );
        assert!(documents
            .iter()
            .all(|d| d.role == "user" && d.search_text.chars().count() <= CHUNK_CHARACTERS));
        assert!(documents.iter().any(|d| d.search_text.ends_with("tail")));
        assert_eq!(
            documents[0]
                .search_text
                .chars()
                .rev()
                .take(CHUNK_OVERLAP_CHARACTERS)
                .collect::<String>(),
            documents[1]
                .search_text
                .chars()
                .take(CHUNK_OVERLAP_CHARACTERS)
                .collect::<String>()
        );
        let content = documents
            .iter()
            .find(|d| d.match_field == "content")
            .unwrap();
        assert_eq!(content.search_text, "typed");
        assert_eq!(documents.last().unwrap().match_field, "thinking");
    }
}
