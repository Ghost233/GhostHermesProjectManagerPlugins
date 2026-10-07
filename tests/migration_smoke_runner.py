"""Actual SDK migration primitives; all identities, services and histories are synthetic."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import shutil
from types import SimpleNamespace as NS

scratch = Path(sys.argv[1]).resolve()
home, state = scratch / 'home', scratch / 'state'
entity = sys.argv[2] if len(sys.argv) > 2 else ''
old_name = 'old-' + entity if entity else 'mono-lead'
new_name = 'new-' + entity if entity else 'new-lead'
global_entry = entity in {'wiki', 'ghost', 'steward'}
role = 'steward' if entity == 'steward' else 'independent' if global_entry else 'project_lead'


def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == '.env' and not path.is_relative_to(scratch):
            raise RuntimeError('Migration smoke refuses unrelated credentials.')
    if event == 'socket.connect' and isinstance(args[1], tuple) and args[1][0] not in ('127.0.0.1', '::1'):
        raise RuntimeError('Migration smoke refuses business network.')


sys.addaudithook(audit)
os.environ['HERMES_SKIP_PM_BOOTSTRAP'] = '1'
os.environ['HERMES_DISABLE_PROJECT_PLUGINS'] = '1'
sys.path.insert(0, str(home / 'plugins' / 'ghost-hermes-pm'))
sys.path.insert(0, str(scratch / 'migration-fixtures'))
from ghost_hermes_pm import Manager, VerifiedIdentity, ManagementError
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from ghost_hermes_pm.native_migration import NativeMigrationHost, verify_new_bot
from ghost_hermes_pm.native_lifecycle import NativeMultiplexLifecycleHost
from ghost_hermes_pm.feishu import NativeFeishuTransport
from ghost_hermes_pm.archives import HermesArchiveProvider
from test_directory import OWNER, make_repo, registration
from hermes_cli.profiles import create_profile, profiles_to_serve, parked_marker_path
from hermes_cli.plugins import get_plugin_manager
from gateway.control_socket import GatewayControlServer
from gateway.run_profile_reconcile import serve_profile_verb, unserve_profile_verb
from tools.registry import registry
from lark_oapi import AppType
import psutil
import yaml
import hashlib
from hermes_state import SessionDB

(home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': ['ghost-hermes-pm'], 'entries': {
    'ghost-hermes-pm': {'settings': {'state_dir': str(state), 'participant_credential_ref': 'native:HERMES_FIXTURE_PARTICIPANT_TOKEN'}}}}}))
old_home = create_profile(old_name, no_skills=True)
(old_home / 'memories').mkdir(exist_ok=True)
(old_home / 'memories' / 'MEMORY.md').write_text('Only selected material.\n\nUnselected history stays old.')
(old_home / 'SOUL.md').write_text('A careful new persona.\n\nUnselected persona stays old.')
(old_home / '.env').write_text('OLD_FIXTURE_KEY=synthetic-only\n')
(old_home / 'codex-development.json').write_text(json.dumps({'github_user': 'Charlotte-765', 'terminal_backend': 'docker', 'old_bank': 'synthetic-old-bank'}))
old_secret = (old_home / '.env').read_bytes()
unrelated_home = create_profile('unrelated-entry', no_skills=True)
unrelated_pid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], env={'PATH': '/usr/bin:/bin'})
manual_pid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], env={'PATH': '/usr/bin:/bin'}) if global_entry else None
manual_id = 'observed-chat:' + old_name
old_db = SessionDB(db_path=old_home / 'state.db')
old_db.create_session('old-original-session', 'cli', profile_name=old_name)
old_db.append_message('old-original-session', 'user', 'Original history stays queryable under a new explicit grant.')
old_db.append_message('old-original-session', 'assistant', 'Retain the complete original history; do not clone the Wiki connection.')
old_db.close()
archive_provider = HermesArchiveProvider(old_home / 'state.db', {'history': ['old-original-session']}, page_size=1)
children = {component: subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],
    env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for component in ('profile_service', 'bot', 'scheduled_entry')}


class GatewayPeer:
    _running = True
    _primary_profile_name = 'default'
    def __init__(self):
        self.lock = asyncio.Lock()
        self._served_profile_homes = dict(profiles_to_serve(True))
    def _multiplex_on(self): return True
    def _reconcile_lock(self): return self.lock
    def served_profile_names(self): return sorted(self._served_profile_homes)
    def _record_served_profiles(self, active, homes): self._served_profile_homes = dict(homes)
    async def _unserve_profile(self, name, profile_home):
        if name == old_name:
            for child in children.values():
                if child.poll() is None: child.terminate()
                child.wait(timeout=5)
        self._served_profile_homes.pop(name, None)
    async def _apply_profile_changes(self, homes, added, removed, changed, *, reason):
        assert old_name not in added
        self._served_profile_homes = dict(homes)
        return {'served_profiles': self.served_profile_names()}


ready = threading.Event()
loop = asyncio.new_event_loop()
server = peer = None
unserve_calls = []
def run_gateway():
    asyncio.set_event_loop(loop)
    async def start():
        global server, peer
        peer = GatewayPeer()
        original_unserve = unserve_profile_verb(peer)
        def counted_unserve(params):
            unserve_calls.append(params['name'])
            return original_unserve(params)
        server = GatewayControlServer(home, verb_handlers={
            'identify': lambda: {'protocol': 1, 'pid': os.getpid(), 'start_time': psutil.Process().create_time(),
                'hermes_home': str(home), 'profile': 'default', 'served_profiles': peer.served_profile_names()},
            'serve-profile': serve_profile_verb(peer), 'unserve-profile': counted_unserve})
        assert await server.start()
        ready.set()
    loop.run_until_complete(start())
    loop.run_forever()
thread = threading.Thread(target=run_gateway, daemon=True)
thread.start()
assert ready.wait(5)


def process_proof(binding):
    stopped = all(child.poll() is not None for child in children.values()) and (manual_pid is None or manual_pid.poll() is not None)
    report = {'binding': binding, 'scope': 'profile', 'status': 'verified', 'state': 'stopped' if stopped else 'ready',
        'execution_coverage': 'complete' if stopped else 'unknown', 'cases': {'scoped_control_no_host_stop': 'PASS'},
        'evidence': {'manual_execution_ids': [manual_id], 'owned_native_processes': [str(child.pid) + ':' + str(child.poll()) for child in children.values()], 'observed_chat_pid': manual_pid.pid} if global_entry else ['owned-synthetic-process:' + str(child.pid) + ':' + str(child.poll()) for child in children.values()],
        'verified_at': datetime.now(timezone.utc).isoformat()}
    report['cases'].update({key: 'PASS' if stopped else 'FAIL' for key in ('profile_runtime', 'bot_routing', 'scheduled_admission', 'related_process_coverage')})
    return report


class SyntheticBotPeer:
    """External Feishu bot-info response boundary; the production transport is unchanged."""
    config = NS(enable_set_token=False, app_type=AppType.SELF, app_id='cli_new', app_secret='synthetic-new-bot')
    def __init__(self):
        from lark_oapi.api.im.v1 import GetChatResponse, GetMessageResponse
        def message(request):
            delivered = request.message_id == 'om_migration_delivery'
            return GetMessageResponse({'code': 0, 'data': {'items': [{'message_id': request.message_id, 'chat_id': 'oc_synthetic', 'deleted': False, 'parent_id': None if delivered else 'om_migration_delivery', 'sender': {'id': 'ou_new' if delivered else 'ou_synthetic_owner', 'id_type': 'open_id', 'sender_type': 'app' if delivered else 'user', 'tenant_key': 'synthetic-bot-tenant' if delivered else 'synthetic-owner'}, 'body': {'content': json.dumps({'text': ('通道验收 ' if delivered else '已受理验收 ') + plan['digest']})}}]}})
        self.im = NS(v1=NS(chat=NS(get=lambda request: GetChatResponse({'code': 0, 'data': {'tenant_key': 'synthetic-bot-tenant'}})), message=NS(get=message)))
    def request(self, request):
        assert request.uri == '/open-apis/bot/v3/info'
        return NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_new', 'activate_status': 2}}).encode()))


bindings = [{'profile_id': profile, 'project_id': None if global_entry else 'mono', 'app_id': app, 'recipient_open_id': bot,
    'owner_open_id': 'ou_synthetic_owner', 'sender_tenant_key': 'synthetic-owner', 'chat_id': 'oc_synthetic', 'recipient_tenant_key': 'synthetic-bot-tenant', 'transport_tenant_key': 'synthetic-transport'}
    for profile, app, bot in [(old_name, 'cli_old', 'ou_old'), (new_name, 'cli_new', 'ou_new')]]
intake = NS(settings={'bindings': bindings}, transports=[(object(), NativeFeishuTransport(SyntheticBotPeer()))])
migration_host = NativeMigrationHost(home, scratch / 'native-work', verifier=lambda operation: verify_new_bot(intake, operation))
class LostReceiptHost(NativeMultiplexLifecycleHost):
    def request(self, profile, component, desired, operation_id):
        result = super().request(profile, component, desired, operation_id)
        if component == 'profile_service':
            raise ManagementError('outcome_unknown', 'Synthetic original host acted; its manager receipt was lost.')
        return result
lifecycle_host = (LostReceiptHost if entity == 'wiki' else NativeMultiplexLifecycleHost)(home, {old_name: old_home}, process_proof)
viewer = VerifiedIdentity('fixture:' + new_name, 'synthetic-native-participant')
restarted_manager = restarted_bridge = None
original_service = original_adapter = None
original_peer = scratch / 'original-executor'
if entity == 'developer':
    from recovery_service_support import fixture_adapter
    from test_requests import MESSAGE, ISSUE
    from test_task_execution import prepare_fixture
    original_service = subprocess.Popen([sys.executable, str(scratch / 'migration-fixtures' / 'recovery_service_fixture.py'), 'service', str(original_peer)], env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.PIPE, text=True)
    original_service_id = json.loads(original_service.stdout.readline())['service_id']
    original_adapter = fixture_adapter(original_peer, original_service_id)
try:
    with Manager(state, owner_identity_ref=OWNER.subject, migration_host=migration_host, lifecycle_host=lifecycle_host,
                 archive_providers={'local:original-history': archive_provider}, codex_adapter=original_adapter) as manager:
        old = registration(make_repo(scratch / 'repo'), profile_id=old_name)
        if global_entry:
            old['profile'].update(role=role, capability='non_development', project_id=None, parent_profile_id=None)
        old['profile']['connection_refs'] = {'bot': 'identity:cli_old:ou_old', 'credential': 'native:old-credential', **({'codex': 'local:fixture-stdio'} if entity == 'developer' else {})}
        manager.apply_directory_change(OWNER, 0, old)
        target = {**old['profile'], 'id': new_name, 'native_profile': new_name, 'identity_ref': viewer.subject,
                  'connection_refs': {'bot': 'identity:cli_new:ou_new', 'credential': 'native:new-credential', 'codex': 'local:new-executor'}}
        manager.apply_directory_change(OWNER, 1, {'profile': target})
        wiki = {'id': 'wiki', 'native_profile': 'wiki', 'identity_ref': 'fixture:wiki', 'role': 'independent',
                'capability': 'non_development', 'project_id': None, 'parent_profile_id': None,
                'connection_refs': {'bot': 'identity:original-wiki-bot'}}
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': wiki})
        grant = {'id': 'original-history-grant', 'name': 'Original history', 'provider_ref': 'local:original-history',
            'wiki_profile_id': 'wiki', 'query_subjects': {OWNER.subject: ['history'], viewer.subject: ['history']},
            'public_channels': [], 'task_profiles': [], 'wiki_bindings': []}
        manager.register_knowledge_source(OWNER, manager.read_snapshot(OWNER)['version'], grant)
        manager.register_archive_source(OWNER, {'id': 'original-history', 'kind': 'hermes_local', 'provider_ref': 'local:original-history',
            'grant_source_id': grant['id'], 'new_profile_id': target['id'], 'scope_ids': ['history'], 'authorization_ref': 'owner:synthetic-30-migration'})
        retained = manager.protect_archive(OWNER, 'original-history', 'migration-long-term-retention')
        assert retained['status'] == 'verified_native_cleanup_copy', retained
        old_data_digest = hashlib.sha256((old_home / 'state.db').read_bytes()).hexdigest()
        credentials = {'owner': OWNER, os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: viewer}
        with ManagementServer(manager, credentials) as bridge:
            owner = ManagementClient(state, 'owner')
            if entity == 'developer':
                request = manager.accept_request(OWNER, 'mono', old_name, MESSAGE, ISSUE)['request']
                manager.publish_request_message(OWNER, request['id'], 'confirmation', 'Synthetic original request')
                segment = manager.claim_delivery(OWNER, request['id'])
                manager.record_delivery(OWNER, request['id'], segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack'})
                prepare_fixture(manager, request['id'], scratch / 'repo')
                started = owner.start_task(request['id'])
                original_worker_pid = json.loads((original_peer / 'execution.json').read_text())['worker_pid']
                os.kill(original_worker_pid, 0)
                (original_peer / 'drop.json').write_text(json.dumps(['turn/interrupt']))
            preview = owner.migrate_profile('preview', {'source_profile_id': old_name})
            selected = [{**next(e for e in preview['materials'] if e['kind'] == kind), 'text': text}
                for kind, text in [('knowledge', 'Only selected material.'), ('persona', 'A careful new persona.')]]
            plan = owner.migrate_profile('plan', {'plan_id': 'native-migration-30', 'expected_version': owner.read_snapshot()['version'],
                'expected_profile_ids': [old_name, new_name], 'source_profile_id': old_name, 'target_profile_id': new_name,
                'selection': selected, 'preferences': [{'statement': 'Use short progress updates.', 'scope': {'kind': 'profile', 'id': new_name} if global_entry else {'kind': 'project', 'id': 'mono'}}],
                'execution': {'model': 'synthetic-model', 'provider': 'custom', 'toolsets': ['memory']}, 'archive_source_ids': ['original-history'],
                'external_memory': {'kind': 'builtin'}, 'human_steps': ['Provision independent cli_new/ou_new native bot credentials.']})
            intake.settings['channel_acceptance'] = {plan['digest']: {'oc_synthetic': {'delivery_message_id': 'om_migration_delivery', 'acceptance_message_id': 'om_migration_acceptance'}}}
            key = {'plan_id': plan['id'], 'digest': plan['digest']}
            prepared = owner.migrate_profile('prepare', key)
            assert prepared['status'] == 'prepared'
            assert prepared['material_receipt']['legacy_codex_development_config'] == 'not_copied'
            assert not (home / 'profiles' / new_name / 'codex-development.json').exists()
            assert new_name not in peer.served_profile_names()
            shutil.copytree(home / 'plugins' / 'ghost-hermes-pm', home / 'profiles' / new_name / 'plugins' / 'ghost-hermes-pm')
            session_env = dict(os.environ, HERMES_HOME=str(home / 'profiles' / new_name))
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name('native_migration_session.py')), 'new-cutover-session'],
                env=session_env, cwd=scratch, text=True, capture_output=True, timeout=30)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            check = owner.migrate_profile('check', {**key, 'session_id': 'new-cutover-session'})
            assert check['session_receipt']['provider'] == 'custom' and check['session_receipt']['tool_names'] == ['memory']
            if global_entry:
                activation = {**key, 'expected_version': owner.read_snapshot()['version'], 'expected_profile_ids': [old_name, new_name],
                    'expected_old_profile_ids': [old_name], 'archive_operation_id': 'retire-only-' + old_name, 'session_id': 'new-cutover-session'}
                reviewed_version = activation['expected_version']
                blocked = owner.migrate_profile('activate', activation)
                assert blocked['status'] == 'blocked' and blocked['old_entry']['profile_ids'] == [old_name], blocked
                assert blocked['old_entry']['manual_required'] == [manual_id] and manual_pid.poll() is None
                assert new_name not in peer.served_profile_names() and old_name not in peer.served_profile_names()
                assert 'unrelated-entry' in peer.served_profile_names() and unrelated_pid.poll() is None
                if entity == 'wiki':
                    assert blocked['old_entry']['entry_requests'][old_name + ':profile_service']['status'] == 'outcome_unknown'
                    before_calls = list(unserve_calls)
                    source_view = next(profile for profile in owner.read_snapshot()['profiles'] if profile['id'] == old_name)
                    assert source_view['lifecycle'] == 'archiving'
                    owner.apply_directory_change(owner.read_snapshot()['version'], {'profile': old['profile']})
                    assert next(profile for profile in owner.read_snapshot()['profiles'] if profile['id'] == old_name)['lifecycle'] == 'archiving'
                    bridge.close(); manager.close()
                    restarted_manager = Manager(state, owner_identity_ref=OWNER.subject, migration_host=migration_host,
                        lifecycle_host=LostReceiptHost(home, {old_name: old_home}, process_proof), archive_providers={'local:original-history': archive_provider})
                    manager = restarted_manager
                    restarted_bridge = ManagementServer(manager, credentials).start()
                    owner = ManagementClient(state, 'owner')
                    snapshot = owner.read_snapshot()
                    assert next(profile for profile in snapshot['profiles'] if profile['id'] == old_name)['lifecycle'] == 'archiving'
                    assert snapshot['migration_plans'][0]['old_entry']['approved_scope']['expected_version'] == reviewed_version
                    assert manual_pid.poll() is None and new_name not in peer.served_profile_names()
                pending = owner.migrate_profile('check', {**key, 'handled_manual_execution_ids': [manual_id]})
                assert pending['old_entry']['status'] == 'blocked', pending
                if entity == 'wiki':
                    assert unserve_calls == before_calls, 'Restart reconciliation must not replay the unknown original request'
                manual_pid.terminate(); manual_pid.wait(timeout=5)
                reconciled = owner.migrate_profile('check', {**key, 'handled_manual_execution_ids': [manual_id]})
                assert reconciled['old_entry']['status'] == 'completed', reconciled
                assert reconciled['old_entry']['approved_scope'] == {'expected_version': reviewed_version, 'expected_profile_ids': [old_name]}
                assert new_name not in peer.served_profile_names()
                activation['expected_version'] = owner.read_snapshot()['version']
            elif entity == 'developer':
                activation = {**key, 'expected_version': owner.read_snapshot()['version'], 'expected_profile_ids': [old_name, new_name],
                    'expected_old_profile_ids': [old_name], 'archive_operation_id': 'retire-only-' + old_name, 'session_id': 'new-cutover-session'}
                blocked = owner.migrate_profile('activate', activation)
                assert blocked['status'] == 'blocked' and not owner.read_snapshot()['requests'][0]['repository_released'], blocked
                assert next(profile for profile in owner.read_snapshot()['profiles'] if profile['id'] == old_name)['lifecycle'] == 'archiving'
                try:
                    manager.accept_request(OWNER, 'mono', old_name, {**MESSAGE, 'message_id': 'new-should-be-gated'}, ISSUE)
                    raise AssertionError('Archived source accepted new work')
                except ManagementError as refusal:
                    assert refusal.code == 'migration_blocked'
                bridge.close(); manager.close()
                (original_peer / 'drop.json').write_text('[]')
                restarted_manager = Manager(state, owner_identity_ref=OWNER.subject, migration_host=migration_host, lifecycle_host=lifecycle_host,
                    archive_providers={'local:original-history': archive_provider}, recovery_adapters={'local:fixture-stdio': fixture_adapter(original_peer, original_service_id, recover=True)})
                manager = restarted_manager
                restarted_bridge = ManagementServer(manager, credentials).start()
                owner = ManagementClient(state, 'owner')
                recovered = owner.reconcile_task(request['id'])
                assert recovered['recovery']['status'] == 'explicit_stop_preserved' and recovered['repository_released'] and recovered['execution'] == 'stopped', recovered
                retired = owner.migrate_profile('check', key)
                assert retired['old_entry']['status'] == 'completed', retired
                assert manager.dispatch_tasks() == []
                execution = json.loads((original_peer / 'execution.json').read_text())
                assert execution['starts'] == 1 and execution['inputs'] == [] and execution['responses'] == []
                methods = [json.loads(line).get('method') for line in (original_peer / 'wire.jsonl').read_text().splitlines()]
                assert methods.count('turn/interrupt') == methods.count('turn/start') == 1
                assert not {'thread/resume', 'thread/fork', 'turn/steer'} & set(methods)
                try:
                    os.kill(original_worker_pid, 0)
                    raise AssertionError('Original execution remained alive')
                except ProcessLookupError:
                    pass
                activation['expected_version'] = owner.read_snapshot()['version']
            else:
                archive = owner.lifecycle('archive', {'operation_id': 'old-native-entry-archived', 'profile_id': old_name,
                    'expected_version': owner.read_snapshot()['version'], 'expected_profile_ids': [old_name]})
                assert archive['status'] == 'completed', archive
                activation = {**key, 'expected_version': owner.read_snapshot()['version'], 'expected_profile_ids': [old_name, new_name],
                    'archive_operation_id': archive['id'], 'session_id': 'new-cutover-session'}
            assert old_name not in peer.served_profile_names() and all(child.poll() is not None for child in children.values())
            switched = owner.migrate_profile('activate', activation)
            assert switched['status'] == 'switched', switched
            assert owner.migrate_profile('activate', activation) == switched
            assert owner.migrate_profile('check', key) == switched
            assert owner.migrate_profile('prepare', key) == switched
            new = ManagementClient(state, os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN'])
            history = new.query_archive('original-history', 'new-identity-full-history', 'all', ['history'], complete=True)
            assert history['status'] == 'complete' and history['coverage']['end_confirmed'] and history['old_entry'] == 'not_started', history
            assert [record['text'] for record in history['records']] == ['Original history stays queryable under a new explicit grant.', 'Retain the complete original history; do not clone the Wiki connection.']
            assert hashlib.sha256((old_home / 'state.db').read_bytes()).hexdigest() == old_data_digest
            assert next(profile for profile in owner.read_snapshot()['profiles'] if profile['id'] == 'wiki')['connection_refs'] == wiki['connection_refs']
            assert switched['switch_archive_checkpoints']['original-history']['coverage']['end_confirmed']
            assert new_name in peer.served_profile_names() and old_name not in peer.served_profile_names()
            assert (old_home / '.env').read_bytes() == old_secret and parked_marker_path(old_home).exists()
            assert not parked_marker_path(home / 'profiles' / new_name).exists()
            native = get_plugin_manager()
            native.discover_and_load()
            facts = json.loads(registry.dispatch('hermes_pm_migration', {'plan_id': plan['id']}, scope=str(home)))
            assert facts['migration_plans'][0]['status'] == 'switched', facts
            assert json.loads(registry.dispatch('hermes_pm_migration', {'action': 'activate'}, scope=str(home)))['code'] == 'invalid_change'
            if entity:
                rolled = owner.migrate_profile('rollback', key)
                assert rolled['rollback']['old_entry'] == 'not_started' and rolled['rollback']['old_tasks'] == 'not_resumed', rolled
                assert old_name not in peer.served_profile_names() and new_name not in peer.served_profile_names()
                assert 'unrelated-entry' in peer.served_profile_names() and unrelated_pid.poll() is None
            assert native.unload('ghost-hermes-pm') and registry.get_entry('hermes_pm_migration', scope=str(home)) is None
finally:
    if restarted_bridge: restarted_bridge.close()
    if restarted_manager: restarted_manager.close()
    if original_service:
        original_service.terminate(); original_service.wait(timeout=5); original_service.stdout.close()
    if server:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    for child in [*children.values(), unrelated_pid, *([manual_pid] if manual_pid else [])]:
        if child.poll() is None: child.terminate()
        child.wait(timeout=5)
print('native load, Dashboard bridge, restart, teardown: OK')
