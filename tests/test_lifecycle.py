"""Owner lifecycle operations at the shared public management boundary."""
import json
from datetime import datetime, timezone

import pytest

from ghost_hermes_pm import Manager, ManagementError
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo, registration
from test_requests import MESSAGE, ISSUE
from test_task_execution import accepted, adapter_for
from test_task_control import TURN, wire


class ProfileHost:
    """Trusted synthetic host; each fact is scoped to one native Profile."""
    def __init__(self):
        self.manager = None
        self.calls = []
        self.pending = set()

    def request(self, profile, component, state, operation_id):
        snapshot = self.manager.read_snapshot(OWNER)
        target = next(p for p in snapshot['profiles'] if p['id'] == profile['id'])
        assert target['lifecycle'] in {'archiving', 'restoring'}
        assert target['archive_intent']
        assert any(p['id'] == operation_id for p in snapshot['lifecycle_operations'])
        self.calls.append((profile['id'], component, state, operation_id))
        return {'status': 'accepted'}

    def capability(self, profile, component, state, operation_id):
        return {'profile_id': profile['id'], 'native_profile': profile['native_profile'], 'project_id': profile['project_id'],
                'component': component, 'operation_id': operation_id, 'scope': 'profile', 'status': 'verified',
                'action': state, 'evidence': 'synthetic-profile-scoped-native-control-only',
                'verified_at': datetime.now(timezone.utc).isoformat()}

    def inspect(self, profile, component, state, operation_id):
        return {'profile_id': profile['id'], 'native_profile': profile['native_profile'],
                'project_id': profile['project_id'], 'component': component, 'operation_id': operation_id,
                'scope': 'profile', 'status': 'verified', 'state': 'running' if (profile['id'], component) in self.pending else state,
                'execution_coverage': 'complete', 'evidence': 'synthetic-profile-host-only',
                'verified_at': datetime.now(timezone.utc).isoformat()}


def tree(manager, root):
    child = registration(make_repo(root / 'child'), 'child', 'child-lead')
    child['profile'].update(identity_ref='fixture:child', role='subproject_lead', parent_profile_id='mono-lead')
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], child)
    for name in ('wiki', 'ghost'):
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': {
            'id': name, 'native_profile': name, 'identity_ref': 'fixture:' + name, 'role': 'independent',
            'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})


def test_owner_archive_blocks_parent_and_child_until_original_execution_and_every_entry_stop_are_verified(tmp_path):
    host = ProfileHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), lifecycle_host=host) as manager:
        host.manager = manager
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        tree(manager, tmp_path)
        manager.start_task(OWNER, request_id)
        host.pending.add(('child-lead', 'bot'))
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            operation = client.lifecycle('archive', {'profile_id': 'mono-lead', 'operation_id': 'archive-29'})
            assert operation['status'] == 'processing'
            snapshot = client.read_snapshot()
            assert {p['id']: p['lifecycle'] for p in snapshot['profiles']} == {
                'mono-lead': 'archiving', 'child-lead': 'archiving', 'wiki': 'configuring', 'ghost': 'configuring'}
            assert snapshot['requests'][0]['stop']['rpc_status'] == 'accepted'
            assert snapshot['requests'][0]['repository_released'] is False
            assert snapshot['lifecycle_events'] == []
            with pytest.raises(ManagementError, match='lifecycle'):
                manager.accept_request(OWNER, 'child', 'child-lead', {**MESSAGE, 'message_id': 'new-child'}, ISSUE)
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [
                {'id': TURN, 'status': 'interrupted', 'itemsView': 'full', 'items': []}]}))
            still = client.lifecycle('check', {'operation_id': 'archive-29'})
            assert still['status'] == 'processing'
            assert client.read_snapshot()['requests'][0]['repository_released'] is True
            host.pending.clear()
            done = client.lifecycle('check', {'operation_id': 'archive-29'})
            assert done['status'] == 'completed'
            assert all(p['lifecycle'] == 'archived' for p in client.read_snapshot()['projects'])
            assert client.read_snapshot()['lifecycle_events'][0]['kind'] == 'archive_completed'
            client.lifecycle('archive', {'profile_id': 'mono-lead', 'operation_id': 'archive-29'})
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/interrupt']) == 1
        assert len(host.calls) == 6


def test_archive_cancels_unstarted_queue_and_restore_only_parent_keeps_old_work_stopped(tmp_path):
    host = ProfileHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), lifecycle_host=host) as manager:
        host.manager = manager
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        tree(manager, tmp_path)
        manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'archive-queued'})
        with pytest.raises(ManagementError, match='lifecycle'):
            manager.start_task(OWNER, request_id)
        with pytest.raises(ManagementError, match='lead'):
            manager.lifecycle(OWNER, 'restore', {'profile_id': 'child-lead', 'operation_id': 'child-early'})
        restored = manager.lifecycle(OWNER, 'restore', {'profile_id': 'mono-lead', 'operation_id': 'parent-restore'})
        assert restored['status'] == 'completed'
        snapshot = manager.read_snapshot(OWNER)
        assert {p['id']: p['lifecycle'] for p in snapshot['profiles']} == {
            'mono-lead': 'active', 'child-lead': 'archived', 'wiki': 'configuring', 'ghost': 'configuring'}
        assert snapshot['requests'][0]['execution'] == 'stopped'
        assert manager.dispatch_tasks() == []
        with pytest.raises(ManagementError, match='archived'):
            manager.start_task(OWNER, request_id)
        manager.lifecycle(OWNER, 'restore', {'profile_id': 'child-lead', 'operation_id': 'child-restore'})
        new = manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'fresh'}, ISSUE)
        assert new['request']['id'] != request_id
        assert len([p for p in manager.read_snapshot(OWNER)['profiles'] if p['project_id'] == 'child']) == 1


def test_observe_only_execution_needs_explicit_owner_handling_and_current_terminal_evidence(tmp_path):
    from test_manual_observation import observer, source, manual_state, READ_ONLY
    host = ProfileHost()
    repo, peer = make_repo(tmp_path / 'repo'), tmp_path / 'manual'
    manual_state(peer, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-daemon': observer(peer)}, lifecycle_host=host) as manager:
        host.manager = manager
        manager.apply_directory_change(OWNER, 0, registration(repo))
        manager.register_observation_source(OWNER, source())
        manager.refresh_manual_sessions(OWNER)
        manual_id = manager.read_snapshot(OWNER)['manual_sessions'][0]['id']
        archived = manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'archive-manual'})
        assert archived['status'] != 'completed'
        assert archived['manual_required'] == [manual_id]
        assert manager.read_snapshot(OWNER)['control_grants'] == []
        manual_state(peer, repo, 'idle')
        assert manager.lifecycle(OWNER, 'check', {'operation_id': 'archive-manual'})['status'] != 'completed'
        checked = manager.lifecycle(OWNER, 'check', {'operation_id': 'archive-manual', 'handled_manual_session_ids': [manual_id]})
        assert checked['status'] == 'completed'
        messages = [json.loads(line) for line in (peer / 'manual-wire.jsonl').read_text().splitlines()]
        assert all(m['method'] in READ_ONLY for m in messages)


def test_unknown_native_lifecycle_is_durably_blocked_and_directory_correction_cannot_clear_intent(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(repo))
        result = manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'archive-unknown'})
        assert result['status'] == 'blocked'
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], registration(repo))
        assert manager.read_snapshot(OWNER)['profiles'][0]['lifecycle'] == 'archiving'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        snapshot = restarted.read_snapshot(OWNER)
        assert snapshot['lifecycle_operations'][0]['status'] == 'blocked'
        assert snapshot['profiles'][0]['archive_intent']['operation_id'] == 'archive-unknown'
        assert snapshot['lifecycle_events'] == []


def test_archive_never_creates_a_new_manual_control_grant_even_when_owner_has_a_control_adapter(tmp_path):
    from test_manual_control import adapters, original_state, setup, ORIGINAL_TURN
    repo, peer = make_repo(tmp_path / 'repo'), tmp_path / 'manual'
    original_state(peer, repo)
    read, control = adapters(peer)
    host = ProfileHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host,
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        host.manager = manager
        request_id, observed = setup(manager, repo)
        manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'observe-archive'})
        with pytest.raises(ManagementError, match='lifecycle'):
            manager.take_over_session(OWNER, request_id, observed['id'], 'new-archive-grant', ORIGINAL_TURN)
        assert manager.read_snapshot(OWNER)['control_grants'] == []
@pytest.mark.asyncio
async def test_group_owner_archive_and_dashboard_restore_show_the_same_durable_lifecycle(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Transport, Gateway, event
    host = ProfileHost()
    adapter, transport = object(), Transport()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host) as manager:
        host.manager = manager
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        tree(manager, tmp_path)
        entry = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: pytest.fail('Lifecycle must not create work'))
        entry.attach_transport(adapter, transport)
        incoming = event('@_user_1 封存项目 mono-lead', 'om_archive')
        assert await entry.receive(incoming, Gateway(adapter)) == {'action': 'skip'}
        assert manager.read_snapshot(OWNER)['profiles'][0]['lifecycle'] == 'archived'
        assert transport.sent[0]['reply_to'] == 'om_archive'
        assert 'completed' in transport.sent[0]['text']
        assert await entry.receive(incoming, Gateway(adapter)) == {'action': 'skip'}
        assert len(transport.sent) == 1
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            app = FastAPI()
            app.include_router(create_router(lambda _: client))
            browser = TestClient(app)
            assert browser.get('/snapshot').json() == client.read_snapshot()
            restored = browser.post('/lifecycle', json={'action': 'restore', 'details': {'profile_id': 'mono-lead', 'operation_id': 'dashboard-restore'}})
            assert restored.status_code == 200 and restored.json()['status'] == 'completed'
            assert browser.get('/snapshot').json()['profiles'][1]['lifecycle'] == 'archived'
            assert browser.post('/lifecycle', json={'action': 'archive', 'details': {'profile_id': 'mono-lead', 'operation_id': 'forged', 'owner': True}}).status_code == 422


def test_public_archive_then_restart_reconciles_the_original_real_process_without_old_execution_replay(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from recovery_service_support import fixture_adapter
    peer = tmp_path / 'peer'
    service = subprocess.Popen([sys.executable, str(Path(__file__).with_name('recovery_service_fixture.py')), 'service', str(peer)],
                               env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.PIPE, text=True)
    host = ProfileHost()
    try:
        service_id = json.loads(service.stdout.readline())['service_id']
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=fixture_adapter(peer, service_id), lifecycle_host=host) as manager:
            host.manager = manager
            request_id = accepted(manager, make_repo(tmp_path / 'repo'))
            tree(manager, tmp_path)
            manager.start_task(OWNER, request_id)
            pid = json.loads((peer / 'execution.json').read_text())['worker_pid']
            os.kill(pid, 0)
            # Lose the actual original connection after the service applied its interrupt.
            (peer / 'drop.json').write_text(json.dumps(['turn/interrupt']))
            archive = manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'process-archive'})
            assert archive['status'] != 'completed'
            assert manager.read_snapshot(OWNER)['requests'][0]['repository_released'] is False
        (peer / 'drop.json').write_text('[]')
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                     recovery_adapters={'local:fixture-stdio': fixture_adapter(peer, service_id, recover=True)}, lifecycle_host=host) as restarted:
            host.manager = restarted
            task = restarted.reconcile_task(OWNER, request_id)
            assert task['recovery']['status'] == 'explicit_stop_preserved'
            assert task['execution'] == 'stopped' and task['repository_released'] is True
            assert restarted.lifecycle(OWNER, 'check', {'operation_id': 'process-archive'})['status'] == 'completed'
            assert len(restarted.read_snapshot(OWNER)['lifecycle_events']) == 1
            assert restarted.lifecycle(OWNER, 'restore', {'profile_id': 'mono-lead', 'operation_id': 'process-restore'})['status'] == 'completed'
            assert restarted.dispatch_tasks() == []
            assert restarted.read_snapshot(OWNER)['requests'][0]['execution'] == 'stopped'
        execution = json.loads((peer / 'execution.json').read_text())
        assert execution['starts'] == 1 and execution['inputs'] == [] and execution['responses'] == []
        methods = [json.loads(line).get('method') for line in (peer / 'wire.jsonl').read_text().splitlines()]
        assert methods.count('turn/interrupt') == methods.count('turn/start') == 1
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        service.terminate()
        service.wait(timeout=5)
        service.stdout.close()


@pytest.mark.asyncio
async def test_full_parent_child_archive_steward_public_authorized_read_and_individual_restore_demo(tmp_path):
    import hashlib
    from ghost_hermes_pm import VerifiedIdentity
    from ghost_hermes_pm.archives import HermesArchiveProvider
    from ghost_hermes_pm.messages import FeishuEntry
    from test_archives import synthetic_history, migration_registration
    from test_knowledge import source_grant, native_transport
    from test_feishu_entry import CONFIG, Gateway, event
    host, path = ProfileHost(), synthetic_history(tmp_path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host,
                 archive_providers={'local:old-hermes': HermesArchiveProvider(path, {'public': ['old-root']}, page_size=1)}) as manager:
        host.manager = manager
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        tree(manager, tmp_path)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': {
            'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward', 'role': 'steward',
            'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})
        channel = {'id': 'steward-public', 'profile_id': 'steward', 'app_id': 'cli_fixture', 'transport_tenant_key': 'tenant-transport',
            'recipient_tenant_key': 'tenant-bot', 'recipient_open_id': 'ou_lead', 'chat_id': 'oc_project', 'scope_ids': ['public'],
            'view_subjects': [OWNER.subject], 'wiki_mention_open_id': 'ou_unused'}
        grant = source_grant()
        grant['query_subjects']['fixture:lead'] = ['public']
        grant['public_channels'] = [channel]
        manager.register_knowledge_source(OWNER, manager.read_snapshot(OWNER)['version'], grant)
        source = migration_registration()
        source['new_profile_id'] = 'mono-lead'
        manager.register_archive_source(OWNER, source)
        config = {**CONFIG, 'bindings': [{**CONFIG['bindings'][0], 'profile_id': 'steward', 'project_id': None}]}
        adapter, sent = object(), []
        entry = FeishuEntry(lambda: manager, OWNER.subject, config, lambda _: pytest.fail('Archive queries cannot create work'))
        entry.attach_transport(adapter, native_transport('cli_fixture', 'ou_lead', 'om_demo_', sent))
        assert await entry.receive(event('@_user_1 封存项目 mono-lead', 'om_archive_demo'), Gateway(adapter)) == {'action': 'skip'}
        assert {p['id']: p['lifecycle'] for p in manager.read_snapshot(OWNER)['profiles']}['child-lead'] == 'archived'
        calls = list(host.calls)
        assert await entry.receive(event('@_user_1 完整档案 old-hermes public：retry', 'om_query_demo'), Gateway(adapter)) == {'action': 'skip'}
        snapshot = manager.read_snapshot(OWNER)
        query = snapshot['archive_queries'][0]
        assert query['status'] == 'complete' and query['requester'] == OWNER.subject
        assert query['old_entry'] == 'not_started' and query['coverage']['original_rows'] == 4
        assert query['channel_id'] == 'steward-public'
        content = json.loads(sent[-1].request_body.content)['zh_cn']['content'][0]
        assert content[0] == {'tag': 'at', 'user_id': 'ou_owner'}
        assert 'hermes-archive:old-root#message-1' in content[1]['text']
        assert host.calls == calls and snapshot['requests'] == []
        assert snapshot['archive_sources'][0]['protection']['status'] == 'unverified'
        steward = VerifiedIdentity('fixture:steward', 'registered-steward')
        with pytest.raises(ManagementError, match='migration'):
            manager.query_archive(steward, 'old-hermes', 'role-is-not-grant', 'retry', ['public'])
        with pytest.raises(ManagementError, match='scope'):
            manager.query_archive(OWNER, 'old-hermes', 'private', 'retry', ['private'])
        assert await entry.receive(event('@_user_1 恢复负责人 mono-lead', 'om_parent_demo'), Gateway(adapter)) == {'action': 'skip'}
        assert manager.read_snapshot(OWNER)['profiles'][1]['lifecycle'] == 'archived'
        assert await entry.receive(event('@_user_1 恢复负责人 child-lead', 'om_child_demo'), Gateway(adapter)) == {'action': 'skip'}
        assert [p['lifecycle'] for p in manager.read_snapshot(OWNER)['profiles'][:2]] == ['active', 'active']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


def test_new_descendant_and_current_work_takeover_cannot_reopen_archiving_scope(tmp_path):
    from ghost_hermes_pm import VerifiedIdentity
    host = ProfileHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host) as manager:
        host.manager = manager
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'frozen-tree'})
        child = registration(make_repo(tmp_path / 'new-child'), 'late-child', 'late-child-lead')
        child['profile'].update(identity_ref='fixture:late', role='subproject_lead', parent_profile_id='mono-lead')
        with pytest.raises(ManagementError, match='lifecycle'):
            manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], child)
        with pytest.raises(ManagementError, match='Owner'):
            manager.lifecycle(VerifiedIdentity('fixture:lead', 'registered-bot'), 'restore', {'profile_id': 'mono-lead', 'operation_id': 'bot-authority'})
        assert len(manager.read_snapshot(OWNER)['projects']) == 1


def test_restore_keeps_consistent_manager_checkpoint_and_blocks_changed_original_profile_binding(tmp_path):
    from pathlib import Path
    import sqlite3
    host = ProfileHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host) as manager:
        host.manager = manager
        repo = make_repo(tmp_path / 'repo')
        manager.apply_directory_change(OWNER, 0, registration(repo))
        host.pending.add(('mono-lead', 'bot'))
        manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'binding-archive'})
        correction = registration(repo)['profile']
        correction['native_profile'] = 'different-native'
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': correction})
        calls = list(host.calls)
        blocked = manager.lifecycle(OWNER, 'check', {'operation_id': 'binding-archive'})
        assert blocked['status'] == 'blocked'
        assert host.calls == calls
        correction['native_profile'] = 'mono-lead'
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': correction})
        host.pending.clear()
        assert manager.lifecycle(OWNER, 'check', {'operation_id': 'binding-archive'})['status'] == 'completed'
        restored = manager.lifecycle(OWNER, 'restore', {'profile_id': 'mono-lead', 'operation_id': 'checkpoint-restore'})
        assert restored['checkpoint']['status'] == 'verified_manager_directory'
        artifact = manager.state_dir / restored['checkpoint']['artifact_ref']
        with sqlite3.connect(Path(artifact).as_uri() + '?mode=ro', uri=True) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            saved = json.loads(db.execute('SELECT payload FROM directory').fetchone()[0])
        assert saved['profiles']['mono-lead']['lifecycle'] == 'restoring'
        assert saved['profiles']['mono-lead']['archive_intent']['operation_id'] == 'checkpoint-restore'
        assert restored['checkpoint']['external_state'] == 'not_backed_up'


def test_unknown_or_host_wide_native_control_capability_does_not_request_a_profile_stop(tmp_path):
    class HostWide(ProfileHost):
        def capability(self, *args):
            return {**super().capability(*args), 'scope': 'host'}
    host = HostWide()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host) as manager:
        host.manager = manager
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        result = manager.lifecycle(OWNER, 'archive', {'profile_id': 'mono-lead', 'operation_id': 'no-host-stop'})
        assert result['status'] == 'blocked'
        assert host.calls == []
        assert manager.read_snapshot(OWNER)['profiles'][0]['lifecycle'] == 'archiving'
