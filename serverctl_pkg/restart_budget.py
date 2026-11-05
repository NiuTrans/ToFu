"""Composed deadlines for the shared artifact preparation/restart chain.

Standard-library control callers import these fixed durations. Outer clocks
cover queueing, both builders and joined cleanup; no stage renews an inner
deadline and no hardware probe sizes a deadline.
"""

NATIVE_LOCK_WAIT_SECONDS = 600
NATIVE_WORK_SECONDS = 600
NATIVE_PREPARATION_SECONDS = NATIVE_LOCK_WAIT_SECONDS + NATIVE_WORK_SECONDS
NATIVE_CHILD_SECONDS = NATIVE_PREPARATION_SECONDS + 30
# Fixed parser allowance includes interpreter startup on the source volume;
# every parse is additionally bounded by the preparation's remaining time.
NATIVE_MANIFEST_PARSE_SECONDS = 30.0
FRONTEND_LOCK_WAIT_SECONDS = 60.0
FRONTEND_BUILD_SECONDS = 600.0
ARTIFACT_PREPARATION_SECONDS = NATIVE_CHILD_SECONDS + FRONTEND_LOCK_WAIT_SECONDS + FRONTEND_BUILD_SECONDS + 120
# First admission may import from a slow installed environment. This entire
# budget runs before the serving worker is drained; expiration preserves it.
NATIVE_SERVICE_PREPARATION_SECONDS = 120.0
# A replaced or stopped host drains its native daemon: maintenance stop,
# overlay merge and the closing checkpoint. On the FUSE authority a clean drain
# measured up to 13.4 s + 17.6 s; a kill inside the checkpoint moved 51-102 s of
# WAL replay onto the next open (2026-10-27). The kill stays a wedge backstop.
NATIVE_DAEMON_STOP_SECONDS = 90.0
# The daemon drain, then the host's startup-thread join (5 s) and record write.
NATIVE_SERVICE_STOP_SECONDS = NATIVE_DAEMON_STOP_SECONDS + 10.0
# Maximum configured worker shutdown, manager escalation, storage lease release,
# then command/transport overhead. Readiness observation has its own clock.
LIFECYCLE_REQUEST_SECONDS = (ARTIFACT_PREPARATION_SECONDS + 300 + 5 + 10 + 45
                             + NATIVE_SERVICE_PREPARATION_SECONDS + NATIVE_SERVICE_STOP_SECONDS)
