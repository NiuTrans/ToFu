import os
import sys
import time
import pytest
pytestmark = pytest.mark.unit
from tofu_environments import EnvironmentError
from tofu_environments.command import CommandRunner

@pytest.mark.skipif(os.name!='posix',reason='default POSIX transport')
def test_real_timeout_and_output_limit_are_uncertain_for_mutations():
    runner=CommandRunner(sys.executable,timeout=0.1)
    with pytest.raises(EnvironmentError) as err:runner.run(['-c','import time;time.sleep(5)'],mutation=True)
    assert err.value.uncertain
    runner=CommandRunner(sys.executable,timeout=3,output_limit=65536)
    with pytest.raises(EnvironmentError) as err:runner.run(['-c','print("x"*200000)'],mutation=True)
    assert err.value.code=='provider_output_limit' and err.value.uncertain

def test_cancel_before_spawn_is_certain():
    runner=CommandRunner('/nonexistent',cancelled=lambda:True)
    with pytest.raises(EnvironmentError) as err:runner.run(['build'],mutation=True)
    assert err.value.code=='cancelled' and not err.value.uncertain

@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-state evidence")
def test_successful_client_exit_reclaims_spawned_descendant(tmp_path):
    import os
    from pathlib import Path
    pid_file=tmp_path/'child.pid'
    script="import subprocess,sys; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);open(sys.argv[1],'w').write(str(child.pid))"
    runner=CommandRunner(sys.executable,timeout=5)
    runner.run(['-c',script,str(pid_file)])
    child=int(pid_file.read_text())
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        state=Path('/proc')/str(child)/'stat'
        if not state.exists() or state.read_text().split()[2]=='Z':break
        time.sleep(.02)
    else:
        os.kill(child,9)
        pytest.fail('Successful command left a live descendant')
