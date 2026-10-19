//! Durable projection facts for the existing single-event patch chain.
//! Minted only from materialized authority state; a supported delta can update
//! these facts without decoding the unchanged checkpoint. Missing/old evidence,
//! normalization dependencies and checkpoint boundaries use ordinary Turn code.

use super::super::*;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct FieldWitness {
    bytes: usize,
    depth: usize,
    string: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(in crate::turn) struct ProjectionWitness {
    version: u64,
    revision: u64,
    bytes: usize,
    fields: BTreeMap<String, FieldWitness>,
    stable_segments: bool,
    actor: String,
    status: String,
    guidance_repair_free: bool,
}

fn size(value: &impl Serialize) -> io::Result<usize> {
    serde_json::to_vec(value)
        .map(|v| v.len())
        .map_err(|_| invalid_data("Turn frame evidence cannot be encoded"))
}

impl FieldWitness {
    fn from_value(value: &Value) -> io::Result<Option<Self>> {
        let Some(depth) = crate::response_json::canonical_value_depth(value, 0)
            .filter(|depth| *depth + 4 <= crate::response_json::MAX_VALIDATED_CONTAINER_DEPTH)
        else {
            return Ok(None);
        };
        Ok(Some(Self {
            bytes: size(value)?,
            depth,
            string: value.is_string(),
        }))
    }
}

impl ProjectionWitness {
    pub(in crate::turn) fn mint(
        projection: &Map<String, Value>,
        revision: u64,
        actor: &str,
        status: &str,
    ) -> io::Result<Option<Self>> {
        let mut fields = BTreeMap::new();
        for (key, value) in projection {
            let Some(field) = FieldWitness::from_value(value)? else {
                return Ok(None);
            };
            fields.insert(key.clone(), field);
        }
        let mut witness = Self {
            version: 1,
            revision,
            bytes: 0,
            fields,
            stable_segments: false,
            actor: actor.to_owned(),
            status: status.to_owned(),
            guidance_repair_free: false,
        };
        witness.bytes = witness.measured_bytes()?;
        if !witness.bounded()? {
            return Ok(None);
        }
        // The shared helpers, not a caller's flag or field type, establish
        // that these semantic repairs have nothing left to do.
        witness.stable_segments =
            crate::turn_projection_segments::stabilize_for_patch(projection, actor, status)
                .is_ok_and(|stable| stable == *projection);
        witness.guidance_repair_free =
            super::require_guidance_repair_free_projection(projection).is_ok();
        Ok(Some(witness))
    }

    fn measured_bytes(&self) -> io::Result<usize> {
        let mut total = 2_usize;
        for (index, (key, field)) in self.fields.iter().enumerate() {
            total = total
                .checked_add(size(key)? + 1 + usize::from(index > 0))
                .and_then(|n| n.checked_add(field.bytes))
                .ok_or_else(|| invalid_data("Turn frame byte witness overflows"))?;
        }
        Ok(total)
    }

    fn bounded(&self) -> io::Result<bool> {
        Ok(
            self.bytes <= crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES
                && size(self)? <= MAX_TURN_PROJECTION_PATCH_BYTES
                && self.fields.keys().next().is_none_or(|key| {
                    !matches!(
                        key.as_str(),
                        "$serde_json::private::RawValue" | "$serde_json::private::Number"
                    )
                }),
        )
    }

    pub(in crate::turn) fn decode(raw: &Value, revision: u64) -> io::Result<Option<Self>> {
        // Unknown evidence versions remain a reconstructible optimization miss.
        if raw.get("version").and_then(Value::as_u64) != Some(1) {
            return Ok(None);
        }
        let witness: Self = serde_json::from_value(raw.clone())
            .map_err(|_| invalid_data("Turn frame evidence is malformed"))?;
        if witness.revision != revision
            || !witness.bounded()?
            || witness.measured_bytes()? != witness.bytes
            || witness.fields.values().any(|field| {
                field.bytes == 0
                    || field.depth.saturating_add(4)
                        > crate::response_json::MAX_VALIDATED_CONTAINER_DEPTH
                    || (field.string && (field.bytes < 2 || field.depth != 0))
            })
        {
            return Err(invalid_data("Turn frame evidence fence or shape differs"));
        }
        Ok(Some(witness))
    }

    pub(in crate::turn) fn verify(
        &self,
        projection: &Map<String, Value>,
        actor: &str,
        status: &str,
    ) -> io::Result<()> {
        if projection.len() != self.fields.len() {
            return Err(invalid_data("Turn frame fields differ"));
        }
        for (key, value) in projection {
            if FieldWitness::from_value(value)?.as_ref() != self.fields.get(key) {
                return Err(invalid_data("Turn frame materialized field differs"));
            }
        }
        if self.guidance_repair_free
            && super::require_guidance_repair_free_projection(projection).is_err()
        {
            return Err(invalid_data("Turn frame guidance proof differs"));
        }
        if self.stable_segments
            && self.actor == actor
            && self.status == status
            && !crate::turn_projection_segments::stabilize_for_patch(projection, actor, status)
                .is_ok_and(|stable| stable == *projection)
        {
            return Err(invalid_data("Turn frame segment proof differs"));
        }
        Ok(())
    }
}

pub(in crate::turn) fn classify_patch(request: &EventRecordRequest) -> bool {
    if request.terminal
        || request.slim
        || request.status != "running"
        || request.background_command_ack.is_some()
        || !request.settlement.is_empty()
        || !request.error.is_empty()
        || request.event_type != "projection_updated"
    {
        return false;
    }
    request
        .projection_patch
        .as_ref()
        .and_then(|p| p.get("operations"))
        .and_then(Value::as_array)
        .is_some_and(|operations| {
            operations.iter().all(|operation| {
                let Some(path) = operation.get("path").and_then(Value::as_array) else {
                    return false;
                };
                path.len() == 1
                    && path[0].is_string()
                    && path[0].as_str() != Some("toolRounds")
                    && matches!(
                        operation.get("op").and_then(Value::as_str),
                        Some("set" | "remove" | "append_text")
                    )
            })
        })
}

pub(in crate::turn) struct WitnessUpdate {
    pub head: ProjectionHeadDescriptor,
    pub projection_bytes: usize,
}

pub(in crate::turn) fn append_from_witness(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    loaded: &LoadedTurnForAttempt,
    request: &EventRecordRequest,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<WitnessUpdate>> {
    if !classify_patch(request) {
        return Ok(None);
    }
    let Some(head) = &loaded.projection_head else {
        return Ok(None);
    };
    let Some(mut witness) = head.frame_witness.clone() else {
        return Ok(None);
    };
    if head.text_tail.is_some()
        || !witness.guidance_repair_free
        || (request.projection_segments_stable
            && (!witness.stable_segments
                || loaded
                    .document
                    .get("actor")
                    .and_then(Value::as_str)
                    .unwrap_or("assistant")
                    != witness.actor
                || loaded
                    .document
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("running")
                    != witness.status))
    {
        return Ok(None);
    }
    let patch = request.projection_patch.as_ref().unwrap();
    let revision = loaded.document["projectionRevision"]
        .as_u64()
        .ok_or_else(|| invalid_data("Turn frame revision is malformed"))?;
    if patch.get("baseRevision").and_then(Value::as_u64) != Some(revision) {
        return Err(typed_conflict(TurnConflictKind::ProjectionStale));
    }
    if patch.get("targetRevision").and_then(Value::as_u64) != revision.checked_add(1) {
        return Err(invalid_input(
            "Turn projection patch must advance exactly one revision",
        ));
    }
    let operations = crate::turn_projection_patch::checked_projection_patch_operations(patch)
        .map_err(|_| invalid_input("Turn projection patch is invalid"))?;
    for operation in operations {
        let key = operation["path"][0].as_str().unwrap();
        let normalization_dependency = matches!(
            key,
            "segments"
                | "content"
                | "thinking"
                | "toolRounds"
                | "fileChanges"
                | "modifiedFileList"
                | "modifiedFiles"
        );
        // A stable-segment request normalizes its *old* state before applying
        // its delta. A changed dependency invalidates the proof for next time.
        if normalization_dependency {
            witness.stable_segments = false;
        }
        match operation["op"].as_str().unwrap() {
            "set" => {
                let Some(field) =
                    FieldWitness::from_value(operation.get("value").unwrap_or(&Value::Null))?
                else {
                    return Ok(None);
                };
                witness.fields.insert(key.to_owned(), field);
            }
            "remove" => {
                witness.fields.remove(key);
            }
            "append_text" => {
                let Some(suffix) = operation.get("value").and_then(Value::as_str) else {
                    return Ok(None);
                };
                let Some(field) = witness.fields.get_mut(key).filter(|field| field.string) else {
                    return Ok(None);
                };
                field.bytes = field
                    .bytes
                    .checked_add(size(&suffix)?.saturating_sub(2))
                    .ok_or_else(|| invalid_data("Turn frame text bytes overflow"))?;
            }
            _ => return Ok(None),
        }
    }
    witness.revision = revision
        .checked_add(1)
        .ok_or_else(|| invalid_data("Turn frame revision overflows"))?;
    witness.bytes = witness.measured_bytes()?;
    if !witness.bounded()? {
        return Ok(None);
    }
    // Prove that the ordinary size validator cannot enter its materialized
    // recovery inspection. The compact Turn envelope may itself be large;
    // no fixed envelope estimate is sufficient.
    let mut envelope = loaded.document.clone();
    envelope.remove("projection");
    envelope.remove("_projectionHead");
    envelope.insert("projectionRevision".into(), json!(witness.revision));
    envelope.insert("status".into(), json!(request.status));
    envelope.insert("settlement".into(), json!(request.settlement));
    envelope.insert("updatedAt".into(), json!(request.now_ms));
    let reserve = MAX_LEGACY_DOCUMENT_BYTES + MAX_TIMING_TRACE_PERSISTED_BYTES + 1024;
    if witness
        .bytes
        .saturating_mul(2)
        .saturating_add(reserve)
        .saturating_add(size(&envelope)?)
        .saturating_add(b",\"projection\":".len())
        > crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES
    {
        return Ok(None);
    }
    let next_count = head.patch_count + 1;
    let next_bytes = head
        .patch_bytes
        .checked_add(size(patch)?)
        .ok_or_else(|| invalid_data("Turn frame patch bytes overflow"))?;
    if next_count > MAX_TURN_PROJECTION_HEAD_PATCHES || next_bytes > MAX_TURN_PROJECTION_PATCH_BYTES
    {
        return Ok(None);
    }
    let mut next = head.clone();
    next.patch_count = next_count;
    next.patch_bytes = next_bytes;
    let projection_bytes = witness.bytes;
    next.frame_witness = Some(witness);
    store_projection_head_patch(
        database,
        transaction,
        conversation_id,
        turn_id,
        &next,
        patch,
        request.now_ms,
    )?;
    Ok(Some(WitnessUpdate {
        head: next,
        projection_bytes,
    }))
}
