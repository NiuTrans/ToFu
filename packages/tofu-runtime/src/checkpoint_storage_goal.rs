//! Whole Goal transaction transport. Only the selected host opens/commits SQL;
//! this synchronous inner entry owns policy and calls transaction-local IO.
use super::*;
use crate::checkpoint_goal_claim::{self as claim,ReadAuthority};
use crate::checkpoint_goal_context::{self as context,ReadAuthority as ContextAuthority};
use crate::checkpoint_goal_consume as consume;
use crate::checkpoint_goal_codec::{charge,text,read_error};
use journal::ActorHandoff;
use serde_json::json;
use std::io;

#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct GoalRequest<A=Value,B=Vec<ActorHandoff>> {
    format:String,operation:String,tenant_id:u64,owner_user_id:u64,
    header_bytes:usize,snapshot_bytes:usize,evidence_bytes:usize,
    authorization:A,actors:B,
}
fn bound(document:usize,identity:usize)->Result<usize,Error>{
    document.checked_mul(REQUEST_DOCUMENT_SLOTS)
        .and_then(|n|identity.checked_mul(REQUEST_IDENTITY_ESCAPE_SLOTS).and_then(|i|n.checked_add(i)))
        .and_then(|n|n.checked_add(ENVELOPE_BYTES)).ok_or(Error::Oversize)
}
impl CallbackStore {
    pub(super) fn goal_callback(&self,operation:&str,authorization:&Value,actors:&[ActorHandoff],evidence:usize)->Result<Value,Error>{
        let request=GoalRequest{format:FORMAT.into(),operation:operation.into(),tenant_id:self.tenant,owner_user_id:self.owner,
            header_bytes:self.header_bytes,snapshot_bytes:self.snapshot_bytes,evidence_bytes:evidence,authorization,actors};
        let response=self.send_bounded(&request,evidence,bound(evidence,self.identity_bytes)?)?;
        if response.version.is_some(){return Err(Error::Storage);}
        response.value.ok_or(Error::Storage)
    }
}
#[derive(Serialize)]
struct IoRequest<'a,T>{format:&'a str,operation:&'a str,tenant_id:u64,owner_user_id:u64,payload:T}
#[derive(Serialize)]
struct Put<'a>{task:&'a str,namespace:&'a str,expected:u64,value:&'a Value}
struct Transaction {store:CallbackStore,evidence:usize,request_bound:usize}
impl Transaction {
    fn io(&self,operation:&str,payload:impl Serialize)->Result<Value,Error>{
        let response=self.store.send_bounded(&IoRequest{format:FORMAT,operation,tenant_id:self.store.tenant,
            owner_user_id:self.store.owner,payload},self.evidence,self.request_bound)?;
        if response.version.is_some(){return Err(Error::Storage);}
        Ok(response.value.unwrap_or(Value::Null))
    }
    fn optional(&self,operation:&str,payload:Value)->Result<Option<Value>,Error>{
        let result=self.io(operation,payload)?;Ok(if result.is_null(){None}else{Some(result)})
    }
    fn put(&self,task:&str,namespace:&str,expected:u64,value:&Value)->Result<(),Error>{
        let maximum=if namespace=="header"{self.store.header_bytes}else{self.store.snapshot_bytes};
        self.store.task(self.store.owner,task)?;self.store.value(value,task,maximum)?;
        if self.io("goal.put",Put{task,namespace,expected,value})?!=true {
            return Err(Error::Storage);
        }Ok(())
    }
    fn append(&self,run:&str,label:&str,cursor:u64,value:&Value)->Result<(),Error>{
        if self.io("goal.append",json!({"run":run,"label":label,"cursor":cursor,"value":value}))?!=true{return Err(Error::Storage);}Ok(())
    }
}
fn io_error(error:Error)->io::Error{
    io::Error::new(match error{Error::Conflict=>io::ErrorKind::AlreadyExists,Error::Invalid|Error::Ineligible=>io::ErrorKind::InvalidData,
        Error::Oversize=>io::ErrorKind::OutOfMemory,_=>io::ErrorKind::Other},"Goal transaction observation refused")
}
impl ContextAuthority for Transaction {
    fn owner(&self)->u64{self.store.owner}
    fn event(&mut self,run:&str,label:&str,sequence:u64)->io::Result<Option<Value>>{
        self.optional("goal.event",json!({"run":run,"label":label,"sequence":sequence})).map_err(io_error)
    }
    fn run(&mut self,run:&str,label:&str)->io::Result<Option<(Value,Value)>>{
        self.optional("goal.run",json!({"run":run,"label":label})).and_then(|value|value.map(|v|serde_json::from_value(v).map_err(|_|Error::Storage)).transpose()).map_err(io_error)
    }
    fn attempt(&mut self,id:&str)->io::Result<Option<Value>>{self.optional("goal.attempt",json!({"id":id})).map_err(io_error)}
    fn turn(&mut self,conversation:&str,id:&str)->io::Result<Option<Value>>{self.optional("goal.turn",json!({"conversation":conversation,"id":id})).map_err(io_error)}
}
impl ReadAuthority for Transaction {
    fn record(&mut self,task:&str)->Result<Option<Value>,Error>{self.optional("goal.record",json!({"task":task}))}
    fn header(&mut self,task:&str)->Result<Option<Header>,Error>{Store::header(self,self.store.owner,task)}
    fn wallet(&mut self)->Result<Option<String>,Error>{self.optional("goal.wallet",json!({}))?.map(|v|v.as_str().map(str::to_owned).ok_or(Error::Storage)).transpose()}
    fn ledger(&mut self,wallet:&str,kind:&str,reference:&str,root:&str)->Result<Option<Value>,Error>{
        self.optional("goal.ledger",json!({"wallet":wallet,"kind":kind,"reference":reference,"root":root}))
    }
}
impl Store for Transaction {
    fn header(&self,owner:u64,task:&str)->Result<Option<Header>,Error>{
        self.store.task(owner,task)?;
        self.optional("goal.header",json!({"task":task}))?.map(|v|{
            let version=v["version"].as_u64().filter(|n|*n>0 && *n<=i64::MAX as u64).ok_or(Error::Storage)?;
            let value=v["value"].clone();self.store.value(&value,task,self.store.header_bytes)?;Ok(Header{version,value})
        }).transpose()
    }
    fn snapshot(&self,owner:u64,task:&str,version:u64)->Result<Option<Value>,Error>{
        self.store.task(owner,task)?;let value=self.optional("goal.snapshot",json!({"task":task,"version":version}))?;
        if let Some(value)=&value{self.store.value(value,task,self.store.snapshot_bytes)?;}Ok(value)
    }
    fn compare_exchange(&self,_:u64,_:&str,_:u64,_:&Value,_:SnapshotWrite<'_>)->Result<u64,Error>{Err(Error::Ineligible)}
    fn writer_quiescent(&self,owner:u64,task:&str,identity:&Value)->Result<bool,Error>{self.store.writer_quiescent(owner,task,identity)}
}
impl Transaction {
    fn claim(&mut self,request:&GoalRequest)->Result<Value,Error>{
        let (claim_request,digest)=claim::parse_request(&request.authorization,self.evidence)?;
        let q=&claim_request;let mut remaining=self.evidence;
        if let Some(event)=context::event(self,&q.source_run_id,&q.tenant_id,q.source_cursor,&mut remaining).map_err(read_error)?{
            for actor in claim::existing_actors(&event,q,&digest)? {
                let header=ReadAuthority::header(self,text(actor,"task_id")?)?.ok_or(Error::Conflict)?;
                charge(&header.value,&mut remaining)?;claim::validate_existing_header(&header,actor,q,&digest)?;
            }return Ok(event);
        }
        let context=context::context(self,&q.source_run_id,&q.target_attempt_id,&q.tenant_id,q.source_cursor,q.launch_sequence,q.account_sequence,None,&mut remaining).map_err(read_error)?;
        let mut prepared=claim::prepare_records(self,q,context,remaining)?;
        claim::collect_frontiers(&mut prepared,q,self.store.owner,|task|{
            journal::inspect_abandoned_frontier(self,self.store.owner,task,self.store.snapshot_bytes,self.store.header_bytes)?.ok_or(Error::Ineligible)
        })?;
        claim::finish(&prepared,q)?;
        let settlement=claim::settlement(self,&q.invoice)?;
        let mut actors=Vec::new();let mut headers=Vec::new();
        for (task,observed) in &prepared.pending {
            let header=ReadAuthority::header(self,task)?.ok_or(Error::Conflict)?;
            if !self.writer_quiescent(self.store.owner,task,&header.value["writer_process"])?{return Err(Error::Ineligible);}
            let retired=claim::retire_header(self.store.owner,task,&header,observed,q,&digest)?;
            encode(&retired,self.store.header_bytes)?;
            actors.push(json!({"task_id":task,"source_version":header.version,"version":header.version.checked_add(1).ok_or(Error::Invalid)?}));
            headers.push((task,header.version,retired));
        }
        let receipt=claim::receipt(q,&digest,&prepared.context,actors)?;encode(&receipt,self.evidence)?;
        for (task,version,header) in headers{self.put(task,"header",version,&header)?;}
        if let Some(intent)=settlement{if self.io("goal.settle",intent)?["applied"]!=true{return Err(Error::Conflict);}}
        self.append(&q.source_run_id,&q.tenant_id,q.source_cursor,&receipt)?;Ok(receipt)
    }
    fn consume(&mut self,request:&GoalRequest)->Result<Value,Error>{
        let (authorization,claim_request,digest)=consume::parse(&request.authorization,self.evidence)?;
        let mut remaining=self.evidence;charge(&request.authorization,&mut remaining)?;
        for actor in &request.actors{for value in [&actor.source_header,&actor.target_header,&actor.target_snapshot]{charge(value,&mut remaining)?;}}
        let context=consume::consumption_context(self,&authorization,&claim_request,&request.actors,&mut remaining)?;
        let root=text(&context["goal"]["accepted"],"taskId")?;
        if request.actors.len()!=authorization.actors.len(){return Err(Error::Invalid);}
        let mut retired=Vec::new();
        for (actor,binding) in request.actors.iter().zip(&authorization.actors){
            let target=&binding.target_task_id;
            if actor.source_task_id!=binding.source_task_id || actor.target_header["task_id"]!=*target
                || actor.source_root_task_id!=claim_request.invoice["task_id"] || actor.target_root_task_id!=root
                || actor.target_header.get("writer_process")!=self.store.writer.as_ref(){return Err(Error::Invalid);}
            if ReadAuthority::header(self,target)?.is_some() || self.io("goal.snapshot",json!({"task":target,"version":0}))?!=Value::Null {return Err(Error::Conflict);}
            let source=ReadAuthority::header(self,&actor.source_task_id)?.ok_or(Error::Ineligible)?;
            let snapshot=Store::snapshot(self,self.store.owner,&actor.source_task_id,source.version)?.ok_or(Error::Ineligible)?;
            let value=consume::transfer_header(self,self.store.owner,self.store.header_bytes,self.store.snapshot_bytes,self.evidence,
                actor,binding,&claim_request,&context,&digest,source,snapshot)?;
            encode(&value,self.store.header_bytes)?;encode(&actor.target_header,self.store.header_bytes)?;encode(&actor.target_snapshot,self.store.snapshot_bytes)?;
            retired.push(value);
        }
        let receipt=consume::consumption_receipt(&authorization,&claim_request,&digest,&context)?;encode(&receipt,self.evidence)?;
        for ((actor,binding),retired) in request.actors.iter().zip(&authorization.actors).zip(retired){
            self.put(&actor.source_task_id,"header",actor.source_version,&retired)?;
            self.put(&binding.target_task_id,"header",0,&actor.target_header)?;
            self.put(&binding.target_task_id,"snapshot",0,&actor.target_snapshot)?;
            if self.io("goal.delete_snapshot",json!({"task":actor.source_task_id}))?!=true{return Err(Error::Storage);}
        }
        let run=format!("goal_{root}");let mut cursor=authorization.target_cursor;
        for cut in &authorization.account_frontiers {
            self.append(&run,&claim_request.tenant_id,cursor,&json!({"type":"execution_account_frontier","launch_id":context["launch"]["launch_metadata"]["launch_id"],"frontier":cut}))?;
            cursor=cursor.checked_add(1).ok_or(Error::Invalid)?;
        }
        self.append(&run,&claim_request.tenant_id,cursor,&receipt)?;Ok(json!({}))
    }
}
pub(crate) fn execute(tenant:u64,owner:u64,document:usize,identity:usize,bytes:&[u8],callback:Callback,context:*mut c_void)->Result<Vec<u8>,Error>{
    let request:GoalRequest=serde_json::from_slice(bytes).map_err(|_|Error::Invalid)?;
    if owner==0 || request.format!=FORMAT || request.tenant_id!=tenant || request.owner_user_id!=owner
        || [request.header_bytes,request.snapshot_bytes,request.evidence_bytes].iter().any(|n|*n==0 || *n>document)
        || !matches!(request.operation.as_str(),"goal_claim"|"goal_handoff") || (request.operation=="goal_claim" && !request.actors.is_empty()) {return Err(Error::Invalid);}
    let mut tx=Transaction{store:CallbackStore{host:Arc::new(Host{callback,context}),tenant,owner,
        header_bytes:request.header_bytes,snapshot_bytes:request.snapshot_bytes,identity_bytes:identity,writer:crate::checkpoint_writer::capture()},
        evidence:request.evidence_bytes,request_bound:bound(document,identity)?};
    let result=if request.operation=="goal_claim"{tx.claim(&request)?}else{tx.consume(&request)?};
    encode(&result,document)
}
