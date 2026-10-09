"""Private bounded IPC to a child owning an unmodified public DSH Host."""
import json
import os
from pathlib import Path
import queue
import select
import shutil
import socket
import stat
import subprocess
import threading
import time
import uuid

from .dsh_remote import FRAME_BOUND
from .manager import ManagementError


class NativeOwnedTransport:
    def __init__(self, *, dsh_home, workspace, runtime_package_root, node_bin=None, timeout=10):
        self.config = {'dsh_home': dsh_home, 'runtime_package_root': runtime_package_root}
        self.workspace, self.timeout = workspace, timeout
        self.node_bin = node_bin or shutil.which('node')
        if (not self.node_bin or not Path(self.node_bin).is_absolute() or not Path(self.node_bin).is_file()
                or not os.access(self.node_bin, os.X_OK)):
            raise ManagementError('invalid_change', 'A local Node runtime executable is required.')
        if not Path(runtime_package_root, '@deepseek-ai/dsh/lib/profile-boot.js').is_file():
            raise ManagementError('invalid_change', 'The original installed DSH profile runtime is required.')
        try:
            self.node_bin = str(Path(self.node_bin).resolve())
            paths = [Path(workspace), Path(runtime_package_root), Path(self.node_bin),
                     Path(__file__).with_name('owned_native_host.mjs'),
                     Path(runtime_package_root, '@deepseek-ai/dsh/lib/profile-boot.js'),
                     Path(runtime_package_root, '@deepseek-ai/dsh-launch-environment/lib/index.js')]
            self._source_bindings = [self._path_binding(path) for path in paths]
            self._home_identity = self._home_binding(Path(dsh_home)) if Path(dsh_home).exists() else None
        except (OSError, ValueError):
            raise ManagementError('invalid_change', 'The owned home, workspace or original runtime could not be bound; startup was refused.') from None
        self._lock, self._send_lock, self._start_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self._closed, self._started = False, False
        self._socket, self._process, self._reader = None, None, None
        self._replies, self._streams = {}, {}
        self.native_identity = None
        self.close_outcome = {'kind': 'not_started', 'exit_code': None}

    @staticmethod
    def _path_binding(path):
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        return {'path': str(path), 'resolved': str(resolved), 'device': str(info.st_dev), 'inode': str(info.st_ino)}

    @staticmethod
    def _home_binding(home):
        info = home.lstat()
        if (str(home.resolve()) != str(home) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ManagementError('invalid_change', 'The independent DSH home must remain canonical and owner-private.')
        return {'device': str(info.st_dev), 'inode': str(info.st_ino)}

    def _prepare_paths(self):
        try:
            if any(self._path_binding(Path(binding['path'])) != binding for binding in self._source_bindings):
                raise ValueError('An original source path changed.')
            home = Path(self.config['dsh_home'])
            if self._home_identity is None:
                if home.exists() or home.is_symlink() or str(home.resolve()) != str(home):
                    raise ValueError('The independent home changed before creation.')
                home.mkdir(mode=0o700, parents=True)
                self._home_identity = self._home_binding(home)
            elif self._home_binding(home) != self._home_identity:
                raise ValueError('The independent home identity changed.')
            self.config.update(home_identity=self._home_identity, source_bindings=self._source_bindings)
        except (OSError, ValueError):
            raise ManagementError('invalid_change', 'An owned home, workspace or original source path changed; startup was refused.') from None

    def _start(self):
        with self._start_lock:
            self._start_original()

    def _start_original(self):
        with self._lock:
            if self._closed:
                raise OSError('Owned native generation ended.')
            if self._started:
                return
            self._prepare_paths()
            home = Path(self.config['dsh_home'])
            parent, child = socket.socketpair()
            parent.setblocking(False)
            environment = dict(os.environ)
            environment.update(HOME=str(home), DSH_HOME=str(home), DSH_TELEMETRY_DISABLED='1', HERMES_PM_IPC_FD=str(child.fileno()))
            try:
                self._process = subprocess.Popen([self.node_bin, str(Path(__file__).with_name('owned_native_host.mjs'))],
                    cwd=self.workspace, env=environment, pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                parent.close()
                raise
            finally:
                child.close()
            self._socket, self._started = parent, True
            self._reader = threading.Thread(target=self._read, name='hermes-pm-owned-carrier', daemon=True)
            self._reader.start()
        try:
            self.native_identity = self._rpc({'op': 'boot', 'config': self.config})
            if (not isinstance(self.native_identity, dict) or type(self.native_identity.get('pid')) is not int
                    or self.native_identity['pid'] != self._process.pid or not isinstance(self.native_identity.get('version'), str)):
                raise OSError('Owned native identity was not confirmed.')
        except Exception:
            self.close()
            raise

    def _send(self, value):
        data = json.dumps(value, allow_nan=False).encode() + b'\n'
        if len(data) > FRAME_BOUND:
            raise ValueError('Owned native frame exceeds its bound.')
        deadline = time.monotonic() + self.timeout
        if not self._send_lock.acquire(timeout=self.timeout):
            raise TimeoutError('Owned native write deadline expired.')
        try:
            with self._lock:
                if self._closed or self._socket is None:
                    raise OSError('Owned native generation ended.')
                channel = self._socket
                channel.setblocking(False)
            view = memoryview(data)
            while view:
                with self._lock:
                    if self._closed:
                        raise OSError('Owned native generation ended.')
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [channel], [], max(0, remaining))[1]:
                    raise TimeoutError('Owned native write deadline expired.')
                try:
                    count = channel.send(view)
                except BlockingIOError:
                    continue
                if count == 0:
                    raise OSError('Owned native generation ended.')
                view = view[count:]
        finally:
            self._send_lock.release()

    def _rpc(self, payload, *, identity=None):
        identity = identity or str(uuid.uuid4())
        inbox = queue.Queue(maxsize=1)
        with self._lock:
            if self._closed:
                raise OSError('Owned native generation ended.')
            self._replies[identity] = inbox
        try:
            self._send({**payload, 'id': identity})
            response = inbox.get(timeout=self.timeout)
            if not isinstance(response, dict) or response.get('ok') is not True:
                raise OSError('Original owned native call was not confirmed.')
            return response.get('value')
        finally:
            with self._lock:
                self._replies.pop(identity, None)

    def _read(self):
        buffered = bytearray()
        try:
            while not self._closed:
                if not select.select([self._socket], [], [], None)[0]:
                    continue
                try:
                    data = self._socket.recv(65536)
                except BlockingIOError:
                    continue
                if not data:
                    break
                buffered.extend(data)
                while b'\n' in buffered:
                    end = buffered.index(10)
                    if end >= FRAME_BOUND:
                        raise ValueError('Owned native frame exceeds its bound.')
                    message = json.loads(buffered[:end])
                    del buffered[:end + 1]
                    with self._lock:
                        target = self._replies.get(message.get('id')) if message.get('type') == 'reply' else self._streams.get(message.get('id'))
                        if target is not None:
                            target.put_nowait(message)
                if len(buffered) >= FRAME_BOUND:
                    raise ValueError('Owned native frame exceeds its bound.')
        except Exception:
            pass
        finally:
            with self._lock:
                for target in [*self._replies.values(), *self._streams.values()]:
                    try:
                        target.put_nowait({'type': 'end', 'failed': True})
                    except queue.Full:
                        pass
            self.close()

    def request(self, method, args, rpc_id):
        self._start()
        return self._rpc({'op': 'call', 'method': method, 'args': args, 'rpcId': rpc_id})

    def stream(self, endpoint, args):
        self._start()
        identity = str(uuid.uuid4())
        result = _OwnedStream(self, identity)
        with self._lock:
            self._streams[identity] = result._inbox
        try:
            self._rpc({'op': 'open', 'endpoint': endpoint, 'args': args}, identity=identity)
        except Exception:
            result.close()
            raise
        return result

    def close(self):
        with self._lock:
            if self._closed:
                return
            process, channel = self._process, self._socket
            self._closed = True
            self.close_outcome = {'kind': 'closing', 'exit_code': None}
            if channel:
                try:
                    channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                channel.close()
        if process:
            kind = 'original_exit'
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                kind = 'forced_terminate'
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    kind = 'forced_kill'
                    process.kill()
                    process.wait(timeout=2)
            self.close_outcome = {'kind': kind if kind != 'original_exit' or process.returncode == 0 else 'abnormal_exit',
                                  'exit_code': process.returncode}
        else:
            self.close_outcome = {'kind': 'not_started', 'exit_code': None}
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)


class _OwnedStream:
    def __init__(self, owner, identity):
        self.owner, self.identity = owner, identity
        self._inbox, self._closed = queue.Queue(maxsize=10000), False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        message = self._inbox.get()
        if message.get('type') == 'item':
            return message['value']
        self._closed = True
        if message.get('failed'):
            raise OSError('Original native stream ended unverified.')
        raise StopIteration

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._inbox.put_nowait({'type': 'end'})
        except queue.Full:
            pass
        with self.owner._lock:
            self.owner._streams.pop(self.identity, None)
            open_generation = not self.owner._closed
        if open_generation:
            self.owner._send({'op': 'cancel', 'streamId': self.identity, 'id': str(uuid.uuid4())})
