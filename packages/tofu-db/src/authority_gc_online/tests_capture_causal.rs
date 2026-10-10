//! Opt-in same-volume placement controls. The injected delay follows an
//! actual cold source metadata call; SIGKILL occurs only after a durable ACK.
use super::*;
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

const SOURCE_RESPONSE_DELAY: Duration = Duration::from_millis(200);
const CHILD_TEST: &str = "authority_gc_online::tests_capture::causal::gc_capture_same_volume_child";

#[test]
#[ignore = "child of paired_gc_capture_same_volume_latency; never opens configured authority"]
fn gc_capture_same_volume_child() {
    let Some(path) = std::env::var_os("TOFU_TEST_GC_CAPTURE_CHILD_DIR") else { return; };
    let old_placement = std::env::var("TOFU_TEST_GC_CAPTURE_OLD_PLACEMENT").unwrap() == "1";
    let vfs = Arc::new(SourceMetadataVfs::new(Arc::new(RealVfs)));
    let (database, source) = fixture(Path::new(&path), vfs.clone());
    let (entered, observed) = mpsc::channel();
    let release = Arc::new((Mutex::new(false), Condvar::new()));
    vfs.arm(source, Some(MetadataAction::Block(entered, release.clone())));
    let worker = {
        let database = database.clone();
        std::thread::spawn(move || {
            let collector = OnlineGarbageCollector::new(test_budget());
            let capture = {
                let guard = database.lock().unwrap();
                let mut capture = guard.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
                if old_placement {
                    // Exact source selection/validator, placed inside the old
                    // capture hold. This is an algorithm placement control in
                    // one test binary, not a frozen release ABBA comparison.
                    let selection = capture.snapshot.selected_history_metadata.take().unwrap();
                    capture.blocks.visit_history_payload_metadata_selection(selection, |id| {
                        capture.snapshot.history_payload_metadata_ids.push(id); Ok(())
                    }).unwrap();
                    capture.snapshot.history_metadata_resident_bytes = 0;
                }
                capture
            };
            let mut epoch = unmarked_epoch(capture, &collector);
            let stop = AtomicBool::new(false);
            collector.build_marks(&mut epoch, &stop).unwrap();
            collector.scan(&mut epoch, &stop).unwrap();
            collector.delete_confirmed(&database, &mut epoch, &stop).unwrap();
            collector.cleanup_epoch(&mut epoch);
        })
    };
    observed.recv_timeout(Duration::from_secs(10)).unwrap();
    let held_during_actual_source_delay = database.try_lock().is_err();
    assert_eq!(held_during_actual_source_delay, old_placement);
    let releaser = std::thread::spawn(move || {
        std::thread::sleep(SOURCE_RESPONSE_DELAY);
        *release.0.lock().unwrap() = true; release.1.notify_all();
    });
    let started = Instant::now();
    let mut guard = database.lock().unwrap();
    let authority_wait_ms = started.elapsed().as_secs_f64() * 1000.0;
    put(&mut guard, b"foreground-durable-ack");
    let acknowledgement_ms = started.elapsed().as_secs_f64() * 1000.0;
    drop(guard);
    worker.join().unwrap(); releaser.join().unwrap();
    assert!(vfs.hits() >= 1);
    println!("GC_CAPTURE_CAUSAL {}", serde_json::json!({
        "source_sha256":env!("TOFUDB_SOURCE_SHA256"),
        "old_placement":old_placement,"source_response_delay_ms":200,
        "held_during_actual_source_delay":held_during_actual_source_delay,
        "authority_wait_ms":authority_wait_ms,"foreground_ack_ms":acknowledgement_ms,
        "actual_target_metadata_calls":vfs.hits(),"raw_cache_capacity_bytes":0,
        "scope":"injected cold-source response placement; not normal end-to-end latency"
    }));
    std::io::stdout().flush().unwrap();
    // The parent SIGKILLs this process: no Engine/Authority Drop, checkpoint,
    // graceful worker stop, or other durability repair can precede reopen.
    loop { std::thread::park(); }
}

struct KillOnDrop(std::process::Child);
impl Drop for KillOnDrop {
    fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
}

#[test]
#[ignore = "requires explicit same-volume scratch parent; real SIGKILL, four diagnostic samples"]
fn paired_gc_capture_same_volume_latency() {
    let parent = std::env::var_os("TOFU_TEST_GC_CAPTURE_SCRATCH_PARENT")
        .expect("set a disposable scratch parent on the selected authority volume");
    std::fs::create_dir_all(&parent).unwrap();
    let mut reports = Vec::new();
    for repeat in 0..2 {
        for old_placement in [true, false] {
            let directory = tempfile::Builder::new().prefix("gc-capture-causal-").tempdir_in(&parent).unwrap();
            let mut child = KillOnDrop(Command::new(std::env::current_exe().unwrap())
                .args(["--exact", CHILD_TEST, "--ignored", "--nocapture"])
                .env("TOFU_TEST_GC_CAPTURE_CHILD_DIR", directory.path())
                .env("TOFU_TEST_GC_CAPTURE_OLD_PLACEMENT", if old_placement { "1" } else { "0" })
                .stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn().unwrap());
            let stdout = child.0.stdout.take().unwrap();
            let (sent, received) = mpsc::channel();
            let reader = std::thread::spawn(move || {
                for line in BufReader::new(stdout).lines() {
                    let line = line.unwrap();
                    if let Some(body) = line.strip_prefix("GC_CAPTURE_CAUSAL ") {
                        sent.send(body.to_owned()).unwrap(); return;
                    }
                }
            });
            let body = received.recv_timeout(Duration::from_secs(60)).expect("child must reach verified-body durable ACK");
            let mut report: serde_json::Value = serde_json::from_str(&body).unwrap();
            child.0.kill().unwrap();
            let exit = child.0.wait().unwrap(); assert!(!exit.success());
            #[cfg(unix)] {
                use std::os::unix::process::ExitStatusExt;
                assert_eq!(exit.signal(), Some(9));
            }
            reader.join().unwrap();
            let reopened = AuthorityDatabase::open_with_vfs(directory.path(), Arc::new(RealVfs)).unwrap();
            let mut transaction = reopened.begin(7, 11).unwrap();
            assert_eq!(reopened.entity_get(&mut transaction, &key()).unwrap().unwrap(), b"foreground-durable-ack");
            report["repeat"] = serde_json::json!(repeat);
            report["sigkill_exact_recovery"] = serde_json::json!(true);
            #[cfg(unix)] {
                use std::os::unix::fs::MetadataExt;
                report["scratch_device"] = serde_json::json!(std::fs::metadata(directory.path()).unwrap().dev());
            }
            if old_placement { assert!(report["authority_wait_ms"].as_f64().unwrap() >= 100.0); }
            else { assert!(report["authority_wait_ms"].as_f64().unwrap() < 100.0); }
            reports.push(report);
        }
    }
    println!("GC_CAPTURE_PAIRED {}", serde_json::json!({"reports":reports,"timings_are_diagnostic":true}));
}
