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


def _without_fenced_material(text):
    lines, fence = [], None
    for line in text.splitlines():
        opening = re.match(r'^[ \t]*(`{3,}|~{3,})', line)
        if fence is not None:
            if re.fullmatch(r'[ \t]*' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*', line):
                fence = None
            lines.append('<markdown material>')
        elif opening:
            fence = opening.group(1)
            lines.append('<markdown material>')
        else:
            lines.append(line)
    return '\n'.join(lines)


def _merge_decision(text):
    from .delivery import OPTIONAL_MERGE, delivery_requirements
    # Materials and quotations do not express a new decision by the current Owner.
    text = re.sub(r'“[^”]*”|‘[^’]*’|「[^」]*」|"[^"]*"|\'[^\']*\'', '<quoted material>', text)
    target = r'(?:(?:此|本|该)?\s*(?:PR|拉取请求|代码|变更|提交)|(?:(?:a|the)\s+)?(?:PR|pull request|code|changes))'
    action = r'(?:合并|merg(?:e|ed|ing))\s*(?:' + target + r')?(?:\s*(?:到|至|into)\s*[\w./-]+)?'
    decisions = []
    for clause in re.split(r'[,，;；。\n]+', text):
        clause = re.sub(r'^\s*(?:本人明确[：:]\s*)?(?:(?:本任务|当前任务|此任务|this task|current task)\s*)?(?:本人|我|I)?\s*', '', clause, flags=re.IGNORECASE).strip()
        if re.fullmatch(r'(?:不批准|不同意|不允许|拒绝|do not approve|deny|refuse)\s*' + action, clause, re.IGNORECASE):
            decisions.append((False, True))
        elif re.fullmatch(r'(?:批准|同意|允许|approve|authorize|consent to)\s*' + action, clause, re.IGNORECASE):
            decisions.append((True, False))
        else:
            optional = OPTIONAL_MERGE.search(clause)
            if optional and re.fullmatch(r'\s*(?:' + target + r')?\s*', clause[:optional.start()], re.IGNORECASE) and re.fullmatch(r'\s*(?:' + target + r')?\s*', clause[optional.end():], re.IGNORECASE):
                decisions.append((False, False))
            elif re.fullmatch(r'(?:(?:' + target + r')\s*)?(?:(?:必须|务必|须|应当|需要|要求|禁止|不得|不要|不能|勿|must|shall|has to|have to|needs to|is required to|must not|shall not|do not|never)\s*(?:be\s+)?)?' + action, clause, re.IGNORECASE):
                policy = delivery_requirements(clause)
                if not policy['clarification_criteria'] and (policy['merge_required'] or policy['merge_forbidden']):
                    decisions.append((policy['merge_required'], policy['merge_forbidden']))
    if not decisions or any(required for required, _ in decisions) and any(not required for required, _ in decisions):
        return
    return {'merge_required': any(required for required, _ in decisions),
            'merge_forbidden': any(forbidden for _, forbidden in decisions), 'clarification_criteria': []}


def owner_scope_answer(record, text):
    from .delivery import frozen_delivery_requirements
    criteria = frozen_delivery_requirements(record)['clarification_criteria']
    decision_text = _without_fenced_material(text)
    if not criteria or not re.search(r'本任务|当前任务|此任务|\b(?:this|current) task\b', decision_text, re.IGNORECASE) or re.search(r'建议|或许|可能|\b(?:suggest|perhaps|maybe)\b|[?？]', decision_text, re.IGNORECASE):
        return
    targets = [criterion for criterion in criteria if criterion in text]
    if not targets and len(criteria) == 1:
        targets = criteria
    if len(targets) != 1:
        return {'status': 'needs_clarification', 'reason': 'Name the exact original acceptance item before its interpretation is applied.'}
    for left, right in [('“', '”'), ('‘', '’'), ('「', '」'), ('"', '"'), ("'", "'")]:
        decision_text = decision_text.replace(left + targets[0] + right, '')
    decision_text = decision_text.replace(targets[0], '')
    policy = _merge_decision(decision_text)
    if policy is None:
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
