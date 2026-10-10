"""Referenced operation decisions must never fall back to broad consent."""
import json
from types import SimpleNamespace

import pytest

from ghost_hermes_pm.repository_approvals import _material, _source, prepare_approval_reply
from ghost_hermes_pm.manager import ManagementError


@pytest.fixture
def reply_context():
    namespace = {'app_id': 'cli_fixture', 'transport_tenant_key': 'tenant-app',
                 'recipient_tenant_key': 'tenant-bot', 'recipient_open_id': 'ou_bot', 'chat_id': 'oc_fixture'}
    record = {'id': 'work-fixture', 'profile_id': 'developer-fixture', 'target': {'repository': 'fixture/repository'},
              'dsh_execution': {'approvals': [{'approval_id': 'approval-fixture',
                  'request_message_id': 'om_approval', 'delivery': dict(namespace)}]}}
    adapter, transport = object(), object()
    intake = SimpleNamespace(snapshot=lambda: {'work': [record]}, transports=[(adapter, transport)])
    return intake, SimpleNamespace(source=SimpleNamespace(is_bot=False)), adapter, namespace


def prepare(context, text, parent=None, **namespace):
    intake, event, adapter, binding = context
    return prepare_approval_reply(intake, event, adapter, binding,
        {**binding, 'parent_id': parent, **namespace}, text, ['developer-fixture'])


def test_decision_requires_exact_notice_or_unique_explicit_id(reply_context):
    assert prepare(reply_context, '批准一次', 'om_approval').work_id == 'work-fixture'
    assert prepare(reply_context, '拒绝 审批 approval-fixture').work_id == 'work-fixture'
    assert prepare(reply_context, '批准一次', 'om_task_start').rejected == 'approval_target'
    assert prepare(reply_context, '批准一次 审批 approval-old').rejected == 'approval_target'
    assert prepare(reply_context, '批准一次 审批 approval-old', 'om_approval').rejected == 'approval_target'


def test_generic_consent_is_recognized_only_as_a_rejected_approval_reply(reply_context):
    from ghost_hermes_pm.repository_approvals import _decision
    for text in ('同意', '继续', '方案确认', '批准永久', '批准一次并继续执行下一操作'):
        assert _decision(text) == (None, None)
        assert prepare(reply_context, text, 'om_approval').action == 'approval'
    assert prepare(reply_context, '同意', 'om_question') is None


def test_bot_foreign_namespace_and_ambiguous_ids_cannot_select_approval(reply_context):
    intake, event, adapter, binding = reply_context
    event.source.is_bot = True
    assert prepare(reply_context, '批准一次', 'om_approval') is None
    event.source.is_bot = False
    assert prepare(reply_context, '批准一次', 'om_approval', chat_id='oc_other') is None
    original = intake.snapshot()['work'][0]
    intake.snapshot = lambda: {'work': [original, {**original, 'id': 'work-other'}]}
    assert prepare(reply_context, '批准一次 审批 approval-fixture').rejected == 'approval_target'


def test_safe_notice_contains_the_exact_original_command_and_scope():
    intake = SimpleNamespace(secret_values=['synthetic-secret-value'])
    command = "printf 'approved\\n' > approval-result.txt"
    operation = {'command': command, 'sandbox_permissions': 'workspace-write', 'description': 'Create one fixture file.'}
    text = _material(intake, {'reason': 'Write only this fixture file.'}, operation, '/fixture/repository')
    assert command in text and 'workspace-write' in text and 'Write only this fixture file.' in text
    assert _material(intake, {'reason': 'synthetic-secret-value'}, operation, '/fixture/repository') is None
    assert _material(intake, {'reason': 'Read API key.'}, operation, '/fixture/repository') is None


@pytest.mark.parametrize('mention', [None, 'ou_fixture'])
def test_native_feishu_post_preserves_the_complete_original_command(mention):
    import asyncio
    from ghost_hermes_pm.feishu import NativeFeishuTransport
    requests = []
    def reply(request):
        requests.append(request)
        return SimpleNamespace(code=0, data=SimpleNamespace(message_id='om_fixture', chat_id='oc_fixture'))
    native = SimpleNamespace(config=None, im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=reply))))
    command = "printf 'approved\\n' > approval-result.txt"
    response = asyncio.run(NativeFeishuTransport(native).send({'uuid': 'fixture', 'text': command,
        'reply_to': 'om_request', 'mention_open_id': mention}))
    assert response['status'] == 'delivered' and requests[0].request_body.msg_type == 'post'
    content = json.loads(requests[0].request_body.content)
    text = ''.join(item['text'] for row in content['zh_cn']['content'] for item in row if item['tag'] == 'text')
    assert command in text and 'text' not in content


def test_original_call_is_authority_for_command_not_approval_reason():
    command = "printf 'fixture\\n' > result.txt"
    request = {'toolName': 'bash', 'callId': 'call-fixture', 'reason': 'A different description.'}
    events = [{'seq': 0, 'type': 'turn/start', 'data': {'turn': 1}},
        {'seq': 1, 'type': 'tool/call', 'data': {'turn': 1, 'step': 1, 'callId': 'call-fixture',
            'name': 'bash', 'arguments': json.dumps({'command': command, 'sandbox_permissions': 'workspace-write'})}},
        {'seq': 2, 'type': 'approval/asked', 'data': {'id': 'approval-fixture', **request}}]
    _, status, operation = _source(events, request, '/fixture/repository')
    assert status['state'] == 'pending' and operation['command'] == command
    with pytest.raises(ManagementError):
        _source(events, {**request, 'callId': 'call-foreign'}, '/fixture/repository')
    events[1]['data']['arguments'] = json.dumps({'command': command, 'sandbox_permissions': 'danger-full-access'})
    with pytest.raises(ManagementError):
        _source(events, request, '/fixture/repository')
