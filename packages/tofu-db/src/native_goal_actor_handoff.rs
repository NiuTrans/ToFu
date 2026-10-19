//! Consume one whole native Goal claim with all successor journals/account cuts.
//! Existing Goal lineage, registered births, Journal transformations and billing
//! admissions remain the authorities. No individual actor may commit separately.
use super::*;
use tofu_runtime::checkpoint_journal::{ActorHandoff,Header};
#[path = "native_goal_actor_handoff_replay.rs"]
mod publication;
pub(in super::super) const OPERATION:&str="native_checkpoint.goal_actor_handoff";
use publication::Replay;

use tofu_runtime::checkpoint_goal_consume::{self as shared_consume,Authorization,parse,consumption_receipt};

fn charge_actor(actor:&ActorHandoff,remaining:&mut usize)->Result<(),Error>{
    for value in [&actor.source_header,&actor.target_header,&actor.target_snapshot] {
        *remaining=remaining.checked_sub(encode(value,*remaining)?.len()).ok_or(Error::Oversize)?;
    }
    Ok(())
}

impl NativeCheckpointStore {
    fn consumption_context(&self,database:&AuthorityDatabase,tx:&mut AuthorityTransaction,
        authorization:&Authorization,request:&Request,actors:&[ActorHandoff],remaining:&mut usize)->Result<Value,Error>{
        shared_consume::consumption_context(&mut Reads{store:self,reads:goal::Reads{database,transaction:tx}},
            authorization,request,actors,remaining)
    }

    pub(in super::super) fn consume_goal_actors(&self,actors:&[ActorHandoff],payload:&Value,evidence_bytes:usize)->Result<(),Error>{
        self.commit_goal_actor_handoff(actors,payload,evidence_bytes,None)
    }

    fn commit_goal_actor_handoff(&self,actors:&[ActorHandoff],payload:&Value,evidence_bytes:usize,proof:Option<&Replay>)->Result<(),Error>{
        let (authorization,request,digest)=parse(payload,evidence_bytes)?;
        let mut database=self.authority.lock().map_err(|_|Error::Storage)?;
        if proof.is_some() && database.logical_outbox_is_configured(){return Err(Error::Ineligible);}
        let mut tx=database.begin_with_identity_claim_scopes(self.tenant,self.owner).map_err(map_io)?;
        let mut remaining=evidence_bytes.checked_sub(encode(payload,evidence_bytes)?.len()).ok_or(Error::Oversize)?;
        for actor in actors {charge_actor(actor,&mut remaining)?;}
        let context=self.consumption_context(&database,&mut tx,&authorization,&request,actors,&mut remaining)?;
        if let Some(proof)=proof {
            if replay::digest(&context,evidence_bytes)?!=proof.context_sha256{return Err(Error::Conflict);}
        }
        let target_root=text(&context["goal"]["accepted"],"taskId")?;
        if actors.len()!=authorization.actors.len(){return Err(Error::Invalid);}
        let now=if let Some(proof)=proof {proof.now} else {u64::try_from(SystemTime::now().duration_since(UNIX_EPOCH)
            .map_err(|_|Error::Storage)?.as_millis()).map_err(|_|Error::Storage)?.max(1)};
        for (actor,binding) in actors.iter().zip(&authorization.actors) {
            let target=&binding.target_task_id;
            if actor.source_task_id!=binding.source_task_id || actor.target_header["task_id"]!=*target
                || actor.source_root_task_id!=request.invoice["task_id"] || actor.target_root_task_id!=target_root
                || (proof.is_none() && actor.target_header.get("writer_process")!=self.writer_process.as_ref()){return Err(Error::Invalid);}
            for task in [&actor.source_task_id,target] {
                if task==&actor.source_task_id && proof.is_some(){
                    self.check_handoff_replay_prefix(&database,&mut tx,task,actor.source_version,proof.unwrap())?;
                }else if self.handoff_document(&database,&mut tx,REPLAY,task,self.header_bytes)?.is_some(){return Err(Error::Ineligible);}
            }
            if self.handoff_document(&database,&mut tx,HEADERS,target,self.header_bytes)?.is_some()
                || self.handoff_document(&database,&mut tx,SNAPSHOTS,target,self.snapshot_bytes)?.is_some(){return Err(Error::Conflict);}
            let (header,version)=self.handoff_document(&database,&mut tx,HEADERS,&actor.source_task_id,self.header_bytes)?.ok_or(Error::Ineligible)?;
            let (snapshot,_)=self.handoff_document(&database,&mut tx,SNAPSHOTS,&actor.source_task_id,self.snapshot_bytes)?.ok_or(Error::Ineligible)?;
            let retired=shared_consume::transfer_header(self,self.owner,self.header_bytes,self.snapshot_bytes,evidence_bytes,
                actor,binding,&request,&context,&digest,Header{value:header,version},snapshot)?;
            for (namespace,task,value,expected,maximum) in [(HEADERS,actor.source_task_id.as_str(),&retired,version,self.header_bytes),
                (HEADERS,target.as_str(),&actor.target_header,0,self.header_bytes),(SNAPSHOTS,target.as_str(),&actor.target_snapshot,0,self.snapshot_bytes)] {
                let key=versioned_document::record_key(self.tenant,self.owner,namespace,task).map_err(map_io)?;
                versioned_document::put(&database,&mut tx,PutRequest {key,namespace:namespace.into(),logical_key:task.into(),
                    value_json:encode(value,maximum)?,expected_version:Some(expected),updated_at_ms:now}).map_err(map_io)?;
            }
            let key=versioned_document::record_key(self.tenant,self.owner,SNAPSHOTS,&actor.source_task_id).map_err(map_io)?;
            versioned_document::delete(&database,&mut tx,key,SNAPSHOTS,&actor.source_task_id,None).map_err(map_io)?;
            if let Some(proof)=proof {
                self.write_handoff_replay_witness(&database,&mut tx,&actor.source_task_id,version.checked_add(1).ok_or(Error::Invalid)?,proof)?;
                self.write_handoff_replay_witness(&database,&mut tx,target,1,proof)?;
            }
        }
        let run=format!("goal_{target_root}");let mut cursor=authorization.target_cursor;
        for cut in &authorization.account_frontiers {
            goal::append_claim(&database,&mut tx,&run,&request.tenant_id,cursor,
                json!({"type":"execution_account_frontier","launch_id":context["launch"]["launch_metadata"]["launch_id"],"frontier":cut}),now).map_err(map_io)?;
            cursor=cursor.checked_add(1).ok_or(Error::Invalid)?;
        }
        goal::append_claim(&database,&mut tx,&run,&request.tenant_id,cursor,consumption_receipt(&authorization,&request,&digest,&context)?,now).map_err(map_io)?;
        if database.logical_outbox_is_configured(){self.capture_goal_actor_handoff(&database,&mut tx,actors,payload,&context,now,evidence_bytes)?;}
        database.commit(tx).map_err(map_io)?;durable(database,())
    }
}
