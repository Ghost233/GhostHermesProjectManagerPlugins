"""Committed public directory/task views without reconciliation or external effects."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def principal(identity, data, owner):
    from .manager import ManagementError, VerifiedIdentity
    if not isinstance(identity, VerifiedIdentity) or not identity.source:
        raise ManagementError('unauthorized', 'A verified entry identity is required.')
    if identity.subject == owner:
        return None
    profile = next((p for p in data['profiles'].values() if p['identity_ref'] == identity.subject), None)
    if profile is None:
        raise ManagementError('unauthorized', 'Identity is not registered.')
    return profile


def visible_profile_ids(actor, data):
    visible = {actor['id']}
    if actor['role'] == 'project_lead':
        while True:
            expanded = visible | {p['id'] for p in data['profiles'].values() if p.get('parent_profile_id') in visible}
            if expanded == visible:
                break
            visible = expanded
    return visible


def directory_view(identity, owner, data, scope=None):
    actor = principal(identity, data, owner)
    projects, profiles = list(data['projects'].values()), list(data['profiles'].values())
    if actor and actor['role'] != 'steward':
        visible = visible_profile_ids(actor, data)
        profiles = [p for p in profiles if p['id'] in visible]
        project_ids = {p['project_id'] for p in profiles if p['project_id'] is not None}
        projects = [p for p in projects if p['id'] in project_ids]
    if scope is not None:
        projects = [p for p in projects if p['id'] == scope]
        profiles = [p for p in profiles if p['project_id'] == scope]
    visible = {p['id'] for p in profiles}
    requests = [r for r in data.get('requests', {}).values() if r['profile_id'] in visible]
    return actor, projects, profiles, requests, visible


class SnapshotReader:
    """Read committed business data using the trusted entry's normal identity policy."""
    def __init__(self, state_dir, *, owner_identity_ref, sensitive_values=()):
        self.state_dir = Path(state_dir).resolve()
        self.owner_identity_ref = owner_identity_ref
        self.sensitive_values = sensitive_values if callable(sensitive_values) else lambda: tuple(sensitive_values)

    def read_snapshot(self, identity, scope=None):
        from .manager import ManagementError, _public_text
        path = self.state_dir / 'manager.sqlite3'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as database:
            schema, version, payload = database.execute('SELECT schema_version, version, payload FROM directory WHERE id=1').fetchone()
        if schema != 2:
            raise ManagementError('unknown_version', 'Directory schema requires a verified upgrade.')
        data = json.loads(payload)
        if data.get('executor_engine') != 'dsh':
            raise ManagementError('unknown_version', 'The committed directory is not registered for DSH; existing material was preserved.')
        _, projects, profiles, requests, _ = directory_view(identity, self.owner_identity_ref, data, scope)
        for request in requests:
            if request.get('execution_capability'):
                request['execution_capability'] = {**request['execution_capability'], 'enabled': False, 'status': 'unverified',
                    'reason': 'Committed observation does not verify current execution capability.'}
        def public(value):
            if isinstance(value, str):
                for secret in self.sensitive_values():
                    if secret:
                        value = value.replace(secret, '[redacted]')
                if value.strip():
                    try:
                        _public_text(value)
                    except ManagementError:
                        return '[redacted]'
                return value
            if isinstance(value, list):
                return [public(item) for item in value]
            if isinstance(value, dict):
                return {key: public(item) for key, item in value.items()}
            return value
        return public({'status': 'committed_snapshot', 'consistency': 'committed_only',
            'observed_at': datetime.now(timezone.utc).isoformat(), 'version': version,
            'last_verified_at': data.get('last_verified_at'), 'projects': projects, 'profiles': profiles,
            'requests': requests, 'execution': 'unverified', 'needs_human': ['Current runtime and control are not verified by a committed snapshot.']})
