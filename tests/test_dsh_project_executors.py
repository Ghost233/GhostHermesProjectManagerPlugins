"""Multiple local project bindings use their own protected DSH adapter."""
import json
from types import SimpleNamespace

import pytest

from ghost_hermes_pm import ManagementError
from ghost_hermes_pm import native
from ghost_hermes_pm.manager import Manager
from ghost_hermes_pm.takeover import executor_for


def native_remote(reference):
    return {'mode': 'remote', 'base_url': 'http://127.0.0.1:9', 'cookie': 'synthetic-auth=value',
            'service_ref': reference, 'source_kind': 'desktop', 'endpoint_ref': 'local:synthetic-endpoint'}


def test_native_executor_map_requires_exact_protected_binding_before_construction(tmp_path, monkeypatch):
    from ghost_hermes_pm import dsh
    called = []
    monkeypatch.setattr(dsh, 'configured_adapter', lambda *args: called.append(args))
    with pytest.raises(ManagementError) as mismatch:
        native._configured_executors({'local:synthetic-a': native_remote('local:synthetic-b')}, tmp_path)
    assert mismatch.value.code == 'invalid_change'
    assert called == []


@pytest.mark.parametrize('configuration', [[], {'': {}}, {'public:synthetic': {}}, {'local:synthetic-a': None}])
def test_native_executor_map_rejects_invalid_shapes(tmp_path, configuration):
    with pytest.raises(ManagementError) as rejected:
        native._configured_executors(configuration, tmp_path)
    assert rejected.value.code == 'invalid_change'


def test_native_executor_map_builds_exact_explicit_adapters_without_connecting(tmp_path):
    adapters = native._configured_executors({ref: native_remote(ref) for ref in ('local:synthetic-a', 'local:synthetic-b')}, tmp_path)
    try:
        assert list(adapters) == ['local:synthetic-a', 'local:synthetic-b']
        assert all(adapter.service_ref == ref and adapter.connection is None for ref, adapter in adapters.items())
    finally:
        for adapter in adapters.values():
            adapter.close()


def test_task_executor_requires_exact_reference_and_never_uses_an_unrelated_fallback(tmp_path):
    mapped = SimpleNamespace(service_ref='local:synthetic-a', close=lambda: None)
    legacy = SimpleNamespace(service_ref='local:legacy', close=lambda: None)
    manual, recovered = object(), object()
    with Manager(tmp_path / 'state', owner_identity_ref='fixture:owner', dsh_adapter=legacy,
                 dsh_adapters={mapped.service_ref: mapped}) as manager:
        assert executor_for(manager, {'accepted_dsh_ref': mapped.service_ref}) is mapped
        assert executor_for(manager, {'session': {'service_ref': mapped.service_ref}}) is mapped
        assert executor_for(manager, {'accepted_dsh_ref': legacy.service_ref}) is legacy
        assert executor_for(manager, {'accepted_dsh_ref': 'local:missing'}) is None
        assert executor_for(manager, {}) is None
        assert executor_for(manager, {'accepted_dsh_ref': mapped.service_ref, 'session': {'service_ref': legacy.service_ref}}) is None
        manager.control_adapters['manual-fixture'] = manual
        manager.recovery_adapters['recovery-fixture'] = recovered
        assert executor_for(manager, {'accepted_dsh_ref': mapped.service_ref,
            'session': {'origin': 'manual_takeover', 'manual_source_id': 'manual-fixture'}}) is manual
        assert executor_for(manager, {'accepted_dsh_ref': mapped.service_ref,
            'session': {'recovery_ref': 'recovery-fixture'}}) is recovered
        manager.control_adapters.clear()
        manager.recovery_adapters.clear()


def test_manager_rejects_a_map_key_that_names_another_adapter(tmp_path):
    adapter = SimpleNamespace(service_ref='local:synthetic-b')
    with pytest.raises(ManagementError) as mismatch:
        Manager(tmp_path / 'state', owner_identity_ref='fixture:owner', dsh_adapters={'local:synthetic-a': adapter})
    assert mismatch.value.code == 'invalid_change'
    assert not (tmp_path / 'state').exists()


def test_unload_closes_each_owned_adapter_once_even_when_singular_is_also_mapped(tmp_path):
    closed = []
    first = SimpleNamespace(service_ref='local:synthetic-a', close=lambda: closed.append('first'))
    second = SimpleNamespace(service_ref='local:synthetic-b', close=lambda: closed.append('second'))
    with Manager(tmp_path / 'state', owner_identity_ref='fixture:owner', dsh_adapter=first,
                 dsh_adapters={first.service_ref: first, second.service_ref: second}):
        pass
    assert closed == ['first', 'second']


def test_owner_snapshot_lists_each_connected_explicit_executor_once(tmp_path):
    from test_directory import OWNER
    def adapter(reference):
        return SimpleNamespace(service_ref=reference, connection={'service_id': reference}, generation='synthetic-generation',
            close=lambda: None, server_requests=lambda _: [{'envelope': {'id': 'synthetic-request',
                'method': 'approval/request'}, 'state': 'pending'}])
    first, second = adapter('local:synthetic-a'), adapter('local:synthetic-b')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=first,
                 dsh_adapters={first.service_ref: first, second.service_ref: second}) as manager:
        requests = manager.read_snapshot(OWNER)['original_interface_requests']
        assert len(requests) == 2
        assert {request['service_id'] for request in requests} == {first.service_ref, second.service_ref}
        assert all(request['answerable'] is False for request in requests)


def test_two_project_tasks_run_on_their_bound_adapters_without_reloading_manager(tmp_path):
    from readiness_support import ReadyManager
    from ghost_hermes_pm.transport import ManagementClient, ManagementServer
    from test_directory import OWNER, make_repo, registration
    from test_repository_queue import acknowledge
    from test_task_execution import adapter_for, prepare_fixture
    first_root, second_root = tmp_path / 'first-peer', tmp_path / 'second-peer'
    first_root.mkdir(); second_root.mkdir()
    first, second = adapter_for(first_root), adapter_for(second_root)
    first.service_ref, second.service_ref = 'local:synthetic-a', 'local:synthetic-b'
    with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                      dsh_adapters={first.service_ref: first, second.service_ref: second}) as manager:
        first_root.joinpath('state').symlink_to(manager.state_dir, target_is_directory=True)
        second_root.joinpath('state').symlink_to(manager.state_dir, target_is_directory=True)
        tasks = []
        for name, adapter in [('first', first), ('second', second)]:
            repo = make_repo(tmp_path / (name + '-repo'))
            value = registration(repo, name, name + '-lead')
            value['profile']['identity_ref'] = 'fixture:' + name
            value['profile']['connection_refs']['dsh'] = adapter.service_ref
            manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], value)
            request_id = acknowledge(manager, name, name + '-lead', name)
            prepare_fixture(manager, request_id, repo)
            tasks.append((request_id, repo, adapter))
        with ManagementServer(manager, {'synthetic-owner-bridge': OWNER}):
            client = ManagementClient(manager.state_dir, 'synthetic-owner-bridge')
            for request_id, _, _ in tasks:
                assert client.start_task(request_id)['status'] == 'running'
            records = {record['id']: record for record in client.read_snapshot()['requests']}
            assert all(records[request_id]['session']['service_ref'] == adapter.service_ref
                       for request_id, _, adapter in tasks)
            assert all(client.refresh_task(request_id)['execution'] == 'running' for request_id, _, _ in tasks)
        for root, (_, repo, _) in zip((first_root, second_root), tasks):
            calls = [json.loads(line) for line in root.joinpath('wire.jsonl').read_text().splitlines()]
            creates = [call for call in calls if call['method'] == 'fixture/create']
            assert len(creates) == 1 and creates[0]['params']['cwd'] == str(repo)
    assert first._process.poll() is not None and second._process.poll() is not None
