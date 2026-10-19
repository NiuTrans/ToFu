"""Paced loopback provider through a verified native process and application writer.

The browser fixture owns tasks/storage. This helper owns only its synthetic HTTP
server, native clients and callback executors; every owner joins before return.
It exercises run_native_task, not the public ROOT admission or a real model.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time


def pipeline_chunk(index):
    return f'PIPELINE_{index:05d} ' + 'Exact synthetic live evidence. ' * 16 + '\n\n'


class NativePipelineProducer:
    @staticmethod
    def joined_child_cpu_ms():
        import resource
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return 1000 * (usage.ru_utime + usage.ru_stime)

    def __init__(self, *, archive, participants, stop, directory, sample_resources):
        from scripts.build_native_runtime import verify
        self.archive = Path(archive)
        self.manifest = verify(self.archive)
        self.sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.directory, self.stop = directory, stop
        self.failures = []
        self.participants = {row['task']['id']: row for row in participants}
        owner = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                try:
                    self.connection.settimeout(5)
                    task_id = self.path.split('/')[1]
                    row = owner.participants[task_id]
                    assert self.path == f'/{task_id}/v1/chat/completions'
                    assert self.headers['Authorization'] == 'Bearer private-fixture-token'
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    assert body['model'] == 'alpha-wire-a' and body['stream'] is True
                    row['nativeIngress']['providerRequests'] += 1
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    for index in range(60):
                        if stop.is_set(): return
                        start = time.perf_counter() * 1000
                        value = {'choices': [{'delta': {'content': pipeline_chunk(index)}, 'finish_reason': None}]}
                        self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                        self.wfile.flush()
                        row['produced'].append({'index': index, 'start': start, 'end': time.perf_counter()*1000})
                        if row is participants[0] and index % 4 == 0:
                            sample_resources('stream')
                        if index < 40 and stop.wait(.04): return
                    tail = {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
                    self.wfile.write(('data: ' + json.dumps(tail) + '\n\ndata: [DONE]\n\n').encode())
                    self.wfile.flush()
                except BaseException as error:
                    owner.failures.append(repr(error))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def produce(self, participant):
        from lib.agent_core.native_process import Budget, NativeProcessClient
        from lib.identity import PrincipalContext
        from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
        from lib.model_routing.repository import OwnerBoundary
        from lib.tasks_pkg.native_execution import compile_native_application_route, project_native_text, run_native_task
        from tests.model_routing.test_native_route import Repository, selection
        from tests.support.native_process_probe import config
        target = participant['task']
        witness = participant['nativeIngress'] = {'providerRequests': 0, 'frames': [], 'clientJoined': False}
        received = hashlib.sha256()
        principal = PrincipalContext.user(subject_id='native-pipeline', owner_user_id=target['_userId'])
        repo = Repository()
        repo.boundary = OwnerBoundary.create(target['_userId'])
        repo.document['connections'][1]['enabled'] = False
        repo.document['connections'][0]['base_url'] = f'http://127.0.0.1:{self.server.server_port}/{target["id"]}/v1'
        route = compile_native_application_route(repo, principal, selection())
        configuration = config('ignored', target['id'])
        for key in ROUTE_OVERRIDE_FIELDS: configuration.pop(key, None)
        configuration.update(owner_user_id=target['_userId'], tools={}, text_progress=True,
            epoch=target['id'], deadline_ms=30000, messages=[{'role': 'user', 'content': 'Stream the synthetic fixture'}],
            terminal_persistence={'operation_id': 'native-pipeline:' + target['id'],
                'attempts': 2, 'ack_ms': 5000, 'frame_bytes': 1 << 20})
        for key in ('event_bytes', 'output_bytes', 'request_bytes', 'retained_bytes'):
            configuration[key] = 1 << 20

        def project(task, delta):
            project_native_text(task, delta)
            received.update(delta.text.encode())
            witness['frames'].append({'sequence': delta.sequence, 'channel': delta.channel,
                'bytes': len(delta.text.encode()), 'at': time.perf_counter()*1000})

        async def run(executor):
            client = NativeProcessClient.from_archive(archive=self.archive, sha256=self.sha256,
                application_version=self.manifest['build']['application_version'], principal=principal,
                budget=Budget(1, 16 << 20, 1 << 20, 1 << 20, 5))
            try:
                await run_native_task(client, route, configuration, task=target, executor=executor,
                    callbacks={}, working_directory=self.directory, project_text=project)
                assert target['status'] == 'done', target.get('error')
            finally:
                await client.close()
                witness['clientJoined'] = client.active_runs == 0

        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(run(executor))
        witness['textSha256'] = received.hexdigest()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        assert not self.thread.is_alive(), 'Native fixture provider did not join'
        assert not self.failures, self.failures
