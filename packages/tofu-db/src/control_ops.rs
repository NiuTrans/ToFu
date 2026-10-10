//! Authenticated control operations served outside the semantic catalog.
//!
//! `system.health` and `system.metrics` observe the daemon itself. They answer
//! from lock-free counters, observer handles with their own locks, and static
//! launch configuration — never from the authority mutex, so observing the
//! engine can never queue behind commits. They dispatch inside the
//! authenticated storage.v2 session but ahead of semantic admission, which
//! keeps them out of the application operation catalog and the semantic
//! differential gate: they expose engine state, not owner data. Payload
//! contracts are versioned (`tofu-db.system-health.v1` /
//! `tofu-db.system-metrics.v1`) and every field set is fixed and bounded.

use serde_json::{json, Value};

use crate::background_pack::BackgroundPackerObserver;
use crate::authority_watch::AuthorityContentionObserver;
use crate::fsync_stats;
use crate::listener::LoopbackListenerObserver;
use crate::authority_gc_online::OnlineGarbageCollectionObserver;
use crate::maintenance::MaintenanceObserver;
use crate::online_backup::OnlineBackupObserver;
use crate::generated_storage_v2::{STATUS_BAD_REQUEST, STATUS_UNAVAILABLE};
use std::io;
use crate::protocol::{Request, Response, PROTOCOL_VERSION};

pub const SYSTEM_HEALTH_OPERATION: &str = "system.health";
pub const SYSTEM_METRICS_OPERATION: &str = "system.metrics";
pub const SYSTEM_BACKUP_OPERATION: &str = "system.backup";
pub const SYSTEM_HEALTH_CONTRACT: &str = "tofu-db.system-health.v1";
pub const SYSTEM_METRICS_CONTRACT: &str = "tofu-db.system-metrics.v1";
pub const SYSTEM_BACKUP_CONTRACT: &str = "tofu-db.system-backup.v1";

/// Observation handles the daemon shares with every connection. Each field is
/// either an observer with its own lock or static launch data; no field ever
/// touches the authority mutex.
#[derive(Clone, Default)]
pub struct ControlObservers {
    pub listener: Option<LoopbackListenerObserver>,
    pub maintenance: Option<MaintenanceObserver>,
    pub packer: Option<BackgroundPackerObserver>,
    pub online_gc: Option<OnlineGarbageCollectionObserver>,
    pub online_backup: Option<OnlineBackupObserver>,
    pub authority_watch: Option<AuthorityContentionObserver>,
    pub wal_durability: Option<&'static str>,
    pub wal_flush_interval_milliseconds: Option<u64>,
    pub resource_budget: Option<Value>,
    pub canonical_json_cache: Option<crate::authority::CanonicalProofCacheObserver>,
    pub published_queries: Option<crate::authority::PublishedQueryObserver>,
    pub decoded_chunk_cache: Option<crate::authority::DecodedChunkCacheObserver>,
}

pub fn is_control_operation(name: &str) -> bool {
    matches!(
        name,
        SYSTEM_HEALTH_OPERATION | SYSTEM_METRICS_OPERATION | SYSTEM_BACKUP_OPERATION
    )
}

/// Answer a control operation. `is_control_operation` gates dispatch, so any
/// non-health name reaching this point is the metrics operation.
pub(crate) fn respond(
    request: &Request,
    schema_id: u32,
    observers: Option<&ControlObservers>,
) -> Response {
    let fallback = ControlObservers::default();
    let observers = observers.unwrap_or(&fallback);
    if request.operation == SYSTEM_BACKUP_OPERATION {
        return backup_response(request, schema_id, observers);
    }
    let payload = if request.operation == SYSTEM_HEALTH_OPERATION {
        health_payload(observers)
    } else {
        metrics_payload(observers)
    };
    // Serialization of a fixed-shape payload cannot fail; stay total anyway
    // because a control response must never take down the connection.
    let payload = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
    Response {
        correlation_id: request.correlation_id,
        schema_id,
        payload,
        status: 0,
        error_code: None,
        error_message: None,
        retryable: false,
    }
}

/// `system.backup` rings the online driver's doorbell and waits (on this
/// connection's thread, never the authority mutex) for the next finished
/// epoch, bounded by the request deadline. The scheduler's receipt contract
/// (`ok` + nonempty `backup`/`manifest` + nonnegative integer `bytes`) is
/// the success payload; every failure is a retryable storage error.
fn backup_response(
    request: &Request,
    schema_id: u32,
    observers: &ControlObservers,
) -> Response {
    let result = observers
        .online_backup
        .as_ref()
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "online backup driver is not running"))
        .and_then(|observer| observer.run_and_wait(request.deadline_unix_ms));
    match result {
        Ok(report) => {
            let payload = serde_json::json!({
                "contract": SYSTEM_BACKUP_CONTRACT,
                "ok": report.ok,
                "backend": report.backend,
                "artifact_kind": report.artifact_kind,
                "backup": report.backup,
                "manifest": report.manifest,
                "bytes": report.bytes,
                "sha256": report.sha256,
                "durable_sequence": report.durable_sequence,
                "copied_blocks": report.copied_blocks,
                "copied_block_bytes": report.copied_block_bytes,
                "manifest_bytes": report.manifest_bytes,
                "estimated_bytes": report.estimated_bytes,
                "retained_generations": report.retained_generations,
                "pruned_generations": report.pruned_generations,
                "pruned_block_bytes": report.pruned_block_bytes,
            });
            Response {
                correlation_id: request.correlation_id,
                schema_id,
                payload: serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec()),
                status: 0,
                error_code: None,
                error_message: None,
                retryable: false,
            }
        }
        Err(error) => {
            let status = match error.kind() {
                io::ErrorKind::InvalidInput => STATUS_BAD_REQUEST,
                _ => STATUS_UNAVAILABLE,
            };
            Response {
                correlation_id: request.correlation_id,
                schema_id,
                payload: Vec::new(),
                status,
                error_code: Some("database_unavailable".to_owned()),
                error_message: Some(error.to_string()),
                retryable: true,
            }
        }
    }
}

fn component(configured: bool, healthy: bool) -> Value {
    json!({"configured": configured, "healthy": !configured || healthy})
}

fn health_payload(observers: &ControlObservers) -> Value {
    let listener_healthy = observers
        .listener
        .as_ref()
        .map(|observer| observer.metrics().is_ok())
        .unwrap_or(true);
    let maintenance_healthy = observers
        .maintenance
        .as_ref()
        .map(|observer| observer.require_healthy().is_ok())
        .unwrap_or(true);
    let packer_healthy = observers
        .packer
        .as_ref()
        .map(|observer| observer.require_healthy().is_ok())
        .unwrap_or(true);
    let online_gc_healthy = observers
        .online_gc
        .as_ref()
        .map(|observer| observer.require_healthy().is_ok())
        .unwrap_or(true);
    let online_backup_healthy = observers
        .online_backup
        .as_ref()
        .map(|observer| observer.require_healthy().is_ok())
        .unwrap_or(true);
    // The authority component reports CONTENTION health, not thread
    // liveness: a wedged holder leaves every driver thread alive and every
    // other component green while no admitted request can run. Continuous
    // contention past the wedge threshold is an outage by definition, so
    // it flips `ready` exactly like a dead driver (authority_watch.rs).
    let (authority_configured, authority_healthy, authority_contended_ms) = observers
        .authority_watch
        .as_ref()
        .map(|observer| {
            (
                true,
                observer.require_healthy().is_ok() && !observer.wedged(),
                observer.contended_milliseconds(),
            )
        })
        .unwrap_or((false, true, 0));
    let ready = listener_healthy
        && maintenance_healthy
        && packer_healthy
        && online_gc_healthy
        && online_backup_healthy
        && authority_healthy;
    let mut payload = json!({
        "contract": SYSTEM_HEALTH_CONTRACT,
        "backend": "tofudb",
        "protocol": PROTOCOL_VERSION,
        "ready": ready,
        "components": {
            "listener": component(observers.listener.is_some(), listener_healthy),
            "maintenance": component(observers.maintenance.is_some(), maintenance_healthy),
            "backgroundPacker": component(observers.packer.is_some(), packer_healthy),
            "onlineGarbageCollector": component(observers.online_gc.is_some(), online_gc_healthy),
            "onlineBackup": component(observers.online_backup.is_some(), online_backup_healthy),
            "authority": component(authority_configured, authority_healthy),
        },
    });
    if authority_configured {
        payload["authorityContendedMilliseconds"] = json!(authority_contended_ms);
    }
    if let Some(durability) = observers.wal_durability {
        payload["walDurability"] = json!(durability);
    }
    if let Some(interval) = observers.wal_flush_interval_milliseconds {
        payload["walFlushIntervalMilliseconds"] = json!(interval);
    }
    if let Some(budget) = &observers.resource_budget {
        payload["resourceBudget"] = budget.clone();
    }
    payload
}

/// Nonzero commit-stage totals, same source as the TOFU_DB_FSYNC_STATS
/// shutdown dump. Stages with no calls are omitted; their names are fixed in
/// `fsync_stats::STAGE_NAMES`, so absence unambiguously means zero calls.
fn commit_stage_snapshot() -> Value {
    let stages = (0..fsync_stats::STAGE_NAMES.len())
        .map(|stage| {
            (
                fsync_stats::STAGE_NAMES[stage],
                fsync_stats::stage_calls(stage),
                fsync_stats::stage_nanos(stage),
                stage,
            )
        })
        .filter(|(_, calls, _, _)| *calls > 0)
        .map(|(stage, calls, total_nanos, index)| {
            let components = fsync_stats::COMPONENT_NAMES.iter().enumerate()
                .filter_map(|(component, name)| {
                    let calls = fsync_stats::component_stage_calls(component, index);
                    (calls > 0).then(|| json!({"component": name, "calls": calls,
                        "totalNanos": fsync_stats::component_stage_nanos(component, index)}))
                }).collect::<Vec<_>>();
            json!({"stage": stage, "calls": calls, "totalNanos": total_nanos,
                   "components": components})
        })
        .collect::<Vec<_>>();
    Value::Array(stages)
}

fn metrics_payload(observers: &ControlObservers) -> Value {
    let mut payload = json!({
        "contract": SYSTEM_METRICS_CONTRACT,
        "backend": "tofudb",
        "protocol": PROTOCOL_VERSION,
        "durability": fsync_stats::counters(),
        "commitStages": commit_stage_snapshot(),
        "requestProfile": crate::request_profile::snapshot(),
        "resourceExhaustionReasons": fsync_stats::resource_exhaustion_snapshot(),
    });
    if let Some(observer) = &observers.published_queries { payload["publishedReadView"] = observer.metrics(); }
    if let Some(observer) = &observers.decoded_chunk_cache {
        payload["decodedBlobChunkCache"] = serde_json::to_value(observer.metrics())
            .unwrap_or_else(|_| json!({"available":false}));
    }
    if let Some(observer) = &observers.canonical_json_cache {
        payload["canonicalJsonProofCache"] = serde_json::to_value(observer.metrics())
            .unwrap_or_else(|_| json!({"available":false}));
    }
    if let Some(durability) = observers.wal_durability {
        payload["walDurability"] = json!(durability);
    }
    if let Some(observer) = &observers.listener {
        if let Ok(metrics) = observer.metrics() {
            payload["listener"] = serde_json::to_value(metrics)
                .unwrap_or_else(|_| json!({"error": "listener metrics encode failed"}));
        }
    }
    if let Some(observer) = &observers.maintenance {
        match observer.metrics() {
            Ok(metrics) => {
                payload["maintenance"] = serde_json::to_value(metrics)
                    .unwrap_or_else(|_| json!({"error": "maintenance metrics encode failed"}));
            }
            Err(_) => payload["maintenance"] = json!({"error": "maintenance metrics poisoned"}),
        }
    }
    if let Some(observer) = &observers.packer {
        match observer.metrics() {
            Ok(Some(metrics)) => {
                payload["backgroundPacker"] = serde_json::to_value(metrics)
                    .unwrap_or_else(|_| json!({"error": "packer metrics encode failed"}));
            }
            Ok(None) => payload["backgroundPacker"] = json!({"stopped": true}),
            Err(_) => payload["backgroundPacker"] = json!({"error": "packer metrics poisoned"}),
        }
        if let Some(pacing) = observer.pacing() {
            payload["backgroundPackerPacing"] = serde_json::to_value(pacing)
                .unwrap_or_else(|_| json!({"error": "packer pacing encode failed"}));
        }
    }
    if let Some(observer) = &observers.online_gc {
        payload["onlineGarbageCollector"] = serde_json::to_value(observer.metrics())
            .unwrap_or_else(|_| json!({"error": "online GC metrics encode failed"}));
    }
    if let Some(observer) = &observers.online_backup {
        payload["onlineBackup"] = serde_json::to_value(observer.metrics())
            .unwrap_or_else(|_| json!({"error": "online backup metrics encode failed"}));
    }
    if let Some(observer) = &observers.authority_watch {
        payload["authority"] = json!({
            "contendedMilliseconds": observer.contended_milliseconds(),
            "wedged": observer.wedged(),
        });
    }
    if let Some(budget) = &observers.resource_budget {
        payload["resourceBudget"] = budget.clone();
    }
    payload
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::PROTOCOL_VERSION;

    fn control_request(operation: &str) -> Request {
        Request {
            correlation_id: [7; 16],
            deadline_unix_ms: 1_000,
            owner_id: 11,
            tenant_id: Some(7),
            command_id: None,
            schema_id: 1,
            operation: operation.to_owned(),
            payload: Vec::new(),
        }
    }

    #[test]
    fn health_payload_without_observers_is_ready_and_reports_contract() {
        let request = control_request(SYSTEM_HEALTH_OPERATION);
        let response = respond(&request, 1, None);
        assert_eq!(response.status, 0);
        assert_eq!(response.correlation_id, request.correlation_id);
        let payload: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(payload["contract"], json!(SYSTEM_HEALTH_CONTRACT));
        assert_eq!(payload["backend"], json!("tofudb"));
        assert_eq!(payload["protocol"], json!(PROTOCOL_VERSION));
        assert_eq!(payload["ready"], json!(true));
        assert_eq!(
            payload["components"]["maintenance"],
            json!({"configured": false, "healthy": true})
        );
    }

    #[test]
    fn metrics_payload_without_observers_reports_fixed_shape() {
        let request = control_request(SYSTEM_METRICS_OPERATION);
        let response = respond(&request, 1, None);
        assert_eq!(response.status, 0);
        let payload: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(payload["contract"], json!(SYSTEM_METRICS_CONTRACT));
        assert!(payload["durability"].is_object());
        assert!(payload["commitStages"].is_array());
        assert_eq!(payload["resourceExhaustionReasons"]["contract"],
            json!(crate::generated_storage_v2::NATIVE_RESOURCE_EXHAUSTION_CONTRACT));
        assert_eq!(payload["resourceExhaustionReasons"]["fixedCounterBytes"], 64);
        assert_eq!(payload["resourceExhaustionReasons"]["counters"].as_array().unwrap().len(), 8);
        assert!(payload.get("listener").is_none());
    }

    #[test]
    fn health_payload_honors_static_launch_fields() {
        let observers = ControlObservers {
            wal_durability: Some("normal"),
            wal_flush_interval_milliseconds: Some(10),
            resource_budget: Some(json!({"memoryCapacityBytes": 1024})),
            ..ControlObservers::default()
        };
        let request = control_request(SYSTEM_HEALTH_OPERATION);
        let response = respond(&request, 1, Some(&observers));
        let payload: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(payload["walDurability"], json!("normal"));
        assert_eq!(payload["walFlushIntervalMilliseconds"], json!(10));
        assert_eq!(
            payload["resourceBudget"],
            json!({"memoryCapacityBytes": 1024})
        );
    }

    #[test]
    fn health_payload_flips_ready_on_authority_wedge() {
        // 2026-09-21 FUSE brownout: every driver thread stayed alive and
        // every observer-handled component stayed green while one holder
        // wedged the authority for 13 minutes. The contention component
        // must flip `ready` exactly like a dead driver.
        let observers = ControlObservers {
            authority_watch: Some(AuthorityContentionObserver::for_test(60_000)),
            ..ControlObservers::default()
        };
        let request = control_request(SYSTEM_HEALTH_OPERATION);
        let response = respond(&request, 1, Some(&observers));
        let payload: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(payload["ready"], json!(false));
        assert_eq!(
            payload["components"]["authority"],
            json!({"configured": true, "healthy": false})
        );
        let contended = payload["authorityContendedMilliseconds"].as_u64().unwrap();
        assert!(contended >= 59_000, "contended={contended}");
    }

    #[test]
    fn health_payload_reports_contention_below_the_wedge_threshold() {
        let observers = ControlObservers {
            authority_watch: Some(AuthorityContentionObserver::for_test(500)),
            ..ControlObservers::default()
        };
        let request = control_request(SYSTEM_HEALTH_OPERATION);
        let response = respond(&request, 1, Some(&observers));
        let payload: Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(payload["ready"], json!(true));
        assert_eq!(
            payload["components"]["authority"],
            json!({"configured": true, "healthy": true})
        );
        assert!(payload["authorityContendedMilliseconds"].as_u64().unwrap() >= 400);
    }

    #[test]
    fn only_the_two_control_names_are_intercepted() {
        assert!(is_control_operation(SYSTEM_HEALTH_OPERATION));
        assert!(is_control_operation(SYSTEM_METRICS_OPERATION));
        assert!(!is_control_operation("system.reclaim"));
        assert!(!is_control_operation("conversation.list"));
    }
}
