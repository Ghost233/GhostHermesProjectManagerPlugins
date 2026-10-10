"""Native Feishu SDK requests. One persisted UUID and receipt per logical segment."""
import asyncio
import json

from .manager import ManagementError


class NativeFeishuTransport:
    def __init__(self, native):
        self.native = native
        self.lifecycle_check = lambda: None
        secret = getattr(getattr(native, 'config', None), 'app_secret', None)
        self.sensitive_values = (secret,) if isinstance(secret, str) and secret else ()

    def bind_lifecycle_guard(self, check):
        self.lifecycle_check = check

    async def verify_identity(self, binding):
        self.lifecycle_check()
        from lark_oapi import BaseRequest, HttpMethod, AccessTokenType, AppType
        config = self.native.config
        # A manually supplied token is not proven to belong to config.app_id.
        if config is None or config.enable_set_token is not False or config.app_type != AppType.SELF or config.app_id != binding['app_id']:
            return None
        request = BaseRequest.builder().http_method(HttpMethod.GET).uri('/open-apis/bot/v3/info').token_types({AccessTokenType.TENANT}).build()
        def request_if_active():
            self.lifecycle_check()
            return self.native.request(request)
        response = await asyncio.to_thread(request_if_active)
        self.lifecycle_check()
        if type(response.code) is not int or response.code != 0 or not response.raw:
            return None
        payload = json.loads(response.raw.content)
        bot = payload.get('bot', {})
        if type(payload.get('code')) is not int or payload['code'] != 0 or not isinstance(bot.get('open_id'), str) or not bot['open_id'] or bot.get('activate_status') != 2:
            return None
        return {'app_id': config.app_id, 'open_id': bot['open_id']}

    async def verify_channel(self, binding, evidence, challenge):
        """Re-read platform receipts; the caller supplies locators, never success claims."""
        from lark_oapi.api.im.v1 import GetChatRequest, GetMessageRequest
        identity = await self.verify_identity(binding)
        if identity != {'app_id': binding['app_id'], 'open_id': binding['recipient_open_id']}:
            raise ManagementError('capability_unverified', 'The current bot identity is unverified.')
        self.lifecycle_check()
        group = await asyncio.to_thread(self.native.im.v1.chat.get, GetChatRequest.builder().chat_id(binding['chat_id']).user_id_type('open_id').build())
        self.lifecycle_check()
        if type(group.code) is not int or group.code != 0 or group.data is None or group.data.tenant_key != binding['recipient_tenant_key']:
            raise ManagementError('capability_unverified', 'The current bot cannot read the exact original tenant/group.')
        if not isinstance(evidence, dict) or set(evidence) != {'delivery_message_id', 'acceptance_message_id'} or any(not isinstance(v, str) or not v for v in evidence.values()) or len(set(evidence.values())) != 2:
            raise ManagementError('capability_unverified', 'Actual delivery and independent acceptance message locators are required.')
        messages = []
        for field in ('delivery_message_id', 'acceptance_message_id'):
            locator = evidence[field]
            result = await asyncio.to_thread(self.native.im.v1.message.get, GetMessageRequest.builder().message_id(locator).user_id_type('open_id').build())
            self.lifecycle_check()
            items = getattr(getattr(result, 'data', None), 'items', None)
            if type(result.code) is not int or result.code != 0 or not isinstance(items, list) or len(items) != 1:
                raise ManagementError('capability_unverified', 'A current platform channel receipt is unavailable.')
            item = items[0]
            if item.message_id != locator or item.chat_id != binding['chat_id'] or item.deleted is not False or item.sender is None or item.sender.id_type != 'open_id':
                raise ManagementError('capability_unverified', 'Channel receipt source identity or content is unverified.')
            messages.append(item)
        delivery, acceptance = messages
        if delivery.sender.sender_type != 'app' or delivery.sender.id != identity['open_id'] or delivery.sender.tenant_key != binding['recipient_tenant_key'] or acceptance.sender.sender_type != 'user' or acceptance.sender.id != binding.get('owner_open_id') or acceptance.sender.tenant_key != binding.get('sender_tenant_key') or acceptance.parent_id != delivery.message_id:
            raise ManagementError('capability_unverified', 'The actual delivery and independent acceptance do not bind this bot, Owner and group.')
        try:
            delivered_text = json.loads(delivery.body.content)['text']
            accepted_text = json.loads(acceptance.body.content)['text']
        except (AttributeError, ValueError, KeyError, TypeError) as exc:
            raise ManagementError('capability_unverified', 'Channel acceptance needs exact readable challenge messages.') from exc
        if delivered_text != '通道验收 ' + challenge or accepted_text != '已受理验收 ' + challenge:
            raise ManagementError('capability_unverified', 'The platform acceptance belongs to another configuration or migration plan.')
        return {**identity, 'chat_id': binding['chat_id'], 'recipient_tenant_key': binding['recipient_tenant_key'], 'transport_tenant_key': binding['transport_tenant_key'], 'sender_tenant_key': binding['sender_tenant_key'], **evidence, 'challenge': challenge, 'source': 'actual_feishu_group_and_message_reads'}

    async def send(self, segment):
        self.lifecycle_check()
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody, CreateMessageRequest, CreateMessageRequestBody
        items = []
        if segment.get('mention_open_id'):
            items.append({'tag': 'at', 'user_id': segment['mention_open_id']})
        items.append({'tag': 'text', 'text': '\n' + segment['text']})
        content = json.dumps({'zh_cn': {'content': [items]}}, ensure_ascii=False)
        if segment.get('path') == 'create':
            body = CreateMessageRequestBody.builder().receive_id(segment['chat_id']).msg_type('post').content(content).uuid(segment['uuid']).build()
            request = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(body).build()
        else:
            body = ReplyMessageRequestBody.builder().msg_type('post').content(content).uuid(segment['uuid']).reply_in_thread(bool(segment.get('thread_id'))).build()
            request = ReplyMessageRequest.builder().message_id(segment['reply_to']).request_body(body).build()
        try:
            def reply_if_active():
                self.lifecycle_check()
                return self.native.im.v1.message.create(request) if segment.get('path') == 'create' else self.native.im.v1.message.reply(request)
            response = await asyncio.to_thread(reply_if_active)
            self.lifecycle_check()
        except Exception:
            return {'status': 'unknown'}
        if type(response.code) is not int:
            return {'status': 'unknown'}
        if response.code != 0:
            return {'status': 'failed', 'code': response.code}
        data = response.data
        if data is None or not isinstance(getattr(data, 'message_id', None), str) or not data.message_id or any(getattr(data, k, None) is not None and not isinstance(getattr(data, k), str) for k in ('chat_id', 'root_id', 'parent_id', 'thread_id')):
            return {'status': 'unknown', 'code': 0}
        return {'status': 'delivered', 'code': 0,
                **{k: getattr(data, k, None) for k in ('message_id', 'chat_id', 'root_id', 'parent_id', 'thread_id')}}


def read_github_issue(url, *, expected_account=None):
    """The intake preserves its unavailable code while sharing authenticated Issue reads."""
    from .github import GitHubDeliverySource
    return GitHubDeliverySource(expected_account=expected_account, error_code='unavailable').read_issue(url)
