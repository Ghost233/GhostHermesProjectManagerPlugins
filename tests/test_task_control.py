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
