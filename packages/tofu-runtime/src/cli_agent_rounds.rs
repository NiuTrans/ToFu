//! Optional host round authority over the existing bounded one-slot stdio port.
//! Raw evidence is acknowledged before later dispatch/terminal; no price policy.
use crate::cli_agent_control::Exchange;
use serde_json::{json, Value};
use serde_core::{Serialize,Serializer,ser::{SerializeMap,Error}};
use std::io::{self,Write};
use std::sync::Arc;
use tofu_runtime::{
    round_admission::{Admission, Context, Evidence, Permit},
    session::ExecutionSession,
    tool_contract::Catalog,
};
/// Borrow native history until serialization, then validate only the bounded wire.
struct Frame<'a> { metadata: &'a Value, history: Option<&'a [Value]>, checkpoint: Option<&'a tofu_runtime::agent_checkpoint::View<'a>>, account: Option<&'a Value>, deadline_unix_ms: Option<u64> }
impl Serialize for Frame<'_> {
    fn serialize<S:Serializer>(&self,serializer:S)->Result<S::Ok,S::Error>{
        let fields=self.metadata.as_object().ok_or_else(||S::Error::custom("round metadata invalid"))?;
        let mut map=serializer.serialize_map(Some(fields.len()+usize::from(self.history.is_some())+usize::from(self.checkpoint.is_some())))?;
        for (key,value) in fields {map.serialize_entry(key,value)?;}
        if let Some(history)=self.history {map.serialize_entry("messages",history)?;}
        if let Some(checkpoint)=self.checkpoint {let mut state=*checkpoint;state.model_account=self.account;state.deadline_unix_ms=self.deadline_unix_ms;map.serialize_entry("state",&state)?;}
        map.end()
    }
}
struct FrameBytes {bytes:Vec<u8>,limit:usize}
impl Write for FrameBytes {
    fn write(&mut self,bytes:&[u8])->io::Result<usize>{
        if bytes.len()>self.limit.saturating_sub(self.bytes.len()){return Err(io::ErrorKind::InvalidData.into());}
        self.bytes.extend_from_slice(bytes);Ok(bytes.len())
    }
    fn flush(&mut self)->io::Result<()>{Ok(())}
}
#[derive(Clone)]
pub(crate) struct Rounds {
    pub session: Arc<ExecutionSession>,
    pub runtime: Arc<tokio::runtime::Runtime>,
    pub control: Arc<Exchange>,
    pub schemas: Arc<Catalog>,
    pub version: String,
    pub owner: u64,
    pub task_id: String,
    pub epoch: String,
    pub frame_bytes: usize,
    pub prepare_history: bool,
    pub progress_limits: tofu_runtime::progress::Limits,
    pub checkpoint_binding: Option<String>,
    pub checkpoint_account: Option<Arc<tofu_runtime::model_account::Account>>,
    pub checkpoint_account_policy: Option<Value>,
    pub shared_counter_policy: Option<Value>,
    pub deadline_unix_ms: Option<u64>,
    pub checkpoint_suspended: Option<Arc<std::sync::atomic::AtomicBool>>,
    pub preparation_deadline: Option<Arc<std::sync::OnceLock<Option<u64>>>>,
}
fn failure() -> Value {
    json!({"kind":"round_control","message":"native host round authority refused"})
}
pub(crate) async fn deadline(session: &ExecutionSession) {
    let snapshot = match session.snapshot() {
        Ok(s) => s,
        Err(_) => return,
    };
    if let Some(n) = snapshot.deadline_remaining_nanos {
        if n <= 0 {
            return;
        }
        let n = n as u128;
        let d = std::time::Duration::new(
            (n / 1_000_000_000).min(u64::MAX as u128) as u64,
            (n % 1_000_000_000) as u32,
        );
        if let Some(at) = tokio::time::Instant::now().checked_add(d) {
            tokio::time::sleep_until(at).await;
            return;
        }
    }
    std::future::pending::<()>().await;
}
impl Rounds {
    pub(crate) fn inherited_deadline(&self)->Result<Option<u64>,Value> {
        // Capture the OS clock before remaining time: observation/serialization
        // delay can shorten this hint, never extend the native authority.
        let cache=self.preparation_deadline.as_ref().ok_or_else(failure)?;
        let deadline=if let Some(deadline)=cache.get(){*deadline}else{
        let mut clock = libc::timespec {tv_sec:0,tv_nsec:0};
        if unsafe {libc::clock_gettime(libc::CLOCK_MONOTONIC,&mut clock)} != 0
            || clock.tv_sec < 0 || !(0..1_000_000_000).contains(&clock.tv_nsec) {
            return Err(failure());
        }
        let now=(clock.tv_sec as u128)*1_000_000_000 + clock.tv_nsec as u128;
        let remaining=self.session.snapshot().map_err(|_|failure())?.deadline_remaining_nanos;
        let deadline=remaining.map(|n| now.checked_add(n.max(0) as u128)
            .and_then(|n|u64::try_from(n).ok()).ok_or_else(failure)).transpose()?;
        *cache.get_or_init(||deadline)
        };
        Ok(deadline)
    }
    fn exchange(
        &self,
        value: Value,
        response: &str,
        session: &ExecutionSession,
        observe_cancel: bool,
        history: Option<&[Value]>,
    ) -> Result<Value, Value> {
        self.exchange_checkpoint(value,response,session,observe_cancel,history,None)
    }
    fn exchange_checkpoint<'a>(
        &'a self,value:Value,response:&str,session:&ExecutionSession,observe_cancel:bool,
        history:Option<&'a [Value]>,checkpoint:Option<&'a tofu_runtime::agent_checkpoint::View<'a>>,
    )->Result<Value,Value>{
        let kind = value["type"].as_str().ok_or_else(failure)?;
        let account=if let Some(view)=checkpoint {
            match &self.checkpoint_account {
                Some(account)=>{
                    let snapshot=account.snapshot(self.owner).map_err(|_|failure())?;
                    if snapshot.pending!=0 || snapshot.pending_observations!=0 || snapshot.reserved!=0 || snapshot.last_ordinal!=view.next_round as u64 {return Err(failure());}
                    Some(json!({"tokens":snapshot.tokens,"last_ordinal":snapshot.last_ordinal,"complete":snapshot.complete,"policy":self.checkpoint_account_policy}))
                },None=>None,
            }
        }else{None};
        let mut writer=FrameBytes {bytes:Vec::new(),limit:self.frame_bytes};
        serde_json::to_writer(&mut writer,&Frame {metadata:&value,history,checkpoint,account:account.as_ref(),deadline_unix_ms:self.deadline_unix_ms}).map_err(|_|failure())?;
        let bytes=writer.bytes;
        let encoded=if checkpoint.is_some(){let mut metadata=value.clone();metadata["state"]=json!({});metadata}else{serde_json::from_slice(&bytes).map_err(|_|failure())?};
        self.schemas.validate(self.owner,kind,encoded).map_err(|_|failure())?;
        self.control.prepare(
            value["request_id"].as_str().ok_or_else(failure)?.into(),
            response,
        )?;
        self.runtime.block_on(async {
            let publication_cancelled = async {
                if observe_cancel {
                    tokio::select! {
                        _ = session.cancelled() => {},
                        _ = deadline(session) => {},
                    }
                } else {
                    deadline(session).await;
                }
            };
            let publish = async {
                if (self.prepare_history && kind == "round_request") || checkpoint.is_some() {
                    crate::cli_agent::publish_preparation(&self.runtime, &bytes, publication_cancelled, &self.control, session).await
                } else {
                    crate::cli_agent::publish_request(&self.runtime, &bytes, publication_cancelled, &self.control).await
                }
            };
            publish.await.map_err(|_| failure())?;
            let reply_cancelled = async {
                if observe_cancel {
                    session.cancelled().await;
                } else {
                    std::future::pending::<()>().await;
                }
            };
            tokio::select! {
                biased;
                _ = reply_cancelled => Err(failure()),
                _ = deadline(session) => Err(failure()),
                reply = self.control.response() => reply,
            }
        })
    }
}
impl Admission for Rounds {
    fn reserve(&self, context: Context<'_>) -> Result<Permit, Value> {
        if context.owner != self.owner || context.task_id != self.task_id {
            return Err(failure());
        }
        let round = context.round;
        let id = format!("{}-round-{round}", self.task_id);
        let mut request = json!({
            "version": self.version, "type": "round_request", "request_id": id,
            "owner_user_id": self.owner, "task_id": self.task_id, "round": round,
            "history_messages": context.messages.len(),
        });
        if self.prepare_history || self.progress_limits.no_progress_rounds > 0 || self.progress_limits.nonretryable_failure_rounds > 0 || self.checkpoint_binding.is_some() {
            let deadline=self.inherited_deadline()?;
            request["deadline_monotonic_ns"]=serde_json::to_value(deadline).map_err(|_|failure())?;
        }
        let mut decision = self.exchange(
            request,
            "round_decision",
            &self.session,
            true,
            self.prepare_history.then_some(context.messages),
        )?;
        if decision["preparation_failed"] == true {
            return Err(json!({"kind":"round_preparation_failed","message":"native host round preparation failed"}));
        }
        if decision["approved"] != true {
            return Err(
                json!({"kind":"round_denied","message":"native host denied the model round"}),
            );
        }
        let prepared = decision.as_object_mut().and_then(|v|v.remove("messages"));
        if self.prepare_history != prepared.is_some() { return Err(failure()); }
        let prepared = prepared.map(|v| match v { Value::Array(values)=>Ok(values),_=>Err(failure()) }).transpose()?;
        let rounds = self.clone();
        let session = self.session.clone();
        let permit = Permit::new(Box::new(move |outcome| {
            let evidence = match outcome {
                Evidence::Returned(r) => {
                    json!({"kind":"returned","usage":r.usage,"usage_observation":tofu_runtime::usage::evidence(&r.usage),"provider_state":r.state.as_str(),"finish_reason":r.finish_reason,"malformed_frames":r.malformed_frames})
                }
                Evidence::Failed(e) => json!({"kind":"failed","error":e}),
                Evidence::Stopped => json!({"kind":"stopped"}),
                Evidence::NotDispatched => json!({"kind":"not_dispatched"}),
                Evidence::Abandoned => json!({"kind":"abandoned"}),
            };
            let ack = rounds.exchange(
                json!({
                    "version": rounds.version, "type": "round_settled",
                    "request_id": format!("{id}-settle"), "owner_user_id": rounds.owner,
                    "task_id": rounds.task_id, "round": round, "evidence": evidence,
                }),
                "round_ack",
                &session,
                false,
                None,
            )?;
            if ack["accepted"] != true {
                return Err(failure());
            }
            Ok(())
        }));
        Ok(match prepared {Some(messages)=>permit.with_messages(messages),None=>permit})
    }
}


impl tofu_runtime::progress::Policy for Rounds {
    fn limits(&self) -> tofu_runtime::progress::Limits {self.progress_limits}
    fn observe(&self, context: tofu_runtime::progress::Context<'_>) -> Result<tofu_runtime::progress::Observation, Value> {
        if context.owner != self.owner || context.task_id != self.task_id {return Err(failure());}
        let decision = self.exchange(json!({
            "version":self.version,"type":"progress_request","request_id":format!("{}-progress-{}",self.task_id,context.round),
            "owner_user_id":self.owner,"task_id":self.task_id,"round":context.round,"tool_calls":context.tool_calls,
        }),"progress_decision",context.session,false,None)?;
        if decision["accepted"] != true {return Err(json!({"kind":"progress_observation_failed","message":"native host progress observation failed"}));}
        let value=&decision["observation"];
        let strings=|key:&str|->Result<Vec<String>,Value>{
            value[key].as_array().ok_or_else(failure)?.iter().map(|v|v.as_str().map(String::from).ok_or_else(failure)).collect()
        };
        Ok(tofu_runtime::progress::Observation {
            world_version:value["worldVersion"].as_str().ok_or_else(failure)?.into(),
            evidence_ids:strings("evidenceIds")?,
            evidence_complete:value["resultEvidenceComplete"].as_bool().ok_or_else(failure)?,
            verification:value["verification"].as_str().ok_or_else(failure)?.into(),
            nonretryable_failure_signatures:strings("nonretryableFailureSignatures")?,
        })
    }
}


impl tofu_runtime::agent_checkpoint::Port for Rounds {
    fn binding(&self)->&str {self.checkpoint_binding.as_deref().unwrap_or("")}
    fn load(&self,identity:tofu_runtime::agent_checkpoint::Identity<'_>)->Result<Option<Value>,Value>{
        let mut reply=self.checkpoint_exchange("load",0,None,identity.session)?;
        let state=reply.as_object_mut().and_then(|v|v.remove("state"));
        if let Some(state)=&state {
            match (self.deadline_unix_ms, state.get("deadline_unix_ms")) {
                (None, None) => {},
                (Some(expected), Some(saved)) if saved.as_u64() == Some(expected) => {},
                _ => return Err(failure()),
            }
            match (&self.shared_counter_policy,state.get("shared_model_account")) {
                (None,None) => {},
                (Some(policy),Some(saved)) if saved["counter_policy"] == *policy && saved["owner_user_id"] == self.owner
                    && saved["root_task_id"] == self.task_id && state.get("model_account").is_none() => {},
                _ => return Err(failure()),
            }
            match (&self.checkpoint_account,state.get("model_account")) {
                (Some(account),Some(saved))=>{
                    let object=saved.as_object().ok_or_else(failure)?;
                    if object.len()!=4 || !["tokens","last_ordinal","complete","policy"].iter().all(|key|object.contains_key(*key))
                        || Some(&saved["policy"])!=self.checkpoint_account_policy.as_ref()
                        || saved["last_ordinal"].as_u64()!=state["next_round"].as_u64() {return Err(failure());}
                    account.restore(self.owner,&self.task_id,saved["tokens"].as_u64().ok_or_else(failure)?,saved["last_ordinal"].as_u64().ok_or_else(failure)?,saved["complete"].as_bool().ok_or_else(failure)?).map_err(|_|failure())?;
                },(None,None)=>{},_=>return Err(failure()),
            }
        }
        Ok(state)
    }
    fn begin(&self,identity:tofu_runtime::agent_checkpoint::Identity<'_>,round:usize)->Result<(),Value>{
        self.checkpoint_exchange("begin",round,None,identity.session).map(|_|())
    }
    fn complete(&self,view:tofu_runtime::agent_checkpoint::View<'_>)->Result<tofu_runtime::agent_checkpoint::Disposition,Value>{
        let reply=self.checkpoint_exchange("complete",view.next_round-1,Some(&view),view.identity.session)?;
        let suspended = reply["disposition"] == "suspend";
        if let Some(marker) = &self.checkpoint_suspended {
            marker.store(suspended, std::sync::atomic::Ordering::SeqCst);
        }
        Ok(if suspended {tofu_runtime::agent_checkpoint::Disposition::Suspend}else{tofu_runtime::agent_checkpoint::Disposition::Continue})
    }
}
impl Rounds {
    fn checkpoint_exchange<'a>(&'a self,action:&str,round:usize,state:Option<&'a tofu_runtime::agent_checkpoint::View<'a>>,session:&ExecutionSession)->Result<Value,Value>{
        let reply=self.exchange_checkpoint(json!({"version":self.version,"type":"checkpoint_request","request_id":format!("{}-checkpoint-{action}-{round}",self.task_id),"owner_user_id":self.owner,"task_id":self.task_id,"epoch":self.epoch,"binding":self.checkpoint_binding,"round":round,"action":action,"deadline_monotonic_ns":self.inherited_deadline()?}),"checkpoint_decision",session,false,None,state)?;
        if reply["accepted"]!=true {return Err(json!({"kind":"checkpoint_policy_failed","message":"native host checkpoint policy refused"}));}
        if (action!="load" && reply.get("state").is_some()) || (action!="complete" && reply["disposition"]!="continue") {return Err(failure());}
        Ok(reply)
    }
}
