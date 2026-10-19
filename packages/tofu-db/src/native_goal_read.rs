//! Scoped native transaction reads for shared whole-Goal validation.
//! Byte/identity checks remain at native document owners; no mutation or policy.
use super::*;
use tofu_runtime::checkpoint_goal_context::ReadAuthority as ContextAuthority;
use tofu_runtime::checkpoint_goal_claim::ReadAuthority as ClaimAuthority;
use tofu_runtime::checkpoint_journal::Header;
pub(super) struct Reads<'a>{pub store:&'a NativeCheckpointStore,pub reads:goal::Reads<'a>}
impl ContextAuthority for Reads<'_>{
    fn owner(&self)->u64{self.store.owner}
    fn event(&mut self,run:&str,label:&str,sequence:u64)->io::Result<Option<Value>>{self.reads.event(run,label,sequence)}
    fn run(&mut self,run:&str,label:&str)->io::Result<Option<(Value,Value)>>{self.reads.run(run,label)}
    fn attempt(&mut self,id:&str)->io::Result<Option<Value>>{self.reads.attempt(id)}
    fn turn(&mut self,conversation:&str,id:&str)->io::Result<Option<Value>>{self.reads.turn(conversation,id)}
}
impl ClaimAuthority for Reads<'_>{
    fn record(&mut self,task:&str)->Result<Option<Value>,Error>{
        crate::task_result::record_get(self.reads.database,self.reads.transaction,task,self.store.owner)
            .map_err(map_io)?.map(|bytes|decode(&bytes)).transpose()
    }
    fn header(&mut self,task:&str)->Result<Option<Header>,Error>{
        self.store.handoff_document(self.reads.database,self.reads.transaction,HEADERS,task,self.store.header_bytes)
            .map(|value|value.map(|(value,version)|Header{value,version}))
    }
    fn wallet(&mut self)->Result<Option<String>,Error>{
        crate::tenant_user::account_id_for_owner(self.reads.database,self.reads.transaction,self.store.owner).map_err(map_io)
    }
    fn ledger(&mut self,wallet:&str,kind:&str,reference:&str,root:&str)->Result<Option<Value>,Error>{
        crate::billing::ledger_find(self.reads.database,self.reads.transaction,wallet,kind,reference,root)
            .map_err(map_io)?.map(|bytes|decode(&bytes)).transpose()
    }
}
