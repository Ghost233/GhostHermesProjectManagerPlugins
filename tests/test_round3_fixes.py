"""Owner scope answers through the original public task control and delivery path."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ghost_hermes_pm import ManagementError, VerifiedIdentity
from ghost_hermes_pm.transport import ManagementClient, ManagementServer
from readiness_support import ReadyManager
from test_directory import OWNER, make_repo
from test_requests import ISSUE
from test_task_execution import accepted, adapter_for


@contextmanager
def original_task(root, criterion):
    repo = make_repo(root / 'repo')
    (repo / 'test_original_scope.py').write_text('def test_original_scope():\n    assert 4 * 4 == 16\n')
    subprocess.run(['git', '-C', str(repo), 'add', 'test_original_scope.py'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Owned Scope', '-c', 'user.email=owned@example.invalid', 'commit', '-qm', 'Owned scope assertions'], check=True)
    scope = {**ISSUE, 'body': '- [ ] ' + criterion}
    with ReadyManager(root / 'state', owner_identity_ref=OWNER.subject, dsh_adapter=adapter_for(root)) as manager:
        request_id = accepted(manager, repo, scope)
        lead = VerifiedIdentity('fixture:lead', 'owned-participant')
        with ManagementServer(manager, {'owner': OWNER, 'lead': lead}):
            client = ManagementClient(root / 'state', 'owner')
            session = client.start_task(request_id)['session']
            yield manager, client, request_id, session, repo, scope


def actual_delivery(root, client, request_id, repo, scope):
    session = next(r for r in client.read_snapshot()['requests'] if r['id'] == request_id)['session']
    argv = [sys.executable, '-m', 'pytest', 'test_original_scope.py', '-q']
    result = subprocess.run(argv, cwd=repo, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, text=True, capture_output=True)
    assert result.returncode == 0 and '1 passed' in result.stdout
    (root / 'observed.json').write_text(json.dumps({'status': {'type': 'idle'}, 'turns': [{'id': session['turn_id'], 'status': 'completed', 'itemsView': 'full', 'items': [{'type': 'commandExecution', 'id': 'scope-test', 'command': ' '.join(argv), 'cwd': str(repo), 'status': 'completed', 'exitCode': result.returncode, 'aggregatedOutput': result.stdout + result.stderr}]}]}))
    from ghost_hermes_pm.delivery import acceptance_criteria
    return client.record_task_delivery(request_id, {'issue_updated_at': scope['updated_at'], 'criteria': [{'text': text, 'test_item_ids': ['scope-test']} for text in acceptance_criteria(scope['body'])], 'source_commit': None, 'pr_url': None, 'sync_branches': []})


@pytest.mark.parametrize('criterion', ['Merge status must be displayed.', '合并 PR 的状态必须展示'])
def test_sentence_initial_merge_status_is_reporting(tmp_path, criterion):
    with original_task(tmp_path, criterion) as (_, client, request_id, _, repo, scope):
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['pr_status'] == 'none'


def test_original_owner_append_resolves_conditional_acceptance(tmp_path):
    criterion = 'Merge the PR if the Owner approves.'
    with original_task(tmp_path, criterion) as (_, client, request_id, session, repo, scope):
        answered = client.control_task(request_id, 'append', 'owner-condition-answer',
            '本人明确：本任务只需完成测试即可交付，PR 无需合并；这是对原条件验收的答复。', session['turn_id'])
        assert answered['status'] == 'accepted' and answered['instruction']['phase'] == 'rpc_accepted'
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['accepted_scope'] == scope
        assert result['delivery_requirements']['clarification_criteria'] == [criterion]
        assert result['pr_status'] == 'none'


@pytest.mark.parametrize('kind', ['unrelated', 'reporting', 'suggestion', 'non_owner', 'stale_turn', 'other_task'])
def test_untrusted_or_unrelated_input_does_not_resolve_scope(tmp_path, kind):
    from test_requests import MESSAGE
    criterion = 'Merge the PR if the Owner approves.'
    text = '本人明确：本任务测试即可交付，PR无需合并。'
    with original_task(tmp_path, criterion) as (manager, client, request_id, session, repo, scope):
        target_client = client
        target = request_id
        expected = session['turn_id']
        if kind == 'unrelated':
            text = '本任务请继续核对测试结果。'
        elif kind == 'reporting':
            text = '本任务 Merge status must be displayed.'
        elif kind == 'suggestion':
            text = '建议：本任务测试即可交付，PR无需合并。'
        elif kind == 'non_owner':
            target_client = ManagementClient(tmp_path / 'state', 'lead')
        elif kind == 'stale_turn':
            expected = 'expired-original-turn'
        elif kind == 'other_task':
            target = manager.accept_request(OWNER, 'mono', 'mono-lead', {**MESSAGE, 'message_id': 'om_other_condition'}, scope)['request']['id']
        if kind in {'stale_turn', 'other_task'}:
            with pytest.raises(ManagementError):
                target_client.control_task(target, 'append', 'invalid-condition-answer', text, expected)
        else:
            assert target_client.control_task(target, 'append', 'non-decision', text, expected)['status'] == 'accepted'
        with pytest.raises(ManagementError) as blocked:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert blocked.value.code == 'needs_clarification'
        record = next(r for r in client.read_snapshot()['requests'] if r['id'] == request_id)
        assert not record.get('delivery_scope_interpretations')
        assert record['accepted_scope'] == scope
        assert record['repository_released'] is False


def test_owner_reply_cannot_waive_explicit_mandatory_acceptance(tmp_path):
    with original_task(tmp_path, 'The PR must be merged into main.') as (_, client, request_id, session, repo, scope):
        assert client.control_task(request_id, 'append', 'owner-unrelated-waiver',
            '本人明确：本任务测试即可交付，PR无需合并。', session['turn_id'])['status'] == 'accepted'
        with pytest.raises(ManagementError) as required:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert required.value.code == 'evidence_missing'
        record = client.read_snapshot()['requests'][0]
        assert not record.get('delivery_scope_interpretations')
        assert record['delivery_requirements']['merge_required'] is True


@pytest.mark.parametrize('prefix', ['追加：', ''])
def test_owner_group_answer_keeps_original_message_provenance_and_duplicate(tmp_path, prefix):
    import asyncio
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, _, repo, scope):
        adapter, transport = object(), Transport()
        intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: ISSUE)
        intake.attach_transport(adapter, transport)
        incoming = event(prefix + '本人明确：本任务测试即可交付，PR无需合并。', 'om_owner_condition_reply')
        incoming.raw_message.event.message.parent_id = 'om_ack'
        assert asyncio.run(intake.receive(incoming, Gateway(adapter))) == {'action': 'skip'}
        assert asyncio.run(intake.receive(incoming, Gateway(adapter))) == {'action': 'skip'}
        record = client.read_snapshot()['requests'][0]
        assert len(record['delivery_scope_interpretations']) == 1
        interpretation = record['delivery_scope_interpretations'][0]
        assert interpretation['owner'] == OWNER.subject
        assert interpretation['source_anchor']['message_id'] == 'om_owner_condition_reply'
        assert interpretation['source_anchor']['chat_id'] == 'oc_project'
        assert interpretation['received_at'] and interpretation['sent_back_at']
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['delivery_evidence']['owner_interpretation_ids'] == [interpretation['id']]
        assert result['accepted_scope'] == scope


def test_owner_approval_of_original_condition_still_requires_merge(tmp_path):
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (_, client, request_id, session, repo, scope):
        assert client.control_task(request_id, 'append', 'owner-approval',
            '本人明确：本任务批准合并 PR。', session['turn_id'])['status'] == 'accepted'
        with pytest.raises(ManagementError) as required:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert required.value.code == 'evidence_missing'
        task = client.read_snapshot()['requests'][0]
        assert task['delivery_scope_interpretations'][0]['merge_required'] is True
        assert task['repository_released'] is False


def test_latest_valid_owner_answer_supersedes_earlier_condition_answer(tmp_path):
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (_, client, request_id, session, repo, scope):
        client.control_task(request_id, 'append', 'owner-first-answer',
            '本人明确：本任务必须合并 PR。', session['turn_id'])
        client.control_task(request_id, 'append', 'owner-latest-answer',
            '本人明确：本任务测试即可交付，PR无需合并。', session['turn_id'])
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert len(result['delivery_scope_interpretations']) == 2
        assert result['delivery_evidence']['owner_interpretation_ids'] == ['owner-latest-answer']
        assert result['accepted_scope'] == scope


def test_explicit_owner_refusal_of_merge_is_not_positive_approval(tmp_path):
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (_, client, request_id, session, repo, scope):
        client.control_task(request_id, 'append', 'owner-refusal',
            '本人明确：本任务不批准合并 PR，完成测试即可交付。', session['turn_id'])
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['delivery_scope_interpretations'][0]['merge_required'] is False
        assert result['delivery_scope_interpretations'][0]['merge_forbidden'] is True


def test_owner_interpretation_expires_when_original_responsibility_changes(tmp_path):
    from test_directory import registration
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (_, client, request_id, session, repo, scope):
        client.control_task(request_id, 'append', 'owner-before-change',
            '本人明确：本任务测试即可交付，PR无需合并。', session['turn_id'])
        snapshot = client.read_snapshot()
        profile = next(p for p in snapshot['profiles'] if p['id'] == 'mono-lead')
        change = registration(repo)
        change['profile'].update(identity_ref='fixture:replacement', connection_refs=profile['connection_refs'])
        client.apply_directory_change(snapshot['version'], change)
        with pytest.raises(ManagementError) as blocked:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert blocked.value.code == 'needs_clarification'
        record = client.read_snapshot()['requests'][0]
        assert len(record['delivery_scope_interpretations']) == 1
        assert record['accepted_scope'] == scope and record['repository_released'] is False
