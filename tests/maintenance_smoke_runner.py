"""Actual fixed SDK reload, owned native management bridge and bounded file restore."""
from pathlib import Path
from datetime import datetime, timezone
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import yaml

scratch = Path(sys.argv[1]).resolve()
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if any(path.is_relative_to(Path.home() / p) for p in ('.hermes', '.codex')) or path.name == '.env' and not path.is_relative_to(scratch):
            raise RuntimeError('Maintenance smoke refused production homes and credentials.')
    if event == 'socket.connect' and isinstance(args[1], tuple):
        raise RuntimeError('Maintenance smoke refused external network.')
sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
home, state = scratch / 'home', scratch / 'state'
plugin = home / 'plugins' / 'ghost-hermes-pm'
settings = {'manager_profile': 'default', 'state_dir': str(state), 'owner_identity_ref': 'fixture:owner',
            'dashboard_credential_ref': 'native:HERMES_FIXTURE_OWNER_TOKEN'}
(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {'ghost-hermes-pm': {'settings': settings}}}}))
sdk = scratch / 'sdk'
# Package the already verified fixed source into this disposable runtime using
# the SDK's public build-stamp writer, never a production install/version claim.
subprocess.run([sys.executable, str(sdk / 'scripts' / 'write_install_stamp.py'), '--output', str(sdk / 'install-stamp.json'),
                '--commit', os.environ['HERMES_TEST_SDK_COMMIT'], '--base-version', '0.21.5', '--distance', '0',
                '--source', 'local', '--update-mechanism', 'external'], check=True, capture_output=True, env=os.environ)
sys.path.insert(0, str(plugin))
sys.path.insert(0, str(scratch / 'maintenance-fixtures'))
from ghost_hermes_pm.transport import ManagementClient
from ghost_hermes_pm.dashboard import create_router
from ghost_hermes_pm.native_maintenance import source_digest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_cli.plugins import get_plugin_manager, ainvoke_hook
from tools.registry import registry
from gateway.control_socket import GatewayControlServer, reload_gateway_plugins
from gateway.run_plugin_rewire import reload_plugins_verb
from native_fixture_boundary import install

files = []
for kind in ('data', 'archive'):
    path = scratch / (kind + '.json')
    path.write_text(json.dumps({'version': kind + '-original', 'entries': 'inactive'}))
    files.append({'id': kind, 'path': str(path), 'kind': kind, 'format': 'file'})
files.append({'id': 'config', 'path': str(home / 'config.yaml'), 'kind': 'config', 'format': 'file'})
original_data = {entry['id']: Path(entry['path']).read_bytes() for entry in files}
releases = {}
proofs = []
def verified(binding):
    # Only this owned gateway, empty native execution scope, and explicitly listed
    # fixture files are covered. Real model/bot/process capabilities remain unknown.
    loaded = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(home)))
    assert loaded['entry_code_digest'] == loaded['entry_source_code_digest']
    good_files = all(Path(entry['path']).read_bytes() == original_data[entry['id']] for entry in files)
    proofs.append(binding['phase'])
    return {'binding': binding, 'status': 'verified', 'verified_at': datetime.now(timezone.utc).isoformat(),
            'evidence': 'owned-fixed-SDK-registry-and-listed-artificial-files-only',
            'cases': {key: 'PASS' for key in ('loaded_registry_source', 'all_profile_rounds', 'all_original_process_paths',
                'all_inflight_requests', 'current_authorization', 'all_config_data_archive_scope', 'fixed_target_source',
                'loaded_target_registry', 'config_data_archive_grants', 'bounded_restore_no_entry_activation',
                'restored_data_query', 'grants_not_widened', 'old_entries_tasks_inactive', 'health_not_restored')},
            'active_turns': [], 'inflight_requests': [], 'execution_coverage': 'complete',
            'authorization_digest': 'synthetic-native-grant-v1', 'configuration_verified': good_files,
            'entries_inactive': good_files, 'old_tasks_started': False}

def external_host(module):
    # Substitute the external host verifier at its configured adapter boundary;
    # requests and reload/registry/file effects still use the actual native adapter.
    module.configured_maintenance_host = lambda config, directory: module.NativeMaintenanceHost(
        str(home), str(home), str(plugin), releases, files, verified)
install(plugin, {'native_maintenance': external_host})
native = get_plugin_manager()
native.discover_and_load()
loaded = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(home)))
assert loaded['plugin_version'] == '0.1.0'
loop = asyncio.new_event_loop()
ready = threading.Event()
reload_calls = []
lose_ack = False
class Gateway:
    _served_profile_homes = {}
    _primary_profile_name = 'default'
    adapters = {}
    def __init__(self): self.stopped = asyncio.Event()
    async def wait_for_shutdown(self): await self.stopped.wait()
gateway = Gateway()
server = None
def run_control():
    global server
    asyncio.set_event_loop(loop)
    async def start():
        global server
        actual_reload = reload_plugins_verb(gateway, loop)
        def reload(params):
            global lose_ack
            reload_calls.append(dict(params))
            result = actual_reload(params)
            if lose_ack:
                lose_ack = False
                # Simulate an external partial configuration failure after actual
                # source reload; this is not a fabricated native capability.
                for entry in files:
                    if entry['kind'] != 'config': Path(entry['path']).write_text('partial-upgrade')
                return {'error': 'owned lost reload acknowledgement after actual effect'}
            return result
        server = GatewayControlServer(home, verb_handlers={'reload-plugins': reload})
        assert await server.start()
        ready.set()
    loop.run_until_complete(start())
    loop.run_forever()
thread = threading.Thread(target=run_control, daemon=True)
thread.start()
assert ready.wait(5)

def wait_loaded(version):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        fact = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(home)))
        if fact.get('plugin_version') == version and fact.get('entry_code_digest') == fact.get('entry_source_code_digest'):
            return fact
        time.sleep(0.02)
    raise AssertionError('Actual SDK loaded implementation did not reach ' + version)

def bootstrap():
    asyncio.run_coroutine_threadsafe(ainvoke_hook('pre_gateway_dispatch', event=object(), gateway=gateway), loop).result(timeout=10)
    return ManagementClient(state, os.environ['HERMES_FIXTURE_OWNER_TOKEN'])

def approval(client, operation, **extra):
    snapshot = client.read_snapshot()
    return {'operation_id': operation, 'expected_version': snapshot['version'],
            'expected_profile_ids': sorted(profile['id'] for profile in snapshot['profiles']), **extra}

try:
    # First independently reproduce actual changed native function behavior.
    old_entry = registry.get_entry('hermes_pm_loaded_version', scope=str(home))
    path = plugin / 'ghost_hermes_pm' / 'native.py'
    path.write_text(path.read_text().replace('return json.dumps(registered_release)', "return json.dumps({**registered_release, 'native_probe': 'target-v2'})"))
    manifest = plugin / 'plugin.yaml'
    manifest.write_text(manifest.read_text().replace('version: "0.1.0"', 'version: "0.2.0"'))
    ack = reload_gateway_plugins(home, profile_home=home)
    assert ack['reloaded'] is True
    v2 = wait_loaded('0.2.0')
    assert v2['native_probe'] == 'target-v2' and v2['load_id'] != loaded['load_id']
    assert registry.get_entry('hermes_pm_loaded_version', scope=str(home)) is not old_entry
    target = scratch / 'release-v3'
    shutil.copytree(plugin, target)
    (target / 'plugin.yaml').write_text((target / 'plugin.yaml').read_text().replace('version: "0.2.0"', 'version: "0.3.0"'))
    target_code = target / 'ghost_hermes_pm' / 'native.py'
    target_code.write_text(target_code.read_text().replace("'native_probe': 'target-v2'", "'native_probe': 'target-v3'"))
    releases['release-v3'] = {'path': str(target), 'plugin_version': '0.3.0', 'source_digest': source_digest(target)}
    client = bootstrap()
    app = FastAPI(); app.include_router(create_router(lambda request: client)); browser = TestClient(app)
    runtime = browser.get('/snapshot').json()['maintenance']['runtime']
    assert runtime['status'] == 'verified' and runtime['plugin_version'] == '0.2.0' and runtime['release_verified'] is False, runtime
    expected = {key: runtime[key] for key in ('plugin_version', 'source_digest', 'sdk_version', 'sdk_source_digest')}
    plan = approval(client, 'native-maintenance-31', expected_release=expected,
                    target_release={key: value for key, value in releases['release-v3'].items() if key != 'path'} | {'id': 'release-v3'})
    assert browser.post('/maintenance', json={'action': 'enter', 'details': plan}).status_code == 200
    checkpoint = browser.post('/maintenance', json={'action': 'checkpoint', 'details': {'operation_id': plan['operation_id']}}).json()
    assert checkpoint['status'] == 'checkpoint_verified', checkpoint
    before_reload_calls = len(reload_calls)
    lose_ack = True
    initial = browser.post('/maintenance', json={'action': 'switch', 'details': approval(client, plan['operation_id'])})
    assert initial.status_code in {200, 422, 503}
    v3 = wait_loaded('0.3.0')
    assert v3['native_probe'] == 'target-v3'
    assert not (state / 'manager.sock').exists(), 'Actual old management bridge must close on native unload.'
    client = bootstrap()
    checked = client.maintenance('check', {'operation_id': plan['operation_id']})
    assert checked['status'] == 'switch_failed', checked
    assert checked['switch_request']['status'] == 'outcome_unknown'
    assert len(reload_calls) == before_reload_calls + 1, 'Reconnected check must not replay native reload.'
    restored = client.maintenance('rollback', approval(client, plan['operation_id']))
    v2_again = wait_loaded('0.2.0')
    assert v2_again['native_probe'] == 'target-v2' and v2_again['load_id'] not in {loaded['load_id'], v2['load_id']}
    assert not (state / 'manager.sock').exists()
    client = bootstrap()
    restored = client.maintenance('check', {'operation_id': plan['operation_id']})
    assert restored['status'] == 'rollback_verified', restored
    assert all(Path(entry['path']).read_bytes() == original_data[entry['id']] for entry in files)
    assert restored['restore']['manager_authority'] == 'preserved_current' and restored['restore']['health'] == 'not_restored'
    assert client.maintenance('reenable', approval(client, plan['operation_id']))['status'] == 'reenabled'
    assert client.read_snapshot()['maintenance']['mode'] == 'active'
    assert native.unload('ghost-hermes-pm')
    assert registry.get_entry('hermes_pm_loaded_version', scope=str(home)) is None
    assert not (state / 'manager.sock').exists()
    native.discover_and_load(force=True)
    client = bootstrap()
    loss = client.read_snapshot()['maintenance']
    assert loss['events'][-1]['status'] == 'pending_verification' and loss['events'][-1]['execution_stopped'] is False
    assert all(Path(entry['path']).read_bytes() == original_data[entry['id']] for entry in files)
    # A timestamp/size-valid stale .pyc must fail actual loaded-code verification.
    import py_compile
    original_native = path.read_bytes()
    original_stat = path.stat()
    py_compile.compile(str(path), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    path.write_bytes(original_native.replace(b"'native_probe': 'target-v2'", b"'native_probe': 'target-v9'"))
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert reload_gateway_plugins(home, profile_home=home)['reloaded'] is True
    stale = json.loads(registry.dispatch('hermes_pm_loaded_version', {}, scope=str(home)))
    assert stale['native_probe'] == 'target-v2'
    assert stale['entry_code_digest'] != stale['entry_source_code_digest']
    client = bootstrap()
    rejected = client.read_snapshot()['maintenance']['runtime']
    assert rejected['status'] == 'unverified' and rejected['release_verified'] is False
    try:
        client.migrate_profile('plan', {})
    except Exception as error:
        assert getattr(error, 'code', None) == 'unknown_version', error
    else:
        raise AssertionError('Stale native code must prevent new migration.')
    path.write_bytes(original_native)
    assert reload_gateway_plugins(home, profile_home=home)['reloaded'] is True
    wait_loaded('0.2.0')
    client = bootstrap()
    assert client.read_snapshot()['maintenance']['runtime']['status'] == 'verified'
    assert native.unload('ghost-hermes-pm')
    print(json.dumps({'reload_requests': len(reload_calls), 'native_proof_phases': sorted(set(proofs)),
                      'actual_loaded_behaviour': ['target-v2', 'target-v3', 'target-v2'], 'real_capabilities': 'unverified'}))
finally:
    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
print('native load, Dashboard bridge, restart, teardown: OK')
