//! Branch label policy over the generated Python casefold, word and whitespace
//! rules. This pure matcher owns no callback, audit, journal or retained cache.
use crate::verdict_policy::{casefold, space, word};

pub fn choose(verdict: &str, labels: &[(usize, &str)]) -> Option<usize> {
    let text = casefold(verdict);
    let mut matches = Vec::new();
    for &(target, label) in labels {
        let label = casefold(label.trim_matches(space));
        if label.is_empty() {
            continue;
        }
        let mut offset = 0;
        while let Some(relative) = text[offset..].find(&label) {
            let start = offset + relative;
            let end = start + label.len();
            if !word(text[..start].chars().next_back()) && !word(text[end..].chars().next()) {
                matches.push((start, end, target));
                offset = end; // Python finditer accepts nonoverlapping matches.
            } else {
                offset = start
                    + text[start..]
                        .chars()
                        .next()
                        .expect("nonempty match")
                        .len_utf8();
            }
        }
    }
    // A sweep discards strictly contained intervals in O(M log M), preserving
    // equal-span options as ambiguity. UTF-8 byte order preserves containment.
    matches.sort_unstable_by(|a, b| a.0.cmp(&b.0).then_with(|| b.1.cmp(&a.1)));
    let mut longest_end = 0;
    let mut longest_start = 0;
    let mut chosen = None;
    for (start, end, target) in matches {
        if end < longest_end || (end == longest_end && start > longest_start) {
            continue;
        }
        if end > longest_end {
            longest_end = end;
            longest_start = start;
        }
        if chosen.is_some_and(|chosen| chosen != target) {
            return None;
        }
        chosen = Some(target);
    }
    chosen
}
