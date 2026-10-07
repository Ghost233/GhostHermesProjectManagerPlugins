"""Role handoffs retain Owner origin, public delivery and independent reception."""
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from .manager import ManagementError, _message_anchor, _public_text


def _now():
    return datetime.now(timezone.utc).isoformat()


def _state(data):
    return data.setdefault('collaboration', {'channels': {}, 'handoffs': {}, 'real_group_acceptance': 'unverified', 'enabled': False})


def _binding(profile):
    return {k: profile.get(k) for k in ('id', 'identity_ref', 'native_profile', 'role', 'capability', 'project_id', 'parent_profile_id')}


def _channel(data, channel_id):
    channel = _state(data)['channels'].get(channel_id)
    if not channel or channel['profile_binding'] != _binding(data['profiles'].get(channel['profile_id'], {})):
        raise ManagementError('binding_conflict', 'The registered role channel responsibility changed.')
    return channel



def _freeze_channels(sending, receiving):
    return json.loads(json.dumps({sending['id']: sending, receiving['id']: receiving}))


def _handoff_channels(data, handoff):
    sending, receiving = _channel(data, handoff['sender_channel_id']), _channel(data, handoff['target_channel_id'])
    if handoff.get('channel_bindings') != _freeze_channels(sending, receiving):
        raise ManagementError('binding_conflict', 'The original handoff channel identities changed; reconcile this frozen public work before further publication or reception.')
    return sending, receiving

def snapshot(data, principal, visible):
    state = _state(data)
    handoffs = [h for h in state['handoffs'].values() if principal is None or principal['role'] == 'steward' or h['target_profile_id'] in visible or h['sender_profile_id'] in visible]
    projected = []
    for handoff in handoffs:
        try:
            _handoff_channels(data, handoff)
            projected.append({**handoff, 'channel_binding': 'registered_match'})
        except ManagementError as exc:
            projected.append({**handoff, 'channel_binding': 'unverified', 'channel_reason': str(exc)})
    channels = [c for c in state['channels'].values() if principal is None or principal['role'] == 'steward' or c['profile_id'] in visible]
    return {'enabled': False, 'real_group_acceptance': 'unverified', 'channels': channels, 'handoffs': projected}


def authorize_accept(manager, identity, delegation_id, project_id, profile_id, message, issue, data):
    handoff = _state(data)['handoffs'].get(delegation_id)
    principal = manager._principal(identity, data)
    if identity.source != 'native-collaboration-ingress' or not principal or not handoff or handoff['target_profile_id'] != principal['id'] or profile_id != principal['id'] or project_id != principal['project_id'] or handoff['acceptance'] != 'source_received' or handoff['received_anchor'] != message or handoff['issue'] != issue:
        raise ManagementError('forbidden', 'Delegated work requires the independent original native receiver and Owner-scoped handoff.')
    _channel(data, handoff['target_channel_id'])
    return {'actor': {'subject': identity.subject, 'source': identity.source, 'profile_id': principal['id']},
        'owner_origin': handoff['owner_origin'], 'delegation_id': delegation_id,
        'forwarding_profile_id': handoff['sender_profile_id'], 'new_owner_decision': False}


def _new_handoff(manager, data, sender, target, issue_url, owner_origin, source_anchor, parent_id):
    state = _state(data)
    targets = [c for c in state['channels'].values() if c['profile_id'] == target['id'] and c['group_kind'] == 'project']
    if len(targets) != 1:
        raise ManagementError('binding_conflict', 'The receiving project role channel must be unique.')
    receiving = _channel(data, targets[0]['id'])
    senders = [c for c in state['channels'].values() if c['profile_id'] == sender['id'] and c['chat_id'] == receiving['chat_id'] and c['group_kind'] == 'project']
    if len(senders) != 1 or not isinstance(issue_url, str) or not re.fullmatch(r'https://github\.com/' + re.escape(receiving['repository']) + r'/issues/[1-9]\d*', issue_url):
        raise ManagementError('binding_conflict', 'The sender group and explicit child Issue repository must match.')
    sending = _channel(data, senders[0]['id'])
    if len([b for b in sending['bot_sources'] if b['profile_id'] == target['id']]) != 1:
        raise ManagementError('binding_conflict', 'The target bot must have an observed identity in the sending app namespace.')
    key = hashlib.sha256(json.dumps(['work', sender['id'], target['id'], parent_id, issue_url]).encode()).hexdigest()
    existing = state['handoffs'].get(key)
    if existing:
        return {**existing, 'duplicate': True}
    issue = manager.delivery_source.read_issue(issue_url) if manager.delivery_source else None
    if not isinstance(issue, dict) or issue.get('url') != issue_url or any(not isinstance(issue.get(k), str) or not issue[k] for k in ('title', 'body', 'updated_at')):
        raise ManagementError('unavailable', 'The original child Issue source could not be verified.')
    _public_text(issue['title'] + '\n' + issue['body'], manager._sensitive_values())
    text = ('子 Issue 工作交接：' + issue['title'] + '\nIssue：' + issue['url'] + '\n受理版本：' + issue['updated_at'] +
        '\n原本人目标关联：' + parent_id + '\n明确子范围：' + issue['body'])
    chunks = [text[n:n + 1400] for n in range(0, len(text), 1400)]
    segments = [{'number': n + 1, 'uuid': str(uuid.uuid4()), 'status': 'pending', 'text': '[hermes-role-work ' + key + ' ' + str(n + 1) + '/' + str(len(chunks)) + ']\n' + chunk, 'attempts': []} for n, chunk in enumerate(chunks)]
    handoff = {'id': key, 'sender_profile_id': sender['id'], 'target_profile_id': target['id'], 'kind': 'work',
        'source_anchor': dict(source_anchor), 'owner_origin': owner_origin, 'issue': issue,
        'sender_channel_id': sending['id'], 'target_channel_id': receiving['id'], 'segments': segments,
        'channel_bindings': _freeze_channels(sending, receiving),
        'delivery': 'pending', 'acceptance': 'awaiting_receiver', 'received_parts': {}, 'task_request_id': None,
        'parent_handoff_id': parent_id, 'created_at': _now(), 'whole_project_complete': False}
    state['handoffs'][key] = handoff
    return handoff


def _result_handoff(manager, data, sender, target, original, text, kind='result'):
    state = _state(data)
    targets = [c for c in state['channels'].values() if c['profile_id'] == target['id'] and c['group_kind'] == 'project']
    if len(targets) != 1:
        raise ManagementError('binding_conflict', 'The result receiver project channel must be unique.')
    receiving = targets[0]
    senders = [c for c in state['channels'].values() if c['profile_id'] == sender['id'] and c['chat_id'] == receiving['chat_id'] and c['group_kind'] == 'project']
    if len(senders) != 1:
        raise ManagementError('binding_conflict', 'The result sender must be in the same project group.')
    sending = senders[0]
    _channel(data, receiving['id']); _channel(data, sending['id'])
    if len([b for b in sending['bot_sources'] if b['profile_id'] == target['id']]) != 1:
        raise ManagementError('binding_conflict', 'The result target bot identity is not registered in this sending namespace.')
    key = hashlib.sha256(json.dumps([kind, original['id'], sender['id'], target['id'], text if kind in {'summary', 'progress'} else None]).encode()).hexdigest()
    if key in state['handoffs']:
        return {**state['handoffs'][key], 'duplicate': True}
    _public_text(text, manager._sensitive_values())
    chunks = [text[n:n + 1400] for n in range(0, len(text), 1400)]
    segments = [{'number': n + 1, 'uuid': str(uuid.uuid4()), 'status': 'pending', 'text': '[hermes-role-' + kind + ' ' + key + ' ' + str(n + 1) + '/' + str(len(chunks)) + ']\n' + chunk, 'attempts': []} for n, chunk in enumerate(chunks)]
    record = {'id': key, 'kind': kind, 'sender_profile_id': sender['id'], 'target_profile_id': target['id'],
        'source_anchor': dict(original['received_anchor']), 'owner_origin': original['owner_origin'], 'issue': original['issue'],
        'sender_channel_id': sending['id'], 'target_channel_id': receiving['id'], 'segments': segments,
        'channel_bindings': _freeze_channels(sending, receiving),
        'delivery': 'pending', 'acceptance': 'awaiting_receiver', 'received_parts': {}, 'task_request_id': None,
        'result_task_id': original.get('task_request_id'), 'original_handoff_id': original['id'],
        'parent_handoff_id': original.get('parent_handoff_id'), 'created_at': _now(), 'whole_project_complete': False}
    state['handoffs'][key] = record
    return record



def synchronize_direct_request(manager, identity, data, task, profile):
    if profile['role'] != 'subproject_lead':
        return
    origin = {'subject': identity.subject, 'source': identity.source, 'source_anchor': dict(task['source_anchor'])}
    task['actor_provenance'] = {'actor': {'subject': identity.subject, 'source': identity.source},
        'owner_origin': origin, 'new_owner_decision': True}
    parents = [h for h in _state(data)['handoffs'].values() if h['kind'] == 'work' and h['target_profile_id'] == profile['parent_profile_id'] and h['acceptance'] == 'accepted' and not h.get('parent_handoff_id')]
    original = {'id': task['id'], 'received_anchor': task['source_anchor'], 'owner_origin': origin,
        'issue': task['accepted_scope'], 'task_request_id': task['id'],
        'parent_handoff_id': parents[0]['id'] if len(parents) == 1 else None}
    try:
        handoff = _result_handoff(manager, data, profile, data['profiles'][profile['parent_profile_id']], original,
            '本人直接派发子 Issue，答复仍回本人；同步上级供协调。\nIssue：' + task['accepted_scope']['url'] + '\n范围：' + task['accepted_scope']['title'], 'progress')
    except (ManagementError, StopIteration):
        task['parent_sync_status'] = 'unverified'
        return
    handoff['direct_task_id'] = task['id']
    task['parent_sync_handoff_id'] = handoff['id']
    task['parent_sync_status'] = 'pending'

def perform(manager, identity, action, details):
    if not isinstance(details, dict):
        raise ManagementError('invalid_change', 'A bounded role operation is required.')
    with manager._lock:
        version, data = manager._load()
        principal = manager._principal(identity, data)
        state = _state(data)
        if action == 'read_routes':
            if details:
                raise ManagementError('invalid_change', 'Role route reads accept no actor fields.')
            visible = set(data['profiles']) if principal is None or principal['role'] == 'steward' else manager._visible_profile_ids(principal, data)
            return {**snapshot(data, principal, visible), 'profiles': [p for p in data['profiles'].values() if p['id'] in visible]}
        if action == 'register_channels':
            if principal is not None or set(details) != {'channels'} or not isinstance(details['channels'], list):
                raise ManagementError('forbidden', 'Only the Owner registers existing verified role/group namespaces.')
            for supplied in details['channels']:
                allowed = {'id', 'profile_id', 'group_kind', 'project_id', 'app_id', 'recipient_open_id', 'recipient_tenant_key', 'transport_tenant_key', 'chat_id', 'owner_open_id', 'owner_tenant_key', 'repository', 'verification_ref', 'bot_sources'}
                if not isinstance(supplied, dict) or set(supplied) != allowed or any(not isinstance(v, str) or not v for k, v in supplied.items() if k not in {'project_id', 'bot_sources'}):
                    raise ManagementError('invalid_change', 'Role channels require exact registered scalar identities and source observations.')
                profile = data['profiles'].get(supplied['profile_id'])
                if not profile or supplied['group_kind'] not in {'entry', 'project'} or supplied['group_kind'] == 'entry' and (profile['role'] != 'steward' or supplied['project_id'] is not None) or supplied['group_kind'] == 'project' and supplied['project_id'] not in data['projects']:
                    raise ManagementError('invalid_change', 'The channel does not match an existing role and project group.')
                if profile['role'] in {'project_lead', 'subproject_lead'}:
                    group_project = data['profiles'][profile['parent_profile_id']]['project_id'] if profile['role'] == 'subproject_lead' else profile['project_id']
                    if supplied['group_kind'] != 'project' or supplied['project_id'] != group_project:
                        raise ManagementError('forbidden', 'Project roles stay in their own project group.')
                if not isinstance(supplied['bot_sources'], list) or any(not isinstance(b, dict) or set(b) != {'profile_id', 'open_id', 'tenant_key', 'native_ids'} or b['profile_id'] not in data['profiles'] or not isinstance(b['native_ids'], list) or not b['native_ids'] or any(not isinstance(i, str) or not i for i in b['native_ids']) for b in supplied['bot_sources']):
                    raise ManagementError('invalid_change', 'Bot source mappings require registered identities in the receiving app namespace.')
                _public_text(json.dumps(supplied), manager._sensitive_values())
                state['channels'][supplied['id']] = {**supplied, 'profile_binding': _binding(profile)}
            result = {'status': 'registered', 'enabled': False}
        elif action in {'project_goal', 'owner_project_goal'}:
            native_owner = action == 'owner_project_goal' and identity.source == 'native-collaboration-ingress' and principal is not None and principal['role'] == 'steward' and principal['id'] == details.get('sender_profile_id')
            if (principal is not None and not native_owner) or set(details) != {'sender_profile_id', 'target_profile_id', 'source_anchor', 'issue_url'}:
                raise ManagementError('forbidden', 'A new project goal requires the verified Owner.')
            sender = data['profiles'].get(details['sender_profile_id'], {})
            target = data['profiles'].get(details['target_profile_id'], {})
            if sender.get('role') != 'steward' or target.get('role') != 'project_lead':
                raise ManagementError('forbidden', 'A project goal explicitly names its steward and registered project lead.')
            message = details['source_anchor']
            required = _message_anchor(message)
            entry = [c for c in state['channels'].values() if c['profile_id'] == sender['id'] and c['group_kind'] == 'entry' and all(c.get(k) == message.get(k) for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')) and c['owner_open_id'] == message['sender_open_id'] and c['owner_tenant_key'] == message['tenant_key']]
            targets = [c for c in state['channels'].values() if c['profile_id'] == target['id'] and c['group_kind'] == 'project']
            if len(entry) != 1 or len(targets) != 1:
                raise ManagementError('binding_conflict', 'The source entry and destination role channel must be unique.')
            receiving = _channel(data, targets[0]['id'])
            senders = [c for c in state['channels'].values() if c['profile_id'] == sender['id'] and c['chat_id'] == receiving['chat_id'] and c['group_kind'] == 'project']
            if len(senders) != 1 or not details['issue_url'].startswith('https://github.com/' + receiving['repository'] + '/issues/'):
                raise ManagementError('binding_conflict', 'The steward must be registered in the destination group and Issue repository.')
            sending = _channel(data, senders[0]['id'])
            mentions = [b for b in sending['bot_sources'] if b['profile_id'] == target['id']]
            if len(mentions) != 1:
                raise ManagementError('binding_conflict', 'A real target mention needs the target bot identity in the sending app namespace.')
            key = hashlib.sha256(json.dumps([message[k] for k in required]).encode()).hexdigest()
            existing = state['handoffs'].get(key)
            if existing:
                if existing['target_profile_id'] != target['id'] or existing['issue']['url'] != details['issue_url']:
                    raise ManagementError('binding_conflict', 'This original Owner message already has a different project goal.')
                return {**existing, 'duplicate': True}
            if manager.delivery_source is None:
                raise ManagementError('unavailable', 'A trusted Issue source is required.')
            issue = manager.delivery_source.read_issue(details['issue_url'])
            if not isinstance(issue, dict) or issue.get('url') != details['issue_url'] or any(not isinstance(issue.get(k), str) or not issue[k] for k in ('title', 'body', 'updated_at')):
                raise ManagementError('invalid_change', 'The original Issue scope could not be verified.')
            _public_text(issue['title'] + '\n' + issue['body'], manager._sensitive_values())
            origin = {'subject': manager.owner_identity_ref if native_owner else identity.subject, 'source': 'verified-native-collaboration-owner' if native_owner else identity.source, 'source_anchor': dict(message)}
            text = '项目工作交接：' + issue['title'] + '\nIssue：' + issue['url'] + '\n受理版本：' + issue['updated_at'] + '\n原本人目标：' + issue['body']
            chunks = [text[n:n + 1400] for n in range(0, len(text), 1400)]
            segments = [{'number': n + 1, 'uuid': str(uuid.uuid4()), 'status': 'pending', 'text': '[hermes-role-work ' + key + ' ' + str(n + 1) + '/' + str(len(chunks)) + ']\n' + chunk, 'attempts': []} for n, chunk in enumerate(chunks)]
            result = {'id': key, 'sender_profile_id': sender['id'], 'target_profile_id': target['id'], 'kind': 'work',
                'source_anchor': dict(message), 'owner_origin': origin, 'issue': issue, 'sender_channel_id': sending['id'],
                'target_channel_id': receiving['id'], 'channel_bindings': _freeze_channels(sending, receiving), 'segments': segments, 'delivery': 'pending', 'acceptance': 'awaiting_receiver',
                'received_parts': {}, 'task_request_id': None, 'created_at': _now(), 'whole_project_complete': False}
            state['handoffs'][key] = result
        elif action == 'delegate_issue':
            if principal is None or principal['role'] != 'project_lead' or set(details) != {'parent_handoff_id', 'target_profile_id', 'issue_url'}:
                raise ManagementError('forbidden', 'Only the accepted project lead delegates a clear child Issue.')
            parent = state['handoffs'].get(details['parent_handoff_id'])
            target = data['profiles'].get(details['target_profile_id'])
            if not parent or parent['acceptance'] != 'accepted' or parent['target_profile_id'] != principal['id'] or not target or target['role'] != 'subproject_lead' or target['parent_profile_id'] != principal['id']:
                raise ManagementError('forbidden', 'The parent goal and explicitly registered child must belong to this lead.')
            result = _new_handoff(manager, data, principal, target, details['issue_url'], parent['owner_origin'], parent['received_anchor'], parent['id'])
        elif action in {'report_summary', 'report_progress'}:
            original = state['handoffs'].get(details.get('handoff_id'))
            if set(details) != {'handoff_id'} or principal is None or not original or original['target_profile_id'] != principal['id'] or original['kind'] != 'work' or original['acceptance'] != 'accepted':
                raise ManagementError('forbidden', 'Only the independently accepted assigned role reports its original work.')
            _handoff_channels(data, original)
            if action == 'report_summary' and principal['role'] != 'project_lead':
                raise ManagementError('forbidden', 'The project lead owns its project summary.')
            target_id = principal.get('parent_profile_id') if principal['role'] == 'subproject_lead' else original['sender_profile_id']
            task = data['requests'].get(original.get('task_request_id'), {})
            received = [state['handoffs'][i] for i in original.get('received_results', []) if i in state['handoffs'] and state['handoffs'][i]['acceptance'] == 'accepted']
            text = '项目进展汇总；子交付待集成，项目整体仍待全局验证。\nIssue：' + original['issue']['url'] + '\n负责人：' + principal['id'] + '\n当前 Issue 执行：' + task.get('execution', 'unverified') + '\n本 Issue 交付：' + task.get('task_delivery', 'pending')
            for child_result in received:
                text += '\n已独立接收子结果：' + child_result['issue']['url'] + ' · ' + child_result['id'] + '\n' + ''.join(part['text'].split(']\n', 1)[1] for part in child_result['segments'])
            result = _result_handoff(manager, data, principal, data['profiles'][target_id], original, text, 'summary' if action == 'report_summary' else 'progress')
            result['received_result_ids'] = [h['id'] for h in received]
        elif action == 'publish_owner_summary':
            original = state['handoffs'].get(details.get('handoff_id'))
            if set(details) != {'handoff_id'} or principal is None or principal['role'] != 'steward' or not original or original['target_profile_id'] != principal['id'] or original['kind'] not in {'summary', 'result', 'progress'} or original['acceptance'] != 'accepted':
                raise ManagementError('forbidden', 'Only the steward returns an independently received project update to its original Owner goal.')
            _handoff_channels(data, original)
            anchor = original['owner_origin']['source_anchor']
            entries = [c for c in state['channels'].values() if c['profile_id'] == principal['id'] and c['group_kind'] == 'entry' and all(c.get(k) == anchor.get(k) for k in ('app_id', 'chat_id', 'recipient_open_id', 'transport_tenant_key', 'recipient_tenant_key')) and c['owner_open_id'] == anchor['sender_open_id'] and c['owner_tenant_key'] == anchor['tenant_key']]
            if len(entries) != 1:
                raise ManagementError('binding_conflict', 'The original Owner entry anchor must match the registered steward entry namespace.')
            entry = _channel(data, entries[0]['id'])
            key = hashlib.sha256(json.dumps(['owner-summary', original['id']]).encode()).hexdigest()
            if key in state['handoffs']:
                return {**state['handoffs'][key], 'duplicate': True}
            text = '总管回传原本人目标；整体完成仍待全局验证。\n' + '\n'.join(part['text'].split(']\n', 1)[1] for part in original['segments'])
            chunks = [text[n:n + 1400] for n in range(0, len(text), 1400)]
            result = {'id': key, 'kind': 'owner_summary', 'sender_profile_id': principal['id'], 'target_profile_id': principal['id'],
                'sender_channel_id': entry['id'], 'target_channel_id': entry['id'], 'owner_origin': original['owner_origin'], 'source_anchor': anchor,
                'issue': original['issue'], 'original_handoff_id': original['id'], 'whole_project_complete': False, 'channel_bindings': _freeze_channels(entry, entry),
                'segments': [{'number': n + 1, 'uuid': str(uuid.uuid4()), 'status': 'pending', 'text': chunk, 'attempts': []} for n, chunk in enumerate(chunks)],
                'delivery': 'pending', 'acceptance': 'owner_notification', 'created_at': _now()}
            state['handoffs'][key] = result
        elif action == 'report_result':
            if set(details) not in ({'handoff_id'}, {'request_id'}) or principal is None:
                raise ManagementError('forbidden', 'Only an assigned role reports its original Issue delivery.')
            if 'request_id' in details:
                task = data['requests'].get(details['request_id'])
                if principal['role'] != 'subproject_lead' or not task or task.get('actor_provenance', {}).get('owner_origin', {}).get('subject') != manager.owner_identity_ref or any(task['accepted_responsibility'].get(k) != principal.get(k) for k in task['accepted_responsibility']):
                    raise ManagementError('forbidden', 'Direct child results require the original Owner request and unchanged responsible binding.')
                direct = state['handoffs'].get(task.get('parent_sync_handoff_id'), {})
                original = {'id': task['id'], 'received_anchor': task['source_anchor'], 'owner_origin': task['actor_provenance']['owner_origin'],
                    'issue': task['accepted_scope'], 'task_request_id': task['id'], 'target_profile_id': task['profile_id'],
                    'acceptance': 'accepted', 'parent_handoff_id': direct.get('parent_handoff_id')}
            else:
                original = state['handoffs'].get(details['handoff_id'])
                task = data['requests'].get((original or {}).get('task_request_id'))
                if original:
                    _handoff_channels(data, original)
            if not original or original['target_profile_id'] != principal['id'] or original['acceptance'] != 'accepted' or not task or task.get('task_delivery') != 'delivered' or task['profile_id'] != principal['id']:
                raise ManagementError('evidence_missing', 'The original assigned Issue has not been delivered with its own evidence.')
            target_id = principal.get('parent_profile_id') if principal['role'] == 'subproject_lead' else original['sender_profile_id']
            target = data['profiles'].get(target_id)
            if not target:
                raise ManagementError('binding_conflict', 'The registered result recipient is unavailable.')
            status = '子 Issue 已交付，待集成；全局验证尚未核对。' if principal['role'] == 'subproject_lead' else 'mono Issue 已交付；项目整体状态仍待全局验证。'
            text = status + '\nIssue：' + task['accepted_scope']['url'] + '\n交付版本：' + str(task.get('delivery_evidence', {}).get('source_commit')) + '\nPR：' + task.get('pr_status', 'none') + '\n原验收与测试证据：' + json.dumps(task.get('delivery_evidence', {}).get('criteria', []), ensure_ascii=False)
            result = _result_handoff(manager, data, principal, target, original, text)
        elif action in {'claim_delivery', 'record_delivery'}:
            allowed = {'handoff_id'} if action == 'claim_delivery' else {'handoff_id', 'uuid', 'receipt'}
            if set(details) != allowed:
                raise ManagementError('invalid_change', 'Only original handoff delivery fields are accepted.')
            handoff = state['handoffs'].get(details['handoff_id'])
            if not handoff or principal is not None and principal['id'] != handoff['sender_profile_id']:
                raise ManagementError('forbidden', 'Only the assigned sending role can deliver its handoff.')
            sending, target = _handoff_channels(data, handoff)
            if action == 'claim_delivery':
                segment = next((s for s in handoff['segments'] if s['status'] != 'delivered'), None)
                if segment is None or segment['status'] != 'pending':
                    return None
                segment['status'] = 'sending'
                segment['attempts'].append({'status': 'sending', 'claimed_at': _now()})
                manager._inflight.add(segment['uuid'])
                if handoff['kind'] == 'owner_summary':
                    result = {**segment, 'path': 'reply', 'chat_id': sending['chat_id'], 'reply_to': handoff['source_anchor']['message_id'], 'mention_open_id': sending['owner_open_id'], 'sender_binding': sending}
                else:
                    mentions = [b for b in sending['bot_sources'] if b['profile_id'] == handoff['target_profile_id']]
                    result = {**segment, 'path': 'create', 'chat_id': target['chat_id'], 'mention_open_id': mentions[0]['open_id'], 'sender_binding': sending}
            else:
                segment = next((s for s in handoff['segments'] if s['uuid'] == details['uuid']), None)
                receipt = details['receipt']
                if not segment or segment['status'] != 'sending' or not isinstance(receipt, dict) or receipt.get('status') not in {'delivered', 'failed', 'unknown'}:
                    raise ManagementError('binding_conflict', 'There is no original in-flight role delivery.')
                receipt = {k: receipt.get(k) for k in ('status', 'message_id', 'chat_id', 'root_id', 'parent_id', 'thread_id', 'code')}
                if receipt['status'] == 'delivered' and (not isinstance(receipt['message_id'], str) or not receipt['message_id'] or receipt['chat_id'] != target['chat_id']):
                    receipt['status'] = 'unknown'
                segment['status'] = receipt['status']
                segment['attempts'][-1].update(receipt)
                manager._inflight.discard(segment['uuid'])
                result = handoff
            statuses = [s['status'] for s in handoff['segments']]
            handoff['delivery'] = next((s for s in ('unknown', 'sending', 'failed', 'pending') if s in statuses), 'delivered')
        elif action in {'publish_ack', 'claim_ack', 'record_ack'}:
            expected = {'handoff_id', 'uuid', 'receipt'} if action == 'record_ack' else {'handoff_id'}
            handoff = state['handoffs'].get(details.get('handoff_id'))
            if set(details) != expected or identity.source != 'native-collaboration-ingress' or principal is None or not handoff or handoff['target_profile_id'] != principal['id'] or handoff['acceptance'] != 'accepted' or not handoff.get('task_request_id'):
                raise ManagementError('forbidden', 'Only the independently accepted native receiver publishes task confirmation.')
            _handoff_channels(data, handoff)
            request_id = handoff['task_request_id']
            if action == 'publish_ack':
                return manager.publish_request_message(identity, request_id, 'confirmation', '已受理原本人目标：' + handoff['issue']['title'] + '\nIssue：' + handoff['issue']['url'] + '\n负责人：' + principal['id'] + '；等待明确基线与执行核验。')
            if action == 'claim_ack':
                return manager.claim_delivery(identity, request_id)
            return manager.record_delivery(identity, request_id, details['uuid'], details['receipt'])
        elif action == 'ingest':
            if identity.source != 'native-collaboration-ingress' or principal is None or set(details) != {'channel_id', 'source_anchor', 'text'}:
                raise ManagementError('forbidden', 'Independent reception requires the separate native ingress credential.')
            receiving = _channel(data, details['channel_id'])
            message = details['source_anchor']
            _message_anchor(message)
            marker = re.match(r'^\[hermes-role-(?P<kind>work|result|summary|progress) (?P<id>[a-f0-9]{64}) (?P<part>\d+)/(?P<total>\d+)\]\n', details['text'])
            if not marker:
                raise ManagementError('invalid_change', 'Only an explicit original work marker may create delegated work.')
            handoff = state['handoffs'].get(marker.group('id'))
            if not handoff or principal['id'] != receiving['profile_id'] or receiving['id'] != handoff['target_channel_id'] or principal['id'] != handoff['target_profile_id']:
                raise ManagementError('forbidden', 'The native receiver is outside this handoff responsibility.')
            _handoff_channels(data, handoff)
            if any(receiving.get(k) != message.get(k) for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')):
                raise ManagementError('binding_conflict', 'The original receiver namespace does not match the registered destination.')
            senders = [b for b in receiving['bot_sources'] if b['profile_id'] == handoff['sender_profile_id'] and b['open_id'] == message['sender_open_id'] and b['tenant_key'] == message['tenant_key']]
            number = int(marker.group('part'))
            if handoff['kind'] != marker.group('kind') or len(senders) != 1 or int(marker.group('total')) != len(handoff['segments']) or not 1 <= number <= len(handoff['segments']) or handoff['segments'][number - 1]['text'] != details['text']:
                raise ManagementError('binding_conflict', 'The source bot or full original handoff content could not be matched.')
            if handoff['acceptance'] == 'accepted':
                return {**handoff, 'duplicate': True}
            if str(number) in handoff['received_parts'] and handoff['acceptance'] == 'awaiting_receiver':
                return {**handoff, 'duplicate': True}
            handoff['received_parts'].setdefault(str(number), dict(message))
            result = handoff
            if len(handoff['received_parts']) == len(handoff['segments']):
                handoff['received_anchor'] = handoff['received_parts']['1']
                handoff['acceptance'] = 'source_received'
                with manager._db:
                    manager._save(version, data)
                if handoff['kind'] == 'work' and principal['capability'] == 'development':
                    task = manager.accept_request(identity, principal['project_id'], principal['id'], handoff['received_anchor'], handoff['issue'], delegation_id=handoff['id'])['request']
                else:
                    task = None
                version, data = manager._load()
                handoff = _state(data)['handoffs'][handoff['id']]
                handoff.update(acceptance='accepted', accepted_at=_now(), task_request_id=task['id'] if task else None)
                if handoff['kind'] == 'result':
                    parent = _state(data)['handoffs'].get(handoff.get('parent_handoff_id'))
                    if parent:
                        parent.setdefault('received_results', []).append(handoff['id'])
                        parent['integration_status'] = 'awaiting_integration'
                        parent['whole_project_complete'] = False
                result = handoff
        else:
            raise ManagementError('unsupported', 'This role operation is not supported.')
        with manager._db:
            manager._save(version, data)
        return result
