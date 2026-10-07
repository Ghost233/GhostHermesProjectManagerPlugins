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
