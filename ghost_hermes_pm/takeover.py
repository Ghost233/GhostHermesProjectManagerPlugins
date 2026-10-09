"""One owner-authorized current-work grant on the verified original manual executor."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .dsh import DshRemoteAdapter, repository_fingerprint
from .manager import ManagementError, _repository
from .observation import READ_METHODS, SOURCE_KINDS

CONTROL_METHODS = frozenset({'session/prompt', 'session/cancel', '$events/result', 'userQuestions/answer'})


def _now():
    return datetime.now(timezone.utc).isoformat()


class OriginalControlAdapter(DshRemoteAdapter):
    """Independent original proxy. The readonly adapter is never promoted."""
    def __init__(self, base_url, *, cookie='', service_ref, source_kind, endpoint_ref, verifier=None, timeout=10, expected_home=None):
        super().__init__(base_url, cookie=cookie, service_ref=service_ref, timeout=timeout, expected_home=expected_home)
        if source_kind not in SOURCE_KINDS or not isinstance(endpoint_ref, str) or not endpoint_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit original endpoint and individual supported source kind are required.')
        self.source_kind, self.endpoint_ref, self.control_verifier = source_kind, endpoint_ref, verifier
        self.origin_proof, self.authority, self.control_repository = None, None, None

    def _proof(self, repository, context):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'service_id': context['original_executor_id'],
                   'endpoint_ref': self.endpoint_ref, 'source_kind': self.source_kind, 'transport': 'desktop_http_mux', 'engine': 'dsh'}
        if self.connection is not None:
            binding['platform'] = self.connection['platform']
            binding['client_id'] = self.connection['client_id']
        proof = self.control_verifier(binding, repository, context) if callable(self.control_verifier) else None
        required = ('permission_profile', 'policy_digest', 'platform_enforcement', 'tool_paths', 'manual_execution_coverage', 'takeover', 'control_access')
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or proof.get('grant_binding') != context or proof.get('repository_fingerprint') != repository_fingerprint(repository) or proof.get('runtime_roots') != [repository['worktree']] or any(not isinstance(proof.get(k), str) or not proof[k] for k in required) or proof.get('control_access') != 'verified-original-input-path' or not proof['permission_profile'].startswith('host:') or not isinstance(proof.get('backend_instance_ref'), str) or not proof['backend_instance_ref']:
            raise ManagementError('capability_unverified', 'Current original-service control, task scope and complete write/tool boundary evidence are unavailable; takeover is not enabled.')
        actions = {'append', 'stop', 'continue', 'idle_input', 'related_execution', 'human_response'}
        if not isinstance(proof.get('task_control'), dict) or any(not isinstance(proof['task_control'].get(action), str) or not proof['task_control'][action] for action in actions):
            raise ManagementError('capability_unverified', 'Original append, stop, continuation and owner-response methods need their own current capability evidence.')
        return proof

    def verify_takeover(self, repository, context):
        self.control_repository = repository
        self.bind_repository(context['thread_id'], repository)
        proof = self._proof(repository, context)
        self.origin_proof = proof
        self.connect()
        proof = self._proof(repository, context)
        self.origin_proof = proof
        self._control_proof = proof
        return proof

    def connect(self):
        if self.origin_proof is None:
            raise ManagementError('capability_unverified', 'An owner-authorized original target and current host proof are required before connecting.')
        if self.connection and self.connection.get('service_id') != self.origin_proof['service_id']:
            raise ManagementError('binding_conflict', 'The control connection belongs to another original executor.')
        connection = super().connect()
        if connection['service_id'] != self.origin_proof['service_id']:
            raise ManagementError('binding_conflict', 'The current DSH backend differs from the original granted executor.')
        self.connection.update(endpoint_ref=self.endpoint_ref, source_kind=self.source_kind,
                               transport='desktop_http_mux', control='manual_work_grant')
        return dict(self.connection)

    def _guard_request(self, method, params):
        if method in READ_METHODS:
            return super()._guard_request(method, params)
        if method not in CONTROL_METHODS:
            raise ManagementError('forbidden', 'Manual takeover cannot create a session or replace the DSH backend.')
        authority = self.authority() if callable(self.authority) else None
        if not authority or authority['status'] != 'active' or authority['id'] != (self.origin_proof or {}).get('grant_binding', {}).get('grant_id'):
            raise ManagementError('forbidden', 'No active current-work grant authorizes this original input.')
        target = params.get('_authority', {})
        if target.get('thread_id') != authority['thread_id'] or target.get('turn_id') != authority['turn_id']:
            raise ManagementError('binding_conflict', 'The input is outside the original granted thread/current turn.')
        action = 'stop' if method == 'session/cancel' else 'human_response' if method in {'$events/result', 'userQuestions/answer'} else 'append'
        self.verify_control(self.control_repository, action, self.origin_proof)
        return super()._guard_request(method, params)

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
        self._control_proof = proof
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


def executor_for(manager, record):
    session = record.get('session', {})
    if session.get('recovery_ref'):
        return manager.recovery_adapters.get(session['recovery_ref'])
    if session.get('origin') == 'manual_takeover':
        return manager.control_adapters.get(session.get('manual_source_id'))
    accepted_ref, session_ref = record.get('accepted_dsh_ref'), session.get('service_ref')
    if accepted_ref and session_ref and accepted_ref != session_ref:
        return None
    reference = accepted_ref or session_ref
    adapter = manager.dsh_adapters.get(reference)
    if adapter is not None:
        return adapter
    legacy = manager.dsh_adapter
    return legacy if legacy is not None and reference == legacy.service_ref else None


def _assignment(record, data):
    if record.get('executor_engine') != 'dsh':
        raise ManagementError('binding_conflict', 'The accepted work belongs to another executor and requires explicit migration.')
    profile = data['profiles'].get(record['profile_id'], {})
    accepted = record.get('accepted_responsibility', {})
    if not accepted or any(profile.get(k) != v for k, v in accepted.items()) or profile.get('connection_refs', {}).get('dsh') != record.get('accepted_dsh_ref') or record['accepted_repository_fingerprint'] != repository_fingerprint(data['projects'][record['project_id']]['repo']):
        raise ManagementError('forbidden', 'The accepted responsibility and repository must remain current for this work grant.')


def _authority(manager, grant_id):
    _, data = manager._load()
    grant = data.get('control_grants', {}).get(grant_id)
    if not grant:
        return None
    record = data['requests'][grant['request_id']]
    effective = grant['status']
    if record.get('control_grant_id') != grant_id or record.get('session', {}).get('control') != 'assigned_task' or record.get('task_delivery') == 'delivered':
        effective = 'expired'
    return {**grant, 'status': effective, 'turn_id': record['session']['turn_id']}


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
        prior_session = record.get('session')
        prior_grant = grants.get(record.get('control_grant_id'), {})
        retaking = bool(prior_session and prior_session.get('origin') == 'manual_takeover' and prior_session.get('manual_session_id') == manual_session_id and prior_grant.get('status') == 'returned')
        if prior_session and not retaking or record.get('task_delivery') == 'delivered' or record['task_start_anchor'] is None or not manual or source.get('status') != 'verified' or manual['state'] not in {'active', 'inactive_verified'} or manual['logical_repository'] != repository['logical_id'] or manual['cwd'] != repository['worktree']:
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
            context.update(grant_id=grant_id, current_turn_id=expected_turn_id, original_turn_id=expected_turn_id)
            proof = adapter.verify_takeover(repository, context)
            thread = adapter.read_thread(manual['thread_id'])
            active = [t['id'] for t in thread.get('turns', []) if t.get('status') == 'inProgress']
            if adapter.connection['service_id'] != manual['original_executor_id'] or thread.get('cwd') != repository['worktree'] or thread.get('canAcceptDirectInput') is not True or expected_turn_id not in [t.get('id') for t in thread.get('turns', [])] or thread['status'].get('type') == 'active' and active != [expected_turn_id]:
                raise ManagementError('binding_conflict', 'The actual original execution or current turn does not match; no takeover input was sent.')
            from .queue import workspace
            baseline = prior_session['baseline'] if retaking else workspace(repository)
            preparation = {'status': 'ready', 'plan': {'branch': baseline['branch'], 'commit': baseline['head'], 'dependencies': [], 'issue_updated_at': record['accepted_scope']['updated_at']},
                'workspace': baseline, 'preserved_files': {p: baseline['file_digests'].get(p) for p in baseline['dirty_paths']}, 'prepared_by': identity.subject}
            if not retaking:
                record['preparation'] = preparation
            record['session'] = {**adapter.connection, 'thread_id': manual['thread_id'], 'turn_id': expected_turn_id, 'origin': 'manual_takeover',
                'manual_source_id': manual['source_id'], 'manual_session_id': manual_session_id, 'grant_id': grant_id, 'control': 'assigned_task',
                'logical_repository': repository['logical_id'], 'repository': repository, 'baseline': baseline, 'capability': proof,
                'known_turn_ids': [t['id'] for t in thread.get('turns', [])], 'start_phase': 'original_thread_granted'}
            record.update(execution='running' if active else 'turn_ended', outer_task_status='running', repository_released=False, task_delivery='unmet', unexecuted_reason=None)
            grant.update(status='active', control_generation=adapter.generation, granted_at=_now())
            record['execution_capability'] = {'status': 'verified', 'enabled': True, 'connection': adapter.connection, 'proof': proof, 'operation': 'manual_takeover'}
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
        if record.get('control_grant_id') != grant_id:
            raise ManagementError('forbidden', 'A superseded grant cannot revoke another current-work authorization.')
        grant.update(status='returned', returned_at=_now())
        if record.get('session'):
            record['session']['control'] = 'observe_only'
        for question in record.get('human_requests', []):
            if question['resolution'] == 'pending':
                question.update(resolution='expired', control_enabled=False)
        adapter = manager.control_adapters.get(grant['source_id'])
        if adapter is not None:
            adapter.revoke(grant['thread_id'])
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, request_id, 'progress', '本次原会话控制已归还，只观察保留；未发送中断。运行执行与仓库占用仍待核对。')
        return grant


def bind_executor(manager, record):
    adapter = executor_for(manager, record)
    if adapter is not None:
        adapter.waterfall_authority = lambda thread_id, turn_id: _waterfall_authority(manager, adapter, thread_id, turn_id)
        adapter.waterfall_question_supported = lambda envelope: _waterfall_question_supported(manager, envelope)
    if adapter is not None and record.get('session', {}).get('recovery_ref'):
        from .recovery import bind_recovery
        return bind_recovery(manager, record, adapter)
    if adapter is not None and record.get('session', {}).get('origin') == 'manual_takeover':
        grant_id = record.get('control_grant_id')
        adapter.authority = lambda: _authority(manager, grant_id)
    return adapter


def _waterfall_authority(manager, adapter, thread_id, turn_id):
    """A pending interaction must still belong to one durably assigned work item."""
    if not manager._lock.acquire(blocking=False):
        return False  # Passing through must not wait behind an execution operation.
    try:
        if adapter._closed or not adapter.connection:
            return False
        schema, _, payload = manager._db.execute('SELECT schema_version, version, payload FROM directory WHERE id=1').fetchone()
        data = json.loads(payload)
        if schema != 2 or data.get('executor_engine') != 'dsh':
            return False
        for candidate in data.get('requests', {}).values():
            session = candidate.get('session', {})
            if (executor_for(manager, candidate) is not adapter or session.get('thread_id') != thread_id
                    or session.get('turn_id') != turn_id or session.get('generation') != adapter.generation
                    or session.get('service_id') != adapter.connection['service_id']
                    or session.get('control') != 'assigned_task' or candidate.get('repository_released')
                    or candidate.get('task_delivery') == 'delivered' or candidate.get('archive_stop_intent')
                    or candidate.get('outer_task_status') == 'stopped'
                    or candidate.get('stop', {}).get('status') == 'processing'):
                continue
            _assignment(candidate, data)
            from .lifecycle import require_active
            require_active(data, candidate['profile_id'], candidate['project_id'])
            if (session.get('origin') == 'manual_takeover'
                    and data.get('control_grants', {}).get(candidate.get('control_grant_id'), {}).get('status') != 'active'):
                return False
            return True
        return False
    except Exception:
        return False
    finally:
        manager._lock.release()


def _waterfall_question_supported(manager, envelope):
    if not manager._lock.acquire(blocking=False):
        return False
    try:
        from .questions import _describe
        return _describe(manager, envelope).get('answerable') is True
    except Exception:
        return False
    finally:
        manager._lock.release()


def _expire_grant(data, grant, status, reason=None):
    grant.update(status=status, reason=reason, ended_at=_now())
    record = data['requests'][grant['request_id']]
    if record.get('control_grant_id') == grant['id'] and record.get('session'):
        record['session']['control'] = 'observe_only'
        record['execution_capability'] = {'status': 'blocked', 'enabled': False, 'reason': reason or 'Current-work control has ended.'}
        if status == 'suspended':
            record.update(execution='unverified', unexecuted_reason=reason)
        for question in record.get('human_requests', []):
            if question['resolution'] == 'pending':
                question.update(resolution='expired', control_enabled=False)


def suspend_grant(manager, identity, request_id, reason):
    with manager._lock, manager._db:
        version, data = manager._load()
        record = manager._request(identity, request_id, data)
        grant = data.get('control_grants', {}).get(record.get('control_grant_id'))
        if not grant or grant['status'] not in {'active', 'suspended'}:
            return
        _expire_grant(data, grant, 'suspended', reason)
        adapter = manager.control_adapters.get(grant['source_id'])
        if adapter is not None:
            adapter.revoke(grant['thread_id'])
        manager._save(version, data)
    manager.publish_request_message(identity, request_id, 'progress', '原会话控制待本人核对，已暂停本次授权：' + reason + '\n其他桌面操作的识别仍未知，原执行未被宣称停止。')


def reconcile_grants(manager, data):
    for grant in data.get('control_grants', {}).values():
        if grant['status'] != 'active':
            continue
        record = data['requests'][grant['request_id']]
        adapter = executor_for(manager, record)
        source = data.get('manual_sources', {}).get(grant['source_id'], {})
        reason = None
        if adapter is None or adapter.generation != grant['control_generation'] or adapter._closed:
            reason = 'Original control connection generation is unavailable; history is not renewed authorization.'
        elif source.get('status') == 'conflict' or source.get('scope', {}).get('executor') != grant['original_executor_id']:
            reason = 'The original observation source has an identity/service conflict.'
        else:
            try:
                _assignment(record, data)
            except ManagementError as exc:
                reason = str(exc)
        if reason:
            _expire_grant(data, grant, 'suspended', reason)


def complete_grant(manager, record, data):
    grant = data.get('control_grants', {}).get(record.get('control_grant_id'))
    if not grant:
        return
    _expire_grant(data, grant, 'completed', 'Frozen Issue delivery and original execution end were verified.')
    grant['completed_at'] = _now()
    adapter = manager.control_adapters.get(grant['source_id'])
    if adapter is not None:
        adapter.revoke(grant['thread_id'])


def configured_control_adapters(configs, state_dir):
    """Independent clients of the existing DSH backend, never promoted observers."""
    from .observation import resolved_configuration
    if not configs:
        return {}
    adapters = {}
    if not isinstance(configs, list):
        raise ManagementError('invalid_change', 'Manual control requires an explicit native configuration list.')
    for config in configs:
        frozen, runtime = resolved_configuration(config, extra=('source_id',))
        if config['source_id'] in adapters:
            raise ManagementError('invalid_change', 'A manual source cannot have multiple control connections.')
        def verifier(binding, repository, context, frozen=frozen):
            try:
                base = Path(state_dir).resolve()
                manifest_path = base / 'dsh-manual-control.json'
                if manifest_path != manifest_path.resolve() or manifest_path.stat().st_size > 65536:
                    raise ValueError('Invalid control manifest.')
                reference = json.loads(manifest_path.read_text())[context['grant_id']]
                path = base / reference['path']
                if path != path.resolve() or not path.is_relative_to(base / 'manual-control-evidence') or path.stat().st_size > 1024 * 1024:
                    raise ValueError('Invalid control receipt.')
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                    raise ValueError('Control receipt digest mismatch.')
                report = json.loads(raw)
                if not isinstance(report, dict) or report.get('grant_binding') != context or report.get('source_id') != frozen['source_id'] or report.get('endpoint_ref') != frozen['endpoint_ref'] or report.get('endpoint_sha256') != hashlib.sha256(frozen['base_url'].encode()).hexdigest() or report.get('source_kind') != frozen['source_kind'] or not isinstance(report.get('backend_instance_ref'), str) or not report['backend_instance_ref']:
                    raise ValueError('The current work/backend scope differs.')
                connection = {**binding, 'platform': binding.get('platform', report.get('platform'))}
                from .validation import validate_receipts
                proof = validate_receipts(report, connection, repository, frozen, state_dir, startup_kind='manual_takeover')
                return {**proof, **binding, 'grant_binding': context, 'takeover': proof['manual_takeover'],
                        'backend_instance_ref': report['backend_instance_ref'],
                        'control_access': 'verified-original-input-path', 'external_actor_coverage': 'unknown'}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ManagementError('capability_unverified', 'Current hashed DSH takeover/write/tool/control evidence is missing; original control remains disabled.') from exc
        adapters[config['source_id']] = OriginalControlAdapter(runtime['base_url'], cookie=runtime['cookie'],
            service_ref=config['service_ref'], source_kind=config['source_kind'], endpoint_ref=config['endpoint_ref'],
            verifier=verifier, expected_home=config.get('expected_home'))
    return adapters
