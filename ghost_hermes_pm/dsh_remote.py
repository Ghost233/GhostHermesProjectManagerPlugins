"""Owned clients for the original local DSH HTTP and Remote-mux service."""
import http.client
import json
import logging
import socket
import threading
from urllib.parse import urlsplit
import uuid

from .manager import ManagementError

FRAME_BOUND = 16 * 1024 * 1024


class NativeRemoteTransport:
    """No launcher, ambient proxy, redirect, or upstream process ownership."""
    def __init__(self, base_url, cookie, timeout):
        try:
            if not isinstance(base_url, str):
                raise ValueError('Invalid endpoint.')
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError:
            raise ManagementError('invalid_change', 'A fixed loopback DSH HTTP endpoint is required.') from None
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {'', '/'} or not port):
            raise ManagementError('invalid_change', 'A fixed loopback DSH HTTP endpoint is required.')
        if not isinstance(cookie, str) or len(cookie) > 16384 or '\r' in cookie or '\n' in cookie:
            raise ManagementError('invalid_change', 'The private DSH credential is malformed.')
        self.base_url, self.cookie, self.timeout = base_url.rstrip('/'), cookie, timeout
        self.host, self.port = parsed.hostname, port
        self._streams, self._requests, self._sockets = set(), set(), set()
        self._lock, self._closed = threading.Lock(), False
        self._logger = logging.Logger('hermes-pm-owned-dsh-wire', level=logging.CRITICAL + 1)
        self._logger.propagate = False

    def request(self, method, args, rpc_id):
        body = json.dumps({'type': 'client-request', 'rpcId': rpc_id, 'method': method,
                           'payload': {'args': args}}, allow_nan=False).encode()
        if len(body) > FRAME_BOUND:
            raise ValueError('The DSH request exceeds its frame bound.')
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        connection.set_debuglevel(0)
        connection.auto_open = 0
        with self._lock:
            if self._closed:
                raise OSError('Owned DSH transport ended.')
            self._requests.add(connection)
        raw_socket = None
        try:
            raw_socket = self._open_socket()
            with self._lock:
                if self._closed:
                    raise OSError('Owned DSH transport ended.')
                connection.sock = raw_socket
            connection.request('POST', '/api/' + method, body,
                               {'Content-Type': 'application/json', 'Cookie': self.cookie})
            response = connection.getresponse()
            if response.status != 200:
                raise OSError('The original DSH endpoint did not confirm the RPC.')
            data = response.read(FRAME_BOUND + 1)
            if len(data) > FRAME_BOUND:
                raise ValueError('The DSH response exceeds its frame bound.')
            with self._lock:
                if self._closed:
                    raise OSError('Owned DSH transport ended.')
            return json.loads(data)
        finally:
            connection.close()
            with self._lock:
                self._requests.discard(connection)
                if raw_socket is not None:
                    self._sockets.discard(raw_socket)
            if raw_socket is not None:
                raw_socket.close()

    def _open_socket(self):
        raw_socket = socket.socket(socket.AF_INET6 if ':' in self.host else socket.AF_INET, socket.SOCK_STREAM)
        raw_socket.settimeout(self.timeout)
        with self._lock:
            if self._closed:
                raw_socket.close()
                raise OSError('Owned DSH transport ended.')
            self._sockets.add(raw_socket)
        try:
            raw_socket.connect((self.host, self.port))
            return raw_socket
        except Exception:
            raw_socket.close()
            with self._lock:
                self._sockets.discard(raw_socket)
            raise

    def stream(self, endpoint, args):
        from websockets.sync.client import connect
        raw_socket = self._open_socket()
        connection = None
        try:
            # Supplied sockets retain their TCP-connect timeout; the WebSocket
            # library owns handshake/receive deadlines after this transfer.
            raw_socket.settimeout(None)
            connection = connect('ws' + self.base_url[4:] + '/api/remote.mux', sock=raw_socket,
                                 additional_headers={'Cookie': self.cookie}, proxy=None,
                                 open_timeout=self.timeout, close_timeout=1, max_size=FRAME_BOUND,
                                 logger=self._logger)
            stream = _RemoteStream(connection, endpoint, args, self.timeout, self._retire, raw_socket)
            with self._lock:
                if self._closed:
                    raise OSError('Owned DSH transport ended.')
                self._streams.add(stream)
            return stream
        except Exception:
            raw_socket.close()
            if connection is not None:
                connection.close()
            with self._lock:
                self._sockets.discard(raw_socket)
            raise

    def _retire(self, stream):
        with self._lock:
            self._streams.discard(stream)
            self._sockets.discard(stream.raw_socket)

    def close(self):
        with self._lock:
            self._closed = True
            streams = list(self._streams)
            requests = list(self._requests)
            sockets = list(self._sockets)
        for raw_socket in sockets:
            try:
                raw_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            raw_socket.close()
        for connection in requests:
            connection.close()
        for stream in streams:
            stream.close()


class _RemoteStream:
    def __init__(self, socket, endpoint, args, timeout, retire, raw_socket):
        self.socket, self.timeout, self._retire = socket, timeout, retire
        self.raw_socket = raw_socket
        self.id, self._closed, self._received = str(uuid.uuid4()), False, False
        self.endpoint = endpoint
        socket.send(json.dumps({'type': 'open', 'streamId': self.id,
                                'endpoint': endpoint, 'payload': {'args': args}}, allow_nan=False))

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            frame = json.loads(self.socket.recv(timeout=None if self.endpoint == '$events' and self._received else self.timeout))
        except Exception:
            self.close()
            raise
        if not isinstance(frame, dict) or frame.get('streamId') != self.id:
            self.close()
            raise ValueError('The DSH stream correlation is invalid.')
        if frame.get('type') == 'item' and set(frame) == {'type', 'streamId', 'value'}:
            self._received = True
            return frame['value']
        self.close()
        if frame.get('type') == 'end' and set(frame) == {'type', 'streamId'}:
            raise StopIteration
        raise ValueError('The DSH stream ended without a confirmed item.')

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.socket.send(json.dumps({'type': 'cancel', 'streamId': self.id}))
        except Exception:
            pass
        finally:
            self.socket.close()
            self._retire(self)
