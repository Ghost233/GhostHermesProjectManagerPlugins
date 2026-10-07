"""Owner interpretations of frozen unclear acceptance through accepted original input."""
import hashlib
import json
import re


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _authorization(record, data):
    profile = data['profiles'].get(record['profile_id'], {})
    responsibility = {key: profile.get(key) for key in record['accepted_responsibility']}
    repository = data['projects'].get(record['project_id'], {}).get('repo')
    if responsibility != record['accepted_responsibility'] or profile.get('connection_refs', {}).get('codex') != record.get('accepted_codex_ref') or _digest(repository) != record.get('accepted_repository_fingerprint'):
        return None
    session = record.get('session', {})
    return _digest({'responsibility': responsibility, 'repository': repository,
        'service_id': session.get('service_id'), 'generation': session.get('generation'),
        'thread_id': session.get('thread_id'), 'arrangement_id': record.get('current_arrangement_id'),
        'control_grant_id': record.get('control_grant_id')})


def owner_scope_answer(record, text):
    from .delivery import delivery_requirements, frozen_delivery_requirements
    criteria = frozen_delivery_requirements(record)['clarification_criteria']
    if not criteria or not re.search(r'本任务|当前任务|此任务|\b(?:this|current) task\b', text, re.IGNORECASE) or re.search(r'建议|或许|可能|\b(?:suggest|perhaps|maybe)\b|[?？]', text, re.IGNORECASE):
        return
    targets = [criterion for criterion in criteria if criterion in text]
    if not targets and len(criteria) == 1:
        targets = criteria
    if len(targets) != 1:
        return {'status': 'needs_clarification', 'reason': 'Name the exact original acceptance item before its interpretation is applied.'}
    decision_text = text.replace(targets[0], '')
    # An explanation must explicitly decide merging; unrelated Owner input is insufficient.
    refused = re.search(r'(?:不(?:批准|同意|允许)|拒绝)\s*合并|\b(?:do not approve|deny|refuse)\s+(?:(?:a|the)\s+)?merg(?:e|ing)\b', decision_text, re.IGNORECASE)
    approved = not refused and re.search(r'(?:批准|同意|允许)\s*合并|\b(?:approve|authorize|consent to)\s+(?:(?:a|the)\s+)?merg(?:e|ing)\b', decision_text, re.IGNORECASE)
    policy = delivery_requirements(decision_text)
    if refused:
        policy = {'merge_required': False, 'merge_forbidden': True, 'clarification_criteria': []}
    elif approved:
        policy = {'merge_required': True, 'merge_forbidden': False, 'clarification_criteria': []}
    optional = re.search(r'(?:无需|不需要|不要求|可选|酌情|可以).{0,30}合并|合并.{0,20}(?:可选|不作要求|不作为交付条件)|\b(?:not required|not necessary|does not need|no|without|optional|may|can|could)\b.{0,40}\bmerg(?:e|ed|ing)\b|\bmerg(?:e|ed|ing)\b.{0,40}\b(?:not required|optional)\b', decision_text, re.IGNORECASE)
    if policy['clarification_criteria'] or not (policy['merge_required'] or policy['merge_forbidden'] or optional):
        return
    return {'status': 'resolved', 'criterion': targets[0], 'policy': policy}


def record_owner_interpretation(manager, identity, record, instruction, data):
    if identity.subject != manager.owner_identity_ref or manager._principal(identity, data) is not None or instruction['action'] != 'append' or instruction['phase'] != 'rpc_accepted':
        return
    answer = owner_scope_answer(record, instruction['text'])
    if not answer:
        return
    if answer['status'] == 'needs_clarification':
        instruction['scope_interpretation'] = answer
        return
    policy = answer['policy']
    authorization = _authorization(record, data)
    if policy['clarification_criteria'] or authorization is None:
        return
    source = next((message['source_anchor'] for message in record.get('messages', []) if message['id'] == instruction['id']), None)
    interpretation = {'id': instruction['id'], 'request_id': record['id'], 'criterion': answer['criterion'],
        'scope_digest': _digest(record['accepted_scope']), 'authorization_digest': authorization,
        'statement': instruction['text'], 'owner': identity.subject, 'owner_source': identity.source,
        'source_anchor': source, 'control_id': instruction['id'], 'thread_id': instruction['thread_id'],
        'turn_id': instruction['turn_id'], 'generation': instruction['generation'],
        'merge_required': policy['merge_required'], 'merge_forbidden': policy['merge_forbidden'],
        'received_at': instruction['accepted_at'], 'sent_back_at': instruction['rpc_accepted_at'], 'status': 'applied'}
    record.setdefault('delivery_scope_interpretations', []).append(interpretation)
    instruction['scope_interpretation'] = {'id': interpretation['id'], 'status': 'applied'}


def apply_owner_interpretations(manager, record, data, original):
    result = {**original, 'merge_criteria': list(original['merge_criteria']),
              'clarification_criteria': list(original['clarification_criteria'])}
    controls = {control['id']: control for control in record.get('controls', [])}
    authorization = _authorization(record, data)
    applied = []
    latest = {}
    for item in record.get('delivery_scope_interpretations', []):
        control = controls.get(item['control_id'], {})
        if item.get('request_id') != record['id'] or item.get('owner') != manager.owner_identity_ref or item.get('scope_digest') != _digest(record['accepted_scope']) or not authorization or item.get('authorization_digest') != authorization or control.get('actor') != item['owner'] or control.get('action') != 'append' or control.get('phase') != 'rpc_accepted' or any(control.get(key) != item.get(key) for key in ('thread_id', 'turn_id', 'generation')) or item['criterion'] not in original['clarification_criteria']:
            continue
        latest[item['criterion']] = item
    for item in latest.values():
        result['clarification_criteria'] = [text for text in result['clarification_criteria'] if text != item['criterion']]
        if item['merge_required']:
            result['merge_required'] = True
            result['merge_criteria'].append(item['criterion'])
        if item['merge_forbidden']:
            result['merge_forbidden'] = True
        applied.append(item['id'])
    if result['merge_required'] and result['merge_forbidden']:
        result['clarification_criteria'] = list(original['clarification_criteria'])
    result['owner_interpretation_ids'] = applied
    return result
