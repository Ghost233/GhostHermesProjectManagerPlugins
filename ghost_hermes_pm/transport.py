"""Private, token-authenticated local bridge to the one Gateway manager."""
from pathlib import Path
import fcntl
import hmac
import json
import os
import socket
import socketserver
import threading

from .manager import ManagementError


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
                        payload = json.loads(self.rfile.readline(1024 * 1024))
                        if not isinstance(payload, dict) or set(payload) - {'token', 'operation', 'expected_version', 'change', 'scope'}:
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
                        else:
                            raise ManagementError('unsupported', 'This management operation is not enabled.')
                        response = {'result': result}
                    except ManagementError as exc:
                        response = {'error': {'code': exc.code, 'message': str(exc)}}
                    except (ValueError, TypeError, KeyError):
                        response = {'error': {'code': 'invalid_change', 'message': 'Malformed management input.'}}
                    self.wfile.write(json.dumps(response).encode() + b'\n')

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
                connection.settimeout(3)
                connection.connect(str(self.path))
                connection.sendall(json.dumps({'token': self.token, 'operation': operation, **args}).encode() + b'\n')
                with connection.makefile('rb') as reader:
                    response = json.loads(reader.readline(1024 * 1024))
        except (OSError, ValueError) as exc:
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
