//! Validate the exact Python-produced account cuts accompanying whole actor handoff.
//! No spend is added or refunded; only the previously paid live actors move to
//! pristine recipients. The storage owner binds these values to retained events.
use super::actor_fees::settled_cut_shape;
use super::Error;
use serde_json::{json,Value};
use std::collections::BTreeSet;

pub fn validate_actor_handoff_accounts(source: &Value, before: &Value, cuts: &[Value],
    bindings: &[(&str,&str)], target_root: &str) -> Result<(),Error> {
    settled_cut_shape(source)?;settled_cut_shape(before)?;
    let snapshot=&source["snapshot"];let source_root=snapshot["root_task_id"].as_str().ok_or(Error::Invalid)?;
    if bindings.is_empty() || bindings.len()!=cuts.len() || target_root.is_empty() || target_root==source_root
        || snapshot["complete"]!=false || snapshot["settled"]!=true || snapshot["usage_known"]!=true
        || snapshot["root_tokens"]!=0 || source["model_requests"]!=json!([])
        || source.get("recovery").is_some_and(|value| value["pending_actors"]
            .as_object().is_none_or(|rows| !rows.is_empty())) {return Err(Error::Ineligible);}
    let mut pending=source["actors"].as_object().ok_or(Error::Invalid)?.clone();
    pending.remove(source_root).ok_or(Error::Invalid)?;
    let source_ids:BTreeSet<_>=bindings.iter().map(|(old,_)|*old).collect();
    let target_ids:BTreeSet<_>=bindings.iter().map(|(_,next)|*next).collect();
    if source_ids.len()!=bindings.len() || target_ids.len()!=bindings.len() || !source_ids.is_disjoint(&target_ids)
        || pending.keys().map(String::as_str).collect::<BTreeSet<_>>()!=source_ids
        || target_ids.contains(target_root) || target_ids.contains(source_root) {return Err(Error::Invalid);}
    let sum=|key:&str|pending.values().try_fold(0_u64,|sum,row|sum.checked_add(row[key].as_u64().ok_or(Error::Invalid)?).ok_or(Error::Invalid));
    let tokens=snapshot["tokens"].as_u64().ok_or(Error::Invalid)?.checked_sub(sum("tokens")?).ok_or(Error::Invalid)?;
    let tools=snapshot["tool_calls"].as_u64().ok_or(Error::Invalid)?.checked_sub(sum("tool_calls")?).ok_or(Error::Invalid)?;
    let mut actors=serde_json::Map::new();
    actors.insert(target_root.into(),json!({"parent_task_id":"","tokens":0,"tool_calls":tools,"model_ordinal":0,"tool_ordinal":0}));
    for target in target_ids {actors.insert(target.into(),json!({"parent_task_id":target_root,"tokens":0,"tool_calls":0,"model_ordinal":0,"tool_ordinal":0}));}
    let mut restored=snapshot.clone();restored["root_task_id"]=json!(target_root);
    let mut expected=json!({"format":source["format"],"version":before["version"],"reason":"attached",
        "snapshot":restored,"actors":actors,"child_ordinal":bindings.len(),"closed_tokens":tokens,"closed_tool_calls":0,
        "model_requests":[],"recovery":{"source_root_task_id":source_root,"source_frontier_version":source["version"],"pending_actors":pending}});
    if before!=&expected {return Err(Error::Conflict);}
    for ((source,target),cut) in bindings.iter().zip(cuts) {
        let mut actor=expected["recovery"]["pending_actors"].as_object_mut().ok_or(Error::Invalid)?
            .remove(*source).ok_or(Error::Invalid)?;
        actor["parent_task_id"]=json!(target_root);expected["actors"][*target]=actor;
        expected["version"]=json!(expected["version"].as_u64().ok_or(Error::Invalid)?.checked_add(1).ok_or(Error::Invalid)?);
        expected["reason"]=json!("actor_rebound");expected["actor_rebinding"]=json!({"source_task_id":source,"target_task_id":target});
        if &expected!=cut {return Err(Error::Conflict);}
    }
    Ok(())
}
