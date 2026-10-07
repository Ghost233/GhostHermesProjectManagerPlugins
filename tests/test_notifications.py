"""Notification timings through the shared management boundary and public native sender."""
from ghost_hermes_pm import Manager
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
            first = client.run_notifications()['notifications']
            assert len(first) == 1 and first[0]['kind'] == 'summary'
            assert first[0]['project_id'] == 'mono' and first[0]['request_ids'] == [task_id]
            assert all(label in first[0]['text'] for label in ('状态', '进展', '阻塞', '待处理', '下一步', '无新进展'))
            clock.advance(900)
            assert len(client.run_notifications()['notifications']) == 2
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
