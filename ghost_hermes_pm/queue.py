"""Durable outer-task ordering for each canonical Git common directory."""
from datetime import datetime, timezone

from .manager import ManagementError


def enroll(data, record, repository):
    data['queue_sequence'] = data.get('queue_sequence', 0) + 1
    record['accepted_repository'] = repository
    record['queue'] = {'logical_repository': repository['logical_id'], 'sequence': data['queue_sequence'],
                       'arranged_at': datetime.now(timezone.utc).isoformat(), 'status': 'accepted',
                       'blocked_by': [], 'reason': 'Awaiting an explicit execution request.'}
    refresh(data)


def refresh(data):
    records = sorted((r for r in data['requests'].values() if r.get('queue')), key=lambda r: r['queue']['sequence'])
    for record in records:
        queue = record['queue']
        if record.get('repository_released') and not queue.get('pending_continuation'):
            queue.update(status='released', blocked_by=[], reason=None)
            continue
        if record.get('session') and not record.get('repository_released'):
            queue.update(status='occupied', blocked_by=[], reason=record.get('unexecuted_reason'))
            continue
        blockers = [r['id'] for r in records if r['id'] != record['id'] and (not r.get('repository_released') or r['queue'].get('pending_continuation')) and
                    r['queue']['logical_repository'] == queue['logical_repository'] and
                    ((r.get('session') and not r.get('repository_released')) or r['queue']['sequence'] < queue['sequence'])]
        validation_blockers = [a['id'] for a in data.get('global_validations', {}).values() if not a['occupancy']['released'] and a['request_id'] != record['id'] and queue['logical_repository'] in a['occupancy']['logical_repositories']]
        queue['validation_blockers'] = validation_blockers
        if validation_blockers:
            queue.update(status='validation_waiting', blocked_by=blockers, reason='Waiting for related global validation occupancy to end.')
            continue
        if queue.get('manual_blockers') or queue.get('observation_blockers'):
            queue.update(status='manual_waiting', blocked_by=blockers, reason=record.get('unexecuted_reason'))
            continue
        if not blockers and queue.get('external_occupancy'):
            queue.update(status='external_unknown', blocked_by=[], reason=queue['external_occupancy']['reason'])
            continue
        if not blockers and record.get('preparation', {}).get('status') == 'blocked':
            queue.update(status='handoff_blocked', blocked_by=[], reason=record['preparation']['reason'])
            continue
        queue.update(status='queued' if blockers else 'accepted', blocked_by=blockers,
                     reason='Waiting for earlier outer work or unknown execution in this logical repository.' if blockers else 'Awaiting an explicit execution request.')


def require_turn(manager, identity, record, version, data):
    refresh(data)
    queue = record.get('queue')
    if queue:
        queue['requested_by'] = identity.subject
    from .observation import guard_repository
    guard_repository(manager, identity, record, version, data)
    if queue and (queue['blocked_by'] or queue.get('validation_blockers')):
        record.update(unexecuted_reason=queue['reason'])
        if not queue.get('pending_continuation'):
            record['execution'] = 'waiting'
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, record['id'], 'progress',
            '仓库排队：' + queue['reason'] + '\n前项：' + ', '.join(queue['blocked_by'] + queue.get('validation_blockers', [])))
        raise ManagementError('repository_busy', queue['reason'])


def workspace(repository):
    from .delivery import source_state
    from .manager import _git
    current = source_state(repository)
    import subprocess
    branch = subprocess.run(['git', '-C', repository['worktree'], 'symbolic-ref', '--quiet', '--short', 'HEAD'], capture_output=True, text=True)
    current['branch'] = branch.stdout.strip() if branch.returncode == 0 else None
    raw = _git(repository['worktree'], 'status', '--porcelain=v1', '-z').split('\0')
    names, index = [], 0
    while index < len(raw):
        entry = raw[index]
        index += 1
        if not entry:
            continue
        names.append(entry[3:])
        if 'R' in entry[:2] or 'C' in entry[:2]:
            if index < len(raw):
                names.append(raw[index])
                index += 1
    current['dirty_paths'] = sorted(set(names))
    return current


def _blocked(manager, identity, record, version, data, reason, current=None):
    preparation = record.setdefault('preparation', {})
    preparation.update(status='blocked', reason=reason, last_checked_at=datetime.now(timezone.utc).isoformat())
    if current is not None:
        preparation['workspace'] = current
    record['queue'].update(status='handoff_blocked', reason=reason)
    record.update(execution='waiting', unexecuted_reason=reason)
    with manager._db:
        manager._save(version, data)
    manager.publish_request_message(identity, record['id'], 'progress', '工作区交接受阻：' + reason)
    raise ManagementError('handoff_blocked', reason)


def prepare_task(manager, identity, request_id, plan):
    import re
    from .execution import _responsible
    from .manager import _git, _repository
    from .codex import repository_fingerprint
    if not isinstance(plan, dict) or set(plan) - {'branch', 'commit', 'dependencies', 'issue_updated_at', 'workspace_digest'} or not isinstance(plan.get('branch'), str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', plan['branch']) or (plan.get('commit') is not None and not re.fullmatch(r'[a-f0-9]{40}', str(plan['commit']))) or not isinstance(plan.get('dependencies'), list) or any(not isinstance(d, str) for d in plan['dependencies']) or len(set(plan['dependencies'])) != len(plan['dependencies']):
        raise ManagementError('invalid_change', 'Preparation needs an explicit local branch, full commit, dependency request IDs and accepted Issue version.')
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        if record.get('session') and not record.get('repository_released'):
            raise ManagementError('binding_conflict', 'Active original execution must finish before a new workspace handoff.')
        repository = record.get('accepted_repository') or data['projects'][record['project_id']]['repo']
        if plan.get('issue_updated_at') != record['accepted_scope']['updated_at']:
            raise ManagementError('binding_conflict', 'Preparation must name this task accepted Issue version.')
        actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
        if repository_fingerprint(actual) != record['accepted_repository_fingerprint'] or repository_fingerprint(data['projects'][record['project_id']]['repo']) != record['accepted_repository_fingerprint']:
            return _blocked(manager, identity, record, version, data, 'The accepted repository layout requires reconciliation.')
        current = workspace(repository)
        record['preparation'] = {'plan': plan, 'workspace': current, 'prepared_by': identity.subject}
        if current['branch'] != plan['branch'] or current['head'] != plan.get('commit'):
            return _blocked(manager, identity, record, version, data, 'The existing workspace does not match the explicit branch and fixed baseline; no checkout was performed.', current)
        if current['head'] is not None and _git(repository['worktree'], 'rev-parse', 'refs/heads/' + plan['branch']) != current['head']:
            return _blocked(manager, identity, record, version, data, 'The local branch does not identify the fixed baseline.', current)
        if current['dirty_paths'] and plan.get('workspace_digest') != current['source_digest']:
            return _blocked(manager, identity, record, version, data, 'Uncommitted or untracked files need explicit preservation acknowledgment of the displayed workspace digest.', current)
        for dep_id in plan['dependencies']:
            dependency = data['requests'].get(dep_id)
            fixed = dependency.get('delivery_evidence', {}).get('source_commit') if dependency else None
            if dep_id == request_id or not dependency or dependency.get('task_delivery') != 'delivered' or dependency.get('queue', {}).get('logical_repository') != repository['logical_id'] or not fixed or not current['head']:
                return _blocked(manager, identity, record, version, data, 'An explicit dependency lacks a delivered fixed version in this logical repository.', current)
            try:
                _git(repository['worktree'], 'merge-base', '--is-ancestor', fixed, current['head'])
            except ManagementError:
                return _blocked(manager, identity, record, version, data, 'The explicit baseline does not include a declared dependency fixed version.', current)
        for previous in data['requests'].values():
            if previous['id'] == request_id or previous.get('queue', {}).get('logical_repository') != repository['logical_id'] or previous['id'] in plan['dependencies'] or previous.get('pr_status') == 'merged':
                continue
            fixed = previous.get('delivery_evidence', {}).get('source_commit')
            before = previous.get('session', {}).get('baseline', {}).get('head')
            if fixed and fixed != before and current['head']:
                try:
                    _git(repository['worktree'], 'merge-base', '--is-ancestor', fixed, current['head'])
                except ManagementError:
                    continue
                return _blocked(manager, identity, record, version, data, 'The baseline includes prior unmerged delivery; declare that dependency or provide the task own approved baseline.', current)
        record['preparation'].update(status='ready', reason=None, last_checked_at=datetime.now(timezone.utc).isoformat(),
            preserved_files={name: current['file_digests'].get(name) for name in current['dirty_paths']})
        record['queue'].update(status='accepted', reason='Baseline confirmed; awaiting explicit execution.')
        record['unexecuted_reason'] = None
        with manager._db:
            manager._save(version, data)
        return record


def require_preparation(manager, identity, record, version, data):
    preparation = record.get('preparation', {})
    repository = record['accepted_repository']
    current = workspace(repository)
    if preparation.get('status') != 'ready':
        return _blocked(manager, identity, record, version, data, 'Confirm this task own Issue, dependencies, branch and fixed baseline before execution.', current)
    old = preparation['workspace']
    if any(current[k] != old[k] for k in ('head', 'branch', 'source_digest', 'workspace_status')):
        return _blocked(manager, identity, record, version, data, 'The confirmed baseline or preserved user content changed; a new handoff is required.', current)
    return current


def dispatch_tasks(manager):
    with manager._lock:
        _, current = manager._load()
        if current.get('maintenance_mode') not in (None, False):
            return []
    """Start only persisted, authorized requests whose own preparation still matches."""
    from .manager import VerifiedIdentity
    with manager._lock:
        _, data = manager._load()
        candidates = [r for r in data['requests'].values() if r.get('queue', {}).get('requested_by') and (not r.get('session') or r['queue'].get('pending_continuation'))
                      and not r['queue']['blocked_by'] and not r['queue'].get('external_occupancy') and not r['queue'].get('validation_blockers') and r.get('preparation', {}).get('status') == 'ready']
    results = []
    for record in sorted(candidates, key=lambda r: r['queue']['sequence']):
        try:
            identity = VerifiedIdentity(record['queue']['requested_by'], 'durable-queue-execution-request')
            pending = record['queue'].get('pending_continuation')
            if pending:
                results.append(manager.control_task(identity, record['id'], 'continue', pending['id'], pending['text'], pending['expected_turn_id']))
            else:
                results.append(manager.start_task(identity, record['id']))
        except ManagementError as exc:
            results.append({'request_id': record['id'], 'status': 'blocked', 'code': exc.code, 'reason': str(exc)})
    return results


def refresh_task_source(manager, identity, request_id):
    import difflib
    import hashlib
    import json
    from .execution import _responsible
    from .manager import _public_text
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        accepted = record['accepted_scope']
        reader = getattr(manager.delivery_source, 'read_issue', None)
        try:
            if reader is None:
                raise ManagementError('evidence_missing', 'No verified Issue read source is configured.')
            current = reader(accepted['url'])
            if not isinstance(current, dict) or current.get('url') != accepted['url'] or any(not isinstance(current.get(k), str) or not current[k] for k in ('title', 'body', 'updated_at')):
                raise ManagementError('evidence_missing', 'The original Issue source identity and update locator were not verified.')
            current = {k: current[k] for k in ('url', 'title', 'body', 'updated_at')}
            _public_text(current['title'], manager._sensitive_values())
            _public_text(current['body'], manager._sensitive_values())
            before = (accepted['title'] + '\n' + accepted['body']).splitlines()
            after = (current['title'] + '\n' + current['body']).splitlines()
            difference = '\n'.join(difflib.unified_diff(before, after, fromfile='accepted Issue', tofile='current Issue', lineterm=''))
            key = hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
            result = {'status': 'changed' if current != accepted else 'unchanged', 'current': current, 'diff': difference,
                      'accepted_updated_at': accepted['updated_at'], 'verified_at': datetime.now(timezone.utc).isoformat(), 'digest': key}
            record['issue_source'] = result
            if not any(update['digest'] == key for update in record.get('source_updates', [])):
                record.setdefault('source_updates', []).append(result)
        except ManagementError as exc:
            record['issue_source'] = {'status': 'unverified', 'code': exc.code, 'reason': str(exc), 'verified_at': datetime.now(timezone.utc).isoformat()}
        with manager._db:
            manager._save(version, data)
        if record['issue_source']['status'] == 'changed':
            manager.publish_request_message(identity, request_id, 'material', 'Issue 来源变化；已受理范围保持 ' + accepted['updated_at'] +
                '。追加或新请求须明确关联本任务。\n当前来源：' + current['url'] + ' @ ' + current['updated_at'] + '\n差异：\n' + (difference or '更新时间变化，目标与验收文字相同。'))
        return manager._load()[1]['requests'][request_id]


def logical_repository(path):
    from pathlib import Path
    from .manager import _git
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ManagementError('capability_unverified', 'Loaded execution repository identity is incomplete.')
    return str(Path(_git(Path(path).resolve(), 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve())


def ensure_queues(data):
    """Upgrade only the durable accepted records, never infer work from service scans."""
    changed = False
    for record in sorted(data['requests'].values(), key=lambda r: (r['accepted_at'], r['id'])):
        if record.get('queue'):
            continue
        repository = record.get('session', {}).get('repository') or data['projects'][record['project_id']]['repo']
        enroll(data, record, repository)
        changed = True
    return changed
