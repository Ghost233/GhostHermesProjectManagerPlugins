"""Reconcile durable work against its original executor before any new input."""
from datetime import datetime, timezone
import hashlib
import json

from .codex import CodexStdioAdapter, repository_fingerprint
from .manager import ManagementError, _repository
from .observation import READ_METHODS
from .takeover import OriginalControlAdapter, CONTROL_METHODS


def _now():
    return datetime.now(timezone.utc).isoformat()


class OriginalRecoveryAdapter(OriginalControlAdapter):
    """A separate, host-verified proxy of an existing executor; never a new server."""
    def _proof(self, repository, context):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'service_id': context['service_id'],
                   'endpoint_ref': self.endpoint_ref, 'source_kind': self.source_kind, 'transport': 'original_proxy_stdio'}
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
            return CodexStdioAdapter._write(self, envelope)
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
        if params.get('threadId') != authority['thread_id'] or method in {'turn/steer', 'turn/interrupt'} and params.get('expectedTurnId', params.get('turnId')) != authority['turn_id'] or method is None and params.get('turnId') != authority['turn_id']:
            raise ManagementError('binding_conflict', 'Recovered input must identify the same original task and current turn.')
        return CodexStdioAdapter._write(self, envelope)

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


def bind_recovery(manager, record, adapter):
    adapter.origin_proof = record['session']['capability']
    request_id = record['id']
    def authority():
        _, data = manager._load()
        current = data['requests'][request_id]
        session = current['session']
        from .takeover import _assignment
        try:
            _assignment(current, data)
            active = session.get('control') == 'assigned_task' and current.get('task_delivery') != 'delivered'
            if session.get('origin') == 'manual_takeover':
                active = active and data.get('control_grants', {}).get(current.get('control_grant_id'), {}).get('status') == 'active'
        except ManagementError:
            active = False
        return {'id': request_id, 'status': 'active' if active else 'observe_only', 'thread_id': session['thread_id'], 'turn_id': session['turn_id']}
    adapter.authority = authority
    return adapter


def _report(manager, identity, request_id, recovery):
    with manager._lock, manager._db:
        version, data = manager._load()
        record = data['requests'][request_id]
        record['recovery'] = recovery
        manager._save(version, data)
    if recovery.get('report_key') != record.get('recovery_report_key'):
        text = '重启／重连对账：' + recovery['status'] + '\n最后已确认执行：' + str(recovery.get('last_confirmed_execution', '待核对')) + '\n核实时间：' + str(recovery.get('last_confirmed_at') or '待核对')
        if recovery.get('needs_human'):
            text += '\n需本人处理：' + '；'.join(recovery['needs_human'])
        manager.publish_request_message(identity, request_id, 'progress', text)
        with manager._lock, manager._db:
            version, data = manager._load()
            data['requests'][request_id]['recovery_report_key'] = recovery['report_key']
            manager._save(version, data)
    return next(r for r in manager.read_snapshot(identity)['requests'] if r['id'] == request_id)


def reconcile_task(manager, identity, request_id):
    from .execution import _responsible
    from .takeover import _assignment, executor_for
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        session = record.get('session')
        recovery = {'status': 'blocked', 'checked_at': _now(), 'last_confirmed_execution': record.get('last_confirmed_execution', record.get('execution')),
            'last_confirmed_at': record.get('last_execution_verified_at'), 'needs_human': []}
        _assignment(record, data)
        if session:
            repository = session['repository']
            actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
            if repository_fingerprint(actual) != session['capability']['repository_fingerprint']:
                raise ManagementError('binding_conflict', 'The original repository boundary changed; no recovery write or dispatch is permitted.')
        try:
            if not session or not session.get('thread_id') or not session.get('turn_id'):
                raise ManagementError('outcome_unknown', 'Original startup identity is incomplete; inspect the original interface without replaying start.')
            if record.get('repository_released'):
                recovery['status'] = 'explicit_stop_preserved' if record.get('outer_task_status') == 'stopped' else 'completed_work_preserved'
            else:
                adapter = executor_for(manager, record)
                if adapter is None or adapter._closed or adapter.generation != session['generation']:
                    adapter = manager.recovery_adapters.get(session['service_ref'])
                    if adapter is None or adapter._closed or adapter.service_ref != session['service_ref']:
                        raise ManagementError('capability_unverified', 'The original executor has no verified reconnect path; inspect its original interface.')
                    context = {'request_id': request_id, 'service_id': session['service_id'], 'previous_generation': session['generation'],
                        'thread_id': session['thread_id'], 'turn_id': session['turn_id'], 'control': session['control'],
                        'accepted_scope_digest': hashlib.sha256(json.dumps(record['accepted_scope'], sort_keys=True).encode()).hexdigest()}
                    proof = adapter.verify_recovery(repository, context)
                    if any(proof.get(k) != session['capability'].get(k) for k in ('permission_profile', 'policy_digest', 'runtime_roots')):
                        raise ManagementError('binding_conflict', 'Recovery proof differs from the original immutable write boundary.')
                    thread = adapter.read_thread(session['thread_id'])
                    if thread.get('id') != session['thread_id'] or thread.get('cwd') != repository['worktree'] or not any(t.get('id') == session['turn_id'] for t in thread.get('turns', [])):
                        raise ManagementError('binding_conflict', 'The actual original thread and turn could not be identified.')
                    record.setdefault('connection_history', []).append({k: session.get(k) for k in ('service_ref', 'service_id', 'generation', 'endpoint_ref')})
                    session.pop('pid', None)
                    session.update(generation=adapter.generation, capability=proof, recovery_ref=session['service_ref'],
                        endpoint_ref=adapter.endpoint_ref, source_kind=adapter.source_kind, transport='original_proxy_stdio')
                    bind_recovery(manager, record, adapter)
                    record['execution_capability'] = {'status': 'verified', 'enabled': session['control'] == 'assigned_task', 'connection': adapter.connection, 'proof': proof}
                    with manager._db:
                        manager._save(version, data)
                task = manager.refresh_task(identity, request_id)
                recovery.update(status='explicit_stop_preserved' if task.get('outer_task_status') == 'stopped' else 'monitoring_restored' if task['execution'] in {'running', 'waiting_approval', 'waiting_input', 'related_execution'} else 'awaiting_reconciliation',
                    last_confirmed_execution=task.get('execution'), last_confirmed_at=task.get('last_execution_verified_at'))
                if task['execution'] == 'turn_ended':
                    recovery['status'], reason = _continue_if_safe(manager, identity, request_id)
                    if reason:
                        recovery['needs_human'].append(reason)
                if any(q.get('resolution') in {'unverified', 'outcome_unknown'} for q in task.get('human_requests', [])):
                    recovery['needs_human'].append('Resolve unverified historical human requests in the original interface; old answers and approvals are not reused.')
                if task['execution'] in {'stopping', 'unverified'}:
                    recovery['needs_human'].append(task.get('unexecuted_reason') or 'Original execution remains unverified; repository occupancy is retained.')
        except ManagementError as exc:
            if exc.code == 'binding_conflict':
                raise
            recovery.update(status='blocked', code=exc.code, needs_human=[str(exc)])
        recovery['report_key'] = json.dumps([recovery['status'], recovery['last_confirmed_execution'], recovery['needs_human']], sort_keys=True)
        return _report(manager, identity, request_id, recovery)


def _continue_if_safe(manager, identity, request_id):
    from .control import terminal_evidence, _binding, _thread
    version, data = manager._load()
    record = data['requests'][request_id]
    session = record['session']
    explicit_stop = record.get('stop', {}).get('turn_id') == session['turn_id']
    profile = data['profiles'][record['profile_id']]
    project = data['projects'][record['project_id']]
    inactive = {'archiving', 'archived', 'disabled', 'deactivating', 'maintenance', 'restoring'}
    if explicit_stop or profile.get('lifecycle') in inactive or project.get('lifecycle') in inactive or profile.get('archive_intent') or project.get('archive_intent') or data.get('maintenance_mode'):
        return 'explicit_intent_preserved', 'Explicit stop, archive or maintenance intent requires a new human arrangement.'
    if session.get('control') != 'assigned_task' or record.get('task_delivery') == 'delivered':
        return 'observe_only_preserved', 'Use the original interface; recovered observation does not renew returned or expired control.'
    if any(c.get('phase') in {'rpc_intent', 'outcome_unknown'} for c in record.get('controls', [])) or session.get('start_phase') not in {None, 'turn_registered'} or any(q.get('reply', {}).get('sent') in {'intent', 'outcome_unknown'} for q in record.get('human_requests', []) if q.get('reply')):
        return 'outcome_unknown_preserved', 'An original start, append or answer outcome is unknown; inspect the original interface without replay.'
    _, session, adapter = _binding(manager, identity, request_id, data, 'related_execution')
    thread = _thread(adapter, session, require_input=False)
    turn = next((t for t in thread['turns'] if t.get('id') == session['turn_id']), None)
    incomplete = adapter.verify_control(session['repository'], 'related_execution', session['capability']).get('work_incomplete', {})
    if not turn or turn.get('status') not in {'interrupted', 'failed'} or incomplete.get('thread_id') != session['thread_id'] or incomplete.get('turn_id') != session['turn_id'] or incomplete.get('status') != 'verified_incomplete' or not isinstance(incomplete.get('evidence'), str) or not incomplete['evidence']:
        return 'awaiting_reconciliation', 'Task stop and unfinished accepted work need fresh evidence; a historical turn end is insufficient.'
    related, evidence = terminal_evidence(adapter, session, thread, session['turn_id'])
    if related:
        return 'awaiting_reconciliation', 'Related execution remains active or unverified; repository occupancy is retained.'
    record['recovery_terminal_evidence'] = evidence
    with manager._db:
        manager._save(version, data)
    instruction_id = 'recovery:' + hashlib.sha256(json.dumps([session['service_id'], session['thread_id'], session['turn_id']]).encode()).hexdigest()
    manager.control_task(identity, request_id, 'append', instruction_id,
        'Continue only the unfinished original accepted task after verified execution stop.\n' + record['accepted_scope']['title'] + '\n' + record['accepted_scope']['body'], session['turn_id'])
    return 'continued_original_task', None
