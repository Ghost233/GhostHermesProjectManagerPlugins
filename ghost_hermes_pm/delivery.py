"""Verify frozen Issue acceptance from service/Git/file evidence, not completion prose."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess

from .manager import ManagementError, _git, _public_text


def source_state(repository):
    root = Path(repository['worktree'])
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    head = result.stdout.strip() if result.returncode == 0 else None
    files = _git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0')
    digest = hashlib.sha256()
    artifacts = [Path(p) for p in repository['test_artifact_paths']]
    for name in sorted(set(files) - {''}):
        path = root / name
        if any(path.is_relative_to(p) for p in artifacts):
            continue
        digest.update(name.encode())
        if path.is_symlink():
            digest.update(path.readlink().as_posix().encode())
        elif path.is_file():
            digest.update(path.read_bytes())
        elif not path.exists():
            digest.update(b'<deleted>')
    return {'head': head, 'source_digest': digest.hexdigest(), 'workspace_status': _git(root, 'status', '--porcelain=v1')}


def acceptance_criteria(body):
    checks = re.findall(r'^\s*[-*]\s+\[[ xX]\]\s+(.+)$', body, re.MULTILINE)
    return checks or [body]


def record_task_delivery(manager, identity, request_id, report):
    from .execution import _responsible, refresh_task
    if not isinstance(report, dict) or set(report) - {'issue_updated_at', 'criteria', 'source_commit', 'pr_url', 'sync_branches'}:
        raise ManagementError('invalid_change', 'Delivery accepts evidence references, never passed or delivered declarations.')
    with manager._lock:
        refreshed = refresh_task(manager, identity, request_id)
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        if record['execution'] != 'turn_ended' or record.get('turn_status') != 'completed' or not record.get('history_complete'):
            raise ManagementError('evidence_missing', 'Original execution and complete terminal history must be verified before delivery.')
        scope = record['accepted_scope']
        expected = acceptance_criteria(scope['body'])
        criteria = report.get('criteria')
        if report.get('issue_updated_at') != scope['updated_at'] or not isinstance(criteria, list) or [c.get('text') for c in criteria if isinstance(c, dict)] != expected:
            raise ManagementError('evidence_missing', 'Every frozen Issue acceptance item must have an exact evidence association.')
        repository = data['projects'][record['project_id']]['repo']
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
        source_changed = current['source_digest'] != baseline['source_digest'] or current['head'] != baseline['head']
        commit = report.get('source_commit')
        if source_changed and (not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit) or commit != current['head'] or current['workspace_status'] != baseline['workspace_status']):
            raise ManagementError('evidence_missing', 'Source delivery needs the current fixed commit and a verified handoff preserving pre-existing user content.')
        if commit is not None and commit != current['head']:
            raise ManagementError('evidence_missing', 'The supplied commit does not identify this local delivery version.')
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
        record.update(task_delivery='delivered', pr_status=pr_status, repository_released=True,
                      test_evidence=list(tests.values()), delivery_evidence={'issue_updated_at': scope['updated_at'], 'criteria': criteria,
                      'source_commit': commit, 'source_changed': source_changed, 'workspace': current, 'artifacts': artifacts,
                      'pr': pr, 'sync': sync, 'verified_at': datetime.now(timezone.utc).isoformat()})
        with manager._db:
            manager._save(version, data)
        manager.publish_request_message(identity, request_id, 'result', '已按冻结 Issue 验收交付：' + scope['url'] +
                                        '\n运行测试证据：' + str(len(tests)) + ' 项；PR：' + pr_status +
                                        '\n原会话：' + record['session']['thread_id'] + '\n源码版本：' + (commit or '无源码变更'))
        return record
