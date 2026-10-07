"""Task controls through the public bridge and synthetic JSONL subprocess peer."""
import json

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
        assert transport.sent[-1]['mention_open_id'] == 'ou_owner'
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
