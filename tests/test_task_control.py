"""Task controls through the public bridge and synthetic JSONL subprocess peer."""
import json
import pytest

from ghost_hermes_pm import Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import accepted, adapter_for

THREAD = '00000000-0000-7000-8000-000000000016'
TURN = '00000000-0000-7000-8000-000000000017'


def wire(root):
    return [json.loads(line) for line in (root / 'wire.jsonl').read_text().splitlines()]


def test_active_append_targets_original_turn_and_duplicate_does_not_resend(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            first = client.control_task(request_id, 'append', 'append-1', text='Add a regression check.', expected_turn_id=TURN)
            repeated = client.control_task(request_id, 'append', 'append-1', text='Add a regression check.', expected_turn_id=TURN)
            task = client.read_snapshot()['requests'][0]
        assert first['status'] == repeated['status'] == 'accepted'
        assert first['instruction']['phase'] == 'rpc_accepted'
        assert task['controls'][0]['thread_id'] == THREAD
        assert task['controls'][0]['turn_id'] == TURN
        controls = [r for r in wire(tmp_path) if r['method'] == 'turn/steer']
        assert len(controls) == 1
        assert controls[0]['params'] == {'threadId': THREAD, 'expectedTurnId': TURN,
            'input': [{'type': 'text', 'text': 'Add a regression check.', 'text_elements': []}], 'clientUserMessageId': 'append-1'}
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1


def test_interrupt_acceptance_is_durable_and_does_not_release_repository(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            stopped = client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            repeated = client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            task = client.read_snapshot()['requests'][0]
        assert stopped['execution'] == repeated['execution'] == 'stopping'
        assert task['stop']['status'] == 'processing'
        assert task['stop']['rpc_status'] == 'accepted'
        assert task['repository_released'] is False
        assert task['task_delivery'] == 'unmet'
        interrupts = [r for r in wire(tmp_path) if r['method'] == 'turn/interrupt']
        assert len(interrupts) == 1
        assert interrupts[0]['params'] == {'threadId': THREAD, 'turnId': TURN}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as restarted:
        task = restarted.read_snapshot(OWNER)['requests'][0]
        assert task['execution'] == 'stopping'
        assert task['stop']['status'] == 'processing'
        assert task['repository_released'] is False


def terminal_state(root, items=None):
    (root / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [
        {'id': TURN, 'status': 'interrupted', 'itemsView': 'full', 'items': items or []}]}))


def test_stop_waits_for_all_background_pages_then_preserves_work_and_ends_outer_task(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        repo = make_repo(tmp_path / 'repo')
        changed = repo / 'keep.txt'
        changed.write_text('Existing work must survive stopping.\n')
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            (tmp_path / 'background.json').write_text(json.dumps({'': {'data': [], 'nextCursor': 'page-2'},
                'page-2': {'data': [{'itemId': 'background-1', 'processId': 'peer-process', 'command': 'fixture long command',
                    'cwd': str(repo), 'osPid': None, 'cpuPercent': None, 'rssKb': None}], 'nextCursor': None}}))
            pending = client.refresh_task(request_id)
            assert pending['execution'] == 'stopping'
            assert pending['repository_released'] is False
            assert pending['stop']['related_execution'][0]['item_id'] == 'background-1'
            (tmp_path / 'background.json').write_text(json.dumps({'': {'data': [], 'nextCursor': None}}))
            confirmed = client.refresh_task(request_id)
            assert confirmed['execution'] == 'stopped'
            assert confirmed['outer_task_status'] == 'stopped'
            assert confirmed['repository_released'] is True
            assert confirmed['stop']['status'] == 'confirmed'
            assert confirmed['stop']['evidence'][0]['turn_status'] == 'interrupted'
            assert confirmed['task_delivery'] == 'unmet'
            assert changed.read_text() == 'Existing work must survive stopping.\n'
            assert confirmed['session']['thread_id'] == THREAD
        backgrounds = [r for r in wire(tmp_path) if r['method'] == 'thread/backgroundTerminals/list']
        assert any(r['params'].get('cursor') == 'page-2' for r in backgrounds)
        assert not any(r['method'] in {'thread/resume', 'thread/fork', 'thread/archive'} for r in wire(tmp_path))


def test_idle_append_starts_new_turn_on_original_thread_and_keeps_old_turn_evidence(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            terminal_state(tmp_path)
            result = client.control_task(request_id, 'append', 'idle-input-1', text='Check the final result.', expected_turn_id=TURN)
            task = client.read_snapshot()['requests'][0]
        assert result['instruction']['method'] == 'turn/start'
        assert task['session']['thread_id'] == THREAD
        assert task['session']['turn_id'] != TURN
        assert task['controls'][0]['previous_turn_id'] == TURN
        starts = [r for r in wire(tmp_path) if r['method'] == 'turn/start']
        assert starts[-1]['params']['threadId'] == THREAD
        assert starts[-1]['params']['clientUserMessageId'] == 'idle-input-1'
        assert len([r for r in wire(tmp_path) if r['method'] == 'thread/start']) == 1


def test_explicit_continue_creates_new_arrangement_on_original_session_preserving_stop(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            stopped = client.refresh_task(request_id)
            result = client.control_task(request_id, 'continue', 'continue-1', text='Continue the original accepted checks.', expected_turn_id=TURN)
            task = client.read_snapshot()['requests'][0]
            repeated = client.control_task(request_id, 'continue', 'continue-1', text='Continue the original accepted checks.', expected_turn_id=TURN)
        assert result['status'] == repeated['status'] == 'accepted'
        assert task['execution'] == 'running'
        assert task['repository_released'] is False
        assert task['session']['thread_id'] == stopped['session']['thread_id'] == THREAD
        assert task['stop_records'][0]['status'] == 'confirmed'
        assert task['stop_records'][0]['turn_id'] == TURN
        arrangement = task['execution_arrangements'][0]
        assert arrangement['request_id'] == request_id
        assert arrangement['thread_id'] == THREAD
        assert arrangement['previous_stop_id'] == 'stop-1'
        assert arrangement['turn_id'] == task['session']['turn_id'] != TURN
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 2
        assert len([r for r in wire(tmp_path) if r['method'] == 'thread/start']) == 1


def test_dashboard_stop_uses_bridge_state_and_rejects_forged_identity(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            app = FastAPI()
            app.include_router(create_router(lambda request: client))
            browser = TestClient(app)
            body = {'action': 'stop', 'request_id': request_id, 'instruction_id': 'dashboard-stop', 'expected_turn_id': TURN}
            assert browser.post('/task', json={**body, 'actor': OWNER.subject}).status_code == 422
            stopped = browser.post('/task', json=body)
            assert stopped.status_code == 200
            assert stopped.json()['execution'] == 'stopping'
            assert browser.get('/snapshot').json() == client.read_snapshot()


async def feishu_controls(root):
    from ghost_hermes_pm.messages import FeishuEntry
    from test_task_execution import execution_registration
    from test_requests import ISSUE
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    surface, transport = object(), Transport()
    with Manager(root / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(root)) as manager:
        manager.apply_directory_change(OWNER, 0, execution_registration(make_repo(root / 'repo')))
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: ISSUE)
        intake.attach_transport(surface, transport)
        await intake.receive(event(), Gateway(surface))
        task = manager.read_snapshot(OWNER)['requests'][0]
        from test_task_execution import prepare_fixture
        prepare_fixture(manager, task['id'], root / 'repo')
        manager.start_task(OWNER, task['id'])
        for text, message_id in [('追加：Add the requested check.', 'om_append'), ('停止', 'om_stop')]:
            incoming = event(text, message_id)
            incoming.raw_message.event.message.parent_id = task['task_start_anchor']['message_id']
            assert await intake.receive(incoming, Gateway(surface)) == {'action': 'skip'}
            assert await intake.receive(incoming, Gateway(surface)) == {'action': 'skip'}
        current = manager.read_snapshot(OWNER)['requests'][0]
        assert current['execution'] == 'stopping'
        assert len([r for r in wire(root) if r['method'] == 'turn/steer']) == 1
        assert len([r for r in wire(root) if r['method'] == 'turn/interrupt']) == 1
        assert transport.sent[-1]['reply_to'] == task['task_start_anchor']['message_id']
        assert transport.sent[-1]['mention_open_id'] is None
        assert any('停止处理中' in s['text'] for s in transport.sent)


def test_original_message_controls_share_durable_state_and_real_reply_anchor(tmp_path):
    import asyncio
    asyncio.run(feishu_controls(tmp_path))


def test_empty_background_list_cannot_confirm_stop_without_host_process_coverage(tmp_path):
    adapter = adapter_for(tmp_path, control_proof={'process_coverage': None})
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            pending = client.refresh_task(request_id)
            assert pending['execution'] == 'stopping'
            assert pending['repository_released'] is False
            assert 'process' in pending['stop']['reason'].lower()


def test_related_child_requires_complete_terminal_and_background_evidence(tmp_path):
    child = '00000000-0000-7000-8000-000000000019'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        repo = make_repo(tmp_path / 'repo')
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path, [{'id': 'collab-1', 'type': 'collabAgentToolCall', 'status': 'completed', 'receiverThreadIds': [child]}])
            child_state = {'id': child, 'cwd': str(repo), 'parentThreadId': THREAD, 'status': {'type': 'idle'}, 'turns': []}
            (tmp_path / 'threads.json').write_text(json.dumps({child: child_state}))
            assert client.refresh_task(request_id)['execution'] == 'stopping'
            child_state['turns'] = [{'id': 'child-turn', 'status': 'interrupted', 'itemsView': 'full', 'items': []}]
            (tmp_path / 'threads.json').write_text(json.dumps({child: child_state}))
            stopped = client.refresh_task(request_id)
            assert stopped['execution'] == 'stopped'
            assert any(e.get('thread_id') == child and e.get('background_coverage') == 'complete' for e in stopped['stop']['evidence'])


def test_unknown_append_is_not_replayed_with_same_or_new_instruction_id(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            (tmp_path / 'behavior.json').write_text(json.dumps({'omit_response': 'turn/steer'}))
            with pytest.raises(ManagementError) as unknown:
                client.control_task(request_id, 'append', 'append-unknown', text='Add the check.', expected_turn_id=TURN)
            assert unknown.value.code == 'outcome_unknown'
            repeated = client.control_task(request_id, 'append', 'append-unknown', text='Add the check.', expected_turn_id=TURN)
            assert repeated['status'] == 'outcome_unknown'
            with pytest.raises(ManagementError) as second:
                client.control_task(request_id, 'append', 'append-new-id', text='Add the check.', expected_turn_id=TURN)
            assert second.value.code == 'binding_conflict'
            assert client.read_snapshot()['requests'][0]['repository_released'] is False
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/steer']) == 1


@pytest.mark.parametrize('case', ['wrong_expected', 'changed_active_turn', 'unverified_idle_input', 'other_profile'])
def test_mismatched_or_unauthorized_append_never_sends_input(tmp_path, case):
    from ghost_hermes_pm import ManagementError, VerifiedIdentity
    from test_directory import registration
    proof = {'task_control': {'append': 'synthetic-peer-only'}} if case == 'unverified_idle_input' else None
    adapter = adapter_for(tmp_path, control_proof=proof)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        identity = OWNER
        if case == 'other_profile':
            change = registration(make_repo(tmp_path / 'other'), 'other', 'other-lead')
            change['profile']['identity_ref'] = 'fixture:other'
            manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], change)
            identity = VerifiedIdentity('fixture:other', 'fixture-authenticated-participant')
        manager.start_task(OWNER, request_id)
        if case == 'changed_active_turn':
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'active', 'activeFlags': []},
                'turns': [{'id': 'other-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}))
        if case == 'unverified_idle_input':
            terminal_state(tmp_path)
        with ManagementServer(manager, {'fixture-entry': identity}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            with pytest.raises(ManagementError):
                client.control_task(request_id, 'append', 'denied', text='Add a check.', expected_turn_id='wrong-turn' if case == 'wrong_expected' else TURN)
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1
        assert not any(r['method'] == 'turn/steer' for r in wire(tmp_path))


@pytest.mark.parametrize('pages', [{'': {'data': []}}, {'': {'data': [], 'nextCursor': 'loop'}, 'loop': {'data': [], 'nextCursor': 'loop'}},
    {'': {'data': None, 'nextCursor': None}}])
def test_incomplete_background_coverage_keeps_stop_and_occupancy(tmp_path, pages):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            (tmp_path / 'background.json').write_text(json.dumps(pages))
            pending = client.refresh_task(request_id)
            assert pending['execution'] == 'stopping'
            assert pending['repository_released'] is False
            assert pending['stop']['reason']


def test_directory_correction_does_not_broaden_old_control_or_erase_occupancy(tmp_path):
    from ghost_hermes_pm import ManagementError, VerifiedIdentity
    from test_directory import registration
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        original = make_repo(tmp_path / 'repo')
        corrected = make_repo(tmp_path / 'corrected')
        request_id = accepted(manager, original)
        manager.start_task(OWNER, request_id)
        manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'project': registration(corrected)['project']})
        with ManagementServer(manager, {'fixture-owner': OWNER, 'fixture-lead': VerifiedIdentity('fixture:lead', 'fixture-authenticated-participant')}):
            lead = ManagementClient(tmp_path / 'state', 'fixture-lead')
            with pytest.raises(ManagementError):
                lead.control_task(request_id, 'append', 'new-directory-control', text='Change the new repository.', expected_turn_id=TURN)
            owner = ManagementClient(tmp_path / 'state', 'fixture-owner')
            stopping = owner.control_task(request_id, 'stop', 'original-stop', expected_turn_id=TURN)
            assert stopping['execution'] == 'stopping'
            record = owner.read_snapshot()['requests'][0]
            assert record['session']['repository']['worktree'] == str(original)
            assert record['repository_released'] is False


def write_host_receipts(root, config, connection, repository, include_control=False):
    """Artificial host receipts test integrity parsing, never actual service acceptance."""
    import hashlib
    from pathlib import Path
    from ghost_hermes_pm.codex import repository_fingerprint
    evidence = root / 'state' / 'validation-evidence'
    evidence.mkdir(exist_ok=True)
    report = {'generation': connection['generation'], 'service_id': connection['service_id'],
        'repository_fingerprint': repository_fingerprint(repository), 'policy_digest': 'synthetic-fixed-policy',
        'permission_profile': 'fixture-boundary', 'runtime_roots': [repository['worktree']], 'model': 'fixture-model', 'receipts': {}}
    binding = {k: report[k] for k in ('generation', 'service_id', 'repository_fingerprint', 'policy_digest')}
    binding.update(platform=connection['platform'], binary_sha256=hashlib.sha256(Path(config['command'][0]).read_bytes()).hexdigest(),
        configuration_sha256=hashlib.sha256(json.dumps({'command': config['command'], 'environment': config['environment']}, sort_keys=True).encode()).hexdigest())
    allowed = ['mono_source_write', 'mono_git_index', 'mono_git_commit', 'mono_gitlink', 'test_artifact_write']
    denied = ['child_source_write', 'child_git_write', 'child_root_rename', 'ancestor_rename', 'atomic_replace', 'symlink_alias',
        'preexisting_hardlink_alias', 'new_hardlink_alias', 'unregistered_path_write', 'test_source_write', 'test_git_write', 'descendant_process_escape']
    documents = {
        'platform_enforcement': {'checks': [{'operation': name, 'outcome': 'allowed'} for name in allowed] +
            [{'operation': name, 'outcome': 'denied', 'before_sha256': 'fixture-preserved', 'after_sha256': 'fixture-preserved'} for name in denied]},
        'tool_paths': {'paths': {name: 'enforced' for name in ['model_files', 'shell_git', 'test_process', 'code_mode', 'local_mcp', 'dynamic_tools', 'filesystem_rpc', 'process_spawn', 'thread_shell']}},
        'task_start': {'actual_methods': ['initialize', 'initialized', 'permissionProfile/list', 'thread/start', 'turn/start', 'thread/read'],
            'runtime_roots': report['runtime_roots'], 'permission_profile': 'fixture-boundary', 'thread_id': THREAD, 'turn_id': TURN},
        'manual_execution_coverage': {'registered_executors_complete': True, 'competing_execution': 'none'}}
    if include_control:
        documents['task_control'] = {'actual_methods': ['thread/read', 'turn/steer', 'turn/start', 'turn/interrupt', 'thread/backgroundTerminals/list', 'thread/loaded/list'],
            'checks': {name: 'PASS' for name in ['active_append', 'idle_input', 'interrupt', 'stop_verification', 'explicit_continue', 'wrong_turn', 'duplicate_instruction', 'disconnect', 'background_pagination', 'related_children', 'exclusive_input']},
            'thread_id': THREAD, 'turn_id': TURN, 'new_turn_id': '00000000-0000-7000-8000-000000000018',
            'unregistered_process_paths': 'disabled_and_verified'}
    for kind, document in documents.items():
        raw = json.dumps({'kind': kind, 'result': 'PASS', **binding, **document}).encode()
        path = evidence / (kind + '.json')
        path.write_bytes(raw)
        report['receipts'][kind] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    (root / 'state' / 'codex-validation.json').write_text(json.dumps(report))


def test_configured_controls_require_separate_hashed_host_receipt(tmp_path):
    import sys
    from pathlib import Path
    from ghost_hermes_pm import ManagementError
    from ghost_hermes_pm.codex import configured_adapter
    config = {'command': [sys.executable, str(Path(__file__).with_name('codex_fixture_server.py')), str(tmp_path)],
        'cwd': str(tmp_path), 'environment': {'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(tmp_path / 'codex-home')}, 'service_ref': 'local:fixture-stdio'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=configured_adapter(config, tmp_path / 'state')) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            initial = client.verify_task_execution(request_id)
            repository = client.read_snapshot()['projects'][0]['repo']
            write_host_receipts(tmp_path, config, initial['connection'], repository)
            client.start_task(request_id)
            with pytest.raises(ManagementError):
                client.control_task(request_id, 'append', 'before-control-receipt', text='Add a check.', expected_turn_id=TURN)
            write_host_receipts(tmp_path, config, initial['connection'], repository, include_control=True)
            assert client.control_task(request_id, 'append', 'after-control-receipt', text='Add a check.', expected_turn_id=TURN)['status'] == 'accepted'


def test_active_turn_race_is_rejected_by_protocol_precondition_without_wrong_delivery(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            (tmp_path / 'behavior.json').write_text(json.dumps({'steer_active_turn': 'raced-turn'}))
            with pytest.raises(ManagementError) as rejected:
                client.control_task(request_id, 'append', 'racing-input', text='Add a check.', expected_turn_id=TURN)
            assert rejected.value.code == 'service_rejected'
            assert client.read_snapshot()['requests'][0]['controls'][0]['phase'] == 'rejected'
        assert not (tmp_path / 'applied-inputs.jsonl').exists()
        steer = next(r for r in wire(tmp_path) if r['method'] == 'turn/steer')
        assert steer['params']['expectedTurnId'] == TURN


def test_unsupported_background_method_cannot_confirm_stop(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            (tmp_path / 'behavior.json').write_text(json.dumps({'rpc_error': 'thread/backgroundTerminals/list'}))
            result = client.refresh_task(request_id)
            assert result['execution'] == 'stopping'
            assert result['repository_released'] is False
            assert 'rejected thread/backgroundTerminals/list' in result['stop']['reason']


def test_continue_obeys_current_repository_occupancy_and_never_auto_restarts(tmp_path):
    from ghost_hermes_pm import ManagementError
    from test_requests import MESSAGE, ISSUE
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            assert client.refresh_task(request_id)['execution'] == 'stopped'
            before = len([r for r in wire(tmp_path) if r['method'] == 'turn/start'])
            assert client.refresh_task(request_id)['execution'] == 'stopped'
            assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == before
            second = manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'om_next'}, ISSUE)['request']['id']
            manager.publish_request_message(OWNER, second, 'confirmation', '已受理后续任务')
            segment = manager.claim_delivery(OWNER, second)
            manager.record_delivery(OWNER, second, segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_next_ack'})
            from test_task_execution import prepare_fixture
            prepare_fixture(manager, second, tmp_path / 'repo')
            client.start_task(second)
            with pytest.raises(ManagementError) as busy:
                client.control_task(request_id, 'continue', 'continue-busy', text='Continue the original work.', expected_turn_id=TURN)
            assert busy.value.code == 'repository_busy'
            original = next(r for r in client.read_snapshot()['requests'] if r['id'] == request_id)
            assert original['stop_records'][0]['status'] == 'confirmed'
            assert original['execution_arrangements'][-1]['phase'] == 'queued'


def test_idle_status_does_not_hide_an_unfinished_other_turn_in_original_thread(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [
                {'id': TURN, 'status': 'interrupted', 'itemsView': 'full', 'items': []},
                {'id': 'raced-other-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}))
            result = client.refresh_task(request_id)
            assert result['execution'] == 'stopping'
            assert result['repository_released'] is False


def test_idle_input_rejects_an_unregistered_intervening_turn(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [
                {'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': []},
                {'id': 'foreign-completed-turn', 'status': 'completed', 'itemsView': 'full', 'items': []}]}))
            with pytest.raises(ManagementError) as mismatched:
                client.control_task(request_id, 'append', 'idle-stale', text='Add a check.', expected_turn_id=TURN)
            assert mismatched.value.code == 'binding_conflict'
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1


def test_delivered_task_cannot_reuse_expired_control_after_releasing_repository(tmp_path):
    from ghost_hermes_pm import ManagementError
    from test_requests import ISSUE
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        repo = make_repo(tmp_path / 'repo')
        request_id = accepted(manager, repo)
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [
                {'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'pytest-final',
                    'command': 'python -m pytest tests/test_fixture.py -q', 'cwd': str(repo), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
            report = {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-final']}],
                'source_commit': None, 'pr_url': None, 'sync_branches': []}
            assert client.record_task_delivery(request_id, report)['repository_released'] is True
            with pytest.raises(ManagementError):
                client.control_task(request_id, 'append', 'expired-control', text='Start more work.', expected_turn_id=TURN)
        assert not any(r['method'] == 'turn/steer' for r in wire(tmp_path))
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1


@pytest.mark.parametrize('field,value', [('policy_digest', 'changed-policy'), ('permission_profile', 'changed-profile')])
def test_fresh_control_proof_cannot_replace_original_task_permission_boundary(tmp_path, field, value):
    from ghost_hermes_pm import ManagementError
    host_proof = {}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path, control_proof=host_proof)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            host_proof[field] = value
            with pytest.raises(ManagementError) as changed:
                client.control_task(request_id, 'append', 'changed-permissions', text='Add a check.', expected_turn_id=TURN)
            assert changed.value.code == 'capability_unverified'
        assert not any(r['method'] == 'turn/steer' for r in wire(tmp_path))


def test_explicit_continue_requires_idle_new_turn_semantics_even_if_old_turn_looks_active(tmp_path):
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'fixture-entry': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'fixture-entry')
            client.start_task(request_id)
            client.control_task(request_id, 'stop', 'stop-1', expected_turn_id=TURN)
            terminal_state(tmp_path)
            assert client.refresh_task(request_id)['execution'] == 'stopped'
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'active', 'activeFlags': []},
                'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}))
            with pytest.raises(ManagementError) as race:
                client.control_task(request_id, 'continue', 'continue-raced', text='Continue the accepted work.', expected_turn_id=TURN)
            assert race.value.code == 'binding_conflict'
        assert not any(r['method'] == 'turn/steer' for r in wire(tmp_path))
