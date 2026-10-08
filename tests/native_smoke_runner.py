"""Runs only in test_native_runtime's clean child environment and temporary filesystem."""
from pathlib import Path
import os
import sys

scratch = Path(sys.argv[1]).resolve()
real_hermes = Path.home() / '.hermes'


def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(real_hermes) or (path.name == '.env' and not path.is_relative_to(scratch)):
            raise RuntimeError('Smoke refused a real Hermes/credential file read.')
    if event == 'import' and args[0] in {'hermes_cli.main', 'run_agent'}:
        raise RuntimeError('Smoke refused a full launch/bootstrap import.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Smoke refused an external network connection.')

sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
os.environ['HERMES_FIXTURE_GITHUB_ACCOUNT'] = 'example-user'

# Consume the gateway host marker before launching any protocol child.
import hermes_bootstrap  # noqa: F401
import json
import subprocess
import types
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# Failure IPC must not return a private bound target even for an unexpected key error.
private_marker = 'synthetic-private-target-marker'
migration_failure = subprocess.run([sys.executable, str(scratch / 'home' / 'plugins' / 'ghost-hermes-pm' / 'ghost_hermes_pm' / 'native_migration_worker.py')],
    input=json.dumps({'action': 'inspect', 'host_home': str(scratch / 'missing-host'), 'work_dir': str(scratch / 'missing-work'),
                     'operation': {'plan': {'target_profile_id': private_marker}, 'bindings': {}}}),
    text=True, capture_output=True)
assert migration_failure.returncode == 1
assert json.loads(migration_failure.stdout)['code'] == 'capability_unverified'
assert private_marker not in migration_failure.stdout and migration_failure.stderr == ''

home = scratch / 'home'
state = scratch / 'state'
repo = scratch / 'repo'
repo.mkdir()
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixed native mono'], check=True)
settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
            'github_account_ref': 'native:HERMES_FIXTURE_GITHUB_ACCOUNT',
            'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN', 'allow_local_dashboard_owner': True,
            'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN',
            'participant_entries': [{'identity_ref': 'fixture:lead', 'credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}]}
settings['feishu_intake'] = {'enabled': True, 'verification_ref': 'fixture:controlled-smoke', 'bindings': [
    {'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot', 'transport_tenant_key': 'tenant-app',
     'verification_ref': 'fixture:identity-map', 'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead',
     'owner_open_id': 'ou_owner', 'owner_native_ids': ['u_owner', 'on_owner'], 'chat_id': 'oc_fixture', 'project_id': 'mono', 'profile_id': 'lead',
     'repository': 'example-user/fixture'}]}
settings['profile_readiness'] = {'lead': {'native_home': str(home)}}
settings['global_validation_host'] = {'host_id': 'local:native-sdk-original-host', 'generation': 'controlled-sdk-generation', 'runner': [sys.executable], 'watcher': [sys.executable, '-c', 'import time; time.sleep(60)'], 'tests': {'unit': ['-c', 'assert True']}, 'environment': {'PATH': '/usr/bin:/bin'}}
settings['archive_providers'] = {'local:original-remote': {'kind': 'feishu_remote', 'binding': {'app_id': 'cli_archive', 'tenant_key': 'tenant-archive', 'bot_open_id': 'ou_archive'}, 'chat_scopes': {'public': ['oc_archive']}, 'credential_ref': 'native:HERMES_FIXTURE_APP_SECRET'}}
ready_profile = {'id': 'lead', 'native_profile': 'default', 'identity_ref': 'fixture:lead', 'role': 'project_lead', 'capability': 'development', 'project_id': 'mono', 'parent_profile_id': None, 'connection_refs': {'bot': 'identity:cli_fixture:ou_lead', 'credential': 'native:HERMES_FIXTURE_APP_SECRET'}}
import hashlib
ready_digest = hashlib.sha256(json.dumps(ready_profile, sort_keys=True).encode()).hexdigest()
settings['feishu_intake']['channel_acceptance'] = {ready_digest: {'oc_fixture': {'delivery_message_id': 'om_channel_delivery', 'acceptance_message_id': 'om_channel_acceptance'}}}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
                                                'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
preserved = state / 'user-file'
state.mkdir(mode=0o700)
preserved.write_text('keep')
# Substitute only the external Issue source before the native registrar captures it.
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
import ghost_hermes_pm.feishu as issue_source
def fixture_issue(url, *, expected_account=None):
    assert expected_account == 'example-user'
    return {'url': url, 'title': 'Native Issue fixture',
    'body': 'Accepted fixture material.\n' + 'A' * 4000, 'updated_at': '2026-10-07T00:00:00Z'}
issue_source.read_github_issue = fixture_issue
from native_fixture_boundary import install
install(home / 'plugins' / 'ghost-hermes-pm', {'feishu': lambda module: setattr(module, 'read_github_issue', issue_source.read_github_issue)}, synthetic_readiness=False)
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.web_server_dashboard import _discover_dashboard_plugins, _mount_plugin_api_routes
manager = get_plugin_manager()
manager.discover_and_load()
info = next(p for p in manager.list_plugins() if p['name'] == 'ghost-hermes-pm')
assert info['enabled'] and info['error'] is None, info
assert info['commands'] == 1, info
manager.invoke_hook('pre_api_request', session_id='not-a-migration-session', model='synthetic', provider='custom',
                    system_prompt='No migration target.', request={'method': 'POST', 'body': {}}, tool_count=0)
from tools.registry import registry
for tool_name in ('hermes_pm_snapshot', 'hermes_pm_task', 'hermes_pm_observe', 'hermes_pm_knowledge', 'hermes_pm_global_validation', 'hermes_pm_migration'):
    assert registry.get_entry(tool_name, scope=str(home)) is not None, tool_name
assert not (state / 'manager.sock').exists(), 'Ordinary CLI discovery must not start authority.'
assert not (state / 'manager.lock').exists(), 'Ordinary CLI discovery must not acquire the manager lease.'
assert not (state / 'manager.sqlite3').exists(), 'Ordinary CLI discovery must not create authoritative storage.'

# Run the actual native Dashboard scanner/gate/import/mount with its minimal host app seam.
# This avoids importing the broad launch web server. Auth behavior is checked separately.
application = FastAPI()
application.state.auth_required = False
host = types.ModuleType('hermes_cli.web_server')
host.app = application
host._get_dashboard_plugins = _discover_dashboard_plugins

def require_token(request):
    if request.headers.get('x-fixture-session') != 'verified-owner':
        raise HTTPException(401, 'Unauthorized')
host._require_token = require_token
sys.modules['hermes_cli.web_server'] = host
_mount_plugin_api_routes()
browser = TestClient(application)
headers = {'x-fixture-session': 'verified-owner'}
base = '/api/plugins/ghost-hermes-pm'
assert browser.get(base + '/snapshot').status_code == 401
change = {'project': {'id': 'mono', 'name': 'Native Fixture Mono', 'repo_path': str(repo), 'test_artifact_paths': [str(repo / 'build')]},
          'profile': ready_profile}
import asyncio
from hermes_cli.lifecycle import ainvoke_hook
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_secret_scope, reset_secret_scope
from gateway.run import GatewayRunner
from gateway.config import Platform, PlatformConfig, GatewayConfig
from gateway.bot_loop_guard import BotLoopGuard


class GatewayFixture(GatewayRunner):
    # Public Gateway lifetime seam; no real adapters, credentials or outbound work.
    def __init__(self):
        self.stopped = asyncio.Event()
        self.config = GatewayConfig()
        self._primary_profile_name = 'default'
        self.adapters, self._profile_adapters = {}, {}
        self.session_store, self.pairing_store, self.pairing_stores = None, None, {}
        self._busy_text_mode, self._human_delay = 'steer', None
        self._bot_loop_guard = BotLoopGuard()
    async def wait_for_shutdown(self):
        await self.stopped.wait()
    def _is_user_authorized_for_source(self, source):
        self.authorized_source = source
        return super()._is_user_authorized_for_source(source)


async def exercise_gateway_lifecycle():
    gateway = GatewayFixture()
    event = object()
    assert browser.get(base + '/snapshot', headers=headers).status_code == 503
    await manager.ainvoke_hook('pre_gateway_dispatch', event=event, gateway=None)
    assert not (state / 'manager.sock').exists()
    foreign = home / 'profiles' / 'foreign'
    foreign.mkdir(parents=True)
    wrong_home = set_hermes_home_override(foreign)
    wrong_secrets = set_secret_scope({'HERMES_FIXTURE_OWNER_TOKEN': 'foreign-scope-token'}, profile_home=str(foreign))
    try:
        await manager.ainvoke_hook('pre_gateway_dispatch', event=event, gateway=gateway)
        assert not (state / 'manager.sock').exists(), 'A foreign Profile cannot start manager authority.'
    finally:
        reset_secret_scope(wrong_secrets)
        reset_hermes_home_override(wrong_home)
    wrong_secrets = set_secret_scope({'HERMES_FIXTURE_OWNER_TOKEN': 'foreign-scope-token'}, profile_home=str(foreign))
    try:
        await manager.ainvoke_hook('pre_gateway_dispatch', event=event, gateway=gateway)
        assert not (state / 'manager.sock').exists(), 'A mismatched secret namespace cannot start manager authority.'
    finally:
        reset_secret_scope(wrong_secrets)
    assert await ainvoke_hook('pre_gateway_dispatch', event=event, gateway=gateway) == []
    assert (state / 'manager.sock').exists()
    response = browser.post(base + '/directory', json={'expected_version': 0, 'change': change}, headers=headers)
    assert response.status_code == 200, response.text
    snapshot = browser.get(base + '/snapshot', headers=headers).json()
    assert snapshot['version'] == 1 and len(snapshot['profiles']) == 1, snapshot
    assert json.loads(registry.dispatch('hermes_pm_snapshot', {}, scope=str(home)))['profiles'][0]['id'] == 'lead'
    observation = json.loads(registry.dispatch('hermes_pm_observe', {'scope': 'mono'}, scope=str(home)))
    assert observation['status'] == 'completed' and observation['manual_sessions'] == [], observation
    assert all(capability['status'] == 'unknown' for capability in observation['manual_capabilities']), observation
    task_rejection = json.loads(registry.dispatch('hermes_pm_task', {'action': 'verify', 'request_id': 'unknown'}, scope=str(home)))
    assert task_rejection['status'] == 'rejected' and task_rejection['code'] == 'invalid_change'
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse, GetChatResponse, GetMessageResponse
    from gateway.session import SessionSource
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    native = Client.builder().app_id('cli_fixture').app_secret('synthetic-unused-secret').build()
    sent = []
    native.request = lambda request: types.SimpleNamespace(code=0, raw=types.SimpleNamespace(
        content=b'{"code":0,"bot":{"open_id":"ou_lead","activate_status":2}}'))
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_native_' + str(len(sent)),
                                    'chat_id': 'oc_fixture', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    owned = Platform('hermes_feishu_pm')
    gateway.adapter = gateway._create_adapter(owned, PlatformConfig(enabled=True, extra={
        'app_id': 'cli_fixture', 'app_secret': 'synthetic-unused-secret', 'require_mention': False,
        'default_group_policy': 'open', 'allow_bots': 'none'}))
    assert gateway.adapter is not None
    gateway.adapters[owned] = gateway.adapter
    gateway._wire_adapter_handlers(gateway.adapter)
    async def chat_info(chat_id): return {'name': 'Synthetic chat', 'type': 'group'}
    gateway.adapter.get_chat_info = chat_info
    factories = manager.get_platform_handler_factories('hermes_feishu_pm')
    assert len(factories) == 1
    factories[0][0](native, gateway.adapter)
    native.im.v1.chat.get = lambda request: GetChatResponse({'code': 0, 'data': {'tenant_key': 'tenant-bot'}})
    def channel_message(request):
        delivery = request.message_id == 'om_channel_delivery'
        return GetMessageResponse({'code': 0, 'data': {'items': [{'message_id': request.message_id, 'chat_id': 'oc_fixture', 'deleted': False, 'parent_id': None if delivery else 'om_channel_delivery', 'sender': {'id': 'ou_lead' if delivery else 'ou_owner', 'id_type': 'open_id', 'sender_type': 'app' if delivery else 'user', 'tenant_key': 'tenant-bot' if delivery else 'tenant-owner'}, 'body': {'content': json.dumps({'text': ('通道验收 ' if delivery else '已受理验收 ') + ready_digest})}}]}})
    native.im.v1.message.get = channel_message
    enabled = browser.post(base + '/directory', json={'expected_version': browser.get(base + '/snapshot', headers=headers).json()['version'], 'change': {'enable_profile': 'lead'}}, headers=headers)
    assert enabled.status_code == 200, enabled.text
    verified = browser.get(base + '/snapshot', headers=headers).json()['profiles'][0]
    assert verified['lifecycle'] == 'active' and verified['can_execute'] is False
    assert verified['readiness']['channels'][0]['source'] == 'actual_feishu_group_and_message_reads'

    source = gateway.adapter.build_source(chat_id='oc_fixture', chat_type='group',
                           user_id='u_owner', user_id_alt='on_owner', is_bot=False,
                           message_id='om_inbound')
    raw = P2ImMessageReceiveV1({'header': {'event_type': 'im.message.receive_v1', 'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'},
        'event': {'sender': {'sender_type': 'user', 'tenant_key': 'tenant-owner',
                            'sender_id': {'open_id': 'ou_owner', 'user_id': 'u_owner', 'union_id': 'on_owner'}},
                  'message': {'message_id': 'om_inbound', 'chat_id': 'oc_fixture', 'chat_type': 'group', 'message_type': 'text',
                              'content': '{"text":"@_user_1 派发 https://github.com/example-user/fixture/issues/15"}',
                              'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': 'tenant-bot',
                                            'id': {'open_id': 'ou_lead'}}]}}})
    inbound = MessageEvent(text='normalized fixture', source=source, message_id='om_inbound', raw_message=raw)
    assert await ainvoke_hook('pre_gateway_dispatch', event=inbound, gateway=gateway) == []
    await gateway.adapter._handle_message_event_data(raw)
    assert gateway.authorized_source._transport_adapter_ref() is gateway.adapter
    assert gateway.authorized_source.message_id == source.message_id
    accepted = browser.get(base + '/snapshot', headers=headers).json()
    assert len(accepted['requests']) == 1 and accepted['requests'][0]['execution'] == 'waiting'
    assert accepted['requests'][0]['delivery'] == 'delivered' and len(sent) == 4
    assert sent[0].message_id == 'om_inbound' and sent[1].message_id == 'om_native_1'
    assert json.loads(sent[0].request_body.content)['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'ou_owner'}
    assert await ainvoke_hook('pre_gateway_dispatch', event=inbound, gateway=gateway) == []
    await gateway.adapter._handle_message_event_data(raw)
    assert len(sent) == 4, 'Duplicate receive must not resend acknowledged segments.'
    from ghost_hermes_pm.transport import ManagementClient
    owner_client = ManagementClient(state, 'synthetic-owner-credential')
    task_id = accepted['requests'][0]['id']
    fixed_head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    plan = owner_client.global_validation('plan', {'request_id': task_id, 'mono_commit': fixed_head, 'children': [], 'test_ids': ['unit']})
    prepared = owner_client.global_validation('prepare', {'validation_id': plan['id'], 'children': []})
    assert prepared['status'] == 'ready' and prepared['preparation']['receipt']['related_execution'] == 'ended'
    try:
        owner_client.global_validation('start', {'validation_id': plan['id']})
    except Exception as error:
        assert error.code == 'capability_unverified', error
    else:
        raise AssertionError('Actual native host must retain missing physical boundary proof gate.')
    from lark_oapi.core.http.transport import Transport as SDKHttp
    from lark_oapi.core.model import RawResponse
    archive_calls = []
    def archive_http(conf, request, option=None):
        archive_calls.append(request.uri)
        if '/auth/' in request.uri:
            body = {'code': 0, 'tenant_access_token': 'synthetic-token', 'expire': 3600}
        elif '/bot/' in request.uri:
            body = {'code': 0, 'bot': {'open_id': 'ou_archive', 'activate_status': 2}}
        elif '/tenant/' in request.uri:
            body = {'code': 0, 'data': {'tenant': {'tenant_key': 'tenant-archive'}}}
        else:
            assert request.uri == '/open-apis/im/v1/messages', request.uri
            body = {'code': 0, 'data': {'has_more': False, 'items': [{'message_id': 'om_original', 'chat_id': 'oc_archive', 'deleted': False, 'body': {'content': '{"text":"Original remote requirement"}'}}]}}
        response = RawResponse(); response.status_code = 200; response.headers = {'Content-Type': 'application/json'}; response.content = json.dumps(body).encode()
        return response
    SDKHttp.execute = archive_http
    owner_client.apply_directory_change(owner_client.read_snapshot()['version'], {'profile': {'id': 'wiki', 'native_profile': 'wiki', 'identity_ref': 'fixture:wiki', 'role': 'independent', 'capability': 'non_development'}})
    owner_client.register_knowledge_source(owner_client.read_snapshot()['version'], {'id': 'original-archive-grant', 'name': 'Explicit original remote source', 'provider_ref': 'local:original-remote', 'wiki_profile_id': 'wiki', 'query_subjects': {'fixture:lead': ['public']}, 'public_channels': [], 'task_profiles': [], 'wiki_bindings': []})
    owner_client.register_archive_source({'id': 'original-remote', 'kind': 'feishu_remote', 'provider_ref': 'local:original-remote', 'grant_source_id': 'original-archive-grant', 'new_profile_id': 'lead', 'scope_ids': ['public'], 'authorization_ref': 'owner:explicit-original-archive'})
    remote = ManagementClient(state, 'synthetic-participant-credential').query_archive('original-remote', 'native-archive-query', 'requirement', ['public'], True)
    assert remote['status'] == 'complete', remote
    assert remote['records'][0]['locator'] == 'feishu-archive:oc_archive#om_original'
    assert '/open-apis/tenant/v2/tenant/query' in archive_calls
    assert '/open-apis/im/v1/messages' in archive_calls
    verified_version = owner_client.read_snapshot()['version']

    gateway.stopped.set()
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists(), 'Gateway shutdown must release authority before plugin unload.'
    assert preserved.read_text() == 'keep'
    assert manager.unload('ghost-hermes-pm')
    await asyncio.sleep(0)
    assert registry.get_entry('hermes_pm_snapshot', scope=str(home)) is None
    assert registry.get_entry('hermes_pm_task', scope=str(home)) is None
    assert registry.get_entry('hermes_pm_knowledge', scope=str(home)) is None
    assert registry.get_entry('hermes_pm_observe', scope=str(home)) is None
    assert registry.get_entry('hermes_pm_migration', scope=str(home)) is None
    manager.discover_and_load(force=True)
    assert not (state / 'manager.sock').exists(), 'Rediscovery without Gateway context remains read-only.'
    restarted_gateway = GatewayFixture()
    assert await ainvoke_hook('pre_gateway_dispatch', event=event, gateway=restarted_gateway) == []
    again = browser.get(base + '/snapshot', headers=headers).json()
    assert again['version'] == verified_version + 1 and {p['id'] for p in again['profiles']} == {'lead', 'wiki'} and len(again['requests']) == 1, again
    assert again['maintenance']['events'][-1]['status'] == 'pending_verification'
    assert again['maintenance']['events'][-1]['execution_stopped'] is False
    assert manager.unload('ghost-hermes-pm')
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists()
    assert preserved.read_text() == 'keep'

asyncio.run(exercise_gateway_lifecycle())
(scratch / 'native-smoke-result.json').write_text(json.dumps({'native_smoke': 'passed'}))
