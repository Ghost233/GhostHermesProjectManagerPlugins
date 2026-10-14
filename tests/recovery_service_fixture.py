"""Owned test service and separate JSONL proxy; never uses a real DSH home."""
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import threading


def run_service(root):
    root.mkdir(exist_ok=True)
    state = {'thread': None, 'starts': 0, 'inputs': [], 'responses': [], 'worker_pid': None}
    service_id = 'local:owned-original:' + str(os.getpid())
    (root / 'origin.json').write_text(json.dumps({'service_id': service_id, 'pid': os.getpid()}))
    lock = threading.RLock()
    worker = None
    def save():
        (root / 'execution.json').write_text(json.dumps(state))
    def stop_worker():
        nonlocal worker
        if worker is not None:
            worker.terminate()
            worker.wait(timeout=5)
            worker = None
        if state['thread']:
            state['thread']['status'] = {'type': 'idle'}
            state['thread']['turns'][-1].update(status='interrupted', items=[])
        save()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            sent_question = False
            for line in self.rfile:
                request = json.loads(line)
                with lock:
                    with (root / 'wire.jsonl').open('a') as log:
                        log.write(json.dumps(request) + '\n')
                    method = request.get('method')
                    params = request.get('params', {})
                    if method is None:
                        state['responses'].append(request)
                        save()
                        continue
                    if method == 'fixture/ready':
                        continue
                    if method == 'fixture/connect':
                        result = {'userAgent': 'fixture-cli/fixture-v1', 'fixtureHome': str(root / 'isolated-home'), 'platformFamily': 'unix', 'platformOs': 'fixture'}
                    elif method == 'fixture/policy':
                        result = {'data': [{'id': 'fixture-boundary', 'allowed': True}], 'nextCursor': None}
                    elif method == 'fixture/loaded':
                        result = {'data': [state['thread']['id']] if state['thread'] else [], 'nextCursor': None}
                    elif method == 'fixture/create':
                        state['thread'] = {'id': 'owned-original-thread', 'cwd': params['cwd'], 'cliVersion': 'fixture-v1', 'canAcceptDirectInput': True, 'status': {'type': 'idle'}, 'turns': []}
                        result = {'thread': state['thread'], 'cwd': params['cwd'], 'activePermissionProfile': {'id': 'fixture-boundary'}, 'runtimeWorkspaceRoots': [params['cwd']]}
                    elif method == 'fixture/start':
                        nonlocal worker
                        if worker is not None:
                            raise RuntimeError('Duplicate execution reached the owned original service.')
                        state['starts'] += 1
                        turn = {'id': 'owned-turn-' + str(state['starts']), 'status': 'inProgress', 'itemsView': 'full', 'items': []}
                        state['thread']['turns'].append(turn)
                        state['thread']['status'] = {'type': 'active', 'activeFlags': []}
                        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        state['worker_pid'] = worker.pid
                        result = {'turn': turn}
                    elif method == 'fixture/append':
                        state['inputs'].append(params)
                        result = {'turnId': params['expectedTurnId']}
                    elif method == 'fixture/stop':
                        stop_worker()
                        result = {}
                    elif method == 'fixture/background':
                        result = {'data': [], 'nextCursor': None}
                    elif method == 'fixture/read':
                        result = {'thread': state['thread']}
                    else:
                        raise RuntimeError('Unexpected operation: ' + str(method))
                    save()
                    drop = root / 'drop.json'
                    if drop.exists() and method in json.loads(drop.read_text()):
                        return  # Apply first, then lose the actual connection/acknowledgement.
                    self.wfile.write((json.dumps({'id': request['id'], 'result': result}) + '\n').encode())
                    self.wfile.flush()
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
    server = Server(str(root / 'original.sock'), Handler)
    def terminate(*_):
        raise SystemExit()
    signal.signal(signal.SIGTERM, terminate)
    print(json.dumps({'ready': True, 'service_id': service_id}), flush=True)
    try:
        server.serve_forever(poll_interval=.05)
    finally:
        stop_worker()
        server.server_close()


def run_proxy(root):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(root / 'original.sock'))
        def receive():
            with connection.makefile('rb') as reader:
                for line in reader:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
            os._exit(0)
        threading.Thread(target=receive, daemon=True).start()
        for line in sys.stdin.buffer:
            connection.sendall(line)


if __name__ == '__main__':
    root = Path(sys.argv[2])
    (run_service if sys.argv[1] == 'service' else run_proxy)(root)
