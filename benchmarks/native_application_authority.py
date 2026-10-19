"""One explicit disposable authority witness for paid ROOT fixture collectors.

Native selection requires a supplied current-source engine and content pin.
Collectors check every worker against this witness; no deployment lookup or
production storage selection occurs here. SQLite remains the default fixture.
"""
import hashlib


APPLICATION_BACKENDS=('sqlite','daemon','embedded')


def authority_witness(backend, engine, root):
    if backend=='sqlite':
        return {'backend':'sqlite','transport':'sidecar','runtime_observation_required':True}
    if backend not in ('daemon','embedded') or engine is None:
        raise ValueError('native authority requires explicit storage engine')
    from serverctl_pkg.native_engine import verify_current,verify_library
    engine=engine.resolve()
    verify_current(engine,root)
    witness={'backend':'tofudb','transport':backend,'runtime_observation_required':True,
        'engine_sha256':hashlib.sha256(engine.read_bytes()).hexdigest()}
    if backend=='embedded':
        verify_library(engine)
        witness['library_sha256']=hashlib.sha256((engine.parent/'libtofu_db.so').read_bytes()).hexdigest()
    return witness


def check_authority_files(engine, expected):
    """Freeze both deployed members; daemon and embedded are distinct cohorts."""
    if expected['backend']=='sqlite':return
    engine=engine.resolve()
    if hashlib.sha256(engine.read_bytes()).hexdigest()!=expected['engine_sha256']:
        raise ValueError('storage engine changed during cohort')
    if expected['transport']=='embedded' and hashlib.sha256((engine.parent/'libtofu_db.so').read_bytes()).hexdigest()!=expected['library_sha256']:
        raise ValueError('embedded storage library changed during cohort')


def observe_runtime_authority(status, expected):
    """Check the ready supervisor's actual transport before accepting work."""
    if not isinstance(status,dict):raise ValueError('missing storage runtime observation')
    backend=status.get('backend')
    transport=status.get('transport') or ('sidecar' if backend=='sqlite' else 'daemon')
    pid=status.get('pid')
    child=type(pid) is int and pid>0
    if (status.get('ready') is not True or backend!=expected['backend']
            or transport!=expected['transport']
            or (pid is not None if transport=='embedded' else not child)):
        raise ValueError('actual storage runtime differs from frozen authority')
    return {'ready':True,'backend':backend,'transport':transport,'pid':pid}


def check_authority(row, expected):
    if expected.get('transport')=='embedded':
        pins=[expected.get(key) for key in ('engine_sha256','library_sha256')]
        if (expected.get('runtime_observation_required') is not True
                or any(not isinstance(pin,str) or len(pin)!=64
                       or any(ch not in '0123456789abcdef' for ch in pin) for pin in pins)):
            raise ValueError('embedded authority requires both pins and runtime observation')
    if row.get('storage_authority')!=expected:
        raise ValueError('worker storage authority differs from frozen cohort')
    if expected.get('runtime_observation_required') is True:
        observe_runtime_authority(row.get('storage_runtime'),expected)
        if row.get('storage_runtime_closed') is not True:
            raise ValueError('storage runtime close was not witnessed')


def check_worker_reuse(row, expected):
    """Freeze requested policy and prove actual native physical-worker reuse."""
    if type(row.get('worker_reuse')) is not bool or row['worker_reuse'] != expected:
        raise ValueError('changed native worker reuse policy')
    if not expected or row['arm'] != 'native':
        return
    witness = row.get('worker_reuse_witness') or {}
    invocations = 1 if row['mode'] == 'cold' else 1 + row['warmups'] + len(row['raw'])
    pids = witness.get('pids')
    if (type(witness.get('physical_workers')) is not int or witness['physical_workers'] != 1
            or type(witness.get('idle_workers')) is not int or witness['idle_workers'] != 1
            or not isinstance(pids, list) or len(pids) != 1
            or type(pids[0]) is not int or pids[0] <= 0
            or type(witness.get('invocations')) is not int or witness['invocations'] != invocations
            or witness.get('all_ready') is not True):
        raise ValueError('invalid native worker reuse witness')


COMPLETION_OBSERVERS=('poll','worker-future')


def check_completion_observer(row, expected):
    """A shared observer boundary is required before timing/memory comparison."""
    if type(expected) is not str or expected not in COMPLETION_OBSERVERS:
        raise ValueError('invalid completion observer')
    actual=row.get('completion_observer','poll')
    if type(actual) is not str or actual not in COMPLETION_OBSERVERS:
        raise ValueError('invalid completion observer')
    if actual!=expected:
        raise ValueError('changed completion observer')


def check_usage_summary(row, expected):
    """Freeze terminal projection selection and prove actual native delivery."""
    actual = row.get('native_usage_summary', False)
    if type(expected) is not bool or type(actual) is not bool or actual != expected:
        raise ValueError('changed native usage summary selection')
    if not expected or row['arm'] != 'native':
        return
    witness = row.get('usage_summary_witness') or {}
    operations = 1 if row['mode'] == 'cold' else 1 + row['warmups'] + len(row['raw'])
    if (type(witness.get('requests')) is not int or witness['requests'] < operations
            or type(witness.get('paid_frozen_requests')) is not int
            or not 0 <= witness['paid_frozen_requests'] <= witness['requests']):
        raise ValueError('invalid native usage summary witness')


def check_image_preparation(row, expected):
    """Require explicit request-scope image policy; no pre-clock admission."""
    actual=row.get('native_image_preparation',False)
    if type(expected) is not bool or type(actual) is not bool or actual!=expected:
        raise ValueError('changed native image preparation policy')
    if not expected:return
    witness=row.get('image_preparation_witness') or {}
    if witness.get('before_first_operation') is not False:
        raise ValueError('native image admitted before operation clock')
    if witness.get('after_first_operation') is not (row['arm']=='native'):
        raise ValueError('missing native image preparation witness')


def check_terminal_reference(row,expected):
    """Freeze final-reference selection and actual accepted SDK frames."""
    selected=row.get('native_terminal_reference',False)
    if type(expected) is not bool or type(selected) is not bool or selected!=expected:
        raise ValueError('changed native terminal reference selection')
    frames=row.get('terminal_reference_frames',0)
    operations=1 if row.get('mode')=='cold' else 1+row.get('warmups',0)+len(row.get('raw',[]))
    wanted=operations if expected and row.get('arm')=='native' else 0
    if type(frames) is not int or frames!=wanted:
        raise ValueError('invalid native terminal reference witness')


def check_token_library(row, expected):
    """Selected host counts must load inside the clock and retire without Python BPE."""
    actual=row.get('native_token_count',False)
    if type(expected) is not bool or type(actual) is not bool or actual!=expected:
        raise ValueError('changed native token library selection')
    if not expected:return
    witness=row.get('token_library_witness') or {}
    native=row.get('arm')=='native'
    pin=row.get('token_library_sha256')
    if (not isinstance(pin,str) or len(pin)!=64 or any(ch not in '0123456789abcdef' for ch in pin)
            or witness.get('before_first_operation') is not False
            or witness.get('python_encoders_before') != []
            or witness.get('after_first_operation') is not native
            or witness.get('closed') is not True
            or (native and (witness.get('encodings') != ['cl100k_base']
                            or witness.get('python_encoders_after') != []))):
        raise ValueError('invalid native token library witness')
