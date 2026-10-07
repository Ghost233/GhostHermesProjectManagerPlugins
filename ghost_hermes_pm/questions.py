"""Human answers target live incoming requests on their original connection."""
from datetime import datetime, timezone
import hashlib
import json

from .manager import ManagementError, _public_text
from .control import _binding, _thread


def _now():
    return datetime.now(timezone.utc).isoformat()


def sync_human_requests(manager, record, adapter):
    session = record['session']
    requests = record.setdefault('human_requests', [])
    for incoming in adapter.server_requests(session['thread_id']):
        envelope = incoming['envelope']
        params = envelope.get('params', {})
        key = hashlib.sha256(json.dumps([session['service_id'], session['generation'], type(envelope['id']).__name__, envelope['id']], sort_keys=True).encode()).hexdigest()
        question = next((q for q in requests if q['id'] == key), None)
        if question is None:
            if envelope['method'] != 'item/tool/requestUserInput':
                continue
            question = {'id': key, 'rpc_id': envelope['id'], 'method': envelope['method'], 'service_id': session['service_id'],
                'generation': session['generation'], 'thread_id': session['thread_id'], 'turn_id': params.get('turnId'),
                'item_id': params.get('itemId'), 'category': 'question', 'blocking': params.get('isBlocking'),
                'questions': params.get('questions', []), 'received_at': _now(), 'reply': None,
                'resolution': 'pending', 'execution_result': 'unverified'}
            requests.append(question)
        if incoming['state'] == 'resolved':
            question['resolution'] = 'resolved'
        elif incoming['state'] == 'outcome_unknown':
            question['resolution'] = 'outcome_unknown'


def answer_human_request(manager, identity, request_id, human_request_id, reply_id, response):
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
        previous = question.get('reply')
        if previous:
            if previous['id'] != reply_id or previous['response'] != response:
                raise ManagementError('binding_conflict', 'This request already has a reply; reconcile its outcome.')
            return {**question, 'duplicate': True}
        thread = _thread(adapter, session, require_input=False)
        sync_human_requests(manager, record, adapter)
        if question['resolution'] != 'pending' or question['turn_id'] != session['turn_id'] or not any(t.get('id') == question['turn_id'] and t.get('status') == 'inProgress' for t in thread.get('turns', [])):
            raise ManagementError('binding_conflict', 'The original request is expired or belongs to another turn.')
        if set(response) != {'answers'} or not isinstance(response['answers'], dict) or set(response['answers']) != {q['id'] for q in question['questions']}:
            raise ManagementError('invalid_change', 'Answers must correspond to every original question ID.')
        for values in response['answers'].values():
            if not isinstance(values, list) or not values:
                raise ManagementError('invalid_change', 'Each question requires text answers.')
            for text in values:
                _public_text(text, manager._sensitive_values())
        result = {'answers': {key: {'answers': value} for key, value in response['answers'].items()}}
        incoming = next((r['envelope'] for r in adapter.server_requests(session['thread_id']) if type(r['envelope']['id']) is type(question['rpc_id']) and r['envelope']['id'] == question['rpc_id']), None)
        question['reply'] = {'id': reply_id, 'actor': identity.subject, 'response': response, 'received': True, 'received_at': _now(), 'sent': 'intent'}
        with manager._db:
            manager._save(version, data)
        try:
            adapter.respond_server_request(question['rpc_id'], incoming, result)
            question['reply'].update(sent='sent', sent_at=_now())
        except ManagementError as exc:
            question['reply']['sent'] = 'outcome_unknown' if exc.code in {'outcome_unknown', 'unavailable'} else 'rejected'
            with manager._db:
                manager._save(version + 1, data)
            raise
        with manager._db:
            manager._save(version + 1, data)
        return question
