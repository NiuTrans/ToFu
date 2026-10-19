//! Frozen original actor quotes at aggregate journal retirement.
//! Canonical checkpoint/private-fee schemas own data shapes. These checks only
//! validate immutable usage/credits and whole-cut coverage, never reprice or grant
//! account, wallet or successor authority. The application/store owns lineage.
use super::{Error, SNAPSHOT_SCHEMA};
use super::wallet::{add, capsule_valid, number, quote_parts, COMPONENTS};
use serde_json::{json, Value};
use std::{collections::BTreeMap, sync::LazyLock};

static COST_SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    let mut schema: Value = serde_json::from_str(include_str!(
        "../../../contracts/native_aggregate_cost_v1.schema.json")).expect("aggregate cost contract");
    schema.as_object_mut().unwrap().remove("description");
    schema["properties"]["format"] = json!({"enum":[schema["$id"]]});
    schema
});

static FRONTIER_SCHEMA: LazyLock<Value> = LazyLock::new(|| serde_json::from_str(
    include_str!("../../../contracts/execution_account_frontier_v1.schema.json")).expect("account frontier contract"));
static FRONTIER_ENVELOPE: LazyLock<Value> = LazyLock::new(|| {
    let mut schema = FRONTIER_SCHEMA.clone();
    // The sole conditional requires actor_rebinding exactly for actor_rebound;
    // settled_cut_shape checks that relationship explicitly.
    schema.as_object_mut().expect("frontier schema object").remove("allOf");
    schema["properties"]["format"] = json!({"enum":[schema["properties"]["format"]["const"]]});
    // Dynamic names/rows are checked separately against the same schema below.
    schema["properties"]["actors"] = json!({"type":"object"});
    schema["properties"]["recovery"]["properties"]["pending_actors"] = json!({"type":"object"});
    schema
});

pub(super) fn settled_cut_shape(cut: &Value) -> Result<(), Error> {
    if !crate::agent_checkpoint::valid(&FRONTIER_ENVELOPE, cut) { return Err(Error::Invalid); }
    let snapshot = &cut["snapshot"];
    // Python's budget_snapshot owner exports this closed evidence envelope.
    let fields = ["format","owner_user_id","root_task_id","limits","complete","settled","usage_known",
        "tokens","root_tokens","tool_calls","model_ordinal","tool_ordinal"];
    if snapshot.as_object().is_none_or(|object| object.len() != fields.len()
        || fields.iter().any(|field| !object.contains_key(*field)))
        || ["complete","settled","usage_known"].iter().any(|key| !snapshot[*key].is_boolean())
        || ["tokens","root_tokens","tool_calls","model_ordinal","tool_ordinal"].iter().any(|key| snapshot[*key].as_u64().is_none())
        || !crate::agent_checkpoint::valid(&SNAPSHOT_SCHEMA["properties"]["shared_model_account"]["properties"]["limits"], &snapshot["limits"]) {
        return Err(Error::Invalid);
    }
    let root = snapshot["root_task_id"].as_str().ok_or(Error::Invalid)?;
    let actors = cut["actors"].as_object().ok_or(Error::Invalid)?;
    for (identity, actor) in actors {
        if !crate::agent_checkpoint::valid(&FRONTIER_SCHEMA["properties"]["actors"]["propertyNames"], &json!(identity))
            || !crate::agent_checkpoint::valid(&FRONTIER_SCHEMA["$defs"]["actor"], actor)
            || ["model_ordinal","tool_ordinal"].iter().any(|key| actor[*key].as_u64() > snapshot[*key].as_u64()) {
            return Err(Error::Invalid);
        }
    }
    if cut.get("recovery").is_some_and(|recovery| recovery["source_root_task_id"] == root)
        || (cut["reason"] == "actor_rebound") != cut.get("actor_rebinding").is_some() {
        return Err(Error::Invalid);
    }
    if let Some(binding) = cut.get("actor_rebinding") {
        if cut.get("recovery").is_none() || binding["target_task_id"] == root
            || !actors.contains_key(binding["target_task_id"].as_str().ok_or(Error::Invalid)?) { return Err(Error::Invalid); }
    }
    Ok(())
}

static ACTOR_SCHEMA: LazyLock<Value> = LazyLock::new(|| {
    let mut schema = SNAPSHOT_SCHEMA["$defs"]["registered_actor_fees"].clone();
    let format = schema["properties"]["format"]["const"].clone();
    schema["properties"]["format"] = json!({"enum":[format]});
    schema["properties"]["rounds"]["items"]["properties"]["quote"] =
        SNAPSHOT_SCHEMA["properties"]["host_fee_projection"]["properties"]["rounds"]["items"]["properties"]["cost"].clone();
    schema
});

pub fn validate_actor_fee_evidence(fees: &Value, owner: u64, root: &str, actor: &str) -> Result<(), Error> {
    if owner == 0 || actor == root || !crate::agent_checkpoint::valid(&ACTOR_SCHEMA, fees)
        || fees["owner_user_id"].as_u64() != Some(owner) || fees["root_task_id"] != root
        || fees["actor_task_id"] != actor { return Err(Error::Invalid); }
    let mut tokens = 0_u64; let mut current = false;
    for (index, row) in fees["rounds"].as_array().ok_or(Error::Invalid)?.iter().enumerate() {
        let producer = row["producer_task_id"].as_str().ok_or(Error::Invalid)?;
        let payer = row["payer_task_id"].as_str().ok_or(Error::Invalid)?;
        if row["round"].as_u64() != Some(index as u64 + 1) || producer == root || payer == producer || payer == actor {
            return Err(Error::Invalid);
        }
        if producer == actor {
            current = true;
            if payer != root { return Err(Error::Invalid); }
        } else if current || payer == root { return Err(Error::Invalid); }
        let (_, consumed) = quote_parts(&row["quote"])?;
        tokens = tokens.checked_add(consumed).ok_or(Error::Invalid)?;
    }
    if fees["tokens"].as_u64() != Some(tokens) { return Err(Error::Invalid); }
    Ok(())
}

pub fn actor_fee_evidence_from_checkpoint(state: &Value, owner: u64, root: &str,
    actor: &str, tenant: &str) -> Result<Value, Error> {
    let fees = &state["host_fee_projection"]; let account = &state["shared_model_account"];
    if !crate::agent_checkpoint::valid(&SNAPSHOT_SCHEMA["properties"]["host_fee_projection"], fees)
        || !crate::agent_checkpoint::valid(&SNAPSHOT_SCHEMA["properties"]["shared_model_account"], account)
        || state["task_id"] != actor || state["owner_user_id"].as_u64() != Some(owner)
        || fees["task_id"] != actor || fees["owner_user_id"].as_u64() != Some(owner)
        || fees["tenant_id"] != tenant || fees["wallet_user_id"] != "" || fees["reserved_micro"] != 0
        || fees.get("wallet_settlement").is_some() || fees.get("child_rounds").is_some_and(|v| v.as_array().is_none_or(|v| !v.is_empty()))
        || account["owner_user_id"].as_u64() != Some(owner) || account["root_task_id"] != root
        || account["actor_task_id"] != actor || account["complete"] != true || account.get("root_tokens").is_some() {
        return Err(Error::Invalid);
    }
    let rows = fees["rounds"].as_array().ok_or(Error::Invalid)?;
    if state["next_round"].as_u64() != Some(rows.len() as u64)
        || state["usage"].as_array().map(|v| v.len()) != Some(rows.len()) { return Err(Error::Invalid); }
    let mut result = Vec::with_capacity(rows.len());
    for (row, usage) in rows.iter().zip(state["usage"].as_array().ok_or(Error::Invalid)?) {
        if row.get("public_round").is_some() { return Err(Error::Invalid); }
        let observed = crate::usage::observed_totals(usage).map_err(|_| Error::Invalid)?;
        if !observed.complete || observed.tokens != quote_parts(&row["cost"])?.1 { return Err(Error::Invalid); }
        let route = row.get("route").unwrap_or(fees);
        result.push(json!({"round":row["round"],"producer_task_id":row.get("task_id").unwrap_or(&fees["task_id"]),
            "payer_task_id":row["billable_task_id"],"logical_model":route["logical_model"],
            "provider_id":route["provider_id"],"quote":row["cost"]}));
    }
    let evidence = json!({"format":"tofu.native-actor-fees/v1","owner_user_id":owner,"root_task_id":root,
        "actor_task_id":actor,"tokens":account["tokens"],"rounds":result});
    validate_actor_fee_evidence(&evidence, owner, root, actor)?;
    Ok(evidence)
}

/// Compare a supplied capsule with the complete original quote union. The store
/// must bind these exact inputs to immutable registered records/native versions.
pub fn validate_aggregate_actor_invoice(cut: &Value, fees: &[Value], invoice: &Value) -> Result<(), Error> {
    settled_cut_shape(cut)?;
    if !(capsule_valid(invoice) || crate::agent_checkpoint::valid(&COST_SCHEMA, invoice))
        || cut["format"] != "tofu.execution-account-frontier/v1"
        || cut["version"].as_u64().is_none_or(|v| v == 0) { return Err(Error::Invalid); }
    let snapshot = &cut["snapshot"];
    let owner = invoice["owner_user_id"].as_u64().ok_or(Error::Invalid)?;
    let root = invoice["task_id"].as_str().ok_or(Error::Invalid)?;
    if snapshot["format"] != "tofu.execution-budget-snapshot/v1"
        || snapshot["owner_user_id"].as_u64() != Some(owner) || snapshot["root_task_id"] != root
        || snapshot["settled"] != true || snapshot["usage_known"] != true || snapshot["root_tokens"] != 0
        || cut["model_requests"].as_array().is_none_or(|rows| !rows.is_empty())
        || cut.get("recovery").is_some_and(|value| value["pending_actors"].as_object().is_none_or(|rows| !rows.is_empty())) {
        return Err(Error::Ineligible);
    }
    let mut actors = BTreeMap::new(); let mut rounds = BTreeMap::new(); let mut payers = BTreeMap::new();
    for evidence in fees {
        let actor = evidence["actor_task_id"].as_str().ok_or(Error::Invalid)?;
        validate_actor_fee_evidence(evidence, owner, evidence["root_task_id"].as_str().ok_or(Error::Invalid)?, actor)?;
        if actors.insert(actor, evidence).is_some() { return Err(Error::Invalid); }
        for row in evidence["rounds"].as_array().ok_or(Error::Invalid)? {
            let producer = row["producer_task_id"].as_str().ok_or(Error::Invalid)?;
            let payer = row["payer_task_id"].as_str().ok_or(Error::Invalid)?;
            if payers.insert(producer, payer).is_some_and(|previous| previous != payer) { return Err(Error::Invalid); }
            let identity = (producer, row["round"].as_u64().ok_or(Error::Invalid)?);
            if rounds.insert(identity, row).is_some_and(|previous| previous != row) { return Err(Error::Invalid); }
        }
    }
    let live = cut["actors"].as_object().ok_or(Error::Invalid)?;
    if live.get(root).is_none_or(|value| value["parent_task_id"] != "" || value["tokens"] != 0)
        || (snapshot["complete"] == true && live.len() != 1) { return Err(Error::Invalid); }
    for (actor, counters) in live {
        if actor == root { continue; }
        let fees = actors.get(actor.as_str()).ok_or(Error::Ineligible)?;
        if fees["root_task_id"] != root || fees["tokens"] != counters["tokens"]
            || counters["parent_task_id"] != root { return Err(Error::Ineligible); }
    }
    for (counter, closed) in [("tokens", "closed_tokens"), ("tool_calls", "closed_tool_calls")] {
        let sum = live.values().try_fold(cut[closed].as_u64().ok_or(Error::Invalid)?, |sum, value|
            sum.checked_add(value[counter].as_u64().ok_or(Error::Invalid)?).ok_or(Error::Invalid))?;
        if snapshot[counter].as_u64() != Some(sum) { return Err(Error::Invalid); }
    }
    let (mut tokens, mut count, mut totals) = (0_u64, 0_u64, [0_u64;7]);
    for row in rounds.values() {
        let (values, consumed) = quote_parts(&row["quote"])?;
        tokens = tokens.checked_add(consumed).ok_or(Error::Invalid)?;
        if row["payer_task_id"] == root {
            count = add(count, 1)?;
            for index in 0..7 { totals[index] = add(totals[index], values[index])?; }
        }
    }
    if Some(tokens) != snapshot["tokens"].as_u64() || number(&invoice["roundCount"])? != count { return Err(Error::Invalid); }
    for (index, (_, component)) in COMPONENTS.iter().enumerate() {
        if number(&invoice["componentsMicro"][*component])? != totals[index] { return Err(Error::Invalid); }
    }
    Ok(())
}
