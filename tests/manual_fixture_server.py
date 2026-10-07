"""Synthetic original-service stdio proxy peer; no real socket/server or execution."""
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    with (root / 'manual-wire.jsonl').open('a') as log:
        log.write(json.dumps(request) + '\n')
    assert 'method' in request and 'result' not in request, 'Observer must not answer server requests.'
    method, params = request['method'], request.get('params', {})
    state = json.loads((root / 'manual-state.json').read_text())
    if method == 'initialized':
        continue
    if method in state.get('unsupported', []):
        print(json.dumps({'id': request['id'], 'error': {'code': -32601, 'message': 'Read method unavailable'}}), flush=True)
        continue
    if method == 'initialize':
        result = {'userAgent': 'codex-cli/0.160.1', 'codexHome': str(root / 'synthetic-home'), 'platformFamily': 'unix', 'platformOs': 'fixture'}
        if state.get('approval_request'):
            print(json.dumps({'id': 'manual-approval', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'manual-thread', 'command': 'synthetic private operation'}}), flush=True)
    elif method == 'thread/loaded/list':
        result = state.get('loaded_pages', {}).get(params.get('cursor', ''), {'data': state.get('loaded', []), 'nextCursor': None})
    elif method == 'thread/list':
        result = state.get('list_pages', {}).get(params.get('cursor', ''), {'data': [state['threads'][i] for i in state.get('listed', [])], 'nextCursor': None, 'backwardsCursor': None})
    elif method == 'thread/read':
        result = {'thread': state['threads'][params['threadId']]}
    elif method == 'thread/backgroundTerminals/list':
        result = state.get('background_pages', {}).get(params.get('cursor', ''), {'data': [], 'nextCursor': None})
    else:
        raise RuntimeError('Read-only peer received forbidden method: ' + method)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
