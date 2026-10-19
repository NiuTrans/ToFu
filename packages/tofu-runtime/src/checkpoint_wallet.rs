//! Preserve an original executor's frozen invoice at atomic journal retirement.
//! Validate existing quote credits only; no prices, wallet writes or new ledger.
use super::{Error, HEADER_SCHEMA, SNAPSHOT_SCHEMA};
use serde_json::Value;

pub(super) const COMPONENTS: [(&str, &str); 7] = [
    ("input", "input"), ("output", "output"), ("cache_read", "cache_read"),
    ("cache_write", "cache_write"), ("base", "baseMicro"),
    ("margin", "marginMicro"), ("total", "totalMicro"),
];
pub(super) fn number(value: &Value) -> Result<u64, Error> {
    value.as_u64().filter(|value| *value <= i64::MAX as u64).ok_or(Error::Invalid)
}
pub(super) fn add(left: u64, right: u64) -> Result<u64, Error> {
    left.checked_add(right).filter(|value| *value <= i64::MAX as u64).ok_or(Error::Invalid)
}
pub(super) fn capsule_valid(capsule: &Value) -> bool {
    if !crate::agent_checkpoint::valid(
        &SNAPSHOT_SCHEMA["properties"]["host_fee_projection"]["properties"]["wallet_settlement"], capsule) {
        return false;
    }
    let values = &capsule["componentsMicro"];
    let base = COMPONENTS[..4].iter().try_fold(0, |sum, (_, component)| add(sum, number(&values[*component])?));
    base.is_ok_and(|base| Some(base) == values["baseMicro"].as_u64()
        && add(base, values["marginMicro"].as_u64().unwrap_or(u64::MAX)).ok() == values["totalMicro"].as_u64())
}

pub(super) fn quote_parts(cost: &Value) -> Result<([u64; 7], u64), Error> {
    let schema = &SNAPSHOT_SCHEMA["properties"]["host_fee_projection"]["properties"]["rounds"]["items"]["properties"]["cost"];
    if !crate::agent_checkpoint::valid(schema, cost) { return Err(Error::Invalid); }
    let credits = cost["credits"].as_object().filter(|fields| fields.len() == 7).ok_or(Error::Invalid)?;
    let mut amounts = [0_u64; 7];
    for (index, (credit, _)) in COMPONENTS.iter().enumerate() {
        amounts[index] = number(credits.get(*credit).ok_or(Error::Invalid)?)?;
    }
    if amounts[..4].iter().try_fold(0, |sum, value| add(sum, *value))? != amounts[4]
        || add(amounts[4], amounts[5])? != amounts[6] { return Err(Error::Invalid); }
    let prompt = cost["price"]["prompt_tokens"].as_u64().ok_or(Error::Invalid)?;
    let output = if cost["amounts"].is_null() {
        if prompt != 0 || amounts.iter().any(|value| *value != 0) { return Err(Error::Invalid); }
        0
    } else {
        if cost["amounts"]["totalInputTokens"].as_u64() != Some(prompt) { return Err(Error::Invalid); }
        cost["amounts"]["outputTokens"].as_u64().ok_or(Error::Invalid)?
    };
    Ok((amounts, prompt.checked_add(output).ok_or(Error::Invalid)?))
}

/// The paid source snapshot remains the authority until its atomic retirement.
/// Both Journal and active/passive Store paths use this check before moving debt.
pub fn handoff_wallet_invoice(state: &Value) -> Result<Option<Value>, Error> {
    let fees = &state["host_fee_projection"];
    if fees["wallet_user_id"].as_str().is_none_or(str::is_empty) {
        return if fees.get("wallet_settlement").is_some() { Err(Error::Invalid) } else { Ok(None) };
    }
    let invoice = fees.get("wallet_settlement").ok_or(Error::Ineligible)?;
    let account = &state["shared_model_account"];
    if !capsule_valid(invoice) || invoice["task_id"] != state["task_id"]
        || invoice["task_id"] != fees["task_id"] || invoice["task_id"] != account["root_task_id"]
        || invoice["owner_user_id"] != state["owner_user_id"]
        || invoice["owner_user_id"] != fees["owner_user_id"]
        || invoice["owner_user_id"] != account["owner_user_id"] || account["complete"] != true
        || invoice["wallet_user_id"] != fees["wallet_user_id"]
        || invoice["reserved_micro"] != fees["reserved_micro"]
    { return Err(Error::Invalid); }
    let roots = fees["rounds"].as_array().ok_or(Error::Invalid)?;
    let children = match fees.get("child_rounds") {
        None => None, Some(rows) => Some(rows.as_array().ok_or(Error::Invalid)?),
    };
    if roots.len() as u64 != number(&state["next_round"])? { return Err(Error::Invalid); }
    let mut totals = [0_u64; 7];
    let (mut count, mut root_tokens, mut all_tokens) = (0_u64, 0_u64, 0_u64);
    for (is_root, row) in roots.iter().map(|row| (true, row))
        .chain(children.into_iter().flatten().map(|row| (false, row))) {
        let cost = &row["cost"];
        let credits = cost["credits"].as_object().filter(|fields| fields.len() == 7).ok_or(Error::Invalid)?;
        let mut amounts = [0_u64; 7];
        for (index, (credit, _)) in COMPONENTS.iter().enumerate() {
            amounts[index] = number(credits.get(*credit).ok_or(Error::Invalid)?)?;
        }
        if amounts[..4].iter().try_fold(0, |sum, value| add(sum, *value))? != amounts[4]
            || add(amounts[4], amounts[5])? != amounts[6] { return Err(Error::Invalid); }
        let prompt = cost["price"]["prompt_tokens"].as_u64().ok_or(Error::Invalid)?;
        let output = if cost["amounts"].is_null() {
            if prompt != 0 || amounts.iter().any(|value| *value != 0) { return Err(Error::Invalid); }
            0
        } else { cost["amounts"]["outputTokens"].as_u64().ok_or(Error::Invalid)? };
        let tokens = prompt.checked_add(output).ok_or(Error::Invalid)?;
        all_tokens = all_tokens.checked_add(tokens).ok_or(Error::Invalid)?;
        if is_root { root_tokens = root_tokens.checked_add(tokens).ok_or(Error::Invalid)?; }
        let receiver = if is_root { row.get("task_id").unwrap_or(&fees["task_id"]) }
            else { &row["billable_task_id"] };
        if receiver.as_str().is_none_or(str::is_empty) { return Err(Error::Invalid); }
        if receiver == &state["task_id"] {
            count = add(count, 1)?;
            for index in 0..7 { totals[index] = add(totals[index], amounts[index])?; }
        }
    }
    if Some(all_tokens) != account["tokens"].as_u64()
        || Some(root_tokens) != account.get("root_tokens").unwrap_or(&account["tokens"]).as_u64()
        || count != number(&invoice["roundCount"])?
    { return Err(Error::Invalid); }
    for (index, (_, component)) in COMPONENTS.iter().enumerate() {
        if totals[index] != number(&invoice["componentsMicro"][*component])? { return Err(Error::Invalid); }
    }
    Ok(Some(invoice.clone()))
}

/// A retired invoice grants billing evidence, never an execution lease.
pub fn retired_wallet_invoice(header: &Value) -> Result<Option<&Value>, Error> {
    if !crate::agent_checkpoint::valid(&HEADER_SCHEMA, header) { return Err(Error::Invalid); }
    let Some(invoice) = header.get("wallet_settlement") else { return Ok(None); };
    if !capsule_valid(invoice) || header["phase"] != "terminal" || !header["lease"].is_null()
        || invoice["task_id"] != header["task_id"] || invoice["owner_user_id"] != header["owner_user_id"]
    { return Err(Error::Invalid); }
    Ok(Some(invoice))
}

/// Preserve the control invoice response shape across storage adapters.
pub fn wallet_invoice_view(owner: u64, task: &str, header: &Value) -> Result<Value, Error> {
    if header["owner_user_id"].as_u64() != Some(owner) || header["task_id"] != task {
        return Err(Error::Invalid);
    }
    let invoice = retired_wallet_invoice(header)?;
    Ok(serde_json::json!({"phase":header["phase"], "wallet_settlement":invoice}))
}
