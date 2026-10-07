"""Stable mono/child validation and independently releasable repository occupancy."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import uuid

from .manager import ManagementError, _git, _repository
from .queue import workspace
from .codex import repository_fingerprint


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _task(manager, identity, request_id, data):
    from .execution import _responsible
    task = _responsible(manager, identity, request_id, data)
    profile = data['profiles'].get(task['profile_id'], {})
    if profile.get('role') != 'project_lead' or any(profile.get(k) != v for k, v in task['accepted_responsibility'].items()):
        raise ManagementError('forbidden', 'Global validation belongs to the original mono responsible Profile.')
    return task


def _inputs(attempt, data):
    result = []
    for item in [{'request_id': attempt['request_id'], 'repository': attempt['repository'], 'commit': attempt['mono_commit']}] + attempt['children']:
        repo = item['repository']
        actual = _repository({'repo_path': repo['worktree'], 'test_artifact_paths': repo['test_artifact_paths']})
        if actual != repo or data['projects'][data['requests'][item['request_id']]['project_id']]['repo'] != repo:
            raise ManagementError('handoff_blocked', 'A registered source/Git/artifact boundary changed.')
        current = workspace(repo)
        metadata = hashlib.sha256()
        for root in sorted(set(repo[k] for k in ('git_dir', 'common_dir'))):
            for path in sorted(Path(root).rglob('*')):
                if path.is_symlink() or path.is_file():
                    metadata.update(str(path).encode())
                    metadata.update(str(path.readlink()).encode() if path.is_symlink() else path.read_bytes())
        task = data['requests'][item['request_id']]
        result.append({'request_id': item['request_id'], 'repository': repo, 'workspace': current,
                       'tree': _git(repo['worktree'], 'rev-parse', 'HEAD^{tree}'), 'git_metadata_digest': metadata.hexdigest(),
                       'accepted_scope': task['accepted_scope'], 'responsibility': task['accepted_responsibility'],
                       'delivery_evidence': task.get('delivery_evidence'), 'pr_status': task.get('pr_status')})
    return result


def _save(manager, version, data, attempt):
    attempt['updated_at'] = _now()
    with manager._db:
        manager._save(version, data)


def _end(manager, identity, version, data, attempt, status, reason=None):
    attempt.update(status=status, reason=reason, whole_project_complete=False)
    attempt.setdefault('occupancy', {})['released'] = True
    attempt['occupancy']['released_at'] = _now()
    _save(manager, version, data, attempt)
    manager.publish_request_message(identity, attempt['request_id'], 'progress',
        '全局验证：' + status + '\n验证：' + attempt['id'] + '\n父版本：' + attempt['mono_commit'] +
        '\n子版本：' + json.dumps([{k: c[k] for k in ('request_id', 'path', 'commit')} for c in attempt['children']], ensure_ascii=False) +
        '\n本轮验证占用已释放；mono 自身任务占用按原任务保留。' + ('\n原因：' + reason if reason else ''))
    return attempt


def _plan(manager, identity, details, version, data):
    if set(details) != {'request_id', 'mono_commit', 'children', 'test_ids'} or not re.fullmatch(r'[a-f0-9]{40}', str(details.get('mono_commit'))) or not isinstance(details['children'], list) or not isinstance(details['test_ids'], list) or not details['test_ids'] or any(not isinstance(t, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', t) for t in details['test_ids']):
        raise ManagementError('invalid_change', 'Name this mono task, a fixed parent commit, explicit related child deliveries and configured test IDs.')
    task = _task(manager, identity, details['request_id'], data)
    repo = task['accepted_repository']
    if _git(repo['worktree'], 'rev-parse', details['mono_commit'] + '^{commit}') != details['mono_commit']:
        raise ManagementError('evidence_missing', 'The fixed mono commit is not locally available.')
    children, seen = [], set()
    for supplied in details['children']:
        if not isinstance(supplied, dict) or set(supplied) != {'request_id', 'path'} or not isinstance(supplied['path'], str) or Path(supplied['path']).is_absolute() or '..' in Path(supplied['path']).parts or supplied['path'] in seen:
            raise ManagementError('invalid_change', 'Each distinct child delivery requires its relative submodule path.')
        child = data['requests'].get(supplied['request_id'], {})
        profile = data['profiles'].get(child.get('profile_id'), {})
        evidence = child.get('delivery_evidence', {})
        commit = evidence.get('source_commit')
        if profile.get('parent_profile_id') != task['profile_id'] or profile.get('role') != 'subproject_lead' or child.get('task_delivery') != 'delivered' or not re.fullmatch(r'[a-f0-9]{40}', str(commit)) or not evidence.get('criteria') or not evidence.get('execution_end'):
            raise ManagementError('evidence_missing', 'A related child has not delivered its fixed source and frozen acceptance evidence.')
        if child.get('pr_status') in {'awaiting_merge', 'awaiting_review'} and any(re.search(r'^(?:merge\b|合并(?:此|本|该)?\s*PR|将.+合并到)', c['text'], re.I) for c in evidence['criteria']):
            raise ManagementError('evidence_missing', 'A child required merge remains unmet.')
        child_repo = child['accepted_repository']
        path = Path(repo['worktree']) / supplied['path']
        if str(path) != child_repo['worktree'] or path.resolve() != path:
            raise ManagementError('handoff_blocked', 'The actual child worktree must be the declared mono submodule without path aliases.')
        entry = _git(repo['worktree'], 'ls-tree', '-z', details['mono_commit'], '--', supplied['path'])
        if entry != '160000 commit ' + commit + '\t' + supplied['path'] + '\0':
            raise ManagementError('handoff_blocked', 'The fixed mono Git tree does not reference this child delivery commit.')
        seen.add(supplied['path'])
        children.append({**supplied, 'profile_id': child['profile_id'], 'project_id': child['project_id'], 'repository': child_repo,
                         'commit': commit, 'issue': child['accepted_scope'], 'tests': child.get('test_evidence', []),
                         'criteria': evidence['criteria'], 'leftover_changes': evidence['leftover_changes'], 'pr_status': child.get('pr_status')})
    attempt = {'id': str(uuid.uuid4()), 'request_id': task['id'], 'profile_id': task['profile_id'], 'repository': repo,
               'mono_commit': details['mono_commit'], 'children': children, 'test_ids': details['test_ids'], 'status': 'planned',
               'created_at': _now(), 'planned_by': identity.subject, 'whole_project_complete': False,
               'occupancy': {'logical_repositories': sorted({repo['logical_id']} | {c['repository']['logical_id'] for c in children}), 'released': True}, 'rework': []}
    data.setdefault('global_validations', {})[attempt['id']] = attempt
    _save(manager, version, data, attempt)
    return attempt


def perform(manager, identity, action, details):
    if not isinstance(details, dict):
        raise ManagementError('invalid_change', 'A bounded global validation operation is required.')
    with manager._lock:
        version, data = manager._load()
        if action == 'plan':
            return _plan(manager, identity, details, version, data)
        if set(details) != {'validation_id'}:
            raise ManagementError('invalid_change', 'Operate on one fixed validation attempt.')
        attempt = data.get('global_validations', {}).get(details['validation_id'])
        if not attempt:
            raise ManagementError('invalid_change', 'Unknown global validation attempt.')
        _task(manager, identity, attempt['request_id'], data)
        if action != 'start':
            raise ManagementError('unsupported', 'This global validation operation is not enabled.')
        if attempt['status'] != 'planned':
            raise ManagementError('binding_conflict', 'This attempt already has an execution intent; do not replay it.')
        attempt['occupancy'].update(released=False, acquired_at=_now(), mono_request_id=attempt['request_id'])
        _save(manager, version, data, attempt)
        version += 1
        if manager.global_validation_host is None:
            _end(manager, identity, version, data, attempt, 'blocked', 'Original source/Git/artifact enforcement and test runner capability are unverified.')
            raise ManagementError('capability_unverified', attempt['reason'])
        return attempt
