//! Whole retired-actor transfer through the existing owned Journal/Lease owner.
//! Preparation preserves paid history, original payers and limits. The supplied
//! Store atomically validates domain authority/account cuts and creates every
//! target before any lease is returned. No retry attachment or second journal.
use super::*;
use super::super::Header;
use serde_json::json;
use std::collections::BTreeSet;

pub struct ActorHandoffSpec {
    pub scope: Scope,
    pub source_task_id: String,
    pub source_root_task_id: String,
    pub target_root_task_id: String,
    pub token: String,
}

#[derive(serde::Serialize,serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActorHandoff {
    pub source_task_id: String,
    pub source_root_task_id: String,
    pub target_root_task_id: String,
    pub source_version: u64,
    pub source_header: Value,
    pub target_header: Value,
    pub target_snapshot: Value,
}

fn charge(value: &Value, remaining: &mut usize) -> Result<(), Error> {
    struct Counter<'a>(&'a mut usize);
    impl std::io::Write for Counter<'_> {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            *self.0=self.0.checked_sub(bytes.len()).ok_or_else(|| std::io::Error::other("actor handoff evidence bound"))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
    }
    serde_json::to_writer(Counter(remaining),value).map_err(|_| Error::Oversize)
}

impl Journal<'_> {
    /// Pure transformation: no storage access or recovery authority. Passive
    /// replay must separately authenticate the supplied original writer bytes.
    pub fn prepare_retired_actor(&self, spec: &ActorHandoffSpec, source: Header,
        mut snapshot: Value, writer: Option<Value>) -> Result<ActorHandoff, Error> {
        if self.scope.owner != spec.scope.owner || self.scope.task_id != spec.scope.task_id
            || self.scope.epoch != spec.scope.epoch || self.scope.binding != spec.scope.binding
            || spec.token.is_empty() || spec.token.len() > self.identity_bytes
            || [&spec.source_task_id,&spec.source_root_task_id,&spec.target_root_task_id]
                .iter().any(|value| value.is_empty() || value.len() > self.identity_bytes)
            || spec.source_task_id == spec.scope.task_id || spec.source_root_task_id == spec.target_root_task_id
            || spec.source_task_id == spec.source_root_task_id || spec.scope.task_id == spec.target_root_task_id {
            return Err(Error::Invalid);
        }
        let original=Journal::new(Scope {task_id:spec.source_task_id.clone(),..spec.scope.clone()},
            self.store,self.snapshot_bytes,self.identity_bytes)?;
        original.validate_header(&source)?;
        if source.value["phase"] != "terminal" || !source.value["lease"].is_null()
            || source.value.get("goal_actor_handoff").is_some()
            || source.value.get("wallet_settlement").is_some()
            || source.value["goal_actor_claim"]["root_task_id"] != spec.source_root_task_id {
            return Err(Error::Ineligible);
        }
        let pending=snapshot.get("tools_pending")==Some(&Value::Bool(true));
        let frontier=source.value["next_round"].as_u64().ok_or(Error::Invalid)?;
        original.validate_frontier(&snapshot,frontier,pending)?;
        let account=&snapshot["shared_model_account"];
        if account["root_task_id"] != spec.source_root_task_id || account["actor_task_id"] != spec.source_task_id
            || account["owner_user_id"].as_u64()!=Some(spec.scope.owner) || account["complete"] != true {
            return Err(Error::Invalid);
        }
        snapshot["task_id"]=json!(spec.scope.task_id);
        snapshot["shared_model_account"]["root_task_id"]=json!(spec.target_root_task_id);
        snapshot["shared_model_account"]["actor_task_id"]=json!(spec.scope.task_id);
        let fees=snapshot.get_mut("host_fee_projection").ok_or(Error::Invalid)?;
        if fees["task_id"] != spec.source_task_id || fees["wallet_user_id"] != ""
            || fees["reserved_micro"] != 0 || fees.get("wallet_settlement").is_some() { return Err(Error::Invalid); }
        // Original producer and billable_task_id stay on every frozen round.
        fees["task_id"]=json!(spec.scope.task_id);
        self.validate_frontier(&snapshot,frontier,pending)?;
        let mut target=source.value.clone();
        target.as_object_mut().ok_or(Error::Invalid)?.remove("goal_actor_claim");
        target["task_id"]=json!(spec.scope.task_id);
        target["phase"]=json!(if pending {"tools_pending"}else{"completed"});
        target["lease"]=json!(spec.token);target["lease_generation"]=json!(1);
        if let Some(writer)=writer {target["writer_process"]=writer;}
        else {target.as_object_mut().ok_or(Error::Invalid)?.remove("writer_process");}
        self.validate_header(&Header {version:1,value:target.clone()})?;
        Ok(ActorHandoff {source_task_id:spec.source_task_id.clone(),source_root_task_id:spec.source_root_task_id.clone(),
            target_root_task_id:spec.target_root_task_id.clone(),source_version:source.version,
            source_header:source.value,target_header:target,target_snapshot:snapshot})
    }
}

impl OwnedLease {
    pub fn handoff_retired_actors(store: Arc<dyn Store>, specs: Vec<ActorHandoffSpec>,
        authorization: &Value, snapshot_bytes: usize, identity_bytes: usize, evidence_bytes: usize) -> Result<Vec<Self>, Error> {
        if specs.is_empty() || evidence_bytes == 0 {return Err(Error::Invalid);}
        let mut remaining=evidence_bytes;charge(authorization,&mut remaining)?;
        let mut sources=BTreeSet::new();let mut targets=BTreeSet::new();let mut actors=Vec::new();
        let first=&specs[0];
        for spec in &specs {
            if spec.scope.owner != first.scope.owner || spec.source_root_task_id != first.source_root_task_id
                || spec.target_root_task_id != first.target_root_task_id || !sources.insert(spec.source_task_id.clone())
                || !targets.insert(spec.scope.task_id.clone()) {return Err(Error::Invalid);}
        }
        if !sources.is_disjoint(&targets) {return Err(Error::Invalid);}
        for spec in &specs {
            let journal=Journal::new(spec.scope.clone(),store.as_ref(),snapshot_bytes,identity_bytes)?;
            if journal.read()?.is_some() {return Err(Error::Conflict);}
            let source=store.header(spec.scope.owner,&spec.source_task_id)?.ok_or(Error::Ineligible)?;
            let snapshot=store.snapshot(spec.scope.owner,&spec.source_task_id,source.version)?.ok_or(Error::Ineligible)?;
            let prepared=journal.prepare_retired_actor(spec,source,snapshot,store.writer_process())?;
            charge(&prepared.source_header,&mut remaining)?;charge(&prepared.target_header,&mut remaining)?;
            charge(&prepared.target_snapshot,&mut remaining)?;actors.push(prepared);
        }
        store.handoff_retired_actors(&actors,authorization,evidence_bytes)?;
        Ok(specs.into_iter().map(|spec| Self {scope:spec.scope,store:Arc::clone(&store),snapshot_bytes,identity_bytes,
            state:Arc::new(LeaseState {token:spec.token,poisoned:AtomicBool::new(false),loading:AtomicBool::new(false),
                ready:AtomicBool::new(false),generation:1})}).collect())
    }
}
