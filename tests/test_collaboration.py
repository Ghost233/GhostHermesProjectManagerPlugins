"""Public role bridge and real Lark builders, using synthetic registered identities."""
import json
from types import SimpleNamespace as NS
import pytest
from lark_oapi import Client
from lark_oapi.api.im.v1 import CreateMessageResponse

from ghost_hermes_pm import Manager, VerifiedIdentity
from readiness_support import ReadyManager as Manager
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


def register_roles(manager, root, lead_capability='development'):
    initial = registration(make_repo(root / 'mono'))
    initial['profile']['capability'] = lead_capability
    manager.apply_directory_change(OWNER, 0, initial)
    manager.apply_directory_change(OWNER, 1, {'profile': {'id': 'steward', 'native_profile': 'steward',
        'identity_ref': STEWARD.subject, 'role': 'steward', 'capability': 'non_development',
        'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})


def channel(profile, group='project'):
    return {'id': profile + '-' + group, 'profile_id': profile, 'group_kind': group,
        'project_id': None if group == 'entry' else 'mono', 'app_id': 'cli_' + profile,
        'recipient_open_id': 'ou_' + profile, 'recipient_tenant_key': 'bot-' + profile,
        'transport_tenant_key': 'app-' + profile, 'chat_id': 'oc_' + group,
        'owner_open_id': 'owner-' + profile, 'owner_tenant_key': 'owner-tenant',
        'repository': 'example-user/fixture', 'verification_ref': 'fixture:registered-map',
        'bot_sources': [{'profile_id': 'steward', 'open_id': 'steward-seen-' + profile,
                         'tenant_key': 'steward-tenant', 'native_ids': ['steward-user-' + profile]}]}


def source(c, sender='owner', message_id='om_owner_goal'):
    return {'app_id': c['app_id'], 'transport_tenant_key': c['transport_tenant_key'],
        'recipient_tenant_key': c['recipient_tenant_key'], 'recipient_open_id': c['recipient_open_id'],
        'tenant_key': c['owner_tenant_key'] if sender == 'owner' else 'steward-tenant',
        'sender_open_id': c['owner_open_id'] if sender == 'owner' else 'steward-seen-' + c['profile_id'],
        'chat_id': c['chat_id'], 'message_id': message_id, 'parent_id': None, 'root_id': None, 'thread_id': None}


def test_repositoryless_steward_entry_routes_an_owner_goal_to_the_bound_project(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
        entry['repository'] = None
        sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving]})
        handoff = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']})
        packet = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': handoff['id']})
        assert packet['chat_id'] == 'oc_project' and packet['mention_open_id'] == 'lead-seen-steward'
        snapshot = manager.read_snapshot(OWNER)
        assert next(c for c in snapshot['collaboration']['channels'] if c['id'] == entry['id'])['repository'] is None
        assert handoff['issue']['url'] == ISSUE['url'] and handoff['acceptance'] == 'awaiting_receiver'
        assert snapshot['requests'] == []


@pytest.mark.parametrize('repository', [None, ''])
def test_project_channel_without_a_repository_is_not_registered(tmp_path, repository):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        register_roles(manager, tmp_path)
        receiving = channel('mono-lead')
        receiving['repository'] = repository
        with pytest.raises(ManagementError) as invalid:
            manager.collaborate(OWNER, 'register_channels', {'channels': [receiving]})
        assert invalid.value.code == 'invalid_change'
        assert manager.read_snapshot(OWNER)['collaboration']['channels'] == []


def test_repositoryless_entry_does_not_allow_an_issue_from_another_project_repository(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
        entry['repository'] = None
        sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving]})
        with pytest.raises(ManagementError) as conflict:
            manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': 'https://github.com/example-user/another-project/issues/1'})
        assert conflict.value.code == 'binding_conflict'
        snapshot = manager.read_snapshot(OWNER)
        assert snapshot['collaboration']['handoffs'] == [] and snapshot['requests'] == []


def test_one_repositoryless_steward_channel_dispatches_distinct_projects_in_the_same_group(tmp_path):
    from ghost_hermes_pm import ManagementError
    other_issue = {**ISSUE, 'url': 'https://github.com/example-user/other/issues/16', 'title': 'Fix the other project'}
    class ProjectIssues:
        def read_issue(self, url):
            return dict({ISSUE['url']: ISSUE, other_issue['url']: other_issue}[url])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=ProjectIssues()) as manager:
        register_roles(manager, tmp_path)
        other = registration(make_repo(tmp_path / 'other'), 'other', 'other-lead')
        other['profile']['identity_ref'] = 'fixture:other'
        manager.apply_directory_change(OWNER, 2, other)
        entry, sending, lead, other_lead = channel('steward', 'entry'), channel('steward'), channel('mono-lead'), channel('other-lead')
        entry['repository'] = None
        sending.update(project_id=None, repository=None)
        other_lead.update(project_id='other', repository='example-user/other')
        sending['bot_sources'].extend([
            {'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']},
            {'profile_id': 'other-lead', 'open_id': 'other-seen-steward', 'tenant_key': 'other-tenant', 'native_ids': ['other-user-steward']}])
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, lead, other_lead]})
        for receiving, issue, identity_ref in [(lead, ISSUE, LEAD.subject), (other_lead, other_issue, 'fixture:other')]:
            handoff = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': receiving['profile_id'],
                'source_anchor': source(entry, message_id='om_goal_' + receiving['profile_id']), 'issue_url': issue['url']})
            packet = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': handoff['id']})
            assert packet['chat_id'] == 'oc_project' and handoff['sender_channel_id'] == sending['id']
            message_id = 'om_scope_' + receiving['profile_id']
            manager.collaborate(STEWARD, 'record_delivery', {'handoff_id': handoff['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': message_id, 'chat_id': 'oc_project'}})
            accepted = manager.collaborate(VerifiedIdentity(identity_ref, 'native-collaboration-ingress'), 'ingest', {
                'channel_id': receiving['id'], 'source_anchor': source(receiving, 'bot', message_id), 'text': packet['text']})
            task = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == accepted['task_request_id'])
            assert task['profile_id'] == receiving['profile_id'] and task['project_id'] == receiving['project_id']
            assert task['accepted_scope']['url'] == issue['url'] and task['actor_provenance']['actor']['subject'] == identity_ref
        with pytest.raises(ManagementError) as conflict:
            manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'other-lead',
                'source_anchor': source(entry, message_id='om_wrong_project'), 'issue_url': ISSUE['url']})
        assert conflict.value.code == 'binding_conflict'
        for invalid_origin in [source(lead, message_id='om_lead_owner_goal'), {**source(sending, message_id='om_foreign_owner'), 'sender_open_id': 'foreign-owner'}]:
            with pytest.raises(ManagementError) as conflict:
                manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead',
                    'source_anchor': invalid_origin, 'issue_url': ISSUE['url']})
            assert conflict.value.code == 'binding_conflict'
        snapshot = manager.read_snapshot(OWNER)
        assert len(snapshot['requests']) == 2 and len(snapshot['collaboration']['handoffs']) == 2
        assert len([c for c in snapshot['collaboration']['channels'] if c['profile_id'] == 'steward' and c['chat_id'] == 'oc_project']) == 1


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
@pytest.mark.parametrize('owner_group', ['entry', 'project'])
async def test_real_sdk_owner_goal_entry_preserves_original_source_auth_and_budget(tmp_path, authorized, budget, owner_group):
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from ghost_hermes_pm.messages import FeishuEntry
    entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
    entry['repository'] = None
    sending.update(project_id=None, repository=None)
    sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
    if owner_group == 'project':
        entry = sending
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, receiving] if owner_group == 'project' else [entry, sending, receiving]})
        raw = P2ImMessageReceiveV1({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1', 'app_id': entry['app_id'], 'tenant_key': entry['transport_tenant_key']},
            'event': {'sender': {'sender_type': 'user', 'tenant_key': entry['owner_tenant_key'], 'sender_id': {'open_id': entry['owner_open_id'], 'user_id': 'owner-native'}},
                'message': {'message_id': 'om_native_owner_goal', 'chat_id': entry['chat_id'], 'chat_type': 'group', 'message_type': 'text',
                    'content': json.dumps({'text': '@_user_1 项目 mono ' + ISSUE['url']}),
                    'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': entry['recipient_tenant_key'], 'id': {'open_id': entry['recipient_open_id']}}]}}})
        original = NS(platform='feishu', user_id='owner-native', user_id_alt=None, chat_id=entry['chat_id'], is_bot=False, message_id='om_native_owner_goal')
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
            accepted = manager.collaborate(INGRESS, 'ingest', {'channel_id': receiving['id'], 'source_anchor': source(receiving, 'bot', 'om_created_1'), 'text': handoffs[0]['segments'][0]['text']})
            assert accepted['acceptance'] == 'accepted'
            progress = manager.collaborate(LEAD, 'report_progress', {'handoff_id': accepted['id']})
            packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': progress['id']})
            manager.collaborate(LEAD, 'record_delivery', {'handoff_id': progress['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_native_progress', 'chat_id': 'oc_project'}})
            observed = source(sending, 'bot', 'om_native_progress')
            observed.update(tenant_key='lead-tenant', sender_open_id='lead-seen-steward')
            received = manager.collaborate(VerifiedIdentity(STEWARD.subject, 'native-collaboration-ingress'), 'ingest', {'channel_id': sending['id'], 'source_anchor': observed, 'text': packet['text']})
            summary = manager.collaborate(STEWARD, 'publish_owner_summary', {'handoff_id': received['id']})
            owner_packet = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': summary['id']})
            assert owner_packet['path'] == 'reply' and owner_packet['chat_id'] == original.chat_id
            assert owner_packet['reply_to'] == original.message_id
            assert len(manager.read_snapshot(OWNER)['requests']) == 1


CHILD = VerifiedIdentity('fixture:child', 'participant')
CHILD_INGRESS = VerifiedIdentity('fixture:child', 'native-collaboration-ingress')


def accepted_parent(manager, root, lead_capability='development'):
    register_roles(manager, root, lead_capability)
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


def test_project_progress_returns_to_the_original_group_when_steward_coordinates_other_groups(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        parent_id, _ = accepted_parent(manager, tmp_path)
        other_group = channel('steward')
        other_group.update(id='steward-other-group', chat_id='oc_other_group', project_id=None, repository=None)
        manager.collaborate(OWNER, 'register_channels', {'channels': [other_group]})
        progress = manager.collaborate(LEAD, 'report_progress', {'handoff_id': parent_id})
        packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': progress['id']})
        assert progress['target_channel_id'] == 'steward-project' and packet['chat_id'] == 'oc_project'
        assert packet['mention_open_id'] == 'steward-seen-mono-lead'
        manager.collaborate(LEAD, 'record_delivery', {'handoff_id': progress['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_project_progress', 'chat_id': 'oc_project'}})
        steward_ingress = VerifiedIdentity(STEWARD.subject, 'native-collaboration-ingress')
        with pytest.raises(ManagementError):
            manager.collaborate(steward_ingress, 'ingest', {'channel_id': other_group['id'], 'source_anchor': source(other_group, 'bot', 'om_project_progress'), 'text': packet['text']})
        receiving = next(c for c in manager.read_snapshot(OWNER)['collaboration']['channels'] if c['id'] == progress['target_channel_id'])
        observed = source(receiving, 'bot', 'om_project_progress')
        observed.update(tenant_key='lead-tenant', sender_open_id='lead-seen-steward')
        received = manager.collaborate(steward_ingress, 'ingest', {'channel_id': receiving['id'], 'source_anchor': observed, 'text': packet['text']})
        owner_summary = manager.collaborate(STEWARD, 'publish_owner_summary', {'handoff_id': received['id']})
        owner_packet = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': owner_summary['id']})
        assert owner_packet['chat_id'] == 'oc_entry' and owner_packet['reply_to'] == 'om_owner_goal'
        assert len(manager.read_snapshot(OWNER)['requests']) == 1


@pytest.mark.parametrize('profile_id', ['mono-lead', 'child'])
def test_project_responsible_roles_cannot_replace_their_binding_with_a_shared_coordinator_channel(tmp_path, profile_id):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        accepted_parent(manager, tmp_path)
        before = manager.read_snapshot(OWNER)['collaboration']['channels']
        replacement = {k: v for k, v in next(c for c in before if c['profile_id'] == profile_id).items() if k != 'profile_binding'}
        replacement.update(project_id=None, repository=None)
        with pytest.raises(ManagementError) as invalid:
            manager.collaborate(OWNER, 'register_channels', {'channels': [replacement]})
        assert invalid.value.code == 'invalid_change'
        assert manager.read_snapshot(OWNER)['collaboration']['channels'] == before


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
        with pytest.raises(Exception):
            manager.collaborate(CHILD, 'report_result', {'request_id': task_id})
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
        summary = manager.collaborate(LEAD, 'report_summary', {'handoff_id': parent_id})
        assert summary['kind'] == 'summary' and summary['target_profile_id'] == 'steward'
        summary_packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': summary['id']})
        assert summary_packet['mention_open_id'] == 'steward-seen-mono-lead'
        assert '待集成' in summary_packet['text'] and h['issue']['url'] in summary_packet['text']
        manager.collaborate(LEAD, 'record_delivery', {'handoff_id': summary['id'], 'uuid': summary_packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_lead_summary', 'chat_id': 'oc_project'}})
        steward_channel = next(c for c in channels if c['profile_id'] == 'steward' and c['group_kind'] == 'project')
        steward_source = source(steward_channel, 'bot', 'om_lead_summary')
        steward_source.update(tenant_key='lead-tenant', sender_open_id='lead-seen-steward')
        steward_ingress = VerifiedIdentity(STEWARD.subject, 'native-collaboration-ingress')
        received_summary = manager.collaborate(steward_ingress, 'ingest', {'channel_id': steward_channel['id'], 'source_anchor': steward_source, 'text': summary_packet['text']})
        owner_summary = manager.collaborate(STEWARD, 'publish_owner_summary', {'handoff_id': received_summary['id']})
        packet = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': owner_summary['id']})
        assert packet['path'] == 'reply' and packet['reply_to'] == 'om_owner_goal'
        assert packet['chat_id'] == 'oc_entry' and packet['mention_open_id'] is None
        assert '全局验证' in packet['text'] and owner_summary['whole_project_complete'] is False
        manager.collaborate(STEWARD, 'record_delivery', {'handoff_id': owner_summary['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_owner_summary', 'chat_id': 'oc_entry'}})
        assert manager.collaborate(STEWARD, 'publish_owner_summary', {'handoff_id': received_summary['id']})['duplicate']
        assert len(manager.read_snapshot(OWNER)['requests']) == before


def test_owner_direct_child_issue_replies_to_owner_and_publicly_synchronizes_parent(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        parent_id, child = accepted_parent(manager, tmp_path)
        direct = source(child, message_id='om_direct_child')
        task = manager.accept_request(OWNER, 'child-project', 'child', direct, ISSUE)['request']
        manager.publish_request_message(OWNER, task['id'], 'confirmation', '已受理本人明确子 Issue')
        answer = manager.claim_delivery(OWNER, task['id'])
        assert answer['reply_to'] == 'om_direct_child' and answer['mention_open_id'] == child['owner_open_id']
        sync = next(h for h in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if h.get('direct_task_id') == task['id'])
        assert sync['kind'] == 'progress' and sync['target_profile_id'] == 'mono-lead'
        assert sync['parent_handoff_id'] == parent_id and sync['owner_origin']['subject'] == OWNER.subject
        assert task['actor_provenance']['actor']['subject'] == OWNER.subject
        packet = manager.collaborate(CHILD, 'claim_delivery', {'handoff_id': sync['id']})
        assert packet['mention_open_id'] == 'lead-seen-child'
        manager.collaborate(CHILD, 'record_delivery', {'handoff_id': sync['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_direct_sync', 'chat_id': 'oc_project'}})
        lead_channel = next(c for c in manager.read_snapshot(OWNER)['collaboration']['channels'] if c['profile_id'] == 'mono-lead')
        observed = source(lead_channel, 'bot', 'om_direct_sync')
        observed.update(tenant_key='child-tenant', sender_open_id='child-seen-lead')
        before = len(manager.read_snapshot(OWNER)['requests'])
        result = manager.collaborate(INGRESS, 'ingest', {'channel_id': lead_channel['id'], 'source_anchor': observed, 'text': packet['text']})
        assert result['acceptance'] == 'accepted' and len(manager.read_snapshot(OWNER)['requests']) == before
        assert manager.accept_request(OWNER, 'child-project', 'child', direct, ISSUE)['duplicate']
        assert len([h for h in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if h.get('direct_task_id') == task['id']]) == 1


def test_scoped_transfer_keeps_child_project_profile_and_stale_handoffs_blocked_with_actor_audit(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        parent_id, child = accepted_parent(manager, tmp_path)
        delegated = manager.collaborate(LEAD, 'delegate_issue', {'parent_handoff_id': parent_id, 'target_profile_id': 'child', 'issue_url': ISSUE['url']})
        other = {'project': {'id': 'other', 'name': 'Other mono', 'repo_path': str(make_repo(tmp_path / 'other'))},
            'profile': {'id': 'other-lead', 'native_profile': 'other-lead', 'identity_ref': 'fixture:other', 'role': 'project_lead', 'capability': 'non_development', 'project_id': 'other'}}
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], other)
        old = next(p for p in manager.read_snapshot(OWNER)['profiles'] if p['id'] == 'child')
        moved = {k: old[k] for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')}
        moved['parent_profile_id'] = 'other-lead'
        version = manager.read_snapshot(OWNER)['version']
        with pytest.raises(ManagementError): manager.apply_directory_change(LEAD, version, {'profile': moved})
        manager.apply_directory_change(STEWARD, version, {'profile': moved})
        updated = next(p for p in manager.read_snapshot(OWNER)['profiles'] if p['id'] == 'child')
        assert all(updated[k] == old[k] for k in ('project_id', 'native_profile', 'identity_ref', 'connection_refs'))
        with pytest.raises(ManagementError) as stale:
            manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': delegated['id']})
        assert stale.value.code == 'binding_conflict'
        assert 'child' not in {p['id'] for p in manager.read_snapshot(LEAD)['profiles']}
        audit = manager.read_snapshot(STEWARD)['directory_audit'][-1]
        assert audit['actor'] == {'subject': STEWARD.subject, 'source': STEWARD.source, 'profile_id': 'steward'}
        assert audit['changes'][0]['before']['parent_profile_id'] == 'mono-lead'
        assert audit['changes'][0]['after']['parent_profile_id'] == 'other-lead'
        assert audit['version'] == version + 1
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        assert manager.read_snapshot(STEWARD)['directory_audit'][-1] == audit


def test_dashboard_collaboration_uses_same_owner_goal_and_visible_tree_without_actor_override(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        independent = {'id': 'wiki', 'native_profile': 'wiki', 'identity_ref': 'fixture:wiki', 'role': 'independent', 'capability': 'non_development', 'project_id': None}
        manager.apply_directory_change(OWNER, 2, {'profile': independent})
        entry, sending, lead = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
        sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
        with ManagementServer(manager, {'owner': OWNER, 'lead': LEAD}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            app = FastAPI(); app.include_router(create_router(lambda request: client)); browser = TestClient(app)
            assert browser.post('/collaboration', json={'action': 'register_channels', 'details': {'channels': [entry, sending, lead]}}).status_code == 200
            goal = {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']}
            assert browser.post('/collaboration', json={'action': 'project_goal', 'details': goal, 'actor': STEWARD.subject}).status_code == 422
            response = browser.post('/collaboration', json={'action': 'project_goal', 'details': goal})
            assert response.status_code == 200
            state = browser.get('/snapshot').json()
            assert state == client.read_snapshot()
            assert state['collaboration']['handoffs'][0]['acceptance'] == 'awaiting_receiver'
            assert {p['id'] for p in state['profiles']} == {'steward', 'mono-lead', 'wiki'}
            assert next(p for p in state['profiles'] if p['id'] == 'wiki')['parent_profile_id'] is None
            assert state['requests'] == []
            participant = ManagementClient(tmp_path / 'state', 'lead')
            with pytest.raises(Exception): participant.collaborate('project_goal', goal)
            with pytest.raises(Exception): participant.collaborate('register_channels', {'channels': [entry]})
            with pytest.raises(Exception): participant.collaborate('project_goal', {**goal, 'target_profile_id': 'wiki'})


def test_changing_registered_native_channel_does_not_retarget_frozen_public_work(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        register_roles(manager, tmp_path)
        entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
        sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving]})
        h = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']})
        receiving['recipient_open_id'] = 'new-bot-identity'
        manager.collaborate(OWNER, 'register_channels', {'channels': [receiving]})
        with pytest.raises(ManagementError) as stale:
            manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': h['id']})
        assert stale.value.code == 'binding_conflict'
        assert manager.read_snapshot(OWNER)['requests'] == []
        assert manager.read_snapshot(OWNER)['collaboration']['handoffs'][0]['channel_binding'] == 'unverified'


@pytest.mark.asyncio
async def test_multipart_native_work_preserves_full_text_first_source_and_creates_only_one_task(tmp_path):
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse
    from ghost_hermes_pm.messages import FeishuEntry
    class LongIssueSource:
        def read_issue(self, url): return {**ISSUE, 'body': 'Original required scope. ' * 140 + ' \n'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=LongIssueSource()) as manager:
        register_roles(manager, tmp_path)
        entry, sending, receiving = channel('steward', 'entry'), channel('steward'), channel('mono-lead')
        sending['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [entry, sending, receiving]})
        goal = manager.collaborate(OWNER, 'project_goal', {'sender_profile_id': 'steward', 'target_profile_id': 'mono-lead', 'source_anchor': source(entry), 'issue_url': ISSUE['url']})
        assert len(goal['segments']) > 1
        native = Client.builder().app_id(receiving['app_id']).app_secret('synthetic-unused-secret').build()
        native.request = lambda request: NS(code=0, raw=NS(content=b'{"code":0,"bot":{"open_id":"ou_mono-lead","activate_status":2}}'))
        native.im.v1.message.reply = lambda request: ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_multipart_ack', 'chat_id': 'oc_project'}})
        adapter = object()
        intake = FeishuEntry(lambda: manager, OWNER.subject, {'enabled': True, 'verification_ref': 'fixture:multipart'}, lambda url: None, collaboration_identity_ref=LEAD.subject)
        intake.attach_transport(adapter, NativeFeishuTransport(native))
        gateway = NS(_intake_adapter_for=lambda source: adapter, _is_user_authorized_for_source=lambda source: True, _admit_bot_message_for_source=lambda source: True)
        async def receive(text, message_id):
            raw = P2ImMessageReceiveV1({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1', 'app_id': receiving['app_id'], 'tenant_key': receiving['transport_tenant_key']},
                'event': {'sender': {'sender_type': 'bot', 'tenant_key': 'steward-tenant', 'sender_id': {'open_id': 'steward-seen-mono-lead', 'user_id': 'steward-user-mono-lead'}},
                    'message': {'message_id': message_id, 'chat_id': 'oc_project', 'chat_type': 'group', 'message_type': 'text', 'content': json.dumps({'text': '@_user_1 ' + text}),
                        'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': receiving['recipient_tenant_key'], 'id': {'open_id': receiving['recipient_open_id']}}]}}})
            original = NS(platform='feishu', user_id='steward-user-mono-lead', user_id_alt=None, chat_id='oc_project', is_bot=True, message_id=message_id)
            return await intake.receive(NS(source=original, raw_message=raw, message_id=message_id), gateway)
        for number, segment in enumerate(goal['segments'], 1):
            packet = manager.collaborate(STEWARD, 'claim_delivery', {'handoff_id': goal['id']})
            manager.collaborate(STEWARD, 'record_delivery', {'handoff_id': goal['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_part_' + str(number), 'chat_id': 'oc_project'}})
            assert await receive(segment['text'], 'om_part_' + str(number)) == {'action': 'skip'}
            if number == 1:
                assert manager.read_snapshot(OWNER)['requests'] == []
                assert await receive(segment['text'], 'om_duplicate_first') == {'action': 'skip'}
                received = manager.read_snapshot(OWNER)['collaboration']['handoffs'][0]
                assert received['received_parts']['1']['message_id'] == 'om_part_1'
        task = manager.read_snapshot(OWNER)['requests'][0]
        assert task['accepted_scope']['body'].endswith(' \n') and task['source_anchor']['message_id'] == 'om_part_1'
        assert await receive(goal['segments'][-1]['text'], 'om_duplicate_last') == {'action': 'skip'}
        assert len(manager.read_snapshot(OWNER)['requests']) == 1


def test_direct_child_delivery_answers_original_owner_and_synchronizes_result_to_parent(tmp_path):
    from test_task_execution import adapter_for, prepare_fixture
    from test_task_control import TURN
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource(), codex_adapter=adapter_for(tmp_path)) as manager:
        parent_id, child = accepted_parent(manager, tmp_path)
        current = next(p for p in manager.read_snapshot(OWNER)['profiles'] if p['id'] == 'child')
        profile = {k: current[k] for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id')}
        profile['connection_refs'] = {'codex': 'local:fixture-stdio'}
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': profile})
        task = manager.accept_request(OWNER, 'child-project', 'child', source(child, message_id='om_direct_delivery'), ISSUE)['request']
        manager.publish_request_message(OWNER, task['id'], 'confirmation', '已受理本人直接 Issue')
        ack = manager.claim_delivery(CHILD, task['id'])
        manager.record_delivery(CHILD, task['id'], ack['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_direct_ack'})
        prepare_fixture(manager, task['id'], tmp_path / 'child')
        manager.start_task(CHILD, task['id'])
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'pytest-direct-child', 'command': 'python -m pytest -q', 'cwd': str(tmp_path / 'child'), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
        manager.record_task_delivery(CHILD, task['id'], {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-direct-child']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []})
        result = manager.collaborate(CHILD, 'report_result', {'request_id': task['id']})
        assert result['target_profile_id'] == 'mono-lead' and result['parent_handoff_id'] == parent_id
        assert result['owner_origin']['source_anchor']['message_id'] == 'om_direct_delivery'
        before = len(manager.read_snapshot(OWNER)['requests'])
        packet = manager.collaborate(CHILD, 'claim_delivery', {'handoff_id': result['id']})
        manager.collaborate(CHILD, 'record_delivery', {'handoff_id': result['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_direct_result', 'chat_id': 'oc_project'}})
        lead = next(c for c in manager.read_snapshot(OWNER)['collaboration']['channels'] if c['profile_id'] == 'mono-lead')
        received = source(lead, 'bot', 'om_direct_result'); received.update(tenant_key='child-tenant', sender_open_id='child-seen-lead')
        manager.collaborate(INGRESS, 'ingest', {'channel_id': lead['id'], 'source_anchor': received, 'text': packet['text']})
        publications = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == task['id'])['outbox']
        assert any(p['kind'] == 'result' for p in publications)
        assert len(manager.read_snapshot(OWNER)['requests']) == before
        assert manager.collaborate(CHILD, 'report_result', {'request_id': task['id']})['duplicate']
        with pytest.raises(Exception): manager.collaborate(LEAD, 'report_result', {'request_id': task['id']})



def test_non_development_project_lead_can_coordinate_explicit_child_without_own_execution(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource()) as manager:
        parent_id, child_channel = accepted_parent(manager, tmp_path, lead_capability='non_development')
        assert manager.read_snapshot(OWNER)['requests'] == []
        delegated = manager.collaborate(LEAD, 'delegate_issue', {'parent_handoff_id': parent_id, 'target_profile_id': 'child', 'issue_url': ISSUE['url']})
        packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': delegated['id']})
        manager.collaborate(LEAD, 'record_delivery', {'handoff_id': delegated['id'], 'uuid': packet['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_nondev_child', 'chat_id': 'oc_project'}})
        observed = source(child_channel, 'bot', 'om_nondev_child'); observed.update(tenant_key='lead-tenant', sender_open_id='lead-seen-child')
        accepted = manager.collaborate(CHILD_INGRESS, 'ingest', {'channel_id': child_channel['id'], 'source_anchor': observed, 'text': packet['text']})
        assert accepted['acceptance'] == 'accepted'
        tasks = manager.read_snapshot(OWNER)['requests']
        assert len(tasks) == 1 and tasks[0]['profile_id'] == 'child'
        assert manager.read_snapshot(LEAD)['execution'] == 'not_enabled'
        with pytest.raises(Exception): manager.collaborate(CHILD, 'delegate_issue', {'parent_handoff_id': parent_id, 'target_profile_id': 'child', 'issue_url': ISSUE['url']})
