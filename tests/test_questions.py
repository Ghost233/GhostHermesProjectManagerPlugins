import json
import sys
from pathlib import Path

from ghost_hermes_pm import Manager
from readiness_support import ReadyManager as Manager
from normalized_executor_fixture import SyntheticExecutorAdapter
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
    return SyntheticExecutorAdapter([sys.executable, str(Path(__file__).with_name('questions_fixture_server.py')), str(root)],
        cwd=root, env=adapter.env,
        service_ref='local:fixture-stdio', verifier=proof, timeout=2)


def emit(root, *events):
    """External normalized business seam only; no native protocol claim."""
    (root / 'requests.json').write_text(json.dumps(events))


def user_question(rpc_id=8, blocking=True, **changes):
    params = {'threadId': THREAD, 'turnId': TURN, 'itemId': 'question-item', 'isBlocking': blocking,
        'native_response_available': True,
        'questions': [{'id': 'colour', 'header': 'Colour', 'question': 'Which colour?', 'isOther': True,
            'isSecret': False, 'options': None}], **changes}
    return {'id': rpc_id, 'method': 'user-questions/request', 'params': params}


def replies(root):
    return [r for r in map(json.loads, (root / 'wire.jsonl').read_text().splitlines()) if 'method' not in r]


def test_owner_answers_original_live_request_after_refresh_without_new_turn(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
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
        assert replies(tmp_path) == [{'id': 8, 'result': {'answers': [{'id': 'colour', 'selected': [], 'custom': 'Blue'}]}}]
        wire = [r.get('method') for r in map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines())]
        assert wire.count('fixture/start') == 1 and 'fixture/append' not in wire

import pytest
from ghost_hermes_pm import ManagementError, VerifiedIdentity


@pytest.mark.parametrize('available', [False, True])
def test_project_question_progress_offers_reply_only_for_verified_original_response(tmp_path, available):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(native_response_available=available))
        manager.refresh_task(OWNER, task_id)
        task = manager.read_snapshot(OWNER)['requests'][0]
        texts = [segment['text'] for publication in task['outbox'] for segment in publication['segments'] if '人工请求：' in segment['text']]
        assert texts
        assert ('本人可回复' in texts[-1]) is available
        assert ('原界面' in texts[-1]) is not available


@pytest.mark.parametrize('case', ['correlated', 'settled', 'changed_frame', 'stale_generation'])
def test_unknown_question_metadata_refresh_requires_unchanged_live_original_frame(tmp_path, monkeypatch, case):
    import copy
    adapter = question_adapter(tmp_path)
    original_reads = adapter.server_requests
    stage = {'correlated': False, 'state': 'pending', 'changed': False}
    frame = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'metadata-question',
        'agentId': THREAD, 'request': {'questions': [{'id': 'colour', 'question': 'Which colour?'}], 'wait': {'callId': 'question-item'}}}
    def reads(thread_id):
        incoming = copy.deepcopy(original_reads(thread_id))
        for item in incoming:
            if item['envelope']['id'] != 'metadata-question':
                continue
            item['state'] = stage['state']
            params = item['envelope']['params']
            params['native_frame'] = copy.deepcopy(frame)
            if stage['changed']:
                params['native_frame']['request']['questions'][0]['question'] = 'Different original request'
            if stage['correlated']:
                params.update(turnId=TURN, itemId='question-item', native_response_available=True)
        return incoming
    monkeypatch.setattr(adapter, 'server_requests', reads)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(rpc_id='metadata-question', turnId=None, native_response_available=False))
        initial = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        assert initial['turn_id'] is None and initial['resolution'] == 'unverified'
        stage['correlated'] = True
        if case == 'settled':
            stage['state'] = 'expired'
        elif case == 'changed_frame':
            stage['changed'] = True
        elif case == 'stale_generation':
            adapter.close()
            manager.dsh_adapter = question_adapter(tmp_path)
        question = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        if case == 'correlated':
            assert question['id'] == initial['id']
            assert question['turn_id'] == TURN and question['resolution'] == 'pending'
            assert question['answerable'] is True and question['control_enabled'] is True
        else:
            assert question['resolution'] != 'pending'
            assert question['answerable'] is False and question['control_enabled'] is False
        assert question['reply'] is None and replies(tmp_path) == []


def approval(method='approval/request', rpc_id='8', **changes):
    return {'id': rpc_id, 'method': method, 'params': {
        'threadId': THREAD, 'turnId': TURN, 'itemId': 'operation-item',
        'reason': 'Run the bounded synthetic operation', **changes}}


@pytest.mark.parametrize('envelope,category,answerable', [
    (user_question(blocking=False), 'nonblocking', True),
    (approval(), 'approval', False),
    (approval('approval/request'), 'approval', False),
    (approval('approval/request', permissions={'network': {'enabled': True}, 'fileSystem': None}), 'approval', False),
    (user_question(questions=[{'id': 'secret', 'header': 'Private', 'question': 'Secret placeholder', 'isOther': False, 'isSecret': True, 'options': None}]), 'sensitive', False),
    (user_question(questions=[{'id': 'grant', 'header': 'Approve', 'question': 'Approve running shell command?', 'isOther': False, 'isSecret': False, 'options': None}]), 'approval', False),
    (approval('fixture/unsupported', token='synthetic-private-placeholder'), 'original_interface', False),
])
def test_content_classification_and_secret_material_never_enter_public_records(tmp_path, envelope, category, answerable):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, envelope)
        task = manager.refresh_task(OWNER, task_id)
        q = task['human_requests'][0]
        assert q['category'] == category and q['answerable'] is answerable
        if envelope['method'] == 'user-questions/request':
            assert q['blocking'] is envelope['params']['isBlocking']
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
    ('approval/request', {}, 'accept', {'decision': 'accept'}),
    ('approval/request', {'reason': 'Write the requested fixture file'}, 'decline', {'decision': 'decline'}),
    ('approval/request', {'permissions': {'network': {'enabled': True}, 'fileSystem': {'read': ['/fixture'], 'write': ['/fixture/out']}}}, 'accept', {'permissions': {'network': {'enabled': True}}, 'scope': 'turn'}),
])
def test_unverified_native_approval_cannot_be_sent_even_with_owner_and_turn_scope(tmp_path, method, extra, decision, result):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, approval(method, **extra))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        response = {'decision': decision, 'operation_id': q.get('operation_id', 'unverified-operation'), 'scope': 'turn'}
        if 'permissions' in result:
            response['permissions'] = result['permissions']
        for bad in ({'decision': '好的'}, {**response, 'scope': 'session'}, {**response, 'operation_id': 'other-operation'}):
            with pytest.raises(ManagementError):
                manager.answer_human_request(OWNER, task_id, q['id'], 'bad', bad)
        assert q['answerable'] is False
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'owner-decision', response)
        assert replies(tmp_path) == []
        assert manager.read_snapshot(OWNER)['requests'][0]['human_requests'][0]['reply'] is None


def test_numeric_and_string_incoming_ids_with_same_outgoing_id_are_distinct(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
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
    repo = make_repo(tmp_path / 'repo')
    original = {}
    if case == 'returned':
        from test_manual_control import adapters, original_state, ORIGINAL_THREAD, ORIGINAL_TURN
        from test_manual_observation import source
        peer = tmp_path / 'original'
        original_state(peer, repo)
        read, control = adapters(peer)
        original = {'observation_adapters': {'local:manual-desktop': read}, 'control_adapters': {'manual-desktop': control}}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter, **original) as manager:
        task_id = accepted(manager, repo)
        if case == 'returned':
            manager.register_observation_source(OWNER, source())
            observed = manager.refresh_manual_sessions(OWNER)['manual_sessions'][0]
            assert manager.take_over_session(OWNER, task_id, observed['id'], 'answer-current-work', ORIGINAL_TURN)['status'] == 'active'
            state = json.loads((peer / 'original-state.json').read_text())
            state['server_requests'] = [user_question(threadId=ORIGINAL_THREAD, turnId=ORIGINAL_TURN)]
            (peer / 'original-state.json').write_text(json.dumps(state))
        else:
            manager.start_task(OWNER, task_id)
            event = user_question(**({'turnId': 'foreign-turn'} if case == 'foreign_turn' else {'threadId': 'foreign-thread'} if case == 'foreign_thread' else {}))
            emit(tmp_path, event)
        task = manager.refresh_task(OWNER, task_id)
        if case == 'foreign_thread':
            assert task['human_requests'] == []
            return
        q = task['human_requests'][0]
        actor = VerifiedIdentity('fixture:lead', 'participant') if case == 'participant' else OWNER
        if case == 'stopped':
            from test_task_control import terminal_state
            manager.control_task(OWNER, task_id, 'stop', 'public-stop', expected_turn_id=TURN)
            terminal_state(tmp_path)
            assert manager.refresh_task(OWNER, task_id)['outer_task_status'] == 'stopped'
        if case == 'returned':
            assert manager.return_session_control(OWNER, task_id, 'answer-current-work')['status'] == 'returned'
            snapshot = manager.read_snapshot(OWNER)
            assert snapshot['requests'][0]['session']['control'] == 'observe_only'
            assert snapshot['requests'][0]['repository_released'] is False
        if case == 'expired':
            (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': []}]}))
        if case in {'resolved', 'race'}:
            emit(tmp_path, {'method': 'serverRequest/resolved', 'params': {'threadId': THREAD, 'requestId': 8}})
            if case == 'resolved':
                assert manager.refresh_task(OWNER, task_id)['human_requests'][0]['resolution'] == 'resolved'
        with pytest.raises(ManagementError):
            manager.answer_human_request(actor, task_id, q['id'], 'denied', {'answers': {'colour': ['Blue']}})
        if case == 'returned':
            original_wire = [json.loads(line) for line in (peer / 'original-wire.jsonl').read_text().splitlines()]
            assert [reply for reply in original_wire if 'method' not in reply] == []
            assert not {'fixture/create', 'fixture/start', 'fixture/append', 'fixture/stop'} & {message.get('method') for message in original_wire}
            assert not (tmp_path / 'wire.jsonl').exists()
        else:
            assert replies(tmp_path) == []


def test_disconnect_and_unknown_reply_are_reconciled_without_replay(tmp_path):
    adapter = question_adapter(tmp_path)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter) as manager:
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
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=replacement) as manager:
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'unknown', {'answers': {'colour': ['Blue']}})
        assert replacement.connection is None
    assert len(replies(tmp_path)) == 1


def test_authenticated_dashboard_answer_and_forged_actor_boundary(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
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
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
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
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(81), user_question(82))
        manager.refresh_task(OWNER, task_id)
        outcome = manager.associate_human_reply(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'om_ambiguous'}, '回答：Blue')
        assert outcome['status'] == 'needs_clarification' and len(outcome['candidate_ids']) == 2
        assert replies(tmp_path) == []


def test_natural_language_question_uses_original_expected_turn_control(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
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
        steer = next(r for r in wire if r.get('method') == 'fixture/append')
        assert steer['params']['expectedTurnId'] == TURN and steer['params']['input'][0]['text'] == 'Blue'
        assert replies(tmp_path) == []


def test_permission_response_cannot_add_unrequested_access(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, approval('approval/request', permissions={'network': None, 'fileSystem': {'read': ['/fixture'], 'write': []}}))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        for granted in ({'network': {'enabled': True}}, {'fileSystem': {'write': ['/outside']}}):
            with pytest.raises(ManagementError):
                manager.answer_human_request(OWNER, task_id, q['id'], 'escalation', {'decision': 'accept', 'operation_id': q.get('operation_id', 'unverified-operation'), 'scope': 'turn', 'permissions': granted})
        assert replies(tmp_path) == []


@pytest.mark.parametrize('questions,category', [
    ([{'id': 'colour', 'header': 'Colour', 'question': 'Which colour?'}], 'question'),
    ([{'id': 'run', 'header': 'Run', 'question': 'Can I run this shell command?'}], 'approval'),
    ([{'id': 'api', 'header': 'Private', 'question': 'Enter your API key?'}], 'sensitive'),
])
def test_wire_defaults_and_operation_or_sensitive_content_are_respected(tmp_path, questions, category):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(questions=questions))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        assert q['category'] == category
        if category == 'question':
            manager.answer_human_request(OWNER, task_id, q['id'], 'default-wire', {'answers': {'colour': ['Blue']}})
        else:
            assert q['answerable'] is False and replies(tmp_path) == []


@pytest.mark.parametrize('case', ['missing', 'foreign_generation', 'incomplete', 'tampered', 'valid'])
def test_configured_response_requires_separate_current_hashed_host_receipt(tmp_path, case):
    """Synthetic exact work scope; missing answer proof transfers the delivery instead of holding it."""
    import hashlib
    import time
    from dsh_fixture_server import remote_peer
    from ghost_hermes_pm.dsh import configured_adapter
    from test_task_control import write_host_receipts
    tmp_path = tmp_path.resolve()
    records = [
        {'type': 'event', 'event': {'type': 'turn/start', 'seq': 0, 'time': 1, 'data': {'turn': 1}}},
        {'type': 'event', 'event': {'type': 'tool/call', 'seq': 1, 'time': 1,
            'data': {'turn': 1, 'step': 1, 'callId': 'synthetic-question-call', 'name': 'ask_user_question', 'arguments': '{}'}}}]
    event_id = 'synthetic-question-' + case
    question = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': event_id,
        'agentId': 'session-fixture', 'request': {'questions': [{'id': 'colour', 'question': 'Which colour?'}],
        'wait': {'callId': 'synthetic-question-call'}}}
    with remote_peer(behavior={'records': records, 'remote_events': [question], 'defer_remote_events': True}) as (url, peer):
        config = {'mode': 'remote', 'base_url': url, 'cookie': 'synthetic-auth=value', 'service_ref': 'local:fixture-stdio',
            'source_kind': 'desktop', 'endpoint_ref': 'local:synthetic-human-source', 'expected_home': '/synthetic/home'}
        adapter = configured_adapter(config, tmp_path / 'state')
        with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter) as manager:
            task_id = accepted(manager, make_repo(tmp_path / 'repo'))
            connection = manager.verify_task_execution(OWNER, task_id)['connection']
            repository = manager.read_snapshot(OWNER)['projects'][0]['repo']
            peer['sessions'][0].update(cwd=repository['worktree'], running=True)
            write_host_receipts(tmp_path, config, connection, repository, include_control=True,
                thread_id='session-fixture', turn_id='dsh-turn:1')
            report_path = tmp_path / 'state' / 'dsh-validation.json'
            report = json.loads(report_path.read_text())
            control = json.loads((tmp_path / 'state' / 'validation-evidence' / 'task_control.json').read_text())
            document = {key: value for key, value in control.items() if key not in {'actual_methods', 'checks', 'new_turn_id', 'unregistered_process_paths'}}
            document.update(kind='human_response', original_connection_responses=True,
                actual_methods=['session/follow', 'user-questions/request', '$events/result'],
                checks={key: 'PASS' for key in ['question', 'nonblocking', 'unsupported_approval',
                    'owner_only', 'wrong_request', 'duplicate', 'resolved_race', 'disconnect', 'secret', 'unknown_no_replay']})
            receipt_path = tmp_path / 'state' / 'validation-evidence' / 'human_response.json'
            def save_receipt():
                raw = json.dumps(document).encode()
                receipt_path.write_bytes(raw)
                report['receipts']['human_response'] = {'path': str(receipt_path), 'sha256': hashlib.sha256(raw).hexdigest()}
                report_path.write_text(json.dumps(report))
            save_receipt()
            proof = adapter.verify_control(repository, 'human_response', {key: report[key]
                for key in ('permission_profile', 'policy_digest', 'runtime_roots')})
            adapter.bind_repository('session-fixture', repository, proof)
            # Only this separately admitted synthetic fixture work is owned here.
            # Actual Manager authority admission is verified by its independent integration tests.
            adapter.waterfall_authority = lambda session_id, turn_id: session_id == 'session-fixture' and turn_id == 'dsh-turn:1'
            adapter.waterfall_question_supported = lambda envelope: (envelope['id'] == event_id
                and envelope['method'] == 'user-questions/request'
                and envelope['params'].get('threadId') == 'session-fixture'
                and envelope['params'].get('turnId') == 'dsh-turn:1')
            peer['emit_remote'].set()
            pending, deadline = [], time.monotonic() + 2
            while not pending and time.monotonic() < deadline:
                pending = [request for request in adapter.server_requests('session-fixture') if request['state'] == 'pending']
                if not pending:
                    time.sleep(.01)
            assert len(pending) == 1
            envelope = pending[0]['envelope']
            assert envelope['params']['turnId'] == 'dsh-turn:1'
            assert envelope['params']['native_response_available'] is True
            answer = {'answers': [{'id': 'colour', 'selected': [], 'custom': 'Blue'}]}
            if case == 'missing':
                del report['receipts']['human_response']
                report_path.write_text(json.dumps(report))
            elif case == 'foreign_generation':
                document['generation'] = 'foreign-generation'
                save_receipt()
            elif case == 'incomplete':
                del document['checks']['resolved_race']
                save_receipt()
            elif case == 'tampered':
                receipt_path.write_text(receipt_path.read_text() + '\n')
            if case == 'valid':
                adapter.respond_server_request(envelope['id'], envelope, answer)
            else:
                with pytest.raises(ManagementError):
                    adapter.respond_server_request(envelope['id'], envelope, answer)
            replies = [call for call in peer['calls'] if call['method'] == '$events/result']
            assert len(replies) == 1
            expected_outcome = {'kind': 'result', 'value': answer} if case == 'valid' else {'kind': 'next'}
            assert replies[0]['payload']['args'] == {'clientId': connection['client_id'], 'eventId': event_id,
                'outcome': expected_outcome}
            with pytest.raises(ManagementError):
                adapter.respond_server_request(envelope['id'], envelope, answer)
            assert len([call for call in peer['calls'] if call['method'] == '$events/result']) == 1
            if case != 'valid':
                # Repairing a receipt cannot revive this already-transferred original frame.
                document['generation'] = connection['generation']
                document['checks']['resolved_race'] = 'PASS'
                save_receipt()
                assert all(request['state'] != 'pending' for request in adapter.server_requests('session-fixture'))


@pytest.mark.parametrize('case', ['outside', 'nested', 'network', 'entries', 'grant_root', 'missing_patch', 'changed_patch'])
def test_approval_cannot_expand_original_task_or_approve_unknown_file_change(tmp_path, case):
    repo_path = make_repo(tmp_path / 'repo')
    if case == 'nested':
        make_repo(repo_path / 'child')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, repo_path)
        manager.start_task(OWNER, task_id)
        if case in {'grant_root', 'missing_patch', 'changed_patch'}:
            event = approval('approval/request', **({'grantRoot': str(repo_path)} if case == 'grant_root' else {}))
            emit(tmp_path, event)
            if case == 'changed_patch':
                (tmp_path / 'observed.json').write_text(json.dumps({'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': [{'type': 'fileChange', 'id': 'operation-item', 'status': 'inProgress', 'changes': [{'path': '/synthetic/file', 'diff': '+synthetic'}]}]}]}))
            if case == 'missing_patch':
                (tmp_path / 'observed.json').write_text(json.dumps({'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}))
        else:
            path = str(tmp_path / 'outside') if case == 'outside' else str(repo_path / 'child' / 'source')
            permissions = {'network': None, 'fileSystem': {'read': [], 'write': [path]}}
            granted = {'fileSystem': {'write': [path]}}
            if case == 'network':
                permissions = {'network': {'enabled': True}, 'fileSystem': None}
                granted = {'network': {'enabled': True}}
            if case == 'entries':
                permissions = {'network': None, 'fileSystem': {'read': [], 'write': [], 'entries': [{'path': {'type': 'glob', 'glob': '**'}, 'access': 'write'}]}}
                granted = {'fileSystem': permissions['fileSystem']}
            emit(tmp_path, approval('approval/request', permissions=permissions))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        if case == 'changed_patch':
            observed = json.loads((tmp_path / 'observed.json').read_text())
            observed['turns'][0]['items'][0]['changes'][0]['diff'] = '+different operation'
            (tmp_path / 'observed.json').write_text(json.dumps(observed))
        response = {'decision': 'accept', 'operation_id': q.get('operation_id', 'unverified-operation'), 'scope': 'turn'}
        if case not in {'grant_root', 'missing_patch', 'changed_patch'}:
            response['permissions'] = granted
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'no-expansion', response)
        assert replies(tmp_path) == []


@pytest.mark.asyncio
async def test_unknown_group_reply_feedback_is_not_replayed_by_duplicate_source(tmp_path):
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, event, Gateway, Transport
    class UnknownFeedback(Transport):
        async def send(self, segment):
            self.sent.append(segment)
            return {'status': 'unknown'}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        transport, native = UnknownFeedback(), object()
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: None)
        intake.attach_transport(native, transport)
        original = event('@_user_1 回答 ' + q['id'] + '：Blue', 'om_unknown_feedback')
        assert await intake.receive(original, Gateway(native)) == {'action': 'skip'}
        first_count = len(transport.sent)
        assert await intake.receive(original, Gateway(native)) == {'action': 'skip'}
        assert len(transport.sent) == first_count
        manager.refresh_task(OWNER, task_id)
        assert len(replies(tmp_path)) == 1


@pytest.mark.asyncio
async def test_real_feishu_sdk_builder_preserves_owner_approval_source_and_mention(tmp_path):
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageResponse
    from types import SimpleNamespace as NS
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, event, Gateway
    native = Client.builder().app_id('cli_fixture').app_secret('synthetic-unused-secret').build()
    native.request = lambda request: NS(code=0, raw=NS(content=json.dumps({'code': 0, 'bot': {'open_id': 'ou_lead', 'activate_status': 2}}).encode()))
    sent = []
    def reply(request):
        sent.append(request)
        return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_sdk_' + str(len(sent)), 'chat_id': 'oc_project', 'parent_id': request.message_id}})
    native.im.v1.message.reply = reply
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, approval())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        adapter = object()
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda url: None)
        intake.attach_transport(adapter, NativeFeishuTransport(native))
        source = event('@_user_1 批准 ' + q['id'] + ' 操作 ' + q.get('operation_id', 'unverified-operation') + ' 范围 turn', 'om_explicit_approval')
        source.raw_message = P2ImMessageReceiveV1(json.loads(json.dumps(source.raw_message, default=lambda value: vars(value))))
        assert await intake.receive(source, Gateway(adapter)) == {'action': 'skip'}
        manager.refresh_task(OWNER, task_id)
        assert replies(tmp_path) == []
        assert manager.read_snapshot(OWNER)['requests'][0]['human_requests'][0]['reply'] is None
        assert sent[-1].message_id == 'om_explicit_approval'
        content = json.loads(sent[-1].request_body.content)['zh_cn']['content'][0]
        assert content[0] == {'tag': 'at', 'user_id': 'ou_owner'}


def test_concurrent_answers_send_at_most_one_original_result(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        with ManagementServer(manager, {'owner': OWNER}):
            def send(index):
                try:
                    return ManagementClient(tmp_path / 'state', 'owner').answer_human_request(task_id, q['id'], str(index), {'answers': {'colour': ['Blue']}})['reply']['sent']
                except ManagementError as exc:
                    return exc.code
            with ThreadPoolExecutor(max_workers=2) as workers:
                outcomes = list(workers.map(send, [1, 2]))
            assert sorted(outcomes) == ['binding_conflict', 'sent']
        manager.refresh_task(OWNER, task_id)
        assert len(replies(tmp_path)) == 1


def test_unanswered_deprecated_timeout_never_creates_a_decision_or_stop(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(autoResolutionMs=0), approval(rpc_id=85, approvalId='callback-1'), approval(rpc_id=86, approvalId='callback-2'))
        manager.refresh_task(OWNER, task_id)
        task = manager.refresh_task(OWNER, task_id)
        assert len(task['human_requests']) == 3
        assert {q.get('approval_id') for q in task['human_requests']} == {None, 'callback-1', 'callback-2'}
        assert all(q['reply'] is None and q['resolution'] == 'pending' for q in task['human_requests'])
        assert replies(tmp_path) == []
        assert all(r.get('method') != 'fixture/stop' for r in map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines()))


def test_changed_natural_question_cannot_receive_an_old_answer(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        state = {'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': [{'type': 'agentMessage', 'id': 'original-question', 'text': 'Which colour?'}]}]}
        (tmp_path / 'observed.json').write_text(json.dumps(state))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        state['turns'][0]['items'][0]['text'] = 'Which font?'
        (tmp_path / 'observed.json').write_text(json.dumps(state))
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'old-answer', {'answers': {'answer': ['Blue']}})
        assert not any(r.get('method') == 'fixture/append' for r in map(json.loads, (tmp_path / 'wire.jsonl').read_text().splitlines()))


def test_unbound_unsupported_request_only_exposes_original_service_locator(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, {'id': 'auth-callback', 'method': 'fixture/unsupported', 'params': {'refreshReason': 'synthetic-sensitive-placeholder'}})
        manager.refresh_task(OWNER, task_id)
        snapshot = manager.read_snapshot(OWNER)
        request = snapshot['original_interface_requests'][0]
        assert request['rpc_id'] == 'auth-callback' and request['thread_id'] is None and request['url'] is None
        assert request['answerable'] is False and request['service_id'] == snapshot['requests'][0]['session']['service_id']
        assert 'synthetic-sensitive-placeholder' not in json.dumps(snapshot)
        assert b'synthetic-sensitive-placeholder' not in (tmp_path / 'state' / 'manager.sqlite3').read_bytes()
        assert replies(tmp_path) == []


@pytest.mark.asyncio
async def test_registered_steward_entry_group_routes_owner_answer_to_original_project(tmp_path):
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, event, Gateway, Transport
    import copy
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        version = manager.read_snapshot(OWNER)['version']
        manager.apply_directory_change(OWNER, version, {'profile': {'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward',
            'role': 'steward', 'capability': 'non_development', 'project_id': None, 'parent_profile_id': None, 'connection_refs': {}}})
        config = copy.deepcopy(CONFIG)
        config['bindings'][0].update(chat_id='oc_entry', profile_id='steward', project_id=None, app_id='cli_steward', recipient_open_id='ou_steward')
        class StewardTransport(Transport):
            async def verify_identity(self, binding):
                return {'app_id': 'cli_steward', 'open_id': 'ou_steward'}
        transport, native = StewardTransport(), object()
        intake = FeishuEntry(lambda: manager, OWNER.subject, config, lambda url: None)
        intake.attach_transport(native, transport)
        source = event('@_user_1 回答 ' + q['id'] + '：Blue', 'om_steward_answer')
        source.raw_message.header.app_id = 'cli_steward'
        source.raw_message.event.message.mentions[0].id.open_id = 'ou_steward'
        source.source.chat_id = source.raw_message.event.message.chat_id = 'oc_entry'
        prepared = intake.prepare(source, native)
        assert prepared is not None and intake.in_scope(prepared, 'steward') is True
        assert intake.in_scope(prepared, 'some-other-profile') is False
        assert await intake.receive(source, Gateway(native)) == {'action': 'skip'}
        manager.refresh_task(OWNER, task_id)
        assert len(replies(tmp_path)) == 1
        assert all(segment['chat_id'] == 'oc_entry' for segment in transport.sent)
        assert manager.read_snapshot(OWNER)['requests'][0]['profile_id'] == 'mono-lead'
        assert manager.read_snapshot(OWNER)['requests'][0]['human_requests'][0]['reply']['source_anchor']['chat_id'] == 'oc_entry'


def test_structured_reply_requires_one_current_active_turn(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']},
            'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': []},
                      {'id': 'unregistered-active-turn', 'status': 'inProgress', 'itemsView': 'full', 'items': []}]}))
        with pytest.raises(ManagementError):
            manager.answer_human_request(OWNER, task_id, q['id'], 'ambiguous-active', {'answers': {'colour': ['Blue']}})
        assert replies(tmp_path) == []


@pytest.mark.parametrize('header,options,category', [
    ('Password', None, 'sensitive'),
    ('Choose', [{'label': 'Allow', 'description': 'Approve executing a shell command.'}], 'approval'),
])
def test_header_and_option_content_cannot_hide_secret_or_operation_approval(tmp_path, header, options, category):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=question_adapter(tmp_path)) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(questions=[{'id': 'value', 'header': header, 'question': 'Which value?',
            'isSecret': False, 'isOther': True, 'options': options}]))
        q = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        assert q['category'] == category and q['answerable'] is False
        assert replies(tmp_path) == []
