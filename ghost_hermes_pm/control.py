"""Control the original task execution, with durable intents and no replay."""
from datetime import datetime, timezone
from pathlib import Path

from .codex import repository_fingerprint
from .execution import _responsible, _current_assignment
from .manager import ManagementError, _public_text, _repository


def _now():
    return datetime.now(timezone.utc).isoformat()


def _binding(manager, identity, request_id, data, action):
    record = _responsible(manager, identity, request_id, data)
    session, adapter = record.get('session'), manager.codex_adapter
    if not session or not session.get('thread_id') or session.get('control') != 'assigned_task':
        raise ManagementError('forbidden', 'No effective original-task control authorization is available.')
    if adapter is None or adapter.generation != session['generation'] or not adapter.connection or adapter.connection['service_id'] != session['service_id']:
        raise ManagementError('capability_unverified', 'The original executor generation is unavailable; history is not control.')
    if manager._principal(identity, data) is not None:
        _current_assignment(manager, record, data)
    repository = session['repository']
    actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
    if repository_fingerprint(actual) != session['capability']['repository_fingerprint'] or any(manager.state_dir.is_relative_to(Path(repository[k])) for k in ('worktree', 'git_dir', 'common_dir')):
        raise ManagementError('capability_unverified', 'The original repository control boundary needs reconciliation.')
    adapter.verify_control(repository, action)
    return record, session, adapter


def _thread(adapter, session):
    thread = adapter.read_thread(session['thread_id'])
    if thread.get('id') != session['thread_id'] or thread.get('cwd') != session['repository']['worktree'] or thread.get('canAcceptDirectInput') is not True:
        raise ManagementError('capability_unverified', 'The original thread and direct-input authority could not be verified.')
    return thread


def control_task(manager, identity, request_id, action, instruction_id, text=None, expected_turn_id=None):
    if action != 'append' or not isinstance(instruction_id, str) or not instruction_id or len(instruction_id) > 256:
        raise ManagementError('invalid_change', 'An explicit task action and stable instruction ID are required.')
    _public_text(text, manager._sensitive_values())
    with manager._lock:
        version, data = manager._load()
        record, session, adapter = _binding(manager, identity, request_id, data, action)
        existing = next((c for c in record.get('controls', []) if c['id'] == instruction_id), None)
        request = {'action': action, 'text': text, 'expected_turn_id': expected_turn_id, 'actor': identity.subject}
        if existing:
            if any(existing.get(k) != v for k, v in request.items()):
                raise ManagementError('binding_conflict', 'An instruction ID is already bound to different control content.')
            return {'status': 'accepted', 'duplicate': True, 'instruction': existing, 'execution': record['execution']}
        thread = _thread(adapter, session)
        active = [t for t in thread.get('turns', []) if t.get('status') == 'inProgress']
        if thread['status'].get('type') != 'active' or len(active) != 1 or active[0].get('id') != session['turn_id'] or expected_turn_id != session['turn_id']:
            raise ManagementError('binding_conflict', 'The expected original active turn does not match; nothing was sent.')
        instruction = {'id': instruction_id, **request, 'thread_id': session['thread_id'], 'turn_id': session['turn_id'],
                       'generation': session['generation'], 'phase': 'rpc_intent', 'accepted_at': _now()}
        record.setdefault('controls', []).append(instruction)
        with manager._db:
            manager._save(version, data)
        try:
            result = adapter.steer_turn(session['thread_id'], session['turn_id'], text, instruction_id)
            if result.get('turnId') != session['turn_id']:
                raise ManagementError('outcome_unknown', 'The service did not confirm the expected original turn.')
            instruction.update(phase='rpc_accepted', rpc_accepted_at=_now())
        except ManagementError as exc:
            instruction.update(phase='rejected' if exc.code == 'service_rejected' else 'outcome_unknown', reason=str(exc))
            raise
        finally:
            with manager._db:
                manager._save(version + 1, data)
        manager.publish_request_message(identity, request_id, 'progress', '追加要求已由原会话受理；执行结果仍需核对。')
        return {'status': 'accepted', 'duplicate': False, 'instruction': instruction, 'execution': record['execution']}
