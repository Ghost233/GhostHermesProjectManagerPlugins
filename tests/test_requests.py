from ghost_hermes_pm import Manager, VerifiedIdentity
from readiness_support import ReadyManager as Manager
from test_directory import OWNER, make_repo, registration


MESSAGE = {'tenant_key': 'tenant-fixture', 'recipient_open_id': 'ou_lead',
           'app_id': 'cli_fixture', 'transport_tenant_key': 'tenant-transport', 'recipient_tenant_key': 'tenant-bot',
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
        assert pending['outbox'][0]['segments'][0]['attempts'][0]['intended_reply_to'] == 'om_request'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        assert restarted.claim_delivery(OWNER, request['id']) is None


def test_plain_input_associates_only_unique_request_and_multiple_candidates_require_clarification(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        first = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
        associated = manager.associate_message(OWNER, 'mono', 'mono-lead',
                                               {**MESSAGE, 'message_id': 'om_input'}, '请保留测试证据')
        assert associated['status'] == 'associated'
        assert associated['request_id'] == first['id']
        second = manager.accept_request(OWNER, 'mono', 'mono-lead',
                                        {**MESSAGE, 'message_id': 'om_second'}, ISSUE)['request']
        ambiguous = manager.associate_message(OWNER, 'mono', 'mono-lead',
                                              {**MESSAGE, 'message_id': 'om_ambiguous'}, '请继续')
        assert ambiguous['status'] == 'needs_clarification'
        assert set(ambiguous['candidate_ids']) == {first['id'], second['id']}
        quote = manager.associate_message(OWNER, 'mono', 'mono-lead',
                                         {**MESSAGE, 'message_id': 'om_quote', 'root_id': 'om_request'}, '进度：正在核对')
        assert quote['request_id'] == first['id']
        assert quote['kind'] == 'progress'
        assert manager.associate_message(OWNER, 'mono', 'mono-lead',
                                        {**MESSAGE, 'message_id': 'om_thanks'}, '谢谢')['status'] == 'ignored'
        snapshot = manager.read_snapshot(OWNER)
        assert len(snapshot['requests']) == 2
        assert snapshot['requests'][0]['accepted_scope'] == ISSUE
        assert len(snapshot['clarifications']) == 1


def test_retry_only_definite_failed_segment_retains_uuid_and_confirmed_anchor(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        request = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
        manager.publish_request_message(OWNER, request['id'], 'confirmation', '已受理')
        ack = manager.claim_delivery(OWNER, request['id'])
        manager.record_delivery(OWNER, request['id'], ack['uuid'],
                                {'status': 'delivered', 'message_id': 'om_ack', 'chat_id': 'oc_project'})
        manager.publish_request_message(OWNER, request['id'], 'material', 'A' * 2500)
        first = manager.claim_delivery(OWNER, request['id'])
        manager.record_delivery(OWNER, request['id'], first['uuid'], {'status': 'failed', 'code': 999})
        manager.retry_delivery(OWNER, request['id'])
        retried = manager.claim_delivery(OWNER, request['id'])
        assert retried['uuid'] == first['uuid']
        assert retried['reply_to'] == 'om_ack'
        manager.record_delivery(OWNER, request['id'], retried['uuid'],
                                {'status': 'delivered', 'message_id': 'om_chunk_1', 'chat_id': 'oc_project'})
        second = manager.claim_delivery(OWNER, request['id'])
        assert second['number'] == 2
        manager.record_delivery(OWNER, request['id'], second['uuid'], {'status': 'unknown'})
        with pytest.raises(ManagementError) as conflict:
            manager.retry_delivery(OWNER, request['id'])
        assert conflict.value.code == 'version_conflict'
        task = manager.read_snapshot(OWNER)['requests'][0]
        assert task['task_start_anchor']['message_id'] == 'om_ack'
        assert task['delivery'] == 'unknown'
        assert len(task['outbox'][1]['segments'][0]['attempts']) == 2


def test_secret_material_is_rejected_before_records_or_group_publications(tmp_path):
    import pytest
    from ghost_hermes_pm import ManagementError
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, sensitive_values=('opaque-fixture-secret',)) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        with pytest.raises(ManagementError):
            manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE,
                                   {**ISSUE, 'body': 'API_KEY=fixture-sensitive-value'})
        assert manager.read_snapshot(OWNER)['requests'] == []
        record = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
        with pytest.raises(ManagementError):
            manager.publish_request_message(OWNER, record['id'], 'result', 'token: fixture-sensitive-value')
        with pytest.raises(ManagementError):
            manager.publish_request_message(OWNER, record['id'], 'result', 'The opaque-fixture-secret must stay private.')
        with pytest.raises(ManagementError):
            manager.associate_message(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'om_secret'},
                                      'password=fixture-sensitive-value')
        assert 'fixture-sensitive-value' not in str(manager.read_snapshot(OWNER))


def test_same_literal_ids_in_another_app_never_duplicate_or_associate(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        first = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
        other = {**MESSAGE, 'app_id': 'cli_other', 'transport_tenant_key': 'other-transport', 'recipient_tenant_key': 'other-bot'}
        plain = manager.associate_message(OWNER, 'mono', 'mono-lead', {**other, 'message_id': 'om_input'}, '请核对')
        assert plain['status'] == 'unassociated'
        second = manager.accept_request(OWNER, 'mono', 'mono-lead', other, {**ISSUE, 'body': 'Another app scope'})
        assert second['duplicate'] is False
        assert second['request']['id'] != first['id']
        assert second['request']['accepted_scope']['body'] == 'Another app scope'


def test_explicit_parent_start_anchor_disambiguates_shared_native_thread(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        ids = []
        for number in (1, 2):
            source = {**MESSAGE, 'message_id': 'om_request_' + str(number), 'root_id': 'om_root', 'thread_id': 'omt_shared'}
            record = manager.accept_request(OWNER, 'mono', 'mono-lead', source, ISSUE)['request']
            ids.append(record['id'])
            manager.publish_request_message(OWNER, record['id'], 'confirmation', '已受理')
            segment = manager.claim_delivery(OWNER, record['id'])
            manager.record_delivery(OWNER, record['id'], segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project',
                'message_id': 'om_ack_' + str(number), 'root_id': 'om_root', 'thread_id': 'omt_shared'})
        result = manager.associate_message(OWNER, 'mono', 'mono-lead',
            {**MESSAGE, 'message_id': 'om_reply', 'parent_id': 'om_ack_1', 'root_id': 'om_root', 'thread_id': 'omt_shared'}, '请核对')
        assert result['status'] == 'associated'
        assert result['request_id'] == ids[0]


def test_new_publication_and_inflight_restart_never_reuse_old_delivered_summary(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as manager:
        manager.apply_directory_change(OWNER, 0, registration(make_repo(tmp_path / 'repo')))
        record = manager.accept_request(OWNER, 'mono', 'mono-lead', MESSAGE, ISSUE)['request']
        manager.publish_request_message(OWNER, record['id'], 'confirmation', '已受理')
        ack = manager.claim_delivery(OWNER, record['id'])
        manager.record_delivery(OWNER, record['id'], ack['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack'})
        assert manager.read_snapshot(OWNER)['requests'][0]['delivery'] == 'delivered'
        manager.publish_request_message(OWNER, record['id'], 'progress', '正在核对')
        assert manager.read_snapshot(OWNER)['requests'][0]['delivery'] == 'pending'
        manager.claim_delivery(OWNER, record['id'])
        assert manager.read_snapshot(OWNER)['requests'][0]['delivery'] == 'sending'
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject) as restarted:
        current = restarted.read_snapshot(OWNER)['requests'][0]
        assert current['delivery'] == 'unknown'
        assert current['outbox'][1]['segments'][0]['status'] == 'unknown'
        assert restarted.claim_delivery(OWNER, record['id']) is None
