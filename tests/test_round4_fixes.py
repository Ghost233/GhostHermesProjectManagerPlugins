"""Explicit original Owner decisions are separate from quoted or pending approval."""
import asyncio

import pytest

from ghost_hermes_pm import ManagementError
from test_round3_fixes import original_task, actual_delivery
from test_directory import OWNER


def answer_original(manager, client, request_id, session, text, seam):
    if seam == 'append':
        assert client.control_task(request_id, 'append', 'owner-round4-answer', text, session['turn_id'])['status'] == 'accepted'
        return
    from ghost_hermes_pm.messages import FeishuEntry
    from test_feishu_entry import CONFIG, Gateway, Transport, event
    adapter = object()
    intake = FeishuEntry(lambda: manager, OWNER.subject, CONFIG, lambda _: None)
    intake.attach_transport(adapter, Transport())
    incoming = event(text, 'om_round4_owner_answer')
    incoming.raw_message.event.message.parent_id = 'om_ack'
    assert asyncio.run(intake.receive(incoming, Gateway(adapter))) == {'action': 'skip'}
    assert asyncio.run(intake.receive(incoming, Gateway(adapter))) == {'action': 'skip'}


@pytest.mark.parametrize('seam', ['append', 'group'])
@pytest.mark.parametrize('text', [
    '本人明确：本任务尚未批准合并 PR，只需完成测试即可交付。',
    '本任务的测试日志出现“批准合并 PR”的字样，请核查来源。',
    '本任务“批准”合并 PR。',
])
def test_pending_or_quoted_approval_is_not_a_current_owner_decision(tmp_path, seam, text):
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, seam)
        record = client.read_snapshot()['requests'][0]
        assert not record.get('delivery_scope_interpretations')
        with pytest.raises(ManagementError) as pending:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert pending.value.code == 'needs_clarification'
        record = client.read_snapshot()['requests'][0]
        assert record['accepted_scope'] == scope and record['repository_released'] is False


@pytest.mark.parametrize('seam', ['append', 'group'])
@pytest.mark.parametrize('text', ['本任务PR不是必须合并', '本任务PR无需合并'])
def test_shared_optional_vocabulary_resolves_original_owner_condition(tmp_path, seam, text):
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, seam)
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['accepted_scope'] == scope
        assert len(result['delivery_scope_interpretations']) == 1
        interpretation = result['delivery_scope_interpretations'][0]
        assert interpretation['merge_required'] is False
        if seam == 'group':
            assert interpretation['source_anchor']['message_id'] == 'om_round4_owner_answer'


@pytest.mark.parametrize('text, required', [
    ('本人明确：本任务尚未批准合并 PR，PR无需合并，合并状态必须展示。', False),
    ('本任务日志写“批准合并 PR”；本人明确：本任务PR无需合并，合并状态必须展示。', False),
    ('本任务日志写“无需合并 PR”；本人明确：本任务批准合并 PR，合并状态必须展示。', True),
])
def test_explicit_current_decision_is_separate_from_materials_and_status(tmp_path, text, required):
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, 'append')
        if required:
            with pytest.raises(ManagementError) as missing:
                actual_delivery(tmp_path, client, request_id, repo, scope)
            assert missing.value.code == 'evidence_missing'
        else:
            result = actual_delivery(tmp_path, client, request_id, repo, scope)
            assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        record = client.read_snapshot()['requests'][0]
        assert record['delivery_scope_interpretations'][0]['merge_required'] is required
        assert record['accepted_scope'] == scope


def test_multiple_original_conditions_need_exact_current_owner_targets(tmp_path):
    first = 'Merge the PR if the Owner approves.'
    second = 'Merge changes if the Owner consents.'
    with original_task(tmp_path, first + '\n- [ ] ' + second) as (_, client, request_id, session, repo, scope):
        client.control_task(request_id, 'append', 'owner-ambiguous', '本任务PR无需合并', session['turn_id'])
        assert not client.read_snapshot()['requests'][0].get('delivery_scope_interpretations')
        for index, criterion in enumerate((first, second)):
            client.control_task(request_id, 'append', 'owner-exact-' + str(index),
                '本任务“' + criterion + '”，PR不是必须合并。', session['turn_id'])
            if index == 0:
                with pytest.raises(ManagementError) as pending:
                    actual_delivery(tmp_path, client, request_id, repo, scope)
                assert pending.value.code == 'needs_clarification'
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['accepted_scope'] == scope
        assert [item['criterion'] for item in result['delivery_scope_interpretations']] == [first, second]
