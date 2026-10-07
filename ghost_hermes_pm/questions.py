"""Human answers target live incoming requests on their original connection."""
from datetime import datetime, timezone
import hashlib
import json

from .manager import ManagementError, _public_text
from .control import _binding, _thread


def _now():
    return datetime.now(timezone.utc).isoformat()


APPROVAL_METHODS = {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                    'item/permissions/requestApproval'}


def _describe(manager, envelope):
    import re
    method, params = envelope['method'], envelope.get('params', {})
    description = {'category': 'original_interface', 'answerable': False, 'blocking': None}
    if not isinstance(params, dict):
        return description
    if method not in APPROVAL_METHODS | {'item/tool/requestUserInput'}:
        return description
    raw_questions = params.get('questions', [])
    sensitive = isinstance(raw_questions, list) and any(q.get('isSecret') is True or re.search(r'(?i)password|api[ _-]?key|credential|private key|验证码|密码|密钥|私钥|凭据', q.get('question', '') if isinstance(q.get('question'), str) else '') for q in raw_questions if isinstance(q, dict))
    try:
        _public_text(json.dumps(params), manager._sensitive_values())
    except ManagementError:
        sensitive = True
    if sensitive:
        return {**description, 'category': 'sensitive'}
    if not all(isinstance(params.get(k), str) and params[k] for k in ('threadId', 'turnId', 'itemId')):
        return description
    if method in APPROVAL_METHODS:
        operation = {k: params[k] for k in ('kind', 'command', 'cwd', 'reason', 'networkApprovalContext', 'permissions', 'grantRoot', 'commandActions', 'additionalPermissions', 'availableDecisions') if k in params}
        return {'category': 'approval', 'blocking': True, 'answerable': not params.get('additionalPermissions') and not params.get('availableDecisions'), 'operation': operation,
            'operation_id': hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest(), 'scope': 'turn'}
    questions = params.get('questions')
    if type(params.get('isBlocking')) is not bool or not isinstance(questions, list) or not questions or any(not isinstance(q, dict) or not isinstance(q.get('id'), str) or not q['id'] or not isinstance(q.get('question'), str) or type(q.get('isSecret', False)) is not bool for q in questions) or len({q['id'] for q in questions}) != len(questions):
        return description
    approval_content = any(re.search(r'(?i)approv|authoriz|permission|grant access|command|shell|stdin|network|socket|execute|run.*script|allow (?:running|executing|access|network)|批准|授权|命令|权限|网络|执行|联网|允许.*(?:运行|访问)', q['question']) for q in questions)
    return {'category': 'approval' if approval_content else 'question' if params['isBlocking'] else 'nonblocking',
        'blocking': params['isBlocking'], 'answerable': not approval_content, 'questions': questions}


def sync_human_requests(manager, record, adapter, thread=None):
    session = record['session']
    requests = record.setdefault('human_requests', [])
    for incoming in adapter.server_requests(session['thread_id']):
        envelope = incoming['envelope']
        params = envelope.get('params', {})
        key = hashlib.sha256(json.dumps([session['service_id'], session['generation'], type(envelope['id']).__name__, envelope['id']], sort_keys=True).encode()).hexdigest()
        question = next((q for q in requests if q['id'] == key), None)
        if question is None:
            question = {'id': key, 'rpc_id': envelope['id'], 'method': envelope['method'], 'service_id': session['service_id'],
                'generation': session['generation'], 'thread_id': session['thread_id'], 'turn_id': params.get('turnId'),
                'item_id': params.get('itemId'), 'approval_id': params.get('approvalId'), 'received_at': _now(), 'reply': None,
                'resolution': 'pending', 'execution_result': 'unverified', **_describe(manager, envelope),
                'original_interface': {'service_ref': adapter.service_ref, 'thread_id': session['thread_id'],
                    'item_id': params.get('itemId'), 'url': None, 'availability': 'original_client_required'}}
            requests.append(question)
        if incoming['state'] == 'resolved':
            question['resolution'] = 'resolved'
        elif incoming['state'] == 'outcome_unknown':
            question['resolution'] = 'outcome_unknown'
        elif question['turn_id'] != session['turn_id'] or record.get('repository_released') or session.get('control') != 'assigned_task':
            question['resolution'] = 'expired'
        if thread is not None:
            turn = next((t for t in thread.get('turns', []) if t.get('id') == question['turn_id']), None)
            if turn and turn.get('status') != 'inProgress' and question['resolution'] == 'pending':
                question['resolution'] = 'expired'
            item = next((i for i in (turn or {}).get('items', []) if i.get('id') == question['item_id']), None)
            if item and item.get('status') in {'completed', 'failed', 'declined'}:
                question['execution_result'] = item['status']
    if thread is not None:
        _natural_question(manager, record, thread)
    for question in requests:
        if question['method'] == 'natural_language' and question['resolution'] == 'pending':
            turn = next((t for t in (thread or {}).get('turns', []) if t.get('id') == question['turn_id']), None)
            messages = [i for i in (turn or {}).get('items', []) if i.get('type') == 'agentMessage']
            latest = messages[-1] if messages else {}
            if question['turn_id'] != session['turn_id'] or latest.get('id') != question['item_id'] or hashlib.sha256((latest.get('text') or '').encode()).hexdigest() != question['source_digest']:
                question['resolution'] = 'expired'
        try:
            adapter.verify_control(session['repository'], 'human_response', session['capability'])
            question['control_enabled'] = session.get('control') == 'assigned_task' and not record.get('repository_released') and record.get('task_delivery') != 'delivered' and record.get('execution') not in {'stopping', 'stopped'}
        except ManagementError:
            question['control_enabled'] = False


def _natural_question(manager, record, thread):
    session = record['session']
    turn = next((t for t in thread.get('turns', []) if t.get('id') == session['turn_id']), None)
    if not turn or turn.get('itemsView') != 'full':
        return
    messages = [i for i in turn.get('items', []) if i.get('type') == 'agentMessage']
    item = messages[-1] if messages else {}
    text = item.get('text')
    if not isinstance(text, str) or not text.rstrip().endswith(('?', '？')) or not isinstance(item.get('id'), str):
        return
    key = hashlib.sha256(json.dumps([session['service_id'], session['generation'], session['thread_id'], session['turn_id'], 'natural_language', item['id']]).encode()).hexdigest()
    if any(q['id'] == key for q in record['human_requests']):
        return
    description = _describe(manager, {'method': 'item/tool/requestUserInput', 'params': {'threadId': session['thread_id'], 'turnId': session['turn_id'], 'itemId': item['id'], 'isBlocking': True,
        'questions': [{'id': 'answer', 'header': 'Original question', 'question': text, 'isSecret': False, 'isOther': True, 'options': None}]}})
    description['blocking'] = None
    description['source_digest'] = hashlib.sha256(text.encode()).hexdigest()
    record['human_requests'].append({'id': key, 'method': 'natural_language', 'service_id': session['service_id'],
        'generation': session['generation'], 'thread_id': session['thread_id'], 'turn_id': session['turn_id'], 'item_id': item['id'],
        'received_at': _now(), 'reply': None, 'resolution': 'pending', 'execution_result': 'unverified', **description,
        'original_interface': {'service_ref': session['service_ref'], 'thread_id': session['thread_id'], 'item_id': item['id'],
            'url': None, 'availability': 'original_client_required'}})


def _subset(granted, requested):
    if isinstance(granted, dict):
        return isinstance(requested, dict) and all(k in requested and _subset(v, requested[k]) for k, v in granted.items())
    if isinstance(granted, list):
        return isinstance(requested, list) and all(v in requested for v in granted)
    return type(granted) is type(requested) and granted == requested


def _response(manager, question, response):
    _public_text(json.dumps(response), manager._sensitive_values())
    if question['method'] in APPROVAL_METHODS:
        allowed = {'decision', 'operation_id', 'scope'}
        permissions_request = question['method'] == 'item/permissions/requestApproval'
        if permissions_request:
            allowed.add('permissions')
        if set(response) - allowed or response.get('operation_id') != question['operation_id'] or response.get('scope') != 'turn' or response.get('decision') not in {'accept', 'decline', 'cancel'}:
            raise ManagementError('invalid_change', 'Approval requires the exact operation ID, explicit decision and turn scope.')
        if permissions_request:
            granted = response.get('permissions')
            requested = question['operation'].get('permissions')
            if not isinstance(granted, dict) or not _subset(granted, requested) or (response['decision'] != 'accept' and granted):
                raise ManagementError('invalid_change', 'Only an explicitly approved requested permission subset can be granted.')
            return {'permissions': granted, 'scope': 'turn'}
        return {'decision': response['decision']}
    if set(response) != {'answers'} or not isinstance(response['answers'], dict) or set(response['answers']) != {q['id'] for q in question['questions']}:
        raise ManagementError('invalid_change', 'Answers must correspond to every original question ID.')
    for question_spec in question['questions']:
        values = response['answers'][question_spec['id']]
        if not isinstance(values, list) or not values or len(values) > 100:
            raise ManagementError('invalid_change', 'Each question requires bounded text answers.')
        options = question_spec.get('options')
        for text in values:
            _public_text(text, manager._sensitive_values())
            if options and question_spec.get('isOther') is not True and text not in {o.get('label') for o in options if isinstance(o, dict)}:
                raise ManagementError('invalid_change', 'Select one of the original allowed options.')
    return {'answers': {key: {'answers': value} for key, value in response['answers'].items()}}


def answer_human_request(manager, identity, request_id, human_request_id, reply_id, response, source_anchor=None):
    if not isinstance(reply_id, str) or not reply_id or len(reply_id) > 256 or not isinstance(response, dict):
        raise ManagementError('invalid_change', 'A stable reply ID and structured response are required.')
    with manager._lock:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Only the verified owner can decide a human request.')
        record, session, adapter = _binding(manager, identity, request_id, data, 'human_response')
        if record.get('repository_released') or record.get('outer_task_status') == 'stopped' or record.get('stop', {}).get('status') == 'processing':
            raise ManagementError('forbidden', 'This work no longer accepts human execution answers.')
        question = next((q for q in record.get('human_requests', []) if q['id'] == human_request_id), None)
        if not question:
            raise ManagementError('invalid_change', 'Unknown original human request.')
        if any(q['id'] != human_request_id and q.get('reply', {}).get('id') == reply_id for r in data['requests'].values() for q in r.get('human_requests', []) if q.get('reply')):
            raise ManagementError('binding_conflict', 'This reply ID is already bound to another human request.')
        previous = question.get('reply')
        if previous:
            if previous['id'] != reply_id or previous['response'] != response:
                raise ManagementError('binding_conflict', 'This request already has a reply; reconcile its outcome.')
            return {**question, 'duplicate': True}
        thread = _thread(adapter, session, require_input=False)
        sync_human_requests(manager, record, adapter, thread)
        if question['resolution'] != 'pending' or question['turn_id'] != session['turn_id'] or not any(t.get('id') == question['turn_id'] and (t.get('status') == 'inProgress' or question['method'] == 'natural_language' and t.get('status') in {'completed', 'failed', 'interrupted'}) for t in thread.get('turns', [])):
            raise ManagementError('binding_conflict', 'The original request is expired or belongs to another turn.')
        if not question['answerable']:
            raise ManagementError('forbidden', 'Use the original private interface for this request; no answer is recorded here.')
        result = _response(manager, question, response)
        incoming = next((r['envelope'] for r in adapter.server_requests(session['thread_id']) if type(r['envelope']['id']) is type(question.get('rpc_id')) and r['envelope']['id'] == question.get('rpc_id')), None)
        question['reply'] = {'id': reply_id, 'actor': identity.subject, 'response': response, 'received': True, 'received_at': _now(), 'sent': 'intent'}
        if source_anchor is not None:
            question['reply']['source_anchor'] = dict(source_anchor)
        with manager._db:
            manager._save(version, data)
        try:
            if question['method'] == 'natural_language':
                outcome = manager.control_task(identity, request_id, 'append', reply_id, '\n'.join(response['answers']['answer']), question['turn_id'])
                _, data = manager._load()
                question = next(q for q in data['requests'][request_id]['human_requests'] if q['id'] == human_request_id)
                question['reply'].update(sent='accepted' if outcome['status'] == 'accepted' else 'outcome_unknown', sent_at=_now())
                question['resolution'] = 'input_accepted' if outcome['status'] == 'accepted' else 'outcome_unknown'
            else:
                adapter.respond_server_request(question['rpc_id'], incoming, result)
                question['reply'].update(sent='sent', sent_at=_now())
        except ManagementError as exc:
            if question['method'] == 'natural_language':
                _, data = manager._load()
                question = next(q for q in data['requests'][request_id]['human_requests'] if q['id'] == human_request_id)
            question['reply']['sent'] = 'outcome_unknown' if exc.code in {'outcome_unknown', 'unavailable'} else 'rejected'
            with manager._db:
                current, _ = manager._load()
                manager._save(current, data)
            raise
        with manager._db:
            current, _ = manager._load()
            manager._save(current, data)
        notify_human_requests(manager, identity, request_id)
        return question


def notify_human_requests(manager, identity, request_id):
    with manager._lock:
        _, data = manager._load()
        record = manager._request(identity, request_id, data)
        for question in record.get('human_requests', []):
            reply = question.get('reply') or {}
            state = [question['resolution'], reply.get('sent'), question['execution_result']]
            if question.get('notification_state') == state:
                continue
            text = ('人工请求：' + question['id'] + '\n分类：' + question['category'] +
                    '\n答复收到：' + ('是' if reply else '否') + '；送回：' + (reply.get('sent') or '未送回') +
                    '；原请求已处理：' + question['resolution'] + '；执行结果：' + question['execution_result'])
            if question['category'] in {'question', 'nonblocking'}:
                text += '\n' + '\n'.join(q['question'] for q in question.get('questions', []))
                text += '\n本人可回复：回答 ' + question['id'] + '：答案'
            elif question['category'] == 'approval' and question['answerable']:
                text += '\n具体操作：' + json.dumps(question['operation'], ensure_ascii=False, sort_keys=True)
                text += '\n操作 ID：' + question['operation_id'] + '；范围：turn（仅本回合）'
                text += '\n本人须明确批准或拒绝并引用请求 ID、操作 ID 及范围。'
            else:
                text += '\n请在原服务的安全原界面处理。服务：' + question['original_interface']['service_ref'] + '\n原会话：' + question['thread_id'] + '；安全链接尚不可用。不要在群里发送秘密答案。'
            publication = manager.publish_request_message(identity, request_id, 'progress', text)
            with manager._db:
                version, current = manager._load()
                saved = next(q for q in current['requests'][request_id]['human_requests'] if q['id'] == question['id'])
                saved['notification_state'] = state
                saved.setdefault('notification_ids', []).append(publication['id'])
                manager._save(version, current)


def associate_human_reply(manager, identity, project_id, profile_id, message, text):
    import re
    import uuid
    from .manager import _message_anchor, _MESSAGE_NAMESPACE
    match = re.fullmatch(r'回答(?:\s+([a-f0-9]{64}))?[：:]\s*(.+)', text, re.DOTALL)
    decision = re.fullmatch(r'(批准|拒绝)\s+([a-f0-9]{64})\s+操作\s+([a-f0-9]{64})\s+范围\s+(turn)(?:\s+权限\s+(.+))?', text, re.DOTALL)
    if not match and not decision:
        return {'status': 'unassociated'}
    with manager._lock:
        version, data = manager._load()
        if manager._principal(identity, data) is not None:
            raise ManagementError('forbidden', 'Only the verified owner entry can answer a human request.')
        _message_anchor(message)
        _public_text(text, manager._sensitive_values())
        target_id = decision.group(2) if decision else match.group(1)
        candidates = []
        for record in data['requests'].values():
            if record['project_id'] != project_id or record['profile_id'] != profile_id:
                continue
            same_namespace = all(record['source_anchor'].get(k) == message.get(k) for k in _MESSAGE_NAMESPACE)
            if not same_namespace and not target_id:
                continue
            for q in record.get('human_requests', []):
                if target_id and q['id'] != target_id:
                    continue
                if not target_id and (q['resolution'] != 'pending' or q.get('reply')):
                    continue
                parent = message.get('parent_id')
                if parent and not target_id:
                    anchors = {a.get('message_id') for a in [record['source_anchor'], record['task_start_anchor'] or {}]}
                    anchors |= {attempt.get('message_id') for p in record['outbox'] if p['id'] in q.get('notification_ids', []) for segment in p['segments'] for attempt in segment['attempts'] if attempt['status'] == 'delivered'}
                    if parent not in anchors:
                        continue
                candidates.append((record, q))
        if not candidates:
            raise ManagementError('binding_conflict', 'No uniquely bound original human request exists; quote its request ID.')
        if len(candidates) > 1:
            key = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
            clarification = data['clarifications'].get(key)
            if clarification is None:
                clarification = {'id': key, 'status': 'needs_clarification', 'project_id': project_id, 'profile_id': profile_id,
                    'source_anchor': dict(message), 'candidate_ids': [q['id'] for _, q in candidates], 'kind': 'human_reply',
                    'uuid': str(uuid.uuid4()), 'delivery': 'pending'}
                data['clarifications'][key] = clarification
                with manager._db:
                    manager._save(version, data)
            return clarification
        record, q = candidates[0]
        if decision:
            response = {'decision': 'accept' if decision.group(1) == '批准' else 'decline', 'operation_id': decision.group(3), 'scope': decision.group(4)}
            if q['method'] == 'item/permissions/requestApproval':
                try:
                    response['permissions'] = json.loads(decision.group(5)) if decision.group(5) else {} if response['decision'] == 'decline' else None
                except ValueError as exc:
                    raise ManagementError('invalid_change', 'Permission approval requires a valid explicit JSON subset.') from exc
        else:
            if q['category'] not in {'question', 'nonblocking'} or len(q.get('questions', [])) != 1:
                raise ManagementError('invalid_change', 'Use explicit operation approval or answer multiple questions in Dashboard.')
            response = {'answers': {q['questions'][0]['id']: [match.group(2)]}}
        reply_id = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
        answered = answer_human_request(manager, identity, record['id'], q['id'], reply_id, response, message)
        return {'status': 'answered', 'request_id': record['id'], 'human_request': answered}
