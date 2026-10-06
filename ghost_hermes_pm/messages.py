"""Verified Feishu cold-path entry. Host compatibility stays at this boundary."""
import asyncio
import inspect
import json
import re

from .manager import ManagementError, VerifiedIdentity


class FeishuEntry:
    def __init__(self, manager, owner, settings, issue_reader, secret_values=()):
        self.manager = manager
        self.owner = owner
        self.settings = settings
        self.issue_reader = issue_reader
        self.secret_values = secret_values
        self.transports = []
        self.lock = asyncio.Lock()

    def attach_transport(self, adapter, transport):
        self.transports = [(a, t) for a, t in self.transports if a is not adapter]
        self.transports.append((adapter, transport))

    async def receive(self, event, gateway):
        if self.settings.get('enabled') is not True or not self.settings.get('verification_ref') or self.manager() is None:
            return None
        try:
            source = event.source
            if getattr(source.platform, 'value', source.platform) != 'feishu' or source.is_bot:
                return None
            raw = event.raw_message.event
            sender, message = raw.sender, raw.message
            ids = sender.sender_id
            if sender.sender_type != 'user' or message.chat_type != 'group' or message.message_type != 'text':
                return None
            # TEXT debounce retains the first raw event and the last normalized ID.
            # Fail closed instead of assigning the merged text to an invented anchor.
            if source.user_id != (getattr(ids, 'user_id', None) or ids.open_id) or source.chat_id != message.chat_id or event.message_id != message.message_id or source.message_id != message.message_id:
                return None
            if getattr(ids, 'union_id', None) and getattr(source, 'user_id_alt', None) != ids.union_id:
                return None
            bindings = [b for b in self.settings.get('bindings', []) if b.get('tenant_key') == sender.tenant_key
                        and b.get('chat_id') == message.chat_id and b.get('owner_open_id') == ids.open_id]
            if len(bindings) != 1:
                return None
            binding = bindings[0]
            text = json.loads(message.content)['text']
            if not isinstance(text, str) or any(secret and secret in text for secret in self.secret_values):
                return None
            mentions = [m for m in (message.mentions or []) if m.id.open_id == binding['recipient_open_id']
                        and m.tenant_key == binding['tenant_key'] and m.mentioned_type == 'bot'
                        and isinstance(m.key, str) and m.key in text]
            command = text
            for mention in mentions:
                command = command.replace(mention.key, '')
            command = command.strip()
            work = re.fullmatch(r'派发\s+(https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*)', command)
            if work and len(mentions) != 1:
                return None
            adapter = gateway._intake_adapter_for(source)
            transport = next((t for a, t in self.transports if a is adapter), None)
            if transport is None or gateway._is_user_authorized_for_source(source) is not True:
                return None
            # skip bypasses native authorization; preserve its bot admission too.
            if not getattr(event, '_bot_loop_admitted', False) and gateway._admit_bot_message_for_source(source) is not True:
                return None
            recipient = await transport.verify_identity(binding)
            if not recipient or recipient.get('app_id') != binding['app_id'] or recipient.get('open_id') != binding['recipient_open_id']:
                return None
            envelope = {'tenant_key': sender.tenant_key, 'recipient_open_id': recipient['open_id'],
                        'chat_id': message.chat_id, 'message_id': message.message_id, 'sender_open_id': ids.open_id,
                        'parent_id': getattr(message, 'parent_id', None) or getattr(message, 'upper_message_id', None),
                        'root_id': getattr(message, 'root_id', None), 'thread_id': getattr(message, 'thread_id', None)}
            identity = VerifiedIdentity(self.owner, 'verified-feishu-owner-entry')
            async with self.lock:
                if not work:
                    return await self._associate(identity, binding, envelope, command, transport)
                url = work.group(1)
                if not url.startswith('https://github.com/' + binding['repository'] + '/issues/'):
                    return None
                snapshot = self.manager().read_snapshot(identity)
                existing = next((r for r in snapshot['requests'] if r['source_anchor'] == envelope), None)
                if existing:
                    record = existing
                else:
                    issue = await asyncio.to_thread(self.issue_reader, url)
                    if inspect.isawaitable(issue):
                        issue = await issue
                    if any(secret and secret in json.dumps(issue) for secret in self.secret_values):
                        return None
                    if issue.get('url') != url:
                        return None
                    record = self.manager().accept_request(identity, binding['project_id'], binding['profile_id'], envelope, issue)['request']
                scope = record['accepted_scope']
                self.manager().publish_request_message(identity, record['id'], 'confirmation',
                    '已受理：项目 ' + record['project_id'] + ' · 负责人 ' + record['profile_id'] + '\n' + scope['url']
                    + '\n范围：' + scope['title'] + '\nIssue 版本：' + scope['updated_at']
                    + '\n等待执行：Codex 执行尚未启用。受理与消息送达分别核对。')
                self.manager().publish_request_message(identity, record['id'], 'material', '已受理范围：\n' + scope['body'])
                await self.deliver(identity, record['id'], transport)
                return {'action': 'skip'}
        except Exception:
            # A missing/broken pinned host contract never grants admission or sends.
            return None

    async def _associate(self, identity, binding, envelope, text, transport):
        return None

    async def deliver(self, identity, request_id, transport):
        while segment := self.manager().claim_delivery(identity, request_id):
            try:
                receipt = await transport.send(segment)
            except Exception:
                receipt = {'status': 'unknown'}
            self.manager().record_delivery(identity, request_id, segment['uuid'], receipt)
            if receipt.get('status') != 'delivered':
                break
