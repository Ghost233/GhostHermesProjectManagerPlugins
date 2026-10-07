"""Owner maintenance through the common bridge and actual original-executor peer."""
import json
import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for
from test_requests import MESSAGE, ISSUE
from test_task_control import TURN, terminal_state, wire


def approval(client, operation_id, **extra):
    snapshot = client.read_snapshot()
    return {'operation_id': operation_id, 'expected_version': snapshot['version'],
            'expected_profile_ids': sorted(p['id'] for p in snapshot['profiles']), **extra}


def test_owner_maintenance_pauses_new_execution_and_dispatch_but_existing_supervision_continues(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        first = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, first)
        second = manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'queued'}, ISSUE)['request']['id']
        with ManagementServer(manager, {'owner': OWNER, 'reader': VerifiedIdentity('fixture:mono', 'trusted-test-entry')}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            reader = ManagementClient(tmp_path / 'state', 'reader')
            decision = approval(client, 'maintenance-31')
            with pytest.raises(ManagementError):
                reader.maintenance('enter', decision)
            entered = client.maintenance('enter', decision)
            assert entered['intent'] == 'maintenance'
            assert client.refresh_task(first)['execution'] == 'running'
            with pytest.raises(ManagementError, match='maintenance'):
                client.start_task(second)
            with pytest.raises(ManagementError, match='maintenance'):
                manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'new'}, ISSUE)
            assert manager.dispatch_tasks() == []
            snapshot = client.read_snapshot()
            assert snapshot['maintenance']['mode'] == 'maintenance'
            assert {r['id'] for r in snapshot['requests']} == {first, second}
            assert not any(r['method'] == 'turn/interrupt' for r in wire(tmp_path))


def test_checkpoint_requires_real_terminal_coverage_and_no_inflight_native_requests(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    host = MaintenanceHost(tmp_path / 'host')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), maintenance_host=host) as manager:
        first = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, first)
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.maintenance('enter', approval(client, 'upgrade-31', expected_release=host.release, target_release={'id': 'release-v2', 'plugin_version': '0.2.0', 'source_digest': 'c' * 64}))
            active = client.maintenance('checkpoint', {'operation_id': 'upgrade-31'})
            assert active['status'] == 'blocked'
            assert active.get('checkpoint') is None
            assert active['checks']['tasks'][0]['execution'] == 'running'
            terminal_state(tmp_path)
            host.pending = True
            pending = client.maintenance('checkpoint', {'operation_id': 'upgrade-31'})
            assert pending['status'] == 'blocked'
            assert pending.get('checkpoint') is None
            host.pending = False
            checkpoint = client.maintenance('checkpoint', {'operation_id': 'upgrade-31'})
            assert checkpoint['status'] == 'checkpoint_verified'
            assert checkpoint['checkpoint']['directory']['status'] == 'verified'
            assert set(checkpoint['checkpoint']['native']['categories']) == {'config', 'data', 'archive'}
            assert host.effects == []
            assert not any(r['method'] == 'turn/interrupt' for r in wire(tmp_path))


def test_deactivate_interrupts_owned_execution_preserves_queue_and_reenable_reconciles_without_restart(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    host = MaintenanceHost(tmp_path / 'host')
    repo = make_repo(tmp_path / 'repo')
    keep = repo / 'keep.txt'
    keep.write_text('Preserved work\n')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), maintenance_host=host) as manager:
        first = accepted(manager, repo)
        manager.start_task(OWNER, first)
        second = manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'queued'}, ISSUE)['request']['id']
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            pending = client.maintenance('deactivate', approval(client, 'disable-31', expected_release=host.release))
            assert pending['status'] == 'blocked'
            assert next(r for r in client.read_snapshot()['requests'] if r['id'] == first)['stop']['rpc_status'] == 'accepted'
            terminal_state(tmp_path)
            stopped = client.maintenance('check', {'operation_id': 'disable-31'})
            assert stopped['status'] == 'deactivated'
            assert client.read_snapshot()['maintenance']['mode'] == 'disabled'
            restored = client.maintenance('reenable', approval(client, 'disable-31'))
            assert restored['status'] == 'reenabled'
            snapshot = client.read_snapshot()
            assert snapshot['maintenance']['mode'] == 'active'
            assert next(r for r in snapshot['requests'] if r['id'] == first)['outer_task_status'] == 'stopped'
            assert next(r for r in snapshot['requests'] if r['id'] == second)['execution'] == 'waiting'
            assert restored['reconciliation'][0]['status'] == 'explicit_stop_preserved'
            assert keep.read_text() == 'Preserved work\n'
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/interrupt']) == 1


def test_failed_switch_restores_checkpoint_data_but_preserves_live_authority_stop_intent_and_health(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    host = MaintenanceHost(tmp_path / 'host')
    host.fail_switch = True
    repo = make_repo(tmp_path / 'repo')
    preserved = repo / 'user.txt'
    preserved.write_text('Do not reset or clean me\n')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), maintenance_host=host) as manager:
        request_id = accepted(manager, repo)
        manager.start_task(OWNER, request_id)
        manager.control_task(OWNER, request_id, 'stop', 'explicit-before-maintenance', expected_turn_id=TURN)
        terminal_state(tmp_path)
        manager.refresh_task(OWNER, request_id)
        from test_knowledge import WIKI, source_grant
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        manager.register_knowledge_source(OWNER, manager.read_snapshot(OWNER)['version'], source_grant())
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            old = dict(host.release)
            client.maintenance('enter', approval(client, 'failed-upgrade', expected_release=old, target_release={'id': 'release-v2', 'plugin_version': '0.2.0', 'source_digest': 'c' * 64}))
            client.maintenance('checkpoint', {'operation_id': 'failed-upgrade'})
            before = client.read_snapshot()
            narrowed = source_grant()
            narrowed['query_subjects'] = {WIKI['identity_ref']: ['public']}
            revocation = client.register_knowledge_source(client.read_snapshot()['version'], narrowed)
            switched = client.maintenance('switch', approval(client, 'failed-upgrade'))
            assert switched['status'] == 'switch_failed'
            assert switched['switch_request']['status'] == 'accepted'
            assert client.read_snapshot()['maintenance']['mode'] == 'maintenance'
            restored = client.maintenance('rollback', approval(client, 'failed-upgrade'))
            assert restored['status'] == 'rollback_verified'
            assert restored['restore']['manager_authority'] == 'preserved_current'
            assert restored['restore']['health'] == 'not_restored'
            assert host.current()['plugin_version'] == '0.1.0'
            assert {kind: json.loads(path.read_text()) for kind, path in host.files.items()} == {
                'config': {'version': 'config-before'}, 'data': {'version': 'data-before'}, 'archive': {'version': 'archive-before'}}
            after = client.read_snapshot()
            assert after['requests'][0]['stop'] == before['requests'][0]['stop']
            assert after['control_grants'] == before['control_grants']
            assert after['knowledge_sources'][0]['revision'] == revocation['source']['revision']
            with pytest.raises(ManagementError) as denied:
                client.query_knowledge('fixture-wiki', 'after-rollback', 'retry', ['public'])
            assert denied.value.code == 'forbidden'
            assert after['notifications']['health']['supervision'] != 'running'
            assert preserved.read_text() == 'Do not reset or clean me\n'
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1


def test_observed_manual_execution_requires_owner_handling_without_automatic_interrupt(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    from test_manual_observation import manual_state, observer, source, READ_ONLY
    host = MaintenanceHost(tmp_path / 'host')
    repo, peer = make_repo(tmp_path / 'repo'), tmp_path / 'manual'
    manual_state(peer, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-daemon': observer(peer)}, maintenance_host=host) as manager:
        from test_directory import registration
        manager.apply_directory_change(OWNER, 0, registration(repo))
        manager.register_observation_source(OWNER, source())
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            blocked = client.maintenance('deactivate', approval(client, 'manual-disable', expected_release=host.release))
            assert blocked['status'] == 'blocked'
            assert blocked['checks']['manual'][0]['control'] == 'observe_only'
            manual_state(peer, repo, 'idle')
            session_id = blocked['checks']['manual'][0]['session_id']
            still = client.maintenance('check', {'operation_id': 'manual-disable'})
            assert still['status'] == 'blocked'
            done = client.maintenance('check', {'operation_id': 'manual-disable', 'handled_manual_session_ids': [session_id]})
            assert done['status'] == 'deactivated'
            assert all(json.loads(line)['method'] in READ_ONLY for line in (peer / 'manual-wire.jsonl').read_text().splitlines())


def test_dashboard_shows_actual_version_unknown_capabilities_and_runtime_loss_as_pending_verification(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    from ghost_hermes_pm.dashboard import create_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    host = MaintenanceHost(tmp_path / 'host')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, maintenance_host=host) as manager:
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            app = FastAPI()
            app.include_router(create_router(lambda request: client))
            browser = TestClient(app)
            snapshot = browser.get('/snapshot').json()
            assert snapshot['maintenance']['runtime']['plugin_version'] == '0.1.0'
            assert snapshot['maintenance']['runtime']['release_verified'] is False
            assert browser.post('/maintenance', json={'action': 'enter', 'details': approval(client, 'dashboard-maintenance')}).status_code == 200
            manager.record_runtime_loss('forced-native-unload')
            loss = browser.get('/snapshot').json()['maintenance']
            assert loss['events'][-1]['status'] == 'pending_verification'
            assert loss['events'][-1]['execution_stopped'] is False
        offline = browser.get('/snapshot').json()['maintenance']
        assert offline['runtime']['status'] == 'unverified'
        assert offline['runtime']['release_verified'] is False


def test_unknown_current_native_version_blocks_new_work_and_migration_without_clearing_old_work(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    from test_directory import registration
    host = MaintenanceHost(tmp_path / 'host')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, maintenance_host=host) as manager:
        repo = make_repo(tmp_path / 'repo')
        manager.apply_directory_change(OWNER, 0, registration(repo))
        host.release['plugin_version'] = 'unknown'
        with pytest.raises(ManagementError) as blocked:
            manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)
        assert blocked.value.code == 'unknown_version'
        with pytest.raises(ManagementError) as migration:
            manager.migrate_profile(OWNER, 'plan', {})
        assert migration.value.code == 'unknown_version'
        assert manager.read_snapshot(OWNER)['requests'] == []
        assert manager.dispatch_tasks() == []


def test_native_backup_scope_cannot_include_live_manager_authority_or_notification_health(tmp_path):
    from ghost_hermes_pm.native_maintenance import NativeMaintenanceHost
    home = tmp_path / 'home'; plugin = home / 'plugins' / 'ghost-hermes-pm'
    plugin.mkdir(parents=True)
    config = home / 'config.yaml'; config.write_text('plugins: {}\n')
    archive = home / 'archive.json'; archive.write_text('{}')
    state = tmp_path / 'state'
    with Manager(state, owner_identity_ref=OWNER.subject):
        pass
    before = (state / 'manager.sqlite3').read_bytes()
    host = NativeMaintenanceHost(home, home, plugin, {}, [
        {'id': 'config', 'path': str(config), 'kind': 'config', 'format': 'file'},
        {'id': 'data', 'path': str(state / 'manager.sqlite3'), 'kind': 'data', 'format': 'sqlite'},
        {'id': 'archive', 'path': str(archive), 'kind': 'archive', 'format': 'file'}], None)
    with pytest.raises(ManagementError, match='authority|health'):
        Manager(state, owner_identity_ref=OWNER.subject, maintenance_host=host)
    assert (state / 'manager.sqlite3').read_bytes() == before


def test_a_plan_id_cannot_change_owner_intent_or_fixed_target_after_confirmation(tmp_path):
    from maintenance_fixture_host import MaintenanceHost
    host = MaintenanceHost(tmp_path / 'host')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, maintenance_host=host) as manager:
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            details = approval(client, 'immutable-plan', expected_release=host.release,
                               target_release={'id': 'release-v2', 'plugin_version': '0.2.0', 'source_digest': 'c' * 64})
            client.maintenance('enter', details)
            with pytest.raises(ManagementError) as changed:
                client.maintenance('enter', {**details, 'target_release': {**details['target_release'], 'id': 'another-target'}})
            assert changed.value.code == 'binding_conflict'
            with pytest.raises(ManagementError) as intent:
                client.maintenance('deactivate', details)
            assert intent.value.code == 'binding_conflict'


def test_snapshot_reports_actual_owner_and_reader_maintenance_permission_without_granting_body_authority(tmp_path):
    from test_knowledge import WIKI
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, {'profile': WIKI})
        reader_identity = VerifiedIdentity(WIKI['identity_ref'], 'trusted-reader-entry')
        with ManagementServer(manager, {'owner': OWNER, 'reader': reader_identity}):
            owner = ManagementClient(tmp_path / 'state', 'owner')
            reader = ManagementClient(tmp_path / 'state', 'reader')
            assert owner.read_snapshot()['maintenance']['permissions'] == {'status': 'verified', 'can_manage': True}
            assert reader.read_snapshot()['maintenance']['permissions'] == {'status': 'verified', 'can_manage': False}
            with pytest.raises(ManagementError) as denied:
                reader.maintenance('enter', approval(owner, 'forged-owner'))
            assert denied.value.code == 'forbidden'
