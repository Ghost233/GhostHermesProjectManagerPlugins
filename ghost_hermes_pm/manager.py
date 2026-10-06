from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import threading
import re
import hashlib
import uuid


@dataclass(frozen=True)
class VerifiedIdentity:
    """An identity established by a trusted entry adapter, never a request body."""
    subject: str
    source: str


class ManagementError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _public_text(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 100000:
        raise ManagementError('invalid_change', 'Bounded public material is required.')
    if re.search(r'(?:password|passwd|secret|token|api[_ -]?key|private[_ -]?key)\s*[:=]\s*\S+|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{16,}|-----BEGIN[^\n]*PRIVATE KEY', text, re.IGNORECASE):
        raise ManagementError('invalid_change', 'Sensitive material must be handled in the original private interface.')


def _message_anchor(message):
    allowed = {'tenant_key', 'recipient_open_id', 'chat_id', 'message_id', 'sender_open_id',
               'parent_id', 'root_id', 'thread_id', 'app_id', 'recipient_tenant_key', 'transport_tenant_key'}
    required = ('tenant_key', 'recipient_open_id', 'chat_id', 'message_id', 'sender_open_id')
    if not isinstance(message, dict) or set(message) - allowed or any(not isinstance(message.get(k), str) or not message[k] for k in required) or any(v is not None and (not isinstance(v, str) or len(v) > 256) for v in message.values()):
        raise ManagementError('invalid_change', 'Only verified scalar message and transport identities are accepted.')
    return required


def _git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True,
                            text=True, env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                                            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
    if result.returncode:
        raise ManagementError('invalid_repository', 'The existing path must be a Git worktree.')
    return result.stdout.strip()


def _repository(value):
    try:
        if not Path(value['repo_path']).is_absolute():
            raise ValueError('Repository path must be absolute.')
        path = Path(value['repo_path']).resolve(strict=True)
        top = Path(_git(path, 'rev-parse', '--show-toplevel')).resolve()
        if top != path:
            raise ValueError('Register the repository root, not a child directory.')
        common = Path(_git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        git_dir = Path(_git(path, 'rev-parse', '--absolute-git-dir')).resolve()
        nested = []
        for current, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d != '.git' and not Path(current, d).is_symlink())
            candidate = Path(current)
            if candidate != path and ('.git' in files or (candidate / '.git').is_dir()):
                nested.append({'worktree': str(candidate.resolve()),
                               'git_dir': str(Path(_git(candidate, 'rev-parse', '--absolute-git-dir')).resolve()),
                               'common_dir': str(Path(_git(candidate, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()),
                               'access': 'read_only'})
        artifacts = []
        protected = [path / '.git', git_dir, common] + [Path(r['worktree']) for r in nested]
        for supplied in value.get('test_artifact_paths', []):
            artifact = Path(supplied).resolve()
            if not Path(supplied).is_absolute() or not artifact.is_relative_to(path) or artifact == path:
                raise ValueError('Test artifacts must be in an explicit subdirectory of the registered worktree.')
            if any(artifact.is_relative_to(p) or p.is_relative_to(artifact) for p in protected):
                raise ValueError('Test artifacts cannot overlap Git metadata or nested read-only repositories.')
            artifacts.append(str(artifact))
        return {'worktree': str(path), 'common_dir': str(common), 'git_dir': str(git_dir),
                'logical_id': str(common), 'nested_repositories': nested,
                'test_artifact_paths': artifacts}
    except (OSError, ValueError, TypeError) as exc:
        raise ManagementError('invalid_repository', str(exc)) from exc


class Manager:
    """One authoritative directory. Callers enter with verified subjects, not claimed roles."""
    def __init__(self, state_dir, *, owner_identity_ref):
        self.owner_identity_ref = owner_identity_ref
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.state_dir / 'manager.sqlite3', check_same_thread=False)
        self._db.execute('CREATE TABLE IF NOT EXISTS directory (id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL)')
        self._db.execute('INSERT OR IGNORE INTO directory VALUES(1, 1, 0, ?)',
                         (json.dumps({'projects': {}, 'profiles': {}, 'last_verified_at': None}),))
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _load(self):
        schema, version, payload = self._db.execute('SELECT schema_version, version, payload FROM directory WHERE id=1').fetchone()
        if schema != 1:
            raise ManagementError('unknown_version', 'Directory schema requires a verified upgrade.')
        data = json.loads(payload)
        data.setdefault('requests', {})
        data.setdefault('clarifications', {})
        return version, data

    def _principal(self, identity, data):
        if not isinstance(identity, VerifiedIdentity) or not identity.source:
            raise ManagementError('unauthorized', 'A verified entry identity is required.')
        if identity.subject == self.owner_identity_ref:
            return None
        profile = next((p for p in data['profiles'].values() if p['identity_ref'] == identity.subject), None)
        if profile is None:
            raise ManagementError('unauthorized', 'Identity is not registered.')
        return profile

    def read_snapshot(self, identity, scope=None):
        with self._lock:
            version, data = self._load()
            principal = self._principal(identity, data)
            projects = list(data['projects'].values())
            profiles = list(data['profiles'].values())
            if principal and principal['role'] != 'steward':
                visible = self._visible_profile_ids(principal, data)
                profiles = [p for p in profiles if p['id'] in visible]
                project_ids = {p['project_id'] for p in profiles if p['project_id'] is not None}
                projects = [p for p in projects if p['id'] in project_ids]
            if scope is not None:
                projects = [p for p in projects if p['id'] == scope]
                profiles = [p for p in profiles if p['project_id'] == scope]
            visible_ids = {p['id'] for p in profiles}
            requests = [r for r in data['requests'].values() if r['profile_id'] in visible_ids]
            return {'status': 'completed', 'version': version, 'last_verified_at': data['last_verified_at'],
                    'projects': projects, 'profiles': profiles, 'requests': requests,
                    'clarifications': [c for c in data['clarifications'].values() if c['profile_id'] in visible_ids], 'runtime': 'directory_available',
                    'execution': 'not_enabled', 'needs_human': ['Execution and channel capabilities are not verified.']}

    def accept_request(self, identity, project_id, profile_id, message, issue):
        """Accept an Issue snapshot from a trusted message entry; never start Codex."""
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            if self._principal(identity, data) is not None:
                raise ManagementError('forbidden', 'New work requires the verified owner.')
            profile = data['profiles'].get(profile_id)
            if project_id not in data['projects'] or not profile or profile['project_id'] != project_id or profile['capability'] != 'development':
                raise ManagementError('invalid_change', 'An explicitly registered development project and responsible Profile are required.')
            required = _message_anchor(message)
            key = hashlib.sha256(json.dumps([message[k] for k in required[:4]]).encode()).hexdigest()
            if key in data['requests']:
                return {'status': 'accepted', 'duplicate': True, 'request': data['requests'][key]}
            if not isinstance(issue, dict) or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*', issue.get('url', '')) or any(not isinstance(issue.get(k), str) or not issue[k] for k in ('title', 'body', 'updated_at')):
                raise ManagementError('invalid_change', 'A verified GitHub Issue snapshot and update time are required.')
            _public_text(issue['title'])
            _public_text(issue['body'])
            record = {'id': key, 'project_id': project_id, 'profile_id': profile_id,
                      'accepted_scope': {k: issue[k] for k in ('url', 'title', 'body', 'updated_at')},
                      'source_anchor': dict(message), 'task_start_anchor': None,
                      'acceptance': 'accepted', 'accepted_at': datetime.now(timezone.utc).isoformat(),
                      'execution': 'waiting', 'unexecuted_reason': 'Codex execution is not enabled.',
                      'delivery': 'pending', 'messages': [], 'outbox': []}
            data['requests'][key] = record
            self._db.execute('UPDATE directory SET version=?, payload=? WHERE id=1', (version + 1, json.dumps(data)))
            return {'status': 'accepted', 'duplicate': False, 'request': record}

    def _request(self, identity, request_id, data):
        principal = self._principal(identity, data)
        record = data['requests'].get(request_id)
        if record is None:
            raise ManagementError('invalid_change', 'Unknown request.')
        if principal and principal['role'] != 'steward' and record['profile_id'] not in self._visible_profile_ids(principal, data):
            raise ManagementError('forbidden', 'Request is outside this responsibility scope.')
        return record

    def associate_message(self, identity, project_id, profile_id, message, text):
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            if self._principal(identity, data) is not None:
                raise ManagementError('forbidden', 'Plain owner input requires the verified owner entry.')
            _message_anchor(message)
            _public_text(text)
            if re.fullmatch(r'(收到|谢谢|感谢|好的|ok|thanks)[。.!！\s]*', text.strip(), re.IGNORECASE):
                return {'status': 'ignored'}
            candidates = [r for r in data['requests'].values() if r['project_id'] == project_id and r['profile_id'] == profile_id
                          and all(r['source_anchor'][k] == message.get(k) for k in ('tenant_key', 'recipient_open_id', 'chat_id', 'sender_open_id'))]
            references = {message.get(k) for k in ('parent_id', 'root_id', 'thread_id')} - {None, ''}
            if references:
                candidates = [r for r in candidates if references & {
                    a.get(k) for a in [r['source_anchor'], r['task_start_anchor'] or {}]
                    for k in ('message_id', 'root_id', 'thread_id')}]
            key = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
            if not candidates:
                return {'status': 'unassociated'}
            if len(candidates) > 1:
                record = data['clarifications'].get(key)
                if record is None:
                    record = {'id': key, 'status': 'needs_clarification', 'project_id': project_id, 'profile_id': profile_id,
                              'source_anchor': dict(message), 'candidate_ids': [r['id'] for r in candidates],
                              'uuid': str(uuid.uuid4()), 'delivery': 'pending'}
                    data['clarifications'][key] = record
                    self._save(version, data)
                return record
            record = candidates[0]
            existing = next((m for m in record['messages'] if m['id'] == key), None)
            if existing:
                return existing
            kind = 'progress' if text.startswith('进度') else 'result' if text.startswith('结果') else 'input'
            associated = {'id': key, 'status': 'associated', 'request_id': record['id'], 'kind': kind,
                          'source_anchor': dict(message), 'text': text}
            record['messages'].append(associated)
            self._save(version, data)
            return associated

    def record_clarification_delivery(self, identity, clarification_id, receipt):
        with self._lock, self._db:
            version, data = self._load()
            if self._principal(identity, data) is not None:
                raise ManagementError('forbidden', 'Only the owner entry may record this clarification.')
            clarification = data['clarifications'][clarification_id]
            clarification['delivery'] = receipt.get('status', 'unknown')
            clarification['receipt'] = receipt
            self._save(version, data)

    def _save(self, version, data):
        self._db.execute('UPDATE directory SET version=?, payload=? WHERE id=1', (version + 1, json.dumps(data)))

    def publish_request_message(self, identity, request_id, kind, text):
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            record = self._request(identity, request_id, data)
            if kind not in {'confirmation', 'material', 'progress', 'result', 'clarification'} or not isinstance(text, str) or not text.strip():
                raise ManagementError('invalid_change', 'An explicit message kind and public material are required.')
            _public_text(text)
            key = hashlib.sha256((kind + ':' + text).encode()).hexdigest()
            existing = next((p for p in record['outbox'] if p['id'] == key), None)
            if existing:
                return existing
            publication = {'id': key, 'kind': kind, 'segments': [
                {'number': index + 1, 'uuid': str(uuid.uuid4()), 'text': text[start:start + 1800],
                 'status': 'pending', 'attempts': []}
                for index, start in enumerate(range(0, len(text), 1800))]}
            record['outbox'].append(publication)
            self._save(version, data)
            return publication

    def claim_delivery(self, identity, request_id):
        """Persist intent before the external send. An interrupted send requires reconciliation."""
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            record = self._request(identity, request_id, data)
            for publication in record['outbox']:
                for segment in publication['segments']:
                    if segment['status'] == 'delivered':
                        continue
                    if segment['status'] == 'sending':
                        segment['status'] = 'unknown'
                        record['delivery'] = 'unknown'
                        self._save(version, data)
                    if segment['status'] != 'pending':
                        return None
                    anchor = record['task_start_anchor'] or record['source_anchor']
                    if publication['kind'] != 'confirmation' and record['task_start_anchor'] is None:
                        return None
                    segment['status'] = 'sending'
                    segment['attempts'].append({'status': 'sending'})
                    self._save(version, data)
                    return {**segment, 'kind': publication['kind'], 'chat_id': anchor['chat_id'],
                            'reply_to': anchor['message_id'], 'thread_id': anchor.get('thread_id'),
                            'mention_open_id': record['source_anchor']['sender_open_id']}
            return None

    def retry_delivery(self, identity, request_id):
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            record = self._request(identity, request_id, data)
            segments = [s for p in record['outbox'] for s in p['segments']]
            if any(s['status'] in {'sending', 'unknown'} for s in segments):
                raise ManagementError('version_conflict', 'Unknown delivery must be reconciled before retrying.')
            failed = [s for s in segments if s['status'] == 'failed']
            if not failed:
                return {'status': 'completed', 'request': record}
            for segment in failed:
                segment['status'] = 'pending'
            record['delivery'] = 'pending'
            self._save(version, data)
            return {'status': 'accepted', 'request': record}

    def record_delivery(self, identity, request_id, segment_uuid, outcome):
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            record = self._request(identity, request_id, data)
            if not isinstance(outcome, dict) or outcome.get('status') not in {'delivered', 'failed', 'unknown'} or set(outcome) - {'status', 'code', 'message_id', 'chat_id', 'root_id', 'parent_id', 'thread_id'}:
                raise ManagementError('invalid_change', 'A scalar delivery receipt is required.')
            found = next(((p, s) for p in record['outbox'] for s in p['segments'] if s['uuid'] == segment_uuid), None)
            if not found or found[1]['status'] != 'sending':
                raise ManagementError('version_conflict', 'There is no matching in-flight delivery.')
            publication, segment = found
            receipt = dict(outcome)
            if receipt['status'] == 'delivered' and (not receipt.get('message_id') or receipt.get('chat_id') != record['source_anchor']['chat_id']):
                receipt['status'] = 'unknown'
            segment['status'] = receipt['status']
            segment['attempts'][-1] = receipt
            if receipt['status'] == 'delivered' and publication['kind'] == 'confirmation' and segment['number'] == 1 and record['task_start_anchor'] is None:
                record['task_start_anchor'] = {k: receipt.get(k) for k in ('message_id', 'chat_id', 'root_id', 'parent_id', 'thread_id')}
            statuses = [s['status'] for p in record['outbox'] for s in p['segments']]
            record['delivery'] = 'unknown' if 'unknown' in statuses else 'failed' if 'failed' in statuses else 'delivered' if all(s == 'delivered' for s in statuses) else 'pending'
            self._save(version, data)
            return {'status': 'completed', 'request': record}

    def _visible_profile_ids(self, principal, data):
        visible = {principal['id']}
        if principal['role'] == 'project_lead':
            while True:
                expanded = visible | {p['id'] for p in data['profiles'].values() if p.get('parent_profile_id') in visible}
                if expanded == visible:
                    break
                visible = expanded
        return visible

    def _authorize_change(self, principal, kind, candidate, data):
        if principal is None:
            return
        old = data[kind + 's'].get(candidate['id'])
        if old is None or principal['role'] in {'subproject_lead', 'independent'}:
            raise ManagementError('forbidden', 'New identities and bindings require the verified owner.')
        visible = self._visible_profile_ids(principal, data)
        if kind == 'project':
            project_ids = {p['project_id'] for p in data['profiles'].values() if p['id'] in visible}
            if principal['role'] != 'steward' and candidate['id'] not in project_ids:
                raise ManagementError('forbidden', 'Project is outside the registered responsibility scope.')
            if candidate['repo'] != old['repo']:
                raise ManagementError('forbidden', 'Repository boundary changes require the verified owner.')
        else:
            if principal['role'] != 'steward' or any(candidate.get(k) != old.get(k) for k in candidate if k != 'parent_profile_id'):
                raise ManagementError('forbidden', 'Only the steward may transfer an existing Profile; identity decisions require the owner.')

    def _validate_project(self, value):
        if not isinstance(value, dict) or set(value) - {'id', 'name', 'repo_path', 'test_artifact_paths'}:
            raise ManagementError('invalid_change', 'Unknown project fields.')
        if any(not isinstance(value.get(k), str) or not value[k].strip() for k in ('id', 'name', 'repo_path')):
            raise ManagementError('invalid_change', 'Project id, name and existing repository are required.')
        if not isinstance(value.get('test_artifact_paths', []), list):
            raise ManagementError('invalid_change', 'Test artifact paths must be explicit local paths.')

    def _validate_profile(self, value, data):
        allowed = {'id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs'}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ManagementError('invalid_change', 'Unknown Profile fields; credentials must stay in native secret storage.')
        if any(not isinstance(value.get(k), str) or not value[k].strip() for k in ('id', 'native_profile', 'identity_ref')):
            raise ManagementError('invalid_change', 'Stable Profile, native Profile and verified identity references are required.')
        if value.get('role') not in {'steward', 'project_lead', 'subproject_lead', 'independent'}:
            raise ManagementError('invalid_change', 'Unknown responsibility role.')
        if value.get('capability') not in {'development', 'non_development'}:
            raise ManagementError('invalid_change', 'Unknown capability classification.')
        if value['role'] in {'steward', 'independent'}:
            if value.get('project_id') is not None or value.get('parent_profile_id') is not None:
                raise ManagementError('invalid_change', 'Global and independent assistants remain outside the project tree.')
        elif value.get('project_id') not in data['projects']:
            raise ManagementError('invalid_change', 'Project Profile requires a registered project.')
        parent = value.get('parent_profile_id')
        if parent is not None:
            if parent == value['id'] or parent not in data['profiles'] or data['profiles'][parent]['role'] != 'project_lead':
                raise ManagementError('invalid_change', 'Parent must be a registered project lead.')
        if value['role'] == 'subproject_lead' and parent is None:
            raise ManagementError('invalid_change', 'Subproject lead requires its project lead.')
        profiles = {**data['profiles'], value['id']: value}
        for profile in profiles.values():
            parent_id = profile.get('parent_profile_id')
            if profile['role'] == 'subproject_lead':
                lead = profiles.get(parent_id)
                if lead is None or lead['role'] != 'project_lead' or lead.get('parent_profile_id') is not None:
                    raise ManagementError('invalid_change', 'Subproject lead requires a root project lead; existing children must remain valid.')
            elif parent_id is not None:
                raise ManagementError('invalid_change', 'Only subproject leads have a parent; project leads cannot form cycles or extra responsibility layers.')
        for profile in data['profiles'].values():
            if profile['id'] != value['id'] and (profile['native_profile'] == value['native_profile'] or profile['identity_ref'] == value['identity_ref']):
                raise ManagementError('binding_conflict', 'Native Profile and identity already belong to another Profile.')
        if value['identity_ref'] == self.owner_identity_ref:
            raise ManagementError('invalid_change', 'A bot identity cannot reuse the owner identity.')
        refs = value.get('connection_refs', {})
        if not isinstance(refs, dict) or set(refs) - {'bot', 'credential', 'codex'}:
            raise ManagementError('invalid_change', 'Use non-sensitive bot, native credential and local service references.')
        if any(not isinstance(ref, str) or not re.fullmatch(r'(native|local|identity):[A-Za-z0-9_.:/-]+', ref) for ref in refs.values()):
            raise ManagementError('invalid_change', 'Only native/local/identity references are accepted, never secret values.')
        if 'credential' in refs and not refs['credential'].startswith('native:'):
            raise ManagementError('invalid_change', 'Credential must reference native secret management.')
        if 'codex' in refs and not refs['codex'].startswith('local:'):
            raise ManagementError('invalid_change', 'Only local execution service references are supported.')

    def apply_directory_change(self, identity, expected_version, change):
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            version, data = self._load()
            principal = self._principal(identity, data)
            if version != expected_version:
                raise ManagementError('version_conflict', 'Directory changed; read the current version first.')
            if not isinstance(change, dict) or set(change) - {'project', 'profile'} or not change:
                raise ManagementError('invalid_change', 'Expected project and/or profile changes.')
            if 'project' in change:
                value = change['project']
                self._validate_project(value)
                candidate = {'id': value['id'], 'name': value['name'], 'repo': _repository(value)}
                self._authorize_change(principal, 'project', candidate, data)
                data['projects'][value['id']] = candidate
            if 'profile' in change:
                self._validate_profile(change['profile'], data)
                value = dict(change['profile'])
                value.setdefault('project_id', None)
                value.setdefault('parent_profile_id', None)
                value.setdefault('connection_refs', {})
                existing = data['profiles'].get(value['id'])
                if existing and existing['project_id'] != value['project_id']:
                    raise ManagementError('binding_conflict', 'Profile has a long-term project binding; create a new Profile.')
                value.update(lifecycle='configuring', can_execute=False,
                             capabilities={'execution': {'enabled': False, 'reason': 'Not verified by an execution adapter.'}})
                self._authorize_change(principal, 'profile', value, data)
                data['profiles'][value['id']] = value
            data['last_verified_at'] = datetime.now(timezone.utc).isoformat()
            self._db.execute('UPDATE directory SET version=?, payload=? WHERE id=1', (version + 1, json.dumps(data)))
            return {'status': 'completed', 'version': version + 1, 'last_verified_at': data['last_verified_at'],
                    'needs_human': ['Verify native Profile, new bot identity, connections and execution capabilities.']}
