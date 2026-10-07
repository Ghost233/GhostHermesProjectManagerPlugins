"""Read original registered executors without obtaining session control."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .codex import CodexStdioAdapter, repository_fingerprint
from .manager import ManagementError
from .queue import logical_repository

READ_METHODS = frozenset({'initialize', 'initialized', 'thread/read', 'thread/list', 'thread/loaded/list', 'thread/backgroundTerminals/list', 'thread/turns/list', 'thread/items/list'})
SOURCE_KINDS = {'daemon', 'independent_cli', 'desktop'}


def _now():
    return datetime.now(timezone.utc).isoformat()


class ReadOnlyCodexAdapter(CodexStdioAdapter):
    def __init__(self, command, *, cwd, env, service_ref, source_kind, endpoint_ref, verifier=None, timeout=10):
        super().__init__(command, cwd=cwd, env=env, service_ref=service_ref, timeout=timeout)
        if source_kind not in SOURCE_KINDS or not isinstance(endpoint_ref, str) or not endpoint_ref.startswith('local:'):
            raise ManagementError('invalid_change', 'An explicit original local endpoint and individual source kind are required.')
        self.source_kind, self.endpoint_ref, self.observation_verifier = source_kind, endpoint_ref, verifier
        self.transport = 'original_proxy_stdio'

    def proof(self):
        binding = {'generation': self.generation, 'service_ref': self.service_ref, 'source_kind': self.source_kind,
                   'endpoint_ref': self.endpoint_ref, 'transport': self.transport}
        proof = self.observation_verifier(binding) if callable(self.observation_verifier) else None
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or any(not isinstance(proof.get(k), str) or not proof[k] for k in ('original_executor_id', 'provenance', 'evidence_ref')) or not isinstance(proof.get('supported_methods'), list) or not {'thread/read', 'thread/list', 'thread/loaded/list'}.issubset(proof['supported_methods']) or not isinstance(proof.get('source_kinds'), list) or not proof['source_kinds'] or any(not isinstance(method, str) or method not in READ_METHODS for method in proof['supported_methods']) or any(kind not in {'cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview', 'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown'} for kind in proof['source_kinds']):
            raise ManagementError('capability_unverified', 'Current original-executor identity and actual read capabilities require trusted host evidence; matching history is insufficient.')
        from .manager import _public_text
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
        thread = self._call('thread/read', {'threadId': thread_id, 'includeTurns': include_turns}).get('thread')
        if not isinstance(thread, dict) or thread.get('id') != thread_id or not isinstance(thread.get('status'), dict):
            raise ManagementError('capability_unverified', 'Original executor returned incomplete or conflicting thread identity.')
        return thread



def _ended(adapter, thread, proof, allowed_repositories):
    if proof.get('runtime_coverage') != 'complete' or 'thread/backgroundTerminals/list' not in proof['supported_methods']:
        return False
    pending, seen = [thread], set()
    while pending:
        current = pending.pop()
        if current['id'] in seen:
            continue
        if len(seen) >= 100 or logical_repository(current.get('cwd')) not in allowed_repositories:
            return False
        seen.add(current['id'])
        if current['status'].get('type') != 'idle':
            return False
        turns = current.get('turns', [])
        if current.get('historyMode') == 'paginated':
            if 'thread/turns/list' not in proof['supported_methods']:
                return False
            turns = adapter._pages('thread/turns/list', {'threadId': current['id'], 'limit': 100, 'itemsView': 'full', 'sortDirection': 'asc'})
        if not isinstance(turns, list) or not turns:
            return False
        for turn in turns:
            if not isinstance(turn, dict) or turn.get('status') not in {'completed', 'failed', 'interrupted'} or turn.get('itemsView') != 'full' or not isinstance(turn.get('items'), list):
                return False
            for item in turn['items']:
                if not isinstance(item, dict):
                    return False
                if item.get('type') in {'commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall'} and item.get('status') not in {'completed', 'failed', 'declined'}:
                    return False
                if item.get('type') == 'collabAgentToolCall':
                    children = item.get('receiverThreadIds')
                    if not isinstance(children, list) or any(not isinstance(c, str) or not c for c in children):
                        return False
                    for child in children:
                        metadata = adapter.read_thread(child, include_turns=False)
                        if logical_repository(metadata.get('cwd')) not in allowed_repositories:
                            return False
                        pending.append(adapter.read_thread(child, include_turns=True))
        if adapter.background_terminals(current['id']):
            return False
    return True


def register_source(manager, identity, registration):
    with manager._lock, manager._db:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'The owner must explicitly register observation source and project scope.')
        allowed = {'id', 'kind', 'project_ids', 'adapter_ref'}
        if not isinstance(registration, dict) or set(registration) != allowed or registration.get('kind') not in SOURCE_KINDS or any(not isinstance(registration.get(k), str) or not registration[k] for k in ('id', 'adapter_ref')) or not registration['adapter_ref'].startswith('local:') or not isinstance(registration.get('project_ids'), list) or not registration['project_ids'] or len(set(registration['project_ids'])) != len(registration['project_ids']) or any(p not in data['projects'] for p in registration['project_ids']):
            raise ManagementError('invalid_change', 'Register a source ID, individual kind, local adapter reference and existing project IDs only.')
        sources = data.setdefault('manual_sources', {})
        if registration['id'] in sources:
            existing = sources[registration['id']]
            if any(existing.get(k) != v for k, v in registration.items()):
                raise ManagementError('binding_conflict', 'An original source cannot be silently rebound.')
            return existing
        sources[registration['id']] = {**registration, 'control': 'observe_only', 'status': 'unknown', 'registered_at': _now(),
            'logical_repositories': {p: data['projects'][p]['repo']['logical_id'] for p in registration['project_ids']},
            'project_fingerprints': {p: repository_fingerprint(data['projects'][p]['repo']) for p in registration['project_ids']},
            'reason': 'Original endpoint and readable scope have not been verified.'}
        from .manager import _public_text
        for key in ('id', 'adapter_ref'):
            _public_text(registration[key], manager._sensitive_values())
        manager._save(version, data)
        return sources[registration['id']]


def refresh_manual_sessions(manager, identity, scope=None):
    with manager._lock:
        visible = {p['id'] for p in manager.read_snapshot(identity, scope)['projects']}
        version, data = manager._load()
        sessions = data.setdefault('manual_sessions', {})
        for source in data.setdefault('manual_sources', {}).values():
            project_ids = set(source['project_ids']) & visible
            if not project_ids:
                continue
            adapter = manager.observation_adapters.get(source['adapter_ref'])
            try:
                if adapter is None or adapter.source_kind != source['kind'] or adapter.service_ref != source['adapter_ref']:
                    raise ManagementError('capability_unverified', 'No approved original-service adapter is registered for this source kind.')
                if any(repository_fingerprint(data['projects'][p]['repo']) != source['project_fingerprints'][p] for p in project_ids):
                    raise ManagementError('binding_conflict', 'The registered observation project boundary changed.')
                if any(manager.state_dir.is_relative_to(Path(data['projects'][p]['repo'][k])) for p in project_ids for k in ('worktree', 'git_dir', 'common_dir')):
                    raise ManagementError('capability_unverified', 'Authoritative observation evidence must be outside the observed project writable source and Git metadata.')
                proof = adapter.proof()
                binding = {'executor': proof['original_executor_id'], 'generation': adapter.generation, 'endpoint_ref': adapter.endpoint_ref}
                expected = source.get('scope') or source.get('expected_binding')
                if expected and any(expected.get(k) != v for k, v in binding.items()):
                    raise ManagementError('binding_conflict', 'Original executor identity or connection generation changed; prior occupancy remains unknown.')
                source.setdefault('expected_binding', binding)
                connection = adapter.connect()
                if connection.get('service_id') != binding['executor'] or connection.get('generation') != binding['generation'] or connection.get('endpoint_ref') != binding['endpoint_ref']:
                    raise ManagementError('binding_conflict', 'Original source identity changed between host verification and the actual connection.')
                loaded = adapter.loaded_threads()
                if any(not isinstance(i, str) or not i for i in loaded):
                    raise ManagementError('capability_unverified', 'Loaded-list coverage is malformed.')
                listed = []
                for archived in (False, True):
                    listed.extend(adapter._pages('thread/list', {'limit': 100, 'sourceKinds': proof['source_kinds'], 'archived': archived, 'useStateDbOnly': True}))
                if any(not isinstance(t, dict) or not isinstance(t.get('id'), str) for t in listed):
                    raise ManagementError('capability_unverified', 'State-database thread-list coverage is malformed.')
                ids = set(loaded) | {t['id'] for t in listed}
                seen = set()
                for thread_id in sorted(ids):
                    thread = adapter.read_thread(thread_id, include_turns=False)
                    logical = logical_repository(thread.get('cwd'))
                    matches = [p for p in project_ids if data['projects'][p]['repo']['logical_id'] == logical]
                    if not matches:
                        continue
                    thread = adapter.read_thread(thread_id, include_turns=True)
                    if logical_repository(thread.get('cwd')) != logical:
                        raise ManagementError('binding_conflict', 'The thread repository changed during observation.')
                    key = hashlib.sha256(json.dumps([source['id'], binding, thread_id], sort_keys=True).encode()).hexdigest()
                    seen.add(key)
                    previous = sessions.get(key, {})
                    status = thread['status'].get('type')
                    turns = thread.get('turns', [])
                    active = thread_id in loaded and status == 'active'
                    ended = thread_id in loaded and status == 'idle' and _ended(adapter, thread, proof, {data['projects'][p]['repo']['logical_id'] for p in project_ids})
                    state = 'active' if active else 'inactive_verified' if ended else 'last_known' if thread_id not in loaded else 'unknown'
                    sessions[key] = {**previous, 'id': key, 'source_id': source['id'], 'source_kind': source['kind'], 'thread_id': thread_id,
                        'original_executor_id': binding['executor'], 'generation': binding['generation'], 'project_ids': matches,
                        'logical_repository': logical, 'cwd': thread['cwd'], 'recorded_thread_source': thread.get('source') if isinstance(thread.get('source'), str) else 'structured_or_unknown', 'state': state, 'thread_status': status,
                        'control': 'observe_only', 'last_verified_at': _now(), 'last_known_state': 'active' if active else 'inactive_verified' if ended else previous.get('last_known_state'),
                        'last_known_state_at': _now() if active or ended else previous.get('last_known_state_at'),
                        'blocks_repository': active or not ended, 'reason': None if active or ended else 'Readable history or idle metadata does not prove execution ended on the original service.'}
                for record in sessions.values():
                    if record['source_id'] == source['id'] and set(record['project_ids']) & project_ids and record['id'] not in seen:
                        record.update(state='unknown', blocks_repository=True, reason='Prior original execution was not found in complete current reads; disappearance is not termination.')
                source.update(status='verified', reason=None, last_verified_at=_now(), scope={**binding, 'coverage': 'connected_executor_only',
                    'source_kinds': proof['source_kinds'], 'read_methods': proof['supported_methods'], 'indexed_history': 'state_db_only',
                    'archive_filters': [False, True], 'runtime_coverage': proof.get('runtime_coverage', 'unknown'), 'global_execution_coverage': 'unknown'})
            except ManagementError as exc:
                source.update(status='conflict' if exc.code == 'binding_conflict' else 'unknown', reason=str(exc), last_observation_attempt_at=_now())
                for record in sessions.values():
                    if record['source_id'] == source['id'] and set(record['project_ids']) & project_ids:
                        record.update(state='unknown', blocks_repository=True, reason=str(exc))
        with manager._db:
            manager._save(version, data)
        for record in data['requests'].values():
            if record['project_id'] not in visible:
                continue
            relevant = [s for s in sessions.values() if s['logical_repository'] == record['queue']['logical_repository']]
            sources = [s for s in data.get('manual_sources', {}).values() if record['queue']['logical_repository'] in s.get('logical_repositories', {}).values()]
            if relevant or sources:
                text = '手动 Codex 只观察；不改变原执行。\n' + '\n'.join(s['source_kind'] + ' / ' + s['thread_id'] + '：' + s['state'] + '；权限 observe_only；核实 ' + s['last_verified_at'] for s in relevant)
                text += '\n范围：仅实际已连接原执行器；其他服务活动仍未知。'
                text += '\n来源：' + '; '.join(s['kind'] + ' ' + s['status'] + ('：' + s['reason'] if s.get('reason') else '') for s in sources)
                report_key = hashlib.sha256(json.dumps([(s['id'], s['state'], s.get('reason')) for s in relevant] + [(s['id'], s['status'], s.get('reason')) for s in sources], sort_keys=True).encode()).hexdigest()
                if record.get('manual_report_state') != report_key:
                    with manager._db:
                        current, latest = manager._load()
                        latest['requests'][record['id']]['manual_report_state'] = report_key
                        manager._save(current, latest)
                    manager.publish_request_message(identity, record['id'], 'progress', text)
        return manager.read_snapshot(identity, scope)


def guard_repository(manager, identity, record, version, data):
    logical = record.get('session', {}).get('logical_repository') or record.get('queue', {}).get('logical_repository')
    grant = data.get('control_grants', {}).get(record.get('control_grant_id'), {})
    own_manual = grant.get('manual_session_id') if grant.get('status') == 'active' else None
    blockers = [s['id'] for s in data.get('manual_sessions', {}).values() if s['logical_repository'] == logical and s['blocks_repository'] and s['id'] != own_manual]
    unknown = [s['id'] for s in data.get('manual_sources', {}).values() if logical in s.get('logical_repositories', {}).values() and s['status'] != 'verified']
    if blockers or unknown:
        reason = '手动执行或原服务观察范围待核对；同仓库新任务等待。'
        record['queue'].update(manual_blockers=blockers, observation_blockers=unknown, reason=reason)
        record['unexecuted_reason'] = reason
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, record['id'], 'progress', reason)
        raise ManagementError('repository_busy', reason)
    record['queue'].update(manual_blockers=[], observation_blockers=[])


def reconcile_connections(manager, data):
    for source in data.get('manual_sources', {}).values():
        adapter = manager.observation_adapters.get(source['adapter_ref'])
        binding = source.get('scope')
        if binding and (adapter is None or adapter.generation != binding['generation'] or adapter._closed):
            reason = 'Original observation connection unavailable; last known activity is retained without a stopped claim.'
            source.update(status='unknown' if source['status'] != 'conflict' else 'conflict', reason=reason)
            for session in data.get('manual_sessions', {}).values():
                if session['source_id'] == source['id']:
                    session.update(state='unknown', blocks_repository=True, reason=reason)


def configured_observation_adapters(configs, state_dir):
    """Native host settings and hashed host evidence; no commands from HTTP/chat."""
    import sys
    if not configs:
        return {}
    if not isinstance(configs, list):
        raise ManagementError('invalid_change', 'Observation adapters require an explicit native configuration list.')
    adapters = {}
    allowed = {'executable', 'cwd', 'environment', 'service_ref', 'source_kind', 'endpoint', 'endpoint_ref'}
    for config in configs:
        if not isinstance(config, dict) or set(config) != allowed or any(not isinstance(config.get(k), str) or not config[k] for k in ('executable', 'cwd', 'service_ref', 'source_kind', 'endpoint', 'endpoint_ref')) or not Path(config['executable']).is_absolute() or not Path(config['endpoint']).is_absolute() or config['service_ref'] in adapters:
            raise ManagementError('invalid_change', 'Specify the fixed executable, original endpoint, source kind and explicit environment; no default socket or owned server is accepted.')
        frozen = dict(config)
        def verifier(binding, frozen=frozen):
            try:
                base = Path(state_dir).resolve()
                manifest = base / 'codex-observation.json'
                if manifest != manifest.resolve() or manifest.stat().st_size > 65536:
                    raise ValueError('Invalid observation manifest.')
                reference = json.loads(manifest.read_text())[frozen['service_ref']]
                receipt_path = base / reference['path']
                if receipt_path != receipt_path.resolve() or not receipt_path.is_relative_to(base / 'observation-evidence') or receipt_path.stat().st_size > 65536:
                    raise ValueError('Invalid observation receipt.')
                raw = receipt_path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                    raise ValueError('Receipt digest mismatch.')
                receipt = json.loads(raw)
                expected = {**binding, 'binary_sha256': hashlib.sha256(Path(frozen['executable']).read_bytes()).hexdigest(),
                    'configuration_sha256': hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
                    'endpoint_sha256': hashlib.sha256(frozen['endpoint'].encode()).hexdigest(), 'platform': sys.platform,
                    'original_endpoint_verified': 'PASS', 'observation_read_only': 'PASS', 'provenance': 'trusted_host_original_executor'}
                if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()) or not isinstance(receipt.get('read_cases'), dict) or any(receipt['read_cases'].get(k) != 'PASS' for k in ('initialize', 'thread/read', 'thread/list', 'thread/loaded/list', 'no_execution_writes', 'original_request_routing', 'unsupported_scope', 'disconnect')):
                    raise ValueError('Original source verification is missing or belongs to another binding.')
                if any(receipt['read_cases'].get(method) != 'PASS' for method in receipt.get('supported_methods', [])):
                    raise ValueError('A declared read method lacks actual bound method evidence.')
                return {**receipt, 'evidence_ref': str(receipt_path)}
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ManagementError('capability_unverified', 'No current hashed host evidence verifies this approved original endpoint and read capabilities; observation remains unavailable.') from exc
        adapters[config['service_ref']] = ReadOnlyCodexAdapter([config['executable'], 'app-server', 'proxy', '--sock', config['endpoint']],
            cwd=config['cwd'], env=config['environment'], service_ref=config['service_ref'], source_kind=config['source_kind'], endpoint_ref=config['endpoint_ref'], verifier=verifier)
    return adapters
