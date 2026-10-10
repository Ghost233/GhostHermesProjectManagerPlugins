"""Markdown log regions remain materials in the original Owner reply paths."""
import pytest

from ghost_hermes_pm import ManagementError
from test_round3_fixes import original_task, actual_delivery
from test_round4_fixes import answer_original


@pytest.mark.parametrize('seam', ['append', 'group'])
def test_fenced_log_is_not_a_current_owner_approval(tmp_path, seam):
    text = '本任务测试日志如下，请核查来源：\n```text\n批准合并 PR\n```'
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, seam)
        assert not client.read_snapshot()['requests'][0].get('delivery_scope_interpretations')
        with pytest.raises(ManagementError) as pending:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert pending.value.code == 'needs_clarification'
        record = client.read_snapshot()['requests'][0]
        assert record['accepted_scope'] == scope and record['repository_released'] is False


@pytest.mark.parametrize('material', [
    '~~~text\n批准合并 PR\n~~~',
    '````text\n```\n批准合并 PR\n````',
    '```text\n~~~\n批准合并 PR\n```',
    '```text\n批准合并 PR',
])
def test_complete_fence_region_keeps_materials_out_of_decisions(tmp_path, material):
    text = '本任务测试日志如下，请核查来源：\n' + material
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, 'append')
        assert not client.read_snapshot()['requests'][0].get('delivery_scope_interpretations')
        with pytest.raises(ManagementError) as pending:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert pending.value.code == 'needs_clarification'


@pytest.mark.parametrize('seam', ['append', 'group'])
@pytest.mark.parametrize('required', [False, True])
def test_current_owner_decision_outside_fence_remains_effective(tmp_path, seam, required):
    material = 'PR无需合并' if required else '批准合并 PR'
    decision = '批准合并 PR' if required else 'PR不是必须合并'
    text = '本任务测试日志如下：\n```text\n' + material + '\n```\n本人明确：本任务' + decision + '。'
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, seam)
        if required:
            with pytest.raises(ManagementError) as missing:
                actual_delivery(tmp_path, client, request_id, repo, scope)
            assert missing.value.code == 'evidence_missing'
        else:
            result = actual_delivery(tmp_path, client, request_id, repo, scope)
            assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        record = client.read_snapshot()['requests'][0]
        assert len(record['delivery_scope_interpretations']) == 1
        interpretation = record['delivery_scope_interpretations'][0]
        assert interpretation['merge_required'] is required
        assert record['accepted_scope'] == scope
        if seam == 'group':
            assert interpretation['source_anchor']['message_id'] == 'om_round4_owner_answer'


def test_exact_original_criterion_in_material_selects_target_without_authorizing(tmp_path):
    first = 'Merge the PR if the Owner approves.'
    second = 'Merge changes if the Owner consents.'
    with original_task(tmp_path, first + '\n- [ ] ' + second) as (_, client, request_id, session, repo, scope):
        text = '本任务原条件与测试日志：\n```text\n' + first + '\n批准合并 PR\n```\n本人明确：本任务PR无需合并。'
        client.control_task(request_id, 'append', 'owner-fenced-criterion', text, session['turn_id'])
        record = client.read_snapshot()['requests'][0]
        assert record['delivery_scope_interpretations'][0]['criterion'] == first
        assert record['delivery_scope_interpretations'][0]['merge_required'] is False
        with pytest.raises(ManagementError) as pending:
            actual_delivery(tmp_path, client, request_id, repo, scope)
        assert pending.value.code == 'needs_clarification'
        client.control_task(request_id, 'append', 'owner-second-criterion',
            '本任务“' + second + '”，PR无需合并。', session['turn_id'])
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['accepted_scope'] == scope


@pytest.mark.parametrize('seam', ['append', 'group'])
def test_questions_and_suggestions_inside_fence_do_not_veto_current_owner_decision(tmp_path, seam):
    text = '本任务测试日志如下：\n```text\n建议批准合并 PR？\n```\n本人明确：本任务PR无需合并。'
    with original_task(tmp_path, 'Merge the PR if the Owner approves.') as (manager, client, request_id, session, repo, scope):
        answer_original(manager, client, request_id, session, text, seam)
        result = actual_delivery(tmp_path, client, request_id, repo, scope)
        assert result['task_delivery'] == 'delivered' and result['repository_released'] is True
        assert result['accepted_scope'] == scope
