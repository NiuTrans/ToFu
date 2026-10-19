//! Native verifier classification over generated Python protocol/Unicode rules.
//! No graph effect, journal, account, environment read or audit sink is owned.
use fancy_regex::Regex;
use serde_json::{json, Value};
use std::sync::OnceLock;
#[path = "generated_verdict_policy.rs"]
mod contract;

struct Patterns {
    defect: Regex,
    unresolved: Regex,
    stop: Regex,
    keep: Regex,
    verdict: Regex,
    progress: Regex,
    header: Regex,
    token_husk: Regex,
}
fn patterns() -> Result<&'static Patterns, String> {
    static PATTERNS: OnceLock<Result<Patterns, String>> = OnceLock::new();
    PATTERNS
        .get_or_init(|| {
            let compile = |source| Regex::new(source).map_err(|error| error.to_string());
            Ok(Patterns {
                defect: compile(contract::PLAN_DEFECT_RE)?,
                unresolved: compile(contract::UNRESOLVED_PHRASE_RE)?,
                stop: compile(contract::LOOSE_STOP_RE)?,
                keep: compile(contract::LOOSE_CONTINUE_RE)?,
                verdict: compile(contract::VERDICT_RE)?,
                progress: compile(contract::PROGRESS_RE)?,
                header: compile(contract::FEEDBACK_HEADER_RE)?,
                token_husk: compile(contract::TOKEN_HUSK_RE)?,
            })
        })
        .as_ref()
        .map_err(Clone::clone)
}
pub(crate) fn space(char: char) -> bool {
    let code = char as u32;
    contract::SPACE_RANGES
        .iter()
        .any(|&(a, b)| a <= code && code <= b)
}
fn trim(text: &str) -> &str {
    text.trim_matches(space)
}
fn rtrim(text: &str) -> &str {
    text.trim_end_matches(space)
}
fn decimal(text: &str, limit: usize) -> Option<String> {
    if limit != 0 && text.chars().count() > limit {
        return None;
    }
    let mut result = String::with_capacity(text.len());
    for char in text.chars() {
        let index = contract::DIGITS
            .binary_search_by_key(&(char as u32), |row| row.0)
            .ok()?;
        result.push((b'0' + contract::DIGITS[index].1) as char);
    }
    let normalized = result.trim_start_matches('0');
    Some(if normalized.is_empty() {
        "0".into()
    } else {
        normalized.into()
    })
}
fn last_capture<'a>(
    regex: &Regex,
    text: &'a str,
) -> Result<Option<fancy_regex::Captures<'a>>, String> {
    let mut last = None;
    for captured in regex.captures_iter(text) {
        last = Some(captured.map_err(|error| error.to_string())?);
    }
    Ok(last)
}
fn erase(regex: &Regex, text: &str) -> Result<String, String> {
    let mut result = String::new();
    let mut start = 0;
    for found in regex.find_iter(text) {
        let found = found.map_err(|error| error.to_string())?;
        result.push_str(&text[start..found.start()]);
        start = found.end();
    }
    result.push_str(&text[start..]);
    Ok(result)
}
pub(crate) fn word(character: Option<char>) -> bool {
    let Some(character) = character else {
        return false;
    };
    let code = character as u32;
    contract::WORD_RANGES
        .binary_search_by(|&(low, high)| {
            if code < low {
                std::cmp::Ordering::Greater
            } else if code > high {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}
// These generated patterns have exactly two outer Python word boundaries.
// Check them against the full input, advancing one character after a rejected
// candidate so a later overlapping valid match cannot disappear.
fn boundary_matches(regex: &Regex, text: &str, first_only: bool) -> Result<usize, String> {
    let mut start = 0;
    let mut count = 0;
    while start < text.len() {
        let Some(found) = regex
            .find_from_pos(text, start)
            .map_err(|error| error.to_string())?
        else {
            break;
        };
        let left = word(text[..found.start()].chars().next_back())
            != word(text[found.start()..].chars().next());
        let right = word(text[..found.end()].chars().next_back())
            != word(text[found.end()..].chars().next());
        if left && right {
            count += 1;
            if first_only {
                break;
            }
            start = found.end();
        } else {
            start = found.start() + text[found.start()..].chars().next().unwrap().len_utf8();
        }
    }
    Ok(count)
}
fn in_ranges(character: char, ranges: &[(u32, u32)]) -> bool {
    let code = character as u32;
    ranges
        .binary_search_by(|&(low, high)| {
            if code < low {
                std::cmp::Ordering::Greater
            } else if code > high {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}
pub(crate) fn lower(text: &str) -> String {
    let characters: Vec<char> = text.chars().collect();
    let mut result = String::with_capacity(text.len());
    for (index, &character) in characters.iter().enumerate() {
        if character == 'Σ' {
            let before = characters[..index]
                .iter()
                .rev()
                .find(|&&c| !in_ranges(c, contract::IGNORABLE_RANGES));
            let after = characters[index + 1..]
                .iter()
                .find(|&&c| !in_ranges(c, contract::IGNORABLE_RANGES));
            let final_sigma = before.is_some_and(|&c| in_ranges(c, contract::CASED_RANGES))
                && !after.is_some_and(|&c| in_ranges(c, contract::CASED_RANGES));
            result.push(if final_sigma { 'ς' } else { 'σ' });
        } else if let Ok(index) =
            contract::LOWERCASE.binary_search_by_key(&(character as u32), |row| row.0)
        {
            result.push_str(contract::LOWERCASE[index].1);
        } else {
            result.push(character)
        }
    }
    result
}
pub(crate) fn justification_chars(text: &str) -> Result<u64, String> {
    let patterns = patterns()?;
    let sentinel = text.replace(contract::VU_DONE_SENTINEL, "");
    let progress = erase(&patterns.progress, &sentinel)?;
    let verdict = erase(&patterns.verdict, &progress)?;
    if verdict == text {
        return Ok(trim(text).chars().count() as u64);
    }
    let normalized = patterns
        .token_husk
        .replace_all(&verdict, contract::TOKEN_HUSK_REPLACEMENT);
    Ok(trim(normalized.as_ref()).chars().count() as u64)
}
pub(crate) fn progress(
    text: &str,
    limit: usize,
) -> Result<(Option<String>, Option<String>), String> {
    let Some(capture) = last_capture(&patterns()?.progress, text)? else {
        return Ok((None, None));
    };
    match (
        decimal(capture.get(1).unwrap().as_str(), limit),
        decimal(capture.get(2).unwrap().as_str(), limit),
    ) {
        (Some(resolved), Some(remaining)) => Ok((Some(resolved), Some(remaining))),
        _ => Ok((None, None)),
    }
}
fn unresolved(patterns: &Patterns, text: &str) -> Result<(usize, usize), String> {
    Ok((
        text.matches(contract::UNRESOLVED_EMOJI).count(),
        boundary_matches(&patterns.unresolved, text, false)?,
    ))
}
pub fn classify(
    text: &str,
    virtual_user: bool,
    loose: bool,
    strip: bool,
    replan: bool,
    digit_limit: usize,
) -> Result<Value, String> {
    let patterns = patterns()?;
    let mut audits = Vec::new();
    let feedback = strip.then(|| text.to_owned());
    if virtual_user {
        let low = lower(text);
        let stop = low.contains(&lower(contract::VU_DONE_SENTINEL))
            || low.contains("[verdict: stop]")
            || low.contains("verdict: stop");
        if low.contains("[verdict: blocked]") && !stop {
            return Ok(
                json!({"phase":"blocked","plan_defect":null,"feedback":feedback,"had_tag":true,"audits":audits}),
            );
        }
        if stop {
            let (crosses, phrases) = unresolved(patterns, text)?;
            if crosses != 0 || phrases != 0 {
                audits.push(json!({"event":"vu_done_override","original":"stop","new":"worker",
                    "x_count":crosses,"phrase_hits":phrases,"reason":"unresolved_markers_in_vu_done"}));
                return Ok(
                    json!({"phase":"worker","plan_defect":null,"feedback":feedback,"had_tag":false,"audits":audits}),
                );
            }
            if let Some(progress) = last_capture(&patterns.progress, text)? {
                let resolved = decimal(progress.get(1).unwrap().as_str(), digit_limit);
                let remaining = decimal(progress.get(2).unwrap().as_str(), digit_limit);
                if resolved.is_some() && remaining.as_ref().is_some_and(|value| value != "0") {
                    audits.push(
                        json!({"event":"vu_done_override","original":"stop","new":"worker",
                        "remaining_decimal":remaining,"reason":"progress_remaining_in_vu_done"}),
                    );
                    return Ok(
                        json!({"phase":"worker","plan_defect":null,"feedback":feedback,"had_tag":false,"audits":audits}),
                    );
                }
            }
            return Ok(
                json!({"phase":"stop","plan_defect":null,"feedback":feedback,"had_tag":false,"audits":audits}),
            );
        }
        return Ok(
            json!({"phase":"worker","plan_defect":null,"feedback":feedback,"had_tag":false,"audits":audits}),
        );
    }
    let defect = last_capture(&patterns.defect, text)?
        .map(|capture| trim(capture.get(1).unwrap().as_str()).to_owned());
    let matched = last_capture(&patterns.verdict, text)?;
    let had_tag = matched.is_some();
    let mut phase = "worker";
    let mut feedback = None;
    if let Some(capture) = matched {
        let tag = capture.get(1).unwrap().as_str().to_uppercase();
        phase = match tag.as_str() {
            "STOP" => "stop",
            "CONTINUE_PLANNER" => "planner",
            _ => "worker",
        };
        if strip {
            let before = rtrim(&text[..capture.get(0).unwrap().start()]);
            let clean = erase(&patterns.header, before)?;
            feedback = Some(rtrim(&erase(&patterns.defect, rtrim(&clean))?).to_owned());
        }
    } else {
        if loose {
            phase = if text.is_empty() || boundary_matches(&patterns.stop, text, true)? != 0 {
                "stop"
            } else if boundary_matches(&patterns.keep, text, true)? != 0 {
                "worker"
            } else {
                "stop"
            };
        }
        if strip {
            feedback = Some(trim(&erase(&patterns.defect, text)?).to_owned());
        }
        // Empty loose feedback returns before the ordinary override gate.
        if loose && text.is_empty() {
            return Ok(
                json!({"phase":phase,"plan_defect":defect,"feedback":feedback,"had_tag":false,"audits":audits}),
            );
        }
    }
    if phase == "stop" {
        let (crosses, phrases) = unresolved(patterns, feedback.as_deref().unwrap_or(text))?;
        if crosses != 0 || phrases != 0 {
            audits.push(json!({"event":"critic_verdict_override","original":"stop","new":"worker",
                "x_count":crosses,"phrase_hits":phrases,"reason":"unresolved_markers_in_stop_feedback"}));
            phase = "worker";
        }
    }
    if phase == "planner" {
        if defect.as_ref().is_none_or(String::is_empty) {
            audits.push(json!({"event":"critic_verdict_override","original":"planner","new":"worker","reason":"missing_plan_defect_tag"}));
            phase = "worker";
        } else if defect.as_ref().is_some_and(|defect| {
            let lower = lower(defect);
            contract::WORKER_RATIONALIZATIONS
                .iter()
                .any(|reason| lower.contains(reason))
        }) {
            audits.push(json!({"event":"critic_verdict_override","original":"planner","new":"worker",
                "reason":"plan_defect_is_worker_problem","defect_preview":defect.as_ref().unwrap().chars().take(200).collect::<String>()}));
            phase = "worker";
        } else if !replan {
            phase = "worker";
        }
    }
    Ok(
        json!({"phase":phase,"plan_defect":defect,"feedback":feedback,"had_tag":had_tag,"audits":audits}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_lowercase_context_and_version_remain_exact() {
        assert_eq!(
            lower("ΟΣ ΟΣΑ AΣ\u{0345} AΣ\u{0301}A"),
            "ος οσα aς\u{0345} aσ\u{0301}a"
        );
        assert_eq!(
            lower("\u{1c89} \u{1c8a} İ K ſ"),
            "\u{1c89} \u{1c8a} i\u{0307} k ſ"
        );
        if let Ok(path) = std::env::var("TOFU_NATIVE_LOWER_ORACLE") {
            let rows: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
            for row in rows.as_array().unwrap() {
                assert_eq!(
                    lower(row["input"].as_str().unwrap()),
                    row["output"].as_str().unwrap(),
                    "{}",
                    row["input"]
                );
            }
        }
    }
    #[test]
    fn structural_and_completion_evidence_remain_distinct() {
        assert_eq!(
            classify(
                "[VERDICT: STOP] still failing",
                false,
                true,
                false,
                true,
                4300
            )
            .unwrap()["phase"],
            "worker"
        );
        assert_eq!(
            classify(
                "[PLAN_DEFECT: wrong decomposition][VERDICT: CONTINUE_PLANNER]",
                false,
                true,
                false,
                true,
                4300
            )
            .unwrap()["phase"],
            "planner"
        );
        assert_eq!(
            classify(
                "[VU: TASK_DONE][PROGRESS: resolved=4 remaining=1]",
                true,
                true,
                false,
                true,
                4300
            )
            .unwrap()["phase"],
            "worker"
        );
        assert_eq!(
            classify("[VERDICT: BLOCKED]", true, true, false, true, 4300).unwrap()["phase"],
            "blocked"
        );
    }
    #[test]
    fn python_unicode_case_space_and_word_boundaries_are_preserved() {
        assert_eq!(
            classify("[VERDİCT:\u{1c}STOP]", false, false, false, true, 4300).unwrap()["phase"],
            "stop"
        );
        assert_eq!(
            classify(
                "[VERDICT: STOP] unresolved\u{301}",
                false,
                false,
                false,
                true,
                4300
            )
            .unwrap()["phase"],
            "worker"
        );
        assert_eq!(
            classify(
                "[VU: TASK_DONE][PROGRESS: resolved=٢ remaining=٣]",
                true,
                true,
                false,
                true,
                4300
            )
            .unwrap()["phase"],
            "worker"
        );
    }
}

pub(crate) fn casefold(text: &str) -> String {
    if text.is_ascii() {return text.to_ascii_lowercase();}
    let mut folded=String::with_capacity(text.len());
    for character in text.chars() {
        if character.is_ascii() {folded.push(character.to_ascii_lowercase());continue;}
        if let Ok(index)=contract::CASEFOLD.binary_search_by_key(&(character as u32),|entry|entry.0) {
            folded.push_str(contract::CASEFOLD[index].1);
        } else {folded.push(character);}
    }
    folded
}
