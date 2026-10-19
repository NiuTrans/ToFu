//! Whole Goal recovery on the shared checkpoint capability registry.
//! Native transaction ownership stays in RuntimeStore/OwnedLease. Successful
//! response retries reuse identical capabilities until first member dispatch;
//! ambiguous storage failures never create an attachment or another journal.
use super::*;
use serde::Serialize;
use sha2::{Digest,Sha256};
use crate::checkpoint_journal::ActorHandoffSpec;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct GoalClaim {
    pub(super) owner_user_id:u64,
    pub(super) header_bytes:usize,
    pub(super) snapshot_bytes:usize,
    pub(super) evidence_bytes:usize,
    pub(super) request:Value,
}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct GoalHandoff {
    owner_user_id:u64,header_bytes:usize,snapshot_bytes:usize,identity_bytes:usize,
    pub(super) evidence_bytes:usize,
    source_root_task_id:String,target_root_task_id:String,
    actors:Vec<Actor>,authorization:Value,
}
#[derive(Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Actor {source_task_id:String,task_id:String,epoch:String,binding:String,token:String}

fn bounds(owner:u64,header:usize,snapshot:usize,evidence:usize,document_maximum_bytes:usize)->bool{
    owner>0 && [header,snapshot,evidence].into_iter().all(|bytes|bytes>0
        && bytes<=document_maximum_bytes)
}
impl GoalClaim {
    pub(super) fn valid(&self,document_maximum_bytes:usize)->bool{bounds(self.owner_user_id,self.header_bytes,self.snapshot_bytes,self.evidence_bytes,document_maximum_bytes)}
}
impl GoalHandoff {
    fn delivery_digest(&mut self) -> Result<String, Error> {
        // The standalone runtime also preserves provider JSON order. This
        // control metadata must retain the original BTreeMap identity instead.
        self.authorization.sort_all_objects();
        let encoded = serde_json::to_vec(self).map_err(|_| Error::Invalid)?;
        if encoded.len() > self.evidence_bytes { return Err(Error::Oversize); }
        Ok(format!("{:x}", Sha256::digest(&encoded)))
    }
    pub(super) fn valid(&self,document_maximum_bytes:usize)->bool{
        bounds(self.owner_user_id,self.header_bytes,self.snapshot_bytes,self.evidence_bytes,document_maximum_bytes)
            && self.identity_bytes>0 && !self.actors.is_empty()
    }
    pub(super) fn response_bound(&self)->io::Result<usize>{
        let row=usize::try_from(CONTRACT["limits"]["goalHandoffRowHeadroomBytes"].as_u64().ok_or_else(invalid)?).map_err(|_|invalid())?;
        self.actors.iter().try_fold(headroom(),|sum,actor|{
            let identity=serde_json::to_vec(&actor.task_id).map_err(|_|invalid())?.len();
            sum.checked_add(row).and_then(|bytes|bytes.checked_add(identity)).ok_or_else(invalid)
        })
    }
    fn response(&self,ids:&[u64])->Value{
        json!({"leases":self.actors.iter().zip(ids).map(|(actor,id)|json!({
            "task_id":actor.task_id,"capability_id":id})).collect::<Vec<_>>()})
    }
}
impl Registry {
    pub(super) fn execute_goal_handoff<R: CheckpointControlRuntime>(&self,runtime:&R,mut query:GoalHandoff)->Result<Value,Error>{
        runtime.checkpoint_owner_open(query.owner_user_id)?;
        let digest = query.delivery_digest()?;
        let ids={
            let mut entries=self.entries.lock().map_err(|_|Error::Storage)?;
            if let Some(ids)=entries.groups.get(&digest){
                if ids.len()!=query.actors.len() || ids.iter().any(|id|!entries.live.contains_key(id)){return Err(Error::Conflict);}
                return Ok(query.response(ids));
            }
            let count=u64::try_from(query.actors.len()).map_err(|_|Error::Oversize)?;
            let end=entries.next.checked_add(count).ok_or(Error::Oversize)?;
            let mut ids=Vec::new();ids.try_reserve_exact(query.actors.len()).map_err(|_|Error::Oversize)?;
            ids.extend(entries.next+1..=end);entries.next=end;ids
        };
        let specs=query.actors.iter().map(|actor|ActorHandoffSpec {
            scope:Scope {owner:query.owner_user_id,task_id:actor.task_id.clone(),epoch:actor.epoch.clone(),binding:actor.binding.clone()},
            source_task_id:actor.source_task_id.clone(),source_root_task_id:query.source_root_task_id.clone(),
            target_root_task_id:query.target_root_task_id.clone(),token:actor.token.clone(),
        }).collect();
        // No registry lock spans storage/WAL waits. Failed/unknown storage
        // delivery leaves no capability; a subsequent attempt cannot attach.
        let leases=runtime.checkpoint_goal_handoff(query.owner_user_id,query.header_bytes,query.snapshot_bytes,
            query.identity_bytes,specs,&query.authorization,query.evidence_bytes)?;
        if leases.len()!=ids.len(){return Err(Error::Invalid);}
        let response=query.response(&ids);
        let group:Arc<str>=Arc::from(digest.as_str());
        let mut entries=self.entries.lock().map_err(|_|Error::Storage)?;
        for (id,lease) in ids.iter().zip(leases){
            entries.live.insert(*id,Arc::new(Entry {lease:Arc::new(lease),
                response_bytes:query.snapshot_bytes+headroom(),finish:Mutex::new(None),group_digest:Some(group.clone())}));
        }
        entries.groups.insert(digest,ids);
        Ok(response)
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delivery_identity_keeps_native_map_order_with_provider_order_enabled() {
        let make = |authorization: &str| -> GoalHandoff {
            serde_json::from_value(json!({"owner_user_id":7,"header_bytes":4096,
                "snapshot_bytes":4096,"identity_bytes":1024,"evidence_bytes":8192,
                "source_root_task_id":"old","target_root_task_id":"new",
                "actors":[{"source_task_id":"old-child","task_id":"new-child",
                    "epoch":"epoch","binding":"binding","token":"token"}],
                "authorization":serde_json::from_str::<Value>(authorization).unwrap()})).unwrap()
        };
        let mut ordered = make(r#"{"a":1,"z":{"a":1,"b":2}}"#);
        let mut shuffled = make(r#"{"z":{"b":2,"a":1},"a":1}"#);
        let original_native = format!("{:x}", Sha256::digest(serde_json::to_vec(&ordered).unwrap()));
        assert_eq!(ordered.delivery_digest().unwrap(), original_native);
        assert_eq!(shuffled.delivery_digest().unwrap(), original_native);
        shuffled.evidence_bytes = 1;
        assert_eq!(shuffled.delivery_digest().unwrap_err(), Error::Oversize);
    }
}
