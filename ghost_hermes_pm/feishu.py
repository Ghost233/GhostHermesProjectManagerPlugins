"""Native Feishu SDK requests. One persisted UUID and receipt per logical segment."""
import asyncio
import json
import subprocess

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

    async def send(self, segment):
        self.lifecycle_check()
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody, CreateMessageRequest, CreateMessageRequestBody
        content = json.dumps({'zh_cn': {'content': [[
            {'tag': 'at', 'user_id': segment['mention_open_id']},
            {'tag': 'text', 'text': '\n' + segment['text']}]]}}, ensure_ascii=False)
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


def read_github_issue(url):
    """Read the fixed Issue scope only after verifying the mandated GitHub account."""
    def run(*args):
        result = subprocess.run(['gh', *args], text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise ManagementError('unavailable', 'The GitHub Issue source could not be verified.')
        return result.stdout
    try:
        run('auth', 'switch', '--hostname', 'github.com', '--user', 'Ghost233')
        if run('api', '--hostname', 'github.com', 'user', '--jq', '.login').strip() != 'Ghost233':
            raise ManagementError('unauthorized', 'The GitHub account must be Ghost233.')
        value = json.loads(run('issue', 'view', url, '--json', 'url,title,body,updatedAt'))
        return {'url': value['url'], 'title': value['title'], 'body': value['body'], 'updated_at': value['updatedAt']}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise ManagementError('unavailable', 'The GitHub Issue source could not be verified.') from exc
