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
