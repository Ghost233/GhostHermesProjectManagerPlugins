"""Synthetic original Unix-WebSocket peer; never starts a Codex executor."""
import asyncio
import json
import sys
from pathlib import Path

from websockets.asyncio.server import unix_serve


async def main():
    root, endpoint = Path(sys.argv[1]), sys.argv[2]

    async def handshake(connection, request):
        with (root / 'handshakes.jsonl').open('a') as log:
            log.write(json.dumps({'path': request.path, 'upgrade': request.headers.get('Upgrade'),
                                  'connection': request.headers.get('Connection')}) + '\n')

    async def receive(connection):
        try:
            async for frame in connection:
                request = json.loads(frame)
                with (root / 'wire.jsonl').open('a') as log:
                    log.write(json.dumps(request) + '\n')
                state = json.loads((root / 'state.json').read_text())
                method = request['method']
                if method == state.get('disconnect_on'):
                    await connection.close()
                    return
                if method == 'initialized':
                    continue
                if method == 'initialize':
                    result = {'userAgent': 'synthetic-codex/0.162.0', 'codexHome': str(root / 'synthetic-home'),
                              'platformFamily': 'unix', 'platformOs': 'fixture'}
                    if state.get('approval_request'):
                        await connection.send(json.dumps({'id': 'server-approval', 'method': 'item/commandExecution/requestApproval',
                            'params': {'threadId': 'synthetic-thread', 'command': 'synthetic operation'}}))
                elif method == 'thread/loaded/list':
                    result = {'data': ['synthetic-thread'], 'nextCursor': None}
                elif method == 'thread/list':
                    result = {'data': [{'id': 'synthetic-thread'}], 'nextCursor': None}
                elif method == 'thread/read':
                    result = {'thread': {'id': 'synthetic-thread', 'cwd': str(root), 'status': {'type': 'active'}, 'turns': []}}
                else:
                    result = {'data': [], 'nextCursor': None}
                if state.get('oversize_on') == method:
                    result['synthetic'] = 'x' * (16 * 1024 * 1024)
                await connection.send(json.dumps({'id': request['id'], 'result': result}))
        finally:
            with (root / 'closed.jsonl').open('a') as log:
                log.write('{"closed": true}\n')

    async with unix_serve(receive, endpoint, process_request=handshake, max_size=16 * 1024 * 1024):
        print('{"ready": true}', flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
