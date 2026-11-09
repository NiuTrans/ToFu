"""Private, incarnation-bound command controls for the resident executor.

One application-owned listener calls an injected TaskRuntime control owner.
Connections carry no queued work or retry authority; entered callbacks remain
physically owned until joined, including when their network observer leaves.
"""
import asyncio
import json
import secrets
import struct
import time
import uuid

from lib.log import get_logger
from tofu_db._loopback import create_connection

logger = get_logger(__name__)

from lib.tasks_pkg.generated_command_control import (
    ACKNOWLEDGEMENT_SECONDS, FORMAT, METHODS, OWNER_METHODS, REQUEST_FIELDS, RESPONSE_FIELDS,
)

_LENGTH = struct.Struct('!I')
# The boolean outcome of each task-addressed method and its optional fields.
_RESULT_FIELDS = {
    'interrupt': ('interrupted', {'reason', 'pid'}),
    'background': ('backgrounded', {'reason'}),
    'swarm_abort': ('aborted', {'reason'}),
    'swarm_agent_stop': ('stopped', {'reason', 'status'}),
}


class CommandControlUnavailable(RuntimeError):
    """No acknowledged result from the selected execution incarnation."""


def _encode(value):
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return _LENGTH.pack(len(data)) + data


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Execution control acknowledgement elapsed')
    return remaining


def _read_exact(connection, count, deadline):
    chunks = bytearray()
    while len(chunks) < count:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(count - len(chunks))
        if not chunk:
            raise OSError('Execution control connection closed before acknowledgement')
        chunks.extend(chunk)
    return bytes(chunks)


def _valid_histogram(row):
    return (isinstance(row, dict) and set(row) == {'labels', 'buckets', 'counts', 'sum', 'count'}
            and isinstance(row['labels'], dict)
            and all(isinstance(key, str) and isinstance(value, str)
                    for key, value in row['labels'].items())
            and isinstance(row['buckets'], list) and isinstance(row['counts'], list)
            and len(row['buckets']) == len(row['counts'])
            and all(type(bound) in (int, float) for bound in row['buckets'])
            and all(type(count) is int and count >= 0 for count in row['counts'])
            and type(row['count']) is int and row['count'] >= 0
            and type(row['sum']) in (int, float))


def _valid_result(method, result):
    if method == 'storage_rpc_metrics':
        return (isinstance(result, dict) and set(result) == {'histograms'}
                and isinstance(result['histograms'], list)
                and all(_valid_histogram(row) for row in result['histograms']))
    if method in OWNER_METHODS:
        return (isinstance(result, dict) and set(result) == {'swarms', 'backgroundCommands'}
                and all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                        for rows in result.values()))
    if result is None:
        return True  # The authenticated owner has no such task or session.
    key, optional = _RESULT_FIELDS[method]
    if (not isinstance(result, dict) or type(result.get(key)) is not bool
            or set(result) - {key, *optional}
            or any(name in result and not isinstance(result[name], str)
                   for name in optional - {'pid'})):
        return False
    if method != 'interrupt':
        return True
    return (type(result.get('pid')) is int and result['pid'] > 0
            if result[key] else 'pid' not in result)


def call(endpoint, method, task_id, *, user_id, params=None):
    """One delivery attempt; a missing acknowledgement is never replayed."""
    if (not isinstance(endpoint, dict) or endpoint.get('format') != FORMAT
            or endpoint.get('host') != '127.0.0.1'
            or type(endpoint.get('port')) is not int or not 0 < endpoint['port'] < 65536
            or not isinstance(endpoint.get('serviceId'), str)
            or not isinstance(endpoint.get('token'), str)):
        raise CommandControlUnavailable('Execution control endpoint is unavailable')
    identity = uuid.uuid4().hex
    deadline = time.monotonic() + ACKNOWLEDGEMENT_SECONDS
    request = {'format': FORMAT, 'serviceId': endpoint['serviceId'], 'token': endpoint['token'],
        'requestId': identity, 'ownerId': user_id, 'method': method, 'taskId': task_id,
        'params': {} if params is None else params,
        'deadlineUnixMs': int((time.time() + ACKNOWLEDGEMENT_SECONDS) * 1000)}
    try:
        with create_connection((endpoint['host'], endpoint['port']), _remaining(deadline)) as connection:
            connection.settimeout(_remaining(deadline))
            connection.sendall(_encode(request))
            length = _LENGTH.unpack(_read_exact(connection, _LENGTH.size, deadline))[0]
            response = json.loads(_read_exact(connection, length, deadline))
        if (not isinstance(response, dict) or set(response) != set(RESPONSE_FIELDS)
                or response['format'] != FORMAT or response['requestId'] != identity
                or response['error'] is not None
                or not _valid_result(method, response['result'])):
            raise CommandControlUnavailable('Execution control was not acknowledged')
        return response['result']
    except (OSError, ValueError, TypeError, struct.error) as error:
        raise CommandControlUnavailable('Execution control outcome is unconfirmed') from error


def _valid_request(request):
    if (not isinstance(request, dict) or set(request) != set(REQUEST_FIELDS)
            or request['format'] != FORMAT or request['method'] not in METHODS
            or type(request['ownerId']) is not int or request['ownerId'] <= 0
            or not isinstance(request['taskId'], str)
            # Owner methods address no task; every other method names one.
            or (request['taskId'] == '') != (request['method'] in OWNER_METHODS)
            or not isinstance(request['requestId'], str) or not request['requestId']
            or type(request['deadlineUnixMs']) is not int
            or not isinstance(request['params'], dict)):
        return False
    params, method = request['params'], request['method']
    if method == 'background':
        return (set(params) == {'round_num', 'tool_call_id'}
                and type(params['round_num']) is int and params['round_num'] >= 1
                and isinstance(params['tool_call_id'], str) and bool(params['tool_call_id']))
    if method == 'swarm_agent_stop':
        return (set(params) == {'agent_id'}
                and isinstance(params['agent_id'], str) and bool(params['agent_id']))
    return params == {}


async def _join_owned(awaitable):
    """Repeated observer cancellation never abandons entered physical work."""
    future = asyncio.ensure_future(awaitable)
    interrupted = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as error:
            interrupted = interrupted or error
        except BaseException as error:
            logger.debug('execution control joined observer failed: %s', error)
            break
    try:
        result = future.result()
    except BaseException as error:
        if interrupted is not None:
            raise BaseExceptionGroup('Execution control interrupted and failed', [interrupted, error]) from None
        raise
    if interrupted is not None:
        raise interrupted
    return result


async def _invoke_joined(apply, request):
    return await _join_owned(asyncio.to_thread(apply,
        request['method'], request['taskId'], user_id=request['ownerId'],
        deadline_unix_ms=request['deadlineUnixMs'], **request['params']))


class CommandControlServer:
    def __init__(self, service_id, apply):
        self._service_id, self._apply = service_id, apply
        self._token = secrets.token_hex(32)
        self._server = None
        self._handlers = set()
        self._closing = False

    @property
    def active_count(self):
        return len(self._handlers)

    async def start(self):
        if self._server is not None or self._closing:
            raise RuntimeError('Execution command listener already has a lifecycle')
        self._server = await asyncio.start_server(self._accept, host='127.0.0.1', port=0)
        return {'format': FORMAT, 'serviceId': self._service_id, 'token': self._token,
                'host': '127.0.0.1', 'port': self._server.sockets[0].getsockname()[1]}

    def _accept(self, reader, writer):
        if self._closing:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _serve(self, reader, writer):
        identity = ''
        result, error = None, 'control_refused'
        deadline = time.monotonic() + ACKNOWLEDGEMENT_SECONDS
        try:
            async with asyncio.timeout(_remaining(deadline)):
                length = _LENGTH.unpack(await reader.readexactly(_LENGTH.size))[0]
                request = json.loads(await reader.readexactly(length))
            if isinstance(request, dict) and isinstance(request.get('requestId'), str):
                identity = request['requestId']
            authenticated = (isinstance(request, dict)
                and request.get('serviceId') == self._service_id
                and isinstance(request.get('token'), str)
                and secrets.compare_digest(request['token'], self._token))
            if not authenticated or not _valid_request(request):
                error = 'control_refused'
            elif self._closing or request['deadlineUnixMs'] <= time.time_ns() // 1_000_000:
                error = 'control_unavailable'
            else:
                result = await _invoke_joined(self._apply, request)
                error = None
        except (OSError, ValueError, TypeError, asyncio.IncompleteReadError):
            error = 'control_refused'
        except Exception as exc:
            logger.debug('execution control request failed: %s', exc)
            error = 'control_failed'
        finally:
            try:
                remaining = _remaining(deadline)
                writer.write(_encode({'format': FORMAT, 'requestId': identity,
                                      'result': result, 'error': error}))
                async with asyncio.timeout(remaining):
                    await writer.drain()
            except (OSError, TimeoutError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.CancelledError):
                    pass

    async def close(self):
        self._closing = True
        await _join_owned(self._close_joined())

    async def _close_joined(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        pending = tuple(self._handlers)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            # Already-completed Futures need not yield to their done callbacks.
            # Explicitly retire the joined snapshot instead of spinning while
            # waiting for callback scheduling to remove the same handlers.
            self._handlers.difference_update(pending)
