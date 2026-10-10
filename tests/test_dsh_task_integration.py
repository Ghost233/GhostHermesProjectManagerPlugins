"""Management operations retain native DSH admission versus completion facts."""
from dsh_fixture_server import remote_peer
from readiness_support import ReadyManager
from test_directory import OWNER, make_repo
from test_dsh_remote import admitted_adapter
from test_task_execution import accepted
import time
import pytest
import json


def test_native_cancel_admission_is_known_but_never_releases_repository(tmp_path):
    with remote_peer(behavior={'admit_prompt': True}) as (url, peer):
        adapter, _ = admitted_adapter(url)
        adapter.service_ref = 'local:fixture-stdio'
        with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                          dsh_adapter=adapter) as manager:
            request_id = accepted(manager, make_repo(tmp_path / 'repo'))
            session = manager.start_task(OWNER, request_id)['session']
            adapter.verifier.target = (session['thread_id'], session['turn_id'])
            outcome = manager.control_task(OWNER, request_id, 'stop', 'native-stop',
                                           expected_turn_id=session['turn_id'])
            assert outcome['stop']['rpc_status'] == 'accepted'
            assert outcome['execution'] == 'stopping'
            record = manager.read_snapshot(OWNER)['requests'][0]
            assert record['repository_released'] is False
            assert record['stop']['status'] == 'processing'
            assert [call['method'] for call in peer['calls']].count('session/cancel') == 1
            repeated = manager.control_task(OWNER, request_id, 'stop', 'native-stop',
                                            expected_turn_id=session['turn_id'])
            assert repeated['duplicate'] is True
            assert [call['method'] for call in peer['calls']].count('session/cancel') == 1


@pytest.mark.parametrize('change', ['stopped', 'assignment_changed'])
def test_native_original_interaction_passes_through_after_current_work_authority_ends(tmp_path, change):
    frame = {'type': 'waterfall', 'event': 'user-questions/request', 'eventId': 'late-question',
             'agentId': 'not-bound-yet', 'request': {'wait': {'callId': 'late-tool'},
                                                   'questions': [{'id': 'choice', 'question': 'Synthetic choice?'}]}}
    behavior = {'admit_prompt': True, 'remote_events': [frame], 'defer_remote_events': True}
    with remote_peer(behavior=behavior) as (url, peer):
        adapter, _ = admitted_adapter(url)
        adapter.service_ref = 'local:fixture-stdio'
        with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                          dsh_adapter=adapter) as manager:
            repo = make_repo(tmp_path / 'repo')
            request_id = accepted(manager, repo)
            session = manager.start_task(OWNER, request_id)['session']
            adapter.verifier.target = (session['thread_id'], session['turn_id'])
            frame['agentId'] = session['thread_id']
            journal = peer['journals'][session['thread_id']]
            journal.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(journal), 'time': 2,
                'data': {'turn': 1, 'step': 1, 'callId': 'late-tool', 'name': 'ask_user_question', 'arguments': '{}'}}})
            if change == 'stopped':
                manager.control_task(OWNER, request_id, 'stop', 'stop-before-interaction',
                                     expected_turn_id=session['turn_id'])
            else:
                from test_task_execution import execution_registration
                profile = execution_registration(repo)['profile']
                profile['connection_refs']['dsh'] = 'local:other-bound-service'
                manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': profile})
            peer['emit_remote'].set()
            deadline = time.monotonic() + 2
            while not any(call['method'] == '$events/result' for call in peer['calls']) and time.monotonic() < deadline:
                time.sleep(0.01)
            replies = [call for call in peer['calls'] if call['method'] == '$events/result']
            assert len(replies) == 1
            assert replies[0]['payload']['args']['outcome'] == {'kind': 'next'}
            assert manager.read_snapshot(OWNER)['requests'][0]['repository_released'] is False


@pytest.mark.parametrize('kind', ['approval', 'timed', 'sensitive', 'operation_question'])
def test_native_transferred_current_request_still_notifies_owner_without_answer_permission(tmp_path, kind):
    from test_notifications import Clock, register_entry
    clock = Clock()
    request = {'wait': {'callId': 'notice-tool'},
               'questions': [{'id': 'choice', 'question': 'Synthetic choice?'}]}
    event = 'user-questions/request'
    tool = 'ask_user_question'
    if kind == 'approval':
        event, tool = 'approval/request', 'bash'
        request = {'callId': 'notice-tool', 'toolName': tool, 'reason': 'Synthetic operation'}
    elif kind == 'timed':
        request['wait']['timed'] = True
    elif kind == 'sensitive':
        request['questions'] = [{'id': 'choice', 'question': 'synthetic-sensitive-placeholder', 'isSecret': True}]
    else:
        request['questions'] = [{'id': 'choice', 'question': 'Allow running synthetic command?'}]
    frame = {'type': 'waterfall', 'event': event, 'eventId': 'notice-event',
             'agentId': 'not-bound-yet', 'request': request}
    behavior = {'admit_prompt': True, 'remote_events': [frame], 'defer_remote_events': True}
    with remote_peer(behavior=behavior) as (url, peer):
        adapter, _ = admitted_adapter(url)
        adapter.service_ref = 'local:fixture-stdio'
        with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                          dsh_adapter=adapter, notification_clock=clock) as manager:
            request_id = accepted(manager, make_repo(tmp_path / 'repo'))
            register_entry(manager)
            session = manager.start_task(OWNER, request_id)['session']
            adapter.verifier.target = (session['thread_id'], session['turn_id'])
            frame['agentId'] = session['thread_id']
            journal = peer['journals'][session['thread_id']]
            journal.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(journal), 'time': 2,
                'data': {'turn': 1, 'step': 1, 'callId': 'notice-tool', 'name': tool, 'arguments': '{}'}}})
            if kind == 'approval':
                journal.append({'type': 'event', 'event': {'type': 'approval/asked', 'seq': len(journal), 'time': 2,
                    'data': {'id': 'native-approval', **request}}})
            peer['emit_remote'].set()
            deadline = time.monotonic() + 2
            while not any(call['method'] == '$events/result' for call in peer['calls']) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert [call['payload']['args']['outcome'] for call in peer['calls']
                    if call['method'] == '$events/result'] == [{'kind': 'next'}]
            notices = [event for event in manager.run_notifications(OWNER)['notifications']
                       if event['kind'] == 'human_request']
            assert len(notices) == 1 and notices[0]['mention_owner'] is True
            assert '原界面' in notices[0]['text']
            snapshot = manager.read_snapshot(OWNER)
            question = snapshot['requests'][0]['human_requests'][0]
            assert question['answerable'] is False and question['control_enabled'] is False
            assert question['original_interface']['thread_id'] == session['thread_id']
            assert 'synthetic-sensitive-placeholder' not in json.dumps(snapshot)
            journal.append({'type': 'event', 'event': {'type': 'tool/result', 'seq': len(journal), 'time': 3,
                'data': {'turn': 1, 'step': 1, 'message': {'id': 'notice-result', 'role': 'tool',
                    'toolCallId': 'notice-tool', 'source': {'kind': 'tool', 'callId': 'notice-tool'},
                    'content': [{'type': 'text', 'text': 'Synthetic result'}]}, 'surfaceOp': 'append'}}})
            clock.advance(1800)
            assert len([event for event in manager.run_notifications(OWNER)['notifications']
                        if event['kind'] == 'human_request']) == 1
            settled = manager.read_snapshot(OWNER)['requests'][0]
            assert settled['execution'] != 'unverified'
            assert settled['human_requests'][0]['resolution'] in {'resolved', 'expired'}
            assert [call['payload']['args']['outcome'] for call in peer['calls']
                    if call['method'] == '$events/result'] == [{'kind': 'next'}]


@pytest.mark.parametrize('include_call_id', [True, False])
def test_native_original_approval_decision_stops_reminders_while_tool_still_running(tmp_path, include_call_id):
    from test_notifications import Clock, register_entry
    clock = Clock()
    request = {'toolName': 'bash', 'reason': 'Synthetic operation'}
    if include_call_id:
        request['callId'] = 'long-tool'
    frame = {'type': 'waterfall', 'event': 'approval/request', 'eventId': 'long-approval-event',
             'agentId': 'not-bound-yet', 'request': request}
    behavior = {'admit_prompt': True, 'remote_events': [frame], 'defer_remote_events': True}
    with remote_peer(behavior=behavior) as (url, peer):
        adapter, _ = admitted_adapter(url)
        adapter.service_ref = 'local:fixture-stdio'
        with ReadyManager(tmp_path / 'state', owner_identity_ref=OWNER.subject,
                          dsh_adapter=adapter, notification_clock=clock) as manager:
            request_id = accepted(manager, make_repo(tmp_path / 'repo'))
            register_entry(manager)
            session = manager.start_task(OWNER, request_id)['session']
            adapter.verifier.target = (session['thread_id'], session['turn_id'])
            frame['agentId'] = session['thread_id']
            journal = peer['journals'][session['thread_id']]
            journal.append({'type': 'event', 'event': {'type': 'tool/call', 'seq': len(journal), 'time': 2,
                'data': {'turn': 1, 'step': 1, 'callId': 'long-tool', 'name': 'bash', 'arguments': '{}'}}})
            journal.append({'type': 'event', 'event': {'type': 'approval/asked', 'seq': len(journal), 'time': 2,
                'data': {'id': 'native-approval', **request}}})
            peer['emit_remote'].set()
            deadline = time.monotonic() + 2
            while not any(call['method'] == '$events/result' for call in peer['calls']) and time.monotonic() < deadline:
                time.sleep(0.01)
            initial = [event for event in manager.run_notifications(OWNER)['notifications']
                       if event['kind'] == 'human_request']
            assert len(initial) == 1 and initial[0]['mention_owner'] is True
            question = manager.read_snapshot(OWNER)['requests'][0]['human_requests'][0]
            assert question['answerable'] is False and question['control_enabled'] is False
            journal.append({'type': 'event', 'event': {'type': 'approval/decided', 'seq': len(journal), 'time': 3,
                'data': {'id': 'native-approval', 'outcome': 'allowed-once'}}})
            clock.advance(1800)
            assert len([event for event in manager.run_notifications(OWNER)['notifications']
                        if event['kind'] == 'human_request']) == 1
            record = manager.read_snapshot(OWNER)['requests'][0]
            assert record['execution'] == 'running'
            assert record['human_requests'][0]['resolution'] in {'resolved', 'expired'}
            assert not any(event['type'] == 'event' and event['event']['type'] == 'tool/result' for event in journal)
            assert [call['payload']['args']['outcome'] for call in peer['calls']
                    if call['method'] == '$events/result'] == [{'kind': 'next'}]
