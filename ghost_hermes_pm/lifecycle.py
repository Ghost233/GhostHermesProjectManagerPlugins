"""Durable Owner lifecycle intent and independently verified Profile boundaries."""
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from .manager import ManagementError, _public_text

COMPONENTS = ('profile_service', 'bot', 'scheduled_entry')


def _now():
    return datetime.now(timezone.utc).isoformat()


def require_active(data, profile_id, project_id):
    for obj in (data['profiles'].get(profile_id, {}), data['projects'].get(project_id, {})):
        if obj.get('lifecycle') not in {None, 'configuring', 'active'} or obj.get('archive_intent') not in (None, False):
            raise ManagementError('lifecycle_blocked', 'Project lifecycle prevents new work or continuation; reconcile the Owner intent.')
    if data.get('maintenance_mode') not in (None, False):
        raise ManagementError('lifecycle_blocked', 'Manager lifecycle maintenance prevents new work.')


def _save(manager, operation):
    version, data = manager._load()
    data.setdefault('lifecycle_operations', {})[operation['id']] = operation
    with manager._db:
        manager._save(version, data)


def _scope_binding(data, profile_id):
    profile = data['profiles'][profile_id]
    return {k: profile.get(k) for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')} | {
        'repository_fingerprint': hashlib.sha256(json.dumps(data['projects'][profile['project_id']]['repo'], sort_keys=True).encode()).hexdigest()}


def _checkpoint(manager, operation):
    directory = manager.state_dir / 'lifecycle-checkpoints'
    if directory != directory.resolve():
        raise ManagementError('unavailable', 'Lifecycle checkpoint directory is an unknown alias.')
    directory.mkdir(exist_ok=True)
    artifact = directory / (hashlib.sha256(operation['id'].encode()).hexdigest() + '.sqlite3')
    if artifact.exists() or artifact.is_symlink():
        raise ManagementError('unavailable', 'An incomplete checkpoint artifact needs Owner reconciliation; it was preserved.')
    with sqlite3.connect(artifact) as target:
        manager._db.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ManagementError('unavailable', 'Lifecycle directory checkpoint integrity is unverified.')
    return {'status': 'verified_manager_directory', 'artifact_ref': str(artifact.relative_to(manager.state_dir)),
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'verified_at': _now(),
            'external_state': 'not_backed_up', 'excluded': ['native_profiles', 'bots', 'scheduled_entries', 'source_archives', 'notification-health.json']}


def _verified_fact(manager, fact, expected):
    if not isinstance(fact, dict) or set(fact) - set(expected) - {'state', 'execution_coverage', 'evidence', 'verified_at'} or any(fact.get(k) != v for k, v in expected.items()) or not fact.get('evidence'):
        raise ManagementError('capability_unverified', 'Current Profile component scope or evidence is incomplete.')
    _public_text(json.dumps(fact), manager._sensitive_values())
    at = datetime.fromisoformat(fact['verified_at'])
    age = (datetime.now(timezone.utc) - at).total_seconds()
    if not at.tzinfo or age < -5 or age > 300:
        raise ManagementError('capability_unverified', 'Profile component evidence is stale.')
    return fact


def operate(manager, identity, action, details):
    if action not in {'archive', 'check', 'restore'} or not isinstance(details, dict) or set(details) - {'operation_id', 'profile_id', 'handled_manual_session_ids', 'expected_version', 'expected_profile_ids'} or not isinstance(details.get('operation_id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', details['operation_id']):
        raise ManagementError('invalid_change', 'Select an explicit lifecycle action, stable operation ID and registered Profile.')
    approved = None
    if action != 'check':
        ids = details.get('expected_profile_ids')
        if type(details.get('expected_version')) is not int or details['expected_version'] < 0 or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ManagementError('invalid_change', 'Lifecycle confirmation requires the originally reviewed directory version and explicit Profile scope.')
        approved = {'expected_version': details['expected_version'], 'expected_profile_ids': sorted(ids)}
    elif 'expected_version' in details or 'expected_profile_ids' in details:
        raise ManagementError('invalid_change', 'Check reconciles the already frozen operation; it cannot approve a new Profile scope.')
    handled = details.get('handled_manual_session_ids', [])
    if not isinstance(handled, list) or any(not isinstance(i, str) for i in handled) or action != 'check' and handled:
        raise ManagementError('invalid_change', 'Owner manual handling belongs to an explicit archive check.')
    with manager._lock:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Lifecycle changes require the verified Owner; responsibility is not new authorization.')
        operations = data.setdefault('lifecycle_operations', {})
        operation = operations.get(details['operation_id'])
        if action == 'check':
            if operation is None or details.get('profile_id') not in (None, operation['profile_id']):
                raise ManagementError('binding_conflict', 'Check must identify the original lifecycle operation.')
        elif operation:
            if operation['action'] != action or operation['profile_id'] != details.get('profile_id') or operation.get('approved_scope') != approved:
                raise ManagementError('binding_conflict', 'Lifecycle operation ID already names another Owner decision.')
            return operation
        else:
            if version != approved['expected_version']:
                raise ManagementError('version_conflict', 'Directory changed after lifecycle preview; review the new explicit scope before confirmation.')
            profile = data['profiles'].get(details.get('profile_id'))
            if not profile or profile['role'] not in {'project_lead', 'subproject_lead'}:
                raise ManagementError('invalid_change', 'Lifecycle target must be a registered project responsible Profile.')
            ids = [profile['id']]
            if action == 'archive':
                require_active(data, profile['id'], profile['project_id'])
                ids += [p['id'] for p in data['profiles'].values() if p.get('parent_profile_id') == profile['id']]
            else:
                if profile.get('lifecycle') != 'archived':
                    raise ManagementError('binding_conflict', 'Restore requires a completed archive.')
                parent = data['profiles'].get(profile.get('parent_profile_id'))
                if parent and parent.get('lifecycle') != 'active':
                    raise ManagementError('lifecycle_blocked', 'Restore the project lead before each child.')
            project_ids = sorted({data['profiles'][i]['project_id'] for i in ids})
            if sorted(ids) != approved['expected_profile_ids']:
                raise ManagementError('binding_conflict', 'Current lifecycle subtree differs from the originally reviewed Profile scope; nothing was changed.')
            if any(o['status'] != 'completed' and set(o['profile_ids']) & set(ids) for o in operations.values()):
                raise ManagementError('binding_conflict', 'An earlier lifecycle decision still requires reconciliation.')
            operation = {'id': details['operation_id'], 'action': action, 'profile_id': profile['id'],
                'profile_ids': ids, 'project_ids': project_ids, 'status': 'processing', 'created_at': _now(),
                'owner_origin': {'subject': identity.subject, 'source': identity.source}, 'checks': {},
                'scope_bindings': {i: _scope_binding(data, i) for i in ids},
                'approved_scope': approved,
                'entry_requests': {}, 'manual_required': [], 'manual_handled': [], 'needs_human': [], 'evidence': []}
            operations[operation['id']] = operation
            intent = {'operation_id': operation['id'], 'action': action, 'owner_origin': operation['owner_origin']}
            for i in ids:
                data['profiles'][i].update(lifecycle='archiving' if action == 'archive' else 'restoring', archive_intent=intent, can_execute=False)
            for i in project_ids:
                data['projects'][i].update(lifecycle='archiving' if action == 'archive' else 'restoring', archive_intent=intent)
            with manager._db:
                manager._save(version, data)
        if operation['status'] == 'completed':
            return operation
        return _check(manager, identity, operation, handled)


def _check(manager, identity, operation, handled):
    state = 'stopped' if operation['action'] == 'archive' else 'ready'
    checks, needs = {}, []
    _, data = manager._load()
    if operation.get('scope_bindings') != {i: _scope_binding(data, i) for i in operation['profile_ids']}:
        operation.update(status='blocked', needs_human=['Original lifecycle Profile, responsibility or repository binding changed.'],
                         checks={'directory_scope': {'status': 'blocked', 'reason': 'Original lifecycle binding changed.'}})
        _save(manager, operation)
        return operation
    if state == 'ready' and not operation.get('checkpoint'):
        try:
            operation['checkpoint'] = _checkpoint(manager, operation)
        except (OSError, sqlite3.Error, ManagementError) as exc:
            operation.update(status='blocked', needs_human=[str(exc)])
            _save(manager, operation)
            return operation
        _save(manager, operation)
    if state == 'stopped':
        task_checks = []
        for record in list(data['requests'].values()):
            if record['profile_id'] not in operation['profile_ids'] or record.get('repository_released'):
                continue
            try:
                if not record.get('session'):
                    version, current = manager._load()
                    queued = current['requests'][record['id']]
                    queued.update(execution='stopped', outer_task_status='stopped', repository_released=True,
                                  archive_stop_intent=operation['id'], unexecuted_reason='Owner project archive cancelled unstarted work.')
                    queued.get('queue', {}).pop('requested_by', None)
                    queued.get('queue', {}).pop('pending_continuation', None)
                    with manager._db:
                        manager._save(version, current)
                    result = queued
                else:
                    if not record.get('stop'):
                        stop_id = 'archive-stop-' + hashlib.sha256((operation['id'] + record['id']).encode()).hexdigest()
                        manager.control_task(identity, record['id'], 'stop', stop_id, expected_turn_id=record['session'].get('turn_id'))
                    result = manager.refresh_task(identity, record['id'])
                verified = result.get('repository_released') is True and result.get('outer_task_status') == 'stopped'
                task_checks.append({'request_id': record['id'], 'status': 'verified' if verified else 'processing',
                                    'stop': result.get('stop'), 'evidence': result.get('related_execution', [])})
            except ManagementError as exc:
                task_checks.append({'request_id': record['id'], 'status': 'blocked', 'reason': str(exc)})
        checks['task_execution'] = task_checks
        for project in operation['project_ids']:
            manager.refresh_manual_sessions(identity, project)
        _, data = manager._load()
        managed_manual = {g['manual_session_id'] for g in data.get('control_grants', {}).values() if g.get('status') == 'active'}
        sessions = [s for s in data.get('manual_sessions', {}).values() if set(s['project_ids']) & set(operation['project_ids']) and s['id'] not in managed_manual]
        required = set(operation['manual_required']) | {s['id'] for s in sessions if s['state'] != 'inactive_verified'}
        if set(handled) - required:
            raise ManagementError('invalid_change', 'Manual handling must name an observed session requiring Owner action.')
        operation['manual_required'] = sorted(required)
        operation['manual_handled'] = sorted(set(operation['manual_handled']) | set(handled))
        checks['observed_manual'] = [{'session_id': s['id'], 'status': 'verified' if s['state'] == 'inactive_verified' and (s['id'] not in required or s['id'] in operation['manual_handled']) else 'blocked',
                                      'control': 'observe_only', 'state': s['state']} for s in sessions]
        if required - set(operation['manual_handled']):
            needs.append('Owner must explicitly handle observed manual execution: ' + ', '.join(sorted(required - set(operation['manual_handled']))))
    for profile_id in operation['profile_ids']:
        profile = data['profiles'][profile_id]
        for component in (*COMPONENTS, 'manual_execution'):
            key = profile_id + ':' + component
            try:
                if manager.lifecycle_host is None:
                    raise ManagementError('capability_unverified', 'No trusted native Profile lifecycle host verifies this component.')
                expected = {'profile_id': profile_id, 'native_profile': profile['native_profile'], 'project_id': profile['project_id'],
                            'component': component, 'operation_id': operation['id'], 'scope': 'profile', 'status': 'verified'}
                if component != 'manual_execution' and key not in operation['entry_requests']:
                    capability = getattr(manager.lifecycle_host, 'capability', None)
                    if not callable(capability):
                        raise ManagementError('capability_unverified', 'A current native Profile scoped control capability is required before requesting entry changes.')
                    proof = _verified_fact(manager, capability(profile, component, state, operation['id']), {**expected, 'action': state})
                    operation['entry_requests'][key] = {'status': 'intent', 'requested_at': _now(), 'capability': proof}
                    _save(manager, operation)
                    try:
                        response = manager.lifecycle_host.request(profile, component, state, operation['id'])
                        if not isinstance(response, dict) or response.get('status') not in {'accepted', 'rejected', 'outcome_unknown'}:
                            raise ManagementError('outcome_unknown', 'Unrecognized native lifecycle acknowledgement.')
                        operation['entry_requests'][key].update(status=response['status'])
                    except (ManagementError, OSError, TimeoutError, ValueError, TypeError):
                        operation['entry_requests'][key].update(status='outcome_unknown')
                    _save(manager, operation)
                component_state = 'stopped' if component == 'manual_execution' else state
                fact = manager.lifecycle_host.inspect(profile, component, component_state, operation['id'])
                _verified_fact(manager, fact, expected)
                if component == 'manual_execution' and fact.get('execution_coverage') != 'complete':
                    raise ManagementError('capability_unverified', 'Current original manual execution coverage is incomplete.')
                checks[key] = fact if fact.get('state') == component_state else {**fact, 'status': 'processing', 'reason': 'Requested state has not been verified.'}
            except (ManagementError, OSError, KeyError, TypeError, ValueError) as exc:
                checks[key] = {'status': 'blocked', 'reason': str(exc)}
    flat = [item for v in checks.values() for item in (v if isinstance(v, list) else [v])]
    complete = not needs and all(c['status'] == 'verified' for c in flat)
    operation.update(checks=checks, status='completed' if complete else 'blocked' if any(c['status'] == 'blocked' for c in flat) else 'processing', verified_at=_now(), needs_human=needs)
    if not complete:
        operation['needs_human'] += [key + ': ' + str(v.get('reason', 'Verification pending.')) for key, v in checks.items() if isinstance(v, dict) and v['status'] != 'verified']
    version, data = manager._load()
    data.setdefault('lifecycle_operations', {})[operation['id']] = operation
    if complete:
        operation['completed_at'] = _now()
        for i in operation['profile_ids']:
            data['profiles'][i].update(lifecycle='archived' if state == 'stopped' else 'active', archive_intent=data['profiles'][i]['archive_intent'] if state == 'stopped' else False)
        for i in operation['project_ids']:
            data['projects'][i].update(lifecycle='archived' if state == 'stopped' else 'active', archive_intent=data['projects'][i]['archive_intent'] if state == 'stopped' else False)
        event = {'id': 'lifecycle:' + operation['id'] + ':completed', 'kind': 'archive_completed' if state == 'stopped' else 'profile_restored',
                 'operation_id': operation['id'], 'profile_id': operation['profile_id'], 'profile_ids': operation['profile_ids'],
                 'project_ids': operation['project_ids'], 'verified_at': operation['completed_at']}
        operation['event'] = event
        data.setdefault('lifecycle_events', {})[event['id']] = event
    with manager._db:
        manager._save(version, data)
    return operation
