"""Exact registered Wiki exchange on original Feishu SDK events."""
import asyncio
from dataclasses import dataclass
import json
import re

from .manager import ManagementError, VerifiedIdentity
from .messages import PreparedMessage, OWNED_PLATFORM
from .knowledge import NAMESPACE


@dataclass(frozen=True)
class PreparedKnowledgeMessage(PreparedMessage):
    knowledge_kind: str
    query_id: str
    knowledge_binding_id: str
    sender_identity: VerifiedIdentity


def _content(message):
    body = json.loads(message.content)
    if message.message_type == 'text':
        return body['text'], set()
    if message.message_type != 'post':
        raise ValueError('Only actual text/post source exchanges are supported.')
    post = body.get('zh_cn', body)
    text, ats = [], set()
    for row in post['content']:
        for node in row:
            if node.get('tag') == 'at':
                ats.add(node['user_id'])
            elif node.get('tag') == 'text':
                text.append(node['text'])
            elif node.get('tag') == 'a':
                text.append(node.get('text', '') + ' ' + node['href'])
    return ''.join(text).strip(), ats


def prepare_knowledge_message(entry, event, adapter):
    if entry.closed or entry.settings.get('enabled') is not True or not entry.settings.get('verification_ref') or entry.manager() is None:
        return None
    try:
        source, raw = event.source, event.raw_message
        sender, message, header = raw.event.sender, raw.event.message, raw.header
        if getattr(source.platform, 'value', source.platform) not in {'feishu', OWNED_PLATFORM} or header.event_type != 'im.message.receive_v1' or message.chat_type != 'group' or source.is_bot is not True or sender.sender_type not in {'bot', 'app'}:
            return None
        ids = sender.sender_id
        if source.user_id != (getattr(ids, 'user_id', None) or ids.open_id) or source.chat_id != message.chat_id or event.message_id != message.message_id or source.message_id != message.message_id:
            return None
        text, ats = _content(message)
        query_match = re.match(r'^资料查询\s+([A-Za-z0-9_.:-]{1,256})(?:\s|$)', text)
        result_match = re.match(r'^资料结果\s+([A-Za-z0-9_.:-]{1,256})\s+([a-f0-9]{64})(?:\s|$)', text)
        if not query_match and not result_match:
            return None
        query_id = (query_match or result_match).group(1)
        snapshot = entry.manager().read_snapshot(VerifiedIdentity(entry.owner, 'trusted-knowledge-policy'))
        query = next((q for q in snapshot['knowledge_queries'] if q['id'] == query_id), None)
        grant = next((s for s in snapshot['knowledge_sources'] if query and s['id'] == query['source_id']), None)
        if not grant:
            return None
        kind = 'query' if query_match else 'result'
        matches = [b for b in grant['wiki_bindings'] if b['kind'] == kind and b['channel_id'] == query['channel_id']
            and b['app_id'] == header.app_id and b['transport_tenant_key'] == header.tenant_key and b['chat_id'] == message.chat_id
            and b['sender_tenant_key'] == sender.tenant_key and b['sender_open_id'] == ids.open_id
            and source.user_id in b['sender_native_ids']]
        if len(matches) != 1:
            return None
        binding = matches[0]
        peers = [p for p in entry.settings.get('registered_bots', []) if p.get('profile_id') == binding['sender_profile_id']
            and p.get('identity_ref') == binding['sender_identity_ref'] and p.get('app_id') == header.app_id
            and p.get('tenant_key') == sender.tenant_key and p.get('open_id') == ids.open_id
            and source.user_id in p.get('native_ids', [])]
        profile = next((p for p in snapshot['profiles'] if p['id'] == binding['sender_profile_id']), None)
        if len(peers) != 1 or not profile or profile['identity_ref'] != binding['sender_identity_ref']:
            return None
        mentions = [m for m in (message.mentions or []) if m.id.open_id == binding['recipient_open_id']
            and m.tenant_key == binding['recipient_tenant_key'] and m.mentioned_type == 'bot'
            and (m.key in text or m.id.open_id in ats)]
        if len(mentions) != 1:
            return None
        if result_match and result_match.group(2) != query.get('result_version'):
            return None
        transport = next((t for a, t in entry.transports if a is adapter), None)
        if transport is None:
            return None
        envelope = {'app_id': header.app_id, 'transport_tenant_key': header.tenant_key, 'tenant_key': sender.tenant_key,
            'recipient_tenant_key': binding['recipient_tenant_key'], 'recipient_open_id': binding['recipient_open_id'],
            'chat_id': message.chat_id, 'sender_open_id': ids.open_id, 'message_id': message.message_id,
            'parent_id': getattr(message, 'parent_id', None), 'root_id': getattr(message, 'root_id', None),
            'thread_id': getattr(message, 'thread_id', None)}
        receiver = next(p for p in snapshot['profiles'] if p['id'] == binding['profile_id'])
        policy = {**binding, 'project_id': receiver['project_id']}
        return PreparedKnowledgeMessage(event, adapter, transport, policy, envelope, text, None, kind, query_id,
            binding['id'], VerifiedIdentity(binding['sender_identity_ref'], 'verified-registered-knowledge-bot'))
    except (AttributeError, KeyError, TypeError, ValueError, ManagementError):
        return None


async def process_knowledge_message(entry, prepared, generation):
    entry.require_active(generation)
    async with entry.lock:
        manager = entry.manager()
        if prepared.knowledge_kind == 'query':
            snapshot = manager.read_snapshot(VerifiedIdentity(entry.owner, 'trusted-knowledge-source'))
            receiver = next(p for p in snapshot['profiles'] if p['id'] == prepared.binding['profile_id'])
            wiki = VerifiedIdentity(receiver['identity_ref'], 'verified-wiki-inbox')
            manager.receive_wiki_query(wiki, prepared.query_id, prepared.knowledge_binding_id, prepared.envelope)
            def read_if_active():
                with entry.lifecycle_lock:
                    entry.require_active(generation)
                    return manager.resolve_knowledge(wiki, prepared.query_id)
            await asyncio.to_thread(read_if_active)
            entry.require_active(generation)
            await deliver_knowledge(entry, wiki, prepared.query_id, prepared.transport, generation)
        else:
            version = re.match(r'^资料结果\s+\S+\s+([a-f0-9]{64})', prepared.command).group(1)
            query = manager.receive_wiki_result(prepared.sender_identity, prepared.query_id,
                prepared.knowledge_binding_id, prepared.envelope, version)
            if query['auto_supplement']:
                requester = VerifiedIdentity(query['requester'], 'registered-knowledge-request-delegation')
                def submit_if_active():
                    with entry.lifecycle_lock:
                        entry.require_active(generation)
                        return manager.supplement_knowledge(requester, prepared.query_id)
                await asyncio.to_thread(submit_if_active)
                entry.require_active(generation)
        return {'action': 'skip'}


async def deliver_knowledge(entry, identity, query_id, transport, generation=None):
    generation = entry.generation if generation is None else generation
    while True:
        entry.require_active(generation)
        binding = entry.manager().next_knowledge_delivery_binding(identity, query_id)
        if binding is None:
            return
        recipient = await transport.verify_identity(binding)
        entry.require_active(generation)
        if not recipient or recipient.get('app_id') != binding['app_id'] or recipient.get('open_id') != binding['recipient_open_id']:
            return
        segment = entry.manager().claim_knowledge_delivery(identity, query_id)
        if segment is None:
            return
        try:
            receipt = await transport.send(segment)
            entry.require_active(generation)
        except Exception:
            entry.require_active(generation)
            receipt = {'status': 'unknown'}
        entry.manager().record_knowledge_delivery(identity, query_id, segment['uuid'], receipt)
        if receipt.get('status') != 'delivered':
            return
