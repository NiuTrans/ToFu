//! Trusted completed-round recovery over explicit owner/task/binding authority.
//! The host atomically fences each attempt before effects and commits completed
//! state after joined tools/accounting. Inflight frontiers are never loadable.
//! Account/deadline recovery belongs to the host; this port grants no new credit.
use crate::{
    agent::Budget,
    progress::{Ledger, Limits},
};
use serde_json::{json, Value};

#[derive(Clone, Copy)]
pub struct Identity<'a> {
    pub owner: u64,
    pub task_id: &'a str,
    pub epoch: &'a str,
    pub binding: &'a str,
    pub session: &'a crate::session::ExecutionSession,
}
#[derive(Clone, Copy)]
pub struct View<'a> {
    pub identity: Identity<'a>,
    pub next_round: usize,
    pub tool_calls: usize,
    pub messages: &'a [Value],
    pub usage: &'a [Value],
    pub progress: &'a Ledger,
    pub budget: Budget,
    pub limits: Limits,
    pub model_account: Option<&'a Value>,
    pub deadline_unix_ms: Option<u64>,
}
/// The only caller allowed to supply recovery data is trusted composition.
/// load must acquire an exclusive recovery lease, reject inflight/terminal
/// frontiers and restore financial/time authority before returning state.
/// begin must durably CAS the frontier to inflight before returning success.
/// complete must CAS that same attempt to completed, never overwrite a newer
/// attempt. All callbacks must join their work before returning or unwinding.
pub enum Disposition {
    Continue,
    Suspend,
}
pub trait Port {
    fn binding(&self) -> &str;
    fn load(&self, identity: Identity<'_>) -> Result<Option<Value>, Value>;
    fn begin(&self, identity: Identity<'_>, round: usize) -> Result<(), Value>;
    fn complete(&self, state: View<'_>) -> Result<Disposition, Value>;
}
impl View<'_> {
    /// Explicit capture for a caller that needs owned storage state. The native
    /// driver itself borrows history and never clones it on the disabled path.
    pub fn capture(&self) -> Value {
        let mut state = json!({"format":"tofu.native-agent-checkpoint/v1", "owner_user_id":self.identity.owner,
            "task_id":self.identity.task_id,"epoch":self.identity.epoch,"binding":self.identity.binding,
            "next_round":self.next_round,"tool_calls":self.tool_calls,"messages":self.messages,"usage":self.usage,
            "round_limit":self.budget.rounds,"tool_limit":self.budget.tool_calls,
            "no_progress_limit":self.limits.no_progress_rounds,"failure_limit":self.limits.nonretryable_failure_rounds,
            "progress":self.progress.snapshot()});
        if let Some(account) = self.model_account {
            state["model_account"] = account.clone();
        }
        if let Some(deadline) = self.deadline_unix_ms {
            state["deadline_unix_ms"] = json!(deadline);
        }
        state
    }
}
impl serde_core::Serialize for View<'_> {
    fn serialize<S: serde_core::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde_core::ser::SerializeMap;
        let mut map =
            serializer.serialize_map(Some(14 + usize::from(self.model_account.is_some()) + usize::from(self.deadline_unix_ms.is_some())))?;
        map.serialize_entry("format", "tofu.native-agent-checkpoint/v1")?;
        map.serialize_entry("owner_user_id", &self.identity.owner)?;
        map.serialize_entry("task_id", self.identity.task_id)?;
        map.serialize_entry("epoch", self.identity.epoch)?;
        map.serialize_entry("binding", self.identity.binding)?;
        map.serialize_entry("next_round", &self.next_round)?;
        map.serialize_entry("tool_calls", &self.tool_calls)?;
        map.serialize_entry("messages", self.messages)?;
        map.serialize_entry("usage", self.usage)?;
        map.serialize_entry("round_limit", &self.budget.rounds)?;
        map.serialize_entry("tool_limit", &self.budget.tool_calls)?;
        map.serialize_entry("no_progress_limit", &self.limits.no_progress_rounds)?;
        map.serialize_entry("failure_limit", &self.limits.nonretryable_failure_rounds)?;
        map.serialize_entry("progress", &self.progress.snapshot())?;
        if let Some(account) = self.model_account {
            map.serialize_entry("model_account", account)?;
        }
        if let Some(deadline) = self.deadline_unix_ms {
            map.serialize_entry("deadline_unix_ms", &deadline)?;
        }
        map.end()
    }
}
pub(crate) struct Restored {
    pub next_round: usize,
    pub tool_calls: usize,
    pub messages: Vec<Value>,
    pub usage: Vec<Value>,
    pub progress: Ledger,
}
// Checkpoint schema vocabulary only. Tool argument admission deliberately
// treats required null as missing and cannot validate persisted digest nulls.
pub(crate) fn valid(schema: &Value, value: &Value) -> bool {
    let supported = [
        "$schema",
        "$id",
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "items",
    ];
    if !schema
        .as_object()
        .is_some_and(|node| node.keys().all(|key| supported.contains(&key.as_str())))
    {
        return false;
    }
    let matches = |kind: &str| match kind {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "integer" => value.as_u64().is_some(),
        "number" => value.as_f64().is_some_and(f64::is_finite),
        "null" => value.is_null(),
        "boolean" => value.is_boolean(),
        _ => false,
    };
    let typed = match &schema["type"] {
        Value::String(kind) => matches(kind),
        Value::Array(kinds) => kinds.iter().any(|kind| kind.as_str().is_some_and(matches)),
        Value::Null => true,
        _ => false,
    };
    if !typed {
        return false;
    }
    if let Some(options) = schema["enum"].as_array() {
        if !options.contains(value) {
            return false;
        }
    }
    if let Some(number) = value.as_u64() {
        if schema["minimum"]
            .as_u64()
            .is_some_and(|minimum| number < minimum)
            || schema["maximum"]
                .as_u64()
                .is_some_and(|maximum| number > maximum)
        {
            return false;
        }
    }
    if value.as_u64().is_none() {
        if let Some(number) = value.as_f64() {
            if schema["minimum"].as_f64().is_some_and(|minimum| number < minimum)
                || schema["maximum"].as_f64().is_some_and(|maximum| number > maximum) {
                return false;
            }
        }
    }
    if let Some(text) = value.as_str() {
        let length = text.chars().count() as u64;
        if schema["minLength"]
            .as_u64()
            .is_some_and(|minimum| length < minimum)
            || schema["maxLength"]
                .as_u64()
                .is_some_and(|maximum| length > maximum)
        {
            return false;
        }
    }
    if let Some(array) = value.as_array() {
        if schema["minItems"]
            .as_u64()
            .is_some_and(|minimum| (array.len() as u64) < minimum)
        {
            return false;
        }
        if schema["items"].is_object() && array.iter().any(|v| !valid(&schema["items"], v)) {
            return false;
        }
    }
    if let Some(object) = value.as_object() {
        if let Some(required) = schema["required"].as_array() {
            if required
                .iter()
                .any(|key| !key.as_str().is_some_and(|key| object.contains_key(key)))
            {
                return false;
            }
        }
        if let Some(properties) = schema["properties"].as_object() {
            if schema["additionalProperties"] == false
                && object.keys().any(|key| !properties.contains_key(key))
            {
                return false;
            }
            if properties
                .iter()
                .any(|(key, node)| object.get(key).is_some_and(|value| !valid(node, value)))
            {
                return false;
            }
        }
    }
    true
}

fn refused() -> Value {
    json!({"kind":"agent_checkpoint","message":"native recovery frontier refused"})
}
pub(crate) fn restore(
    mut value: Value,
    identity: Identity<'_>,
    budget: Budget,
    limits: Limits,
) -> Result<Restored, Value> {
    let schema: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_agent_checkpoint_v1.schema.json"
    ))
    .map_err(|_| refused())?;
    if !valid(&schema, &value) {
        return Err(refused());
    }
    if value["owner_user_id"].as_u64() != Some(identity.owner)
        || value["task_id"] != identity.task_id
        || value["epoch"] != identity.epoch
        || value["binding"] != identity.binding
        || value["round_limit"].as_u64() != Some(budget.rounds as u64)
        || value["tool_limit"].as_u64() != Some(budget.tool_calls as u64)
        || value["no_progress_limit"].as_u64() != Some(limits.no_progress_rounds as u64)
        || value["failure_limit"].as_u64() != Some(limits.nonretryable_failure_rounds as u64)
    {
        return Err(refused());
    }
    let count = |key: &str| {
        usize::try_from(value[key].as_u64().ok_or_else(refused)?).map_err(|_| refused())
    };
    let next_round = count("next_round")?;
    let tool_calls = count("tool_calls")?;
    if next_round == 0
        || next_round >= budget.rounds
        || tool_calls < next_round
        || tool_calls > budget.tool_calls
    {
        return Err(refused());
    }
    let progress =
        Ledger::restore(&value["progress"], budget.feedback_bytes, limits).ok_or_else(refused)?;
    let object = value.as_object_mut().unwrap();
    let take = |value: Value| match value {
        Value::Array(values) => Ok(values),
        _ => Err(refused()),
    };
    let messages = take(object.remove("messages").unwrap())?;
    let usage = take(object.remove("usage").unwrap())?;
    if messages.iter().any(|v| !v.is_object())
        || usage.len() != next_round
        || messages.last().and_then(|v| v["role"].as_str()) != Some("tool")
    {
        return Err(refused());
    }
    Ok(Restored {
        next_round,
        tool_calls,
        messages,
        usage,
        progress,
    })
}

#[cfg(test)]
mod deadline_schema_tests {
    use super::*;
    #[test]
    fn shared_deadline_accepts_fractional_seconds_and_rejects_invalid_bounds() {
        let schema = json!({"type":["number","null"],"minimum":0,"maximum":2000000001});
        assert!(valid(&schema, &json!(2000000000.125)));
        assert!(valid(&schema, &Value::Null));
        assert!(!valid(&schema, &json!(-0.125)));
        assert!(!valid(&schema, &json!(2000000001.125)));
        assert!(!valid(&schema, &json!(true)));
        assert!(!valid(&schema, &json!("2000000000.125")));
    }

    #[test]
    fn checkpoint_timestamp_bounds_preserve_exact_unsigned_integers() {
        let schema = json!({"type":"integer","minimum":0,"maximum":18446744073709551615u64});
        assert!(valid(&schema, &json!(0)));
        assert!(valid(&schema, &json!(u64::MAX)));
        assert!(!valid(&schema, &json!(-1)));
        assert!(!valid(&schema, &json!(true)));
        assert!(!valid(&schema, &json!(1.0)));
        assert!(!valid(&json!({"type":"integer","maximum":100}), &json!(101)));
    }
}
