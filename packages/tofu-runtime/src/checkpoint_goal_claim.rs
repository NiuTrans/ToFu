//! Shared whole-Goal claim preparation and protected retirement authority.
//! A selected transaction supplies exact records; Rust owns immutable lineage,
//! fee/account completeness and the pending actor set. This module grants no
//! lease and does not commit a backend transaction.
use crate::checkpoint_journal::{Error,Header,actor_fee_evidence_from_checkpoint,validate_actor_fee_evidence,validate_aggregate_actor_invoice};
use crate::checkpoint_goal_context::{self as context,ReadAuthority as ContextAuthority};
use crate::checkpoint_goal_policy as fee_account;
use crate::checkpoint_goal_codec::{charge,digest,text,read_error};
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
use std::{collections::{BTreeMap,BTreeSet},sync::LazyLock};
pub trait ReadAuthority:ContextAuthority {
    fn record(&mut self,task:&str)->Result<Option<Value>,Error>;
    fn header(&mut self,task:&str)->Result<Option<Header>,Error>;
    fn wallet(&mut self)->Result<Option<String>,Error>;
    fn ledger(&mut self,wallet:&str,kind:&str,reference:&str,root:&str)->Result<Option<Value>,Error>;
}
static REQUEST_SCHEMA: LazyLock<Value> = LazyLock::new(||
    serde_json::from_str(include_str!("../../../contracts/native_goal_actor_claim_v1.schema.json")).expect("Goal actor claim contract"));

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub format: String, pub tenant_id: String, pub source_run_id: String, pub target_attempt_id: String,
    pub source_cursor: u64, pub launch_sequence: u64, pub account_sequence: u64,
    pub actors: Vec<Actor>, pub invoice: Value,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Actor {
    pub event_sequence: u64, pub registration_version: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checkpoint_version: Option<u64>,
}
pub fn parse_request(payload: &Value, evidence_bytes: usize) -> Result<(Request, String), Error> {
    let request: Request = serde_json::from_value(payload.clone()).map_err(|_| Error::Invalid)?;
    if request.format != REQUEST_SCHEMA["properties"]["format"]["const"] || evidence_bytes == 0 || request.actors.is_empty()
        || request.source_run_id.is_empty() || request.source_run_id.chars().count() > 512
        || request.target_attempt_id.is_empty() || request.target_attempt_id.chars().count() > 512
        || payload["actors"].as_array().is_none_or(|rows| rows.iter().any(|row|
            row.get("checkpoint_version").is_some_and(Value::is_null))) {
        return Err(Error::Invalid);
    }
    Ok((request, digest(payload, evidence_bytes)?))
}
pub struct Prepared {
    pub context:Value,pub records:BTreeMap<String,u64>,pub pending:BTreeMap<String,Value>,
    pub remaining:usize,fees:Vec<Value>,
}
pub fn prepare_records(authority:&mut impl ReadAuthority,request:&Request,context:Value,mut remaining:usize)->Result<Prepared,Error>{
    let owner=authority.owner();
        let metadata = &context["launch"]["launch_metadata"];
        let reserved = if fee_account::cost_only(&request.invoice) { json!(0) }
            else { request.invoice["reserved_micro"].clone() };
        fee_account::validate_launch(&request.invoice, metadata, &reserved)?;
        let root = text(metadata, "task_id")?; let conversation = text(metadata, "conversation_id")?;
        if request.invoice["task_id"] != root || request.invoice["owner_user_id"].as_u64() != Some(owner)
            || request.invoice["model"] != metadata["launch"]["model"] { return Err(Error::Invalid); }
        let mut records = BTreeMap::new(); let mut pending = BTreeMap::new(); let mut fees = Vec::new();
        let mut sequences = BTreeSet::new();
        for actor in &request.actors {
            if actor.event_sequence >= request.source_cursor || actor.registration_version == 0
                || !sequences.insert(actor.event_sequence) { return Err(Error::Invalid); }
            let event = context::logical_event(authority, &request.source_run_id,
                &request.tenant_id, actor.event_sequence, &mut remaining).map_err(read_error)?;
            if event["kind"] != "agent" || !matches!(event["type"].as_str(),
                Some("execution_effect_started" | "execution_effect_completed" | "execution_effect_prepared")) { return Err(Error::Invalid); }
            if event["type"]=="execution_effect_prepared" {
                incoming_witness(authority,request,&event,actor.event_sequence,&mut remaining)?;
            }
            let task = text(&event, "invocation_id")?;
            if records.insert(task.to_owned(), actor.registration_version).is_some() { return Err(Error::Invalid); }
            let record = authority.record(task)?.ok_or(Error::Ineligible)?;
            charge(&record,&mut remaining)?;
            if record["version"].as_u64() != Some(actor.registration_version) { return Err(Error::Conflict); }
            let value = &record["value"]; let binding = &event["invocation_completion"];
            let birth = &binding["identity"];
            if binding["format"] != "tofu.flow.invocation-completion/v1"
                || birth["user_id"].as_u64() != Some(owner) || birth["conversation_id"] != conversation
                || birth["child_task_id"] != task || birth["archive_scope"] != format!("invocation:{}:{task}", owner)
                || value["child_invocation"] != *birth || value["task_id"] != task
                || value["user_id"].as_u64() != Some(owner) || value["conv_id"] != conversation
                || binding["account_root_task_id"] != birth["parent_task_id"] {
                return Err(Error::Invalid);
            }
            let admission = json!({"type":if event["type"]=="execution_effect_prepared" {"execution_effect_prepared"} else {"execution_effect_started"},"key":event["key"],"kind":"agent",
                "input_hash":event["input_hash"],"invocation_id":task,"invocation_completion":binding});
            if let Some(version) = actor.checkpoint_version {
                if version == 0 || !matches!(event["type"].as_str(),Some("execution_effect_started"|"execution_effect_prepared"))
                    || value.get("flow_invocation_completion").is_some() || birth["parent_task_id"] != root {
                    return Err(Error::Ineligible);
                }
                pending.insert(task.to_owned(), json!({"version":version,"admission":admission}));
            } else {
                let completion = &value["flow_invocation_completion"];
                let receipt = &completion["receipt"];
                if completion["admission"] != admission || completion.as_object().is_none_or(|v| v.len() != 3)
                    || receipt["kind"] != "agent" || receipt["input_hash"] != event["input_hash"]
                    || receipt["invocation_id"] != task || receipt["invocation_completion"] != *binding
                    || (event["type"] == "execution_effect_completed" && (
                        receipt["result"] != event["result"] || receipt["execution_account"] != event["execution_account"])) {
                    return Err(Error::Invalid);
                }
                let private_fees = &completion["native_fees"];
                validate_actor_fee_evidence(private_fees, owner, text(birth, "parent_task_id")?, task)?;
                if receipt["execution_account"]["root_task_id"] != birth["parent_task_id"]
                    || receipt["execution_account"]["owner_user_id"].as_u64() != Some(owner)
                    || receipt["execution_account"]["usage_known"] != true
                    || receipt["execution_account"]["limits"] != context["account"]["frontier"]["snapshot"]["limits"] {
                    return Err(Error::Ineligible);
                }
                fees.push(private_fees.clone());
            }
        }
        let live = context["account"]["frontier"]["actors"].as_object().ok_or(Error::Invalid)?;
        if pending.is_empty() || live.keys().filter(|id| id.as_str() != root).collect::<BTreeSet<_>>()
            != pending.keys().collect::<BTreeSet<_>>() { return Err(Error::Ineligible); }
    Ok(Prepared{context,records,pending,remaining,fees})
}
pub fn collect_frontiers(prepared:&mut Prepared,request:&Request,owner:u64,mut get:impl FnMut(&str)->Result<Value,Error>)->Result<(),Error>{
    let root=text(&prepared.context["launch"]["launch_metadata"],"task_id")?;
    for (task,observed) in &mut prepared.pending {
        let frontier=get(task)?;
        charge(&frontier,&mut prepared.remaining)?;
            if frontier["version"] != observed["version"] { return Err(Error::Conflict); }
            let state = &frontier["snapshot"]; let local = &state["shared_model_account"];
            let cut = &prepared.context["account"]["frontier"]; let counters = &cut["actors"][task];
            if local["limits"] != cut["snapshot"]["limits"] || ["tokens","model_ordinal","tool_ordinal"]
                .iter().any(|key| local[*key] != counters[*key])
                || local["tool_calls"].as_u64().zip(counters["tool_calls"].as_u64()).is_none_or(|(a,b)| a > b) {
                return Err(Error::Ineligible);
            }
            prepared.fees.push(actor_fee_evidence_from_checkpoint(state, owner, root, task, &request.tenant_id)?);
            observed["frontier"] = frontier;
    }
    Ok(())
}
pub fn finish(prepared:&Prepared,request:&Request)->Result<(),Error>{
    validate_aggregate_actor_invoice(&prepared.context["account"]["frontier"],&prepared.fees,&request.invoice)
}
pub fn incoming_witness(authority:&mut impl ReadAuthority,request:&Request,event:&Value,sequence:u64,remaining:&mut usize)->Result<Value,Error>{
    let owner=authority.owner();
        let task=text(event,"invocation_id")?;let root=text(&request.invoice,"task_id")?;
        let conversation=text(&event["invocation_completion"]["identity"],"conversation_id")?;
        let unstarted=event["type"]=="execution_effect_prepared";
        if !unstarted && event["type"]!="execution_effect_started" {return Err(Error::Invalid);}
        let mut preparation=event.clone();preparation["type"]=json!("execution_effect_prepared");
        fee_account::validate_preparation(&preparation,owner,root,conversation,task)?;
        let mut matched=None;
        for index in request.launch_sequence+1..request.source_cursor {
            let value=context::event(authority,&request.source_run_id,&request.tenant_id,index,remaining)
                .map_err(read_error)?.ok_or(Error::Conflict)?;
            if unstarted && index>sequence && value["key"]==event["key"] && matches!(value["type"].as_str(),
                Some("execution_effect_started"|"execution_effect_completed"|"execution_effect_prepared")) {
                return Err(Error::Conflict);
            }
            if value["type"]!="execution_recovery_consumed" {continue;}
            for actor in value["actors"].as_array().ok_or(Error::Invalid)? {
                if actor["target_task_id"]!=task {continue;}
                if matched.is_some() || actor["preparation"]!=preparation
                    || (unstarted && index<=sequence) || (!unstarted && index>=sequence) {
                    return Err(Error::Conflict);
                }
                let source=text(actor,"source_task_id")?;
                let header=authority.header(source)?.ok_or(Error::Ineligible)?.value;
                let handoff=&header["goal_actor_handoff"];let claim=&header["goal_actor_claim"];
                if header["owner_user_id"].as_u64()!=Some(owner) || header["task_id"]!=source
                    || header["phase"]!="terminal" || !header["lease"].is_null()
                    || handoff["target_task_id"]!=task || handoff["target_root_task_id"]!=root
                    || handoff["request_sha256"]!=value["request_sha256"]
                    || handoff["witness_sha256"]!=digest(actor,*remaining)?
                    || claim["source_run_id"]!=value["source_run_id"] || claim["source_cursor"]!=value["source_cursor"] {
                    return Err(Error::Ineligible);
                }
                charge(&header,remaining)?;
                matched=Some(actor.clone());
            }
        }
        matched.ok_or(Error::Ineligible)
}

/// Cost-only launches must not conceal any old or fresh wallet activity.
pub fn require_no_financial_activity(authority:&mut impl ReadAuthority,root:&str)->Result<(),Error>{
    if let Some(wallet)=authority.wallet()? {
        for (kind,reference) in [("reserve","reserve"),("native_admission","reserve"),("reserve_release","reserve"),("debit","task")] {
            if authority.ledger(&wallet,kind,reference,root)?.is_some(){return Err(Error::Ineligible);}
        }
    }
    Ok(())
}

/// Protected claim bytes are identical across both authorities and replay.
pub fn protected_claim(request:&Request,digest:&str)->Value {
    json!({"source_run_id":request.source_run_id,"source_cursor":request.source_cursor,
        "root_task_id":request.invoice["task_id"],"target_attempt_id":request.target_attempt_id,"request_sha256":digest})
}
pub fn existing_actors<'a>(event:&'a Value,request:&Request,digest:&str)->Result<&'a Vec<Value>,Error>{
    let claim=&event["native_actor_claim"];
    if event["type"]!="execution_recovery_claim" || claim["request_sha256"]!=digest {return Err(Error::Conflict);}
    let actors=claim["actors"].as_array().filter(|rows|!rows.is_empty()).ok_or(Error::Invalid)?;
    if claim["invoice"]!=request.invoice || claim["target_attempt_id"]!=request.target_attempt_id
        || actors.len()!=request.actors.iter().filter(|row|row.checkpoint_version.is_some()).count(){return Err(Error::Conflict);}
    Ok(actors)
}
pub fn validate_existing_header(header:&Header,actor:&Value,request:&Request,digest:&str)->Result<(),Error>{
    if header.value["phase"]!="terminal" || !header.value["lease"].is_null()
        || actor["version"].as_u64().is_none_or(|claimed|header.version<claimed)
        || header.value["goal_actor_claim"]!=protected_claim(request,digest){return Err(Error::Conflict);}
    Ok(())
}
/// Writer quiescence is established by active callers, or authenticated replay.
pub fn retire_header(owner:u64,task:&str,header:&Header,observed:&Value,request:&Request,digest:&str)->Result<Value,Error>{
    if Some(header.version)!=observed["version"].as_u64() || header.value["phase"]!=observed["frontier"]["phase"]
        || header.value["owner_user_id"].as_u64()!=Some(owner) || header.value["task_id"]!=task {return Err(Error::Conflict);}
    let mut retired=header.value.clone();retired["phase"]=json!("terminal");retired["lease"]=Value::Null;
    retired["goal_actor_claim"]=protected_claim(request,digest);Ok(retired)
}
pub fn receipt(request:&Request,digest:&str,context:&Value,actors:Vec<Value>)->Result<Value,Error>{
    let target=text(&context["accepted"],"taskId")?;
    Ok(json!({"type":"execution_recovery_claim","successor_run_id":format!("goal_{target}"),
        "native_actor_claim":{"format":"tofu.native-goal-actor-claim/v1","request_sha256":digest,
            "target_attempt_id":request.target_attempt_id,"source_cursor":request.source_cursor,
            "account_version":context["account"]["frontier"]["version"],"actors":actors,"invoice":request.invoice}}))
}
/// Validate the original hold before returning a settlement intent. The existing
/// billing owner executes it inside this same transaction; no retry is granted.
pub fn settlement(authority:&mut impl ReadAuthority,invoice:&Value)->Result<Option<Value>,Error>{
    let root=text(invoice,"task_id")?;
    if fee_account::cost_only(invoice){require_no_financial_activity(authority,root)?;return Ok(None);}
    let wallet=invoice["wallet_user_id"].as_str().ok_or(Error::Invalid)?;
    if wallet.is_empty() || authority.wallet()?.as_deref()!=Some(wallet){return Err(Error::Ineligible);}
    let mut ledger=Vec::new();
    for (kind,reference) in [("reserve","reserve"),("native_admission","reserve"),("reserve_release","reserve"),("debit","task")] {
        ledger.push(authority.ledger(wallet,kind,reference,root)?);
    }
    let reserved=invoice["reserved_micro"].as_u64().filter(|n|*n>0 && *n<=i64::MAX as u64).ok_or(Error::Invalid)?;
    fee_account::validate_wallet_hold(reserved,ledger[0].as_ref(),ledger[1].as_ref(),ledger[2].as_ref(),ledger[3].as_ref())?;
    let actual=invoice["componentsMicro"]["totalMicro"].as_u64().ok_or(Error::Invalid)?;
    use sha2::{Sha256,Digest};
    let stable=|kind:&str|{let digest=format!("{:x}",Sha256::digest(format!("{kind}\0{wallet}\0{root}").as_bytes()));format!("led_{}",&digest[..32])};
    Ok(Some(json!({"user_id":wallet,"ref_id":root,"reserved_micro":reserved,"actual_micro":actual,
        "note":serde_json::to_string(invoice).map_err(|_|Error::Invalid)?,"release_id":stable("release"),"debit_id":stable("debit")})))
}
