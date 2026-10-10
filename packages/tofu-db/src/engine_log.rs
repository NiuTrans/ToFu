//! Leveled, single-line JSON diagnostics for engine-internal events.
//!
//! The engine deliberately ships no logging framework: the host application
//! owns log policy (rotation, redaction, flood control) and inherits the
//! daemon's stderr. What the engine owes that pipeline is structure — every
//! operational line is exactly one JSON object with the fixed shape
//! `{"source":"tofu-db","level":...,"target":...,"message":...}` so it can be
//! routed and filtered without parsing free text. CLI contract output
//! (key=value / JSON payloads) still goes to stdout, and the
//! TOFU_DB_FSYNC_STATS shutdown dump stays human-oriented because it is only
//! emitted on explicit operator opt-in.

use serde_json::json;

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
    eprintln!(
        "{}",
        json!({
            "source": "tofu-db",
            "level": level,
            "target": target,
            "message": message,
        })
    );
}

#[cfg(test)]
mod tests {
    use super::*;

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
        // Capture is not possible for eprintln; assert the contract through
        // the same serializer the emitter uses.
        let line = json!({
            "source": "tofu-db",
            "level": "error",
            "target": "background_pack",
            "message": "line one\nline two \"quoted\"",
        })
        .to_string();
        assert!(!line.contains('\n'));
        let parsed: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(parsed["source"], "tofu-db");
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
}
