"""Durable project notifications derived from verified management facts."""
import hashlib
import json
import uuid
import os

from .manager import ManagementError


def state(data):
    saved = data.setdefault('notifications', {'events': {}, 'projects': {}, 'tasks': {}, 'human_requests': {}, 'health': {
        'supervision': 'unverified', 'delivery': 'unverified', 'last_checked_at': None}})
    for key in ('human_requests', 'anchors'):
        saved.setdefault(key, {})
    saved['health'].setdefault('sources', {})
    return saved


def snapshot(manager, data, project_ids):
    saved = state(data)
    health = dict(saved['health'])
    path = manager.state_dir / 'notification-health.json'
    if path.exists():
        try:
            external = json.loads(path.read_text())
            if external.get('generation') == manager._notification_generation and external.get('supervision') == 'unavailable':
                health.update(external)
        except (OSError, ValueError):
            health.update(supervision='unverified', delivery='unverified')
    if saved.get('generation') and saved['generation'] != manager._notification_generation:
        health.update(supervision='unavailable', delivery='unverified')
    return {'events': [e for e in saved['events'].values() if e['project_id'] in project_ids],
            'health': health}


def human_text(question):
    text = '人工请求：' + question['id'] + '\n分类：' + question['category']
    if question['category'] in {'question', 'nonblocking'}:
        text += '\n' + '\n'.join(q['question'] for q in question.get('questions', []))
        text += '\n本人可回复：回答 ' + question['id'] + '：答案'
    elif question['category'] == 'approval' and question['answerable']:
        text += '\n具体操作：' + json.dumps(question.get('operation', {}), ensure_ascii=False, sort_keys=True)
        text += '\n操作 ID：' + str(question.get('operation_id')) + '；范围：turn（仅本回合）'
        text += '\n本人须明确批准或拒绝并引用请求 ID、操作 ID 及范围。'
    else:
        text += '\n请在原服务的安全原界面处理。服务：' + question['original_interface']['service_ref']
        text += '\n原会话：' + question['thread_id'] + '；安全链接尚不可用。不要在群里发送秘密答案。'
    return text


def emit(saved, kind, project_id, tasks, text, now, *, human_request_id=None, mention_owner=False, key=None):
    key = key or hashlib.sha256(json.dumps([kind, project_id, [t['id'] for t in tasks], human_request_id, now]).encode()).hexdigest()
    event = {'id': key, 'kind': kind, 'project_id': project_id, 'request_ids': [t['id'] for t in tasks],
             'human_request_id': human_request_id, 'mention_owner': mention_owner, 'text': text,
             'created_at': now, 'delivery': 'pending'}
    return saved['events'].setdefault(key, event)


def observe(manager, task, thread=None, turn=None, items=(), events=()):
    verified = thread is not None and task['execution'] != 'unverified'
    explanation = 'explicit_wait' if task['execution'] in {'waiting_input', 'waiting_approval', 'stopping'} else None
    if any(item.get('type') in {'commandExecution', 'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall'} and item.get('status') == 'inProgress' for item in items):
        explanation = 'current_long_operation'
    if any(q['resolution'] == 'pending' and not q.get('reply') and (q.get('blocking') is True or q['category'] == 'approval') for q in task.get('human_requests', [])):
        explanation = 'pending_human_request'
    complete = bool(turn and turn.get('itemsView') == 'full')
    digest = hashlib.sha256(json.dumps([task['execution'], turn, events], sort_keys=True).encode()).hexdigest() if verified else None
    task['supervision_observation'] = {'verified': verified, 'progress_digest': digest,
        'checked_at': manager.notification_clock(), 'explanation': explanation, 'complete': complete,
        'source': 'original_thread_read', 'service_id': task['session']['service_id'],
        'eligible_stall': verified and complete and task['execution'] == 'running' and not explanation}


def stall_coverage(manager, task):
    from .takeover import executor_for
    from .control import terminal_evidence
    adapter = executor_for(manager, task)
    if adapter is None:
        return 'unverified'
    try:
        session = task['session']
        thread = adapter.read_thread(session['thread_id'])
        if thread.get('id') != session['thread_id'] or thread.get('cwd') != session['repository']['worktree']:
            return 'unverified'
        related, evidence = terminal_evidence(adapter, session, thread, session['turn_id'])
        if any(r['kind'] in {'background_terminal', 'unfinished_item'} or r['thread_id'] != session['thread_id'] for r in related):
            return 'explained_related_execution'
        if not any(e.get('process_coverage') for e in evidence):
            return 'unverified'
        return 'verified'
    except ManagementError:
        return 'unverified'


def run(manager, identity):
    with manager._lock:
        _, initial = manager._load()
        if manager._principal(identity, initial) is not None:
            raise ManagementError('forbidden', 'Only the manager supervision entry schedules global notifications.')
        task_ids = [t['id'] for t in initial['requests'].values() if t.get('session') and not t.get('repository_released') and t.get('outer_task_status') != 'stopped']
    for task_id in task_ids:
        try:
            manager.refresh_task(identity, task_id)
        except ManagementError:
            continue
    with manager._lock, manager._db:
        version, data = manager._load()
        principal = manager._principal(identity, data)
        if principal is not None:
            raise ManagementError('forbidden', 'Only the manager supervision entry schedules global notifications.')
        now = manager.notification_clock()
        saved = state(data)
        if saved.get('generation') != manager._notification_generation:
            for schedule in saved['projects'].values():
                schedule['summary_at'] = now
            for tracker in saved['tasks'].values():
                tracker['progress_at'] = now
                if 'disconnected_at' in tracker:
                    tracker['disconnected_at'] = now
            for question_id in saved['human_requests']:
                saved['human_requests'][question_id] = now
            for event in saved['events'].values():
                if event['kind'] == 'summary' and event['delivery'] == 'pending':
                    event['delivery'] = 'expired'
            saved['generation'] = manager._notification_generation
        active = {}
        for task in data['requests'].values():
            if not task.get('repository_released') and task.get('outer_task_status') != 'stopped':
                active.setdefault(task['project_id'], []).append(task)
        for task in data['requests'].values():
            observation = task.get('supervision_observation')
            if observation:
                tracker = saved['tasks'].setdefault(task['id'], {'progress_at': now, 'digest': observation['progress_digest'], 'stall_sent': False})
                saved['health']['sources'][task['id']] = {'status': 'verified' if observation['verified'] else 'unverified',
                    'service_id': observation['service_id'], 'checked_at': observation['checked_at'],
                    'last_confirmed_execution': task.get('last_confirmed_execution', task['execution']),
                    'execution': task['execution']}
                if not observation['verified']:
                    tracker.setdefault('disconnected_at', now)
                    if now - tracker['disconnected_at'] >= 120 and not tracker.get('disconnect_sent'):
                        emit(saved, 'channel_lost', task['project_id'], [task],
                             '原监督通道持续失联2分钟；执行待核实，保留最后核实状态及仓库占用。\nIssue：' + task['accepted_scope']['url'], now)
                        tracker['disconnect_sent'] = True
                elif 'disconnected_at' in tracker:
                    if tracker.get('disconnect_sent'):
                        emit(saved, 'channel_recovered', task['project_id'], [task], '原监督通道已恢复核查；实际执行：' + task['execution'] + '\nIssue：' + task['accepted_scope']['url'], now)
                    tracker.pop('disconnected_at', None)
                    tracker.pop('disconnect_sent', None)
                    tracker.update(progress_at=now, stall_sent=False)
            if observation and observation['verified']:
                tracker = saved['tasks'][task['id']]
                if tracker['digest'] != observation['progress_digest'] or observation['explanation']:
                    tracker.update(progress_at=now, digest=observation['progress_digest'], stall_sent=False)
                if observation['eligible_stall'] and now - tracker['progress_at'] >= 900 and not tracker['stall_sent']:
                    coverage = stall_coverage(manager, task)
                    saved['health']['sources'][task['id']]['stall_coverage'] = coverage
                    if coverage == 'verified':
                        emit(saved, 'suspected_stall', task['project_id'], [task],
                             '疑似停滞：已核查原执行服务及后台覆盖，连续15分钟未见可核实进展，未发现明确等待或可解释长命令。\nIssue：' + task['accepted_scope']['url'] + '\n原会话：' + task['session']['thread_id'], now)
                        tracker['stall_sent'] = True
                    elif coverage == 'explained_related_execution':
                        tracker.update(progress_at=now, stall_sent=False)
            for question in task.get('human_requests', []):
                if question['resolution'] != 'pending' or question.get('reply') or not question.get('control_enabled') or not (question.get('blocking') is True or question['category'] == 'approval'):
                    continue
                previous = saved['human_requests'].get(question['id'])
                if previous is not None and now - previous < 1800:
                    continue
                emit(saved, 'human_request', task['project_id'], [task], human_text(question), now,
                     human_request_id=question['id'], mention_owner=True)
                saved['human_requests'][question['id']] = now
        for project_id in list(saved['projects']):
            if project_id not in active:
                del saved['projects'][project_id]
        for project_id, tasks in active.items():
            schedule = saved['projects'].setdefault(project_id, {'summary_at': now})
            if now - schedule['summary_at'] < 900:
                continue
            text = '项目汇总：' + project_id + '\n' + '\n'.join(
                '负责人：' + t['profile_id'] + '；状态：' + t['execution'] + '\n进展：无新进展；交付：' + t.get('task_delivery', 'unmet') +
                '；PR：' + t.get('pr_status', 'none') + '\n阻塞：' + (t.get('unexecuted_reason') or '无已核实阻塞') +
                '\n待处理：' + '\n'.join(human_text(q) for q in t.get('human_requests', []) if q['resolution'] == 'pending' and not q.get('reply')) +
                '\n下一步：核对原服务与当前有效请求\nIssue：' + t['accepted_scope']['url'] for t in tasks)
            key = hashlib.sha256(json.dumps(['summary', project_id, now]).encode()).hexdigest()
            saved['events'][key] = {'id': key, 'kind': 'summary', 'project_id': project_id,
                'request_ids': [t['id'] for t in tasks], 'text': text, 'created_at': now, 'delivery': 'pending'}
            schedule['summary_at'] = now
        from .collaboration import _channel
        entries = [c for c in data.get('collaboration', {}).get('channels', {}).values() if c['group_kind'] == 'entry']
        target = None
        if len(entries) == 1:
            try:
                target = _channel(data, entries[0]['id'])
            except ManagementError:
                pass
        for event in saved['events'].values():
            if 'target_channel' in event:
                continue
            event['target_channel'] = json.loads(json.dumps(target))
            event['segments'] = [{'number': index + 1, 'uuid': str(uuid.uuid4()), 'text': event['text'][start:start + 1400],
                'status': 'pending', 'attempts': []} for index, start in enumerate(range(0, len(event['text']), 1400))]
            if target is None:
                event['delivery'] = 'blocked'
        saved['health'].update(supervision='running', last_checked_at=now)
        manager._save(version, data)
        persist_health(manager, saved['health'])
        return {'status': 'completed', 'notifications': list(saved['events'].values()), 'health': saved['health']}


def manage(manager, identity, action, details):
    with manager._lock, manager._db:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Notification delivery uses the trusted shared manager entry.')
        saved = state(data)
        if not isinstance(details, dict):
            raise ManagementError('invalid_change', 'A notification operation is required.')
        if action == 'channel_unavailable' and not details:
            saved['health']['delivery'] = 'unavailable'
            manager._save(version, data)
            return saved['health']
        if action not in {'claim', 'receipt'} or set(details) != ({'event_id'} if action == 'claim' else {'event_id', 'uuid', 'receipt'}):
            raise ManagementError('invalid_change', 'An exact notification delivery operation is required.')
        event = saved['events'].get(details['event_id'])
        if event is None:
            raise ManagementError('invalid_change', 'Unknown notification.')
        from .collaboration import _channel
        frozen = event.get('target_channel')
        if not frozen or _channel(data, frozen['id']) != frozen:
            raise ManagementError('binding_conflict', 'The original notification entry channel changed or was unavailable.')
        if action == 'claim':
            if event.get('human_request_id'):
                question = next((q for task_id in event['request_ids'] for q in data['requests'][task_id].get('human_requests', []) if q['id'] == event['human_request_id']), None)
                if not question or question['resolution'] != 'pending' or question.get('reply') or not question.get('control_enabled'):
                    event['delivery'] = 'expired'
                    manager._save(version, data)
                    return None
            if event['delivery'] not in {'pending', 'sending'}:
                return None
            for segment in event['segments']:
                if segment['status'] == 'delivered':
                    continue
                if segment['status'] != 'pending':
                    return None
                anchor = saved['anchors'].get(event['project_id'])
                if anchor and anchor['channel'] != frozen:
                    raise ManagementError('binding_conflict', 'The project entry anchor belongs to another registered channel.')
                packet = {**segment, 'event_id': event['id'], 'kind': event['kind'],
                    'path': 'reply' if anchor else 'create', 'chat_id': frozen['chat_id'],
                    'reply_to': anchor['message_id'] if anchor else None, 'thread_id': anchor.get('thread_id') if anchor else None,
                    'mention_open_id': frozen['owner_open_id'] if event.get('mention_owner') else None, 'sender_binding': frozen}
                segment['status'] = 'sending'
                manager._inflight.add(segment['uuid'])
                segment['attempts'].append({'status': 'sending', 'path': packet['path'], 'intended_chat_id': packet['chat_id'],
                    'intended_reply_to': packet['reply_to'], 'intended_thread_id': packet['thread_id']})
                event['delivery'] = 'sending'
                manager._save(version, data)
                return packet
            return None
        receipt = details['receipt']
        if not isinstance(receipt, dict) or receipt.get('status') not in {'delivered', 'failed', 'unknown'} or set(receipt) - {'status', 'code', 'message_id', 'chat_id', 'parent_id', 'root_id', 'thread_id'} or any(v is not None and not isinstance(v, (str, int)) for v in receipt.values()):
            raise ManagementError('invalid_change', 'A scalar notification receipt is required.')
        segment = next((s for s in event['segments'] if s['uuid'] == details['uuid']), None)
        if not segment or segment['status'] != 'sending':
            raise ManagementError('version_conflict', 'No matching notification delivery intent.')
        receipt = dict(receipt)
        attempt = segment['attempts'][-1]
        if receipt['status'] == 'delivered' and (not isinstance(receipt.get('message_id'), str) or not receipt['message_id'] or receipt.get('chat_id') != frozen['chat_id'] or attempt['path'] == 'reply' and receipt.get('parent_id') != attempt['intended_reply_to']):
            receipt['status'] = 'unknown'
        segment['status'] = receipt['status']
        attempt.update(receipt)
        manager._inflight.discard(segment['uuid'])
        if receipt['status'] == 'delivered' and event['project_id'] not in saved['anchors']:
            saved['anchors'][event['project_id']] = {'channel': frozen, 'message_id': receipt['message_id'], 'thread_id': receipt.get('thread_id')}
        statuses = {s['status'] for s in event['segments']}
        event['delivery'] = 'delivered' if statuses == {'delivered'} else receipt['status'] if receipt['status'] != 'delivered' else 'pending'
        saved['health']['delivery'] = 'available' if event['delivery'] == 'delivered' else 'unverified'
        manager._save(version, data)
        return event


async def deliver(intake, identity, generation):
    intake.require_active(generation)
    import asyncio
    def poll_if_active():
        with intake.lifecycle_lock:
            intake.require_active(generation)
            return intake.manager().run_notifications(identity)
    result = await asyncio.to_thread(poll_if_active)
    for event in result['notifications']:
        if event['delivery'] not in {'pending', 'sending'} or not event.get('target_channel'):
            continue
        matches = []
        for _, transport in tuple(intake.transports):
            try:
                recipient = await transport.verify_identity(event['target_channel'])
                intake.require_active(generation)
                if recipient and recipient.get('app_id') == event['target_channel']['app_id'] and recipient.get('open_id') == event['target_channel']['recipient_open_id']:
                    matches.append(transport)
            except Exception:
                intake.require_active(generation)
        if len(matches) != 1:
            intake.manager().manage_notifications(identity, 'channel_unavailable', {})
            continue
        while True:
            intake.require_active(generation)
            packet = intake.manager().manage_notifications(identity, 'claim', {'event_id': event['id']})
            if packet is None:
                break
            try:
                receipt = await matches[0].send(packet)
                intake.require_active(generation)
            except Exception:
                intake.require_active(generation)
                receipt = {'status': 'unknown'}
            intake.manager().manage_notifications(identity, 'receipt', {'event_id': event['id'], 'uuid': packet['uuid'], 'receipt': receipt})
            if receipt.get('status') != 'delivered':
                break


def reconcile(manager, data):
    saved = state(data)
    for event in saved['events'].values():
        for segment in event.get('segments', []):
            if segment['status'] == 'sending' and segment['uuid'] not in manager._inflight:
                segment['status'] = 'unknown'
                segment['attempts'][-1]['status'] = 'unknown'
                event['delivery'] = 'unknown'


def persist_health(manager, health):
    temporary = manager.state_dir / ('.notification-health-' + str(uuid.uuid4()))
    temporary.write_text(json.dumps({'generation': manager._notification_generation, **health}))
    os.replace(temporary, manager.state_dir / 'notification-health.json')


def shutdown(manager):
    persist_health(manager, {'supervision': 'unavailable', 'delivery': 'unverified', 'stopped_at': manager.notification_clock()})


def component_unavailable(manager):
    persist_health(manager, {'supervision': 'unavailable', 'delivery': 'unverified',
        'checked_at': manager.notification_clock(), 'reason': 'supervision_component_unavailable'})
