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

import pytest
from ghost_hermes_pm import ManagementError, VerifiedIdentity


def approval(method='item/commandExecution/requestApproval', rpc_id='8', **changes):
    return {'id': rpc_id, 'method': method, 'params': {'threadId': THREAD, 'turnId': TURN,
        'itemId': 'operation-item', 'startedAtMs': 1, 'command': 'git status', 'cwd': '/fixture/repo', **changes}}


@pytest.mark.parametrize('envelope,category,answerable', [
    (user_question(blocking=False), 'nonblocking', True),
    (approval(), 'approval', True),
    (approval('item/fileChange/requestApproval'), 'approval', True),
    (approval('item/permissions/requestApproval', permissions={'network': {'enabled': True}, 'fileSystem': None}), 'approval', True),
    (user_question(questions=[{'id': 'secret', 'header': 'Private', 'question': 'Secret placeholder', 'isOther': False, 'isSecret': True, 'options': None}]), 'sensitive', False),
    (user_question(questions=[{'id': 'grant', 'header': 'Approve', 'question': 'Approve running shell command?', 'isOther': False, 'isSecret': False, 'options': None}]), 'approval', False),
    (approval('account/chatgptAuthTokens/refresh', token='synthetic-private-placeholder'), 'original_interface', False),
])
def test_content_classification_and_secret_material_never_enter_public_records(tmp_path, envelope, category, answerable):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, envelope)
        task = manager.refresh_task(OWNER, task_id)
        q = task['human_requests'][0]
        assert q['category'] == category and q['answerable'] is answerable
        if not answerable:
            with pytest.raises(ManagementError):
                manager.answer_human_request(OWNER, task_id, q['id'], 'no-secret', {'answers': {'secret': ['synthetic-private-answer']}})
        if category in {'sensitive', 'original_interface'}:
            raw = (tmp_path / 'state' / 'manager.sqlite3').read_bytes()
            assert b'Secret placeholder' not in raw and b'synthetic-private-placeholder' not in raw
            assert 'questions' not in q and 'operation' not in q
            assert q['original_interface']['url'] is None
            assert q['original_interface']['thread_id'] == THREAD
        assert replies(tmp_path) == []


@pytest.mark.parametrize('method,extra,decision,result', [
    ('item/commandExecution/requestApproval', {}, 'accept', {'decision': 'accept'}),
    ('item/fileChange/requestApproval', {'reason': 'Write the requested fixture file'}, 'decline', {'decision': 'decline'}),
    ('item/permissions/requestApproval', {'permissions': {'network': {'enabled': True}, 'fileSystem': {'read': ['/fixture'], 'write': ['/fixture/out']}}}, 'accept', {'permissions': {'network': {'enabled': True}}, 'scope': 'turn'}),
])
def test_explicit_owner_approval_binds_operation_and_turn_scope(tmp_path, method, extra, decision, result):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, approval(method, **extra))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        response = {'decision': decision, 'operation_id': q['operation_id'], 'scope': 'turn'}
        if 'permissions' in result:
            response['permissions'] = result['permissions']
        for bad in ({'decision': '好的'}, {**response, 'scope': 'session'}, {**response, 'operation_id': 'other-operation'}):
            with pytest.raises(ManagementError):
                manager.answer_human_request(OWNER, task_id, q['id'], 'bad', bad)
        sent = manager.answer_human_request(OWNER, task_id, q['id'], 'owner-decision', response)
        assert sent['reply']['sent'] == 'sent'
        assert manager.answer_human_request(OWNER, task_id, q['id'], 'owner-decision', response)['duplicate'] is True
        manager.refresh_task(OWNER, task_id)
        assert replies(tmp_path) == [{'id': '8', 'result': result}]


def test_numeric_and_string_incoming_ids_with_same_outgoing_id_are_distinct(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(7), user_question('7'))
        questions = manager.refresh_task(OWNER, task_id)['human_requests']
        assert len(questions) == 2 and questions[0]['id'] != questions[1]['id']
        for index, q in enumerate(questions):
            manager.answer_human_request(OWNER, task_id, q['id'], str(index), {'answers': {'colour': ['Blue']}})
        manager.refresh_task(OWNER, task_id)
        assert [(type(r['id']), r['id']) for r in replies(tmp_path)] == [(int, 7), (str, '7')]


@pytest.mark.parametrize('case', ['participant', 'foreign_turn', 'foreign_thread', 'returned', 'stopped', 'expired', 'resolved', 'race', 'missing_receipt'])
def test_invalid_or_racing_request_never_receives_a_new_answer(tmp_path, case):
    adapter = question_adapter(tmp_path)
    if case == 'missing_receipt':
        adapter = adapter_for(tmp_path)
        adapter.command[1] = str(Path(__file__).with_name('questions_fixture_server.py'))
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        event = user_question(**({'turnId': 'foreign-turn'} if case == 'foreign_turn' else {'threadId': 'foreign-thread'} if case == 'foreign_thread' else {}))
        emit(tmp_path, event)
        task = manager.refresh_task(OWNER, task_id)
        if case == 'foreign_thread':
            assert task['human_requests'] == []
            return
        q = task['human_requests'][0]
        actor = VerifiedIdentity('fixture:lead', 'participant') if case == 'participant' else OWNER
        if case in {'returned', 'stopped'}:
            with manager._lock, manager._db:
                version, data = manager._load()
                record = data['requests'][task_id]
                if case == 'returned':
                    record['session']['control'] = 'observe'
                else:
                    record['outer_task_status'] = 'stopped'
                    record['repository_released'] = True
                manager._save(version, data)
        if case == 'expired':
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': []}]}))
        if case in {'resolved', 'race'}:
            emit(tmp_path, {'method': 'serverRequest/resolved', 'params': {'threadId': THREAD, 'requestId': 8}})
            if case == 'resolved':
                assert manager.refresh_task(OWNER, task_id)['human_requests'][0]['resolution'] == 'resolved'
        with pytest.raises(ManagementError):
            manager.answer_human_request(actor, task_id, q['id'], 'denied', {'answers': {'colour': ['Blue']}})
        assert replies(tmp_path) == []


def test_disconnect_and_unknown_reply_are_reconciled_without_replay(tmp_path):
    adapter = question_adapter(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        (tmp_path / 'questions-behavior.json').write_text(json.dumps({'disconnect_after_reply': True}))
        manager.answer_human_request(OWNER, task_id, q['id'], 'unknown', {'answers': {'colour': ['Blue']}})
        manager.refresh_task(OWNER, task_id)
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'new-id', {'answers': {'colour': ['Blue']}})
    replacement = question_adapter(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=replacement) as manager:
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'unknown', {'answers': {'colour': ['Blue']}})
        assert replacement.connection is None
    assert len(replies(tmp_path)) == 1


def test_authenticated_dashboard_answer_and_forged_actor_boundary(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        with ManagementServer(manager, {'owner': OWNER, 'bot': VerifiedIdentity('fixture:lead', 'bot')}):
            app = FastAPI()
            app.include_router(create_router(lambda request: ManagementClient(tmp_path / 'state', request.headers.get('x-fixture-entry', 'invalid'))))
            browser = TestClient(app)
            body = {'action': 'answer', 'request_id': task_id, 'human_request_id': q['id'], 'reply_id': 'dashboard', 'response': {'answers': {'colour': ['Blue']}}}
            assert browser.post('/task', json={**body, 'actor': OWNER.subject}, headers={'x-fixture-entry': 'owner'}).status_code == 422
            assert browser.post('/task', json=body, headers={'x-fixture-entry': 'bot'}).status_code == 403
            assert browser.post('/task', json=body, headers={'x-fixture-entry': 'owner'}).status_code == 200
        assert len(replies(tmp_path)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cross_group', [False, True])
async def test_verified_owner_group_answer_uses_unique_original_request(tmp_path, cross_group):
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, event, Gateway, Transport
    import copy
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        transport, native = Transport(), object()
        config = copy.deepcopy(CONFIG)
        if cross_group:
            config['bindings'][0]['chat_id'] = 'oc_entry'
        intake = FeishuEntry(lambda: manager, OWNER.subject, config, lambda url: None)
        intake.attach_transport(native, transport)
        message = event('@_user_1 回答 ' + q['id'] + '：Blue', 'om_answer')
        if cross_group:
            message.source.chat_id = message.raw_message.event.message.chat_id = 'oc_entry'
        assert await intake.receive(message, Gateway(native)) == {'action': 'skip'}
        manager.refresh_task(OWNER, task_id)
        assert len(replies(tmp_path)) == 1
        assert manager.read_snapshot(OWNER)['requests'][0]['human_requests'][0]['reply']['source_anchor']['chat_id'] == ('oc_entry' if cross_group else 'oc_project')
        assert any(s.get('mention_open_id') == 'ou_owner' for s in transport.sent)


def test_ambiguous_human_reply_requires_clarification_instead_of_latest_request(tmp_path):
    from test_requests import MESSAGE
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(81), user_question(82))
        manager.refresh_task(OWNER, task_id)
        outcome = manager.associate_human_reply(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'om_ambiguous'}, '回答：Blue')
        assert outcome['status'] == 'needs_clarification' and len(outcome['candidate_ids']) == 2
        assert replies(tmp_path) == []


def test_natural_language_question_uses_original_expected_turn_control(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        (tmp_path / 'observed.json').write_text(json.dumps({'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full',
            'items': [{'type': 'agentMessage', 'id': 'natural-question', 'text': 'Which colour should the fixture use?'}]}]}))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        assert q['method'] == 'natural_language' and q['category'] == 'question'
        manager.answer_human_request(OWNER, task_id, q['id'], 'natural-answer', {'answers': {'answer': ['Blue']}})
        task = manager.refresh_task(OWNER, task_id)
        assert task['human_requests'][0]['reply']['sent'] == 'accepted'
        assert task['controls'][0]['id'] == 'natural-answer'
        wire = list(map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines()))
        steer = next(r for r in wire if r.get('method') == 'turn/steer')
        assert steer['params']['expectedTurnId'] == TURN and steer['params']['input'][0]['text'] == 'Blue'
        assert replies(tmp_path) == []


def test_permission_response_cannot_add_unrequested_access(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, approval('item/permissions/requestApproval', permissions={'network': None, 'fileSystem': {'read': ['/fixture'], 'write': []}}))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        for granted in ({'network': {'enabled': True}}, {'fileSystem': {'write': ['/outside']}}):
            with pytest.raises(ManagementError):
                manager.answer_human_request(OWNER, task_id, q['id'], 'escalation', {'decision': 'accept', 'operation_id': q['operation_id'], 'scope': 'turn', 'permissions': granted})
        assert replies(tmp_path) == []


@pytest.mark.parametrize('questions,category', [
    ([{'id': 'colour', 'header': 'Colour', 'question': 'Which colour?'}], 'question'),
    ([{'id': 'run', 'header': 'Run', 'question': 'Can I run this shell command?'}], 'approval'),
    ([{'id': 'api', 'header': 'Private', 'question': 'Enter your API key?'}], 'sensitive'),
])
def test_wire_defaults_and_operation_or_sensitive_content_are_respected(tmp_path, questions, category):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(questions=questions))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        assert q['category'] == category
        if category == 'question':
            manager.answer_human_request(OWNER, task_id, q['id'], 'default-wire', {'answers': {'colour': ['Blue']}})
        else:
            assert q['answerable'] is False and replies(tmp_path) == []
