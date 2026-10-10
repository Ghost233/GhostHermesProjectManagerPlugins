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
from .manager import _private_state_directory
from .trusted_controller import controller_helper, controller_identity, validate_controller_helper


class _ControllerNotAdmitted(OSError):
    pass


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

    def _rpc(self, payload, *, identity=None, timeout=None):
        identity = identity or str(uuid.uuid4())
        inbox = queue.Queue(maxsize=1)
        with self._lock:
            if self._closed:
                raise OSError('Owned native generation ended.')
            self._replies[identity] = inbox
        try:
            self._send({**payload, 'id': identity})
            response = inbox.get(timeout=self.timeout if timeout is None else timeout)
            if isinstance(response, dict) and response.get('reason') == 'controller_not_admitted':
                raise _ControllerNotAdmitted('The socket peer is not an admitted original controller.')
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


class PersistentOwnedTransport(NativeOwnedTransport):
    """A supervisor connection to one managed, independently owned generation."""
    def __init__(self, *, instance_dir, configuration, node_bin=None, timeout=10, environment=None, attach_only=False):
        import hashlib
        self.instance_dir = _private_state_directory(instance_dir)
        required = {'dsh_home', 'workspace', 'runtime_package_root', 'instance_id', 'generation', 'session_id'}
        if not isinstance(configuration, dict) or not required <= set(configuration):
            raise ManagementError('configuration_missing', 'An admitted owned generation configuration is required.')
        super().__init__(dsh_home=configuration['dsh_home'], workspace=configuration['workspace'],
                         runtime_package_root=configuration['runtime_package_root'], node_bin=node_bin, timeout=timeout)
        self.config.update(configuration)
        self.attach_only = attach_only
        self.environment = dict(environment or {})
        self.driver = Path(__file__).with_name('owned_persistent_host.mjs')
        self.configuration_path = self.instance_dir / 'native-configuration.json'
        saved = None
        if self.configuration_path.exists():
            info = self.configuration_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ManagementError('outcome_unknown', 'Original controller configuration is not private.')
            saved = json.loads(self.configuration_path.read_text())
        controllers = self.config.get('trusted_controllers', [])
        if not isinstance(controllers, list) or len(controllers) > 1:
            raise ManagementError('binding_conflict', 'One original gateway controller may be admitted.')
        if saved:
            if saved.get('trusted_controllers') != controllers:
                raise ManagementError('binding_conflict', 'Original initial gateway identity differs.')
            helper = saved.get('controller_helper_path')
            launcher = saved.get('launcher_controller')
            if not helper or not launcher:
                raise ManagementError('outcome_unknown', 'Original kernel controller binding is absent; no replacement was started.')
        else:
            helper, launcher = controller_helper(), controller_identity()
        helper = validate_controller_helper(helper, self.workspace, self.config['dsh_home'])
        self.config.update(controller_helper_path=helper, launcher_controller=launcher, trusted_controllers=controllers)
        self._source_bindings.extend(self._path_binding(p) for p in (self.driver, self.driver.with_name('owned_runtime.mjs'),
            self.driver.with_name('trusted_controller.mjs'), self.driver.with_name('trusted_controller.c'), Path(helper).parent, Path(helper)))
        for name in ('test_python', 'test_runner_path'):
            if self.config.get(name):
                self._source_bindings.append(self._path_binding(Path(self.config[name])))
        digest = hashlib.sha256(str(self.instance_dir).encode()).hexdigest()[:20]
        socket_dir = _private_state_directory('/private/tmp/hpm-dsh-' + digest)
        self.socket_path = socket_dir / 'carrier.sock'
        self.identity_path = self.instance_dir / 'native-identity.json'
        self.birth_path = self.instance_dir / 'process-birth.json'
        self.exit_path = self.instance_dir / 'native-exit.json'
        self.config.update(socket_path=str(self.socket_path), identity_path=str(self.identity_path), exit_path=str(self.exit_path))
        self.config['source_bindings'] = self._source_bindings
        self.config['configuration_sha256'] = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _path_binding(path):
        import hashlib
        binding = NativeOwnedTransport._path_binding(path)
        if path.is_file():
            binding['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        return binding

    def event_frames(self):
        """Read the generation's retained original deliveries without retiring them."""
        self._start()
        value = self._rpc({'op': 'events'})
        if (not isinstance(value, dict) or value.get('generation') != self.config['generation']
                or value.get('session_id') != self.config['session_id'] or not isinstance(value.get('client_id'), str)
                or not isinstance(value.get('frames'), list)):
            raise ManagementError('binding_conflict', 'Original event observation changed execution binding.')
        return value

    def admit_controller(self, identity):
        """The pinned gateway admits one exact worker after its original claim check."""
        self._start()
        value = self._rpc({'op': 'admit_controller', 'controller': identity})
        if value != identity:
            raise ManagementError('outcome_unknown', 'The original worker kernel admission was not confirmed.')
        return value

    def verify_repository_tests(self, test_files, test_python):
        """Replay explicit tests through the original Shell under a read-only policy."""
        if test_python != self.config.get('test_python'):
            raise ManagementError('binding_conflict', 'The admitted verification Python runtime differs.')
        self._start()
        value = self._rpc({'op': 'verify_tests', 'test_files': test_files}, timeout=65)
        if (not isinstance(value, dict) or value.get('generation') != self.config['generation']
                or value.get('session_id') != self.config['session_id']):
            raise ManagementError('binding_conflict', 'Original test verification execution binding differs.')
        return value

    def _start_original(self):
        import fcntl
        with self._lock:
            if self._closed:
                raise OSError('The supervisor connection has already detached.')
        if self._started:
            return
        lock_fd = os.open(self.instance_dir / 'startup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if self.attach_only and not self.configuration_path.exists():
                raise ManagementError('outcome_unknown', 'Original owned execution configuration is absent; no replacement was started.')
            self._prepare_paths()
            if not self.configuration_path.exists():
                data = json.dumps(self.config)
                fd = os.open(self.configuration_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                home = Path(self.config['dsh_home'])
                temporary = home / 'tmp'
                temporary.mkdir(mode=0o700, exist_ok=True)
                environment = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'TZ'}}
                environment.update(HOME=str(home), DSH_HOME=str(home), DSH_TELEMETRY_DISABLED='1', TMPDIR=str(temporary), **self.environment)
                with self._lock:
                    if self._closed:
                        raise OSError('The supervisor detached before native startup.')
                    self._process = subprocess.Popen([self.node_bin, str(self.driver), str(self.configuration_path)],
                        cwd=self.workspace, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                import psutil
                birth = {'pid': self._process.pid, 'created_at': psutil.Process(self._process.pid).create_time(),
                         'generation': self.config['generation']}
                fd = os.open(self.birth_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as target:
                    json.dump(birth, target)
                    target.flush()
                    os.fsync(target.fileno())
            else:
                if self.configuration_path.is_symlink():
                    raise ManagementError('outcome_unknown', 'The original owned configuration must be reconciled.')
                saved = json.loads(self.configuration_path.read_text())
                if saved.get('configuration_sha256') != self.config['configuration_sha256']:
                    raise ManagementError('binding_conflict', 'The admitted owned configuration changed.')
                if saved.get('source_bindings') != self._source_bindings or saved.get('home_identity') != self._home_identity:
                    raise ManagementError('binding_conflict', 'Original owned home or source identity changed.')
            deadline = time.monotonic() + self.timeout
            while not self.identity_path.exists() and time.monotonic() < deadline:
                if self._process is not None and self._process.poll() is not None:
                    break
                time.sleep(0.02)
            if not self.identity_path.exists() or self.identity_path.is_symlink():
                raise ManagementError('outcome_unknown', 'The original owned startup remains unconfirmed; do not replace it.')
            identity = json.loads(self.identity_path.read_text())
            import psutil
            if self.birth_path.is_symlink() or not self.birth_path.exists():
                raise ManagementError('outcome_unknown', 'Original process creation remains unconfirmed.')
            birth = json.loads(self.birth_path.read_text())
            if (identity.get('pid') != birth.get('pid') or birth.get('generation') != self.config['generation']
                or psutil.Process(birth['pid']).create_time() != birth.get('created_at')):
                raise ManagementError('binding_conflict', 'Original process creation identity changed.')
            for key in ('instance_id', 'generation', 'configuration_sha256'):
                if identity.get(key) != self.config[key]:
                    raise ManagementError('binding_conflict', 'The original owned identity changed.')
            endpoint = self.socket_path.lstat()
            if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077:
                raise ManagementError('unsafe_state', 'The original owned connection is not private.')
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(self.timeout)
            channel.connect(str(self.socket_path))
            channel.setblocking(False)
            # Claim admission can arrive from the original gateway while this
            # exact new worker waits; release the startup lock before attaching.
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            self._socket, self._started = channel, True
            self._reader = threading.Thread(target=self._read, name='hermes-pm-persistent-carrier', daemon=True)
            self._reader.start()
            while True:
                try:
                    actual = self._rpc({'op': 'attach'})
                    break
                except _ControllerNotAdmitted:
                    if time.monotonic() >= deadline:
                        channel.close()
                        self._started = False
                        raise ManagementError('outcome_unknown', 'The exact worker remains unadmitted; no original execution was replaced.') from None
                    time.sleep(0.05)
            if actual != identity or type(identity.get('pid')) is not int:
                raise ManagementError('binding_conflict', 'The connected owned generation differs from its registered identity.')
            os.kill(identity['pid'], 0)
            self.native_identity = identity
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            channel = self._socket
            if channel:
                try:
                    channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                channel.close()
            self.close_outcome = {'kind': 'supervisor_detached', 'exit_code': None}
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)

    def shutdown_owned(self):
        self._start()
        identity = dict(self.native_identity)
        self._rpc({'op': 'shutdown'})
        deadline = time.monotonic() + self.timeout
        while self.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.close()
        if self._process is not None:
            code = self._process.wait(timeout=self.timeout)
        else:
            while not self.exit_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            receipt = json.loads(self.exit_path.read_text()) if self.exit_path.exists() and not self.exit_path.is_symlink() else {}
            code = receipt.get('exit_code') if all(receipt.get(k) == identity.get(k) for k in
                       ('pid', 'generation', 'created_at_ms', 'configuration_sha256')) and receipt.get('shutdown_requested') is True else None
        self.close_outcome = {'kind': 'original_exit' if code == 0 and not self.socket_path.exists() else 'unconfirmed',
                              'exit_code': code, 'pid': identity['pid']}
        if self.close_outcome['kind'] == 'original_exit':
            self.socket_path.parent.rmdir()
