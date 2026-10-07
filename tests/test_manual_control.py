"""Owner grants current-work control of an original manual service through the bridge."""
import json
import sys
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError, VerifiedIdentity
from ghost_hermes_pm.codex import repository_fingerprint
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for
from test_manual_observation import observer, source

ORIGINAL_THREAD = 'original-manual-thread'
ORIGINAL_TURN = 'original-manual-turn'
CONTROLLER = VerifiedIdentity('fixture:lead', 'verified-controller-entry')


def original_state(peer, repo):
    peer.mkdir(exist_ok=True)
    thread = {'id': ORIGINAL_THREAD, 'cwd': str(repo), 'cliVersion': '0.160.1', 'source': 'cli', 'canAcceptDirectInput': True,
              'status': {'type': 'active', 'activeFlags': []}, 'turns': [{'id': ORIGINAL_TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}
    (peer / 'original-state.json').write_text(json.dumps({'thread': thread}))


def adapters(peer, host_capability=True):
    from ghost_hermes_pm.takeover import OriginalControlAdapter
    read = observer(peer)
    read.command[1] = str(Path(__file__).with_name('takeover_fixture_server.py'))
    def verifier(binding, repository, context):
        if not host_capability:
            return None
        return {**binding, 'grant_binding': context, 'permission_profile': 'original-fixture-policy', 'policy_digest': 'original-fixture-policy-digest',
            'repository_fingerprint': repository_fingerprint(repository), 'runtime_roots': [repository['worktree']],
            'platform_enforcement': 'synthetic-original-only', 'tool_paths': 'synthetic-original-only', 'manual_execution_coverage': 'synthetic-original-only',
            'takeover': 'synthetic-original-only', 'model': 'fixture-model',
            'task_control': {action: 'synthetic-original-only' for action in ('append', 'stop', 'continue', 'idle_input', 'related_execution', 'human_response')},
            'process_coverage': {'kind': 'no_unregistered_process_paths', 'evidence': 'synthetic-original-only'},
            'external_actor_coverage': 'unknown', 'control_access': 'verified-original-input-path'}
    control = OriginalControlAdapter([sys.executable, str(Path(__file__).with_name('takeover_fixture_server.py')), str(peer), 'app-server', 'proxy', '--sock', str(peer / 'registered-synthetic.sock')],
        cwd=peer, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(peer / 'synthetic-home')}, service_ref='local:manual-daemon-control',
        source_kind='daemon', endpoint_ref='local:registered-daemon', verifier=verifier, timeout=1)
    return read, control


def setup(manager, repo):
    request_id = accepted(manager, repo)
    manager.register_observation_source(OWNER, source())
    observed = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
    return request_id, observed


def test_owner_takeover_controls_original_turn_and_return_keeps_it_running_without_interrupt(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            granted = owner.take_over_session(request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
            assert granted['status'] == 'active'
            assert granted['controller_profile_id'] == 'mono-lead'
            assert granted['original_executor_id'] == observed['original_executor_id']
            assert granted['thread_id'] == ORIGINAL_THREAD
            assert controller.control_task(request_id, 'append', 'manual-append', text='Complete only the accepted work.', expected_turn_id=ORIGINAL_TURN)['status'] == 'accepted'
            returned = owner.return_session_control(request_id, 'grant-current-work')
            assert returned['status'] == 'returned'
            task = owner.refresh_task(request_id)
            assert task['execution'] == 'running'
            assert task['session']['control'] == 'observe_only'
            assert task['repository_released'] is False
            with pytest.raises(ManagementError) as expired:
                controller.control_task(request_id, 'append', 'after-return', text='Do new work.', expected_turn_id=ORIGINAL_TURN)
            assert expired.value.code == 'forbidden'
        methods = [json.loads(line).get('method') for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
        assert methods.count('turn/steer') == 1
        assert not {'thread/start', 'thread/resume', 'thread/fork', 'turn/interrupt'} & set(methods)


def test_external_current_turn_change_suspends_grant_without_claiming_all_desktop_actor_detection(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            owner.take_over_session(request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
            state = json.loads((peer / 'original-state.json').read_text())
            state['thread']['turns'] = [{'id': 'externally-changed-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}]
            (peer / 'original-state.json').write_text(json.dumps(state))
            with pytest.raises(ManagementError):
                controller.control_task(request_id, 'append', 'wrong-current-turn', text='Do accepted work.', expected_turn_id=ORIGINAL_TURN)
            snapshot = owner.read_snapshot()
            assert snapshot['control_grants'][0]['status'] == 'suspended'
            assert snapshot['control_grants'][0]['external_actor_coverage'] == 'unknown'
            assert snapshot['requests'][0]['session']['control'] == 'observe_only'
            assert snapshot['requests'][0]['repository_released'] is False
    assert not any(json.loads(line).get('method') == 'turn/steer' for line in (peer / 'original-wire.jsonl').read_text().splitlines())


def test_completed_issue_delivery_ends_manual_grant_only_after_original_background_verification(tmp_path):
    from test_requests import ISSUE
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            owner.take_over_session(request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
            state = json.loads((peer / 'original-state.json').read_text())
            state['thread']['status'] = {'type': 'idle'}
            state['thread']['turns'][0].update(status='completed', items=[{'id': 'original-test', 'type': 'commandExecution', 'command': 'python -m pytest -q',
                'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}])
            state['backgrounds'] = [{'itemId': 'running-bg', 'processId': 'running-process'}]
            (peer / 'original-state.json').write_text(json.dumps(state))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['original-test']}]}
            with pytest.raises(ManagementError): owner.record_task_delivery(request_id, report)
            assert owner.read_snapshot()['control_grants'][0]['status'] == 'active'
            assert owner.read_snapshot()['requests'][0]['repository_released'] is False
            state['backgrounds'] = []
            (peer / 'original-state.json').write_text(json.dumps(state))
            assert owner.record_task_delivery(request_id, report)['task_delivery'] == 'delivered'
            snapshot = owner.read_snapshot()
            assert snapshot['control_grants'][0]['status'] == 'completed'
            assert snapshot['requests'][0]['session']['control'] == 'observe_only'
            with pytest.raises(ManagementError): owner.control_task(request_id, 'append', 'future-work', text='Do future work.', expected_turn_id=ORIGINAL_TURN)
    assert not any(json.loads(line).get('method') == 'turn/interrupt' for line in (peer / 'original-wire.jsonl').read_text().splitlines())


def test_owner_only_grant_and_concurrent_session_claim_have_one_current_controller(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from test_repository_queue import acknowledge
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        second = acknowledge(manager, suffix='other-work')
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            with pytest.raises(ManagementError) as forbidden:
                controller.take_over_session(request_id, observed['id'], 'bot-grant', ORIGINAL_TURN)
            assert forbidden.value.code == 'forbidden'
            def claim(target, grant):
                try: return owner.take_over_session(target, observed['id'], grant, ORIGINAL_TURN)['status']
                except ManagementError as exc: return exc.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda pair: claim(*pair), [(request_id, 'grant-one'), (second, 'grant-two')]))
            assert sorted(results) == ['active', 'binding_conflict']
            active = [g for g in owner.read_snapshot()['control_grants'] if g['status'] == 'active']
            assert len(active) == 1 and active[0]['controller_profile_id'] == 'mono-lead'
            with pytest.raises(ManagementError) as rebound:
                owner.take_over_session(second if active[0]['request_id'] == request_id else request_id, observed['id'], active[0]['id'], ORIGINAL_TURN)
            assert rebound.value.code == 'binding_conflict'
    assert not any(json.loads(line).get('method') in {'thread/start', 'turn/start', 'thread/resume'} for line in (peer / 'original-wire.jsonl').read_text().splitlines())


def test_missing_original_control_capability_keeps_observation_without_starting_control_proxy(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer, host_capability=False)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        before = (peer / 'original-wire.jsonl').read_text().count('initialize')
        with ManagementServer(manager, {'manual-owner': OWNER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            with pytest.raises(ManagementError) as disabled:
                owner.take_over_session(request_id, observed['id'], 'unverified-grant', ORIGINAL_TURN)
            assert disabled.value.code == 'capability_unverified'
            snapshot = owner.read_snapshot()
            assert snapshot['control_grants'][0]['status'] == 'blocked'
            assert snapshot['manual_sessions'][0]['control'] == 'observe_only'
            assert snapshot['requests'][0].get('session') is None
        assert (peer / 'original-wire.jsonl').read_text().count('initialize') == before


def test_restarted_control_connection_cannot_inherit_a_previous_current_work_grant(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        manager.take_over_session(OWNER, request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
    replacement_read, replacement_control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': replacement_read}, control_adapters={'manual-daemon': replacement_control}) as restarted:
        with ManagementServer(restarted, {'manual-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'manual-owner')
            snapshot = client.read_snapshot()
            assert snapshot['control_grants'][0]['status'] == 'suspended'
            assert snapshot['requests'][0]['repository_released'] is False
            with pytest.raises(ManagementError): client.control_task(request_id, 'append', 'old-grant-retry', text='Continue old work.', expected_turn_id=ORIGINAL_TURN)
    assert not any(json.loads(line).get('method') == 'turn/steer' for line in (peer / 'original-wire.jsonl').read_text().splitlines())


def test_original_human_response_requires_owner_and_return_expires_pending_request_without_replay(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            owner.take_over_session(request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
            state = json.loads((peer / 'original-state.json').read_text())
            state['server_requests'] = [{'id': 'original-approval', 'method': 'item/commandExecution/requestApproval', 'params': {
                'threadId': ORIGINAL_THREAD, 'turnId': ORIGINAL_TURN, 'itemId': 'original-cmd', 'command': 'python -m pytest -q', 'cwd': str(repo)}}]
            (peer / 'original-state.json').write_text(json.dumps(state))
            task = owner.refresh_task(request_id)
            question = task['human_requests'][0]
            response = {'decision': 'accept', 'operation_id': question['operation_id'], 'scope': 'turn'}
            with pytest.raises(ManagementError) as denied:
                controller.answer_human_request(request_id, question['id'], 'bot-approval', response)
            assert denied.value.code == 'forbidden'
            answered = owner.answer_human_request(request_id, question['id'], 'owner-approval', response)
            assert answered['reply']['sent'] == 'sent'
            state = json.loads((peer / 'original-state.json').read_text())
            state['server_requests'].append({'id': 777, 'method': 'item/tool/requestUserInput', 'params': {'threadId': ORIGINAL_THREAD,
                'turnId': ORIGINAL_TURN, 'itemId': 'pending-input', 'isBlocking': True, 'questions': [{'id': 'choice', 'header': 'Choice', 'question': 'Choose a value?', 'isSecret': False, 'isOther': True, 'options': None}]}})
            (peer / 'original-state.json').write_text(json.dumps(state))
            task = owner.refresh_task(request_id)
            pending = next(q for q in task['human_requests'] if q['rpc_id'] == 777)
            owner.return_session_control(request_id, 'grant-current-work')
            with pytest.raises(ManagementError): owner.answer_human_request(request_id, pending['id'], 'late-answer', {'answers': {'choice': ['one']}})
            assert next(q for q in owner.read_snapshot()['requests'][0]['human_requests'] if q['rpc_id'] == 777)['resolution'] == 'expired'
    responses = [json.loads(line) for line in (peer / 'original-wire.jsonl').read_text().splitlines() if 'method' not in json.loads(line)]
    assert responses == [{'id': 'original-approval', 'result': {'decision': 'accept'}}]


def test_granted_manual_stop_and_explicit_continue_use_same_original_thread_and_preserve_stop(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            owner.take_over_session(request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
            assert controller.control_task(request_id, 'stop', 'manual-stop', expected_turn_id=ORIGINAL_TURN)['execution'] == 'stopping'
            state = json.loads((peer / 'original-state.json').read_text())
            state['thread']['status'] = {'type': 'idle'}
            state['thread']['turns'][0]['status'] = 'interrupted'
            (peer / 'original-state.json').write_text(json.dumps(state))
            assert owner.refresh_task(request_id)['execution'] == 'stopped'
            resumed = controller.control_task(request_id, 'continue', 'manual-continue', text='Continue this accepted work.', expected_turn_id=ORIGINAL_TURN)
            assert resumed['execution'] == 'running'
            task = owner.read_snapshot()['requests'][0]
            assert task['session']['thread_id'] == ORIGINAL_THREAD
            assert task['session']['turn_id'] == 'continued-original-turn'
            assert task['stop_records'][0]['status'] == 'confirmed'
    methods = [json.loads(line).get('method') for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
    assert methods.count('turn/interrupt') == 1 and methods.count('turn/start') == 1
    assert not {'thread/start', 'thread/resume', 'thread/fork'} & set(methods)


def test_new_explicit_grant_for_same_current_work_does_not_reactivate_old_pending_approval(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER, 'manual-controller': CONTROLLER}):
            owner = ManagementClient(tmp_path / 'state', 'manual-owner')
            controller = ManagementClient(tmp_path / 'state', 'manual-controller')
            owner.take_over_session(request_id, observed['id'], 'first-grant', ORIGINAL_TURN)
            state = json.loads((peer / 'original-state.json').read_text())
            state['server_requests'] = [{'id': 'old-pending', 'method': 'item/commandExecution/requestApproval', 'params': {
                'threadId': ORIGINAL_THREAD, 'turnId': ORIGINAL_TURN, 'itemId': 'old-command', 'command': 'python -m pytest -q', 'cwd': str(repo)}}]
            (peer / 'original-state.json').write_text(json.dumps(state))
            old = owner.refresh_task(request_id)['human_requests'][0]
            owner.return_session_control(request_id, 'first-grant')
            assert owner.take_over_session(request_id, observed['id'], 'new-explicit-grant', ORIGINAL_TURN)['status'] == 'active'
            with pytest.raises(ManagementError):
                owner.answer_human_request(request_id, old['id'], 'new-old-approval', {'decision': 'accept', 'operation_id': old['operation_id'], 'scope': 'turn'})
            assert controller.control_task(request_id, 'append', 'new-grant-input', text='Finish the same accepted work.', expected_turn_id=ORIGINAL_TURN)['status'] == 'accepted'
            assert owner.return_session_control(request_id, 'first-grant')['duplicate'] is True
            assert owner.read_snapshot()['requests'][0]['session']['control'] == 'assigned_task'
    assert not any('result' in json.loads(line) and 'method' not in json.loads(line) for line in (peer / 'original-wire.jsonl').read_text().splitlines())


def test_native_control_config_does_not_connect_without_complete_hashed_current_grant_receipts(tmp_path):
    from ghost_hermes_pm.takeover import configured_control_adapters
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, _ = adapters(peer)
    config = {'source_id': 'manual-daemon', 'executable': sys.executable, 'cwd': str(peer),
        'environment': {'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(peer / 'synthetic-home')}, 'service_ref': 'local:manual-daemon-control',
        'source_kind': 'daemon', 'endpoint': str(peer / 'explicit-approved.sock'), 'endpoint_ref': 'local:registered-daemon'}
    controls = configured_control_adapters([config], tmp_path / 'state')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters=controls) as manager:
        request_id, observed = setup(manager, repo)
        before = (peer / 'original-wire.jsonl').read_text().count('initialize')
        with ManagementServer(manager, {'manual-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'manual-owner')
            with pytest.raises(ManagementError): client.take_over_session(request_id, observed['id'], 'host-grant', ORIGINAL_TURN)
            (tmp_path / 'state' / 'manual-control.json').write_text(json.dumps({'enabled': True, 'permission_profile': 'full-access', 'takeover': 'PASS'}))
            with pytest.raises(ManagementError): client.take_over_session(request_id, observed['id'], 'new-host-grant', ORIGINAL_TURN)
            assert all(g['status'] == 'blocked' for g in client.read_snapshot()['control_grants'])
        assert (peer / 'original-wire.jsonl').read_text().count('initialize') == before


def test_foreign_original_service_evidence_and_unsupported_desktop_do_not_enable_control(tmp_path):
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    provider = control.control_verifier
    control.control_verifier = lambda binding, repository, context: {**provider(binding, repository, context), 'service_id': 'another-executor-with-same-history'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        request_id, observed = setup(manager, repo)
        with ManagementServer(manager, {'manual-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'manual-owner')
            with pytest.raises(ManagementError) as wrong: client.take_over_session(request_id, observed['id'], 'wrong-service', ORIGINAL_TURN)
            assert wrong.value.code == 'capability_unverified'
            control.source_kind = 'desktop'
            with pytest.raises(ManagementError): client.take_over_session(request_id, observed['id'], 'unsupported-desktop', ORIGINAL_TURN)
            assert client.read_snapshot()['manual_sessions'][0]['control'] == 'observe_only'
    methods = [json.loads(line).get('method') for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
    assert not {'turn/start', 'turn/steer', 'turn/interrupt', 'thread/start', 'thread/resume'} & set(methods)


@pytest.mark.asyncio
async def test_group_dashboard_show_same_owner_grant_and_non_interrupting_return(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    from test_requests import ISSUE
    from test_task_execution import execution_registration
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    surface, transport = object(), Transport()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control}) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(repo))
        manager.register_observation_source(OWNER, source())
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: ISSUE)
        intake.attach_transport(surface, transport)
        await intake.receive(event(), Gateway(surface))
        task = manager.read_snapshot(OWNER)['requests'][0]
        observed = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
        async def command(text, message_id):
            incoming = event(text, message_id)
            incoming.raw_message.event.message.parent_id = task['task_start_anchor']['message_id']
            return await intake.receive(incoming, Gateway(surface))
        await command('接管本次工作：' + observed['id'] + ' 回合：' + ORIGINAL_TURN, 'om_takeover')
        with ManagementServer(manager, {'manual-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'manual-owner')
            app = FastAPI(); app.include_router(create_router(lambda request: client))
            browser = TestClient(app)
            snapshot = browser.get('/snapshot').json()
            assert snapshot == client.read_snapshot()
            assert snapshot['control_grants'][0]['status'] == 'active'
            assert browser.post('/task', json={'action': 'return', 'request_id': task['id'], 'grant_id': snapshot['control_grants'][0]['id'], 'actor': OWNER.subject}).status_code == 422
            await command('追加：Finish this accepted work.', 'om_granted_append')
            await command('归还本次控制', 'om_return')
            snapshot = browser.get('/snapshot').json()
            assert snapshot == client.read_snapshot()
            assert snapshot['control_grants'][0]['status'] == 'returned'
            assert snapshot['requests'][0]['session']['control'] == 'observe_only'
            assert snapshot['requests'][0]['repository_released'] is False
            feedback = [s for s in transport.sent if '本次工作接管' in s['text'] or '控制已归还' in s['text']]
            assert len(feedback) == 2 and all(s['reply_to'] == task['task_start_anchor']['message_id'] and s['mention_open_id'] == 'ou_owner' for s in feedback)
    methods = [json.loads(line).get('method') for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
    assert methods.count('turn/steer') == 1 and 'turn/interrupt' not in methods


@pytest.mark.parametrize('authorization', ['active', 'returned', 'new_grant_after_query'])
def test_scoped_wiki_facts_use_original_manual_executor_and_cannot_cross_grant_epoch(tmp_path, authorization):
    from test_knowledge import local_provider, WIKI, prepared_result
    repo = make_repo(tmp_path / 'repo')
    peer = tmp_path / 'original'
    original_state(peer, repo)
    read, control = adapters(peer)
    provider = local_provider(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path),
                 observation_adapters={'local:manual-daemon': read}, control_adapters={'manual-daemon': control},
                 knowledge_providers={'local:fixture-wiki': provider}) as manager:
        request_id, observed = setup(manager, repo)
        manager.take_over_session(OWNER, request_id, observed['id'], 'grant-current-work', ORIGINAL_TURN)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': WIKI})
        with ManagementServer(manager, {'manual-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'manual-owner')
            query_id = prepared_result(manager, client, tmp_path, request_id, auto=False)
            if authorization != 'active': client.return_session_control(request_id, 'grant-current-work')
            if authorization == 'new_grant_after_query': client.take_over_session(request_id, observed['id'], 'new-explicit-grant', ORIGINAL_TURN)
            result = client.supplement_knowledge(query_id)
            assert result['status'] == ('accepted' if authorization == 'active' else 'materials_only')
            snapshot = client.read_snapshot()
            assert snapshot['knowledge_queries'][0]['requester'] == OWNER.subject
            assert snapshot['knowledge_queries'][0]['scope_ids'] == ['public']
            assert snapshot['knowledge_queries'][0]['materials']
    steering = [json.loads(line) for line in (peer / 'original-wire.jsonl').read_text().splitlines() if json.loads(line).get('method') == 'turn/steer']
    assert len(steering) == (1 if authorization == 'active' else 0)
    if steering:
        assert steering[0]['params']['threadId'] == ORIGINAL_THREAD and steering[0]['params']['expectedTurnId'] == ORIGINAL_TURN
        assert 'untrusted source data' in steering[0]['params']['input'][0]['text']
    assert not (tmp_path / 'wire.jsonl').exists(), 'Wiki facts must not connect or control the independent owned executor.'
