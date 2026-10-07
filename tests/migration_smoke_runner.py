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
from ghost_hermes_pm import Manager, VerifiedIdentity
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
old_home = create_profile('mono-lead', no_skills=True)
(old_home / 'memories').mkdir(exist_ok=True)
(old_home / 'memories' / 'MEMORY.md').write_text('Only selected material.\n\nUnselected history stays old.')
(old_home / 'SOUL.md').write_text('A careful new persona.\n\nUnselected persona stays old.')
(old_home / '.env').write_text('OLD_FIXTURE_KEY=synthetic-only\n')
old_secret = (old_home / '.env').read_bytes()
old_db = SessionDB(db_path=old_home / 'state.db')
old_db.create_session('old-original-session', 'cli', profile_name='mono-lead')
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
        if name == 'mono-lead':
            for child in children.values():
                if child.poll() is None: child.terminate()
                child.wait(timeout=5)
        self._served_profile_homes.pop(name, None)
    async def _apply_profile_changes(self, homes, added, removed, changed, *, reason):
        assert 'mono-lead' not in added
        self._served_profile_homes = dict(homes)
        return {'served_profiles': self.served_profile_names()}


ready = threading.Event()
loop = asyncio.new_event_loop()
server = peer = None
def run_gateway():
    asyncio.set_event_loop(loop)
    async def start():
        global server, peer
        peer = GatewayPeer()
        server = GatewayControlServer(home, verb_handlers={
            'identify': lambda: {'protocol': 1, 'pid': os.getpid(), 'start_time': psutil.Process().create_time(),
                'hermes_home': str(home), 'profile': 'default', 'served_profiles': peer.served_profile_names()},
            'serve-profile': serve_profile_verb(peer), 'unserve-profile': unserve_profile_verb(peer)})
        assert await server.start()
        ready.set()
    loop.run_until_complete(start())
    loop.run_forever()
thread = threading.Thread(target=run_gateway, daemon=True)
thread.start()
assert ready.wait(5)


def process_proof(binding):
    stopped = all(child.poll() is not None for child in children.values())
    report = {'binding': binding, 'scope': 'profile', 'status': 'verified', 'state': 'stopped' if stopped else 'ready',
        'execution_coverage': 'complete' if stopped else 'unknown', 'cases': {'scoped_control_no_host_stop': 'PASS'},
        'evidence': ['owned-synthetic-process:' + str(child.pid) + ':' + str(child.poll()) for child in children.values()],
        'verified_at': datetime.now(timezone.utc).isoformat()}
    report['cases'].update({key: 'PASS' if stopped else 'FAIL' for key in ('profile_runtime', 'bot_routing', 'scheduled_admission', 'related_process_coverage')})
    return report


class SyntheticBotPeer:
    """External Feishu bot-info response boundary; the production transport is unchanged."""
    config = NS(enable_set_token=False, app_type=AppType.SELF, app_id='cli_new', app_secret='synthetic-new-bot')
    def request(self, request):
        assert request.uri == '/open-apis/bot/v3/info'
        return NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_new', 'activate_status': 2}}).encode()))


bindings = [{'profile_id': profile, 'project_id': 'mono', 'app_id': app, 'recipient_open_id': bot,
    'chat_id': 'oc_synthetic', 'recipient_tenant_key': 'synthetic-bot-tenant', 'transport_tenant_key': 'synthetic-transport'}
    for profile, app, bot in [('mono-lead', 'cli_old', 'ou_old'), ('new-lead', 'cli_new', 'ou_new')]]
intake = NS(settings={'bindings': bindings}, transports=[(object(), NativeFeishuTransport(SyntheticBotPeer()))])
migration_host = NativeMigrationHost(home, scratch / 'native-work', verifier=lambda operation: verify_new_bot(intake, operation))
lifecycle_host = NativeMultiplexLifecycleHost(home, {'mono-lead': old_home}, process_proof)
viewer = VerifiedIdentity('fixture:new-lead', 'synthetic-native-participant')
try:
    with Manager(state, owner_identity_ref=OWNER.subject, migration_host=migration_host, lifecycle_host=lifecycle_host,
                 archive_providers={'local:original-history': archive_provider}) as manager:
        old = registration(make_repo(scratch / 'repo'))
        old['profile']['connection_refs'] = {'bot': 'identity:cli_old:ou_old', 'credential': 'native:old-credential'}
        manager.apply_directory_change(OWNER, 0, old)
        target = {**old['profile'], 'id': 'new-lead', 'native_profile': 'new-lead', 'identity_ref': viewer.subject,
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
        with ManagementServer(manager, {'owner': OWNER, os.environ['HERMES_FIXTURE_PARTICIPANT_TOKEN']: viewer}):
            owner = ManagementClient(state, 'owner')
            preview = owner.migrate_profile('preview', {'source_profile_id': 'mono-lead'})
            selected = [{**next(e for e in preview['materials'] if e['kind'] == kind), 'text': text}
                for kind, text in [('knowledge', 'Only selected material.'), ('persona', 'A careful new persona.')]]
            plan = owner.migrate_profile('plan', {'plan_id': 'native-migration-30', 'expected_version': owner.read_snapshot()['version'],
                'expected_profile_ids': ['mono-lead', 'new-lead'], 'source_profile_id': 'mono-lead', 'target_profile_id': 'new-lead',
                'selection': selected, 'preferences': [{'statement': 'Use short progress updates.', 'scope': {'kind': 'project', 'id': 'mono'}}],
                'execution': {'model': 'synthetic-model', 'provider': 'custom', 'toolsets': ['memory']}, 'archive_source_ids': ['original-history'],
                'external_memory': {'kind': 'builtin'}, 'human_steps': ['Provision independent cli_new/ou_new native bot credentials.']})
            key = {'plan_id': plan['id'], 'digest': plan['digest']}
            assert owner.migrate_profile('prepare', key)['status'] == 'prepared'
            assert 'new-lead' not in peer.served_profile_names()
            shutil.copytree(home / 'plugins' / 'ghost-hermes-pm', home / 'profiles' / 'new-lead' / 'plugins' / 'ghost-hermes-pm')
            session_env = dict(os.environ, HERMES_HOME=str(home / 'profiles' / 'new-lead'))
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name('native_migration_session.py')), 'new-cutover-session'],
                env=session_env, cwd=scratch, text=True, capture_output=True, timeout=30)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            check = owner.migrate_profile('check', {**key, 'session_id': 'new-cutover-session'})
            assert check['session_receipt']['provider'] == 'custom' and check['session_receipt']['tool_names'] == ['memory']
            archive = owner.lifecycle('archive', {'operation_id': 'old-native-entry-archived', 'profile_id': 'mono-lead',
                'expected_version': owner.read_snapshot()['version'], 'expected_profile_ids': ['mono-lead']})
            assert archive['status'] == 'completed', archive
            assert 'mono-lead' not in peer.served_profile_names() and all(child.poll() is not None for child in children.values())
            activation = {**key, 'expected_version': owner.read_snapshot()['version'], 'expected_profile_ids': ['mono-lead', 'new-lead'],
                'archive_operation_id': archive['id'], 'session_id': 'new-cutover-session'}
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
            assert 'new-lead' in peer.served_profile_names() and 'mono-lead' not in peer.served_profile_names()
            assert (old_home / '.env').read_bytes() == old_secret and parked_marker_path(old_home).exists()
            assert not parked_marker_path(home / 'profiles' / 'new-lead').exists()
            native = get_plugin_manager()
            native.discover_and_load()
            facts = json.loads(registry.dispatch('hermes_pm_migration', {'plan_id': plan['id']}, scope=str(home)))
            assert facts['migration_plans'][0]['status'] == 'switched', facts
            assert json.loads(registry.dispatch('hermes_pm_migration', {'action': 'activate'}, scope=str(home)))['code'] == 'invalid_change'
            assert native.unload('ghost-hermes-pm') and registry.get_entry('hermes_pm_migration', scope=str(home)) is None
finally:
    if server:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    for child in children.values():
        if child.poll() is None: child.terminate()
        child.wait(timeout=5)
print('native load, Dashboard bridge, restart, teardown: OK')
