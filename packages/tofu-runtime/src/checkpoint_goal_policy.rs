//! Shared immutable Goal fee mode, fresh wallet hold and actor birth rules.
//! These functions validate supplied transaction observations only. Storage owns
//! exact owner filtering, locks, atomic ledger writes and durable acknowledgement.
use crate::checkpoint_journal::Error;
use serde_json::{json, Value};

pub fn cost_only(invoice: &Value) -> bool {
    invoice["format"] == "tofu.native-aggregate-cost/v1"
}

pub fn validate_launch(invoice: &Value, metadata: &Value, reserved: &Value) -> Result<(), Error> {
    let expected = if cost_only(invoice) {
        if reserved != 0 {
            return Err(Error::Invalid);
        }
        json!({"mode":"cost"})
    } else {
        // Old paid launches predate this explicit immutable field. Their exact
        // original/target holds and native admissions are still checked below.
        if metadata.get("fee_account").is_none() {
            return Ok(());
        }
        json!({"mode":"wallet","wallet_user_id":invoice["wallet_user_id"],"reserved_micro":reserved})
    };
    if metadata["fee_account"] != expected {
        return Err(Error::Ineligible);
    }
    Ok(())
}

pub fn validate_preparation(
    event: &Value,
    owner: u64,
    root: &str,
    conversation: &str,
    task: &str,
) -> Result<(), Error> {
    let binding = &event["invocation_completion"];
    let birth = &binding["identity"];
    let hex = |value: &Value, size: usize| {
        value.as_str().is_some_and(|text| {
            text.len() == size
                && text
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
    };
    if event.as_object().is_none_or(|value| value.len() != 6)
        || event["type"] != "execution_effect_prepared"
        || event["kind"] != "agent"
        || event["key"].as_str().is_none_or(str::is_empty)
        || !hex(&event["input_hash"], 64)
        || event["invocation_id"] != task
        || binding.as_object().is_none_or(|value| value.len() != 4)
        || binding["format"] != "tofu.flow.invocation-completion/v1"
        || !hex(&binding["nonce"], 32)
        || binding["account_root_task_id"] != root
        || birth
            != &json!({"user_id":owner,
            "conversation_id":conversation,"parent_task_id":root,"child_task_id":task,
            "archive_scope":format!("invocation:{owner}:{task}")})
    {
        return Err(Error::Invalid);
    }
    Ok(())
}

/// A fresh native hold is consumed once. Existing releases/debits refuse.
pub fn validate_wallet_hold(
    reserved: u64,
    reserve: Option<&Value>,
    admission: Option<&Value>,
    release: Option<&Value>,
    debit: Option<&Value>,
) -> Result<(), Error> {
    if reserved == 0 || reserved > i64::MAX as u64 {
        return Err(Error::Invalid);
    }
    if reserve.and_then(|value| value["amount_micro"].as_i64()) != Some(-(reserved as i64))
        || admission.is_none_or(|value| value["amount_micro"] != 0)
        || release.is_some()
        || debit.is_some()
    {
        return Err(Error::Ineligible);
    }
    Ok(())
}
