//! Shared whole-Goal claim consumption authority and exact receipt projection.
//! Reads are bound to the caller's selected transaction. No per-actor commit or
//! capability publication occurs here; backends commit the entire validated set.
use crate::checkpoint_journal::{ActorHandoff,Error,validate_actor_handoff_accounts};
use crate::checkpoint_goal_context as goal;
use crate::checkpoint_goal_policy as fee_account;
use crate::checkpoint_goal_codec::{charge,digest,text,read_error};
use crate::checkpoint_goal_claim::{ReadAuthority,Request,parse_request,incoming_witness,require_no_financial_activity};
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
use std::{collections::{BTreeSet,BTreeMap},sync::LazyLock};
static SCHEMA:LazyLock<Value>=LazyLock::new(||serde_json::from_str(
    include_str!("../../../contracts/native_goal_actor_handoff_v1.schema.json")).expect("actor handoff contract"));
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Authorization {
    pub format:String,pub claim_request:Value,pub target_launch_sequence:u64,pub target_account_sequence:u64,
    pub target_cursor:u64,pub target_reserved_micro:u64,pub actors:Vec<Binding>,pub account_frontiers:Vec<Value>,
}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {pub source_task_id:String,pub target_task_id:String,pub target_registration_version:u64}

pub fn parse(value:&Value,maximum:usize)->Result<(Authorization,Request,String),Error>{
    let authorization:Authorization=serde_json::from_value(value.clone()).map_err(|_|Error::Invalid)?;
    if authorization.format!=SCHEMA["properties"]["format"]["const"] || authorization.actors.is_empty()
        || authorization.account_frontiers.len()!=authorization.actors.len()
        || authorization.target_launch_sequence>=authorization.target_account_sequence
        || authorization.target_account_sequence>=authorization.target_cursor
        || authorization.target_reserved_micro>i64::MAX as u64 {
        return Err(Error::Invalid);
    }
    let (request,_)=parse_request(&authorization.claim_request,maximum)?;
    if (authorization.target_reserved_micro==0) != fee_account::cost_only(&request.invoice) {
        return Err(Error::Invalid);
    }
    let mut sources=BTreeSet::new();let mut targets=BTreeSet::new();
    for row in &authorization.actors {
        if row.target_registration_version==0 || [&row.source_task_id,&row.target_task_id].iter()
            .any(|s|s.is_empty()||s.chars().count()>512) || !sources.insert(&row.source_task_id)
            || !targets.insert(&row.target_task_id) {return Err(Error::Invalid);}
    }
    if !sources.is_disjoint(&targets){return Err(Error::Invalid);}
    Ok((authorization,request,digest(value,maximum)?))
}


pub fn consumption_context(authority:&mut impl ReadAuthority,authorization:&Authorization,request:&Request,actors:&[ActorHandoff],remaining:&mut usize)->Result<Value,Error>{
    let owner=authority.owner();
        let claim=goal::event(authority,&request.source_run_id,&request.tenant_id,request.source_cursor,remaining)
            .map_err(read_error)?.ok_or(Error::Ineligible)?;
        let digest=digest(&authorization.claim_request,*remaining)?;
        let rows=claim["native_actor_claim"]["actors"].as_array().ok_or(Error::Invalid)?;
        if claim["type"]!="execution_recovery_claim" || claim["native_actor_claim"]["request_sha256"]!=digest
            || claim["native_actor_claim"]["invoice"]!=request.invoice
            || claim["native_actor_claim"]["target_attempt_id"]!=request.target_attempt_id
            || rows.len()!=authorization.actors.len() || rows.iter().map(|row|text(row,"task_id"))
                .collect::<Result<BTreeSet<_>,_>>()? != authorization.actors.iter().map(|row|row.source_task_id.as_str()).collect() {
            return Err(Error::Conflict);
        }
        let context=goal::context(authority,&request.source_run_id,&request.target_attempt_id,
            &request.tenant_id,request.source_cursor,request.launch_sequence,request.account_sequence,Some(&claim),remaining).map_err(read_error)?;
        let target_root=text(&context["accepted"],"taskId")?;let run=format!("goal_{target_root}");
        if context["target_state"]["next_event_sequence"].as_u64()!=Some(authorization.target_cursor)
            || claim["successor_run_id"]!=run {return Err(Error::Conflict);}
        let launch=goal::logical_event(authority,&run,&request.tenant_id,authorization.target_launch_sequence,remaining).map_err(read_error)?;
        let account=goal::logical_event(authority,&run,&request.tenant_id,authorization.target_account_sequence,remaining).map_err(read_error)?;
        let metadata=&launch["launch_metadata"];let original=&context["launch"]["launch_metadata"];
        if launch["type"]!="execution_journal_start" || launch["parent_run_id"]!=request.source_run_id
            || account["type"]!="execution_account_frontier" || account["launch_id"]!=metadata["launch_id"]
            || metadata["launch_id"].as_str().is_none_or(str::is_empty)
            || metadata["task_id"]!=target_root || metadata["run_id"]!=run
            || metadata["owner_user_id"].as_u64()!=Some(owner) || metadata["tenant_id"]!=request.tenant_id
            || metadata["attempt_id"]!=request.target_attempt_id || metadata["turn_id"]!=context["accepted"]["turnId"]
            || ["conversation_id","objective","launch","invocation_policy"].iter().any(|key|metadata[*key]!=original[*key]) {
            return Err(Error::Invalid);
        }
        let mut preparations=BTreeMap::new();
        for sequence in authorization.target_account_sequence+1..authorization.target_cursor {
            let value=goal::event(authority,&run,&request.tenant_id,sequence,remaining).map_err(read_error)?
                .ok_or(Error::Conflict)?;
            if value["type"]=="execution_account_frontier" {return Err(Error::Conflict);}
            if value["type"]=="execution_effect_prepared" && value.get("invocation_completion").is_some() {
                let target=text(&value,"invocation_id")?;
                fee_account::validate_preparation(&value,owner,target_root,text(metadata,"conversation_id")?,target)?;
                if preparations.insert(target.to_owned(),value).is_some(){return Err(Error::Conflict);}
            }
        }
        if !preparations.is_empty() {
            if preparations.len()!=authorization.actors.len(){return Err(Error::Invalid);}
            let mut sources=BTreeMap::new();
            let mut witnesses=BTreeMap::new();
            for actor in &request.actors {
                let event=goal::logical_event(authority,&request.source_run_id,&request.tenant_id,
                    actor.event_sequence,remaining).map_err(read_error)?;
                sources.insert(text(&event,"invocation_id")?.to_owned(),(event,actor.event_sequence));
            }
            for actor in &authorization.actors {
                let prepared=preparations.get(&actor.target_task_id).ok_or(Error::Invalid)?;
                let (original,sequence)=sources.get(&actor.source_task_id).ok_or(Error::Invalid)?;
                if prepared["key"]!=original["key"] || prepared["input_hash"]!=original["input_hash"] {
                    return Err(Error::Invalid);
                }
                let native=actors.iter().find(|native|native.source_task_id==actor.source_task_id).ok_or(Error::Invalid)?;
                let mut witness=serde_json::to_value(actor).map_err(|_|Error::Invalid)?;
                witness["preparation"]=prepared.clone();
                if let Some(recovery)=native.target_snapshot.get("tool_recovery") {
                    let producer=text(recovery,"producer_task_id")?;
                    let origin=if producer==actor.source_task_id {
                        json!({"task_id":producer,"parent_task_id":request.invoice["task_id"]})
                    } else {
                        let incoming=incoming_witness(authority,request,original,*sequence,remaining)?;
                        if incoming["effect_origin"]["task_id"]!=producer {return Err(Error::Ineligible);}
                        incoming["effect_origin"].clone()
                    };
                    witness["effect_origin"]=origin;
                }
                witnesses.insert(actor.target_task_id.clone(),witness);
            }
            // Keep the exact row hashed into the retired protected header.
            preparations=witnesses;
        }
        let bindings:Vec<_>=authorization.actors.iter().map(|row|(row.source_task_id.as_str(),row.target_task_id.as_str())).collect();
        validate_actor_handoff_accounts(&context["account"]["frontier"],&account["frontier"],&authorization.account_frontiers,&bindings,target_root)?;
        let mut records=Vec::new();
        for row in &authorization.actors {
            let value=authority.record(&row.target_task_id)?.ok_or(Error::Ineligible)?;
            charge(&value,remaining)?;
            if value["version"].as_u64()!=Some(row.target_registration_version)
                || value["value"]["task_id"]!=row.target_task_id || value["value"]["user_id"].as_u64()!=Some(owner)
                || value["value"]["conv_id"]!=metadata["conversation_id"] || value["value"]["status"]!="registered"
                || value["value"].get("flow_invocation_completion").is_some()
                || value["value"]["child_invocation"]!=json!({"user_id":owner,"conversation_id":metadata["conversation_id"],
                    "parent_task_id":target_root,"child_task_id":row.target_task_id,"archive_scope":format!("invocation:{}:{}",owner,row.target_task_id)}) {
                return Err(Error::Conflict);
            }
            records.push(value);
        }
        let source_reserved = if fee_account::cost_only(&request.invoice) { json!(0) }
            else { request.invoice["reserved_micro"].clone() };
        fee_account::validate_launch(&request.invoice, original, &source_reserved)?;
        fee_account::validate_launch(&request.invoice, metadata, &json!(authorization.target_reserved_micro))?;
        let mut ledger=Vec::new();
        if fee_account::cost_only(&request.invoice) {
            require_no_financial_activity(authority,target_root)?;
            require_no_financial_activity(authority,text(&request.invoice,"task_id")?)?;
        } else {
        // A fresh parent hold/admission is required; old prepaid work does not
        // authorize an unreserved new payer. This operation never charges it.
        let wallet=request.invoice["wallet_user_id"].as_str().ok_or(Error::Invalid)?;
        if authority.wallet()?.as_deref()!=Some(wallet){return Err(Error::Ineligible);}
        for (kind,reference) in [("reserve","reserve"),("native_admission","reserve"),("reserve_release","reserve"),("debit","task")] {
            ledger.push(authority.ledger(wallet,kind,reference,target_root)?);
        }
        fee_account::validate_wallet_hold(authorization.target_reserved_micro,ledger[0].as_ref(),ledger[1].as_ref(),
            ledger[2].as_ref(),ledger[3].as_ref())?;
        }
        let mut result=json!({"goal":context,"claim":claim,"launch":launch,"account":account,"records":records,"ledger":ledger});
        // Preserve legacy archived context digests when no preparation was bound.
        if !preparations.is_empty(){result["preparations"]=serde_json::to_value(preparations).map_err(|_|Error::Invalid)?;}
        charge(&result,remaining)?;
        Ok(result)
    }

pub fn consumption_receipt(authorization:&Authorization,request:&Request,digest:&str,context:&Value)->Result<Value,Error>{
    let mut actors=serde_json::to_value(&authorization.actors).map_err(|_|Error::Invalid)?;
    for actor in actors.as_array_mut().ok_or(Error::Invalid)? {
        if let Some(prepared)=context.get("preparations").and_then(|rows|actor["target_task_id"].as_str().and_then(|task|rows.get(task))) {
            *actor=prepared.clone();
        }
    }
    Ok(json!({"type":"execution_recovery_consumed","source_run_id":request.source_run_id,"source_cursor":request.source_cursor,
        "request_sha256":digest,"actors":actors}))
}

/// Revalidate each prepared successor against the protected source in the same
/// committing transaction. Journal remains the sole checkpoint transformer.
pub fn transfer_header(store:&dyn crate::checkpoint_journal::Store,owner:u64,
    header_bytes:usize,snapshot_bytes:usize,evidence_bytes:usize,actor:&ActorHandoff,binding:&Binding,
    request:&Request,context:&Value,request_digest:&str,source:crate::checkpoint_journal::Header,
    snapshot:Value)->Result<Value,Error>{
    use crate::checkpoint_journal::{ActorHandoffSpec,Header,Journal,Scope};
    let target=&binding.target_task_id;
    let target_root=text(&context["goal"]["accepted"],"taskId")?;
    let crate::checkpoint_journal::Header{value:header,version}=source;
            if header!=actor.source_header || version!=actor.source_version
                || header["goal_actor_claim"]!=json!({"source_run_id":request.source_run_id,"source_cursor":request.source_cursor,
                    "root_task_id":request.invoice["task_id"],"target_attempt_id":request.target_attempt_id,
                    "request_sha256":context["claim"]["native_actor_claim"]["request_sha256"]}) {return Err(Error::Conflict);}
            let scope=Scope {owner:owner,task_id:target.clone(),epoch:header["epoch"].as_str().ok_or(Error::Invalid)?.into(),
                binding:header["binding"].as_str().ok_or(Error::Invalid)?.into()};
            let spec=ActorHandoffSpec {scope:scope.clone(),source_task_id:actor.source_task_id.clone(),source_root_task_id:actor.source_root_task_id.clone(),
                target_root_task_id:target_root.into(),token:actor.target_header["lease"].as_str().ok_or(Error::Invalid)?.into()};
            let prepared=Journal::new(scope,store,snapshot_bytes,header_bytes)?.prepare_retired_actor(&spec,
                Header {version,value:header.clone()},snapshot,actor.target_header.get("writer_process").cloned())?;
            if prepared.target_header!=actor.target_header || prepared.target_snapshot!=actor.target_snapshot {return Err(Error::Conflict);}
            let mut retired=header;
            retired["goal_actor_handoff"]=json!({"target_task_id":target,"target_root_task_id":target_root,"request_sha256":request_digest});
            if let Some(prepared)=context.get("preparations").and_then(|rows|rows.get(target)) {
                retired["goal_actor_handoff"]["witness_sha256"]=json!(digest(prepared,evidence_bytes)?);
            }
    Ok(retired)
}
