"""Single accepted Issue execution, persisted before every external mutation."""
from datetime import datetime, timezone
from pathlib import Path

from .manager import ManagementError, _repository
from .codex import repository_fingerprint


def _responsible(manager, identity, request_id, data):
    record = manager._request(identity, request_id, data)
    principal = manager._principal(identity, data)
    if principal and principal['id'] != record['profile_id']:
        raise ManagementError('forbidden', 'Task execution requires the owner or the assigned responsible Profile.')
    return record



def _current_assignment(manager, record, data):
    profile = data['profiles'].get(record['profile_id'])
    accepted = record.get('accepted_responsibility')
    if not profile or not isinstance(accepted, dict) or any(profile.get(k) != v for k, v in accepted.items()) or profile.get('project_id') != record['project_id'] or profile.get('capability') != 'development' or profile.get('role') not in {'project_lead', 'subproject_lead'}:
        raise ManagementError('forbidden', 'The current Profile no longer has the accepted responsibility; original work requires reconciliation.')
    if record.get('session', {}).get('origin') == 'manual_takeover':
        from .takeover import _assignment
        _assignment(record, data)
        return
    service_ref = profile.get('connection_refs', {}).get('codex')
    if service_ref != manager.codex_adapter.service_ref or (record.get('accepted_codex_ref') is not None and record['accepted_codex_ref'] != service_ref):
        raise ManagementError('capability_unverified', 'The actual executor does not match the responsible Profile local Codex binding.')
    if record.get('accepted_repository_fingerprint') != repository_fingerprint(data['projects'][record['project_id']]['repo']):
        raise ManagementError('capability_unverified', 'The repository boundary differs from the accepted task scope.')
    repository = data['projects'][record['project_id']]['repo']
    if any(manager.state_dir.is_relative_to(Path(repository[k])) for k in ('worktree', 'git_dir', 'common_dir')):
        raise ManagementError('capability_unverified', 'Authoritative manager and validation/test receipts must be outside the task writable repository and Git metadata.')

def start_task(manager, identity, request_id):
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        adapter = manager.codex_adapter
        if adapter is None:
            raise ManagementError('capability_unverified', 'Codex execution is not enabled.')
        _current_assignment(manager, record, data)
        if record.get('session'):
            raise ManagementError('binding_conflict', 'This task already has a session or an unresolved start intent; reconcile it first.')
        if record['task_start_anchor'] is None:
            raise ManagementError('capability_unverified', 'Public acceptance has not been confirmed on the original request.')
        project = data['projects'][record['project_id']]
        repository = project['repo']
        from .queue import require_turn, require_preparation
        require_turn(manager, identity, record, version, data)
        if any(r['id'] != request_id and r.get('session', {}).get('logical_repository') == repository['logical_id'] and not r.get('repository_released') for r in data['requests'].values()):
            raise ManagementError('repository_busy', 'Another unfinished task owns this logical repository.')
        actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
        if repository_fingerprint(actual) != repository_fingerprint(repository):
            raise ManagementError('capability_unverified', 'The registered repository layout changed; execution evidence is invalid.')
        baseline = require_preparation(manager, identity, record, version, data)
        from .memory import start_context
        memory_text = start_context(manager, identity, record, data)
        occupation = record['queue'].get('external_occupancy')
        if occupation and occupation.get('generation') != adapter.generation:
            raise ManagementError('capability_unverified', 'The original external occupancy generation is unavailable; reconciliation is required.')
        try:
            proof = adapter.verify_start(repository)
        except ManagementError as exc:
            if exc.code == 'repository_busy':
                record['queue']['external_occupancy'] = {**(getattr(adapter, 'last_start_occupancy', None) or {}),
                    'status': 'unknown', 'generation': adapter.generation, 'connection': adapter.connection, 'reason': str(exc),
                    'observed_at': datetime.now(timezone.utc).isoformat()}
            record['unexecuted_reason'] = str(exc)
            with manager._db:
                manager._save(version, data)
            manager.publish_request_message(identity, request_id, 'progress', '仓库执行待核对；未开启新任务：' + str(exc))
            raise
        record['queue'].pop('external_occupancy', None)
        record['session'] = {**adapter.connection, 'thread_id': None, 'turn_id': None,
                             'logical_repository': repository['logical_id'], 'start_phase': 'thread_start_intent',
                             'control': 'assigned_task', 'capability': proof, 'baseline': baseline, 'repository': repository}
        record.update(execution='unverified', unexecuted_reason='Thread creation needs confirmation.',
                      task_delivery='unmet', pr_status='none', repository_released=False, outer_task_status='execution_pending')
        with manager._db:
            manager._save(version, data)
        try:
            response = adapter.start_thread(repository, proof)
            thread = response.get('thread') if isinstance(response.get('thread'), dict) else {}
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
                      '\nConfirmed task baseline and dependencies: ' + str(record['preparation']['plan']) +
                      '\nPreserved user paths and digests: ' + str(record['preparation']['preserved_files']) +
                      '\nDo not modify preserved user paths; report any conflict before proceeding. ' +
                      '\nOnly modify this repository own source and Git metadata; nested repositories remain read-only. '
                      'Tests may write only registered artifacts. Do not expand permissions or use full access. '
                      'GitHub authentication must switch to Ghost233 and verify the actual login before every authenticated business command. '
                      'After remote writes sync affected local branches only by fast-forward. Protect user changes. '
                      'Report executed tests and fixed delivery evidence; do not call a turn end delivery or require a PR for test-only work.')
            prompt += memory_text
            result = adapter.start_turn(thread['id'], prompt)
            turn = result.get('turn') if isinstance(result.get('turn'), dict) else {}
            if not isinstance(turn.get('id'), str) or not turn['id']:
                raise ManagementError('outcome_unknown', 'The turn identity was not confirmed.')
            record['session'].update(turn_id=turn['id'], start_phase='turn_registered')
            if record.get('memory_context'):
                import hashlib
                record['memory_context'].update(status='loaded', thread_id=thread['id'], turn_id=turn['id'],
                    generation=adapter.generation, loaded_at=datetime.now(timezone.utc).isoformat(),
                    input_digest=hashlib.sha256(prompt.encode()).hexdigest(), verification='original_turn_start_receipt')
            record.update(execution='running', outer_task_status='running', unexecuted_reason=None, last_execution_verified_at=datetime.now(timezone.utc).isoformat(),
                          execution_capability={'status': 'verified', 'enabled': True, 'connection': adapter.connection, 'proof': proof})
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


def refresh_task(manager, identity, request_id):
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        if record.get('stop', {}).get('status') == 'processing':
            from .control import refresh_stop
            return refresh_stop(manager, identity, request_id)
        if record.get('outer_task_status') == 'stopped':
            return record
        session = record.get('session')
        from .takeover import bind_executor
        adapter = bind_executor(manager, record)
        if not session or not session.get('thread_id'):
            raise ManagementError('binding_conflict', 'No confirmed original thread is available for observation.')
        if adapter is None or adapter.generation != session['generation']:
            record.update(execution='unverified', unexecuted_reason='The original executor generation is unavailable; matching history is not control identity.')
        else:
            try:
                repository = session.get('repository') or data['projects'][record['project_id']]['repo']
                actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
                if repository_fingerprint(actual) != session['capability']['repository_fingerprint']:
                    raise ManagementError('capability_unverified', 'Repository permissions changed; execution requires reconciliation.')
                thread = adapter.read_thread(session['thread_id'])
                if thread.get('id') != session['thread_id'] or thread.get('cwd') != repository['worktree']:
                    raise ManagementError('capability_unverified', 'The observation does not identify the original task and boundary.')
                from .questions import sync_human_requests
                sync_human_requests(manager, record, adapter, thread)
                events = adapter.take_events(session['thread_id'])
                record.setdefault('service_events', []).extend({'method': e['method'], 'turn_id': e.get('params', {}).get('turnId'),
                                                              'request_id': e.get('id'), 'generation': adapter.generation}
                                                             for e in events[-100:])
                record['service_events'] = record['service_events'][-100:]
                turns = thread.get('turns', [])
                turn = next((t for t in turns if t.get('id') == session['turn_id']), None)
                status = thread.get('status', {})
                flags = status.get('activeFlags', [])
                state = 'unverified'
                if status.get('type') == 'active':
                    state = 'waiting_approval' if 'waitingOnApproval' in flags else 'waiting_input' if 'waitingOnUserInput' in flags else 'running'
                elif status.get('type') == 'idle' and turn and turn.get('status') in {'completed', 'failed', 'interrupted'}:
                    state = 'turn_ended'
                related = []
                items = turn.get('items', []) if turn else []
                for item in items:
                    if item.get('type') in {'commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange'} and item.get('status') == 'inProgress':
                        related.append(item.get('id'))
                    if item.get('type') == 'collabAgentToolCall':
                        for child in item.get('receiverThreadIds', []):
                            child_thread = adapter.read_thread(child)
                            if child_thread.get('status', {}).get('type') != 'idle':
                                related.append(child)
                if state == 'turn_ended' and related:
                    state = 'related_execution'
                record.update(execution=state, unexecuted_reason=None if state != 'unverified' else 'The original turn state is incomplete.',
                              turn_status=turn.get('status') if turn else None, related_execution=related,
                              history_complete=bool(turn and turn.get('itemsView') == 'full'),
                              last_execution_verified_at=datetime.now(timezone.utc).isoformat())
                import hashlib
                from .manager import _public_text
                commands = []
                for item in items:
                    if item.get('type') != 'commandExecution' or item.get('status') not in {'completed', 'failed', 'declined'}:
                        continue
                    command = item.get('command')
                    try:
                        _public_text(command, manager._sensitive_values())
                    except ManagementError:
                        continue
                    commands.append({'item_id': item['id'], 'turn_id': session['turn_id'], 'command': command,
                                     'cwd': item.get('cwd'), 'status': item['status'], 'exit_code': item.get('exitCode'),
                                     'output_digest': hashlib.sha256((item.get('aggregatedOutput') or '').encode()).hexdigest(),
                                     'source': 'codex_command_execution', 'service_id': session['service_id'],
                                     'generation': session['generation'], 'observed_at': record['last_execution_verified_at']})
                record['command_evidence'] = commands
                record.setdefault('test_evidence', [])
            except ManagementError as exc:
                record.update(execution='unverified', unexecuted_reason=str(exc))
        with manager._db:
            manager._save(version, data)
        report_state = (record['execution'], record.get('turn_status'))
        if tuple(record.get('execution_report_state', ())) != report_state:
            record['execution_report_state'] = report_state
            with manager._db:
                current, data = manager._load()
                data['requests'][request_id]['execution_report_state'] = report_state
                manager._save(current, data)
            manager.publish_request_message(identity, request_id, 'progress', '执行核对：' + record['execution'] + '\nIssue：' + record['accepted_scope']['url'] +
                                        '\n交付：' + record.get('task_delivery', 'unmet') + '；PR：' + record.get('pr_status', 'none') +
                                        '\n核实时间：' + record.get('last_execution_verified_at', '待核对'))
        from .questions import notify_human_requests
        notify_human_requests(manager, identity, request_id)
        return record


def verify_task_execution(manager, identity, request_id):
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        repository = data['projects'][record['project_id']]['repo']
        adapter = manager.codex_adapter
        if adapter is None:
            result = {'status': 'blocked', 'enabled': False, 'reason': 'No registered local stdio executor.'}
        else:
            try:
                _current_assignment(manager, record, data)
                proof = adapter.verify_start(repository)
                result = {'status': 'verified', 'enabled': True, 'connection': adapter.connection, 'proof': proof}
            except ManagementError as exc:
                result = {'status': 'blocked', 'enabled': False, 'code': exc.code, 'reason': str(exc), 'connection': adapter.connection}
        record['execution_capability'] = result
        with manager._db:
            manager._save(version, data)
        return result
