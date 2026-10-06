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
    if event == 'import' and args[0] in {'hermes_cli.main', 'hermes_bootstrap', 'run_agent'}:
        raise RuntimeError('Smoke refused a full launch/bootstrap import.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Smoke refused an external network connection.')

sys.addaudithook(audit)

import json
import subprocess
import types
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

home = scratch / 'home'
state = scratch / 'state'
repo = scratch / 'repo'
repo.mkdir()
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
            'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN', 'allow_local_dashboard_owner': True,
            'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN',
            'participant_entries': [{'identity_ref': 'fixture:lead', 'credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}]}
settings['feishu_intake'] = {'enabled': True, 'verification_ref': 'fixture:controlled-smoke', 'bindings': [
    {'sender_tenant_key': 'tenant-owner', 'recipient_tenant_key': 'tenant-bot', 'transport_tenant_key': 'tenant-app',
     'verification_ref': 'fixture:identity-map', 'app_id': 'cli_fixture', 'recipient_open_id': 'ou_lead',
     'owner_open_id': 'ou_owner', 'chat_id': 'oc_fixture', 'project_id': 'mono', 'profile_id': 'lead',
     'repository': 'Ghost233/fixture'}]}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
                                                'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
preserved = state / 'user-file'
state.mkdir()
preserved.write_text('keep')
# Substitute only the external Issue source before the native registrar captures it.
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
import ghost_hermes_pm.feishu as issue_source
issue_source.read_github_issue = lambda url: {'url': url, 'title': 'Native Issue fixture',
    'body': 'Accepted fixture material.\n' + 'A' * 4000, 'updated_at': '2026-10-07T00:00:00Z'}
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.web_server_dashboard import _discover_dashboard_plugins, _mount_plugin_api_routes
manager = get_plugin_manager()
manager.discover_and_load()
info = next(p for p in manager.list_plugins() if p['name'] == 'ghost-hermes-pm')
assert info['enabled'] and info['error'] is None, info
assert info['tools'] == 1 and info['commands'] == 1 and info['hooks'] == 1, info
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
          'profile': {'id': 'lead', 'native_profile': 'default', 'identity_ref': 'fixture:lead', 'role': 'project_lead',
                      'capability': 'development', 'project_id': 'mono', 'parent_profile_id': None, 'connection_refs': {}}}
import asyncio
from hermes_cli.lifecycle import ainvoke_hook
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from agent.secret_scope import set_secret_scope, reset_secret_scope


class GatewayFixture:
    # Public Gateway lifetime seam; no real adapters, credentials or outbound work.
    def __init__(self):
        self.stopped = asyncio.Event()
    async def wait_for_shutdown(self):
        await self.stopped.wait()
    def _intake_adapter_for(self, source):
        return self.adapter
    def _is_user_authorized_for_source(self, source):
        self.authorized_source = source
        return True
    def _admit_bot_message_for_source(self, source):
        return True


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
    from tools.registry import registry
    assert json.loads(registry.dispatch('hermes_pm_snapshot', {}, scope=str(home)))['profiles'][0]['id'] == 'lead'
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse
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
    gateway.adapter = object()
    factories = manager.get_platform_handler_factories('feishu')
    assert len(factories) == 1
    factories[0][0](native, gateway.adapter)
    source = SessionSource(platform=Platform.FEISHU, chat_id='oc_fixture', chat_type='group',
                           user_id='u_owner', user_id_alt='on_owner', is_bot=False,
                           message_id='om_inbound', profile='default')
    raw = P2ImMessageReceiveV1({'header': {'event_type': 'im.message.receive_v1', 'app_id': 'cli_fixture', 'tenant_key': 'tenant-app'},
        'event': {'sender': {'sender_type': 'user', 'tenant_key': 'tenant-owner',
                            'sender_id': {'open_id': 'ou_owner', 'user_id': 'u_owner', 'union_id': 'on_owner'}},
                  'message': {'message_id': 'om_inbound', 'chat_id': 'oc_fixture', 'chat_type': 'group', 'message_type': 'text',
                              'content': '{"text":"@_user_1 派发 https://github.com/Ghost233/fixture/issues/15"}',
                              'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': 'tenant-bot',
                                            'id': {'open_id': 'ou_lead'}}]}}})
    inbound = MessageEvent(text='normalized fixture', source=source, message_id='om_inbound', raw_message=raw)
    assert await ainvoke_hook('pre_gateway_dispatch', event=inbound, gateway=gateway) == [{'action': 'skip'}]
    assert gateway.authorized_source is source
    accepted = browser.get(base + '/snapshot', headers=headers).json()
    assert len(accepted['requests']) == 1 and accepted['requests'][0]['execution'] == 'waiting'
    assert accepted['requests'][0]['delivery'] == 'delivered' and len(sent) == 4
    assert sent[0].message_id == 'om_inbound' and sent[1].message_id == 'om_native_1'
    assert json.loads(sent[0].request_body.content)['zh_cn']['content'][0][0] == {'tag': 'at', 'user_id': 'ou_owner'}
    assert await ainvoke_hook('pre_gateway_dispatch', event=inbound, gateway=gateway) == [{'action': 'skip'}]
    assert len(sent) == 4, 'Duplicate receive must not resend acknowledged segments.'
    verified_version = accepted['version']
    gateway.stopped.set()
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists(), 'Gateway shutdown must release authority before plugin unload.'
    assert preserved.read_text() == 'keep'
    assert manager.unload('ghost-hermes-pm')
    await asyncio.sleep(0)
    assert registry.get_entry('hermes_pm_snapshot', scope=str(home)) is None
    manager.discover_and_load(force=True)
    assert not (state / 'manager.sock').exists(), 'Rediscovery without Gateway context remains read-only.'
    restarted_gateway = GatewayFixture()
    assert await ainvoke_hook('pre_gateway_dispatch', event=event, gateway=restarted_gateway) == []
    again = browser.get(base + '/snapshot', headers=headers).json()
    assert again['version'] == verified_version and len(again['profiles']) == 1 and len(again['requests']) == 1, again
    assert manager.unload('ghost-hermes-pm')
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists()
    assert preserved.read_text() == 'keep'

asyncio.run(exercise_gateway_lifecycle())
print('native load, Dashboard bridge, restart, teardown: OK')
