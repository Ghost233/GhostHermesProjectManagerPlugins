from ghost_hermes_pm import Manager, VerifiedIdentity
from test_directory import OWNER, make_repo, registration


MESSAGE = {'tenant_key': 'tenant-fixture', 'recipient_open_id': 'ou_lead',
           'chat_id': 'oc_project', 'message_id': 'om_request', 'sender_open_id': 'ou_owner',
           'parent_id': None, 'root_id': None, 'thread_id': None}
ISSUE = {'url': 'https://github.com/Ghost233/fixture/issues/15', 'title': 'Fix fixture behavior',
         'body': 'Acceptance: preserve the original request.', 'updated_at': '2026-10-07T00:00:00Z'}


def test_verified_owner_accepts_issue_once_with_fixed_scope_and_waiting_execution(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        first = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)
        duplicate = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE,
                                           {**ISSUE, 'body': 'Changed source must not expand accepted scope.'})
        tasks = manager.read_snapshot(OWNER)['requests']
    assert first['status'] == 'accepted'
    assert duplicate['request']['id'] == first['request']['id']
    assert len(tasks) == 1
    assert tasks[0]['accepted_scope']['body'] == 'Acceptance: preserve the original request.'
    assert tasks[0]['execution'] == 'waiting'
    assert tasks[0]['unexecuted_reason'] == 'Codex execution is not enabled.'
    assert tasks[0]['delivery'] == 'pending'
    assert tasks[0]['source_anchor']['message_id'] == 'om_request'


def test_confirmation_material_is_tracked_per_segment_and_unknown_send_is_not_retried(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        request = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
        publication = manager.publish_request_message(OWNER, request['id'], 'confirmation', 'A' * 2500)
        segment = manager.claim_delivery(OWNER, request['id'])
        manager.record_delivery(OWNER, request['id'], segment['uuid'], {'status': 'unknown'})
        assert manager.claim_delivery(OWNER, request['id']) is None
        pending = manager.read_snapshot(OWNER)['requests'][0]
        assert pending['delivery'] == 'unknown'
        assert len(pending['outbox'][0]['segments']) == 2
        assert pending['task_start_anchor'] is None
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        assert restarted.claim_delivery(OWNER, request['id']) is None
