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
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            old = dict(host.release)
            client.maintenance('enter', approval(client, 'failed-upgrade', expected_release=old, target_release={'id': 'release-v2', 'plugin_version': '0.2.0', 'source_digest': 'c' * 64}))
            client.maintenance('checkpoint', {'operation_id': 'failed-upgrade'})
            before = client.read_snapshot()
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
            assert after['notifications']['health']['supervision'] != 'running'
            assert preserved.read_text() == 'Do not reset or clean me\n'
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1
