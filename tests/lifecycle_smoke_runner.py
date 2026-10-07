"""Pristine SDK scope and owned synthetic component processes, never a production host."""
from pathlib import Path
import json
import os
import subprocess
import sys
import yaml
import asyncio
import threading
from datetime import datetime, timezone
import psutil

scratch = Path(sys.argv[1]).resolve()
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(Path.home() / p) for p in ('.hermes', '.codex')) or path.name == '.env' and not path.is_relative_to(scratch):
            raise RuntimeError('Lifecycle smoke refused real homes and credentials.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Lifecycle smoke refused external network.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home, state = scratch / 'home', scratch / 'state'
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {'ghost-hermes-pm': {'settings': {
    'state_dir': str(state), 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}}}}}))
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
sys.path.insert(0, str(scratch / 'lifecycle-fixtures'))
from ghost_hermes_pm import Manager, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from ghost_hermes_pm.dashboard import create_router
from ghost_hermes_pm.native_lifecycle import NativeMultiplexLifecycleHost, configured_lifecycle_host
from test_directory import OWNER, make_repo, registration
from test_lifecycle import tree, scope_approval, decide
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.profiles import get_profile_dir, parked_marker_path, profiles_to_serve
from gateway.control_socket import GatewayControlServer
from gateway.run_profile_reconcile import unserve_profile_verb, serve_profile_verb
from tools.registry import registry
from fastapi import FastAPI
from fastapi.testclient import TestClient


class OwnedProfileProcesses:
    def __init__(self):
        self.children = {}
        for profile in ('mono-lead', 'child-lead', 'wiki', 'ghost'):
            directory = get_profile_dir(profile)
            directory.mkdir(parents=True)
            (directory / 'config.yaml').write_text('model: {}\n')
            for component in ('profile_service', 'bot', 'scheduled_entry'):
                self.children[profile, component] = self.spawn()

    def spawn(self):
        return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


processes = OwnedProfileProcesses()
class ControlledMultiplexer:
    """Actual SDK scoped verbs, with owned artificial adapter/process effects only."""
    _running = True
    _primary_profile_name = 'default'
    def __init__(self):
        self._served_profile_homes = dict(profiles_to_serve(True))
        self.lock = asyncio.Lock()
        self.effects = []
    def _multiplex_on(self): return True
    def _reconcile_lock(self): return self.lock
    def served_profile_names(self): return sorted(self._served_profile_homes)
    def _record_served_profiles(self, active, homes): self._served_profile_homes = dict(homes)
    async def _unserve_profile(self, name, profile_home):
        for component in ('profile_service', 'bot', 'scheduled_entry'):
            child = processes.children[name, component]
            child.terminate(); child.wait(timeout=5)
        self._served_profile_homes.pop(name)
        self.effects.append(('unserved', name))
    async def _apply_profile_changes(self, homes, added, removed, changed, *, reason):
        for name in added:
            for component in ('profile_service', 'bot', 'scheduled_entry'):
                assert processes.children[name, component].poll() is not None
                processes.children[name, component] = processes.spawn()
            self.effects.append(('served', name))
        self._served_profile_homes = homes
        return {'served_profiles': self.served_profile_names()}

ready = threading.Event()
control_loop = asyncio.new_event_loop()
runner = None
server = None
def run_control():
    asyncio.set_event_loop(control_loop)
    async def start():
        global runner, server
        runner = ControlledMultiplexer()
        server = GatewayControlServer(home, verb_handlers={
            'identify': lambda: {'protocol': 1, 'pid': os.getpid(), 'start_time': psutil.Process().create_time(), 'hermes_home': str(home),
                'profile': 'default', 'served_profiles': runner.served_profile_names()},
            'unserve-profile': unserve_profile_verb(runner), 'serve-profile': serve_profile_verb(runner)})
        assert await server.start()
        ready.set()
    control_loop.run_until_complete(start())
    control_loop.run_forever()
control_thread = threading.Thread(target=run_control, daemon=True)
control_thread.start()
assert ready.wait(5)

authority_ref = None
native_binding = None
def proof(binding):
    global native_binding
    native_binding = binding
    assert authority_ref is not None
    snapshot = authority_ref.read_snapshot(OWNER)
    assert any(o['id'] == binding['operation_id'] for o in snapshot['lifecycle_operations']), 'public durable intent must precede native requests'
    name = binding['native_profile']
    if binding['phase'] == 'state' and binding['component'] != 'manual_execution':
        child = processes.children[name, binding['component']]
        assert (child.poll() is not None) == (binding['desired_state'] == 'stopped')
    return {'binding': binding, 'status': 'verified', 'scope': 'profile', 'state': binding['desired_state'],
            'cases': {case: 'PASS' for case in ('scoped_control_no_host_stop', 'profile_runtime', 'bot_routing', 'scheduled_admission', 'related_process_coverage')},
            'execution_coverage': 'complete', 'evidence': 'controlled-native-rpc-and-owned-processes-only',
            'verified_at': datetime.now(timezone.utc).isoformat()}
host = NativeMultiplexLifecycleHost(home, {name: str(get_profile_dir(name)) for name in ('mono-lead', 'child-lead')}, proof)
participant = VerifiedIdentity('fixture:lead', 'isolated-native-participant')
plugin_manager = get_plugin_manager()
plugin_manager.discover_and_load()
try:
    unknown = configured_lifecycle_host({'host_home': str(home), 'profile_homes': {'mono-lead': str(get_profile_dir('mono-lead'))}}, scratch / 'unsupported')
    with Manager(scratch / 'unsupported', owner_identity_ref=OWNER.subject, lifecycle_host=unknown) as blocked:
        blocked.apply_directory_change(OWNER, 0, registration(make_repo(scratch / 'unsupported-repo')))
        result = decide(blocked, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'native-unknown'})
        assert result['status'] == 'blocked' and blocked.read_snapshot(OWNER)['lifecycle_events'] == []
        assert runner.effects == [] and all(child.poll() is None for child in processes.children.values())
        assert not parked_marker_path(get_profile_dir('mono-lead')).exists(), 'Missing capability must not mutate native admission.'
    with Manager(state, owner_identity_ref=OWNER.subject, lifecycle_host=host) as authority:
        authority_ref = authority
        authority.apply_directory_change(OWNER, 0, registration(make_repo(scratch / 'repo')))
        tree(authority, scratch)
        independent = {key: child.pid for key, child in processes.children.items() if key[0] in {'wiki', 'ghost'}}
        with ManagementServer(authority, {os.environ['HERMES_FIXTURE_OWNER_TOKEN']: OWNER, os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant}):
            client = ManagementClient(state, os.environ['HERMES_FIXTURE_OWNER_TOKEN'])
            app = FastAPI(); app.include_router(create_router(lambda _: client)); browser = TestClient(app)
            result = browser.post('/lifecycle', json={'action': 'archive', 'details': scope_approval(client.read_snapshot(), 'archive', {'profile_id': 'mono-lead', 'operation_id': 'native-archive'})}).json()
            assert result['status'] == 'completed', result
            native = json.loads(registry.dispatch('hermes_pm_lifecycle', {}, scope=str(home)))
            assert native['lifecycle_events'][0]['kind'] == 'archive_completed', native
            assert json.loads(registry.dispatch('hermes_pm_lifecycle', {'owner': True, 'action': 'restore'}, scope=str(home)))['status'] == 'rejected'
            assert browser.get('/snapshot').json() == client.read_snapshot()
            assert {'mono-lead', 'child-lead'}.isdisjoint({n for n, _ in profiles_to_serve(True)})
    with Manager(state, owner_identity_ref=OWNER.subject, lifecycle_host=host) as authority:
        authority_ref = authority
        with ManagementServer(authority, {os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: participant, os.environ['HERMES_FIXTURE_OWNER_TOKEN']: OWNER}):
            client = ManagementClient(state, os.environ['HERMES_FIXTURE_OWNER_TOKEN'])
            assert len(client.read_snapshot()['lifecycle_events']) == 1
            client.lifecycle('restore', scope_approval(client.read_snapshot(), 'restore', {'profile_id': 'mono-lead', 'operation_id': 'native-parent'}))
            served = {n for n, _ in profiles_to_serve(True)}
            assert 'mono-lead' in served and 'child-lead' not in served
            client.lifecycle('restore', scope_approval(client.read_snapshot(), 'restore', {'profile_id': 'child-lead', 'operation_id': 'native-child'}))
            assert {'mono-lead', 'child-lead', 'wiki', 'ghost', 'default'} <= {n for n, _ in profiles_to_serve(True)}
            assert client.read_snapshot()['requests'] == []
            assert all(processes.children[key].pid == pid and processes.children[key].poll() is None for key, pid in independent.items())
            assert runner.effects == [('unserved', 'mono-lead'), ('unserved', 'child-lead'), ('served', 'mono-lead'), ('served', 'child-lead')]
            assert control_thread.is_alive() and 'default' in runner.served_profile_names()
            print(json.dumps({'source': 'pristine SDK / actual GatewayControlServer and scoped lifecycle verbs / synthetic component peers',
                'sdk_sha256': native_binding['sdk_sha256'], 'native_control_effects': runner.effects,
                'final_served_profiles': runner.served_profile_names(), 'unknown_native_capability': 'blocked_no_rpc_no_marker',
                'archive_checks': {key: value['status'] for key, value in result['checks'].items() if isinstance(value, dict)},
                'durable_completion_events': len(client.read_snapshot()['lifecycle_events']),
                'independent_processes': 'same_pids_still_running', 'production_chat_cron_process_coverage': 'unverified'}, sort_keys=True))
    assert plugin_manager.unload('ghost-hermes-pm')
    assert registry.get_entry('hermes_pm_lifecycle', scope=str(home)) is None
finally:
    asyncio.run_coroutine_threadsafe(server.stop(), control_loop).result(timeout=5)
    control_loop.call_soon_threadsafe(control_loop.stop)
    control_thread.join(timeout=5)
    control_loop.close()
    processes.close()
print('native lifecycle: real pristine SDK control socket and scoped unserve/serve verbs, isolated component processes, parent-first restore, independent host preserved: OK')
print('native load, Dashboard bridge, restart, teardown: OK')
