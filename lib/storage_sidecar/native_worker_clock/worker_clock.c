/* Shared PostgreSQL worker lease clock. Entry point: tofu_worker_lease_clock_v1.
 * GetNamedDSMSegment owns one lazy clock per database server lifetime. UTC offset
 * high-water + server monotonic elapsed + observation high-water match TofuDB.
 * No client timestamp, environment override, clock row or worker thread exists.
 */
#include "postgres.h"
#include "fmgr.h"
#include "lib/stringinfo.h"
#include "port/atomics.h"
#include "storage/dsm_registry.h"
#include "utils/builtins.h"
#include "utils/jsonb.h"
#include <time.h>
#include "generated_clock_contract.h"

#if PG_VERSION_NUM < TOFU_WORKER_CLOCK_MIN_PG_VERSION
#error "Tofu worker lease clock requires PostgreSQL 17 or newer"
#endif

PG_MODULE_MAGIC;
PG_FUNCTION_INFO_V1(tofu_worker_lease_clock_v1);

#define CLOCK_MAX_MS TOFU_WORKER_CLOCK_MAX_MS
#define CLOCK_STATE_MAGIC UINT64CONST(0x544f4655434c4b31)

typedef struct TofuWorkerClock {
    uint64 magic;
    uint64 origin_ms;
    pg_atomic_uint64 offset_ms;
    pg_atomic_uint64 observed_ms;
    unsigned char epoch[TOFU_WORKER_CLOCK_EPOCH_BYTES];
} TofuWorkerClock;
StaticAssertDecl(sizeof(TofuWorkerClock) <= TOFU_WORKER_CLOCK_MAX_STATE_BYTES,
                 "worker clock state exceeds its contract");

static uint64
read_clock_ms(clockid_t kind)
{
    struct timespec value;
    uint64 result;
    if (clock_gettime(kind, &value) != 0 || value.tv_sec < 0 ||
        value.tv_nsec < 0 || value.tv_nsec >= 1000000000L ||
        (uint64) value.tv_sec > CLOCK_MAX_MS / 1000)
        ereport(ERROR, (errmsg("Invalid server worker lease clock")));
    result = (uint64) value.tv_sec * 1000 + (uint64) value.tv_nsec / 1000000;
    if (result == 0 || result > CLOCK_MAX_MS)
        ereport(ERROR, (errmsg("Server worker lease clock exceeds its bound")));
    return result;
}

static uint64
atomic_high_water(pg_atomic_uint64 *target, uint64 candidate)
{
    uint64 observed = pg_atomic_read_u64(target);
    while (observed < candidate)
    {
        if (pg_atomic_compare_exchange_u64(target, &observed, candidate))
            return candidate;
    }
    return observed;
}

static void
initialize_clock(void *memory)
{
    TofuWorkerClock *clock = memory;
    uint64 wall;
    wall = read_clock_ms(CLOCK_REALTIME);
    clock->origin_ms = read_clock_ms(CLOCK_MONOTONIC);
    if (!pg_strong_random(clock->epoch, sizeof(clock->epoch)))
        ereport(ERROR, (errmsg("Cannot initialize server worker lease identity")));
    pg_atomic_init_u64(&clock->offset_ms, wall);
    pg_atomic_init_u64(&clock->observed_ms, wall);
    clock->magic = CLOCK_STATE_MAGIC;
}

Datum
tofu_worker_lease_clock_v1(PG_FUNCTION_ARGS)
{
    TofuWorkerClock *clock;
    uint64 monotonic, elapsed, wall, offset, candidate, now;
    bool found;
    StringInfoData result;
    char epoch[TOFU_WORKER_CLOCK_EPOCH_BYTES * 2 + 1];
    static const char hex[] = "0123456789abcdef";
    int index;

    clock = GetNamedDSMSegment("tofu.worker-lease-clock/v1", sizeof(*clock),
                              initialize_clock, &found);
    if (clock->magic != CLOCK_STATE_MAGIC)
        ereport(ERROR, (errmsg("Invalid server worker lease state")));
    /* UTC first prevents a scheduling pause from inflating the offset. */
    wall = read_clock_ms(CLOCK_REALTIME);
    monotonic = read_clock_ms(CLOCK_MONOTONIC);
    if (monotonic < clock->origin_ms)
        ereport(ERROR, (errmsg("Server worker monotonic clock moved backwards")));
    elapsed = monotonic - clock->origin_ms;
    offset = atomic_high_water(&clock->offset_ms, wall > elapsed ? wall - elapsed : 0);
    if (offset > CLOCK_MAX_MS - elapsed)
        ereport(ERROR, (errmsg("Server worker lease time exceeds its bound")));
    candidate = offset + elapsed;
    now = atomic_high_water(&clock->observed_ms, candidate);
    for (index = 0; index < TOFU_WORKER_CLOCK_EPOCH_BYTES; ++index)
    {
        epoch[index * 2] = hex[clock->epoch[index] >> 4];
        epoch[index * 2 + 1] = hex[clock->epoch[index] & 15];
    }
    epoch[TOFU_WORKER_CLOCK_EPOCH_BYTES * 2] = '\0';
    initStringInfo(&result);
    appendStringInfo(&result,
        "{\"format\":\"" TOFU_WORKER_CLOCK_FORMAT "\",\"epoch\":\"%s\","
        "\"nowMs\":" UINT64_FORMAT ",\"utcMs\":" UINT64_FORMAT ",\"stateBytes\":%zu}",
        epoch, now, wall, sizeof(*clock));
    PG_RETURN_DATUM(DirectFunctionCall1(jsonb_in, CStringGetDatum(result.data)));
}
