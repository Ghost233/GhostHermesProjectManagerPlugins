"""Pristine SDK tools and Dashboard recover an owned independent original service."""
from pathlib import Path
import json
import os
import subprocess
import sys
import yaml

scratch = Path(sys.argv[1]).resolve()
protected = [Path.home() / '.hermes', Path.home() / '.codex']
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(p) for p in protected) or path.name == '.env' and not path.is_relative_to(scratch):
            raise RuntimeError('Recovery smoke refused real home, history or credential access.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Recovery smoke refused external network access.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home, state = scratch / 'home', scratch / 'state'
settings = {'state_dir': str(state), 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from ghost_hermes_pm.dashboard import create_router
sys.path.insert(0, str(scratch / 'control-fixtures'))
from test_directory import OWNER, make_repo
from test_task_execution import accepted
from recovery_service_support import fixture_adapter
from hermes_cli.plugins import get_plugin_manager
from tools.registry import registry
from fastapi import FastAPI
from fastapi.testclient import TestClient

peer = scratch / 'original-service'
service = subprocess.Popen([sys.executable, str(scratch / 'control-fixtures' / 'recovery_service_fixture.py'), 'service', str(peer)],
    env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.PIPE, text=True)
participant = VerifiedIdentity('fixture:lead', 'fixture-native-participant')
try:
    service_id = json.loads(service.stdout.readline())['service_id']
    with Manager(state, owner_identity_ref=OWNER.subject, codex_adapter=fixture_adapter(peer, service_id)) as authority:
        request_id = accepted(authority, make_repo(scratch / 'repo'))
        authority.start_task(OWNER, request_id)
    plugin_manager = get_plugin_manager()
    plugin_manager.discover_and_load()
    with Manager(state, owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': fixture_adapter(peer, service_id, recover=True)}) as authority:
        with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant, os.environ['HERMES_FIXTURE_OWNER_TOKEN']: OWNER}):
            task = json.loads(registry.dispatch('hermes_pm_task', {'request_id': request_id, 'action': 'reconcile'}, scope=str(home)))
            assert task['execution'] == 'running' and task['recovery']['status'] == 'monitoring_restored', task
            app = FastAPI()
            app.include_router(create_router(lambda request: ManagementClient(state, os.environ['HERMES_FIXTURE_OWNER_TOKEN'])))
            browser = TestClient(app)
            snapshot = browser.get('/snapshot').json()
            assert snapshot == authority.read_snapshot(OWNER)
            assert snapshot['requests'][0]['session']['service_id'] == service_id
            assert browser.post('/task', json={'action': 'stop', 'request_id': request_id, 'instruction_id': 'native-original-stop', 'expected_turn_id': task['session']['turn_id']}).status_code == 200
    with Manager(state, owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': fixture_adapter(peer, service_id, recover=True)}) as authority:
        with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant}):
            stopped = json.loads(registry.dispatch('hermes_pm_task', {'request_id': request_id, 'action': 'reconcile'}, scope=str(home)))
            assert stopped['execution'] == 'stopped' and stopped['recovery']['status'] == 'explicit_stop_preserved', stopped
            assert stopped['repository_released'] is True
    execution = json.loads((peer / 'execution.json').read_text())
    assert execution['starts'] == 1 and execution['responses'] == []
    assert plugin_manager.unload('ghost-hermes-pm')
finally:
    service.terminate()
    service.wait(timeout=5)
    service.stdout.close()
print('owned original service, real child, persistent restart, no duplicate execution/reply: OK')
print('native load, Dashboard bridge, restart, teardown: OK')
