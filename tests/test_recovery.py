"""Restart recovery through the authoritative public management bridge."""
import json
import sys
from pathlib import Path

from ghost_hermes_pm import Manager, ManagementError
from ghost_hermes_pm.codex import repository_fingerprint
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for
from test_task_control import THREAD, TURN, wire


def recovery_adapter(root, service_id, **changes):
    from ghost_hermes_pm.recovery import OriginalRecoveryAdapter
    peer = root / 'original'
    peer.mkdir(exist_ok=True)
    def verifier(binding, repository, context):
        return {**binding, 'service_id': service_id, 'recovery_binding': context,
            'repository_fingerprint': repository_fingerprint(repository), 'runtime_roots': [repository['worktree']],
            'permission_profile': 'fixture-boundary', 'policy_digest': 'fixture-policy',
            'platform_enforcement': 'synthetic-original-only', 'tool_paths': 'synthetic-original-only',
            'manual_execution_coverage': 'synthetic-original-only', 'recovery': 'synthetic-original-only',
            'control_access': 'verified-original-input-path',
            'process_coverage': {'kind': 'no_unregistered_process_paths', 'evidence': 'synthetic-original-only'},
            'task_control': {k: 'synthetic-original-only' for k in ('append', 'stop', 'continue', 'idle_input', 'related_execution', 'human_response')},
            **changes}
    return OriginalRecoveryAdapter([sys.executable, str(Path(__file__).with_name('takeover_fixture_server.py')), str(peer)],
        cwd=peer, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(peer / 'synthetic-home')},
        service_ref='local:fixture-stdio', source_kind='daemon', endpoint_ref='local:original-fixture', verifier=verifier, timeout=1)


def original_state(root, repo, *, status='inProgress'):
    peer = root / 'original'
    peer.mkdir(exist_ok=True)
    (peer / 'original-state.json').write_text(json.dumps({'thread': {'id': THREAD, 'cwd': str(repo),
        'cliVersion': '0.160.1', 'canAcceptDirectInput': True,
        'status': {'type': 'active' if status == 'inProgress' else 'idle', 'activeFlags': []},
        'turns': [{'id': TURN, 'status': status, 'itemsView': 'full', 'items': []}]}}))


def methods(root):
    path = root / 'original' / 'original-wire.jsonl'
    return [json.loads(line).get('method') for line in path.read_text().splitlines()] if path.exists() else []


def test_restart_recovers_original_running_monitor_and_stop_intent_without_double_execution(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        task = manager.start_task(OWNER, request_id)
        service_id = task['session']['service_id']
    original_state(tmp_path, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id)}) as restarted:
        with ManagementServer(restarted, {'recovery-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'recovery-owner')
            recovered = client.reconcile_task(request_id)
            assert recovered['execution'] == 'running'
            assert recovered['recovery']['status'] == 'monitoring_restored'
            assert recovered['repository_released'] is False
            assert recovered['session']['thread_id'] == THREAD
            client.control_task(request_id, 'stop', 'stop-original', expected_turn_id=TURN)
    original_state(tmp_path, repo, status='interrupted')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id)}) as restarted:
        with ManagementServer(restarted, {'recovery-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'recovery-owner')
            stopped = client.reconcile_task(request_id)
            assert stopped['execution'] == 'stopped'
            assert stopped['stop']['status'] == 'confirmed'
            assert stopped['recovery']['status'] == 'explicit_stop_preserved'
            client.reconcile_task(request_id)
    assert not {'thread/start', 'thread/resume', 'thread/fork', 'turn/start', 'turn/steer'} & set(methods(tmp_path))
    assert methods(tmp_path).count('turn/interrupt') == 1
    assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1


def test_restart_only_continues_verified_incomplete_stopped_original_work_once(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        service_id = manager.start_task(OWNER, request_id)['session']['service_id']
    original_state(tmp_path, repo, status='interrupted')
    proof = {'work_incomplete': {'thread_id': THREAD, 'turn_id': TURN, 'status': 'verified_incomplete', 'evidence': 'controlled-interrupted-unfinished-work'}}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id, **proof)}) as manager:
        with ManagementServer(manager, {'recovery-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'recovery-owner')
            resumed = client.reconcile_task(request_id)
            assert resumed['recovery']['status'] == 'continued_original_task'
            assert resumed['execution'] == 'running'
            assert resumed['session']['thread_id'] == THREAD
            assert resumed['session']['turn_id'] == 'continued-original-turn'
            assert resumed['repository_released'] is False
            client.reconcile_task(request_id)
    assert methods(tmp_path).count('turn/start') == 1
    state = json.loads((tmp_path / 'original' / 'original-state.json').read_text())
    assert state['thread']['turns'][0]['id'] == TURN
    assert not {'thread/start', 'thread/resume', 'thread/fork', 'turn/steer'} & set(methods(tmp_path))


def test_reconnect_old_human_request_points_to_original_interface_and_never_reuses_approval(tmp_path):
    import pytest
    from test_questions import question_adapter, emit, user_question, replies
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        service_id = manager.start_task(OWNER, request_id)['session']['service_id']
        emit(tmp_path, user_question())
        old = manager.refresh_task(OWNER, request_id)['human_requests'][0]
        assert old['control_enabled'] is True
    original_state(tmp_path, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id)}) as manager:
        with ManagementServer(manager, {'recovery-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'recovery-owner')
            task = client.reconcile_task(request_id)
            question = task['human_requests'][0]
            assert question['resolution'] == 'unverified'
            assert question['control_enabled'] is False
            assert question['original_interface']['availability'] == 'original_client_required'
            assert any('original interface' in reason for reason in task['recovery']['needs_human'])
            with pytest.raises(ManagementError):
                client.answer_human_request(request_id, old['id'], 'old-answer', {'answers': {'colour': ['Blue']}})
    assert replies(tmp_path) == []
    assert json.loads((tmp_path / 'original' / 'original-state.json').read_text()).get('responses', []) == []


def test_corrupt_durable_directory_blocks_restart_without_starting_any_service(tmp_path):
    import pytest
    state = tmp_path / 'state'
    state.mkdir()
    database = state / 'manager.sqlite3'
    database.write_bytes(b'broken original directory; preserve for recovery')
    before = database.read_bytes()
    with pytest.raises(ManagementError) as failure:
        Manager(state, owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path))
    assert failure.value.code == 'unavailable'
    assert database.read_bytes() == before
    assert not (tmp_path / 'wire.jsonl').exists()


def test_dashboard_uses_same_recovery_outcome_and_retains_last_confirmation_offline(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        manager.start_task(OWNER, request_id)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        app = FastAPI()
        app.include_router(create_router(lambda request: ManagementClient(tmp_path / 'state', 'recovery-owner')))
        browser = TestClient(app)
        with ManagementServer(manager, {'recovery-owner': OWNER}):
            response = browser.post('/task', json={'action': 'reconcile', 'request_id': request_id})
            assert response.status_code == 200
            task = response.json()
            assert task['execution'] == 'unverified'
            assert task['recovery']['status'] == 'blocked'
            assert task['recovery']['last_confirmed_execution'] == 'running'
            assert task['recovery']['last_confirmed_at']
            assert task['repository_released'] is False
            snap = browser.get('/snapshot').json()
            assert snap == manager.read_snapshot(OWNER)
            message = task['outbox'][-1]['segments'][0]['text']
            assert 'running' in message and 'blocked' in message and '需本人处理' in message
        offline = browser.get('/snapshot').json()
        assert offline['status'] == 'unverified'
        assert offline['runtime'] == 'manager_unavailable'
        assert offline['requests'] == snap['requests']


def test_native_recovery_configuration_never_connects_without_current_hashed_original_receipt(tmp_path):
    from ghost_hermes_pm.recovery import configured_recovery_adapters
    config = {'executable': sys.executable, 'cwd': str(tmp_path),
        'environment': {'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(tmp_path / 'isolated-home')},
        'service_ref': 'local:fixture-stdio', 'source_kind': 'daemon',
        'endpoint': str(tmp_path / 'original.sock'), 'endpoint_ref': 'local:original-fixture'}
    adapter = configured_recovery_adapters([config], tmp_path / 'state')['local:fixture-stdio']
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        manager.start_task(OWNER, request_id)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, recovery_adapters={adapter.service_ref: adapter}) as manager:
        task = manager.reconcile_task(OWNER, request_id)
        assert task['recovery']['status'] == 'blocked'
        assert task['repository_released'] is False
        assert adapter.connection is None
        assert task['session']['service_id'].startswith('local:fixture-stdio:')


def test_abrupt_manager_process_restart_reclaims_only_its_proven_orphan_socket(tmp_path):
    import os
    import subprocess
    import pytest
    state = tmp_path / 'state'
    state.mkdir()
    preserved = state / 'keep-user-file'
    preserved.write_text('preserved')
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('recovery_process_runner.py')), str(state)],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}, stdout=subprocess.PIPE, text=True)
    try:
        assert json.loads(process.stdout.readline())['ready'] is True
        assert ManagementClient(state, 'recovery-owner').read_snapshot()['requests'] == []
        process.kill()
        process.wait(timeout=5)
        with Manager(state, owner_identity_ref=OWNER.subject) as manager:
            with ManagementServer(manager, {'recovery-owner': OWNER}):
                assert ManagementClient(state, 'recovery-owner').read_snapshot()['status'] == 'completed'
        assert preserved.read_text() == 'preserved'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def test_unknown_append_outcome_prevents_automatic_replay_or_continuation(tmp_path):
    import pytest
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        service_id = manager.start_task(OWNER, request_id)['session']['service_id']
        (tmp_path / 'behavior.json').write_text(json.dumps({'omit_response': 'turn/steer'}))
        with pytest.raises(ManagementError):
            manager.control_task(OWNER, request_id, 'append', 'unknown-append', 'Only this original work.', TURN)
    original_state(tmp_path, repo, status='interrupted')
    incomplete = {'thread_id': THREAD, 'turn_id': TURN, 'status': 'verified_incomplete', 'evidence': 'unfinished'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id, work_incomplete=incomplete)}) as manager:
        task = manager.reconcile_task(OWNER, request_id)
        assert task['recovery']['status'] == 'outcome_unknown_preserved'
        assert task['controls'][0]['phase'] == 'outcome_unknown'
        assert task['repository_released'] is False
    assert not {'turn/start', 'turn/steer'} & set(methods(tmp_path))
    assert len([r for r in wire(tmp_path) if r['method'] == 'turn/steer']) == 1


def test_same_history_on_foreign_original_executor_does_not_recover_identity(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        service_id = manager.start_task(OWNER, request_id)['session']['service_id']
    original_state(tmp_path, repo)
    replacement = recovery_adapter(tmp_path, 'local:foreign-original-executor')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': replacement}) as manager:
        task = manager.reconcile_task(OWNER, request_id)
        assert task['session']['service_id'] == service_id
        assert task['execution'] == 'unverified'
        assert task['recovery']['status'] == 'blocked'
        assert task['repository_released'] is False
        assert replacement.connection is None
    assert methods(tmp_path) == []


def test_changed_responsibility_blocks_all_recovery_writes_and_dispatch(tmp_path):
    import pytest
    from test_task_execution import execution_registration
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, repo)
        service_id = manager.start_task(OWNER, request_id)['session']['service_id']
        correction = execution_registration(repo)['profile']
        correction['identity_ref'] = 'fixture:new-person'
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': correction})
    original_state(tmp_path, repo)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                 recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id)}) as manager:
        before = manager.read_snapshot(OWNER)['version']
        with pytest.raises(ManagementError):
            manager.reconcile_task(OWNER, request_id)
        assert manager.read_snapshot(OWNER)['version'] == before
        assert manager.read_snapshot(OWNER)['requests'][0]['repository_released'] is False
    assert methods(tmp_path) == []


def test_returned_manual_control_remains_observation_after_restart(tmp_path):
    from test_manual_control import adapters, original_state as manual_state, setup, ORIGINAL_THREAD, ORIGINAL_TURN
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    manual_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, observation_adapters={'local:manual-daemon': read},
                 control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        manager.take_over_session(OWNER, request_id, observed['id'], 'manual-current-work', ORIGINAL_TURN)
        manager.return_session_control(OWNER, request_id, 'manual-current-work')
        service_id = manager.read_snapshot(OWNER)['requests'][0]['session']['service_id']
    recovery = recovery_adapter(tmp_path, service_id, permission_profile='original-fixture-policy', policy_digest='original-fixture-policy-digest')
    recovery.service_ref = 'local:manual-daemon-control'
    recovery.endpoint_ref = 'local:registered-daemon'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, recovery_adapters={recovery.service_ref: recovery}) as manager:
        task = manager.reconcile_task(OWNER, request_id)
        assert task['execution'] == 'running'
        assert task['session']['thread_id'] == ORIGINAL_THREAD
        assert task['session']['control'] == 'observe_only'
        assert manager.read_snapshot(OWNER)['control_grants'][0]['status'] == 'returned'
        assert task['repository_released'] is False
    assert 'turn/interrupt' not in methods(tmp_path)


def test_real_process_and_connection_interruptions_recover_same_live_task_and_durable_stop(tmp_path):
    import os
    import subprocess
    import pytest
    from recovery_service_support import fixture_adapter
    peer = tmp_path / 'original-service'
    service = subprocess.Popen([sys.executable, str(Path(__file__).with_name('recovery_service_fixture.py')), 'service', str(peer)],
        env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.PIPE, text=True)
    supervisor = None
    try:
        service_id = json.loads(service.stdout.readline())['service_id']
        state = tmp_path / 'state'
        supervisor = subprocess.Popen([sys.executable, str(Path(__file__).with_name('recovery_process_runner.py')), str(state), str(peer), service_id],
            env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}, stdout=subprocess.PIPE, text=True)
        request_id = json.loads(supervisor.stdout.readline())['request_id']
        execution = json.loads((peer / 'execution.json').read_text())
        worker_pid = execution['worker_pid']
        os.kill(worker_pid, 0)
        supervisor.kill()
        supervisor.wait(timeout=5)
        with Manager(state, owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': fixture_adapter(peer, service_id, recover=True)}) as manager:
            with ManagementServer(manager, {'recovery-owner': OWNER}):
                client = ManagementClient(state, 'recovery-owner')
                task = client.reconcile_task(request_id)
                assert task['execution'] == 'running'
                assert task['session']['service_id'] == service_id
                assert task['repository_released'] is False
                assert json.loads((peer / 'execution.json').read_text())['worker_pid'] == worker_pid
                os.kill(worker_pid, 0)
                (peer / 'drop.json').write_text(json.dumps(['turn/steer']))
                with pytest.raises(ManagementError):
                    client.control_task(request_id, 'append', 'lost-append', 'Original work only.', task['session']['turn_id'])
                disconnected = client.refresh_task(request_id)
                assert disconnected['execution'] == 'unverified'
                assert disconnected['repository_released'] is False
        (peer / 'drop.json').write_text('[]')
        with Manager(state, owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': fixture_adapter(peer, service_id, recover=True)}) as manager:
            with ManagementServer(manager, {'recovery-owner': OWNER}):
                client = ManagementClient(state, 'recovery-owner')
                task = client.reconcile_task(request_id)
                assert task['execution'] == 'running'
                assert len(json.loads((peer / 'execution.json').read_text())['inputs']) == 1
                stopped = client.control_task(request_id, 'stop', 'explicit-stop-after-reconnect', expected_turn_id=task['session']['turn_id'])
                assert stopped['stop']['status'] == 'processing'
        with Manager(state, owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': fixture_adapter(peer, service_id, recover=True)}) as manager:
            with ManagementServer(manager, {'recovery-owner': OWNER}):
                task = ManagementClient(state, 'recovery-owner').reconcile_task(request_id)
                assert task['execution'] == 'stopped'
                assert task['recovery']['status'] == 'explicit_stop_preserved'
                assert task['repository_released'] is True
                assert task['session']['thread_id'] == 'owned-original-thread'
        execution = json.loads((peer / 'execution.json').read_text())
        assert execution['starts'] == 1
        assert len(execution['inputs']) == 1
        assert execution['responses'] == []
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        for process in (supervisor, service):
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                process.stdout.close()


def test_registered_original_endpoint_cannot_be_replaced_during_recovery(tmp_path):
    import os
    import subprocess
    import pytest
    from recovery_service_support import fixture_adapter
    peer = tmp_path / 'original-service'
    service = subprocess.Popen([sys.executable, str(Path(__file__).with_name('recovery_service_fixture.py')), 'service', str(peer)],
        env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.PIPE, text=True)
    try:
        service_id = json.loads(service.stdout.readline())['service_id']
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=fixture_adapter(peer, service_id)) as manager:
            request_id = accepted(manager, make_repo(tmp_path / 'repo'))
            manager.start_task(OWNER, request_id)
        replacement = fixture_adapter(peer, service_id, recover=True)
        replacement.endpoint_ref = 'local:replacement-endpoint'
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, recovery_adapters={replacement.service_ref: replacement}) as manager:
            before = manager.read_snapshot(OWNER)['version']
            with pytest.raises(ManagementError) as conflict:
                manager.reconcile_task(OWNER, request_id)
            assert conflict.value.code == 'binding_conflict'
            assert manager.read_snapshot(OWNER)['version'] == before
            assert replacement.connection is None
    finally:
        service.terminate()
        service.wait(timeout=5)
        service.stdout.close()
