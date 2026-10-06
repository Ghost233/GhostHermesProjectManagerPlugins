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
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'],
                                                'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
preserved = state / 'user-file'
state.mkdir()
preserved.write_text('keep')
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
    assert again['version'] == 1 and len(again['profiles']) == 1, again
    assert manager.unload('ghost-hermes-pm')
    await asyncio.sleep(0)
    assert not (state / 'manager.sock').exists()
    assert preserved.read_text() == 'keep'

asyncio.run(exercise_gateway_lifecycle())
print('native load, Dashboard bridge, restart, teardown: OK')
