"""Native authorized materializer, fixed runner and original input-watch binding."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from .manager import ManagementError


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class NativeGlobalValidationHost:
    def __init__(self, config, state_dir):
        self.config = config
        self.state_dir = Path(state_dir).resolve()
        self.work = self.state_dir / 'validation-native'
        self.work.mkdir(parents=True, exist_ok=True)
        self.runners, self.watches, self.events, self.watch_bindings = {}, {}, {}, {}
        self.lock = threading.RLock()
        self.configuration_digest = _digest(config)

    def _path(self, identifier, kind):
        return self.work / (hashlib.sha256(identifier.encode()).hexdigest() + '-' + kind + '.json')

    def _worker(self, payload, path):
        env = {**self.config['environment'], 'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([sys.executable, '-m', 'ghost_hermes_pm.native_global_worker', str(path)], cwd=self.work, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        return process

    def prepare(self, attempt, authorization):
        path = self._path(attempt['id'], 'preparation')
        if path.exists():
            raise ManagementError('outcome_unknown', 'A previous preparation receipt exists; reconcile without replay.')
        process = self._worker({'action': 'prepare', 'attempt': attempt, 'authorization': authorization}, path)
        process.wait()
        if process.returncode != 0:
            raise ManagementError('outcome_unknown', 'Authorized materialization did not return a terminal original receipt.')
        receipt = self.find_preparation(attempt, authorization)
        self._watch(attempt)
        return receipt

    def find_preparation(self, attempt, authorization):
        result = self._read(self._path(attempt['id'], 'preparation'))
        if result.get('authorization_digest') != authorization['digest']:
            raise ManagementError('binding_conflict', 'Original materialization authorization changed.')
        return result

    def _read(self, path):
        try:
            if path != path.resolve() or not path.is_relative_to(self.work) or path.stat().st_size > 1048576:
                raise ValueError('Invalid native receipt path.')
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ManagementError('capability_unverified', 'The original native operation receipt is unavailable.') from exc

    def verify_boundary(self, attempt):
        try:
            watch = self.watches.get(attempt['id'])
            if watch is None or watch.poll() is not None:
                raise ValueError('The independently prepared original watch is unavailable.')
            manifest = self.state_dir / 'global-validation-host.json'
            if manifest != manifest.resolve() or manifest.stat().st_size > 65536:
                raise ValueError('Invalid original host manifest.')
            reference = json.loads(manifest.read_text())[attempt['id']]
            path = self.state_dir / reference['path']
            if path != path.resolve() or not path.is_relative_to(self.state_dir / 'validation-evidence') or path.stat().st_size > 65536:
                raise ValueError('Invalid original enforcement receipt.')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                raise ValueError('Original enforcement receipt changed.')
            proof = json.loads(raw)
            expected = {'host_id': self.config['host_id'], 'generation': self.config['generation'], 'runner_configuration_digest': self.configuration_digest,
                **self.watch_bindings[attempt['id']],
                'validation_id': attempt['id'], 'input_digest': attempt['input_digest'], 'source_access': 'read-only', 'git_access': 'read-only', 'artifact_roots': attempt['repository']['test_artifact_paths'],
                'runner_binary_sha256': hashlib.sha256(Path(self.config['runner'][0]).read_bytes()).hexdigest(), 'watcher_binary_sha256': hashlib.sha256(Path(self.config['watcher'][0]).read_bytes()).hexdigest()}
            if any(proof.get(k) != v for k, v in expected.items()) or proof.get('scope') != 'verified-original-host' or type(proof.get('watch_event_cursor')) is not int or not 0 <= proof['watch_event_cursor'] <= len(self.events[attempt['id']]):
                raise ValueError('Original runner/watch proof is not current for this exact configuration and source.')
            # The Manager additionally enforces the complete platform/tool/hardlink matrix.
            result = {**proof, 'receipt': {'path': str(path), 'sha256': reference['sha256']}, 'evidence_ref': str(path)}
            if watch.poll() is not None:
                raise ValueError('Original watch ended while its proof was read.')
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagementError('capability_unverified', 'No current original runner, input-watch and physical boundary proof is installed; native validation remains blocked.') from exc

    def _watch(self, attempt):
        with self.lock:
            if attempt['id'] in self.watches:
                if self.watches[attempt['id']].poll() is not None:
                    raise ManagementError('capability_unverified', 'The original native input watch disconnected.')
                return
            roots = sorted({r['worktree'] for r in [attempt['repository']] + [c['repository'] for c in attempt['children'] + attempt.get('unassigned', [])]} | {r['common_dir'] for r in [attempt['repository']] + [c['repository'] for c in attempt['children'] + attempt.get('unassigned', [])]})
            process = subprocess.Popen(self.config['watcher'] + roots, cwd=self.work, env=self.config['environment'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.watches[attempt['id']], self.events[attempt['id']] = process, []
            self.watch_bindings[attempt['id']] = {'watcher_pid': process.pid, 'input_watch': str(uuid.uuid4())}
            self._path(attempt['id'], 'watch-binding').write_text(json.dumps(self.watch_bindings[attempt['id']]))
            artifacts = [Path(p) for p in attempt['repository']['test_artifact_paths']]
            def collect():
                pending = b''
                while chunk := process.stdout.read(1):
                    if chunk != b'\0':
                        pending += chunk
                        continue
                    event = pending.decode(errors='replace'); pending = b''
                    if not any(Path(event).is_relative_to(p) for p in artifacts):
                        with self.lock:
                            self.events[attempt['id']].append({'source': 'original_native_watch', 'path': event})
            threading.Thread(target=collect, name='hermes-global-input-watch', daemon=True).start()

    def start(self, attempt):
        self.verify_boundary(attempt)
        if any(t not in self.config['tests'] for t in attempt['test_ids']):
            raise ManagementError('invalid_change', 'This native host has no approved fixed runner for a requested test ID.')
        run = {k: attempt['boundary'][k] for k in ('host_id', 'generation')} | {'run_id': 'native-' + attempt['id'], 'validation_id': attempt['id'], 'input_digest': attempt['input_digest']}
        descriptor = self._path(attempt['id'], 'run')
        if descriptor.exists():
            raise ManagementError('outcome_unknown', 'An original run start already exists; reconcile without replay.')
        descriptor.write_text(json.dumps(run))
        path = self._path(run['run_id'], 'result')
        process = self._worker({'action': 'run', 'attempt': attempt, 'run': run, 'runner': self.config['runner'], 'tests': self.config['tests'], 'environment': self.config['environment']}, path)
        self.runners[run['run_id']] = process
        return run

    def find_run(self, attempt):
        return self._read(self._path(attempt['id'], 'run'))

    def read_result(self, run_id):
        return self._read(self._path(run_id, 'result'))

    def read_input_changes(self, attempt):
        with self.lock:
            process = self.watches.get(attempt['id'])
            if process is None or process.poll() is not None:
                raise ManagementError('capability_unverified', 'Original continuous input-watch coverage is unavailable; a replacement watch cannot prove the past run.')
            original = self.watch_bindings[attempt['id']]
            proof = attempt.get('boundary', {})
            if any(proof.get(k) != v for k, v in original.items()):
                raise ManagementError('capability_unverified', 'The actual watcher process and nonce differ from the original verified run.')
            return list(self.events[attempt['id']][proof['watch_event_cursor']:])

    def close(self):
        for process in self.watches.values():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        # An already-started runner is reconciled separately, never cancelled by unload.


def configured_global_validation_host(config, state_dir):
    if not config:
        return None
    required = {'host_id', 'generation', 'runner', 'watcher', 'tests', 'environment'}
    if not isinstance(config, dict) or set(config) != required or any(not isinstance(config.get(k), str) or not config[k] for k in ('host_id', 'generation')) or not isinstance(config['environment'], dict) or not isinstance(config['tests'], dict) or not config['tests']:
        raise ManagementError('invalid_change', 'Global validation needs an explicit original host/generation, fixed runner, original watcher, test IDs and environment.')
    for argv in [config['runner'], config['watcher'], *config['tests'].values()]:
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
            raise ManagementError('invalid_change', 'Native runner and test vectors must be explicit immutable arguments.')
    if any(not Path(config[k][0]).is_absolute() or not Path(config[k][0]).is_file() for k in ('runner', 'watcher')):
        raise ManagementError('invalid_change', 'The approved original runner and watcher binaries must already exist at absolute paths.')
    return NativeGlobalValidationHost(config, state_dir)
