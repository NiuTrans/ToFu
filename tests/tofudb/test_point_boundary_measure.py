"""Attribution failures must retire owned authorities and diagnostic wrappers."""
from types import SimpleNamespace

import pytest

from benchmarks.tofudb_point_boundary_measure import caller_edges, measure

pytestmark = pytest.mark.unit


def test_incorrect_timed_value_fails_measurement_and_closes_authority():
    class Engine:
        closed = False
        reads = 0
        values = {}

        def start(self):
            pass

        def execute(self, operation, payload, command):
            if operation == "record.put":
                self.values[payload["key"]] = payload["value"]
                return {}
            self.reads += 1
            return self.values[payload["key"]] if self.reads <= 256 else {"incorrect": True}

        def stop(self):
            self.closed = True

    engine = Engine()
    with pytest.raises(ValueError, match="differs from its fixture"):
        measure(engine, count=10, native=False)
    assert engine.closed


def test_native_failure_restores_caller_wrappers_before_cleanup():
    import tofu_db.embedded as sdk
    dumps, loads = sdk._dumps, sdk._loads

    def fail(*args):
        raise RuntimeError("native transport failure")

    library = SimpleNamespace(tofudb_embed_execute=fail)
    engine = SimpleNamespace(_embedded=SimpleNamespace(_lib=library))
    with pytest.raises(RuntimeError, match="native transport failure"):
        with caller_edges(engine, True):
            engine._embedded._lib.tofudb_embed_execute()
    assert engine._embedded._lib is library
    assert sdk._dumps is dumps and sdk._loads is loads


@pytest.mark.parametrize("history", [3, 25])
def test_window_fixture_uses_retained_sql_workload_and_exact_stable_values(history):
    from scripts.tofudb_perf_compare import RawSqliteEngine
    reports=[]
    for _ in range(2):
        engine=RawSqliteEngine()
        reports.append(measure(engine,count=3,native=False,workload="window",history_turns=history))
        assert engine._dir is None and engine._conn is None
    first, second=reports
    assert first["fixture_sha256"]==second["fixture_sha256"]
    assert first["operation"]=="turn.sync.snapshot"
    assert first["exact_values_checked"]==3 and first["window_turns"]==min(20,history)
    assert "not equivalent complete response work" in first["measurement_scope"]


def test_corrupt_timed_window_fails_and_retires_real_authority():
    from scripts.tofudb_perf_compare import RawSqliteEngine
    class Corrupt(RawSqliteEngine):
        reads=0
        def execute(self, operation, payload, command):
            result=super().execute(operation,payload,command)
            if operation=="turn.sync.snapshot":
                self.reads+=1
                if self.reads>256:result["turns"].reverse()
            return result
    engine=Corrupt()
    with pytest.raises(ValueError,match="window read differs"):
        measure(engine,count=3,native=False,workload="window",history_turns=25)
    assert engine._dir is None and engine._conn is None


def test_native_window_continuation_and_exact_content_are_independent_checks():
    from benchmarks.tofudb_point_boundary_measure import _window_fixture
    from scripts.tofudb_perf_compare import RawSqliteEngine
    import copy
    setup,operation,payloads,values,check=_window_fixture(25,True)
    engine=RawSqliteEngine();engine.start()
    try:
        setup(engine)
        rows=engine.execute(operation,payloads[0],False)["turns"]
    finally:
        engine.stop()
    result={"turns":[{"turnId":t["turn_id"],"projection":{"content":t["content"]},"actor":t["actor"],"createdAt":t["created_at"],"ordinal":i} for i,t in enumerate(rows,5)],
            "turnWindow":{"totalTurns":25,"hasMore":True,"nextBeforeOrdinal":5}}
    check(result,0)
    for field,value in [("totalTurns",24),("hasMore",False),("nextBeforeOrdinal",6)]:
        broken=copy.deepcopy(result);broken["turnWindow"][field]=value
        with pytest.raises(ValueError,match="window read differs"):check(broken,0)
    broken=copy.deepcopy(result);broken["turns"][0]["ordinal"]=6
    with pytest.raises(ValueError,match="window read differs"):check(broken,0)
    result["turns"][0]["projection"]["content"]="wrong"
    with pytest.raises(ValueError,match="window read differs"):check(result,0)
