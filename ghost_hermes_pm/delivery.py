"""Verify frozen Issue acceptance from service/Git/file evidence, not completion prose."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess
import shlex

from .manager import ManagementError, _git, _public_text


def source_state(repository):
    root = Path(repository['worktree'])
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    head = result.stdout.strip() if result.returncode == 0 else None
    files = _git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')
    digest = hashlib.sha256()
    file_digests = {}
    artifacts = [Path(p) for p in repository['test_artifact_paths']]
    for name in sorted(set(files) - {''}):
        path = root / name
        if any(path.is_relative_to(p) for p in artifacts):
            continue
        digest.update(name.encode())
        if path.is_symlink():
            raw = path.readlink().as_posix().encode()
        elif path.is_file():
            raw = path.read_bytes()
        elif not path.exists():
            raw = None
        else:
            continue
        digest.update(raw if raw is not None else b'<deleted>')
        file_digests[name] = hashlib.sha256(raw).hexdigest() if raw is not None else None
    return {'head': head, 'source_digest': digest.hexdigest(), 'file_digests': file_digests, 'workspace_status': _git(root, 'status', '--porcelain=v1')}



def _direct_test_command(command):
    if any(character in command for character in '|;&<>\n'):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args or any(a in {'--help', '-h', '--version', '--collect-only', '--co'} for a in args):
        return False
    name = Path(args[0]).name
    return (name == 'pytest' or (re.fullmatch(r'python(?:[0-9.]+)?', name) and args[1:3] in [['-m', 'pytest'], ['-m', 'unittest']]))

def acceptance_criteria(body):
    checks = re.findall(r'^\s*[-*]\s+\[[ xX]\]\s+(.+)$', body, re.MULTILINE)
    return checks or [body]


def record_task_delivery(manager, identity, request_id, report):
    from .execution import _responsible, refresh_task
    if not isinstance(report, dict) or set(report) - {'issue_updated_at', 'criteria', 'source_commit', 'pr_url', 'sync_branches'}:
        raise ManagementError('invalid_change', 'Delivery accepts evidence references, never passed or delivered declarations.')
    with manager._lock:
        refresh_task(manager, identity, request_id)
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        if record['execution'] != 'turn_ended' or record.get('turn_status') != 'completed' or not record.get('history_complete'):
            raise ManagementError('evidence_missing', 'Original execution and complete terminal history must be verified before delivery.')
        from .control import terminal_evidence, _thread
        session = record['session']
        from .takeover import bind_executor
        adapter = bind_executor(manager, record)
        related, execution_end = terminal_evidence(adapter, session, _thread(adapter, session, require_input=False), session['turn_id'])
        if related:
            record.update(related_execution=related, handoff_reason='Related original execution has not finished.')
            with manager._db:
                manager._save(version, data)
            raise ManagementError('evidence_missing', record['handoff_reason'])
        scope = record['accepted_scope']
        expected = acceptance_criteria(scope['body'])
        criteria = report.get('criteria')
        if report.get('issue_updated_at') != scope['updated_at'] or not isinstance(criteria, list) or [c.get('text') for c in criteria if isinstance(c, dict)] != expected:
            raise ManagementError('evidence_missing', 'Every frozen Issue acceptance item must have an exact evidence association.')
        if manager._principal(identity, data) is not None:
            from .execution import _current_assignment
            _current_assignment(manager, record, data)
        repository = record['session'].get('repository') or data['projects'][record['project_id']]['repo']
        evidence = {c['item_id']: c for c in record.get('command_evidence', [])}
        tests, artifacts = {}, []
        for criterion in criteria:
            if set(criterion) - {'text', 'test_item_ids', 'artifact_refs', 'pr_evidence'}:
                raise ManagementError('invalid_change', 'Unknown criterion evidence fields.')
            item_ids = criterion.get('test_item_ids', [])
            artifact_refs = criterion.get('artifact_refs', [])
            if not isinstance(item_ids, list) or not isinstance(artifact_refs, list):
                raise ManagementError('invalid_change', 'Evidence references must be explicit lists.')
            if not item_ids and not artifact_refs and criterion.get('pr_evidence') is not True:
                raise ManagementError('evidence_missing', 'An acceptance item has no verifiable evidence.')
            for item_id in item_ids:
                command = evidence.get(item_id)
                if not command or command['status'] != 'completed' or type(command['exit_code']) is not int or command['exit_code'] != 0 or command['cwd'] != repository['worktree']:
                    raise ManagementError('evidence_missing', 'Referenced tests did not complete successfully in the original task repository.')
                tests[item_id] = command
            for reference in artifact_refs:
                if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
                    raise ManagementError('invalid_change', 'Artifact evidence requires a path and fixed digest.')
                path = Path(reference['path'])
                resolved = path.resolve()
                if not path.is_absolute() or path != resolved or not resolved.is_relative_to(Path(repository['worktree'])) or not resolved.is_file() or any(resolved.is_relative_to(Path(r['worktree'])) for r in repository['nested_repositories']) or resolved.is_relative_to(Path(repository['git_dir'])):
                    raise ManagementError('evidence_missing', 'Artifact evidence must identify an allowed regular file without aliases.')
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
                if digest != reference['sha256']:
                    raise ManagementError('evidence_missing', 'The artifact version no longer matches its evidence.')
                artifacts.append({**reference, 'source': 'local_file_read'})
        current = source_state(repository)
        baseline = record['session']['baseline']
        preserved = record.get('preparation', {}).get('preserved_files', {})
        if any(current['file_digests'].get(name) != digest for name, digest in preserved.items()):
            raise ManagementError('evidence_missing', 'Preserved user files changed; workspace handoff requires reconciliation.')
        source_changed = current['source_digest'] != baseline['source_digest'] or current['head'] != baseline['head']
        commit = report.get('source_commit')
        if source_changed and (not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit) or commit != current['head'] or current['workspace_status'] != baseline['workspace_status']):
            raise ManagementError('evidence_missing', 'Source delivery needs the current fixed commit and a verified handoff preserving pre-existing user content.')
        if commit is not None and commit != current['head']:
            raise ManagementError('evidence_missing', 'The supplied commit does not identify this local delivery version.')
        if source_changed:
            before, after = baseline.get('file_digests', {}), current['file_digests']
            for name in set(before) | set(after):
                if before.get(name) == after.get(name):
                    continue
                blob = subprocess.run(['git', '-C', repository['worktree'], 'show', commit + ':' + name], capture_output=True)
                fixed_digest = hashlib.sha256(blob.stdout).hexdigest() if blob.returncode == 0 else None
                if fixed_digest != after.get(name):
                    raise ManagementError('evidence_missing', 'A changed source file is absent from the supplied fixed commit; user content was preserved.')
        for item_id, test in tests.items():
            if not source_changed and _direct_test_command(test['command']):
                continue
            reader = getattr(manager.delivery_source, 'read_test_version', None)
            if reader is None:
                raise ManagementError('evidence_missing', 'Source changes or custom/compound commands require trusted test runner receipts; completion text and command labels are insufficient.')
            receipt = reader(record['session'], item_id)
            expected_receipt = {'service_id': test['service_id'], 'generation': test['generation'], 'turn_id': test['turn_id'],
                'item_id': item_id, 'command_sha256': hashlib.sha256(test['command'].encode()).hexdigest(),
                'output_digest': test['output_digest'], 'exit_code': 0, 'before_source_digest': current['source_digest'],
                'after_source_digest': current['source_digest'], 'source_access': 'read-only', 'git_access': 'read-only',
                'artifact_roots': repository['test_artifact_paths']}
            if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected_receipt.items()):
                raise ManagementError('evidence_missing', 'The test runner receipt does not verify this stable delivery source and test-only write boundary.')
            tests[item_id] = {**test, 'tested_source_digest': current['source_digest'], 'version_source': 'trusted_host_test_runner'}
        pr_url = report.get('pr_url')
        pr = None
        branches = report.get('sync_branches', [])
        if not isinstance(branches, list) or any(not isinstance(b, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', b) for b in branches) or len(set(branches)) != len(branches):
            raise ManagementError('invalid_change', 'Sync evidence must name distinct local branches.')
        if pr_url is not None:
            if manager.delivery_source is None or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9]\d*', pr_url):
                raise ManagementError('evidence_missing', 'A verified GitHub PR read source is required.')
            pr = manager.delivery_source.read_pr(pr_url)
            issue_repo = scope['url'].split('/issues/')[0]
            if not isinstance(pr, dict) or pr.get('url') != pr_url or not pr_url.startswith(issue_repo + '/pull/') or pr.get('state') not in {'open', 'merged'}:
                raise ManagementError('evidence_missing', 'The PR repository or actual state could not be verified.')
            fixed_head = commit or current['head']
            if pr.get('head_commit') != fixed_head:
                if pr['state'] != 'merged' or not isinstance(pr.get('head_commit'), str) or not re.fullmatch(r'[a-f0-9]{40}', pr['head_commit']):
                    raise ManagementError('evidence_missing', 'The PR does not identify the fixed delivery source.')
                try:
                    _git(repository['worktree'], 'merge-base', '--is-ancestor', pr['head_commit'], fixed_head)
                    _git(repository['worktree'], 'merge-base', '--is-ancestor', pr['merge_commit'], fixed_head)
                except (ManagementError, KeyError, TypeError) as exc:
                    raise ManagementError('evidence_missing', 'The merged PR is not contained in the local fixed delivery version.') from exc
            record['pr_status'] = 'merged' if pr['state'] == 'merged' else 'awaiting_merge' if pr.get('review') == 'approved' else 'awaiting_review'
            record['pr_evidence'] = pr
            with manager._db:
                manager._save(version, data)
            version += 1
        sync = []
        for branch in branches:
            if manager.delivery_source is None:
                raise ManagementError('evidence_missing', 'Actual remote branch reads are required for synchronization evidence.')
            local = _git(repository['worktree'], 'rev-parse', 'refs/heads/' + branch)
            remote = manager.delivery_source.read_branch(scope['url'].split('/issues/')[0], branch)
            if not re.fullmatch(r'[a-f0-9]{40}', local) or remote != local:
                raise ManagementError('evidence_missing', 'A involved local branch is not synchronized with its actual remote hash.')
            sync.append({'branch': branch, 'local_commit': local, 'remote_commit': remote, 'source': 'local_git_and_github_read'})
        merge_required = any(re.search(r'^(?:merge\b|合并(?:此|本|该)?\s*PR|将.+合并到)', c, re.IGNORECASE) for c in expected)
        if any(c.get('pr_evidence') is True for c in criteria) and pr is None:
            raise ManagementError('evidence_missing', 'PR acceptance requires an actual PR read result.')
        if merge_required and (not pr or pr['state'] != 'merged' or not sync or pr.get('base_branch') not in branches):
            raise ManagementError('evidence_missing', 'This Issue requires a verified merge and synchronized local/remote branches.')
        pr_status = 'merged' if pr and pr['state'] == 'merged' else 'awaiting_merge' if pr and pr.get('review') == 'approved' else 'awaiting_review' if pr else 'none'
        record.update(task_delivery='delivered', pr_status=pr_status, repository_released=True, handoff_reason=None, outer_task_status='delivered',
                      test_evidence=list(tests.values()), delivery_evidence={'issue_updated_at': scope['updated_at'], 'criteria': criteria,
                      'source_commit': commit or current['head'], 'source_changed': source_changed, 'execution_end': execution_end, 'leftover_changes': current['workspace_status'], 'workspace': current, 'artifacts': artifacts,
                      'pr': pr, 'sync': sync, 'verified_at': datetime.now(timezone.utc).isoformat()})
        from .takeover import complete_grant
        complete_grant(manager, record, data)
        for arrangement in record.get('execution_arrangements', []):
            if arrangement['id'] == record.get('current_arrangement_id'):
                arrangement.update(phase='delivered', ended_at=record['delivery_evidence']['verified_at'])
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, request_id, 'result', '已按冻结 Issue 验收交付：' + scope['url'] +
                                        '\n运行测试证据：' + str(len(tests)) + ' 项；PR：' + pr_status +
                                        '\n原会话：' + record['session']['thread_id'] + '\n源码版本：' + (commit or '无源码变更'))
        manager.dispatch_tasks()
        return record
