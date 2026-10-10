"""DSH-only original-service adapter with conservative capability admission."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
import uuid

from .dsh_remote import NativeRemoteTransport
from .dsh_events import fold_turns, approval_notice_status
from .manager import ManagementError

READ_METHODS = frozenset({'session/list', 'session/projections', 'session/page', 'session/follow', 'job/list', 'job/follow'})
CONTROL_METHODS = frozenset({'session/create', 'session/prompt', 'session/cancel', '$events/result'})


def repository_fingerprint(repository):
    return hashlib.sha256(json.dumps(repository, sort_keys=True).encode()).hexdigest()


def validate_exclusive_input(proof, connection, repository, thread_id, turn_id):
    exclusive = proof.get('exclusive_input') if isinstance(proof, dict) else None
    expected = {'kind': 'exclusive_original_input', 'generation': connection['generation'],
                'service_id': connection['service_id'], 'repository_fingerprint': repository_fingerprint(repository),
                'thread_id': thread_id, 'turn_id': turn_id}
    if (not isinstance(exclusive, dict) or any(exclusive.get(key) != value for key, value in expected.items())
            or not isinstance(exclusive.get('evidence'), str) or not exclusive['evidence']):
        raise ManagementError('capability_unverified', 'Current DSH exclusive input evidence is required because native mutations have no expected-turn guard.')


class DshRemoteAdapter:
    """Canonical task/session values are derived from native DSH journal facts.

    The trusted verifier supplies filesystem, tool and exclusive input evidence.
    A successful inbox acceptance never substitutes for a durable started turn.
    """
    def __init__(self, base_url, *, cookie='', service_ref, verifier=None, timeout=10,
                 expected_home=None, transport=None):
        if (not isinstance(service_ref, str) or not service_ref.startswith('local:') or len(service_ref) <= 6
                or type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ManagementError('invalid_change', 'An explicit local DSH service reference and bounded timeout are required.')
        validated = NativeRemoteTransport(base_url, cookie, timeout) if base_url is not None or transport is None else None
        self._transport = validated if transport is None else transport
        self.base_url, self.cookie = validated.base_url if validated else None, cookie
        self.service_ref, self.verifier, self.timeout = service_ref, verifier, timeout
        self.expected_home, self.generation = expected_home, str(uuid.uuid4())
        self.connection, self._closed = None, False
        self._condition, self._rpc_lock = threading.Condition(), threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._server_requests, self._events, self._pending_inputs = {}, [], {}
        self._event_stream, self._reader = None, None
        self._control_proof = None
        self._known_repositories = {}
        self._creation_repository = None
        self._created_sessions = {}
        self._creation_intents = {}
        self._deliveries = {}
        self.waterfall_authority = None
        self.waterfall_question_supported = None

    def connect(self):
        with self._rpc_lock:
            with self._lifecycle_lock:
                if self._closed:
                    raise ManagementError('unavailable', 'This original DSH connection generation has ended.')
                if self.connection is not None:
                    return dict(self.connection)
            try:
                self._event_stream = self._transport.stream('$events', {})
                ready = next(self._event_stream)
                if (not isinstance(ready, dict) or set(ready) != {'type', 'clientId', 'host'}
                        or ready.get('type') != 'ready' or not isinstance(ready.get('clientId'), str)
                        or not ready['clientId'] or not isinstance(ready.get('host'), dict)
                        or set(ready['host']) != {'home'} or not isinstance(ready['host']['home'], str)
                        or not Path(ready['host']['home']).is_absolute()
                        or self.expected_home is not None and ready['host']['home'] != self.expected_home):
                    raise ValueError('The original DSH opening identity is incomplete.')
                connection = self._connection_identity(ready)
                with self._lifecycle_lock:
                    if self._closed:
                        raise ManagementError('unavailable', 'This original DSH connection generation has ended.')
                    self.connection = connection
                    self._reader = threading.Thread(target=self._read_events, name='hermes-pm-dsh-events', daemon=True)
                    self._reader.start()
                    return dict(self.connection)
            except Exception:
                self.close()
                raise ManagementError('unavailable', 'The original DSH event generation could not be verified.') from None

    def _connection_identity(self, ready):
        endpoint_digest = hashlib.sha256(self.base_url.encode()).hexdigest()
        home_digest = hashlib.sha256(ready['host']['home'].encode()).hexdigest()
        return {'service_ref': self.service_ref, 'service_id': 'dsh:' + endpoint_digest + ':' + home_digest,
                'generation': self.generation, 'platform': sys.platform, 'engine': 'dsh',
                'transport': 'desktop_http_mux', 'client_id': ready['clientId'],
                'host_home_sha256': home_digest, 'endpoint_sha256': endpoint_digest,
                'verified_at': datetime.now(timezone.utc).isoformat()}

    def _alive(self):
        if self._closed or self.connection is None:
            raise ManagementError('unavailable', 'The original DSH generation is unavailable; reconcile before control.')

    def _guard_request(self, method, params):
        if method not in READ_METHODS | CONTROL_METHODS:
            raise ManagementError('forbidden', 'This adapter permits only the explicit DSH Remote method allowlist.')
        if method in CONTROL_METHODS and not isinstance(params.get('_authority'), dict):
            raise ManagementError('forbidden', 'DSH native mutations require an admitted current task or work grant.')
        if method in CONTROL_METHODS:
            authority = params['_authority']
            thread_id, turn_id = authority.get('thread_id'), authority.get('turn_id')
            repository = self._creation_repository if method == 'session/create' else self._known_repositories.get(thread_id)
            if repository is None or self._control_proof is None:
                raise ManagementError('capability_unverified', 'No admitted DSH task boundary authorizes this mutation.')
            self._verify_boundary(self._control_proof, repository)
            proof = self._control_proof
            initial = method == 'session/prompt' and authority.get('operation') == 'task_start'
            if method == 'session/create' or initial:
                self._validate_startup_lease(proof, repository)
                if initial:
                    owned = self._created_sessions.get(thread_id)
                    if (owned is None or owned.get('initial_pending') is not True or owned.get('verified') is not True
                            or authority.get('creation_id') != thread_id or turn_id is not None
                            or proof['startup_exclusive_input']['evidence'] != owned['startup_evidence']):
                        raise ManagementError('capability_unverified', 'The DSH first input is outside its verified new Session admission.')
            else:
                validate_exclusive_input(proof, self.connection, repository, thread_id, turn_id)
            if method in {'session/prompt', 'session/cancel'} and params.get('request', {}).get('sessionId') != thread_id:
                raise ManagementError('binding_conflict', 'The DSH mutation target differs from its admitted authority.')

    def _call(self, method, params):
        self._guard_request(method, params)
        self.connect()
        rpc_id = str(uuid.uuid4())
        try:
            response = self._transport.request(method, {key: value for key, value in params.items() if key != '_authority'}, rpc_id)
        except Exception:
            raise ManagementError('outcome_unknown' if method in CONTROL_METHODS else 'unavailable',
                                  'The original DSH response is unconfirmed; mutations must not be replayed.') from None
        if (not isinstance(response, dict) or set(response) != {'type', 'rpcId', 'result'}
                or response.get('type') != 'server-response' or response.get('rpcId') != rpc_id
                or not isinstance(response.get('result'), dict) or type(response['result'].get('ok')) is not bool):
            raise ManagementError('outcome_unknown' if method in CONTROL_METHODS else 'capability_unverified',
                                  'The original DSH response correlation is incomplete.')
        result = response['result']
        if result['ok'] is False:
            if not isinstance(result.get('error'), dict) or not isinstance(result['error'].get('code'), str):
                raise ManagementError('capability_unverified', 'The original DSH rejection is malformed.')
            raise ManagementError('service_rejected', 'The original DSH service rejected the requested operation.')
        if method == '$events/result' and set(result) == {'ok'}:
            return None
        if set(result) != {'ok', 'value'}:
            raise ManagementError('outcome_unknown' if method in CONTROL_METHODS else 'capability_unverified',
                                  'The original DSH result value is missing.')
        return result['value']

    def _pages(self, method, params):
        if method != 'session/list':
            raise ManagementError('capability_unverified', 'This DSH endpoint is not a unary list API.')
        value = self._call(method, params)
        if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list):
            raise ManagementError('capability_unverified', 'The original DSH complete Session list is unavailable.')
        return value['items']

    def list_sessions(self, source_kinds=None, archived=None):
        source_kind = getattr(self, 'source_kind', 'desktop')
        if source_kinds not in (None, [source_kind]) or archived is True:
            raise ManagementError('capability_unverified', 'The DSH list does not prove this additional source or archive scope.')
        rows = self._pages('session/list', {'_request': {}})
        if any(not isinstance(row, dict) or not isinstance(row.get('sessionId'), str)
               or not row['sessionId'] or type(row.get('running')) is not bool
               or type(row.get('agentAvailable')) is not bool for row in rows):
            raise ManagementError('capability_unverified', 'The original DSH Session summary is incomplete.')
        if len({row['sessionId'] for row in rows}) != len(rows):
            raise ManagementError('capability_unverified', 'The original DSH list has conflicting identities.')
        return [{**row, 'id': row['sessionId'], 'status': {'type': 'active' if row['running'] else 'idle'},
                 'source': 'registered_backend', 'canAcceptDirectInput': row['agentAvailable'],
                 'runtimeCoverage': 'unknown'} for row in rows]

    def loaded_threads(self):
        return [row['id'] for row in self.list_sessions() if row['agentAvailable']]

    def read_thread(self, thread_id, include_turns=True):
        if not isinstance(thread_id, str) or not thread_id:
            raise ManagementError('invalid_change', 'An explicit DSH Session identity is required.')
        rows = self.list_sessions()
        row = next((row for row in rows if row['id'] == thread_id), None)
        if row is None:
            raise ManagementError('binding_conflict', 'The original DSH Session is not in the current visible catalog.')
        if not include_turns:
            return {**row, 'turns': [], 'historyMode': 'not_loaded'}
        args = {'request': {'address': {'kind': 'session', 'sessionId': thread_id}, 'maxMessages': 500}}
        self._guard_request('session/follow', args)
        stream = None
        try:
            stream = self._transport.stream('session/follow', args)
            snapshot = next(stream)
        except Exception:
            raise ManagementError('unavailable', 'The original DSH history opening was not confirmed.') from None
        finally:
            if stream is not None:
                stream.close()
        if (not isinstance(snapshot, dict) or snapshot.get('type') != 'snapshot'
                or not isinstance(snapshot.get('header'), dict) or snapshot['header'].get('id') != thread_id
                or snapshot['header'].get('cwd') != row.get('cwd')
                or type(snapshot.get('cursor')) is not int or snapshot['cursor'] < -1
                or type(snapshot.get('hasMore')) is not bool or not isinstance(snapshot.get('records'), list)
                or not isinstance(snapshot.get('projections'), dict)):
            raise ManagementError('capability_unverified', 'The original DSH journal identity or coverage is incomplete.')
        records, has_more, pages = list(snapshot['records']), snapshot['hasMore'], 0
        while has_more:
            if pages >= 100 or not records:
                raise ManagementError('capability_unverified', 'The DSH history pagination did not reach a complete bounded prefix.')
            first = self._journal_event(records[0])['seq']
            page = self._call('session/page', {'request': {**args['request'], 'throughSeq': snapshot['cursor'], 'beforeSeq': first}})
            if not isinstance(page, dict) or type(page.get('hasMore')) is not bool or not isinstance(page.get('records'), list) or not page['records']:
                raise ManagementError('capability_unverified', 'The DSH history prefix is missing.')
            earlier = [self._journal_event(record) for record in page['records']]
            if earlier[-1]['seq'] >= first:
                raise ManagementError('capability_unverified', 'The DSH history prefix overlaps or does not progress.')
            records = page['records'] + records
            has_more, pages = page['hasMore'], pages + 1
        events = [self._journal_event(record) for record in records]
        if events and (len(events) != snapshot['cursor'] + 1 or any(event['seq'] != index for index, event in enumerate(events))):
            raise ManagementError('capability_unverified', 'The original DSH journal contains a gap or a conflicting sequence.')
        if not events and snapshot['cursor'] != -1:
            raise ManagementError('capability_unverified', 'The original DSH journal cut has no matching records.')
        turns = fold_turns(events, snapshot['header'].get('cwd'))
        state = 'active' if any(turn['status'] == 'inProgress' for turn in turns) else row['status']['type']
        return {**row, 'status': {'type': state}, 'cwd': snapshot['header'].get('cwd'), 'turns': turns, 'historyMode': 'full',
                'nativeHeader': snapshot['header'], 'nativeProjections': snapshot['projections'],
                'nativeEvents': events, 'journalCursor': snapshot['cursor'], 'runtimeCoverage': 'unknown'}

    @staticmethod
    def _journal_event(record):
        if (not isinstance(record, dict) or set(record) != {'type', 'event'} or record.get('type') != 'event'
                or not isinstance(record.get('event'), dict) or not isinstance(record['event'].get('type'), str)
                or type(record['event'].get('seq')) is not int or record['event']['seq'] < 0
                or 'data' not in record['event']):
            raise ManagementError('capability_unverified', 'A native DSH journal record is malformed.')
        known = {'turn/start', 'turn/end', 'user/message', 'assistant/message', 'tool/call', 'tool/result',
                 'tool/ptc-dispatch-start', 'tool/ptc-dispatch'}
        if record['event']['type'] in known and not isinstance(record['event']['data'], dict):
            raise ManagementError('capability_unverified', 'A known DSH execution record has a malformed producer payload.')
        return record['event']

    def background_terminals(self, thread_id):
        self.connect()
        args = {'request': {'sessionId': thread_id}}
        self._guard_request('job/list', args)
        stream = None
        try:
            stream = self._transport.stream('job/list', args)
            roster = next(stream)
        except Exception:
            raise ManagementError('capability_unverified', 'The DSH job roster could not be verified.') from None
        finally:
            if stream is not None:
                stream.close()
        if (not isinstance(roster, dict) or roster.get('type') != 'rows' or not isinstance(roster.get('jobs'), list)
                or any(not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id']
                       or job.get('owner') not in {None, thread_id}
                       or job.get('status') not in {'running', 'stopping', 'completed', 'killed', 'failed'} for job in roster['jobs'])):
            raise ManagementError('capability_unverified', 'The DSH job roster has incomplete identities or lifecycle status.')
        return roster['jobs']

    def verify_start(self, repository):
        connection = self.connect()
        if not callable(self.verifier):
            raise ManagementError('capability_unverified', 'Current DSH filesystem, tools and input admission evidence is missing.')
        proof = self.verifier(dict(connection), json.loads(json.dumps(repository)))
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths',
                    'task_start', 'manual_execution_coverage')
        self._verify_boundary(proof, repository, required)
        self._validate_startup_lease(proof, repository)
        prior = self._creation_intents.get(repository_fingerprint(repository))
        if prior and prior['status'] in {'intent', 'outcome_unknown'}:
            raise ManagementError('outcome_unknown', 'A prior original DSH creation remains unreconciled; never replay it.')
        self.last_start_occupancy = None
        for row in self.list_sessions():
            if row.get('cwd') == repository['worktree'] and row['running']:
                self.last_start_occupancy = {'thread_id': row['id'], 'cwd': row['cwd'],
                                             'logical_repository': repository['logical_id']}
                raise ManagementError('repository_busy', 'An original DSH Session is still running in this repository.')
        self._control_proof = proof
        self._creation_repository = json.loads(json.dumps(repository))
        return dict(proof)

    def _verify_boundary(self, proof, repository, required=()):
        self._alive()
        if (not isinstance(proof, dict) or proof.get('generation') != self.generation
                or proof.get('service_id') != self.connection['service_id']
                or proof.get('repository_fingerprint') != repository_fingerprint(repository)
                or proof.get('runtime_roots') != [repository['worktree']]
                or any(not isinstance(proof.get(key), str) or not proof[key] for key in required)):
            raise ManagementError('capability_unverified', 'Current DSH execution evidence does not match this generation and exact repository boundary.')

    def start_thread(self, repository, proof):
        with self._rpc_lock:
            self._verify_boundary(proof, repository)
            self._validate_startup_lease(proof, repository)
            if self._control_proof != proof:
                raise ManagementError('capability_unverified', 'The current DSH creation capability has not been admitted.')
            request = {'cwd': repository['worktree'], 'sessionId': 'session-' + str(uuid.uuid4())}
            if isinstance(proof.get('agent_preset'), str):
                request['agentPreset'] = proof['agent_preset']
            fingerprint = repository_fingerprint(repository)
            prior = self._creation_intents.get(fingerprint)
            if prior and prior['status'] in {'intent', 'outcome_unknown'}:
                raise ManagementError('outcome_unknown', 'The original DSH creation intent is unresolved; do not replay it.')
            intent = {'session_id': request['sessionId'], 'status': 'intent'}
            self._creation_intents[fingerprint] = intent
            try:
                result = self._call('session/create', {'request': request, '_authority': {'thread_id': None, 'turn_id': None}})
            except ManagementError:
                intent['status'] = 'outcome_unknown'
                raise
            if not isinstance(result, dict) or not isinstance(result.get('sessionId'), str) or not result['sessionId']:
                intent['status'] = 'outcome_unknown'
                raise ManagementError('outcome_unknown', 'The DSH created Session identity was not confirmed; do not replay creation.')
            intent['status'] = 'registered'
            self._created_sessions[result['sessionId']] = {'requested_id': request['sessionId'],
                'repository_fingerprint': fingerprint, 'startup_evidence': proof['startup_exclusive_input']['evidence'],
                'initial_pending': True, 'verified': False}
            return {'thread': {'id': result['sessionId'], 'cwd': repository['worktree']}, 'nativeReceipt': result}

    def _validate_startup_lease(self, proof, repository):
        exclusive = proof.get('startup_exclusive_input') if isinstance(proof, dict) else None
        if not isinstance(exclusive, dict) or exclusive.get('scope') != 'create_and_first_input':
            raise ManagementError('capability_unverified', 'A dedicated DSH creation and first-input lease is required.')
        validate_exclusive_input({**proof, 'exclusive_input': exclusive}, self.connection, repository, None, None)

    @staticmethod
    def _verify_fresh_session(thread):
        projections = thread.get('nativeProjections', {})
        inbox = projections.get('values', {}).get('inbox')
        if (thread.get('blank') is not True or thread.get('turns') or thread.get('status', {}).get('type') != 'idle'
                or projections.get('asOfSeq') != thread.get('journalCursor')
                or not isinstance(inbox, dict) or inbox.get('next-turn') != [] or inbox.get('next-step') != []
                or any(event['type'] in {'user/message', 'assistant/message', 'tool/call', 'tool/result', 'tool/ptc-dispatch-start'}
                       for event in thread.get('nativeEvents', []))):
            raise ManagementError('binding_conflict', 'The exact newly-created DSH Session is not blank with an empty native inbox.')

    def bind_repository(self, thread_id, repository, proof=None):
        if proof is not None:
            self._verify_boundary(proof, repository)
        self._known_repositories[thread_id] = json.loads(json.dumps(repository))
        if proof is not None:
            self._control_proof = proof

    def verify_thread(self, response, repository, proof):
        thread_id = response.get('thread', {}).get('id')
        if not isinstance(thread_id, str) or not thread_id:
            raise ManagementError('capability_unverified', 'The new DSH Session has no registered identity.')
        self._verify_boundary(proof, repository)
        owned = self._created_sessions.get(thread_id)
        if (not owned or owned['requested_id'] != thread_id
                or owned['repository_fingerprint'] != repository_fingerprint(repository)):
            raise ManagementError('binding_conflict', 'The DSH creation response did not confirm the exact client-created Session identity.')
        thread = self.read_thread(thread_id)
        if thread.get('cwd') != repository['worktree'] or thread.get('canAcceptDirectInput') is not True:
            raise ManagementError('capability_unverified', 'The created DSH workspace does not match its admitted repository.')
        self._verify_fresh_session(thread)
        self.bind_repository(thread_id, repository, proof)
        owned['verified'] = True

    def verify_control(self, repository, action, expected_capability):
        self._alive()
        if not callable(self.verifier):
            raise ManagementError('capability_unverified', 'The original DSH task control evidence is unavailable.')
        proof = self.verifier(dict(self.connection), json.loads(json.dumps(repository)))
        self._verify_boundary(proof, repository)
        controls = proof.get('task_control', {})
        if (not isinstance(controls, dict) or not isinstance(controls.get(action), str) or not controls[action]
                or any(proof.get(key) != expected_capability.get(key) for key in ('permission_profile', 'policy_digest', 'runtime_roots'))):
            raise ManagementError('capability_unverified', 'This original DSH control lacks current action and unchanged boundary evidence.')
        self._control_proof = proof
        return proof

    def _authorize_input(self, thread_id, turn_id, action):
        repository = self._known_repositories.get(thread_id)
        if repository is None or self._control_proof is None:
            raise ManagementError('capability_unverified', 'This DSH Session has no admitted repository control boundary.')
        if action == 'task_start':
            owned = self._created_sessions.get(thread_id)
            if not owned or owned.get('verified') is not True or owned.get('initial_pending') is not True or turn_id is not None:
                raise ManagementError('capability_unverified', 'No exact new DSH Session owns this first-input admission.')
            proof = self.verifier(dict(self.connection), json.loads(json.dumps(repository))) if callable(self.verifier) else None
            self._verify_boundary(proof, repository, ('task_start', 'platform_enforcement', 'tool_paths', 'manual_execution_coverage'))
            self._validate_startup_lease(proof, repository)
            if (any(proof.get(key) != self._control_proof.get(key) for key in ('permission_profile', 'policy_digest', 'runtime_roots'))
                    or proof['startup_exclusive_input']['evidence'] != owned['startup_evidence']):
                raise ManagementError('capability_unverified', 'The DSH first-input lease or immutable repository boundary changed.')
            self._verify_fresh_session(self.read_thread(thread_id))
            self._control_proof = proof
            return {'thread_id': thread_id, 'turn_id': None, 'operation': 'task_start', 'creation_id': thread_id}
        proof = self.verify_control(repository, action, self._control_proof)
        validate_exclusive_input(proof, self.connection, repository, thread_id, turn_id)
        return {'thread_id': thread_id, 'turn_id': turn_id}

    def _prompt(self, thread_id, text, request_id, *, mode, expected_turn_id):
        with self._rpc_lock:
            if thread_id in self._pending_inputs:
                raise ManagementError('outcome_unknown', 'An original DSH submission remains unreconciled; it must not be replayed.')
            if not isinstance(text, str) or not text.strip() or not isinstance(request_id, str) or not request_id:
                raise ManagementError('invalid_change', 'A bounded DSH prompt and stable submission identity are required.')
            action = 'task_start' if mode == 'queue' and expected_turn_id is None else 'append' if mode == 'steer' else 'continue'
            authority = self._authorize_input(thread_id, expected_turn_id, action)
            self._pending_inputs[thread_id] = {'request_id': request_id, 'status': 'intent'}
            result = self._call('session/prompt', {'request': {'requestId': request_id, 'sessionId': thread_id,
                                       'mode': mode, 'content': [{'type': 'text', 'text': text}]}, '_authority': authority})
            if not isinstance(result, dict) or set(result) != {'accepted'} or result.get('accepted') is not True:
                raise ManagementError('outcome_unknown', 'The original DSH prompt acceptance is unconfirmed; do not replay.')
            self._pending_inputs[thread_id]['status'] = 'accepted'
            if action == 'task_start':
                self._created_sessions[thread_id]['initial_pending'] = False
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                thread = self.read_thread(thread_id)
                matching = [turn for turn in thread['turns'] if any(item.get('requestId') == request_id for item in turn['items'])]
                if len(matching) == 1:
                    turn = matching[0]
                    if mode == 'steer' and turn['id'] != expected_turn_id:
                        raise ManagementError('outcome_unknown', 'The DSH steer was admitted outside the expected original turn.')
                    self._pending_inputs.pop(thread_id)
                    return {'turn': turn, 'turnId': turn['id'], 'requestId': request_id, 'accepted': True}
                if len(matching) > 1:
                    raise ManagementError('binding_conflict', 'The DSH submission correlation appears in multiple turns.')
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            raise ManagementError('outcome_unknown', 'The DSH inbox accepted input, but no correlated durable started turn was confirmed; do not replay.')

    def start_turn(self, thread_id, prompt):
        return self._prompt(thread_id, prompt, str(uuid.uuid4()), mode='queue', expected_turn_id=None)

    def steer_turn(self, thread_id, turn_id, text, instruction_id):
        return self._prompt(thread_id, text, instruction_id, mode='steer', expected_turn_id=turn_id)

    @staticmethod
    def verify_idle(thread, previous_turn_id, known_turn_ids):
        turns = thread.get('turns', [])
        previous = next((turn for turn in turns if turn.get('id') == previous_turn_id), None)
        if (thread.get('status', {}).get('type') != 'idle' or not previous
                or previous.get('status') not in {'completed', 'failed', 'interrupted'}
                or any(turn.get('id') not in known_turn_ids or turn.get('status') == 'inProgress' for turn in turns)):
            raise ManagementError('binding_conflict', 'The original DSH Session is not idle at the exact known previous turn.')

    def start_idle_turn(self, thread_id, previous_turn_id, text, instruction_id, *, expected_cwd, known_turn_ids):
        with self._rpc_lock:
            thread = self.read_thread(thread_id)
            if thread.get('cwd') != expected_cwd or thread.get('canAcceptDirectInput') is not True:
                raise ManagementError('binding_conflict', 'The original DSH idle input target changed.')
            self.verify_idle(thread, previous_turn_id, known_turn_ids)
            return self._prompt(thread_id, text, instruction_id, mode='queue', expected_turn_id=previous_turn_id)

    def interrupt_turn(self, thread_id, turn_id):
        with self._rpc_lock:
            authority = self._authorize_input(thread_id, turn_id, 'stop')
            result = self._call('session/cancel', {'request': {'sessionId': thread_id}, '_authority': authority})
            if not isinstance(result, dict) or set(result) != {'accepted'} or result.get('accepted') is not True:
                raise ManagementError('outcome_unknown', 'The DSH cancel admission was not confirmed; reconcile before further control.')
            # Admission is not process exit or the end of the exact targeted turn.
            return {'accepted': True}

    def verify_process_coverage(self, session, stop):
        raise ManagementError('capability_unverified', 'DSH related jobs, subagents and child-process exit coverage remain unverified.')

    def server_requests(self, thread_id):
        with self._condition:
            candidates = [request for request in self._server_requests.values()
                          if request['envelope'].get('params', {}).get('threadId') == thread_id]
            for request in candidates:
                params = request['envelope']['params']
                params.update(turnId=None, isBlocking=None, native_response_available=False, notice_current=False)
            if self._closed:
                for request in candidates:
                    if request['envelope']['params'].get('notification_only') is True:
                        request['state'] = 'expired'
                return [json.loads(json.dumps(request)) for request in candidates
                        if request['envelope']['params'].get('notice_validated') is True]
        if candidates:
            thread = self.read_thread(thread_id)
            events = thread.get('nativeEvents', [])
            with self._condition:
                for request in candidates:
                    envelope, params = request['envelope'], request['envelope']['params']
                    frame = params['native_frame']
                    native_request = frame['request']
                    if params.get('notification_only') is True and envelope['method'] == 'approval/request':
                        active = [turn for turn in thread['turns'] if turn['status'] == 'inProgress']
                        turn = active[0] if len(active) == 1 else None
                        authorized = (turn is not None and callable(self.waterfall_authority)
                            and self.waterfall_authority(thread_id, turn['id']) is True)
                        if not authorized:
                            request['state'] = 'expired'
                            continue
                        params.update(turnId=turn['id'], isBlocking=True)
                        tool = next((item for item in turn['items'] if item.get('id') == native_request.get('callId')), None)
                        if tool is not None and tool.get('nativeResultSeq') is not None:
                            request['state'] = 'resolved'
                            continue
                        status = approval_notice_status(events, native_request, turn, params.get('approvalId'))
                        if request['state'] in {'notice_candidate', 'notice'}:
                            request['state'] = ('notice' if status['state'] == 'pending' else 'resolved'
                                                if status['state'] == 'resolved' else 'notice_unverified')
                            params['notice_validated'] = True
                            if status.get('approval_id'):
                                params['approvalId'] = status['approval_id']
                            params['notice_uncertainty'] = status.get('reason')
                        params['notice_current'] = request['state'] == 'notice'
                        continue
                    wait = native_request.get('wait')
                    call_id = wait.get('callId') if isinstance(wait, dict) else native_request.get('callId')
                    matching = [event for event in events if event['type'] == 'tool/call'
                                and event['data'].get('callId') == call_id
                                and (envelope['method'] != 'user-questions/request' or event['data'].get('name') == 'ask_user_question')]
                    if not call_id or len(matching) != 1:
                        self._next_waterfall(frame['eventId'])
                        if params.get('notification_only') is True:
                            request['state'] = 'expired'
                        continue
                    index = matching[0]['data'].get('turn')
                    turn = next((turn for turn in thread['turns'] if turn['nativeTurn'] == index), None)
                    if not turn:
                        self._next_waterfall(frame['eventId'])
                        if params.get('notification_only') is True:
                            request['state'] = 'expired'
                        continue
                    params['turnId'] = turn['id']
                    params['isBlocking'] = True
                    tool = next((item for item in turn['items'] if item.get('id') == call_id), None)
                    settled = tool is not None and tool.get('nativeResultSeq') is not None
                    if params.get('notification_only') is True:
                        authorized = (callable(self.waterfall_authority)
                            and self.waterfall_authority(thread_id, turn['id']) is True)
                        if request['state'] in {'notice_candidate', 'notice'}:
                            if settled:
                                request['state'] = 'resolved'
                            elif (not authorized or turn['status'] != 'inProgress' or tool is None
                                  or envelope['method'] == 'approval/request'
                                  and native_request.get('toolName') != matching[0]['data'].get('name')):
                                request['state'] = 'expired'
                            else:
                                request['state'] = 'notice'
                                params['notice_validated'] = True
                        params['notice_current'] = request['state'] == 'notice'
                        continue
                    if settled and request['state'] == 'pending':
                        request['state'] = 'resolved'
                    work_authorized = callable(self.waterfall_authority) and self.waterfall_authority(thread_id, turn['id']) is True
                    authorized = work_authorized
                    if authorized:
                        try:
                            authorized = (callable(self.waterfall_question_supported)
                                and self.waterfall_question_supported({**envelope,
                                    'params': {**params, 'native_response_available': True}}) is True)
                            if authorized:
                                authority = self._authorize_input(thread_id, turn['id'], 'human_response')
                                self._guard_request('$events/result', {'_authority': authority})
                        except ManagementError:
                            authorized = False
                    if settled or not authorized:
                        self._next_waterfall(frame['eventId'])
                    if params.get('notification_only') is True:
                        params['notice_current'] = work_authorized and not settled and turn['status'] == 'inProgress'
                        if not params['notice_current'] and request['state'] == 'notice':
                            request['state'] = 'expired'
                    params['native_response_available'] = (envelope['method'] == 'user-questions/request'
                        and not (isinstance(wait, dict) and wait.get('timed') is True)
                        and authorized and not settled and turn['status'] == 'inProgress' and request['state'] == 'pending')
        with self._condition:
            return [json.loads(json.dumps(request)) for request in self._server_requests.values()
                    if request['envelope'].get('params', {}).get('threadId') == thread_id
                    and (request['envelope']['params'].get('notification_only') is not True
                         or request['envelope']['params'].get('notice_validated') is True)]

    def respond_server_request(self, rpc_id, envelope, result):
        with self._rpc_lock:
            self._alive()
            current = self._server_requests.get((type(rpc_id), rpc_id))
            if not current or current['state'] != 'pending' or current['envelope'] != envelope:
                raise ManagementError('binding_conflict', 'The original DSH question is no longer pending on this generation.')
            params = envelope['params']
            if not callable(self.waterfall_authority) or self.waterfall_authority(params.get('threadId'), params.get('turnId')) is not True:
                self._next_waterfall(rpc_id)
                raise ManagementError('forbidden', 'This original DSH work no longer authorizes a question answer.')
            if not callable(self.waterfall_question_supported) or self.waterfall_question_supported(envelope) is not True:
                self._next_waterfall(rpc_id)
                raise ManagementError('forbidden', 'This original DSH question is outside supported answer scope.')
            if envelope['method'] != 'user-questions/request' or params.get('native_response_available') is not True:
                raise ManagementError('capability_unverified', 'This original DSH question or approval has no verified answer lifetime.')
            latest = next((request for request in self.server_requests(params['threadId'])
                           if request['envelope']['id'] == rpc_id), None)
            if latest is None or latest['envelope'] != envelope or latest['envelope']['params'].get('native_response_available') is not True:
                raise ManagementError('binding_conflict', 'The original DSH tool question changed before answering.')
            questions = params.get('questions', [])
            answers = result.get('answers') if isinstance(result, dict) else None
            if (not isinstance(answers, list) or not isinstance(result, dict) or set(result) != {'answers'}
                    or any(not isinstance(answer, dict) or not isinstance(answer.get('id'), str)
                           or not isinstance(answer.get('selected'), list)
                           or any(not isinstance(value, str) for value in answer['selected'])
                           or set(answer) - {'id', 'selected', 'custom'}
                           or 'custom' in answer and not isinstance(answer['custom'], str) for answer in answers)
                    or len({answer['id'] for answer in answers}) != len(answers)
                    or {answer['id'] for answer in answers} != {question['id'] for question in questions}):
                raise ManagementError('invalid_change', 'DSH answers must name the exact original question batch.')
            authority = self._authorize_input(params['threadId'], params['turnId'], 'human_response')
            with self._condition:
                if current['state'] != 'pending':
                    raise ManagementError('binding_conflict', 'The original DSH question resolved before its answer intent.')
                current['state'] = 'intent'
                delivery = self._deliveries.get(rpc_id)
                if delivery:
                    delivery['state'] = 'answer_intent'
            try:
                self._call('$events/result', {'clientId': self.connection['client_id'], 'eventId': rpc_id,
                                              'outcome': {'kind': 'result', 'value': result}, '_authority': authority})
            except ManagementError as error:
                with self._condition:
                    current['state'] = 'resolved' if error.code == 'service_rejected' else 'outcome_unknown'
                raise
            with self._condition:
                if current['state'] == 'intent':
                    current['state'] = 'sent'

    def take_events(self, thread_id):
        with self._condition:
            matching = [event for event in self._events if event.get('params', {}).get('threadId') == thread_id]
            self._events = [event for event in self._events if event not in matching]
            return matching

    def close(self):
        with self._lifecycle_lock:
            self._closed = True
            reader = self._reader
        for event_id in list(self._deliveries):
            try:
                self._next_waterfall(event_id)
            except Exception:
                pass
        self._transport.close()
        if reader and reader is not threading.current_thread():
            reader.join(timeout=2)
        with self._condition:
            for request in self._server_requests.values():
                if request['state'] == 'pending':
                    request['state'] = 'connection_ended'
            self._condition.notify_all()

    def _read_events(self):
        try:
            for frame in self._event_stream:
                with self._lifecycle_lock:
                    if self._closed:
                        break
                self._accept_remote_event(frame)
                with self._condition:
                    self._condition.notify_all()
        except Exception:
            pass
        finally:
            with self._lifecycle_lock:
                self._closed = True
            if self._event_stream is not None:
                self._event_stream.close()
            with self._condition:
                self._condition.notify_all()

    def _accept_remote_event(self, frame):
        if not isinstance(frame, dict):
            raise ValueError('Malformed DSH event.')
        if frame.get('type') == 'cancel' and set(frame) == {'type', 'eventId'}:
            current = self._server_requests.get((str, frame['eventId']))
            if current:
                current['state'] = 'resolved'
            delivery = self._deliveries.get(frame['eventId'])
            if delivery:
                delivery['state'] = 'resolved'
            return
        if frame.get('type') == 'emit' and set(frame) == {'type', 'event', 'args'} and isinstance(frame['args'], list):
            return
        if (frame.get('type') != 'waterfall' or set(frame) != {'type', 'event', 'eventId', 'agentId', 'request'}
                or any(not isinstance(frame.get(key), str) or not frame[key] for key in ('event', 'eventId', 'agentId'))
                or not isinstance(frame.get('request'), dict)):
            raise ValueError('Malformed original DSH Remote waterfall.')
        digest = hashlib.sha256(json.dumps(frame, sort_keys=True).encode()).hexdigest()
        with self._condition:
            existing_delivery = self._deliveries.get(frame['eventId'])
            if existing_delivery:
                if existing_delivery['digest'] != digest:
                    raise ValueError('Conflicting original DSH delivery identity.')
                return
            delivery = {'digest': digest, 'state': 'pending', 'client_id': self.connection['client_id'],
                        'generation': self.generation}
            self._deliveries[frame['eventId']] = delivery
            if len(self._deliveries) > 10000:
                raise ValueError('DSH delivery bound exceeded.')
        if not self._hold_waterfall(frame):
            self._register_notice_candidate(frame)
            self._next_waterfall(frame['eventId'])
            return
        request = frame['request']
        questions = request.get('questions', [])
        if frame['event'] == 'user-questions/request' and (not isinstance(questions, list) or len(questions) > 100):
            raise ValueError('Malformed original DSH question.')
        wait = request.get('wait')
        envelope = {'id': frame['eventId'], 'method': frame['event'],
                    'params': {'threadId': frame['agentId'], 'turnId': None,
                               'itemId': wait.get('callId') if isinstance(wait, dict) else request.get('callId'),
                               'questions': questions, 'native_response_available': False,
                               'response_codec': 'dsh_remote_waterfall', 'native_frame': frame}}
        existing = self._server_requests.get((str, frame['eventId']))
        if existing and existing['envelope']['params']['native_frame'] != frame:
            raise ValueError('Conflicting original DSH event identity.')
        if existing is None:
            self._server_requests[(str, frame['eventId'])] = {'envelope': envelope, 'state': 'pending'}
            self._events.append(envelope)
        if len(self._server_requests) > 10000:
            raise ValueError('DSH pending-event bound exceeded.')

    def _hold_waterfall(self, frame):
        request = frame['request']
        wait = request.get('wait')
        repository = self._known_repositories.get(frame['agentId'])
        if (frame['event'] != 'user-questions/request' or repository is None or self._control_proof is None
                or not isinstance(wait, dict) or wait.get('timed') is True or not isinstance(wait.get('callId'), str)
                or not isinstance(request.get('questions'), list)):
            return False
        try:
            thread = self.read_thread(frame['agentId'])
            matching = [event for event in thread['nativeEvents'] if event['type'] == 'tool/call'
                        and event['data'].get('callId') == wait['callId'] and event['data'].get('name') == 'ask_user_question']
            if len(matching) != 1:
                return False
            turn = next((turn for turn in thread['turns'] if turn['nativeTurn'] == matching[0]['data'].get('turn')), None)
            if turn is None or turn['status'] != 'inProgress':
                return False
            if not callable(self.waterfall_authority) or self.waterfall_authority(frame['agentId'], turn['id']) is not True:
                return False
            tool = next((item for item in turn['items'] if item.get('id') == wait['callId']), None)
            if tool is None or tool.get('nativeResultSeq') is not None:
                return False
            envelope = {'id': frame['eventId'], 'method': frame['event'], 'params': {
                'threadId': frame['agentId'], 'turnId': turn['id'], 'itemId': wait['callId'],
                'questions': request['questions'], 'isBlocking': True, 'native_response_available': True,
                'response_codec': 'dsh_remote_waterfall', 'native_frame': frame}}
            if not callable(self.waterfall_question_supported) or self.waterfall_question_supported(envelope) is not True:
                return False
            authority = self._authorize_input(frame['agentId'], turn['id'], 'human_response')
            self._guard_request('$events/result', {'_authority': authority})
            return (self.waterfall_authority(frame['agentId'], turn['id']) is True
                    and self.waterfall_question_supported(envelope) is True)
        except ManagementError:
            return False

    def _register_notice_candidate(self, frame):
        if frame['event'] not in {'approval/request', 'user-questions/request'} or frame['agentId'] not in self._known_repositories:
            return
        request = frame['request']
        wait = request.get('wait')
        call_id = wait.get('callId') if isinstance(wait, dict) else request.get('callId')
        if frame['event'] != 'approval/request' and (not isinstance(call_id, str) or not call_id):
            return
        if frame['event'] == 'approval/request' and not isinstance(request.get('toolName'), str):
            return
        envelope = {'id': frame['eventId'], 'method': frame['event'], 'params': {
            'threadId': frame['agentId'], 'turnId': None, 'itemId': call_id, 'questions': request.get('questions', []),
            'native_response_available': False, 'notification_only': True, 'notice_current': False,
            'response_codec': 'dsh_remote_waterfall', 'native_frame': frame}}
        with self._condition:
            self._server_requests[(str, frame['eventId'])] = {'envelope': envelope, 'state': 'notice_candidate'}

    def _next_waterfall(self, event_id):
        """Retire only this transport delivery; never answer or approve a request."""
        with self._condition:
            delivery = self._deliveries.get(event_id)
            if (not delivery or delivery['state'] != 'pending' or delivery['client_id'] != self.connection['client_id']
                    or delivery['generation'] != self.generation):
                return
            delivery['state'] = 'next_intent'
            current = self._server_requests.get((str, event_id))
            if current:
                current['envelope']['params']['native_response_available'] = False
                if current['state'] == 'pending':
                    current['state'] = 'notice'
                    current['envelope']['params'].update(notification_only=True, notice_current=False, notice_validated=True)
        rpc_id = str(uuid.uuid4())
        response = self._transport.request('$events/result', {'clientId': delivery['client_id'],
                    'eventId': event_id, 'outcome': {'kind': 'next'}}, rpc_id)
        if (not isinstance(response, dict) or response.get('type') != 'server-response' or response.get('rpcId') != rpc_id
                or not isinstance(response.get('result'), dict) or response['result'].get('ok') is not True
                or set(response['result']) - {'ok', 'value'} or response['result'].get('value') is not None):
            delivery['state'] = 'outcome_unknown'
            raise ValueError('The original DSH delivery transfer was not confirmed.')
        delivery['state'] = 'delegated'

    def retire_waterfalls(self, thread_id=None):
        for event_id, request in list(self._server_requests.items()):
            if thread_id is None or request['envelope']['params']['threadId'] == thread_id:
                self._next_waterfall(event_id[1])
                request['envelope']['params']['native_response_available'] = False
                if request['state'] == 'pending':
                    request['state'] = 'delegated'


def configured_adapter(config, state_dir):
    """Only protected native DSH configuration and fresh hashed host receipts."""
    if not config:
        return None
    if not isinstance(config, dict):
        raise ManagementError('invalid_change', 'An explicit owned native configuration is required.')
    mode = config.get('mode', 'owned_native')
    if mode == 'owned_native':
        from .dsh_owned import DshOwnedAdapter
        required = {'dsh_home', 'workspace', 'runtime_package_root', 'service_ref'}
        allowed = required | {'mode', 'node_bin', 'timeout'}
        if not required <= set(config) or set(config) - allowed:
            raise ManagementError('invalid_change', 'Owned native DSH requires an independent home, workspace and original runtime; no remote authentication is implicit.')
        return DshOwnedAdapter(**{key: value for key, value in config.items() if key != 'mode'})
    if mode != 'remote':
        raise ManagementError('invalid_change', 'Select owned_native or an explicitly registered native remote connection.')
    config = {key: value for key, value in config.items() if key != 'mode'}
    from .observation import resolved_configuration
    frozen, runtime = resolved_configuration(config)
    evidence_path = Path(state_dir) / 'dsh-validation.json'

    def verifier(connection, repository):
        try:
            base = Path(state_dir).resolve()
            if evidence_path != evidence_path.resolve() or not evidence_path.is_relative_to(base) or evidence_path.stat().st_size > 65536:
                raise ValueError('Invalid DSH evidence manifest.')
            evidence = json.loads(evidence_path.read_text())
            from .validation import validate_receipts
            return validate_receipts(evidence, connection, repository, frozen, state_dir)
        except (OSError, KeyError, TypeError, ValueError):
            raise ManagementError('capability_unverified', 'Current DSH platform, tool and admission receipts are missing; execution remains disabled.') from None

    adapter = DshRemoteAdapter(runtime['base_url'], cookie=runtime.get('cookie', ''), service_ref=runtime['service_ref'],
                               verifier=verifier, expected_home=runtime.get('expected_home'))
    adapter.source_kind, adapter.endpoint_ref = runtime['source_kind'], runtime['endpoint_ref']
    return adapter
