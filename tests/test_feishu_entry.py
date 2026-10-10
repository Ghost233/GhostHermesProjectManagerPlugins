from types import SimpleNamespace as NS
import pytest

from ghost_hermes_pm import Manager
from readiness_support import ReadyManager as Manager
from test_directory import OWNER, make_repo, registration
from test_requests import ISSUE


CONFIG = {'enabled': True, 'verification_ref': 'fixture:controlled-contract', 'bindings': [
    {'sender_tenant_key': 'tenant-fixture', 'recipient_tenant_key': 'tenant-bot',
     'transport_tenant_key': 'tenant-transport', 'verification_ref': 'fixture:identity-map',
     'chat_id': 'oc_project', 'owner_open_id': 'ou_owner',
     'recipient_open_id': 'ou_lead', 'app_id': 'cli_fixture',
     'project_id': 'mono', 'profile_id': 'mono-lead', 'repository': 'example-user/fixture'}]}


def event(text='@_user_1 派发 https://github.com/example-user/fixture/issues/15', message_id='om_request'):
    raw = NS(header=NS(app_id='cli_fixture', tenant_key='tenant-transport', event_type='im.message.receive_v1'),
             event=NS(sender=NS(sender_type='user', tenant_key='tenant-fixture',
                               sender_id=NS(open_id='ou_owner', user_id='u_owner', union_id='on_owner')),
                      message=NS(message_id=message_id, chat_id='oc_project', chat_type='group',
                                 message_type='text', content=__import__('json').dumps({'text': text}),
                                 mentions=[NS(key='@_user_1', mentioned_type='bot', tenant_key='tenant-bot',
                                              id=NS(open_id='ou_lead'))], parent_id=None,
                                 upper_message_id=None, root_id=None, thread_id=None)))
    source = NS(platform='feishu', user_id='u_owner', user_id_alt='on_owner', chat_id='oc_project',
                is_bot=False, message_id=message_id, profile='transport-profile')
    return NS(source=source, raw_message=raw, message_id=message_id, text=text)


class Gateway:
    def __init__(self, adapter, authorized=True):
        self.adapter = adapter
        self.authorized = authorized
        self.authorized_sources = []
    def _intake_adapter_for(self, source):
        return self.adapter
    def _is_user_authorized_for_source(self, source):
        self.authorized_sources.append(source)
        return self.authorized
    def _admit_bot_message_for_source(self, source):
        return True


class Transport:
    def __init__(self):
        self.sent = []
    async def verify_identity(self, binding):
        return {'app_id': 'cli_fixture', 'open_id': 'ou_lead'}
    async def send(self, segment):
        self.sent.append(segment)
        return {'status': 'delivered', 'message_id': 'om_ack_' + str(len(self.sent)),
                'chat_id': segment['chat_id'], 'parent_id': segment['reply_to']}


@pytest.mark.asyncio
async def test_native_entry_preserves_original_source_auth_and_accepts_real_mention(tmp_path):
    from ghost_hermes_pm.messages import FeishuEntry
    adapter = object()
    transport = Transport()
    gateway = Gateway(adapter)
    original = event()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: ISSUE)
        intake.attach_transport(adapter, transport)
        result = await intake.receive(original, gateway)
        snapshot = manager.read_snapshot(OWNER)
    assert result == {'action': 'skip'}
    assert gateway.authorized_sources == [original.source]
    assert len(snapshot['requests']) == 1
    assert snapshot['requests'][0]['acceptance'] == 'accepted'
    assert snapshot['requests'][0]['delivery'] == 'delivered'
    assert snapshot['requests'][0]['execution'] == 'waiting'
    assert transport.sent[0]['reply_to'] == 'om_request'
    assert transport.sent[1]['reply_to'] == 'om_ack_1'
    assert transport.sent[0]['mention_open_id'] == 'ou_owner'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['auth_false', 'auth_unknown', 'auth_raises', 'missing_auth', 'sender',
                                 'source_chat', 'tenant', 'mention_text', 'mention_tenant',
                                 'mention_type', 'recipient', 'debounced', 'echo', 'disabled',
                                 'header_app', 'header_type', 'missing_header_tenant', 'unknown_sender'])
async def test_unverified_sources_never_accept_or_send_and_preserve_host_path(tmp_path, case):
    from ghost_hermes_pm.messages import FeishuEntry
    adapter, transport = object(), Transport()
    gateway, incoming = Gateway(adapter), event()
    config = {**CONFIG}
    if case == 'auth_false': gateway.authorized = False
    if case == 'auth_unknown': gateway.authorized = None
    if case == 'auth_raises':
        gateway._is_user_authorized_for_source = lambda source: (_ for _ in ()).throw(RuntimeError('fixture failure'))
    if case == 'missing_auth': gateway._is_user_authorized_for_source = None
    if case == 'sender': incoming.source.user_id = 'forged'
    if case == 'source_chat': incoming.source.chat_id = 'oc_other'
    if case == 'tenant': incoming.raw_message.event.sender.tenant_key = None
    if case == 'mention_text': incoming.raw_message.event.message.mentions = []
    if case == 'mention_tenant': incoming.raw_message.event.message.mentions[0].tenant_key = 'other'
    if case == 'mention_type': incoming.raw_message.event.message.mentions[0].mentioned_type = None
    if case == 'recipient':
        async def other(binding): return {'app_id': 'other', 'open_id': 'ou_lead'}
        transport.verify_identity = other
    if case == 'debounced': incoming.message_id = incoming.source.message_id = 'om_last'
    if case == 'echo': incoming.source.is_bot = True
    if case == 'disabled': config['enabled'] = False
    if case == 'header_app': incoming.raw_message.header.app_id = 'cli_other'
    if case == 'header_type': incoming.raw_message.header.event_type = 'synthetic'
    if case == 'missing_header_tenant': incoming.raw_message.header.tenant_key = None
    if case == 'unknown_sender': incoming.source.is_bot = None
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, config, lambda url: ISSUE)
        intake.attach_transport(adapter, transport)
        assert await intake.receive(incoming, gateway) is None
        assert manager.read_snapshot(OWNER)['requests'] == []
        assert transport.sent == []


@pytest.mark.asyncio
async def test_plain_input_uses_original_ack_and_ambiguity_sends_explicit_clarification(tmp_path):
    from ghost_hermes_pm.messages import FeishuEntry
    adapter, transport = object(), Transport()
    gateway = Gateway(adapter)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: ISSUE)
        intake.attach_transport(adapter, transport)
        await intake.receive(event(), gateway)
        assert await intake.receive(event('请保留测试证据', 'om_followup'), gateway) == {'action': 'skip'}
        assert transport.sent[-1]['reply_to'] == 'om_ack_1'
        await intake.receive(event(message_id='om_second'), gateway)
        assert await intake.receive(event('请继续', 'om_ambiguous'), gateway) == {'action': 'skip'}
        assert '请引用任务起始消息' in transport.sent[-1]['text']
        assert transport.sent[-1]['reply_to'] == 'om_ambiguous'
        assert manager.read_snapshot(OWNER)['clarifications'][0]['delivery'] == 'delivered'
        count = len(transport.sent)
        await intake.receive(event('谢谢', 'om_echo'), gateway)
        assert len(transport.sent) == count


@pytest.mark.asyncio
async def test_explicit_owner_retry_reuses_failed_confirmation_and_accepts_only_once(tmp_path):
    from ghost_hermes_pm.messages import FeishuEntry
    adapter, transport = object(), Transport()
    gateway = Gateway(adapter)
    async def failed(segment):
        transport.sent.append(segment)
        return {'status': 'failed', 'code': 999}
    transport.send = failed
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: ISSUE)
        intake.attach_transport(adapter, transport)
        await intake.receive(event(), gateway)
        request = manager.read_snapshot(OWNER)['requests'][0]
        original_uuid = transport.sent[0]['uuid']
        await intake.receive(event(), gateway)
        assert len(transport.sent) == 1
        transport.send = Transport.send.__get__(transport)
        assert await intake.receive(event('重试投递 ' + request['id'], 'om_retry'), gateway) == {'action': 'skip'}
        assert len(transport.sent) == 3
        assert transport.sent[1]['uuid'] == original_uuid
        assert len(manager.read_snapshot(OWNER)['requests']) == 1
        assert manager.read_snapshot(OWNER)['requests'][0]['delivery'] == 'delivered'
