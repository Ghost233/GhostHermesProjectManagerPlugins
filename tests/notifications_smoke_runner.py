"""Actual fixed Gateway/adapter/Lark pipeline in the staged artificial runtime."""
import os
import sys
from pathlib import Path

scratch = Path(sys.argv[1]).resolve()
protected_home = Path.home()
for name in ('os-home', 'os-state', 'os-config'):
    (scratch / name).mkdir()
os.environ['HOME'] = str(scratch / 'os-home')
os.environ['XDG_STATE_HOME'] = str(scratch / 'os-state')
os.environ['XDG_CONFIG_HOME'] = str(scratch / 'os-config')
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
os.environ['HERMES_FEISHU_TEXT_BATCH_DELAY_SECONDS'] = '0.01'
os.environ['HERMES_FEISHU_TEXT_BATCH_SPLIT_DELAY_SECONDS'] = '0.01'
if os.environ.get('HERMES_TEST_OWNED_UNLOAD_STAGE') in {'failure_replay', 'failure_optional'}:
    os.environ['HERMES_FEISHU_DEDUP_CACHE_SIZE'] = '32'
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(protected_home / p) for p in ('.hermes', '.config', '.local/state/hermes')) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Owned smoke refused real configuration/credential files.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Owned smoke refused external HTTP.')
    if event == 'import' and args[0] in {'hermes_cli.main', 'run_agent'}:
        raise RuntimeError('Owned smoke refused launch/model imports.')
sys.addaudithook(audit)

import asyncio
import json
import subprocess
import types
import yaml
from lark_oapi import Client
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse, CreateMessageResponse
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.profile_routing import ProfileRoute
from gateway.session_identity import identity_of
from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from hermes_cli.profiles import get_profile_dir
from hermes_cli.plugins import get_plugin_manager

home, state = scratch / 'home', scratch / 'state'
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
sys.path.insert(0, str(scratch / 'collaboration-fixtures'))
from test_collaboration import channel, register_roles, IssueSource, STEWARD, OWNER
from ghost_hermes_pm.transport import ManagementClient
from test_task_execution import adapter_for
from test_task_control import TURN
from test_questions import question_adapter, emit, user_question
import time
clock = [10000.0]
time.time = lambda: clock[0]
from ghost_hermes_pm.queue import workspace
from tools.registry import registry
channels = [channel('steward', 'entry'), channel('steward'), channel('mono-lead'), channel('child')]
channels[2]['bot_sources'].append({'profile_id': 'child', 'open_id': 'child-seen-lead', 'tenant_key': 'child-tenant', 'native_ids': ['child-user-lead']})
channels[3]['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-child', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-child']})
channels[1]['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-steward', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-user-steward']})
bindings = [{**{k: c[k] for k in ('app_id', 'recipient_open_id', 'recipient_tenant_key', 'transport_tenant_key', 'chat_id', 'profile_id', 'project_id', 'repository', 'verification_ref')},
    'owner_open_id': c['owner_open_id'], 'sender_tenant_key': c['owner_tenant_key'], 'owner_native_ids': ['owner-native']} for c in channels]
bots = [{'profile_id': b['profile_id'], 'identity_ref': 'fixture:' + ('lead' if b['profile_id'] == 'mono-lead' else b['profile_id']),
    'app_id': c['app_id'], 'tenant_key': b['tenant_key'], 'open_id': b['open_id'], 'native_ids': b['native_ids']} for c in channels for b in c['bot_sources']]
settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': OWNER.subject,
    'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN', 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN',
    'participant_entries': [{'identity_ref': 'fixture:lead', 'credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}, {'identity_ref': 'fixture:child', 'credential_ref': 'native:HERMES_FIXTURE_CHILD_TOKEN'}], 'collaboration_identity_ref': STEWARD.subject,
    'feishu_intake': {'enabled': True, 'verification_ref': 'fixture:owned-role-source', 'bindings': bindings, 'registered_bots': bots}}
os.environ['HERMES_FIXTURE_CHILD_TOKEN'] = 'synthetic-child-credential'
(home / '.env').write_text('HERMES_PM_FEISHU_ALLOWED_USERS=' + ','.join(sorted({'owner-native'} | {i for c in channels for b in c['bot_sources'] for i in b['native_ids']})) + '\n')
for profile in ('steward', 'mono-lead', 'child'):
    get_profile_dir(profile).mkdir(parents=True, exist_ok=True)
    (get_profile_dir(profile) / '.env').write_text('')
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
import ghost_hermes_pm.github as external_issue
external_issue.GitHubDeliverySource.read_issue = lambda self, url: IssueSource().read_issue(url)
import ghost_hermes_pm.codex as original_codex
original_codex.configured_adapter = lambda config, directory: question_adapter(scratch)
from native_fixture_boundary import install
install(home / 'plugins' / 'ghost-hermes-pm', {
    'github': lambda module: setattr(module.GitHubDeliverySource, 'read_issue', lambda self, url: IssueSource().read_issue(url)),
    'codex': lambda module: setattr(module, 'configured_adapter', lambda config, directory: question_adapter(scratch))})
plugins = get_plugin_manager()
plugins.discover_and_load()

class FixtureRunner(GatewayRunner):
    async def wait_for_shutdown(self): await self.stopped.wait()
    def _is_user_authorized_for_source(self, source, **kwargs):
        self.auth_sources.append(source)
        return self.authorized and super()._is_user_authorized_for_source(source, **kwargs)
    def _admit_bot_message_for_source(self, source):
        self.budget_sources.append(source)
        return self.budget and super()._admit_bot_message_for_source(source)
    async def _handle_message(self, event): self.native.append(event)
    async def _handle_active_session_busy_message(self, event, key): self.native.append(event); return True
    async def _handle_adapter_fatal_error(self, adapter): raise RuntimeError('Unexpected adapter failure')

def raw(c, text, message_id, bot=False, sender_profile='steward'):
    sender = next((b for b in c['bot_sources'] if b['profile_id'] == sender_profile), None) if bot else None
    return P2ImMessageReceiveV1({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1', 'app_id': c['app_id'], 'tenant_key': c['transport_tenant_key']},
        'event': {'sender': {'sender_type': 'bot' if bot else 'user', 'tenant_key': sender['tenant_key'] if bot else c['owner_tenant_key'],
            'sender_id': {'open_id': sender['open_id'] if bot else c['owner_open_id'], 'user_id': sender['native_ids'][0] if bot else 'owner-native'}},
            'message': {'message_id': message_id, 'chat_id': c['chat_id'], 'chat_type': 'group', 'message_type': 'text',
                'content': json.dumps({'text': '@_user_1 ' + text}), 'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': c['recipient_tenant_key'], 'id': {'open_id': c['recipient_open_id']}}]}}})

async def main():
    runners, adapters, clients, created, replies = [], [], [], [], []
    delivered_chats = {}
    for profile in ('steward', 'mono-lead', 'child'):
        own = [c for c in channels if c['profile_id'] == profile]
        runner = object.__new__(FixtureRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner.config.profile_routes = [ProfileRoute(name=profile + c['id'], platform='hermes_feishu_pm', profile=profile, chat_id=c['chat_id']) for c in own]
        runner._primary_profile_name = 'default'
        runner.adapters, runner._profile_adapters = {}, {}
        runner.session_store, runner.pairing_store, runner.pairing_stores = None, None, {}
        runner._busy_text_mode, runner._human_delay = 'steer', None
        runner._bot_loop_guard = BotLoopGuard(settings=lambda: BotLoopGuardSettings(max_events=20))
        runner.auth_sources, runner.budget_sources, runner.native = [], [], []
        runner.authorized = runner.budget = True
        runner.stopped = asyncio.Event()
        platform = Platform('hermes_feishu_pm')
        adapter = runner._create_adapter(platform, PlatformConfig(enabled=True, extra={'app_id': own[0]['app_id'], 'app_secret': 'synthetic-unused-secret', 'require_mention': False, 'default_group_policy': 'open', 'allow_bots': 'all'}))
        assert adapter is not None
        runner.adapters[platform] = adapter
        runner._wire_adapter_handlers(adapter)
        async def chat_info(chat_id): return {'name': 'Synthetic role chat', 'type': 'group'}
        adapter.get_chat_info = chat_info
        native = Client.builder().app_id(own[0]['app_id']).app_secret('synthetic-unused-secret').build()
        native.request = lambda request, c=own[0]: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(content=json.dumps({'code': 0, 'bot': {'open_id': c['recipient_open_id'], 'activate_status': 2}}).encode()))
        def create(request, profile=profile):
            created.append((profile, request))
            message_id = 'om_cross_' + str(len(created))
            delivered_chats[message_id] = request.request_body.receive_id
            return CreateMessageResponse({'code': 0, 'data': {'message_id': message_id, 'chat_id': request.request_body.receive_id}})
        def reply(request, profile=profile):
            replies.append((profile, request))
            message_id = 'om_ack_' + str(len(replies))
            chat_id = delivered_chats.get(request.message_id, 'oc_entry' if request.message_id.startswith('om_sdk_owner') or request.message_id.startswith('om_owner_answer') else 'oc_project')
            delivered_chats[message_id] = chat_id
            return ReplyMessageResponse({'code': 0, 'data': {'message_id': message_id, 'chat_id': chat_id, 'parent_id': request.message_id}})
        native.im.v1.message.create, native.im.v1.message.reply = create, reply
        plugins.get_platform_handler_factories('hermes_feishu_pm')[0][0](native, adapter)
        runners.append(runner); adapters.append(adapter); clients.append(native)
    await plugins.ainvoke_hook('pre_gateway_dispatch', event=object(), gateway=runners[0])
    owner = ManagementClient(state, 'synthetic-owner-credential')
    repo = scratch / 'mono'; repo.mkdir(); subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    owner.apply_directory_change(0, {'enable_profile': 'mono-lead', 'project': {'id': 'mono', 'name': 'Synthetic mono', 'repo_path': str(repo)}, 'profile': {'id': 'mono-lead', 'native_profile': 'mono-lead', 'identity_ref': 'fixture:lead', 'role': 'project_lead', 'capability': 'development', 'project_id': 'mono', 'connection_refs': {'codex': 'local:fixture-stdio'}}})
    owner.apply_directory_change(1, {'enable_profile': 'steward', 'profile': {'id': 'steward', 'native_profile': 'steward', 'identity_ref': STEWARD.subject, 'role': 'steward', 'capability': 'non_development', 'project_id': None}})
    child_repo = scratch / 'child-repo'; child_repo.mkdir(); subprocess.run(['git', 'init', '-q', str(child_repo)], check=True)
    owner.apply_directory_change(2, {'enable_profile': 'child', 'project': {'id': 'child-project', 'name': 'Explicit SDK child', 'repo_path': str(child_repo)}, 'profile': {'id': 'child', 'native_profile': 'child', 'identity_ref': 'fixture:child', 'role': 'subproject_lead', 'capability': 'development', 'project_id': 'child-project', 'parent_profile_id': 'mono-lead', 'connection_refs': {'codex': 'local:fixture-stdio'}}})
    owner.collaborate('register_channels', {'channels': channels})
    incoming = raw(channels[0], '项目 mono https://github.com/Ghost233/fixture/issues/15', 'om_sdk_owner_goal')
    runners[0].authorized = False
    await adapters[0]._handle_message_event_data(incoming)
    assert not owner.read_snapshot()['collaboration']['handoffs'] and not created
    runners[0].authorized = True; runners[0].budget = False
    await adapters[0]._handle_message_event_data(raw(channels[0], '项目 mono https://github.com/Ghost233/fixture/issues/15', 'om_sdk_budget_denied'))
    assert not owner.read_snapshot()['collaboration']['handoffs'] and not created
    runners[0].budget = True
    await adapters[0]._handle_message_event_data(raw(channels[0], '项目 mono https://github.com/Ghost233/fixture/issues/15', 'om_sdk_owner_goal_valid'))
    h = owner.read_snapshot()['collaboration']['handoffs'][0]
    assert len(created) == 1 and h['acceptance'] == 'awaiting_receiver' and owner.read_snapshot()['requests'] == []
    request = created[0][1]
    post = json.loads(request.request_body.content)['zh_cn']['content'][0]
    assert post[0] == {'tag': 'at', 'user_id': 'lead-seen-steward'}
    original_bot = raw(channels[2], h['segments'][0]['text'], 'om_cross_1', bot=True)
    await adapters[1]._handle_message_event_data(original_bot)
    snapshot = owner.read_snapshot()
    assert snapshot['collaboration']['handoffs'][0]['acceptance'] == 'accepted', snapshot
    assert snapshot['requests'][0]['task_start_anchor']['message_id'] == 'om_ack_1'
    assert snapshot['requests'][0]['actor_provenance']['actor']['subject'] == 'fixture:lead'
    assert any(s.is_bot is True for s in runners[1].auth_sources) and any(s.is_bot is True for s in runners[1].budget_sources)
    before = len(runners[1].budget_sources)
    adapters[1]._committed.clear()
    await adapters[1]._handle_message_event_data(original_bot)
    assert len(runners[1].budget_sources) == before and len(owner.read_snapshot()['requests']) == 1
    task = owner.read_snapshot()['requests'][0]
    current = workspace(owner.read_snapshot()['projects'][0]['repo'])
    owner.prepare_task(task['id'], {'branch': current['branch'], 'commit': current['head'], 'workspace_digest': current['source_digest'], 'dependencies': [], 'issue_updated_at': h['issue']['updated_at']})
    assert owner.start_task(task['id'])['status'] == 'running'
    async def wait_for(predicate):
        for _ in range(100):
            snapshot = owner.read_snapshot()
            if predicate(snapshot): return snapshot
            await asyncio.sleep(0.1)
        raise AssertionError('Native notification supervision did not produce its verified public outcome')
    await wait_for(lambda s: task['id'] in s['notifications']['health']['sources'])
    clock[0] += 899
    await asyncio.sleep(5.2)
    assert not any(e['kind'] == 'summary' for e in owner.read_snapshot()['notifications']['events'])
    clock[0] += 1
    summarized = await wait_for(lambda s: any(e['kind'] == 'summary' and e['delivery'] == 'delivered' for e in s['notifications']['events']))
    summary = next(e for e in summarized['notifications']['events'] if e['kind'] == 'summary')
    assert summary['target_channel']['chat_id'] == 'oc_entry'
    assert all(e['mention_owner'] is False for e in summarized['notifications']['events'] if e['kind'] == 'summary')
    summary_uuid = summary['segments'][0]['uuid']
    requests = [r for _, r in created + replies if r.request_body.uuid == summary_uuid]
    assert len(requests) == 1
    post = json.loads(requests[0].request_body.content)['zh_cn']['content'][0]
    assert all(item['tag'] != 'at' for item in post)
    emit(scratch, user_question())
    pending = await wait_for(lambda s: any(e['kind'] == 'human_request' and e['delivery'] == 'delivered' for e in s['notifications']['events']))
    alert = next(e for e in pending['notifications']['events'] if e['kind'] == 'human_request')
    actual = [r for _, r in replies if r.request_body.uuid == alert['segments'][0]['uuid']][0]
    assert json.loads(actual.request_body.content)['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'owner-steward'}
    assert alert['target_channel']['chat_id'] == 'oc_entry' and actual.message_id != task['task_start_anchor']['message_id']
    incoming = raw(channels[0], '回答 ' + alert['human_request_id'] + '：Blue', 'om_owner_answer')
    runners[0].budget = False
    await adapters[0]._handle_message_event_data(incoming)
    assert owner.read_snapshot()['requests'][0]['human_requests'][0]['reply'] is None
    runners[0].budget = True
    await adapters[0]._handle_message_event_data(raw(channels[0], '回答 ' + alert['human_request_id'] + '：Blue', 'om_owner_answer_valid'))
    resolved = await wait_for(lambda s: s['requests'][0]['human_requests'][0]['resolution'] == 'resolved')
    assert resolved['requests'][0]['human_requests'][0]['reply']['source_anchor']['chat_id'] == 'oc_entry'
    clock[0] += 3600
    await asyncio.sleep(5.2)
    assert len([e for e in owner.read_snapshot()['notifications']['events'] if e['kind'] == 'human_request']) == 1
    sent_count = len(created) + len(replies)
    assert plugins.unload('ghost-hermes-pm')
    assert not (state / 'manager.sock').exists()
    health = json.loads((state / 'notification-health.json').read_text())
    assert health['supervision'] == 'unavailable' and health['delivery'] == 'unverified'
    clock[0] += 9000
    await asyncio.sleep(0.05)
    assert sent_count == len(created) + len(replies)
    for runner in runners: runner.stopped.set()
    print('native load, Dashboard bridge, restart, teardown: OK')

asyncio.run(main())
