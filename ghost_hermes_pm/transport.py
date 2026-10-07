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
                        if not isinstance(payload, dict) or set(payload) - {'token', 'operation', 'expected_version', 'change', 'scope', 'request_id', 'report'}:
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
                        elif payload.get('operation') == 'apply_directory_change':
                            result = bridge.manager.apply_directory_change(identity, payload.get('expected_version'), payload.get('change'))
                        elif payload.get('operation') in {'start_task', 'refresh_task', 'verify_task_execution'}:
                            result = getattr(bridge.manager, payload['operation'])(identity, payload.get('request_id'))
                        elif payload.get('operation') == 'record_task_delivery':
                            result = bridge.manager.record_task_delivery(identity, payload.get('request_id'), payload.get('report'))
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
                connection.settimeout(30 if operation in {'start_task', 'refresh_task', 'verify_task_execution', 'record_task_delivery'} else 3)
                connection.connect(str(self.path))
                connection.sendall(_frame({'token': self.token, 'operation': operation, **args}))
                with connection.makefile('rb') as reader:
                    response = _read_frame(reader)
        except (OSError, ValueError) as exc:
            if operation == 'start_task':
                raise ManagementError('outcome_unknown', 'Task start response was not confirmed; read the same durable request before retrying. Repository occupancy is retained.') from exc
            raise ManagementError('unavailable', 'The management instance is unavailable; no operation was confirmed.') from exc
        if 'error' in response:
            raise ManagementError(response['error']['code'], response['error']['message'])
        return response['result']

    def read_snapshot(self, scope=None):
        return self._call('read_snapshot', scope=scope)

    def read_participant_snapshot(self):
        return self._call('read_participant_snapshot')

    def apply_directory_change(self, expected_version, change):
        return self._call('apply_directory_change', expected_version=expected_version, change=change)

    def start_task(self, request_id):
        return self._call('start_task', request_id=request_id)

    def refresh_task(self, request_id):
        return self._call('refresh_task', request_id=request_id)

    def record_task_delivery(self, request_id, report):
        return self._call('record_task_delivery', request_id=request_id, report=report)

    def verify_task_execution(self, request_id):
        return self._call('verify_task_execution', request_id=request_id)
