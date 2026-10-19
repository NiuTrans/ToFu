//! Authenticated whole-actor handoff publication on the existing native outbox.
//! Targets are reconstructed from exact retained source prefixes; no paid
//! snapshot is duplicated in the record and passive replay grants no lease.
use super::*;
use tofu_runtime::checkpoint_journal::{ActorHandoffSpec,Journal,Scope};

static SCHEMA:LazyLock<Value>=LazyLock::new(||serde_json::from_str(include_str!(
    "../../../contracts/native_goal_actor_handoff_outbox_v1.schema.json")).expect("actor handoff outbox contract"));
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Mutation {format:String,source_uuid:String,authorization:Value,actors:Vec<Actor>,context_sha256:String,updated_at_ms:u64}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Actor {source_task_id:String,source_version:u64,target_header:Value}
pub(super) struct Replay {pub(super) context_sha256:String,pub(super) now:u64,
    source_uuid:String,archive:Option<uuid::Uuid>,digest:String}
fn archive_matches(receipt:&Value,archive:Option<uuid::Uuid>)->bool{
    match archive {Some(id)=>receipt["archive_uuid"].as_str()==Some(id.to_string().as_str()),None=>receipt.get("archive_uuid").is_none()}
}

impl NativeCheckpointStore {
    pub(super) fn capture_goal_actor_handoff(&self,db:&AuthorityDatabase,tx:&mut AuthorityTransaction,
        actors:&[ActorHandoff],authorization:&Value,context:&Value,now:u64,maximum:usize)->Result<(),Error>{
        let mutation=Mutation {format:SCHEMA["$id"].as_str().unwrap().into(),source_uuid:db.authority_uuid().to_string(),
            authorization:authorization.clone(),actors:actors.iter().map(|actor|Actor {source_task_id:actor.source_task_id.clone(),
                source_version:actor.source_version,target_header:actor.target_header.clone()}).collect(),
            context_sha256:replay::digest(context,maximum)?,updated_at_ms:now};
        let bytes=encode(&mutation,maximum.min(MAX_LOGICAL_PAYLOAD_BYTES))?;
        db.logical_outbox_capture(tx,LogicalOutboxCapture {schema_version:crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
            registry_version:crate::generated_storage_operations::STORAGE_REGISTRY_VERSION,operation:OPERATION.into(),
            request_id:uuid::Uuid::new_v4().to_string(),request_digest:Sha256::digest(&bytes).into(),command_id:None,
            clear_payload:bytes,committed_at_ms:now}).map_err(map_io)?;Ok(())
    }
    pub(super) fn check_handoff_replay_prefix(&self,db:&AuthorityDatabase,tx:&mut AuthorityTransaction,
        task:&str,version:u64,proof:&Replay)->Result<(),Error>{
        let (receipt,_)=self.handoff_document(db,tx,REPLAY,task,self.header_bytes)?.ok_or(Error::Conflict)?;
        if receipt["source_uuid"]!=proof.source_uuid || receipt["version"].as_u64()!=Some(version)
            || !archive_matches(&receipt,proof.archive){return Err(Error::Conflict);}Ok(())
    }
    pub(super) fn write_handoff_replay_witness(&self,db:&AuthorityDatabase,tx:&mut AuthorityTransaction,
        task:&str,version:u64,proof:&Replay)->Result<(),Error>{
        let mut receipt=json!({"source_uuid":proof.source_uuid,"version":version,"digest":proof.digest});
        if let Some(archive)=proof.archive{receipt["archive_uuid"]=json!(archive.to_string());}
        let key=versioned_document::record_key(self.tenant,self.owner,REPLAY,task).map_err(map_io)?;
        versioned_document::put(db,tx,PutRequest {key,namespace:REPLAY.into(),logical_key:task.into(),value_json:encode(&receipt,self.header_bytes)?,
            expected_version:None,updated_at_ms:proof.now}).map_err(map_io)?;Ok(())
    }
    fn already_replayed_handoff(&self,mutation:&Mutation,proof:&Replay,verified_old:bool,maximum:usize)->Result<bool,Error>{
        let (authorization,request,digest)=parse(&mutation.authorization,maximum)?;
        let db=self.authority.lock().map_err(|_|Error::Storage)?;
        if db.logical_outbox_is_configured(){return Err(Error::Ineligible);}
        let mut tx=db.begin_with_identity_claim_scopes(self.tenant,self.owner).map_err(map_io)?;
        let mut remaining=maximum;
        let target_root=mutation.actors.first().and_then(|actor|actor.target_header["task_id"].as_str()).ok_or(Error::Invalid)?;
        // The registered target's parent is authoritative even on an old replay;
        // never infer it from a caller-supplied run name.
        let record=decode(&crate::task_result::record_get(&db,&mut tx,target_root,self.owner).map_err(map_io)?.ok_or(Error::Ineligible)?)?;
        remaining=remaining.checked_sub(encode(&record,remaining)?.len()).ok_or(Error::Oversize)?;
        let root=record["value"]["child_invocation"]["parent_task_id"].as_str().ok_or(Error::Invalid)?;
        let sequence=authorization.target_cursor.checked_add(authorization.account_frontiers.len() as u64).ok_or(Error::Invalid)?;
        let event=goal::event(&db,&mut tx,&format!("goal_{root}"),&request.tenant_id,sequence,&mut remaining).map_err(map_io)?;
        let Some(event)=event else{drop(tx);return durable(db,false);};
        let mut base=event.clone();let rows=base["actors"].as_array_mut().ok_or(Error::Invalid)?;
        for row in rows {let value=row.as_object_mut().ok_or(Error::Invalid)?;value.remove("preparation");value.remove("effect_origin");}
        if base!=consumption_receipt(&authorization,&request,&digest,&json!({}))?
            || mutation.actors.len()!=authorization.actors.len(){return Err(Error::Conflict);}
        for ((actor,binding),witness) in mutation.actors.iter().zip(&authorization.actors).zip(event["actors"].as_array().ok_or(Error::Invalid)?){
            if actor.source_task_id!=binding.source_task_id || actor.target_header["task_id"]!=binding.target_task_id{return Err(Error::Invalid);}
            let mut handoff=json!({"target_task_id":binding.target_task_id,"target_root_task_id":root,"request_sha256":digest});
            if witness.get("preparation").is_some(){handoff["witness_sha256"]=json!(replay::digest(witness,remaining)?);}
            else if witness.get("effect_origin").is_some(){return Err(Error::Invalid);}
            for (task,created) in [(&actor.source_task_id,actor.source_version.checked_add(1).ok_or(Error::Invalid)?),(&binding.target_task_id,1)]{
                let (header,version)=self.handoff_document(&db,&mut tx,HEADERS,task,self.header_bytes)?.ok_or(Error::Conflict)?;
                self.check_handoff_replay_prefix(&db,&mut tx,task,version,proof)?;
                let (receipt,_)=self.handoff_document(&db,&mut tx,REPLAY,task,self.header_bytes)?.ok_or(Error::Conflict)?;
                if version<created || (version>created&&!verified_old) || (version==created&&receipt["digest"]!=proof.digest)
                    || (task==&actor.source_task_id && header["goal_actor_handoff"]!=handoff){return Err(Error::Conflict);}
                remaining=remaining.checked_sub(encode(&header,remaining)?.len()+encode(&receipt,remaining)?.len()).ok_or(Error::Oversize)?;
            }
        }
        drop(tx);durable(db,true)
    }
    pub(in super::super::super) fn replay_goal_handoff_clear(&self,clear:&[u8],source_uuid:&str,record:&SealedLogicalOutboxRecord,
        archive:Option<uuid::Uuid>,verified_old:bool,maximum:usize)->Result<u64,Error>{
        if maximum==0 || clear.len()>maximum{return Err(Error::Oversize);}
        let mutation:Mutation=serde_json::from_slice(clear).map_err(|_|Error::Invalid)?;
        if mutation.format!=SCHEMA["$id"] || mutation.source_uuid!=source_uuid || uuid::Uuid::parse_str(source_uuid).is_err()
            || mutation.updated_at_ms==0 || mutation.updated_at_ms!=record.identity.committed_at_ms{return Err(Error::Invalid);}
        let proof=Replay {source_uuid:source_uuid.into(),archive,digest:record.identity.request_digest.iter().map(|v|format!("{v:02x}")).collect(),
            context_sha256:mutation.context_sha256.clone(),now:mutation.updated_at_ms};
        if self.already_replayed_handoff(&mutation,&proof,verified_old,maximum)?{return Ok(1);}
        let (authorization,request,_)=parse(&mutation.authorization,maximum)?;
        if mutation.actors.len()!=authorization.actors.len(){return Err(Error::Invalid);}
        let mut actors=Vec::new();let mut remaining=maximum-clear.len();
        {
            let db=self.authority.lock().map_err(|_|Error::Storage)?;
            if db.logical_outbox_is_configured(){return Err(Error::Ineligible);}
            let mut tx=db.begin_with_identity_claim_scopes(self.tenant,self.owner).map_err(map_io)?;
            for (actor,binding) in mutation.actors.iter().zip(&authorization.actors){
                if actor.source_task_id!=binding.source_task_id || actor.target_header["task_id"]!=binding.target_task_id{return Err(Error::Invalid);}
                self.check_handoff_replay_prefix(&db,&mut tx,&actor.source_task_id,actor.source_version,&proof)?;
                let (header,version)=self.handoff_document(&db,&mut tx,HEADERS,&actor.source_task_id,self.header_bytes)?.ok_or(Error::Conflict)?;
                if version!=actor.source_version{return Err(Error::Conflict);}
                let (snapshot,_)=self.handoff_document(&db,&mut tx,SNAPSHOTS,&actor.source_task_id,self.snapshot_bytes)?.ok_or(Error::Conflict)?;
                let target_record=decode(&crate::task_result::record_get(&db,&mut tx,&binding.target_task_id,self.owner).map_err(map_io)?.ok_or(Error::Ineligible)?)?;
                remaining=remaining.checked_sub(encode(&target_record,remaining)?.len()).ok_or(Error::Oversize)?;
                let scope=Scope {owner:self.owner,task_id:binding.target_task_id.clone(),epoch:header["epoch"].as_str().ok_or(Error::Invalid)?.into(),
                    binding:header["binding"].as_str().ok_or(Error::Invalid)?.into()};
                let spec=ActorHandoffSpec {scope:scope.clone(),source_task_id:actor.source_task_id.clone(),
                    source_root_task_id:request.invoice["task_id"].as_str().ok_or(Error::Invalid)?.into(),
                    target_root_task_id:target_record["value"]["child_invocation"]["parent_task_id"].as_str().ok_or(Error::Invalid)?.into(),
                    token:actor.target_header["lease"].as_str().ok_or(Error::Invalid)?.into()};
                let prepared=Journal::new(scope,self,self.snapshot_bytes,self.header_bytes)?.prepare_retired_actor(&spec,
                    Header {version,value:header},snapshot,actor.target_header.get("writer_process").cloned())?;
                if prepared.target_header!=actor.target_header{return Err(Error::Invalid);}
                charge_actor(&prepared,&mut remaining)?;actors.push(prepared);
            }
        }
        self.commit_goal_actor_handoff(&actors,&mutation.authorization,maximum,Some(&proof))?;Ok(1)
    }
}
