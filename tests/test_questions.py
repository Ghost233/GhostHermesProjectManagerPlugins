import json
import sys
from pathlib import Path

from ghost_hermes_pm import Manager
from ghost_hermes_pm.codex import CodexStdioAdapter
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import adapter_for, accepted

THREAD = '00000000-0000-7000-8000-000000000016'
TURN = '00000000-0000-7000-8000-000000000017'


def question_adapter(root):
    adapter = adapter_for(root)
    verifier = adapter.verifier
    def proof(connection, repository):
        result = verifier(connection, repository)
        result['task_control']['human_response'] = 'synthetic-peer-only'
        return result
    return CodexStdioAdapter([sys.executable, str(Path(__file__).with_name('questions_fixture_server.py')), str(root)],
        cwd=root, env={'PATH': '/usr/bin:/bin', 'CODEX_HOME': str(root / 'codex-home')},
        service_ref='local:fixture-stdio', verifier=proof, timeout=2)


def emit(root, *events):
    (root / 'requests.json').write_text(json.dumps(events))


def user_question(rpc_id=8, blocking=True, **changes):
    params = {'threadId': THREAD, 'turnId': TURN, 'itemId': 'question-item', 'isBlocking': blocking,
        'questions': [{'id': 'colour', 'header': 'Colour', 'question': 'Which colour?', 'isOther': True,
            'isSecret': False, 'options': None}], **changes}
    return {'id': rpc_id, 'method': 'item/tool/requestUserInput', 'params': params}


def replies(root):
    return [r for r in map(json.loads, (root / 'wire.jsonl').read_text().splitlines()) if 'method' not in r]


def test_owner_answers_original_live_request_after_refresh_without_new_turn(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        request_id = accepted(manager, make_repo(tmp_path / 'repo'))
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.start_task(request_id)
            emit(tmp_path, user_question())
            task = client.refresh_task(request_id)
            question = task['human_requests'][0]
            assert question['category'] == 'question' and question['blocking'] is True
            assert question['rpc_id'] == 8 and question['thread_id'] == THREAD and question['turn_id'] == TURN
            result = client.answer_human_request(request_id, question['id'], 'reply-one', {'answers': {'colour': ['Blue']}})
            assert result['reply']['received'] is True and result['reply']['sent'] == 'sent'
            updated = client.refresh_task(request_id)['human_requests'][0]
            assert updated['resolution'] == 'resolved'
            assert updated['execution_result'] == 'unverified'
        assert replies(tmp_path) == [{'id': 8, 'result': {'answers': {'colour': {'answers': ['Blue']}}}}]
        wire = [r.get('method') for r in map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines())]
        assert wire.count('turn/start') == 1 and 'turn/steer' not in wire
