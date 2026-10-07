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
        if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in binding.items()) or any(not isinstance(proof.get(k), str) or not proof[k] for k in ('original_executor_id', 'provenance', 'evidence_ref')) or not isinstance(proof.get('supported_methods'), list) or not {'thread/read', 'thread/list', 'thread/loaded/list'}.issubset(proof['supported_methods']) or not isinstance(proof.get('source_kinds'), list) or not proof['source_kinds']:
            raise ManagementError('capability_unverified', 'Current original-executor identity and actual read capabilities require trusted host evidence; matching history is insufficient.')
        return proof

    def connect(self):
        proof = self.proof()
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
            'project_fingerprints': {p: repository_fingerprint(data['projects'][p]['repo']) for p in registration['project_ids']},
            'reason': 'Original endpoint and readable scope have not been verified.'}
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
                if adapter is None or adapter.source_kind != source['kind']:
                    raise ManagementError('capability_unverified', 'No approved original-service adapter is registered for this source kind.')
                if any(repository_fingerprint(data['projects'][p]['repo']) != source['project_fingerprints'][p] for p in project_ids):
                    raise ManagementError('binding_conflict', 'The registered observation project boundary changed.')
                proof = adapter.proof()
                binding = {'executor': proof['original_executor_id'], 'generation': adapter.generation, 'endpoint_ref': adapter.endpoint_ref}
                if source.get('scope') and any(source['scope'].get(k) != v for k, v in binding.items()):
                    raise ManagementError('binding_conflict', 'Original executor identity or connection generation changed; prior occupancy remains unknown.')
                adapter.connect()
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
                    ended = thread_id in loaded and status == 'idle' and isinstance(turns, list) and turns and all(t.get('status') in {'completed', 'failed', 'interrupted'} and t.get('itemsView') == 'full' for t in turns) and proof.get('runtime_coverage') == 'complete' and 'thread/backgroundTerminals/list' in proof['supported_methods'] and not adapter.background_terminals(thread_id)
                    state = 'active' if active else 'inactive_verified' if ended else 'last_known' if thread_id not in loaded else 'unknown'
                    sessions[key] = {**previous, 'id': key, 'source_id': source['id'], 'source_kind': source['kind'], 'thread_id': thread_id,
                        'original_executor_id': binding['executor'], 'generation': binding['generation'], 'project_ids': matches,
                        'logical_repository': logical, 'cwd': thread['cwd'], 'state': state, 'thread_status': status,
                        'control': 'observe_only', 'last_verified_at': _now(), 'last_known_state': 'active' if active else previous.get('last_known_state'),
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
        return manager.read_snapshot(identity, scope)


def guard_repository(manager, identity, record, version, data):
    logical = record.get('session', {}).get('logical_repository') or record.get('queue', {}).get('logical_repository')
    blockers = [s['id'] for s in data.get('manual_sessions', {}).values() if s['logical_repository'] == logical and s['blocks_repository']]
    unknown = [s['id'] for s in data.get('manual_sources', {}).values() if record['project_id'] in s['project_ids'] and s['status'] != 'verified']
    if blockers or unknown:
        reason = '手动执行或原服务观察范围待核对；同仓库新任务等待。'
        record['queue'].update(manual_blockers=blockers, observation_blockers=unknown, reason=reason)
        record['unexecuted_reason'] = reason
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, record['id'], 'progress', reason)
        raise ManagementError('repository_busy', reason)
    record['queue'].update(manual_blockers=[], observation_blockers=[])
