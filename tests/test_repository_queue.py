"""Repository queue behavior through the authenticated public bridge and JSONL peer."""
import json
import subprocess
from pathlib import Path

import pytest

from ghost_hermes_pm import Manager, ManagementError
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from test_directory import OWNER, make_repo, registration
from test_requests import MESSAGE, ISSUE
from test_task_execution import adapter_for, accepted


def acknowledge(manager, project_id='mono', profile_id='mono-lead', suffix='second', issue=None):
    request = manager.accept_request(OWNER, project_id, profile_id, {**MESSAGE, 'message_id': 'om_' + suffix}, issue or ISSUE)['request']
    manager.publish_request_message(OWNER, request['id'], 'confirmation', '已受理')
    segment = manager.claim_delivery(OWNER, request['id'])
    manager.record_delivery(OWNER, request['id'], segment['uuid'], {'status': 'delivered', 'chat_id': 'oc_project', 'message_id': 'om_ack_' + suffix})
    return request['id']


def test_busy_repository_persists_fifo_reason_and_unknown_occupancy_across_restart(tmp_path):
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as manager:
        first = accepted(manager, make_repo(tmp_path / 'repo'))
        second = acknowledge(manager)
        with ManagementServer(manager, {'queue-owner': OWNER}):
            client = ManagementClient(tmp_path / 'state', 'queue-owner')
            client.start_task(first)
            with pytest.raises(ManagementError) as busy:
                client.start_task(second)
            assert busy.value.code == 'repository_busy'
            records = {r['id']: r for r in client.read_snapshot()['requests']}
            assert records[first]['queue']['status'] == 'occupied'
            assert records[second]['queue']['status'] == 'queued'
            assert records[second]['queue']['blocked_by'] == [first]
            assert records[second]['queue']['sequence'] > records[first]['queue']['sequence']
            assert any('排队' in s['text'] for p in records[second]['outbox'] for s in p['segments'])
    with Manager(tmp_path / 'state', owner_identity_ref=OWNER.subject, codex_adapter=adapter_for(tmp_path)) as restarted:
        records = {r['id']: r for r in restarted.read_snapshot(OWNER)['requests']}
        assert records[first]['execution'] == 'unverified'
        assert records[first]['queue']['status'] == 'occupied'
        assert records[second]['queue']['blocked_by'] == [first]
        with pytest.raises(ManagementError) as busy:
            restarted.start_task(OWNER, second)
        assert busy.value.code == 'repository_busy'
