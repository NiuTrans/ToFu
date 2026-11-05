//! Leveled, single-line JSON diagnostics for engine-internal events.
//!
//! The engine deliberately ships no logging framework: the host application
//! owns log policy (rotation, redaction, flood control) and inherits the
//! daemon's stderr. What the engine owes that pipeline is structure — every
//! operational line is exactly one JSON object with the fixed shape
//! `{"source":"tofu-db","timestamp":...,"level":...,"target":...,"message":...}`
//! so it can be routed and filtered without parsing free text. `timestamp`
//! is the wall-clock emit instant in RFC 3339 UTC with milliseconds, the
//! incident index's `timestamp` form, so a raw console line correlates with
//! application logs; a duration record (`authority_hold`, a lifecycle
//! `state=done`) is emitted when its interval ends. CLI contract output
//! (key=value / JSON payloads) still goes to stdout, and the
//! TOFU_DB_FSYNC_STATS shutdown dump stays human-oriented because it is only
//! emitted on explicit operator opt-in.

use serde_json::json;
use std::time::{SystemTime, UNIX_EPOCH};

pub fn error(target: &str, message: &str) {
    emit("error", target, message);
}

pub fn warn(target: &str, message: &str) {
    emit("warn", target, message);
}

pub fn info(target: &str, message: &str) {
    emit("info", target, message);
}

/// One-shot boot/stop diagnostics; errors retain their original type and value.
pub(crate) fn lifecycle_step<T>(
    target: &str,
    step: &str,
    operation: impl FnOnce() -> std::io::Result<T>,
) -> std::io::Result<T> {
    let started = std::time::Instant::now();
    info(target, &format!("step={step} state=start"));
    let result = operation();
    let state = if result.is_ok() { "done" } else { "failed" };
    info(target, &format!("step={step} state={state} elapsed_ms={}", started.elapsed().as_millis()));
    result
}

fn emit(level: &str, target: &str, message: &str) {
    eprintln!("{}", record(SystemTime::now(), level, target, message));
}

fn record(now: SystemTime, level: &str, target: &str, message: &str) -> String {
    json!({
        "source": "tofu-db",
        "timestamp": rfc3339_utc_millis(now),
        "level": level,
        "target": target,
        "message": message,
    })
    .to_string()
}

/// `YYYY-MM-DDTHH:MM:SS.mmm+00:00`; a clock set before 1970 still formats.
fn rfc3339_utc_millis(now: SystemTime) -> String {
    let millis = match now.duration_since(UNIX_EPOCH) {
        Ok(after) => after.as_millis() as i64,
        Err(before) => -(before.duration().as_millis() as i64),
    };
    let (year, month, day) = civil_from_days(millis.div_euclid(86_400_000));
    let millis_of_day = millis.rem_euclid(86_400_000);
    let seconds = millis_of_day / 1_000;
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}+00:00",
        seconds / 3_600,
        seconds / 60 % 60,
        seconds % 60,
        millis_of_day % 1_000,
    )
}

/// Proleptic Gregorian (year, month, day) of a day count since 1970-01-01:
/// Howard Hinnant's `civil_from_days`, whose 400-year era holds 146,097 days.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 { month_index + 3 } else { month_index - 9 };
    (year_of_era + era * 400 + i64::from(month <= 2), month, day)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn at_millis(millis: i64) -> SystemTime {
        let offset = Duration::from_millis(millis.unsigned_abs());
        if millis >= 0 { UNIX_EPOCH + offset } else { UNIX_EPOCH - offset }
    }

    #[test]
    fn lifecycle_timing_preserves_results_and_runs_operation_once() {
        let mut calls = 0;
        let value = lifecycle_step("test", "success", || {
            calls += 1;
            Ok(17)
        }).unwrap();
        assert_eq!(value, 17);
        assert_eq!(calls, 1);
        let error = lifecycle_step::<()>("test", "failure", || {
            Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "sentinel"))
        }).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
        assert_eq!(error.to_string(), "sentinel");
    }

    #[test]
    fn emitted_lines_are_single_line_json_with_a_fixed_shape() {
        // Capture is not possible for eprintln; the emitter prints exactly
        // this record for its own clock reading.
        let line = record(
            at_millis(1_793_525_250_172),
            "error",
            "background_pack",
            "line one\nline two \"quoted\"",
        );
        assert!(!line.contains('\n'));
        let parsed: serde_json::Value = serde_json::from_str(&line).unwrap();
        let mut keys: Vec<_> = parsed.as_object().unwrap().keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, ["level", "message", "source", "target", "timestamp"]);
        assert_eq!(parsed["source"], "tofu-db");
        assert_eq!(parsed["timestamp"], "2026-11-01T09:27:30.172+00:00");
        assert_eq!(parsed["level"], "error");
        assert_eq!(parsed["target"], "background_pack");
        assert_eq!(
            parsed["message"],
            "line one\nline two \"quoted\""
        );
        // Exercise the emitters so a signature drift fails to compile.
        error("test", "m");
        warn("test", "m");
        info("test", "m");
    }

    #[test]
    fn timestamps_are_rfc3339_utc_with_milliseconds() {
        // Expected values come from Python's datetime, an independent calendar.
        for (millis, expected) in [
            (0, "1970-01-01T00:00:00.000+00:00"),
            (-1, "1969-12-31T23:59:59.999+00:00"),
            (951_868_799_999, "2000-02-29T23:59:59.999+00:00"),
            (951_868_800_000, "2000-03-01T00:00:00.000+00:00"),
            (4_107_456_000_000, "2100-02-28T00:00:00.000+00:00"),
            (4_107_542_400_000, "2100-03-01T00:00:00.000+00:00"),
            (1_793_525_250_172, "2026-11-01T09:27:30.172+00:00"),
            (253_402_300_799_999, "9999-12-31T23:59:59.999+00:00"),
        ] {
            assert_eq!(rfc3339_utc_millis(at_millis(millis)), expected, "{millis}");
        }
        let now = rfc3339_utc_millis(SystemTime::now());
        let shape = regex::Regex::new(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$").unwrap();
        assert!(shape.is_match(&now), "{now}");
    }

    #[test]
    fn calendar_days_match_a_day_by_day_walk() {
        fn days_in_month(year: i64, month: i64) -> i64 {
            let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
            match month {
                2 if leap => 29,
                2 => 28,
                4 | 6 | 9 | 11 => 30,
                _ => 31,
            }
        }
        // 1599..2400 spans every leap-rule branch: 1600, 1900, 2000, 2100, 2400.
        let (mut year, mut month, mut day) = (1970, 1, 1);
        for days in 0..(430 * 366) {
            assert_eq!(civil_from_days(days), (year, month, day), "{days}");
            day += 1;
            if day > days_in_month(year, month) {
                day = 1;
                month += 1;
                if month > 12 {
                    month = 1;
                    year += 1;
                }
            }
        }
        let (mut year, mut month, mut day) = (1970, 1, 1);
        for days in (-(370 * 366)..0).rev() {
            day -= 1;
            if day == 0 {
                month -= 1;
                if month == 0 {
                    month = 12;
                    year -= 1;
                }
                day = days_in_month(year, month);
            }
            assert_eq!(civil_from_days(days), (year, month, day), "{days}");
        }
    }
}
