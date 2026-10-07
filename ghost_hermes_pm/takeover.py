"""One owner-authorized current-work grant on the verified original manual executor."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .codex import CodexStdioAdapter, repository_fingerprint
from .manager import ManagementError, _repository
from .observation import READ_METHODS, SOURCE_KINDS

CONTROL_METHODS = frozenset({'turn/steer', 'turn/start', 'turn/interrupt'})


def _now():
    return datetime.now(timezone.utc).isoformat()


class OriginalControlAdapter(CodexStdioAdapter):
    """Independent original proxy. The readonly adapter is never promoted."""
    def __init__(self, command, *, cwd, env, service_ref, source_kind, endpoint_ref, verifier=None, timeout=10):
        super().__init__(command, cwd=cwd, env=env, service_ref=service_ref, timeout=timeout)
        if source_kind not in SOURCE_KINDS or not isinstance(endpoint_ref, str) or not endpoint_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit original endpoint and individual supported source kind are required.')
        self.source_kind, self.endpoint_ref, self.control_verifier = source_kind, endpoint_ref, verifier
        self.origin_proof, self.authority = None, None

    def _proof(self, repository, context):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'service_id': context['original_executor_id'],
                   'endpoint_ref': self.endpoint_ref, 'source_kind': self.source_kind, 'transport': 'original_proxy_stdio'}
        proof = self.control_verifier(binding, repository, context) if callable(self.control_verifier) else None
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths', 'manual_execution_coverage', 'takeover', 'control_access')
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or proof.get('grant_binding') != context or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']] or any(not isinstance(proof.get(k), str) or not proof[k] for k in required) or proof.get('control_access') != 'verified-original-input-path':
            raise ManagementError('capability_unverified', 'Current original-service control, task scope and complete write/tool boundary evidence are unavailable; takeover is not enabled.')
        return proof

    def verify_takeover(self, repository, context):
        proof = self._proof(repository, context)
        self.origin_proof = proof
        self.connect()
        return proof

    def connect(self):
        if self.origin_proof is None:
            raise ManagementError('capability_unverified', 'An owner-authorized original target and current host proof are required before connecting.')
        if self.connection and self.connection.get('service_id') != self.origin_proof['service_id']:
            raise ManagementError('binding_conflict', 'The control connection belongs to another original executor.')
        connection = super().connect()
        self.connection.update(service_id=self.origin_proof['service_id'], endpoint_ref=self.endpoint_ref, source_kind=self.source_kind,
                               transport='original_proxy_stdio', control='manual_work_grant')
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
        if params.get('threadId') != authority['thread_id'] or method in {'turn/steer', 'turn/interrupt'} and params.get('expectedTurnId', params.get('turnId')) != authority['turn_id'] or method is None and params.get('turnId') != authority['turn_id']:
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

    def revoke(self):
        with self._condition:
            for incoming in self._server_requests.values():
                if incoming['state'] == 'pending':
                    incoming['state'] = 'control_returned'


def executor_for(manager, record):
    session = record.get('session', {})
    return manager.control_adapters.get(session.get('manual_source_id')) if session.get('origin') == 'manual_takeover' else manager.codex_adapter


def _assignment(record, data):
    profile = data['profiles'].get(record['profile_id'], {})
    accepted = record.get('accepted_responsibility', {})
    if not accepted or any(profile.get(k) != v for k, v in accepted.items()) or profile.get('connection_refs', {}).get('codex') != record.get('accepted_codex_ref') or record['accepted_repository_fingerprint'] != repository_fingerprint(data['projects'][record['project_id']]['repo']):
        raise ManagementError('forbidden', 'The accepted responsibility and repository must remain current for this work grant.')


def _authority(manager, grant_id):
    _, data = manager._load()
    grant = data.get('control_grants', {}).get(grant_id)
    if not grant:
        return None
    record = data['requests'][grant['request_id']]
    return {**grant, 'turn_id': record['session']['turn_id']}


def take_over_session(manager, identity, request_id, manual_session_id, grant_id, expected_turn_id):
    if not isinstance(grant_id, str) or not grant_id or len(grant_id) > 256 or not isinstance(expected_turn_id, str) or not expected_turn_id:
        raise ManagementError('invalid_change', 'A stable grant ID and explicit original current turn are required.')
    with manager._lock:
        _, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Only the verified owner may grant current-work manual control.')
        manager.refresh_manual_sessions(identity)
        version, data = manager._load()
        record = manager._request(identity, request_id, data)
        grants = data.setdefault('control_grants', {})
        existing = grants.get(grant_id)
        intent = {'request_id': request_id, 'manual_session_id': manual_session_id, 'authorized_by': identity.subject, 'expected_turn_id': expected_turn_id}
        if existing:
            if any(existing.get(k) != v for k, v in intent.items()):
                raise ManagementError('binding_conflict', 'A grant ID is permanently bound to its current-work authorization.')
            return {**existing, 'duplicate': True}
        _assignment(record, data)
        manual = data.get('manual_sessions', {}).get(manual_session_id)
        source = data.get('manual_sources', {}).get((manual or {}).get('source_id'), {})
        repository = record['accepted_repository']
        if record.get('session') or record.get('task_delivery') == 'delivered' or record['task_start_anchor'] is None or not manual or source.get('status') != 'verified' or manual['state'] not in {'active', 'inactive_verified'} or manual['logical_repository'] != repository['logical_id'] or manual['cwd'] != repository['worktree']:
            raise ManagementError('binding_conflict', 'Takeover needs this accepted work and a current original session in its exact existing repository boundary.')
        if any(g['status'] in {'active', 'pending', 'suspended'} and g['original_executor_id'] == manual['original_executor_id'] and g['thread_id'] == manual['thread_id'] for g in grants.values()):
            raise ManagementError('binding_conflict', 'One current controller already owns or is reconciling this original session.')
        if any(r['id'] != request_id and r.get('session', {}).get('logical_repository') == repository['logical_id'] and not r.get('repository_released') for r in data['requests'].values()):
            raise ManagementError('repository_busy', 'Another outer task already occupies the original logical repository.')
        actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
        if repository_fingerprint(actual) != record['accepted_repository_fingerprint'] or any(manager.state_dir.is_relative_to(Path(repository[k])) for k in ('worktree', 'git_dir', 'common_dir')):
            raise ManagementError('capability_unverified', 'Original repository layout and authoritative evidence storage must match the accepted boundary.')
        grant = {'id': grant_id, **intent, 'status': 'pending', 'scope': record['accepted_scope'], 'scope_digest': hashlib.sha256(json.dumps(record['accepted_scope'], sort_keys=True).encode()).hexdigest(),
            'controller_profile_id': record['profile_id'], 'controller_identity_ref': record['accepted_responsibility']['identity_ref'],
            'source_id': manual['source_id'], 'original_executor_id': manual['original_executor_id'], 'observation_generation': manual['generation'],
            'thread_id': manual['thread_id'], 'endpoint_ref': source['scope']['endpoint_ref'], 'authorized_at': _now(),
            'repository_fingerprint': record['accepted_repository_fingerprint'], 'external_actor_coverage': 'unknown'}
        grants[grant_id] = grant
        record['control_grant_id'] = grant_id
        with manager._db:
            manager._save(version, data)
        adapter = manager.control_adapters.get(manual['source_id'])
        try:
            if adapter is None or adapter.source_kind != source['kind'] or adapter.endpoint_ref != grant['endpoint_ref']:
                raise ManagementError('capability_unverified', 'This source has no verified controllable original endpoint; it remains observe-only.')
            context = {k: grant[k] for k in ('request_id', 'manual_session_id', 'controller_profile_id', 'controller_identity_ref', 'source_id', 'original_executor_id', 'observation_generation', 'thread_id', 'endpoint_ref', 'scope_digest')}
            context.update(grant_id=grant_id, current_turn_id=expected_turn_id)
            proof = adapter.verify_takeover(repository, context)
            thread = adapter.read_thread(manual['thread_id'])
            active = [t['id'] for t in thread.get('turns', []) if t.get('status') == 'inProgress']
            if adapter.connection['service_id'] != manual['original_executor_id'] or thread.get('cwd') != repository['worktree'] or thread.get('canAcceptDirectInput') is not True or expected_turn_id not in [t.get('id') for t in thread.get('turns', [])] or thread['status'].get('type') == 'active' and active != [expected_turn_id]:
                raise ManagementError('binding_conflict', 'The actual original execution or current turn does not match; no takeover input was sent.')
            from .queue import workspace
            baseline = workspace(repository)
            record['preparation'] = {'status': 'ready', 'plan': {'branch': baseline['branch'], 'commit': baseline['head'], 'dependencies': [], 'issue_updated_at': record['accepted_scope']['updated_at']},
                'workspace': baseline, 'preserved_files': {p: baseline['file_digests'].get(p) for p in baseline['dirty_paths']}, 'prepared_by': identity.subject}
            record['session'] = {**adapter.connection, 'thread_id': manual['thread_id'], 'turn_id': expected_turn_id, 'origin': 'manual_takeover',
                'manual_source_id': manual['source_id'], 'manual_session_id': manual_session_id, 'grant_id': grant_id, 'control': 'assigned_task',
                'logical_repository': repository['logical_id'], 'repository': repository, 'baseline': baseline, 'capability': proof,
                'known_turn_ids': [t['id'] for t in thread.get('turns', [])], 'start_phase': 'original_thread_granted'}
            record.update(execution='running' if active else 'turn_ended', outer_task_status='running', repository_released=False, task_delivery='unmet', unexecuted_reason=None)
            grant.update(status='active', control_generation=adapter.generation, granted_at=_now())
            adapter.authority = lambda: _authority(manager, grant_id)
        except ManagementError as exc:
            grant.update(status='blocked', reason=str(exc))
            raise
        finally:
            with manager._db:
                current, _ = manager._load()
                manager._save(current, data)
        manager.publish_request_message(identity, request_id, 'progress', '本人已授权本次工作接管原会话：' + grant['thread_id'] + '\n控制负责人：' + grant['controller_profile_id'] + '；范围仅本 Issue。其他桌面操作的识别覆盖仍未知。')
        return grant


def return_session_control(manager, identity, request_id, grant_id):
    with manager._lock:
        version, data = manager._load()
        record = manager._request(identity, request_id, data)
        principal = manager._principal(identity, data)
        grant = data.get('control_grants', {}).get(grant_id)
        if not grant or grant['request_id'] != request_id or principal and (principal['id'] != grant['controller_profile_id'] or identity.subject != grant['controller_identity_ref']):
            raise ManagementError('forbidden', 'Control return must identify this work grant and its owner or current controller.')
        if grant['status'] == 'returned':
            return {**grant, 'duplicate': True}
        grant.update(status='returned', returned_at=_now())
        if record.get('session'):
            record['session']['control'] = 'observe_only'
        for question in record.get('human_requests', []):
            if question['resolution'] == 'pending':
                question.update(resolution='expired', control_enabled=False)
        adapter = manager.control_adapters.get(grant['source_id'])
        if adapter is not None:
            adapter.revoke()
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, request_id, 'progress', '本次原会话控制已归还，只观察保留；未发送中断。运行执行与仓库占用仍待核对。')
        return grant
