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
