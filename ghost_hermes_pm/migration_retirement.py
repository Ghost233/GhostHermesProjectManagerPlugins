"""Migration-owned single old entry retirement, independent of project subtree archive."""
from .manager import ManagementError
from .migration import _now, _save, digest
from .lifecycle import COMPONENTS, _verified_fact


def _save_intent(manager, operation, source, entry):
    version, data = manager._load()
    current = data['profiles'][source['id']]
    gate = current.get('migration_gate')
    if gate and gate['plan_id'] != operation['id']:
        raise ManagementError('binding_conflict', 'Another reviewed migration owns this original entry.')
    current.update(lifecycle='archiving', can_execute=False,
                   migration_gate={'plan_id': operation['id'], 'state': 'retiring_old_entry', 'operation_id': entry['id']})
    operation['old_entry'] = entry
    data['migration_plans'][operation['id']] = operation
    with manager._db:
        manager._save(version, data)


def reconcile(manager, identity, operation, approval, handled=()):
    _, data = manager._load()
    source = data['profiles'][operation['plan']['source_profile_id']]
    expected_ids = approval.get('expected_old_profile_ids')
    if expected_ids != [source['id']]:
        raise ManagementError('invalid_change', 'Retiring a named old entry requires the originally reviewed single old Profile list.')
    entry = operation.get('old_entry')
    if entry is None:
        entry = {'id': approval['archive_operation_id'], 'profile_id': source['id'], 'profile_ids': [source['id']],
            'approved_scope': {'expected_version': approval['expected_version'], 'expected_profile_ids': expected_ids},
            'owner_origin': operation['owner_origin'], 'status': 'processing', 'entry_requests': {}, 'checks': {},
            'manual_required': [], 'manual_handled': [], 'needs_human': [], 'created_at': _now()}
        _save_intent(manager, operation, source, entry)
    elif entry['id'] != approval['archive_operation_id'] or entry['profile_ids'] != expected_ids:
        raise ManagementError('binding_conflict', 'Original single-entry retirement ID or scope changed.')
    if not isinstance(handled, (list, tuple)) or any(not isinstance(i, str) or not i for i in handled):
        raise ManagementError('invalid_change', 'Name exact observed manual execution IDs handled by the Owner.')
    if set(handled) - set(entry['manual_required']):
        raise ManagementError('invalid_change', 'Owner handling must name the original persisted observed execution inventory.')
    checks, needs = {}, []
    task_checks = []
    for record in list(data['requests'].values()):
        if record['profile_id'] != source['id'] or record.get('repository_released'):
            continue
        try:
            if not record.get('session'):
                version, current = manager._load()
                queued = current['requests'][record['id']]
                queued.update(execution='stopped', outer_task_status='stopped', repository_released=True,
                    archive_stop_intent=entry['id'], unexecuted_reason='Owner single-entry migration cancelled unstarted work.')
                queued.get('queue', {}).pop('requested_by', None)
                queued.get('queue', {}).pop('pending_continuation', None)
                with manager._db:
                    manager._save(version, current)
                result = queued
            else:
                if not record.get('stop'):
                    manager.control_task(identity, record['id'], 'stop', 'migration-stop-' + digest([entry['id'], record['id']]),
                                         expected_turn_id=record['session'].get('turn_id'))
                result = manager.refresh_task(identity, record['id'])
            task_checks.append({'request_id': record['id'], 'status': 'verified' if result.get('repository_released') and result.get('outer_task_status') == 'stopped' else 'processing',
                'stop': result.get('stop'), 'evidence': result.get('related_execution', [])})
        except ManagementError as exc:
            task_checks.append({'request_id': record['id'], 'status': 'blocked', 'reason': str(exc)})
    checks['task_execution'] = task_checks
    if source['project_id']:
        manager.refresh_manual_sessions(identity, source['project_id'])
    _, data = manager._load()
    controlled = {g['manual_session_id'] for g in data.get('control_grants', {}).values() if g.get('status') == 'active'}
    observed = [s for s in data.get('manual_sessions', {}).values() if source['project_id'] in s['project_ids'] and s['id'] not in controlled]
    required = set(entry['manual_required']) | {s['id'] for s in observed if s['state'] != 'inactive_verified'}
    for component in (*COMPONENTS, 'manual_execution'):
        key = source['id'] + ':' + component
        expected = {'profile_id': source['id'], 'native_profile': source['native_profile'], 'project_id': source['project_id'],
            'component': component, 'operation_id': entry['id'], 'scope': 'profile', 'status': 'verified'}
        try:
            host = manager.lifecycle_host
            if host is None:
                raise ManagementError('capability_unverified', 'Current native single-Profile host control is unavailable.')
            if component != 'manual_execution' and key not in entry['entry_requests']:
                proof = _verified_fact(manager, host.capability(source, component, 'stopped', entry['id']), {**expected, 'action': 'stopped'})
                evidence = proof['evidence']
                ids = evidence.get('manual_execution_ids', []) if isinstance(evidence, dict) else []
                if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
                    raise ManagementError('capability_unverified', 'Native observed execution inventory is malformed.')
                required.update(ids)
                entry['manual_required'] = sorted(required)
                entry['entry_requests'][key] = {'status': 'intent', 'capability': proof, 'requested_at': _now()}
                _save(manager, operation)
                try:
                    answer = host.request(source, component, 'stopped', entry['id'])
                    entry['entry_requests'][key]['status'] = answer.get('status') if isinstance(answer, dict) and answer.get('status') in {'accepted', 'rejected', 'outcome_unknown'} else 'outcome_unknown'
                except (ManagementError, OSError, TimeoutError, ValueError, TypeError):
                    entry['entry_requests'][key]['status'] = 'outcome_unknown'
                _save(manager, operation)
            fact = _verified_fact(manager, host.inspect(source, component, 'stopped', entry['id']), expected)
            if fact.get('state') != 'stopped' or component == 'manual_execution' and fact.get('execution_coverage') != 'complete':
                raise ManagementError('capability_unverified', 'Original related execution termination is incomplete; unserve is not termination.')
            checks[key] = fact
        except (ManagementError, OSError, KeyError, TypeError, ValueError) as exc:
            checks[key] = {'status': 'blocked', 'reason': str(exc)}
    if set(handled) - required:
        raise ManagementError('invalid_change', 'Owner handling must name an original observed execution requiring action.')
    entry['manual_required'] = sorted(required)
    entry['manual_handled'] = sorted(set(entry['manual_handled']) | set(handled))
    checks['observed_manual'] = [{'session_id': s['id'], 'control': 'observe_only', 'state': s['state'],
        'status': 'verified' if s['state'] == 'inactive_verified' and (s['id'] not in required or s['id'] in entry['manual_handled']) else 'blocked'} for s in observed]
    if required - set(entry['manual_handled']):
        needs.append('Owner must explicitly handle original observed executions: ' + ', '.join(sorted(required - set(entry['manual_handled']))))
    flat = [item for value in checks.values() for item in (value if isinstance(value, list) else [value])]
    complete = all(c['status'] == 'verified' for c in flat) and not needs
    entry.update(checks=checks, status='completed' if complete else 'blocked', needs_human=needs + [c.get('reason', 'Original stop remains unverified.') for c in flat if c['status'] != 'verified'], verified_at=_now())
    if complete:
        version, current = manager._load()
        current['profiles'][source['id']].update(lifecycle='archived', can_execute=False)
        current['migration_plans'][operation['id']] = operation
        with manager._db:
            manager._save(version, current)
    else:
        _save(manager, operation)
    return entry
