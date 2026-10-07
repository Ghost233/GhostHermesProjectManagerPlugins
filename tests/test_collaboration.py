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
