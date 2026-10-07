"""Stable mono/child validation and independently releasable repository occupancy."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import uuid

from .manager import ManagementError, _git, _repository
from .queue import workspace


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
                       'delivery_evidence': task.get('delivery_evidence') if item['request_id'] != attempt['request_id'] else None,
                       'pr_status': task.get('pr_status') if item['request_id'] != attempt['request_id'] else None,
                       'all_source_digest': _source_digest(repo)})
    return result


def _source_digest(repository):
    digest = hashlib.sha256()
    excluded = [Path(repository['worktree']) / '.git'] + [Path(p) for p in repository['test_artifact_paths']] + [Path(n['worktree']) for n in repository['nested_repositories']]
    for root, dirs, files in os.walk(repository['worktree']):
        dirs[:] = sorted(d for d in dirs if not any((Path(root) / d).is_relative_to(p) for p in excluded))
        for name in sorted(files + [d for d in dirs if (Path(root) / d).is_symlink()]):
            path = Path(root) / name
            if any(path.is_relative_to(p) for p in excluded):
                continue
            digest.update(str(path.relative_to(repository['worktree'])).encode())
            digest.update(str(path.readlink()).encode() if path.is_symlink() else path.read_bytes())
    return digest.hexdigest()


def _boundary(manager, attempt):
    host = manager.global_validation_host
    verifier = getattr(host, 'verify_boundary', None)
    proof = verifier(attempt) if callable(verifier) else None
    expected = {'validation_id': attempt['id'], 'input_digest': attempt['input_digest'], 'source_access': 'read-only',
                'git_access': 'read-only', 'artifact_roots': attempt['repository']['test_artifact_paths']}
    checks = ('platform_enforcement', 'tool_paths', 'preexisting_hardlink', 'process_paths', 'evidence_ref')
    if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items()) or any(not isinstance(proof.get(k), str) or not proof[k] for k in checks) or proof.get('scope') not in {'synthetic-fixture', 'verified-original-host'}:
        raise ManagementError('capability_unverified', 'Actual source, Git metadata, artifacts, hardlinks and all process/tool paths need independent host boundary evidence.')
    if proof['scope'] == 'verified-original-host':
        ref = proof.get('receipt')
        path = Path(ref.get('path', '')) if isinstance(ref, dict) else Path('.')
        if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(manager.state_dir / 'validation-evidence') or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref.get('sha256'):
            raise ManagementError('capability_unverified', 'Original-host enforcement receipt is absent or changed.')
        receipt = json.loads(path.read_text())
        required = {'source_write_denied', 'child_source_write_denied', 'parent_git_write_denied', 'child_git_write_denied', 'artifact_write_allowed', 'artifact_escape_denied', 'preexisting_hardlink_write_denied', 'tool_paths_confined', 'process_paths_confined'}
        if any(receipt.get(k) != v for k, v in expected.items()) or set(receipt.get('checks', {})) != required or any(v != 'PASS' for v in receipt['checks'].values()):
            raise ManagementError('capability_unverified', 'The original-host enforcement matrix is incomplete for this exact combination.')
    return proof


def _acquire(manager, identity, version, data, attempt):
    logical = set(attempt['occupancy']['logical_repositories'])
    blockers = [r['id'] for r in data['requests'].values() if r['id'] != attempt['request_id'] and r.get('session') and not r.get('repository_released') and r.get('queue', {}).get('logical_repository') in logical]
    blockers += [a['id'] for a in data.get('global_validations', {}).values() if a['id'] != attempt['id'] and not a['occupancy']['released'] and logical & set(a['occupancy']['logical_repositories'])]
    blockers += [r['id'] for r in data['requests'].values() if r.get('queue', {}).get('logical_repository') in logical and r.get('queue', {}).get('external_occupancy')]
    blockers += [s['id'] for s in data.get('manual_sessions', {}).values() if s['logical_repository'] in logical and s['blocks_repository']]
    blockers += [s['id'] for s in data.get('manual_sources', {}).values() if logical & set(s.get('logical_repositories', {}).values()) and s['status'] != 'verified']
    if blockers:
        attempt['blocked_by'] = blockers
        _end(manager, identity, version, data, attempt, 'blocked', 'Existing managed/manual execution or observation coverage remains active or unknown; it was not interrupted.')
        raise ManagementError('repository_busy', attempt['reason'])
    attempt['occupancy'].update(released=False, acquired_at=_now(), mono_request_id=attempt['request_id'])
    attempt['status'] = 'preparing'
    _save(manager, version, data, attempt)


def _finish(manager, identity, version, data, attempt):
    if attempt['status'] not in {'running', 'unverified', 'invalidating'} or not attempt.get('run'):
        raise ManagementError('binding_conflict', 'Only an original validation run can be checked; no replay or new executor.')
    host = manager.global_validation_host
    if host is None:
        attempt.update(status='unverified', reason='Original validation runner unavailable; occupancy retained until reconciliation.')
        _save(manager, version, data, attempt)
        return attempt
    try:
        current = _inputs(attempt, data)
        changed = _digest(current) != attempt['input_digest']
    except (ManagementError, OSError):
        current, changed = [], True
    if changed:
        attempt.update(status='invalidating', whole_project_complete=False, changed_inputs=current)
    try:
        result = host.read_result(attempt['run']['run_id'])
    except (ManagementError, OSError):
        result = None
    expected = {'run_id': attempt['run']['run_id'], 'validation_id': attempt['id'], 'input_digest': attempt['input_digest']}
    if not isinstance(result, dict) or any(result.get(k) != v for k, v in expected.items()) or result.get('related_execution') != 'ended':
        attempt.update(status='invalidating' if changed else 'unverified', reason='Original test and related execution termination are not yet verified; occupancy retained.')
        _save(manager, version, data, attempt)
        return attempt
    if changed:
        return _end(manager, identity, version, data, attempt, 'invalidated', 'Actual source, parent/child version, Git metadata, worktree, accepted input or boundary changed during this round.')
    tests = result.get('tests')
    if result.get('status') != 'ended' or not isinstance(tests, list) or {t.get('id') for t in tests if isinstance(t, dict)} != set(attempt['test_ids']) or any(type(t.get('exit_code')) is not int or t.get('cwd') != attempt['repository']['worktree'] or not re.fullmatch(r'[a-f0-9]{64}', str(t.get('output_digest'))) or not isinstance(t.get('argv'), list) or not t['argv'] for t in tests):
        return _end(manager, identity, version, data, attempt, 'blocked', 'The exact configured tests did not provide complete original-run execution receipts.')
    for test in tests:
        for ref in test.get('artifact_refs', []):
            path = Path(ref.get('path', ''))
            roots = [Path(p) for p in attempt['repository']['test_artifact_paths']]
            if not path.is_absolute() or path.resolve() != path or not path.is_file() or not any(path.is_relative_to(p) for p in roots) or hashlib.sha256(path.read_bytes()).hexdigest() != ref.get('sha256'):
                return _end(manager, identity, version, data, attempt, 'blocked', 'Test artifact evidence escaped its explicit boundary or changed.')
    attempt.update(tests=tests, boundary_scope=attempt['boundary']['scope'], defects=result.get('defects', []), finished_at=_now())
    return _end(manager, identity, version, data, attempt, 'passed' if all(t['exit_code'] == 0 for t in tests) else 'failed')


def _save(manager, version, data, attempt):
    attempt['updated_at'] = _now()
    with manager._db:
        manager._save(version, data)


def _end(manager, identity, version, data, attempt, status, reason=None):
    attempt.update(status=status, reason=reason, whole_project_complete=False)
    task = data['requests'][attempt['request_id']]
    if task.get('global_validation_id') == attempt['id']:
        task['whole_project_complete'] = False
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


def _prepare(manager, identity, details, version, data, attempt):
    if manager._principal(identity, data) is not None:
        raise ManagementError('forbidden', 'Child materialization is a separate exact Owner authorization, never mono-source execution permission.')
    scope = [{k: c[k] for k in ('request_id', 'commit', 'path')} for c in attempt['children']]
    if set(details) != {'validation_id', 'children'} or details['children'] != scope or attempt['status'] != 'planned':
        raise ManagementError('invalid_change', 'Authorize exactly this round fixed child materialization; no other versions or replay.')
    _acquire(manager, identity, version, data, attempt)
    version += 1
    try:
        before = _inputs(attempt, data)
        attempt['preparation_inputs'] = before
        if any(i['workspace']['dirty_paths'] for i in before[1:]) or any(p not in {c['path'] for c in attempt['children']} for p in before[0]['workspace']['dirty_paths']):
            raise ManagementError('handoff_blocked', 'User changes block child checkout; files are preserved and require a separate recoverable plan.')
        materializer = getattr(manager.global_validation_host, 'prepare', None)
        if not callable(materializer):
            raise ManagementError('capability_unverified', 'An independently authorized original materialization host is unavailable.')
        authorization = {'owner': identity.subject, 'source': identity.source, 'children': scope, 'authorized_at': _now()}
        authorization['digest'] = _digest(authorization)
        attempt.update(preparation={'status': 'intent', 'authorization': authorization})
        _save(manager, version, data, attempt)
        version += 1
        result = materializer(attempt, authorization)
        if not isinstance(result, dict) or result.get('validation_id') != attempt['id'] or result.get('authorization_digest') != authorization['digest'] or result.get('status') != 'ended' or result.get('related_execution') != 'ended' or result.get('operations') != scope:
            raise ManagementError('outcome_unknown', 'The original materialization action termination is unverified; do not replay or run tests.')
        after = _inputs(attempt, data)
        if after[0]['workspace']['head'] != before[0]['workspace']['head'] or after[0]['all_source_digest'] != before[0]['all_source_digest'] or any(i['workspace']['head'] != c['commit'] or i['workspace']['dirty_paths'] for i, c in zip(after[1:], attempt['children'])):
            raise ManagementError('handoff_blocked', 'Materialization changed mono source or did not provide the exact clean child versions.')
        attempt.update(status='ready', preparation={'status': 'ended', 'authorization': authorization, 'receipt': result, 'ended_at': _now()})
        attempt['occupancy']['released'] = True
        _save(manager, version, data, attempt)
        return attempt
    except ManagementError as exc:
        if attempt.get('preparation', {}).get('status') == 'intent':
            attempt.update(status='preparation_unverified', reason=str(exc))
            _save(manager, version, data, attempt)
        else:
            _end(manager, identity, version, data, attempt, 'blocked', str(exc))
        raise


def _check(manager, identity, version, data, attempt):
    if attempt['status'] not in {'passed', 'complete'}:
        return attempt
    try:
        current = _inputs(attempt, data)
        unchanged = _digest(current) == attempt['input_digest']
    except (ManagementError, OSError):
        unchanged = False
    if not unchanged:
        return _end(manager, identity, version, data, attempt, 'invalidated', 'Previously validated inputs changed; a new stable combination must be tested.')
    return attempt


def _rework(manager, identity, details, version, data, attempt):
    from .manager import _public_text
    from .collaboration import _new_handoff
    if attempt['status'] != 'failed' or set(details) != {'validation_id', 'target', 'issue_url'}:
        raise ManagementError('invalid_change', 'Return one failed-round defect using its exact target and a concrete repair Issue.')
    target = details['target']
    defects = [d for d in attempt.get('defects', []) if isinstance(d, dict) and d.get('target') == target and set(d.get('test_ids', [])) <= set(attempt['test_ids']) and d.get('description')]
    if not defects:
        raise ManagementError('evidence_missing', 'This target has no defect evidence in the original failed tests.')
    if not isinstance(details['issue_url'], str) or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*', details['issue_url']):
        raise ManagementError('invalid_change', 'A concrete GitHub repair Issue is required; the manager never creates unapproved network work.')
    existing = next((r for r in attempt['rework'] if r['target'] == target and r['issue']['url'] == details['issue_url']), None)
    if existing:
        return attempt
    reader = getattr(manager.delivery_source, 'read_issue', None)
    issue = reader(details['issue_url']) if callable(reader) else None
    if not isinstance(issue, dict) or issue.get('url') != details['issue_url'] or any(not isinstance(issue.get(k), str) or not issue[k] for k in ('title', 'body', 'updated_at')):
        raise ManagementError('evidence_missing', 'Repair Issue scope and current locator require the trusted Ghost233 source.')
    _public_text(issue['title'] + '\n' + issue['body'], manager._sensitive_values())
    repair = {'target': target, 'issue': {k: issue[k] for k in ('url', 'title', 'body', 'updated_at')}, 'defects': defects,
              'validation_id': attempt['id'], 'status': 'awaiting_repair', 'created_at': _now()}
    task = data['requests'][attempt['request_id']]
    child = next((c for c in attempt['children'] if c['path'] == target), None)
    if target in {'environment', 'permission', 'version_preparation'}:
        repair.update(route='blocked', profile_id=None, status='blocked')
    elif target == 'mono':
        repair.update(route='mono_self', profile_id=attempt['profile_id'], request_id=attempt['request_id'])
    elif child:
        profile = data['profiles'].get(child['profile_id'], {})
        if profile.get('parent_profile_id') != attempt['profile_id'] or profile.get('project_id') != child['project_id']:
            raise ManagementError('binding_conflict', 'The child responsibility changed; repair requires reconciliation.')
        actor = task.get('accepted_actor', {})
        origin = task.get('actor_provenance', {}).get('owner_origin')
        if origin is None and actor.get('subject') == manager.owner_identity_ref:
            origin = {**actor, 'source_anchor': task['source_anchor']}
        if not origin:
            raise ManagementError('evidence_missing', 'Original Owner authorization provenance is unavailable.')
        try:
            handoff = _new_handoff(manager, data, data['profiles'][attempt['profile_id']], profile, issue['url'], origin,
                                   task['task_start_anchor'] or task['source_anchor'], task.get('actor_provenance', {}).get('delegation_id') or attempt['request_id'])
            handoff.update(rework_validation_id=attempt['id'], rework_child_request_id=child['request_id'])
            repair.update(route='child', profile_id=child['profile_id'], handoff_id=handoff['id'])
        except ManagementError as exc:
            repair.update(route='child', profile_id=child['profile_id'], status='blocked', reason=str(exc))
    else:
        repair.update(route='owner_decision', profile_id=None, status='needs_owner')
    attempt['rework'].append(repair)
    _save(manager, version, data, attempt)
    manager.publish_request_message(identity, attempt['request_id'], 'material', '全局验证返工：' + target +
        '\n明确 Issue：' + issue['url'] + ' @ ' + issue['updated_at'] + '\n责任：' + str(repair['profile_id']) +
        '\n处理：' + repair['route'] + '\n原验证：' + attempt['id'] + '\n本轮占用已释放；修复后提交固定交付并新建重验。')
    return attempt


def _complete(manager, identity, version, data, attempt):
    attempt = _check(manager, identity, version, data, attempt)
    task = data['requests'][attempt['request_id']]
    if attempt['status'] not in {'passed', 'complete'} or task.get('task_delivery') != 'delivered' or task.get('delivery_evidence', {}).get('source_commit') != attempt['mono_commit']:
        raise ManagementError('evidence_missing', 'Original mono acceptance and this current stable combination global tests must both be complete.')
    pending_repairs = [r for a in data.get('global_validations', {}).values() if a['request_id'] == attempt['request_id'] and a['id'] != attempt['id'] for r in a.get('rework', []) if r['status'] not in {'resolved'}]
    for repair in pending_repairs:
        handoff = data.get('collaboration', {}).get('handoffs', {}).get(repair.get('handoff_id'), {})
        child_task = data['requests'].get(handoff.get('task_request_id'), {})
        fixed = next((c for c in attempt['children'] if c['path'] == repair['target'] and c['request_id'] == child_task.get('id')), None)
        if repair['route'] == 'mono_self':
            repair['status'] = 'resolved'
        elif fixed and child_task.get('task_delivery') == 'delivered':
            repair.update(status='resolved', repaired_request_id=child_task['id'], revalidated_by=attempt['id'])
        else:
            raise ManagementError('evidence_missing', 'An earlier concrete repair Issue still lacks its fixed delivery in this revalidation combination.')
    attempt.update(status='complete', whole_project_complete=True, completed_at=_now())
    task.update(global_validation_id=attempt['id'], whole_project_complete=True)
    _save(manager, version, data, attempt)
    manager.publish_request_message(identity, attempt['request_id'], 'result', '项目任务全局验证完成。\n验证：' + attempt['id'] +
        '\n父版本：' + attempt['mono_commit'] + '\n固定子交付：' + json.dumps([{k: c[k] for k in ('request_id', 'path', 'commit')} for c in attempt['children']], ensure_ascii=False) +
        '\n验证范围：' + attempt['boundary_scope'] + '\n测试输出摘要：' + json.dumps(attempt['tests'], ensure_ascii=False))
    return attempt


def perform(manager, identity, action, details):
    if not isinstance(details, dict):
        raise ManagementError('invalid_change', 'A bounded global validation operation is required.')
    with manager._lock:
        version, data = manager._load()
        if action == 'plan':
            return _plan(manager, identity, details, version, data)
        if action not in {'prepare', 'rework'} and set(details) != {'validation_id'}:
            raise ManagementError('invalid_change', 'Operate on one fixed validation attempt.')
        attempt = data.get('global_validations', {}).get(details['validation_id'])
        if not attempt:
            raise ManagementError('invalid_change', 'Unknown global validation attempt.')
        _task(manager, identity, attempt['request_id'], data)
        if action == 'check':
            return _check(manager, identity, version, data, attempt)
        if action == 'complete':
            return _complete(manager, identity, version, data, attempt)
        if action == 'rework':
            return _rework(manager, identity, details, version, data, attempt)
        if action == 'prepare':
            return _prepare(manager, identity, details, version, data, attempt)
        if action == 'finish':
            return _finish(manager, identity, version, data, attempt)
        if action != 'start':
            raise ManagementError('unsupported', 'This global validation operation is not enabled.')
        if attempt['status'] not in {'planned', 'ready'}:
            raise ManagementError('binding_conflict', 'This attempt already has an execution intent; do not replay it.')
        _acquire(manager, identity, version, data, attempt)
        version += 1
        try:
            inputs = _inputs(attempt, data)
            expected_commits = [attempt['mono_commit']] + [c['commit'] for c in attempt['children']]
            if any(i['workspace']['head'] != commit or i['workspace']['dirty_paths'] for i, commit in zip(inputs, expected_commits)):
                raise ManagementError('handoff_blocked', 'Actual materialized parent/child commits and preserved worktrees need approved preparation before testing.')
            attempt.update(inputs=inputs, input_digest=_digest(inputs))
            if attempt.get('preparation', {}).get('status') != 'ended':
                attempt['preparation'] = {'status': 'ended', 'method': 'already_materialized_readonly', 'ended_at': _now()}
            if manager.global_validation_host is None:
                raise ManagementError('capability_unverified', 'Original source/Git/artifact enforcement and test runner capability are unverified.')
            attempt['boundary'] = _boundary(manager, attempt)
            attempt['status'] = 'start_intent'
            _save(manager, version, data, attempt)
            version += 1
            run = manager.global_validation_host.start(attempt)
            if not isinstance(run, dict) or run.get('validation_id') != attempt['id'] or run.get('input_digest') != attempt['input_digest'] or not isinstance(run.get('run_id'), str) or not run['run_id']:
                raise ManagementError('outcome_unknown', 'Original validation test start was not confirmed; do not replay.')
            attempt.update(run=run, status='running', started_at=_now())
            _save(manager, version, data, attempt)
            return attempt
        except ManagementError as exc:
            if attempt['status'] == 'start_intent':
                attempt.update(status='unverified', reason=str(exc))
                _save(manager, version, data, attempt)
            else:
                _end(manager, identity, version, data, attempt, 'blocked', str(exc))
            raise
