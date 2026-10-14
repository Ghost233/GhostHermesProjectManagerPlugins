"""Synthetic normalized executor boundary for business regressions only.

This test double owns disposable JSONL fixture peers. Its fixture/* messages
are not a DSH protocol, capability proof or real-service acceptance result.
Actual DSH HTTP and stream contracts are tested separately.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from ghost_hermes_pm.manager import ManagementError


def repository_fingerprint(repository):
    return hashlib.sha256(json.dumps(repository, sort_keys=True).encode()).hexdigest()


class SyntheticExecutorAdapter:
    """Real JSONL RPC. A trusted host verifier supplies current execution evidence.

    The verifier is a Python capability boundary, never supplied by a chat/HTTP body.
    Without it observation is possible, but task creation remains disabled.
    """
    def __init__(self, command, *, cwd, env, service_ref, verifier=None, timeout=10):
        if not command or not Path(command[0]).is_absolute() or not Path(cwd).is_absolute() or not service_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit local executable and owned working directory are required.')
        if not isinstance(env, dict) or not Path(env.get('FIXTURE_HOME', '')).is_absolute():
            raise ManagementError('invalid_change', 'An explicit environment and registered FIXTURE_HOME are required.')
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
                raise ManagementError('unavailable', 'The registered Synthetic executor process could not start.') from exc
            self._reader = threading.Thread(target=self._read, name='hermes-pm-fixture-stdio', daemon=True)
            self._reader.start()
            result = self._call('fixture/connect', {'clientInfo': {'name': 'ghost-hermes-pm', 'version': '0.1.0'},
                                                'capabilities': {'experimentalApi': True, 'requestAttestation': False}})
            if not isinstance(result, dict) or any(not isinstance(result.get(k), str) or not result[k] for k in ('userAgent', 'fixtureHome', 'platformOs', 'platformFamily')):
                raise ManagementError('capability_unverified', 'Synthetic executor initialization identity is incomplete.')
            if Path(result['fixtureHome']).resolve() != Path(self.env['FIXTURE_HOME']).resolve():
                raise ManagementError('capability_unverified', 'The service state root does not match its registered source.')
            self._write({'method': 'fixture/ready'})
            self.connection = {'service_ref': self.service_ref, 'service_id': self.service_ref + ':' + str(self._process.pid),
                               'generation': self.generation, 'pid': self._process.pid,
                               'user_agent': result['userAgent'], 'platform': result['platformOs'],
                               'engine': 'dsh', 'client_id': 'fixture-client:' + self.generation,
                               'verified_at': datetime.now(timezone.utc).isoformat()}
            return dict(self.connection)

    def _alive(self):
        if self._closed or self._process is None or self._process.poll() is not None:
            raise ManagementError('unavailable', 'The original Synthetic executor connection is unavailable; execution needs reconciliation.')

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
                    self._failure_reason = 'Synthetic executor JSONL exceeds the 16 MiB frame bound; executor liveness remains unknown.'
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
                        raise ManagementError('outcome_unknown' if method in {'fixture/create', 'fixture/start', 'fixture/append', 'fixture/stop'} else 'unavailable',
                                              self._failure_reason or 'Synthetic executor response was not confirmed; mutating requests are never replayed.')
                    self._condition.wait(remaining)
                response = self._responses.pop(key)
                self._outgoing.discard(key)
            if 'error' in response:
                raise ManagementError('service_rejected', 'Synthetic executor rejected ' + method + '; inspect the registered original service.')
            if 'result' not in response or not isinstance(response['result'], dict):
                raise ManagementError('outcome_unknown', 'Synthetic executor returned an unrecognized response.')
            return response['result']

    def _pages(self, method, params):
        items, cursors = [], set()
        while True:
            result = self._call(method, params)
            if not isinstance(result.get('data'), list):
                raise ManagementError('capability_unverified', 'Synthetic executor list coverage could not be verified.')
            items.extend(result['data'])
            if 'nextCursor' not in result:
                raise ManagementError('capability_unverified', 'Synthetic executor list pagination coverage is missing.')
            cursor = result.get('nextCursor')
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursors) >= 100:
                raise ManagementError('capability_unverified', 'Synthetic executor list pagination is incomplete.')
            cursors.add(cursor)
            params = {**params, 'cursor': cursor}

    def verify_start(self, repository):
        connection = self.connect()
        if self.verifier is None:
            raise ManagementError('capability_unverified', 'Platform filesystem and tool enforcement evidence is missing; task start remains disabled.')
        self.last_start_occupancy = None
        proof = self.verifier(dict(connection), json.loads(json.dumps(repository)))
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths', 'task_start', 'manual_execution_coverage', 'model')
        if not isinstance(proof, dict) or any(not isinstance(proof.get(k), str) or not proof[k] for k in required) or proof.get('generation') != self.generation or proof.get('service_id') != connection['service_id'] or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']]:
            raise ManagementError('capability_unverified', 'Execution evidence is incomplete or belongs to another connection or repository.')
        if 'fixture-v1' not in connection['user_agent']:
            raise ManagementError('capability_unverified', 'The connected Synthetic executor protocol version is not verified.')
        profiles = self._pages('fixture/policy', {'cwd': repository['worktree'], 'limit': 100})
        if not any(isinstance(p, dict) and p.get('id') == proof['permission_profile'] and p.get('allowed') is True for p in profiles):
            raise ManagementError('capability_unverified', 'The verified permission profile is not selectable on this service.')
        # Full loaded/list coverage comes from this executor only. A host verifier must
        # also establish manual/desktop coverage; matching histories cannot do so.
        loaded = self._pages('fixture/loaded', {'limit': 100})
        for thread_id in loaded:
            thread = self.read_thread(thread_id)
            from ghost_hermes_pm.queue import logical_repository
            if logical_repository(thread.get('cwd')) != repository['logical_id']:
                continue
            if thread.get('status', {}).get('type') != 'idle' or any(t.get('status') not in {'completed', 'failed', 'interrupted'} or t.get('itemsView') != 'full' for t in thread.get('turns', [])) or self.background_terminals(thread_id):
                self.last_start_occupancy = {'thread_id': thread_id, 'cwd': thread['cwd'], 'logical_repository': repository['logical_id']}
                raise ManagementError('repository_busy', 'The registered service has unfinished or unknown execution in this logical repository.')
        return dict(proof)

    def start_thread(self, repository, proof):
        self._alive()
        return self._call('fixture/create', {'cwd': repository['worktree'], 'runtimeWorkspaceRoots': proof['runtime_roots'],
                                           'permissions': proof['permission_profile'], 'model': proof['model'],
                                           'approvalPolicy': 'untrusted', 'ephemeral': False})

    def verify_thread(self, response, repository, proof):
        thread = response.get('thread')
        profile = response.get('activePermissionProfile')
        if not isinstance(thread, dict) or not isinstance(profile, dict) or not isinstance(thread.get('id'), str) or not thread.get('id') or thread.get('cwd') != repository['worktree'] or response.get('cwd') != repository['worktree'] or thread.get('canAcceptDirectInput') is not True or thread.get('cliVersion') != 'fixture-v1' or profile.get('id') != proof['permission_profile'] or response.get('runtimeWorkspaceRoots') != proof['runtime_roots']:
            raise ManagementError('capability_unverified', 'The created thread did not confirm the verified runtime boundary.')
        self._alive()

    def start_turn(self, thread_id, prompt):
        return self._call('fixture/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]})

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

    def bind_repository(self, thread_id, repository, proof):
        # A business-boundary fixture stores the admitted normalized context only.
        self.bound_repository = (thread_id, json.loads(json.dumps(repository)), json.loads(json.dumps(proof)))

    def steer_turn(self, thread_id, turn_id, text, instruction_id):
        return self._call('fixture/append', {'threadId': thread_id, 'expectedTurnId': turn_id,
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
        # fixture/start has no expected-idle precondition. A host receipt must prove
        # an exclusive input path; this lock serializes the owned adapter writers.
        with self._rpc_lock:
            thread = self.read_thread(thread_id)
            if thread.get('id') != thread_id or thread.get('cwd') != expected_cwd or thread.get('canAcceptDirectInput') is not True:
                raise ManagementError('binding_conflict', 'The original idle input target changed.')
            self.verify_idle(thread, previous_turn_id, known_turn_ids)
            result = self._call('fixture/start', {'threadId': thread_id, 'clientUserMessageId': instruction_id,
                'input': [{'type': 'text', 'text': text, 'text_elements': []}]})
            turn = result.get('turn', {})
            if not isinstance(turn, dict) or not isinstance(turn.get('id'), str) or not turn['id'] or turn['id'] in {t.get('id') for t in thread.get('turns', [])} or turn.get('status') != 'inProgress':
                raise ManagementError('outcome_unknown', 'A distinct original-thread new turn was not confirmed; do not replay.')
            return result

    def interrupt_turn(self, thread_id, turn_id):
        result = self._call('fixture/stop', {'threadId': thread_id, 'turnId': turn_id})
        if result != {}:
            raise ManagementError('outcome_unknown', 'Synthetic cancellation admission was not confirmed.')
        return {'accepted': True}

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
        rows = self._pages('fixture/background', {'threadId': thread_id, 'limit': 100})
        return [row if isinstance(row, dict) and 'id' in row else
                {'id': row.get('itemId'), 'kind': 'fixture', 'owner': thread_id,
                 'status': row.get('status', 'running'), 'command': row.get('command')}
                for row in rows]

    def loaded_threads(self):
        return self._pages('fixture/loaded', {'limit': 100})

    def list_sessions(self, source_kinds=None, archived=None):
        return self._pages('fixture/list', {'limit': 100, 'sourceKinds': source_kinds or [getattr(self, 'source_kind', 'desktop')],
                                          'useStateDbOnly': True})

    def read_thread(self, thread_id):
        self.connect()
        thread = self._call('fixture/read', {'threadId': thread_id, 'includeTurns': True}).get('thread')
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


READ_METHODS = frozenset({'fixture/connect','fixture/ready','fixture/list','fixture/loaded','fixture/read','fixture/turns','fixture/background'})
CONTROL_METHODS = frozenset({'fixture/start','fixture/append','fixture/stop'})
SOURCE_KINDS = {'desktop','web'}

class SyntheticReadOnlyAdapter(SyntheticExecutorAdapter):
    def __init__(self, command, *, cwd, env, service_ref, source_kind, endpoint_ref, verifier=None, timeout=10, transport='synthetic_domain_pipe'):
        super().__init__(command, cwd=cwd, env=env, service_ref=service_ref, timeout=timeout)
        if source_kind not in SOURCE_KINDS or not isinstance(endpoint_ref, str) or not endpoint_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit original local endpoint and individual source kind are required.')
        self.source_kind, self.endpoint_ref, self.observation_verifier = source_kind, endpoint_ref, verifier
        if transport not in {'synthetic_domain_pipe', 'synthetic_domain_alt'}:
            raise ManagementError('invalid_change', 'Select an explicit registered observation transport.')
        self.transport = transport

    def proof(self):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'source_kind': self.source_kind,
                   'endpoint_ref': self.endpoint_ref, 'transport': self.transport}
        proof = self.observation_verifier(binding) if callable(self.observation_verifier) else None
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or any(not isinstance(proof.get(k), str) or not proof[k] for k in ('original_executor_id', 'provenance', 'evidence_ref')) or not isinstance(proof.get('supported_methods'), list) or not {'fixture/read', 'fixture/list', 'fixture/loaded'}.issubset(proof['supported_methods']) or not isinstance(proof.get('source_kinds'), list) or not proof['source_kinds'] or any(not isinstance(method, str) or method not in READ_METHODS for method in proof['supported_methods']) or any(kind not in SOURCE_KINDS for kind in proof['source_kinds']):
            raise ManagementError('capability_unverified', 'Current original-executor identity and actual read capabilities require trusted host evidence; matching history is insufficient.')
        from ghost_hermes_pm.manager import _public_text
        for key in ('original_executor_id', 'provenance', 'evidence_ref'):
            _public_text(proof[key])
        return proof

    def connect(self):
        proof = self.proof()
        if self.connection and self.connection.get('control') == 'observe_only' and self.connection.get('service_id') != proof['original_executor_id']:
            raise ManagementError('binding_conflict', 'The existing original-service connection conflicts with current host identity evidence.')
        connection = super().connect()
        self.connection.update(service_id=proof['original_executor_id'], transport=self.transport, endpoint_ref=self.endpoint_ref,
                               source_kind=self.source_kind, control='observe_only')
        return dict(self.connection)

    def _write(self, envelope):
        if envelope.get('method') not in READ_METHODS or 'result' in envelope or 'error' in envelope:
            raise ManagementError('forbidden', 'Observation transport permits only the explicit read-method allowlist; server requests receive no reply.')
        super()._write(envelope)

    def _call(self, method, params):
        if method not in READ_METHODS:
            raise ManagementError('forbidden', 'Observation cannot execute, resume, steer, interrupt or answer requests.')
        return super()._call(method, params)

    def read_thread(self, thread_id, include_turns=True):
        self.connect()
        thread = self._call('fixture/read', {'threadId': thread_id, 'includeTurns': include_turns}).get('thread')
        if not isinstance(thread, dict) or thread.get('id') != thread_id or not isinstance(thread.get('status'), dict):
            raise ManagementError('capability_unverified', 'Original executor returned incomplete or conflicting thread identity.')
        return thread



class SyntheticControlAdapter(SyntheticExecutorAdapter):
    """Independent original proxy. The readonly adapter is never promoted."""
    def __init__(self, command, *, cwd, env, service_ref, source_kind, endpoint_ref, verifier=None, timeout=10):
        super().__init__(command, cwd=cwd, env=env, service_ref=service_ref, timeout=timeout)
        if source_kind not in SOURCE_KINDS or not isinstance(endpoint_ref, str) or not endpoint_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit original endpoint and individual supported source kind are required.')
        self.source_kind, self.endpoint_ref, self.control_verifier = source_kind, endpoint_ref, verifier
        self.origin_proof, self.authority = None, None

    def _proof(self, repository, context):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'service_id': context['original_executor_id'],
                   'endpoint_ref': self.endpoint_ref, 'source_kind': self.source_kind, 'transport': 'synthetic_domain_pipe'}
        if self.connection is not None:
            binding['platform'] = self.connection['platform']
        proof = self.control_verifier(binding, repository, context) if callable(self.control_verifier) else None
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths', 'manual_execution_coverage', 'takeover', 'control_access')
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or proof.get('grant_binding') != context or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']] or any(not isinstance(proof.get(k), str) or not proof[k] for k in required) or proof.get('control_access') != 'verified-original-input-path':
            raise ManagementError('capability_unverified', 'Current original-service control, task scope and complete write/tool boundary evidence are unavailable; takeover is not enabled.')
        actions = {'append', 'stop', 'continue', 'idle_input', 'related_execution', 'human_response'}
        if not isinstance(proof.get('task_control'), dict) or any(not isinstance(proof['task_control'].get(action), str) or not proof['task_control'][action] for action in actions):
            raise ManagementError('capability_unverified', 'Original append, stop, continuation and owner-response methods need their own current capability evidence.')
        return proof

    def verify_takeover(self, repository, context):
        proof = self._proof(repository, context)
        self.origin_proof = proof
        self.connect()
        proof = self._proof(repository, context)
        self.origin_proof = proof
        return proof

    def connect(self):
        if self.origin_proof is None:
            raise ManagementError('capability_unverified', 'An owner-authorized original target and current host proof are required before connecting.')
        if self.connection and self.connection.get('service_id') != self.origin_proof['service_id']:
            raise ManagementError('binding_conflict', 'The control connection belongs to another original executor.')
        connection = super().connect()
        self.connection.update(service_id=self.origin_proof['service_id'], endpoint_ref=self.endpoint_ref, source_kind=self.source_kind,
                               transport='synthetic_domain_pipe', control='manual_work_grant')
        return dict(self.connection)

    def _write(self, envelope):
        method = envelope.get('method')
        if method in READ_METHODS:
            return super()._write(envelope)
        if method is not None and method not in CONTROL_METHODS:
            raise ManagementError('forbidden', 'Manual takeover cannot create/resume/fork a thread or control the daemon.')
        authority = self.authority() if callable(self.authority) else None
        if not authority or authority['status'] != 'active' or authority['id'] != self.origin_proof['grant_binding']['grant_id']:
            raise ManagementError('forbidden', 'No active current-work grant authorizes this original input.')
        params = envelope.get('params', {})
        if method is None:
            incoming = self._server_requests.get((type(envelope.get('id')), envelope.get('id')))
            params = incoming['envelope'].get('params', {}) if incoming else {}
        if params.get('threadId') != authority['thread_id'] or method in {'fixture/append', 'fixture/stop'} and params.get('expectedTurnId', params.get('turnId')) != authority['turn_id'] or method is None and params.get('turnId') != authority['turn_id']:
            raise ManagementError('binding_conflict', 'The input is outside the original granted thread/current turn.')
        return super()._write(envelope)

    def verify_control(self, repository, action, expected_capability):
        authority = self.authority() if callable(self.authority) else None
        if action != 'related_execution' and (not authority or authority['status'] != 'active'):
            raise ManagementError('forbidden', 'The current-work grant has ended; the original session is observe-only.')
        context = dict(expected_capability['grant_binding'])
        context['current_turn_id'] = authority['turn_id'] if authority else context['current_turn_id']
        proof = self._proof(repository, context)
        if not isinstance(proof.get('task_control'), dict) or not isinstance(proof['task_control'].get(action), str) or not proof['task_control'][action] or any(proof.get(k) != expected_capability.get(k) for k in ('permission_profile', 'policy_digest', 'runtime_roots')):
            raise ManagementError('capability_unverified', 'The current original control capability differs from the granted immutable boundary.')
        self.origin_proof = proof
        return proof

    def verify_start(self, repository):
        if self.origin_proof is None:
            raise ManagementError('capability_unverified', 'No original work grant exists.')
        return self.verify_control(repository, 'continue', self.origin_proof)

    def revoke(self, thread_id=None):
        with self._condition:
            for incoming in self._server_requests.values():
                if incoming['state'] == 'pending' and (thread_id is None or incoming['envelope'].get('params', {}).get('threadId') == thread_id):
                    incoming['state'] = 'control_returned'


class SyntheticRecoveryAdapter(SyntheticControlAdapter):
    """A separate, host-verified proxy of an existing executor; never a new server."""
    def _proof(self, repository, context):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'service_id': context['service_id'],
                   'endpoint_ref': self.endpoint_ref, 'source_kind': self.source_kind, 'transport': 'synthetic_domain_pipe'}
        if self.connection is not None:
            binding['platform'] = self.connection['platform']
        proof = self.control_verifier(binding, repository, context) if callable(self.control_verifier) else None
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths', 'manual_execution_coverage', 'recovery')
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or proof.get('recovery_binding') != context or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']] or any(not isinstance(proof.get(k), str) or not proof[k] for k in required):
            raise ManagementError('capability_unverified', 'Fresh host evidence for this original executor, session and boundary is missing; matching history cannot recover control.')
        return proof

    def verify_recovery(self, repository, context):
        self.origin_proof = self._proof(repository, context)
        self.connect()
        self.origin_proof = self._proof(repository, context)
        return self.origin_proof

    def _write(self, envelope):
        method = envelope.get('method')
        if method in READ_METHODS:
            return SyntheticExecutorAdapter._write(self, envelope)
        if method is not None and method not in CONTROL_METHODS:
            raise ManagementError('forbidden', 'Recovery cannot create, resume or fork a thread or replace the original service.')
        authority = self.authority() if callable(self.authority) else None
        context = (self.origin_proof or {}).get('recovery_binding', {})
        if not authority or authority['status'] != 'active' or authority['id'] != context.get('request_id'):
            raise ManagementError('forbidden', 'Recovered observation does not grant new current-work authority.')
        params = envelope.get('params', {})
        if method is None:
            incoming = self._server_requests.get((type(envelope.get('id')), envelope.get('id')))
            params = incoming['envelope'].get('params', {}) if incoming else {}
        if params.get('threadId') != authority['thread_id'] or method in {'fixture/append', 'fixture/stop'} and params.get('expectedTurnId', params.get('turnId')) != authority['turn_id'] or method is None and params.get('turnId') != authority['turn_id']:
            raise ManagementError('binding_conflict', 'Recovered input must identify the same original task and current turn.')
        return SyntheticExecutorAdapter._write(self, envelope)

    def verify_control(self, repository, action, expected_capability):
        context = dict(expected_capability['recovery_binding'])
        authority = self.authority() if callable(self.authority) else None
        if action != 'related_execution' and (not authority or authority['status'] != 'active'):
            raise ManagementError('forbidden', 'The recovered original task is observe-only.')
        context['turn_id'] = authority['turn_id'] if authority else context['turn_id']
        proof = self._proof(repository, context)
        if any(proof.get(k) != expected_capability.get(k) for k in ('permission_profile', 'policy_digest', 'runtime_roots')) or action != 'related_execution' and proof.get('control_access') != 'verified-original-input-path' or not isinstance(proof.get('task_control'), dict) or not isinstance(proof['task_control'].get(action), str) or not proof['task_control'][action]:
            raise ManagementError('capability_unverified', 'The current recovered action and immutable original boundary have no fresh proof.')
        self.origin_proof = proof
        return proof
