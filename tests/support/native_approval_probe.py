"""Verified native write/denial/fault/Stop probe; only private scratch files."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tarfile
import tempfile
import threading

from lib.agent_core.native_process import Budget, NativeProcessClient, NativeProcessError
from scripts.build_native_runtime import verify
from tests.support.native_process_probe import config
from tests.agent_core.test_native_process import principal


async def probe(binary, manifest, directory):
    results = []
    for mode in ['approved', 'denied', 'policy_fault', 'tool_fault', 'cancel_wait', 'deadline_wait', 'cancel_write']:
        seen, decisions, calls = [], [], []
        entered = asyncio.Event()
        target = directory / f'{mode}.txt'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(body)
                if len(seen) == 1:
                    delta = {'tool_calls': [{'index':0,'id':'call','type':'function',
                        'function':{'name':'lookup','arguments':'{}'}}]}
                    finish = 'tool_calls'
                else:
                    expected = 'Tool call denied by owner approval policy' if mode == 'denied' else 'written'
                    assert body['messages'][-1]['content'] == expected
                    delta, finish = {'content':'finished'}, 'stop'
                wire = ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}]})
                        +'\n\ndata: [DONE]\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        class Policy:
            async def decide(self, request):
                assert request.principal.owner_user_id == 7 and request.task_id == mode
                assert request.call_id == 'call' and request.name == 'lookup' and request.epoch == 'epoch'
                assert request.arguments == {} and request.caller is None
                # Detached accessor mutation cannot rewrite the original receipt.
                request.arguments['foreign'] = True
                decisions.append(request.call_id)
                if mode == 'policy_fault':
                    raise OSError('fixture approval service failure')
                if mode in {'cancel_wait','deadline_wait'}:
                    entered.set()
                    await request.cancelled.wait()
                    await asyncio.sleep(0.01)
                return 'denied' if mode == 'denied' else 'auto_write_mode'

        async def write(request):
            assert request.call_id == 'call' and request.arguments == {}
            calls.append(request.call_id)
            if mode == 'tool_fault':
                raise OSError('fixture write executor failure')
            if mode == 'cancel_write':
                entered.set()
                await request.cancelled.wait()
                await asyncio.sleep(0.01)
            await asyncio.to_thread(target.write_text, 'approved scratch mutation')
            return 'written'

        server = HTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01})
        thread.start()
        client = NativeProcessClient(binary=binary,
            sha256=manifest['files']['bin/tofu-native']['sha256'],
            protocol_version=manifest['build']['stdio_contract'],principal=principal(),
            budget=Budget(1,16*1024*1024,65536,131072,5))
        settings = config(f'http://127.0.0.1:{server.server_port}/chat', mode)
        settings['tools']['lookup']['permission'] = 'write'
        settings['approval_control'] = {'receipts':8,'identity_bytes':8192}
        if mode == 'deadline_wait':
            settings['deadline_ms'] = 100
        try:
            run = asyncio.create_task(client.run(settings,callbacks={'lookup':write},
                working_directory=directory,approval_policy=Policy()))
            if mode in {'cancel_wait','cancel_write'}:
                async with asyncio.timeout(5):
                    await entered.wait()
                run.cancel()
                with_cancel = False
                try:
                    await run
                except asyncio.CancelledError:
                    with_cancel = True
                assert with_cancel
            elif mode in {'policy_fault','tool_fault'}:
                try:
                    await run
                except NativeProcessError as error:
                    assert isinstance(error.__cause__,OSError)
                else:
                    raise AssertionError('failed approval became success')
            else:
                result = await run
                assert result['status'] == ('aborted' if mode=='deadline_wait' else 'done'), (mode,result,len(seen),decisions,calls)
            assert len(decisions) == 1
            assert len(seen) == (2 if mode in {'approved','denied'} else 1)
            assert len(calls) == (1 if mode in {'approved','cancel_write','tool_fault'} else 0)
            assert target.exists() == (mode in {'approved','cancel_write'})
            assert client.active_runs == 0
            results.append(dict(mode=mode,model_requests=len(seen),approval_decisions=len(decisions),
                                tool_callbacks=len(calls),scratch_mutations=int(target.exists()),active_runs_after=0))
        finally:
            await client.close()
            server.shutdown()
            thread.join()
            server.server_close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    args = parser.parse_args()
    manifest = verify(args.archive)
    with tempfile.TemporaryDirectory(prefix='tofu-native-approval-') as folder:
        directory = Path(folder)
        with tarfile.open(args.archive) as bundle:
            bundle.extractall(directory,filter='data')
        print(json.dumps(asyncio.run(probe(directory/'bin/tofu-native',manifest,directory))))


if __name__ == '__main__':
    main()
