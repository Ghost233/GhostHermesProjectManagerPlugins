"""Notification timings through the shared management boundary and public native sender."""
from ghost_hermes_pm import Manager
from readiness_support import ReadyManager as Manager
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo
from test_task_execution import adapter_for, execution_registration, prepare_fixture
from test_requests import MESSAGE, ISSUE
from test_collaboration import channel


class Clock:
    def __init__(self):
        self.now = 10000.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds



def accepted(manager, repo):
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], execution_registration(repo))
    task = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
    manager.publish_request_message(OWNER, task['id'], 'confirmation', '已受理')
    packet = manager.claim_delivery(OWNER, task['id'])
    manager.record_delivery(OWNER, task['id'], packet['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack'})
    prepare_fixture(manager, task['id'], repo)
    return task['id']


def register_entry(manager):
    manager.apply_directory_change(OWNER, manager.read_snapshot(OWNER)['version'], {'profile': {
        'id': 'steward', 'native_profile': 'steward', 'identity_ref': 'fixture:steward',
        'role': 'steward', 'capability': 'non_development', 'project_id': None,
        'parent_profile_id': None, 'connection_refs': {}}})
    manager.collaborate(OWNER, 'register_channels', {'channels': [channel('steward', 'entry')]})


def test_active_projects_are_summarized_every_fifteen_minutes_even_without_progress_and_idle_is_quiet(tmp_path):
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.run_notifications()
            clock.advance(3600)
            assert client.run_notifications()['notifications'] == []
            task_id = accepted(manager, make_repo(tmp_path / 'repo'))
            register_entry(manager)
            client.start_task(task_id)
            client.run_notifications()
            clock.advance(899)
            assert client.run_notifications()['notifications'] == []
            clock.advance(1)
            first = [e for e in client.run_notifications()['notifications'] if e['kind'] == 'summary']
            assert len(first) == 1 and first[0]['kind'] == 'summary'
            assert first[0]['project_id'] == 'mono' and first[0]['request_ids'] == [task_id]
            assert all(label in first[0]['text'] for label in ('状态', '进展', '阻塞', '待处理', '下一步', '无新进展'))
            clock.advance(900)
            assert len([e for e in client.run_notifications()['notifications'] if e['kind'] == 'summary']) == 2
            assert client.read_snapshot()['notifications']['events'][-1]['kind'] == 'summary'


def test_owner_blocking_request_is_immediate_reminded_at_thirty_minutes_and_stops_after_resolution(tmp_path):
    from test_questions import question_adapter, emit, user_question
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            client.start_task(task_id)
            emit(tmp_path, user_question())
            question = client.refresh_task(task_id)['human_requests'][0]
            initial = client.run_notifications()['notifications']
            assert [e['kind'] for e in initial] == ['human_request']
            assert initial[0]['human_request_id'] == question['id'] and initial[0]['mention_owner'] is True
            assert 'colour' in initial[0]['text'] and question['id'] in initial[0]['text']
            clock.advance(1799)
            assert len([e for e in client.run_notifications()['notifications'] if e['kind'] == 'human_request']) == 1
            clock.advance(1)
            assert len([e for e in client.run_notifications()['notifications'] if e['kind'] == 'human_request']) == 2
            client.answer_human_request(task_id, question['id'], 'owner-answer', {'answers': {'colour': ['Blue']}})
            client.refresh_task(task_id)
            clock.advance(3600)
            assert len([e for e in client.run_notifications()['notifications'] if e['kind'] == 'human_request']) == 2


def test_real_lark_entry_builder_mentions_only_urgent_owner_and_keeps_actual_local_anchor(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace as NS
    from lark_oapi import Client
    from lark_oapi.api.im.v1 import CreateMessageResponse, ReplyMessageResponse
    from ghost_hermes_pm.messages import FeishuEntry
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    from test_questions import question_adapter, emit, user_question
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        question = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        native = Client.builder().app_id('cli_steward').app_secret('synthetic-unused-secret').build()
        native.request = lambda request: NS(code=0, raw=NS(content=b'{"code":0,"bot":{"open_id":"ou_steward","activate_status":2}}'))
        sent = []
        def create(request):
            sent.append(request)
            return CreateMessageResponse({'code': 0, 'data': {'message_id': 'om_entry_request', 'chat_id': 'oc_entry'}})
        def reply(request):
            sent.append(request)
            return ReplyMessageResponse({'code': 0, 'data': {'message_id': 'om_entry_summary', 'chat_id': 'oc_entry', 'parent_id': 'om_entry_request'}})
        native.im.v1.message.create, native.im.v1.message.reply = create, reply
        intake = FeishuEntry(lambda: manager, OWNER.subject, {'enabled': True, 'verification_ref': 'fixture:entry'}, lambda url: None)
        intake.attach_transport(object(), NativeFeishuTransport(native))
        asyncio.run(intake.deliver_notifications(OWNER))
        initial = manager.read_snapshot(OWNER)['notifications']['events'][0]
        assert initial['delivery'] == 'delivered' and initial['human_request_id'] == question['id']
        first = json.loads(sent[0].request_body.content)['zh_cn']['content'][0]
        assert first[0] == {'tag': 'at', 'user_id': 'owner-steward'}
        assert sent[0].request_body.receive_id == 'oc_entry'
        clock.advance(900)
        asyncio.run(intake.deliver_notifications(OWNER))
        assert len(sent) == 2 and sent[1].message_id == 'om_entry_request'
        second = json.loads(sent[1].request_body.content)['zh_cn']['content'][0]
        assert all(item['tag'] != 'at' for item in second)
        event = manager.read_snapshot(OWNER)['notifications']['events'][-1]
        assert event['delivery'] == 'delivered' and event['segments'][0]['attempts'][-1]['message_id'] == 'om_entry_summary'


def test_stall_threshold_checks_original_service_again_and_never_interrupts_or_starts_new_execution(tmp_path):
    import json
    from test_task_control import TURN, wire
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        clock.advance(899)
        assert not any(e['kind'] == 'suspected_stall' for e in manager.run_notifications(OWNER)['notifications'])
        reads_before = len([r for r in wire(tmp_path) if r['method'] == 'thread/read'])
        clock.advance(1)
        events = manager.run_notifications(OWNER)['notifications']
        assert len([r for r in wire(tmp_path) if r['method'] == 'thread/read']) > reads_before
        stalls = [e for e in events if e['kind'] == 'suspected_stall']
        assert len(stalls) == 1 and '核查' in stalls[0]['text'] and stalls[0]['mention_owner'] is False
        clock.advance(900)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'suspected_stall']) == 1
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1
        assert not any(r['method'] == 'turn/interrupt' for r in wire(tmp_path))
        (tmp_path / 'observed.json').write_text(json.dumps({'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': [{'id': 'fresh', 'type': 'agentMessage', 'text': 'new verified progress'}]}]}))
        manager.run_notifications(OWNER)
        clock.advance(899)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'suspected_stall']) == 1


def test_waits_explained_long_commands_and_unverified_history_do_not_raise_stall(tmp_path):
    import json
    from test_task_control import TURN
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        for observation in [
            {'status': {'type': 'active', 'activeFlags': ['waitingOnApproval']}},
            {'status': {'type': 'active', 'activeFlags': ['waitingOnUserInput']}},
            {'status': {'type': 'active', 'activeFlags': []}, 'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'long', 'command': 'python -m pytest', 'cwd': str(tmp_path / 'repo'), 'status': 'inProgress'}]}]},
            {'status': {'type': 'active', 'activeFlags': []}, 'turns': []},
        ]:
            (tmp_path / 'observed.json').write_text(json.dumps(observation))
            clock.advance(901)
            assert not any(e['kind'] == 'suspected_stall' for e in manager.run_notifications(OWNER)['notifications'])


def test_continuous_original_channel_loss_warns_once_after_two_minutes_and_once_on_recovery(tmp_path):
    import json
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        (tmp_path / 'behavior.json').write_text(json.dumps({'rpc_error': 'thread/read'}))
        manager.run_notifications(OWNER)
        clock.advance(119)
        assert not any(e['kind'] == 'channel_lost' for e in manager.run_notifications(OWNER)['notifications'])
        clock.advance(1)
        lost = manager.run_notifications(OWNER)['notifications']
        assert len([e for e in lost if e['kind'] == 'channel_lost']) == 1
        clock.advance(500)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'channel_lost']) == 1
        task = manager.read_snapshot(OWNER)['requests'][0]
        assert task['execution'] == 'unverified' and task['last_confirmed_execution'] == 'running'
        assert task['repository_released'] is False and task['task_delivery'] == 'unmet'
        (tmp_path / 'behavior.json').write_text('{}')
        restored = manager.run_notifications(OWNER)['notifications']
        assert len([e for e in restored if e['kind'] == 'channel_recovered']) == 1
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'channel_recovered']) == 1
        assert manager.read_snapshot(OWNER)['notifications']['health']['sources'][task_id]['status'] == 'verified'


def test_active_background_and_unknown_process_coverage_explain_or_block_stall_judgment(tmp_path):
    import json
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        (tmp_path / 'background.json').write_text(json.dumps({'': {'data': [{'itemId': 'bg', 'processId': 'registered-background', 'command': 'python -m pytest'}], 'nextCursor': None}}))
        clock.advance(900)
        assert not any(e['kind'] == 'suspected_stall' for e in manager.run_notifications(OWNER)['notifications'])


def test_restart_does_not_catch_up_ticks_or_replay_unknown_delivery_and_offline_health_is_visible(tmp_path):
    from test_recovery import original_state, recovery_adapter
    clock = Clock()
    repo = make_repo(tmp_path / 'repo')
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, repo)
        register_entry(manager)
        service_id = manager.start_task(OWNER, task_id)['session']['service_id']
        manager.run_notifications(OWNER)
        clock.advance(900)
        event = next(e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'summary')
        packet = manager.manage_notifications(OWNER, 'claim', {'event_id': event['id']})
        assert packet is not None
    original_state(tmp_path, repo)
    clock.advance(9000)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, recovery_adapters={'local:fixture-stdio': recovery_adapter(tmp_path, service_id)}, notification_clock=clock) as manager:
        before = manager.read_snapshot(OWNER)['notifications']
        assert before['health']['supervision'] == 'unavailable'
        manager.reconcile_task(OWNER, task_id)
        events = manager.run_notifications(OWNER)['notifications']
        summaries = [e for e in events if e['kind'] == 'summary']
        assert len(summaries) == 1 and summaries[0]['delivery'] == 'unknown'
        assert manager.manage_notifications(OWNER, 'claim', {'event_id': event['id']}) is None
        clock.advance(899)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'summary']) == 1
        clock.advance(1)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'summary']) == 2


def test_nonblocking_questions_wait_for_summary_and_pending_alerts_expire_before_send(tmp_path):
    from test_questions import question_adapter, emit, user_question
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(blocking=False))
        manager.refresh_task(OWNER, task_id)
        assert not any('人工请求' in s['text'] for p in manager.read_snapshot(OWNER)['requests'][0]['outbox'] for s in p['segments'])
        assert not any(e['kind'] == 'human_request' for e in manager.run_notifications(OWNER)['notifications'])
        clock.advance(900)
        summary = next(e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'summary')
        assert 'colour' in summary['text']
        emit(tmp_path, user_question(rpc_id=9))
        question = next(q for q in manager.refresh_task(OWNER, task_id)['human_requests'] if q['rpc_id'] == 9)
        alert = next(e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request')
        manager.answer_human_request(OWNER, task_id, question['id'], 'resolved-before-send', {'answers': {'colour': ['Blue']}})
        assert manager.manage_notifications(OWNER, 'claim', {'event_id': alert['id']}) is None
        assert next(e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['id'] == alert['id'])['delivery'] == 'expired'


def test_confirmed_stop_updates_once_and_processing_intent_does_not_claim_completion(tmp_path):
    import json
    from test_task_control import TURN
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.control_task(OWNER, task_id, 'stop', 'actual-stop', expected_turn_id=TURN)
        assert not any(e['kind'] == 'stop_confirmed' for e in manager.run_notifications(OWNER)['notifications'])
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'interrupted', 'itemsView': 'full', 'items': []}]}))
        manager.refresh_task(OWNER, task_id)
        first = [e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'stop_confirmed']
        assert len(first) == 1 and first[0]['mention_owner'] is False
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'stop_confirmed']) == 1


def test_project_completion_is_once_only_after_fresh_stable_global_validation_and_late_inputs_revoke_it(tmp_path):
    import json
    from test_global_validation import combination, FixtureHost, git, LEAD
    from test_repository_queue import queue_adapter
    clock = Clock()
    host = FixtureHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=host, notification_clock=clock) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        register_entry(manager)
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})
        assert not any(e['kind'] == 'project_completed' for e in manager.run_notifications(OWNER)['notifications'])
        task = next(r for r in manager.read_snapshot(OWNER)['requests'] if r['id'] == parent)
        session = task['session']
        (tmp_path / 'queue-observed.json').write_text(json.dumps({session['thread_id']: {'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'mono-test', 'command': 'python -m unittest', 'cwd': str(mono), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': 'Ran 1 test in 0.01s\n\nOK'}]}]}}))
        manager.record_task_delivery(LEAD, parent, {'source_commit': git(mono, 'rev-parse', 'HEAD'), 'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['mono-test']}]})
        manager.global_validation(LEAD, 'complete', {'validation_id': plan['id']})
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'project_completed']) == 1
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'project_completed']) == 1
        (child / 'source.py').write_text('late manual input\n')
        events = manager.run_notifications(OWNER)['notifications']
        original = next(e for e in events if e['kind'] == 'project_completed')
        assert original['delivery'] == 'expired'
        assert len([e for e in events if e['kind'] == 'needs_owner']) == 1


def test_child_delivery_immediately_queues_original_public_parent_return_once(tmp_path):
    import pytest
    import json
    from test_collaboration import accepted_parent, IssueSource, LEAD, CHILD, CHILD_INGRESS, source
    from test_task_execution import adapter_for, prepare_fixture
    from test_task_control import THREAD, TURN
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, delivery_source=IssueSource(), codex_adapter=adapter_for(tmp_path)) as manager:
        parent_id, child_channel = accepted_parent(manager, tmp_path)
        version = manager.read_snapshot(OWNER)['version']
        profile = next(p for p in manager.read_snapshot(OWNER)['profiles'] if p['id'] == 'child')
        manager.apply_directory_change(OWNER, version, {'profile': {k: profile[k] for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id')} | {'connection_refs': {'codex': 'local:fixture-stdio'}}})
        channels = [dict(c) for c in manager.read_snapshot(OWNER)['collaboration']['channels']]
        for c in channels:
            c.pop('profile_binding')
        manager.collaborate(OWNER, 'register_channels', {'channels': channels})
        h = manager.collaborate(LEAD, 'delegate_issue', {'parent_handoff_id': parent_id, 'target_profile_id': 'child', 'issue_url': ISSUE['url']})
        p = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': h['id']})
        manager.collaborate(LEAD, 'record_delivery', {'handoff_id': h['id'], 'uuid': p['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_child_result_scope', 'chat_id': 'oc_project'}})
        observed = source(child_channel, 'bot', 'om_child_result_scope')
        observed.update(tenant_key='lead-tenant', sender_open_id='lead-seen-child')
        accepted = manager.collaborate(CHILD_INGRESS, 'ingest', {'channel_id': child_channel['id'], 'source_anchor': observed, 'text': p['text']})
        task_id = accepted['task_request_id']
        manager.collaborate(CHILD_INGRESS, 'publish_ack', {'handoff_id': h['id']})
        ack = manager.collaborate(CHILD_INGRESS, 'claim_ack', {'handoff_id': h['id']})
        manager.collaborate(CHILD_INGRESS, 'record_ack', {'handoff_id': h['id'], 'uuid': ack['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_child_ack', 'chat_id': 'oc_project'}})
        with pytest.raises(Exception):
            manager.collaborate(CHILD, 'report_result', {'handoff_id': h['id']})
        prepare_fixture(manager, task_id, tmp_path / 'child')
        manager.start_task(CHILD, task_id)
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full',
            'items': [{'type': 'commandExecution', 'id': 'pytest-child', 'command': 'python -m pytest tests/test_fixture.py -q', 'cwd': str(tmp_path / 'child'), 'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '1 passed'}]}]}))
        manager.record_task_delivery(CHILD, task_id, {'issue_updated_at': ISSUE['updated_at'], 'criteria': [{'text': ISSUE['body'], 'test_item_ids': ['pytest-child']}], 'source_commit': None, 'pr_url': None, 'sync_branches': []})
        results = [h for h in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if h['kind'] == 'result']
        assert len(results) == 1
        result = results[0]
        assert result['sender_profile_id'] == 'child' and result['target_profile_id'] == 'mono-lead'
        assert result['owner_origin']['subject'] == OWNER.subject
        notice = next(e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['kind'] == 'child_delivery')
        assert notice['role_handoff_id'] == result['id'] and notice['mention_owner'] is False
        packet = manager.collaborate(CHILD, 'claim_delivery', {'handoff_id': result['id']})
        assert packet['mention_open_id'] == 'lead-seen-child'
        manager.run_notifications(OWNER)
        assert len([h for h in manager.read_snapshot(OWNER)['collaboration']['handoffs'] if h['kind'] == 'result']) == 1


def test_verified_rework_is_immediately_linked_to_actual_public_child_outbox_once(tmp_path):
    from test_collaboration import channel
    from test_global_validation import combination, FailedHost, IssueReadSource, git, LEAD
    from test_repository_queue import queue_adapter
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=queue_adapter(tmp_path), global_validation_host=FailedHost(), delivery_source=IssueReadSource()) as manager:
        mono, child, parent, kid = combination(manager, tmp_path)
        lead_channel, kid_channel = channel('mono-lead'), channel('child-lead')
        lead_channel['bot_sources'] = []
        kid_channel['bot_sources'] = []
        lead_channel['bot_sources'].append({'profile_id': 'child-lead', 'open_id': 'child-seen-lead', 'tenant_key': 'child-tenant', 'native_ids': ['child-native']})
        kid_channel['bot_sources'].append({'profile_id': 'mono-lead', 'open_id': 'lead-seen-child', 'tenant_key': 'lead-tenant', 'native_ids': ['lead-native']})
        manager.collaborate(OWNER, 'register_channels', {'channels': [lead_channel, kid_channel]})
        plan = manager.global_validation(LEAD, 'plan', {'request_id': parent, 'mono_commit': git(mono, 'rev-parse', 'HEAD'), 'children': [{'request_id': kid, 'path': 'child'}], 'test_ids': ['unit']})
        manager.global_validation(LEAD, 'start', {'validation_id': plan['id']})
        assert manager.global_validation(LEAD, 'finish', {'validation_id': plan['id']})['status'] == 'failed'
        returned = manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/example-user/fixture/issues/28'})
        assert returned['rework'][0]['route'] == 'child' and returned['rework'][0]['profile_id'] == 'child-lead'
        assert returned['rework'][0]['issue']['url'].endswith('/issues/28')
        handoff = returned['rework'][0]['handoff_id']
        packet = manager.collaborate(LEAD, 'claim_delivery', {'handoff_id': handoff})
        assert packet['mention_open_id'] == 'child-seen-lead' and '/issues/28' in packet['text']
        assert returned['occupancy']['released'] is True
        assert manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/example-user/fixture/issues/28'})['rework'] == returned['rework']

        notice = next(e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['kind'] == 'rework')
        assert notice['role_handoff_id'] == handoff and notice['mention_owner'] is False
        manager.global_validation(LEAD, 'rework', {'validation_id': plan['id'], 'target': 'child', 'issue_url': 'https://github.com/example-user/fixture/issues/28'})
        assert len([e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['kind'] == 'rework']) == 1


def test_summary_distinguishes_actual_new_progress_from_an_unchanged_period(tmp_path):
    import json
    from test_task_control import TURN
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        clock.advance(600)
        (tmp_path / 'observed.json').write_text(json.dumps({'turns': [{'id': TURN, 'status': 'inProgress', 'itemsView': 'full', 'items': [{'id': 'progress-1', 'type': 'agentMessage', 'text': 'Verified selected project progress'}]}]}))
        manager.run_notifications(OWNER)
        clock.advance(300)
        first = next(e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'summary')
        assert '本周期已核实变化' in first['text'] and '无新进展' not in first['text']
        clock.advance(900)
        last = [e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'summary'][-1]
        assert '无新进展' in last['text']


def test_dashboard_offline_retains_same_notifications_but_never_claims_live_supervision_or_delivery(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ghost_hermes_pm.dashboard import create_router
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.run_notifications(OWNER)
        clock.advance(900)
        manager.run_notifications(OWNER)
        app = FastAPI()
        app.include_router(create_router(lambda request: ManagementClient(tmp_path / 'state', 'owner')))
        with TestClient(app) as browser:
            with ManagementServer(manager, {'owner': OWNER}):
                online = browser.get('/snapshot').json()
                assert online['notifications'] == manager.read_snapshot(OWNER)['notifications']
            offline = browser.get('/snapshot').json()
            assert offline['runtime'] == 'manager_unavailable'
            assert offline['notifications']['events'] == online['notifications']['events']
            assert offline['notifications']['health']['supervision'] == 'unavailable'
            assert offline['notifications']['health']['delivery'] == 'unverified'


def test_owner_short_answer_quotes_actual_entry_notification_and_returns_to_original_rpc(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace as NS
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import Transport, Gateway
    from test_questions import question_adapter, emit, user_question, replies
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        manager.refresh_task(OWNER, task_id)
        entry = channel('steward', 'entry')
        binding = {**entry, 'sender_tenant_key': entry['owner_tenant_key']}
        class EntryTransport(Transport):
            async def verify_identity(self, supplied):
                return {'app_id': entry['app_id'], 'open_id': entry['recipient_open_id']}
        transport, adapter = EntryTransport(), object()
        intake = FeishuEntry(lambda: manager, OWNER.subject, {'enabled': True, 'verification_ref': 'fixture:entry', 'bindings': [binding]}, lambda url: None)
        intake.attach_transport(adapter, transport)
        asyncio.run(intake.deliver_notifications(OWNER))
        alert = next(e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['kind'] == 'human_request')
        actual_id = alert['segments'][0]['attempts'][0]['message_id']
        raw = P2ImMessageReceiveV1({'header': {'event_type': 'im.message.receive_v1', 'app_id': entry['app_id'], 'tenant_key': entry['transport_tenant_key']},
            'event': {'sender': {'sender_type': 'user', 'tenant_key': entry['owner_tenant_key'], 'sender_id': {'open_id': entry['owner_open_id'], 'user_id': 'native-owner'}},
                'message': {'message_id': 'om_short_answer', 'chat_id': 'oc_entry', 'chat_type': 'group', 'message_type': 'text', 'parent_id': actual_id,
                    'content': json.dumps({'text': '@_user_1 回答：Blue'}), 'mentions': [{'key': '@_user_1', 'mentioned_type': 'bot', 'tenant_key': entry['recipient_tenant_key'], 'id': {'open_id': entry['recipient_open_id']}}]}}})
        original = NS(platform='feishu', user_id='native-owner', user_id_alt=None, chat_id='oc_entry', is_bot=False, message_id='om_short_answer')
        event = NS(source=original, raw_message=raw, message_id=original.message_id)
        assert asyncio.run(intake.receive(event, Gateway(adapter))) == {'action': 'skip'}
        resolved = manager.refresh_task(OWNER, task_id)['human_requests'][0]
        assert resolved['resolution'] == 'resolved' and resolved['reply']['source_anchor']['parent_id'] == actual_id
        assert replies(tmp_path) == [{'id': 8, 'result': {'answers': {'colour': {'answers': ['Blue']}}}}]


def test_stale_supervision_is_visible_without_claiming_current_delivery_or_current_source_coverage(tmp_path):
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        clock.advance(121)
        stale = manager.read_snapshot(OWNER)['notifications']['health']
        assert stale['supervision'] == 'unverified' and stale['delivery'] == 'unverified'
        assert stale['sources'][task_id]['status'] == 'unverified'
        assert stale['sources'][task_id]['last_confirmed_execution'] == 'running'
        manager.run_notifications(OWNER)
        assert manager.read_snapshot(OWNER)['notifications']['health']['supervision'] == 'running'


def test_revoked_host_process_coverage_blocks_stall_and_keeps_unknown_coverage_visible(tmp_path):
    clock = Clock()
    host_receipt = {}
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path, host_receipt), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        host_receipt['process_coverage'] = None
        clock.advance(900)
        assert not any(e['kind'] == 'suspected_stall' for e in manager.run_notifications(OWNER)['notifications'])
        assert manager.read_snapshot(OWNER)['notifications']['health']['sources'][task_id]['stall_coverage'] == 'unverified'


def test_approval_and_sensitive_requests_remain_immediate_and_stop_after_the_original_request_expires(tmp_path):
    import json
    from test_questions import question_adapter, emit, approval, user_question
    from test_task_control import TURN
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        emit(tmp_path, approval(rpc_id='operation'), user_question(rpc_id='secret', questions=[{'id': 'secret', 'header': 'Private', 'question': 'synthetic-sensitive-placeholder', 'isSecret': True, 'isOther': False, 'options': None}]))
        events = [e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request']
        assert len(events) == 2 and all(e['mention_owner'] for e in events)
        assert any('具体操作' in e['text'] and '范围：turn' in e['text'] for e in events)
        sensitive = next(q for q in manager.read_snapshot(OWNER)['requests'][0]['human_requests'] if q['category'] == 'sensitive')
        secret_notice = next(e for e in events if e['human_request_id'] == sensitive['id'])
        assert '原界面' in secret_notice['text'] and 'synthetic-sensitive-placeholder' not in json.dumps(manager.read_snapshot(OWNER))
        clock.advance(1800)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request']) == 4
        (tmp_path / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': TURN, 'status': 'completed', 'itemsView': 'full', 'items': []}]}))
        manager.run_notifications(OWNER)
        clock.advance(1800)
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request']) == 4


def test_repeated_handoff_block_does_not_create_new_alerts_or_new_execution(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    from test_task_control import wire
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        for _ in range(2):
            with pytest.raises(ManagementError):
                manager.record_task_delivery(OWNER, task_id, {'issue_updated_at': ISSUE['updated_at'], 'criteria': []})
            manager.run_notifications(OWNER)
        blocked = [e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['kind'] == 'blocked']
        assert len(blocked) == 1 and blocked[0]['mention_owner'] is False
        assert len([r for r in wire(tmp_path) if r['method'] == 'turn/start']) == 1


def test_changed_responsibility_keeps_actual_execution_unknown_and_supervision_block_visible(tmp_path):
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        snapshot = manager.read_snapshot(OWNER)
        profile = next(p for p in snapshot['profiles'] if p['id'] == 'mono-lead')
        correction = {k: profile[k] for k in ('id', 'native_profile', 'identity_ref', 'role', 'capability', 'project_id', 'parent_profile_id', 'connection_refs')}
        correction['identity_ref'] = 'fixture:new-responsible-role'
        manager.apply_directory_change(OWNER, snapshot['version'], {'profile': correction})
        manager.run_notifications(OWNER)
        changed = manager.read_snapshot(OWNER)
        assert changed['notifications']['health']['sources'][task_id]['status'] == 'unverified'
        task = changed['requests'][0]
        assert task['execution'] == 'unverified' and task['last_confirmed_execution'] == 'running' and task['repository_released'] is False
        assert len([e for e in changed['notifications']['events'] if e['kind'] == 'needs_owner']) == 1
        assert len([e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'needs_owner']) == 1


def test_idle_and_unchanged_active_sampling_do_not_expire_owner_scope_versions(tmp_path):
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path), notification_clock=clock) as manager:
        initial = manager.read_snapshot(OWNER)['version']
        manager.run_notifications(OWNER)
        clock.advance(5)
        manager.run_notifications(OWNER)
        assert manager.read_snapshot(OWNER)['version'] == initial
        assert manager.read_snapshot(OWNER)['notifications']['health']['supervision'] == 'running'
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        waiting_version = manager.read_snapshot(OWNER)['version']
        manager.run_notifications(OWNER)
        assert manager.read_snapshot(OWNER)['version'] == waiting_version
        manager.start_task(OWNER, task_id)
        manager.run_notifications(OWNER)
        stable = manager.read_snapshot(OWNER)['version']
        clock.advance(5)
        manager.run_notifications(OWNER)
        assert manager.read_snapshot(OWNER)['version'] == stable
        assert manager.read_snapshot(OWNER)['notifications']['health']['sources'][task_id]['checked_at'] == clock.now


def test_public_archive_only_notifies_after_all_scoped_entries_are_verified_and_restart_does_not_repeat(tmp_path):
    from test_lifecycle import ProfileHost, tree, scope_approval
    clock, host = Clock(), ProfileHost()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=host, notification_clock=clock) as manager:
        host.manager = manager
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        tree(manager, tmp_path)
        register_entry(manager)
        with ManagementServer(manager, {'owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'owner')
            approval = scope_approval(client.read_snapshot(), 'archive', {'profile_id': 'mono-lead', 'operation_id': 'notify-archive'})
            client.run_notifications()
            assert client.read_snapshot()['version'] == approval['expected_version']
            host.pending.add(('child-lead', 'bot'))
            operation = client.lifecycle('archive', approval)
            assert operation['status'] == 'processing'
            assert not any(e['kind'] == 'archive_completed' for e in client.run_notifications()['notifications'])
            host.pending.clear()
            done = client.lifecycle('check', {'operation_id': 'notify-archive'})
            assert done['status'] == 'completed' and done['approved_scope'] == {k: approval[k] for k in ('expected_version', 'expected_profile_ids')}
            first = [e for e in client.run_notifications()['notifications'] if e['kind'] == 'archive_completed']
            assert len(first) == 1 and first[0]['mention_owner'] is False
            assert first[0]['lifecycle_event_id'] == client.read_snapshot()['lifecycle_events'][0]['id']
            assert len([e for e in client.run_notifications()['notifications'] if e['kind'] == 'archive_completed']) == 1
    clock.advance(10000)
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, lifecycle_host=ProfileHost(), notification_clock=clock) as manager:
        events = [e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'archive_completed']
        assert len(events) == 1
        assert next(p for p in manager.read_snapshot(OWNER)['profiles'] if p['id'] == 'mono-lead')['lifecycle'] == 'archived'


def test_explicit_stop_intent_disables_pending_owner_reminders_before_actual_stop_is_confirmed(tmp_path):
    from test_questions import question_adapter, emit, user_question
    from test_task_control import TURN
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question())
        first = next(e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request')
        manager.control_task(OWNER, task_id, 'stop', 'stop-instead-of-answer', expected_turn_id=TURN)
        clock.advance(1800)
        notices = [e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request']
        assert len(notices) == 1
        assert manager.manage_notifications(OWNER, 'claim', {'event_id': first['id']}) is None
        assert manager.read_snapshot(OWNER)['requests'][0]['stop']['status'] == 'processing'


def test_partial_unknown_notification_delivery_is_durable_and_never_replayed_after_request_expires(tmp_path):
    from test_questions import question_adapter, emit, user_question
    clock = Clock()
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=question_adapter(tmp_path), notification_clock=clock) as manager:
        task_id = accepted(manager, make_repo(tmp_path / 'repo'))
        register_entry(manager)
        manager.start_task(OWNER, task_id)
        emit(tmp_path, user_question(questions=[{'id': 'long-question', 'header': 'Long', 'question': 'bounded public question ' * 200, 'isSecret': False, 'isOther': True, 'options': None}]))
        alert = next(e for e in manager.run_notifications(OWNER)['notifications'] if e['kind'] == 'human_request')
        first = manager.manage_notifications(OWNER, 'claim', {'event_id': alert['id']})
        manager.manage_notifications(OWNER, 'receipt', {'event_id': alert['id'], 'uuid': first['uuid'], 'receipt': {'status': 'delivered', 'message_id': 'om_partial', 'chat_id': 'oc_entry'}})
        second = manager.manage_notifications(OWNER, 'claim', {'event_id': alert['id']})
        assert second['reply_to'] == 'om_partial'
        manager.manage_notifications(OWNER, 'receipt', {'event_id': alert['id'], 'uuid': second['uuid'], 'receipt': {'status': 'unknown'}})
        assert manager.manage_notifications(OWNER, 'claim', {'event_id': alert['id']}) is None
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, notification_clock=clock) as manager:
        assert manager.manage_notifications(OWNER, 'claim', {'event_id': alert['id']}) is None
        saved = next(e for e in manager.read_snapshot(OWNER)['notifications']['events'] if e['id'] == alert['id'])
        assert saved['delivery'] == 'unknown'
        assert saved['segments'][0]['status'] == 'delivered' and saved['segments'][1]['status'] == 'unknown'
        assert saved['segments'][2]['status'] == 'pending'
