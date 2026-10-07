"""Multi-thread synthetic JSONL service; durable bridge state governs every start."""
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
external = root / 'queue-external.json'
threads = json.loads(external.read_text()) if external.exists() else {}
sequence = 0

def state():
    observed = root / 'queue-observed.json'
    for thread_id, patch in json.loads(observed.read_text()).items() if observed.exists() else []:
        threads[thread_id].update(patch)

for line in sys.stdin:
    request = json.loads(line)
    with (root / 'wire.jsonl').open('a') as log:
        log.write(json.dumps(request) + '\n')
    method, params = request['method'], request.get('params', {})
    state()
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent': 'codex-cli/0.160.1', 'codexHome': str(root / 'codex-home'), 'platformFamily': 'unix', 'platformOs': 'fixture'}
    elif method == 'permissionProfile/list':
        result = {'data': [{'id': 'fixture-boundary', 'allowed': True}], 'nextCursor': None}
    elif method == 'thread/loaded/list':
        result = {'data': list(threads), 'nextCursor': None}
    elif method == 'thread/start':
        sequence += 1
        thread_id = 'queue-thread-' + str(sequence)
        thread = {'id': thread_id, 'cwd': params['cwd'], 'cliVersion': '0.160.1', 'status': {'type': 'idle'}, 'turns': [], 'canAcceptDirectInput': True}
        threads[thread_id] = thread
        result = {'thread': thread, 'model': 'fixture-model', 'cwd': params['cwd'], 'activePermissionProfile': {'id': params['permissions']}, 'runtimeWorkspaceRoots': params['runtimeWorkspaceRoots']}
    elif method == 'turn/start':
        with sqlite3.connect(root / 'state' / 'manager.sqlite3') as db:
            records = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])['requests'].values()
        assert any(r.get('session', {}).get('thread_id') == params['threadId'] and not r.get('repository_released') for r in records)
        sequence += 1
        turn = {'id': 'queue-turn-' + str(sequence), 'status': 'inProgress', 'itemsView': 'full', 'items': []}
        thread = threads[params['threadId']]
        thread['turns'].append(turn)
        thread['status'] = {'type': 'active', 'activeFlags': []}
        result = {'turn': turn}
    elif method == 'thread/read':
        result = {'thread': threads[params['threadId']]}
    elif method == 'thread/backgroundTerminals/list':
        path = root / 'queue-background.json'
        pages = json.loads(path.read_text()) if path.exists() else {}
        result = pages.get(params['threadId'], {}).get(params.get('cursor', ''), {'data': [], 'nextCursor': None})
    elif method == 'turn/interrupt':
        result = {}
    else:
        print(json.dumps({'id': request['id'], 'error': {'code': -32601, 'message': 'Unsupported queue fixture method'}}), flush=True)
        continue
    (root / 'active-threads.json').write_text(json.dumps([t for t in threads.values() if t['status']['type'] == 'active']))
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
