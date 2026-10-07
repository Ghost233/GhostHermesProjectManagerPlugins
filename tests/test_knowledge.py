"""Public knowledge boundaries with actual local material and protocol peers."""
import hashlib
import json
from pathlib import Path

from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.knowledge import LocalKnowledgeProvider
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER


WIKI = {'id': 'wiki', 'native_profile': 'wiki', 'identity_ref': 'fixture:wiki', 'role': 'independent',
        'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}


def source_grant():
    return {'id': 'fixture-wiki', 'name': 'Fixture knowledge', 'provider_ref': 'local:fixture-wiki',
            'wiki_profile_id': 'wiki', 'query_subjects': {OWNER.subject: ['public']},
            'public_channels': [], 'task_profiles': [], 'wiki_bindings': []}


def local_provider(root):
    wiki = root / 'source'; wiki.mkdir()
    (wiki / 'retry.md').write_text('Retry policy\n\nRetry only definite failures. Unknown delivery needs reconciliation.\n')
    (wiki / 'private.md').write_text('PRIVATE material must never enter the public result.\n')
    return LocalKnowledgeProvider(wiki, [
        {'id': 'retry', 'path': 'retry.md', 'scope_id': 'public', 'kind': 'fact', 'terms': ['retry', 'delivery']},
        {'id': 'private', 'path': 'private.md', 'scope_id': 'private', 'kind': 'fact', 'terms': ['retry']}])


def test_token_query_reads_actual_allowed_material_and_provenance_without_issue_or_source_write(tmp_path):
    provider = local_provider(tmp_path)
    before = hashlib.sha256((tmp_path / 'source' / 'retry.md').read_bytes()).hexdigest()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        with ManagementServer(manager, {'owner-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner-entry')
            client.register_knowledge_source(1, source_grant())
            result = client.query_knowledge('fixture-wiki', 'query-1', 'retry delivery', ['public'])
            snapshot = client.read_snapshot()
        assert result['status'] == 'found'
        assert result['requester'] == OWNER.subject
        assert result['materials'][0]['text'] == 'Retry only definite failures. Unknown delivery needs reconciliation.'
        assert result['materials'][0]['kind'] == 'fact'
        assert result['materials'][0]['locator'].endswith('retry.md#L3')
        assert result['materials'][0]['version'] == 'sha256:' + before
        assert result['materials'][0]['updated_at']
        assert result['searched_scope'] == ['public']
        assert 'PRIVATE' not in json.dumps(result)
        assert snapshot['requests'] == []
        assert snapshot['knowledge_queries'][0]['id'] == 'query-1'
        assert snapshot['knowledge_sources'][0]['id'] == 'fixture-wiki'
    assert hashlib.sha256((tmp_path / 'source' / 'retry.md').read_bytes()).hexdigest() == before


def test_wiki_and_superior_membership_cannot_replace_original_requester_source_grants(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    provider = local_provider(tmp_path)
    superior = {**WIKI, 'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward', 'role': 'steward'}
    wiki_identity = VerifiedIdentity(WIKI['identity_ref'], 'fixture-wiki-entry')
    steward_identity = VerifiedIdentity(superior['identity_ref'], 'fixture-steward-entry')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        manager.apply_directory_change(OWNER, 1, {'profile': superior})
        with ManagementServer(manager, {'owner': OWNER, 'wiki': wiki_identity, 'steward': steward_identity}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            owner.register_knowledge_source(2, source_grant())
            for token in ('owner', 'wiki', 'steward'):
                with pytest.raises(ManagementError) as denied:
                    ManagementClient(tmp_path / 'state', token).query_knowledge('fixture-wiki', token + '-private', 'retry', ['private'])
                assert denied.value.code == 'forbidden'
            assert owner.read_snapshot()['knowledge_queries'] == []
            assert owner.read_snapshot()['requests'] == []


def test_duplicate_query_keeps_fixed_result_and_material_is_hidden_after_grant_revocation(tmp_path):
    provider = local_provider(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': provider}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.register_knowledge_source(1, source_grant())
            first = client.query_knowledge('fixture-wiki', 'same-query', 'retry delivery', ['public'])
            (tmp_path / 'source' / 'retry.md').write_text('Retry policy\n\nChanged material must not replace a repeated fixed result.\n')
            repeated = client.query_knowledge('fixture-wiki', 'same-query', 'retry delivery', ['public'])
            assert repeated['materials'] == first['materials']
            changed = source_grant(); changed['query_subjects'] = {WIKI['identity_ref']: ['public']}
            client.register_knowledge_source(client.read_snapshot()['version'], changed)
            snapshot = client.read_snapshot()
            assert all(not q.get('materials') for q in snapshot['knowledge_queries'])
            assert 'Retry only definite failures' not in json.dumps(snapshot)


def public_grant():
    from test_requests import MESSAGE
    channel = {k: MESSAGE[k] for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}
    channel.update(id='project-chat', profile_id='mono-lead', scope_ids=['public'],
                   view_subjects=[OWNER.subject, 'fixture:lead'], wiki_mention_open_id='ou_wiki')
    source = source_grant()
    source['public_channels'] = [channel]
    source['task_profiles'] = ['mono-lead']
    source['query_subjects']['fixture:lead'] = ['public']
    source['wiki_bindings'] = [
        {**channel, 'id': 'wiki-request-inbox', 'kind': 'query', 'channel_id': 'project-chat', 'profile_id': 'wiki',
         'app_id': 'cli_wiki', 'recipient_open_id': 'ou_wiki_self', 'recipient_tenant_key': 'tenant-wiki-bot',
         'sender_profile_id': 'mono-lead', 'sender_identity_ref': 'fixture:lead', 'sender_tenant_key': 'tenant-lead',
         'sender_open_id': 'ou_lead_in_wiki', 'sender_native_ids': ['u_lead_in_wiki', 'ou_lead_in_wiki']},
        {**channel, 'id': 'wiki-result-inbox', 'kind': 'result', 'channel_id': 'project-chat',
         'sender_profile_id': 'wiki', 'sender_identity_ref': WIKI['identity_ref'], 'sender_tenant_key': 'tenant-wiki',
         'sender_open_id': 'ou_wiki', 'sender_native_ids': ['u_wiki', 'ou_wiki']}]
    # A binding describes an incoming namespace; outbound-only display fields are not identities.
    for binding in source['wiki_bindings']:
        for key in ('view_subjects', 'wiki_mention_open_id'):
            binding.pop(key)
    return source


def test_public_query_creates_durable_real_mention_request_without_starting_codex(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import ReplyMessageResponse
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    from test_directory import make_repo
    from test_task_execution import accepted
    provider = local_provider(tmp_path)
    native = Client.builder().app_id('cli_fixture').app_secret('synthetic-unused-secret').build()
    native.request = lambda request: SimpleNamespace(code=0, raw=SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    sent = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_query', 'chat_id': 'oc_project', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.register_knowledge_source(client.read_snapshot()['version'], public_grant())
            query = client.query_knowledge('fixture-wiki', 'public-query', 'retry delivery', ['public'], request_id=request_id, channel_id='project-chat')
            assert query['status'] == 'awaiting_wiki'
            assert query['materials'] == []
            segment = manager.claim_knowledge_delivery(OWNER, query['id'])
            transport = NativeFeishuTransport(native)
            assert asyncio.run(transport.verify_identity(segment))['open_id'] == 'ou_lead'
            receipt = asyncio.run(transport.send(segment))
            manager.record_knowledge_delivery(OWNER, query['id'], segment['uuid'], receipt)
            assert manager.claim_knowledge_delivery(OWNER, query['id']) is None
            assert sent[0].message_id == 'om_ack'
            assert sent[0].request_body.uuid == segment['uuid']
            assert json.loads(sent[0].request_body.content)['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'ou_wiki'}
            assert '资料查询 public-query' in segment['text']
            assert client.read_snapshot()['requests'][0]['execution'] == 'waiting'
    assert not (tmp_path / 'wire.jsonl').exists()


def test_source_reply_cannot_turn_material_into_new_task_or_supplement_authority(tmp_path):
    provider = local_provider(tmp_path)
    class InjectedProvider:
        def query(self, **request):
            result = provider.query(**request)
            return result | {'auto_supplement': True, 'request_id': 'other-task', 'query_subjects': {'everyone': ['private']}}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': InjectedProvider()}) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.register_knowledge_source(1, source_grant())
            result = client.query_knowledge('fixture-wiki', 'injected-reply', 'retry', ['public'])
            assert result['status'] == 'source_denied'
            assert result['request_id'] is None
            assert result['auto_supplement'] is False
            assert result['materials'] == []
            assert client.read_snapshot()['requests'] == []


def test_registered_wiki_resolves_original_requester_and_returns_to_actual_sender(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    from test_directory import make_repo
    from test_task_execution import accepted
    provider = local_provider(tmp_path)
    wiki_identity = VerifiedIdentity(WIKI['identity_ref'], 'verified-wiki-source')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'wiki': wiki_identity}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.register_knowledge_source(client.read_snapshot()['version'], public_grant())
            query = client.query_knowledge('fixture-wiki', 'resolve-query', 'retry delivery', ['public'], request_id=request_id, channel_id='project-chat')
            outgoing = manager.claim_knowledge_delivery(OWNER, query['id'])
            manager.record_knowledge_delivery(OWNER, query['id'], outgoing['uuid'],
                {'status': 'delivered', 'message_id': 'om_query', 'chat_id': 'oc_project', 'parent_id': outgoing['reply_to']})
            binding = public_grant()['wiki_bindings'][0]
            received = {k: binding[k] for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}
            received.update(tenant_key=binding['sender_tenant_key'], sender_open_id=binding['sender_open_id'], message_id='om_query', parent_id='om_ack', root_id=None, thread_id=None)
            manager.receive_wiki_query(wiki_identity, query['id'], binding['id'], received)
            result = ManagementClient(tmp_path / 'state', 'wiki').resolve_knowledge(query['id'])
            assert result['requester'] == OWNER.subject
            assert result['status'] == 'found'
            assert result['materials'][0]['text'].startswith('Retry only definite failures.')
            reply = manager.claim_knowledge_delivery(wiki_identity, query['id'])
            assert reply['reply_to'] == 'om_query'
            assert reply['mention_open_id'] == 'ou_lead_in_wiki'
            assert reply['app_id'] == 'cli_wiki'
            assert '资料结果 resolve-query' in reply['text']
            assert 'sha256:' in reply['text']
            assert 'PRIVATE' not in reply['text']


def prepared_result(manager, client, source_root, request_id, query_id='facts-query', auto=True):
    from test_task_control import TURN
    source = public_grant()
    client.register_knowledge_source(client.read_snapshot()['version'], source)
    query = client.query_knowledge('fixture-wiki', query_id, 'retry delivery', ['public'], request_id=request_id,
                                   channel_id='project-chat', auto_supplement=auto)
    segment = manager.claim_knowledge_delivery(OWNER, query_id)
    manager.record_knowledge_delivery(OWNER, query_id, segment['uuid'], {'status': 'delivered', 'message_id': 'om_query', 'chat_id': 'oc_project'})
    binding = source['wiki_bindings'][0]
    anchor = {k: binding[k] for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}
    anchor.update(tenant_key=binding['sender_tenant_key'], sender_open_id=binding['sender_open_id'], message_id='om_query', parent_id='om_ack', root_id=None, thread_id=None)
    wiki = VerifiedIdentity(WIKI['identity_ref'], 'verified-wiki-source')
    manager.receive_wiki_query(wiki, query_id, binding['id'], anchor)
    result = manager.resolve_knowledge(wiki, query_id)
    segment = manager.claim_knowledge_delivery(wiki, query_id)
    manager.record_knowledge_delivery(wiki, query_id, segment['uuid'], {'status': 'delivered', 'message_id': 'om_wiki_result', 'chat_id': 'oc_project'})
    binding = source['wiki_bindings'][1]
    returned = {k: binding[k] for k in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}
    returned.update(tenant_key=binding['sender_tenant_key'], sender_open_id=binding['sender_open_id'], message_id='om_wiki_result', parent_id='om_query', root_id=None, thread_id=None)
    manager.receive_wiki_result(wiki, query_id, binding['id'], returned, result['result_version'])
    return query_id


def test_task_supplement_targets_original_active_turn_once_and_carries_facts_as_data(tmp_path):
    from test_directory import make_repo
    from test_task_execution import accepted, adapter_for
    from test_task_control import THREAD, TURN, wire
    provider = local_provider(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            query_id = prepared_result(manager, client, tmp_path, request_id)
            first = client.supplement_knowledge(query_id)
            repeated = client.supplement_knowledge(query_id)
            assert first['status'] == repeated['status'] == 'accepted'
            steering = [r for r in wire(tmp_path) if r['method'] == 'turn/steer']
            assert len(steering) == 1
            assert steering[0]['params']['threadId'] == THREAD and steering[0]['params']['expectedTurnId'] == TURN
            text = steering[0]['params']['input'][0]['text']
            assert 'Retry only definite failures' in text
            assert 'untrusted source data' in text
            assert 'Current accepted goal' in text and 'Repository boundary' in text
            assert 'PRIVATE' not in text
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1
            assert client.read_snapshot()['knowledge_queries'][0]['supplement']['instruction_id'].startswith('knowledge:')


def bot_message(binding, text, message_id, parent_id):
    from types import SimpleNamespace as NS
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    raw = {'header': {'app_id': binding['app_id'], 'tenant_key': binding['transport_tenant_key'], 'event_type': 'im.message.receive_v1'},
           'event': {'sender': {'sender_type': 'bot', 'tenant_key': binding['sender_tenant_key'],
               'sender_id': {'open_id': binding['sender_open_id'], 'user_id': binding['sender_native_ids'][0]}},
           'message': {'message_id': message_id, 'chat_id': binding['chat_id'], 'chat_type': 'group', 'message_type': 'post',
               'parent_id': parent_id, 'content': json.dumps({'zh_cn': {'content': [[
                   {'tag': 'at', 'user_id': binding['recipient_open_id']}, {'tag': 'text', 'text': '\n' + text}]]}}),
               'mentions': [{'key': '@_user_1', 'id': {'open_id': binding['recipient_open_id']},
                   'mentioned_type': 'bot', 'tenant_key': binding['recipient_tenant_key']}]}}}
    source = NS(platform='feishu', user_id=binding['sender_native_ids'][0], user_id_alt=None, chat_id=binding['chat_id'],
                is_bot=True, message_id=message_id, profile=binding['profile_id'])
    return NS(source=source, raw_message=P2ImMessageReceiveV1(raw), message_id=message_id, text=text)


def native_transport(app_id, open_id, prefix, sent):
    from types import SimpleNamespace as NS
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import ReplyMessageResponse
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    client = Client.builder().app_id(app_id).app_secret('synthetic-unused-' + app_id).build()
    client.request = lambda request: NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': open_id, 'activate_status': 2}}).encode()))
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': prefix + str(len(sent)), 'chat_id': 'oc_project', 'parent_id': request.message_id}})
    client.im.v1.message.reply = reply
    return NativeFeishuTransport(client)


def test_actual_registered_bot_messages_and_builders_complete_public_query_result_and_task_fact_chain(tmp_path):
    import asyncio
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway
    from test_directory import make_repo
    from test_task_execution import accepted, adapter_for
    from test_task_control import wire
    provider = local_provider(tmp_path)
    source = public_grant()
    sender = VerifiedIdentity('fixture:lead', 'verified-native-participant')
    settings = {**CONFIG, 'registered_bots': [{'profile_id': b['sender_profile_id'], 'identity_ref': b['sender_identity_ref'],
        'app_id': b['app_id'], 'tenant_key': b['sender_tenant_key'], 'open_id': b['sender_open_id'], 'native_ids': b['sender_native_ids']} for b in source['wiki_bindings']]}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'owner': OWNER, 'lead': sender}):
            owner, lead = ManagementClient(tmp_path / 'state', 'owner'), ManagementClient(tmp_path / 'state', 'lead')
            owner.register_knowledge_source(owner.read_snapshot()['version'], source)
            lead.query_knowledge('fixture-wiki', 'full-chain', 'retry delivery', ['public'], request_id=request_id, channel_id='project-chat', auto_supplement=True)
            left, right, left_sent, right_sent = object(), object(), [], []
            left_transport = native_transport('cli_fixture', 'ou_lead', 'om_lead_', left_sent)
            right_transport = native_transport('cli_wiki', 'ou_wiki_self', 'om_wiki_', right_sent)
            intake = FeishuEntry(lambda: manager, OWNER.subject, settings, lambda _: None)
            intake.attach_transport(left, left_transport); intake.attach_transport(right, right_transport)
            async def exchange():
                await intake.deliver_knowledge(sender, 'full-chain', left_transport)
                query_text = json.loads(left_sent[0].request_body.content)['zh_cn']['content'][0][1]['text'].strip()
                query_event = bot_message(source['wiki_bindings'][0], query_text, 'om_lead_1', 'om_ack')
                assert await intake.receive(query_event, Gateway(right)) == {'action': 'skip'}
                assert right_sent[0].message_id == 'om_lead_1'
                assert json.loads(right_sent[0].request_body.content)['zh_cn']['content'][0][0]['user_id'] == 'ou_lead_in_wiki'
                result_text = json.loads(right_sent[0].request_body.content)['zh_cn']['content'][0][1]['text'].strip()
                result_event = bot_message(source['wiki_bindings'][1], result_text, 'om_wiki_1', 'om_lead_1')
                assert await intake.receive(result_event, Gateway(left)) == {'action': 'skip'}
                assert await intake.receive(result_event, Gateway(left)) == {'action': 'skip'}
            asyncio.run(exchange())
            query = lead.read_snapshot()['knowledge_queries'][0]
            assert query['status'] == 'found'
            assert query['supplement']['status'] == 'accepted'
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/steer']) == 1
            assert owner.read_snapshot()['requests'][0]['id'] == request_id
