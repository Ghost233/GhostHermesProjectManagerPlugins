"""Owner-reviewed selective migration; plans never stand in for a real cutover."""
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3

from .manager import ManagementError, VerifiedIdentity, _public_text


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _binding(profile):
    return {k: profile.get(k) for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')}


def _save(manager, operation):
    version, data = manager._load()
    data.setdefault('migration_plans', {})[operation['id']] = operation
    with manager._db:
        manager._save(version, data)


def _checkpoint(manager, operation):
    directory = manager.state_dir / 'migration-checkpoints'
    if directory != directory.resolve():
        raise ManagementError('unavailable', 'Migration checkpoint directory must be canonical.')
    directory.mkdir(exist_ok=True)
    artifact = directory / (digest(operation['id']) + '.sqlite3')
    if artifact.exists() or artifact.is_symlink():
        raise ManagementError('unavailable', 'Existing incomplete migration checkpoint was preserved for reconciliation.')
    with sqlite3.connect(artifact) as target:
        manager._db.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ManagementError('unavailable', 'Migration checkpoint integrity is unverified.')
    return {'status': 'verified_manager_directory', 'artifact_ref': str(artifact.relative_to(manager.state_dir)),
        'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'verified_at': _now(),
        'excluded': ['source_profiles', 'credentials', 'bots', 'external_banks', 'repositories', 'notification-health.json'],
        'control_intent': 'target_parked;old_entry_not_started'}


def _archives(manager, operation, data):
    from .archives import _source
    target = data['profiles'][operation['plan']['target_profile_id']]
    identity = VerifiedIdentity(target['identity_ref'], 'migration-target-grant-check')
    result = {}
    for source_id in operation['plan']['archive_source_ids']:
        registered = data.get('archive_sources', {}).get(source_id)
        if not registered or registered['new_profile_id'] != target['id']:
            raise ManagementError('forbidden', 'Old history requires a separately registered source and new identity grant.')
        source, grant = _source(manager, identity, source_id, registered['scope_ids'], data)
        result[source_id] = {k: source[k] for k in ('grant_source_id', 'new_identity_ref', 'provider_binding', 'scope_ids', 'authorization_ref')}
        result[source_id]['grant_revision'] = grant['revision']
    return result


def _plan(manager, identity, details, version, data):
    fields = {'plan_id', 'expected_version', 'expected_profile_ids', 'source_profile_id', 'target_profile_id',
              'selection', 'preferences', 'execution', 'archive_source_ids', 'external_memory', 'human_steps'}
    if set(details) != fields or not isinstance(details.get('plan_id'), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', details['plan_id']):
        raise ManagementError('invalid_change', 'An explicit bounded migration plan, selection and human steps are required.')
    prior = data.get('migration_plans', {}).get(details['plan_id'])
    if prior:
        if prior['plan'] != details:
            raise ManagementError('binding_conflict', 'Migration plan ID already names an immutable Owner decision.')
        return prior
    if type(details['expected_version']) is not int or version != details['expected_version']:
        raise ManagementError('version_conflict', 'Directory changed after review; keep the original confirmation version.')
    source = data['profiles'].get(details['source_profile_id'])
    target = data['profiles'].get(details['target_profile_id'])
    ids = sorted([details['source_profile_id'], details['target_profile_id']])
    if not source or not target or source['id'] == target['id'] or sorted(details['expected_profile_ids']) != ids:
        raise ManagementError('binding_conflict', 'Review the exact original and independent target Profile scope.')
    if any(source[k] == target[k] for k in ('native_profile', 'identity_ref')) or source['project_id'] != target['project_id']:
        raise ManagementError('binding_conflict', 'Migration requires an independent native Profile and identity in the reviewed project.')
    refs = target['connection_refs']
    if not refs.get('bot') or not refs.get('credential') or any(refs[k] == source['connection_refs'].get(k) for k in refs):
        raise ManagementError('binding_conflict', 'Rebuild the new bot, native credential and execution bindings independently.')
    if target.get('migration_gate') or target.get('archive_intent') or any(r['profile_id'] == target['id'] for r in data['requests'].values()):
        raise ManagementError('migration_blocked', 'Target must have no previous work or other migration/lifecycle intent.')
    if not isinstance(details['selection'], list) or len(details['selection']) > 20:
        raise ManagementError('invalid_change', 'Choose bounded original knowledge/persona entries explicitly.')
    for entry in details['selection']:
        if not isinstance(entry, dict) or set(entry) != {'kind', 'locator', 'source_version', 'source_digest', 'text'} or entry['kind'] not in {'knowledge', 'persona'} or not all(isinstance(entry[k], str) and entry[k] for k in entry):
            raise ManagementError('invalid_change', 'Selected original material needs kind, locator, version, digest and exact text.')
        _public_text(entry['text'], manager._sensitive_values())
    if not isinstance(details['preferences'], list) or len(details['preferences']) > 20:
        raise ManagementError('invalid_change', 'Only explicit Owner preference statements may move.')
    for preference in details['preferences']:
        if not isinstance(preference, dict) or set(preference) != {'statement', 'scope'} or preference['scope'] not in ({'kind': 'project', 'id': target['project_id']}, {'kind': 'profile', 'id': target['id']}):
            raise ManagementError('invalid_change', 'Owner preferences require an explicit target Profile or project scope.')
        _public_text(preference['statement'], manager._sensitive_values())
    execution = details['execution']
    if not isinstance(execution, dict) or set(execution) != {'model', 'provider', 'toolsets'} or any(not isinstance(execution[k], str) or not execution[k] for k in ('model', 'provider')) or not isinstance(execution['toolsets'], list) or any(not isinstance(t, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', t) for t in execution['toolsets']):
        raise ManagementError('invalid_change', 'Rebuild only the explicitly reviewed model, provider and toolsets; no cloned config.')
    external = details['external_memory']
    if external != {'kind': 'builtin'}:
        raise ManagementError('capability_unverified', 'External banks require a separately verified new writer/reader identity; bank migration is unavailable.')
    if not isinstance(details['archive_source_ids'], list) or any(not isinstance(s, str) or not s for s in details['archive_source_ids']) or not isinstance(details['human_steps'], list) or any(not isinstance(s, str) or not s for s in details['human_steps']):
        raise ManagementError('invalid_change', 'List exact registered archive sources and concrete human steps.')
    _public_text(json.dumps(details), manager._sensitive_values())
    operation = {'id': details['plan_id'], 'digest': digest(details), 'plan': json.loads(json.dumps(details)),
        'approved_scope': {'expected_version': version, 'expected_profile_ids': ids},
        'bindings': {p['id']: _binding(p) for p in (source, target)}, 'owner_origin': {'subject': identity.subject, 'source': identity.source},
        'status': 'planned', 'native_state': 'not_created', 'created_at': _now(), 'selection_ledger': [],
        'needs_human': list(details['human_steps']), 'rollback': {'status': 'not_requested', 'old_entry': 'not_started', 'old_tasks': 'not_resumed', 'repositories': 'not_modified'}}
    operation['archive_bindings'] = _archives(manager, operation, data)
    target['migration_gate'] = {'plan_id': operation['id'], 'state': 'configuring'}
    data.setdefault('migration_plans', {})[operation['id']] = operation
    with manager._db:
        manager._save(version, data)
    return operation


def operate(manager, identity, action, details):
    if action not in {'plan', 'prepare', 'check', 'activate', 'rollback', 'preview'} or not isinstance(details, dict):
        raise ManagementError('invalid_change', 'Select a concrete migration operation.')
    with manager._lock:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Migration decisions require the verified Owner.')
        if action == 'plan':
            return _plan(manager, identity, details, version, data)
        if action == 'preview':
            if set(details) != {'source_profile_id'} or details['source_profile_id'] not in data['profiles'] or manager.migration_host is None:
                raise ManagementError('capability_unverified', 'Select a registered source on the configured native migration host.')
            return manager.migration_host.preview(data['profiles'][details['source_profile_id']])
        if set(details) - {'plan_id', 'digest', 'session_id'} or not {'plan_id', 'digest'} <= set(details) or action != 'check' and 'session_id' in details:
            raise ManagementError('invalid_change', 'Use the originally reviewed immutable plan ID and digest.')
        operation = data.get('migration_plans', {}).get(details['plan_id'])
        if not operation or operation['digest'] != details['digest']:
            raise ManagementError('binding_conflict', 'Original migration plan digest does not match.')
        if operation['bindings'] != {i: _binding(data['profiles'][i]) for i in operation['bindings']} or operation['archive_bindings'] != _archives(manager, operation, data):
            raise ManagementError('binding_conflict', 'Original Profile or source authorization binding changed; review a new plan.')
        if action == 'prepare':
            if not operation.get('checkpoint'):
                operation['checkpoint'] = _checkpoint(manager, operation)
                _save(manager, operation)
            if manager.migration_host is None:
                operation.update(status='blocked', needs_human=['Native migration host is unavailable; no Profile or material was created.'] + operation['plan']['human_steps'])
            else:
                operation.update(status='preparing')
                _save(manager, operation)
                try:
                    result = manager.migration_host.prepare(operation)
                    operation.update(result)
                except (ManagementError, OSError) as exc:
                    operation.update(status='blocked', needs_human=[str(exc)])
            _save(manager, operation)
            return operation
        if action == 'check':
            if manager.migration_host is None or not operation.get('material_receipt'):
                operation.update(status='blocked', switch_state='not_switched', needs_human=['Prepare the configured native target and independent bot first.'])
            else:
                try:
                    operation.update(manager.migration_host.check(operation, details.get('session_id')))
                    if operation['status'] == 'prepared':
                        operation['needs_human'] = operation['plan']['human_steps'] + ['Verify new bot/channel identity, original entry/task/bot/scheduler stop, and actual new-session prompt before explicit switch.']
                except (ManagementError, OSError) as exc:
                    operation.update(status='blocked', switch_state='not_switched', needs_human=[str(exc)])
            _save(manager, operation)
            return operation
        raise ManagementError('capability_unverified', 'The prepared native state requires current cutover and rollback evidence.')
