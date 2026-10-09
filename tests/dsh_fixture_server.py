"""Synthetic native DSH HTTP/Remote-mux peer, not live-service acceptance."""
from contextlib import contextmanager
import base64
import hashlib
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def remote_peer(*, behavior=None):
    behavior = behavior or {}
    state = {'calls': [], 'streams': [], 'closed': 0, 'prompts': [], 'emit_remote': threading.Event(), 'sessions': [
        {'sessionId': 'session-fixture', 'cwd': '/synthetic/project', 'running': False,
         'agentAvailable': True, 'blank': False, 'updatedAt': 2},
    ]}
    default_records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'user/message', 'seq': 1, 'time': 1, 'data': {
            'id': 'fixture-old-message', 'role': 'user', 'content': [{'type': 'text', 'text': 'synthetic task'}],
            'source': {'kind': 'user', 'rpcId': 'old-input'}}}},
        {'type': 'event', 'event': {'type': 'turn/end', 'seq': 2, 'time': 2, 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}},
    ]
    state['journals'] = {'session-fixture': list(behavior.get('records', default_records))}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def __init__(self, *args, **kwargs):
            self._send_lock = threading.Lock()
            super().__init__(*args, **kwargs)

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(request)
            assert self.headers.get('Cookie') == behavior.get('cookie', 'synthetic-auth=value')
            assert self.path == '/api/' + request['method']
            assert request['type'] == 'client-request'
            method, args = request['method'], request['payload']['args']
            if method == 'session/list':
                assert args == {'_request': {}}
                value = {'items': state['sessions']}
            elif method == 'session/projections':
                value = {'asOfSeq': 4, 'values': {'inbox': {'next-turn': [], 'next-step': []}}}
            elif method == 'session/create':
                value = {'sessionId': args['request']['sessionId']}
                if behavior.get('create_foreign_id'):
                    value['sessionId'] = 'session-foreign'
                state['sessions'].append({'sessionId': value['sessionId'], 'cwd': args['request']['cwd'],
                                          'running': False, 'agentAvailable': True, 'blank': True, 'updatedAt': 3})
                state['journals'].setdefault(value['sessionId'], list(behavior.get('records', [])))
            elif method == 'session/prompt':
                state['prompts'].append(args['request'])
                if behavior.get('admit_prompt'):
                    submission = args['request']
                    journal = state['journals'][submission['sessionId']]
                    starts = [record['event']['data']['turn'] for record in journal if record['event']['type'] == 'turn/start']
                    ends = [record['event']['data']['turn'] for record in journal if record['event']['type'] == 'turn/end']
                    active = next((turn for turn in reversed(starts) if turn not in ends), None)
                    if submission['mode'] == 'steer' and active is not None:
                        turn = active
                    else:
                        turn = max(starts, default=0) + 1
                        journal.append({'type': 'event', 'event': {'type': 'turn/start', 'seq': len(journal),
                                                                 'time': 3, 'data': {'turn': turn}}})
                    journal.append({'type': 'event', 'event': {'type': 'user/message', 'seq': len(journal), 'time': 3,
                        'data': {'id': 'message-' + submission['requestId'], 'role': 'user', 'content': submission['content'],
                                 'source': {'kind': 'user', 'rpcId': submission['requestId']}}}})
                    row = next(row for row in state['sessions'] if row['sessionId'] == submission['sessionId'])
                    row.update(blank=False, running=True)
                value = {'accepted': True}
            elif method == 'session/cancel':
                value = {'accepted': True}
            elif method == '$events/result':
                value = None
            elif method == 'session/page':
                value = behavior.get('page', {'records': [], 'hasMore': False})
            else:
                value = None
            result = {'ok': True, 'value': value}
            if method == '$events/result':
                result = {'ok': True}
            if behavior.get('reject') == method:
                result = {'ok': False, 'error': {'code': 'gateway/bad-request', 'message': 'fixture rejection', 'details': {}}}
            response = {'type': 'server-response', 'rpcId': request['rpcId'], 'result': result}
            received = behavior.get('http_received')
            if received is not None and behavior.get('blocked_http_method') == method:
                received.set()
                behavior['release_http_response'].wait(timeout=3)
            if behavior.get('numeric_acceptance') == method:
                response['result']['value'] = {'accepted': 1}
            if behavior.get('oversized') == method:
                response['result']['value'] = {'padding': 'x' * (17 * 1024 * 1024)}
            if behavior.get('wrong_id') == method:
                response['rpcId'] = 'unrelated-response'
            if behavior.get('omit_value') == method:
                response['result'] = {'ok': True}
            if behavior.get('redirect') == method:
                self.send_response(307)
                self.send_header('Location', 'http://127.0.0.1:9/private')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if behavior.get('drop') == method:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def read_exact(self, count):
            data = self.rfile.read(count)
            if len(data) != count:
                raise EOFError
            return data

        def read_frame(self):
            first, second = self.read_exact(2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self.read_exact(8))[0]
            assert length < 17 * 1024 * 1024
            mask = self.read_exact(4) if second & 128 else b''
            body = self.read_exact(length)
            if mask:
                body = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
            return first & 15, body

        def send_frame(self, value, opcode=1):
            body = json.dumps(value).encode() if opcode == 1 else value
            prefix = bytes([128 | opcode])
            if len(body) < 126:
                prefix += bytes([len(body)])
            elif len(body) < 65536:
                prefix += b'\x7e' + struct.pack('!H', len(body))
            else:
                prefix += b'\x7f' + struct.pack('!Q', len(body))
            with self._send_lock:
                self.wfile.write(prefix + body)
                self.wfile.flush()

        def do_GET(self):
            assert self.path == '/api/remote.mux'
            assert self.headers.get('Cookie') == behavior.get('cookie', 'synthetic-auth=value')
            key = self.headers['Sec-WebSocket-Key']
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            try:
                while True:
                    opcode, body = self.read_frame()
                    if opcode == 8:
                        self.send_frame(body, 8)
                        break
                    if opcode == 9:
                        self.send_frame(body, 10)
                        continue
                    request = json.loads(body)
                    state['streams'].append(request)
                    stream_id = request['streamId']
                    if request['type'] == 'cancel':
                        self.send_frame({'type': 'end', 'streamId': stream_id})
                    elif request['endpoint'] == '$events':
                        state['send_remote'] = lambda frame, stream_id=stream_id: self.send_frame({
                            'type': 'item', 'streamId': stream_id, 'value': frame})
                        ready = {'type': 'ready', 'clientId': 'fixture-client', 'host': {'home': '/synthetic/home'}}
                        self.send_frame({'type': 'item', 'streamId': stream_id, 'value': ready})
                        if behavior.get('defer_remote_events'):
                            state['emit_remote'].wait()
                        for event in behavior.get('remote_events', []):
                            self.send_frame({'type': 'item', 'streamId': stream_id, 'value': event})
                        if behavior.get('disconnect_events'):
                            self.send_frame(b'', 8)
                            break
                    elif request['endpoint'] == 'session/follow':
                        session_id = request['payload']['args']['request']['address']['sessionId']
                        row = next(row for row in state['sessions'] if row['sessionId'] == session_id)
                        records = list(state['journals'][session_id])
                        snapshot = {'type': 'snapshot', 'header': {'id': session_id, 'version': 1, 'createdAt': 0,
                                    'cwd': row['cwd'], 'isSeeded': False}, 'cursor': records[-1]['event']['seq'] if records else -1,
                                    'records': records, 'hasMore': behavior.get('has_more', False),
                                    'projections': {'asOfSeq': records[-1]['event']['seq'] if records else -1,
                                                    'values': {'inbox': behavior.get('inbox', {'next-turn': [], 'next-step': []})}}}
                        self.send_frame({'type': 'item', 'streamId': stream_id, 'value': snapshot})
                    elif request['endpoint'] == 'job/list':
                        self.send_frame({'type': 'item', 'streamId': stream_id,
                                         'value': {'type': 'rows', 'jobs': behavior.get('jobs', [])}})
            except (EOFError, OSError):
                pass
            finally:
                state['closed'] += 1
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), state
    finally:
        state['emit_remote'].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
