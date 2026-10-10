/* Test-only UTC injection around the retained production clock algorithm.
 * The wrapper initializes its private named delta state before entering the
 * production initializer, avoiding nested registry-lock acquisition. The built
 * library exists only inside an owned test cluster; production exports no setter.
 */
#include "postgres.h"
#include "fmgr.h"
#include "port/atomics.h"
#include "storage/dsm_registry.h"
#include <time.h>

#define TEST_DELTA_BIAS UINT64CONST(86400000)
typedef struct TestWallDelta {
    pg_atomic_uint64 encoded;
    pg_atomic_uint64 next_probe_delay_ms;
} TestWallDelta;
static TestWallDelta *test_delta;

static void
initialize_test_delta(void *memory)
{
    TestWallDelta *value = memory;
    pg_atomic_init_u64(&value->encoded, TEST_DELTA_BIAS);
    pg_atomic_init_u64(&value->next_probe_delay_ms, 0);
}

static void
attach_test_delta(void)
{
    bool found;
    test_delta = GetNamedDSMSegment("tofu.test.worker-wall-delta/v1",
        sizeof(*test_delta), initialize_test_delta, &found);
}

static int
injected_clock_gettime(clockid_t kind, struct timespec *value)
{
    if (kind == CLOCK_REALTIME && test_delta != NULL)
    {
        uint64 delay = pg_atomic_exchange_u64(&test_delta->next_probe_delay_ms, 0);
        if (delay > 0)
            pg_usleep((long) delay * 1000);
    }
    int result = clock_gettime(kind, value);
    if (result == 0 && kind == CLOCK_REALTIME)
    {
        uint64 encoded;
        int64 delta, nanos;
        if (test_delta == NULL)
            ereport(ERROR, (errmsg("Private clock injection is not attached")));
        encoded = pg_atomic_read_u64(&test_delta->encoded);
        delta = (int64) encoded - (int64) TEST_DELTA_BIAS;
        nanos = (int64) value->tv_sec * INT64CONST(1000000000) + value->tv_nsec
            + delta * INT64CONST(1000000);
        if (nanos <= 0)
            ereport(ERROR, (errmsg("Invalid private test wall clock")));
        value->tv_sec = nanos / INT64CONST(1000000000);
        value->tv_nsec = nanos % INT64CONST(1000000000);
    }
    return result;
}

#define clock_gettime injected_clock_gettime
#define tofu_worker_lease_clock_v1 test_actual_worker_clock
#include "../../lib/storage_sidecar/native_worker_clock/worker_clock.c"
#undef tofu_worker_lease_clock_v1
#undef clock_gettime

PG_FUNCTION_INFO_V1(tofu_worker_lease_clock_v1);
Datum
tofu_worker_lease_clock_v1(PG_FUNCTION_ARGS)
{
    attach_test_delta();
    return test_actual_worker_clock(fcinfo);
}

PG_FUNCTION_INFO_V1(test_force_worker_clock_wall_delta);
Datum
test_force_worker_clock_wall_delta(PG_FUNCTION_ARGS)
{
    int64 delta = PG_GETARG_INT64(0);
    if (delta < -(int64) TEST_DELTA_BIAS || delta > (int64) TEST_DELTA_BIAS)
        ereport(ERROR, (errmsg("Private clock delta exceeds its test bound")));
    attach_test_delta();
    pg_atomic_write_u64(&test_delta->encoded, (uint64) (delta + (int64) TEST_DELTA_BIAS));
    PG_RETURN_INT64(delta);
}

PG_FUNCTION_INFO_V1(test_force_worker_clock_probe_delay);
Datum
test_force_worker_clock_probe_delay(PG_FUNCTION_ARGS)
{
    int64 delay = PG_GETARG_INT64(0);
    if (delay < 0 || delay > 250)
        ereport(ERROR, (errmsg("Private clock probe delay exceeds its test bound")));
    attach_test_delta();
    pg_atomic_write_u64(&test_delta->next_probe_delay_ms, (uint64) delay);
    PG_RETURN_INT64(delay);
}
