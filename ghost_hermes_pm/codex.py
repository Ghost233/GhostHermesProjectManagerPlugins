"""Owned Codex 0.160.1 stdio transport; connection identity is never a HOME path."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .manager import ManagementError


def repository_fingerprint(repository):
    return hashlib.sha256(json.dumps(repository, sort_keys=True).encode()).hexdigest()


class CodexStdioAdapter:
    """Real JSONL RPC. A trusted host verifier supplies current execution evidence.

    The verifier is a Python capability boundary, never supplied by a chat/HTTP body.
    Without it observation is possible, but task creation remains disabled.
    """
    def __init__(self, command, *, cwd, env, service_ref, verifier=None, timeout=10):
        if not command or not Path(command[0]).is_absolute() or not Path(cwd).is_absolute() or not service_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit local executable and owned working directory are required.')
        if not isinstance(env, dict) or not Path(env.get('CODEX_HOME', '')).is_absolute():
            raise ManagementError('invalid_change', 'An explicit environment and registered CODEX_HOME are required.')
        self.command, self.cwd, self.env = list(command), str(cwd), dict(env)
        self.service_ref, self.verifier, self.timeout = service_ref, verifier, timeout
        self.generation = str(uuid.uuid4())
        self._condition = threading.Condition()
        self._rpc_lock = threading.RLock()
        self._responses, self._events = {}, []
        self._outgoing, self._server_requests = set(), {}
        self._counter, self._process, self.connection = 0, None, None
        self._closed = False
        self._failure_reason = None

    def connect(self):
        with self._rpc_lock:
            if self._closed:
                raise ManagementError('unavailable', 'This connection generation has ended.')
            if self.connection is not None:
                self._alive()
                return self.connection
            if self._process is not None:
                raise ManagementError('unavailable', 'Incomplete initialization requires reconciliation, not reconnect replay.')
            try:
                self._process = subprocess.Popen(self.command, cwd=self.cwd, env=self.env,
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise ManagementError('unavailable', 'The registered Codex process could not start.') from exc
            self._reader = threading.Thread(target=self._read, name='hermes-pm-codex-stdio', daemon=True)
            self._reader.start()
            result = self._call('initialize', {'clientInfo': {'name': 'ghost-hermes-pm', 'version': '0.1.0'},
                                                'capabilities': {'experimentalApi': True, 'requestAttestation': False}})
            if not isinstance(result, dict) or any(not isinstance(result.get(k), str) or not result[k] for k in ('userAgent', 'codexHome', 'platformOs', 'platformFamily')):
                raise ManagementError('capability_unverified', 'Codex initialization identity is incomplete.')
            if Path(result['codexHome']).resolve() != Path(self.env['CODEX_HOME']).resolve():
                raise ManagementError('capability_unverified', 'The service state root does not match its registered source.')
            self._write({'method': 'initialized'})
            self.connection = {'service_ref': self.service_ref, 'service_id': self.service_ref + ':' + str(self._process.pid),
                               'generation': self.generation, 'pid': self._process.pid,
                               'user_agent': result['userAgent'], 'platform': result['platformOs'],
                               'verified_at': datetime.now(timezone.utc).isoformat()}
            return dict(self.connection)

    def _alive(self):
        if self._closed or self._process is None or self._process.poll() is not None:
            raise ManagementError('unavailable', 'The original Codex connection is unavailable; execution needs reconciliation.')

    def _write(self, envelope):
        self._alive()
        try:
            self._process.stdin.write((json.dumps(envelope) + '\n').encode())
            self._process.stdin.flush()
        except OSError as exc:
            raise ManagementError('outcome_unknown', 'The RPC outcome is unknown; it must not be replayed.') from exc

    def _read(self):
        try:
            while True:
                line = self._process.stdout.readline(16 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 16 * 1024 * 1024 or not line.endswith(b'\n'):
                    self._failure_reason = 'Codex JSONL exceeds the 16 MiB frame bound; executor liveness remains unknown.'
                    break
                envelope = json.loads(line)
                if not isinstance(envelope, dict):
                    break
                with self._condition:
                    if 'method' in envelope:
                        if not isinstance(envelope['method'], str) or not isinstance(envelope.get('params', {}), dict) or ('id' in envelope and type(envelope['id']) not in (str, int)):
                            break
                        rpc_id = envelope.get('id')
                        if type(rpc_id) in (str, int):
                            key = (type(rpc_id), rpc_id)
                            if key in self._server_requests:
                                if self._server_requests[key]['envelope'] != envelope:
                                    break
                            else:
                                self._server_requests[key] = {'envelope': envelope, 'state': 'pending'}
                        elif envelope['method'] == 'serverRequest/resolved':
                            resolved = envelope.get('params', {})
                            key = (type(resolved.get('requestId')), resolved.get('requestId'))
                            current = self._server_requests.get(key)
                            if current and current['envelope'].get('params', {}).get('threadId') == resolved.get('threadId'):
                                current['state'] = 'resolved'
                        self._events.append(envelope)
                        if len(self._events) > 10000:
                            break
                    elif type(envelope.get('id')) in (str, int):
                        key = (type(envelope['id']), envelope['id'])
                        if key in self._outgoing:
                            self._responses[key] = envelope
                    else:
                        break
                    self._condition.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def _call(self, method, params):
        with self._rpc_lock:
            self._counter += 1
            request_id = self._counter
            with self._condition:
                self._outgoing.add((int, request_id))
            self._write({'id': request_id, 'method': method, 'params': params})
            deadline = time.monotonic() + self.timeout
            with self._condition:
                key = (int, request_id)
                while key not in self._responses:
                    remaining = deadline - time.monotonic()
                    if self._closed or remaining <= 0:
                        raise ManagementError('outcome_unknown' if method in {'thread/start', 'turn/start', 'turn/steer', 'turn/interrupt'} else 'unavailable',
                                              self._failure_reason or 'Codex response was not confirmed; mutating requests are never replayed.')
                    self._condition.wait(remaining)
                response = self._responses.pop(key)
                self._outgoing.discard(key)
            if 'error' in response:
                raise ManagementError('service_rejected', 'Codex rejected ' + method + '; inspect the registered original service.')
            if 'result' not in response or not isinstance(response['result'], dict):
                raise ManagementError('outcome_unknown', 'Codex returned an unrecognized response.')
            return response['result']

    def _pages(self, method, params):
        items, cursors = [], set()
        while True:
            result = self._call(method, params)
            if not isinstance(result.get('data'), list):
                raise ManagementError('capability_unverified', 'Codex list coverage could not be verified.')
            items.extend(result['data'])
            if 'nextCursor' not in result:
                raise ManagementError('capability_unverified', 'Codex list pagination coverage is missing.')
            cursor = result.get('nextCursor')
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursors) >= 100:
                raise ManagementError('capability_unverified', 'Codex list pagination is incomplete.')
            cursors.add(cursor)
            params = {**params, 'cursor': cursor}

    def verify_start(self, repository):
        connection = self.connect()
        if self.verifier is None:
            raise ManagementError('capability_unverified', 'Platform filesystem and tool enforcement evidence is missing; task start remains disabled.')
        proof = self.verifier(dict(connection), json.loads(json.dumps(repository)))
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths', 'task_start', 'manual_execution_coverage', 'model')
        if not isinstance(proof, dict) or any(not isinstance(proof.get(k), str) or not proof[k] for k in required) or proof.get('generation') != self.generation or proof.get('service_id') != connection['service_id'] or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']]:
            raise ManagementError('capability_unverified', 'Execution evidence is incomplete or belongs to another connection or repository.')
        if '0.160.1' not in connection['user_agent']:
            raise ManagementError('capability_unverified', 'The connected Codex protocol version is not verified.')
        profiles = self._pages('permissionProfile/list', {'cwd': repository['worktree'], 'limit': 100})
        if not any(isinstance(p, dict) and p.get('id') == proof['permission_profile'] and p.get('allowed') is True for p in profiles):
            raise ManagementError('capability_unverified', 'The verified permission profile is not selectable on this service.')
        # Full loaded/list coverage comes from this executor only. A host verifier must
        # also establish manual/desktop coverage; matching histories cannot do so.
        loaded = self._pages('thread/loaded/list', {'limit': 100})
        for thread_id in loaded:
            thread = self.read_thread(thread_id)
            if thread.get('cwd') == repository['worktree'] and thread.get('status', {}).get('type') != 'idle':
                raise ManagementError('repository_busy', 'The registered service has unfinished execution in this repository.')
        return dict(proof)

    def start_thread(self, repository, proof):
        self._alive()
        return self._call('thread/start', {'cwd': repository['worktree'], 'runtimeWorkspaceRoots': proof['runtime_roots'],
                                           'permissions': proof['permission_profile'], 'model': proof['model'],
                                           'approvalPolicy': 'untrusted', 'ephemeral': False})

    def verify_thread(self, response, repository, proof):
        thread = response.get('thread')
        profile = response.get('activePermissionProfile')
        if not isinstance(thread, dict) or not isinstance(profile, dict) or not isinstance(thread.get('id'), str) or not thread.get('id') or thread.get('cwd') != repository['worktree'] or response.get('cwd') != repository['worktree'] or thread.get('canAcceptDirectInput') is not True or thread.get('cliVersion') != '0.160.1' or profile.get('id') != proof['permission_profile'] or response.get('runtimeWorkspaceRoots') != proof['runtime_roots']:
            raise ManagementError('capability_unverified', 'The created thread did not confirm the verified runtime boundary.')
        self._alive()

    def start_turn(self, thread_id, prompt):
        return self._call('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]})

    def verify_control(self, repository, action, expected_capability):
        self._alive()
        if self.verifier is None or self.connection is None:
            raise ManagementError('capability_unverified', 'Current-service task control receipts are missing.')
        proof = self.verifier(dict(self.connection), json.loads(json.dumps(repository)))
        if not isinstance(proof, dict) or proof.get('generation') != self.generation or proof.get('service_id') != self.connection['service_id'] or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']] or not isinstance(proof.get('task_control'), dict) or not isinstance(proof['task_control'].get(action), str) or not proof['task_control'][action]:
            raise ManagementError('capability_unverified', 'This task control has not been verified on the original service and boundary.')
        if any(proof.get(k) != expected_capability.get(k) for k in ('permission_profile', 'policy_digest', 'runtime_roots')):
            raise ManagementError('capability_unverified', 'The fresh control proof does not match the original task permission boundary.')
        return proof

    def steer_turn(self, thread_id, turn_id, text, instruction_id):
        return self._call('turn/steer', {'threadId': thread_id, 'expectedTurnId': turn_id,
            'input': [{'type': 'text', 'text': text, 'text_elements': []}], 'clientUserMessageId': instruction_id})

    @staticmethod
    def verify_idle(thread, previous_turn_id, known_turn_ids):
        turns = thread.get('turns', [])
        previous = next((t for t in turns if t.get('id') == previous_turn_id), None)
        if any(t.get('id') not in known_turn_ids for t in turns):
            raise ManagementError('binding_conflict', 'Unregistered intervening turns require reconciliation before input.')
        if thread.get('status', {}).get('type') != 'idle' or any(t.get('status') == 'inProgress' for t in turns) or not previous or previous.get('status') not in {'completed', 'failed', 'interrupted'} or previous.get('itemsView') != 'full':
            raise ManagementError('binding_conflict', 'The original thread is not verified idle at the expected prior turn.')

    def start_idle_turn(self, thread_id, previous_turn_id, text, instruction_id, *, expected_cwd, known_turn_ids):
        # turn/start has no expected-idle precondition. A host receipt must prove
        # an exclusive input path; this lock serializes the owned adapter writers.
        with self._rpc_lock:
            thread = self.read_thread(thread_id)
            if thread.get('id') != thread_id or thread.get('cwd') != expected_cwd or thread.get('canAcceptDirectInput') is not True:
                raise ManagementError('binding_conflict', 'The original idle input target changed.')
            self.verify_idle(thread, previous_turn_id, known_turn_ids)
            result = self._call('turn/start', {'threadId': thread_id, 'clientUserMessageId': instruction_id,
                'input': [{'type': 'text', 'text': text, 'text_elements': []}]})
            turn = result.get('turn', {})
            if not isinstance(turn, dict) or not isinstance(turn.get('id'), str) or not turn['id'] or turn['id'] in {t.get('id') for t in thread.get('turns', [])} or turn.get('status') != 'inProgress':
                raise ManagementError('outcome_unknown', 'A distinct original-thread new turn was not confirmed; do not replay.')
            return result

    def interrupt_turn(self, thread_id, turn_id):
        return self._call('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})

    def verify_process_coverage(self, session, stop):
        proof = self.verify_control(session['repository'], 'related_execution', session['capability'])
        coverage = proof.get('process_coverage')
        if not isinstance(coverage, dict) or not isinstance(coverage.get('evidence'), str) or not coverage['evidence']:
            raise ManagementError('capability_unverified', 'Host process coverage is missing; empty native background lists cannot prove all related processes stopped.')
        if coverage.get('kind') == 'task_processes_stopped':
            if coverage.get('thread_id') != session['thread_id'] or coverage.get('turn_id') != stop['turn_id'] or coverage.get('all_registered_processes_exited') is not True:
                raise ManagementError('capability_unverified', 'Host process exit evidence does not identify the original task and turn.')
        elif coverage.get('kind') != 'no_unregistered_process_paths':
            raise ManagementError('capability_unverified', 'Unregistered child process execution paths remain unverified.')
        return {**coverage, 'generation': session['generation'], 'service_id': session['service_id'],
                'repository_fingerprint': proof['repository_fingerprint']}

    def background_terminals(self, thread_id):
        return self._pages('thread/backgroundTerminals/list', {'threadId': thread_id, 'limit': 100})

    def loaded_threads(self):
        return self._pages('thread/loaded/list', {'limit': 100})

    def read_thread(self, thread_id):
        self.connect()
        thread = self._call('thread/read', {'threadId': thread_id, 'includeTurns': True}).get('thread')
        if not isinstance(thread, dict) or not isinstance(thread.get('status'), dict) or not isinstance(thread.get('turns', []), list) or any(not isinstance(t, dict) or not isinstance(t.get('items', []), list) or any(not isinstance(i, dict) for i in t.get('items', [])) for t in thread.get('turns', [])):
            raise ManagementError('capability_unverified', 'The original thread read is malformed or incomplete.')
        return thread

    def server_requests(self, thread_id):
        with self._condition:
            return [json.loads(json.dumps(r)) for r in self._server_requests.values()
                    if r['envelope'].get('params', {}).get('threadId') == thread_id]

    def respond_server_request(self, rpc_id, envelope, result):
        with self._rpc_lock, self._condition:
            self._alive()
            current = self._server_requests.get((type(rpc_id), rpc_id))
            if not current or current['state'] != 'pending' or current['envelope'] != envelope:
                raise ManagementError('binding_conflict', 'The original request is no longer pending on this connection.')
            current['state'] = 'intent'
            try:
                self._write({'id': rpc_id, 'result': result})
            except ManagementError:
                current['state'] = 'outcome_unknown'
                raise
            current['state'] = 'sent'

    def take_events(self, thread_id):
        with self._condition:
            matching = [e for e in self._events if isinstance(e.get('params'), dict) and e['params'].get('threadId') == thread_id]
            self._events = [e for e in self._events if e not in matching]
            return matching

    def close(self):
        with self._rpc_lock:
            if self._process is None:
                self._closed = True
                return
            try:
                self._process.stdin.close()
                self._process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
            self._closed = True
            self._process.stdout.close()


def configured_adapter(config, state_dir):
    """Trusted host configuration only. A current-generation report cannot be a Boolean."""
    if not config:
        return None
    allowed = {'command', 'cwd', 'environment', 'service_ref'}
    if not isinstance(config, dict) or set(config) - allowed or any(k not in config for k in allowed):
        raise ManagementError('invalid_change', 'Codex stdio configuration requires explicit command, cwd, environment and service reference.')
    evidence_path = Path(state_dir) / 'codex-validation.json'

    def verifier(connection, repository):
        try:
            if evidence_path.stat().st_size > 65536:
                raise ValueError('Oversized proof.')
            evidence = json.loads(evidence_path.read_text())
        except (OSError, ValueError) as exc:
            raise ManagementError('capability_unverified', 'No current-generation platform/tool/start verification report; actual task start remains disabled.') from exc
        from .validation import validate_receipts
        try:
            return validate_receipts(evidence, connection, repository, config['command'], config['environment'], state_dir)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ManagementError('capability_unverified', 'The current-service capability receipt set is incomplete.') from exc

    return CodexStdioAdapter(config['command'], cwd=config['cwd'], env=config['environment'],
                             service_ref=config['service_ref'], verifier=verifier)
