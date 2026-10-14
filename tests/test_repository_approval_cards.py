"""Original Feishu card requests preserve one operation and one native decision."""
import json
import asyncio
import hashlib
import time
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from ghost_hermes_pm.feishu import NativeFeishuTransport


@pytest.mark.asyncio
async def test_original_transport_publishes_interactive_approval_card():
    requests = []

    def reply(request):
        requests.append(request)
        return NS(code=0, data=NS(message_id='om_approval', chat_id='oc_fixture'))

    native = NS(config=None, im=NS(v1=NS(message=NS(reply=reply))))
    card = {'header': {'title': {'tag': 'plain_text', 'content': '具体执行审批'}},
            'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': '原命令：printf fixture'}}]}
    receipt = await NativeFeishuTransport(native).send({'uuid': 'fixture-card',
        'text': 'approval fallback', 'card': card, 'reply_to': 'om_request', 'chat_id': 'oc_fixture'})

    assert receipt['status'] == 'delivered'
    assert requests[0].request_body.msg_type == 'interactive'
    assert json.loads(requests[0].request_body.content) == card
    assert requests[0].request_body.uuid == 'fixture-card'
    assert requests[0].message_id == 'om_request'


@pytest.mark.asyncio
async def test_original_transport_updates_only_the_existing_approval_card():
    requests = []

    def patch(request):
        requests.append(request)
        return NS(code=0)

    native = NS(config=None, im=NS(v1=NS(message=NS(patch=patch))))
    card = {'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': '原审批已拒绝；原工具失败'}}]}
    receipt = await NativeFeishuTransport(native).update_card({'message_id': 'om_approval', 'card': card})

    assert receipt == {'status': 'updated', 'code': 0}
    assert requests[0].message_id == 'om_approval'
    assert json.loads(requests[0].request_body.content) == card


@pytest.fixture
def pending_card():
    binding = {'app_id': 'cli_fixture', 'transport_tenant_key': 'tenant-app',
        'recipient_tenant_key': 'tenant-bot', 'recipient_open_id': 'ou_bot', 'chat_id': 'oc_fixture',
        'sender_tenant_key': 'tenant-owner', 'owner_open_id': 'ou_owner',
        'owner_native_ids': ['u_owner', 'on_owner'], 'profile_id': 'developer-fixture',
        'verification_ref': 'fixture:identity'}
    notice = {'approval_id': 'approval-fixture', 'call_id': 'call-fixture',
        'work_id': 'work-fixture', 'card_id': 'card-fixture', 'generation': 'generation-fixture',
        'session_id': 'session-fixture', 'command_sha256': 'a' * 64, 'arguments_sha256': 'b' * 64,
        'state': 'pending', 'answerable': True, 'material': "原命令：printf 'fixture\\n' > result.txt\n本次范围：workspace-write",
        'request_renderer': 'interactive', 'request_notice': 'delivered', 'request_message_id': 'om_approval',
        'delivery': {key: binding[key] for key in ('app_id', 'transport_tenant_key', 'recipient_tenant_key', 'recipient_open_id', 'chat_id')}}
    record = {'id': 'work-fixture', 'card_id': 'card-fixture', 'profile_id': 'developer-fixture',
        'target': {'repository': 'example-user/fixture'},
        'issue': {'title': 'One bounded operation', 'url': 'https://github.com/example-user/fixture/issues/15'},
        'dsh_execution': {'state': 'running', 'generation': 'generation-fixture', 'session_id': 'session-fixture',
                          'native_title': 'Fixture work', 'approvals': [notice]}}
    adapter, transport = object(), object()
    intake = NS(settings={'enabled': True, 'verification_ref': 'fixture:verified', 'bindings': [binding]},
                snapshot=lambda: {'work': [record]}, transports=[(adapter, transport)], closed=False)
    return intake, adapter, record, notice, binding


def buttons(card):
    return [button for element in card['elements'] for button in element.get('actions', [])]


def test_approval_card_shows_exact_operation_and_only_once_or_reject(pending_card):
    from ghost_hermes_pm.repository_approvals import _approval_card
    _, _, record, notice, _ = pending_card
    card = _approval_card(record, notice)
    actions = buttons(card)

    assert [action['text']['content'] for action in actions] == ['批准一次', '拒绝']
    assert [action['value']['outcome'] for action in actions] == ['allowed-once', 'rejected']
    assert notice['material'] in [element['text']['content'] for element in card['elements'] if 'text' in element]
    assert all(set(action['value']) == {'kind', 'approval_id', 'binding_sha256', 'outcome'} for action in actions)
    assert all(action['value']['approval_id'] == notice['approval_id'] for action in actions)


@pytest.mark.parametrize('state', ['intent', 'queued', 'outcome_unknown', 'allowed', 'completed', 'rejected', 'expired', 'unsafe'])
def test_received_completed_or_expired_approval_card_has_no_active_buttons(pending_card, state):
    from ghost_hermes_pm.repository_approvals import _approval_card
    _, _, record, notice, _ = pending_card
    if state in {'intent', 'queued', 'outcome_unknown'}:
        notice['reply_status'] = state
    elif state in {'allowed', 'completed', 'rejected'}:
        notice.update(state='resolved', outcome='rejected' if state == 'rejected' else 'allowed-once',
                      operation_state='settled' if state == 'completed' else 'failed' if state == 'rejected' else 'pending')
    elif state == 'expired':
        record['dsh_execution']['state'] = 'stopped'
    else:
        notice['answerable'] = False

    card = _approval_card(record, notice)

    assert buttons(card) == []
    text = '\n'.join(element['text']['content'] for element in card['elements'] if 'text' in element)
    if state == 'allowed':
        assert '原审批：已允许本次操作' in text and '原工具：尚未结束' in text
    if state == 'completed':
        assert '原审批：已允许本次操作' in text and '原工具：已结束' in text
    if state == 'outcome_unknown':
        assert '尚未确认' in text


def original_card_payload(context, outcome='allowed-once'):
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
    from ghost_hermes_pm.repository_approvals import _approval_card
    _, _, record, notice, binding = context
    value = next(button['value'] for button in buttons(_approval_card(record, notice)) if button['value']['outcome'] == outcome)
    payload = {'schema': '2.0', 'header': {'event_type': 'card.action.trigger', 'event_id': 'event-card-fixture',
        'app_id': binding['app_id'], 'tenant_key': binding['transport_tenant_key']},
        'event': {'operator': {'open_id': binding['owner_open_id'], 'user_id': 'u_owner',
            'union_id': 'on_owner', 'tenant_key': binding['sender_tenant_key']},
            'context': {'open_message_id': notice['request_message_id'], 'open_chat_id': binding['chat_id']},
            'action': {'tag': 'button', 'value': value}}}
    return json.loads(lark.JSON.marshal(P2CardActionTrigger(payload)))


@pytest.mark.parametrize('outcome', ['allowed-once', 'rejected'])
def test_original_callback_binds_owner_message_and_original_operation(pending_card, outcome):
    from ghost_hermes_pm.repository_approvals import prepare_card_action
    intake, adapter, record, _, _ = pending_card
    prepared = prepare_card_action(intake, original_card_payload(pending_card, outcome), adapter)

    assert prepared.work_id == record['id'] and prepared.action == 'approval_card'
    assert prepared.command == ('批准一次' if outcome == 'allowed-once' else '拒绝') + ' 审批 approval-fixture'
    assert prepared.envelope['parent_id'] == 'om_approval'
    assert prepared.envelope['event_id'] == 'event-card-fixture'


@pytest.mark.parametrize('mismatch', ['owner', 'operator_tenant', 'app', 'transport_tenant', 'chat', 'message',
    'outcome', 'hash', 'extra_identity', 'resolved', 'generation', 'call', 'unsafe', 'text_notice', 'ambiguous'])
def test_wrong_foreign_stale_or_forged_card_actions_cannot_select_operation(pending_card, mismatch):
    from ghost_hermes_pm.repository_approvals import prepare_card_action
    intake, adapter, record, notice, _ = pending_card
    payload = original_card_payload(pending_card)
    event = payload['event']
    if mismatch == 'owner': event['operator']['open_id'] = 'ou_other'
    elif mismatch == 'operator_tenant': event['operator']['tenant_key'] = 'other-tenant'
    elif mismatch == 'app': payload['header']['app_id'] = 'other-app'
    elif mismatch == 'transport_tenant': payload['header']['tenant_key'] = 'other-tenant'
    elif mismatch == 'chat': event['context']['open_chat_id'] = 'oc_other'
    elif mismatch == 'message': event['context']['open_message_id'] = 'om_old'
    elif mismatch == 'outcome': event['action']['value']['outcome'] = 'allowed-always'
    elif mismatch == 'hash': event['action']['value']['binding_sha256'] = 'c' * 64
    elif mismatch == 'extra_identity': event['action']['value']['is_owner'] = True
    elif mismatch == 'resolved': notice['state'] = 'resolved'
    elif mismatch == 'generation': record['dsh_execution']['generation'] = 'new-generation'
    elif mismatch == 'call': notice['call_id'] = 'new-call'
    elif mismatch == 'unsafe': notice['answerable'] = False
    elif mismatch == 'text_notice': notice['request_renderer'] = 'post'
    else:
        other = deepcopy(record)
        intake.snapshot = lambda: {'work': [record, other]}

    assert prepare_card_action(intake, payload, adapter) is None


def test_owned_native_dispatcher_returns_only_verification_toast_and_forwards_original_callback(pending_card):
    from lark_oapi.core.model.raw_request import RawRequest
    from ghost_hermes_pm.owned_feishu_process import _native_event_handler
    payload = original_card_payload(pending_card)
    frames = []
    handler = _native_event_handler(frames.append)
    request = RawRequest()
    request.uri = '/fixture-local-dispatcher'
    request.body = json.dumps(payload).encode()

    response = handler.do(request)

    assert response.status_code == 200
    receipt = json.loads(response.content)
    assert receipt['toast']['type'] == 'info' and '正在核验' in receipt['toast']['content']
    assert '批准' not in receipt['toast']['content'] and receipt.get('card') is None
    assert len(frames) == 1 and frames[0]['kind'] == 'event'
    assert frames[0]['payload']['event']['operator'] == payload['event']['operator']
    assert frames[0]['payload']['event']['context'] == payload['event']['context']
    assert frames[0]['payload']['event']['action']['value'] == payload['event']['action']['value']


def test_same_owned_native_dispatcher_preserves_message_receive_subscription():
    from lark_oapi.core.model.raw_request import RawRequest
    from ghost_hermes_pm.owned_feishu_process import _native_event_handler
    frames = []
    request = RawRequest()
    request.uri = '/fixture-local-dispatcher'
    request.body = json.dumps({'schema': '2.0', 'header': {'event_type': 'im.message.receive_v1',
        'app_id': 'cli_fixture', 'tenant_key': 'tenant-app', 'event_id': 'event-message-fixture'},
        'event': {'sender': {'sender_type': 'user', 'tenant_key': 'tenant-owner',
            'sender_id': {'open_id': 'ou_owner'}}, 'message': {'message_id': 'om_question',
                'message_type': 'text', 'chat_id': 'oc_fixture', 'chat_type': 'group',
                'content': json.dumps({'text': 'Which colour?'})}}}).encode()

    response = _native_event_handler(frames.append).do(request)

    assert response.status_code == 200 and json.loads(response.content) == {'msg': 'success'}
    assert len(frames) == 1 and frames[0]['payload']['header']['event_type'] == 'im.message.receive_v1'


@pytest.mark.asyncio
async def test_confirmed_failed_card_update_retries_after_backoff_but_unknown_does_not(pending_card, monkeypatch):
    import ghost_hermes_pm.repository_approvals as approvals
    intake, _, record, notice, _ = pending_card
    notice['reply_status'] = 'queued'
    now = [100.0]
    monkeypatch.setattr(approvals, 'time', NS(time=lambda: now[0]), raising=False)
    intake.require_active = lambda generation: None
    intake._save_execution = lambda *args, **kwargs: None
    calls = []
    async def update(value):
        calls.append(value)
        return {'status': 'failed', 'code': 503} if len(calls) == 1 else {'status': 'updated', 'code': 0}
    transport = NS(update_card=update)
    await approvals._refresh_approval_card(intake, record, notice, transport, 0)
    await approvals._refresh_approval_card(intake, record, notice, transport, 0)
    assert len(calls) == 1
    now[0] += 60
    await approvals._refresh_approval_card(intake, record, notice, transport, 0)
    assert len(calls) == 2
    await approvals._refresh_approval_card(intake, record, notice, transport, 0)
    assert len(calls) == 2
    notice['card_updates'] = {}
    async def unknown(value):
        calls.append(value)
        return {'status': 'unknown'}
    transport.update_card = unknown
    await approvals._refresh_approval_card(intake, record, notice, transport, 0)
    now[0] += 60
    await approvals._refresh_approval_card(intake, record, notice, transport, 0)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_lifecycle_end_during_card_patch_never_submits_native_approval(pending_card, tmp_path, monkeypatch):
    import ghost_hermes_pm.repository_approvals as approvals
    import ghost_hermes_pm.repository_supervision as supervision
    from ghost_hermes_pm.manager import ManagementError
    intake, _, record, notice, binding = pending_card
    operation = {'command': 'printf fixture', 'sandbox_permissions': 'workspace-write'}
    notice.update(turn_id='turn-fixture', event_id='event-native',
        command_sha256=hashlib.sha256(operation['command'].encode()).hexdigest(),
        arguments_sha256=hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest())
    record['target']['repo_path'] = str(tmp_path)
    intake.lock = asyncio.Lock()
    intake.lock_path = tmp_path / 'approval.lock'
    intake.lock_path.touch(mode=0o600)
    intake._verified_card = lambda *args: record['card_id']
    intake._save_execution = lambda *args, **kwargs: None
    active = [True]
    def check(generation):
        if not active[0]: raise ManagementError('unavailable', 'Owner lifecycle ended.')
    intake.require_active = check
    calls = []
    def request(*args):
        calls.append(args)
        return {'type': 'server-response', 'rpcId': args[-1], 'result': {'ok': True}}
    carrier = NS(request=request, close=lambda: None, event_frames=lambda: {
        'session_id': notice['session_id'], 'generation': notice['generation'], 'client_id': 'client-fixture',
        'frames': [{'eventId': notice['event_id'], 'event': 'approval/request', 'agentId': notice['session_id'], 'request': {}}]})
    async def update(value):
        active[0] = False
        return {'status': 'updated', 'code': 0}
    async def send(value):
        raise AssertionError('No message may be sent after the owner lifecycle ends.')
    transport = NS(update_card=update, send=send)
    monkeypatch.setattr(supervision, 'attach_existing_owned', lambda *args: carrier)
    monkeypatch.setattr(supervision, 'original_history', lambda *args: ([], {}))
    monkeypatch.setattr(approvals, '_source', lambda *args: ({'id': 'turn-fixture', 'startSeq': 0}, {'state': 'pending'}, operation))
    prepared = NS(action='approval', rejected=None, command='批准一次', work_id=record['id'], transport=transport,
                  envelope={**binding, 'parent_id': 'om_approval', 'message_id': 'om_owner'})

    with pytest.raises(ManagementError):
        await approvals.process_approval_reply(intake, prepared, 0)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status,closed,expected', [('failed', False, 1), ('unknown', False, 0), ('failed', True, 0)])
async def test_released_work_repairs_only_confirmed_failed_card_notification(pending_card, monkeypatch, tmp_path, status, closed, expected):
    from ghost_hermes_pm import repository_supervision as supervision
    from ghost_hermes_pm.repository_approvals import _approval_card
    from ghost_hermes_pm.manager import ManagementError
    intake, _, record, notice, binding = pending_card
    adapter = NS(bindings=[binding])
    record['source_anchor'] = {**notice['delivery'], 'message_id': 'om_task_start'}
    record['dsh_execution'].update(state='released', native_identity={'pid': 999})
    notice.update(state='resolved', outcome='allowed-once', operation_state='settled', reply_status='accepted')
    digest = hashlib.sha256(json.dumps(_approval_card(record, notice), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    notice['card_updates'] = {digest: status}
    notice['card_update_retries'] = {digest: {'attempts': 1, 'not_before': time.time() - 1}}
    intake.generation, intake.closed, intake.lock = 0, closed, asyncio.Lock()
    intake.lock_path = tmp_path / 'intake.lock'
    intake.lock_path.write_bytes(b'')
    intake._save_execution = lambda *args, **kwargs: None
    def active(generation):
        if intake.closed:
            raise ManagementError('lifecycle_changed', 'Fixture owner unloaded.')
    intake.require_active = active
    calls = []
    async def verify(recipient):
        return {'app_id': binding['app_id'], 'open_id': binding['recipient_open_id']}
    async def update(segment):
        calls.append(segment)
        return {'status': 'updated', 'code': 0}
    intake.transports = [(adapter, NS(verify_identity=verify, update_card=update))]
    def no_native(*args, **kwargs):
        raise AssertionError('Released notifications cannot attach native execution or submit a decision.')
    monkeypatch.setattr(supervision, 'attach_existing_owned', no_native)
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID', raising=False)
    task = asyncio.create_task(supervision.observe_human_requests(intake))
    try:
        await asyncio.sleep(.3)
    finally:
        intake.closed = True
        await asyncio.wait_for(task, 1)
    assert len(calls) == expected
    if expected:
        assert calls[0]['message_id'] == 'om_approval' and buttons(calls[0]['card']) == []
        assert notice['card_updates'][digest] == 'updated'
    else:
        assert notice['card_updates'][digest] == status


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['cancel', 'unload'])
async def test_released_card_lock_wait_closes_descriptor_when_owner_stops(tmp_path, stop):
    import fcntl
    import os
    from ghost_hermes_pm.repository_approvals import refresh_released_approval_cards
    from ghost_hermes_pm.manager import ManagementError
    path = tmp_path / 'intake.lock'
    path.touch()
    stat = path.stat()
    def matching_descriptors():
        descriptors = set()
        for descriptor in range(4096):
            try:
                current = os.fstat(descriptor)
            except OSError:
                continue
            if (current.st_dev, current.st_ino) == (stat.st_dev, stat.st_ino):
                descriptors.add(descriptor)
        return descriptors
    holder = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = {'id': 'fixture-work', 'dsh_execution': {'state': 'released', 'approvals': [{'state': 'resolved'}]}}
    intake = NS(lock=asyncio.Lock(), lock_path=path, closed=False, snapshot=lambda: {'work': [record]})
    def active(generation):
        if intake.closed:
            raise ManagementError('lifecycle_changed', 'Fixture owner unloaded.')
    intake.require_active = active
    task = asyncio.create_task(refresh_released_approval_cards(intake, record, 0))
    opened = set()
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while asyncio.get_running_loop().time() < deadline:
            opened = matching_descriptors() - {holder}
            if opened:
                break
            await asyncio.sleep(.01)
        assert len(opened) == 1 and intake.lock.locked() and not task.done()
        if stop == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        else:
            intake.closed = True
            with pytest.raises(ManagementError, match='unloaded'):
                await asyncio.wait_for(task, .5)
        assert matching_descriptors() == {holder}
        assert not intake.lock.locked()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(.1)
        for descriptor in opened & matching_descriptors():
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
