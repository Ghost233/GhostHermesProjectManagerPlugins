"""Relay one verified owner's decision to one original pending operation."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .dsh_events import approval_notice_status, fold_turns
from .manager import ManagementError, _public_text
from .repository_questions import _NAMESPACE, question_destination


_TERMINAL = {'released', 'stopped', 'budget_stopped', 'execution_failed', 'outcome_unknown'}
_CARD_BINDING = ('work_id', 'card_id', 'generation', 'session_id', 'approval_id', 'call_id',
                 'command_sha256', 'arguments_sha256')


def _decision(command):
    match = re.fullmatch(r'(批准一次|拒绝)(?:\s+审批\s+(\S+))?', command)
    return ({'批准一次': 'allowed-once', '拒绝': 'rejected'}[match[1]], match[2]) if match else (None, None)


def _source(events, request, workspace, expected_id=None):
    matches = [(turn, approval_notice_status(events, request, turn, expected_id))
               for turn in fold_turns(events, workspace)]
    matches = [(turn, status) for turn, status in matches if status['state'] != 'unverified']
    if len(matches) != 1:
        raise ManagementError('binding_conflict', 'Original approval audit is not unique.')
    turn, status = matches[0]
    calls = [event for event in events if event['type'] == 'tool/call'
             and event['data'].get('callId') == request.get('callId')
             and turn['startSeq'] < event['seq'] < turn.get('endSeq', float('inf'))]
    if len(calls) != 1 or calls[0]['data'].get('name') != 'bash' or request.get('toolName') != 'bash':
        raise ManagementError('binding_conflict', 'Original approval operation is unverified.')
    try:
        operation = json.loads(calls[0]['data']['arguments'])
        if not isinstance(operation, dict) or not isinstance(operation.get('command'), str) or not operation['command']:
            raise ValueError('Original command is unavailable.')
    except (KeyError, TypeError, ValueError) as error:
        raise ManagementError('binding_conflict', 'Original approval command is unavailable.') from error
    if operation.get('sandbox_permissions') not in {None, 'read-only', 'workspace-write'}:
        raise ManagementError('binding_conflict', 'Original approval permission scope is not admitted.')
    return turn, status, operation


def _material(intake, request, operation, workspace):
    text = ('原工具：bash\n原命令：\n' + operation['command'] + '\n工作目录：'
            + str(operation.get('workdir', workspace)) + '\n本次范围：'
            + str(operation.get('sandbox_permissions', '原生当前范围')) + '\n原审批理由：'
            + (request['reason'] if isinstance(request.get('reason'), str)
               else json.dumps(request.get('reason'), ensure_ascii=False)))
    if operation.get('description'):
        text += '\n原操作说明：' + str(operation['description'])
    try:
        _public_text(text, intake.secret_values)
    except ManagementError:
        return None
    if len(text) > 6000 or re.search(r'password|passwd|credential|secret|token|api[_ -]?key|private[_ -]?key|密码|密钥|凭据|令牌', text, re.I):
        return None
    return text


def _approval_binding_digest(notice):
    return hashlib.sha256(json.dumps({key: notice[key] for key in _CARD_BINDING}, sort_keys=True).encode()).hexdigest()


def _approval_card(record, notice):
    execution = record['dsh_execution']
    elements = [{'tag': 'div', 'text': {'tag': 'plain_text', 'content':
        '任务：' + record['issue']['title'] + '\n总 Issue：' + record['issue']['url']
        + '\n任务标签：' + execution['native_title'] + '\n审批 ID：' + notice['approval_id']}}]
    if notice['answerable'] and notice.get('material'):
        elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': notice['material']}})
    if not notice['answerable']:
        status = '原操作包含无法安全公开的必要材料；审批保持受阻。'
    elif notice['state'] == 'resolved':
        status = '原审批：' + {'allowed-once': '已允许本次操作', 'rejected': '已拒绝操作',
            'cancelled': '已取消', 'unavailable': '不可用，操作未获许可'}[notice['outcome']]
        status += '\n原工具：' + {'pending': '尚未结束', 'failed': '失败', 'settled': '已结束'}[notice['operation_state']]
    elif execution['state'] in _TERMINAL:
        status = '原工作已结束；审批已失效，不能再决定。'
    elif notice.get('reply_status') == 'outcome_unknown':
        status = '决定回送尚未确认；正在核对原审计，按钮已停用。原工具结果另行核对。'
    elif notice.get('reply_status'):
        status = '本人决定已受理；正在核对原审批。原工具尚未结束，执行结果另行通知。'
    else:
        status = '等待本人决定；仅允许上述本次操作，不改变常驻权限。文字审批 ID 答复仍可使用。'
    elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': status}})
    if (notice['answerable'] and notice['state'] == 'pending' and not notice.get('reply_status')
            and execution['state'] not in _TERMINAL):
        values = {'kind': 'hermes-operation-approval', 'approval_id': notice['approval_id'],
                  'binding_sha256': _approval_binding_digest(notice)}
        elements.append({'tag': 'action', 'actions': [
            {'tag': 'button', 'text': {'tag': 'plain_text', 'content': label}, 'type': style,
             'value': {**values, 'outcome': outcome}}
            for label, style, outcome in (('批准一次', 'primary', 'allowed-once'), ('拒绝', 'danger', 'rejected'))]})
    return {'config': {'wide_screen_mode': True, 'update_multi': True},
            'header': {'template': 'orange', 'title': {'tag': 'plain_text', 'content': '具体执行审批'}},
            'elements': elements}


def prepare_card_action(intake, payload, adapter):
    if intake.closed or intake.settings.get('enabled') is not True or not intake.settings.get('verification_ref'):
        return None
    try:
        header, event = payload['header'], payload['event']
        operator, context, action = event['operator'], event['context'], event['action']
        value = action['value']
        if (header['event_type'] != 'card.action.trigger' or not isinstance(header['event_id'], str)
            or not header['event_id'] or action['tag'] != 'button' or not isinstance(value, dict)
            or set(value) != {'kind', 'approval_id', 'binding_sha256', 'outcome'}
            or value['kind'] != 'hermes-operation-approval' or value['outcome'] not in {'allowed-once', 'rejected'}):
            return None
        bindings = [binding for binding in intake.settings.get('bindings', [])
            if binding.get('verification_ref') and binding['app_id'] == header['app_id']
            and binding['transport_tenant_key'] == header['tenant_key'] and binding['chat_id'] == context['open_chat_id']
            and binding['owner_open_id'] == operator['open_id'] and binding['sender_tenant_key'] == operator['tenant_key']
            and {operator.get(key) for key in ('user_id', 'open_id', 'union_id')} & set(binding['owner_native_ids'])]
        if len(bindings) != 1:
            return None
        binding = bindings[0]
        profiles = binding.get('target_profiles', [binding['profile_id']])
        matches = [(record, notice) for record in intake.snapshot()['work'] if record['profile_id'] in profiles
            for notice in record.get('dsh_execution', {}).get('approvals', [])
            if notice.get('request_renderer') == 'interactive' and notice.get('request_notice') == 'delivered'
            and notice.get('request_message_id') == context['open_message_id'] and notice['approval_id'] == value['approval_id']
            and all(notice.get('delivery', {}).get(key) == binding[key] for key in _NAMESPACE)
            and notice['answerable'] and notice['state'] == 'pending' and not notice.get('reply_status')
            and record['dsh_execution']['state'] not in _TERMINAL
            and notice['generation'] == record['dsh_execution']['generation']
            and notice['session_id'] == record['dsh_execution']['session_id']
            and notice['work_id'] == record['id'] and notice['card_id'] == record['card_id']
            and value['binding_sha256'] == _approval_binding_digest(notice)]
        transport = next((transport for current, transport in intake.transports if current is adapter), None)
        if len(matches) != 1 or transport is None:
            return None
        record, notice = matches[0]
        envelope = {key: binding[key] for key in _NAMESPACE}
        envelope.update(tenant_key=operator['tenant_key'], sender_open_id=operator['open_id'],
            message_id=context['open_message_id'], parent_id=context['open_message_id'],
            event_id=header['event_id'], card_binding_sha256=value['binding_sha256'])
        command = ('批准一次' if value['outcome'] == 'allowed-once' else '拒绝') + ' 审批 ' + notice['approval_id']
        from .simple_development import WorkMessage
        return WorkMessage(payload, adapter, transport, binding, envelope, record['target'], command,
                           None, record['id'], action='approval_card')
    except (KeyError, TypeError, ValueError):
        return None


async def _refresh_approval_card(intake, record, notice, transport, generation):
    if notice.get('request_renderer') != 'interactive' or not notice.get('request_message_id'):
        return
    card = _approval_card(record, notice)
    digest = hashlib.sha256(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    updates = notice.setdefault('card_updates', {})
    if digest in updates:
        return
    intake.require_active(generation)
    updates[digest] = 'intent'
    intake._save_execution(record, fields=['approvals'])
    try:
        response = await transport.update_card({'message_id': notice['request_message_id'], 'card': card})
        intake.require_active(generation)
        updates[digest] = response.get('status', 'unknown')
    except (AttributeError, ManagementError, OSError, TimeoutError):
        updates[digest] = 'unknown'
    intake._save_execution(record, fields=['approvals'])


def _update_outcome(notice, events, turn, status):
    if status['state'] != 'resolved':
        return
    notice.update(state='resolved', outcome=status['outcome'])
    if notice.get('reply_status'):
        notice['reply_status'] = 'settled'
        notice['admitted_seq'] = next(event['seq'] for event in events if event['type'] == 'approval/decided'
                                      and event['data'].get('id') == notice['approval_id'])
    results = [event for event in events if event['type'] == 'tool/result'
               and event['data'].get('message', {}).get('toolCallId') == notice['call_id']
               and event['seq'] > turn['startSeq']]
    notice['operation_state'] = ('failed' if results[-1]['data']['message'].get('isError') is True
                                 else 'settled') if results else 'pending'


def _resume_approval_card(intake, record, notice, generation):
    if notice.get('reply_status') != 'settled' or notice.get('resume_unblock') or not notice.get('admitted_seq'):
        return
    execution = record['dsh_execution']
    current = next(row for row in intake.snapshot()['work'] if row['id'] == record['id'])['dsh_execution']
    if current.get('journal_cursor', -1) >= notice['admitted_seq'] or current.get('state') not in {'running', 'awaiting_acceptance'}:
        return
    card_id = intake._verified_card(record, Path(record['target']['repo_path']).resolve(strict=True), record['card_id'])
    card = intake._kanban('kanban_show', {'task_id': card_id}).get('task', {})
    if card.get('status') != 'blocked':
        return
    intake.require_active(generation)
    notice['resume_unblock'] = {'status': 'intent', 'card_id': card_id, 'generation': execution['generation'],
        'session_id': execution['session_id'], 'previous_run_id': execution['supervisor_claim']['run_id']}
    execution['state'] = 'running'
    intake._save_execution(record, fields=['approvals', 'state'])
    try:
        result = intake._kanban('kanban_unblock', {'task_id': card_id})
        if result.get('ok') is not True:
            raise ManagementError('outcome_unknown', 'Original approval supervision resumption is unconfirmed.')
    except ManagementError:
        notice['resume_unblock']['status'] = 'outcome_unknown'
        execution['state'] = 'outcome_unknown'
        intake._save_execution(record, fields=['approvals', 'state'])
        return
    notice['resume_unblock']['status'] = 'accepted'
    intake._save_execution(record, fields=['approvals'])


async def observe_approvals(intake, record, carrier, events, projections, generation):
    async with intake.lock:
        descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        try:
            await _observe_approvals(intake, record, carrier, events, generation)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


async def _observe_approvals(intake, record, carrier, events, generation):
    intake.require_active(generation)
    execution = record['dsh_execution']
    frames = await asyncio.to_thread(carrier.event_frames)
    if frames.get('generation') != execution['generation'] or frames.get('session_id') != execution['session_id']:
        raise ManagementError('binding_conflict', 'Original approval observer identity differs.')
    fresh = next(row for row in intake.snapshot()['work'] if row['id'] == record['id'])
    notices = execution['approvals'] = fresh['dsh_execution'].get('approvals', [])
    for frame in frames.get('frames', []):
        if frame.get('type') != 'waterfall' or frame.get('event') != 'approval/request':
            continue
        if frame.get('agentId') != execution['session_id'] or not isinstance(frame.get('request'), dict):
            raise ManagementError('binding_conflict', 'Original approval target differs.')
        request = frame['request']
        turn, status, operation = _source(events, request, record['target']['repo_path'])
        digest = hashlib.sha256(operation['command'].encode()).hexdigest()
        args_digest = hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()
        binding = {'approval_id': status['approval_id'], 'event_id': frame['eventId'], 'turn_id': turn['id'],
                   'tool_name': request['toolName'], 'call_id': request['callId'], 'command_sha256': digest,
                   'arguments_sha256': args_digest, 'generation': execution['generation'],
                   'session_id': execution['session_id'], 'work_id': record['id'], 'card_id': record['card_id']}
        notice = next((row for row in notices if row['approval_id'] == status['approval_id']), None)
        if notice is None:
            material = _material(intake, request, operation, record['target']['repo_path'])
            notice = {**binding, 'state': 'pending', 'answerable': material is not None, 'material': material}
            notices.append(notice)
        elif any(notice.get(key) != value for key, value in binding.items()):
            raise ManagementError('binding_conflict', 'Original approval audit binding changed.')
        _update_outcome(notice, events, turn, status)
        _resume_approval_card(intake, record, notice, generation)
        destination = question_destination(intake, record)
        if destination is None:
            continue
        transport, recipient = destination
        if await transport.verify_identity(recipient) != {'app_id': recipient['app_id'], 'open_id': recipient['recipient_open_id']}:
            raise ManagementError('binding_conflict', 'Original approval notification recipient is unverified.')
        intake.require_active(generation)
        await _refresh_approval_card(intake, record, notice, transport, generation)
        kind = ('settlement' if notice['state'] == 'resolved' and notice.get('result_notice')
                and notice['operation_state'] != 'pending' else 'result' if notice['state'] == 'resolved' else 'request')
        if notice.get(kind + '_notice'):
            continue
        if kind == 'request':
            notice['request_renderer'] = 'interactive'
            text = ('具体执行审批\n任务：' + record['issue']['title'] + '\n总 Issue：' + record['issue']['url']
                    + '\n任务标签：' + execution['native_title'] + '\n审批 ID：' + notice['approval_id'])
            if notice['answerable']:
                text += '\n' + _material(intake, request, operation, record['target']['repo_path'])
                text += '\n引用本条审批通知并真实 @ 本机器人，回复「批准一次」或「拒绝」；也可回复「批准一次 审批 ' + notice['approval_id'] + '」或「拒绝 审批 ' + notice['approval_id'] + '」。本次决定仅对应上述原操作，不改变常驻权限。'
            else:
                text += '\n原操作包含无法安全公开的必要内容；群内不能核验或批准该操作，审批保持受阻。'
        else:
            outcomes = {'allowed-once': '原审批已允许本次操作', 'rejected': '原审批已拒绝操作',
                        'cancelled': '原审批已取消', 'unavailable': '原审批不可用，操作未获许可'}
            text = (outcomes[notice['outcome']] + '。\n总 Issue：' + record['issue']['url']
                    + '\n审批 ID：' + notice['approval_id'] + '\n操作后续：'
                    + ('原工具尚未结束' if notice['operation_state'] == 'pending'
                       else '原工具失败' if notice['operation_state'] == 'failed' else '原工具已结束'))
        _public_text(text, intake.secret_values)
        notice[kind + '_notice'] = 'intent'
        intake._save_execution(record, fields=['approvals'])
        anchor = record['source_anchor']
        segment = {'uuid': hashlib.sha256((record['id'] + notice['approval_id'] + kind).encode()).hexdigest()[:32],
            'text': text, 'chat_id': anchor['chat_id'], 'reply_to': anchor['message_id'],
            'mention_open_id': recipient['owner_open_id'], 'thread_id': anchor.get('thread_id')}
        if kind == 'request':
            segment['card'] = _approval_card(record, notice)
        response = await transport.send(segment)
        intake.require_active(generation)
        notice[kind + '_notice'] = response.get('status', 'unknown')
        if response.get('status') == 'delivered':
            notice[kind + '_message_id'] = response['message_id']
            if kind == 'request':
                notice['delivery'] = {key: recipient[key] for key in _NAMESPACE}
                digest = hashlib.sha256(json.dumps(segment['card'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                notice.setdefault('card_updates', {})[digest] = 'delivered'
        intake._save_execution(record, fields=['approvals'])
    intake._save_execution(record, fields=['approvals'])


def prepare_approval_reply(intake, event, adapter, binding, envelope, command, authorized_profiles):
    if event.source.is_bot is not False:
        return None
    outcome, approval_id = _decision(command)
    candidates = [(record, notice) for record in intake.snapshot()['work'] if record['profile_id'] in authorized_profiles
                  for notice in record.get('dsh_execution', {}).get('approvals', [])
                  if all(notice.get('delivery', {}).get(key) == envelope.get(key) for key in _NAMESPACE)]
    matches = [(record, notice) for record, notice in candidates
               if (envelope.get('parent_id') == notice.get('request_message_id') and envelope.get('parent_id'))
               or approval_id == notice['approval_id']]
    if not matches and not (outcome and candidates):
        return None
    from .simple_development import WorkMessage
    transport = next((transport for current, transport in intake.transports if current is adapter), None)
    valid = len(matches) == 1 and (approval_id is None or approval_id == matches[0][1]['approval_id'])
    record = matches[0][0] if valid else None
    return WorkMessage(event, adapter, transport, binding, envelope, record['target'] if record else {},
                       command, None, record['id'] if record else None,
                       rejected=None if valid else 'approval_target', action='approval')


async def _reply_notice(intake, prepared, generation, text):
    intake.require_active(generation)
    response = await prepared.transport.send({'uuid': hashlib.sha256(('approval-reply:' + prepared.envelope.get('event_id', prepared.envelope['message_id'])).encode()).hexdigest()[:32],
        'text': text, 'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id']})
    intake.require_active(generation)
    return response.get('status', 'unknown')


async def process_approval_reply(intake, prepared, generation):
    from .repository_supervision import attach_existing_owned, original_history
    intake.require_active(generation)
    outcome, approval_id = _decision(prepared.command)
    if prepared.rejected or outcome is None:
        notification = await _reply_notice(intake, prepared, generation,
            '群内答复没有改变权限或原审批结果。请引用当前具体审批通知并回复「批准一次」或「拒绝」，也可明确写出审批 ID。')
        return {'status': 'rejected', 'code': 'approval_target', 'notification': notification}
    async with intake.lock:
        descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        carrier = None
        try:
            record = next(row for row in intake.snapshot()['work'] if row['id'] == prepared.work_id)
            execution = record['dsh_execution']
            matches = [notice for notice in execution.get('approvals', [])
                       if (prepared.envelope.get('parent_id') == notice.get('request_message_id') and prepared.envelope.get('parent_id'))
                       or approval_id == notice['approval_id']]
            if len(matches) != 1:
                raise ManagementError('binding_conflict', 'Original approval reply is not unique.')
            notice = matches[0]
            if (not notice['answerable'] or notice['generation'] != execution['generation']
                or notice['session_id'] != execution['session_id'] or notice['work_id'] != record['id']
                or notice['card_id'] != record['card_id'] or execution['state'] in _TERMINAL
                or any(notice['delivery'].get(key) != prepared.envelope.get(key) for key in _NAMESPACE)):
                raise ManagementError('binding_conflict', 'Original approval reply binding is no longer current.')
            if (prepared.action == 'approval_card'
                and prepared.envelope.get('card_binding_sha256') != _approval_binding_digest(notice)):
                raise ManagementError('binding_conflict', 'Original approval card binding changed.')
            intake._verified_card(record, Path(record['target']['repo_path']).resolve(strict=True), record['card_id'])
            carrier = attach_existing_owned(intake, record)
            events, _ = await asyncio.to_thread(original_history, carrier, execution['session_id'], record['target']['repo_path'])
            packet = await asyncio.to_thread(carrier.event_frames)
            frames = [frame for frame in packet['frames'] if frame.get('eventId') == notice['event_id']
                      and frame.get('event') == 'approval/request' and frame.get('agentId') == execution['session_id']]
            if len(frames) != 1 or packet['generation'] != execution['generation'] or packet['session_id'] != execution['session_id']:
                raise ManagementError('binding_conflict', 'Original approval delivery is unavailable.')
            turn, status, operation = _source(events, frames[0]['request'], record['target']['repo_path'], notice['approval_id'])
            _update_outcome(notice, events, turn, status)
            if notice.get('reply_status') or status['state'] == 'resolved':
                intake._save_execution(record, fields=['approvals'])
                await _refresh_approval_card(intake, record, notice, prepared.transport, generation)
                return {'status': notice.get('reply_status', 'settled'), 'duplicate': True}
            if ('endSeq' in turn or turn['id'] != notice['turn_id']
                or hashlib.sha256(operation['command'].encode()).hexdigest() != notice['command_sha256']
                or hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest() != notice['arguments_sha256']):
                raise ManagementError('binding_conflict', 'Original approval command or turn changed.')
            intake.require_active(generation)
            notice.update(reply_status='intent', reply_message_id=prepared.envelope.get('event_id', prepared.envelope['message_id']), requested_outcome=outcome)
            intake._save_execution(record, fields=['approvals'])
            await _refresh_approval_card(intake, record, notice, prepared.transport, generation)
            rpc_id = str(uuid.uuid4())
            binding = {key: notice[key] for key in ('approval_id', 'call_id', 'command_sha256', 'generation', 'session_id')}
            try:
                response = await asyncio.to_thread(carrier.request, '$events/result', {
                    'request': {'clientId': packet['client_id'], 'eventId': notice['event_id'],
                                'outcome': {'kind': 'result', 'value': outcome}}, '_hermes_approval': binding}, rpc_id)
                if response.get('type') != 'server-response' or response.get('rpcId') != rpc_id or response.get('result', {}).get('ok') is not True:
                    raise OSError('Original approval reply acknowledgement is unconfirmed.')
                notice['reply_status'] = 'queued'
            except (OSError, TimeoutError, ManagementError):
                notice['reply_status'] = 'outcome_unknown'
            try:
                events, _ = await asyncio.to_thread(original_history, carrier, execution['session_id'], record['target']['repo_path'])
                turn, status, _ = _source(events, frames[0]['request'], record['target']['repo_path'], notice['approval_id'])
                _update_outcome(notice, events, turn, status)
            except (ManagementError, OSError, TimeoutError):
                notice['reply_status'] = 'outcome_unknown'
            intake._save_execution(record, fields=['approvals'])
            await _refresh_approval_card(intake, record, notice, prepared.transport, generation)
            text = ('审批回送结果尚未确认；保留当前操作，核对原审计后才确认，不会重复发送。'
                    if notice['reply_status'] == 'outcome_unknown' else '本次审批决定已送回同一原操作；执行结果另行通知。')
            await _reply_notice(intake, prepared, generation, text)
            return {'status': notice['reply_status']}
        except (ManagementError, OSError, TimeoutError):
            await _reply_notice(intake, prepared, generation,
                '当前审批绑定或原审计无法核验；原操作保持待核对，本条答复不会授予权限或重复发送决定。')
            return {'status': 'rejected', 'code': 'approval_target'}
        finally:
            if carrier is not None:
                carrier.close()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
