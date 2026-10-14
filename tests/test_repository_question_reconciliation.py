"""Lost acknowledgements reconcile from original audit without replaying answers."""
from copy import deepcopy
import asyncio
import hashlib
import json
from types import SimpleNamespace as NS

import pytest

from ghost_hermes_pm.repository_questions import _observe_questions


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
async def test_complete_delivery_payload_does_not_create_a_question_or_keep_old_misclassification(wrapped):
    criterion = '请确认合成规则，需要我决定测试 token 上限。'
    report = {'source_commit': 'a' * 40, 'issue_updated_at': '2026-10-10T00:00:00Z',
        'criteria': [{'text': criterion, 'test_call_ids': ['fixture-test']}], 'test_files': ['test_fixture.py'],
        'fine_issue_urls': [], 'pr_url': None, 'sync_branches': [], 'leftovers': []}
    text = json.dumps(report, ensure_ascii=False)
    if wrapped:
        text = '```json\n' + text + '\n```\n\n合成测试证据已回传。'
    text = 'HERMES_REPOSITORY_DELIVERY_JSON\n' + text
    question = {'id': 'message-1', 'kind': 'natural', 'state': 'open', 'answerable': False,
        'generation': 'fixture-generation', 'session_id': 'fixture-session', 'source_seq': 1,
        'source_digest': hashlib.sha256(json.dumps([{'id': 'answer', 'question': text}],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    record = {'id': 'fixture-work', 'profile_id': 'fixture-profile', 'source_anchor': {},
        'dsh_execution': {'generation': 'fixture-generation', 'session_id': 'fixture-session',
            'state': 'awaiting_acceptance', 'questions': [question]}}
    intake = NS(require_active=lambda generation: None, snapshot=lambda: {'work': [record]},
        _save_execution=lambda *args, **kwargs: None, secret_values=[], transports=[])
    carrier = NS(event_frames=lambda: {'generation': 'fixture-generation', 'session_id': 'fixture-session', 'frames': []})
    events = [{'seq': 1, 'type': 'assistant/message', 'data': {'turn': 1, 'message': {
        'content': [{'type': 'text', 'text': text}]}}},
        {'seq': 2, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}]
    projections = {'userQuestions': {'active': [], 'settled': []}}

    assert await _observe_questions(intake, record, carrier, events, projections, 0) is False
    assert record['dsh_execution']['questions'] == [question]
    assert question['state'] == 'expired' and not question.get('reply_status')
    from ghost_hermes_pm.repository_acceptance import _report
    assert _report(events) == (None if wrapped else report)


@pytest.fixture
def settled_round():
    answer = {'id': 'colour', 'selected': ['Blue']}
    question = {'id': 'call-question', 'kind': 'structured', 'state': 'open',
        'generation': 'generation-fixture', 'session_id': 'session-fixture',
        'source_seq': 1, 'questions': [{'id': 'colour', 'question': 'Which colour?'}],
        'answers': {'colour': answer}, 'reply_status': 'intent', 'reply_path': 'live'}
    record = {'id': 'work-fixture', 'dsh_execution': {'generation': 'generation-fixture',
        'session_id': 'session-fixture', 'journal_cursor': 10, 'state': 'running', 'questions': [question]}}
    intake = NS(require_active=lambda generation: None, snapshot=lambda: {'work': [record]},
                _save_execution=lambda *args, **kwargs: None)
    carrier = NS(event_frames=lambda: {'generation': 'generation-fixture',
        'session_id': 'session-fixture', 'frames': []})
    events = [{'seq': 1, 'type': 'tool/call', 'data': {'callId': 'call-question', 'name': 'ask_user_question'}},
        {'seq': 2, 'type': 'tool/result', 'data': {'message': {'toolCallId': 'call-question', 'isError': False}}}]
    projections = {'userQuestions': {'active': [], 'settled': [{'callId': 'call-question', 'answers': [answer]}]}}
    return intake, record, carrier, events, projections


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'HERMES_REPOSITORY_DELIVERY_JSON\n请确认合成测试范围？',
    'HERMES_REPOSITORY_DELIVERY_JSON\n{}\n需要我决定合成测试范围。',
    'HERMES_REPOSITORY_DELIVERY_JSON\n```json\n{"criteria": [{"text": "请确认？"}]}',
    '请确认合成测试范围？',
])
async def test_incomplete_delivery_marker_and_real_questions_remain_pending(settled_round, text):
    intake, record, carrier, _events, projections = settled_round
    record['dsh_execution']['questions'] = []
    record.update(profile_id='fixture-profile', source_anchor={})
    intake.secret_values, intake.transports = [], []
    projections['userQuestions']['settled'] = []
    events = [{'seq': 1, 'type': 'assistant/message', 'data': {'turn': 1, 'message': {
        'content': [{'type': 'text', 'text': text}]}}}]

    assert await _observe_questions(intake, record, carrier, events, projections, 0) is True
    question = record['dsh_execution']['questions'][0]
    assert question['state'] == 'open' and question['questions'][0]['question'] == text


@pytest.mark.asyncio
async def test_complete_delivery_payload_keeps_a_real_surrounding_question(settled_round):
    intake, record, carrier, _events, projections = settled_round
    record['dsh_execution']['questions'] = []
    record.update(profile_id='fixture-profile', source_anchor={})
    intake.secret_values, intake.transports = [], []
    projections['userQuestions']['settled'] = []
    report = {'source_commit': 'a' * 40, 'issue_updated_at': '2026-10-10T00:00:00Z',
        'criteria': [{'text': 'Synthetic verification completed.', 'test_call_ids': ['fixture-test']}],
        'test_files': ['test_fixture.py'], 'fine_issue_urls': [], 'sync_branches': [], 'leftovers': []}
    text = 'HERMES_REPOSITORY_DELIVERY_JSON\n```json\n' + json.dumps(report) + '\n```\n请确认下一步合成范围？'
    events = [{'seq': 1, 'type': 'assistant/message', 'data': {'turn': 1, 'message': {
        'content': [{'type': 'text', 'text': text}]}}}]

    assert await _observe_questions(intake, record, carrier, events, projections, 0) is True
    question = record['dsh_execution']['questions'][0]
    assert question['state'] == 'open'
    assert '请确认下一步合成范围？' in question['questions'][0]['question']
    assert 'Synthetic verification completed.' not in question['questions'][0]['question']


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['intent', 'outcome_unknown', 'queued'])
@pytest.mark.parametrize('path', ['live', 'continued'])
async def test_original_settled_batch_reconciles_lost_ack_without_resend(settled_round, status, path):
    intake, record, carrier, events, projections = settled_round
    question = record['dsh_execution']['questions'][0]
    question.update(reply_status=status, reply_path=path)
    if path == 'continued':
        events[1] = {'seq': 2, 'type': 'user/message', 'data': {'source': {
            'kind': 'user-question-reply', 'callId': question['id'], 'outcome': 'answered'}}}

    waiting = await _observe_questions(intake, record, carrier, events, projections, 0)

    assert question['reply_status'] == 'accepted'
    assert question['state'] == 'settled' and question['admitted_seq'] == 2
    assert waiting is False


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['answers', 'audit', 'path'])
async def test_unconfirmed_batch_keeps_unknown_and_never_resends(settled_round, mismatch):
    intake, record, carrier, events, projections = settled_round
    question = record['dsh_execution']['questions'][0]
    question['reply_status'] = 'outcome_unknown'
    if mismatch == 'answers':
        projections['userQuestions']['settled'][0]['answers'][0] = {'id': 'colour', 'selected': ['Green']}
    elif mismatch == 'audit':
        events[1]['data']['message']['isError'] = True
    else:
        question.pop('reply_path')

    assert await _observe_questions(intake, record, carrier, events, projections, 0) is True
    assert question['reply_status'] == 'outcome_unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['intent', 'outcome_unknown', 'queued'])
async def test_original_ordinary_input_reconciles_only_matching_request_id(settled_round, status):
    intake, record, carrier, _, projections = settled_round
    question = record['dsh_execution']['questions'][0]
    question.update(kind='natural', reply_status=status, reply_path='natural', request_id='answer-fixture')
    projections['userQuestions']['settled'] = []
    events = [{'seq': 2, 'type': 'user/message', 'data': {'source': {'kind': 'user', 'rpcId': 'other-request'}}}]
    assert await _observe_questions(intake, record, carrier, events, projections, 0) is True
    events[0]['data']['source']['rpcId'] = 'answer-fixture'
    assert await _observe_questions(intake, record, carrier, events, projections, 0) is False
    assert question['reply_status'] == 'accepted'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['live', 'continued', 'natural'])
async def test_reply_path_and_request_id_are_saved_before_native_submission(settled_round, tmp_path, monkeypatch, path):
    import ghost_hermes_pm.repository_questions as questions
    import ghost_hermes_pm.repository_supervision as supervision
    intake, record, carrier, _, _ = settled_round
    question = record['dsh_execution']['questions'][0]
    for key in ('reply_status', 'reply_path'):
        question.pop(key)
    question.update(answerable=True, answers={}, reply_messages=[], event_id='event-question',
                    delivery={'message_id': 'om_question'})
    saved = []
    intake._save_execution = lambda *args, **kwargs: saved.append(deepcopy(question))
    intake.lock = asyncio.Lock()
    intake.lock_path = tmp_path / 'question.lock'
    intake.lock_path.touch(mode=0o600)
    intake.secret_values = []
    record['target'] = {'repo_path': str(tmp_path)}
    projections = {'userQuestions': {'active': [{
        'callId': question['id'], 'questions': question['questions'], 'state': 'continued' if path == 'continued' else 'open'}]}}
    events = []
    if path == 'natural':
        question['kind'] = 'natural'
        question['questions'] = [{'id': 'answer', 'question': 'Which colour?'}]
        projections['userQuestions']['active'] = []
        projections['inbox'] = {}
        events = [{'seq': 1, 'type': 'assistant/message', 'data': {'message': {
            'content': [{'type': 'text', 'text': 'Which colour?'}]}}}]

    def submission(*args):
        assert saved[0]['reply_status'] == 'intent' and saved[0]['reply_path'] == path
        if path == 'natural':
            assert saved[0]['request_id'] == 'answer-om_reply'
        raise OSError('Synthetic lost acknowledgement after submission.')

    def call(original, method, arguments):
        if method == 'session/list':
            return {'items': [{'sessionId': 'session-fixture', 'agentAvailable': True, 'running': False}]}
        return submission()

    carrier.event_frames = lambda: {'client_id': 'client-fixture', 'frames': []}
    carrier.request = submission
    carrier.close = lambda: None
    monkeypatch.setattr(questions, 'connected_execution', lambda *args: carrier)
    monkeypatch.setattr(supervision, 'original_history', lambda *args: (events, projections))
    monkeypatch.setattr(supervision, 'owned_call', call)
    async def send(segment):
        return {'status': 'delivered'}
    prepared = NS(work_id=record['id'], rejected=None, transport=NS(send=send), command='Blue',
                  envelope={'parent_id': 'om_question', 'message_id': 'om_reply', 'chat_id': 'oc_fixture'})

    result = await questions.process_reply(intake, prepared, 0)

    assert result['status'] == 'outcome_unknown'
    assert saved[0]['reply_path'] == path
