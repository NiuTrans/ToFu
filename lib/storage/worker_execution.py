"""Pure private worker entry witnesses shared by application and SQL storage.

Entry: execution_witness validates the existing process_owner identity and
claim. Transaction owners store it once with Attempt entry; no process probe,
recovery policy, journal or execution authority is created by this codec.
"""
from collections.abc import Mapping

from lib.storage.errors import StorageError
from lib.storage.generated_worker_job_contract import (
    WORKER_EXECUTION_CONFIG_KEY, WORKER_EXECUTION_PROCESS_FIELDS,
    WORKER_EXECUTION_WITNESS_FORMAT,
)


def execution_witness(process, claim):
    if (not isinstance(claim, Mapping) or set(claim) != {'worker_id', 'fencing_token'}
            or not isinstance(claim['worker_id'], str) or not 0 < len(claim['worker_id']) <= 256
            or type(claim['fencing_token']) is not int or not 0 < claim['fencing_token'] < 2**63):
        raise StorageError('database_protocol_error', 'Worker entry requires its exact claim')
    if process is not None and (
            not isinstance(process, Mapping) or set(process) != WORKER_EXECUTION_PROCESS_FIELDS
            or type(process['pid']) is not int or process['pid'] < 1
            or any(not isinstance(process[key], str) or not process[key]
                   for key in WORKER_EXECUTION_PROCESS_FIELDS - {'pid'})
            or any(not process[key].isascii() or not process[key].isdecimal()
                   for key in ('started', 'pid_namespace'))):
        raise StorageError('database_protocol_error', 'Invalid worker process identity')
    return {'format': WORKER_EXECUTION_WITNESS_FORMAT, 'claim': dict(claim),
            'process': None if process is None else dict(process)}


__all__ = ['execution_witness', 'WORKER_EXECUTION_CONFIG_KEY']
