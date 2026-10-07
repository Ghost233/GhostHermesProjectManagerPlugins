"""Synthetic 0.160.1 JSONL peer, never a real service acceptance result."""
import json
import os
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
thread_id = '00000000-0000-7000-8000-000000000016'
turn_id = '00000000-0000-7000-8000-000000000017'
thread = None
turn_sequence = 17
for line in sys.stdin:
    request = json.loads(line)
    with (root / 'wire.jsonl').open('a') as log:
        log.write(json.dumps(request) + '\n')
    if 'method' not in request:
        behavior = json.loads((root / 'questions-behavior.json').read_text()) if (root / 'questions-behavior.json').exists() else {}
        with sqlite3.connect(root / 'state' / 'manager.sqlite3') as db:
            payload = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])
        assert any(q.get('reply') and q['reply'].get('sent') in {'intent', 'sent'} for r in payload['requests'].values() for q in r.get('human_requests', [])), 'response intent must be durable'
        if behavior.get('disconnect_after_reply'):
            sys.exit(0)
        if not behavior.get('omit_resolved'):
            print(json.dumps({'method': 'serverRequest/resolved', 'params': {'threadId': thread_id, 'requestId': request['id']}}), flush=True)
        continue
    method = request['method']
    params = request.get('params', {})
    behavior = json.loads((root / 'behavior.json').read_text()) if (root / 'behavior.json').exists() else {}
    if behavior.get('rpc_error') == method:
        print(json.dumps({'id': request['id'], 'error': {'code': -32601, 'message': 'Unsupported fixture method'}}), flush=True)
        continue
    if method == 'initialized':
        continue
    if method == 'initialize':
        value = {'userAgent': 'codex-cli/0.160.1', 'codexHome': str(root / 'codex-home'),
                 'platformFamily': 'unix', 'platformOs': 'fixture'}
        print(json.dumps({'method': 'remoteControl/status/changed', 'params': {'status': 'disabled'}}), flush=True)
    elif method == 'permissionProfile/list':
        value = {'data': [{'id': 'fixture-boundary', 'description': 'Synthetic verified policy', 'allowed': True}], 'nextCursor': None}
    elif method == 'thread/loaded/list':
        value = json.loads((root / 'loaded.json').read_text()) if (root / 'loaded.json').exists() else {'data': [], 'nextCursor': None}
    elif method == 'thread/list':
        value = {'data': [], 'nextCursor': None, 'backwardsCursor': None}
    elif method == 'thread/start':
        thread = {'id': thread_id, 'cwd': params['cwd'], 'cliVersion': '0.160.1',
                  'status': {'type': 'idle'}, 'canAcceptDirectInput': True, 'turns': []}
        value = {'thread': thread, 'model': 'fixture-model', 'cwd': params['cwd'],
                 'activePermissionProfile': {'id': params['permissions'], 'extends': ':read-only'},
                 'runtimeWorkspaceRoots': params['runtimeWorkspaceRoots']}
    elif method == 'turn/start':
        with sqlite3.connect(root / 'state' / 'manager.sqlite3') as db:
            payload = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])
        assert any(r.get('session', {}).get('thread_id') == thread_id for r in payload['requests'].values()), 'thread must be durable before turn/start'
        if params.get('clientUserMessageId'):
            assert any(c.get('phase') == 'rpc_intent' and c.get('id') == params['clientUserMessageId'] for r in payload['requests'].values() for c in r.get('controls', []))
            turn_sequence += 1
            turn_id = '00000000-0000-7000-8000-' + str(turn_sequence).zfill(12)
        value = {'turn': {'id': turn_id, 'status': 'inProgress', 'items': [], 'itemsView': 'full'}}
        thread['status'] = {'type': 'active', 'activeFlags': []}
        thread['turns'] = [value['turn']]
    elif method == 'turn/steer':
        if behavior.get('steer_active_turn'):
            turn_id = behavior['steer_active_turn']
            thread['turns'] = [{'id': turn_id, 'status': 'inProgress', 'itemsView': 'full', 'items': []}]
        with sqlite3.connect(root / 'state' / 'manager.sqlite3') as db:
            payload = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])
        assert any(c.get('phase') == 'rpc_intent' and c.get('id') == params['clientUserMessageId'] for r in payload['requests'].values() for c in r.get('controls', []))
        if params['threadId'] != thread_id or params['expectedTurnId'] != turn_id:
            print(json.dumps({'id': request['id'], 'error': {'code': -32000, 'message': 'Wrong active turn'}}), flush=True)
            continue
        with (root / 'applied-inputs.jsonl').open('a') as applied:
            applied.write(json.dumps({'thread_id': thread_id, 'turn_id': turn_id, 'instruction_id': params['clientUserMessageId']}) + '\n')
        value = {'turnId': turn_id}
    elif method == 'turn/interrupt':
        with sqlite3.connect(root / 'state' / 'manager.sqlite3') as db:
            payload = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])
        assert any(r.get('stop', {}).get('status') == 'processing' for r in payload['requests'].values())
        value = {}
    elif method == 'thread/backgroundTerminals/list':
        pages = json.loads((root / 'background.json').read_text()) if (root / 'background.json').exists() else {'': {'data': [], 'nextCursor': None}}
        value = pages[params.get('cursor', '')]
    elif method == 'thread/read':
        state = json.loads((root / 'observed.json').read_text()) if (root / 'observed.json').exists() else {}
        thread.update(state)
        others = json.loads((root / 'threads.json').read_text()) if (root / 'threads.json').exists() else {}
        value = {'thread': thread if params['threadId'] == thread_id else others.get(params['threadId'])}
    else:
        print(json.dumps({'id': request['id'], 'error': {'code': -32601, 'message': 'Unsupported fixture method'}}), flush=True)
        continue
    behavior = json.loads((root / 'behavior.json').read_text()) if (root / 'behavior.json').exists() else {}
    import time
    time.sleep(behavior.get('delay', {}).get(method, 0))
    value.update(behavior.get(method, {}))
    if behavior.get('oversized') == method:
        print(json.dumps({'id': request['id'], 'result': {'padding': 'x' * (17 * 1024 * 1024)}}), flush=True)
        continue
    if behavior.get('omit_response') == method:
        continue
    pending_path = root / 'requests.json'
    if method == 'thread/read' and pending_path.exists():
        for event in json.loads(pending_path.read_text()):
            print(json.dumps(event), flush=True)
        pending_path.unlink()
    response = json.dumps({'id': request['id'], 'result': value})
    # Fragment the envelope: reader must frame by newline, not one read == one response.
    midpoint = len(response) // 2
    sys.stdout.write(response[:midpoint]); sys.stdout.flush()
    sys.stdout.write(response[midpoint:] + '\n'); sys.stdout.flush()
