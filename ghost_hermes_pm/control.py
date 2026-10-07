"""Control the original task execution, with durable intents and no replay."""
from datetime import datetime, timezone
from pathlib import Path

from .codex import repository_fingerprint
from .execution import _responsible, _current_assignment
from .manager import ManagementError, _public_text, _repository


def _now():
    return datetime.now(timezone.utc).isoformat()


def _authorize(manager, identity, request_id, data):
    record = _responsible(manager, identity, request_id, data)
    session = record.get('session')
    if session and session.get('origin') == 'manual_takeover' and data.get('control_grants', {}).get(record.get('control_grant_id'), {}).get('status') != 'active':
        raise ManagementError('forbidden', 'This current-work manual grant is inactive; the original session is observe-only.')
    if not session or not session.get('thread_id') or session.get('control') != 'assigned_task' or record.get('task_delivery') == 'delivered':
        raise ManagementError('forbidden', 'No effective original-task control authorization is available.')
    if manager._principal(identity, data) is not None:
        profile = data['profiles'].get(record['profile_id'], {})
        accepted = record.get('accepted_responsibility')
        if not isinstance(accepted, dict) or any(profile.get(k) != v for k, v in accepted.items()) or profile.get('connection_refs', {}).get('codex') != record.get('accepted_codex_ref') or record.get('accepted_repository_fingerprint') != repository_fingerprint(data['projects'][record['project_id']]['repo']):
            raise ManagementError('forbidden', 'The original task control responsibility is no longer current.')
    return record, session


def _binding(manager, identity, request_id, data, action):
    record, session = _authorize(manager, identity, request_id, data)
    from .takeover import bind_executor
    adapter = bind_executor(manager, record)
    if adapter is None or adapter.generation != session['generation'] or not adapter.connection or adapter.connection['service_id'] != session['service_id']:
        raise ManagementError('capability_unverified', 'The original executor generation is unavailable; history is not control.')
    repository = session['repository']
    actual = _repository({'repo_path': repository['worktree'], 'test_artifact_paths': repository['test_artifact_paths']})
    if repository_fingerprint(actual) != session['capability']['repository_fingerprint'] or any(manager.state_dir.is_relative_to(Path(repository[k])) for k in ('worktree', 'git_dir', 'common_dir')):
        raise ManagementError('capability_unverified', 'The original repository control boundary needs reconciliation.')
    adapter.verify_control(repository, action, session['capability'])
    return record, session, adapter


def _thread(adapter, session, require_input=True):
    thread = adapter.read_thread(session['thread_id'])
    if thread.get('id') != session['thread_id'] or thread.get('cwd') != session['repository']['worktree'] or (require_input and thread.get('canAcceptDirectInput') is not True):
        raise ManagementError('capability_unverified', 'The original thread and direct-input authority could not be verified.')
    return thread


def control_task(manager, identity, request_id, action, instruction_id, text=None, expected_turn_id=None):
    if action not in {'append', 'stop', 'continue'} or not isinstance(instruction_id, str) or not instruction_id or len(instruction_id) > 256:
        raise ManagementError('invalid_change', 'An explicit task action and stable instruction ID are required.')
    if action in {'append', 'continue'}:
        _public_text(text, manager._sensitive_values())
    elif text is not None:
        raise ManagementError('invalid_change', 'Stop does not accept additional execution input.')
    with manager._lock:
        version, data = manager._load()
        record, session = _authorize(manager, identity, request_id, data)
        if action != 'stop':
            from .lifecycle import require_active
            require_active(data, record['profile_id'], record['project_id'])
        existing = next((c for c in record.get('controls', []) if c['id'] == instruction_id), None)
        request = {'action': action, 'text': text, 'expected_turn_id': expected_turn_id, 'actor': identity.subject}
        if existing:
            if any(existing.get(k) != v for k, v in request.items()):
                raise ManagementError('binding_conflict', 'An instruction ID is already bound to different control content.')
            return {'status': existing['phase'] if existing['phase'] in {'outcome_unknown', 'rejected'} else 'accepted', 'duplicate': True, 'instruction': existing, 'execution': record['execution']}
        pending = record.get('queue', {}).get('pending_continuation')
        if action == 'continue' and pending:
            if pending['id'] != instruction_id or any(pending.get(k) != v for k, v in request.items()):
                raise ManagementError('binding_conflict', 'A queued continuation is already bound to this arrangement and authorization.')
            if record['queue']['blocked_by']:
                return {'status': 'queued', 'duplicate': True, 'execution': record['execution'], 'queue': record['queue']}
        if action == 'stop':
            return _stop(manager, identity, request_id, version, data, record, session, instruction_id, request)
        confirmed_at = record.get('stop', {}).get('confirmed_at', '')
        if any(c['action'] in {'append', 'continue'} and c['phase'] in {'rpc_intent', 'outcome_unknown'} and c['accepted_at'] > confirmed_at for c in record.get('controls', [])):
            raise ManagementError('binding_conflict', 'An earlier input outcome is unresolved; reconcile it before any new input.')
        if action == 'continue':
            if record.get('stop', {}).get('status') != 'confirmed' or record.get('outer_task_status') != 'stopped':
                raise ManagementError('binding_conflict', 'Explicit continuation requires a confirmed prior stop.')
        elif record.get('stop', {}).get('status') == 'processing' or record.get('outer_task_status') == 'stopped':
            raise ManagementError('binding_conflict', 'Stopped work requires an explicit new execution arrangement.')
        record, session, adapter = _binding(manager, identity, request_id, data, action)
        if action == 'append':
            from .observation import guard_repository
            guard_repository(manager, identity, record, version, data)
        if action == 'continue':
            _current_assignment(manager, record, data)
            if expected_turn_id != session['turn_id']:
                raise ManagementError('binding_conflict', 'The expected original turn does not match; nothing was sent.')
            from .queue import enroll, require_turn, require_preparation
            if not pending:
                enroll(data, record, session['repository'])
                record['queue']['pending_continuation'] = {'id': instruction_id, **request}
                record.setdefault('execution_arrangements', []).append({'id': instruction_id, 'request_id': request_id,
                    'thread_id': session['thread_id'], 'previous_stop_id': record['stop']['instruction_id'],
                    'previous_turn_id': session['turn_id'], 'turn_id': None, 'phase': 'queued',
                    'authorized_by': identity.subject, 'created_at': _now(), 'generation': session['generation']})
                with manager._db:
                    manager._save(version, data)
                version += 1
            require_turn(manager, identity, record, version, data)
            require_preparation(manager, identity, record, version, data)
            adapter.verify_start(session['repository'])
        thread = _thread(adapter, session)
        active = [t for t in thread.get('turns', []) if t.get('status') == 'inProgress']
        if expected_turn_id != session['turn_id']:
            raise ManagementError('binding_conflict', 'The expected original turn does not match; nothing was sent.')
        method = 'turn/steer' if thread['status'].get('type') == 'active' else 'turn/start'
        if action == 'continue' and method != 'turn/start':
            raise ManagementError('binding_conflict', 'Explicit continuation requires verified idle new-turn semantics.')
        if method == 'turn/steer' and (len(active) != 1 or active[0].get('id') != session['turn_id']):
            raise ManagementError('binding_conflict', 'The expected original active turn does not match; nothing was sent.')
        known_turn_ids = session.setdefault('known_turn_ids', [session['turn_id']])
        if method == 'turn/start':
            adapter.verify_control(session['repository'], 'idle_input', session['capability'])
            adapter.verify_idle(thread, session['turn_id'], known_turn_ids)
        instruction = {'id': instruction_id, **request, 'thread_id': session['thread_id'], 'turn_id': session['turn_id'],
                       'generation': session['generation'], 'method': method, 'previous_turn_id': session['turn_id'],
                       'phase': 'rpc_intent', 'accepted_at': _now()}
        record.setdefault('controls', []).append(instruction)
        arrangement = None
        if action == 'continue':
            from .delivery import source_state
            arrangement = next(a for a in record['execution_arrangements'] if a['id'] == instruction_id)
            arrangement.update(phase='rpc_intent', baseline=source_state(session['repository']))
            record['queue'].pop('pending_continuation', None)
            record.update(current_arrangement_id=instruction_id, execution='unverified', outer_task_status='execution_pending', repository_released=False)
        with manager._db:
            manager._save(version, data)
        try:
            if method == 'turn/steer':
                result = adapter.steer_turn(session['thread_id'], session['turn_id'], text, instruction_id)
                if result.get('turnId') != session['turn_id']:
                    raise ManagementError('outcome_unknown', 'The service did not confirm the expected original turn.')
            else:
                execution_text = text
                if action == 'continue':
                    execution_text += '\nConfirmed continuation baseline and dependencies: ' + str(record['preparation']['plan']) + '\nPreserve these user paths without modification: ' + str(record['preparation']['preserved_files'])
                result = adapter.start_idle_turn(session['thread_id'], session['turn_id'], execution_text, instruction_id,
                                                 expected_cwd=session['repository']['worktree'], known_turn_ids=known_turn_ids)
                session['turn_id'] = result['turn']['id']
                known_turn_ids.append(session['turn_id'])
                instruction['turn_id'] = session['turn_id']
                record.update(execution='running', outer_task_status='running', repository_released=False)
                if arrangement is not None:
                    arrangement.update(turn_id=session['turn_id'], phase='running')
            instruction.update(phase='rpc_accepted', rpc_accepted_at=_now())
        except ManagementError as exc:
            instruction.update(phase='rejected' if exc.code == 'service_rejected' else 'outcome_unknown', reason=str(exc))
            if arrangement is not None:
                arrangement.update(phase=instruction['phase'], reason=str(exc))
            raise
        finally:
            with manager._db:
                manager._save(version + 1, data)
        manager.publish_request_message(identity, request_id, 'progress', '已登记明确继续的新执行安排；旧停止记录保留。执行结果仍需核对。' if action == 'continue' else '追加要求已由原会话受理；执行结果仍需核对。')
        return {'status': 'accepted', 'duplicate': False, 'instruction': instruction, 'execution': record['execution']}


def _stop(manager, identity, request_id, version, data, record, session, instruction_id, request):
    if request['expected_turn_id'] != session['turn_id']:
        raise ManagementError('binding_conflict', 'The requested original turn does not match; nothing was interrupted.')
    if record.get('stop', {}).get('status') == 'processing' or record.get('outer_task_status') == 'stopped':
        raise ManagementError('binding_conflict', 'This execution already has a durable stop decision.')
    instruction = {'id': instruction_id, **request, 'thread_id': session['thread_id'], 'turn_id': session['turn_id'],
                   'generation': session['generation'], 'phase': 'rpc_intent', 'accepted_at': _now()}
    stop = {'instruction_id': instruction_id, 'thread_id': session['thread_id'], 'turn_id': session['turn_id'],
            'generation': session['generation'], 'status': 'processing', 'rpc_status': 'pending',
            'requested_at': _now(), 'related_execution': [], 'evidence': []}
    record.setdefault('controls', []).append(instruction)
    record.setdefault('stop_records', []).append(stop)
    record.update(stop=stop, execution='stopping', repository_released=False, outer_task_status='stopping')
    with manager._db:
        manager._save(version, data)
    try:
        _, _, adapter = _binding(manager, identity, request_id, data, 'stop')
        thread = _thread(adapter, session, require_input=False)
        active = [t for t in thread.get('turns', []) if t.get('status') == 'inProgress']
        if thread['status'].get('type') == 'active':
            if len(active) != 1 or active[0].get('id') != session['turn_id']:
                raise ManagementError('binding_conflict', 'The original active turn changed; stop needs reconciliation.')
            result = adapter.interrupt_turn(session['thread_id'], session['turn_id'])
            if result != {}:
                raise ManagementError('outcome_unknown', 'Interrupt returned an unrecognized acknowledgement.')
            stop['rpc_status'] = 'accepted'
        else:
            stop['rpc_status'] = 'not_sent'
        instruction.update(phase='rpc_accepted' if stop['rpc_status'] == 'accepted' else 'verification_pending')
    except ManagementError as exc:
        instruction.update(phase='rejected' if exc.code == 'service_rejected' else 'outcome_unknown', reason=str(exc))
        stop.update(rpc_status=instruction['phase'], reason=str(exc))
    with manager._db:
        manager._save(version + 1, data)
    manager.publish_request_message(identity, request_id, 'progress', '停止处理中；中断受理：' + stop['rpc_status'] + '。原回合及相关后台执行尚待核实，仓库占用保留。')
    return {'status': 'accepted', 'duplicate': False, 'instruction': instruction, 'execution': 'stopping', 'stop': stop}


def refresh_stop(manager, identity, request_id, *, sampling=False):
    """A turn end is insufficient: every related thread and background page counts."""
    with manager._lock:
        version, data = manager._load()
        record = _responsible(manager, identity, request_id, data)
        from .notifications import meaningful_observation
        before = meaningful_observation(record)
        stop = next(s for s in record['stop_records'] if s['instruction_id'] == record['stop']['instruction_id'])
        record['stop'] = stop
        try:
            _, session, adapter = _binding(manager, identity, request_id, data, 'related_execution')
            thread = _thread(adapter, session, require_input=False)
            turn = next((t for t in thread.get('turns', []) if t.get('id') == stop['turn_id']), None)
            if not turn or turn.get('status') not in {'completed', 'failed', 'interrupted'} or turn.get('itemsView') != 'full' or thread['status'].get('type') != 'idle':
                raise ManagementError('capability_unverified', 'The original turn has no complete terminal evidence.')
            related, evidence = terminal_evidence(adapter, session, thread, stop['turn_id'])
            stop.update(related_execution=related, evidence=evidence, last_verified_at=_now())
            record.update(related_execution=related, last_execution_verified_at=stop['last_verified_at'], turn_status=turn['status'])
            if related:
                stop['reason'] = 'Related execution remains active or lacks complete terminal evidence.'
            else:
                from .queue import workspace
                stop['handoff'] = {'workspace': workspace(session['repository']), 'verified_at': _now()}
                stop.update(status='confirmed', confirmed_at=_now(), reason=None)
                for instruction in record.get('controls', []):
                    if instruction['id'] == stop['instruction_id']:
                        instruction['phase'] = 'stop_confirmed'
                for arrangement in record.get('execution_arrangements', []):
                    if arrangement['id'] == record.get('current_arrangement_id'):
                        arrangement.update(phase='stopped', ended_at=stop['confirmed_at'])
                record.update(execution='stopped', outer_task_status='stopped', repository_released=True, unexecuted_reason=None)
        except ManagementError as exc:
            stop.update(reason=str(exc), last_verification_attempt_at=_now())
        if stop['status'] != 'confirmed':
            record.update(execution='stopping', outer_task_status='stopping', repository_released=False, unexecuted_reason=stop.get('reason'))
        if not sampling or before != meaningful_observation(record):
            with manager._db:
                manager._save(version, data)
        report_state = (stop['status'], stop.get('rpc_status'), stop.get('reason'), str(stop.get('related_execution')))
        if tuple(record.get('stop_report_state', ())) != report_state:
            record['stop_report_state'] = report_state
            with manager._db:
                current, data = manager._load()
                data['requests'][request_id]['stop_report_state'] = report_state
                manager._save(current, data)
            message = ('已核实停止，外层任务结束；原会话、改动与停止证据保留。' if stop['status'] == 'confirmed' else
                       '停止处理中；原回合及相关执行核实未完成，仓库占用保留。' + (stop.get('reason') or ''))
            manager.publish_request_message(identity, request_id, 'progress', message)
        if stop['status'] == 'confirmed':
            manager.dispatch_tasks()
        return record


def terminal_evidence(adapter, session, thread, turn_id):
    pending = [(thread, thread['turns'])]
    seen, related, evidence = set(), [], []
    loaded = adapter.loaded_threads()
    if any(not isinstance(t, str) or not t for t in loaded):
        raise ManagementError('capability_unverified', 'Loaded thread coverage is malformed.')
    for thread_id in loaded:
        if thread_id != session['thread_id']:
            other = adapter.read_thread(thread_id)
            if other.get('id') != thread_id:
                raise ManagementError('capability_unverified', 'Loaded execution identity is incomplete.')
            from .queue import logical_repository
            if logical_repository(other.get('cwd')) == session['logical_repository']:
                pending.append((other, other.get('turns', [])))
    while pending:
        if len(seen) >= 100:
            raise ManagementError('capability_unverified', 'Related execution coverage exceeds the bounded verification pass.')
        current, turns = pending.pop(0)
        thread_id = current.get('id')
        if not isinstance(thread_id, str) or not thread_id:
            raise ManagementError('capability_unverified', 'Related thread identity is incomplete.')
        if thread_id in seen:
            continue
        seen.add(thread_id)
        if current.get('status', {}).get('type') != 'idle' or not turns or any(t.get('status') not in {'completed', 'failed', 'interrupted'} or t.get('itemsView') != 'full' for t in turns):
            related.append({'thread_id': thread_id, 'kind': 'unconfirmed_thread'})
        for observed in turns:
            evidence.append({'thread_id': thread_id, 'turn_id': observed.get('id'), 'turn_status': observed.get('status'),
                'items_complete': observed.get('itemsView') == 'full', 'generation': session['generation'], 'observed_at': _now()})
            for item in observed.get('items', []):
                if item.get('type') in {'commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange', 'collabAgentToolCall'} and item.get('status') not in {'completed', 'failed', 'declined'}:
                    related.append({'thread_id': thread_id, 'item_id': item.get('id'), 'kind': 'unfinished_item'})
                if item.get('type') == 'collabAgentToolCall':
                    receivers = item.get('receiverThreadIds')
                    if not isinstance(receivers, list) or any(not isinstance(child, str) or not child for child in receivers):
                        raise ManagementError('capability_unverified', 'Related child execution coverage is incomplete.')
                    for child in receivers:
                        child_thread = adapter.read_thread(child)
                        if child_thread.get('id') != child:
                            raise ManagementError('capability_unverified', 'Related child thread identity does not match.')
                        pending.append((child_thread, child_thread.get('turns', [])))
        backgrounds = adapter.background_terminals(thread_id)
        for terminal in backgrounds:
            if not isinstance(terminal, dict) or not isinstance(terminal.get('itemId'), str) or not isinstance(terminal.get('processId'), str):
                raise ManagementError('capability_unverified', 'Background execution evidence is malformed.')
            related.append({'thread_id': thread_id, 'item_id': terminal['itemId'], 'process_id': terminal['processId'], 'kind': 'background_terminal'})
        evidence.append({'thread_id': thread_id, 'background_coverage': 'complete', 'background_count': len(backgrounds),
                         'generation': session['generation'], 'observed_at': _now()})
    process_coverage = adapter.verify_process_coverage(session, {'turn_id': turn_id})
    evidence.append({'process_coverage': process_coverage, 'observed_at': _now()})
    return related, evidence
