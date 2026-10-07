"""Private, token-authenticated local bridge to the one Gateway manager."""
from pathlib import Path
import fcntl
import hmac
import json
import os
import socket
import socketserver
import threading
import struct

from .manager import ManagementError


def _read_frame(reader, limit=None):
    def read_exact(size):
        chunks = []
        remaining = size
        while remaining:
            chunk = reader.read(min(remaining, 65536))
            if not chunk:
                raise ValueError('Incomplete management frame.')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)
    size = struct.unpack('!Q', read_exact(8))[0]
    if limit is not None and size > limit:
        raise ValueError('Management request frame exceeds its bound.')
    return json.loads(read_exact(size))


def _frame(value):
    payload = json.dumps(value).encode()
    return struct.pack('!Q', len(payload)) + payload


class ManagementServer:
    def __init__(self, manager, credentials):
        self.manager = manager
        self.credentials = credentials
        self.path = manager.state_dir / 'manager.sock'
        self._server = None
        self._lease = None

    def start(self):
        self._lease = open(self.manager.state_dir / 'manager.lock', 'a')
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                raise ManagementError('unavailable', 'An existing manager socket requires reconciliation; it was preserved.')
            bridge = self

            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    self.request.settimeout(3)
                    try:
                        payload = _read_frame(self.rfile, limit=1024 * 1024)
                        if not isinstance(payload, dict) or set(payload) - {'token', 'operation', 'expected_version', 'change', 'scope', 'request_id', 'report', 'action', 'instruction_id', 'text', 'expected_turn_id', 'human_request_id', 'reply_id', 'response', 'plan', 'source', 'source_id', 'query_id', 'question', 'scope_ids', 'channel_id', 'auto_supplement', 'material_ids', 'registration', 'details'}:
                            raise ManagementError('invalid_change', 'Unknown bridge fields; caller identity is not a body field.')
                        token = payload.get('token', '')
                        identity = next((identity for secret, identity in bridge.credentials.items()
                                         if isinstance(token, str) and hmac.compare_digest(secret.encode(), token.encode())), None)
                        if identity is None:
                            raise ManagementError('unauthorized', 'Invalid entry credential.')
                        if payload.get('operation') in {'read_snapshot', 'read_participant_snapshot'}:
                            if payload['operation'] == 'read_participant_snapshot' and identity.subject == bridge.manager.owner_identity_ref:
                                raise ManagementError('forbidden', 'The participant entry cannot borrow owner authority.')
                            result = bridge.manager.read_snapshot(identity, payload.get('scope'))
                        elif payload.get('operation') == 'global_validation':
                            result = bridge.manager.global_validation(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'collaborate':
                            result = bridge.manager.collaborate(identity, payload.get('action'), payload.get('details'))
                        elif payload.get('operation') == 'register_observation_source':
                            result = bridge.manager.register_observation_source(identity, payload.get('registration'))
                        elif payload.get('operation') == 'refresh_manual_sessions':
                            result = bridge.manager.refresh_manual_sessions(identity, payload.get('scope'))
                        elif payload.get('operation') == 'apply_directory_change':
                            result = bridge.manager.apply_directory_change(identity, payload.get('expected_version'), payload.get('change'))
                        elif payload.get('operation') == 'prepare_task':
                            result = bridge.manager.prepare_task(identity, payload.get('request_id'), payload.get('plan'))
                        elif payload.get('operation') in {'start_task', 'refresh_task', 'verify_task_execution', 'refresh_task_source'}:
                            result = getattr(bridge.manager, payload['operation'])(identity, payload.get('request_id'))
                        elif payload.get('operation') == 'control_task':
                            result = bridge.manager.control_task(identity, payload.get('request_id'), payload.get('action'),
                                payload.get('instruction_id'), payload.get('text'), payload.get('expected_turn_id'))
                        elif payload.get('operation') == 'answer_human_request':
                            result = bridge.manager.answer_human_request(identity, payload.get('request_id'), payload.get('human_request_id'), payload.get('reply_id'), payload.get('response'))
                        elif payload.get('operation') == 'record_task_delivery':
                            result = bridge.manager.record_task_delivery(identity, payload.get('request_id'), payload.get('report'))
                        elif payload.get('operation') == 'register_knowledge_source':
                            result = bridge.manager.register_knowledge_source(identity, payload.get('expected_version'), payload.get('source'))
                        elif payload.get('operation') == 'query_knowledge':
                            result = bridge.manager.query_knowledge(identity, payload.get('source_id'), payload.get('query_id'), payload.get('question'), payload.get('scope_ids'), payload.get('request_id'), payload.get('channel_id'), payload.get('auto_supplement', False))
                        elif payload.get('operation') == 'resolve_knowledge':
                            result = bridge.manager.resolve_knowledge(identity, payload.get('query_id'))
                        elif payload.get('operation') == 'supplement_knowledge':
                            result = bridge.manager.supplement_knowledge(identity, payload.get('query_id'), payload.get('material_ids'))
                        else:
                            raise ManagementError('unsupported', 'This management operation is not enabled.')
                        response = {'result': result}
                    except ManagementError as exc:
                        response = {'error': {'code': exc.code, 'message': str(exc)}}
                    except (ValueError, TypeError, KeyError):
                        response = {'error': {'code': 'invalid_change', 'message': 'Malformed management input.'}}
                    try:
                        self.wfile.write(_frame(response))
                    except OSError:
                        pass  # The durable operation remains authoritative after caller disconnect.

            class Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads = False
                block_on_close = True

            self._server = Server(str(self.path), Handler)
            os.chmod(self.path, 0o600)
            self._inode = self.path.stat().st_ino
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={'poll_interval': 0.05}, name='hermes-pm-directory', daemon=True)
            self._thread.start()
            return self
        except (OSError, ManagementError) as exc:
            self._lease.close()
            self._lease = None
            if isinstance(exc, ManagementError):
                raise
            raise ManagementError('unavailable', 'Another manager owns this state directory or the bridge cannot bind.') from exc

    def close(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)
        if self.path.exists() and self.path.stat().st_ino == self._inode:
            self.path.unlink()
        self._server = None
        self._lease.close()
        self._lease = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


class ManagementClient:
    def __init__(self, state_dir, token):
        self.path = Path(state_dir) / 'manager.sock'
        self.token = token

    def _call(self, operation, **args):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(30 if operation in {'start_task', 'refresh_task', 'verify_task_execution', 'record_task_delivery', 'control_task', 'answer_human_request', 'prepare_task', 'refresh_task_source', 'refresh_manual_sessions', 'collaborate'} else 3)
                connection.connect(str(self.path))
                connection.sendall(_frame({'token': self.token, 'operation': operation, **args}))
                with connection.makefile('rb') as reader:
                    response = _read_frame(reader)
        except (OSError, ValueError) as exc:
            if operation in {'start_task', 'control_task', 'answer_human_request'}:
                raise ManagementError('outcome_unknown', 'Task start response was not confirmed; read the same durable request before retrying. Repository occupancy is retained.') from exc
            raise ManagementError('unavailable', 'The management instance is unavailable; no operation was confirmed.') from exc
        if 'error' in response:
            raise ManagementError(response['error']['code'], response['error']['message'])
        return response['result']

    def read_snapshot(self, scope=None):
        return self._call('read_snapshot', scope=scope)

    def read_participant_snapshot(self):
        return self._call('read_participant_snapshot')

    def global_validation(self, action, details):
        return self._call('global_validation', action=action, details=details)

    def collaborate(self, action, details):
        return self._call('collaborate', action=action, details=details)

    def apply_directory_change(self, expected_version, change):
        return self._call('apply_directory_change', expected_version=expected_version, change=change)

    def register_observation_source(self, registration):
        return self._call('register_observation_source', registration=registration)

    def refresh_manual_sessions(self, scope=None):
        return self._call('refresh_manual_sessions', scope=scope)

    def refresh_task_source(self, request_id):
        return self._call('refresh_task_source', request_id=request_id)

    def prepare_task(self, request_id, plan):
        return self._call('prepare_task', request_id=request_id, plan=plan)

    def start_task(self, request_id):
        return self._call('start_task', request_id=request_id)

    def control_task(self, request_id, action, instruction_id, text=None, expected_turn_id=None):
        return self._call('control_task', request_id=request_id, action=action, instruction_id=instruction_id,
                          text=text, expected_turn_id=expected_turn_id)

    def answer_human_request(self, request_id, human_request_id, reply_id, response):
        return self._call('answer_human_request', request_id=request_id, human_request_id=human_request_id, reply_id=reply_id, response=response)

    def refresh_task(self, request_id):
        return self._call('refresh_task', request_id=request_id)

    def record_task_delivery(self, request_id, report):
        return self._call('record_task_delivery', request_id=request_id, report=report)

    def verify_task_execution(self, request_id):
        return self._call('verify_task_execution', request_id=request_id)

    def register_knowledge_source(self, expected_version, source):
        return self._call('register_knowledge_source', expected_version=expected_version, source=source)

    def query_knowledge(self, source_id, query_id, question, scope_ids, request_id=None, channel_id=None, auto_supplement=False):
        return self._call('query_knowledge', source_id=source_id, query_id=query_id, question=question, scope_ids=scope_ids,
                          request_id=request_id, channel_id=channel_id, auto_supplement=auto_supplement)

    def resolve_knowledge(self, query_id):
        return self._call('resolve_knowledge', query_id=query_id)

    def supplement_knowledge(self, query_id, material_ids=None):
        return self._call('supplement_knowledge', query_id=query_id, material_ids=material_ids)
