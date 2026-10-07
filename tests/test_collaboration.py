"""Public role bridge and real Lark builders, using synthetic registered identities."""
import json
from types import SimpleNamespace as NS
import pytest
from lark_oapi import Client
from lark_oapi.api.im.v1 import CreateMessageResponse

from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementServer, ManagementClient
from ghost_hermes_pm.feishu import NativeFeishuTransport
from test_directory import OWNER, registration, make_repo
from test_requests import ISSUE

STEWARD = VerifiedIdentity('fixture:steward', 'participant')
LEAD = VerifiedIdentity('fixture:lead', 'participant')
INGRESS = VerifiedIdentity('fixture:lead', 'native-collaboration-ingress')


class IssueSource:
    def read_issue(self, url):
        assert url == ISSUE['url']
        return dict(ISSUE)


def register_roles(manager, root):
    manager.apply_directory_change(OWNER, 0, registration(make_repo(root / 'mono')))
    manager.apply_directory_change(OWNER, 1, {'profile': {'id': 'steward', 'native_profile': 'steward',
        'identity_ref': STEWARD.subject, 'role': 'steward', 'capability': 'non_development',
        'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})


def channel(profile, group='project'):
    return {'id': profile + '-' + group, 'profile_id': profile, 'group_kind': group,
        'project_id': None if group == 'entry' else 'mono', 'app_id': 'cli_' + profile,
        'recipient_open_id': 'ou_' + profile, 'recipient_tenant_key': 'bot-' + profile,
        'transport_tenant_key': 'app-' + profile, 'chat_id': 'oc_' + group,
        'owner_open_id': 'owner-' + profile, 'owner_tenant_key': 'owner-tenant',
        'repository': 'Ghost233/fixture', 'verification_ref': 'fixture:registered-map',
        'bot_sources': [{'profile_id': 'steward', 'open_id': 'steward-seen-' + profile,
                         'tenant_key': 'steward-tenant', 'native_ids': ['steward-user-' + profile]}]}


def source(c, sender='owner', message_id='om_owner_goal'):
    return {'app_id': c['app_id'], 'transport_tenant_key': c['transport_tenant_key'],
        'recipient_tenant_key': c['recipient_tenant_key'], 'recipient_open_id': c['recipient_open_id'],
        'tenant_key': c['owner_tenant_key'] if sender == 'owner' else 'steward-tenant',
        'sender_open_id': c['owner_open_id'] if sender == 'owner' else 'steward-seen-' + c['profile_id'],
        'chat_id': c['chat_id'], 'message_id': message_id, 'parent_id': None, 'root_id': None, 'thread_id': None}


@pytest.mark.asyncio
async def test_owner_goal_is_publicly_sent_by_steward_and_independently_accepted_by_lead(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'steward': STEWARD, 'lead': LEAD, 'lead-ingress': INGRESS}):
            owner, steward, lead, ingress = [ManagementClient(tmp_path / 'state', t) for t in ('owner', 'steward', 'lead', 'lead-ingress')]
            entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
            sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
            owner.collaborate('register_channels', {'channels': [entry, sending, receiving]})
            handoff = owner.collaborate('project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead',
                'source_anchor': source(entry), 'issue_url': ISSUE['url']})
            assert handoff['acceptance'] == 'awaiting_receiver' and owner.read_snapshot()['requests'] == []
            packet = steward.collaborate('claim_delivery', {'handoff_id': handoff['id']})
            assert packet['path'] == 'create' and packet['chat_id'] == 'oc_project' and packet.get('reply_to') is None
            native = Client.builder().app_id('cli_steward').app_secret('synthetic-unused-secret').build()
            native.request = lambda request: NS(code=0, raw=NS(content=b'{"code":0,"bot":{"open_id":"ou_steward","activate_status":2}}'))
            sent = []
            def create(request):
                sent.append(request)
                return CreateMessageResponse({'code': 0, 'data': {'message_id': 'om_project_local', 'chat_id': 'oc_project'}})
            native.im.v1.message.create = create
            transport = NativeFeishuTransport(native)
            assert await transport.verify_identity(packet['sender_binding']) == {'app_id': 'cli_steward', 'open_id': 'ou_steward'}
            receipt = await transport.send(packet)
            steward.collaborate('record_delivery', {'handoff_id': handoff['id'], 'uuid': packet['uuid'], 'receipt': receipt})
            before = owner.read_snapshot()['collaboration']['handoffs'][0]
            assert before['delivery'] == 'delivered' and before['acceptance'] == 'awaiting_receiver'
            assert sent[0].receive_id_type == 'chat_id' and sent[0].request_body.receive_id == 'oc_project'
            assert sent[0].request_body.uuid == packet['uuid']
            content = json.loads(sent[0].request_body.content)['zh_cn']['content'][0]
            assert content[0] == {'tag': 'at', 'user_id': 'lead-seen-steward'}
            observed = {'channel_id': receiving['id'], 'source_anchor': source(receiving, 'bot', 'om_project_local'), 'text': packet['text']}
            with pytest.raises(Exception):
                lead.collaborate('ingest', observed)
            accepted = ingress.collaborate('ingest', observed)
            task = owner.read_snapshot()['requests'][0]
            assert accepted['acceptance'] == 'accepted' and task['acceptance'] == 'accepted'
            assert task['profile_id'] == 'mono-lead' and task['queue']['status'] == 'accepted'
            assert task['actor_provenance']['actor']['subject'] == LEAD.subject
            assert task['actor_provenance']['owner_origin']['subject'] == OWNER.subject
            assert task['source_anchor']['chat_id'] == 'oc_project'
            assert handoff['source_anchor']['chat_id'] == 'oc_entry'
            assert task['task_start_anchor'] is None and task['execution'] == 'waiting'


@pytest.mark.parametrize('case', ['wrong_sender', 'wrong_app', 'wrong_tenant', 'wrong_recipient', 'wrong_group', 'wrong_content', 'participant_receipt'])
def test_public_delivery_or_forged_source_does_not_create_a_new_task(tmp_path, case):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'steward': STEWARD, 'lead': LEAD, 'lead-ingress': INGRESS}):
            owner, steward, lead, ingress = [ManagementClient(tmp_path / 'state', t) for t in ('owner', 'steward', 'lead', 'lead-ingress')]
            entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
            sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
            owner.collaborate('register_channels', {'channels': [entry, sending, receiving]})
            h = owner.collaborate('project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']})
            p = steward.collaborate('claim_delivery', {'handoff_id': h['id']})
            steward.collaborate('record_delivery', {'handoff_id': h['id'], 'uuid': p['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_project', 'chat_id': 'oc_project'}})
            event = {'channel_id': receiving['id'], 'source_anchor': source(receiving, 'bot', 'om_project'), 'text': p['text']}
            field = {'wrong_sender': 'sender_open_id', 'wrong_app': 'app_id', 'wrong_tenant': 'transport_tenant_key', 'wrong_recipient': 'recipient_open_id', 'wrong_group': 'chat_id'}.get(case)
            if field:
                event['source_anchor'][field] = 'foreign'
            if case == 'wrong_content':
                event['text'] += '\nNew scope invented by a bot.'
            with pytest.raises(Exception):
                (lead if case == 'participant_receipt' else ingress).collaborate('ingest', event)
            snapshot = owner.read_snapshot()
            assert snapshot['requests'] == [] and snapshot['collaboration']['handoffs'][0]['acceptance'] == 'awaiting_receiver'


def test_unknown_cross_group_send_is_never_replayed_after_restart(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
        sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving]})
        h = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']})
        p = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': h['id']})
        manager.collaborate(STEWARD, 'record_delivery', {'handoff_id': h['id'], 'uuid': p['uuid'], 'receipt': {'status': 'unknown'}})
        assert manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': h['id']}) is None
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        assert manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': h['id']}) is None
        assert manager.read_snapshot(OWNER)['requests'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized,budget', [(True, True), (False, True), (True, False)])
async def test_real_sdk_owner_goal_entry_preserves_original_source_auth_and_budget(tmp_path, authorized, budget):
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from ghost_hermes_pm.messages import FeishuEntry
    entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
    sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving]})
        raw = P2ImMessageReceiveV1({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1', 'app_id': entry['app_id'], 'tenant_key': entry['transport_tenant_key']},
            'event': {'sender': {'sender_type': 'user', 'tenant_key': entry['owner_tenant_key'], 'sender_id': {'open_id': entry['owner_open_id'], 'user_id': 'owner-native'}},
                'message': {'message_id': 'om_native_owner_goal', 'chat_id': entry['chat_id'], 'chat_type': 'group', 'message_type': 'text',
                    'content': json.dumps({'text': '@_user_1 项目 mono ' + ISSUE['url']}),
                    'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': entry['recipient_tenant_key'], 'id': {'open_id': entry['recipient_open_id']}}]}}})
        original = NS(platform='feishu', user_id='owner-native', user_id_alt=None, chat_id='oc_entry', is_bot=False, message_id='om_native_owner_goal')
        event = NS(source=original, raw_message=raw, message_id=original.message_id)
        native = Client.builder().app_id('cli_steward').app_secret('synthetic-unused-secret').build()
        native.request = lambda request: NS(code=0, raw=NS(content=b'{"code":0,"bot":{"open_id":"ou_steward","activate_status":2}}'))
        sent = []
        def create(request):
            sent.append(request)
            return CreateMessageResponse({'code': 0, 'data': {'message_id': 'om_created_' + str(len(sent)), 'chat_id': 'oc_project'}})
        native.im.v1.message.create = create
        adapter = object()
        class Gateway:
            def __init__(self): self.auth_sources, self.budget_sources = [], []
            def _intake_adapter_for(self, source): return adapter
            def _is_user_authorized_for_source(self, source): self.auth_sources.append(source); return authorized
            def _admit_bot_message_for_source(self, source): self.budget_sources.append(source); return budget
        gateway = Gateway()
        intake = FeishuEntry(lambda: manager, OWNER.subject, {'enabled': True, 'verification_ref': 'fixture:entry'}, lambda url: None,
            collaboration_identity_ref=STEWARD.subject)
        intake.attach_transport(adapter, NativeFeishuTransport(native))
        result = await intake.receive(event, gateway)
        assert gateway.auth_sources == [original]
        assert gateway.budget_sources == ([original] if authorized else [])
        handoffs = manager.read_snapshot(OWNER)['collaboration']['handoffs']
        if not authorized or not budget:
            assert result is None and handoffs == [] and sent == []
        else:
            assert result == {'action': 'skip'} and len(sent) == 1
            assert handoffs[0]['owner_origin']['subject'] == OWNER.subject
            assert handoffs[0]['owner_origin']['source'] == 'verified-native-collaboration-owner'
            assert handoffs[0]['source_anchor']['message_id'] == original.message_id


CHILD = VerifiedIdentity('fixture:child', 'participant')
CHILD_INGRESS = VerifiedIdentity('fixture:child', 'native-collaboration-ingress')


def accepted_parent(manager, root):
    register_roles(manager, root)
    child_repo = make_repo(root / 'child')
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'project': {'id': 'child-project', 'name': 'Explicit child', 'repo_path': str(child_repo)},
        'profile': {'id': 'child', 'native_profile': 'child', 'identity_ref': CHILD.subject, 'role': 'subproject_lead', 'capability': 'development',
                    'project_id': 'child-project', 'parent_profile_id': 'mono-lead', 'connection_refs': {}}})
    entry, sending, receiving, child = channel('steward', 'entry'), channel('steward'), channel('mono-lead'), channel('child')
    sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
    receiving['bot_sources'].append({'profile_id': 'child', 'open_id': 'child-seen-lead', 'tenant_key': 'child-tenant', 'native_ids': ['child-user-lead']})
    child['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-child', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-child']})
    manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving, child]})
    h = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']})
    p = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': h['id']})
    manager.collaborate(STEWARD, 'record_delivery', {'handoff_id': h['id'], 'uuid': p['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_parent_scope', 'chat_id': 'oc_project'}})
    manager.collaborate(INGRESS, 'ingest', {'channel_id': receiving['id'], 'source_anchor': source(receiving, 'bot', 'om_parent_scope'), 'text': p['text']})
    return h['id'], child


def test_project_lead_delegates_a_clear_issue_only_to_explicit_own_child(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        parent_id, child = accepted_parent(manager, tmp_path)
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD, 'child': CHILD, 'child-ingress': CHILD_INGRESS}):
            lead, kid = ManagementClient(tmp_path / 'state', 'lead'), ManagementClient(tmp_path / 'state', 'child')
            request = {'parent_handoff_id': parent_id, 'target_profile_id': 'child', 'issue_url': ISSUE['url']}
            delegated = lead.collaborate('delegate_issue', request)
            assert delegated['sender_profile_id'] == 'mono-lead' and delegated['target_profile_id'] == 'child'
            assert delegated['owner_origin']['subject'] == OWNER.subject and delegated['parent_handoff_id'] == parent_id
            assert lead.collaborate('delegate_issue', request)['duplicate'] is True
            with pytest.raises(Exception):
                kid.collaborate('delegate_issue', request)
            packet = lead.collaborate('claim_delivery', {'handoff_id': delegated['id']})
            assert packet['mention_open_id'] == 'child-seen-lead'
            lead.collaborate('record_delivery', {'handoff_id': delegated['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_child_scope'}})
            observed = source(child, 'bot', 'om_child_scope')
            observed.update(tenant_key='lead-tenant', sender_open_id='lead-seen-child')
            received = ManagementClient(tmp_path / 'state', 'child-ingress').collaborate('ingest', {'channel_id': child['id'], 'source_anchor': observed, 'text': packet['text']})
            child_task = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == received['task_request_id'])
            assert child_task['profile_id'] == 'child' and child_task['project_id'] == 'child-project'
            assert child_task['actor_provenance']['actor']['subject'] == CHILD.subject
            assert child_task['actor_provenance']['forwarding_profile_id'] == 'mono-lead'
            assert child_task['actor_provenance']['owner_origin']['subject'] == OWNER.subject
            assert child_task['queue']['status'] == 'accepted' and child_task['execution'] == 'waiting'


def test_child_result_uses_original_issue_delivery_and_parent_only_reports_pending_integration(tmp_path):
    from test_task_execution import adapter_for, prepare_fixture
    from test_task_control import THREAD, TURN
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource(), codex_adapter=adapter_for(tmp_path)) as manager:
        parent_id, child_channel = accepted_parent(manager, tmp_path)
        version = manager.read_snapshot(OWNER)['version']
        profile = next(p for p in manager.read_snapshot(OWNER)['profiles'] if p['id'] == 'child')
        manager.apply_directory_change(OWNER, version, {'profile': {k: profile[k] for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id')} | {'connection_refs': {'codex': 'local:fixture-stdio'}}})
        channels = [dict(c) for c in manager.read_snapshot(OWNER)['collaboration']['channels']]
        for c in channels:
            c.pop('profile_binding')
        manager.collaborate(OWNER, 'register_channels', {'channels': channels})
        h = manager.collaborate(LEAD, 'delegate_issue', {'parent_handoff_id': parent_id, 'target_profile_id': 'child', 'issue_url': ISSUE['url']})
        p = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': h['id']})
        manager.collaborate(LEAD, 'record_delivery', {'handoff_id': h['id'], 'uuid': p['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_child_result_scope', 'chat_id': 'oc_project'}})
        observed = source(child_channel, 'bot', 'om_child_result_scope')
        observed.update(tenant_key='lead-tenant', sender_open_id='lead-seen-child')
        accepted = manager.collaborate(CHILD_INGRESS, 'ingest', {'channel_id': child_channel['id'], 'source_anchor': observed, 'text': p['text']})
        task_id = accepted['task_request_id']
        manager.collaborate(CHILD_INGRESS, 'publish_ack', {'handoff_id': h['id']})
        ack = manager.collaborate(CHILD_INGRESS, 'claim_ack', {'handoff_id': h['id']})
        manager.collaborate(CHILD_INGRESS, 'record_ack', {'handoff_id': h['id'], 'uuid': ack['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_child_ack', 'chat_id': 'oc_project'}})
        with pytest.raises(Exception):
            manager.collaborate(CHILD, 'report_result', {'handoff_id': h['id']})
        prepare_fixture(manager, task_id, tmp_path / 'child')
        manager.start_task(CHILD, task_id)
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full',
            'items': [{'type': 'commandExecution', 'id': 'pytest-child', 'command': 'python -m pytest tests/test_fixture.py -q', 'cwd': str(tmp_path / 'child'), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
        manager.record_task_delivery(CHILD, task_id, {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-child']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []})
        result = manager.collaborate(CHILD, 'report_result', {'handoff_id': h['id']})
        assert result['kind'] == 'result' and result['sender_profile_id'] == 'child' and result['target_profile_id'] == 'mono-lead'
        packet = manager.collaborate(CHILD, 'claim_delivery', {'handoff_id': result['id']})
        assert packet['mention_open_id'] == 'lead-seen-child'
        assert '待集成' in packet['text'] and '项目全部完成' not in packet['text']
        manager.collaborate(CHILD, 'record_delivery', {'handoff_id': result['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_child_result', 'chat_id': 'oc_project'}})
        parent_channel = next(c for c in channels if c['profile_id'] == 'mono-lead')
        received_source = source(parent_channel, 'bot', 'om_child_result')
        received_source.update(tenant_key='child-tenant', sender_open_id='child-seen-lead')
        before = len(manager.read_snapshot(OWNER)['requests'])
        received = manager.collaborate(INGRESS, 'ingest', {'channel_id': parent_channel['id'], 'source_anchor': received_source, 'text': packet['text']})
        assert received['acceptance'] == 'accepted' and len(manager.read_snapshot(OWNER)['requests']) == before
        parent = next(x for x in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if x['id'] == parent_id)
        assert parent['integration_status'] == 'awaiting_integration' and parent['whole_project_complete'] is False
