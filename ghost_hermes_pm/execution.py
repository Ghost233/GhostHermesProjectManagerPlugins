"""Single accepted Issue execution, persisted before every external mutation."""
from datetime import datetime, timezone

from .manager import ManagementError, _repository
from .codex import repository_fingerprint


def _responsible(manager, identity, request_id, data):
    record = manager._request(identity, request_id, data)
    principal = manager._principal(identity, data)
    if principal and principal['id'] != record['profile_id']:
        raise ManagementError('forbidden', 'Task execution requires the owner or the assigned responsible Profile.')
    return record


def start_task(manager, identity, request_id):
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        adapter = manager.codex_adapter
        if adapter is None:
            raise ManagementError('capability_unverified', 'Codex execution is not enabled.')
        if record.get('session'):
            raise ManagementError('binding_conflict', 'This task already has a session or an unresolved start intent; reconcile it first.')
        if record['task_start_anchor'] is None:
            raise ManagementError('capability_unverified', 'Public acceptance has not been confirmed on the original request.')
        project = data['projects'][record['project_id']]
        repository = project['repo']
        if any(r['id'] != request_id and r.get('session', {}).get('logical_repository') == repository['logical_id'] and not r.get('repository_released') for r in data['requests'].values()):
            raise ManagementError('repository_busy', 'Another unfinished task owns this logical repository.')
        actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
        if repository_fingerprint(actual) != repository_fingerprint(repository):
            raise ManagementError('capability_unverified', 'The registered repository layout changed; execution evidence is invalid.')
        proof = adapter.verify_start(repository)
        record['session'] = {**adapter.connection, 'thread_id': None, 'turn_id': None,
                             'logical_repository': repository['logical_id'], 'start_phase': 'thread_start_intent',
                             'control': 'assigned_task', 'capability': proof}
        record.update(execution='unverified', unexecuted_reason='Thread creation needs confirmation.',
                      task_delivery='unmet', pr_status='none', repository_released=False)
        with manager._db:
            manager._save(version, data)
        try:
            response = adapter.start_thread(repository, proof)
            thread = response.get('thread', {})
            # Save even an unexpected thread before checking its write capability.
            if isinstance(thread.get('id'), str) and thread['id']:
                record['session']['thread_id'] = thread['id']
                record['session']['start_phase'] = 'thread_registered'
                with manager._db:
                    manager._save(version + 1, data)
            adapter.verify_thread(response, repository, proof)
            record['session']['start_phase'] = 'turn_start_intent'
            with manager._db:
                manager._save(version + 2, data)
            prompt = ('Complete this accepted Issue using the repository rules and Matt workflow.\n'
                      'Accepted scope is frozen; source changes require an explicit addition.\n'
                      'Goal and acceptance:\n' + record['accepted_scope']['title'] + '\n' + record['accepted_scope']['body'] +
                      '\nIssue: ' + record['accepted_scope']['url'] + '\nAccepted Issue version: ' + record['accepted_scope']['updated_at'] +
                      '\nRepository boundary: ' + str(repository) +
                      '\nOnly modify this repository own source and Git metadata; nested repositories remain read-only. '
                      'Tests may write only registered artifacts. Do not expand permissions or use full access. '
                      'GitHub authentication must switch to Ghost233 and verify the actual login before every authenticated business command. '
                      'After remote writes sync affected local branches only by fast-forward. Protect user changes. '
                      'Report executed tests and fixed delivery evidence; do not call a turn end delivery or require a PR for test-only work.')
            result = adapter.start_turn(thread['id'], prompt)
            turn = result.get('turn', {})
            if not isinstance(turn.get('id'), str) or not turn['id']:
                raise ManagementError('outcome_unknown', 'The turn identity was not confirmed.')
            record['session'].update(turn_id=turn['id'], start_phase='turn_registered')
            record.update(execution='running', unexecuted_reason=None, last_execution_verified_at=datetime.now(timezone.utc).isoformat())
            with manager._db:
                manager._save(version + 3, data)
        except ManagementError as exc:
            record.update(execution='unverified', unexecuted_reason=str(exc))
            with manager._db:
                current, _ = manager._load()
                manager._save(current, data)
            raise
        manager.publish_request_message(identity, request_id, 'progress', 'Codex 已核实运行。Issue：' + record['accepted_scope']['url'] +
                                        '\n原会话：' + thread['id'] + '\n交付：尚未满足验收；PR：无。')
        return {'status': 'running', 'request_id': request_id, 'session': record['session']}
