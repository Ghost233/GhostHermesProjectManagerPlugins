import tempfile
from pathlib import Path
import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import registration, make_repo, OWNER


def test_authenticated_bridge_uses_one_manager_and_cleans_only_its_socket(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with tempfile.TemporaryDirectory(prefix='hpm-', dir='/tmp') as runtime:
        state = Path(runtime)
        preserved = state / 'user-file'
        preserved.write_text('keep')
        with Manager(state, owner_identity_ref=OWNER.subject) as manager:
            with ManagementServer(manager, {'fixture-owner-token': OWNER}) as server:
                client = ManagementClient(state, 'fixture-owner-token')
                client.apply_directory_change(0, registration(repo))
                assert client.read_snapshot() == manager.read_snapshot(OWNER)
                forged = ManagementClient(state, 'not-the-owner-token')
                with pytest.raises(ManagementError) as rejected:
                    forged.read_snapshot()
                assert rejected.value.code == 'unauthorized'
                with pytest.raises(ManagementError):
                    ManagementServer(manager, {'another-token': OWNER}).start()
            assert not (state / 'manager.sock').exists()
        assert preserved.read_text() == 'keep'
        with Manager(state, owner_identity_ref=OWNER.subject) as restarted:
            assert len(restarted.read_snapshot(OWNER)['profiles']) == 1


def test_bridge_reads_complete_large_snapshot_without_losing_frozen_material(tmp_path):
    from test_requests import MESSAGE, ISSUE
    with tempfile.TemporaryDirectory(prefix='hpm-large-', dir='/tmp') as state:
        with Manager(state, owner_identity_ref=OWNER.subject) as manager:
            manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
            for number in range(9):
                record = manager.accept_request(OWNER, 'mono', 'mono-lead',
                    {**MESSAGE, 'message_id': 'om_large_' + str(number)}, {**ISSUE, 'body': 'A' * 60000})['request']
                manager.publish_request_message(OWNER, record['id'], 'confirmation', '已受理')
                manager.publish_request_message(OWNER, record['id'], 'material', 'A' * 60000)
            with ManagementServer(manager, {'fixture-token': OWNER}):
                snapshot = ManagementClient(state, 'fixture-token').read_snapshot()
            assert snapshot == manager.read_snapshot(OWNER)
            assert len(snapshot['requests']) == 9
            assert all(len(r['accepted_scope']['body']) == 60000 for r in snapshot['requests'])
            assert all(len(r['outbox'][1]['segments']) == 34 for r in snapshot['requests'])
