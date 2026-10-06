"""Real lark-oapi 1.6.8 model/builders; only external HTTP methods are substitutes."""
import json
from types import SimpleNamespace as NS
import pytest
from lark_oapi import Client, AppType
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse

from ghost_hermes_pm import Manager
from ghost_hermes_pm.feishu import NativeFeishuTransport
from ghost_hermes_pm.messages import FeishuEntry
from test_directory import OWNER, make_repo, registration
from test_requests import ISSUE
from test_feishu_entry import CONFIG, Gateway, event


def sdk_event():
    incoming = event()
    incoming.raw_message = P2ImMessageReceiveV1(json.loads(json.dumps(
        incoming.raw_message, default=lambda value: vars(value))))
    return incoming


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['valid', 'inactive', 'manual_token', 'isv', 'bad_json', 'wrong_bot', 'no_bot'])
async def test_real_sdk_entry_checks_bot_identity_and_builds_real_mention_reply(tmp_path, mode):
    native = Client.builder().app_id('cli_fixture').app_secret('synthetic-unused-secret').build()
    if mode == 'manual_token':
        native = Client.builder().app_id('cli_fixture').enable_set_token(True).build()
    if mode == 'isv':
        native = Client.builder().app_id('cli_fixture').app_type(AppType.ISV).build()
    payload = {'code': 0, 'bot': {'open_id': 'ou_lead', 'activate_status': 2}}
    if mode == 'inactive': payload['bot']['activate_status'] = 1
    if mode == 'wrong_bot': payload['bot']['open_id'] = 'ou_other'
    if mode == 'no_bot': payload.pop('bot')
    requests, replies = [], []
    def bot_info(request):
        requests.append(request)
        return NS(code=0, raw=NS(content=b'not-json' if mode == 'bad_json' else json.dumps(payload).encode()))
    def reply(request):
        replies.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sdk_' + str(len(replies)),
                                     'chat_id': 'oc_project', 'parent_id': request.message_id}})
    native.request = bot_info
    native.im.v1.message.reply = reply
    adapter = object()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: ISSUE)
        intake.attach_transport(adapter, NativeFeishuTransport(native))
        result = await intake.receive(sdk_event(), Gateway(adapter))
        snapshot = manager.read_snapshot(OWNER)
    if mode != 'valid':
        assert result is None
        assert snapshot['requests'] == []
        assert replies == []
    else:
        assert result == {'action': 'skip'}
        assert requests[0].uri == '/open-apis/bot/v3/info'
        assert len(replies) == 2
        assert replies[0].message_id == 'om_request'
        body = replies[0].request_body
        assert body.uuid == snapshot['requests'][0]['outbox'][0]['segments'][0]['uuid']
        assert body.msg_type == 'post'
        assert json.loads(body.content)['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'ou_owner'}
        assert replies[1].message_id == 'om_sdk_1'
