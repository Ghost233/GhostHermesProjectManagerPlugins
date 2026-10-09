"""Observe durable task intent through the public snapshot while RPC is in flight."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

from readiness_support import ReadyManager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for


def test_public_snapshot_observes_committed_intent_before_rpc_returns(tmp_path):
    (tmp_path / 'behavior.json').write_text(json.dumps({'delay': {'fixture/start': 1.0}}))
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}), ThreadPoolExecutor(max_workers=1) as pool:
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            started = pool.submit(client.start_task, request_id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                wire = tmp_path / 'wire.jsonl'
                if wire.exists() and any(json.loads(line)['method'] == 'fixture/start' for line in wire.read_text().splitlines()):
                    break
                time.sleep(.01)
            else:
                raise AssertionError('Original service did not receive turn/start.')
            snapshot = client.read_snapshot(committed=True)
            assert snapshot['requests'][0]['session']['start_phase'] == 'turn_start_intent'
            assert not started.done(), 'Snapshot must be observable before original RPC response.'
            assert started.result()['status'] == 'running'


def test_committed_snapshot_does_not_reconcile_send_or_change_capability(tmp_path):
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            before = client.read_snapshot()
            events = (tmp_path / 'wire.jsonl').read_text()
            observed = client.read_snapshot(committed=True)
            after = client.read_snapshot()
            assert observed['status'] == 'committed_snapshot' and observed['observed_at']
            assert observed['execution'] == 'unverified'
            assert observed['requests'][0]['execution_capability']['enabled'] is False
            assert before['version'] == observed['version'] == after['version']
            assert before['requests'] == after['requests']
            assert before['requests'][0]['execution_capability']['enabled'] is True
            assert (tmp_path / 'wire.jsonl').read_text() == events
            assert set(observed) >= {'projects', 'profiles', 'requests', 'version'}
            assert 'payload' not in observed and 'schema_version' not in observed


def test_committed_snapshot_uses_normal_role_and_project_visibility(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError, VerifiedIdentity
    from test_directory import registration
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        first = registration(make_repo(tmp_path / 'first'))
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], first)
        second = registration(make_repo(tmp_path / 'second'), 'other', 'other-lead')
        second['profile']['identity_ref'] = 'fixture:other'
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], second)
        lead = VerifiedIdentity('fixture:lead', 'owned-participant')
        unknown = VerifiedIdentity('fixture:unknown', 'owned-unregistered')
        with ManagementServer(manager, {'owner': OWNER, 'lead': lead, 'unknown': unknown}):
            client = ManagementClient(tmp_path / 'state', 'lead')
            assert client.read_snapshot(committed=True)['projects'] == client.read_snapshot()['projects']
            assert [p['id'] for p in client.read_snapshot(committed=True)['projects']] == ['mono']
            assert client.read_snapshot('other', committed=True)['projects'] == []
            assert len(ManagementClient(tmp_path / 'state', 'owner').read_snapshot(committed=True)['projects']) == 2
            with pytest.raises(ManagementError) as denied:
                ManagementClient(tmp_path / 'state', 'unknown').read_snapshot(committed=True)
            assert denied.value.code == 'unauthorized'


def test_committed_public_reader_filters_new_sensitive_values(tmp_path):
    from ghost_hermes_pm.snapshots import SnapshotReader
    from test_directory import registration
    secrets = []
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, sensitive_values=lambda: secrets) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        version = manager.read_snapshot(OWNER)['version']
        secrets.append('My Mono')
        reader = SnapshotReader(tmp_path / 'state', owner_identity_ref=OWNER.subject, sensitive_values=lambda: secrets)
        snapshot = reader.read_snapshot(OWNER)
        assert snapshot['projects'][0]['name'] == '[redacted]'
        assert 'My Mono' not in json.dumps(snapshot)
        assert reader.read_snapshot(OWNER)['version'] == version


def test_original_service_rejects_missing_durable_thread_before_send(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    from test_task_execution import execution_registration
    adapter = adapter_for(tmp_path)
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(make_repo(tmp_path / 'repo')))
        repository = manager.read_snapshot(OWNER)['projects'][0]['repo']
        proof = adapter.verify_start(repository)
        response = adapter.start_thread(repository, proof)
        with pytest.raises(ManagementError):
            adapter.start_turn(response['thread']['id'], 'Owned invalid order: no durable manager session yet.')
        observed = [json.loads(line) for line in (tmp_path / 'public-snapshots.jsonl').read_text().splitlines()]
        assert observed[-1]['method'] == 'fixture/start'
        assert not any(r.get('session', {}).get('thread_id') == response['thread']['id'] for r in observed[-1]['snapshot']['requests'])
        events = [json.loads(line) for line in (tmp_path / 'oracle-events.jsonl').read_text().splitlines()]
        assert events[-1] == {'method': 'fixture/start', 'requirement': 'thread must be durable before turn/start', 'satisfied': False}
