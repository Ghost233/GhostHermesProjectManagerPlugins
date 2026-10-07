"""Verified Feishu cold-path entry. Host compatibility stays at this boundary."""
import asyncio
import inspect
import json
import re
import threading
from dataclasses import dataclass

from .manager import VerifiedIdentity, ManagementError

OWNED_PLATFORM = 'hermes_feishu_pm'


@dataclass(frozen=True)
class PreparedMessage:
    event: object
    adapter: object
    transport: object
    binding: dict
    envelope: dict
    command: str
    issue_url: str | None


class FeishuEntry:
    def __init__(self, manager, owner, settings, issue_reader, secret_values=()):
        self.manager = manager
        self.owner = owner
        self.settings = settings
        self.issue_reader = issue_reader
        self.secret_values = secret_values
        self.transports = []
        self.lock = asyncio.Lock()
        self.closed = False
        self.generation = 0
        self.lifecycle_lock = threading.RLock()

    def deactivate(self):
        with self.lifecycle_lock:
            if not self.closed:
                self.generation += 1
                self.closed = True
            self.transports.clear()

    def require_active(self, generation):
        with self.lifecycle_lock:
            if self.closed or generation != self.generation:
                raise ManagementError('unavailable', 'The intake lifecycle ended; no new action is authorized.')

    def attach_transport(self, adapter, transport):
        if self.closed:
            return
        if callable(getattr(transport, 'bind_lifecycle_guard', None)):
            generation = self.generation
            transport.bind_lifecycle_guard(lambda: self.require_active(generation))
        self.transports = [(a, t) for a, t in self.transports if a is not adapter]
        self.transports.append((adapter, transport))
        self.secret_values = tuple(dict.fromkeys((*self.secret_values, *getattr(transport, 'sensitive_values', ()))))

    def detach_transport(self, adapter):
        self.transports = [(a, t) for a, t in self.transports if a is not adapter]

    def prepare(self, event, adapter):
        """Inspect one original event without auth charges, writes or external requests."""
        if self.closed or self.settings.get('enabled') is not True or not self.settings.get('verification_ref'):
            return None
        from .knowledge_entry import prepare_knowledge_message
        knowledge = prepare_knowledge_message(self, event, adapter)
        if knowledge is not None:
            return knowledge
        try:
            source, header, raw = event.source, event.raw_message.header, event.raw_message.event
            if getattr(source.platform, 'value', source.platform) not in {'feishu', OWNED_PLATFORM} or source.is_bot is not False:
                return None
            sender, message = raw.sender, raw.message
            ids = sender.sender_id
            if header.event_type != 'im.message.receive_v1' or sender.sender_type != 'user' or message.chat_type != 'group' or message.message_type != 'text':
                return None
            if any(not isinstance(v, str) or not v for v in (header.app_id, header.tenant_key, sender.tenant_key, ids.open_id, message.chat_id, message.message_id)):
                return None
            if source.user_id != (getattr(ids, 'user_id', None) or ids.open_id) or source.chat_id != message.chat_id or event.message_id != message.message_id or source.message_id != message.message_id:
                return None
            if getattr(ids, 'union_id', None) and getattr(source, 'user_id_alt', None) != ids.union_id:
                return None
            bindings = [b for b in self.settings.get('bindings', []) if b.get('sender_tenant_key') == sender.tenant_key
                        and b.get('transport_tenant_key') == header.tenant_key and b.get('app_id') == header.app_id
                        and b.get('verification_ref') and b.get('chat_id') == message.chat_id and b.get('owner_open_id') == ids.open_id]
            if len(bindings) != 1:
                return None
            binding = bindings[0]
            owner_ids = binding.get('owner_native_ids')
            if owner_ids is not None and source.user_id not in owner_ids and getattr(source, 'user_id_alt', None) not in owner_ids:
                return None
            text = json.loads(message.content)['text']
            if not isinstance(text, str) or any(secret and secret in text for secret in self.secret_values):
                return None
            mentions = [m for m in (message.mentions or []) if m.id.open_id == binding['recipient_open_id']
                        and m.tenant_key == binding['recipient_tenant_key'] and m.mentioned_type == 'bot'
                        and isinstance(m.key, str) and m.key in text]
            for mention in mentions:
                text = text.replace(mention.key, '')
            command = text.strip()
            work = re.fullmatch(r'派发\s+(https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*)', command)
            if work and (len(mentions) != 1 or not work.group(1).startswith('https://github.com/' + binding['repository'] + '/issues/')):
                return None
            transport = next((t for a, t in self.transports if a is adapter), None)
            if transport is None:
                return None
            envelope = {'tenant_key': sender.tenant_key, 'recipient_open_id': binding['recipient_open_id'],
                        'app_id': binding['app_id'], 'recipient_tenant_key': binding['recipient_tenant_key'],
                        'transport_tenant_key': header.tenant_key, 'chat_id': message.chat_id,
                        'message_id': message.message_id, 'sender_open_id': ids.open_id,
                        'parent_id': getattr(message, 'parent_id', None) or getattr(message, 'upper_message_id', None),
                        'root_id': getattr(message, 'root_id', None), 'thread_id': getattr(message, 'thread_id', None)}
            if not work:
                manager = self.manager()
                human_reply = bool(re.match(r'^(回答|批准|拒绝)', command))
                if human_reply:
                    if manager is None:
                        return None
                    snapshot = manager.read_snapshot(VerifiedIdentity(self.owner, 'verified-feishu-owner-entry'))
                    entry_profile = next((p for p in snapshot['profiles'] if p['id'] == binding['profile_id']), None)
                    steward = entry_profile and entry_profile['role'] == 'steward' and entry_profile['project_id'] is None and binding['project_id'] is None
                    if not any(r.get('human_requests') and (steward or r['project_id'] == binding['project_id'] and r['profile_id'] == binding['profile_id']) for r in snapshot['requests']):
                        return None
                if not human_reply and (manager is None or not any(r['profile_id'] == binding['profile_id'] and all(r['source_anchor'].get(k) == envelope[k] for k in ('app_id', 'tenant_key', 'recipient_tenant_key', 'transport_tenant_key', 'recipient_open_id', 'chat_id', 'sender_open_id'))
                    for r in manager.read_snapshot(VerifiedIdentity(self.owner, 'verified-feishu-owner-entry'))['requests'])):
                    return None
            return PreparedMessage(event, adapter, transport, binding, envelope, command, work.group(1) if work else None)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def in_scope(self, prepared, runtime_profile):
        manager = self.manager()
        if manager is None:
            return False
        snapshot = manager.read_snapshot(VerifiedIdentity(self.owner, 'verified-feishu-owner-entry'))
        if getattr(prepared, 'knowledge_kind', None):
            return any(p['id'] == prepared.binding['profile_id'] and p['native_profile'] == runtime_profile for p in snapshot['profiles'])
        if re.match(r'^(回答|批准|拒绝)', prepared.command):
            return any(p['id'] == prepared.binding['profile_id'] and p['native_profile'] == runtime_profile and
                       (p['role'] == 'steward' and p['project_id'] is None and prepared.binding['project_id'] is None or
                        p['capability'] == 'development' and p['project_id'] == prepared.binding['project_id']) for p in snapshot['profiles'])
        return any(p['id'] == prepared.binding['profile_id'] and p['project_id'] == prepared.binding['project_id']
                   and p['native_profile'] == runtime_profile and p['capability'] == 'development'
                   for p in snapshot['profiles'])

    async def process_prepared(self, prepared, generation=None):
        """Business processing after the owned driver has irrevocably consumed this original."""
        if getattr(prepared, 'knowledge_kind', None):
            from .knowledge_entry import process_knowledge_message
            return await process_knowledge_message(self, prepared, self.generation if generation is None else generation)
        identity = VerifiedIdentity(self.owner, 'verified-feishu-owner-entry')
        binding, envelope, transport = prepared.binding, prepared.envelope, prepared.transport
        generation = self.generation if generation is None else generation
        self.require_active(generation)
        async with self.lock:
            self.require_active(generation)
            if not prepared.issue_url:
                return await self._associate(identity, binding, envelope, prepared.command, transport, generation)
            existing = next((r for r in self.manager().read_snapshot(identity)['requests'] if r['source_anchor'] == envelope), None)
            if existing:
                record = existing
            else:
                def read_if_active():
                    self.require_active(generation)
                    return self.issue_reader(prepared.issue_url)
                issue = await asyncio.to_thread(read_if_active)
                self.require_active(generation)
                if inspect.isawaitable(issue):
                    issue = await issue
                    self.require_active(generation)
                if issue.get('url') != prepared.issue_url or any(secret and secret in json.dumps(issue) for secret in self.secret_values):
                    from .manager import ManagementError
                    raise ManagementError('invalid_change', 'The Issue source or public scope could not be verified.')
                with self.lifecycle_lock:
                    self.require_active(generation)
                    record = self.manager().accept_request(identity, binding['project_id'], binding['profile_id'], envelope, issue)['request']
            scope = record['accepted_scope']
            self.manager().publish_request_message(identity, record['id'], 'confirmation',
                '已受理：项目 ' + record['project_id'] + ' · 负责人 ' + record['profile_id'] + '\n' + scope['url']
                + '\n范围：' + scope['title'] + '\nIssue 版本：' + scope['updated_at']
                + '\n等待执行能力核验与明确启动。受理与消息送达分别核对。')
            self.manager().publish_request_message(identity, record['id'], 'material', '已受理范围：\n' + scope['body'])
            self.require_active(generation)
            await self.deliver(identity, record['id'], transport, generation)
            return {'action': 'skip'}

    async def receive(self, event, gateway):
        """Legacy direct entry fixture; production intake is owned prebatch only."""
        if self.manager() is None:
            return None
        prepared = None
        generation = self.generation
        try:
            adapter = gateway._intake_adapter_for(event.source)
            prepared = self.prepare(event, adapter)
            if prepared is None or gateway._is_user_authorized_for_source(event.source) is not True:
                return None
            recipient = await prepared.transport.verify_identity(prepared.binding)
            self.require_active(generation)
            if not recipient or recipient.get('app_id') != prepared.binding['app_id'] or recipient.get('open_id') != prepared.binding['recipient_open_id']:
                return None
            if gateway._admit_bot_message_for_source(event.source) is not True:
                return None
            return await self.process_prepared(prepared, generation)
        except Exception:
            if self.closed or generation != self.generation:
                return None
            if prepared and any(r['source_anchor'] == prepared.envelope for r in self.manager().read_snapshot(
                VerifiedIdentity(self.owner, 'verified-feishu-owner-entry'))['requests']):
                return {'action': 'skip'}
            return None

    async def _associate(self, identity, binding, envelope, text, transport, generation):
        self.require_active(generation)
        retry = re.fullmatch(r'重试投递\s+([a-f0-9]{64})', text)
        if retry:
            record = next((r for r in self.manager().read_snapshot(identity)['requests'] if r['id'] == retry.group(1)), None)
            if not record or record['project_id'] != binding['project_id'] or record['profile_id'] != binding['profile_id'] or any(record['source_anchor'].get(k) != envelope.get(k) for k in ('app_id', 'tenant_key', 'recipient_open_id', 'chat_id', 'sender_open_id')):
                return None
            self.manager().retry_delivery(identity, record['id'])
            await self.deliver(identity, record['id'], transport, generation)
            return {'action': 'skip'}
        if re.match(r'^(回答|批准|拒绝)', text):
            try:
                def answer_if_active():
                    with self.lifecycle_lock:
                        self.require_active(generation)
                        return self.manager().associate_human_reply(identity, binding['project_id'], binding['profile_id'], envelope, text)
                result = await asyncio.to_thread(answer_if_active)
                if result['status'] == 'answered':
                    original = next(r['source_anchor'] for r in self.manager().read_snapshot(identity)['requests'] if r['id'] == result['request_id'])
                    if all(original.get(k) == envelope.get(k) for k in ('app_id', 'recipient_open_id', 'recipient_tenant_key', 'transport_tenant_key')):
                        await self.deliver(identity, result['request_id'], transport, generation)
                    reply = result['human_request']['reply']
                    feedback = '人工答复已收到；送回：' + reply['sent'] + '；已处理与执行结果请核对原请求。'
                else:
                    feedback = '存在多个人工请求，请引用具体人工请求 ID 后回答。'
                    if result['delivery'] == 'pending':
                        self.manager().record_clarification_delivery(identity, result['id'], {'status': 'unknown'})
            except ManagementError as exc:
                feedback = '人工答复未执行：' + str(exc)
                result = None
            self.require_active(generation)
            from .questions import claim_reply_feedback, record_reply_feedback
            segment = claim_reply_feedback(self.manager(), identity, envelope, feedback)
            if segment is not None:
                try:
                    receipt = await transport.send(segment)
                    self.require_active(generation)
                except Exception:
                    self.require_active(generation)
                    receipt = {'status': 'unknown'}
                record_reply_feedback(self.manager(), identity, segment['id'], receipt)
                if result and result['status'] == 'needs_clarification':
                    self.manager().record_clarification_delivery(identity, result['id'], receipt)
            return {'action': 'skip'}
        result = self.manager().associate_message(identity, binding['project_id'], binding['profile_id'], envelope, text)
        if result['status'] == 'unassociated':
            return None
        if result['status'] == 'needs_clarification':
            if result['delivery'] == 'pending':
                # Record intent before calling the transport; interrupted delivery remains unknown.
                self.manager().record_clarification_delivery(identity, result['id'], {'status': 'unknown'})
                segment = {'uuid': result['uuid'], 'text': '存在多个候选请求，请引用任务起始消息后补充输入。',
                           'chat_id': envelope['chat_id'], 'reply_to': envelope['message_id'],
                           'thread_id': envelope['thread_id'], 'mention_open_id': envelope['sender_open_id']}
                try:
                    receipt = await transport.send(segment)
                    self.require_active(generation)
                except Exception:
                    self.require_active(generation)
                    receipt = {'status': 'unknown'}
                self.manager().record_clarification_delivery(identity, result['id'], receipt)
        elif result['status'] == 'associated':
            operations = {'执行': 'start_task', '核对执行': 'refresh_task', '核验执行能力': 'verify_task_execution', '核对Issue来源': 'refresh_task_source'}
            operation = operations.get(text)
            control = None
            preparation = re.fullmatch(r'确认基线[：:]\s*(\S+)\s+([a-f0-9]{40}|unborn)(?:\s+依赖[：:]([a-f0-9,]+))?(?:\s+保留[：:]([a-f0-9]{64}))?', text)
            explicit = re.fullmatch(r'(追加|继续)[：:]\s*(.*)', text, re.DOTALL)
            if text in {'停止', '结束当前任务'}:
                control = ('stop', None)
            elif text in {'明确继续', '继续原工作'}:
                control = ('continue', 'Continue the original accepted Issue within the existing scope.')
            elif explicit:
                control = ('append' if explicit.group(1) == '追加' else 'continue', explicit.group(2))
            if operation or control or preparation:
                def call_if_active():
                    with self.lifecycle_lock:
                        self.require_active(generation)
                        if preparation:
                            record = next(r for r in self.manager().read_snapshot(identity)['requests'] if r['id'] == result['request_id'])
                            return self.manager().prepare_task(identity, result['request_id'], {'branch': preparation.group(1),
                                'commit': None if preparation.group(2) == 'unborn' else preparation.group(2), 'dependencies': preparation.group(3).split(',') if preparation.group(3) else [],
                                'issue_updated_at': record['accepted_scope']['updated_at'], 'workspace_digest': preparation.group(4)})
                        if control:
                            record = next(r for r in self.manager().read_snapshot(identity)['requests'] if r['id'] == result['request_id'])
                            previous = next((c for c in record.get('controls', []) if c['id'] == result['id']), None)
                            expected = previous['expected_turn_id'] if previous else record.get('session', {}).get('turn_id')
                            return self.manager().control_task(identity, result['request_id'], control[0], result['id'], control[1], expected)
                        return getattr(self.manager(), operation)(identity, result['request_id'])
                try:
                    outcome = await asyncio.to_thread(call_if_active)
                    self.require_active(generation)
                    if outcome.get('status') == 'blocked':
                        self.manager().publish_request_message(identity, result['request_id'], 'progress',
                            '执行能力受阻：' + outcome['reason'])
                except ManagementError as exc:
                    self.require_active(generation)
                    self.manager().publish_request_message(identity, result['request_id'], 'progress',
                        '执行操作待核对：' + str(exc))
            else:
                self.manager().publish_request_message(identity, result['request_id'], 'progress',
                    '已关联输入 ' + envelope['message_id'] + '。已受理范围保持原 Issue 版本。')
            await self.deliver(identity, result['request_id'], transport, generation)
        return {'action': 'skip'}

    async def deliver(self, identity, request_id, transport, generation=None):
        generation = self.generation if generation is None else generation
        while True:
            self.require_active(generation)
            segment = self.manager().claim_delivery(identity, request_id)
            if segment is None:
                break
            try:
                receipt = await transport.send(segment)
                self.require_active(generation)
            except Exception:
                self.require_active(generation)
                receipt = {'status': 'unknown'}
            self.manager().record_delivery(identity, request_id, segment['uuid'], receipt)
            if receipt.get('status') != 'delivered':
                break

    async def deliver_knowledge(self, identity, query_id, transport, generation=None):
        from .knowledge_entry import deliver_knowledge
        return await deliver_knowledge(self, identity, query_id, transport, generation)
