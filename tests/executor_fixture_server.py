"""Synthetic fixture-v1 JSONL peer, never a real service acceptance result."""
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, os.environ.get('HERMES_FIXTURE_PLUGIN_ROOT', str(Path(__file__).resolve().parents[1])))
from ghost_hermes_pm import VerifiedIdentity
from ghost_hermes_pm.snapshots import SnapshotReader
reader = SnapshotReader(root / 'state', owner_identity_ref='fixture:owner')
identity = VerifiedIdentity('fixture:owner', 'owned-original-service-public-observer')

def committed_requests(method):
    snapshot = reader.read_snapshot(identity)
    with (root / 'public-snapshots.jsonl').open('a') as log:
        log.write(json.dumps({'method': method, 'snapshot': snapshot}) + '\n')
    return snapshot['requests']

def require(condition, requirement, method):
    with (root / 'oracle-events.jsonl').open('a') as log:
        log.write(json.dumps({'method': method, 'requirement': requirement, 'satisfied': bool(condition)}) + '\n')
    assert condition, requirement

thread_id = '00000000-0000-7000-8000-000000000016'
turn_id = '00000000-0000-7000-8000-000000000017'
thread = None
turn_sequence = 17
for line in sys.stdin:
    request = json.loads(line)
    with (root / 'wire.jsonl').open('a') as log:
        log.write(json.dumps(request) + '\n')
    method = request['method']
    params = request.get('params', {})
    behavior = json.loads((root / 'behavior.json').read_text()) if (root / 'behavior.json').exists() else {}
    if behavior.get('rpc_error') == method:
        print(json.dumps({'id': request['id'], 'error': {'code': -32601, 'message': 'Unsupported fixture method'}}), flush=True)
        continue
    if method == 'fixture/ready':
        continue
    if method == 'fixture/connect':
        value = {'userAgent': 'fixture-cli/fixture-v1', 'fixtureHome': str(root / 'fixture-home'),
                 'platformFamily': 'unix', 'platformOs': 'fixture'}
        print(json.dumps({'method': 'remoteControl/status/changed', 'params': {'status': 'disabled'}}), flush=True)
    elif method == 'fixture/policy':
        value = {'data': [{'id': 'fixture-boundary', 'description': 'Synthetic verified policy', 'allowed': True}], 'nextCursor': None}
    elif method == 'fixture/loaded':
        value = json.loads((root / 'loaded.json').read_text()) if (root / 'loaded.json').exists() else {'data': [], 'nextCursor': None}
    elif method == 'fixture/list':
        value = {'data': [], 'nextCursor': None, 'backwardsCursor': None}
    elif method == 'fixture/create':
        thread = {'id': thread_id, 'cwd': params['cwd'], 'cliVersion': 'fixture-v1',
                  'status': {'type': 'idle'}, 'canAcceptDirectInput': True, 'turns': []}
        value = {'thread': thread, 'model': 'fixture-model', 'cwd': params['cwd'],
                 'activePermissionProfile': {'id': params['permissions'], 'extends': ':read-only'},
                 'runtimeWorkspaceRoots': params['runtimeWorkspaceRoots']}
    elif method == 'fixture/start':
        records = committed_requests(method)
        require(any(r.get('session', {}).get('thread_id') == thread_id for r in records), 'thread must be durable before turn/start', method)
        if params.get('clientUserMessageId'):
            require(any(c.get('phase') == 'rpc_intent' and c.get('id') == params['clientUserMessageId'] for r in records for c in r.get('controls', [])), 'control intent must be durable before original input RPC', method)
            turn_sequence += 1
            turn_id = '00000000-0000-7000-8000-' + str(turn_sequence).zfill(12)
        value = {'turn': {'id': turn_id, 'status': 'inProgress', 'items': [], 'itemsView': 'full'}}
        thread['status'] = {'type': 'active', 'activeFlags': []}
        thread['turns'] = [value['turn']]
    elif method == 'fixture/append':
        if behavior.get('steer_active_turn'):
            turn_id = behavior['steer_active_turn']
            thread['turns'] = [{'id': turn_id, 'status': 'inProgress', 'itemsView': 'full', 'items': []}]
        records = committed_requests(method)
        require(any(c.get('phase') == 'rpc_intent' and c.get('id') == params['clientUserMessageId'] for r in records for c in r.get('controls', [])), 'control intent must be durable before original input RPC', method)
        if params['threadId'] != thread_id or params['expectedTurnId'] != turn_id:
            print(json.dumps({'id': request['id'], 'error': {'code': -32000, 'message': 'Wrong active turn'}}), flush=True)
            continue
        with (root / 'applied-inputs.jsonl').open('a') as applied:
            applied.write(json.dumps({'thread_id': thread_id, 'turn_id': turn_id, 'instruction_id': params['clientUserMessageId']}) + '\n')
        value = {'turnId': turn_id}
    elif method == 'fixture/stop':
        records = committed_requests(method)
        require(any(r.get('stop', {}).get('status') == 'processing' for r in records), 'stop intent must be durable before interrupt', method)
        value = {}
    elif method == 'fixture/background':
        pages = json.loads((root / 'background.json').read_text()) if (root / 'background.json').exists() else {'': {'data': [], 'nextCursor': None}}
        value = pages[params.get('cursor', '')]
    elif method == 'fixture/read':
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
    response = json.dumps({'id': request['id'], 'result': value})
    # Fragment the envelope: reader must frame by newline, not one read == one response.
    midpoint = len(response) // 2
    sys.stdout.write(response[:midpoint]); sys.stdout.flush()
    sys.stdout.write(response[midpoint:] + '\n'); sys.stdout.flush()
