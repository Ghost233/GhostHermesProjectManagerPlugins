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
