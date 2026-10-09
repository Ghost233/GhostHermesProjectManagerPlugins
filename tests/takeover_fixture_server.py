"""Synthetic original service shared by distinct readonly/control proxy-shaped peers."""
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
emitted = set()
events_emitted = False
for line in sys.stdin:
    request = json.loads(line)
    with (root / 'original-wire.jsonl').open('a') as log:
        log.write(json.dumps(request) + '\n')
    state = json.loads((root / 'original-state.json').read_text())
    thread = state['thread']
    if 'method' not in request:
        state.setdefault('responses', []).append(request)
        (root / 'original-state.json').write_text(json.dumps(state))
        continue
    method, params = request['method'], request.get('params', {})
    if method == 'fixture/ready':
        continue
    if method == 'fixture/connect':
        result = {'userAgent': 'fixture-cli/fixture-v1', 'fixtureHome': str(root / 'synthetic-home'), 'platformFamily': 'unix', 'platformOs': 'fixture'}
    elif method == 'fixture/loaded':
        result = {'data': state.get('loaded', [thread['id']]), 'nextCursor': None}
    elif method == 'fixture/list':
        result = {'data': [thread], 'nextCursor': None, 'backwardsCursor': None}
    elif method == 'fixture/read':
        result = {'thread': thread}
    elif method == 'fixture/background':
        result = {'data': state.get('backgrounds', []), 'nextCursor': None}
    elif method == 'fixture/append':
        active = [t['id'] for t in thread['turns'] if t['status'] == 'inProgress']
        if params['threadId'] != thread['id'] or active != [params['expectedTurnId']]:
            print(json.dumps({'id': request['id'], 'error': {'code': -32000, 'message': 'Wrong original turn'}}), flush=True)
            continue
        state.setdefault('inputs', []).append(params)
        (root / 'original-state.json').write_text(json.dumps(state))
        result = {'turnId': params['expectedTurnId']}
    elif method == 'fixture/stop':
        result = {}
    elif method == 'fixture/start':
        turn = {'id': 'continued-original-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}
        thread['turns'].append(turn)
        thread['status'] = {'type': 'active', 'activeFlags': []}
        (root / 'original-state.json').write_text(json.dumps(state))
        result = {'turn': turn}
    else:
        raise RuntimeError('Takeover must not create/resume/fork a thread or change daemon: ' + method)
    if not events_emitted and state.get('events'):
        for event in state['events']:
            print(json.dumps(event), flush=True)
        events_emitted = True
    for envelope in state.get('server_requests', []):
        key = (type(envelope['id']).__name__, envelope['id'])
        if key not in emitted:
            print(json.dumps(envelope), flush=True)
            emitted.add(key)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
