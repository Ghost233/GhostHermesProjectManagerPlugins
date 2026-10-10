"""One original question round, one referenced owner reply batch."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .manager import ManagementError, _public_text


_NAMESPACE = ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')


def question_destination(intake, record):
    anchor = record['source_anchor']
    matches = [(transport, binding) for adapter, transport in intake.transports for binding in adapter.bindings
               if all(binding.get(key) == anchor.get(key) for key in _NAMESPACE)
               and record['profile_id'] in binding.get('target_profiles', [binding.get('profile_id')])]
    return matches[0] if len(matches) == 1 else None


def _safe_questions(intake, questions):
    if not isinstance(questions, list) or not questions or len(questions) > 100:
        raise ManagementError('outcome_unknown', 'Original question batch is malformed.')
    material = json.dumps(questions, ensure_ascii=False)
    try:
        _public_text(material, intake.secret_values)
    except ManagementError:
        return False
    if re.search(r'password|passwd|secret|token|api[_ -]?key|private[_ -]?key|密码|密钥|凭据|令牌', material, re.I):
        return False
    if re.search(r'(?:approve|allow|批准|允许).*(?:execut|shell|command|tool|权限|执行|命令)', material, re.I):
        return False
    ids = [q.get('id') for q in questions if isinstance(q, dict)]
    if len(ids) != len(questions) or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ManagementError('outcome_unknown', 'Original question identities are malformed.')
    return True


def _natural_question(events):
    messages = [event for event in events if event['type'] == 'assistant/message']
    if not messages:
        return None
    event = messages[-1]
    content = event['data'].get('message', {}).get('content', [])
    text = '\n'.join(block['text'] for block in content if block.get('type') == 'text')
    if not re.search(r'[?？]|(?:请|需要).*(?:确认|选择|回答|决定|补充)', text):
        return None
    if any(row['seq'] > event['seq'] and row['type'] in {'user/message', 'tool/call', 'assistant/attempt'} for row in events):
        return None
    return event, text


def _readable_material(intake, record, text):
    """Show small referenced plans as content in the same question message."""
    repository = Path(record['target']['repo_path']).resolve(strict=True)
    for value in dict.fromkeys(re.findall(r'/(?:[^\s`"<>]+)\.md', text)):
        path = Path(value)
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(repository) or not resolved.is_file() or resolved.stat().st_size > 16000:
                raise ValueError('The shared plan must be a small bound repository file.')
            material = resolved.read_text()
            _public_text(material, intake.secret_values)
        except (OSError, ValueError, UnicodeError, ManagementError):
            return '方案材料请在原生 DSH 私有界面审阅；当前群内无法核验完整可读材料。', False
        text = text.replace(value, path.name) + '\n\n方案材料（' + path.name + '）：\n' + material
    if len(text) > 7500:
        return '方案材料请在原生 DSH 私有界面审阅；当前群内材料超过单轮可读消息范围。', False
    return text, True


def _resume_original_card(intake, record, question, generation):
    if (question.get('reply_status') != 'accepted' or question.get('resume_unblock')
            or question.get('reply_path') == 'live' or not question.get('admitted_seq')):
        return
    execution = record['dsh_execution']
    current = next(r for r in intake.snapshot()['work'] if r['id'] == record['id'])['dsh_execution']
    if current.get('journal_cursor', -1) >= question['admitted_seq']:
        question['resume_unblock'] = {'status': 'already_observed'}
        return
    if current.get('state') not in {'running', 'awaiting_acceptance'}:
        return
    card_id = intake._verified_card(record, Path(record['target']['repo_path']).resolve(strict=True), record['card_id'])
    if card_id != record['card_id']:
        raise ManagementError('binding_conflict', 'Original question supervision card changed.')
    card = intake._kanban('kanban_show', {'task_id': card_id}).get('task', {})
    if card['status'] != 'blocked':
        return
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    previous = current.get('supervisor_claim')
    with connect_closing(board=intake.board) as connection:
        original_card = kb.get_task(connection, card_id)
        original_run = kb.get_run(connection, previous['run_id']) if previous else None
        latest_run = kb.latest_run(connection, card_id)
    if (not original_card or original_card.status != 'blocked' or not original_run
            or original_run.task_id != card_id or original_run.status == 'running'
            or original_card.current_run_id is not None or not latest_run or latest_run.id != previous['run_id']):
        raise ManagementError('binding_conflict', 'Original blocked supervision claim is unavailable or still running.')
    intake.require_active(generation)
    question['resume_unblock'] = {'status': 'intent', 'card_id': record['card_id'],
        'generation': execution['generation'], 'session_id': execution['session_id'], 'previous_run_id': previous['run_id']}
    execution['state'] = 'running'
    intake._save_execution(record, fields=['questions', 'state'])
    try:
        result = intake._kanban('kanban_unblock', {'task_id': record['card_id']})
        if result.get('ok') is not True:
            raise ManagementError('outcome_unknown', 'Original supervision resumption is unconfirmed.')
    except ManagementError:
        question['resume_unblock']['status'] = 'outcome_unknown'
        execution['state'] = 'outcome_unknown'
        intake._save_execution(record, fields=['questions', 'state'])
        return
    intake.require_active(generation)
    question['resume_unblock']['status'] = 'accepted'
    intake._save_execution(record, fields=['questions'])


async def observe_questions(intake, record, carrier, events, projections, generation):
    async with intake.lock:
        descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        try:
            return await _observe_questions(intake, record, carrier, events, projections, generation)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


async def _observe_questions(intake, record, carrier, events, projections, generation):
    intake.require_active(generation)
    execution = record['dsh_execution']
    packet = await asyncio.to_thread(carrier.event_frames)
    if packet['generation'] != execution['generation'] or packet['session_id'] != execution['session_id']:
        raise ManagementError('binding_conflict', 'Original question observation belongs to another execution.')
    fresh = next(r for r in intake.snapshot()['work'] if r['id'] == record['id'])
    rounds = fresh['dsh_execution'].get('questions', [])
    execution['questions'] = rounds
    native = projections.get('userQuestions')
    if not isinstance(native, dict) or not isinstance(native.get('active'), list) or not isinstance(native.get('settled'), list):
        raise ManagementError('outcome_unknown', 'Original question lifecycle is unavailable.')
    candidates = []
    for row in native['active']:
        calls = [e for e in events if e['type'] == 'tool/call' and e['data'].get('name') == 'ask_user_question'
                 and e['data'].get('callId') == row['callId']]
        if len(calls) != 1:
            raise ManagementError('binding_conflict', 'Original question call is not unique.')
        frames = [frame for frame in packet['frames'] if frame.get('type') == 'waterfall'
                  and frame.get('event') == 'user-questions/request' and frame.get('agentId') == execution['session_id']
                  and frame['request'].get('wait', {}).get('callId') == row['callId']]
        if row['state'] == 'open' and len(frames) != 1:
            continue
        candidates.append({'id': row['callId'], 'kind': 'structured', 'state': row['state'],
                           'questions': row['questions'], 'source_seq': calls[0]['seq'],
                           'event_id': frames[0]['eventId'] if frames else None})
    natural = _natural_question(events) if not native['active'] else None
    if natural:
        source, text = natural
        candidates.append({'id': 'message-' + str(source['seq']), 'kind': 'natural', 'state': 'open',
                           'questions': [{'id': 'answer', 'question': text}], 'source_seq': source['seq'], 'event_id': None})
    active_ids = {candidate['id'] for candidate in candidates}
    for question in rounds:
        if question['id'] not in active_ids:
            question['state'] = 'settled' if any(row['callId'] == question['id'] for row in native['settled']) else 'expired'
        if question.get('reply_status') == 'queued' and question['kind'] == 'structured':
            batch = [question['answers'][q['id']] for q in question['questions']]
            settled = any(row['callId'] == question['id'] and row['answers'] == batch for row in native['settled'])
            source = ('user/message' if question.get('reply_path') == 'continued' else 'tool/result')
            matching = [event for event in events if event['type'] == source and event['seq'] > question['source_seq']
                        and (event['data'].get('source', {}).get('kind') == 'user-question-reply'
                             and event['data']['source'].get('callId') == question['id']
                             and event['data']['source'].get('outcome') == 'answered' if source == 'user/message'
                             else event['data'].get('message', {}).get('toolCallId') == question['id']
                             and event['data']['message'].get('isError') is not True)]
            if settled and matching:
                question.update(reply_status='accepted', state='settled', admitted_seq=matching[-1]['seq'])
        elif question.get('reply_status') == 'queued' and question['kind'] == 'natural':
            matching = [event for event in events if event['type'] == 'user/message' and event['seq'] > question['source_seq']
                   and event['data'].get('source', {}).get('kind') == 'user'
                   and event['data']['source'].get('rpcId') == question.get('request_id')]
            if matching:
                question.update(reply_status='accepted', state='settled', admitted_seq=matching[-1]['seq'])
        _resume_original_card(intake, record, question, generation)
    for candidate in candidates:
        digest = hashlib.sha256(json.dumps(candidate['questions'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        question = next((q for q in rounds if q['id'] == candidate['id']), None)
        if question is None:
            safe = _safe_questions(intake, candidate['questions'])
            question = {**candidate, 'source_digest': digest, 'generation': execution['generation'],
                        'session_id': execution['session_id'], 'answers': {}, 'reply_messages': [], 'answerable': safe}
            if not safe:
                question.pop('questions')
            rounds.append(question)
        elif question['source_digest'] != digest or question['generation'] != execution['generation']:
            raise ManagementError('binding_conflict', 'Original question changed its identity or contents.')
        question['state'] = candidate['state']
        if question.get('notification') or question.get('reply_status'):
            continue
        destination = question_destination(intake, record)
        if destination is None:
            continue
        transport, binding = destination
        identity = await transport.verify_identity(binding)
        intake.require_active(generation)
        if identity != {'app_id': binding['app_id'], 'open_id': binding['recipient_open_id']}:
            raise ManagementError('binding_conflict', 'Original question recipient is unverified.')
        text = '任务：' + record['issue']['title'] + '\n总 Issue：' + record['issue']['url']
        if question['answerable']:
            text += '\n问题轮次：' + question['id']
            for item in question['questions']:
                text += '\n\n' + item['id'] + '：' + item.get('header', '') + '\n' + item['question']
                if item.get('detail'):
                    text += '\n' + item['detail']
                for option in item.get('options', []):
                    text += '\n- ' + option['label'] + ('：' + option['description'] if option.get('description') else '')
            text += '\n\n引用本轮消息并真实 @ 本机器人，按「题号：答复」分次回答。明确跳过请写「跳过」；委托决定请保留原意。规划确认不授予执行权限。'
            text, readable = _readable_material(intake, record, text)
            if not readable:
                question['answerable'] = False
        else:
            text += '\n原问题包含敏感输入或具体操作许可，请在原生 DSH 私有界面处理。'
        _public_text(text, intake.secret_values)
        question['notification'] = 'intent'
        intake._save_execution(record, fields=['questions'])
        anchor = record['source_anchor']
        response = await transport.send({'uuid': hashlib.sha256((record['id'] + question['id']).encode()).hexdigest()[:32],
            'text': text, 'chat_id': binding['chat_id'], 'reply_to': anchor['message_id'],
            'mention_open_id': binding['owner_open_id'], 'thread_id': anchor.get('thread_id')})
        intake.require_active(generation)
        question['notification'] = response.get('status', 'unknown')
        if response.get('status') == 'delivered':
            question['delivery'] = {key: binding[key] for key in _NAMESPACE}
            question['delivery']['message_id'] = response['message_id']
        intake._save_execution(record, fields=['questions'])
    intake._save_execution(record, fields=['questions'])
    return any(q.get('reply_status') in {'intent', 'queued', 'outcome_unknown'}
               or q['state'] in {'open', 'continued'} and not q.get('reply_status') for q in rounds)


def prepare_reply(intake, event, adapter, binding, envelope, command, authorized_profiles):
    if event.source.is_bot is not False or not envelope.get('parent_id'):
        return None
    matches = [(record, question) for record in intake.snapshot()['work'] if record['profile_id'] in authorized_profiles
               for question in record.get('dsh_execution', {}).get('questions', [])
               if question.get('delivery', {}).get('message_id') == envelope['parent_id']
               and all(question['delivery'].get(key) == envelope.get(key) for key in _NAMESPACE)]
    if len(matches) != 1:
        candidates = [record for record in intake.snapshot()['work'] if record['profile_id'] in authorized_profiles
            and any(all(q.get('delivery', {}).get(key) == envelope.get(key) for key in _NAMESPACE)
                    for q in record.get('dsh_execution', {}).get('questions', []))]
        if not candidates:
            return None
        from .simple_development import WorkMessage
        transport = next((t for a, t in intake.transports if a is adapter), None)
        return WorkMessage(event, adapter, transport, binding, envelope, {}, command, None,
                           rejected='question_target', action='question')
    from .simple_development import WorkMessage
    record, _ = matches[0]
    transport = next((t for a, t in intake.transports if a is adapter), None)
    return WorkMessage(event, adapter, transport, binding, envelope, record['target'], command,
                       None, record['id'], action='question')


def _answers(question, command):
    text = re.sub(r'^回答\s*', '', command).strip()
    specs = question['questions']
    if len(specs) == 1 and not re.match(r'^' + re.escape(specs[0]['id']) + r'\s*[:：]', text):
        values = {specs[0]['id']: text}
    else:
        pattern = r'(?:^|\n)\s*(' + '|'.join(re.escape(spec['id']) for spec in specs) + r')\s*[:：]\s*'
        parts = re.split(pattern, text)
        if parts[0].strip() or len(parts) == 1:
            raise ManagementError('invalid_change', 'Answer using each original question ID.')
        values = dict(zip(parts[1::2], parts[2::2]))
        if len(values) != len(parts[1::2]):
            raise ManagementError('invalid_change', 'One reply may name each question only once.')
    result = {}
    for spec in specs:
        if spec['id'] not in values:
            continue
        value = values[spec['id']].strip()
        if not value:
            raise ManagementError('invalid_change', 'An unanswered question stays pending.')
        options = {option['label'] for option in spec.get('options', [])}
        selected = [item.strip() for item in re.split(r'[,，;；]', value)]
        if value in {'跳过', 'skip'}:
            answer = {'id': spec['id'], 'selected': []}
        elif all(item in options for item in selected):
            if len(set(selected)) != len(selected) or len(selected) > 1 and not spec.get('multiSelect'):
                raise ManagementError('invalid_change', 'Use the original allowed selection count.')
            answer = {'id': spec['id'], 'selected': selected}
        else:
            answer = {'id': spec['id'], 'selected': [], 'custom': value}
        result[spec['id']] = answer
    return result


def connected_execution(intake, record):
    from .repository_supervision import attach_existing_owned
    return attach_existing_owned(intake, record)


async def process_reply(intake, prepared, generation):
    from .repository_supervision import owned_call, original_history
    intake.require_active(generation)
    if prepared.rejected:
        text = ('敏感答复已拦截；请在原生 DSH 私有界面处理。未向原工作发送公开输入。' if prepared.rejected == 'sensitive'
                else '本次答复未匹配唯一原问题轮次；请引用本轮提问并真实 @ 发问机器人。未向原工作发送输入。')
        await prepared.transport.send({'uuid': hashlib.sha256(('question-target:' + prepared.envelope['message_id']).encode()).hexdigest()[:32],
            'text': text,
            'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id']})
        intake.require_active(generation)
        return {'status': 'rejected', 'code': 'question_target'}
    async with intake.lock:
        descriptor = os.open(intake.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        carrier = None
        try:
            intake.require_active(generation)
            record = next(r for r in intake.snapshot()['work'] if r['id'] == prepared.work_id)
            execution = record['dsh_execution']
            question = next(q for q in execution.get('questions', [])
                            if q.get('delivery', {}).get('message_id') == prepared.envelope['parent_id'])
            if not question['answerable'] or execution['state'] in {'released', 'stopped', 'budget_stopped', 'execution_failed', 'outcome_unknown'}:
                raise ManagementError('binding_conflict', 'This original work no longer accepts public answers.')
            if question.get('reply_status') or prepared.envelope['message_id'] in question['reply_messages']:
                return {'status': question.get('reply_status', 'partial'), 'duplicate': True}
            _public_text(prepared.command, intake.secret_values)
            carrier = connected_execution(intake, record)
            events, projections = await asyncio.to_thread(original_history, carrier, execution['session_id'], record['target']['repo_path'])
            if question['generation'] != execution['generation'] or question['session_id'] != execution['session_id']:
                raise ManagementError('binding_conflict', 'The reply belongs to another execution generation.')
            if question['kind'] == 'structured':
                active = [q for q in projections['userQuestions']['active'] if q['callId'] == question['id']]
                if len(active) != 1 or active[0]['questions'] != question['questions']:
                    raise ManagementError('binding_conflict', 'The original question has expired or was already answered.')
            else:
                natural = _natural_question(events)
                if not natural or natural[0]['seq'] != question['source_seq'] or natural[1] != question['questions'][0]['question']:
                    raise ManagementError('binding_conflict', 'The original ordinary question changed or expired.')
            answers = _answers(question, prepared.command)
            question['answers'].update(answers)
            question['reply_messages'].append(prepared.envelope['message_id'])
            pending = [q['id'] for q in question['questions'] if q['id'] not in question['answers']]
            if pending:
                status, text = 'partial', '已记录本次答复；待答：' + '、'.join(pending) + '。收齐本轮后才送回原工作。'
            else:
                question['reply_status'] = 'intent'
                intake._save_execution(record, fields=['questions'])
                try:
                    batch = {'answers': [question['answers'][q['id']] for q in question['questions']]}
                    if question['kind'] == 'structured' and active[0]['state'] == 'continued':
                        question['reply_path'] = 'continued'
                        accepted = await asyncio.to_thread(owned_call, carrier, 'userQuestions/answer',
                            {'agentId': execution['session_id'], 'callId': question['id'], 'answer': batch})
                        if accepted is not True:
                            raise ManagementError('binding_conflict', 'The original continued question is no longer answerable.')
                    elif question['kind'] == 'structured':
                        question['reply_path'] = 'live'
                        packet = await asyncio.to_thread(carrier.event_frames)
                        await asyncio.to_thread(owned_call, carrier, '$events/result', {'clientId': packet['client_id'],
                            'eventId': question['event_id'], 'outcome': {'kind': 'result', 'value': batch}})
                    else:
                        listing = await asyncio.to_thread(owned_call, carrier, 'session/list', {})
                        session = next(row for row in listing['items'] if row['sessionId'] == execution['session_id'])
                        inbox = projections.get('inbox')
                        if not isinstance(inbox, dict) or any(inbox.values()) or session.get('agentAvailable') is not True:
                            raise ManagementError('outcome_unknown', 'Original ordinary input state is unavailable.')
                        question['request_id'] = 'answer-' + prepared.envelope['message_id']
                        request = {'sessionId': execution['session_id'], 'requestId': question['request_id'],
                            'mode': 'steer' if session['running'] else 'queue',
                            'content': [{'type': 'text', 'text': question['answers']['answer'].get('custom', prepared.command)}]}
                        source = question['questions'][0]['question']
                        envelope = await asyncio.to_thread(carrier.request, 'session/prompt', {'request': request,
                            '_hermes_question_source': {'seq': question['source_seq'],
                                'sha256': hashlib.sha256(source.encode()).hexdigest()}}, question['request_id'])
                        accepted = envelope.get('result', {}).get('value', {}) if envelope.get('result', {}).get('ok') else {}
                        if accepted.get('accepted') is not True:
                            raise ManagementError('outcome_unknown', 'Original ordinary answer acceptance is unconfirmed.')
                except (ManagementError, OSError, TimeoutError):
                    question['reply_status'] = 'outcome_unknown'
                    intake._save_execution(record, fields=['questions'])
                    intake.require_active(generation)
                    await prepared.transport.send({'uuid': hashlib.sha256(('question-unknown:' + prepared.envelope['message_id']).encode()).hexdigest()[:32],
                        'text': '本轮回送结果尚未确认；已保留原工作与答复记录，不会再次发送。请在原生界面核对当前轮次。',
                        'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id']})
                    intake.require_active(generation)
                    return {'status': 'outcome_unknown'}
                question['reply_status'] = 'queued'
                status, text = 'queued', '本轮已排入同一原 DSH 工作，等待原执行接纳；后续问题或进展继续关联本任务。'
            intake._save_execution(record, fields=['questions'])
            intake.require_active(generation)
            await prepared.transport.send({'uuid': hashlib.sha256(('question-reply:' + prepared.envelope['message_id']).encode()).hexdigest()[:32],
                'text': text, 'chat_id': prepared.envelope['chat_id'], 'reply_to': prepared.envelope['message_id']})
            intake.require_active(generation)
            return {'status': status}
        finally:
            if carrier is not None:
                carrier.close()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
