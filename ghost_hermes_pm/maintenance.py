"""Durable Owner maintenance intent at the common management boundary."""
from datetime import datetime, timezone
import re
import hashlib
import json
import sqlite3

from .manager import ManagementError, _public_text


def _now():
    return datetime.now(timezone.utc).isoformat()


def snapshot(manager, data, principal):
    try:
        runtime = {**_runtime(manager), 'release_verified': False, 'capability_release': 'requires_all_approved_real_acceptance'}
    except (ManagementError, OSError) as exc:
        runtime = {**data.get('maintenance_runtime', {}), 'status': 'unverified', 'loaded': False, 'release_verified': False,
                   'reason': str(exc), 'plugin_version': data.get('maintenance_runtime', {}).get('plugin_version', 'unknown')}
    return {'permissions': {'status': 'verified', 'can_manage': principal is None}, 'mode': data.get('maintenance_mode', {}).get('intent', 'active') if isinstance(data.get('maintenance_mode'), dict) else 'active',
            'plans': list(data.get('maintenance_plans', {}).values()) if principal is None else [],
            'runtime': runtime,
            'events': list(data.get('maintenance_events', {}).values()) if principal is None else []}


def operate(manager, identity, action, details):
    if action not in {'enter', 'deactivate', 'check', 'checkpoint', 'reenable', 'switch', 'rollback'} or not isinstance(details, dict) or set(details) - {'operation_id', 'expected_version', 'expected_profile_ids', 'expected_release', 'target_release', 'handled_manual_session_ids'} or not isinstance(details.get('operation_id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', details['operation_id']):
        raise ManagementError('invalid_change', 'Select a stable maintenance operation ID and reviewed current scope.')
    with manager._lock, manager._db:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Maintenance requires the verified Owner entry.')
        if action not in {'enter', 'deactivate'}:
            if action not in {'reenable', 'switch', 'rollback'} and set(details) - {'operation_id', 'handled_manual_session_ids'}:
                raise ManagementError('invalid_change', 'Check the original immutable maintenance plan by ID.')
            plan = data.get('maintenance_plans', {}).get(details['operation_id'])
            if not plan or (data.get('maintenance_mode') or {}).get('operation_id') != plan['id']:
                raise ManagementError('binding_conflict', 'No active original maintenance plan matches this ID.')
            handled = details.get('handled_manual_session_ids', [])
            if not isinstance(handled, list) or any(not isinstance(i, str) for i in handled) or set(handled) - set(plan.get('manual_required', [])):
                raise ManagementError('invalid_change', 'Manual handling must name actual observed sessions requiring Owner action.')
            plan['manual_handled'] = sorted(set(plan.get('manual_handled', [])) | set(handled))
            if action in {'switch', 'rollback'}:
                _confirmation(plan, details, version, data)
                return _switch_or_restore(manager, identity, plan, action)
            if action == 'reenable':
                return _reenable(manager, identity, plan, details, version, data)
            return _advance(manager, identity, plan, action)
        _public_text(json.dumps(details), manager._sensitive_values())
        approved = {'expected_version': details.get('expected_version'), 'expected_profile_ids': details.get('expected_profile_ids')}
        existing = data.setdefault('maintenance_plans', {}).get(details['operation_id'])
        if existing:
            if existing['approved_scope'] != approved or existing['intent'] != ('deactivation' if action == 'deactivate' else 'maintenance') or existing.get('expected_release') != details.get('expected_release') or existing.get('target_release') != details.get('target_release'):
                raise ManagementError('binding_conflict', 'Operation ID already belongs to another reviewed decision.')
            return existing
        if type(approved['expected_version']) is not int or approved['expected_version'] != version:
            raise ManagementError('version_conflict', 'Review the current directory version before maintenance.')
        if approved['expected_profile_ids'] != sorted(data['profiles']):
            raise ManagementError('binding_conflict', 'Review the complete current Profile scope before maintenance.')
        if data.get('maintenance_mode') not in (None, False):
            raise ManagementError('binding_conflict', 'An earlier maintenance intent still requires reconciliation.')
        plan = {'id': details['operation_id'], 'intent': 'deactivation' if action == 'deactivate' else 'maintenance', 'approved_scope': approved,
                'owner_origin': {'subject': identity.subject, 'source': identity.source}, 'created_at': _now(),
                'status': 'maintenance', 'needs_human': ['Verify no active turns, related execution, manual execution or in-flight requests before checkpoint and switch.'],
                'checks': {}, 'expected_release': details.get('expected_release'), 'target_release': details.get('target_release'),
                'scope_binding': _scope(data)}
        data['maintenance_plans'][plan['id']] = plan
        data['maintenance_mode'] = {'operation_id': plan['id'], 'intent': plan['intent']}
        manager._save(version, data)
        return _advance(manager, identity, plan, 'check') if action == 'deactivate' else plan


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _scope(data):
    return _digest({'profiles': {i: {k: p.get(k) for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')} for i, p in data['profiles'].items()},
                    'projects': {i: p.get('repo') for i, p in data['projects'].items()}})


def _save(manager, plan):
    version, data = manager._load()
    data.setdefault('maintenance_plans', {})[plan['id']] = plan
    with manager._db:
        manager._save(version, data)


def _runtime(manager):
    if manager.maintenance_host is None:
        raise ManagementError('capability_unverified', 'No current original native maintenance host verifies version or handoff.')
    fact = manager.maintenance_host.current()
    if not isinstance(fact, dict) or fact.get('status') != 'verified' or fact.get('loaded') is not True or any(not isinstance(fact.get(k), str) or not fact[k] for k in ('plugin_version', 'sdk_version', 'service_id', 'generation', 'evidence')) or any(not re.fullmatch(r'[a-f0-9]{64}', str(fact.get(k))) for k in ('source_digest', 'sdk_source_digest')):
        raise ManagementError('unknown_version', 'Current loaded plugin/SDK version and code source cannot be independently verified.')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9_.-]+)?', fact['plugin_version']) or fact['sdk_version'] in {'unknown', 'unverified', '0.0.0'}:
        raise ManagementError('unknown_version', 'Current native plugin/SDK version is unknown; no migration or new execution is permitted.')
    _fresh(manager, fact)
    return fact


def _fresh(manager, fact):
    _public_text(json.dumps(fact), manager._sensitive_values())
    try:
        at = datetime.fromisoformat(fact['verified_at'])
        if not at.tzinfo or not -5 <= (datetime.now(timezone.utc) - at).total_seconds() <= 300:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ManagementError('capability_unverified', 'Current maintenance evidence is missing or stale.')


def _proof(manager, plan, fact, phase):
    if not isinstance(fact, dict) or fact.get('status') != 'verified' or fact.get('operation_id') != plan['id'] or fact.get('phase') != phase or fact.get('scope_profile_ids') != plan['approved_scope']['expected_profile_ids'] or not fact.get('evidence') or any(fact.get(k) != plan['runtime'][k] for k in ('service_id', 'generation')):
        raise ManagementError('capability_unverified', 'Original maintenance service, generation or complete approved scope is unverified.')
    _fresh(manager, fact)
    return fact


def _inflight(manager, data):
    pending = ['manager:' + str(i) for i in manager._inflight]
    def visit(value, path):
        if isinstance(value, dict):
            if value.get('status') in {'sending', 'unknown'} and 'uuid' in value or value.get('phase') in {'rpc_intent', 'outcome_unknown'} or value.get('sent') in {'intent', 'outcome_unknown'} or value.get('start_phase') in {'thread_start_intent', 'turn_start_intent', 'outcome_unknown'}:
                pending.append(path)
            for key, child in value.items():
                visit(child, path + ':' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + ':' + str(index))
    for key in ('requests', 'knowledge_queries', 'archive_queries', 'collaboration'):
        visit(data.get(key, {}), key)
    return pending


def _handoff(manager, identity, plan, expected=None):
    _, data = manager._load()
    if plan['scope_binding'] != _scope(data):
        raise ManagementError('binding_conflict', 'Reviewed maintenance Profile/repository scope changed; review a new plan.')
    current = _runtime(manager)
    expected = expected or plan.get('expected_release')
    if not isinstance(expected, dict) or set(expected) != {'plugin_version', 'source_digest', 'sdk_version', 'sdk_source_digest'} or any(current.get(k) != v for k, v in expected.items()):
        raise ManagementError('unknown_version', 'The reviewed current plugin/SDK version and fixed code source differ or are unknown.')
    plan['runtime'] = current
    tasks = []
    for record in list(data['requests'].values()):
        if not record.get('session') or record.get('repository_released'):
            continue
        try:
            observed = manager.refresh_task(identity, record['id'])
            item = {'request_id': record['id'], 'execution': observed['execution'], 'status': 'blocked'}
            if observed['execution'] in {'turn_ended', 'stopped'}:
                from .takeover import executor_for
                from .control import terminal_evidence
                session = observed['session']
                actual = executor_for(manager, observed)
                if actual is None or actual._closed or actual.generation != session['generation']:
                    raise ManagementError('capability_unverified', 'Original executor is unavailable; supervision loss is not termination.')
                thread = actual.read_thread(session['thread_id'])
                if thread.get('id') != session['thread_id'] or thread.get('cwd') != session['repository']['worktree']:
                    raise ManagementError('binding_conflict', 'Original thread or repository identity changed.')
                related, evidence = terminal_evidence(actual, session, thread, session['turn_id'])
                item.update(status='verified' if not related else 'blocked', related_execution=related, evidence=evidence)
            tasks.append(item)
        except ManagementError as exc:
            tasks.append({'request_id': record['id'], 'status': 'blocked', 'reason': str(exc)})
    manager.refresh_manual_sessions(identity)
    _, data = manager._load()
    manual = [{'session_id': s['id'], 'state': s['state'], 'control': 'observe_only',
               'status': 'verified' if s['state'] == 'inactive_verified' else 'blocked'} for s in data.get('manual_sessions', {}).values()]
    plan['manual_required'] = sorted(set(plan.get('manual_required', [])) | {s['session_id'] for s in manual if s['status'] != 'verified'})
    for session in manual:
        if session['session_id'] in plan['manual_required'] and session['session_id'] not in plan.get('manual_handled', []):
            session.update(status='blocked', needs_human='Owner must explicitly handle and identify this observed manual session; inactivity is independently rechecked.')
    native = _proof(manager, plan, manager.maintenance_host.inspect(plan), 'handoff')
    inflight = _inflight(manager, data)
    plan['checks'] = {'tasks': tasks, 'manual': manual, 'inflight_requests': inflight, 'native': native}
    if any(t['status'] != 'verified' for t in tasks + manual) or inflight or native.get('active_turns') != [] or native.get('inflight_requests') != [] or native.get('execution_coverage') != 'complete':
        raise ManagementError('handoff_blocked', 'Active or unverified original rounds, related/manual execution or in-flight requests prevent a consistent handoff; Owner handling is required.')
    return data


def _checkpoint(manager, plan, data):
    directory = manager.state_dir / 'maintenance-checkpoints' / _digest(plan['id'])
    if directory != directory.resolve():
        raise ManagementError('unavailable', 'An unknown checkpoint alias was preserved.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = directory / 'manager.sqlite3'
    if artifact.exists():
        raise ManagementError('outcome_unknown', 'An earlier incomplete checkpoint artifact was preserved; review it before a new plan.')
    with sqlite3.connect(artifact) as database:
        manager._db.backup(database)
        if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ManagementError('unavailable', 'Authoritative directory checkpoint failed integrity verification.')
    artifact.chmod(0o600)
    native = _proof(manager, plan, manager.maintenance_host.checkpoint(plan, directory), 'checkpoint')
    if set(native.get('categories', [])) != {'config', 'data', 'archive'} or native.get('authorization_digest') != plan['checks']['native'].get('authorization_digest') or not native.get('artifacts'):
        raise ManagementError('capability_unverified', 'Native checkpoint configuration/data/archive coverage or current authorization is incomplete.')
    for entry in native['artifacts'].values():
        from pathlib import Path
        path = Path(entry['path'])
        if path != path.resolve() or not path.is_relative_to(directory) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ManagementError('capability_unverified', 'Checkpoint artifact source or digest is unverified.')
    return {'directory': {'status': 'verified', 'artifact_ref': str(artifact.relative_to(manager.state_dir)),
                          'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'restore': 'evidence_only_live_authority_never_replaced'},
            'native': native, 'authority_digest': _digest({k: data.get(k, {}) for k in ('control_grants', 'knowledge_sources')}),
            'health_restore': 'excluded', 'verified_at': _now()}


def _advance(manager, identity, plan, action):
    try:
        if plan.get('restore_request'):
            return _verify_restore(manager, identity, plan)
        if plan.get('switch_request'):
            return _verify_switch(manager, identity, plan)
        if plan['intent'] == 'deactivation':
            _stop_owned(manager, identity, plan)
        data = _handoff(manager, identity, plan)
        if action == 'checkpoint':
            if not plan.get('checkpoint'):
                # Commit the immutable maintenance gate before SQLite's independent backup.
                manager._db.commit()
                plan['checkpoint'] = _checkpoint(manager, plan, data)
            plan.update(status='checkpoint_verified', needs_human=[])
        elif plan['intent'] == 'deactivation':
            plan.update(status='deactivated', needs_human=[])
            version, current = manager._load()
            current['maintenance_mode']['intent'] = 'disabled'
            manager._save(version, current)
        else:
            plan.update(status='handoff_verified', needs_human=[])
    except (ManagementError, OSError, sqlite3.Error) as exc:
        plan.update(status='blocked', needs_human=[str(exc) if isinstance(exc, ManagementError) else 'Checkpoint or original native host is unavailable; preserve all data.'])
    _save(manager, plan)
    return plan



def _stop_owned(manager, identity, plan):
    _, data = manager._load()
    checks = []
    for record in list(data['requests'].values()):
        if not record.get('session') or record.get('repository_released'):
            continue
        session = record['session']
        if session.get('control') != 'assigned_task' or session.get('origin') == 'manual_takeover' and data.get('control_grants', {}).get(record.get('control_grant_id'), {}).get('status') != 'active':
            checks.append({'request_id': record['id'], 'status': 'observe_only', 'needs_human': 'Owner must handle the actual original manual execution; no new control grant was created.'})
            continue
        try:
            if not record.get('stop'):
                manager.control_task(identity, record['id'], 'stop', 'maintenance-stop-' + _digest([plan['id'], record['id']]), expected_turn_id=session.get('turn_id'))
            task = manager.refresh_task(identity, record['id'])
            checks.append({'request_id': record['id'], 'status': 'verified' if task.get('outer_task_status') == 'stopped' and task.get('repository_released') is True else 'processing', 'stop': task.get('stop')})
        except ManagementError as exc:
            checks.append({'request_id': record['id'], 'status': 'blocked', 'reason': str(exc)})
    plan['stop_checks'] = checks


def _reenable(manager, identity, plan, details, version, data):
    if set(details) != {'operation_id', 'expected_version', 'expected_profile_ids'} or type(details['expected_version']) is not int or version != details['expected_version']:
        raise ManagementError('version_conflict', 'Re-enabling requires Owner review of the current directory version and complete scope.')
    if details['expected_profile_ids'] != sorted(data['profiles']) or _scope(data) != plan['scope_binding']:
        raise ManagementError('binding_conflict', 'Re-enable scope differs from the original maintenance plan.')
    try:
        runtime = _runtime(manager)
        expected = _target(plan) if plan.get('switch_verified_at') and not plan.get('restore') else plan.get('expected_release')
        if not isinstance(expected, dict) or any(runtime.get(k) != v for k, v in expected.items()):
            raise ManagementError('unknown_version', 'The current release differs from the reviewed maintained release.')
        plan['runtime'] = runtime
        reconciliation = []
        # The gate remains set during original-service reconciliation, so recovery cannot auto-resume.
        for record in list(data['requests'].values()):
            if record.get('session'):
                task = manager.reconcile_task(identity, record['id'])
                result = task.get('recovery', {})
                reconciliation.append({'request_id': record['id'], 'status': result.get('status'), 'execution': task.get('execution')})
                if result.get('status') in {'blocked', 'awaiting_reconciliation', 'outcome_unknown_preserved'} or task.get('execution') in {'unverified', 'stopping'}:
                    raise ManagementError('handoff_blocked', 'Original work remains unverified; keep maintenance and resolve it without replay.')
        manager.refresh_manual_sessions(identity)
        _, current = manager._load()
        if _inflight(manager, current):
            raise ManagementError('outcome_unknown', 'Unresolved in-flight work requires original-interface reconciliation before re-enabling.')
        fact = _proof(manager, plan, manager.maintenance_host.inspect(plan), 'handoff')
        if fact.get('execution_coverage') != 'complete' or fact.get('inflight_requests') != []:
            raise ManagementError('capability_unverified', 'Original native entry and execution coverage is not current.')
        plan.update(status='reenabled', reconciliation=reconciliation, reenabled_at=_now(), needs_human=[])
        current['maintenance_mode'] = False
        current['maintenance_runtime'] = {**runtime, 'release_verified': False, 'capability_release': 'requires_all_approved_real_acceptance'}
        current['maintenance_plans'][plan['id']] = plan
        manager._save(manager._load()[0], current)
    except (ManagementError, OSError) as exc:
        plan.update(status='blocked', needs_human=[str(exc)])
        _save(manager, plan)
    return plan



def _confirmation(plan, details, version, data):
    if set(details) != {'operation_id', 'expected_version', 'expected_profile_ids'} or type(details['expected_version']) is not int or details['expected_version'] != version:
        raise ManagementError('version_conflict', 'Review the current version before switching or restoring the frozen maintenance plan.')
    if details['expected_profile_ids'] != sorted(data['profiles']) or _scope(data) != plan['scope_binding']:
        raise ManagementError('binding_conflict', 'The current maintenance scope changed after review.')


def _target(plan):
    target = plan.get('target_release')
    if not isinstance(target, dict) or set(target) != {'id', 'plugin_version', 'source_digest'} or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', str(target.get('id'))) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9_.-]+)?', str(target.get('plugin_version'))) or not re.fullmatch(r'[a-f0-9]{64}', str(target.get('source_digest'))):
        raise ManagementError('unknown_version', 'Switch requires a registered fixed target version and exact code-source digest.')
    if not isinstance(plan.get('expected_release'), dict) or set(plan['expected_release']) != {'plugin_version', 'source_digest', 'sdk_version', 'sdk_source_digest'}:
        raise ManagementError('unknown_version', 'The original reviewed SDK/plugin release is incomplete.')
    return {**plan['expected_release'], 'plugin_version': target['plugin_version'], 'source_digest': target['source_digest']}


def _checkpoint_integrity(manager, plan):
    checkpoint = plan.get('checkpoint')
    if not checkpoint:
        raise ManagementError('handoff_blocked', 'A verified consistency checkpoint is required before switching or restoring.')
    path = manager.state_dir / checkpoint['directory']['artifact_ref']
    from .recovery import verify_directory
    if path != path.resolve() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != checkpoint['directory']['sha256']:
        raise ManagementError('capability_unverified', 'Directory checkpoint digest no longer matches; preserve it for Owner reconciliation.')
    verify_directory(path)
    for artifact in checkpoint['native']['artifacts'].values():
        from pathlib import Path
        path = Path(artifact['path'])
        if path != path.resolve() or not path.is_relative_to(manager.state_dir / 'maintenance-checkpoints') or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
            raise ManagementError('capability_unverified', 'An original configuration/data/archive checkpoint changed; no restore is permitted.')
    return checkpoint


def _switch_or_restore(manager, identity, plan, action):
    try:
        checkpoint = _checkpoint_integrity(manager, plan)
        if action == 'switch':
            _target(plan)
            if plan.get('restore_request'):
                raise ManagementError('binding_conflict', 'A rollback intent already exists; do not apply this target again.')
            if plan.get('switch_request'):
                return _verify_switch(manager, identity, plan)
            _handoff(manager, identity, plan)
            request_key, callback = 'switch_request', lambda: manager.maintenance_host.switch(plan)
        else:
            if plan.get('restore_request'):
                return _verify_restore(manager, identity, plan)
            current = _runtime(manager)
            observed_release = {key: current[key] for key in plan['expected_release']}
            _handoff(manager, identity, plan, observed_release)
            if plan['checks']['native'].get('authorization_digest') != checkpoint['native']['authorization_digest']:
                raise ManagementError('binding_conflict', 'Current native authorization changed; restoring old configuration could widen or revert grants. Preserve the new authority and obtain a revised bounded restore plan.')
            request_key, callback = 'restore_request', lambda: manager.maintenance_host.restore(plan, checkpoint['native'])
        plan[request_key] = {'status': 'intent', 'requested_at': _now()}
        plan.update(status='switch_pending' if action == 'switch' else 'rollback_pending', needs_human=[])
        _save(manager, plan)
        manager._db.commit()
        try:
            result = callback()
            if not isinstance(result, dict) or result.get('status') not in {'accepted', 'verified', 'rejected', 'outcome_unknown'}:
                raise ManagementError('outcome_unknown', 'Native maintenance acknowledgement is incomplete.')
            plan[request_key].update(status=result['status'])
        except (ManagementError, OSError, TimeoutError, ValueError) as exc:
            plan[request_key].update(status='outcome_unknown', reason=str(exc) if isinstance(exc, ManagementError) else 'Original native outcome is unknown.')
        _save(manager, plan)
        return _verify_switch(manager, identity, plan) if action == 'switch' else _verify_restore(manager, identity, plan)
    except (ManagementError, OSError, sqlite3.Error) as exc:
        plan.update(status='blocked', needs_human=[str(exc) if isinstance(exc, ManagementError) else 'Native maintenance storage is unavailable; preserve data and inspect the original intent.'])
        _save(manager, plan)
        return plan


def _verify_switch(manager, identity, plan):
    try:
        _handoff(manager, identity, plan, _target(plan))
        proof = _proof(manager, plan, manager.maintenance_host.verify_switch(plan), 'switch')
        if set(proof.get('categories', [])) != {'config', 'data', 'archive', 'grants'} or proof.get('configuration_verified') is not True or proof.get('authorization_digest') != plan['checkpoint']['native']['authorization_digest']:
            raise ManagementError('capability_unverified', 'Loaded target configuration/data/archive/authorization verification failed; maintenance remains and checkpoint rollback requires an explicit Owner decision.')
        plan.update(status='switch_verified', switch_verified_at=_now(), switch_evidence=proof, needs_human=[])
        version, data = manager._load()
        data['maintenance_runtime'] = {**plan['runtime'], 'release_verified': False, 'capability_release': 'requires_all_approved_real_acceptance'}
        manager._save(version, data)
    except (ManagementError, OSError) as exc:
        plan.update(status='switch_failed', needs_human=[str(exc)])
    _save(manager, plan)
    return plan


def _verify_restore(manager, identity, plan):
    try:
        checkpoint = _checkpoint_integrity(manager, plan)
        _handoff(manager, identity, plan, plan['expected_release'])
        proof = _proof(manager, plan, manager.maintenance_host.verify_restore(plan, checkpoint['native']), 'restore')
        if set(proof.get('categories', [])) != {'config', 'data', 'archive'} or proof.get('entries_inactive') is not True or proof.get('old_tasks_started') is not False or proof.get('authorization_digest') != checkpoint['native']['authorization_digest'] or proof.get('restored_sha256') != {key: entry['sha256'] for key, entry in checkpoint['native']['artifacts'].items()}:
            raise ManagementError('capability_unverified', 'Restored configuration/data/archive or unchanged authorization/inactive-entry evidence is incomplete.')
        plan.update(status='rollback_verified', restored_at=_now(), needs_human=[], restore={'native': proof,
                    'manager_authority': 'preserved_current', 'health': 'not_restored', 'old_tasks_started': False})
        version, data = manager._load()
        data['maintenance_runtime'] = {**plan['runtime'], 'release_verified': False, 'capability_release': 'requires_all_approved_real_acceptance'}
        manager._save(version, data)
    except (ManagementError, OSError) as exc:
        plan.update(status='rollback_pending', needs_human=[str(exc)])
    _save(manager, plan)
    return plan



def runtime_loss(manager, reason):
    """Trusted native teardown notice: disappearance never asserts executor termination."""
    with manager._lock, manager._db:
        version, data = manager._load()
        event = {'id': 'native-loss:' + manager._notification_generation, 'reason': reason,
                 'status': 'pending_verification', 'recorded_at': _now(), 'execution_stopped': False,
                 'operation_id': (data.get('maintenance_mode') or {}).get('operation_id'),
                 'last_confirmed_tasks': [{'request_id': r['id'], 'execution': r.get('last_confirmed_execution', r['execution']),
                                           'confirmed_at': r.get('last_execution_verified_at')} for r in data['requests'].values() if r.get('session')]}
        data.setdefault('maintenance_events', {})[event['id']] = event
        data['maintenance_runtime'] = {**data.get('maintenance_runtime', {}), 'status': 'unverified', 'loaded': False, 'release_verified': False,
                                        'reason': 'Native manager unavailable; original execution remains pending verification.'}
        manager._save(version, data)
