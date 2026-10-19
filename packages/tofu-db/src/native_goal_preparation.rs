//! Prepared invocation witnesses consumed by an atomic native Goal handoff.
//! The retired producer header authenticates the exact correlation digest;
//! ordinary appended events alone cannot manufacture recovery admission.
use super::*;


impl NativeCheckpointStore {
    pub(super) fn incoming_goal_actor_witness(&self,database:&AuthorityDatabase,tx:&mut AuthorityTransaction,
        request:&Request,event:&Value,sequence:u64,remaining:&mut usize)->Result<Value,Error>{
        shared_claim::incoming_witness(&mut Reads{store:self,reads:goal::Reads{database,transaction:tx}},
            request,event,sequence,remaining)

    }
}
