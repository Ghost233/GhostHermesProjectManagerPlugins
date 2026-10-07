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
