"""Durable Owner maintenance intent at the common management boundary."""
from datetime import datetime, timezone
import re

from .manager import ManagementError


def _now():
    return datetime.now(timezone.utc).isoformat()


def snapshot(manager, data, principal):
    return {'mode': data.get('maintenance_mode', {}).get('intent', 'active') if isinstance(data.get('maintenance_mode'), dict) else 'active',
            'plans': list(data.get('maintenance_plans', {}).values()) if principal is None else [],
            'runtime': data.get('maintenance_runtime', {'status': 'unverified', 'release_verified': False}),
            'events': list(data.get('maintenance_events', {}).values()) if principal is None else []}


def operate(manager, identity, action, details):
    if action != 'enter' or not isinstance(details, dict) or set(details) - {'operation_id', 'expected_version', 'expected_profile_ids'} or not isinstance(details.get('operation_id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', details['operation_id']):
        raise ManagementError('invalid_change', 'Select a stable maintenance operation ID and reviewed current scope.')
    with manager._lock, manager._db:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Maintenance requires the verified Owner entry.')
        approved = {'expected_version': details.get('expected_version'), 'expected_profile_ids': details.get('expected_profile_ids')}
        existing = data.setdefault('maintenance_plans', {}).get(details['operation_id'])
        if existing:
            if existing['approved_scope'] != approved:
                raise ManagementError('binding_conflict', 'Operation ID already belongs to another reviewed decision.')
            return existing
        if type(approved['expected_version']) is not int or approved['expected_version'] != version:
            raise ManagementError('version_conflict', 'Review the current directory version before maintenance.')
        if approved['expected_profile_ids'] != sorted(data['profiles']):
            raise ManagementError('binding_conflict', 'Review the complete current Profile scope before maintenance.')
        if data.get('maintenance_mode') not in (None, False):
            raise ManagementError('binding_conflict', 'An earlier maintenance intent still requires reconciliation.')
        plan = {'id': details['operation_id'], 'intent': 'maintenance', 'approved_scope': approved,
                'owner_origin': {'subject': identity.subject, 'source': identity.source}, 'created_at': _now(),
                'status': 'maintenance', 'needs_human': ['Verify no active turns, related execution, manual execution or in-flight requests before checkpoint and switch.'],
                'checks': {}}
        data['maintenance_plans'][plan['id']] = plan
        data['maintenance_mode'] = {'operation_id': plan['id'], 'intent': plan['intent']}
        manager._save(version, data)
        return plan
