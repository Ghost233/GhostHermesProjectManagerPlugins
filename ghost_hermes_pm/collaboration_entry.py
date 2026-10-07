"""Original native messages select roles; text never supplies an actor identity."""
from dataclasses import dataclass
import json
import re

from .manager import ManagementError, VerifiedIdentity


@dataclass(frozen=True)
class PreparedRoleMessage:
    event: object
    adapter: object
    transport: object
    binding: dict
    envelope: dict
    command: str
    role_kind: str
    role_details: dict
    issue_url: str | None = None


class CollaborationEntry:
    def __init__(self, intake, identity_ref, client=None):
        self.intake, self.identity_ref, self.client = intake, identity_ref, client

    def call(self, action, details, receiver=None):
        manager = self.intake.manager()
        if manager is not None:
            subject = receiver['profile_binding']['identity_ref'] if receiver else self.identity_ref
            return manager.collaborate(VerifiedIdentity(subject, 'native-collaboration-ingress'), action, details)
        client = self.client() if self.client else None
        if client is None:
            raise ManagementError('unavailable', 'The original shared management authority is unavailable.')
        return client.collaborate(action, details)

    def prepare(self, event, adapter):
        if not self.identity_ref and not self.client or self.intake.closed or self.intake.settings.get('enabled') is not True or not self.intake.settings.get('verification_ref'):
            return None
        try:
            raw, source = event.raw_message, event.source
            header, sender, message = raw.header, raw.event.sender, raw.event.message
            text = json.loads(message.content)['text']
            if header.event_type != 'im.message.receive_v1' or message.chat_type != 'group' or message.message_type != 'text' or not isinstance(text, str):
                return None
            if not (re.search(r'\[hermes-role-', text) or re.search(r'项目\s+\S+\s+https://github\.com/', text)):
                return None
            if getattr(source.platform, 'value', source.platform) not in {'feishu', 'hermes_feishu_pm'} or source.chat_id != message.chat_id or source.message_id != message.message_id or event.message_id != message.message_id or source.user_id != (getattr(sender.sender_id, 'user_id', None) or sender.sender_id.open_id) or getattr(sender.sender_id, 'union_id', None) and getattr(source, 'user_id_alt', None) != sender.sender_id.union_id:
                return None
            routes = self.call('read_routes', {})
            channels = [c for c in routes['channels'] if c['app_id'] == header.app_id and c['transport_tenant_key'] == header.tenant_key and c['chat_id'] == message.chat_id]
            if len(channels) != 1:
                return None
            channel = channels[0]
            transport = next((t for a, t in self.intake.transports if a is adapter), None)
            if transport is None:
                return None
            mentions = [m for m in message.mentions or [] if m.mentioned_type == 'bot' and m.tenant_key == channel['recipient_tenant_key'] and m.id.open_id == channel['recipient_open_id'] and m.key in text]
            if len(mentions) != 1:
                return None
            text = text.replace(mentions[0].key, '', 1).lstrip()
            envelope = {'app_id': header.app_id, 'transport_tenant_key': header.tenant_key,
                'tenant_key': sender.tenant_key, 'recipient_tenant_key': channel['recipient_tenant_key'],
                'recipient_open_id': channel['recipient_open_id'], 'chat_id': message.chat_id,
                'sender_open_id': sender.sender_id.open_id, 'message_id': message.message_id,
                'parent_id': getattr(message, 'parent_id', None) or None, 'root_id': getattr(message, 'root_id', None) or None,
                'thread_id': getattr(message, 'thread_id', None) or None}
            if sender.sender_type == 'user' and source.is_bot is False:
                text = text.strip()
                goal = re.fullmatch(r'项目\s+(\S+)\s+(https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*)', text)
                if not goal or channel['group_kind'] != 'entry' or channel['owner_open_id'] != sender.sender_id.open_id or channel['owner_tenant_key'] != sender.tenant_key:
                    return None
                targets = [p for p in routes['profiles'] if p['role'] == 'project_lead' and p['project_id'] == goal.group(1)]
                if len(targets) != 1:
                    return None
                details = {'sender_profile_id': channel['profile_id'], 'target_profile_id': targets[0]['id'], 'source_anchor': envelope, 'issue_url': goal.group(2)}
                return PreparedRoleMessage(event, adapter, transport, channel, envelope, text, 'owner_goal', details)
            if sender.sender_type not in {'bot', 'app'} or source.is_bot is not True or not re.match(r'^\[hermes-role-(?:work|result|summary|progress) [a-f0-9]{64} \d+/\d+\]\n', text):
                return None
            native_ids = {getattr(sender.sender_id, k, None) for k in ('user_id', 'open_id', 'union_id')} - {None, ''}
            senders = [b for b in channel['bot_sources'] if b['open_id'] == sender.sender_id.open_id and b['tenant_key'] == sender.tenant_key and native_ids & set(b['native_ids'])]
            if len(senders) != 1:
                return None
            return PreparedRoleMessage(event, adapter, transport, channel, envelope, text, 'work',
                {'channel_id': channel['id'], 'source_anchor': envelope, 'text': text})
        except (AttributeError, KeyError, TypeError, ValueError, ManagementError):
            return None

    def already_processed(self, prepared):
        routes = self.call('read_routes', {})
        return any(h['source_anchor'] == prepared.envelope if prepared.role_kind == 'owner_goal' else any(a == prepared.envelope for a in h.get('received_parts', {}).values()) for h in routes['handoffs'])

    def bot_source_in_scope(self, sender, message, app_id):
        try:
            text = json.loads(message.content)['text']
            if not re.search(r'\[hermes-role-(?:work|result|summary|progress) ', text):
                return False
            routes = self.call('read_routes', {})
            return any(c['app_id'] == app_id and c['chat_id'] == message.chat_id and any(b['tenant_key'] == sender.tenant_key and b['open_id'] == sender.sender_id.open_id for b in c['bot_sources']) for c in routes['channels'])
        except (AttributeError, ValueError, KeyError, ManagementError):
            return False

    def in_scope(self, prepared, runtime_profile):
        return prepared.binding['profile_binding']['native_profile'] == runtime_profile

    async def deliver(self, handoff_id, generation):
        routes = self.call('read_routes', {})
        handoff = next((h for h in routes['handoffs'] if h['id'] == handoff_id), None)
        if handoff is None:
            raise ManagementError('forbidden', 'The role handoff is outside this publication scope.')
        sending = next(c for c in routes['channels'] if c['id'] == handoff['sender_channel_id'])
        while True:
            self.intake.require_active(generation)
            packet = self.call('claim_delivery', {'handoff_id': handoff_id}, sending)
            if packet is None:
                return
            binding = packet['sender_binding']
            matches = []
            for _, transport in tuple(self.intake.transports):
                try:
                    recipient = await transport.verify_identity(binding)
                    self.intake.require_active(generation)
                    if recipient and recipient.get('app_id') == binding['app_id'] and recipient.get('open_id') == binding['recipient_open_id']:
                        matches.append(transport)
                except Exception:
                    self.intake.require_active(generation)
            if len(matches) != 1:
                receipt = {'status': 'unknown'}
            else:
                try:
                    receipt = await matches[0].send(packet)
                    self.intake.require_active(generation)
                except Exception:
                    self.intake.require_active(generation)
                    receipt = {'status': 'unknown'}
            self.call('record_delivery', {'handoff_id': handoff_id, 'uuid': packet['uuid'], 'receipt': receipt}, sending)
            if receipt.get('status') != 'delivered':
                return

    async def process(self, prepared, generation):
        self.intake.require_active(generation)
        action = 'owner_project_goal' if prepared.role_kind == 'owner_goal' else 'ingest'
        with self.intake.lifecycle_lock:
            self.intake.require_active(generation)
            result = self.call(action, prepared.role_details, prepared.binding)
        if prepared.role_kind == 'owner_goal':
            await self.deliver(result['id'], generation)
        elif result.get('task_request_id'):
            self.call('publish_ack', {'handoff_id': result['id']}, prepared.binding)
            while True:
                self.intake.require_active(generation)
                segment = self.call('claim_ack', {'handoff_id': result['id']}, prepared.binding)
                if segment is None:
                    break
                receipt = await prepared.transport.send(segment)
                self.intake.require_active(generation)
                self.call('record_ack', {'handoff_id': result['id'], 'uuid': segment['uuid'], 'receipt': receipt}, prepared.binding)
                if receipt.get('status') != 'delivered':
                    break
        elif result.get('kind') in {'summary', 'progress', 'result'} and result.get('acceptance') == 'accepted' and prepared.binding['profile_binding']['role'] == 'steward':
            output = self.call('publish_owner_summary', {'handoff_id': result['id']}, prepared.binding)
            await self.deliver(output['id'], generation)
        return {'action': 'skip'}
